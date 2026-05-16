from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_TITLE = "PlatformIO 离线安装程序"


def resource_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class StepInfo:
    title: str
    weight: int


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_dir = resource_root()
        self.root.title(APP_TITLE)
        self.root.geometry("820x560")
        self.root.resizable(False, False)

        bundle_root = self.base_dir / "r"
        if not bundle_root.exists():
            bundle_root = self.base_dir / "resources"

        self.runtime_python = bundle_root / "rt" / "python" / "python.exe"
        if not self.runtime_python.exists():
            self.runtime_python = bundle_root / "runtime" / "python" / "python.exe"

        self.source_pio_root = bundle_root / "p" / ".platformio"
        if not self.source_pio_root.exists():
            self.source_pio_root = bundle_root / "pio-data" / ".platformio"

        self.vsix_dir = bundle_root / "vx"
        if not self.vsix_dir.exists():
            self.vsix_dir = bundle_root / "vsix"

        self.patch_dir = bundle_root / "pt"
        if not self.patch_dir.exists():
            self.patch_dir = bundle_root / "patch"

        self.vscode_dir = bundle_root / "vc"
        if not self.vscode_dir.exists():
            self.vscode_dir = bundle_root / "vscode"

        self.user_home = Path.home()
        self.user_pio_root = self.user_home / ".platformio"
        self.user_vscode_dir = self.user_home / "AppData" / "Local" / "Programs" / "Microsoft VS Code"
        self.user_code_settings = Path(os.environ["APPDATA"]) / "Code" / "User" / "settings.json"
        self.user_vscode_extensions = self.user_home / ".vscode" / "extensions"

        self.log_path = self.base_dir / "install.log"
        self.portable_scripts: Path | None = None
        self.site_packages: Path | None = None
        self.code_cli: Path | None = None
        self.installed_vscode: bool = False

        self.steps = [
            StepInfo("检查安装资源", 6),
            StepInfo("复制 PlatformIO Core", 28),
            StepInfo("修复命令包装器", 14),
            StepInfo("定位或安装 VS Code", 18),
            StepInfo("写入 VS Code 配置", 10),
            StepInfo("关闭 telemetry", 6),
            StepInfo("安装离线扩展", 12),
            StepInfo("应用离线补丁", 6),
        ]
        self.total_weight = sum(step.weight for step in self.steps)
        self.current_step_index = -1
        self.current_step_start = 0
        self.current_step_weight = 0

        self.status_var = tk.StringVar(value="准备安装")
        self.detail_var = tk.StringVar(value="安装器启动后会自动开始离线部署。")
        self.summary_var = tk.StringVar(value="正在初始化安装器")
        self.install_started = False

        self._build_ui()
        self.root.after(250, self.start_install)

    def _build_ui(self):
        root = self.root

        header = ttk.Frame(root, padding=(18, 18, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="PlatformIO 离线安装", font=("Microsoft YaHei", 16, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="自动部署离线 PlatformIO Core、VS Code 扩展和本地修补文件。",
            foreground="#666666",
        ).pack(anchor="w", pady=(4, 0))

        status = ttk.LabelFrame(root, text="安装状态", padding=(16, 12))
        status.pack(fill="x", padx=18, pady=(0, 10))

        ttk.Label(status, textvariable=self.status_var, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        ttk.Label(status, textvariable=self.detail_var, foreground="#3f6288").pack(anchor="w", pady=(6, 0))

        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 6))
        ttk.Label(status, textvariable=self.summary_var, foreground="#666666").pack(anchor="w")

        self.step_list = tk.Listbox(root, height=len(self.steps), activestyle="none", exportselection=False)
        self.step_list.pack(fill="x", padx=18)
        for idx, step in enumerate(self.steps, start=1):
            self.step_list.insert("end", f"{idx}. {step.title}")
        self.step_list.configure(state="disabled", bg="#fafafa")

        log_frame = ttk.LabelFrame(root, text="安装日志", padding=(10, 10))
        log_frame.pack(fill="both", expand=True, padx=18, pady=10)
        self.log_text = tk.Text(log_frame, height=16, wrap="word", state="disabled", bg="#f7f7f7")
        self.log_text.pack(fill="both", expand=True)

        btn_frame = ttk.Frame(root, padding=(18, 0, 18, 16))
        btn_frame.pack(fill="x")
        self.open_log_btn = ttk.Button(btn_frame, text="打开日志", command=self.open_log)
        self.open_log_btn.pack(side="left")
        self.exit_btn = ttk.Button(btn_frame, text="关闭", command=root.destroy)
        self.exit_btn.pack(side="right")

    def log(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_step_visual(self, index: int):
        self.step_list.configure(state="normal")
        self.step_list.selection_clear(0, "end")
        if index >= 0:
            self.step_list.selection_set(index)
            self.step_list.activate(index)
            self.step_list.see(index)
        self.step_list.configure(state="disabled")

    def begin_step(self, index: int, detail: str):
        prior_weight = sum(step.weight for step in self.steps[:index])
        self.current_step_index = index
        self.current_step_start = prior_weight
        self.current_step_weight = self.steps[index].weight
        self.status_var.set(f"第 {index + 1}/{len(self.steps)} 步：{self.steps[index].title}")
        self.detail_var.set(detail)
        self.summary_var.set(f"总进度 {round(prior_weight * 100 / self.total_weight)}%")
        self._set_step_visual(index)
        self._set_progress_absolute(prior_weight)

    def update_step_progress(self, fraction: float, detail: str | None = None):
        fraction = min(max(fraction, 0.0), 1.0)
        absolute = self.current_step_start + self.current_step_weight * fraction
        if detail:
            self.detail_var.set(detail)
        self.summary_var.set(f"总进度 {round(absolute * 100 / self.total_weight)}%")
        self._set_progress_absolute(absolute)

    def _set_progress_absolute(self, value: float):
        percent = round(min(max(value, 0), self.total_weight) * 100 / self.total_weight)
        self.progress.configure(value=percent)
        self.root.update_idletasks()

    def open_log(self):
        if self.log_path.exists():
            os.startfile(str(self.log_path))

    def start_install(self):
        if self.install_started:
            return
        self.install_started = True
        self.open_log_btn.configure(state="disabled")
        self.log_path.write_text("", encoding="utf-8")
        self.log("安装任务开始")
        threading.Thread(target=self.run_install, daemon=True).start()

    def run_install(self):
        try:
            self.step_check_resources()
            self.step_copy_platformio()
            self.step_prepare_wrappers()
            self.step_ensure_vscode()
            self.step_write_settings()
            self.step_disable_telemetry()
            self.step_install_vsix()
            self.step_apply_patch()
            self.root.after(0, self.finish_success)
        except Exception as exc:
            self.root.after(0, lambda err=exc: self.finish_error(err))

    def _safe_glob_first(self, folder: Path, pattern: str) -> Path:
        matches = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            raise RuntimeError(f"未找到资源：{folder}\\{pattern}")
        return matches[0]

    def _run_command(
        self,
        args: list[str],
        *,
        check: bool = False,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        log_command: bool = True,
        success_message: str | None = None,
        suppress_stdout: bool = False,
        suppress_stderr: bool = False,
    ):
        if log_command:
            self.log("执行命令: " + " ".join(f'"{a}"' if " " in a else a for a in args))
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd) if cwd else None,
        )
        stdout_text = proc.stdout.strip()
        stderr_text = proc.stderr.strip()
        if stdout_text and not suppress_stdout:
            self.log(proc.stdout.strip())
        if stderr_text and not suppress_stderr:
            self.log(proc.stderr.strip())
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令执行失败（{proc.returncode}）：{' '.join(args)}")
        if proc.returncode == 0 and success_message:
            self.log(success_message)
        return proc

    def _find_existing_code_cli(self) -> Path | None:
        candidates: list[Path] = []

        found = shutil.which("code")
        if found:
            candidates.append(Path(found))

        local_app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
        candidates.append(local_app)
        candidates.append(self.user_vscode_dir / "bin" / "code.cmd")

        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    def _install_vscode_if_needed(self):
        if self.code_cli:
            return

        vscode_setup = self._safe_glob_first(self.vscode_dir, "VSCodeUserSetup-*.exe")
        self.log(f"未检测到 VS Code CLI，尝试静默安装 VS Code：{vscode_setup.name}")
        self.update_step_progress(0.2, "未找到 VS Code，正在离线安装 VS Code")

        # /MERGETASKS=!runcode keeps the installer quiet and avoids auto-launch.
        args = [
            str(vscode_setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/MERGETASKS=!runcode,addcontextmenufiles,addcontextmenufolders",
        ]
        proc = self._run_command(args)
        if proc.returncode not in (0, 1641, 3010):
            raise RuntimeError("VS Code 离线安装失败，请查看安装日志。")

        self.installed_vscode = True
        self.update_step_progress(0.7, "VS Code 安装完成，正在定位 code.cmd")
        self.code_cli = self._find_existing_code_cli()
        if not self.code_cli:
            raise RuntimeError("VS Code 已安装，但仍未找到 code.cmd。")

    def step_check_resources(self):
        self.begin_step(0, "正在检查安装器内置资源")
        required = [
            self.runtime_python,
            self.source_pio_root,
            self.vsix_dir,
            self.patch_dir,
            self.vscode_dir,
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError("缺少必要资源：\n" + "\n".join(missing))

        self._safe_glob_first(self.vsix_dir, "platformio-ide-*-offline.vsix")
        self._safe_glob_first(self.vsix_dir, "cpptools-*.vsix")
        self._safe_glob_first(self.vscode_dir, "VSCodeUserSetup-*.exe")
        self.update_step_progress(1.0, "资源检查完成")
        self.log("资源检查完成")

    def step_copy_platformio(self):
        self.begin_step(1, "正在复制 PlatformIO Core 到用户目录")
        self.log(f"复制 {self.source_pio_root} -> {self.user_pio_root}")
        backup = self.user_home / f".platformio.backup-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if self.user_pio_root.exists():
            self.log(f"备份现有 .platformio 到 {backup}")
            self.update_step_progress(0.15, "检测到现有 .platformio，正在备份")
            shutil.move(str(self.user_pio_root), str(backup))

        self.update_step_progress(0.3, "正在复制 PlatformIO 核心文件")
        shutil.copytree(self.source_pio_root, self.user_pio_root)
        self.update_step_progress(1.0, "PlatformIO Core 复制完成")

    def step_prepare_wrappers(self):
        self.begin_step(2, "正在生成稳定的命令包装器")
        self.log("生成 portable-bin\\pio.cmd / platformio.cmd")

        portable_scripts = self.user_pio_root / "portable-bin"
        portable_scripts.mkdir(parents=True, exist_ok=True)
        site_packages = self.user_pio_root / "penv" / "Lib" / "site-packages"
        penv_python = self.user_pio_root / "penv" / "Scripts" / "python.exe"
        penv_pio = self.user_pio_root / "penv" / "Scripts" / "platformio.exe"
        builtin_python_dir = self.user_pio_root / "python3"

        self.update_step_progress(0.2, "正在复制内置 Python 运行时")
        if builtin_python_dir.exists():
            shutil.rmtree(builtin_python_dir, ignore_errors=True)
        shutil.copytree(self.runtime_python.parent, builtin_python_dir)

        self.update_step_progress(0.6, "正在写入 pio.cmd 和 platformio.cmd")
        if penv_pio.exists():
            cmd_body = (
                "@echo off\r\n"
                f'set "PLATFORMIO_CORE_DIR={self.user_pio_root}"\r\n'
                f'set "PLATFORMIO_PACKAGES_DIR={self.user_pio_root / "packages"}"\r\n'
                f'set "PLATFORMIO_PLATFORMS_DIR={self.user_pio_root / "platforms"}"\r\n'
                f'set "PYTHONPATH={site_packages}"\r\n'
                f'"{penv_pio}" %*\r\n'
            )
        else:
            cmd_body = (
                "@echo off\r\n"
                f'set "PLATFORMIO_CORE_DIR={self.user_pio_root}"\r\n'
                f'set "PLATFORMIO_PACKAGES_DIR={self.user_pio_root / "packages"}"\r\n'
                f'set "PLATFORMIO_PLATFORMS_DIR={self.user_pio_root / "platforms"}"\r\n'
                f'set "PYTHONPATH={site_packages}"\r\n'
                f'"{penv_python}" -m platformio %*\r\n'
            )

        for name in ("pio.cmd", "platformio.cmd"):
            (portable_scripts / name).write_text(cmd_body, encoding="ascii")

        self.portable_scripts = portable_scripts
        self.site_packages = site_packages
        self.update_step_progress(1.0, "命令包装器生成完成")

    def step_ensure_vscode(self):
        self.begin_step(3, "正在定位 VS Code 安装环境")
        self.code_cli = self._find_existing_code_cli()
        if self.code_cli:
            self.log(f"已找到 VS Code CLI: {self.code_cli}")
            self.update_step_progress(1.0, "已找到可用的 VS Code CLI")
            return

        self._install_vscode_if_needed()
        self.log(f"VS Code CLI 就绪: {self.code_cli}")
        self.update_step_progress(1.0, "VS Code 已就绪")

    def step_write_settings(self):
        self.begin_step(4, "正在写入 VS Code 配置")
        if not self.portable_scripts or not self.site_packages:
            raise RuntimeError("命令包装器未初始化。")

        self.user_code_settings.parent.mkdir(parents=True, exist_ok=True)
        self.update_step_progress(0.3, "正在生成 settings.json")

        existing_settings: dict[str, object] = {}
        if self.user_code_settings.exists():
            try:
                existing_settings = json.loads(self.user_code_settings.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.log("现有 settings.json 不是合法 JSON，将使用新配置覆盖。")

        custom_path = str(self.portable_scripts)
        existing_settings.update(
            {
                "update.mode": "none",
                "extensions.autoUpdate": False,
                "telemetry.telemetryLevel": "off",
                "workbench.enableExperiments": False,
                "platformio-ide.disablePIOHomeStartup": True,
                "platformio-ide.useBuiltinPython": False,
                "platformio-ide.useBuiltinPIOCore": False,
                "platformio-ide.customPATH": custom_path,
            }
        )
        self.user_code_settings.write_text(json.dumps(existing_settings, ensure_ascii=False, indent=2), encoding="utf-8")

        self.update_step_progress(0.75, "正在写入 PlatformIO 环境变量")
        os.environ["PLATFORMIO_CORE_DIR"] = str(self.user_pio_root)
        os.environ["PLATFORMIO_PACKAGES_DIR"] = str(self.user_pio_root / "packages")
        os.environ["PLATFORMIO_PLATFORMS_DIR"] = str(self.user_pio_root / "platforms")
        os.environ["PLATFORMIO_PATH"] = str(self.portable_scripts)
        os.environ["PYTHONPATH"] = str(self.site_packages)

        env_pairs = {
            "PLATFORMIO_CORE_DIR": str(self.user_pio_root),
            "PLATFORMIO_PACKAGES_DIR": str(self.user_pio_root / "packages"),
            "PLATFORMIO_PLATFORMS_DIR": str(self.user_pio_root / "platforms"),
            "PLATFORMIO_PATH": str(self.portable_scripts),
            "PYTHONPATH": str(self.site_packages),
        }
        for key, value in env_pairs.items():
            self._run_command(
                ["setx", key, value],
                log_command=False,
                suppress_stdout=True,
                suppress_stderr=True,
                success_message=f"已写入环境变量: {key}",
            )

        self.update_step_progress(1.0, "VS Code 配置写入完成")

    def step_disable_telemetry(self):
        self.begin_step(5, "正在关闭 PlatformIO telemetry")
        if not self.site_packages:
            raise RuntimeError("PlatformIO 运行环境未初始化。")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.site_packages)
        env["PLATFORMIO_CORE_DIR"] = str(self.user_pio_root)
        env["PLATFORMIO_PACKAGES_DIR"] = str(self.user_pio_root / "packages")
        env["PLATFORMIO_PLATFORMS_DIR"] = str(self.user_pio_root / "platforms")

        self.update_step_progress(0.35, "正在执行 PlatformIO 设置命令")
        proc = self._run_command(
            [str(self.user_pio_root / "penv" / "Scripts" / "python.exe"), "-m", "platformio", "settings", "set", "enable_telemetry", "No"],
            env=env,
            suppress_stdout=True,
        )
        if proc.returncode != 0:
            self.log("关闭 telemetry 时返回非 0，继续安装，但建议复查日志。")
        else:
            self.log("已关闭 PlatformIO telemetry")
        self.update_step_progress(1.0, "telemetry 设置完成")

    def step_install_vsix(self):
        self.begin_step(6, "正在安装离线扩展")
        if not self.code_cli:
            raise RuntimeError("VS Code CLI 不可用，无法安装扩展。")

        pio_vsix = self._safe_glob_first(self.vsix_dir, "platformio-ide-*-offline.vsix")
        cpp_vsix = self._safe_glob_first(self.vsix_dir, "cpptools-*.vsix")
        vsix_items = [pio_vsix, cpp_vsix]

        for idx, vsix in enumerate(vsix_items, start=1):
            fraction = (idx - 1) / len(vsix_items)
            self.update_step_progress(fraction, f"正在安装扩展 {idx}/{len(vsix_items)}：{vsix.name}")
            proc = self._run_command([str(self.code_cli), "--install-extension", str(vsix), "--force"])
            if proc.returncode != 0 and "cpptools" not in vsix.name:
                raise RuntimeError(f"安装扩展失败: {vsix.name}")

        self.update_step_progress(1.0, "离线扩展安装完成")

    def step_apply_patch(self):
        self.begin_step(7, "正在应用 PlatformIO 本地补丁")
        self.user_vscode_extensions.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            self.user_vscode_extensions.glob("platformio.platformio-ide-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("未找到已安装的 PlatformIO 扩展目录。")

        target = candidates[0]
        self.log(f"补丁目标: {target}")
        self.update_step_progress(0.4, "正在覆盖 extension.js")
        shutil.copy2(self.patch_dir / "extension.js", target / "dist" / "extension.js")
        shutil.copy2(self.patch_dir / "extension.js.map", target / "dist" / "extension.js.map")
        self.update_step_progress(0.8, "正在覆盖 package.json")
        shutil.copy2(self.patch_dir / "package.json", target / "package.json")
        self.update_step_progress(1.0, "补丁应用完成")

    def finish_success(self):
        self.current_step_index = len(self.steps) - 1
        self._set_step_visual(self.current_step_index)
        self._set_progress_absolute(self.total_weight)
        self.status_var.set("安装完成")
        self.detail_var.set("离线 PlatformIO 环境已经安装完成。")
        restart_tip = "请重启 VS Code 后再打开 PlatformIO 工程。"
        if self.installed_vscode:
            restart_tip = "VS Code 已完成离线安装，请先启动一次 VS Code，再打开 PlatformIO 工程。"
        self.summary_var.set(restart_tip)
        self.log("安装完成")
        messagebox.showinfo("安装完成", restart_tip)
        self.open_log_btn.configure(state="normal")

    def finish_error(self, err: Exception):
        self.log(f"[ERROR] {err}")
        self.status_var.set("安装失败")
        self.detail_var.set("请查看日志并重新运行安装程序。")
        self.summary_var.set(str(err))
        messagebox.showerror("安装失败", str(err))
        self.install_started = False
        self.open_log_btn.configure(state="normal")


def main() -> int:
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

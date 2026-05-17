# -*- coding: utf-8 -*-
"""
PlatformIO 离线部署管理工具

面向“已具备 PlatformIO 基础环境，本工具仅分发离线 SDK”的场景：
1. 扫描本机 .platformio/platforms 与 .platformio/packages
2. 按“基平台 + 版本明细”展示
3. 打包选中的平台版本及依赖工具/框架
4. 安装离线 SDK 到当前用户 .platformio
5. 清理选中的平台版本并调用官方 prune
"""

from __future__ import annotations

import configparser
import ctypes
import json
import os
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml


def show_startup_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "PlatformIO 离线部署管理工具", 0x10)
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


try:
    enable_dpi_awareness()
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:
    error_text = (
        "启动失败：无法加载 tkinter 图形界面模块。\n\n"
        "常见原因：\n"
        "1. 虚拟机中的 Python 没安装 tkinter\n"
        "2. Python 安装不完整\n"
        "3. 正在使用精简版/嵌入式 Python\n\n"
        f"原始错误：{exc}"
    )
    show_startup_error(error_text)
    raise


def resource_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


APP_BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
RESOURCE_ROOT = resource_root()
BUNDLE_ROOT = RESOURCE_ROOT / "r" if (RESOURCE_ROOT / "r").exists() else APP_BASE_DIR / "resources"
ENV_DIR = APP_BASE_DIR / "env"
TOOLS_DIR = BUNDLE_ROOT / "tl" if (BUNDLE_ROOT / "tl").exists() else BUNDLE_ROOT / "tools"
RESOURCES_DIR = APP_BASE_DIR / "resources"
SDK_DIR = (APP_BASE_DIR / "sdk") if getattr(sys, "frozen", False) else (RESOURCES_DIR / "sdk")
SDK_BUNDLED_DIR = BUNDLE_ROOT / "sdk"
SEVEN_ZIP = TOOLS_DIR / "7z.exe"
PIO_ROOT = Path.home() / ".platformio"
PLATFORMS_DIR = PIO_ROOT / "platforms"
PACKAGES_DIR = PIO_ROOT / "packages"
PENV_PIO = PIO_ROOT / "penv" / "Scripts" / "pio.exe"
MANIFEST_FILE = "sdk-manifest.json"
ERROR_LOG = APP_BASE_DIR / "pio_gui_error.log"
DEPENDENCY_MANIFEST_PATH = BUNDLE_ROOT / "dependencies" / "platformio-deps.yml"
DEPENDENCY_LEVEL_LABELS = {
    "required": "必需",
    "warning": "建议",
    "optional": "可选",
    "ignored": "忽略",
}
LOW_PRIORITY_OPTIONAL_PACKAGE_NAMES = {
    "framework-arduino-c2-skeleton-lib",
    "tool-dfuutil-arduino",
    "tool-cppcheck",
    "tool-clangtidy",
    "tool-pvs-studio",
}

ENV_DIR.mkdir(exist_ok=True)
SDK_DIR.mkdir(parents=True, exist_ok=True)
if not getattr(sys, "frozen", False):
    SDK_BUNDLED_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class PlatformEntry:
    base_name: str
    version_name: str
    platform_dir_name: str
    platform_dir: Path
    platform_version: str
    title: str
    packages: List[str] = field(default_factory=list)
    package_dirs: List[str] = field(default_factory=list)
    required_specs: List[str] = field(default_factory=list)
    source: str = ""
    size_mb: float = 0.0


@dataclass
class ProjectEnvConfig:
    env_name: str
    platform_spec: str
    frameworks: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class StepInfo:
    title: str
    weight: int


_dependency_manifest_cache: Optional[dict] = None


def normalize_dependency_level(level: str) -> str:
    text = (level or "").strip().lower()
    if text in {"required", "must", "mandatory"}:
        return "required"
    if text in {"warning", "recommended", "suggested"}:
        return "warning"
    if text in {"optional", "info", "nice-to-have"}:
        return "optional"
    if text in {"ignored", "skip", "hidden"}:
        return "ignored"
    return "optional"


def load_dependency_manifest() -> dict:
    global _dependency_manifest_cache
    if _dependency_manifest_cache is not None:
        return _dependency_manifest_cache
    if not DEPENDENCY_MANIFEST_PATH.exists():
        _dependency_manifest_cache = {}
        return _dependency_manifest_cache
    try:
        data = yaml.safe_load(DEPENDENCY_MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RuntimeError(f"读取依赖清单失败：{DEPENDENCY_MANIFEST_PATH} ({exc})")
    if not isinstance(data, dict):
        raise RuntimeError(f"依赖清单格式错误：{DEPENDENCY_MANIFEST_PATH}")
    _dependency_manifest_cache = data
    return _dependency_manifest_cache


def get_package_catalog() -> dict:
    manifest = load_dependency_manifest()
    catalog = manifest.get("package_catalog") or {}
    return catalog if isinstance(catalog, dict) else {}


def get_package_purpose(package_name: str) -> str:
    catalog = get_package_catalog()
    package_info = catalog.get(package_name)
    if isinstance(package_info, dict):
        return str(package_info.get("purpose", "")).strip()
    return ""


def get_platform_dependency_versions(base_name: str) -> List[dict]:
    manifest = load_dependency_manifest()
    platforms = manifest.get("platforms") or {}
    if not isinstance(platforms, dict):
        return []
    platform_info = platforms.get(base_name) or {}
    if not isinstance(platform_info, dict):
        return []
    versions = platform_info.get("versions") or []
    if not isinstance(versions, list):
        return []
    return [item for item in versions if isinstance(item, dict)]


def match_dependency_version_entry(base_name: str, platform_dir_name: str, version_name: str, platform_version: str) -> Optional[dict]:
    candidates = get_platform_dependency_versions(base_name)
    if not candidates:
        return None

    norm_platform_version = normalize_numeric_version_text(platform_version)
    norm_version_name = normalize_numeric_version_text(version_name)

    def score(entry: dict) -> int:
        value = 0
        entry_dir_name = str(entry.get("platform_dir_name", "")).strip()
        entry_version = str(entry.get("platform_version", "")).strip()
        if entry_dir_name and entry_dir_name == platform_dir_name:
            value += 8
        if entry_dir_name and entry_dir_name == version_name:
            value += 4
        if entry_version and entry_version == platform_version:
            value += 6
        if norm_platform_version and normalize_numeric_version_text(entry_version) == norm_platform_version:
            value += 5
        if norm_version_name and normalize_numeric_version_text(entry_version) == norm_version_name:
            value += 3
        return value

    scored = sorted(((score(entry), entry) for entry in candidates), key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return None


def iter_dependency_items(entry: Optional[dict]) -> List[dict]:
    if not isinstance(entry, dict):
        return []
    dependencies = entry.get("dependencies") or []
    if not isinstance(dependencies, list):
        return []
    results: List[dict] = []
    for item in dependencies:
        if isinstance(item, str):
            name = item.strip()
            if name:
                results.append({"name": name, "level": "optional", "frameworks": []})
            continue
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            item_frameworks = item.get("frameworks") or []
            normalized_frameworks: List[str] = []
            if isinstance(item_frameworks, list):
                for framework_name in item_frameworks:
                    framework_text = str(framework_name).strip().lower()
                    if framework_text and framework_text not in normalized_frameworks:
                        normalized_frameworks.append(framework_text)
            results.append(
                {
                    "name": name,
                    "level": normalize_dependency_level(str(item.get("level", "optional"))),
                    "frameworks": normalized_frameworks,
                }
            )
    return results


def get_dependency_level_map(entry: Optional[dict], package_map: dict, frameworks: Optional[List[str]] = None) -> Dict[str, str]:
    level_map: Dict[str, str] = {}
    active_frameworks = [item.lower() for item in (frameworks or []) if item.strip()]
    for item in iter_dependency_items(entry):
        item_frameworks = item.get("frameworks") or []
        if item_frameworks and not any(name in active_frameworks for name in item_frameworks):
            continue
        level_map[item["name"]] = item["level"]

    for package_name, package_info in package_map.items():
        if package_name in level_map:
            continue
        optional = bool((package_info or {}).get("optional", False))
        if optional and package_name in LOW_PRIORITY_OPTIONAL_PACKAGE_NAMES:
            level_map[package_name] = "ignored"
        elif optional:
            level_map[package_name] = "optional"
        else:
            level_map[package_name] = "required"
    return level_map


def format_dependency_label(package_name: str, level: str) -> str:
    purpose = get_package_purpose(package_name)
    level_label = DEPENDENCY_LEVEL_LABELS.get(level, "")
    if purpose and level_label:
        return f"{package_name}（{level_label}：{purpose}）"
    if purpose:
        return f"{package_name}（{purpose}）"
    if level_label:
        return f"{package_name}（{level_label}）"
    return package_name


def split_csv_like(value: str) -> List[str]:
    if not value:
        return []
    parts: List[str] = []
    for chunk in re.split(r"[\r\n,]+", value):
        text = chunk.strip()
        if text:
            parts.append(text)
    return parts


def normalize_numeric_version_text(version_text: str) -> str:
    digits = re.findall(r"\d+", version_text or "")
    if not digits:
        return ""
    return ".".join(str(int(part)) for part in digits)


def parse_version_tuple(version_text: str) -> Optional[tuple]:
    digits = re.findall(r"\d+", version_text or "")
    if not digits:
        return None
    return tuple(int(part) for part in digits)


def compare_version_tuples(left: tuple, right: tuple) -> int:
    width = max(len(left), len(right))
    lval = left + (0,) * (width - len(left))
    rval = right + (0,) * (width - len(right))
    if lval < rval:
        return -1
    if lval > rval:
        return 1
    return 0


def bump_version_for_tilde(base: tuple, component_count: int) -> tuple:
    if component_count <= 1:
        index = 0
    else:
        index = 1
    values = list(base)
    while len(values) <= index:
        values.append(0)
    values[index] += 1
    for i in range(index + 1, len(values)):
        values[i] = 0
    return tuple(values)


def bump_version_for_caret(base: tuple) -> tuple:
    values = list(base)
    if not values:
        return (1,)
    values[0] += 1
    for i in range(1, len(values)):
        values[i] = 0
    return tuple(values)


def is_remote_package_spec(spec: str) -> bool:
    text = (spec or "").strip().lower()
    return (
        "://" in text
        or text.startswith("git+")
        or text.endswith(".zip")
        or text.endswith(".tar.gz")
        or text.endswith(".tgz")
    )


def split_semver_constraints(spec: str) -> List[str]:
    text = (spec or "").strip()
    if not text:
        return []
    tokens = [part.strip() for part in text.split(",") if part.strip()]
    if len(tokens) == 1 and " " in tokens[0]:
        tokens = [part.strip() for part in tokens[0].split(" ") if part.strip()]
    return tokens


def match_single_constraint(version: tuple, constraint: str) -> bool:
    token = constraint.strip()
    if not token:
        return True

    if token.startswith("~"):
        raw = token[1:].strip()
        lower = parse_version_tuple(raw)
        if not lower:
            return False
        upper = bump_version_for_tilde(lower, len([part for part in raw.split(".") if part.strip() != ""]))
        return compare_version_tuples(version, lower) >= 0 and compare_version_tuples(version, upper) < 0

    if token.startswith("^"):
        raw = token[1:].strip()
        lower = parse_version_tuple(raw)
        if not lower:
            return False
        upper = bump_version_for_caret(lower)
        return compare_version_tuples(version, lower) >= 0 and compare_version_tuples(version, upper) < 0

    for op in (">=", "<=", ">", "<", "=="):
        if token.startswith(op):
            raw = token[len(op):].strip()
            target = parse_version_tuple(raw)
            if not target:
                return False
            cmp = compare_version_tuples(version, target)
            if op == ">=":
                return cmp >= 0
            if op == "<=":
                return cmp <= 0
            if op == ">":
                return cmp > 0
            if op == "<":
                return cmp < 0
            return cmp == 0

    target = parse_version_tuple(token)
    if not target:
        return False
    return compare_version_tuples(version, target) == 0


def version_satisfies_spec(installed_version: str, spec: str) -> bool:
    if is_remote_package_spec(spec):
        return True
    version = parse_version_tuple(installed_version)
    if not version:
        return False
    constraints = split_semver_constraints(spec)
    if not constraints:
        return True
    return all(match_single_constraint(version, constraint) for constraint in constraints)


def read_package_manifest_version(pkg_dir_name: str) -> str:
    pkg_dir = PACKAGES_DIR / pkg_dir_name
    manifest_path = pkg_dir / "package.json"
    if manifest_path.exists():
        try:
            manifest = load_json(manifest_path)
            version = str(manifest.get("version", "")).strip()
            if version:
                return version
        except Exception:
            pass

    if "@" in pkg_dir_name:
        tail = pkg_dir_name.split("@", 1)[1]
        if tail and not tail.startswith("src-"):
            return tail
    return ""


def resolve_env_config_map(project_ini_path: Path) -> Dict[str, ProjectEnvConfig]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(project_ini_path, encoding="utf-8")

    default_env_options = dict(parser.items("env")) if parser.has_section("env") else {}
    cache: Dict[str, Dict[str, str]] = {}

    def resolve_env_options(env_name: str, stack: Optional[Set[str]] = None) -> Dict[str, str]:
        if env_name in cache:
            return dict(cache[env_name])
        if stack is None:
            stack = set()
        if env_name in stack:
            return {}
        section_name = f"env:{env_name}"
        if not parser.has_section(section_name):
            return {}
        stack.add(env_name)

        merged = dict(default_env_options)
        section_options = dict(parser.items(section_name))
        extends_names = split_csv_like(section_options.get("extends", ""))
        for ext in extends_names:
            ext_name = ext[4:] if ext.startswith("env:") else ext
            merged.update(resolve_env_options(ext_name, stack))
        merged.update(section_options)
        merged.pop("extends", None)

        stack.remove(env_name)
        cache[env_name] = dict(merged)
        return dict(merged)

    env_map: Dict[str, ProjectEnvConfig] = {}
    for section_name in parser.sections():
        if not section_name.startswith("env:"):
            continue
        env_name = section_name.split(":", 1)[1].strip()
        options = resolve_env_options(env_name)
        platform_spec = options.get("platform", "").strip()
        frameworks = [item.lower() for item in split_csv_like(options.get("framework", ""))]
        env_map[env_name] = ProjectEnvConfig(env_name=env_name, platform_spec=platform_spec, frameworks=frameworks)
    return env_map


def match_entry_for_platform_spec(entries: List[PlatformEntry], platform_spec: str) -> Optional[PlatformEntry]:
    spec = (platform_spec or "").strip()
    if not spec:
        return None
    if "://" in spec:
        return None

    base = spec
    version = ""
    if "@" in spec:
        base, version = spec.split("@", 1)
        base = base.strip()
        version = version.strip()
    base_lower = base.lower()
    norm_version = normalize_numeric_version_text(version)

    candidates = [entry for entry in entries if entry.base_name.lower() == base_lower]
    if not candidates:
        return None
    if not version:
        return sorted(candidates, key=lambda item: item.version_name.lower())[-1]

    for entry in candidates:
        if (
            entry.version_name == version
            or entry.platform_version == version
            or normalize_numeric_version_text(entry.version_name) == norm_version
            or normalize_numeric_version_text(entry.platform_version) == norm_version
        ):
            return entry
    return None


def check_entry_dependencies(entry: PlatformEntry, frameworks: Optional[List[str]] = None) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {
        "missing_required": [],
        "mismatch_required": [],
        "missing_optional": [],
        "mismatch_optional": [],
    }
    manifest_path = entry.platform_dir / "platform.json"
    if not manifest_path.exists():
        result["missing_required"].append(f"{entry.platform_dir_name}: 缺少 platform.json")
        return result

    try:
        platform_manifest = load_json(manifest_path)
    except Exception as exc:
        result["missing_required"].append(f"{entry.platform_dir_name}: platform.json 解析失败 ({exc})")
        return result

    package_map = platform_manifest.get("packages") or {}
    dependency_entry = match_dependency_version_entry(
        entry.base_name,
        entry.platform_dir_name,
        entry.version_name,
        entry.platform_version,
    )
    level_map = get_dependency_level_map(dependency_entry, package_map, frameworks)

    for package_name, package_info in package_map.items():
        level = level_map.get(package_name, "optional")
        if level == "ignored":
            continue

        specs = []
        primary_spec = str((package_info or {}).get("version", "")).strip()
        if primary_spec:
            specs.append(primary_spec)
        optional_specs = (package_info or {}).get("optionalVersions") or []
        if isinstance(optional_specs, list):
            specs.extend(str(item).strip() for item in optional_specs if str(item).strip())
        specs = [item for item in specs if item]
        if not specs:
            specs = [""]

        candidates = resolve_package_dir_names(package_name)
        if not candidates:
            key = "missing_required" if level == "required" else "missing_optional"
            result[key].append(f"{format_dependency_label(package_name, level)} ({' | '.join(specs)})")
            continue

        matched = False
        remote_only = all(is_remote_package_spec(spec) for spec in specs if spec)
        if remote_only:
            matched = True
        else:
            versions = {name: read_package_manifest_version(name) for name in candidates}
            for spec in specs:
                if is_remote_package_spec(spec):
                    matched = True
                    break
                for version in versions.values():
                    if version_satisfies_spec(version, spec):
                        matched = True
                        break
                if matched:
                    break

        if not matched:
            key = "mismatch_required" if level == "required" else "mismatch_optional"
            result[key].append(f"{format_dependency_label(package_name, level)} ({' | '.join(specs)})")

    return result

def get_dir_size_mb(path: Path) -> float:
    total = 0
    if not path.exists():
        return 0.0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return round(total / (1024 * 1024), 1)


def format_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def force_remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise exc_info[1]


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, onerror=force_remove_readonly)


def try_remove_tree(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        remove_tree(path)
        return True
    except Exception:
        return False


def hidden_subprocess_kwargs() -> dict[str, object]:
    options: dict[str, object] = {}
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            options["creationflags"] = creation_flags
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_cls:
            startupinfo = startupinfo_cls()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            options["startupinfo"] = startupinfo
    return options


def create_junction(link_path: Path, target_path: Path) -> None:
    if link_path.exists():
        return
    link_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_subprocess_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"创建目录联接失败：{link_path} -> {target_path}\n{proc.stdout.strip()}")


def remove_junction(link_path: Path) -> None:
    if not link_path.exists():
        return
    subprocess.run(
        ["cmd", "/c", "rmdir", str(link_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_subprocess_kwargs(),
    )


def robocopy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        remove_tree(dst)
    try:
        shutil.copytree(src, dst)
    except Exception as exc:
        raise RuntimeError(f"复制目录失败：{src} -> {dst}\n{exc}") from exc


def list_existing_drive_letters() -> Set[str]:
    letters: Set[str] = set()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if Path(f"{letter}:\\").exists():
            letters.add(letter)
    return letters


def create_subst_drive(target_path: Path, preferred_letters: List[str]) -> str:
    existing = list_existing_drive_letters()
    for letter in preferred_letters:
        if letter in existing:
            continue
        proc = subprocess.run(
            ["cmd", "/c", "subst", f"{letter}:", str(target_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode == 0:
            return f"{letter}:\\" 
    raise RuntimeError(f"无法为短路径映射可用盘符：{target_path}")


def remove_subst_drive(drive_root: str) -> None:
    drive = drive_root.rstrip("\\")
    subprocess.run(
        ["cmd", "/c", "subst", drive, "/D"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_subprocess_kwargs(),
    )


def explain_remove_error(path: Path, exc: Exception) -> str:
    base = f"{path.name}: {exc}"
    text = str(exc)
    lower = text.lower()
    if "winerror 5" in lower or "拒绝访问" in text:
        return (
            base
            + "\n可能原因：目录内存在只读文件，或文件正被 VS Code / Git / PlatformIO 占用。"
            + "\n建议先关闭相关程序后重试。"
        )
    if "winerror 32" in lower or "另一个程序正在使用" in text:
        return base + "\n可能原因：文件正在被其他程序占用，请先关闭占用它的程序后重试。"
    return base


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def ensure_pio_cli() -> Path:
    if PENV_PIO.exists():
        return PENV_PIO
    raise FileNotFoundError(
        "未找到 PlatformIO CLI。\n请先在本机安装并启动一次 PlatformIO，确保存在 .platformio/penv/Scripts/pio.exe。"
    )


def run_command(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_subprocess_kwargs(),
    )


def normalize_version_name(dir_name: str, manifest_version: str) -> str:
    if "@" in dir_name:
        return dir_name.split("@", 1)[1]
    if manifest_version:
        return manifest_version
    return "unknown"


def guess_source(dir_name: str, manifest: dict) -> str:
    if "repository" in manifest and isinstance(manifest["repository"], dict):
        return manifest["repository"].get("url", "") or ""
    if "@" in dir_name:
        return "registry-cache"
    return "active"


def resolve_package_dir_names(pkg_name: str) -> List[str]:
    results: List[str] = []
    if not PACKAGES_DIR.exists():
        return results

    exact = PACKAGES_DIR / pkg_name
    if exact.is_dir():
        results.append(exact.name)

    prefix_with_at = f"{pkg_name}@"
    prefix_src_hash = f"{pkg_name}-src-"
    for item in sorted(PACKAGES_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_dir():
            continue
        if item.name == pkg_name or item.name.startswith(prefix_with_at) or item.name.startswith(prefix_src_hash):
            if item.name not in results:
                results.append(item.name)
    return results


def choose_platform_entry(existing: PlatformEntry, candidate: PlatformEntry) -> PlatformEntry:
    try:
        existing_mtime = existing.platform_dir.stat().st_mtime
    except OSError:
        existing_mtime = 0
    try:
        candidate_mtime = candidate.platform_dir.stat().st_mtime
    except OSError:
        candidate_mtime = 0
    if candidate_mtime >= existing_mtime:
        return candidate
    return existing


def collect_platform_entries() -> List[PlatformEntry]:
    entries: List[PlatformEntry] = []
    if not PLATFORMS_DIR.exists():
        return entries

    for platform_dir in sorted(PLATFORMS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not platform_dir.is_dir():
            continue
        manifest_path = platform_dir / "platform.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue

        base_name = manifest.get("name") or platform_dir.name.split("@", 1)[0]
        version_name = normalize_version_name(platform_dir.name, manifest.get("version", ""))
        dependency_entry = match_dependency_version_entry(base_name, platform_dir.name, version_name, manifest.get("version", ""))
        package_names = set((manifest.get("packages") or {}).keys())
        package_names.update(item["name"] for item in iter_dependency_items(dependency_entry))
        package_names = sorted(package_names, key=str.lower)
        package_dirs: List[str] = []
        for pkg_name in package_names:
            for pkg_dir_name in resolve_package_dir_names(pkg_name):
                if pkg_dir_name not in package_dirs:
                    package_dirs.append(pkg_dir_name)

        size_mb = get_dir_size_mb(platform_dir)
        for pkg_dir_name in package_dirs:
            size_mb += get_dir_size_mb(PACKAGES_DIR / pkg_dir_name)

        candidate = PlatformEntry(
            base_name=base_name,
            version_name=version_name,
            platform_dir_name=platform_dir.name,
            platform_dir=platform_dir,
            platform_version=manifest.get("version", ""),
            title=manifest.get("title") or base_name,
            packages=package_names,
            package_dirs=package_dirs,
            required_specs=[f"{base_name}@{version_name}"],
            source=guess_source(platform_dir.name, manifest),
            size_mb=round(size_mb, 1),
        )
        duplicate_index = next(
            (i for i, item in enumerate(entries) if item.base_name == candidate.base_name and item.version_name == candidate.version_name),
            None,
        )
        if duplicate_index is None:
            entries.append(candidate)
        else:
            entries[duplicate_index] = choose_platform_entry(entries[duplicate_index], candidate)
    return entries


def collect_platform_entries_with_progress(progress_callback=None) -> List[PlatformEntry]:
    entries: List[PlatformEntry] = []
    if not PLATFORMS_DIR.exists():
        if progress_callback:
            progress_callback(0, 0, "未找到 .platformio/platforms")
        return entries

    platform_dirs = [p for p in sorted(PLATFORMS_DIR.iterdir(), key=lambda p: p.name.lower()) if p.is_dir()]
    total = len(platform_dirs)
    if progress_callback:
        progress_callback(0, total, f"发现 {total} 个平台目录，开始扫描")

    for index, platform_dir in enumerate(platform_dirs, start=1):
        if progress_callback:
            progress_callback(index - 1, total, f"正在读取 {platform_dir.name} 的 platform.json")

        manifest_path = platform_dir / "platform.json"
        if not manifest_path.exists():
            if progress_callback:
                progress_callback(index, total, f"跳过 {platform_dir.name}：缺少 platform.json")
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            if progress_callback:
                progress_callback(index, total, f"跳过 {platform_dir.name}：platform.json 解析失败")
            continue

        base_name = manifest.get("name") or platform_dir.name.split("@", 1)[0]
        version_name = normalize_version_name(platform_dir.name, manifest.get("version", ""))
        dependency_entry = match_dependency_version_entry(base_name, platform_dir.name, version_name, manifest.get("version", ""))
        package_names = set((manifest.get("packages") or {}).keys())
        package_names.update(item["name"] for item in iter_dependency_items(dependency_entry))
        package_names = sorted(package_names, key=str.lower)

        if progress_callback:
            progress_callback(index - 1, total, f"正在解析 {base_name}@{version_name} 的依赖列表")

        package_dirs: List[str] = []
        for pkg_name in package_names:
            for pkg_dir_name in resolve_package_dir_names(pkg_name):
                if pkg_dir_name not in package_dirs:
                    package_dirs.append(pkg_dir_name)

        if progress_callback:
            progress_callback(index - 1, total, f"正在统计 {base_name}@{version_name} 的大小")

        size_mb = get_dir_size_mb(platform_dir)
        for pkg_dir_name in package_dirs:
            pkg_dir = PACKAGES_DIR / pkg_dir_name
            if progress_callback:
                progress_callback(index - 1, total, f"正在统计依赖包 {pkg_dir_name} 的大小")
            size_mb += get_dir_size_mb(pkg_dir)

        candidate = PlatformEntry(
            base_name=base_name,
            version_name=version_name,
            platform_dir_name=platform_dir.name,
            platform_dir=platform_dir,
            platform_version=manifest.get("version", ""),
            title=manifest.get("title") or base_name,
            packages=package_names,
            package_dirs=package_dirs,
            required_specs=[f"{base_name}@{version_name}"],
            source=guess_source(platform_dir.name, manifest),
            size_mb=round(size_mb, 1),
        )
        duplicate_index = next(
            (i for i, item in enumerate(entries) if item.base_name == candidate.base_name and item.version_name == candidate.version_name),
            None,
        )
        if duplicate_index is None:
            entries.append(candidate)
        else:
            entries[duplicate_index] = choose_platform_entry(entries[duplicate_index], candidate)

        if progress_callback:
            progress_callback(index, total, f"已完成 {base_name}@{version_name}")

    if progress_callback:
        progress_callback(total, total, f"扫描完成，共 {len(entries)} 个有效平台版本")
    return entries


def parse_bundle_manifest(bundle_file: Path) -> Optional[dict]:
    if not bundle_file.exists():
        return None
    if not SEVEN_ZIP.exists():
        return None
    probe = run_command([str(SEVEN_ZIP), "e", "-so", str(bundle_file), MANIFEST_FILE])
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    try:
        return json.loads(probe.stdout)
    except json.JSONDecodeError:
        return None


def estimate_archive_unpacked_size(bundle_file: Path) -> int:
    if not bundle_file.exists():
        return 0
    if not SEVEN_ZIP.exists():
        return 0
    proc = run_command([str(SEVEN_ZIP), "l", "-slt", str(bundle_file)])
    if proc.returncode != 0:
        return 0
    total = 0
    for line in proc.stdout.splitlines():
        if line.startswith("Size = "):
            value = line[7:].strip()
            if value.isdigit():
                total += int(value)
    return total


def stage_single_sdk(output_path: Path, entry: PlatformEntry) -> str:
    temp_root = Path(tempfile.mkdtemp(prefix="pio_", dir=tempfile.gettempdir()))
    try:
        sdk_root = temp_root / "sdk"
        (sdk_root / "platforms").mkdir(parents=True, exist_ok=True)
        (sdk_root / "packages").mkdir(parents=True, exist_ok=True)

        manifest = {
            "format": 1,
            "created_by": "PlatformIO Offline SDK Manager",
            "entries": [
                {
                    "base_name": entry.base_name,
                    "version_name": entry.version_name,
                    "platform_dir_name": entry.platform_dir_name,
                    "platform_version": entry.platform_version,
                    "packages": entry.packages,
                    "package_dirs": entry.package_dirs,
                    "source": entry.source,
                    "size_mb": entry.size_mb,
                }
            ],
        }

        copy_map = [
            (entry.platform_dir, sdk_root / "platforms" / entry.platform_dir_name),
        ]
        for pkg_dir_name in entry.package_dirs:
            src_pkg_dir = PACKAGES_DIR / pkg_dir_name
            if src_pkg_dir.exists():
                copy_map.append((src_pkg_dir, sdk_root / "packages" / pkg_dir_name))

        for src, dst in copy_map:
            shutil.copytree(src, dst)

        (sdk_root / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if output_path.exists():
            output_path.unlink()

        proc = run_command(
            [str(SEVEN_ZIP), "a", "-t7z", str(output_path), "sdk", "-mx=5"],
            cwd=temp_root,
        )
        if proc.returncode != 0 or not output_path.exists():
            raise RuntimeError(proc.stdout.strip() or "7z 打包失败")
        return ""
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


class PioManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PlatformIO 离线安装与 SDK 管理")
        self.root.geometry("1420x980")
        self.root.minsize(1320, 900)
        try:
            self.root.tk.call("tk", "scaling", 1.5)
        except Exception:
            pass

        self.font = ("Microsoft YaHei UI", 13)
        self.font_bold = ("Microsoft YaHei UI", 15, "bold")
        root.option_add("*Font", self.font)
        self._configure_styles()

        self.base_dir = APP_BASE_DIR
        self.runtime_python = BUNDLE_ROOT / "rt" / "python" / "python.exe"
        self.source_pio_root = BUNDLE_ROOT / "p" / ".platformio"
        self.vsix_dir = BUNDLE_ROOT / "vx"
        self.patch_dir = BUNDLE_ROOT / "pt"
        self.vscode_dir = BUNDLE_ROOT / "vc"
        self.user_home = Path.home()
        self.user_pio_root = self.user_home / ".platformio"
        self.user_vscode_dir = self.user_home / "AppData" / "Local" / "Programs" / "Microsoft VS Code"
        self.user_code_settings = Path(os.environ["APPDATA"]) / "Code" / "User" / "settings.json"
        self.user_vscode_extensions = self.user_home / ".vscode" / "extensions"
        self.log_path = self.base_dir / "install.log"
        self.portable_scripts: Optional[Path] = None
        self.site_packages: Optional[Path] = None
        self.code_cli: Optional[Path] = None
        self.installed_vscode = False
        self.install_started = False
        self.pio_install_buttons_locked = False
        self.pio_install_running = False
        self.pio_step_index = -1
        self.pio_step_start = 0
        self.pio_step_weight = 0

        self.entries_by_key: Dict[str, PlatformEntry] = {}
        self.group_items: Dict[str, str] = {}
        self.child_items: Dict[str, str] = {}
        self.group_children: Dict[str, List[str]] = {}
        self.checked_keys: Set[str] = set()
        self.install_busy = False
        self.install_locked_selection: tuple[str, ...] = ()
        self.pkg_busy = False

        self.status_var = tk.StringVar(value="就绪")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.build_install_pio_tab()
        self.build_install_sdk_tab()
        self.build_package_tab()
        self.build_clean_tab()

        status_bar = ttk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(8, 4))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

        style.configure(".", font=self.font)
        style.configure("TNotebook.Tab", font=self.font_bold, padding=(20, 12))
        style.configure("TButton", padding=(14, 9))
        style.configure("Treeview", font=self.font, rowheight=38)
        style.configure("Treeview.Heading", font=self.font_bold)
        style.configure("Pkg.Treeview", font=self.font, rowheight=38)
        style.configure("Pkg.Treeview.Heading", font=self.font_bold)
        style.configure("PkgDisabled.Treeview", font=self.font, rowheight=38, foreground="#9a9a9a", background="#f3f3f3", fieldbackground="#f3f3f3")
        style.configure("PkgDisabled.Treeview.Heading", font=self.font_bold)
        style.map("PkgDisabled.Treeview", foreground=[("selected", "#9a9a9a")], background=[("selected", "#e8e8e8")])

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
            self.log_install("执行命令: " + " ".join(f'"{a}"' if " " in a else a for a in args))
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd) if cwd else None,
            **hidden_subprocess_kwargs(),
        )
        if proc.stdout.strip() and not suppress_stdout:
            self.log_install(proc.stdout.strip())
        if proc.stderr.strip() and not suppress_stderr:
            self.log_install(proc.stderr.strip())
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令执行失败（{proc.returncode}）：{' '.join(args)}")
        if proc.returncode == 0 and success_message:
            self.log_install(success_message)
        return proc

    def build_install_pio_tab(self):
        tab = ttk.Frame(self.notebook, padding=15)
        self.install_pio_tab = tab
        self.notebook.add(tab, text="  安装 PIO  ")

        ttk.Label(tab, text="离线安装 PlatformIO 基础环境", font=self.font_bold).pack(anchor=tk.W)
        ttk.Label(
            tab,
            text="点击开始后会复制离线 PlatformIO Core、写入配置并安装离线 VS Code 扩展。",
            foreground="#666",
        ).pack(anchor=tk.W, pady=(2, 8))

        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        self.btn_pio_install = ttk.Button(top, text="开始安装 PIO", command=self.start_pio_install)
        self.btn_pio_install.pack(side=tk.LEFT)
        self.btn_pio_open_log = ttk.Button(top, text="打开日志", command=self.open_install_log)
        self.btn_pio_open_log.pack(side=tk.LEFT, padx=(8, 0))
        self.pio_status_label = ttk.Label(top, text="尚未开始安装", foreground="gray")
        self.pio_status_label.pack(side=tk.LEFT, padx=(12, 0))

        self.pio_detail_var = tk.StringVar(value="点击“开始安装 PIO”部署离线 PlatformIO。")
        ttk.Label(tab, textvariable=self.pio_detail_var, foreground="#4f6b8a").pack(anchor=tk.W, pady=(6, 6))
        self.pio_progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.pio_progress.pack(fill=tk.X, pady=(0, 8))

        self.pio_steps = [
            StepInfo("检查安装资源", 6),
            StepInfo("复制 PlatformIO Core", 28),
            StepInfo("修复命令包装器", 14),
            StepInfo("定位或安装 VS Code", 18),
            StepInfo("写入 VS Code 配置", 10),
            StepInfo("关闭 telemetry", 6),
            StepInfo("安装离线扩展", 12),
            StepInfo("应用离线补丁", 6),
        ]
        self.pio_total_weight = sum(step.weight for step in self.pio_steps)
        self.pio_step_list = tk.Listbox(tab, height=len(self.pio_steps), activestyle="none", exportselection=False)
        self.pio_step_list.pack(fill=tk.X)
        for idx, step in enumerate(self.pio_steps, start=1):
            self.pio_step_list.insert("end", f"{idx}. {step.title}")
        self.pio_step_list.configure(state="disabled", bg="#fafafa")

        log_frame = ttk.LabelFrame(tab, text="安装日志", padding=(10, 10))
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.pio_log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled", bg="#f7f7f7")
        self.pio_log_text.pack(fill="both", expand=True)

        self.pio_summary_var = tk.StringVar(value="尚未开始安装")
        ttk.Label(tab, textvariable=self.pio_summary_var, foreground="#666666").pack(anchor=tk.W, pady=(8, 0))

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.root.update_idletasks()

    def log_install(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.pio_log_text.configure(state="normal")
        self.pio_log_text.insert("end", line + "\n")
        self.pio_log_text.see("end")
        self.pio_log_text.configure(state="disabled")

    def open_install_log(self):
        if self.log_path.exists():
            os.startfile(str(self.log_path))

    def _set_pio_step_visual(self, index: int):
        self.pio_step_list.configure(state="normal")
        self.pio_step_list.selection_clear(0, "end")
        if index >= 0:
            self.pio_step_list.selection_set(index)
            self.pio_step_list.activate(index)
            self.pio_step_list.see(index)
        self.pio_step_list.configure(state="disabled")

    def pio_begin_step(self, index: int, detail: str):
        prior_weight = sum(step.weight for step in self.pio_steps[:index])
        self.pio_step_index = index
        self.pio_step_start = prior_weight
        self.pio_step_weight = self.pio_steps[index].weight
        self.pio_status_label.config(text=f"第 {index + 1}/{len(self.pio_steps)} 步：{self.pio_steps[index].title}", foreground="darkblue")
        self.pio_detail_var.set(detail)
        self.pio_summary_var.set(f"总进度 {round(prior_weight * 100 / self.pio_total_weight)}%")
        self._set_pio_step_visual(index)
        self._set_pio_progress_absolute(prior_weight)

    def pio_update_step_progress(self, fraction: float, detail: str | None = None):
        fraction = min(max(fraction, 0.0), 1.0)
        absolute = self.pio_step_start + self.pio_step_weight * fraction
        if detail:
            self.pio_detail_var.set(detail)
        self.pio_summary_var.set(f"总进度 {round(absolute * 100 / self.pio_total_weight)}%")
        self._set_pio_progress_absolute(absolute)

    def _set_pio_progress_absolute(self, value: float):
        percent = round(min(max(value, 0), self.pio_total_weight) * 100 / self.pio_total_weight)
        self.pio_progress.configure(value=percent)
        self.root.update_idletasks()

    def start_pio_install(self):
        if self.install_started:
            return
        self.install_started = True
        self.btn_pio_install.configure(state="disabled")
        self.btn_pio_open_log.configure(state="disabled")
        self.log_path.write_text("", encoding="utf-8")
        self.log_install("安装任务开始")
        threading.Thread(target=self.run_pio_install, daemon=True).start()

    def run_pio_install(self):
        try:
            self.step_check_resources()
            self.step_copy_platformio()
            self.step_prepare_wrappers()
            self.step_ensure_vscode()
            self.step_write_settings()
            self.step_disable_telemetry()
            self.step_install_vsix()
            self.step_apply_patch()
            self.root.after(0, self.finish_pio_success)
        except Exception as exc:
            self.root.after(0, lambda err=exc: self.finish_pio_error(err))

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
        self.log_install(f"未检测到 VS Code CLI，尝试静默安装 VS Code：{vscode_setup.name}")
        self.pio_update_step_progress(0.2, "未找到 VS Code，正在离线安装 VS Code")

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
        self.pio_update_step_progress(0.7, "VS Code 安装完成，正在定位 code.cmd")
        self.code_cli = self._find_existing_code_cli()
        if not self.code_cli:
            raise RuntimeError("VS Code 已安装，但仍未找到 code.cmd。")

    def step_check_resources(self):
        self.pio_begin_step(0, "正在检查安装器内置资源")
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
        self.pio_update_step_progress(1.0, "资源检查完成")
        self.log_install("资源检查完成")

    def step_copy_platformio(self):
        self.pio_begin_step(1, "正在复制 PlatformIO Core 到用户目录")
        self.log_install(f"复制 {self.source_pio_root} -> {self.user_pio_root}")
        backup = self.user_home / f".platformio.backup-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if self.user_pio_root.exists():
            self.log_install(f"备份现有 .platformio 到 {backup}")
            self.pio_update_step_progress(0.15, "检测到现有 .platformio，正在备份")
            if backup.exists():
                remove_tree(backup)
            try:
                self.user_pio_root.rename(backup)
                self.log_install("已通过重命名完成备份")
            except OSError as exc:
                self.log_install(f"重命名备份失败，改用完整复制：{exc}")
                robocopy_tree(self.user_pio_root, backup)
                remove_tree(self.user_pio_root)

        self.pio_update_step_progress(0.3, "正在复制 PlatformIO 核心文件")
        shutil.copytree(self.source_pio_root, self.user_pio_root)
        self.pio_update_step_progress(1.0, "PlatformIO Core 复制完成")

    def step_prepare_wrappers(self):
        self.pio_begin_step(2, "正在生成稳定的命令包装器")
        self.log_install("生成 portable-bin\\pio.cmd / platformio.cmd")

        portable_scripts = self.user_pio_root / "portable-bin"
        portable_scripts.mkdir(parents=True, exist_ok=True)
        site_packages = self.user_pio_root / "penv" / "Lib" / "site-packages"
        penv_python = self.user_pio_root / "penv" / "Scripts" / "python.exe"
        penv_pio = self.user_pio_root / "penv" / "Scripts" / "platformio.exe"
        builtin_python_dir = self.user_pio_root / "python3"

        self.pio_update_step_progress(0.2, "正在复制内置 Python 运行时")
        if builtin_python_dir.exists():
            shutil.rmtree(builtin_python_dir, ignore_errors=True)
        shutil.copytree(self.runtime_python.parent, builtin_python_dir)

        self.pio_update_step_progress(0.6, "正在写入 pio.cmd 和 platformio.cmd")
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
        self.pio_update_step_progress(1.0, "命令包装器生成完成")

    def step_ensure_vscode(self):
        self.pio_begin_step(3, "正在定位 VS Code 安装环境")
        self.code_cli = self._find_existing_code_cli()
        if self.code_cli:
            self.log_install(f"已找到 VS Code CLI: {self.code_cli}")
            self.pio_update_step_progress(1.0, "已找到可用的 VS Code CLI")
            return

        self._install_vscode_if_needed()
        self.log_install(f"VS Code CLI 就绪: {self.code_cli}")
        self.pio_update_step_progress(1.0, "VS Code 已就绪")

    def step_write_settings(self):
        self.pio_begin_step(4, "正在写入 VS Code 配置")
        if not self.portable_scripts or not self.site_packages:
            raise RuntimeError("命令包装器未初始化。")

        self.user_code_settings.parent.mkdir(parents=True, exist_ok=True)
        self.pio_update_step_progress(0.3, "正在生成 settings.json")

        existing_settings: dict[str, object] = {}
        if self.user_code_settings.exists():
            try:
                existing_settings = json.loads(self.user_code_settings.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.log_install("现有 settings.json 不是合法 JSON，将使用新配置覆盖。")

        penv_scripts = self.user_pio_root / "penv" / "Scripts"
        custom_path_parts = [str(penv_scripts), str(self.portable_scripts)]
        custom_path = os.pathsep.join(custom_path_parts)
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

        self.pio_update_step_progress(0.75, "正在写入 PlatformIO 环境变量")
        os.environ["PLATFORMIO_CORE_DIR"] = str(self.user_pio_root)
        os.environ["PLATFORMIO_PACKAGES_DIR"] = str(self.user_pio_root / "packages")
        os.environ["PLATFORMIO_PLATFORMS_DIR"] = str(self.user_pio_root / "platforms")
        os.environ["PLATFORMIO_PATH"] = custom_path
        os.environ["PYTHONPATH"] = str(self.site_packages)

        env_pairs = {
            "PLATFORMIO_CORE_DIR": str(self.user_pio_root),
            "PLATFORMIO_PACKAGES_DIR": str(self.user_pio_root / "packages"),
            "PLATFORMIO_PLATFORMS_DIR": str(self.user_pio_root / "platforms"),
            "PLATFORMIO_PATH": custom_path,
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

        self.pio_update_step_progress(1.0, "VS Code 配置写入完成")

    def step_disable_telemetry(self):
        self.pio_begin_step(5, "正在关闭 PlatformIO telemetry")
        if not self.site_packages:
            raise RuntimeError("PlatformIO 运行环境未初始化。")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.site_packages)
        env["PLATFORMIO_CORE_DIR"] = str(self.user_pio_root)
        env["PLATFORMIO_PACKAGES_DIR"] = str(self.user_pio_root / "packages")
        env["PLATFORMIO_PLATFORMS_DIR"] = str(self.user_pio_root / "platforms")

        self.pio_update_step_progress(0.35, "正在执行 PlatformIO 设置命令")
        proc = self._run_command(
            [str(self.user_pio_root / "penv" / "Scripts" / "python.exe"), "-m", "platformio", "settings", "set", "enable_telemetry", "No"],
            env=env,
            suppress_stdout=True,
        )
        if proc.returncode != 0:
            self.log_install("关闭 telemetry 时返回非 0，继续安装，但建议复查日志。")
        else:
            self.log_install("已关闭 PlatformIO telemetry")
        self.pio_update_step_progress(1.0, "telemetry 设置完成")

    def step_install_vsix(self):
        self.pio_begin_step(6, "正在安装离线扩展")
        if not self.code_cli:
            raise RuntimeError("VS Code CLI 不可用，无法安装扩展。")

        pio_vsix = self._safe_glob_first(self.vsix_dir, "platformio-ide-*-offline.vsix")
        cpp_vsix = self._safe_glob_first(self.vsix_dir, "cpptools-*.vsix")
        vsix_items = [pio_vsix, cpp_vsix]

        for idx, vsix in enumerate(vsix_items, start=1):
            fraction = (idx - 1) / len(vsix_items)
            self.pio_update_step_progress(fraction, f"正在安装扩展 {idx}/{len(vsix_items)}：{vsix.name}")
            proc = self._run_command([str(self.code_cli), "--install-extension", str(vsix), "--force"])
            if proc.returncode != 0 and "cpptools" not in vsix.name:
                raise RuntimeError(f"安装扩展失败: {vsix.name}")

        self.pio_update_step_progress(1.0, "离线扩展安装完成")

    def step_apply_patch(self):
        self.pio_begin_step(7, "正在应用 PlatformIO 本地补丁")
        self.user_vscode_extensions.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            self.user_vscode_extensions.glob("platformio.platformio-ide-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("未找到已安装的 PlatformIO 扩展目录。")

        target = candidates[0]
        self.log_install(f"补丁目标: {target}")
        self.pio_update_step_progress(0.4, "正在覆盖 extension.js")
        shutil.copy2(self.patch_dir / "extension.js", target / "dist" / "extension.js")
        shutil.copy2(self.patch_dir / "extension.js.map", target / "dist" / "extension.js.map")
        self.pio_update_step_progress(0.8, "正在覆盖 package.json")
        shutil.copy2(self.patch_dir / "package.json", target / "package.json")
        self.pio_update_step_progress(1.0, "补丁应用完成")

    def finish_pio_success(self):
        self.pio_step_index = len(self.pio_steps) - 1
        self._set_pio_step_visual(self.pio_step_index)
        self._set_pio_progress_absolute(self.pio_total_weight)
        self.pio_status_label.config(text="安装完成", foreground="green")
        self.pio_detail_var.set("离线 PlatformIO 环境已经安装完成。")
        restart_tip = "请重启 VS Code 后再打开 PlatformIO 工程。"
        if self.installed_vscode:
            restart_tip = "VS Code 已完成离线安装，请先启动一次 VS Code，再打开 PlatformIO 工程。"
        self.pio_summary_var.set(restart_tip)
        self.log_install("安装完成")
        messagebox.showinfo("安装完成", restart_tip)
        self.btn_pio_open_log.configure(state="normal")
        self.btn_pio_install.configure(state="disabled")

    def finish_pio_error(self, err: Exception):
        self.log_install(f"[ERROR] {err}")
        self.pio_status_label.config(text="安装失败", foreground="red")
        self.pio_detail_var.set("请查看日志并重新运行安装程序。")
        self.pio_summary_var.set(str(err))
        messagebox.showerror("安装失败", str(err))
        self.install_started = False
        self.btn_pio_open_log.configure(state="normal")
        self.btn_pio_install.configure(state="normal")

    def safe_bg(self, worker, on_error_text: str):
        def wrapped():
            try:
                worker()
            except Exception as exc:
                self.root.after(
                    0,
                    lambda err=exc: self._handle_bg_error(on_error_text, err),
                )
        threading.Thread(target=wrapped, daemon=True).start()

    def _handle_bg_error(self, on_error_text: str, exc: Exception):
        self.btn_clean_scan.config(state=tk.NORMAL)
        self.btn_pkg_scan.config(state=tk.NORMAL)
        self.set_status(on_error_text)
        messagebox.showerror("错误", str(exc))

    def build_install_sdk_tab(self):
        tab = ttk.Frame(self.notebook, padding=15)
        self.install_sdk_tab = tab
        self.notebook.add(tab, text="  安装 SDK  ")
        self.build_install_sdk_content(tab)

    def build_package_tab(self):
        tab = ttk.Frame(self.notebook, padding=15)
        self.package_tab = tab
        self.notebook.add(tab, text="  打包 SDK  ")
        self.build_package_content(tab)

    # ==================== TAB 1: 清理 ====================
    def build_clean_tab(self):
        tab = ttk.Frame(self.notebook, padding=15)
        self.clean_tab = tab
        self.notebook.add(tab, text="  清理 SDK  ")

        ttk.Label(tab, text="选择要删除的已安装 SDK 版本", font=self.font_bold).pack(anchor=tk.W)
        ttk.Label(
            tab,
            text="建议只删除不再使用的 SDK 版本；删除后会调用官方 `pio system prune` 清理无用包。",
            foreground="#666",
        ).pack(anchor=tk.W, pady=(2, 8))

        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 8))
        self.btn_clean_scan = ttk.Button(top, text="扫描本机环境", command=self.do_clean_scan)
        self.btn_clean_scan.pack(side=tk.LEFT)
        self.clean_summary_var = tk.StringVar(value="尚未扫描")
        ttk.Label(top, textvariable=self.clean_summary_var, foreground="#666").pack(side=tk.LEFT, padx=(12, 0))
        self.clean_scan_note_var = tk.StringVar(value="说明：扫描会统计平台目录和依赖包大小，首次可能较慢。")
        ttk.Label(tab, textvariable=self.clean_scan_note_var, foreground="#4f6b8a").pack(anchor=tk.W, pady=(0, 6))
        self.clean_scan_progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.clean_scan_progress.pack(fill=tk.X, pady=(0, 8))

        list_frame = ttk.Frame(tab)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.clean_tree = ttk.Treeview(
            list_frame,
            columns=("version", "size", "packages"),
            show="tree headings",
            selectmode="browse",
        )
        self.clean_tree.heading("#0", text="")
        self.clean_tree.heading("version", text="版本 / 目录")
        self.clean_tree.heading("size", text="大小")
        self.clean_tree.heading("packages", text="依赖包目录")
        self.clean_tree.column("#0", width=220, stretch=False)
        self.clean_tree.column("version", width=300)
        self.clean_tree.column("size", width=120, stretch=False, anchor=tk.CENTER)
        self.clean_tree.column("packages", width=620)
        self.clean_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.clean_tree.yview)
        self.clean_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.clean_tree.bind("<ButtonRelease-1>", self.on_clean_tree_click)

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill=tk.X, pady=(10, 4))
        self.btn_clean_exec = tk.Button(
            btn_frame,
            text="删除选中 SDK 并清理",
            bg="#cd5c5c",
            fg="white",
            disabledforeground="white",
            activeforeground="white",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=16,
            pady=4,
            state=tk.DISABLED,
            command=self.do_clean_exec,
        )
        self.btn_clean_exec.pack(side=tk.LEFT)

        self.clean_log = tk.Text(tab, height=10, wrap=tk.WORD, state=tk.DISABLED, bg="#f7f7f7")
        self.clean_log.pack(fill=tk.BOTH, expand=False, pady=(8, 0))

    def log_clean(self, text: str):
        self.clean_log.configure(state=tk.NORMAL)
        self.clean_log.insert(tk.END, text + "\n")
        self.clean_log.see(tk.END)
        self.clean_log.configure(state=tk.DISABLED)

    def do_clean_scan(self):
        self.set_status("正在扫描清理项...")
        self.clean_log.configure(state=tk.NORMAL)
        self.clean_log.delete("1.0", tk.END)
        self.clean_log.configure(state=tk.DISABLED)
        self.btn_clean_exec.config(state=tk.DISABLED)
        self.btn_clean_scan.config(state=tk.DISABLED)
        self.clean_scan_progress.configure(value=0, maximum=100)
        self.clean_scan_note_var.set("准备扫描 .platformio/platforms ...")

        def worker():
            entries = collect_platform_entries_with_progress(
                lambda current, total, text: self.root.after(
                    0, lambda c=current, t=total, s=text: self.update_scan_progress("clean", c, t, s)
                )
            )
            self.root.after(0, lambda: self.finish_scan("clean", entries))

        self.safe_bg(worker, "扫描失败")

    def on_clean_tree_click(self, event):
        self.handle_tree_click(self.clean_tree, event)
        self.btn_clean_exec.config(state=tk.NORMAL if self.checked_keys else tk.DISABLED)

    def do_clean_exec(self):
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            messagebox.showinfo("提示", "请先选择要删除的 SDK 版本。")
            return

        names = "\n".join(f"  - {e.base_name} / {e.version_name}" for e in selected_entries)
        if not messagebox.askyesno(
            "确认删除",
            "将删除以下已安装 SDK 版本，并调用 `pio system prune --platform-packages --core-packages`：\n\n"
            + names
            + "\n\n此操作不可恢复，确定继续吗？",
            icon="warning",
        ):
            return

        self.btn_clean_exec.config(state=tk.DISABLED)
        self.set_status("正在删除已安装 SDK 版本...")
        self.log_clean(">>> 开始清理")

        def worker():
            removed_platforms = []
            removed_packages = []
            errors = []
            packages_to_check: Set[str] = set()

            for entry in selected_entries:
                if entry.platform_dir.exists():
                    try:
                        remove_tree(entry.platform_dir)
                        removed_platforms.append(entry.platform_dir.name)
                    except Exception as exc:
                        errors.append("删除平台失败 " + explain_remove_error(entry.platform_dir, exc))
                for pkg_dir_name in entry.package_dirs:
                    packages_to_check.add(pkg_dir_name)

            referenced_now = self.collect_referenced_package_dirs()
            for pkg_dir_name in sorted(packages_to_check):
                pkg_dir = PACKAGES_DIR / pkg_dir_name
                if pkg_dir_name in referenced_now or not pkg_dir.exists():
                    continue
                try:
                    remove_tree(pkg_dir)
                    removed_packages.append(pkg_dir_name)
                except Exception as exc:
                    errors.append("删除依赖包失败 " + explain_remove_error(pkg_dir, exc))

            prune_output = ""
            try:
                pio = ensure_pio_cli()
                proc = run_command([str(pio), "system", "prune", "--force", "--platform-packages", "--core-packages"])
                prune_output = proc.stdout.strip()
                if proc.returncode != 0:
                    errors.append(f"`pio system prune` 失败，退出码 {proc.returncode}")
            except Exception as exc:
                errors.append(f"调用 `pio system prune` 失败: {exc}")

            def finish():
                for name in removed_platforms:
                    self.log_clean(f"已删除平台: {name}")
                for name in removed_packages:
                    self.log_clean(f"已删除包: {name}")
                if prune_output:
                    self.log_clean("--- prune 输出 ---")
                    for line in prune_output.splitlines():
                        self.log_clean(line)
                if errors:
                    self.log_clean("--- 错误 ---")
                    for line in errors:
                        self.log_clean(line)
                    messagebox.showwarning("完成（有错误）", "\n".join(errors))
                else:
                    messagebox.showinfo("完成", "已删除选中的 SDK 版本，并完成 prune。")
                self.do_clean_scan()

            self.root.after(0, finish)

        self.safe_bg(worker, "清理失败")

    def collect_referenced_package_dirs(self) -> Set[str]:
        refs = set()
        for entry in collect_platform_entries():
            refs.update(entry.package_dirs)
        return refs

    # ==================== TAB 2: 打包 ====================
    def build_package_content(self, tab):
        ttk.Label(tab, text="选择要打包的平台版本（基平台 + 版本明细）", font=self.font_bold).pack(anchor=tk.W)
        ttk.Label(
            tab,
            text="本工具会将 platforms / packages 以及必要的公共构建工具一起打包为离线 SDK。",
            foreground="#666",
        ).pack(anchor=tk.W, pady=(2, 8))

        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        self.btn_pkg_scan = ttk.Button(top, text="扫描本机环境", command=self.do_pkg_scan)
        self.btn_pkg_scan.pack(side=tk.LEFT)
        self.btn_pkg_verify = ttk.Button(top, text="按项目环境预检", command=self.do_pkg_precheck_from_project)
        self.btn_pkg_verify.pack(side=tk.LEFT, padx=(8, 0))
        self.pkg_status = ttk.Label(top, text="尚未扫描", foreground="gray")
        self.pkg_status.pack(side=tk.LEFT, padx=(12, 0))
        self.pkg_scan_note_var = tk.StringVar(value="说明：扫描会统计每个版本及其依赖包大小，期间界面保持可响应。")
        ttk.Label(tab, textvariable=self.pkg_scan_note_var, foreground="#4f6b8a").pack(anchor=tk.W, pady=(6, 6))
        self.pkg_scan_progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.pkg_scan_progress.pack(fill=tk.X, pady=(0, 8))
        self.pkg_precheck_var = tk.StringVar(value="预检说明：可选中项目的 platformio.ini，按 env 校验平台与依赖是否齐全。")
        ttk.Label(tab, textvariable=self.pkg_precheck_var, foreground="#4f6b8a").pack(anchor=tk.W, pady=(0, 8))

        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 8))

        self.pkg_tree = ttk.Treeview(
            tree_frame,
            columns=("version", "size", "packages"),
            show="tree headings",
            selectmode="browse",
            style="Pkg.Treeview",
        )
        self.pkg_tree.heading("#0", text="")
        self.pkg_tree.heading("version", text="版本 / 目录")
        self.pkg_tree.heading("size", text="大小")
        self.pkg_tree.heading("packages", text="依赖包目录")
        self.pkg_tree.column("#0", width=220, stretch=False)
        self.pkg_tree.column("version", width=300)
        self.pkg_tree.column("size", width=120, stretch=False, anchor=tk.CENTER)
        self.pkg_tree.column("packages", width=620)
        self.pkg_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.pkg_tree.yview)
        self.pkg_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.pkg_tree.bind("<Button-1>", self.on_pkg_tree_mouse_down, add="+")
        self.pkg_tree.bind("<ButtonRelease-1>", self.on_pkg_tree_click)
        self.pkg_tree.bind("<Key>", self.on_pkg_tree_key_down, add="+")

        out_frame = ttk.Frame(tab)
        out_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(out_frame, text="输出文件名:").pack(side=tk.LEFT)
        self.pkg_output_var = tk.StringVar(value="(请先选择版本)")
        ttk.Entry(out_frame, textvariable=self.pkg_output_var, width=48, state="readonly").pack(side=tk.LEFT, padx=(6, 0))

        self.btn_pkg_pack = tk.Button(
            tab,
            text="开始打包",
            bg="#4682b4",
            fg="white",
            disabledforeground="white",
            activeforeground="white",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=20,
            pady=6,
            state=tk.DISABLED,
            command=self.do_pkg_pack,
        )
        self.btn_pkg_pack.pack()

    def do_pkg_scan(self):
        self.set_status("正在扫描可打包版本...")
        self.pkg_output_var.set("(请先选择版本)")
        self.btn_pkg_pack.config(state=tk.DISABLED)
        self.btn_pkg_scan.config(state=tk.DISABLED)
        self.pkg_scan_progress.configure(value=0, maximum=100)
        self.pkg_scan_note_var.set("准备扫描 .platformio/platforms ...")

        def worker():
            entries = collect_platform_entries_with_progress(
                lambda current, total, text: self.root.after(
                    0, lambda c=current, t=total, s=text: self.update_scan_progress("pkg", c, t, s)
                )
            )
            self.root.after(0, lambda: self.finish_scan("pkg", entries))

        self.safe_bg(worker, "扫描失败")

    def do_pkg_precheck_from_project(self):
        if not self.entries_by_key:
            messagebox.showinfo("提示", "请先点击“扫描本机环境”，再进行项目依赖预检。")
            return

        ini_path = filedialog.askopenfilename(
            title="选择项目的 platformio.ini",
            filetypes=[("PlatformIO 配置", "platformio.ini"), ("INI 文件", "*.ini"), ("所有文件", "*.*")],
        )
        if not ini_path:
            return

        project_ini = Path(ini_path)
        if not project_ini.exists():
            messagebox.showerror("错误", f"文件不存在：\n{project_ini}")
            return

        try:
            env_map = resolve_env_config_map(project_ini)
        except Exception as exc:
            messagebox.showerror("预检失败", f"解析 platformio.ini 失败：\n{exc}")
            return

        if not env_map:
            messagebox.showwarning("预检结果", "未发现 [env:xxx] 配置。")
            self.pkg_precheck_var.set("预检完成：未找到任何 env。")
            return

        unique_entries = list({entry.platform_dir_name: entry for entry in self.entries_by_key.values()}.values())
        error_lines: List[str] = []
        warning_lines: List[str] = []

        for env_name in sorted(env_map.keys(), key=str.lower):
            env_cfg = env_map[env_name]
            if not env_cfg.platform_spec:
                warning_lines.append(f"[{env_name}] 缺少 platform 配置，已跳过。")
                continue

            entry = match_entry_for_platform_spec(unique_entries, env_cfg.platform_spec)
            if not entry:
                error_lines.append(f"[{env_name}] 未匹配到已扫描平台：{env_cfg.platform_spec}")
                continue

            check = check_entry_dependencies(entry, frameworks=env_cfg.frameworks)
            env_prefix = f"[{env_name}] {entry.base_name}@{entry.version_name}"

            for item in check["missing_required"]:
                error_lines.append(f"{env_prefix} 缺少必需包: {item}")
            for item in check["mismatch_required"]:
                error_lines.append(f"{env_prefix} 版本不匹配: {item}")
            for item in check["missing_optional"]:
                warning_lines.append(f"{env_prefix} 缺少可选包: {item}")
            for item in check["mismatch_optional"]:
                warning_lines.append(f"{env_prefix} 可选包版本不匹配: {item}")

        summary = f"预检完成：env {len(env_map)} 个，错误 {len(error_lines)}，提醒 {len(warning_lines)}。"
        self.pkg_precheck_var.set(summary)
        self.set_status(summary)

        preview_lines = error_lines[:10]
        if warning_lines and len(preview_lines) < 12:
            preview_lines.extend(warning_lines[: 12 - len(preview_lines)])
        detail = "\n".join(preview_lines)
        if len(error_lines) + len(warning_lines) > len(preview_lines):
            detail += "\n..."

        if error_lines:
            messagebox.showwarning("预检发现问题", summary + ("\n\n" + detail if detail else ""))
        elif warning_lines:
            messagebox.showinfo("预检完成（有提醒）", summary + ("\n\n" + detail if detail else ""))
        else:
            messagebox.showinfo("预检通过", summary + "\n\n未发现缺失或版本不匹配的必需依赖。")

    def on_pkg_tree_click(self, event):
        if self.pkg_busy:
            return
        self.handle_tree_click(self.pkg_tree, event)
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            self.pkg_output_var.set("(请先选择版本)")
            self.btn_pkg_pack.config(state=tk.DISABLED)
            return
        if len(selected_entries) == 1:
            entry = selected_entries[0]
            self.pkg_output_var.set(f"{entry.base_name}-{entry.version_name}.sdk.7z")
        else:
            self.pkg_output_var.set(f"将分别输出 {len(selected_entries)} 个 SDK 包")
        self.btn_pkg_pack.config(state=tk.NORMAL)

    def on_pkg_tree_mouse_down(self, _event):
        if self.pkg_busy:
            return "break"
        return None

    def on_pkg_tree_key_down(self, _event):
        if self.pkg_busy:
            return "break"
        return None

    def set_pkg_ui_busy(self, busy: bool):
        self.pkg_busy = busy
        self.pkg_tree.configure(style="PkgDisabled.Treeview" if busy else "Pkg.Treeview")
        self.btn_pkg_scan.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_pkg_verify.config(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.btn_pkg_pack.config(state=tk.DISABLED, text="打包中...")
        else:
            self.btn_pkg_pack.config(
                state=tk.NORMAL if self.get_selected_entries() else tk.DISABLED,
                text="开始打包",
            )

    def do_pkg_pack(self):
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            messagebox.showinfo("提示", "请先选择要打包的平台版本。")
            return
        error_lines, warning_lines = self.precheck_selected_entries(selected_entries)
        if error_lines or warning_lines:
            summary = f"打包前预检：错误 {len(error_lines)}，提醒 {len(warning_lines)}。"
            preview = error_lines[:10]
            if warning_lines and len(preview) < 12:
                preview.extend(warning_lines[: 12 - len(preview)])
            detail = "\n".join(preview)
            if len(error_lines) + len(warning_lines) > len(preview):
                detail += "\n..."
            prompt = summary + ("\n\n" + detail if detail else "") + "\n\n仍要继续打包吗？"
            if not messagebox.askyesno("预检提示", prompt, icon="warning"):
                self.pkg_precheck_var.set(summary + " 已取消打包。")
                return
            self.pkg_precheck_var.set(summary + " 已选择继续打包。")
        if not SEVEN_ZIP.exists():
            messagebox.showerror("错误", f"未找到 7z.exe：\n{SEVEN_ZIP}")
            return

        output_hint = self.pkg_output_var.get().strip()
        if not output_hint or output_hint.startswith("("):
            messagebox.showinfo("提示", "请先选择要打包的平台版本。")
            return
        self.set_pkg_ui_busy(True)
        self.pkg_status.config(text="正在准备离线 SDK...", foreground="darkblue")
        self.pkg_scan_progress.configure(value=0, maximum=100)
        self.pkg_scan_note_var.set("准备打包离线 SDK ...")
        self.set_status("正在打包离线 SDK...")

        def worker():
            successes = []
            failures = []
            total = len(selected_entries)
            for index, entry in enumerate(selected_entries, start=1):
                output_name = f"{entry.base_name}-{entry.version_name}.sdk.7z"
                output_path = SDK_DIR / output_name
                self.root.after(
                    0,
                    lambda i=index, total=total, name=output_name:
                        self.update_pkg_pack_progress(i - 1, total, f"正在准备 {name}"),
                )
                try:
                    self.root.after(
                        0,
                        lambda i=index, total=total, name=output_name:
                            self.update_pkg_pack_progress(i - 1, total, f"正在打包 {name}"),
                    )
                    stage_single_sdk(output_path, entry)
                    successes.append(output_name)
                except Exception as exc:
                    failures.append(f"{output_name}: {exc}")
                self.root.after(
                    0,
                    lambda i=index, total=total, name=output_name:
                        self.update_pkg_pack_progress(i, total, f"已完成 {name}"),
                )

            def finish():
                self.set_pkg_ui_busy(False)
                self.pkg_scan_progress.configure(value=100)
                if successes and not failures:
                    self.pkg_status.config(text=f"打包完成: 共生成 {len(successes)} 个 SDK 包", foreground="green")
                    self.pkg_scan_note_var.set("打包完成。")
                    self.set_status("打包完成")
                    messagebox.showinfo(
                        "完成",
                        "离线 SDK 已生成。\n\n"
                        + "\n".join(successes[:12])
                        + ("\n..." if len(successes) > 12 else "")
                        + "\n\n"
                        "使用方式：\n"
                        "1. 先确保目标机器已经有可用的 PlatformIO 基础环境\n"
                        "2. 再在“SDK 管理 / 安装 SDK”页导入此 SDK",
                    )
                else:
                    if successes:
                        self.pkg_status.config(text=f"部分完成: 成功 {len(successes)} 个，失败 {len(failures)} 个", foreground="darkorange")
                        self.pkg_scan_note_var.set("打包部分完成。")
                        self.set_status("部分打包完成")
                        messagebox.showwarning(
                            "部分完成",
                            "以下 SDK 已成功生成：\n\n"
                            + "\n".join(successes[:10])
                            + ("\n..." if len(successes) > 10 else "")
                            + "\n\n失败项：\n"
                            + "\n".join(failures[:10]),
                        )
                    else:
                        self.pkg_status.config(text="打包失败", foreground="red")
                        self.pkg_scan_note_var.set("打包失败。")
                        self.set_status("打包失败")
                        messagebox.showerror("打包失败", "\n".join(failures[:10]) if failures else "7z 打包失败")

            self.root.after(0, finish)

        self.safe_bg(worker, "打包失败")

    def precheck_selected_entries(self, selected_entries: List[PlatformEntry]) -> tuple[List[str], List[str]]:
        error_lines: List[str] = []
        warning_lines: List[str] = []
        for entry in selected_entries:
            check = check_entry_dependencies(entry, frameworks=None)
            prefix = f"[{entry.base_name}@{entry.version_name}]"
            for item in check["missing_required"]:
                error_lines.append(f"{prefix} 缺少必需包: {item}")
            for item in check["mismatch_required"]:
                error_lines.append(f"{prefix} 版本不匹配: {item}")
            for item in check["missing_optional"]:
                warning_lines.append(f"{prefix} 缺少可选包: {item}")
            for item in check["mismatch_optional"]:
                warning_lines.append(f"{prefix} 可选包版本不匹配: {item}")
        return error_lines, warning_lines

    def build_install_sdk_content(self, tab):
        ttk.Label(tab, text="选择要安装的离线 SDK", font=self.font_bold).pack(anchor=tk.W)
        ttk.Label(
            tab,
            text="本工具会将离线 SDK 导入到当前用户已安装好的 .platformio 环境。",
            foreground="#666",
        ).pack(anchor=tk.W, pady=(2, 8))

        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        self.ins_scan_btn = ttk.Button(top, text="扫描安装包", command=self.do_ins_scan)
        self.ins_scan_btn.pack(side=tk.LEFT)
        self.ins_pick_btn = ttk.Button(top, text="选择外部 7z 包", command=self.pick_bundle)
        self.ins_pick_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.ins_status = ttk.Label(top, text="尚未扫描", foreground="gray")
        self.ins_status.pack(side=tk.LEFT, padx=(12, 0))
        self.ins_note_var = tk.StringVar(value="说明：安装时会解压 SDK，并逐个复制平台目录和依赖包目录。大包首次导入可能需要几分钟。")
        ttk.Label(tab, textvariable=self.ins_note_var, foreground="#4f6b8a").pack(anchor=tk.W, pady=(6, 6))
        self.ins_progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.ins_progress.pack(fill=tk.X, pady=(0, 8))

        list_frame = ttk.Frame(tab)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 8))
        columns = ("文件名", "大小", "平台明细")
        self.ins_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.ins_tree.heading("文件名", text="文件名")
        self.ins_tree.heading("大小", text="大小")
        self.ins_tree.heading("平台明细", text="平台明细")
        self.ins_tree.column("文件名", width=420)
        self.ins_tree.column("大小", width=120, stretch=False, anchor=tk.CENTER)
        self.ins_tree.column("平台明细", width=640)
        self.ins_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.ins_tree.yview)
        self.ins_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.bundle_paths: Dict[str, Path] = {}
        self.bundle_manifest_cache: Dict[str, Optional[dict]] = {}

        self.btn_install = tk.Button(
            tab,
            text="安装选中 SDK",
            bg="#3c8d40",
            fg="white",
            disabledforeground="white",
            activeforeground="white",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=20,
            pady=6,
            state=tk.DISABLED,
            command=self.do_install_bundle,
        )
        self.btn_install.pack()
        self.ins_tree.bind("<<TreeviewSelect>>", lambda _e: self.btn_install.config(state=tk.NORMAL if self.ins_tree.selection() else tk.DISABLED))
        self.ins_tree.bind("<Button-1>", self.on_install_tree_mouse_down, add="+")
        self.ins_tree.bind("<Key>", self.on_install_tree_key_down, add="+")

    def on_install_tree_mouse_down(self, _event):
        if self.install_busy:
            return "break"
        return None

    def on_install_tree_key_down(self, _event):
        if self.install_busy:
            return "break"
        return None

    def set_install_ui_busy(self, busy: bool):
        self.install_busy = busy
        self.btn_install.config(state=tk.DISABLED if busy else (tk.NORMAL if self.ins_tree.selection() else tk.DISABLED))
        for btn in ("ins_scan_btn", "ins_pick_btn"):
            widget = getattr(self, btn, None)
            if widget:
                widget.config(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.install_locked_selection = tuple(self.ins_tree.selection())
        else:
            self.install_locked_selection = ()

    def summarize_bundle(self, manifest: Optional[dict]) -> str:
        if not manifest or not manifest.get("entries"):
            return "普通 7z 包（无 manifest）"
        return ", ".join(
            f"{item['base_name']}@{item['version_name']}"
            for item in manifest.get("entries", [])[:5]
        )

    def add_bundle_row(self, bundle_path: Path):
        if bundle_path.name in self.bundle_paths:
            return
        manifest = parse_bundle_manifest(bundle_path)
        self.bundle_paths[bundle_path.name] = bundle_path
        self.bundle_manifest_cache[bundle_path.name] = manifest
        detail = self.summarize_bundle(manifest)
        size_mb = round(bundle_path.stat().st_size / (1024 * 1024), 1)
        self.ins_tree.insert("", tk.END, values=(bundle_path.name, format_size(size_mb), detail))

    def do_ins_scan(self):
        if self.install_busy:
            return
        self.ins_tree.delete(*self.ins_tree.get_children())
        self.bundle_paths.clear()
        self.bundle_manifest_cache.clear()
        bundle_dirs = [SDK_DIR]
        if SDK_BUNDLED_DIR.exists() and SDK_BUNDLED_DIR != SDK_DIR:
            bundle_dirs.append(SDK_BUNDLED_DIR)
        bundles: List[Path] = []
        for bundle_dir in bundle_dirs:
            bundles.extend(sorted(bundle_dir.glob("*.7z"), key=lambda p: p.name.lower()))
        for bundle in bundles:
            self.add_bundle_row(bundle)
        if bundles:
            self.ins_status.config(text=f"找到 {len(self.bundle_paths)} 个离线包", foreground="gray")
        else:
            self.ins_status.config(text="未找到 .7z 离线包", foreground="darkorange")
        self.btn_install.config(state=tk.DISABLED)

    def pick_bundle(self):
        if self.install_busy:
            return
        filename = filedialog.askopenfilename(
            title="选择离线 SDK 7z 包",
            filetypes=[("7z 压缩包", "*.7z"), ("所有文件", "*.*")],
        )
        if not filename:
            return
        self.add_bundle_row(Path(filename))
        self.ins_status.config(text="已加载外部离线包", foreground="gray")

    def update_install_progress(self, current: int, total: int, text: str):
        if str(self.ins_progress.cget("mode")) != "determinate":
            self.ins_progress.stop()
            self.ins_progress.configure(mode="determinate")
        total_safe = max(total, 1)
        percent = min(100, round(current * 100 / total_safe))
        self.ins_progress.configure(value=percent)
        self.ins_note_var.set(f"安装进度 {percent}%：{text}")
        self.ins_status.config(text=f"安装中... {current}/{total}", foreground="darkblue")
        self.set_status(text)

    def begin_install_extract_feedback(self, bundle_name: str):
        self.ins_progress.stop()
        self.ins_progress.configure(mode="indeterminate")
        self.ins_progress.start(12)
        self.ins_status.config(text="正在解压离线 SDK...", foreground="darkblue")
        self.ins_note_var.set(f"正在解压 {bundle_name}。大包首次导入可能需要几分钟，请耐心等待。")
        self.set_status(f"正在解压 {bundle_name}")

    def tick_install_extract_feedback(self, bundle_name: str, elapsed_seconds: int):
        self.ins_status.config(text=f"正在解压离线 SDK... {elapsed_seconds}s", foreground="darkblue")
        self.ins_note_var.set(
            f"正在解压 {bundle_name}，已耗时 {elapsed_seconds}s。大包首次导入可能需要几分钟，请耐心等待。"
        )
        self.set_status(f"正在解压 {bundle_name}（{elapsed_seconds}s）")

    def do_install_bundle(self):
        selection = self.ins_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一个离线 SDK。")
            return
        if not SEVEN_ZIP.exists():
            messagebox.showerror("错误", f"未找到 7z.exe：\n{SEVEN_ZIP}")
            return
        try:
            ensure_pio_cli()
        except FileNotFoundError as exc:
            messagebox.showerror("错误", str(exc))
            return

        values = self.ins_tree.item(selection[0], "values")
        bundle_name = values[0]
        bundle_path = self.bundle_paths[bundle_name]
        manifest = self.bundle_manifest_cache.get(bundle_name)
        temp_dir = Path(tempfile.gettempdir())
        unpacked_size = estimate_archive_unpacked_size(bundle_path)
        if unpacked_size > 0:
            free_bytes = shutil.disk_usage(temp_dir).free
            reserve_bytes = 512 * 1024 * 1024
            required_bytes = unpacked_size + reserve_bytes
            if free_bytes < required_bytes:
                need_gb = required_bytes / (1024 ** 3)
                free_gb = free_bytes / (1024 ** 3)
                messagebox.showerror(
                    "空间不足",
                    "安装前检查发现临时目录所在磁盘可用空间不足。\n\n"
                    f"临时目录: {temp_dir}\n"
                    f"预计解压后至少需要: {need_gb:.1f} GB\n"
                    f"当前可用空间: {free_gb:.1f} GB\n\n"
                    "请先清理系统盘空间，或把 TEMP/TMP 指到空间更大的磁盘后重试。",
                )
                return

        overwrite_hint = "会覆盖同名平台目录和包目录。"
        if manifest and manifest.get("entries"):
            detail = "\n".join(f"  - {e['base_name']} / {e['version_name']}" for e in manifest["entries"])
            msg = f"即将安装以下 SDK：\n\n{detail}\n\n{overwrite_hint}\n继续吗？"
        else:
            msg = f"即将解压并覆盖 SDK 内容：\n{bundle_name}\n\n{overwrite_hint}\n继续吗？"
        if not messagebox.askyesno("确认安装", msg, icon="warning"):
            return

        self.set_install_ui_busy(True)
        self.btn_install.config(state=tk.DISABLED, text="安装中...")
        self.ins_status.config(text="正在安装离线 SDK...", foreground="darkblue")
        self.ins_progress.stop()
        self.ins_progress.configure(mode="determinate", value=0, maximum=100)
        self.ins_note_var.set("准备解压离线 SDK ...")
        self.set_status("正在安装离线 SDK...")

        def worker():
            temp_root = Path(tempfile.mkdtemp(prefix="sdk_", dir=str(temp_dir)))
            short_root = temp_root / "w"
            short_pio = short_root / "p"
            temp_drive = ""
            pio_drive = ""
            try:
                target_platforms = PIO_ROOT / "platforms"
                target_packages = PIO_ROOT / "packages"
                target_platforms.mkdir(parents=True, exist_ok=True)
                target_packages.mkdir(parents=True, exist_ok=True)
                short_root.mkdir(parents=True, exist_ok=True)
                create_junction(short_pio, PIO_ROOT)
                temp_drive = create_subst_drive(temp_root, ["T", "U", "V"])
                pio_drive = create_subst_drive(short_pio, ["P", "Q", "R"])
                temp_sdk_root = Path(temp_drive) / "sdk"
                pio_platforms_short = Path(pio_drive) / "platforms"
                pio_packages_short = Path(pio_drive) / "packages"

                list_proc = run_command([str(SEVEN_ZIP), "l", "-slt", str(bundle_path)])
                if list_proc.returncode != 0:
                    raise RuntimeError(list_proc.stdout.strip() or "无法读取离线 SDK 目录结构")

                platform_names: Set[str] = set()
                package_names: Set[str] = set()
                for line in list_proc.stdout.splitlines():
                    if not line.startswith("Path = sdk\\"):
                        continue
                    rel = line[11:].strip()
                    if rel.startswith("platforms\\"):
                        rest = rel[10:]
                        if rest:
                            platform_names.add(rest.split("\\", 1)[0])
                    elif rel.startswith("packages\\"):
                        rest = rel[9:]
                        if rest:
                            package_names.add(rest.split("\\", 1)[0])

                platform_sources = sorted(platform_names, key=str.lower)
                package_sources = sorted(package_names, key=str.lower)
                if not platform_sources and not package_sources:
                    raise RuntimeError("离线包中未找到可安装的 platforms/packages 内容。")

                total_steps = 1 + len(platform_sources) + len(package_sources)
                step_index = 1
                installed_platforms = []
                installed_packages = []
                deferred_cleanup: List[Path] = []

                self.root.after(0, lambda name=bundle_name: self.begin_install_extract_feedback(name))
                start_time = time.time()
                while step_index <= total_steps:
                    elapsed = int(time.time() - start_time)
                    self.root.after(0, lambda name=bundle_name, sec=elapsed: self.tick_install_extract_feedback(name, sec))
                    break
                self.root.after(0, lambda ts=total_steps: self.update_install_progress(1, ts, "已读取 SDK 目录结构，开始分批安装"))
                temp_output_dir = temp_drive.rstrip("\\")

                for name in platform_sources:
                    self.root.after(0, lambda idx=step_index, ts=total_steps, n=name: self.update_install_progress(idx, ts, f"正在安装平台目录 {n}"))
                    dst = target_platforms / name
                    remove_tree(dst)
                    proc = run_command([str(SEVEN_ZIP), "x", "-y", f"-o{temp_output_dir}", str(bundle_path), f"sdk\\platforms\\{name}\\*"])
                    if proc.returncode != 0:
                        raise RuntimeError(proc.stdout.strip() or f"解压平台目录失败: {name}")
                    extracted = temp_sdk_root / "platforms" / name
                    if not extracted.exists():
                        raise RuntimeError(f"未找到解压后的平台目录: {name}")
                    robocopy_tree(extracted, pio_platforms_short / name)
                    if not try_remove_tree(extracted):
                        deferred_cleanup.append(extracted)
                    installed_platforms.append(name)
                    step_index += 1

                for name in package_sources:
                    self.root.after(0, lambda idx=step_index, ts=total_steps, n=name: self.update_install_progress(idx, ts, f"正在安装依赖包目录 {n}"))
                    dst = target_packages / name
                    remove_tree(dst)
                    proc = run_command([str(SEVEN_ZIP), "x", "-y", f"-o{temp_output_dir}", str(bundle_path), f"sdk\\packages\\{name}\\*"])
                    if proc.returncode != 0:
                        raise RuntimeError(proc.stdout.strip() or f"解压依赖包目录失败: {name}")
                    extracted = temp_sdk_root / "packages" / name
                    if not extracted.exists():
                        raise RuntimeError(f"未找到解压后的依赖包目录: {name}")
                    robocopy_tree(extracted, pio_packages_short / name)
                    if not try_remove_tree(extracted):
                        deferred_cleanup.append(extracted)
                    installed_packages.append(name)
                    step_index += 1
            finally:
                if pio_drive:
                    remove_subst_drive(pio_drive)
                if temp_drive:
                    remove_subst_drive(temp_drive)
                remove_junction(short_pio)
                for path in deferred_cleanup if 'deferred_cleanup' in locals() else []:
                    try_remove_tree(path)
                shutil.rmtree(temp_root, ignore_errors=True)

            def finish():
                self.btn_install.config(text="安装选中 SDK")
                self.set_install_ui_busy(False)
                self.ins_progress.configure(value=100)
                self.ins_note_var.set("安装完成。说明：平台目录和依赖包目录已复制到当前用户 .platformio。")
                self.ins_status.config(
                    text=f"安装完成：{len(installed_platforms)} 个平台目录，{len(installed_packages)} 个包目录",
                    foreground="green",
                )
                self.set_status("安装完成")
                messagebox.showinfo(
                    "安装完成",
                    "离线 SDK 已安装到当前用户 `.platformio`。\n\n"
                    f"平台目录: {len(installed_platforms)}\n"
                    f"包目录: {len(installed_packages)}\n\n"
                    "建议重新打开你的工程后再进行编译。",
                )

            self.root.after(0, finish)

        self.safe_bg(worker, "安装失败")

    # ==================== 通用 Tree 行为 ====================
    def update_scan_progress(self, target: str, current: int, total: int, text: str):
        total_safe = max(total, 1)
        percent = min(100, round(current * 100 / total_safe))
        if target == "clean":
            self.clean_scan_progress.configure(value=percent)
            self.clean_scan_note_var.set(f"扫描进度 {percent}%：{text}")
            self.clean_summary_var.set(f"扫描中... {current}/{total}")
        else:
            self.pkg_scan_progress.configure(value=percent)
            self.pkg_scan_note_var.set(f"扫描进度 {percent}%：{text}")
            self.pkg_status.config(text=f"扫描中... {current}/{total}", foreground="darkblue")
        self.set_status(text)

    def update_pkg_pack_progress(self, current: int, total: int, text: str):
        total_safe = max(total, 1)
        percent = min(100, round(current * 100 / total_safe))
        self.pkg_scan_progress.configure(value=percent)
        self.pkg_scan_note_var.set(f"打包进度 {percent}%：{text}")
        self.pkg_status.config(text=f"打包中... {current}/{total}", foreground="darkblue")
        self.set_status(text)

    def finish_scan(self, target: str, entries: List[PlatformEntry]):
        if target == "clean":
            self.btn_clean_scan.config(state=tk.NORMAL)
            self.populate_tree(self.clean_tree, entries, update_summary=self.clean_summary_var.set)
            self.clean_scan_progress.configure(value=100)
            self.clean_scan_note_var.set(
                "扫描完成。说明：大小统计最耗时，平台和依赖越多，扫描越久。"
            )
        else:
            self.btn_pkg_scan.config(state=tk.NORMAL)
            self.populate_tree(self.pkg_tree, entries, update_summary=self.pkg_status.config, summary_key="text")
            self.pkg_scan_progress.configure(value=100)
            self.pkg_scan_note_var.set(
                "扫描完成。说明：已按“基平台 + 版本明细”展示，可直接勾选版本打包。"
            )
        self.set_status("扫描完成")

    def populate_tree(self, tree: ttk.Treeview, entries: List[PlatformEntry], update_summary, summary_key: Optional[str] = None):
        tree.delete(*tree.get_children())
        self.entries_by_key.clear()
        self.group_items.clear()
        self.child_items.clear()
        self.group_children.clear()
        self.checked_keys.clear()
        grouped: Dict[str, List[PlatformEntry]] = {}
        for entry in entries:
            grouped.setdefault(entry.base_name, []).append(entry)

        total_versions = len(entries)
        for base_name in sorted(grouped.keys(), key=str.lower):
            versions = sorted(grouped[base_name], key=lambda e: e.version_name.lower())
            group_size = round(sum(v.size_mb for v in versions), 1)
            group_id = tree.insert(
                "",
                tk.END,
                text="☐ " + base_name,
                values=(f"{len(versions)} 个版本", format_size(group_size), self.group_preview(versions)),
                open=True,
            )
            self.group_items[base_name] = group_id
            self.group_children[base_name] = []

            for entry in versions:
                key = f"{entry.base_name}|{entry.platform_dir_name}"
                self.entries_by_key[key] = entry
                pkg_preview = ", ".join(entry.package_dirs[:4])
                if len(entry.package_dirs) > 4:
                    pkg_preview += f" ... 共{len(entry.package_dirs)}项"
                elif not pkg_preview:
                    pkg_preview = "(无依赖包目录)"
                child_id = tree.insert(
                    group_id,
                    tk.END,
                    text="☐",
                    values=(
                        f"{entry.version_name} / {entry.platform_dir_name}",
                        format_size(entry.size_mb),
                        pkg_preview,
                    ),
                    tags=(key,),
                )
                self.child_items[key] = child_id
                self.group_children[base_name].append(key)

        summary = f"扫描完成：{len(grouped)} 个基平台，{total_versions} 个版本"
        if summary_key:
            update_summary(**{summary_key: summary})
        else:
            update_summary(summary)

    def group_preview(self, versions: List[PlatformEntry]) -> str:
        names = [v.version_name for v in versions[:4]]
        preview = ", ".join(names)
        if len(versions) > 4:
            preview += f" ... 共{len(versions)}个版本"
        return preview

    def handle_tree_click(self, tree: ttk.Treeview, event):
        item = tree.identify_row(event.y)
        if not item:
            return
        parent = tree.parent(item)
        if not parent:
            label = tree.item(item, "text").replace("☑ ", "").replace("☐ ", "")
            keys = self.group_children.get(label, [])
            should_check = not all(key in self.checked_keys for key in keys)
            for key in keys:
                if should_check:
                    self.checked_keys.add(key)
                else:
                    self.checked_keys.discard(key)
                child_item = self.child_items.get(key)
                if child_item:
                    tree.item(child_item, text="☑" if should_check else "☐")
            tree.item(item, text=("☑ " if should_check else "☐ ") + label)
            return

        tags = tree.item(item, "tags")
        if not tags:
            return
        key = tags[0]
        if key in self.checked_keys:
            self.checked_keys.remove(key)
            tree.item(item, text="☐")
        else:
            self.checked_keys.add(key)
            tree.item(item, text="☑")

        group_item = parent
        base_name = tree.item(group_item, "text").replace("☑ ", "").replace("☐ ", "")
        keys = self.group_children.get(base_name, [])
        all_checked = keys and all(k in self.checked_keys for k in keys)
        tree.item(group_item, text=("☑ " if all_checked else "☐ ") + base_name)

    def get_selected_entries(self) -> List[PlatformEntry]:
        entries = [self.entries_by_key[key] for key in sorted(self.checked_keys) if key in self.entries_by_key]
        return entries


def main():
    root = tk.Tk()
    app = PioManager(root)
    root.mainloop()


def open_sdk_manager_window(master: tk.Misc | None = None) -> PioManager:
    window = tk.Toplevel(master) if master else tk.Tk()
    app = PioManager(window)
    return app


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            ERROR_LOG.write_text(tb, encoding="utf-8")
        except Exception:
            pass
        show_startup_error(
            "程序启动失败，详细错误已写入：\n"
            + str(ERROR_LOG)
            + "\n\n"
            + tb[-1500:]
        )
        raise

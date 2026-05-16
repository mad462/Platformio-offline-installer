from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def resource_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def main() -> int:
    base_dir = resource_root()
    script = base_dir / "setup_offline_pio.ps1"
    if not script.exists():
        print(f"[Error] Missing script: {script}")
        return 1

    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    proc = subprocess.run(cmd, cwd=str(base_dir))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

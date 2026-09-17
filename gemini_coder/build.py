"""Build script to create a clickable .exe for Gemini Coder."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "gemini_coder"
ICON_PATH = APP_DIR / "icon.ico"

def build():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "GeminiCoder",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--add-data", f"{APP_DIR / 'ui'}{';'}gemini_coder/ui",
        str(APP_DIR / "launch.pyw"),
    ]

    if ICON_PATH.exists():
        cmd.insert(4, "--icon")
        cmd.insert(5, str(ICON_PATH))

    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    print("\nBuild complete! Find GeminiCoder.exe in dist/")

if __name__ == "__main__":
    build()

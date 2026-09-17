"""Build a single Autocoder.exe and place it on the Desktop.

Uses PyInstaller --onefile --windowed. Bundles the Autocoder package
(this repo) and the sibling ``gemini_coder`` package (usually
``../gemini_coder`` under Desktop/AI; falls back to
``../claude interaction tool/gemini_coder``).

Run: python build_exe.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent                # .../gemini_coder_web (repo root)
ROOT = HERE.parent                                    # e.g. Desktop/AI


def _gemini_coder_pkg() -> Path:
    """Directory containing the gemini_coder package (has __init__.py)."""
    candidates = (
        ROOT / "gemini_coder",
        ROOT / "claude interaction tool" / "gemini_coder",
        ROOT / "claude_interaction_tool" / "gemini_coder",
    )
    for p in candidates:
        if (p / "__init__.py").is_file():
            return p.resolve()
    raise FileNotFoundError(
        "gemini_coder package not found next to this repo. Tried:\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


GEMINI_CODER_PKG = _gemini_coder_pkg()
GEMINI_CODER_PATH_PARENT = GEMINI_CODER_PKG.parent
DIST = HERE / "dist"
BUILD = HERE / "build"
DESKTOP = Path.home() / "Desktop"
# Additional drop locations — every build copies the fresh exe here too.
# Both folder names are populated; "Program Exe and Batch" is where it's been
# living, "exe and batch" is a shorter sibling folder the user also watches.
EXTRA_DROPS = [
    DESKTOP / "AI2" / "Program Exe and Batch",
    DESKTOP / "AI2" / "exe and batch",
]
APP_NAME = "Autocoder"
ENTRY = HERE / "_autocoder_entry.py"
SPEC = HERE / f"{APP_NAME}.spec"


ENTRY_SRC = '''"""Frozen-exe entry point for Autocoder."""
import io
import logging
import os
import sys
from pathlib import Path

# Make stdout/stderr safe for frozen windowed exe
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
elif sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure bundled packages are importable
base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
if str(base) not in sys.path:
    sys.path.insert(0, str(base))

log_dir = Path.home() / ".autocoder"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(log_dir / "autocoder.log", encoding="utf-8")],
)

from Autocoder.ui.app_web import AutocoderApp

app = AutocoderApp()

# Clamp to screen, center, and force to foreground
sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
w = min(app._cfg.window_width,  max(1000, sw - 80))
h = min(app._cfg.window_height, max(650,  sh - 140))
x = max(10, (sw - w) // 2)
y = max(10, (sh - h) // 3)
app.geometry(f"{w}x{h}+{x}+{y}")
app.update_idletasks()
app.deiconify()
app.lift()
app.attributes("-topmost", True)
app.after(400, lambda: app.attributes("-topmost", False))
app.focus_force()

app.mainloop()
'''


def write_entry():
    ENTRY.write_text(ENTRY_SRC, encoding="utf-8")
    print(f"[1/4] Entry script: {ENTRY}")


def run_pyinstaller():
    selectors = HERE / "default_selectors.json"

    # Icon to embed at build time. Using PyInstaller's --icon=... is
    # safe for --onefile exes; post-build UpdateResource corrupted the
    # appended archive (shrunk a 63 MB exe to 350 KB) so we do it here
    # instead.
    icon_candidates = (
        Path.home() / "Desktop" / "AI2" / "Autocoder_src" / "autocoder_icon.ico",
        HERE / "autocoder_icon.ico",
    )
    icon_file = next((p for p in icon_candidates if p.is_file()), None)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",
        "--onefile",]
    if icon_file is not None:
        cmd.append(f"--icon={icon_file}")
        print(f"[icon] Embedding icon at build time: {icon_file}")
    else:
        print("[icon] WARN: no autocoder_icon.ico found; building without icon")

    cmd.extend([
        # Include Autocoder sources + gemini_coder import root
        f"--paths={ROOT}",
        f"--paths={GEMINI_CODER_PATH_PARENT}",
        # Bundle the selectors data file so it's inside the exe
        f"--add-data={selectors};Autocoder",
        # Bundle the Autocoder source tree into _MEIPASS
        f"--add-data={HERE};Autocoder",
        # Bundle the gemini_coder source tree
        f"--add-data={GEMINI_CODER_PKG};gemini_coder",
        # Hidden imports — Autocoder package
        "--hidden-import=Autocoder",
        "--hidden-import=Autocoder.ui",
        "--hidden-import=Autocoder.ui.app_web",
        "--hidden-import=Autocoder.ai_profiles",
        "--hidden-import=Autocoder.auto_save",
        "--hidden-import=Autocoder.broadcast",
        "--hidden-import=Autocoder.browser_actions",
        "--hidden-import=Autocoder.browser_client",
        "--hidden-import=Autocoder.cdp_client",
        "--hidden-import=Autocoder.session_manager",
        "--hidden-import=Autocoder.universal_client",
        "--hidden-import=Autocoder.window_manager",
        "--hidden-import=Autocoder.project_maintainer",
        # Hidden imports — gemini_coder base package
        "--hidden-import=gemini_coder",
        "--hidden-import=gemini_coder.ui",
        "--hidden-import=gemini_coder.ui.app",
        "--hidden-import=gemini_coder.ui.theme",
        "--hidden-import=gemini_coder.config",
        "--hidden-import=gemini_coder.task_manager",
        "--hidden-import=gemini_coder.expander",
        "--hidden-import=gemini_coder.history",
        "--hidden-import=gemini_coder.platform_utils",
        # Third-party
        "--hidden-import=customtkinter",
        "--hidden-import=pyautogui",
        "--hidden-import=pyperclip",
        "--hidden-import=pystray",
        "--hidden-import=PIL",
        "--hidden-import=psutil",
        "--hidden-import=websocket",
        "--collect-all=customtkinter",
        str(ENTRY),
    ])

    print(f"[2/4] Running PyInstaller (this takes a few minutes)...")
    result = subprocess.run(cmd, cwd=str(HERE))
    if result.returncode != 0:
        print(f"ERROR: PyInstaller failed (exit {result.returncode})")
        sys.exit(result.returncode)


def copy_to_desktop():
    src = DIST / f"{APP_NAME}.exe"
    if not src.exists():
        print(f"ERROR: {src} not found")
        sys.exit(1)
    primary = DESKTOP / f"{APP_NAME}.exe"
    shutil.copy2(src, primary)
    size_mb = primary.stat().st_size / (1024 * 1024)
    print(f"[3/4] Copied to Desktop: {primary} ({size_mb:.1f} MB)")
    # Also copy to EXTRA_DROPS so every build refreshes every known location
    for extra_dir in EXTRA_DROPS:
        try:
            extra_dir.mkdir(parents=True, exist_ok=True)
            extra_dst = extra_dir / f"{APP_NAME}.exe"
            shutil.copy2(src, extra_dst)
            print(f"[3/4] Also copied to: {extra_dst}")
        except Exception as e:
            print(f"[3/4] WARN: could not copy to {extra_dir}: {e}")
    return primary


def cleanup():
    if ENTRY.exists():
        ENTRY.unlink()
    if SPEC.exists():
        SPEC.unlink()
    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    print(f"[4/4] Cleaned up temp build artifacts")


def main():
    print(f"\n{'='*60}\n  Building {APP_NAME}.exe -> Desktop\n{'='*60}\n")
    write_entry()
    # PyInstaller's --icon= embeds the icon at build time — safe for --onefile.
    # Post-build UpdateResource (apply_icon.py) is NOT used here because it
    # truncates PyInstaller --onefile exes: the icon patch only rewrites the
    # PE sections and discards the appended archive, leaving a 350 KB stub.
    run_pyinstaller()
    dst = copy_to_desktop()
    cleanup()
    print(f"\n{'='*60}\n  DONE: {dst}\n{'='*60}\n")


if __name__ == "__main__":
    main()

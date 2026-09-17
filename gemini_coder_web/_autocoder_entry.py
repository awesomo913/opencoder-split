"""Frozen-exe entry point for Autocoder."""
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

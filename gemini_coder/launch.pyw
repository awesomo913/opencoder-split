"""Clickable launcher for Gemini Coder (.pyw = no console window on Windows)."""

import sys
from pathlib import Path

# Ensure the parent directory is on the path so the package can be found
parent = str(Path(__file__).resolve().parent.parent)
if parent not in sys.path:
    sys.path.insert(0, parent)

from gemini_coder.ui.app import GeminiCoderApp


def main():
    app = GeminiCoderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

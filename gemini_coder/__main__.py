"""Entry point for Gemini Coder - run with: python -m gemini_coder"""

import argparse
import io
import logging
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import __version__, __app_name__


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gemini-coder",
        description=f"{__app_name__} v{__version__} - Automated coding with Google Gemini",
    )
    parser.add_argument(
        "--version", action="version", version=f"{__app_name__} {__version__}"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level",
    )
    parser.add_argument(
        "--theme",
        choices=["dark", "light"],
        default=None,
        help="Override UI theme",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Set API key (prefer config file for persistence)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode (future: CLI interface)",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Generate diagnostic report and exit",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.log_level:
        logging.basicConfig(level=getattr(logging, args.log_level))

    if args.diagnostic:
        from .platform_utils import detect_platform, get_config_dir
        from .config import ConfigManager
        from .task_manager import TaskQueue
        from .diagnostics import generate_diagnostic_report

        platform_info = detect_platform()
        config_mgr = ConfigManager()
        queue = TaskQueue(save_path=get_config_dir() / "task_queue.json")

        report = generate_diagnostic_report(
            platform_info=platform_info,
            config=config_mgr.config,
            gemini_configured=bool(config_mgr.config.api_key),
            task_queue=queue,
            uptime_seconds=0,
        )
        print(report)
        return

    if args.headless:
        print(f"{__app_name__} v{__version__}")
        print("Headless/CLI mode is planned for a future release.")
        print("Use --diagnostic to generate a system report.")
        return

    from .config import ConfigManager
    config_mgr = ConfigManager()
    if args.api_key:
        config_mgr.update(api_key=args.api_key)
    if args.theme:
        config_mgr.update(theme=args.theme)

    from .ui.app import GeminiCoderApp
    app = GeminiCoderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

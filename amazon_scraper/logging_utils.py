from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import warnings
from typing import Optional


def configure_logging(
    level: str = "DEBUG",
    log_file: Optional[str] = "logs/scraper.log",
    console: bool = True,
) -> None:
    """Configure logging for CLI execution with file persistence by default."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s:%(lineno)d - %(message)s",
        handlers=handlers,
        force=True,
    )

    # Selenium's wire-level DEBUG logs include entire HTML responses, which makes rotated logs unusable.
    logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.INFO)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)

    # Route Python warnings into standard logging so warning-level events are persisted.
    logging.captureWarnings(True)
    warnings.simplefilter("default")

    # Capture uncaught exceptions in both main thread and worker threads.
    def _log_unhandled_exception(exc_type: type[BaseException], exc_value: BaseException, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger(__name__).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _threading_exception_handler(args: threading.ExceptHookArgs) -> None:
        logging.getLogger(__name__).critical(
            "Unhandled thread exception thread=%s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_unhandled_exception
    threading.excepthook = _threading_exception_handler

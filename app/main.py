from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app import theme
from app.backend import create_backend
from app.main_window import MainWindow
from app.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Live viewer for files on SSH servers")
    parser.add_argument("host", nargs="?", help="user@host or ~/.ssh/config alias")
    parser.add_argument("path", nargs="?", help="remote folder to open")
    parser.add_argument("--backend", choices=("auto", "python", "rust"), default="auto")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Remote Viewer")
    settings = Settings()
    theme.apply(app, settings.theme)
    backend = create_backend(args.backend, settings.cache_mb * 1024 * 1024)
    window = MainWindow(backend, settings)
    window.show()
    if args.host:
        QTimer.singleShot(0, lambda: window.connect_to(args.host, args.path))
    status = app.exec()
    backend.close()
    return status


if __name__ == "__main__":
    sys.exit(main())

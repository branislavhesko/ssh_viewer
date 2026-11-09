"""The app icon, and the desktop entry Linux docks need to show it.

GNOME on Wayland ignores icons set by the window: it looks up the .desktop
file named by the app id, and takes the icon from there.

``python -m app.icon out.png`` renders it for packaging.
"""
from __future__ import annotations

import os
import shlex
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

APP_ID = "remote-viewer"

# A folder holding a shell prompt, on the accent blue. The 824 px tile inside
# 1024 leaves the margin macOS expects around app icons.
SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs>
<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#6d96ff"/><stop offset="1" stop-color="#2f5cd4"/>
</linearGradient>
</defs>
<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#tile)"/>
<path fill="#ffffff" fill-opacity="0.55" d="M232 318a46 46 0 0 1 46-46h168l58 58h234a46 46 0 0 1 46 46v40H232z"/>
<path fill="#ffffff" d="M232 400a46 46 0 0 1 46-46h460a46 46 0 0 1 46 46v318a46 46 0 0 1-46 46H278a46 46 0 0 1-46-46z"/>
<path fill="none" stroke="#2f5cd4" stroke-width="54" stroke-linecap="round"
      stroke-linejoin="round" d="M352 470l112 94-112 94"/>
<path fill="none" stroke="#2f5cd4" stroke-width="54" stroke-linecap="round" d="M532 660h150"/>
</svg>"""


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(SVG.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


@lru_cache(maxsize=None)
def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(QPixmap.fromImage(render(size)))
    return icon


def launch_command() -> str:
    """How the desktop entry starts this same install of the app."""
    if getattr(sys, "frozen", False):
        return shlex.quote(sys.executable)
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if script and os.access(script, os.X_OK) and not script.endswith(".py"):
        return shlex.quote(script)
    return f"{shlex.quote(sys.executable)} -m app.main"


def desktop_entry() -> str:
    command = launch_command()
    # "-m app.main" needs the folder it was started from, unless installed.
    folder = f"Path={os.getcwd()}\n" if command.endswith(" -m app.main") else ""
    return ("[Desktop Entry]\nType=Application\nName=Remote Viewer\n"
            "Comment=Live viewer for files on SSH servers\n"
            f"Exec={command}\n{folder}Icon={APP_ID}\nTerminal=false\n"
            "Categories=Network;Utility;FileTools;\n")


def install_desktop_entry(data_home: Path | None = None) -> None:
    """Write the icon and .desktop file under ~/.local/share, when changed.

    Best effort: a read-only home only costs the dock icon.
    """
    if data_home is None:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    files = {data_home / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg": SVG,
             data_home / "applications" / f"{APP_ID}.desktop": desktop_entry()}
    for path, content in files.items():
        try:
            if path.exists() and path.read_text(encoding="utf-8") == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication
    application = QGuiApplication([])
    target = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    render(1024).save(target)
    print(target)

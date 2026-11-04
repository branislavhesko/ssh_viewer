"""Look of the app: Fusion, a palette per theme, one stylesheet, and icons.

The palette matters as much as the stylesheet: custom-painted widgets (the log
view, the editor gutter) and the syntax colours read it directly.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, QStandardPaths, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

LIGHT = dict(window="#f5f6f8", surface="#ffffff", sidebar="#eef0f4", raised="#ffffff",
             hover="#e6e9ef", border="#dde0e6", text="#1d2129", muted="#6a7280",
             accent="#3a6df0", accent_hover="#2f5cd4", on_accent="#ffffff",
             selection="#d9e4fd", thumb="#c3c8d1", thumb_hover="#a3aab6")
DARK = dict(window="#16181d", surface="#1b1e24", sidebar="#14161a", raised="#272b33",
            hover="#2e333d", border="#2d323b", text="#e4e7ec", muted="#8d95a3",
            accent="#5f8bff", accent_hover="#7aa0ff", on_accent="#0f1420",
            selection="#2b3a5e", thumb="#3c424d", thumb_hover="#515966")

STYLESHEET = """
* { outline: 0; }
QWidget { color: {text}; }
QMainWindow, QDialog { background: {window}; }
QToolTip { background: {raised}; color: {text}; border: 1px solid {border}; padding: 4px 6px; }

QToolBar { background: {window}; border: 0; border-bottom: 1px solid {border};
           padding: 8px 10px; spacing: 8px; }
QStatusBar { background: {window}; border-top: 1px solid {border}; color: {muted}; }
QStatusBar::item { border: 0; }
QStatusBar QLabel { color: {muted}; padding: 0 8px; }
QSplitter::handle { background: {border}; }

QPushButton { background: {raised}; border: 1px solid {border}; border-radius: 6px;
              padding: 5px 12px; }
QPushButton:hover { background: {hover}; }
QPushButton:pressed { background: {border}; }
QPushButton:disabled { color: {muted}; background: transparent; }
QPushButton#primary { background: {accent}; border-color: {accent}; color: {on_accent}; }
QPushButton#primary:hover { background: {accent_hover}; border-color: {accent_hover}; }
QPushButton#primary:disabled { background: {hover}; border-color: {hover}; color: {muted}; }

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: {surface}; border: 1px solid {border}; border-radius: 6px;
    padding: 4px 8px; selection-background-color: {accent}; selection-color: {on_accent}; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: {accent}; }
QComboBox QAbstractItemView { background: {raised}; border: 1px solid {border};
    selection-background-color: {selection}; selection-color: {text}; }

QTreeView, QTableView, QListWidget { background: {surface}; border: 0;
    alternate-background-color: {surface}; selection-background-color: {selection};
    selection-color: {text}; gridline-color: {border}; show-decoration-selected: 1; }
QTreeView#sidebar { background: {sidebar}; }
QTreeView::item, QListWidget::item { padding: 3px 2px; border: 0; }
QTreeView::item:hover, QTableView::item:hover, QListWidget::item:hover { background: {hover}; }
QTreeView::item:selected, QTableView::item:selected, QListWidget::item:selected {
    background: {selection}; color: {text}; }
QTableView::item { padding: 0 6px; border: 0; }
QTableCornerButton::section { background: {window}; border: 0; }
QHeaderView { background: {window}; border: 0; }
QHeaderView::section { background: {window}; color: {muted}; border: 0;
    border-bottom: 1px solid {border}; border-right: 1px solid {border}; padding: 5px 8px; }

QTabWidget::pane { border: 0; border-top: 1px solid {border}; top: -1px; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: {muted}; border: 0;
               border-bottom: 2px solid transparent; padding: 7px 14px; }
QTabBar::tab:hover { color: {text}; }
QTabBar::tab:selected { color: {text}; border-bottom-color: {accent}; }

QComboBox::drop-down { border: 0; width: 22px; subcontrol-position: center right; }
QComboBox::down-arrow { image: url({images}/down.svg); width: 10px; height: 10px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { border: 0; background: transparent;
    width: 18px; subcontrol-origin: border; }
QAbstractSpinBox::up-button { subcontrol-position: top right; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: {hover}; }
QAbstractSpinBox::up-arrow { image: url({images}/up.svg); width: 8px; height: 8px; }
QAbstractSpinBox::down-arrow { image: url({images}/down.svg); width: 8px; height: 8px; }

QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid {muted}; border-radius: 4px;
                       background: {surface}; }
QCheckBox::indicator:hover { border-color: {accent}; }
QCheckBox::indicator:checked { background: {accent}; border-color: {accent};
                               image: url({images}/check.svg); }

QPlainTextEdit, QTextBrowser, QAbstractScrollArea#logView, QGraphicsView {
    background: {surface}; border: 1px solid {border}; border-radius: 6px;
    selection-background-color: {selection}; selection-color: {text}; }
QLabel#title { color: {muted}; }
QLabel#empty { color: {muted}; font-size: 15px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle { background: {thumb}; border-radius: 4px; margin: 1px; }
QScrollBar::handle:hover { background: {thumb_hover}; }
QScrollBar::handle:vertical { min-height: 28px; }
QScrollBar::handle:horizontal { min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSlider::groove:horizontal { background: {border}; height: 4px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: {accent}; border-radius: 2px; }
QSlider::handle:horizontal { background: {accent}; width: 14px; height: 14px;
                             margin: -5px 0; border-radius: 7px; }
"""

FOLDER = ('<path fill="#5b8def" d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5'
          'a2 2 0 0 1-2-2z"/>')
FILE = ('<path fill="none" stroke="#8a93a3" stroke-width="1.6" stroke-linejoin="round" '
        'd="M6 3h8l5 5v13H6zM14 3v5h5"/>')
LINK_BADGE = ('<circle cx="18" cy="18" r="5.5" fill="#e8a33d"/><path fill="none" stroke="#fff" '
              'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
              'd="M15.5 20.5l5-5m-3.5 0h3.5v3.5"/>')
ICONS = {"folder": FOLDER, "file": FILE, "folder-link": FOLDER + LINK_BADGE,
         "file-link": FILE + LINK_BADGE}
ICON_PIXELS = 64


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{ICONS[name]}</svg>'
    pixmap = QPixmap(ICON_PIXELS, ICON_PIXELS)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, ICON_PIXELS, ICON_PIXELS))
    painter.end()
    return QIcon(pixmap)


def _palette(tokens: dict[str, str]) -> QPalette:
    role = QPalette.ColorRole
    palette = QPalette()
    for target, token in ((role.Window, "window"), (role.WindowText, "text"),
                          (role.Base, "surface"), (role.AlternateBase, "sidebar"),
                          (role.Text, "text"), (role.Button, "raised"), (role.ButtonText, "text"),
                          (role.BrightText, "text"), (role.Highlight, "accent"),
                          (role.HighlightedText, "on_accent"), (role.Link, "accent"),
                          (role.PlaceholderText, "muted"), (role.ToolTipBase, "raised"),
                          (role.ToolTipText, "text"), (role.Mid, "border"), (role.Dark, "border"),
                          (role.Light, "raised"), (role.Midlight, "hover")):
        palette.setColor(target, QColor(tokens[token]))
    for target in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, target, QColor(tokens["muted"]))
    return palette


def _write_images(tokens: dict[str, str], folder: str) -> None:
    def stroke(path: str, color: str) -> str:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path fill="none" '
                f'stroke="{color}" stroke-width="2.2" stroke-linecap="round" '
                f'stroke-linejoin="round" d="{path}"/></svg>')
    images = {"down.svg": stroke("M3 6l5 5 5-5", tokens["muted"]),
              "up.svg": stroke("M3 10l5-5 5 5", tokens["muted"]),
              "check.svg": stroke("M3 8.5l3.5 3.5L13 5", tokens["on_accent"])}
    os.makedirs(folder, exist_ok=True)
    for name, svg in images.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as out:
            out.write(svg)


def _stylesheet(tokens: dict[str, str]) -> str:
    # Stylesheets can only load images from files. A fixed place, rewritten on
    # each start, leaves nothing to clean up after a crash.
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    folder = os.path.join(cache, "theme", "dark" if tokens is DARK else "light")
    _write_images(tokens, folder)
    sheet = STYLESHEET.replace("{images}", folder.replace(os.sep, "/"))
    for name, value in tokens.items():
        sheet = sheet.replace("{" + name + "}", value)
    return sheet


def _system_is_dark(app: QApplication) -> bool:
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


_mode: Optional[str] = None


def apply(app: QApplication, mode: str = "auto") -> None:
    """``mode`` is "auto" (follow the system), "light" or "dark"."""
    global _mode
    if _mode is None:
        app.setStyle("Fusion")
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: _mode == "auto" and apply(app, "auto"))
    _mode = mode
    dark = mode == "dark" or (mode == "auto" and _system_is_dark(app))
    tokens = DARK if dark else LIGHT
    app.setPalette(_palette(tokens))
    app.setStyleSheet(_stylesheet(tokens))

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QPalette

from app import theme
from app.viewers.editor import EditorViewer
from app.viewers.text_log import TextLogViewer
from test_ui import Harness, pump, qapp  # noqa: F401


@pytest.fixture
def restore_theme(qapp):
    yield
    qapp.setStyleSheet("")
    qapp.setPalette(qapp.style().standardPalette())


def base_is_dark(app):
    return app.palette().color(QPalette.ColorRole.Base).lightness() < 128


def test_tokens_all_resolve_and_sheet_parses(qapp, restore_theme):
    problems = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: problems.append(message))
    try:
        for mode in ("light", "dark"):
            theme.apply(qapp, mode)
            assert "{" + "text}" not in qapp.styleSheet()
    finally:
        qInstallMessageHandler(previous)
    assert not [p for p in problems if "style" in p.lower()]


def test_stylesheet_images_exist(qapp, restore_theme):
    import re
    for mode in ("light", "dark"):
        theme.apply(qapp, mode)
        urls = re.findall(r"url\((.+?)\)", qapp.styleSheet())
        assert urls and all(os.path.exists(url) for url in urls)


def test_modes_set_the_palette_custom_painted_widgets_read(qapp, restore_theme):
    theme.apply(qapp, "dark")
    assert base_is_dark(qapp)
    theme.apply(qapp, "light")
    assert not base_is_dark(qapp)


def test_icons_are_not_blank(qapp):
    for name in theme.ICONS:
        image = theme.icon(name).pixmap(32, 32).toImage()
        assert any(image.pixelColor(x, y).alpha() for x in range(32) for y in range(32))


def first_keyword_color(spans):
    return next(s.color for s in spans if s.text.startswith("def"))


@pytest.fixture
def ui(qapp, server, make_backend, restore_theme):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def test_switching_theme_recolours_open_code(ui, server, qapp):
    theme.apply(qapp, "light")
    (server.root / "a.py").write_text("def f():\n    pass\n")
    ui.window.list_model.reload()
    ui.select("a.py")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer) and preview.current.view.syntax)
    viewer = preview.current
    before = first_keyword_color(viewer.view.syntax[0])
    theme.apply(qapp, "dark")
    assert pump(lambda: first_keyword_color(viewer.view.syntax[0]) != before)

    preview.edit()
    editor = preview.current
    assert isinstance(editor, EditorViewer)
    assert pump(lambda: bool(editor.highlighter.lines))
    before = first_keyword_color(editor.highlighter.lines[0])
    theme.apply(qapp, "light")
    assert pump(lambda: first_keyword_color(editor.highlighter.lines[0]) != before)

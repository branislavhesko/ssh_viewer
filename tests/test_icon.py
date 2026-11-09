import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.icon import APP_ID, SVG, app_icon, desktop_entry, install_desktop_entry
from tests.test_ui import qapp  # noqa: F401  (fixture)


def test_icon_renders_at_dock_sizes(qapp):
    icon = app_icon()
    assert not icon.isNull()
    image = icon.pixmap(256).toImage()
    assert image.pixelColor(128, 128).alpha() == 255  # the tile, not transparency
    assert image.pixelColor(2, 2).alpha() == 0         # the margin around it


def test_desktop_entry_is_written_once_and_names_the_icon(tmp_path):
    install_desktop_entry(tmp_path)
    entry = tmp_path / "applications" / f"{APP_ID}.desktop"
    icon = tmp_path / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
    assert icon.read_text() == SVG
    text = entry.read_text()
    assert f"Icon={APP_ID}\n" in text and "Exec=" in text and text == desktop_entry()
    before = entry.stat().st_mtime_ns
    install_desktop_entry(tmp_path)
    assert entry.stat().st_mtime_ns == before


def test_unwritable_home_is_not_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    install_desktop_entry(blocker)  # parents can't be made under a file

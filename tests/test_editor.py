import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QTextCursor

from app.viewers.editor import MAX_EDIT_BYTES, EditorViewer, TextCodec
from app.viewers.text_log import TextLogViewer
from test_ui import Harness, pump, qapp  # noqa: F401


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def open_editor(ui, name, ready=True):
    ui.window.list_model.reload()
    ui.select(name)
    preview = ui.window.preview
    assert pump(lambda: preview.edit_button.isEnabled())
    preview.edit()
    viewer = preview.current
    assert isinstance(viewer, EditorViewer)
    if ready:
        assert pump(lambda: not viewer.edit.isReadOnly()), viewer.status.text()
    return viewer


def type_at_end(viewer, text):
    cursor = viewer.edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    viewer.edit.setTextCursor(cursor)
    viewer.edit.insertPlainText(text)


def saved(viewer):
    return pump(lambda: viewer.status.text() == "Saved")


@pytest.mark.parametrize("data", [
    b"plain\n", b"no newline", b"", b"\n\n", b"a\r\nb\r\n", b"\xef\xbb\xbfbom\n",
    "non breaking\n".encode(), "emoji \U0001f600\n".encode(), b"tab\there\n",
])
def test_opening_and_saving_changes_only_what_was_typed(ui, server, data):
    path = server.root / "f.txt"
    path.write_bytes(data)
    viewer = open_editor(ui, "f.txt")
    assert not viewer.dirty and not viewer.save_button.isEnabled()
    type_at_end(viewer, "!")
    viewer.save()
    assert saved(viewer)
    assert path.read_bytes() == data + b"!"
    assert not viewer.dirty


@pytest.mark.parametrize("data,reason", [
    ("café\n".encode("latin-1"), "isn't UTF-8"),
    (b"mixed\r\nendings\n", "mixed line endings"),
    (b"lone\rcarriage\n", "mixed line endings"),
])
def test_files_that_would_be_damaged_open_read_only(ui, server, data, reason):
    path = server.root / "f.txt"
    path.write_bytes(data)
    viewer = open_editor(ui, "f.txt", ready=False)
    assert pump(lambda: reason in viewer.status.text())
    assert viewer.edit.isReadOnly() and not viewer.save_button.isEnabled()
    viewer.save()
    assert path.read_bytes() == data


def test_codec_round_trip():
    codec = TextCodec(b"\xef\xbb\xbfa\r\nb")
    assert codec.decode(b"\xef\xbb\xbfa\r\nb") == "a\nb"
    assert codec.encode("a b") == b"\xef\xbb\xbfa\r\nb"


def test_save_keeps_mode(ui, server):
    path = server.root / "run.sh"
    path.write_text("echo hi\n")
    path.chmod(0o755)
    viewer = open_editor(ui, "run.sh")
    type_at_end(viewer, "echo bye\n")
    viewer.save()
    assert saved(viewer)
    assert path.stat().st_mode & 0o777 == 0o755


def test_conflict_overwrite_cancel_and_reload(ui, server, monkeypatch):
    path = server.root / "f.txt"
    path.write_text("one\n")
    viewer = open_editor(ui, "f.txt")
    type_at_end(viewer, "mine\n")
    stamp = path.stat().st_mtime
    path.write_text("two\n")  # same size, same second: only the bytes differ
    os.utime(path, (stamp, stamp))

    asked = []
    monkeypatch.setattr(viewer, "ask_conflict", lambda: asked.append(1) or "cancel")
    viewer.save()
    assert pump(lambda: viewer.status.text() == "Not saved")
    assert asked and path.read_text() == "two\n" and viewer.dirty

    monkeypatch.setattr(viewer, "ask_conflict", lambda: "overwrite")
    viewer.save()
    assert saved(viewer)
    assert path.read_text() == "one\nmine\n"

    path.write_text("three\n")
    type_at_end(viewer, "more\n")
    monkeypatch.setattr(viewer, "ask_conflict", lambda: "reload")
    viewer.save()
    assert pump(lambda: viewer.edit.raw_text().replace(" ", "\n") == "three\n")
    assert path.read_text() == "three\n" and not viewer.dirty


def test_deleted_file_counts_as_a_conflict(ui, server, monkeypatch):
    path = server.root / "f.txt"
    path.write_text("one\n")
    viewer = open_editor(ui, "f.txt")
    type_at_end(viewer, "x")
    path.unlink()
    monkeypatch.setattr(viewer, "ask_conflict", lambda: "overwrite")
    viewer.save()
    assert saved(viewer)
    assert path.read_text() == "one\nx"


def test_clean_editor_follows_the_server_and_dirty_one_does_not(ui, server):
    path = server.root / "f.txt"
    path.write_text("one\n")
    viewer = open_editor(ui, "f.txt")
    pump(lambda: False, 0.6)  # polling baseline
    path.write_text("two two\n")
    assert pump(lambda: viewer.edit.raw_text().startswith("two two"))
    assert not viewer.dirty

    type_at_end(viewer, "mine")
    path.write_text("three three three\n")
    assert pump(lambda: viewer.reload_button.isVisibleTo(viewer))
    assert "mine" in viewer.edit.raw_text() and viewer.dirty


def test_own_save_does_not_read_as_a_remote_change(ui, server):
    path = server.root / "f.txt"
    path.write_text("one\n")
    viewer = open_editor(ui, "f.txt")
    pump(lambda: False, 0.6)
    type_at_end(viewer, "two\n")
    viewer.save()
    assert saved(viewer)
    pump(lambda: False, 1.0)
    assert not viewer.reload_button.isVisibleTo(viewer)
    assert viewer.status.text() == "Saved"


def test_failed_save_keeps_the_edits(ui, server):
    folder = server.root / "locked"
    folder.mkdir()
    path = folder / "f.txt"
    path.write_text("one\n")
    ui.window.navigate(str(folder))
    assert pump(lambda: ui.window.list_model.row_of(str(path)) >= 0)
    ui.window.table.selectRow(0)
    preview = ui.window.preview
    assert pump(lambda: preview.edit_button.isEnabled())
    preview.edit()
    viewer = preview.current
    assert pump(lambda: not viewer.edit.isReadOnly())
    type_at_end(viewer, "edit")
    path.chmod(0o444)
    folder.chmod(0o555)
    try:
        viewer.save()
        assert pump(lambda: viewer.status.text().startswith("Save failed"))
        assert viewer.dirty and "edit" in viewer.edit.raw_text()
    finally:
        folder.chmod(0o755)


def test_switching_files_asks_and_can_keep_editing(ui, server, monkeypatch):
    (server.root / "a.txt").write_text("a\n")
    (server.root / "b.txt").write_text("b\n")
    viewer = open_editor(ui, "a.txt")
    type_at_end(viewer, "edit")
    preview = ui.window.preview

    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "cancel")
    ui.select("b.txt")
    pump(lambda: False, 0.2)
    assert preview.current is viewer and "edit" in viewer.edit.raw_text()
    model, table = ui.window.list_model, ui.window.table
    assert model.entry(table.currentIndex().row()).name == "a.txt"  # list follows the editor

    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "save")
    ui.select("b.txt")
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    assert (server.root / "a.txt").read_text() == "a\nedit"


def test_discard_and_done_return_to_the_viewer(ui, server, monkeypatch):
    path = server.root / "a.txt"
    path.write_text("a\n")
    viewer = open_editor(ui, "a.txt")
    type_at_end(viewer, "edit")
    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "discard")
    viewer.done_button.click()
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    assert path.read_text() == "a\n"
    assert preview.edit_button.isEnabled()


def test_closing_the_window_asks_and_can_be_refused(ui, server, monkeypatch):
    (server.root / "a.txt").write_text("a\n")
    viewer = open_editor(ui, "a.txt")
    type_at_end(viewer, "edit")
    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "cancel")
    ui.window.close()
    assert ui.window.isVisible()
    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "discard")
    ui.window.close()
    assert pump(lambda: not ui.window.isVisible())


def test_quitting_with_save_closes_once_saved_and_stays_if_it_fails(ui, server, monkeypatch):
    path = server.root / "a.txt"
    path.write_text("a\n")
    viewer = open_editor(ui, "a.txt")
    type_at_end(viewer, "edit")
    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "save")
    monkeypatch.setattr(viewer, "ask_conflict", lambda: "cancel")
    path.write_text("changed\n")
    ui.window.close()
    assert pump(lambda: viewer.status.text() == "Not saved")
    assert ui.window.isVisible() and viewer.dirty

    monkeypatch.setattr(viewer, "ask_conflict", lambda: "overwrite")
    ui.window.close()
    assert pump(lambda: not ui.window.isVisible())
    assert path.read_text() == "a\nedit"


def test_code_is_coloured_while_editing_and_typing_does_not_mark_clean(ui, server):
    (server.root / "a.py").write_text("def f():\n    return 1\n")
    viewer = open_editor(ui, "a.py")
    assert pump(lambda: bool(viewer.highlighter.lines))
    block = viewer.edit.document().firstBlock()
    assert pump(lambda: any(r.format.foreground().color().isValid() for r in block.layout().formats()))
    assert not viewer.dirty
    type_at_end(viewer, "x = 2\n")
    assert pump(lambda: len(viewer.highlighter.lines) >= 3)
    assert viewer.dirty


def test_failed_save_on_switch_stays_in_the_editor(ui, server, monkeypatch):
    path = server.root / "a.txt"
    path.write_text("a\n")
    (server.root / "b.txt").write_text("b\n")
    viewer = open_editor(ui, "a.txt")
    type_at_end(viewer, "edit")
    monkeypatch.setattr(viewer, "ask_unsaved", lambda: "save")
    monkeypatch.setattr(viewer, "ask_conflict", lambda: "cancel")
    path.write_text("changed\n")
    ui.select("b.txt")
    assert pump(lambda: viewer.status.text() == "Not saved")
    preview = ui.window.preview
    assert preview.current is viewer and preview.entry.name == "a.txt"
    assert ui.window.list_model.entry(ui.window.table.currentIndex().row()).name == "a.txt"


def test_viewer_shows_saved_content_after_done(ui, server):
    path = server.root / "a.md"
    path.write_text("# old\n")
    viewer = open_editor(ui, "a.md")
    type_at_end(viewer, "new line\n")
    viewer.save()
    assert saved(viewer)
    viewer.done_button.click()
    preview = ui.window.preview
    assert pump(lambda: preview.editor is None)
    assert pump(lambda: "new line" in preview.current.browser.toPlainText())


def test_binary_and_huge_files_offer_no_edit(ui, server):
    (server.root / "b.bin").write_bytes(b"\x00\x01")
    (server.root / "big.txt").write_bytes(b"x\n" * (MAX_EDIT_BYTES // 2 + 1))
    ui.window.list_model.reload()
    preview = ui.window.preview
    ui.select("b.bin")
    assert pump(lambda: preview.entry is not None and preview.current is not None)
    assert not preview.edit_button.isEnabled()
    ui.select("big.txt")
    assert pump(lambda: preview.entry.name == "big.txt" and preview.current is not None)
    assert not preview.edit_button.isEnabled()

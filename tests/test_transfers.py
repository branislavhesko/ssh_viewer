"""Transfers against the real test sshd, on both backends."""
import os
import stat
import subprocess
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from app.backend import TransferCancelled
from app.transfers import UMASK, Decision, Job, JobState, TransferManager, safe_name
from test_ui import Harness, pump, qapp  # noqa: F401


def leftovers(folder):
    return [p.name for p in folder.rglob("*") if ".rv-" in p.name]


def run(job, timeout=30):
    deadline = time.time() + timeout
    while not job.finished:
        assert time.time() < deadline, job.state
        time.sleep(0.005)
    return job


def never_asked(job, targets):
    raise AssertionError(f"unexpected clash: {targets}")


@pytest.fixture
def manager(backend):
    return TransferManager(backend, never_asked)


@pytest.fixture
def local(tmp_path):
    folder = tmp_path / "local"
    folder.mkdir()
    return folder


# Per-file calls --------------------------------------------------------------
def test_upload_file_streams_and_reports_progress(server, backend, local):
    data = os.urandom(9 * 1024 * 1024 + 123)
    (local / "blob").write_bytes(data)
    seen = []
    target = server.root / "blob"
    backend.upload_file(str(local / "blob"), str(target), 0o640, seen.append).future.result(20)
    assert target.read_bytes() == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert seen[0] == 0 and seen == sorted(seen)
    assert not leftovers(server.root)


def test_download_file_streams_and_reports_progress(server, backend, local):
    data = os.urandom(9 * 1024 * 1024 + 123)
    (server.root / "blob").write_bytes(data)
    seen = []
    target = local / "blob"
    backend.download_file(str(server.root / "blob"), str(target), seen.append).future.result(20)
    assert target.read_bytes() == data
    assert seen[0] == 0 and seen == sorted(seen)
    assert pump_until(lambda: seen[-1] == len(data))


def pump_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_upload_over_existing_file_keeps_its_mode_and_is_seen_by_reads(server, backend, local):
    (local / "f").write_text("new content")
    target = server.root / "f"
    target.write_text("old")
    target.chmod(0o600)
    assert backend.fetch(str(target)).result(5) == b"old"
    backend.upload_file(str(local / "f"), str(target), 0o755, lambda _: None).future.result(5)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert backend.fetch(str(target)).result(5) == b"new content"


def test_upload_through_a_symlink_keeps_the_link(server, backend, local):
    (local / "f").write_text("new")
    (server.root / "real").write_text("old")
    os.symlink("real", server.root / "link")
    backend.upload_file(str(local / "f"), str(server.root / "link"), 0o644,
                        lambda _: None).future.result(5)
    assert (server.root / "link").is_symlink()
    assert (server.root / "real").read_text() == "new"


def test_a_name_at_the_length_limit_uploads(server, backend, local):
    name = "n" * 255
    (local / "f").write_text("x")
    backend.upload_file(str(local / "f"), str(server.root / name), 0o644,
                        lambda _: None).future.result(5)
    assert (server.root / name).read_text() == "x"


def test_cancelled_upload_keeps_the_old_file_and_no_temp(server, backend, local):
    (local / "blob").write_bytes(os.urandom(64 * 1024 * 1024))
    target = server.root / "blob"
    target.write_text("old")
    transfer = backend.upload_file(str(local / "blob"), str(target), 0o644,
                                   lambda done: done and transfer.cancel())
    with pytest.raises(TransferCancelled):
        transfer.future.result(20)
    assert target.read_text() == "old"
    assert not leftovers(server.root)


def test_cancelled_download_raises(server, backend, local):
    (server.root / "blob").write_bytes(os.urandom(64 * 1024 * 1024))
    transfer = backend.download_file(str(server.root / "blob"), str(local / "blob"),
                                     lambda done: done and transfer.cancel())
    with pytest.raises(TransferCancelled):
        transfer.future.result(20)


def test_missing_source_fails(server, backend, local):
    with pytest.raises(Exception):
        backend.upload_file(str(local / "nope"), str(server.root / "x"), 0o644,
                            lambda _: None).future.result(5)
    assert not leftovers(server.root)
    with pytest.raises(Exception):
        backend.download_file(str(server.root / "nope"), str(local / "x"),
                              lambda _: None).future.result(5)


def test_mkdir(server, backend):
    path = server.root / "made"
    backend.mkdir(str(path), 0o750).result(5)
    assert stat.S_IMODE(path.stat().st_mode) == 0o750 & ~UMASK
    backend.mkdir(str(path)).result(5)  # already there
    (server.root / "file").write_text("")
    with pytest.raises(Exception):
        backend.mkdir(str(server.root / "file")).result(5)
    with pytest.raises(Exception):
        backend.mkdir(str(server.root / "no" / "parent")).result(5)


# Jobs ------------------------------------------------------------------------
def make_tree(root):
    (root / "tree" / "sub" / "deep").mkdir(parents=True)
    (root / "tree" / "empty").mkdir()
    (root / "tree" / "a.txt").write_text("a")
    (root / "tree" / "sub" / "b.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    (root / "tree" / "sub" / "deep" / "run.sh").write_text("#!/bin/sh\n")
    (root / "tree" / "sub" / "deep" / "run.sh").chmod(0o755)
    (root / "tree" / "sub" / "secret").write_text("key")
    (root / "tree" / "sub" / "secret").chmod(0o600)


def snapshot(root):
    return {str(p.relative_to(root)): None if p.is_dir() else
            (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in root.rglob("*")}


def test_upload_and_download_a_folder(server, manager, local):
    make_tree(local)
    job = run(manager.upload([str(local / "tree")], str(server.root)))
    assert job.state is JobState.DONE and not job.failures
    assert job.done_files == job.total_files == 4
    assert job.done_bytes == job.total_bytes
    assert snapshot(server.root / "tree") == snapshot(local / "tree")

    back = local / "back"
    back.mkdir()
    job = run(manager.download([str(server.root / "tree")], str(back)))
    assert job.state is JobState.DONE and not job.failures
    assert snapshot(back / "tree") == snapshot(local / "tree")
    assert not leftovers(server.root) and not leftovers(local)


def test_several_files_and_folders_in_one_job(server, manager, local):
    make_tree(local)
    (local / "single").write_text("s")
    job = run(manager.upload([str(local / "tree"), str(local / "single")], str(server.root)))
    assert not job.failures and job.done_files == 5
    assert (server.root / "single").read_text() == "s"
    back = local / "back"
    back.mkdir()
    job = run(manager.download([str(server.root / "single"), str(server.root / "tree" / "sub")],
                               str(back)))
    assert not job.failures
    assert (back / "single").read_text() == "s"
    assert (back / "sub" / "deep" / "run.sh").exists()


def test_many_small_files(server, manager, local):
    (local / "many").mkdir()
    for index in range(300):
        (local / "many" / f"{index}.txt").write_text(str(index))
    job = run(manager.upload([str(local / "many")], str(server.root)), 60)
    assert job.done_files == 300 and not job.failures
    assert len(list((server.root / "many").iterdir())) == 300


def test_download_does_not_widen_past_umask(server, manager, local):
    (server.root / "wide").write_text("x")
    (server.root / "wide").chmod(0o777)
    run(manager.download([str(server.root / "wide")], str(local)))
    assert stat.S_IMODE((local / "wide").stat().st_mode) == 0o777 & ~UMASK


def test_links_and_special_files(server, manager, local):
    """Same rules both ways: file links become files, the rest is skipped."""
    for root in (local, server.root):
        (root / "dir").mkdir()
        (root / "dir" / "real").write_text("content")
        os.symlink("real", root / "dir" / "to_file")
        os.symlink("..", root / "dir" / "to_folder")
        os.symlink("gone", root / "dir" / "broken")
        os.mkfifo(root / "dir" / "pipe")
    remote_target = server.root / "up"
    remote_target.mkdir()
    job = run(manager.upload([str(local / "dir")], str(remote_target)))
    down = local / "down"
    down.mkdir()
    other = run(manager.download([str(server.root / "dir")], str(down)))
    for done, folder in ((job, remote_target / "dir"), (other, down / "dir")):
        assert done.state is JobState.DONE and not done.failures
        assert len(done.skipped) == 3
        assert sorted(p.name for p in folder.iterdir()) == ["real", "to_file"]
        assert not (folder / "to_file").is_symlink()
        assert (folder / "to_file").read_text() == "content"


def test_selected_link_to_a_folder_is_followed(server, manager, local):
    make_tree(server.root)
    os.symlink("tree", server.root / "shortcut")
    job = run(manager.download([str(server.root / "shortcut")], str(local)))
    assert not job.failures and (local / "shortcut" / "a.txt").read_text() == "a"


def test_non_utf8_names_both_ways(server, manager, local):
    raw = b"caf\xe9"
    with open(os.fsencode(server.root) + b"/" + raw, "wb") as out:
        out.write(b"remote")
    with open(os.fsencode(local) + b"/up" + raw, "wb") as out:
        out.write(b"local")
    name, upname = os.fsdecode(raw), os.fsdecode(b"up" + raw)
    down = local / "down"
    down.mkdir()
    job = run(manager.download([f"{server.root}/{name}"], str(down)))
    assert not job.failures
    assert open(os.fsencode(down) + b"/" + raw, "rb").read() == b"remote"
    job = run(manager.upload([f"{local}/{upname}"], str(server.root)))
    assert not job.failures
    assert open(os.fsencode(server.root) + b"/up" + raw, "rb").read() == b"local"


@pytest.mark.parametrize("decision, expected", [(Decision.REPLACE, "new"), (Decision.SKIP, "old")])
def test_clash_is_asked_once_and_folders_merge(server, backend, local, decision, expected):
    asked = []

    def ask(job, targets):
        asked.append(targets)
        return decision
    manager = TransferManager(backend, ask)
    (local / "dir" / "sub").mkdir(parents=True)
    (local / "dir" / "sub" / "clash").write_text("new")
    (local / "dir" / "fresh").write_text("fresh")
    (server.root / "dir" / "sub").mkdir(parents=True)
    (server.root / "dir" / "sub" / "clash").write_text("old")
    (server.root / "dir" / "kept").write_text("kept")

    job = run(manager.upload([str(local / "dir")], str(server.root)))
    assert asked == [[str(server.root / "dir" / "sub" / "clash")]]
    assert (server.root / "dir" / "sub" / "clash").read_text() == expected
    assert (server.root / "dir" / "fresh").read_text() == "fresh"
    assert (server.root / "dir" / "kept").read_text() == "kept"
    assert len(job.skipped) == (decision is Decision.SKIP)

    asked.clear()
    down = local / "down"
    (down / "dir" / "sub").mkdir(parents=True)
    (down / "dir" / "sub" / "clash").write_text("mine")
    run(manager.download([str(server.root / "dir")], str(down)))
    assert asked == [[str(down / "dir" / "sub" / "clash")]]
    assert (down / "dir" / "sub" / "clash").read_text() == \
        (expected if decision is Decision.REPLACE else "mine")
    assert (down / "dir" / "kept").read_text() == "kept"


def test_cancelling_at_the_clash_prompt_touches_nothing(server, backend, local):
    manager = TransferManager(backend, lambda job, targets: Decision.CANCEL)
    (local / "dir").mkdir()
    (local / "dir" / "clash").write_text("new")
    (local / "dir" / "fresh").write_text("new")
    (server.root / "dir").mkdir()
    (server.root / "dir" / "clash").write_text("old")
    job = run(manager.upload([str(local / "dir")], str(server.root)))
    assert job.state is JobState.CANCELLED
    assert sorted(p.name for p in (server.root / "dir").iterdir()) == ["clash"]


def test_file_and_folder_in_each_others_place_fail(server, manager, local):
    (local / "dir").mkdir()
    (local / "dir" / "child").write_text("x")
    (local / "plain").write_text("x")
    (local / "ok").write_text("x")
    (server.root / "dir").write_text("a file")
    (server.root / "plain").mkdir()
    job = run(manager.upload([str(local / n) for n in ("dir", "plain", "ok")], str(server.root)))
    assert job.state is JobState.DONE and len(job.failures) == 2
    assert (server.root / "dir").read_text() == "a file"
    assert (server.root / "ok").exists()


def test_one_unreadable_file_does_not_stop_the_rest(server, manager, local):
    (server.root / "dir").mkdir()
    (server.root / "dir" / "good").write_text("x")
    (server.root / "dir" / "locked").write_text("x")
    (server.root / "dir" / "locked").chmod(0)
    if os.access(server.root / "dir" / "locked", os.R_OK):
        pytest.skip("running as root")
    job = run(manager.download([str(server.root / "dir")], str(local)))
    assert job.state is JobState.DONE
    assert len(job.failures) == 1 and "locked" in job.failures[0]
    assert job.done_files == job.total_files == 1
    assert (local / "dir" / "good").exists() and not (local / "dir" / "locked").exists()
    assert not leftovers(local)


def test_failed_download_leaves_the_old_file(server, backend, local):
    manager = TransferManager(backend, lambda job, targets: Decision.REPLACE)
    (server.root / "locked").write_text("new")
    (server.root / "locked").chmod(0)
    if os.access(server.root / "locked", os.R_OK):
        pytest.skip("running as root")
    (local / "locked").write_text("old")
    job = run(manager.download([str(server.root / "locked")], str(local)))
    assert len(job.failures) == 1
    assert (local / "locked").read_text() == "old"


def test_missing_destination_fails_the_job(server, manager, local):
    (local / "f").write_text("x")
    job = run(manager.upload([str(local / "f")], str(server.root / "nope")))
    assert job.state is JobState.FAILED and job.error


def test_cancelling_a_running_job_cleans_up(server, manager, local):
    (local / "dir").mkdir()
    for index in range(6):
        (local / "dir" / str(index)).write_bytes(os.urandom(32 * 1024 * 1024))
    job = manager.upload([str(local / "dir")], str(server.root))
    assert pump_until(lambda: job.done_bytes > 0, 20)
    job.cancel()
    assert run(job).state is JobState.CANCELLED
    assert not leftovers(server.root)


def test_a_queued_job_can_be_cancelled_before_it_starts(server, manager, local):
    (local / "big").write_bytes(os.urandom(32 * 1024 * 1024))
    (local / "small").write_text("x")
    first = manager.upload([str(local / "big")], str(server.root))
    second = manager.upload([str(local / "small")], str(server.root))
    second.cancel()
    assert run(second).state is JobState.CANCELLED
    run(first)
    assert not (server.root / "small").exists()


def test_a_dropped_connection_restarts_the_file(server, backend, manager, local):
    data = os.urandom(96 * 1024 * 1024)
    (local / "blob").write_bytes(data)
    job = manager.upload([str(local / "blob")], str(server.root))
    assert pump_until(lambda: job.done_bytes > 0, 20)
    subprocess.run(["pkill", "-P", str(server.process.pid)], check=False)
    run(job, 60)
    assert job.state is JobState.DONE and not job.failures, job.failures
    assert (server.root / "blob").read_bytes() == data


class HostileBackend:
    """A server listing names that try to climb out of the download folder."""

    def __init__(self, inner, folder, names):
        self._inner, self._folder, self._names = inner, folder, names

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def listdir(self, path):
        if path != self._folder:
            return self._inner.listdir(path)
        from concurrent.futures import Future
        from dataclasses import replace
        (real,) = self._inner.listdir(path).result(5)
        future = Future()
        future.set_result([replace(real, name=name, path=f"{path}/{name}") for name in self._names])
        return future


@pytest.mark.parametrize("name", ["../escaped", "..", ".", "a/../../escaped", "/tmp/escaped", ""])
def test_names_from_the_server_cannot_leave_the_download_folder(server, backend, local, name):
    folder = str(server.root / "dir")
    (server.root / "dir").mkdir()
    (server.root / "dir" / "real").write_text("x")
    manager = TransferManager(HostileBackend(backend, folder, [name]), never_asked)
    down = local / "down"
    down.mkdir()
    job = run(manager.download([folder], str(down)))
    assert len(job.failures) == 1 and job.done_files == 0
    assert not (local / "escaped").exists()
    assert [p.name for p in (down / "dir").iterdir()] == []


def test_safe_name():
    assert safe_name("normal.txt") and safe_name("..hidden") and safe_name("a b")
    assert not any(safe_name(n) for n in ("", ".", "..", "a/b", "x\0y"))


# Window ----------------------------------------------------------------------
@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def wait_idle(ui):
    panel = ui.window.transfers
    assert pump(lambda: panel._rows and panel.active() == 0 and not panel._timer.isActive(), 20)


def test_download_selected_rows_from_the_window(ui, server, local, monkeypatch):
    (server.root / "folder").mkdir()
    (server.root / "folder" / "inner").write_text("inner")
    (server.root / "one.txt").write_text("one")
    (server.root / "not_me.txt").write_text("no")
    ui.window.list_model.reload()
    ui.wait_listing(3)
    monkeypatch.setattr(ui.window, "pick_folder", lambda title: [str(local)])
    table = ui.window.table
    table.selectRow(ui.window.list_model.row_of(str(server.root / "folder")))
    table.selectionModel().select(
        ui.window.list_model.index(ui.window.list_model.row_of(str(server.root / "one.txt")), 0),
        table.selectionModel().SelectionFlag.Select | table.selectionModel().SelectionFlag.Rows)
    ui.window._download(ui.window._selected_paths())
    wait_idle(ui)
    assert (local / "folder" / "inner").read_text() == "inner"
    assert (local / "one.txt").read_text() == "one"
    assert not (local / "not_me.txt").exists()
    row = ui.window.transfers._rows[0]
    assert row.item.text(2) == "Done, 2 files" and row.bar.value() == 1000
    assert ui.window.tabs.tabText(2) == "Transfers"


def test_dropping_a_folder_uploads_it_and_the_list_shows_it(ui, server, local):
    make_tree(local)
    ui.wait_listing(0)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(local / "tree"))])
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    ui.window.dropEvent(event)
    wait_idle(ui)
    assert snapshot(server.root / "tree") == snapshot(local / "tree")
    assert pump(lambda: ui.window.list_model.row_of(str(server.root / "tree")) >= 0)


def test_upload_actions_ask_for_files_and_folders(ui, server, local, monkeypatch):
    (local / "f").write_text("f")
    (local / "d").mkdir()
    (local / "d" / "g").write_text("g")
    monkeypatch.setattr(ui.window, "pick_files", lambda: [str(local / "f")])
    monkeypatch.setattr(ui.window, "pick_folder", lambda title: [str(local / "d")])
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    ui.window._add_upload_actions(menu, lambda: str(server.root))
    for action in menu.actions():
        action.trigger()
    wait_idle(ui)
    assert pump(lambda: ui.window.transfers.active() == 0
                and (server.root / "d" / "g").exists() and (server.root / "f").exists())


def test_clash_prompt_runs_on_the_ui_thread(ui, server, local, monkeypatch):
    (local / "f").write_text("new")
    (server.root / "f").write_text("old")
    import threading
    threads = []

    def confirm(job, targets):
        threads.append(threading.current_thread())
        return Decision.REPLACE
    monkeypatch.setattr(ui.window.transfers, "confirm_replace", confirm)
    ui.window._upload([str(local / "f")], str(server.root))
    wait_idle(ui)
    assert threads == [threading.main_thread()]
    assert (server.root / "f").read_text() == "new"


def test_failures_show_in_the_row(ui, server, local):
    (local / "plain").write_text("x")
    (server.root / "plain").mkdir()
    ui.window._upload([str(local / "plain")], str(server.root))
    wait_idle(ui)
    row = ui.window.transfers._rows[0]
    assert "1 failed" in row.item.text(2)
    ui.window.transfers._clear_finished()
    assert ui.window.transfers.view.topLevelItemCount() == 0


def test_a_new_job_can_be_cancelled_straight_away(ui, server, local):
    (local / "f").write_text("x")
    ui.window._upload([str(local / "f")], str(server.root))
    wait_idle(ui)
    job = start_slow_upload(ui, server, local)
    panel = ui.window.transfers
    assert [row.job for row in panel._selected()] == [job]
    assert panel.cancel_button.isEnabled()
    panel.cancel_button.click()
    wait_idle(ui)
    assert job.state is JobState.CANCELLED and not leftovers(server.root)


def start_slow_upload(ui, server, local):
    (local / "blob").write_bytes(os.urandom(192 * 1024 * 1024))
    ui.window._upload([str(local / "blob")], str(server.root))
    job = ui.window.transfers._rows[-1].job
    assert pump(lambda: job.done_bytes > 0, 20)
    return job


def test_connecting_elsewhere_stops_running_transfers_first(ui, server, local, monkeypatch):
    job = start_slow_upload(ui, server, local)
    connected = []
    monkeypatch.setattr(ui.window.transfers, "confirm_stop", lambda count: True)
    monkeypatch.setattr(ui.window, "_connect_now", lambda: connected.append(job.finished))
    ui.window._connect()
    assert pump(lambda: connected, 20)
    assert connected == [True] and job.state is JobState.CANCELLED


def test_declining_keeps_the_transfer_and_the_connection(ui, server, local, monkeypatch):
    job = start_slow_upload(ui, server, local)
    monkeypatch.setattr(ui.window.transfers, "confirm_stop", lambda count: False)
    monkeypatch.setattr(ui.window, "_connect_now", lambda: pytest.fail("connected"))
    ui.window._connect()
    assert not job.finished
    job.cancel()
    wait_idle(ui)


def test_closing_the_window_cancels_transfers_first(ui, server, local, monkeypatch):
    job = start_slow_upload(ui, server, local)
    monkeypatch.setattr(ui.window.transfers, "confirm_stop", lambda count: True)
    ui.window.close()
    assert pump(lambda: not ui.window.isVisible(), 20)
    assert job.state is JobState.CANCELLED
    assert not leftovers(server.root)


def test_menus_build_without_a_connection_path(ui, server, monkeypatch):
    """Right-click code paths: menus are built, then dismissed at once."""
    from PySide6.QtWidgets import QMenu
    shown = []

    class Menu(QMenu):
        def exec(self, *_):
            shown.append([action.text() for action in self.actions()])
    monkeypatch.setattr("app.main_window.QMenu", Menu)
    ui.window._table_menu(QPoint(1, 1))
    assert shown == [["Upload files…", "Upload folder…"]]
    (server.root / "f").write_text("x")
    ui.window.list_model.reload()
    ui.wait_listing(1)
    ui.select("f")
    ui.window._table_menu(QPoint(1, 1))
    assert shown[1][0] == "Download…"


def test_upload_before_connecting_explains_itself(ui, local):
    ui.window._upload([str(local)], "")
    assert ui.window.transfers.view.topLevelItemCount() == 0
    assert "Connect" in ui.window.statusBar().currentMessage()


@pytest.mark.parametrize("label, decision", [("Replace", Decision.REPLACE), ("Skip", Decision.SKIP),
                                             ("Cancel", Decision.CANCEL)])
def test_clash_dialog_buttons(ui, label, decision):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    def click():
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox) and "7 file(s)" in box.text() and "2 more" in box.text()
        next(b for b in box.buttons() if b.text().replace("&", "") == label).click()
    QTimer.singleShot(50, click)
    job = Job(True, ["/x"], "/y")
    assert ui.window.transfers.confirm_replace(job, [f"/y/{i}" for i in range(7)]) is decision


def test_details_dialog_lists_failures_and_skips(ui, server, local, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    (local / "dir").mkdir()
    os.symlink("gone", local / "dir" / "broken")
    (local / "plain").write_text("x")
    (server.root / "plain").mkdir()
    ui.window._upload([str(local / "dir"), str(local / "plain")], str(server.root))
    wait_idle(ui)
    shown = []

    class Box(QMessageBox):
        def exec(self):
            shown.append(self.detailedText())
    monkeypatch.setattr("app.transfer_panel.QMessageBox", Box)
    ui.window.transfers._show_details(ui.window.transfers._rows[0].item)
    assert "Failed:" in shown[0] and "Skipped:" in shown[0] and "broken" in shown[0]

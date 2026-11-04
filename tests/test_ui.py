"""Drives the real window, offscreen, against the real test sshd."""
import os
import subprocess
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import tifffile
from PIL import Image
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.settings import Settings
from app.viewers.image import ImageViewer
from app.viewers.pdf import PdfViewer
from app.viewers.text_log import TextLogViewer
from app.viewers.hex import HexViewer


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(condition, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if condition():
            return True
        time.sleep(0.005)
    return False


class Harness:
    def __init__(self, server, backend, watcher="polling"):
        self.server = server
        self.backend = backend
        self.window = MainWindow(self.backend, Settings())
        self.window.resize(1200, 700)
        self.window.show()
        self.window.settings.watcher = watcher
        params = server.params(watcher=watcher)
        self.window._params = params
        self.window._initial_path = str(server.root)
        self.window._connect()
        assert pump(lambda: self.window.list_model.directory == str(server.root))
        assert pump(lambda: self.backend.watcher_mode() != "off")

    def wait_listing(self, count):
        assert pump(lambda: self.window.list_model.rowCount() == count), \
            self.window.list_model.rowCount()

    def select(self, name):
        model = self.window.list_model
        assert pump(lambda: model.row_of(str(self.server.root / name)) >= 0)
        self.window.table.selectRow(model.row_of(str(self.server.root / name)))

    def close(self):
        editor = self.window.preview.editor
        if editor is not None:  # or the unsaved-changes prompt waits forever
            editor.edit.document().setModified(False)
        self.window.close()


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def test_tree_reveals_path_and_list_shows_metadata(ui, server):
    (server.root / "folder").mkdir()
    (server.root / "file.txt").write_text("hello")
    ui.window.list_model.reload()
    ui.wait_listing(2)
    model = ui.window.list_model
    assert model.entry(0).name == "folder"
    assert model.index(1, 1).data() == "5 B"
    assert model.index(1, 3).data().startswith("-rw")
    assert ui.window.tree.currentIndex().data(Qt.ItemDataRole.UserRole) == str(server.root)


def test_list_and_tree_patch_from_events(ui, server):
    ui.wait_listing(0)
    time.sleep(0.6)
    pump(lambda: False, 0.3)  # polling baseline
    (server.root / "newdir").mkdir()
    (server.root / "new.txt").write_text("x")
    ui.wait_listing(2)
    assert ui.window.list_model.entry(0).name == "newdir"
    (server.root / "new.txt").unlink()
    ui.wait_listing(1)


def test_undecodable_name_displays(ui, server):
    with open(os.fsencode(server.root) + b"/caf\xe9.txt", "wb") as out:
        out.write(b"hi\n")
    ui.window.list_model.reload()
    ui.wait_listing(1)
    assert "caf" in ui.window.list_model.index(0, 0).data()
    ui.window.table.selectRow(0)
    viewer = ui.window.preview
    assert pump(lambda: isinstance(viewer.current, TextLogViewer))
    assert pump(lambda: viewer.current.buffer.get(0, 3) == b"hi\n")


def test_log_viewer_follows_and_handles_truncation(ui, server):
    log = server.root / "app.log"
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    ui.window.list_model.reload()
    ui.select("app.log")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    viewer = preview.current
    ui.window.table.setFocus()
    viewer.view.resize(600, 300)

    def tail_rows():
        viewer.view.repaint()
        return [r.text for r in viewer.view._rows]
    assert pump(lambda: "line 999" in tail_rows())
    pump(lambda: False, 0.6)  # polling baseline
    with log.open("a") as out:
        out.write("appended!\n")
    assert pump(lambda: "appended!" in tail_rows())
    log.write_text("fresh\n")
    assert pump(lambda: tail_rows()[:1] == ["fresh"]), tail_rows()


def test_old_text_file_opens_at_top_without_follow(ui, server):
    path = server.root / "notes.txt"
    path.write_text("".join(f"line {i}\n" for i in range(5000)))
    os.utime(path, (1_000_000_000, 1_000_000_000))
    ui.window.list_model.reload()
    ui.select("notes.txt")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    viewer = preview.current

    def first_row():
        viewer.view.repaint()
        return viewer.view._rows[0].text if viewer.view._rows else None
    assert pump(lambda: first_row() == "line 0")
    assert not viewer.follow.isChecked()


def test_grep_search_jumps_and_highlights(ui, server):
    log = server.root / "big.log"
    log.write_text("".join(f"row {i}\n" for i in range(50000)) + "needle here\n"
                   + "".join(f"row {i}\n" for i in range(5000)))
    ui.window.list_model.reload()
    ui.select("big.log")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    viewer = preview.current
    viewer.search.setText("NEEDLE")
    viewer._search_or_next()
    assert pump(lambda: viewer.status.text() == "1/1")

    def on_screen():
        viewer.view.repaint()
        return bool(viewer.view._rows) and viewer.view._rows[0].text == "needle here"
    assert pump(on_screen)
    assert not viewer.follow.isChecked()
    viewer.follow.setChecked(True)


def test_binary_goes_to_hex(ui, server):
    (server.root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    ui.window.list_model.reload()
    ui.select("blob.bin")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, HexViewer))
    assert pump(lambda: "00 01 02" in preview.current.text.toPlainText())


def test_image_viewer_16bit_reload_keeps_zoom(ui, server):
    path = server.root / "frame.tif"
    array = np.arange(256 * 256, dtype=np.uint16).reshape(256, 256)
    tifffile.imwrite(path, array)
    ui.window.list_model.reload()
    ui.select("frame.tif")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, ImageViewer))
    viewer = preview.current
    assert pump(lambda: viewer.array is not None and viewer.array.dtype == np.uint16)
    assert viewer.high.value() > viewer.low.value() > 0
    pump(lambda: False, 0.6)

    viewer.canvas.fit_mode = False
    viewer.canvas.scale(3, 3)
    transform = viewer.canvas.transform()
    tifffile.imwrite(path, array[::-1].copy())
    # Polling sees whole-second mtimes only; see the inotify test below.
    later = time.time() + 2
    os.utime(path, (later, later))
    assert pump(lambda: viewer.array is not None and viewer.array[0, 0] == array[-1, 0])
    assert viewer.canvas.transform() == transform


def test_same_second_same_size_rewrite_reloads_with_inotify(qapp, inotify_server, make_backend):
    harness = Harness(inotify_server, make_backend(), watcher="inotify")
    try:
        path = inotify_server.root / "frame.tif"
        array = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
        tifffile.imwrite(path, array)
        stamp = path.stat().st_mtime
        harness.window.list_model.reload()
        harness.select("frame.tif")
        preview = harness.window.preview
        assert pump(lambda: isinstance(preview.current, ImageViewer))
        viewer = preview.current
        assert pump(lambda: viewer.array is not None and viewer.array[0, 1] == 1)
        assert pump(lambda: harness.backend.established_watches() >= {str(inotify_server.root)})

        tifffile.imwrite(path, array + 7)
        os.utime(path, (stamp, stamp))
        assert pump(lambda: viewer.array is not None and viewer.array[0, 1] == 8)
    finally:
        harness.close()


def test_image_viewer_multipage_float(ui, server):
    path = server.root / "stack.tiff"
    data = np.random.rand(3, 32, 32).astype(np.float32)
    with tifffile.TiffWriter(path) as tiff:
        for page in data:
            tiff.write(page)
    ui.window.list_model.reload()
    ui.select("stack.tiff")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, ImageViewer))
    viewer = preview.current
    assert pump(lambda: viewer.page.maximum() == 3)
    viewer.page.setValue(2)
    assert np.array_equal(viewer.array, data[1])
    assert not viewer.canvas.item.pixmap().isNull()


def test_png_palette_and_corrupt_image(ui, server):
    Image.new("P", (10, 10)).save(server.root / "p.png")
    (server.root / "bad.png").write_bytes(b"not an image")
    ui.window.list_model.reload()
    ui.select("p.png")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, ImageViewer))
    assert pump(lambda: not preview.current.canvas.item.pixmap().isNull())
    ui.select("bad.png")
    assert pump(lambda: "Could not read" in preview.current.status.text())


def minimal_pdf(pages):
    """Hand-rolled PDF: enough for QtPdf, no extra test dependency."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for _ in range(pages):
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>")
    body, offsets = b"%PDF-1.4\n", []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        body += f"{offset:010} 00000 n \n".encode()
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


def test_pdf_reload_restores_page(ui, server):
    path = server.root / "doc.pdf"
    path.write_bytes(minimal_pdf(3))
    ui.window.list_model.reload()
    ui.select("doc.pdf")
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, PdfViewer))
    viewer = preview.current
    assert pump(lambda: viewer.document.pageCount() == 3)
    viewer.page.setValue(2)
    pump(lambda: False, 0.6)
    path.write_bytes(minimal_pdf(5))
    assert pump(lambda: viewer.document.pageCount() == 5)
    assert viewer.view.pageNavigator().currentPage() == 1


def test_remote_search_opens_result(ui, server):
    (server.root / "deep").mkdir()
    (server.root / "deep" / "target.txt").write_text("found\n")
    ui.window.search.setText("target")
    ui.window._start_search()
    assert pump(lambda: ui.window.results.count() == 1)
    ui.window.results.setCurrentRow(0)
    preview = ui.window.preview
    assert pump(lambda: preview.entry is not None and preview.entry.name == "target.txt")

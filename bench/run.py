"""Benchmarks against a throwaway local sshd. Run with --backend python|rust.

Loopback has ~0 RTT, so these measure our own overhead, not a real network.
Set RV_TEST_INOTIFY_DIR when inotifywait isn't on PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from conftest import Server, inotify_dir  # noqa: E402

from app.backend import create_backend  # noqa: E402

MB = 1024 * 1024


def fetch_range(url: str, start: int, end: int) -> tuple[float, int]:
    """(seconds to first byte, bytes read)"""
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    began = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        first = response.read(1)
        ttfb = time.perf_counter() - began
        rest = response.read()
    return ttfb, len(first) + len(rest)


def median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 2)


def bench_listing(backend, root: Path) -> dict:
    results = {}
    for count in (1000, 50000):
        folder = root / f"dir{count}"
        folder.mkdir()
        for index in range(count):
            (folder / f"file_{index:06}.dat").touch()
        samples = []
        for _ in range(5):
            began = time.perf_counter()
            entries = backend.listdir(str(folder)).result(60)
            samples.append(time.perf_counter() - began)
            assert len(entries) == count
        results[f"listdir_{count}_ms"] = median_ms(samples)
    return results


def bench_throughput(backend, root: Path, size_mb: int) -> dict:
    path = root / "big.bin"
    with path.open("wb") as out:
        for _ in range(size_mb):
            out.write(os.urandom(MB))
    url = backend.open_url(str(path))
    began = time.perf_counter()
    _, length = fetch_range(url, 0, size_mb * MB - 1)
    cold = time.perf_counter() - began
    assert length == size_mb * MB
    return {"range_cold_MBps": round(size_mb / cold, 1)}


def bench_seek(backend, root: Path) -> dict:
    """Random 1 MB reads across a large sparse file: the shape of video seeks."""
    path = root / "sparse.bin"
    size = 5 * 1024 * MB
    with path.open("wb") as out:
        out.truncate(size)
    url = backend.open_url(str(path))
    samples = []
    for index in range(30):
        start = (index * 2654435761) % (size - MB)
        ttfb, _ = fetch_range(url, start, start + MB - 1)
        samples.append(ttfb)
    return {"seek_ttfb_median_ms": median_ms(samples),
            "seek_ttfb_max_ms": round(max(samples) * 1000, 2)}


def bench_log_latency(backend, root: Path) -> dict:
    """Write to a watched file, then time until read() returns the new bytes."""
    path = root / "live.log"
    path.write_bytes(b"start\n")
    arrived = threading.Event()
    state = {"offset": 6}

    def got(future):
        data = future.result()
        if data:
            state["offset"] += len(data)
            arrived.set()

    def on_events(events):
        if any(e.path == str(path) for e in events):
            backend.read(str(path), state["offset"], 4096).add_done_callback(got)
    backend.on_events(on_events)
    backend.watch([str(root)], hot=[str(root), str(path)])
    time.sleep(1.5)
    samples = []
    for index in range(30):
        arrived.clear()
        began = time.perf_counter()
        with path.open("ab") as out:
            out.write(b"line %d\n" % index)
        if arrived.wait(5):
            samples.append(time.perf_counter() - began)
        time.sleep(0.05)
    backend.on_events(lambda _: None)
    return {"log_line_median_ms": median_ms(samples), "log_line_max_ms":
            round(max(samples) * 1000, 2), "watcher": backend.watcher_mode()}


def bench_ui(backend, server: Server, root: Path) -> dict:
    """Event loop stall while opening 50k entries, 1 GB log scrolling, video."""
    import imageio_ffmpeg
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.main_window import MainWindow
    from app.settings import Settings
    from app.viewers.text_log import TextLogViewer
    from app.viewers.video import VideoViewer

    app = QApplication.instance() or QApplication([])
    window = MainWindow(backend)
    window.resize(1200, 700)
    window.show()
    window.tree_model.reset_root("/")

    def pump(condition, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            if condition():
                return True
        return False

    results = {}
    # 1. Stall: a 1 ms timer shows the longest time the UI thread was busy.
    ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    began = time.perf_counter()
    window.navigate(str(root / "dir50000"))
    assert pump(lambda: window.list_model.rowCount() == 50000)
    results["open_50k_ms"] = round((time.perf_counter() - began) * 1000, 1)
    pump(lambda: False, 0.2)
    timer.stop()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    results["open_50k_longest_ui_stall_ms"] = round(max(gaps) * 1000, 1)

    # 2. 1 GB log: repaint time while jumping around, once blocks are local.
    log = root / "huge.log"
    line = b"2026-01-01 12:00:00 INFO worker-7 processed item with a typical line length\n"
    chunk = line * (MB // len(line))
    with log.open("wb") as out:
        for _ in range(1024):
            out.write(chunk)
    window.navigate(str(root))
    assert pump(lambda: window.list_model.row_of(str(log)) >= 0)
    began = time.perf_counter()
    window.table.selectRow(window.list_model.row_of(str(log)))
    preview = window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    viewer = preview.current

    def rows_visible():
        viewer.view.repaint()
        return bool(viewer.view._rows)
    assert pump(rows_visible)
    results["log_1gb_first_rows_ms"] = round((time.perf_counter() - began) * 1000, 1)
    viewer.follow.setChecked(False)
    size = log.stat().st_size
    paint, wait = [], []
    for index in range(40):
        target = (index * 2654435761) % size
        began = time.perf_counter()
        viewer.view.jump_to(target)
        assert pump(rows_visible)
        wait.append(time.perf_counter() - began)
        for _ in range(30):
            began = time.perf_counter()
            viewer.view.scroll_lines(3)
            viewer.view.repaint()
            paint.append(time.perf_counter() - began)
    results["log_1gb_jump_median_ms"] = median_ms(wait)
    results["log_1gb_scroll_frame_median_ms"] = median_ms(paint)
    results["log_1gb_scroll_frame_p99_ms"] = round(
        sorted(paint)[int(len(paint) * 0.99)] * 1000, 2)
    preview.clear()

    # 3. Video: first frame, seek, and bytes actually pulled.
    video = root / "clip.mp4"
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=duration=120:size=1280x720:rate=30", "-c:v", "libx264",
         "-preset", "ultrafast", "-b:v", "20M", "-minrate", "20M", "-maxrate", "20M",
         "-bufsize", "4M", "-g", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(video)], check=True)
    results["video_size_mb"] = round(video.stat().st_size / MB, 1)
    assert pump(lambda: window.list_model.row_of(str(video)) >= 0 or (
        window.list_model.reload() or False), 10)
    reads_before = backend.reads_issued()
    began = time.perf_counter()
    window.table.selectRow(window.list_model.row_of(str(video)))
    assert pump(lambda: isinstance(preview.current, VideoViewer))
    viewer = preview.current
    sink = viewer.player.widget.videoSink()
    frames = []
    sink.videoFrameChanged.connect(lambda frame: frames.append(time.perf_counter()))
    assert pump(lambda: frames, 15)
    results["video_first_frame_ms"] = round((frames[0] - began) * 1000, 1)
    player = viewer.player.player
    samples = []
    for target in (100000, 20000, 70000, 110000, 40000, 90000):
        count = len(frames)
        began = time.perf_counter()
        viewer.player.seek(target)
        assert pump(lambda: abs(player.position() - target) < 1000 and len(frames) > count, 10)
        samples.append(time.perf_counter() - began)
    results["video_seek_median_ms"] = median_ms(samples)
    results["video_seek_max_ms"] = round(max(samples) * 1000, 1)
    pulled = (backend.reads_issued() - reads_before) * 256 * 1024
    results["video_pulled_mb"] = round(pulled / MB, 1)
    preview.clear()
    window.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="python", choices=("python", "rust"))
    parser.add_argument("--size-mb", type=int, default=256)
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--json")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="rv-bench-") as temp:
        directory = inotify_dir()
        server = Server(Path(temp), path_prefix=directory or "")
        backend = create_backend(args.backend)
        results: dict = {"backend": args.backend}
        try:
            backend.connect(server.params()).result(15)
            for _ in range(200):
                if backend.watcher_mode() != "off":
                    break
                time.sleep(0.01)
            root = server.root
            results.update(bench_listing(backend, root))
            results.update(bench_throughput(backend, root, args.size_mb))
            results.update(bench_seek(backend, root))
            results.update(bench_log_latency(backend, root))
            if not args.skip_ui:
                results.update(bench_ui(backend, server, root))
        finally:
            backend.close()
            server.stop()
    print(json.dumps(results, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

import os
import queue
import subprocess
import threading
import time

import pytest

from app.backend import ConnectionState, EventKind
from app.backend.py_backend.watcher import parse_event_line


def wait_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class Collector:
    def __init__(self, backend):
        self.events = queue.Queue()
        backend.on_events(lambda batch: [self.events.put(e) for e in batch])

    def expect(self, kind, path, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=max(deadline - time.time(), 0.01))
            except queue.Empty:
                break
            if event.kind is kind and event.path == str(path):
                return event
        pytest.fail(f"no {kind} for {path}")


def test_coalescer_sends_first_event_at_once_and_batches_the_burst():
    import asyncio

    from app.backend import Event
    from app.backend.py_backend.watcher import Coalescer

    async def main():
        batches = []
        coalescer = Coalescer(batches.append, window=0.02)
        coalescer.add(Event(EventKind.MODIFY, "/first"))
        assert len(batches) == 1  # no waiting for the window
        for index in range(100):
            coalescer.add(Event(EventKind.MODIFY, f"/burst{index % 10}"))
        await asyncio.sleep(0.03)
        assert len(batches) == 2 and len(batches[1]) == 10
        await asyncio.sleep(0.05)
        coalescer.add(Event(EventKind.MODIFY, "/later"))
        assert len(batches) == 3
    asyncio.run(main())


def test_burst_on_real_watcher_arrives_complete(inotify_server, inotify_backend):
    root = inotify_server.root
    batches = queue.Queue()
    inotify_backend.on_events(batches.put)
    inotify_backend.watch([str(root)])
    assert established(inotify_backend, [str(root)])
    for index in range(300):
        (root / f"f{index}").write_text("x")
    seen, count = set(), 0
    deadline = time.time() + 5
    while len(seen) < 300 and time.time() < deadline:
        try:
            batch = batches.get(timeout=1)
        except queue.Empty:
            break
        count += 1
        seen |= {e.path for e in batch if e.kind is EventKind.CLOSE_WRITE}
    assert len(seen) == 300
    assert count < 300  # coalesced, not one callback per event


def test_parse_event_line():
    (event,) = parse_event_line(b"CREATE,ISDIR|/a//b|c\n")
    assert event.kind is EventKind.CREATE and event.path == "/a/b|c" and event.is_dir
    kinds = [e.kind for e in parse_event_line(b"CLOSE_WRITE,CLOSE|/a/b\n")]
    assert kinds == [EventKind.CLOSE_WRITE]
    assert parse_event_line(b"") == []


@pytest.fixture
def inotify_backend(inotify_server, make_backend):
    instance = make_backend()
    instance.connect(inotify_server.params(watcher="inotify")).result(10)
    assert wait_until(lambda: instance.watcher_mode() == "inotify")
    return instance


def established(backend, directories):
    return wait_until(lambda: backend.established_watches() == set(directories))


def test_inotify_events(inotify_server, inotify_backend):
    root = inotify_server.root
    collect = Collector(inotify_backend)
    inotify_backend.watch([str(root)])
    assert established(inotify_backend, [str(root)])

    path = root / "a.log"
    path.write_text("x")
    collect.expect(EventKind.CREATE, path)
    collect.expect(EventKind.CLOSE_WRITE, path)
    path.rename(root / "b.log")
    collect.expect(EventKind.MOVED_FROM, path)
    collect.expect(EventKind.MOVED_TO, root / "b.log")
    (root / "b.log").unlink()
    collect.expect(EventKind.DELETE, root / "b.log")


def test_watch_set_changes_and_missing_dirs_are_ignored(inotify_server, inotify_backend):
    root = inotify_server.root
    (root / "one").mkdir()
    (root / "two").mkdir()
    collect = Collector(inotify_backend)
    inotify_backend.watch([str(root / "one"), str(root / "missing")])
    assert established(inotify_backend, [str(root / "one"), str(root / "missing")])
    inotify_backend.watch([str(root / "two")])
    assert established(inotify_backend, [str(root / "two")])
    (root / "two" / "f").write_text("x")
    collect.expect(EventKind.CLOSE_WRITE, root / "two" / "f")


def remote_watchers(root):
    out = subprocess.run(["pgrep", "-f", f"inotifywait.*{root}"], capture_output=True, text=True)
    return [p for p in out.stdout.split() if int(p) != os.getpid()]


def test_remote_watcher_exits_when_replaced_or_closed(inotify_server, make_backend):
    backend = make_backend()
    backend.connect(inotify_server.params(watcher="inotify")).result(10)
    root = inotify_server.root
    (root / "x").mkdir()
    wait_until(lambda: backend.watcher_mode() == "inotify")
    backend.watch([str(root)])
    assert established(backend, [str(root)])
    assert wait_until(lambda: len(remote_watchers(root)) >= 1)
    backend.watch([str(root), str(root / "x")])
    assert established(backend, [str(root), str(root / "x")])
    backend.close()
    assert wait_until(lambda: not remote_watchers(root)), remote_watchers(root)


def test_falls_back_to_polling_without_inotifywait(server, make_backend):
    backend = make_backend()
    backend.connect(server.params(watcher="auto")).result(10)
    assert wait_until(lambda: backend.watcher_mode() != "off")
    if subprocess.run(["sh", "-c", "command -v inotifywait"], capture_output=True).returncode == 0:
        pytest.skip("inotifywait is installed")
    assert backend.watcher_mode() == "polling"


def test_polling_events(server, backend):
    root = server.root
    target = root / "hot.log"
    target.write_text("a")
    collect = Collector(backend)
    assert wait_until(lambda: backend.watcher_mode() == "polling")
    backend.watch([str(root)], hot=[str(root), str(target)])
    time.sleep(0.6)  # baseline pass

    (root / "new").write_text("x")
    collect.expect(EventKind.CREATE, root / "new")
    collect.expect(EventKind.CLOSE_WRITE, root / "new")
    target.write_text("abc")
    collect.expect(EventKind.MODIFY, target)
    collect.expect(EventKind.CLOSE_WRITE, target)
    (root / "new").unlink()
    collect.expect(EventKind.DELETE, root / "new")


def test_reconnects_and_resyncs(tmp_path, make_backend):
    from conftest import Server
    server = Server(tmp_path)
    states = queue.Queue()
    backend = make_backend()
    backend.on_state(lambda state, detail: states.put(state))
    try:
        backend.connect(server.params(watcher="polling")).result(10)
        (server.root / "f").write_text("data")
        wait_until(lambda: backend.watcher_mode() == "polling")
        collect = Collector(backend)
        backend.watch([str(server.root)])

        # Kill only the per-connection child; the listener stays up.
        subprocess.run(["pkill", "-P", str(server.process.pid)], check=False)
        assert backend.read(str(server.root / "f"), 0, 4).result(20) == b"data"
        collect.expect(EventKind.RESYNC, server.root, timeout=10)
        seen = []
        while not states.empty():
            seen.append(states.get())
        assert ConnectionState.RECONNECTING in seen
        assert seen[-1] is ConnectionState.CONNECTED
    finally:
        server.stop()

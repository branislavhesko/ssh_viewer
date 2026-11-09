"""Running and debugging a Python file on the server, through app/debug_agent.py."""
from __future__ import annotations

import codecs
import json
import pkgutil
import posixpath
import shlex
import threading
from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QObject, Signal

from app.backend import Backend
from app.bridge import Bridge

# Loads the agent, sent as the first stdin line, into a module of its own: the
# script gets a clean __main__. Double quotes only, so any shell can take it.
BOOTSTRAP = ('import sys,json,types;m=types.ModuleType("rv_agent");sys.modules["rv_agent"]=m;'
             'exec(compile(json.loads(sys.stdin.readline()),"<rv-agent>","exec"),m.__dict__)')
DEFAULT_INTERPRETER = "python3"


@lru_cache(maxsize=1)
def agent_source() -> str:
    return pkgutil.get_data("app", "debug_agent.py").decode("utf-8")


def run_command(path: str, interpreter: str) -> str:
    """``interpreter`` is the user's own words, e.g. ~/venv/bin/python."""
    folder = posixpath.dirname(path) or "/"
    return (f"cd {shlex.quote(folder)} && exec {interpreter.strip() or DEFAULT_INTERPRETER} "
            f"-u -c {shlex.quote(BOOTSTRAP)}")


class PythonSession(QObject):
    """One run of one script. Signals arrive on the Qt thread."""

    output = Signal(str)           # what the script printed, stdout and stderr together
    event = Signal(dict)           # agent events: started, paused, running, vars, inspect, ...
    finished = Signal(object, str)  # exit status or None, and a message ("" if none)

    def __init__(self, backend: Backend, bridge: Bridge, path: str, interpreter: str,
                 debug: bool, breakpoints: dict[str, set[int]],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.path, self.debug = path, debug
        self.bridge = bridge
        self.running = True
        self.status_from_agent: Optional[int] = None
        self._lines = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._scheduled = False
        self.process = backend.start_process(run_command(path, interpreter), self._stdout,
                                             self._stderr, self._exited)
        start = {"path": path, "debug": debug,
                 "breakpoints": {file: sorted(lines) for file, lines in breakpoints.items() if lines}}
        self.process.write((json.dumps(agent_source()) + "\n" + json.dumps(start) + "\n").encode())

    # Commands ----------------------------------------------------------------
    def send(self, command: dict) -> None:
        if self.running:
            self.process.write((json.dumps(command) + "\n").encode())

    def set_breakpoints(self, path: str, lines: set[int]) -> None:
        self.send({"cmd": "break", "path": path, "lines": sorted(lines)})

    def stop(self) -> None:
        if self.running:
            self.process.close()

    # Backend thread ----------------------------------------------------------
    def _stdout(self, data: bytes) -> None:
        self._lines += data
        *complete, self._lines = self._lines.split(b"\n")
        for line in complete:
            try:
                event = json.loads(line)
            except ValueError:  # not the agent talking: show it rather than lose it
                self._print(line.decode("utf-8", "replace") + "\n")
                continue
            self.bridge.post(lambda e=event: self._event(e))

    def _stderr(self, data: bytes) -> None:
        self._print(self._decoder.decode(data))

    def _print(self, text: str) -> None:
        """Coalesce: a chatty script costs one repaint per Qt turn, not per chunk."""
        with self._lock:
            self._pending.append(text)
            if self._scheduled:
                return
            self._scheduled = True
        self.bridge.post(self._flush)

    def _exited(self, status: Optional[int], error: Optional[str]) -> None:
        self._print(self._decoder.decode(b"", final=True))
        self.bridge.post(lambda: self._finish(status, error))

    # Qt thread ---------------------------------------------------------------
    def _flush(self) -> None:
        with self._lock:
            text = "".join(self._pending)
            self._pending.clear()
            self._scheduled = False
        if text:
            self.output.emit(text)

    def _event(self, event: dict) -> None:
        if event.get("ev") == "finished":
            self.status_from_agent = event.get("status")
        self.event.emit(event)

    def _finish(self, status: Optional[int], error: Optional[str]) -> None:
        self._flush()
        if not self.running:
            return
        self.running = False
        if status is None:
            status = self.status_from_agent
        self.finished.emit(status, error or "")

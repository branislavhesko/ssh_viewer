"""Finding a Python on the server to run scripts with."""
from __future__ import annotations

import posixpath
import shlex
from typing import Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QWidget

from app.backend import Backend
from app.bridge import Bridge, display_text

DETECT_TIMEOUT_MS = 15000
PARENT_FOLDERS = 6  # how far up from the script to look for a .venv

# Where conda, pyenv and virtualenvwrapper keep their Pythons; globs the shell expands.
HOME_PLACES = (
    "miniconda3/bin/python", "miniconda3/envs/*/bin/python",
    "anaconda3/bin/python", "anaconda3/envs/*/bin/python",
    "miniforge3/bin/python", "miniforge3/envs/*/bin/python",
    "mambaforge/bin/python", "mambaforge/envs/*/bin/python",
    ".conda/envs/*/bin/python", ".virtualenvs/*/bin/python",
    ".pyenv/versions/*/bin/python", ".local/share/virtualenvs/*/bin/python",
)
VENV_NAMES = (".venv", "venv", "env")


def folders_up(folder: str) -> list[str]:
    """``folder`` and its parents, nearest first, a few levels at most."""
    found = []
    folder = posixpath.normpath(folder) if folder else ""
    while folder and len(found) < PARENT_FOLDERS:
        found.append(folder)
        if folder == "/":
            break
        folder = posixpath.dirname(folder)
    return found


def detect_command(folder: str) -> str:
    """POSIX sh, whatever the login shell: one ``path<TAB>version<TAB>key``
    line per Python found. ``key`` is its folder plus resolved target, so
    python and python3 side by side count once while a venv's python, a
    symlink to the system one, still counts on its own."""
    near = " ".join(shlex.quote(posixpath.join(f, name, "bin", "python"))
                    for f in folders_up(folder) for name in VENV_NAMES)
    home = " ".join(f'"$HOME"/{place}' for place in HOME_PLACES)
    script = (
        'for p in "$(command -v python3)" "$(command -v python)" '
        f"{near} {home} /usr/local/bin/python3 /usr/bin/python3; do "
        '[ -n "$p" ] && [ -f "$p" ] && [ -x "$p" ] || continue; '
        'v=$("$p" -c "import sys; print(sys.version.split()[0])" 2>/dev/null) || v=""; '
        'r=$(readlink -f "$p" 2>/dev/null) || r="$p"; '
        "printf '%s\\t%s\\t%s\\n' \"$p\" \"$v\" \"$(dirname \"$p\") $r\"; "
        "done")
    return "sh -c " + shlex.quote(script)


def parse_detected(text: str) -> list[tuple[str, str]]:
    """``(path, version)`` in the order found, each Python once."""
    seen, found = set(), []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        path, version, key = parts
        if key in seen or path in {p for p, _ in found}:
            continue
        seen.add(key)
        found.append((path, version))
    return found


class InterpreterMenu(QMenu):
    """Pick the Python: the one active in the Terminal tab, one found on the
    server, or a path typed by hand. Rebuilt, and the server searched again,
    each time it opens."""

    def __init__(self, backend: Backend, bridge: Bridge, current: Callable[[], str],
                 choose: Callable[[str, str], None], folder: Callable[[], str],
                 type_path: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.backend, self.bridge = backend, bridge
        self._current, self._choose, self._folder, self._type_path = current, choose, folder, type_path
        # Set by the window: asks the terminal, then calls done(path) or failed(message).
        self.ask_terminal: Optional[Callable[[Callable[[str], None], Callable[[str], None]], None]] = None
        self.on_failure: Callable[[str], None] = lambda _message: None
        self._request = 0
        self._process = None
        self._placeholder = None
        self._found_anchor = None
        self.aboutToShow.connect(self.rebuild)

    def rebuild(self) -> None:
        self.clear()
        self._request += 1
        current = self._current()
        using = self.addAction(f"Using: {current}")
        using.setEnabled(False)
        self.addSeparator()
        terminal = self.addAction("From the terminal's active environment",
                                  self._from_terminal)
        terminal.setToolTip("The python3 the Terminal tab's shell finds now: activate a "
                            "conda env or venv there first")
        self.addSeparator()
        self._placeholder = self.addAction("Searching the server…")
        self._placeholder.setEnabled(False)
        self._found_anchor = self.addSeparator()
        self.addAction("Type a path…", self._type_path)
        self._detect()

    # The server --------------------------------------------------------------
    def _detect(self) -> None:
        if self._process is not None:
            self._process.close()
        request, chunks = self._request, []

        def exited(status: Optional[int], error: Optional[str]) -> None:
            text = b"".join(chunks).decode("utf-8", "replace")
            self.bridge.post(lambda: self._detected(request, parse_detected(text), error))
        try:
            self._process = self.backend.start_process(detect_command(self._folder()),
                                                       chunks.append, lambda _data: None, exited)
        except Exception as exc:  # not connected
            self._detected(request, [], str(exc))
            return
        QTimer.singleShot(DETECT_TIMEOUT_MS, lambda: self._timed_out(request))

    def _timed_out(self, request: int) -> None:
        if request == self._request and self._placeholder is not None:
            if self._process is not None:
                self._process.close()
            self._placeholder.setText("The search timed out")
            self._placeholder = None

    def _detected(self, request: int, found: list[tuple[str, str]], error: Optional[str]) -> None:
        if request != self._request or self._placeholder is None:
            return
        self._process = None
        if not found:
            self._placeholder.setText(f"Search failed: {error}" if error else "No Python found")
            self._placeholder = None
            return
        self.removeAction(self._placeholder)
        self._placeholder = None
        current = self._current()
        for path, version in found:
            label = f"{display_text(path)}  ({version})" if version else display_text(path)
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(path == current)
            action.triggered.connect(lambda _checked=False, p=path: self._choose(p, "found on the server"))
            self.insertAction(self._found_anchor, action)  # above "Type a path…"

    # The terminal ------------------------------------------------------------
    def _from_terminal(self) -> None:
        if self.ask_terminal is None:
            self.on_failure("There is no terminal to ask.")
            return
        self.ask_terminal(lambda path: self._choose(path, "from the terminal"), self.on_failure)

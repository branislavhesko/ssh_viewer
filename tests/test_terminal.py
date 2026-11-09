"""The built-in terminal: emulator pieces alone, then the tab against real sshd."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from app.terminal import TerminalScreen, TerminalView, key_bytes
from tests.test_ui import Harness, pump, qapp  # noqa: F401  (qapp is a fixture)


def feed(screen, text):
    import pyte
    pyte.ByteStream(screen).feed(text.encode())


def rows(screen):
    return [screen.display[y].rstrip() for y in range(screen.lines)]


def test_lines_scrolled_off_go_to_history():
    screen = TerminalScreen(20, 3, lambda _: None)
    feed(screen, "one\r\ntwo\r\nthree\r\nfour\r\nfive")
    assert rows(screen) == ["three", "four", "five"]
    assert ["".join(c.data for c in line.values()).rstrip() for line in screen.history] \
        == ["one", "two"]
    assert screen.first_line() == 0 and screen.line(4)[0].data == "f"


def test_alternate_screen_restores_and_keeps_history_clean():
    screen = TerminalScreen(20, 3, lambda _: None)
    feed(screen, "prompt$ ")
    feed(screen, "\x1b[?1049h\x1b[Hfull\r\nscreen\r\napp\r\nscrolls")
    assert len(screen.history) == 0
    feed(screen, "\x1b[?1049l")
    assert rows(screen)[0] == "prompt$" and (screen.cursor.x, screen.cursor.y) == (8, 0)


def test_shrinking_keeps_the_prompt_and_growing_keeps_text():
    screen = TerminalScreen(20, 10, lambda _: None)
    feed(screen, "a\r\nb\r\n$ ")
    screen.resize(4, 20)
    assert rows(screen)[:3] == ["a", "b", "$"] and len(screen.history) == 0
    screen.resize(2, 20)
    assert rows(screen) == ["b", "$"] and screen.cursor.y == 1 and len(screen.history) == 1


def test_clear_scrollback_sequence():
    screen = TerminalScreen(20, 2, lambda _: None)
    feed(screen, "1\r\n2\r\n3\r\n4")
    feed(screen, "\x1b[3J")
    assert len(screen.history) == 0 and rows(screen) == ["3", "4"]


def test_device_status_report_is_answered():
    replies = []
    screen = TerminalScreen(20, 5, replies.append)
    feed(screen, "ab\x1b[6n")
    assert replies == ["\x1b[1;3R"]


def key(code, modifiers=Qt.KeyboardModifier.NoModifier, text=""):
    return QKeyEvent(QEvent.Type.KeyPress, code, modifiers, text)


def test_keys(qapp):
    control = Qt.KeyboardModifier.ControlModifier
    assert key_bytes(key(Qt.Key.Key_C, control, "\x03"), False) == b"\x03"
    assert key_bytes(key(Qt.Key.Key_Up), False) == b"\x1b[A"
    assert key_bytes(key(Qt.Key.Key_Up), True) == b"\x1bOA"
    assert key_bytes(key(Qt.Key.Key_Return, text="\r"), False) == b"\r"
    assert key_bytes(key(Qt.Key.Key_Backspace), False) == b"\x7f"
    assert key_bytes(key(Qt.Key.Key_B, Qt.KeyboardModifier.AltModifier, "b"), False) == b"\x1bb"
    assert key_bytes(key(Qt.Key.Key_Eacute, text="é"), False) == "é".encode()
    assert key_bytes(key(Qt.Key.Key_Shift), False) == b""


def test_view_paints_and_selects(qapp):
    view = TerminalView()
    view.resize(400, 200)
    view.show()
    typed = []
    view.typed.connect(typed.append)
    view.feed(b"\x1b[31mred\x1b[0m wide \xe4\xb8\xad!\r\nnext")
    view.grab()  # paints without raising
    assert view.visible_text().splitlines()[:2] == ["red wide 中!", "next"]
    view._selection = ((view.top_line(), 0), (view.top_line(), 3))
    assert view.selected_text() == "red"
    view.keyPressEvent(key(Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier, "\x0c"))
    assert typed == [b"\x0c"]
    view.close()


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def screen_text(ui):
    return ui.window.terminal.view.visible_text()


def run(ui, command, expect):
    ui.window.terminal.view.typed.emit(command.encode() + b"\r")
    assert pump(lambda: expect in screen_text(ui), 10), screen_text(ui)


def test_terminal_tab_runs_commands_in_the_listed_folder(ui, server):
    window = ui.window
    window.tabs.setCurrentWidget(window.terminal)
    assert pump(window.terminal.running)
    # Markers built by the shell, so the echoed command line can't match.
    run(ui, "echo P$((1+1))=$(pwd)", f"P2={server.root}")

    (server.root / "sub").mkdir()
    window.open_terminal(str(server.root / "sub"))
    run(ui, "echo Q$((1+1))=$(pwd)", f"Q2={server.root / 'sub'}")

    window.terminal.view.typed.emit(b"exit\r")
    assert pump(lambda: "Press Enter for a new one" in screen_text(ui), 10), screen_text(ui)
    assert not window.terminal.running()
    window.terminal.view.typed.emit(b"\r")
    assert pump(window.terminal.running)
    run(ui, "echo R$((1+1))", "R2")


def test_resizing_the_window_resizes_the_pty(ui):
    window = ui.window
    window.tabs.setCurrentWidget(window.terminal)
    assert pump(window.terminal.running)
    run(ui, "echo S$((1+1))=$(stty size)", "S2=")
    window.resize(1500, 900)
    assert pump(lambda: window.terminal.view.grid_size()
                == (window.terminal.view.screen.columns, window.terminal.view.screen.lines))
    columns, lines = window.terminal.view.grid_size()
    run(ui, "echo T$((1+1))=$(stty size)", f"T2={lines} {columns}")

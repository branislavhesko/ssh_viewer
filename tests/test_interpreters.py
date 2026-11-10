"""Picking the Python: detection on the server, the terminal's answer, the menu."""
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.interpreters import detect_command, folders_up, parse_detected
from app.terminal import TerminalPanel
from tests.test_ui import Harness, pump, qapp  # noqa: F401  (qapp is a fixture)


def fake_env(folder, name="bin"):
    """A venv-like folder whose python and python3 point at this Python."""
    bin_dir = folder / name
    bin_dir.mkdir(parents=True)
    for exe in ("python", "python3"):
        (bin_dir / exe).symlink_to(sys.executable)
    return bin_dir / "python"


def test_folders_up_stops_at_root_and_after_a_few():
    assert folders_up("/a/b") == ["/a/b", "/a", "/"]
    assert len(folders_up("/1/2/3/4/5/6/7/8")) == 6
    assert folders_up("") == []


def test_parse_counts_python_and_python3_side_by_side_once():
    text = ("/e/bin/python3\t3.12.1\t/e/bin /usr/bin/python3.12\n"
            "/e/bin/python\t3.12.1\t/e/bin /usr/bin/python3.12\n"
            "/usr/bin/python3\t3.12.1\t/usr/bin /usr/bin/python3.12\n"
            "garbage\n"
            "/usr/bin/python3\t3.12.1\t/usr/bin /usr/bin/python3.12\n")
    assert parse_detected(text) == [("/e/bin/python3", "3.12.1"), ("/usr/bin/python3", "3.12.1")]


def test_detection_finds_a_venv_above_the_script_and_in_home(tmp_path):
    project = tmp_path / "project"
    venv_python = fake_env(project / ".venv")
    home = tmp_path / "home"
    conda_python = fake_env(home / "miniconda3" / "envs" / "ml")
    output = subprocess.run(detect_command(str(project / "src" / "deep")), shell=True,
                            capture_output=True, text=True, env={**os.environ, "HOME": str(home)},
                            check=True).stdout
    found = dict(parse_detected(output))
    version = sys.version.split()[0]
    assert found[str(venv_python)] == version and found[str(conda_python)] == version
    assert str(venv_python.with_name("python3")) not in found  # same env, once


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.window.run_panel.stop()
    harness.close()


def test_menu_lists_found_pythons_and_choosing_one_sets_it(ui, server):
    script = server.root / "job.py"
    script.write_text("print(1)\n")
    venv_python = fake_env(server.root / ".venv")
    ui.window.list_model.reload()
    ui.select("job.py")
    assert pump(lambda: ui.window.preview.entry is not None)
    menu = ui.window.run_panel.interpreter_menu
    menu.rebuild()
    labels = lambda: [action.text() for action in menu.actions()]
    assert pump(lambda: any(text.startswith(str(venv_python)) for text in labels()), 15), labels()
    assert labels()[0].startswith("Using: ") and labels()[-1] == "Type a path…"
    action = next(a for a in menu.actions() if a.text().startswith(str(venv_python)))
    action.trigger()
    panel = ui.window.run_panel
    assert panel.interpreter.text() == str(venv_python)
    assert panel.isVisible() and "found on the server" in panel.status.text()


def test_terminal_reports_its_active_python_without_showing_the_answer(ui, server):
    window = ui.window
    env_python = fake_env(server.root / "my env")  # a space, so the choice gets quoted
    window.tabs.setCurrentWidget(window.terminal)
    assert pump(window.terminal.running)
    window.terminal.view.typed.emit(f"export PATH='{env_python.parent}':$PATH\r".encode())
    answers, failures = [], []
    window.terminal.ask_python(answers.append, failures.append)
    assert pump(lambda: answers or failures, 10)
    assert answers == [str(env_python.with_name("python3"))], failures
    # Only the typed command echoes; the answer itself never reaches the screen.
    assert str(env_python.with_name("python3")) not in window.terminal.view.visible_text()
    assert "\x1b" not in window.terminal.view.visible_text()

    window.run_panel.interpreter_menu.ask_terminal(
        lambda path: window.run_panel.choose_interpreter(path, "from the terminal"), failures.append)
    assert pump(lambda: "from the terminal" in window.run_panel.status.text(), 10), failures
    assert window.run_panel.interpreter.text() == f"'{env_python.with_name('python3')}'"


def test_terminal_answer_split_across_chunks(qapp):
    panel = TerminalPanel.__new__(TerminalPanel)  # only the parsing, no shell
    answers = []
    panel._question = (b"abcd", answers.append, lambda message: answers.append(("failed", message)))
    panel._held = b""
    whole = b"before \x1b]777;rv-python;abcd;/opt/py/bin/python3\r\n\x07 after"
    shown = b""
    pieces = [whole[:9], whole[9:12], whole[12:30], whole[30:]]
    for piece in pieces:
        shown += panel._take_answer(piece) if panel._question else piece
    assert answers == ["/opt/py/bin/python3"]
    assert shown == b"before  after"


def test_asking_with_no_shell_explains(ui):
    failures = []
    ui.window.terminal.ask_python(lambda _p: None, failures.append)
    assert failures and "Open the Terminal tab" in failures[0]

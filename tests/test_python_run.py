"""Running Python on the server: the agent alone, then the editor and panel."""
import json
import os
import subprocess
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from app.python_run import BOOTSTRAP, agent_source, run_command
from tests.test_ui import Harness, pump, qapp  # noqa: F401  (qapp is a fixture)

# torch and pandas tests need an interpreter that has them.
DATA_PYTHON = os.environ.get("RV_TEST_PYTHON", "")


def has_modules(python, *modules):
    if not python:
        return False
    check = ";".join(f"import {m}" for m in modules)
    return subprocess.run([python, "-c", check], capture_output=True).returncode == 0


class Agent:
    """The agent as the backend runs it, but as a local process."""

    def __init__(self, path, debug=True, breakpoints=(), python=sys.executable):
        self.process = subprocess.Popen([python, "-u", "-c", BOOTSTRAP], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        start = {"path": str(path), "debug": debug, "breakpoints": {str(path): list(breakpoints)}}
        self.write(json.dumps(agent_source()))
        self.write(json.dumps(start))
        self.stderr = b""
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for chunk in iter(lambda: self.process.stderr.read1(4096), b""):
            self.stderr += chunk

    def write(self, line):
        self.process.stdin.write(line.encode() + b"\n")
        self.process.stdin.flush()

    def send(self, **command):
        self.write(json.dumps(command))

    def next_event(self, kind=None):
        while True:
            line = self.process.stdout.readline()
            assert line, f"agent ended; stderr: {self.stderr.decode()}"
            event = json.loads(line)
            if kind is None or event["ev"] == kind:
                return event

    def inspect(self, expr):
        self.send(cmd="inspect", expr=expr)
        return self.next_event("inspect")

    def finish(self):
        event = self.next_event("finished")
        self.process.wait(10)
        return event


def info(result):
    return dict(result["info"])


@pytest.fixture
def script(tmp_path):
    def make(text):
        path = tmp_path / "script.py"
        path.write_text(text)
        return path
    return make


def test_breakpoints_steps_and_variables(script):
    path = script("x = 1\n"
                  "def double(a):\n"
                  "    b = a * 2\n"
                  "    return b\n"
                  "y = double(x)\n"
                  "z = y + 1\n")
    agent = Agent(path, breakpoints=[3])
    assert agent.next_event()["ev"] == "started"
    paused = agent.next_event("paused")
    assert paused["stack"][-1]["line"] == 3 and paused["stack"][-1]["name"] == "double"
    assert [v["name"] for v in paused["vars"]] == ["a"]
    agent.send(cmd="next")
    assert agent.next_event("paused")["stack"][-1]["line"] == 4
    agent.send(cmd="frame", index=0)  # the module frame: functions are hidden
    assert [v["name"] for v in agent.next_event("vars")["vars"]] == ["x"]
    agent.send(cmd="return")
    agent.send(cmd="next")
    assert agent.next_event("paused")["stack"][-1]["line"] in (5, 6)
    agent.send(cmd="continue")
    assert agent.finish()["status"] == 0


def test_breakpoint_added_while_running_is_hit(script, tmp_path):
    flag = tmp_path / "go"
    path = script("import os, time\n"
                  f"while not os.path.exists({str(flag)!r}):\n"
                  "    time.sleep(0.01)\n"
                  "done = True\n")
    agent = Agent(path)
    agent.next_event("started")
    agent.send(cmd="break", path=str(path), lines=[4])
    time.sleep(0.2)
    flag.touch()
    assert agent.next_event("paused")["stack"][-1]["line"] == 4
    agent.send(cmd="continue")
    agent.finish()


def test_output_goes_to_stderr_and_crashes_pause_post_mortem(script):
    path = script("import subprocess, sys\n"
                  "print('hello')\n"
                  "subprocess.run(['echo', 'child'])\n"
                  "items = [1, 2]\n"
                  "items[5]\n")
    agent = Agent(path, debug=False)
    agent.next_event("started")
    assert "IndexError" in agent.next_event("exception")["text"]
    paused = agent.next_event("paused")
    assert paused["post_mortem"] and paused["stack"][-1]["line"] == 5
    assert agent.inspect("items")["summary"].startswith("len 2")
    agent.send(cmd="continue")
    assert agent.finish()["status"] == 1
    text = agent.stderr.decode()
    assert "hello\n" in text and "child\n" in text
    assert "<rv-agent>" not in text and 'script.py", line 5' in text


def test_system_exit_status(script):
    agent = Agent(script("import sys\nsys.exit(4)\n"), debug=False)
    assert agent.finish()["status"] == 4


def test_closing_stdin_stops_the_script_and_its_children(script, tmp_path):
    marker = tmp_path / "pid"
    path = script("import subprocess, time\n"
                  "child = subprocess.Popen(['sleep', '60'])\n"
                  f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
                  "time.sleep(60)\n")
    agent = Agent(path, debug=False)
    agent.next_event("started")
    deadline = time.time() + 10
    while not marker.exists() or not marker.read_text():
        assert time.time() < deadline
        time.sleep(0.02)
    agent.process.stdin.close()
    agent.process.wait(10)
    child = int(marker.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the script's child outlived Stop")


def test_inspecting_numpy_arrays(script):
    pytest.importorskip("numpy")
    path = script("import numpy as np\n"
                  "a = np.arange(24, dtype=np.float32).reshape(2, 3, 4)\n"
                  "a[0, 0, 0] = np.nan\n"
                  "big = np.zeros((500, 80))\n"
                  "pass\n")
    agent = Agent(path, breakpoints=[5])
    paused = agent.next_event("paused")
    summaries = {v["name"]: v["summary"] for v in paused["vars"]}
    assert summaries == {"a": "float32 [2, 3, 4]", "big": "float64 [500, 80]"}
    result = agent.inspect("a")
    facts = info(result)
    assert facts["nan"] == "1" and facts["min"] == "1" and facts["max"] == "23"
    assert result["note"] == "showing [0, :, :]"
    assert result["table"]["rows"][0] == ["nan", "1", "2", "3"]
    big = agent.inspect("big")["table"]
    assert len(big["rows"]) == 200 and len(big["columns"]) == 50 and big["total_rows"] == 500
    assert "Error" in agent.inspect("nope")["error"]
    assert agent.inspect("c = a.sum()")["summary"] == "done"
    assert agent.inspect("c")["type"] == "numpy.float32"
    agent.send(cmd="continue")
    agent.finish()


def test_inspecting_containers_and_objects(script):
    path = script("class Box:\n"
                  "    def __init__(self):\n"
                  "        self.size = 3\n"
                  "data = {'box': Box(), 'items': [1, 2, 3]}\n"
                  "pass\n")
    agent = Agent(path, breakpoints=[5])
    agent.next_event("paused")
    children = {c["name"]: c["expr"] for c in agent.inspect("data")["children"]}
    assert children == {"'box'": "data['box']", "'items'": "data['items']"}
    box = agent.inspect("data['box']")["children"]
    assert box == [{"name": "size", "type": "int", "summary": "3", "expr": "data['box'].size"}]
    agent.send(cmd="continue")
    agent.finish()


@pytest.mark.skipif(not has_modules(DATA_PYTHON, "torch", "pandas"),
                    reason="set RV_TEST_PYTHON to a Python with torch and pandas")
def test_inspecting_tensors_and_frames(script):
    path = script("import torch, pandas as pd\n"
                  "t = torch.arange(6, dtype=torch.bfloat16).reshape(2, 3).requires_grad_()\n"
                  "net = torch.nn.Sequential(torch.nn.Linear(4, 2))\n"
                  "df = pd.DataFrame({'x': [1.0, None, 3.0], 'name': ['a', 'b', 'c']})\n"
                  "pass\n")
    agent = Agent(path, breakpoints=[5], python=DATA_PYTHON)
    summaries = {v["name"]: v["summary"] for v in agent.next_event("paused")["vars"]}
    assert summaries["t"] == "bfloat16 [2, 3] cpu grad"
    assert summaries["net"] == "Sequential, 10 parameters"
    assert summaries["df"] == "3 rows × 2 columns"
    tensor = agent.inspect("t")
    assert info(tensor)["max"] == "5" and tensor["table"]["rows"][1] == ["3", "4", "5"]
    layers = agent.inspect("net")["children"]
    assert layers[0]["expr"] == "net.get_submodule('0')" and "Linear" in layers[0]["summary"]
    frame = agent.inspect("df")
    assert info(frame)["missing values"] == "1"
    assert frame["table"]["rows"][0] == ["1", "a"]
    column = agent.inspect(frame["children"][0]["expr"])
    assert info(column)["mean"] == "2" and column["table"]["rows"][1] == ["nan"]
    agent.send(cmd="continue")
    agent.finish()


def test_run_command_quotes_the_folder():
    command = run_command("/srv/my files/train.py", "~/venv/bin/python")
    assert command.startswith("cd '/srv/my files' && exec ~/venv/bin/python -u -c '")


# Through the real window and sshd -------------------------------------------
@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    harness.window.run_panel.interpreter.setText(sys.executable)
    yield harness
    harness.window.run_panel.stop()
    harness.close()


def open_in_editor(ui, path):
    window = ui.window
    ui.select(path.name)
    assert pump(lambda: window.preview.entry is not None and window.preview.entry.path == str(path))
    window.preview.edit()
    editor = window.preview.editor
    assert pump(lambda: not editor.edit.isReadOnly())
    return editor


def test_debugging_from_the_editor(ui, server):
    path = server.root / "train.py"
    path.write_text("total = 0\n"
                    "for step in range(3):\n"
                    "    total += step\n"
                    "print('total', total)\n")
    ui.window.list_model.reload()
    editor = open_in_editor(ui, path)
    assert editor.debug_button.isVisible()
    editor.edit.toggle_breakpoint(3)
    assert ui.window.preview.editor.edit.breakpoints() == {3}

    editor.debug_button.click()
    panel = ui.window.run_panel
    assert panel.isVisible()
    assert pump(lambda: panel.paused, 15), panel.output.toPlainText()
    assert editor.edit.execution_line == 3
    names = [panel.variables.topLevelItem(i).text(0) for i in range(panel.variables.topLevelItemCount())]
    assert names == ["total", "step"]

    panel.inspect("step")
    assert pump(lambda: panel.inspector.expression.text() == "step"
                and "int" in panel.inspector.info.text())

    editor.edit.toggle_breakpoint(3)  # off again, while paused
    panel.step("continue")
    assert pump(lambda: not panel.running, 15), panel.output.toPlainText()
    assert "total 3" in panel.output.toPlainText()
    assert "exit code 0" in panel.status.text()
    assert editor.edit.execution_line == 0


def test_run_saves_first_and_shows_output_and_crash(ui, server):
    path = server.root / "job.py"
    path.write_text("print('old')\n")
    ui.window.list_model.reload()
    editor = open_in_editor(ui, path)
    editor.edit.selectAll()  # typed, not setPlainText: that would leave it unmodified
    editor.edit.insertPlainText("for i in range(3):\n    print(f'\\rprogress {i}', end='')\n"
                             "print()\nraise RuntimeError('bad shape')\n")
    editor.run_button.click()
    panel = ui.window.run_panel
    assert pump(lambda: panel.paused, 15), panel.output.toPlainText()
    assert path.read_text().startswith("for i in range(3)")  # saved before running
    output = panel.output.toPlainText()
    assert "progress 2\n" in output and "progress 0" not in output  # \r rewrote the line
    assert "RuntimeError: bad shape" in output and "Crashed" in panel.status.text()
    assert not panel.over_button.isEnabled() and panel.continue_button.isEnabled()
    panel.stop()
    assert pump(lambda: not panel.running, 10)
    assert panel.status.text() == "Stopped"


def test_console_runs_code_in_the_paused_frame(script):
    path = script("def work(values):\n"
                  "    total = sum(values)\n"
                  "    return total\n"
                  "result = work([1, 2, 3])\n"
                  "print('result', result)\n")
    agent = Agent(path, breakpoints=[3])
    agent.next_event("paused")

    def run(code):
        agent.send(cmd="exec", code=code)
        event = agent.next_event("console")
        variables = agent.next_event("vars")["vars"]
        return event, {v["name"]: v["summary"] for v in variables}

    event, _ = run("print('hi')\nsquares = [v * v for v in values]\nsquares")
    assert event["output"] == "hi\n" and event["result"] == "[1, 4, 9]"
    assert event["name"] == "_1" and event["inspectable"]
    event, _ = run("sum(squares) + _1[0]")  # console names persist; _1 is the last result
    assert event["result"] == "15"
    assert agent.inspect("_1")["summary"].startswith("len 3")
    event, _ = run("for v in values:\n    if v > 1:\n        print(v)")
    assert event["output"] == "2\n3\n" and event["result"] is None
    event, _ = run("1 / 0")
    assert "ZeroDivisionError" in event["error"] and "<rv-agent>" not in event["error"]
    assert 'File "<console>", line 1' in event["error"]
    event, _ = run("def f(:")
    assert "SyntaxError" in event["error"]
    event, names = run("total = 100")
    assert event["result"] is None and "_1" not in names and "squares" not in names
    if sys.version_info >= (3, 13):  # writing a function's locals sticks from 3.13
        assert names["total"] == "100"
    agent.send(cmd="continue")
    agent.finish()
    if sys.version_info >= (3, 13):
        assert "result 100" in agent.stderr.decode()


def test_console_at_module_level_defines_names_for_the_script(script):
    path = script("x = 2\n"
                  "pass\n"
                  "print('y is', y)\n")
    agent = Agent(path, breakpoints=[2])
    agent.next_event("paused")
    agent.send(cmd="exec", code="y = x * 21")
    assert agent.next_event("console")["error"] is None
    agent.send(cmd="continue")
    agent.finish()
    assert "y is 42" in agent.stderr.decode()


def test_console_input_knows_when_a_block_is_complete():
    from app.run_panel import is_complete
    assert is_complete("x = 1")
    assert not is_complete("for i in range(3):")
    assert not is_complete("for i in range(3):\n    print(i)")
    assert is_complete("for i in range(3):\n    print(i)\n")
    assert is_complete("def f(:")  # an error: run it, to show it
    assert is_complete("a = 1\nb = 2")  # pasted lines run as they are


def test_console_tab_in_the_window(ui, server):
    path = server.root / "loop.py"
    path.write_text("import math\nvalues = [1.0, 4.0]\nroots = [math.sqrt(v) for v in values]\n"
                    "print(roots)\n")
    ui.window.list_model.reload()
    editor = open_in_editor(ui, path)
    editor.edit.toggle_breakpoint(4)
    editor.debug_button.click()
    panel = ui.window.run_panel
    assert pump(lambda: panel.paused, 15), panel.output.toPlainText()
    console = panel.console
    assert not console.input.isReadOnly()
    panel.tabs.setCurrentWidget(console)
    from PySide6.QtTest import QTest
    QTest.keyClicks(console.input, "for r in roots:")
    QTest.keyClick(console.input, Qt.Key.Key_Return)
    assert console.input.toPlainText() == "for r in roots:\n    "  # waits for the body
    QTest.keyClicks(console.input, "print(r * 10)")
    QTest.keyClick(console.input, Qt.Key.Key_Return)
    QTest.keyClick(console.input, Qt.Key.Key_Return)  # blank line ends the block
    assert pump(lambda: "20.0" in console.transcript.toPlainText(), 10)
    QTest.keyClicks(console.input, "sum(roots)")
    QTest.keyClick(console.input, Qt.Key.Key_Return)
    assert pump(lambda: "_1 = 3.0" in console.transcript.toPlainText(), 10), \
        console.transcript.toPlainText()
    QTest.keyClick(console.input, Qt.Key.Key_Up)
    assert console.input.toPlainText() == "sum(roots)"
    console.input.clear()
    QTest.keyClicks(console.input, "roots")
    QTest.keyClick(console.input, Qt.Key.Key_Return)
    assert pump(lambda: "Inspect _2" in console.transcript.toPlainText(), 10)
    console.inspect.emit("_2")  # what clicking the link does
    assert pump(lambda: panel.inspector.expression.text() == "_2"
                and panel.inspector.children.topLevelItemCount() == 2)
    panel.step("continue")
    assert pump(lambda: not panel.running, 15)
    assert console.input.isReadOnly()

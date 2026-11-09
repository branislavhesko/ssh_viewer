"""Runs on the server: executes one script, optionally under a debugger.

Sent over stdin, so nothing is installed remotely. Needs Python 3.8+ and the
standard library only; torch, numpy and pandas are used if the script loaded
them.

stdin, one JSON value per line: this source, then the start message
``{"path", "debug", "breakpoints": {path: [lines]}}``, then commands.
stdout carries only events, as JSON lines; the script's own stdout and
stderr, and its children's, go to stderr. EOF on stdin ends everything.
"""
import ast
import bdb
import builtins
import io
import json
import linecache
import os
import queue
import reprlib
import signal
import sys
import threading
import traceback
import types

ROWS, COLUMNS, CHILDREN, CELL = 200, 50, 300, 120
AGENT_FILE = "<rv-agent>"

_events = None
_events_lock = threading.Lock()


def send(event):
    with _events_lock:
        _events.write(json.dumps(event, default=str) + "\n")
        _events.flush()


def hard_exit(status=0, kill_group=False):
    """End now. On Stop, ``kill_group`` takes the script's children too: they
    share our process group. The agent dies with them, by the same signal."""
    try:
        sys.stderr.flush()
    except Exception:
        pass
    if kill_group:
        try:
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
        except Exception:
            pass
    os._exit(status)


# Describing values ----------------------------------------------------------
_repr = reprlib.Repr()
_repr.maxstring = _repr.maxother = 200
_repr.maxlist = _repr.maxtuple = _repr.maxdict = _repr.maxset = 20
_repr.maxlevel = 3


def safe_repr(value, limit=200):
    try:
        text = _repr.repr(value)
    except Exception as exc:
        text = "<repr failed: %s>" % type(exc).__name__
    return text if len(text) <= limit else text[:limit - 1] + "…"


def type_name(value):
    kind = type(value)
    module = kind.__module__
    if module == "builtins":
        return kind.__qualname__
    return "%s.%s" % (module.split(".")[0], kind.__qualname__)


def _module(name):
    return sys.modules.get(name)


def kind_of(value):
    torch, numpy, pandas = _module("torch"), _module("numpy"), _module("pandas")
    if torch is not None:
        if isinstance(value, torch.Tensor):
            return "tensor"
        if isinstance(value, torch.nn.Module):
            return "module"
    if numpy is not None and isinstance(value, numpy.ndarray):
        return "array"
    if pandas is not None:
        if isinstance(value, pandas.DataFrame):
            return "frame"
        if isinstance(value, pandas.Series):
            return "series"
    return None


def short_dtype(dtype):
    return str(dtype).replace("torch.", "")


def shape_text(shape):
    return "[" + ", ".join(str(n) for n in shape) + "]"


def summary(value):
    kind = kind_of(value)
    try:
        if kind == "tensor":
            text = "%s %s %s" % (short_dtype(value.dtype), shape_text(value.shape), value.device)
            return text + (" grad" if value.requires_grad else "")
        if kind == "array":
            return "%s %s" % (value.dtype, shape_text(value.shape))
        if kind == "frame":
            return "%d rows × %d columns" % value.shape
        if kind == "series":
            return "%s, %d values%s" % (value.dtype, len(value),
                                        "" if value.name is None else ", " + safe_repr(value.name, 60))
        if kind == "module":
            count = sum(p.numel() for p in value.parameters())
            return "%s, %s parameters" % (type(value).__name__, format(count, ","))
        if isinstance(value, (list, tuple, dict, set, frozenset)):
            return "len %d: %s" % (len(value), safe_repr(value, 160))
    except Exception:
        pass
    return safe_repr(value)


def hidden(name, value):
    return (name.startswith("__") or (name[:1] == "_" and name[1:].isdigit()) or isinstance(value, (types.ModuleType, types.FunctionType,
                                                        types.BuiltinFunctionType, type)))


def variables(frame):
    found = []
    for name, value in list(frame.f_locals.items()):
        if hidden(name, value):
            continue
        found.append({"name": name, "type": type_name(value), "summary": summary(value),
                      "expr": name})
        if len(found) >= 1000:
            break
    return found


def cell(value):
    if isinstance(value, float):
        return "%.6g" % value
    if isinstance(value, complex):
        return "%.4g%+.4gj" % (value.real, value.imag)
    text = str(value)
    return text if len(text) <= CELL else text[:CELL - 1] + "…"


def number(value):
    return "%.6g" % value if isinstance(value, float) else str(value)


def grid(rows_2d, total_rows, total_columns):
    """Values as a table, a list of lists or a flat list, already sliced."""
    if rows_2d and not isinstance(rows_2d[0], list):
        return {"columns": ["value"], "index": [str(i) for i in range(len(rows_2d))],
                "rows": [[cell(v)] for v in rows_2d], "total_rows": total_rows,
                "total_columns": 1}
    width = len(rows_2d[0]) if rows_2d else 0
    return {"columns": [str(j) for j in range(width)],
            "index": [str(i) for i in range(len(rows_2d))],
            "rows": [[cell(v) for v in row] for row in rows_2d],
            "total_rows": total_rows, "total_columns": total_columns}


def slice_to_grid(value):
    """Index leading dimensions at 0 down to 2, slice, return (sliced, note)."""
    lead = []
    while len(value.shape) > 2:
        value = value[0]
        lead.append("0")
    note = "showing [%s, :, :]" % ", ".join(lead) if lead else ""
    if len(value.shape) == 1:
        return value[:ROWS], value.shape[0], 1, note
    return value[:ROWS, :COLUMNS], value.shape[0], value.shape[1], note


def tensor_details(value, details):
    torch = _module("torch")
    tensor = value.detach()
    info = details["info"]
    info += [["shape", shape_text(tensor.shape)], ["dtype", short_dtype(tensor.dtype)],
             ["device", str(tensor.device)], ["requires_grad", str(value.requires_grad)],
             ["elements", format(tensor.numel(), ",")],
             ["memory", "%.1f MB" % (tensor.numel() * tensor.element_size() / 2 ** 20)]]
    if tensor.numel() and not tensor.is_complex():
        floats = tensor.float() if tensor.dtype != torch.float64 else tensor
        nan = int(torch.isnan(floats).sum())
        inf = int(torch.isinf(floats).sum())
        finite = floats[torch.isfinite(floats)] if nan or inf else floats
        if finite.numel():
            info += [["min", number(finite.min().item())], ["max", number(finite.max().item())],
                     ["mean", number(finite.mean().item())]]
            if finite.numel() > 1:
                info.append(["std", number(finite.std().item())])
        info += [["nan", str(nan)], ["inf", str(inf)]]
    if tensor.dim() == 0:
        info.append(["value", cell(tensor.item())])
        return
    sliced, rows, columns, note = slice_to_grid(tensor)
    if sliced.dtype in (torch.bfloat16, torch.float16) or "float8" in str(sliced.dtype):
        sliced = sliced.float()
    details["table"] = grid(sliced.cpu().tolist(), rows, columns)
    details["note"] = note


def array_details(value, details):
    numpy = _module("numpy")
    info = details["info"]
    info += [["shape", shape_text(value.shape)], ["dtype", str(value.dtype)],
             ["elements", format(value.size, ",")], ["memory", "%.1f MB" % (value.nbytes / 2 ** 20)]]
    if value.size and value.dtype.kind in "biuf":
        data = value.astype(numpy.float64) if value.dtype.kind == "b" else value
        if value.dtype.kind == "f":
            nan = int(numpy.isnan(data).sum())
            inf = int(numpy.isinf(data).sum())
            finite = data[numpy.isfinite(data)] if nan or inf else data
        else:
            nan = inf = 0
            finite = data
        if finite.size:
            info += [["min", number(finite.min().item())], ["max", number(finite.max().item())],
                     ["mean", number(float(finite.mean(dtype=numpy.float64)))]]
            if finite.size > 1:
                info.append(["std", number(float(finite.std(dtype=numpy.float64)))])
        info += [["nan", str(nan)], ["inf", str(inf)]]
    if value.ndim == 0:
        info.append(["value", cell(value.item())])
        return
    sliced, rows, columns, note = slice_to_grid(value)
    details["table"] = grid(sliced.tolist(), rows, columns)
    details["note"] = note


def frame_details(value, expr, details):
    rows, columns = value.shape
    details["info"] += [
        ["shape", "%d rows × %d columns" % (rows, columns)],
        ["memory", "%.1f MB" % (value.memory_usage(index=True, deep=False).sum() / 2 ** 20)],
        ["missing values", format(int(value.isna().sum().sum()), ",")],
        ["index", type(value.index).__name__]]
    head = value.iloc[:ROWS, :COLUMNS]
    details["table"] = {"columns": [cell(c) for c in head.columns],
                        "index": [cell(i) for i in head.index],
                        "rows": [[cell(v) for v in row] for row in head.itertuples(index=False)],
                        "total_rows": rows, "total_columns": columns}
    missing = value.isna().sum()
    details["children"] = [
        {"name": cell(name), "type": str(value.dtypes.iloc[i]),
         "summary": "%s missing" % format(int(missing.iloc[i]), ","),
         "expr": "%s[%r]" % (expr, name) if expr and isinstance(name, (str, int)) else None}
        for i, name in enumerate(list(value.columns)[:CHILDREN])]


def series_details(value, details):
    details["info"] += [["dtype", str(value.dtype)], ["length", format(len(value), ",")],
                        ["missing values", format(int(value.isna().sum()), ",")],
                        ["name", safe_repr(value.name, 80)]]
    if len(value) and value.dtype.kind in "biuf":
        clean = value.dropna()
        if len(clean):
            details["info"] += [["min", number(clean.min().item())], ["max", number(clean.max().item())],
                                ["mean", number(float(clean.mean()))]]
            if len(clean) > 1:
                details["info"].append(["std", number(float(clean.std()))])
    head = value.iloc[:ROWS]
    details["table"] = {"columns": [cell(value.name) if value.name is not None else "value"],
                        "index": [cell(i) for i in head.index],
                        "rows": [[cell(v)] for v in head.tolist()],
                        "total_rows": len(value), "total_columns": 1}


def child(name, value, expr):
    return {"name": name, "type": type_name(value), "summary": summary(value), "expr": expr}


def container_children(value, expr):
    children = []
    if isinstance(value, dict):
        for key in list(value)[:CHILDREN]:
            keyed = expr and isinstance(key, (str, int, float, bool, tuple, type(None)))
            children.append(child(safe_repr(key, 80), value[key],
                                  "%s[%r]" % (expr, key) if keyed else None))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value[:CHILDREN]):
            children.append(child(str(index), item, "%s[%d]" % (expr, index) if expr else None))
    elif isinstance(value, (set, frozenset)):
        for item in list(value)[:CHILDREN]:
            children.append(child("·", item, None))
    elif kind_of(value) == "module":
        for name, module in list(value.named_children())[:CHILDREN]:
            children.append(child(name, module, "%s.%s" % (expr, name) if expr and name.isidentifier()
                                  else ("%s.get_submodule(%r)" % (expr, name) if expr else None)))
        for name, parameter in list(value.named_parameters(recurse=False))[:CHILDREN]:
            children.append(child(name, parameter, "%s.%s" % (expr, name) if expr else None))
    else:
        try:
            attributes = vars(value)
        except TypeError:
            attributes = {}
        for name, item in list(attributes.items())[:CHILDREN]:
            if not name.startswith("__") and not callable(item):
                children.append(child(name, item, "%s.%s" % (expr, name)
                                      if expr and name.isidentifier() else None))
    return children


def details(value, expr):
    result = {"expr": expr, "type": type_name(value), "summary": summary(value), "info": [],
              "table": None, "note": "", "children": [], "repr": ""}
    kind = kind_of(value)
    try:
        if kind == "tensor":
            tensor_details(value, result)
        elif kind == "array":
            array_details(value, result)
        elif kind == "frame":
            frame_details(value, expr, result)
        elif kind == "series":
            series_details(value, result)
        else:
            result["children"] = container_children(value, expr)
            if not result["children"]:
                text = repr(value)
                result["repr"] = text if len(text) < 20000 else text[:20000] + "…"
    except Exception:
        result["error"] = traceback.format_exc(limit=3)
    return result


# Debugger ---------------------------------------------------------------------
class Debugger(bdb.Bdb):
    def __init__(self):
        super().__init__()
        self.commands = queue.Queue()
        self.frames = []
        self.selected = 0
        self.lock = threading.Lock()
        # Names made in the console that aren't the program's, and results
        # as _1, _2... They outlive steps, as a REPL's would.
        self.scratch = {}
        self.results = 0

    # Breakpoints arrive from the reader thread, while the script runs.
    def replace_breakpoints(self, path, lines):
        with self.lock:
            self.clear_all_file_breaks(path)
            for line in lines:
                linecache.checkcache(path)
                self.set_break(path, int(line))

    def run_code(self, code, globals_):
        self.reset()
        # Stop frame: this one, which runs no lines of the script. So the script
        # runs on, and stops at breakpoints only.
        self._set_stopinfo(sys._getframe(), None, -1)
        sys.settrace(self.trace_dispatch)
        try:
            exec(code, globals_)
        except bdb.BdbQuit:
            pass
        finally:
            self.quitting = True
            sys.settrace(None)

    def set_continue(self):
        # Bdb stops tracing when there are no breakpoints; one may come later.
        self._set_stopinfo(self.botframe, None, -1)

    def user_line(self, frame):
        self.interaction(frame, None)

    def post_mortem(self, traceback_):
        self.reset()
        self.interaction(None, traceback_, post_mortem=True)

    def interaction(self, frame, traceback_, post_mortem=False):
        stack, _ = self.get_stack(frame, traceback_)
        self.frames = [(f, line) for f, line in stack
                       if f.f_code.co_filename not in (AGENT_FILE, bdb.__file__)
                       and f.f_code.co_filename != "<string>"]
        if not self.frames:
            self.set_continue()
            return
        self.selected = len(self.frames) - 1
        top = self.frames[-1][0]
        event = {"ev": "paused", "stack": [
            {"name": f.f_code.co_name, "file": os.path.abspath(f.f_code.co_filename), "line": line}
            for f, line in self.frames], "selected": self.selected,
            "vars": variables(top), "post_mortem": post_mortem}
        if post_mortem:
            event["exception"] = "".join(traceback.format_exception_only(
                *sys.exc_info()[:2])).strip() if sys.exc_info()[0] else ""
        send(event)
        while True:
            command = self.commands.get()
            name = command.get("cmd")
            if name in ("continue", "step", "next", "return"):
                if post_mortem or name == "continue":
                    self.set_continue()
                elif name == "step":
                    self.set_step()
                elif name == "next":
                    self.set_next(top)
                else:
                    self.set_return(top)
                send({"ev": "running"})
                return
            if name == "frame":
                self.selected = max(0, min(int(command["index"]), len(self.frames) - 1))
                frame_ = self.frames[self.selected][0]
                send({"ev": "vars", "selected": self.selected, "vars": variables(frame_)})
            elif name == "inspect":
                send(self.evaluate(command.get("expr", ""), command.get("id")))
            elif name == "exec":
                self.console(command.get("code", ""), command.get("id"))

    # Code typed by the user -----------------------------------------------
    def namespace(self, frame):
        """One dict, so comprehensions and functions defined in the console see
        the frame's locals on every Python version."""
        names = dict(frame.f_globals)
        names.update(self.scratch)
        names.update(frame.f_locals)
        return names

    def write_back(self, frame, names, before):
        """Changed names go to the program if they are its own, else to scratch."""
        module = frame.f_locals is frame.f_globals
        for name, value in names.items():
            if name in before and before[name] is value:
                continue
            if module and name in frame.f_globals:
                frame.f_globals[name] = value
            elif name in frame.f_locals:
                try:
                    frame.f_locals[name] = value  # sticks from Python 3.13
                except Exception:
                    self.scratch[name] = value
            elif module and not name.startswith("_"):
                frame.f_globals[name] = value  # new top-level name, as in the script
            else:
                self.scratch[name] = value

    def run_user_code(self, source, filename, frame):
        """Run statements, then evaluate a final expression. Returns
        (value, has_value)."""
        names = self.namespace(frame)
        before = dict(names)
        tree = ast.parse(source, filename, "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        try:
            if tree.body:
                exec(compile(tree, filename, "exec"), names)
            if last is not None:
                return eval(compile(last, filename, "eval"), names), True
            return None, False
        finally:
            self.write_back(frame, names, before)

    def evaluate(self, expr, ident):
        frame = self.frames[self.selected][0]
        try:
            value, has_value = self.run_user_code(expr, "<inspect>", frame)
            if has_value:
                result = details(value, expr)
            else:
                result = {"expr": expr, "type": "", "summary": "done", "info": [], "table": None,
                          "note": "", "children": [], "repr": ""}
        except Exception:
            lines = traceback.format_exception_only(*sys.exc_info()[:2])
            result = {"expr": expr, "error": "".join(lines).strip()}
        result.update(ev="inspect", id=ident)
        return result

    def console(self, source, ident):
        """Like the Python REPL: printed output, then the value of a final
        expression, kept as _N for the inspector."""
        frame = self.frames[self.selected][0]
        captured = io.StringIO()
        event = {"ev": "console", "id": ident, "output": "", "result": None, "name": None,
                 "inspectable": False, "error": None}
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = captured
        try:
            value, has_value = self.run_user_code(source, "<console>", frame)
        except BaseException:
            kind, error, trace = sys.exc_info()
            while trace is not None and trace.tb_frame.f_code.co_filename != "<console>":
                trace = trace.tb_next
            event["error"] = "".join(traceback.format_exception(kind, error, trace))
        else:
            if has_value and value is not None:
                self.results += 1
                name = "_%d" % self.results
                self.scratch[name] = self.scratch["_"] = value
                event.update(result=console_repr(value), name=name,
                             inspectable=kind_of(value) is not None or bool(
                                 container_children(value, name)))
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        event["output"] = captured.getvalue()
        send(event)
        send({"ev": "vars", "selected": self.selected, "vars": variables(frame)})


def console_repr(value, limit=20000):
    """The REPL's repr, without building megabytes for a huge list."""
    if isinstance(value, (list, tuple, dict, set, frozenset)) and len(value) > 1000:
        return safe_repr(value, limit)
    try:
        text = repr(value)
    except Exception as exc:
        return "<repr failed: %s: %s>" % (type(exc).__name__, exc)
    return text if len(text) <= limit else text[:limit] + "…"


def read_commands(source, debugger):
    for line in source:
        try:
            command = json.loads(line)
        except ValueError:
            continue
        if command.get("cmd") == "stop":
            break
        if command.get("cmd") == "break":
            if debugger is not None:
                debugger.replace_breakpoints(command["path"], command["lines"])
            continue
        if debugger is not None:
            debugger.commands.put(command)
    hard_exit(137, kill_group=True)


def main():
    global _events
    commands = sys.stdin
    start = json.loads(commands.readline())
    path = os.path.abspath(start["path"])
    # Events get the real stdout; everything the script prints goes to stderr.
    _events = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdin = open(os.devnull)
    try:
        os.setsid()  # our own process group, so Stop takes children too
    except OSError:
        pass

    debugger = Debugger()
    for file, lines in (start.get("breakpoints") or {}).items():
        debugger.replace_breakpoints(os.path.abspath(file), lines)
    threading.Thread(target=read_commands, args=(commands, debugger), daemon=True).start()

    with open(path, "rb") as handle:
        source = handle.read()
    main_module = sys.modules["__main__"]
    main_module.__dict__.clear()
    main_module.__dict__.update({"__name__": "__main__", "__file__": path,
                                 "__builtins__": builtins, "__spec__": None})
    sys.argv = [path] + list(start.get("args") or [])
    sys.path[0] = os.path.dirname(path)
    send({"ev": "started", "python": sys.version.split()[0], "pid": os.getpid(),
          "executable": sys.executable})
    status = 0
    try:
        code = compile(source, path, "exec")
        if start.get("debug"):
            debugger.run_code(code, main_module.__dict__)
        else:
            exec(code, main_module.__dict__)
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        status = 1
        kind, error, trace = sys.exc_info()
        # Start the traceback at the script: our own frames are noise.
        while trace.tb_next is not None and trace.tb_frame.f_code.co_filename in (AGENT_FILE,
                                                                                  bdb.__file__):
            trace = trace.tb_next
        sys.stderr.write("".join(traceback.format_exception(kind, error, trace)))
        sys.stderr.flush()
        send({"ev": "exception", "text": "".join(traceback.format_exception_only(kind, error)).strip()})
        debugger.post_mortem(trace)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    send({"ev": "finished", "status": status})
    hard_exit(status)


if __name__ == "rv_agent":  # the name the bootstrap gives it on the server
    main()

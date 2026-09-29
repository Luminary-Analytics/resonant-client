"""Every program Lumi starts comes from lumi/executables.py (or is Python itself).

On Windows a program started by a bare name (``git``) is looked for in the
working folder first, and a repository is often that folder. This scan reads
every module in ``lumi/`` and follows each launch's program back to where it
comes from. It must be one of:

* a resolver's answer (``program``, ``find_program``, ``system_program``,
  ``configured_program``, ``project_tool``, ``safe_git.argv`` and the few
  helpers built on them, ``RESOLVERS``),
* ``sys.executable``,
* an absolute path written in the code,
* a parameter of a function that starts it (a wrapper), whose callers are
  checked in turn, wherever they are.

Anything else fails: a name or relative path written in the code (module
constants, f-strings, list ``append``, ``*prefix`` too), a program the scan
can't follow, a shell (``shell=True``, ``os.system``, ``loop.subprocess_shell``),
``shutil.which`` (which searched the working folder before Python 3.12),
``os.startfile`` (also through ``getattr``), ctypes' ``ShellExecuteW`` and
``webbrowser``. ``BY_DESIGN`` lists, with how many times each is expected,
the launches that are allowed on purpose: a command the model or the person
asked for runs in the project with their environment, and the rest were
checked by hand.
"""

from __future__ import annotations

import ast
import collections
import functools
import re
from dataclasses import dataclass
from pathlib import Path

LUMI = Path(__file__).resolve().parents[1] / "lumi"

# Calls that start a process, and where their program is among the arguments.
LAUNCHERS = {
    "subprocess.run": 0, "subprocess.Popen": 0, "subprocess.call": 0, "subprocess.check_call": 0,
    "subprocess.check_output": 0, "asyncio.create_subprocess_exec": 0, "pty.spawn": 0,
    **{f"os.{name}": 0 for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
                                     "execlpe", "posix_spawn", "posix_spawnp")},
    **{f"os.{name}": 1 for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
                                     "spawnlpe")},
}
# Methods that start a process on whatever object has them (an event loop).
LAUNCHING_METHODS = {"subprocess_exec": 1}
SHELL_METHODS = {"subprocess_shell"}
# Never used outside the places listed in BY_DESIGN.
FORBIDDEN = {
    "shutil.which": "use lumi.executables.find_program: which() searches the working folder before Python 3.12",
    "os.system": "a shell", "os.popen": "a shell", "subprocess.getoutput": "a shell",
    "subprocess.getstatusoutput": "a shell", "asyncio.create_subprocess_shell": "a shell",
    "os.startfile": "ShellExecute looks in the working folder: use lumi.executables.open_path or open_url",
    "webbrowser.open": "use lumi.executables.open_url", "webbrowser.open_new": "use lumi.executables.open_url",
    "webbrowser.open_new_tab": "use lumi.executables.open_url", "webbrowser.get": "use lumi.executables.open_url",
}
# ctypes calls into Windows that start programs or open files.
WINDOWS_LAUNCH_API = {"ShellExecuteW", "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA", "WinExec",
                      "CreateProcessW", "CreateProcessA", "CreateProcessAsUserW"}
# Functions whose answer is a program Lumi may start: an absolute path, or a
# list starting with one.
RESOLVERS = {
    # lumi/executables.py
    "program", "find_program", "system_program", "configured_program", "project_tool", "show_in_folder_command",
    # lumi/safe_git.py
    "argv",
    # built on them, each checked by hand
    "trusted_git_executable", "resolve_codex_cli_path", "resolve_claude_cli_path", "program_path",
    "_ripgrep_executable", "_build_grep_command", "_bundled_ripgrep", "_find_chrome", "command_for",
    "_preferred_harness_python", "_detect_bash", "_lumi_command", "_source_command",
}
# Functions that return their argument's program, perhaps behind another
# resolved program (the shell sandbox's): their argument is followed.
PASSTHROUGH = {"prepare_argv": 0, "list": 0, "tuple": 0}

# (module, function, rule): (how many, why). A command the model or the
# person asked for runs in the project by design, with their environment
# (secrets_store.child_env); the rest start programs the scan can't follow
# and were checked by hand.
BY_DESIGN: dict[tuple[str, str, str], tuple[int, str]] = {
    # Commands the model or the person asked for, in the project, as their own terminal would run them.
    ("lumi/engine/tools.py", "_run_subprocess_with_cancel", "shell"):
        (1, "the agent's shell runner: bash, check_run and the composer's ! commands"),
    ("lumi/engine/tools.py", "_exec_bash", "shell"):
        (1, "the agent's bash and check_run tools: a shell unless the shell sandbox wraps the command"),
    ("lumi/engine/tools.py", "_exec_bash", "unresolved"):
        (2, "the agent's command, as written or wrapped by the shell sandbox (prepare_shell)"),
    ("lumi/engine/jobs.py", "JobManager.start", "unresolved"):
        (1, "the agent's job_start command, from the project as its terminal would (project_command)"),
    ("lumi/engine/previews.py", "PreviewManager.start", "unresolved"):
        (1, "the agent's preview_start command, from the project as its terminal would (project_command)"),
    ("lumi/engine/hooks.py", "_run_command", "shell"): (1, "the person's own hook commands"),
    ("lumi/engine/hooks.py", "HookRunner.run_hooks", "unresolved"): (1, "the person's own hook command"),
    ("lumi/orchestration/acceptance_check.py", "BashRunner.run", "shell"):
        (1, "a plan's [bash] criteria, in cmd.exe when no bash is installed"),
    ("lumi/orchestration/acceptance_check.py", "BashRunner.run", "unresolved"):
        (1, "_bash_path: a bash a test gives; otherwise _detect_bash (find_program)"),
    ("lumi/model_evals.py", "_check", "shell"): (1, "the person's check command for a model comparison"),
    ("lumi/model_evals.py", "_check", "unresolved"):
        (1, "the person's check command, wrapped by the shell sandbox when it's on (prepare_shell)"),
    ("lumi/model_evals.py", "Runner._run", "unresolved"): (1, "the person's check command, through _one and _check"),
    ("lumi/harness/prompts.py", "HarnessPrompts.run_harness_generator_validation_probes", "shell"):
        (1, "the validation commands the model wrote, in sprint mode"),
    ("lumi/harness/prompts.py", "HarnessPrompts.run_harness_generator_validation_probes", "unresolved"):
        (3, "the validation commands the model wrote, in sprint mode"),
    ("lumi/engine/worktrees.py", "WorktreeManager.integrate", "shell"): (1, "validation commands a caller names"),
    # Opening things for the person.
    ("lumi/update_file.py", "install", "os.startfile"): (1, "the verified installer, by its full path"),
    ("lumi/engine/computer_use.py", "exec_open_application", "os.startfile"):
        (1, "an installed application, an allowed address or a document, checked by _windows_application"),
    ("lumi/executables.py", "open_url", "os.startfile"): (1, "an http(s) address: its protocol's handler"),
    ("lumi/executables.py", "open_path", "os.startfile"):
        (1, "an existing document or folder by its full path; never a file that would run (opens_as_program)"),
    # Programs found by a resolver and kept in an attribute or behind a helper, checked by hand.
    ("lumi/backends.py", "CodexCliBackend.health", "unresolved"):
        (1, "cli_path: resolve_codex_cli_path, or the full path the person configured"),
    ("lumi/backends.py", "CodexCliBackend.stream", "unresolved"): (1, "_command() starts with cli_path, as above"),
    ("lumi/backends.py", "ClaudeCodeCliBackend.health", "unresolved"):
        (1, "cli_path: resolve_claude_cli_path, or the full path the person configured"),
    ("lumi/backends.py", "ClaudeCodeCliBackend.stream", "unresolved"): (1, "_command() starts with cli_path, as above"),
    ("lumi/engine/lsp.py", "LanguageServer._start", "unresolved"):
        (1, "the server lsp.choose found (program_source), wrapped by the shell sandbox when it's on"),
    ("lumi/engine/swarming/git_boundary.py", "git_bytes", "unresolved"):
        (1, "Team integration's Git: trusted_git_executable, outside the project and its worktrees"),
    ("lumi/engine/swarming/effect_child.py", "main", "unresolved"):
        (1, "a Team effect: the argv its host resolved and sent over the private channel"),
    ("lumi/engine/swarming/process_worker.py", "ManagedWorkerProcess._spawn", "unresolved"):
        (1, "a Team worker: this Python with Lumi (_source_command), or the command a test gives"),
    ("lumi/schedules.py", "start", "unresolved"): (1, "command(): this installation's Lumi (sys.executable)"),
}

_ABSOLUTE = re.compile(r"^(/|[A-Za-z]:[\\/]|\\\\)")
_MODULES = {"subprocess", "os", "shutil", "webbrowser", "asyncio", "pty", "functools", "sys"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} in {self.function or '<module>'}: {self.rule}: {self.detail}"


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Names imported from the modules of interest, as ``module`` or ``module.attribute``."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _MODULES:
                    found[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in _MODULES:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return found


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str:
    """``subprocess.run`` for a reference to it; a helper's plain name otherwise."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in aliases and "." not in aliases[node.value.id]:
            return f"{aliases[node.value.id]}.{node.attr}"
        return node.attr
    return ""


def _own_nodes(body: list[ast.AST]):
    """The nodes of a body, without descending into nested functions and classes."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(child)


class _Scope:
    """What each name in a function (or the module) may hold, as far as the code says."""

    def __init__(self, node: ast.AST | None, parent: "_Scope | None" = None):
        self.parent = parent
        # A class's self.<name> assignments, with the method scope each is in.
        self.attributes: dict[str, list[tuple[ast.AST, _Scope]]] = {}
        self.values: dict[str, list[ast.AST]] = collections.defaultdict(list)
        self.appended: dict[str, list[ast.AST]] = collections.defaultdict(list)
        self.parameters: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args]
            if arguments and arguments[0].arg in ("self", "cls"):
                arguments = arguments[1:]
            self.parameters = [argument.arg for argument in arguments]
            self.parameters += [argument.arg for argument in node.args.kwonlyargs]
            if node.args.vararg:
                self.parameters.append(node.args.vararg.arg)
        body = getattr(node, "body", [])
        appended: dict[str, list[tuple[int, ast.AST]]] = collections.defaultdict(list)
        for child in _own_nodes(body):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    self._bind(target, child.value)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.value is not None:
                self.values[child.target.id].append(child.value)
            elif isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
                appended[child.target.id].append((child.lineno, child.value))  # cmd += [...]
            elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(child.target, ast.Name):
                self.values[child.target.id].append(ast.Subscript(value=child.iter, slice=ast.Constant(0)))
            elif isinstance(child, ast.withitem) and isinstance(child.optional_vars, ast.Name):
                self.values[child.optional_vars.id].append(child.context_expr)
            elif (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                  and isinstance(child.func.value, ast.Name) and child.args
                  and child.func.attr in ("append", "extend", "insert")):
                if child.func.attr == "insert" and not (isinstance(child.args[0], ast.Constant)
                                                        and child.args[0].value == 0):
                    continue  # inserted after the program
                value = child.args[-1] if child.func.attr == "insert" else child.args[0]
                if child.func.attr == "insert":
                    appended[child.func.value.id].insert(0, (0, ast.List(elts=[value])))
                    continue
                if child.func.attr != "extend":
                    value = ast.List(elts=[value])
                appended[child.func.value.id].append((child.lineno, value))
        for name, items in appended.items():
            # What was added first starts the list, when it began empty.
            self.appended[name] = [value for _line, value in sorted(items, key=lambda item: item[0])]

    def _bind(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.values[target.id].append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            # a, b = resolver(...): the program is the first element.
            for index, element in enumerate(target.elts):
                if isinstance(element, ast.Name):
                    self.values[element.id].append(ast.Subscript(value=value, slice=ast.Constant(index)))

    def attribute(self, name: str) -> list[tuple[ast.AST, "_Scope"]]:
        """What ``self.<name>`` may hold, from the enclosing class's methods."""
        if name in self.attributes:
            return self.attributes[name]
        return self.parent.attribute(name) if self.parent is not None else []

    def lookup(self, name: str) -> tuple[list[ast.AST], list[ast.AST], bool]:
        """(values assigned, values appended, whether it's a parameter) for ``name``, here or outside."""
        if name in self.values or name in self.appended or name in self.parameters:
            return self.values.get(name, []), self.appended.get(name, []), name in self.parameters
        if self.parent is not None:
            return self.parent.lookup(name)
        return [], [], False


def _origins(node: ast.AST | None, scope: _Scope, aliases: dict[str, str], *, depth: int = 0,
             element: bool = False) -> list[str]:
    """Where the program ``node`` holds (``element``) or starts with (a command list) comes from.

    Each origin is "ok", "parameter:<name>", "bare:<text>" or "unknown:<code>".
    """
    if node is None or depth > 8:
        return ["unknown:too deep"]
    again = functools.partial(_origins, scope=scope, aliases=aliases, depth=depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value if element else (node.value.split() or [""])[0]
        return ["ok" if _ABSOLUTE.match(text) else f"bare:{text}"]
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant):
            return again(first, element=True)
        return again(first.value, element=True) if isinstance(first, ast.FormattedValue) else ["unknown:f-string"]
    if isinstance(node, (ast.List, ast.Tuple)):
        if not node.elts:
            return []
        first = node.elts[0]
        if isinstance(first, ast.Starred):
            return again(first.value, element=False)
        return again(first, element=True)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return again(node.left, element=element)
    if isinstance(node, ast.IfExp):
        return again(node.body, element=element) + again(node.orelse, element=element)
    if isinstance(node, ast.BoolOp):
        return [origin for value in node.values for origin in again(value, element=element)]
    if isinstance(node, ast.Attribute) and _qualified(node, aliases) == "sys.executable":
        return ["ok"]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        assigned = scope.attribute(node.attr)
        found = [origin for value, where in assigned
                 for origin in _origins(value, where, aliases, depth=depth + 1, element=element)]
        # A parameter of the method that set it isn't this function's to vouch for.
        return [f"unknown:self.{node.attr} ({origin[10:]})" if origin.startswith("parameter:") else origin
                for origin in found] or [f"unknown:self.{node.attr}"]
    if isinstance(node, ast.Subscript):
        inner = node.value
        if isinstance(inner, ast.Call) and _qualified(inner.func, aliases) in RESOLVERS:
            return ["ok"]
        if isinstance(inner, (ast.List, ast.Tuple)) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int) and node.slice.value < len(inner.elts):
            return again(inner.elts[node.slice.value], element=True)
        return again(inner, element=False)
    if isinstance(node, ast.Call):
        name = _qualified(node.func, aliases)
        if name in RESOLVERS:
            return ["ok"]
        if name in PASSTHROUGH and len(node.args) > PASSTHROUGH[name]:
            return again(node.args[PASSTHROUGH[name]], element=element)
        return [f"unknown:{name or ast.unparse(node.func)}()"]
    if isinstance(node, ast.Name):
        values, appended, parameter = scope.lookup(node.id)
        found = []
        for value in values:
            if isinstance(value, ast.List) and not value.elts:
                if appended and not element:  # cmd = []; cmd.append(program)
                    found += again(appended[0], element=False)
                continue
            found += again(value, element=element)
        if parameter:
            found.append(f"parameter:{node.id}")
        return found or [f"unknown:{node.id}"]
    return [f"unknown:{ast.unparse(node)[:60]}"]


def _class_scope(node: ast.ClassDef, parent: _Scope) -> _Scope:
    """A scope for a class's methods that knows what each ``self.<name>`` is set to."""
    scope = _Scope(None, parent=parent)
    attributes: dict[str, list[tuple[ast.AST, _Scope]]] = collections.defaultdict(list)
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method = _Scope(item, parent=scope)
            for child in _own_nodes(item.body):
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                                and target.value.id == "self" and child.value is not None:
                            attributes[target.attr].append((child.value, method))
    scope.attributes = dict(attributes)
    return scope


def _program(call: ast.Call, index: int) -> ast.AST | None:
    """The call's program argument at ``index`` (or ``args=``); a ``*args`` there is followed."""
    for position, argument in enumerate(call.args):
        if isinstance(argument, ast.Starred):
            return argument.value if position <= index else None
        if position == index:
            return argument
    return next((keyword.value for keyword in call.keywords if keyword.arg in ("args", "argv")), None)


def _module_name(path: str) -> tuple[str, bool]:
    """``lumi/engine/tools.py`` -> ("lumi.engine.tools", False); a package's __init__ -> (its name, True)."""
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


class _Names:
    """Which module each imported name in a module refers to."""

    def __init__(self, tree: ast.AST, module: str, is_package: bool, known: set[str]):
        self.module = module
        self.functions: dict[str, tuple[str, str]] = {}  # local name -> (module, attribute)
        self.modules: dict[str, str] = {}                # local name -> module
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = module.split(".") if is_package else module.split(".")[:-1]
                    base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                    source = ".".join([*base, *([node.module] if node.module else [])])
                else:
                    source = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    if f"{source}.{alias.name}" in known:
                        self.modules[local] = f"{source}.{alias.name}"
                    else:
                        self.functions[local] = (source, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name in known:
                        self.modules[alias.asname] = alias.name

    def target(self, func: ast.AST) -> tuple[str, str] | None:
        """(module, function) a call's function refers to, when the scan can tell."""
        if isinstance(func, ast.Name):
            return self.functions.get(func.id, (self.module, func.id))
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in self.modules:
                return self.modules[func.value.id], func.attr
            if func.value.id in ("self", "cls"):
                return self.module, func.attr
        return None


def _launch_index(call: ast.Call, aliases: dict[str, str], wrappers: dict[tuple[str, str], int],
                  partials: dict[str, int], names: _Names | None) -> tuple[str, int] | None:
    target = _qualified(call.func, aliases)
    if target in LAUNCHERS:
        return target, LAUNCHERS[target]
    if isinstance(call.func, ast.Attribute) and call.func.attr in LAUNCHING_METHODS:
        return call.func.attr, LAUNCHING_METHODS[call.func.attr]
    if isinstance(call.func, ast.Name) and target in partials:
        return target, partials[target]
    where = names.target(call.func) if names is not None else None
    if where is not None and where in wrappers:
        return target, wrappers[where]
    return None


def _partials(tree: ast.AST, aliases: dict[str, str]) -> dict[str, int]:
    """``run = functools.partial(subprocess.run, ...)``: ``run`` starts its first argument."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Call) and _qualified(node.value.func, aliases) == "functools.partial" \
                and node.value.args and _qualified(node.value.args[0], aliases) in LAUNCHERS:
            found[node.targets[0].id] = max(0, LAUNCHERS[_qualified(node.value.args[0], aliases)]
                                            - (len(node.value.args) - 1))
    return found


def _wrappers_in(tree: ast.AST, module: str, wrappers: dict[tuple[str, str], int],
                 names_in: "_Names | None") -> dict[tuple[str, str], int]:
    """This module's functions that start one of their parameters: ``def run(argv): Popen(argv)``,
    or pass it to a function that does (``wrappers``)."""
    aliases = _aliases(tree)
    partials = _partials(tree, aliases)
    found: dict[tuple[str, str], int] = {}

    def visit(node: ast.AST, parent: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, _class_scope(child, parent))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = _Scope(child, parent=parent)
                for call in _own_nodes(child.body):
                    if not isinstance(call, ast.Call):
                        continue
                    launch = _launch_index(call, aliases, wrappers, partials, names_in)
                    if launch is None:
                        continue
                    for origin in _origins(_program(call, launch[1]), scope, aliases):
                        if origin.startswith("parameter:") and origin[len("parameter:"):] in scope.parameters:
                            found[(module, child.name)] = scope.parameters.index(origin[len("parameter:"):])
                visit(child, scope)

    visit(tree, _Scope(tree))
    return found


def scan_source(source: str, path: str, wrappers: dict[tuple[str, str], int] | None = None,
                known: set[str] | None = None) -> list[Finding]:
    """Every launch in ``source`` that breaks the rule, before BY_DESIGN is applied.

    ``wrappers`` are the functions anywhere in Lumi that start a parameter,
    as (module, name); this module's own are added. ``known`` are Lumi's
    module names.
    """
    tree = ast.parse(source)
    module, is_package = _module_name(path)
    aliases = _aliases(tree)
    names_in = _Names(tree, module, is_package, known or set())
    wrappers = dict(wrappers or {})
    while True:  # this module's own wrappers, including wrappers of its wrappers
        more = {key: value for key, value in _wrappers_in(tree, module, wrappers, names_in).items()
                if key not in wrappers}
        if not more:
            break
        wrappers.update(more)
    partials = _partials(tree, aliases)
    findings: list[Finding] = []
    module_scope = _Scope(tree)

    def visit(node: ast.AST, names: list[str], scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = [*names, child.name]
                visit(child, qualified, _Scope(child, parent=scope))
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, [*names, child.name], _class_scope(child, scope))
                continue
            function = ".".join(names)
            if isinstance(child, (ast.Attribute, ast.Name)) and not isinstance(getattr(child, "ctx", None), ast.Store):
                reference = _qualified(child, aliases)
                if reference in FORBIDDEN and (isinstance(child, ast.Attribute) or child.id in aliases):
                    rule = "shell" if FORBIDDEN[reference] == "a shell" else reference
                    findings.append(Finding(path, child.lineno, function, rule, FORBIDDEN[reference]))
                if isinstance(child, ast.Attribute) and child.attr in WINDOWS_LAUNCH_API:
                    findings.append(Finding(path, child.lineno, function, "ShellExecute",
                                            f"{child.attr} starts or opens by name: use lumi.executables"))
                if isinstance(child, ast.Attribute) and child.attr in SHELL_METHODS:
                    findings.append(Finding(path, child.lineno, function, "shell", f"{child.attr}()"))
            if isinstance(child, ast.Call):
                target = _qualified(child.func, aliases)
                if target == "getattr" and len(child.args) >= 2 and isinstance(child.args[1], ast.Constant):
                    reference = f"{_qualified(child.args[0], aliases)}.{child.args[1].value}"
                    if reference in FORBIDDEN:
                        rule = "shell" if FORBIDDEN[reference] == "a shell" else reference
                        findings.append(Finding(path, child.lineno, function, rule, FORBIDDEN[reference]))
                launch = _launch_index(child, aliases, wrappers, partials, names_in)
                if launch is not None:
                    name, index = launch
                    for keyword in child.keywords:
                        if keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant)
                                                           and keyword.value.value is False):
                            findings.append(Finding(path, child.lineno, function, "shell", f"{name}(shell=...)"))
                    own_parameters = scope.parameters if names else []
                    is_wrapper = bool(names) and (module, names[-1]) in wrappers
                    for origin in sorted(set(_origins(_program(child, index), scope, aliases))):
                        if origin == "ok" or (origin.startswith("parameter:") and is_wrapper
                                              and origin[len("parameter:"):] in own_parameters):
                            continue
                        if origin.startswith("bare:"):
                            findings.append(Finding(path, child.lineno, function, "bare name",
                                                    f"{name} starts {origin[5:]!r} by name"))
                        else:
                            findings.append(Finding(path, child.lineno, function, "unresolved",
                                                    f"{name} starts {origin.split(':', 1)[1]}"))
            visit(child, names, scope)

    visit(tree, [], module_scope)
    return findings


@functools.lru_cache(maxsize=1)
def _sources() -> tuple[dict[str, str], dict[tuple[str, str], int], set[str]]:
    """Lumi's modules by path, the functions anywhere in them that start a parameter, and their names."""
    files = sorted(LUMI.rglob("*.py"))
    sources = {file.relative_to(LUMI.parent).as_posix(): file.read_text(encoding="utf-8") for file in files}
    known = {_module_name(path)[0] for path in sources}
    trees = {path: ast.parse(source) for path, source in sources.items()}
    names = {path: _Names(tree, *_module_name(path), known) for path, tree in trees.items()}
    wrappers: dict[tuple[str, str], int] = {}
    while True:  # functions that start a parameter, then those that pass one on to them, and so on
        found: dict[tuple[str, str], int] = {}
        for path, tree in trees.items():
            found.update(_wrappers_in(tree, _module_name(path)[0], wrappers, names[path]))
        if found.keys() <= wrappers.keys():
            break
        wrappers.update(found)
    return sources, wrappers, known


@functools.lru_cache(maxsize=1)
def scan() -> tuple[Finding, ...]:
    sources, wrappers, known = _sources()
    findings: list[Finding] = []
    for path, source in sources.items():
        findings += scan_source(source, path, wrappers, known)
    return tuple(findings)


def test_lumis_own_launches_come_from_the_resolver():
    counts = collections.Counter((finding.path, finding.function, finding.rule) for finding in scan())
    unexpected = [finding for finding in scan()
                  if counts[(finding.path, finding.function, finding.rule)]
                  != BY_DESIGN.get((finding.path, finding.function, finding.rule), (0, ""))[0]]
    assert not unexpected, (
        "Start Lumi's own programs by their full path from lumi/executables.py "
        "(program, find_program, system_program, project_tool) or lumi/safe_git.py, or add a reviewed "
        "entry with its count to BY_DESIGN:\n" + "\n".join(map(str, unexpected)))


def test_every_by_design_entry_is_still_used():
    used = {(finding.path, finding.function, finding.rule) for finding in scan()}
    assert set(BY_DESIGN) <= used, sorted(set(BY_DESIGN) - used)


BAD = {
    'import subprocess\nsubprocess.run(["git", "status"])\n': ["bare name"],
    'import subprocess\ndef f(args):\n    subprocess.run(["git"] + list(args))\n': ["bare name"],
    'import subprocess\ndef f(a):\n    cmd = ["git", *a]\n    subprocess.Popen(cmd, cwd=".")\n': ["bare name"],
    'import subprocess\nsubprocess.run("git status", shell=True)\n': ["shell", "bare name"],
    'from subprocess import check_output as run\nrun([r"bin\\rg.exe", "-e", "x"])\n': ["bare name"],
    'import shutil\nprogram = shutil.which("git")\n': ["shutil.which"],
    'from shutil import which\nprogram = which("git")\n': ["shutil.which"],
    'import os\nos.startfile("notes.txt")\n': ["os.startfile"],
    'import os\nopen_it = launcher or os.startfile\n': ["os.startfile"],
    'import webbrowser\nwebbrowser.open(url)\n': ["webbrowser.open"],
    'import os\nos.system("git status")\n': ["shell"],
    'import subprocess, shutil\nsubprocess.run([shutil.which("rg") or "rg", "x"])\n':
        ["bare name", "unresolved", "shutil.which"],
    'from lumi.processes import popen_in_kill_job\npopen_in_kill_job(["git", "gc"])\n': ["bare name"],
    'import subprocess\ndef _exec(argv):\n    return subprocess.run(argv)\n_exec(["taskkill", "/F"])\n':
        ["bare name"],
    'import subprocess\nclass A:\n    def go(self):\n        subprocess.Popen(["explorer", "/select,", p])\n':
        ["bare name"],
    'import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec("git", "status")\n': ["bare name"],
    # module constants, f-strings, a *prefix, append, partial, event loops, getattr, ctypes, pty
    'import subprocess\nGIT = "git"\ndef f():\n    subprocess.run([GIT, "status"])\n': ["bare name"],
    'import subprocess\ndef f(v):\n    subprocess.run([f"git{v}", "status"])\n': ["bare name"],
    'import subprocess\nBASE = ["git", "-c", "x=y"]\ndef f():\n    subprocess.run([*BASE, "status"])\n':
        ["bare name"],
    'import subprocess\ndef f():\n    cmd = []\n    cmd.append("git")\n    subprocess.run(cmd)\n': ["bare name"],
    'import functools, subprocess\nrun = functools.partial(subprocess.run, check=True)\nrun(["git", "gc"])\n':
        ["bare name"],
    'async def f(loop, p):\n    await loop.subprocess_exec(p, "git", "status")\n': ["bare name"],
    'async def f(loop, p):\n    await loop.subprocess_shell(p, "git status")\n': ["shell"],
    'import os\ngetattr(os, "startfile")(path)\n': ["os.startfile"],
    'import ctypes\nctypes.windll.shell32.ShellExecuteW(None, "open", "notes.bat", None, None, 1)\n':
        ["ShellExecute"],
    'import pty\npty.spawn(["sh"])\n': ["bare name"],
    'import subprocess\ndef f(self):\n    subprocess.run([self.cli_path, "exec"])\n': ["unresolved"],
    'import subprocess\ndef f():\n    subprocess.run(make_command())\n': ["unresolved"],
}
GOOD = [
    'import subprocess\nfrom lumi.executables import program\nsubprocess.run([program("git"), "status"])\n',
    'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "pytest"], shell=False)\n',
    'import subprocess\nsubprocess.run(["C:\\\\Windows\\\\System32\\\\cmd.exe", "/c", "ver"])\n',
    'import subprocess\nsubprocess.run(["/usr/bin/open", "-R", p])\n',
    'import subprocess\ndef f(argv):\n    return subprocess.run(argv)\nf([program("git")])\n',
    'import subprocess\nproc: subprocess.Popen | None = None\n',
    'import subprocess\nfrom lumi import safe_git\nsubprocess.run(safe_git.argv("status"))\n',
    'import subprocess\ndef f():\n    tool, source, notice = project_tool("ruff", p, trusted=True)\n'
    '    subprocess.run([tool, "check"])\n',
    'import subprocess\ndef f():\n    cmd = [find_program("git")]\n    cmd.append("status")\n    subprocess.run(cmd)\n',
    'import subprocess\nGIT = "/usr/bin/git"\ndef f():\n    subprocess.run([GIT, "status"])\n',
]


def test_the_scan_finds_what_it_is_for():
    # A wrapper in another module, as the whole scan finds it.
    shared = {("lumi.processes", "popen_in_kill_job"): 0}
    for source, rules in BAD.items():
        assert [finding.rule for finding in scan_source(source, "x.py", shared, {"lumi.processes"})] == rules, source
    for source in GOOD:
        assert scan_source(source, "x.py", shared, {"lumi.processes"}) == [], source


def test_the_scan_follows_wrappers_across_modules():
    wrappers = _sources()[1]
    assert wrappers[("lumi.processes", "popen_in_kill_job")] == 0
    assert wrappers[("lumi.engine.tools", "_run_subprocess_with_cancel")] == 0
    assert wrappers[("lumi.engine.provider_extensions", "run")] == 1  # its callers' command_for() is checked
    assert ("lumi.safe_git", "run") not in wrappers  # it starts safe_git.argv(...), never a parameter


def test_lumis_own_git_goes_through_safe_git():
    """Every Git call Lumi makes gets safe_git's fixed options and trust check (lumi/safe_git.py).

    Team integration keeps its own equivalent options and checks
    (engine/swarming/git_boundary.py); the agent's shell runs Git as the
    person would.
    """
    allowed = {"lumi/safe_git.py", "lumi/engine/swarming/git_boundary.py"}
    found = []
    for path, source in _sources()[0].items():
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and _qualified(node.func, {}) in ("program", "find_program",
                                                                              "configured_program", "project_tool")
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "git"
                    and path not in allowed):
                found.append(f"{path}:{node.lineno}")
    assert not found, "Run Lumi's own Git through lumi/safe_git.py (run or argv): " + ", ".join(found)

"""No new launch of a program by bare name in Lumi's code (lumi/executables.py).

On Windows a program started by a bare name (``git``) is looked for in the
working folder first, and a repository is often that folder. Lumi's own
launches take their program's full path from ``lumi/executables.py``. This
scan reads every module in ``lumi/`` and fails on a launch whose program is a
name or relative path written in the code, a shell (``shell=True``,
``os.system``), ``shutil.which`` (which searched the working folder before
Python 3.12), ``os.startfile`` or ``webbrowser`` anywhere but the places
below, each of which does so by design.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

LUMI = Path(__file__).resolve().parents[1] / "lumi"

# Calls that start a process, and where their program is among the arguments.
LAUNCHERS = {
    "subprocess.run": 0, "subprocess.Popen": 0, "subprocess.call": 0, "subprocess.check_call": 0,
    "subprocess.check_output": 0, "asyncio.create_subprocess_exec": 0,
    **{f"os.{name}": 0 for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
                                     "execlpe", "posix_spawn", "posix_spawnp")},
    **{f"os.{name}": 1 for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
                                     "spawnlpe")},
}
# Lumi's own helpers that start their first argument, wherever they're called from.
SHARED_WRAPPERS = {"popen_in_kill_job": 0, "_run_subprocess_with_cancel": 0}
# Never used outside the places listed in BY_DESIGN.
FORBIDDEN = {
    "shutil.which": "use lumi.executables.find_program: which() searches the working folder before Python 3.12",
    "os.system": "a shell", "os.popen": "a shell", "subprocess.getoutput": "a shell",
    "subprocess.getstatusoutput": "a shell", "asyncio.create_subprocess_shell": "a shell",
    "os.startfile": "ShellExecute looks in the working folder: use lumi.executables.open_path or open_url",
    "webbrowser.open": "use lumi.executables.open_url", "webbrowser.open_new": "use lumi.executables.open_url",
    "webbrowser.open_new_tab": "use lumi.executables.open_url", "webbrowser.get": "use lumi.executables.open_url",
}

# (module, function, rule): why that launch is allowed. A command the model or
# the person asked for runs in the project by design, with their environment
# (secrets_store.child_env).
BY_DESIGN = {
    ("lumi/engine/tools.py", "_run_subprocess_with_cancel", "shell"):
        "the agent's shell runner: bash, check_run and the composer's ! commands",
    ("lumi/engine/tools.py", "_exec_bash", "shell"):
        "the agent's bash and check_run tools: a shell unless the shell sandbox wraps the command",
    ("lumi/engine/hooks.py", "_run_command", "shell"): "the person's own hook commands",
    ("lumi/orchestration/acceptance_check.py", "BashRunner.run", "shell"):
        "a plan's [bash] criteria, in cmd.exe when no bash is installed",
    ("lumi/model_evals.py", "_check", "shell"): "the person's check command for a model comparison",
    ("lumi/harness/prompts.py", "HarnessPrompts.run_harness_generator_validation_probes", "shell"):
        "the validation commands the model wrote, in sprint mode",
    ("lumi/engine/worktrees.py", "WorktreeManager.integrate", "shell"): "validation commands a caller names",
    ("lumi/update_file.py", "install", "os.startfile"): "the verified installer, by its full path",
    ("lumi/engine/computer_use.py", "exec_open_application", "os.startfile"):
        "an application the model named, resolved first (_windows_application)",
    ("lumi/executables.py", "open_url", "os.startfile"): "an http(s) address: its protocol's handler",
    ("lumi/executables.py", "open_path", "os.startfile"): "an existing file, by its full path",
}

_ABSOLUTE = re.compile(r"^(/|[A-Za-z]:[\\/]|\\\\)")


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
    modules = {"subprocess", "os", "shutil", "webbrowser", "asyncio"}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    found[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in modules:
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


class _Scope:
    def __init__(self, name: str, node: ast.AST | None):
        self.name = name
        self.assigned: dict[str, list[ast.AST]] = {}
        self.parameters: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args]
            if arguments and arguments[0].arg in ("self", "cls"):
                arguments = arguments[1:]
            self.parameters = [argument.arg for argument in arguments]
            body = node.body
        else:
            body = getattr(node, "body", [])
        for child in _own_nodes(body):
            if isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                self.assigned.setdefault(child.targets[0].id, []).append(child.value)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.value:
                self.assigned.setdefault(child.target.id, []).append(child.value)


def _own_nodes(body: list[ast.AST]):
    """The nodes of a body, without descending into nested functions and classes."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(child)


def _constant_programs(node: ast.AST, scope: _Scope, depth: int = 0) -> list[str]:
    """Program names written in the code that ``node``, a program and its arguments, may start."""
    if depth > 5:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.split()[0]] if node.value.split() else [""]
    if isinstance(node, (ast.List, ast.Tuple)):
        return _constant_program(node.elts[0], scope, depth + 1) if node.elts else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _constant_programs(node.left, scope, depth + 1)
    if isinstance(node, ast.Name):
        return [name for value in scope.assigned.get(node.id, []) for name in _constant_programs(value, scope, depth + 1)]
    if isinstance(node, ast.BoolOp):
        return [name for value in node.values for name in _constant_programs(value, scope, depth + 1)]
    if isinstance(node, ast.IfExp):
        return _constant_programs(node.body, scope, depth + 1) + _constant_programs(node.orelse, scope, depth + 1)
    return []  # computed: a call, an attribute, a subscript


def _constant_program(node: ast.AST, scope: _Scope, depth: int) -> list[str]:
    """The same for one element: the program itself."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return [name for value in scope.assigned.get(node.id, []) for name in _constant_program(value, scope, depth + 1)]
    if isinstance(node, ast.BoolOp):
        return [name for value in node.values for name in _constant_program(value, scope, depth + 1)]
    if isinstance(node, ast.IfExp):
        return _constant_program(node.body, scope, depth + 1) + _constant_program(node.orelse, scope, depth + 1)
    return []


def _argument(call: ast.Call, index: int) -> ast.AST | None:
    """The call's argument at ``index`` (or ``args=``), unless an unpacked argument comes first."""
    leading = call.args[:index + 1]
    if len(leading) > index and not any(isinstance(argument, ast.Starred) for argument in leading):
        return call.args[index]
    return next((keyword.value for keyword in call.keywords if keyword.arg == "args"), None)


def _local_wrappers(tree: ast.AST, aliases: dict[str, str]) -> dict[str, int]:
    """This module's functions that start one of their parameters: ``def run(argv): Popen(argv)``."""
    wrappers: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = _Scope(node.name, node)
        for child in _own_nodes(node.body):
            if isinstance(child, ast.Call) and _qualified(child.func, aliases) in LAUNCHERS:
                program = _argument(child, LAUNCHERS[_qualified(child.func, aliases)])
                if isinstance(program, ast.Name) and program.id in scope.parameters:
                    wrappers[node.name] = scope.parameters.index(program.id)
    return wrappers


def scan_source(source: str, path: str) -> list[Finding]:
    """Every launch in ``source`` that breaks the rule, before BY_DESIGN is applied."""
    tree = ast.parse(source)
    aliases = _aliases(tree)
    wrappers = {**_local_wrappers(tree, aliases), **SHARED_WRAPPERS}
    findings: list[Finding] = []

    def visit(node: ast.AST, names: list[str], scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = [*names, child.name]
                visit(child, qualified, _Scope(".".join(qualified), child))
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, [*names, child.name], scope)
                continue
            function = ".".join(names)
            if isinstance(child, (ast.Attribute, ast.Name)) and not isinstance(getattr(child, "ctx", None), ast.Store):
                reference = _qualified(child, aliases)
                if reference in FORBIDDEN and (isinstance(child, ast.Attribute) or child.id in aliases):
                    rule = "shell" if FORBIDDEN[reference] == "a shell" else reference
                    findings.append(Finding(path, child.lineno, function, rule, FORBIDDEN[reference]))
            if isinstance(child, ast.Call):
                target = _qualified(child.func, aliases)
                index = LAUNCHERS.get(target, wrappers.get(target))
                if index is not None:
                    for keyword in child.keywords:
                        if keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant)
                                                           and keyword.value.value is False):
                            findings.append(Finding(path, child.lineno, function, "shell", f"{target}(shell=...)"))
                    program = _argument(child, index)
                    for name in _constant_programs(program, scope) if program is not None else []:
                        if not _ABSOLUTE.match(name):
                            findings.append(Finding(path, child.lineno, function, "bare name",
                                                    f"{target} starts {name!r} by name"))
            visit(child, names, scope)

    visit(tree, [], _Scope("", tree))
    return findings


@functools.lru_cache(maxsize=1)
def scan() -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for file in sorted(LUMI.rglob("*.py")):
        path = file.relative_to(LUMI.parent).as_posix()
        findings += scan_source(file.read_text(encoding="utf-8"), path)
    return tuple(findings)


def test_lumis_own_launches_never_name_a_program_it_could_find_in_the_project():
    unexpected = [finding for finding in scan()
                  if (finding.path, finding.function, finding.rule) not in BY_DESIGN]
    assert not unexpected, (
        "Start Lumi's own programs by their full path from lumi/executables.py "
        "(program, find_program, system_program), or add a reviewed entry to BY_DESIGN:\n"
        + "\n".join(map(str, unexpected)))


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
    'import subprocess, shutil\nsubprocess.run([shutil.which("rg") or "rg", "x"])\n': ["bare name", "shutil.which"],
    'from lumi.processes import popen_in_kill_job\npopen_in_kill_job(["git", "gc"])\n': ["bare name"],
    'import subprocess\ndef _exec(argv):\n    return subprocess.run(argv)\n_exec(["taskkill", "/F"])\n':
        ["bare name"],
    'import subprocess\nclass A:\n    def go(self):\n        subprocess.Popen(["explorer", "/select,", p])\n':
        ["bare name"],
    'import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec("git", "status")\n': ["bare name"],
}
GOOD = [
    'import subprocess\nfrom lumi.executables import program\nsubprocess.run([program("git"), "status"])\n',
    'import subprocess\nsubprocess.run([sys.executable, "-m", "pytest"], shell=False)\n',
    'import subprocess\nsubprocess.run(["C:\\\\Windows\\\\System32\\\\cmd.exe", "/c", "ver"])\n',
    'import subprocess\nsubprocess.run(["/usr/bin/open", "-R", p])\n',
    'import subprocess\ndef f(argv):\n    return subprocess.run(argv)\nf([program("git")])\n',
    'import subprocess\nproc: subprocess.Popen | None = None\n',
]


def test_the_scan_finds_what_it_is_for():
    for source, rules in BAD.items():
        assert [finding.rule for finding in scan_source(source, "x.py")] == rules, source
    for source in GOOD:
        assert scan_source(source, "x.py") == [], source

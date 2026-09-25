"""pywebview's JavaScript bridge under the page's Content-Security-Policy.

The policy refuses eval and new Function (lumi/gui/local_access.py).
pywebview builds window.pywebview.api with new Function and hands results
back through eval, and WebKit (macOS, Linux) applies the page's policy to
those scripts too. lumi/gui/webview_bridge.py replaces both steps. These
tests run the installed pywebview's own scripts in a Node context that
refuses code generation from strings, as WebKit does under the policy.
"""

import json
import shutil
import subprocess
import time

import pytest

webview_util = pytest.importorskip("webview.util")

from lumi.gui import webview_bridge  # noqa: E402

NODE = shutil.which("node")


class _Api:
    def ping(self, value):
        return {"pong": value}


class _Window:
    """The parts of a pywebview window its script loader and bridge read."""

    def __init__(self):
        self.uid = "master"
        self.js_api_endpoint = None
        # As Lumi's desktop window (gui/server.py).
        self.text_select = True
        self.frameless = True
        self.easy_drag = False
        self.zoomable = False
        self.draggable = False
        self.state = {}
        self._functions = {}
        self._js_api = _Api()
        self.ran = []
        self.evaluated = []

    def run_js(self, script):
        self.ran.append(script)

    def evaluate_js(self, script, callback=None):
        self.evaluated.append(script)


@pytest.fixture
def window(monkeypatch):
    """A window with the bridge installed; pywebview's loader is restored afterwards."""
    monkeypatch.setattr(webview_util, "load_js_files", webview_util.load_js_files)
    window = _Window()
    webview_bridge.install(window)
    return window


def _returned_script(window, value_id="5"):
    """How pywebview hands ping('hi')'s result back to the page."""
    webview_util.js_bridge_call(window, "ping", ["hi"], value_id)
    deadline = time.monotonic() + 5
    while not (window.ran or window.evaluated) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window.evaluated == [], "a result went through eval"
    [script] = window.ran
    return script


def test_results_return_without_eval(window):
    script = _returned_script(window)
    assert script.startswith(webview_bridge.RESULT_CALLBACK_PREFIX)
    assert '"5"' in script and "pong" in script

    # Everything else still goes through pywebview's evaluate_js.
    window.evaluate_js("document.title")
    window.evaluate_js(script, callback=print)
    assert window.evaluated == ["document.title", script]


def test_installing_twice_adds_the_api_once(window):
    webview_bridge.install(_Window())
    js_code, _finish = webview_util.load_js_files(window, "cocoa")
    assert js_code.count(webview_bridge.CREATE_API_WITHOUT_EVAL) == 1


# Runs pywebview's page script, its finish script and a returned result, in a
# context that refuses eval and new Function. Math.random is fixed so the
# call's id is "5", the id the Python side answered with.
_RUN_BRIDGE = r"""
const vm = require('node:vm');
const [pageScript, finishScript, resultScript] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const posted = [], events = [], noop = () => {};
const context = vm.createContext({
    console, EventTarget, Event, CustomEvent,
    Window: function Window() {}, Node: function Node() {},
    location: {href: 'http://127.0.0.1:48123/', origin: 'http://127.0.0.1:48123'},
    document: {readyState: 'complete', body: {addEventListener: noop}, head: {appendChild: noop},
               addEventListener: noop, createElement: () => ({}), querySelectorAll: () => []},
    webkit: {messageHandlers: {jsBridge: {postMessage: message => posted.push(JSON.parse(message))},
                               browserDelegate: {postMessage: noop}}},
    addEventListener: noop,
    dispatchEvent: event => { events.push(event.type); return true; },
}, {codeGeneration: {strings: false, wasm: false}});
context.window = context;
const run = code => vm.runInContext(code, context);
const report = {};
const done = () => process.stdout.write(JSON.stringify(report));
run('Math.random = () => 0.5;');
try {
    run(pageScript);
    run(finishScript);
} catch (error) {
    report.error = `${error.name}: ${error.message}`;
}
report.ready = events.includes('pywebviewready');
report.api = typeof (context.pywebview && context.pywebview.api.ping);
if (report.api !== 'function') {
    done();
} else {
    const call = run("window.pywebview.api.ping('hi')");
    report.posted = posted;
    run(resultScript);
    call.then(value => { report.result = value; done(); },
              error => { report.rejected = String(error); done(); });
}
"""


def _run_bridge(page_script, finish_script, result_script):
    # pywebview fills in the API it found on js_api, here _Api.ping(value).
    finish_script = finish_script % {"functions": json.dumps([{"func": "ping", "params": ["value"]}])}
    completed = subprocess.run(
        [NODE, "-e", _RUN_BRIDGE],
        input=json.dumps([page_script, finish_script, result_script]).encode("utf-8"),
        capture_output=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="needs Node.js")
def test_bridge_works_where_eval_is_refused(window):
    page_script, finish_script = webview_util.load_js_files(window, "cocoa")
    report = _run_bridge(page_script, finish_script, _returned_script(window))

    assert "error" not in report, report
    assert report["ready"] and report["api"] == "function"
    assert report["posted"] == [{"funcName": "ping", "params": ["hi"], "id": "5"}]
    assert report["result"] == {"pong": "hi"}


@pytest.mark.skipif(NODE is None, reason="needs Node.js")
def test_pywebviews_own_bridge_fails_where_eval_is_refused():
    """The control: without the replacement, the same context breaks pywebview."""
    assert not getattr(webview_util.load_js_files, "_lumi_without_eval", False)
    page_script, finish_script = webview_util.load_js_files(_Window(), "cocoa")
    report = _run_bridge(page_script, finish_script, "")

    assert report["error"].startswith("EvalError"), report
    assert not report["ready"]
    assert report["api"] != "function"

"""
pywebview's JavaScript bridge, without eval.

The page's Content-Security-Policy (gui/local_access.py) refuses ``eval`` and
``new Function``. pywebview uses both: it builds ``window.pywebview.api`` with
``new Function``, and it hands every call's result back through
``Window.evaluate_js``, which wraps the code in ``eval``. WebView2 on Windows
exempts the scripts its host runs, but WebKit (macOS, Linux) applies the
page's policy to them. There the API would never be built, and the frameless
window's minimize, maximize and close buttons would stop working.

:func:`install` replaces both steps with equivalents that need no eval: the
API functions become closures, defined with pywebview's own script, and each
result is handed back with ``Window.run_js``, which runs the callback as is.

Both rely on pywebview internals (``load_js_files``, ``_createApi``,
``_checkValue``, ``_jsApiCallback``, ``_returnValuesCallbacks``). Each change
applies only when those exist, and tests/test_webview_bridge.py runs the
installed pywebview's bridge where code generation from strings is refused.

:func:`install` also checks every call that reaches pywebview's dispatcher
(``js_bridge_call``). pywebview looks the call's name up attribute by
attribute on the API object, and builds the result callback it runs in the
page from the name and the call's id without escaping either. The window's
own page sends plain names and ids, but WebKit (macOS, Linux) gives every
frame in the window the bridge's message handler, and the window can hold a
capability pack's sandboxed panel (gui/extension_panels.py). A name or id
that isn't a plain identifier is refused, so no frame can run script in the
page through a result callback or reach attributes past the API's methods.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

logger = logging.getLogger(__name__)

# The names and ids the window's page sends: Lumi's window API (gui/server.py),
# pywebview's own callbacks (pywebviewMoveWindow, ...) and ids such as "move",
# random digits or a uuid's hex.
_BRIDGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_BRIDGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Appended to the script pywebview injects on every page load, which runs
# before its finish script calls _createApi. Same stubs as pywebview's own,
# written as closures.
CREATE_API_WITHOUT_EVAL = """
;(function () {
    var bridge = window.pywebview;
    if (!bridge || typeof bridge._checkValue !== 'function'
            || typeof bridge._jsApiCallback !== 'function' || !bridge._returnValuesCallbacks) {
        return;
    }
    bridge._createApi = function (funcList) {
        funcList.forEach(function (entry) {
            var funcName = entry.func;
            var path = funcName.split('.');
            var name = path.pop();
            var owner = path.reduce(function (obj, prop) {
                if (!obj[prop]) obj[prop] = {};
                return obj[prop];
            }, window.pywebview.api);
            owner[name] = function () {
                var id = (Math.random() + '').substring(2);
                var promise = new Promise(function (resolve, reject) {
                    window.pywebview._checkValue(funcName, resolve, reject, id);
                });
                window.pywebview._jsApiCallback(funcName, Array.prototype.slice.call(arguments), id);
                return promise;
            };
            window.pywebview._returnValuesCallbacks[funcName] = {};
        });
    };
})();
"""

# How pywebview's js_bridge_call hands a result back to the page.
RESULT_CALLBACK_PREFIX = "window.pywebview._returnValuesCallbacks["


def checked_bridge_call(call: Any) -> Any:
    """``call``, pywebview's ``js_bridge_call``, refusing a name or id that isn't a plain identifier."""
    if getattr(call, "_lumi_checked", False):
        return call

    def js_bridge_call(window, func_name, param, value_id):
        if not (isinstance(func_name, str) and _BRIDGE_NAME.fullmatch(func_name)
                and isinstance(value_id, str) and _BRIDGE_ID.fullmatch(value_id)):
            logger.warning("Refused a window bridge call whose name or id isn't a plain identifier")
            return None
        return call(window, func_name, param, value_id)

    js_bridge_call._lumi_checked = True
    return js_bridge_call


def check_bridge_calls() -> None:
    """Send every bridge call through :func:`checked_bridge_call`.

    Platform modules copy ``js_bridge_call`` when they're imported (``from
    webview.util import js_bridge_call``), which ``webview.start`` does after
    :func:`install`; one imported already is patched in place.
    """
    import webview.util as webview_util

    webview_util.js_bridge_call = checked_bridge_call(webview_util.js_bridge_call)
    for name, module in list(sys.modules.items()):
        if name.startswith("webview.platforms.") and callable(getattr(module, "js_bridge_call", None)):
            module.js_bridge_call = checked_bridge_call(module.js_bridge_call)


def install(window: Any) -> None:
    """Make ``window``'s bridge work where the page refuses eval, and accept only plain calls.

    Call after ``webview.create_window`` and before ``webview.start``.
    """
    import webview.util as webview_util

    check_bridge_calls()
    load_js_files = webview_util.load_js_files
    if not getattr(load_js_files, "_lumi_without_eval", False):
        def load_js_files_without_eval(*args, **kwargs):
            loaded = load_js_files(*args, **kwargs)
            if isinstance(loaded, tuple) and loaded and isinstance(loaded[0], str):
                return (loaded[0] + CREATE_API_WITHOUT_EVAL, *loaded[1:])
            return loaded

        load_js_files_without_eval._lumi_without_eval = True
        webview_util.load_js_files = load_js_files_without_eval

    run_js = getattr(window, "run_js", None)
    if run_js is None:
        logger.debug("pywebview has no run_js; results still go through eval")
        return
    evaluate_js = window.evaluate_js

    def evaluate_js_without_eval(script, callback=None):
        if callback is None and isinstance(script, str) and script.startswith(RESULT_CALLBACK_PREFIX):
            return run_js(script)
        return evaluate_js(script, callback)

    window.evaluate_js = evaluate_js_without_eval

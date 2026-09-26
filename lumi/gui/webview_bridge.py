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
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

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


def install(window: Any) -> None:
    """Make ``window``'s bridge work where the page refuses eval.

    Call after ``webview.create_window`` and before ``webview.start``.
    """
    import webview.util as webview_util

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

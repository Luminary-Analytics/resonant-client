"""External acceptance checks. Invoked by benchmark.py, never copied to agents."""

from __future__ import annotations

import csv
from html.parser import HTMLParser
import importlib
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import urlopen
from wsgiref.simple_server import make_server, WSGIRequestHandler


class ExportForm(HTMLParser):
    """Read the actual form and submit control used by the integration check."""

    def __init__(self, document):
        super().__init__()
        self.forms = []
        self.current = None
        self.button = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.current = {"attrs": dict(attrs), "buttons": []}
            self.forms.append(self.current)
        elif tag == "button" and self.current is not None:
            self.button = {"attrs": dict(attrs), "text": ""}
            self.current["buttons"].append(self.button)

    def handle_data(self, data):
        if self.button is not None:
            self.button["text"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        if tag == "button":
            self.button = None


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def preserved_inputs(root, scenario):
    for relative, content in scenario["seed"].items():
        if relative not in scenario["editable"]:
            require((root / relative).read_bytes() == content.encode("utf-8"),
                    f"Input changed: {relative}")


def investigation(root):
    def findings():
        data = json.loads((root / "report.json").read_text(encoding="utf-8"))
        return data["findings"]

    def check_finding(identifier, source, line, observed, expected):
        matching = [item for item in findings() if item.get("id") == identifier]
        require(len(matching) == 1, f"Missing/duplicate finding: {identifier}")
        finding = matching[0]
        require(finding["source"] == source and finding["line"] == line,
                "Source location does not identify the defect")
        require(finding["observed"] == observed and finding["expected"] == expected,
                "Finding does not match the reproduced observation and contract")
        require(isinstance(finding["explanation"], str) and finding["explanation"].strip(),
                "Finding has no explanation")

    def cache_finding():
        observed = importlib.import_module("cache").is_fresh(1000, 0, 30)
        check_finding("cache_ttl", "cache.py", 4, observed, True)

    def url_finding():
        observed = importlib.import_module("urls").search_url(["red", "blue"])
        check_finding("repeated_tags", "urls.py", 5, observed, "/search?tag=red&tag=blue")

    def complete_report():
        require(len(findings()) == 2, "Report must cover both independent investigations")

    return [("cache_finding", cache_finding), ("url_finding", url_finding),
            ("complete_report", complete_report)]


def csv_checks(root):
    rows = json.loads((root / "rows.json").read_text(encoding="utf-8"))

    def check_csv(body, expected):
        require(type(body) is bytes, "CSV response must be UTF-8 bytes")
        decoded = body.decode("utf-8")
        reader = csv.DictReader(StringIO(decoded, newline=""))
        require(reader.fieldnames == ["id", "name", "note"], "Incorrect CSV header")
        require(list(reader) == [{key: str(value) for key, value in row.items()}
                                 for row in expected], "CSV rows did not round-trip")
        require(decoded.endswith("\r\n"), "CSV must use RFC 4180 record terminators")

    def backend_edge_cases():
        backend = importlib.import_module("backend")
        check_csv(backend.export_csv(rows), rows)
        check_csv(backend.export_csv([]), [])

    def export_form(document):
        forms = ExportForm(document).forms
        matches = [form for form in forms if any(
            button["text"].strip() == "Export CSV"
            and button["attrs"].get("type", "submit") == "submit"
            and "disabled" not in button["attrs"] for button in form["buttons"])]
        require(len(matches) == 1, "One enabled, labelled Export CSV submit control is required")
        form = matches[0]["attrs"]
        require(form.get("method", "get").lower() == "get"
                and form.get("action") == "/export.csv", "Form does not use the agreed route")
        return form

    def frontend_control():
        export_form((root / "frontend.html").read_text(encoding="utf-8"))

    def integrated_http():
        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        app = importlib.import_module("app").application
        with make_server("127.0.0.1", 0, app, handler_class=QuietHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/", timeout=2) as response:
                    form = export_form(response.read().decode("utf-8"))
                with urlopen(base + form["action"], timeout=2) as response:
                    require(response.headers.get_content_type() == "text/csv", "Incorrect MIME type")
                    require(response.headers["Content-Disposition"]
                            == 'attachment; filename="contacts.csv"', "Missing attachment filename")
                    check_csv(response.read(), rows)
                try:
                    urlopen(base + "/missing", timeout=2)
                except HTTPError as exc:
                    require(exc.code == 404, "Unknown route must return 404")
                else:
                    raise AssertionError("Unknown route did not return 404")
            finally:
                server.shutdown()
                thread.join(timeout=2)
    return [("csv_backend_edge_cases", backend_edge_cases),
            ("frontend_export_control", frontend_control), ("integrated_http_export", integrated_http)]


def serial_checks(root):
    def summarize(events):
        return importlib.import_module("ledger").summarize(events)

    def exact_cents():
        result = summarize([
            {"account": " B ", "amount": "0.10", "kind": "charge"},
            {"account": "A", "amount": "12.30", "kind": "charge"},
            {"account": "B", "amount": "0.20", "kind": "charge"},
            {"account": "A", "amount": "2.01", "kind": "refund"},
        ])
        require(result == {"schema": 2, "totals": [
            {"account": "A", "net_cents": 1029, "entries": 2},
            {"account": "B", "net_cents": 30, "entries": 2},
        ]}, "Pipeline did not normalize, aggregate, and render schema 2 correctly")
        require(all(type(total["net_cents"]) is int for total in result["totals"]),
                "Amounts must be integer cents")

    def empty_input():
        require(summarize([]) == {"schema": 2, "totals": []}, "Incorrect empty summary")

    def invalid_events():
        for changes in ({"account": "  "}, {"kind": "credit"}, {"amount": "-1"},
                        {"amount": "0.001"}, {"amount": "NaN"},
                        {"amount": "Infinity"}, {"amount": "nonsense"}):
            event = {"account": "A", "amount": "1.00", "kind": "charge", **changes}
            try:
                summarize([event])
            except ValueError:
                continue
            raise AssertionError(f"Invalid event accepted: {changes}")
    return [("schema_and_exact_cents", exact_cents), ("empty_input", empty_input),
            ("invalid_events", invalid_events)]


def recovery_checks(root):
    tasks = json.loads((root / "tasks.json").read_text(encoding="utf-8"))

    def interrupted_state(state):
        module = importlib.import_module("processor")
        try:
            module.run(state, tasks, interrupt_after_effect=1)
        except module.Interrupted:
            pass
        else:
            raise AssertionError("Requested interruption did not occur")
        records = [json.loads(line) for line in (state / "effects.jsonl").read_text().splitlines()]
        require(records == tasks[:1], "Interruption must follow exactly one durable new effect")
        checkpoint = state / "checkpoint.json"
        require(not checkpoint.exists() or json.loads(checkpoint.read_text()) == [],
                "Interruption must leave the first effect uncheckpointed")
        return module

    def boundary():
        with tempfile.TemporaryDirectory(prefix="sonn-interruption-") as directory:
            interrupted_state(Path(directory))

    def resume():
        with tempfile.TemporaryDirectory(prefix="sonn-recovery-") as directory:
            state = Path(directory)
            module = interrupted_state(state)
            prefix = (state / "effects.jsonl").read_bytes()
            module.run(state, tasks)
            contents = (state / "effects.jsonl").read_bytes()
            require(contents.startswith(prefix), "Resume overwrote durable work")
            require([json.loads(line) for line in contents.splitlines()] == tasks,
                    "Resume lost or duplicated effects")
            require(json.loads((state / "checkpoint.json").read_text())
                    == [task["id"] for task in tasks], "Checkpoint was not reconciled")
            before = {path.name: path.read_bytes() for path in state.iterdir()}
            module.run(state, tasks)
            require(before == {path.name: path.read_bytes() for path in state.iterdir()},
                    "Completed run is not idempotent")

    def corrupt_state():
        cases = [
            ("broken\n", None),
            (json.dumps(tasks[0]) + "\n" + json.dumps(tasks[0]) + "\n", None),
            (json.dumps({"id": "foreign", "value": 1}) + "\n", None),
            (json.dumps({"id": "a", "value": 99}) + "\n", None),
            (json.dumps(tasks[0]) + "\n", '["foreign"]'),
        ]
        module = importlib.import_module("processor")
        for journal, checkpoint in cases:
            with tempfile.TemporaryDirectory(prefix="sonn-corrupt-") as directory:
                state = Path(directory)
                (state / "effects.jsonl").write_text(journal, encoding="utf-8")
                if checkpoint is not None:
                    (state / "checkpoint.json").write_text(checkpoint, encoding="utf-8")
                before = {path.name: path.read_bytes() for path in state.iterdir()}
                try:
                    module.run(state, tasks)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Corrupt durable state was accepted")
                require(before == {path.name: path.read_bytes() for path in state.iterdir()},
                        "Recovery wrote effects before validating existing state")
    return [("interruption_boundary", boundary), ("explicit_resume_exactly_once", resume),
            ("corrupt_state_fails_closed", corrupt_state)]


def main():
    scenario_id, directory = sys.argv[1:]
    root = Path(directory).resolve()
    scenario = json.loads(Path(__file__).with_name("scenarios.json").read_text(
        encoding="utf-8"))[scenario_id]
    sys.path.insert(0, str(root))
    factory = {"independent_investigation": investigation, "csv_export": csv_checks,
               "serial_control": serial_checks, "interruption_recovery": recovery_checks}[scenario_id]
    checks = [("preserved_inputs", lambda: preserved_inputs(root, scenario)), *factory(root)]
    results = []
    for name, check in checks:
        try:
            check()
        except Exception as exc:
            results.append({"name": name, "passed": False,
                            "detail": f"{type(exc).__name__}: {exc}"})
        else:
            results.append({"name": name, "passed": True, "detail": "Behavior observed"})
    print(json.dumps(results))


if __name__ == "__main__":
    main()

"""Trusted scripted native-child harness; never selected by the live runner CLI.

Only this injected test backend reads the reference pack. Reference files are
outside the granted project and never supplied as model inputs or file tools.
"""
import json
import os
from pathlib import Path

from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


REFERENCES = json.loads((Path(__file__).parent / "swarming" / "references.json").read_text(encoding="utf-8"))


class FixtureBackend(StreamingBackend):
    def stream(self, **kwargs):
        if self.model == "explicit-scripted-crash":
            # This trusted fault fixture exits after real host request admission.
            # It is separate from processor.py's application-resume exercise.
            os._exit(37)
        if not self.stream_count:
            prompt = "\n".join(str(row.get("content", "")) for row in kwargs["conversation_history"] if row.get("role") == "user")
            if "Assigned finding IDs: " in prompt:
                identifiers = prompt.split("Assigned finding IDs: ", 1)[1].split(". Include only", 1)[0].split(", ")
                findings = [row for row in json.loads(REFERENCES["independent_investigation"]["report.json"])["findings"] if row["id"] in identifiers]
                self._scripts = [
                    [*[tool_call("file_read", {"path": row["source"]}, f"read-{index}") for index, row in enumerate(findings)], done(model=self.model)],
                    [text_delta(json.dumps({"findings": findings})), done(model=self.model)],
                ]
            else:
                roots = prompt.split("Your assigned write scope: ", 1)[1].split(". Implement only", 1)[0].split(", ")
                scenario = next(key for key, values in REFERENCES.items() if set(roots) <= set(values))
                self._scripts = [
                    [*[tool_call("file_write", {"path": root, "content": REFERENCES[scenario][root]}, f"write-{index}") for index, root in enumerate(roots)], done(model=self.model)],
                    [text_delta("Scripted fixture implementation retained for host checks."), done(model=self.model)],
                ]
        yield from super().stream(**kwargs)


def factory(spec):
    return FixtureBackend(name=spec.backend_type, model=spec.model)


if __name__ == "__main__":
    raise SystemExit(main(backend_factory=factory))

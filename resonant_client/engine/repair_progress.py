"""Recognize successful edit reversals without mistaking reads for stalled work."""

import hashlib
import os
from collections import OrderedDict, deque


class RepairProgress:
    def __init__(self):
        self.paths = OrderedDict()
        self.warned = set()

    def observe(self, name, arguments, *, is_error):
        if name != "file_edit" or is_error:
            return ""
        path = arguments.get("path")
        old, new = arguments.get("old_text"), arguments.get("new_text")
        if not path or not isinstance(old, str) or not isinstance(new, str) or old == new:
            return ""
        key = os.path.normcase(os.path.normpath(str(path)))
        edits = self.paths.setdefault(key, deque(maxlen=4))
        self.paths.move_to_end(key)
        if len(self.paths) > 64:
            expired, _ = self.paths.popitem(last=False)
            self.warned.discard(expired)
        edits.append(tuple(hashlib.sha256(s.encode()).hexdigest() for s in (old, new)))
        if len(edits) != 4 or key in self.warned:
            return ""
        a, b, c, d = edits
        if a != c or b != d or a != b[::-1]:
            return ""
        self.warned.add(key)
        return str(path)


def recovery_guidance(path):
    return (
        f"[Repair progress] Four successful edits to {path} have alternated between "
        "the same two text states. Reversing the edit again is not evidence of a repair. "
        "Inspect the current failure and state transitions, identify one falsifiable "
        "cause, and run a small check that distinguishes the two explanations before "
        "another edit. Preserve assertions; do not weaken tests to obtain a pass. "
        "For browser features, exercise the actual UI event and its observable result; "
        "a direct backend test cannot establish that the browser sends the event. "
        "Continue the task using the resulting evidence."
    )

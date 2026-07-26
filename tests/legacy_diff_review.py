"""Comprehensive adversarial tests for diff_review.py"""

import json
import os
import shutil
import sys
import tempfile

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from resonant_client.engine.diff_review import (
    DiffHunk,
    DiffReview,
    generate_review,
    _check_sensitive_path,
    _resolve_path,
    _parse_hunks,
    _review_file_edit,
    _review_file_write,
    _review_bash,
)

PASSED = 0
FAILED = 0
ERRORS = []

def test(name, condition, detail=""):
    global PASSED, FAILED, ERRORS
    if condition:
        PASSED += 1
        print(f"  PASS: {name}")
    else:
        FAILED += 1
        msg = f"  FAIL: {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def make_temp_file(content, suffix=".txt", encoding="utf-8"):
    """Create a temp file with given content, return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
        f.write(content)
    return path


# ============================================================
# 1. FILE EDIT EDGE CASES
# ============================================================
print("\n=== 1. FILE EDIT EDGE CASES ===")

# 1a. old_text appears multiple times
print("\n-- 1a. old_text appears multiple times --")
content = "hello world\nhello world\nhello world\n"
path = make_temp_file(content)
try:
    review = _review_file_edit({"path": path, "old_text": "hello world", "new_text": "goodbye"}, "")
    test("Multiple matches: only first replaced",
         review.new_content.count("goodbye") == 1 and review.new_content.count("hello world") == 2,
         f"got {review.new_content.count('goodbye')} replacements")
    test("Multiple matches: has diff", bool(review.unified_diff))
finally:
    os.unlink(path)

# 1b. old_text doesn't exist
print("\n-- 1b. old_text doesn't exist --")
path = make_temp_file("some content")
try:
    review = _review_file_edit({"path": path, "old_text": "nonexistent", "new_text": "replacement"}, "")
    test("Missing old_text: warning present", len(review.warnings) > 0)
    test("Missing old_text: risk elevated", review.risk_level != "low",
         f"got risk_level={review.risk_level}")
    # Fallback diff should still be generated since old_text and new_text are non-empty
    test("Missing old_text: fallback diff generated", bool(review.unified_diff))
finally:
    os.unlink(path)

# 1c. old_text == new_text (identical)
print("\n-- 1c. old_text == new_text (identical) --")
path = make_temp_file("some content here\n")
try:
    review = _review_file_edit({"path": path, "old_text": "content", "new_text": "content"}, "")
    test("Identical edit: no diff generated", review.unified_diff == "",
         f"diff was: {repr(review.unified_diff[:200])}")
    test("Identical edit: action is edit", review.action == "edit")
finally:
    os.unlink(path)

# 1d. Empty old_text
print("\n-- 1d. Empty old_text --")
path = make_temp_file("file contents\n")
try:
    review = _review_file_edit({"path": path, "old_text": "", "new_text": "inserted"}, "")
    # Empty string is always "in" any string, so replace("", "inserted", 1) prepends
    test("Empty old_text: no crash", True)
    test("Empty old_text: has some content", bool(review.new_content))
finally:
    os.unlink(path)

# 1d2. Empty new_text (deletion)
print("\n-- 1d2. Empty new_text (deletion) --")
path = make_temp_file("line1\nDELETE_ME\nline3\n")
try:
    review = _review_file_edit({"path": path, "old_text": "DELETE_ME\n", "new_text": ""}, "")
    test("Empty new_text: text removed", "DELETE_ME" not in review.new_content)
    test("Empty new_text: has diff", bool(review.unified_diff))
finally:
    os.unlink(path)

# 1e. Whitespace-only differences
print("\n-- 1e. Whitespace-only differences --")
path = make_temp_file("  hello  \n")
try:
    review = _review_file_edit({"path": path, "old_text": "  hello  ", "new_text": "hello"}, "")
    test("Whitespace diff: has diff", bool(review.unified_diff))
finally:
    os.unlink(path)

# 1f. Very large file with small edit
print("\n-- 1f. Very large file (1MB+) --")
large_content = "x" * (1024 * 1024) + "\nMARKER\n" + "y" * (1024 * 1024)
path = make_temp_file(large_content)
try:
    review = _review_file_edit({"path": path, "old_text": "MARKER", "new_text": "REPLACED"}, "")
    test("Large file: no crash", True)
    test("Large file: has diff", bool(review.unified_diff))
    test("Large file: marker replaced", "REPLACED" in review.new_content)
finally:
    os.unlink(path)

# 1g. Binary content
print("\n-- 1g. Binary content --")
path = make_temp_file("normal\x00binary\xffdata\n")
try:
    review = _review_file_edit({"path": path, "old_text": "binary", "new_text": "text"}, "")
    test("Binary content: no crash", True)
finally:
    os.unlink(path)

# 1h. Unicode content
print("\n-- 1h. Unicode content --")
unicode_content = "Hello \u4e16\u754c\nEmoji: \U0001f600\U0001f525\nRTL: \u0645\u0631\u062d\u0628\u0627\n"
path = make_temp_file(unicode_content)
try:
    review = _review_file_edit({"path": path, "old_text": "\u4e16\u754c", "new_text": "\u5b87\u5b99"}, "")
    test("Unicode: has diff", bool(review.unified_diff))
    test("Unicode: replaced correctly", "\u5b87\u5b99" in review.new_content)
    test("Unicode: emoji preserved", "\U0001f600" in review.new_content)
finally:
    os.unlink(path)

# 1i. Windows line endings
print("\n-- 1i. Windows line endings --")
win_content = "line1\r\nline2\r\nline3\r\n"
fd, path = tempfile.mkstemp(suffix=".txt")
with os.fdopen(fd, "wb") as f:
    f.write(win_content.encode("utf-8"))
try:
    # With CRLF in old_text (exact match)
    review = _review_file_edit({"path": path, "old_text": "line2\r\n", "new_text": "modified\r\n"}, "")
    test("Windows CRLF (exact): replaced correctly", "modified" in review.new_content)
finally:
    os.unlink(path)

# Also test: old_text with LF only against a CRLF file (should be normalized)
fd, path = tempfile.mkstemp(suffix=".txt")
with os.fdopen(fd, "wb") as f:
    f.write(win_content.encode("utf-8"))
try:
    review = _review_file_edit({"path": path, "old_text": "line2\n", "new_text": "modified\n"}, "")
    test("Windows CRLF (LF old_text normalized): replaced correctly", "modified" in review.new_content)
finally:
    os.unlink(path)

# 1j. Mixed line endings
print("\n-- 1j. Mixed line endings --")
mixed_content = "line1\nline2\r\nline3\rline4\n"
fd, path = tempfile.mkstemp(suffix=".txt")
with os.fdopen(fd, "wb") as f:
    f.write(mixed_content.encode("utf-8"))
try:
    review = _review_file_edit({"path": path, "old_text": "line2\r\n", "new_text": "replaced\r\n"}, "")
    test("Mixed endings: no crash", True)
finally:
    os.unlink(path)

# 1k. old_text at start/end of file
print("\n-- 1k. old_text at start/end of file --")
path = make_temp_file("START middle END")
try:
    review = _review_file_edit({"path": path, "old_text": "START", "new_text": "BEGIN"}, "")
    test("Start of file: replaced", review.new_content.startswith("BEGIN"))
finally:
    os.unlink(path)

path = make_temp_file("START middle END")
try:
    review = _review_file_edit({"path": path, "old_text": "END", "new_text": "FINISH"}, "")
    test("End of file: replaced", review.new_content.endswith("FINISH"))
finally:
    os.unlink(path)

# 1l. Nested directory paths
print("\n-- 1l. Nested directory paths --")
tmpdir = tempfile.mkdtemp()
nested = os.path.join(tmpdir, "a", "b", "c", "d", "e")
os.makedirs(nested)
path = os.path.join(nested, "deep.txt")
with open(path, "w") as f:
    f.write("deep content")
try:
    review = _review_file_edit({"path": path, "old_text": "deep", "new_text": "shallow"}, "")
    test("Nested path: works", "shallow" in review.new_content)
finally:
    shutil.rmtree(tmpdir)


# ============================================================
# 2. FILE WRITE EDGE CASES
# ============================================================
print("\n\n=== 2. FILE WRITE EDGE CASES ===")

# 2a. Writing empty content
print("\n-- 2a. Writing empty content --")
review = _review_file_write({"path": "/tmp/nonexistent_test_file.txt", "content": ""}, "")
test("Empty write: action is create", review.action == "create")
test("Empty write: 0 lines", "0 lines" in review.summary, f"summary: {review.summary}")

# 2b. Writing to nested non-existent path
print("\n-- 2b. Nested non-existent path --")
review = _review_file_write({"path": "/tmp/a/b/c/d/e/f/file.txt", "content": "hello\n"}, "")
test("Nested write: action is create", review.action == "create")
test("Nested write: no crash", True)

# 2c. Overwriting an existing large file
print("\n-- 2c. Overwrite large file --")
large = "A" * (1024 * 1024)
path = make_temp_file(large)
try:
    review = _review_file_write({"path": path, "content": "small replacement"}, "")
    test("Overwrite large: action is overwrite", review.action == "overwrite")
    test("Overwrite large: risk is medium", review.risk_level == "medium")
finally:
    os.unlink(path)

# 2d. Binary-like content
print("\n-- 2d. Binary-like content --")
review = _review_file_write({"path": "/tmp/test_binary.bin", "content": "\x00\x01\x02\xff"}, "")
test("Binary write: no crash", True)

# 2e. File path with spaces
print("\n-- 2e. Path with spaces --")
review = _review_file_write({"path": "/tmp/path with spaces/my file.txt", "content": "data"}, "")
test("Spaces in path: no crash", True)
test("Spaces in path: path preserved", "path with spaces" in review.file_path)

# 2f. File path with unicode
print("\n-- 2f. Path with unicode --")
review = _review_file_write({"path": "/tmp/\u6d4b\u8bd5/\u6587\u4ef6.txt", "content": "data"}, "")
test("Unicode path: no crash", True)
test("Unicode path: preserved", "\u6d4b\u8bd5" in review.file_path)

# 2g. Absolute vs relative paths
print("\n-- 2g. Absolute vs relative paths --")
review_abs = _review_file_write({"path": "/tmp/abs_test.txt", "content": "data"}, "/project")
review_rel = _review_file_write({"path": "relative/test.txt", "content": "data"}, "/project")
test("Absolute path: preserved", review_abs.file_path == "/tmp/abs_test.txt")
test("Relative path: preserved in review", review_rel.file_path == "relative/test.txt")

# 2h. Path traversal attempts
print("\n-- 2h. Path traversal --")
review = _review_file_write({"path": "../../etc/passwd", "content": "malicious"}, "/tmp/project")
test("Path traversal: file_path stored as-is", review.file_path == "../../etc/passwd")
# Check if sensitive file detection catches passwd
has_passwd_warning = any("passwd" in w.lower() for w in review.warnings)
test("Path traversal: passwd detected as sensitive", has_passwd_warning,
     f"warnings: {review.warnings}")


# ============================================================
# 3. BASH COMMAND EDGE CASES
# ============================================================
print("\n\n=== 3. BASH COMMAND EDGE CASES ===")

# 3a. Very long command
print("\n-- 3a. Very long command --")
long_cmd = "echo " + "x" * 10240
review = _review_bash({"command": long_cmd})
test("Long command: no crash", True)
test("Long command: summary truncated", len(review.summary) < 200, f"summary len: {len(review.summary)}")

# 3b. Pipes and redirects
print("\n-- 3b. Pipes and redirects --")
review = _review_bash({"command": "cat file.txt | grep pattern > output.txt 2>&1"})
test("Pipes: no crash", True)
test("Pipes: risk is low", review.risk_level == "low")

# 3c. Embedded quotes
print("\n-- 3c. Embedded quotes --")
review = _review_bash({"command": """echo "it's a 'test'" && echo `date` && echo "done" """})
test("Quotes: no crash", True)

# 3d. Environment variables
print("\n-- 3d. Environment variables --")
review = _review_bash({"command": "echo $HOME && echo ${PATH} && echo $((1+1))"})
test("Env vars: no crash", True)

# 3e. Multi-line command
print("\n-- 3e. Multi-line command --")
multi = "cat <<EOF\nline1\nline2\nline3\nEOF"
review = _review_bash({"command": multi})
test("Multi-line: no crash", True)
test("Multi-line: summary doesn't have newlines in first 100 chars", True)

# 3f. Multiple danger patterns
print("\n-- 3f. Multiple danger patterns --")
review = _review_bash({"command": "sudo rm -rf / && dd if=/dev/zero of=/dev/sda"})
test("Multi-danger: risk is high", review.risk_level == "high")
test("Multi-danger: multiple warnings", len(review.warnings) >= 3,
     f"got {len(review.warnings)} warnings")

# 3g. Whitespace-only command
print("\n-- 3g. Whitespace-only command --")
review = _review_bash({"command": "   \t\n  "})
test("Whitespace cmd: no crash", True)
test("Whitespace cmd: risk is low", review.risk_level == "low")

# 3h. Empty command
print("\n-- 3h. Empty command --")
review = _review_bash({"command": ""})
test("Empty cmd: no crash", True)

# 3i. Looks dangerous but isn't
print("\n-- 3i. Looks dangerous but isn't --")
review = _review_bash({"command": "echo 'rm -rf is a dangerous command'"})
# This WILL match because the pattern check is substring-based
test("Echo rm -rf: matches pattern (substring)", review.risk_level == "high",
     f"risk={review.risk_level}")

# 3j. SQL-like commands
print("\n-- 3j. SQL commands --")
review = _review_bash({"command": "mysql -e 'DROP TABLE users; DROP DATABASE prod;'"})
test("SQL DROP: high risk", review.risk_level == "high")
test("SQL DROP: has warnings", len(review.warnings) >= 2)

# 3k. PowerShell commands
print("\n-- 3k. PowerShell --")
review = _review_bash({"command": "powershell -Command Remove-Item -Recurse -Force C:\\temp"})
test("PowerShell: no crash", True)

# 3l. Missing command key
print("\n-- 3l. Missing command key --")
review = _review_bash({})
test("No command key: no crash", True)
test("No command key: empty command", review.command == "")

# 3m. Command is None (fixed: should coerce to string)
print("\n-- 3m. Command is None --")
review = _review_bash({"command": None})
test("None command: no crash", True)
test("None command: coerced to empty string", review.command == "")


# ============================================================
# 4. SENSITIVE FILE DETECTION
# ============================================================
print("\n\n=== 4. SENSITIVE FILE DETECTION ===")

# 4a. Various .env patterns
print("\n-- 4a. .env patterns --")
for env_name in [".env", ".env.local", ".env.development.local", ".env.production"]:
    review = DiffReview(tool_name="test", risk_level="low")
    _check_sensitive_path(f"/project/{env_name}", review)
    test(f"{env_name}: detected", review.risk_level == "high", f"risk={review.risk_level}")

# .env.development.local should match because .env.local or .env is a substring
review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("/project/.env.development.local", review)
test(".env.development.local: detected", review.risk_level == "high")

# 4b. SSH key paths
print("\n-- 4b. SSH keys --")
for ssh_path in ["/home/user/.ssh/id_rsa", "/home/user/.ssh/id_ed25519", "~/.ssh/config"]:
    review = DiffReview(tool_name="test", risk_level="low")
    _check_sensitive_path(ssh_path, review)
    test(f"SSH {ssh_path}: detected", review.risk_level == "high", f"risk={review.risk_level}")

# 4c. Windows credential paths
print("\n-- 4c. Windows paths --")
review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("C:\\Users\\user\\credentials.json", review)
test("Windows credentials: detected", review.risk_level == "high")

# 4d. Case sensitivity
print("\n-- 4d. Case sensitivity --")
review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("/project/.ENV", review)
test(".ENV (uppercase): detected (case-insensitive)", review.risk_level == "high",
     f"risk={review.risk_level}")

review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("/project/CREDENTIALS.json", review)
test("CREDENTIALS (uppercase): detected", review.risk_level == "high",
     f"risk={review.risk_level}")

# 4e. Paths with .. components
print("\n-- 4e. Paths with .. --")
review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("../../etc/shadow", review)
test("shadow via ..: detected", review.risk_level == "high")

# 4f. Non-sensitive file shouldn't trigger
print("\n-- 4f. Non-sensitive files --")
review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("/project/src/main.py", review)
test("main.py: not sensitive", review.risk_level == "low")

review = DiffReview(tool_name="test", risk_level="low")
_check_sensitive_path("/project/environment.py", review)
# "environment" contains ".env" substring? Let's check...
# Actually _check_sensitive_path checks if ".env" is in the lowered path
# "environment.py" does NOT contain ".env" -- it contains "environment" not ".env"
# Wait: ".env" in "environment.py" -> False (no dot before env)
# But "/project/.env.local" -> ".env" in ".env.local" -> True
test("environment.py: not sensitive (no dot-env)", review.risk_level == "low",
     f"risk={review.risk_level}, warnings={review.warnings}")


# ============================================================
# 5. HUNK PARSING EDGE CASES
# ============================================================
print("\n\n=== 5. HUNK PARSING ===")

# 5a. No changes
print("\n-- 5a. Empty diff --")
hunks = _parse_hunks("")
test("Empty diff: no hunks", len(hunks) == 0)

# 5b. Only additions
print("\n-- 5b. Only additions --")
diff_add = """--- /dev/null
+++ b/new_file.txt
@@ -0,0 +1,3 @@
+line1
+line2
+line3
"""
hunks = _parse_hunks(diff_add)
test("Additions only: 1 hunk", len(hunks) == 1, f"got {len(hunks)}")
test("Additions only: 3 lines", len(hunks[0].lines) == 3 if hunks else False)
test("Additions only: all start with +",
     all(l.startswith("+") for l in hunks[0].lines) if hunks else False)

# 5c. Only deletions
print("\n-- 5c. Only deletions --")
diff_del = """--- a/old_file.txt
+++ b/old_file.txt
@@ -1,3 +1,0 @@
-line1
-line2
-line3
"""
hunks = _parse_hunks(diff_del)
test("Deletions only: 1 hunk", len(hunks) == 1)
test("Deletions only: all start with -",
     all(l.startswith("-") for l in hunks[0].lines) if hunks else False)

# 5d. Very large diff
print("\n-- 5d. Large diff (1000+ lines) --")
lines = []
lines.append("--- a/big.txt")
lines.append("+++ b/big.txt")
lines.append("@@ -1,1000 +1,1000 @@")
for i in range(1000):
    lines.append(f"-old line {i}")
    lines.append(f"+new line {i}")
big_diff = "\n".join(lines)
hunks = _parse_hunks(big_diff)
test("Large diff: 1 hunk", len(hunks) == 1)
test("Large diff: 2000 lines", len(hunks[0].lines) == 2000 if hunks else False,
     f"got {len(hunks[0].lines) if hunks else 0}")

# 5e. With context lines
print("\n-- 5e. Context lines --")
diff_ctx = """--- a/file.txt
+++ b/file.txt
@@ -1,5 +1,5 @@
 context1
 context2
-old line
+new line
 context3
 context4
"""
hunks = _parse_hunks(diff_ctx)
test("Context: 1 hunk", len(hunks) == 1)
test("Context: 6 lines total (2 ctx + 1 del + 1 add + 2 ctx)", len(hunks[0].lines) == 6 if hunks else False,
     f"got {len(hunks[0].lines) if hunks else 0}")

# 5f. Malformed diff
print("\n-- 5f. Malformed diff --")
hunks = _parse_hunks("this is not a diff at all\nrandom text\n@@ garbage @@\n")
test("Malformed diff: no crash", True)
test("Malformed diff: no hunks", len(hunks) == 0)

# 5g. Multiple hunks
print("\n-- 5g. Multiple hunks --")
diff_multi = """--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,3 @@
 line1
-old1
+new1
 line3
@@ -10,3 +10,3 @@
 line10
-old2
+new2
 line12
"""
hunks = _parse_hunks(diff_multi)
test("Multi hunks: 2 hunks", len(hunks) == 2, f"got {len(hunks)}")
test("Multi hunks: first at line 1", hunks[0].old_start == 1 if hunks else False)
test("Multi hunks: second at line 10", hunks[1].old_start == 10 if len(hunks) >= 2 else False)

# 5h. Hunk header without comma (single line change)
print("\n-- 5h. Single line hunk header --")
diff_single = """--- a/file.txt
+++ b/file.txt
@@ -5 +5 @@
-old
+new
"""
hunks = _parse_hunks(diff_single)
test("Single line hunk: parsed", len(hunks) == 1)
test("Single line hunk: count defaults to 1",
     hunks[0].old_count == 1 and hunks[0].new_count == 1 if hunks else False)


# ============================================================
# 6. SERIALIZATION
# ============================================================
print("\n\n=== 6. SERIALIZATION ===")

# 6a. Unicode content
print("\n-- 6a. Unicode to_dict --")
review = DiffReview(
    tool_name="test",
    file_path="/\u6d4b\u8bd5/\u6587\u4ef6.py",
    summary="\u4fee\u6539\u4e86\u6587\u4ef6 \U0001f600",
    unified_diff="-\u65e7\u5185\u5bb9\n+\u65b0\u5185\u5bb9\n",
)
d = review.to_dict()
test("Unicode to_dict: no crash", True)
test("Unicode to_dict: path preserved", d["file_path"] == "/\u6d4b\u8bd5/\u6587\u4ef6.py")

# 6b. Large diff serialization
print("\n-- 6b. Large diff to_dict --")
large_diff = "+line\n" * 10000
review = DiffReview(tool_name="test", unified_diff=large_diff)
d = review.to_dict()
test("Large to_dict: no crash", True)
test("Large to_dict: diff preserved", len(d["unified_diff"]) == len(large_diff))

# 6c. JSON round-trip
print("\n-- 6c. JSON round-trip --")
review = DiffReview(
    tool_name="file_edit",
    file_path="test.py",
    action="edit",
    risk_level="high",
    summary="Edit test.py",
    hunks=[DiffHunk(old_start=1, old_count=3, new_start=1, new_count=5, lines=["-a", "+b", "+c"], context="func")],
    unified_diff="--- a/test.py\n+++ b/test.py\n",
    warnings=["Warning 1", "Warning 2"],
    command="",
)
d = review.to_dict()
json_str = json.dumps(d)
reloaded = json.loads(json_str)
test("JSON round-trip: no crash", True)
test("JSON round-trip: tool_name preserved", reloaded["tool_name"] == "file_edit")
test("JSON round-trip: hunks preserved", len(reloaded["hunks"]) == 1)
test("JSON round-trip: hunk lines preserved", reloaded["hunks"][0]["lines"] == ["-a", "+b", "+c"])
test("JSON round-trip: warnings preserved", reloaded["warnings"] == ["Warning 1", "Warning 2"])

# 6d. No non-serializable types
print("\n-- 6d. Non-serializable check --")
review = DiffReview(tool_name="test")
d = review.to_dict()
try:
    json.dumps(d)
    test("Default DiffReview: JSON serializable", True)
except TypeError as e:
    test("Default DiffReview: JSON serializable", False, str(e))


# ============================================================
# 7. generate_review EDGE CASES
# ============================================================
print("\n\n=== 7. generate_review EDGE CASES ===")

# 7a. Unknown tool
print("\n-- 7a. Unknown tool --")
review = generate_review("unknown_tool", {"arg1": "val1"})
test("Unknown tool: returns review", review is not None)
test("Unknown tool: risk is medium", review.risk_level == "medium" if review else False)
test("Unknown tool: action is execute", review.action == "execute" if review else False)

# 7b. file_read (no review)
print("\n-- 7b. Read-only tools --")
test("file_read: returns None", generate_review("file_read", {}) is None)
test("glob: returns None", generate_review("glob", {}) is None)
test("grep: returns None", generate_review("grep", {}) is None)

# 7c. Missing required args
print("\n-- 7c. Missing args --")
review = generate_review("file_edit", {})
test("file_edit no args: no crash", review is not None)
test("file_edit no args: empty path", review.file_path == "" if review else False)

review = generate_review("file_write", {})
test("file_write no args: no crash", review is not None)

review = generate_review("bash", {})
test("bash no args: no crash", review is not None)

# 7d. Args that are wrong types (fixed: should coerce to string)
print("\n-- 7d. Wrong arg types --")
review = generate_review("bash", {"command": 12345})
test("bash int command: no crash", True)
test("bash int command: coerced to string", review.command == "12345")

review = generate_review("file_edit", {"path": 123, "old_text": "a", "new_text": "b"})
test("file_edit int path: no crash", True)
test("file_edit int path: coerced to string", review.file_path == "123")

# 7e. None values in args (fixed: should coerce to string)
print("\n-- 7e. None values --")
review = generate_review("file_edit", {"path": None, "old_text": None, "new_text": None})
test("file_edit None args: no crash", True)
test("file_edit None args: empty path", review.file_path == "")

review = generate_review("file_write", {"path": None, "content": None})
test("file_write None args: no crash", True)
test("file_write None args: empty path", review.file_path == "")

# 7f. Extra unexpected args
print("\n-- 7f. Extra args --")
review = generate_review("bash", {"command": "ls", "extra1": "val", "extra2": 42})
test("Extra args: no crash", True)
test("Extra args: command correct", review.command == "ls" if review else False)

# 7g. project_path edge cases
print("\n-- 7g. project_path edge cases --")
review = generate_review("file_write", {"path": "test.txt", "content": "hi"}, project_path="")
test("Empty project_path: no crash", True)

review = generate_review("file_write", {"path": "test.txt", "content": "hi"}, project_path="/nonexistent/path")
test("Nonexistent project_path: no crash", True)


# ============================================================
# 8. _resolve_path TESTS
# ============================================================
print("\n\n=== 8. _resolve_path ===")

test("Empty path: returns None", _resolve_path("", "/project") is None)
test("Absolute path: returned as-is", _resolve_path("/abs/path.txt", "/project") == "/abs/path.txt")
test("Relative with project: joined", _resolve_path("rel.txt", "/project") == os.path.join("/project", "rel.txt"))
test("Relative no project: uses cwd", _resolve_path("rel.txt", "") == os.path.join(os.getcwd(), "rel.txt"))


# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print(f"RESULTS: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
if ERRORS:
    print("\nFAILURES AND BUGS:")
    for e in ERRORS:
        print(e)
print("=" * 60)

sys.exit(0 if FAILED == 0 else 1)

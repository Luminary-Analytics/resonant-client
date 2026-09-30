"""Hold a file open from another process, as an antivirus scan, a sync client or an editor does (Windows).

    python hold_open.py <file> <share mode>

Opens <file> for reading with CreateFileW and the share mode given
(FILE_SHARE_READ 1, FILE_SHARE_WRITE 2, FILE_SHARE_DELETE 4, added up), prints
"open" and keeps it open until stdin closes. Prints "failed <error>" and exits
1 when it can't open it. Used by tests/test_first_run_polish.py and
tests/first_run.browser.cjs.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80


def main() -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(sys.argv[1], GENERIC_READ, int(sys.argv[2]), None, OPEN_EXISTING,
                                  FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        print("failed", ctypes.get_last_error(), flush=True)
        sys.exit(1)
    print("open", flush=True)
    sys.stdin.read()  # until the test closes it; the handle closes as the process ends


if __name__ == "__main__":
    main()

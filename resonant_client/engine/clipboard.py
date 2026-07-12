"""
Platform-specific clipboard reader for both images and text.

- Image: used by the multimodal Ctrl+V paste path (Windows, macOS, Linux).
- Text:  exposed as `clipboard_read` / `clipboard_write` agent tools so the
         agent can stash content across calls without going through the FS.
"""

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from resonant_client.processes import background_process_kwargs


# ── Text clipboard ─────────────────────────────────────────────────────


def read_clipboard_text() -> str:
    """
    Return the current clipboard text, or '' if empty / non-text / unavailable.
    Uses pyperclip (cross-platform); falls back to OS-specific tools.
    """
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste() or ""
    except ImportError:
        pass
    except Exception:
        return ""

    # Fallbacks
    try:
        if sys.platform == "win32":
            # PowerShell Get-Clipboard
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
                **background_process_kwargs(),
            )
            return (result.stdout or "").rstrip("\r\n")
        elif sys.platform == "darwin":
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5,
                                    encoding="utf-8", errors="replace")
            return result.stdout or ""
        else:
            for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"], ["wl-paste"]):
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                                            encoding="utf-8", errors="replace")
                    if result.returncode == 0:
                        return result.stdout or ""
                except FileNotFoundError:
                    continue
            return ""
    except Exception:
        return ""


def write_clipboard_text(text: str) -> None:
    """
    Replace the current clipboard contents with `text`. Raises on failure.
    """
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text or "")
        return
    except ImportError:
        pass

    if sys.platform == "win32":
        # Use clip.exe — pipe text via stdin
        proc = subprocess.run(
            ["clip"], input=text, text=True, encoding="utf-8",
            **background_process_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError("clip.exe failed")
    elif sys.platform == "darwin":
        proc = subprocess.run(["pbcopy"], input=text, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError("pbcopy failed")
    else:
        # Try wl-copy, xclip, xsel in order
        last_err: Optional[Exception] = None
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
            try:
                proc = subprocess.run(cmd, input=text, text=True, encoding="utf-8")
                if proc.returncode == 0:
                    return
            except FileNotFoundError as e:
                last_err = e
                continue
        raise RuntimeError(f"no clipboard utility available (install xclip, xsel, or wl-clipboard): {last_err}")


# ── Tool wrappers (ToolResult shape) ────────────────────────────────────


def exec_clipboard_read(args: dict, start: float):
    """Tool: clipboard_read — returns current text contents."""
    from .tools import ToolResult
    text = read_clipboard_text()
    if not text:
        return ToolResult("(clipboard is empty or non-text)", elapsed=time.time() - start, metadata={"chars": 0})
    return ToolResult(text, elapsed=time.time() - start, metadata={"chars": len(text)})


def exec_clipboard_write(args: dict, start: float):
    """Tool: clipboard_write — replaces clipboard contents with given text."""
    from .tools import ToolResult
    text = args.get("text")
    if text is None:
        return ToolResult("`text` is required.", is_error=True, elapsed=time.time() - start)
    if not isinstance(text, str):
        text = str(text)
    try:
        write_clipboard_text(text)
    except Exception as exc:
        return ToolResult(f"Clipboard write failed: {exc}", is_error=True, elapsed=time.time() - start)
    return ToolResult(
        f"Copied {len(text)} chars to clipboard",
        elapsed=time.time() - start,
        metadata={"chars": len(text)},
    )


# ── Image clipboard (existing) ─────────────────────────────────────────


def read_clipboard_image() -> tuple[Optional[bytes], str]:
    """
    Read an image from the system clipboard.

    Returns:
        (image_bytes, media_type) if an image was found
        (None, "") if no image in clipboard
    """
    if sys.platform == "win32":
        return _read_windows()
    elif sys.platform == "darwin":
        return _read_macos()
    else:
        return _read_linux()


def image_to_base64(image_bytes: bytes) -> str:
    """Convert image bytes to base64 string."""
    return base64.b64encode(image_bytes).decode("utf-8")


def image_size_label(image_bytes: bytes) -> str:
    """Human-readable size label for image bytes."""
    size = len(image_bytes)
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


def _read_windows() -> tuple[Optional[bytes], str]:
    """
    Read clipboard image on Windows using PowerShell.

    Uses System.Windows.Forms.Clipboard to extract the image
    and save as PNG to a temp file.
    """
    tmp = os.path.join(tempfile.gettempdir(), "resonant_clipboard.png")

    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img -ne $null) {{
    $img.Save('{tmp}', [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output 'OK'
}} else {{
    Write-Output 'NONE'
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            **background_process_kwargs(),
        )
        output = result.stdout.strip()
        if output == "OK" and os.path.exists(tmp):
            image_bytes = Path(tmp).read_bytes()
            os.unlink(tmp)
            if len(image_bytes) > 0:
                return (image_bytes, "image/png")
        return (None, "")
    except Exception:
        return (None, "")


def _read_macos() -> tuple[Optional[bytes], str]:
    """
    Read clipboard image on macOS using osascript.

    Extracts PNGf class from clipboard and writes to temp file.
    """
    tmp = os.path.join(tempfile.gettempdir(), "resonant_clipboard.png")

    applescript = f'''
        try
            set imgData to the clipboard as «class PNGf»
            set outFile to open for access POSIX file "{tmp}" with write permission
            write imgData to outFile
            close access outFile
            return "OK"
        on error
            return "NONE"
        end try
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if output == "OK" and os.path.exists(tmp):
            image_bytes = Path(tmp).read_bytes()
            os.unlink(tmp)
            if len(image_bytes) > 0:
                return (image_bytes, "image/png")
        return (None, "")
    except Exception:
        return (None, "")


def _read_linux() -> tuple[Optional[bytes], str]:
    """
    Read clipboard image on Linux.

    Tries Wayland (wl-paste) first, then X11 (xclip).
    """
    # Try Wayland first
    try:
        result = subprocess.run(
            ["wl-paste", "--type", "image/png"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0 and len(result.stdout) > 0:
            return (result.stdout, "image/png")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fall back to X11
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0 and len(result.stdout) > 0:
            return (result.stdout, "image/png")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return (None, "")

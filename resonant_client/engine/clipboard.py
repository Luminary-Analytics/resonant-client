"""
Platform-specific clipboard image reader.

Reads images from the system clipboard for multimodal input.
Supports Windows, macOS, and Linux (Wayland + X11).
"""

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


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

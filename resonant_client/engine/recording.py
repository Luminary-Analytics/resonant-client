"""
Screen recording for the Resonant Engine.

Provides a `Recorder` class that captures the screen (or a region/monitor) to
an MP4 file. Used by the `screen_record_start` / `screen_record_stop` tools.

Encoding:
- Preferred: opencv-python (cv2.VideoWriter, mp4v fourcc) — pure-Python install.
- Fallback:  ffmpeg subprocess (image2pipe → mp4) when cv2 is unavailable.

Concurrency:
- Only one active recorder per process. start() while already recording is a
  no-op that returns the existing path.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from resonant_client.processes import background_process_kwargs

from .tools import ToolResult


_RECORDINGS_DIR = Path.home() / ".resonant" / "recordings"


class Recorder:
    """Background-thread screen recorder. One active per process."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._output_path: Optional[Path] = None
        self._fps: int = 10
        self._monitor_index: int = 0
        self._region: Optional[dict] = None
        self._error: Optional[str] = None
        self._frames_written: int = 0
        self._started_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        output_path: Optional[Path] = None,
        fps: int = 10,
        monitor: int = 0,
        region: Optional[dict] = None,
    ) -> Path:
        with self._lock:
            if self.is_active:
                return self._output_path

            _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            if output_path is None:
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                output_path = _RECORDINGS_DIR / f"recording-{ts}.mp4"

            self._output_path = output_path
            self._fps = max(1, min(int(fps or 10), 30))
            self._monitor_index = int(monitor or 0)
            self._region = region
            self._error = None
            self._frames_written = 0
            self._started_at = time.time()
            self._stop_event.clear()

            self._thread = threading.Thread(target=self._loop, daemon=True, name="screen-recorder")
            self._thread.start()
            return self._output_path

    def stop(self, *, timeout: float = 5.0) -> dict:
        with self._lock:
            if not self.is_active:
                return {"error": "no active recording"}

            self._stop_event.set()
            t = self._thread
        if t:
            t.join(timeout=timeout)

        with self._lock:
            duration = time.time() - self._started_at
            path = self._output_path
            frames = self._frames_written
            err = self._error
            self._thread = None

        if err:
            return {"error": err, "path": str(path) if path else "", "frames": frames, "duration_seconds": round(duration, 2)}
        size_mb = round(path.stat().st_size / 1024 / 1024, 2) if path and path.exists() else 0.0
        return {
            "path": str(path),
            "duration_seconds": round(duration, 2),
            "frames": frames,
            "size_mb": size_mb,
        }

    # ── Internal ──

    def _loop(self) -> None:
        try:
            import mss
        except ImportError:
            self._error = "mss not installed (pip install mss)"
            return

        try:
            import numpy as np  # noqa: F401  (used by cv2)
        except ImportError:
            pass

        # Try cv2 first; fall back to ffmpeg.
        encoder = self._make_cv2_encoder() or self._make_ffmpeg_encoder()
        if encoder is None:
            self._error = "no MP4 encoder available (install opencv-python or ffmpeg)"
            return

        period = 1.0 / self._fps
        try:
            with mss.mss() as sct:
                # Resolve target region
                if self._region is not None:
                    monitor = {
                        "left": int(self._region.get("x", 0)),
                        "top": int(self._region.get("y", 0)),
                        "width": int(self._region.get("width", 800)),
                        "height": int(self._region.get("height", 600)),
                    }
                else:
                    monitors = sct.monitors[1:]
                    idx = self._monitor_index
                    if not (0 <= idx < len(monitors)):
                        idx = 0
                    monitor = monitors[idx]

                next_tick = time.time()
                while not self._stop_event.is_set():
                    sct_img = sct.grab(monitor)
                    encoder.write_frame(sct_img)
                    self._frames_written += 1
                    next_tick += period
                    sleep = next_tick - time.time()
                    if sleep > 0:
                        time.sleep(sleep)
                    else:
                        next_tick = time.time()
        except Exception as exc:
            self._error = f"recorder loop error: {exc}"
        finally:
            try:
                encoder.close()
            except Exception:
                pass

    def _make_cv2_encoder(self):
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            return None

        class _CVEncoder:
            def __init__(self, path: Path, fps: int):
                self.path = path
                self.fps = fps
                self.writer = None

            def write_frame(self, sct_img):
                # mss returns BGRA; cv2 wants BGR
                arr = np.array(sct_img, dtype=np.uint8)  # shape (H, W, 4)
                bgr = arr[:, :, :3]
                if self.writer is None:
                    h, w = bgr.shape[:2]
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    self.writer = cv2.VideoWriter(str(self.path), fourcc, self.fps, (w, h))
                self.writer.write(bgr)

            def close(self):
                if self.writer is not None:
                    self.writer.release()
                    self.writer = None

        return _CVEncoder(self._output_path, self._fps)

    def _make_ffmpeg_encoder(self):
        # Check ffmpeg present
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                timeout=3,
                check=False,
                **background_process_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        class _FFmpegEncoder:
            def __init__(self, path: Path, fps: int):
                self.path = path
                self.fps = fps
                self.proc = None
                self._size = None

            def write_frame(self, sct_img):
                if self.proc is None:
                    h, w = sct_img.height, sct_img.width
                    self._size = (w, h)
                    self.proc = subprocess.Popen([
                        "ffmpeg", "-y",
                        "-f", "rawvideo", "-vcodec", "rawvideo",
                        "-pix_fmt", "bgra",
                        "-s", f"{w}x{h}",
                        "-r", str(self.fps),
                        "-i", "-",
                        "-vcodec", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                        str(self.path),
                    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **background_process_kwargs())
                try:
                    self.proc.stdin.write(bytes(sct_img.bgra))
                except (BrokenPipeError, OSError):
                    pass

            def close(self):
                if self.proc is not None:
                    try:
                        self.proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        self.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()

        return _FFmpegEncoder(self._output_path, self._fps)


# ── Singleton + tool wrappers ──────────────────────────────────────────


_RECORDER = Recorder()


def exec_screen_record_start(args: dict, start: float) -> ToolResult:
    if _RECORDER.is_active:
        return ToolResult(
            f"Already recording: {_RECORDER._output_path}",
            elapsed=time.time() - start,
            metadata={"already_active": True, "path": str(_RECORDER._output_path or "")},
        )

    fps = args.get("fps", 10)
    monitor = args.get("monitor", 0)
    region = args.get("region")
    try:
        path = _RECORDER.start(fps=int(fps), monitor=int(monitor), region=region)
    except Exception as exc:
        return ToolResult(f"Failed to start recording: {exc}", is_error=True, elapsed=time.time() - start)

    return ToolResult(
        f"Recording to {path} (fps={fps})",
        elapsed=time.time() - start,
        metadata={"path": str(path), "fps": int(fps), "monitor": int(monitor)},
    )


def exec_screen_record_stop(args: dict, start: float) -> ToolResult:
    data = _RECORDER.stop()
    if data.get("error"):
        return ToolResult(data["error"], is_error=True, elapsed=time.time() - start, metadata=data)
    return ToolResult(
        f"Saved {data['path']} ({data['duration_seconds']}s, {data['frames']} frames, {data['size_mb']}MB)",
        elapsed=time.time() - start,
        metadata=data,
    )

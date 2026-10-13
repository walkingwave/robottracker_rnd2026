"""Frame sources: live cameras and video files, with timestamps."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np


def _backend() -> int:
    if sys.platform == "win32":
        return cv2.CAP_DSHOW  # opens in ~1 s; MSMF can take 10+
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


class CameraSource:
    """Live camera. A background thread always holds the newest frame, so
    processing never falls behind a buffered queue of stale frames."""

    kind = "camera"

    def __init__(self, index: int, width: int | None = None, height: int | None = None,
                 fps: int | None = None):
        self.index = index
        cap = cv2.VideoCapture(index, _backend())
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Camera {index} could not be opened (in use, unplugged, or no permission).")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if width and height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps:
            cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise RuntimeError(f"Camera {index} opened but returned no image.")
        self._cap = cap
        self.size = (frame.shape[1], frame.shape[0])
        self.nominal_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.label = f"Camera {index} ({self.size[0]}x{self.size[1]})"
        self.paused = False
        self.ended = False
        self.first_frame = frame
        self._cond = threading.Condition()
        self._latest: tuple[np.ndarray, float] | None = (frame, time.monotonic())
        self._seq = 1
        self._read_seq = 0
        self._running = True
        self._thread = threading.Thread(target=self._grab, daemon=True, name="camera")
        self._thread.start()

    def _grab(self) -> None:
        failures = 0
        while self._running:
            ok, frame = self._cap.read()
            t = time.monotonic()
            if not ok or frame is None:
                failures += 1
                if failures > 50:
                    self.ended = True
                    break
                time.sleep(0.02)
                continue
            failures = 0
            with self._cond:
                self._latest = (frame, t)
                self._seq += 1
                self._cond.notify_all()

    def read(self, timeout: float = 0.5) -> tuple[np.ndarray, float] | None:
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > self._read_seq or not self._running, timeout):
                return None
            self._read_seq = self._seq
            return self._latest

    def play(self) -> None:
        pass

    def rewind(self) -> None:
        pass

    def close(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2)
        self._cap.release()


class FileSource:
    """Video file. Holds on the first frame until play(); timestamps come from
    the file, and playback is paced to real time unless realtime=False."""

    kind = "file"

    def __init__(self, path: str | Path, realtime: bool = True):
        self.path = Path(path)
        if not self.path.exists():
            raise RuntimeError(f"Video file not found: {self.path}")
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video file: {self.path}")
        ok, frame = cap.read()
        if not ok:
            cap.release()
            raise RuntimeError(f"Video file has no readable frames: {self.path}")
        self._cap = cap
        self.realtime = realtime
        self.nominal_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.size = (frame.shape[1], frame.shape[0])
        self.label = self.path.name
        self.first_frame = frame
        self.paused = True
        self.ended = False
        self._idx = 0
        self._wall_t0: float | None = None
        self.rewind()

    def read(self, timeout: float = 0.5) -> tuple[np.ndarray, float] | None:
        if self.paused or self.ended:
            time.sleep(min(timeout, 0.05))
            return None
        ok, frame = self._cap.read()
        if not ok:
            self.ended = True
            return None
        t = self._idx / self.nominal_fps
        self._idx += 1
        if self.realtime:
            now = time.monotonic()
            if self._wall_t0 is None:
                self._wall_t0 = now - t
            delay = self._wall_t0 + t - now
            if delay > 0:
                time.sleep(delay)
        return frame, t

    def play(self) -> None:
        self.paused = False
        self._wall_t0 = None

    def rewind(self) -> None:
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._idx = 0
        self.ended = False
        self._wall_t0 = None

    def close(self) -> None:
        self._cap.release()


def _macos_camera_names() -> list[str]:
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType", "-json"],
                             capture_output=True, text=True, timeout=10).stdout
        return [d.get("_name", "?") for d in json.loads(out).get("SPCameraDataType", [])]
    except Exception:
        return []


def list_cameras(max_index: int = 6, timeout_s: float = 4.0) -> list[dict]:
    """Probe camera indices. Each probe has a timeout so a wedged device can't hang the scan."""
    names = _macos_camera_names()
    found = []
    misses = 0
    for i in range(max_index):
        result: dict = {}

        def probe(i=i, result=result):
            cap = cv2.VideoCapture(i, _backend())
            if cap.isOpened():
                ok, frame = cap.read()
                result["size"] = (frame.shape[1], frame.shape[0]) if ok and frame is not None else None
            cap.release()

        th = threading.Thread(target=probe, daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            found.append({"index": i, "label": f"Camera {i} (busy or no permission)", "ok": False})
            continue
        if "size" not in result:
            misses += 1
            if misses >= 2:
                break
            continue
        name = names[i] if i < len(names) else f"Camera {i}"
        size = result["size"]
        res = f"{size[0]}x{size[1]}" if size else "no signal"
        found.append({"index": i, "label": f"{i}: {name} ({res})", "ok": size is not None})
    return found

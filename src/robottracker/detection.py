"""Per-frame rover detectors.

MotionDetector: difference against an image of the empty maze, in the
top-down view. Needs no changes to the rover; the background is captured from
the live camera with one click.

MarkerDetector: finds a printed ArUco marker on the rover. Immune to shadows,
people and lighting, and also gives heading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config import TrackerConfig
from .geometry import MazeGeometry


@dataclass
class Detection:
    x_mm: float
    y_mm: float
    area_mm2: float
    heading_deg: float | None = None
    marker_id: int | None = None


def _ellipse(size: int) -> np.ndarray:
    size = max(1, int(size)) | 1  # odd
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))


class MotionDetector:
    """Foreground blobs in the top-down view, filtered by real-world size."""

    def __init__(self, cfg: TrackerConfig, geometry: MazeGeometry, background: np.ndarray | None):
        self.cfg = cfg
        self.geometry = geometry
        mpp = geometry.mm_per_px
        rover_area = cfg.rover_size_mm**2
        self.min_area_mm2 = cfg.min_area_ratio * rover_area
        self.max_area_mm2 = cfg.max_area_ratio * rover_area
        self._open = _ellipse(round(15 / mpp))
        self._close = _ellipse(round(cfg.rover_size_mm / 6 / mpp))
        self._bg: np.ndarray | None = None
        self._mog: cv2.BackgroundSubtractor | None = None
        self.last_mask: np.ndarray | None = None
        self.set_background(background)

    @property
    def has_background(self) -> bool:
        return self._bg is not None

    def set_background(self, background: np.ndarray | None) -> None:
        w, h = self.geometry.top_size
        if background is not None and background.shape[:2] != (h, w):
            background = cv2.resize(background, (w, h), interpolation=cv2.INTER_AREA)
        self._bg = None if background is None else cv2.GaussianBlur(background, (5, 5), 0).astype(
            np.float32
        )
        self._mog = None

    def background_image(self) -> np.ndarray | None:
        return None if self._bg is None else self._bg.astype(np.uint8)

    @staticmethod
    def build_background(top_frames: list[np.ndarray]) -> np.ndarray:
        """Per-pixel median of several frames: removes noise and anything that moved."""
        return np.median(np.stack(top_frames), axis=0).astype(np.uint8)

    def foreground(self, top: np.ndarray) -> np.ndarray:
        blur = cv2.GaussianBlur(top, (5, 5), 0)
        if self._bg is None:
            # No empty-maze reference yet: fall back to an adaptive model. Works
            # while the rover moves, but a captured background is far better.
            if self._mog is None:
                self._mog = cv2.createBackgroundSubtractorMOG2(
                    history=300, varThreshold=32, detectShadows=False
                )
            mask = self._mog.apply(blur)
        else:
            diff = cv2.absdiff(blur, self._bg.astype(np.uint8))
            diff = diff.max(axis=2) if diff.ndim == 3 else diff
            # A global lighting change shifts the whole difference image; the
            # median measures that shift so it can be ignored.
            offset = int(np.median(diff))
            _, mask = cv2.threshold(diff, self.cfg.diff_threshold + offset, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._close)
        return mask

    def detect(self, top: np.ndarray) -> list[Detection]:
        mask = self.foreground(top)
        self.last_mask = mask
        mpp = self.geometry.mm_per_px
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in contours:
            area_mm2 = cv2.contourArea(c) * mpp * mpp
            if not (self.min_area_mm2 <= area_mm2 <= self.max_area_mm2):
                continue
            m = cv2.moments(c)
            if m["m00"] <= 0:
                continue
            out.append(Detection(m["m10"] / m["m00"] * mpp, m["m01"] / m["m00"] * mpp, area_mm2))
        return out

    def local_measurement(self, x_mm: float, y_mm: float, radius_mm: float) -> Detection | None:
        """Foreground centroid within a disc around (x, y).

        When something (a hand, an arm) touches the rover, the blobs merge and
        the whole-blob centroid is dragged toward it. Looking only near the
        predicted rover position keeps the measurement on the rover.
        """
        if self.last_mask is None:
            return None
        mpp = self.geometry.mm_per_px
        r = int(radius_mm / mpp)
        cx, cy = int(x_mm / mpp), int(y_mm / mpp)
        h, w = self.last_mask.shape
        x0, y0, x1, y1 = max(0, cx - r), max(0, cy - r), min(w, cx + r + 1), min(h, cy + r + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        win = self.last_mask[y0:y1, x0:x1]
        disc = np.zeros_like(win)
        cv2.circle(disc, (cx - x0, cy - y0), r, 255, -1)
        m = cv2.moments(cv2.bitwise_and(win, disc), binaryImage=True)
        if m["m00"] <= 0:
            return None
        return Detection(
            (x0 + m["m10"] / m["m00"]) * mpp, (y0 + m["m01"] / m["m00"]) * mpp, m["m00"] * mpp * mpp
        )

    def adapt(self, top: np.ndarray, exclude_mm: list[tuple[float, float]]) -> None:
        """Let the background follow slow lighting drift, except around the rover."""
        rate = self.cfg.background_adapt_rate
        if self._bg is None or rate <= 0:
            return
        mpp = self.geometry.mm_per_px
        mask = np.full(top.shape[:2], 255, np.uint8)
        radius = int(self.cfg.rover_size_mm * 1.2 / mpp)
        for x, y in exclude_mm:
            cv2.circle(mask, (int(x / mpp), int(y / mpp)), radius, 0, -1)
        blur = cv2.GaussianBlur(top, (5, 5), 0).astype(np.float32)
        cv2.accumulateWeighted(blur, self._bg, rate, mask=mask)


class MarkerDetector:
    """ArUco marker detection on the full-resolution camera frame."""

    def __init__(self, cfg: TrackerConfig, geometry: MazeGeometry):
        self.cfg = cfg
        self.geometry = geometry
        try:
            dict_id = getattr(cv2.aruco, cfg.aruco_dictionary)
        except AttributeError as exc:
            raise ValueError(f"Unknown ArUco dictionary: {cfg.aruco_dictionary}") from exc
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self._detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(dict_id), params)
        self.last_corners_px: list[np.ndarray] = []

    def detect(self, frame: np.ndarray) -> list[Detection]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _ = self._detector.detectMarkers(gray)
        self.last_corners_px = []
        out = []
        if ids is None:
            return out
        for c, marker_id in zip(corners, ids.flatten()):
            if self.cfg.aruco_id is not None and int(marker_id) != self.cfg.aruco_id:
                continue
            px = c.reshape(4, 2)
            self.last_corners_px.append(px)
            mm = self.geometry.px_to_mm(px)
            cx, cy = mm.mean(axis=0)
            # Marker corners are TL, TR, BR, BL of the printed marker: "forward"
            # points from the bottom edge to the top edge.
            fx, fy = (mm[0] + mm[1]) / 2 - (mm[3] + mm[2]) / 2
            heading = math.degrees(math.atan2(-fy, fx))  # CCW from +x, y-up convention
            x, y = mm[:, 0], mm[:, 1]
            area = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
            out.append(Detection(float(cx), float(cy), float(area), heading, int(marker_id)))
        return out

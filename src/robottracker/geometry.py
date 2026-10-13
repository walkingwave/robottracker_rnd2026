"""Camera calibration and the pixel <-> maze (mm) mapping.

The maze floor is a plane, so four clicked corners define a homography from
camera pixels to millimetres. Tracking itself runs on a top-down "warped"
image of the floor, so every size and distance is in real units no matter
what camera, resolution or mounting height is used.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

CORNER_NAMES = ("top-left", "top-right", "bottom-right", "bottom-left")


def atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class Calibration:
    """Four maze corners clicked in a camera image of a known size."""

    corners_px: list[tuple[float, float]]  # TL, TR, BR, BL
    image_size: tuple[int, int]  # (width, height) of the image the corners were clicked on

    def __post_init__(self) -> None:
        self.corners_px = [(float(x), float(y)) for x, y in self.corners_px]
        self.image_size = (int(self.image_size[0]), int(self.image_size[1]))
        validate_corners(self.corners_px)

    def scaled_to(self, size: tuple[int, int]) -> list[tuple[float, float]]:
        """Corners for a frame of a different resolution (same camera position)."""
        sx = size[0] / self.image_size[0]
        sy = size[1] / self.image_size[1]
        return [(x * sx, y * sy) for x, y in self.corners_px]

    def to_dict(self) -> dict:
        return {"corners_px": [list(c) for c in self.corners_px], "image_size": list(self.image_size)}

    @classmethod
    def from_dict(cls, d: dict) -> Calibration:
        return cls(corners_px=[tuple(c) for c in d["corners_px"]], image_size=tuple(d["image_size"]))

    def save(self, path: Path) -> None:
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> Calibration | None:
        if not path.exists():
            return None
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def validate_corners(corners: list[tuple[float, float]]) -> None:
    """Reject corner sets that can't be a correctly ordered maze outline."""
    if len(corners) != 4:
        raise ValueError("Calibration needs exactly 4 corners.")
    pts = np.asarray(corners, dtype=np.float64)
    # Shoelace area. Image y points down, so a visually clockwise outline
    # (TL -> TR -> BR -> BL) has positive area.
    x, y = pts[:, 0], pts[:, 1]
    area = 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    if abs(area) < 100:
        raise ValueError("Corners are too close together.")
    if area < 0:
        raise ValueError(
            "Corners are in the wrong order. Click top-left, top-right, "
            "bottom-right, bottom-left (clockwise)."
        )
    if not cv2.isContourConvex(pts.astype(np.float32).reshape(-1, 1, 2)):
        raise ValueError("Corners don't form a convex outline. Check the click order.")


class MazeGeometry:
    """Pixel <-> mm mapping plus the top-down warp used for tracking."""

    def __init__(
        self,
        corners_px: list[tuple[float, float]],
        length_mm: float,
        width_mm: float,
        mm_per_px: float,
    ):
        validate_corners(corners_px)
        self.length_mm = float(length_mm)
        self.width_mm = float(width_mm)
        self.mm_per_px = float(mm_per_px)
        self.corners_px = np.asarray(corners_px, dtype=np.float64)

        world = np.array(
            [[0, 0], [length_mm, 0], [length_mm, width_mm], [0, width_mm]], dtype=np.float64
        )
        self.H = cv2.getPerspectiveTransform(
            self.corners_px.astype(np.float32), world.astype(np.float32)
        ).astype(np.float64)
        self.H_inv = np.linalg.inv(self.H)

        scale = np.diag([1.0 / mm_per_px, 1.0 / mm_per_px, 1.0])
        self.H_top = scale @ self.H
        self.top_size = (
            int(math.ceil(length_mm / mm_per_px)),
            int(math.ceil(width_mm / mm_per_px)),
        )

    @staticmethod
    def _apply(H: np.ndarray, pts) -> np.ndarray:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        homog = np.hstack([arr, np.ones((len(arr), 1))]) @ H.T
        return homog[:, :2] / homog[:, 2:3]

    def px_to_mm(self, pts) -> np.ndarray:
        """Camera pixels -> maze mm. Accepts (x, y) or an (N, 2) array."""
        return self._apply(self.H, pts)

    def mm_to_px(self, pts) -> np.ndarray:
        return self._apply(self.H_inv, pts)

    def warp(self, frame: np.ndarray) -> np.ndarray:
        """Top-down view of the maze floor; 1 pixel = mm_per_px millimetres."""
        return cv2.warpPerspective(frame, self.H_top, self.top_size, flags=cv2.INTER_LINEAR)

    def contains(self, x_mm: float, y_mm: float, margin_mm: float = 0.0) -> bool:
        return (
            -margin_mm <= x_mm <= self.length_mm + margin_mm
            and -margin_mm <= y_mm <= self.width_mm + margin_mm
        )

    def nadir_mm(self, frame_size: tuple[int, int] | None = None) -> tuple[float, float]:
        """Floor point directly below the camera.

        The optical centre is unknown, so the image centre stands in for it
        (good for an overhead mount); without a frame size, the maze centre.
        """
        if frame_size is None:
            return self.length_mm / 2, self.width_mm / 2
        x, y = self.px_to_mm((frame_size[0] / 2, frame_size[1] / 2))[0]
        return float(x), float(y)

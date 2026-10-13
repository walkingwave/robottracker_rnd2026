"""Configuration: one YAML file, validated on load.

Every key is optional; anything left out uses the defaults below. Unknown keys
are rejected so typos surface immediately instead of being silently ignored.
Relative paths are resolved against the directory containing the config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MazeConfig(_Model):
    # x runs along the length, y along the width; origin is the top-left corner
    # as clicked during calibration.
    length_mm: float = Field(2438.4, gt=0)
    width_mm: float = Field(1219.2, gt=0)
    # Interior wall blocks as [x0, y0, x1, y1] in mm. Used for drawing only.
    walls: list[tuple[float, float, float, float]] = Field(default_factory=list)


class CameraConfig(_Model):
    source: Literal["camera", "file", "none"] = "camera"
    index: int = 0
    file: str = ""
    width: int | None = 1280
    height: int | None = 720
    fps: int | None = 30
    # Play video files at their natural speed (False = as fast as possible).
    realtime: bool = True


class TrackerConfig(_Model):
    # motion: background difference against the empty maze (no rover changes needed)
    # aruco:  a printed ArUco marker on top of each rover (most robust, gives heading)
    mode: Literal["motion", "aruco"] = "motion"
    # Resolution of the top-down working image. Lower = finer but slower.
    mm_per_px: float = Field(3.0, gt=0)
    # Approximate rover footprint (side of a square), used to size the blob filters.
    rover_size_mm: float = Field(250.0, gt=0)
    min_area_ratio: float = 0.15
    max_area_ratio: float = 3.0
    # Colour difference (0-255) above which a pixel counts as "not background".
    diff_threshold: int = Field(35, ge=1, le=255)
    # How fast the background model follows slow lighting changes (0 disables).
    background_adapt_rate: float = Field(0.02, ge=0, le=1)
    # Max distance (mm) between predicted and measured position to accept a match.
    gate_mm: float = 300.0
    # How long to coast on prediction before declaring the rover lost.
    max_coast_s: float = 1.5
    # Consecutive consistent detections needed to lock on without operator help.
    confirm_frames: int = Field(3, ge=1)
    # Kalman filter tuning.
    accel_noise_mms2: float = 1000.0
    measurement_noise_mm: float = 8.0
    # ArUco settings.
    aruco_dictionary: str = "DICT_4X4_50"
    aruco_id: int | None = None
    # Parallax correction: the camera sees the top of the rover, not its footprint.
    # Set both to correct positions away from the image centre.
    camera_height_mm: float | None = None
    target_height_mm: float = 0.0


class MetricsConfig(_Model):
    # Movement smaller than this is treated as jitter when summing distance.
    min_step_mm: float = 40.0
    # Window for speed estimates (top speed, live speed).
    speed_window_s: float = 0.5
    # Speed above which the rover counts as moving.
    moving_speed_mms: float = 40.0


class ScoringConfig(_Model):
    # Seconds added to the run time per logged collision class / intervention.
    collision_penalty_s: dict[int, float] = Field(default_factory=lambda: {1: 2.0, 2: 5.0, 3: 10.0})
    intervention_penalty_s: float = 15.0


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = 8000
    # Operator controls are always allowed from this computer. Set a token to also
    # allow them from other devices: open /operator?token=<token> once.
    operator_token: str | None = None
    stream_fps: int = 15
    stream_max_width: int = 1280
    jpeg_quality: int = Field(75, ge=10, le=100)
    # Show the camera feed on the public dashboard (off by default for privacy).
    public_video: bool = False


class PathsConfig(_Model):
    trials_dir: str = "trials"
    setup_dir: str = "setup"
    record_video: bool = True


class AppConfig(_Model):
    maze: MazeConfig = Field(default_factory=MazeConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else self._base_dir / p

    @property
    def trials_dir(self) -> Path:
        return self.resolve(self.paths.trials_dir)

    @property
    def setup_dir(self) -> Path:
        return self.resolve(self.paths.setup_dir)


def load_config(path: str | Path | None = None, base_dir: str | Path | None = None) -> AppConfig:
    """Load config from YAML. A missing default config.yaml means 'all defaults'."""
    data: dict = {}
    if path is not None:
        path = Path(path)
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        elif path.name != "config.yaml":
            raise FileNotFoundError(f"Config file not found: {path}")
        if base_dir is None:
            base_dir = path.resolve().parent
    cfg = AppConfig.model_validate(data)
    cfg._base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
    return cfg

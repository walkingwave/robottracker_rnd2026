"""Constant-velocity Kalman filter in maze coordinates (mm, mm/s)."""

from __future__ import annotations

import numpy as np

_H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])
_I4 = np.eye(4)


class ConstantVelocityKF:
    """State [x, y, vx, vy]. Handles variable frame intervals."""

    def __init__(self, x: float, y: float, accel_noise: float, meas_noise: float):
        self.x = np.array([x, y, 0.0, 0.0])
        self.P = np.diag([meas_noise**2, meas_noise**2, 500.0**2, 500.0**2])
        self.q = accel_noise**2
        self.R = np.eye(2) * meas_noise**2

    def predict(self, dt: float) -> None:
        if dt <= 0:
            return
        F = _I4.copy()
        F[0, 2] = F[1, 3] = dt
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + (G @ G.T) * self.q

    def update(self, zx: float, zy: float) -> None:
        z = np.array([zx, zy])
        S = _H @ self.P @ _H.T + self.R
        K = self.P @ _H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - _H @ self.x)
        # Joseph form keeps P symmetric and positive definite.
        A = _I4 - K @ _H
        self.P = A @ self.P @ A.T + K @ self.R @ K.T

    def damp_velocity(self, factor: float) -> None:
        """Shrink velocity while coasting so a lost rover doesn't drift off."""
        self.x[2:] *= factor

    @property
    def position(self) -> tuple[float, float]:
        return float(self.x[0]), float(self.x[1])

    @property
    def velocity(self) -> tuple[float, float]:
        return float(self.x[2]), float(self.x[3])

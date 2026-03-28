"""
cerberus/anatomy/kinematics.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Digital Anatomy — Go2 Kinematic Model

Models:
  • 12-DOF joint model (4 legs × 3 joints: hip_ab, hip_flex, knee)
  • Center of Mass (COM) tracking
  • Support polygon and stability margin
  • Energy consumption estimation
  • Fatigue accumulation per joint
  • Stress / load awareness

Go2 joint layout (per leg):
  FL/FR/RL/RR hip abductor  → index 0,3,6,9
  FL/FR/RL/RR hip flexor    → index 1,4,7,10
  FL/FR/RL/RR knee          → index 2,5,8,11

Joint limits (radians) from Unitree SDK:
  Hip abductor:  [-1.047, 1.047]  (±60°)
  Hip flexor:    [-3.490, 1.745]  (-200° to 100°)
  Knee:          [-0.524, 4.189]  (-30° to 240°)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import RobotState

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Go2 URDF link lengths (meters) - from unitree_ros go2_description
L_HIP   = 0.0955   # hip to thigh pivot
L_THIGH = 0.213    # thigh length
L_CALF  = 0.213    # calf length

# Go2 mass (kg)
ROBOT_MASS = 15.0

# Gravity
G = 9.81

# Leg indices
LEGS = ["FL", "FR", "RL", "RR"]

# Joint limits [min, max] radians
JOINT_LIMITS = {
    "hip_ab":   (-1.047, 1.047),
    "hip_flex": (-3.490, 1.745),
    "knee":     (-0.524, 4.189),
}


# ── Joint model ────────────────────────────────────────────────────────────────

@dataclass
class JointState:
    name: str
    position: float = 0.0     # radians
    velocity: float = 0.0     # rad/s
    torque: float   = 0.0     # Nm
    temperature: float = 25.0 # °C (estimated)
    fatigue: float  = 0.0     # 0.0–1.0 accumulator

    @property
    def limits(self) -> tuple[float, float]:
        if "hip_ab" in self.name:
            return JOINT_LIMITS["hip_ab"]
        if "hip_flex" in self.name or "thigh" in self.name:
            return JOINT_LIMITS["hip_flex"]
        return JOINT_LIMITS["knee"]

    @property
    def at_limit(self) -> bool:
        lo, hi = self.limits
        return self.position <= lo + 0.05 or self.position >= hi - 0.05

    @property
    def power_w(self) -> float:
        """Instantaneous power consumption (W)."""
        return abs(self.torque * self.velocity)

    def update_fatigue(self, dt: float) -> None:
        """Accumulate fatigue based on torque and velocity."""
        intensity = min(1.0, abs(self.torque) / 20.0)  # 20Nm nominal max
        recovery  = 0.001 * dt  # slow recovery
        strain    = intensity * abs(self.velocity) * dt * 0.01
        self.fatigue = max(0.0, min(1.0, self.fatigue + strain - recovery))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "position_deg": round(math.degrees(self.position), 2),
            "velocity_rads": round(self.velocity, 3),
            "torque_nm": round(self.torque, 2),
            "temperature_c": round(self.temperature, 1),
            "fatigue": round(self.fatigue, 3),
            "at_limit": self.at_limit,
        }


# ── Foot position (FK) ────────────────────────────────────────────────────────

@dataclass
class FootPosition:
    leg: str
    x: float = 0.0  # forward
    y: float = 0.0  # lateral
    z: float = 0.0  # vertical (up positive)
    contact: bool = False
    force: float = 0.0  # N

    def to_dict(self) -> dict:
        return {"leg": self.leg, "pos": [self.x, self.y, self.z],
                "contact": self.contact, "force_n": round(self.force, 2)}


def forward_kinematics(hip_ab: float, hip_flex: float, knee: float,
                        side: str = "L") -> tuple[float, float, float]:
    """
    3-DOF per-leg forward kinematics.
    Returns (x_fwd, y_lat, z_vert) of foot in body frame.
    Sign of y flipped for right legs.
    """
    sign = 1.0 if side == "L" else -1.0

    # Hip abductor rotation (around x-axis)
    y_hip = sign * L_HIP * math.cos(hip_ab)
    z_hip = -L_HIP * math.sin(hip_ab)

    # Hip flexor + knee (planar in sagittal plane)
    x_foot = L_THIGH * math.sin(hip_flex) + L_CALF * math.sin(hip_flex + knee)
    z_foot = -(L_THIGH * math.cos(hip_flex) + L_CALF * math.cos(hip_flex + knee))

    return (x_foot, y_hip, z_hip + z_foot)


# ── COM and stability ─────────────────────────────────────────────────────────

def compute_com(feet: list[FootPosition], weights: list[float] | None = None) -> tuple[float, float, float]:
    """
    Estimate COM from foot positions (approximation).
    weights: relative mass contribution per leg (default equal).
    """
    if not feet:
        return (0.0, 0.0, 0.27)
    w = weights or [1.0] * len(feet)
    total = sum(w)
    x = sum(f.x * w[i] for i, f in enumerate(feet)) / total
    y = sum(f.y * w[i] for i, f in enumerate(feet)) / total
    z = sum(f.z * w[i] for i, f in enumerate(feet)) / total
    return (x, y, z)


def support_polygon(feet: list[FootPosition]) -> list[tuple[float, float]]:
    """Return the 2D convex hull (x, y) of feet that are in contact."""
    contact_feet = [(f.x, f.y) for f in feet if f.contact]
    if len(contact_feet) < 3:
        return contact_feet
    # Simple convex hull
    pts = sorted(contact_feet)
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def stability_margin(com: tuple[float, float, float],
                     polygon: list[tuple[float, float]]) -> float:
    """Minimum distance from projected COM to support polygon edges (m)."""
    if len(polygon) < 3:
        return 0.0
    cx, cy = com[0], com[1]
    n = len(polygon)
    min_dist = float("inf")
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        # Distance from (cx,cy) to segment (a,b)
        dx, dy = bx - ax, by - ay
        if dx == dy == 0:
            dist = math.hypot(cx - ax, cy - ay)
        else:
            t = ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            px, py = ax + t * dx, ay + t * dy
            dist = math.hypot(cx - px, cy - py)
        min_dist = min(min_dist, dist)
    return min_dist


# ── Energy model ──────────────────────────────────────────────────────────────

class EnergyModel:
    """Estimate total power draw and project remaining runtime."""

    def __init__(self, battery_capacity_wh: float = 100.0):
        self._capacity_wh = battery_capacity_wh
        self._consumed_wh = 0.0
        self._last_update  = time.monotonic()
        self._idle_power_w = 30.0   # Go2 idle ~30W
        self._motion_power_w = 0.0

    def update(self, joints: list[JointState], dt: float) -> None:
        joint_power = sum(j.power_w for j in joints)
        self._motion_power_w = joint_power
        total_power = self._idle_power_w + joint_power
        self._consumed_wh += total_power * dt / 3600.0

    @property
    def total_power_w(self) -> float:
        return self._idle_power_w + self._motion_power_w

    @property
    def remaining_wh(self) -> float:
        return max(0.0, self._capacity_wh - self._consumed_wh)

    @property
    def estimated_runtime_min(self) -> float:
        if self.total_power_w <= 0:
            return float("inf")
        return (self.remaining_wh / self.total_power_w) * 60.0

    def to_dict(self) -> dict:
        return {
            "total_power_w": round(self.total_power_w, 1),
            "motion_power_w": round(self._motion_power_w, 1),
            "consumed_wh": round(self._consumed_wh, 3),
            "remaining_wh": round(self.remaining_wh, 2),
            "estimated_runtime_min": round(self.estimated_runtime_min, 1),
        }


# ── Digital Anatomy Manager ────────────────────────────────────────────────────

class DigitalAnatomy:
    """
    Main anatomy model. Updated every engine tick.

    Attach to engine:
        engine.anatomy = DigitalAnatomy()
    """

    def __init__(self):
        joint_names = [
            "FL_hip_ab", "FL_hip_flex", "FL_knee",
            "FR_hip_ab", "FR_hip_flex", "FR_knee",
            "RL_hip_ab", "RL_hip_flex", "RL_knee",
            "RR_hip_ab", "RR_hip_flex", "RR_knee",
        ]
        self.joints = [JointState(name=n) for n in joint_names]
        self.feet   = [FootPosition(leg=leg) for leg in LEGS]
        self.energy = EnergyModel()
        self._last_update = time.monotonic()
        self.com        = (0.0, 0.0, 0.27)
        self.stability  = 0.0
        self.polygon: list = []

    async def update(self, state: "RobotState") -> None:
        now = time.monotonic()
        dt  = min(now - self._last_update, 0.1)  # cap dt at 100ms
        self._last_update = now

        # Sync joint data from robot state
        for i, j in enumerate(self.joints):
            if i < len(state.joint_positions):
                j.position = state.joint_positions[i]
                j.velocity = state.joint_velocities[i]
                j.torque   = state.joint_torques[i]
            j.update_fatigue(dt)

        # Forward kinematics per leg
        for leg_idx, leg in enumerate(LEGS):
            base = leg_idx * 3
            hip_ab, hip_flex, knee = (
                self.joints[base].position,
                self.joints[base + 1].position,
                self.joints[base + 2].position,
            )
            side = "L" if "L" in leg else "R"
            x, y, z = forward_kinematics(hip_ab, hip_flex, knee, side)
            foot = self.feet[leg_idx]
            foot.x = x
            foot.y = y
            foot.z = z
            foot.force = state.foot_force[leg_idx] if leg_idx < len(state.foot_force) else 0.0
            foot.contact = foot.force > 5.0  # 5N threshold

        # COM and stability
        self.com      = compute_com(self.feet)
        self.polygon  = support_polygon(self.feet)
        self.stability = stability_margin(self.com, self.polygon)

        # Energy
        self.energy.update(self.joints, dt)

    def status(self) -> dict:
        return {
            "joints": [j.to_dict() for j in self.joints],
            "feet": [f.to_dict() for f in self.feet],
            "com": {"x": round(self.com[0], 3), "y": round(self.com[1], 3), "z": round(self.com[2], 3)},
            "stability_margin_m": round(self.stability, 4),
            "energy": self.energy.to_dict(),
            "max_fatigue": round(max(j.fatigue for j in self.joints), 3),
        }

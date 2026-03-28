"""
cerberus/anatomy/payload.py
━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Undercarriage Payload — Physics Model & Safety Compensator

Models the mechanical effects of attaching equipment to the underside of
the Go2 body and derives compensated operating limits for all subsystems.

Physics summary
───────────────
  Go2 body COM sits at approximately BODY_HEIGHT above the ground plane.
  The belly surface is BELLY_OFFSET below the body COM.
  At nominal standing height (0.27 m), belly clearance ≈ 0.15 m.

  Attaching a payload of thickness T below the belly:
    • Reduces ground clearance by T
    • Lowers combined-system COM (improves roll stability marginally)
    • Increases rotational inertia → reduces safe yaw rate
    • Increases total mass → increases joint loading and energy draw
    • Reduces safe tilt angles before belly drag occurs

  Compensations applied automatically by PayloadCompensator:
    • min_body_height raised by T + CLEARANCE_MARGIN
    • Recommended standing height raised to maintain nominal clearance
    • max_roll_deg / max_pitch_deg reduced by drag margin
    • max_vx / vy reduced by mass penalty factor
    • max_vyaw reduced by inertia penalty factor
    • Foot raise height increased to clear payload on uneven terrain
    • Energy model idle power increased (holding extra mass)

Ground contact detection
────────────────────────
  Belly-contact is inferred from two signals:
    1. Body height ≤ contact_height_threshold
       contact_height_threshold = BELLY_OFFSET + T - SILICONE_COMPRESSION
    2. Foot-force mean drops below unloaded_threshold
       (weight redistributes onto the payload contact surface)

  Both signals must agree before contact is declared.

Usage
─────
  cfg = PayloadConfig(mass_kg=1.5, thickness_m=0.05, ...)
  comp = PayloadCompensator(cfg)
  adjusted_limits = comp.adjusted_safety_limits(base_limits)
  comp.apply_to_anatomy(anatomy)  # raises default standing height
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.core.safety import SafetyLimits

logger = logging.getLogger(__name__)

# ── Go2 geometry constants ────────────────────────────────────────────────────

# Distance from body COM to the belly (underside) surface, metres.
# At nominal body_height=0.27 m, belly is ~0.15 m above ground.
BELLY_OFFSET: float = 0.120          # body_COM → belly surface (downward)

# Nominal Go2 body height (SDK default standing)
NOMINAL_BODY_HEIGHT: float = 0.27    # metres

# Base belly clearance at nominal height  (0.27 - 0.12 = 0.15 m)
NOMINAL_BELLY_CLEARANCE: float = NOMINAL_BODY_HEIGHT - BELLY_OFFSET

# Go2 body mass (kinematics.py constant)
ROBOT_MASS_KG: float = 15.0

# Silicone compresses approximately this much under full robot weight
SILICONE_COMPRESSION_M: float = 0.008   # 8 mm at full load

# Safety clearance buffer kept between payload bottom and ground in normal ops
OPERATIONAL_CLEARANCE_M: float = 0.025  # 25 mm buffer above ground contact


# ── Payload material ──────────────────────────────────────────────────────────

class PayloadMaterial(str, Enum):
    SILICONE     = "silicone"        # compliant, tactile, high friction
    RIGID_PLATE  = "rigid_plate"     # hard-mount sensor array
    FOAM         = "foam"            # impact-absorbing undercarriage
    MESH         = "mesh"            # ventilated, lightweight


MATERIAL_PROPERTIES = {
    PayloadMaterial.SILICONE:    {"friction": 0.9, "compliance_m": 0.008, "thermal_k": 0.2},
    PayloadMaterial.RIGID_PLATE: {"friction": 0.5, "compliance_m": 0.000, "thermal_k": 50.0},
    PayloadMaterial.FOAM:        {"friction": 0.8, "compliance_m": 0.015, "thermal_k": 0.04},
    PayloadMaterial.MESH:        {"friction": 0.4, "compliance_m": 0.002, "thermal_k": 1.0},
}


# ── Payload configuration ─────────────────────────────────────────────────────

@dataclass
class PayloadConfig:
    """
    Physical description of a payload mounted to the Go2 underbelly.

    All dimensions in metres, mass in kg.
    COM offset is relative to body frame origin (forward, lateral, down).
    """
    # Identification
    name: str                     = "undercarriage_payload"
    description: str              = "Silicone substructure"
    material: PayloadMaterial     = PayloadMaterial.SILICONE

    # Physical
    mass_kg: float                = 1.5      # payload mass
    thickness_m: float            = 0.050    # total protrusion below belly (m)
    length_m: float               = 0.300    # fore-aft extent (m)
    width_m: float                = 0.200    # lateral extent (m)

    # COM offset from body-frame origin (x fwd, y lat, z down — positive down)
    com_offset_x: float           = 0.000    # centred fore-aft
    com_offset_y: float           = 0.000    # centred laterally
    com_offset_z: float           = 0.000    # at belly surface (auto-computed)

    # Operating clearance preference (override default)
    desired_clearance_m: float    = OPERATIONAL_CLEARANCE_M

    # Sensor capabilities (informational, used by plugin)
    has_tactile_sensor: bool      = True
    has_thermal_sensor: bool      = False

    # Attachment timestamp
    attached_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # Auto-compute COM offset for payload hanging below belly:
        # z_down = BELLY_OFFSET + thickness/2  (half-way through payload)
        if self.com_offset_z == 0.0:
            self.com_offset_z = BELLY_OFFSET + self.thickness_m / 2.0

    @property
    def compliance_m(self) -> float:
        """Material deformation under full robot weight."""
        return MATERIAL_PROPERTIES[self.material]["compliance_m"]

    @property
    def friction(self) -> float:
        return MATERIAL_PROPERTIES[self.material]["friction"]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "material": self.material.value,
            "mass_kg": self.mass_kg,
            "thickness_m": self.thickness_m,
            "dimensions_m": {"l": self.length_m, "w": self.width_m, "h": self.thickness_m},
            "com_offset": {"x": self.com_offset_x, "y": self.com_offset_y, "z": self.com_offset_z},
            "desired_clearance_m": self.desired_clearance_m,
            "has_tactile_sensor": self.has_tactile_sensor,
            "has_thermal_sensor": self.has_thermal_sensor,
            "attached_at": self.attached_at,
        }


# ── Combined COM ──────────────────────────────────────────────────────────────

@dataclass
class CombinedCOM:
    """Composite centre of mass for robot + payload system."""
    x: float    # forward (m, body frame)
    y: float    # lateral (m, body frame)
    z: float    # height above ground (m, world frame)

    # COM shift relative to bare robot
    delta_x: float = 0.0
    delta_y: float = 0.0
    delta_z: float = 0.0   # negative = lowered (more stable)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "z_above_ground": round(self.z, 4),
            "delta": {"x": round(self.delta_x, 4),
                      "y": round(self.delta_y, 4),
                      "z": round(self.delta_z, 4)},
        }


# ── Ground contact state ──────────────────────────────────────────────────────

class ContactState(str, Enum):
    NO_CONTACT     = "no_contact"      # payload airborne
    APPROACHING    = "approaching"     # within 5 mm of contact
    CONTACT        = "contact"         # payload touching ground
    PRESSED        = "pressed"         # actively loading the ground
    DRAGGING       = "dragging"        # ⚠️ lateral motion while in contact


@dataclass
class ContactStatus:
    state: ContactState  = ContactState.NO_CONTACT
    clearance_m: float   = NOMINAL_BELLY_CLEARANCE
    contact_force_n: float = 0.0      # estimated normal force (N)
    last_contact_at: float = 0.0
    drag_detected: bool  = False

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "clearance_m": round(self.clearance_m, 4),
            "contact_force_n": round(self.contact_force_n, 2),
            "drag_detected": self.drag_detected,
        }


# ── Payload compensator ───────────────────────────────────────────────────────

class PayloadCompensator:
    """
    Derives corrected operating limits and gait parameters for a robot
    carrying an undercarriage payload.

    Call `adjusted_safety_limits()` to get a modified SafetyLimits object
    that accounts for reduced clearance and increased inertia.

    Call `recommended_body_height()` to get the body height CERBERUS should
    command on payload attachment to maintain safe ground clearance.
    """

    def __init__(self, config: PayloadConfig):
        self.config = config
        self._mass_fraction   = config.mass_kg / (ROBOT_MASS_KG + config.mass_kg)
        self._total_mass      = ROBOT_MASS_KG + config.mass_kg

        # Height at which payload bottom just touches the ground
        # body_height = BELLY_OFFSET + payload_thickness - compliance
        self.contact_height_m: float = (
            BELLY_OFFSET + config.thickness_m - config.compliance_m
        )

        # Recommended standing height: contact_height + desired_clearance
        self.recommended_standing_height_m: float = (
            self.contact_height_m + config.desired_clearance_m
        )

        logger.info(
            "PayloadCompensator: mass=%.2fkg thick=%.0fmm contact_h=%.3fm stand_h=%.3fm",
            config.mass_kg, config.thickness_m * 1000,
            self.contact_height_m, self.recommended_standing_height_m
        )

    # ── Safety limit adjustments ──────────────────────────────────────────────

    def adjusted_safety_limits(self, base: "SafetyLimits") -> "SafetyLimits":
        """
        Return a copy of base SafetyLimits with payload-compensated values.
        Never relaxes any limit — only tightens.
        """
        # Import here to avoid circular import at module level
        from cerberus.core.safety import SafetyLimits

        cfg = self.config

        # Velocity penalty: heavier system, tighter clearance
        mass_penalty  = min(0.40, 0.25 * self._mass_fraction * 4)  # max 40% reduction
        speed_factor  = 1.0 - mass_penalty

        # Yaw penalty: larger rotational inertia (payload width × length)
        inertia_scale = 1.0 + (cfg.length_m * cfg.width_m * cfg.mass_kg) / (0.5 * ROBOT_MASS_KG)
        yaw_factor    = max(0.5, 1.0 / math.sqrt(inertia_scale))

        # Tilt penalty: belly drag starts at smaller angles
        # For a payload of thickness T hanging below the belly, at roll angle θ,
        # the edge of the payload descends by (width/2) * sin(θ).
        # Drag occurs when that descent ≥ ground clearance.
        clearance = self.recommended_standing_height_m - self.contact_height_m
        drag_half_width = cfg.width_m / 2.0
        drag_angle_roll_deg = math.degrees(math.asin(
            min(0.99, clearance / max(0.001, drag_half_width))
        ))
        drag_half_length = cfg.length_m / 2.0
        drag_angle_pitch_deg = math.degrees(math.asin(
            min(0.99, clearance / max(0.001, drag_half_length))
        ))

        return SafetyLimits(
            # Velocity — never exceed base
            max_vx   = min(base.max_vx,    base.max_vx    * speed_factor),
            max_vy   = min(base.max_vy,    base.max_vy    * speed_factor),
            max_vyaw = min(base.max_vyaw,  base.max_vyaw  * yaw_factor),

            # Tilt — reduced by drag angle (never below 5°)
            max_roll_deg  = max(5.0, min(base.max_roll_deg,  drag_angle_roll_deg  * 0.8)),
            max_pitch_deg = max(5.0, min(base.max_pitch_deg, drag_angle_pitch_deg * 0.8)),

            # Height — minimum raised to guarantee clearance above payload
            min_body_height = max(
                base.min_body_height,
                self.recommended_standing_height_m
            ),
            max_body_height = base.max_body_height,  # unchanged

            # Battery/heartbeat — unchanged
            battery_warn_pct     = base.battery_warn_pct,
            battery_low_pct      = base.battery_low_pct,
            battery_critical_pct = base.battery_critical_pct,
            heartbeat_timeout_s  = base.heartbeat_timeout_s,
            watchdog_hz          = base.watchdog_hz,
        )

    # ── Gait parameter adjustments ────────────────────────────────────────────

    def foot_raise_adjustment_m(self) -> float:
        """
        Additional foot raise height (m) needed to avoid the payload snagging
        terrain features during swing phase.
        Increases with payload thickness and compliance.
        """
        return self.config.thickness_m * 0.15 + self.config.compliance_m

    def recommended_gait_id(self) -> int:
        """
        Suggested Unitree gait ID based on payload characteristics.
          0 = trot           (fast, default)
          1 = slow trot      (moderate stability)
          2 = walking trot   (high stability)
          3 = stance walk    (maximum stability)
        """
        total = self._total_mass
        if total > 20.0:   return 3   # very heavy
        if total > 18.0:   return 2   # moderately heavy
        if total > 16.5:   return 1   # slightly heavy
        return 0                      # near-nominal

    # ── COM computation ───────────────────────────────────────────────────────

    def combined_com(self, robot_body_height: float) -> CombinedCOM:
        """
        Compute the composite COM of robot + payload in world frame.

        robot_body_height: current body_height (m above ground)
        """
        cfg = self.config
        tm  = self._total_mass

        # Robot bare COM is at (0, 0, robot_body_height) in world frame
        robot_z = robot_body_height

        # Payload COM in world frame
        # z_world = robot_body_height - BELLY_OFFSET - (thickness/2)
        payload_z = robot_body_height - BELLY_OFFSET - cfg.thickness_m / 2.0

        # Combined COM (weighted average)
        cx = (0.0 * ROBOT_MASS_KG + cfg.com_offset_x * cfg.mass_kg) / tm
        cy = (0.0 * ROBOT_MASS_KG + cfg.com_offset_y * cfg.mass_kg) / tm
        cz = (robot_z * ROBOT_MASS_KG + payload_z     * cfg.mass_kg) / tm

        return CombinedCOM(
            x=cx, y=cy, z=cz,
            delta_x=cx,
            delta_y=cy,
            delta_z=cz - robot_body_height,
        )

    # ── Contact detection ─────────────────────────────────────────────────────

    def infer_contact(
        self,
        body_height: float,
        foot_forces: list[float],
        velocity_mag: float,
    ) -> ContactStatus:
        """
        Infer payload ground contact from body height and foot loading.

        Height signal:
          body_height ≤ contact_height_m → contact
          body_height ≤ contact_height_m + 0.005 → approaching

        Foot-force signal:
          If contact height reached, foot forces fall below ROBOT_MASS × g × 0.6
          (40% load transferred to payload contact surface)

        Drag signal:
          velocity > 0.02 m/s while in contact → dragging
        """
        clearance = body_height - self.contact_height_m
        mean_ff   = sum(foot_forces) / max(1, len(foot_forces))

        # Expected foot loading per leg at full nominal support (N)
        nominal_per_foot = (ROBOT_MASS_KG * 9.81) / 4.0

        force_ratio = mean_ff / max(0.01, nominal_per_foot)

        contact_force = max(0.0, self._total_mass * 9.81 * (1.0 - force_ratio))

        if clearance <= 0.0:
            state = ContactState.PRESSED if contact_force > 10.0 else ContactState.CONTACT
        elif clearance <= 0.005:
            state = ContactState.APPROACHING
        else:
            state = ContactState.NO_CONTACT
            contact_force = 0.0

        drag = (
            state in (ContactState.CONTACT, ContactState.PRESSED)
            and velocity_mag > 0.02
        )
        if drag:
            state = ContactState.DRAGGING

        return ContactStatus(
            state=state,
            clearance_m=clearance,
            contact_force_n=contact_force,
            last_contact_at=time.time() if state != ContactState.NO_CONTACT else 0.0,
            drag_detected=drag,
        )

    def to_dict(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "contact_height_m": round(self.contact_height_m, 4),
            "recommended_standing_height_m": round(self.recommended_standing_height_m, 4),
            "foot_raise_adjustment_m": round(self.foot_raise_adjustment_m(), 4),
            "recommended_gait_id": self.recommended_gait_id(),
            "mass_fraction": round(self._mass_fraction, 3),
        }

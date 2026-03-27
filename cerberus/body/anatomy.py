"""
cerberus/body/anatomy.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Digital Anatomy — "The Body"

Models the physical reality of the Go2 quadruped:
  JointModel      — per-joint kinematics, torque, temperature, wear
  EnergyModel     — battery, metabolic expenditure, fatigue accumulation
  StabilityModel  — ZMP / COM tracking, tip-over prediction
  DigitalAnatomy  — integrates all physical subsystems + exposes body state

This layer sits between the cognitive engine (what the robot wants to do)
and the control layer (what the motors actually do).

It answers questions like:
  "Can I perform this action given current fatigue level?"
  "Is this pose stable?"
  "How much energy will this mission consume?"
  "Are my joints under excessive stress?"

Integration:
  - SafetyEnforcer queries StabilityModel before each command
  - AnimationPlayer respects joint limits from JointModel
  - LearningSystem uses EnergyModel data for efficiency training
  - PersonalityEngine modulates behavior based on fatigue (tired dog = calmer)
"""

import math
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Any

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.body')

# ── Go2 joint configuration ───────────────────────────────────────────────

JOINT_NAMES = [
    'FR_0', 'FR_1', 'FR_2',   # Front-Right: abduction, hip, knee
    'FL_0', 'FL_1', 'FL_2',   # Front-Left
    'RR_0', 'RR_1', 'RR_2',   # Rear-Right
    'RL_0', 'RL_1', 'RL_2',   # Rear-Left
]

# Joint limits (radians) — from Go2 URDF
JOINT_LIMITS: Dict[str, Tuple[float, float]] = {
    'FR_0': (-0.86, 0.86), 'FL_0': (-0.86, 0.86),
    'RR_0': (-0.86, 0.86), 'RL_0': (-0.86, 0.86),
    'FR_1': (-1.57, 3.14), 'FL_1': (-1.57, 3.14),
    'RR_1': (-1.57, 3.14), 'RL_1': (-1.57, 3.14),
    'FR_2': (-2.72, -0.88), 'FL_2': (-2.72, -0.88),
    'RR_2': (-2.72, -0.88), 'RL_2': (-2.72, -0.88),
}

# Max torques per joint type (Nm)
MAX_TORQUE: Dict[str, float] = {
    k: (23.0 if k.endswith('_0') else 45.0) for k in JOINT_NAMES
}

# Thermal resistance (°C/W) per joint
JOINT_THERMAL_R = 0.15  # °C/W


# ════════════════════════════════════════════════════════════════════════════
# JOINT MODEL
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class JointState:
    """Physical state of a single joint."""
    name:        str
    position:    float = 0.0        # radians
    velocity:    float = 0.0        # rad/s
    torque:      float = 0.0        # Nm
    temperature: float = 25.0       # °C
    wear_factor: float = 0.0        # [0, 1] accumulated wear
    error_count: int   = 0          # limit violations
    lo_limit:    float = -3.14
    hi_limit:    float =  3.14
    max_torque:  float = 45.0

    @property
    def at_limit(self) -> bool:
        margin = 0.05  # rad
        return (self.position <= self.lo_limit + margin or
                self.position >= self.hi_limit - margin)

    @property
    def thermal_ok(self) -> bool:
        return self.temperature < 72.0

    @property
    def stress(self) -> float:
        """Normalized stress [0, 1] from torque and temperature."""
        torque_stress = abs(self.torque) / max(self.max_torque, 1)
        temp_stress   = max(0, (self.temperature - 40.0) / 32.0)
        return min(1.0, torque_stress * 0.6 + temp_stress * 0.4)

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'position': round(self.position, 3),
            'velocity': round(self.velocity, 3),
            'torque':   round(self.torque, 2),
            'temp_c':   round(self.temperature, 1),
            'stress':   round(self.stress, 2),
            'at_limit': self.at_limit,
            'wear':     round(self.wear_factor, 3),
        }


class JointModel:
    """
    Per-joint physical simulation and constraint enforcement.
    Tracks kinematics, temperature, wear, and limit violations.
    """

    AMBIENT_TEMP   = 22.0
    COOL_RATE_C_S  = 0.08  # °C/s passive cooling per degree above ambient

    def __init__(self):
        self.joints: Dict[str, JointState] = {}
        for name in JOINT_NAMES:
            lo, hi = JOINT_LIMITS[name]
            self.joints[name] = JointState(
                name=name, position=0.0,
                lo_limit=lo, hi_limit=hi,
                max_torque=MAX_TORQUE[name],
                temperature=self.AMBIENT_TEMP,
            )

    def update(self, joint_positions: Dict[str, float],
               joint_torques: Dict[str, float],
               dt_s: float):
        """Update joint states from new positions and computed torques."""
        for name, js in self.joints.items():
            q_prev = js.position
            q_new  = joint_positions.get(name, q_prev)
            tau    = joint_torques.get(name, 0.0)

            # Clamp to limits
            q_clamped = max(js.lo_limit, min(js.hi_limit, q_new))
            if q_clamped != q_new:
                js.error_count += 1

            js.velocity    = (q_clamped - q_prev) / max(dt_s, 1e-6)
            js.position    = q_clamped
            js.torque      = max(-js.max_torque, min(js.max_torque, tau))

            # Thermal model: heat from torque, cool by natural convection
            power_w = abs(js.torque) * abs(js.velocity)
            heat    = JOINT_THERMAL_R * power_w * dt_s
            cool    = self.COOL_RATE_C_S * (js.temperature - self.AMBIENT_TEMP) * dt_s
            js.temperature = max(self.AMBIENT_TEMP, js.temperature + heat - cool)

            # Wear accumulation (very slow under normal operation)
            js.wear_factor = min(1.0, js.wear_factor + 1e-7 * abs(js.torque) * dt_s)

    def hottest_joint(self) -> Tuple[str, float]:
        """Return (joint_name, temperature) of the hottest joint."""
        j = max(self.joints.values(), key=lambda x: x.temperature)
        return j.name, j.temperature

    def max_stress(self) -> float:
        return max(j.stress for j in self.joints.values())

    def summary(self) -> dict:
        hj, ht = self.hottest_joint()
        return {
            'hottest_joint': hj,
            'max_temp_c':    round(ht, 1),
            'max_stress':    round(self.max_stress(), 3),
            'joints_at_limit': sum(1 for j in self.joints.values() if j.at_limit),
            'total_error_count': sum(j.error_count for j in self.joints.values()),
        }

    def all_states(self) -> List[dict]:
        return [j.to_dict() for j in self.joints.values()]


# ════════════════════════════════════════════════════════════════════════════
# ENERGY MODEL
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class EnergyState:
    """Current energy and fatigue state of the robot."""
    battery_pct:    float = 100.0     # [0, 100]
    battery_mah:    float = 8000.0    # current charge mAh
    capacity_mah:   float = 8000.0    # total capacity
    voltage:        float = 29.4      # current voltage V
    current_a:      float = 1.0       # instantaneous current draw A
    power_w:        float = 0.0       # instantaneous power W
    fatigue_level:  float = 0.0       # [0, 1] accumulated fatigue
    total_work_j:   float = 0.0       # total work done (Joules)
    session_mah:    float = 0.0       # charge used this session

    @property
    def is_critical(self) -> bool:
        return self.battery_pct < 10.0

    @property
    def is_low(self) -> bool:
        return self.battery_pct < 25.0

    @property
    def estimated_runtime_min(self) -> float:
        """Estimated remaining runtime in minutes at current draw."""
        if self.current_a <= 0: return 999.0
        remaining_mah = self.battery_mah
        return (remaining_mah / (self.current_a * 1000)) * 60.0

    def to_dict(self) -> dict:
        return {
            'battery_pct':   round(self.battery_pct, 1),
            'voltage':       round(self.voltage, 2),
            'current_a':     round(self.current_a, 2),
            'power_w':       round(self.power_w, 1),
            'fatigue_level': round(self.fatigue_level, 3),
            'session_mah':   round(self.session_mah, 0),
            'est_runtime_min': round(self.estimated_runtime_min, 0),
            'is_low':        self.is_low,
            'is_critical':   self.is_critical,
        }


class EnergyModel:
    """
    Metabolic energy and fatigue model.
    Tracks battery drain, current draw, and fatigue accumulation.
    Fatigue affects: maximum velocity, agility, reaction time, mood.

    Fatigue model:
      accumulate: proportional to power output and activity level
      recover:    during rest/idle states (passive recovery)
      thresholds: mild (>0.3), moderate (>0.6), severe (>0.85)
    """

    # Current draw per activity state (A at 29.4V)
    CURRENT_BY_STATE = {
        'idle': 0.8, 'standing': 1.0, 'sitting': 0.6,
        'walking': 4.5, 'following': 5.0, 'navigating': 5.5,
        'interacting': 6.0, 'performing': 7.0, 'patrolling': 5.0,
        'estop': 0.1, 'fault': 0.3, 'offline': 0.0,
    }

    FATIGUE_RATE   = 0.0000025  # per J of work (~2% per minute walking)
    RECOVERY_RATE  = 0.0005  # per second at rest

    def __init__(self, capacity_mah: float = 8000.0):
        self.state = EnergyState(capacity_mah=capacity_mah,
                                  battery_mah=capacity_mah * 0.87)  # start at 87%

    def update(self, robot_state: str, joint_torques: Dict[str, float],
               joint_velocities: Dict[str, float], dt_s: float):
        """Update energy and fatigue based on current activity."""
        s = self.state

        # Instantaneous power from joint work + idle draw
        mech_power = sum(
            abs(t * joint_velocities.get(n, 0))
            for n, t in joint_torques.items()
        )
        base_current = self.CURRENT_BY_STATE.get(robot_state, 1.0)
        elec_power   = base_current * 29.4
        s.power_w    = mech_power + elec_power
        s.current_a  = s.power_w / max(s.voltage, 1.0)

        # Battery drain
        drain_mah = s.current_a * (dt_s / 3600.0) * 1000.0
        s.battery_mah  = max(0.0, s.battery_mah - drain_mah)
        s.session_mah += drain_mah
        s.battery_pct  = (s.battery_mah / s.capacity_mah) * 100.0
        s.voltage       = 19.0 + (s.battery_pct / 100.0) * 14.4  # 19-33.4V range
        s.total_work_j += s.power_w * dt_s

        # Fatigue
        if robot_state in ('idle', 'sitting', 'standing'):
            # Recovery during rest
            s.fatigue_level = max(0.0, s.fatigue_level - self.RECOVERY_RATE * dt_s)
        else:
            # Accumulate from power output
            s.fatigue_level = min(1.0, s.fatigue_level + self.FATIGUE_RATE * s.power_w * dt_s)

    @property
    def fatigue_label(self) -> str:
        f = self.state.fatigue_level
        if f < 0.3:  return 'fresh'
        if f < 0.6:  return 'mild'
        if f < 0.85: return 'moderate'
        return 'severe'

    def velocity_cap_factor(self) -> float:
        """Returns [0.4, 1.0] — fatigue reduces max speed."""
        return max(0.4, 1.0 - self.state.fatigue_level * 0.6)

    def estimated_mission_cost_mah(self, mission_type: str,
                                    duration_s: float) -> float:
        """Estimate battery cost of a mission."""
        avg_current = self.CURRENT_BY_STATE.get(
            {'patrol': 'patrolling', 'follow': 'following',
             'inspect': 'navigating'}.get(mission_type, 'walking'), 4.5)
        return avg_current * (duration_s / 3600.0) * 1000.0


# ════════════════════════════════════════════════════════════════════════════
# STABILITY MODEL
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class StabilityState:
    """Current stability assessment."""
    com_x:      float = 0.0   # center of mass x offset (m)
    com_y:      float = 0.0   # center of mass y offset (m)
    zmp_x:      float = 0.0   # zero moment point x
    zmp_y:      float = 0.0   # zero moment point y
    in_support: bool  = True  # is ZMP inside support polygon?
    tilt_angle: float = 0.0   # combined pitch+roll magnitude (deg)
    margin:     float = 1.0   # stability margin [0, 1] (1 = very stable)
    contacts:   int   = 4     # number of feet in contact

    def to_dict(self) -> dict:
        return {
            'com': {'x': round(self.com_x, 3), 'y': round(self.com_y, 3)},
            'zmp': {'x': round(self.zmp_x, 3), 'y': round(self.zmp_y, 3)},
            'in_support_polygon': self.in_support,
            'tilt_deg':   round(self.tilt_angle, 2),
            'margin':     round(self.margin, 3),
            'contacts':   self.contacts,
        }


class StabilityModel:
    """
    Real-time stability assessment using simplified ZMP approximation.
    Feeds into SafetyEnforcer and CognitiveMind for risk-aware planning.

    ZMP criterion: stability guaranteed when ZMP remains within
    the convex hull of contact points (support polygon).
    """

    # Approximate foot positions in body frame (x, y) — meters
    FOOT_POSITIONS = {
        'FL': (-0.15,  0.12), 'FR': (-0.15, -0.12),
        'RL': ( 0.15,  0.12), 'RR': ( 0.15, -0.12),
    }

    ROBOT_MASS_KG = 15.0
    GRAVITY_MS2   = 9.81

    def __init__(self):
        self.state = StabilityState()

    def update(self, pitch_deg: float, roll_deg: float,
               foot_forces: Dict[str, float],
               com_x: float = 0.0, body_height: float = 0.30):
        """
        Update stability state from sensor readings.
        Simplified ZMP computation from foot forces.
        """
        s = self.state

        # Count contacts
        s.contacts = sum(1 for f in foot_forces.values() if f > 2.0)

        # ZMP from weighted foot positions
        total_force = sum(max(0, f) for f in foot_forces.values())
        if total_force > 0:
            foot_labels = {'fl': 'FL', 'fr': 'FR', 'rl': 'RL', 'rr': 'RR'}
            s.zmp_x = sum(
                max(0, foot_forces.get(lk, 0)) * self.FOOT_POSITIONS[label][0]
                for lk, label in foot_labels.items()
            ) / total_force
            s.zmp_y = sum(
                max(0, foot_forces.get(lk, 0)) * self.FOOT_POSITIONS[label][1]
                for lk, label in foot_labels.items()
            ) / total_force
        else:
            s.zmp_x = s.zmp_y = 0.0

        # COM from IMU pitch/roll
        s.com_x = com_x
        s.com_y = math.tan(math.radians(roll_deg)) * body_height

        # Stability margin: distance of ZMP from support polygon boundary
        # Simplified: use half-width/length of support polygon
        hw = 0.10  # half-width (m) — lateral
        hl = 0.14  # half-length (m) — fore-aft
        zmp_in_x = abs(s.zmp_x) < hl
        zmp_in_y = abs(s.zmp_y) < hw
        s.in_support = zmp_in_x and zmp_in_y

        # Margin: 1.0 = center, 0.0 = at boundary
        margin_x = max(0, 1.0 - abs(s.zmp_x) / hl)
        margin_y = max(0, 1.0 - abs(s.zmp_y) / hw)
        s.margin = min(margin_x, margin_y) * (s.contacts / 4.0)

        s.tilt_angle = math.sqrt(pitch_deg**2 + roll_deg**2)

    def is_safe(self, pitch_limit: float = 10.0, roll_limit: float = 10.0) -> bool:
        return (self.state.in_support and
                self.state.tilt_angle < math.sqrt(pitch_limit**2 + roll_limit**2) and
                self.state.contacts >= 2)

    def tip_over_risk(self) -> float:
        """Risk score [0, 1] — 1.0 = imminent tip-over."""
        return 1.0 - self.state.margin


# ════════════════════════════════════════════════════════════════════════════
# DIGITAL ANATOMY (subsystem)
# ════════════════════════════════════════════════════════════════════════════

class DigitalAnatomy(Subsystem):
    """
    CERBERUS Digital Anatomy — integrates joint, energy, and stability models.
    Runs at Priority.CONTROL (500Hz) to stay synchronized with control loop.
    Exposes body state to all other subsystems.
    """

    name     = 'digital_anatomy'
    priority = Priority.CONTROL

    def __init__(self, bus: SystemEventBus):
        self._bus       = bus
        self.joints     = JointModel()
        self.energy     = EnergyModel()
        self.stability  = StabilityModel()
        self._runtime   = None
        self._tick_count = 0
        self._robot_state = 'idle'
        self._last_tel:  dict = {}

        # Subscribe to telemetry updates
        bus.subscribe('telemetry', self._on_telemetry)
        bus.subscribe('fsm.transition', self._on_state_change)

    def _on_telemetry(self, event: str, data: Any):
        self._last_tel = data if isinstance(data, dict) else {}

    def _on_state_change(self, event: str, data: Any):
        if isinstance(data, dict):
            self._robot_state = data.get('to', 'idle')

    async def on_start(self, runtime):
        self._runtime = runtime
        log.info('DigitalAnatomy started')

    async def on_tick(self, ctx: TickContext):
        self._tick_count += 1
        tel = self._last_tel
        if not tel: return

        # Update joints from telemetry
        joint_pos  = tel.get('joint_positions', {})
        joint_tau  = {k: 0.0 for k in joint_pos}  # torques estimated from PD controller
        joint_vel  = {k: 0.0 for k in joint_pos}

        self.joints.update(joint_pos, joint_tau, ctx.dt_s)

        # Update energy model
        self.energy.update(
            robot_state     = self._robot_state,
            joint_torques   = joint_tau,
            joint_velocities = joint_vel,
            dt_s            = ctx.dt_s,
        )

        # Update stability model (every 10th tick to save CPU)
        if self._tick_count % 10 == 0:
            self.stability.update(
                pitch_deg   = tel.get('pitch_deg', 0.0),
                roll_deg    = tel.get('roll_deg', 0.0),
                foot_forces = tel.get('foot_forces', {}),
                com_x       = tel.get('com_x', 0.0),
            )

        # Publish body state every 50 ticks (~10Hz)
        if self._tick_count % 50 == 0:
            await self._bus.emit('body.state', self.body_state(), 'anatomy')

            # Critical alerts
            if self.energy.state.is_critical:
                await self._bus.emit('body.battery_critical',
                                     {'pct': self.energy.state.battery_pct},
                                     'anatomy', priority=Priority.SAFETY)
            _, hot_temp = self.joints.hottest_joint()
            if hot_temp > 70.0:
                await self._bus.emit('body.overtemp',
                                     {'temp_c': hot_temp},
                                     'anatomy', priority=Priority.SAFETY)
            if not self.stability.is_safe():
                await self._bus.emit('body.unstable',
                                     self.stability.state.to_dict(),
                                     'anatomy', priority=Priority.SAFETY)

    def body_state(self) -> dict:
        return {
            'energy':    self.energy.state.to_dict(),
            'fatigue':   self.energy.fatigue_label,
            'stability': self.stability.state.to_dict(),
            'joints':    self.joints.summary(),
            'velocity_cap': round(self.energy.velocity_cap_factor(), 2),
        }

    def status(self) -> dict:
        return {
            'name':    self.name,
            'enabled': self.enabled,
            'ticks':   self._tick_count,
            **self.body_state(),
        }

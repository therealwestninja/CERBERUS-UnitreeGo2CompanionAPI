"""
plugins/undercarriage_payload/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS UndercarriagePayload Plugin — v1.0.0

Manages a compliant silicone (or other material) substructure mounted to the
underside of the Go2 body.  Handles:

  • Automatic payload registration with the anatomy / safety subsystems
  • Continuous ground-clearance monitoring with drag detection
  • Five autonomous belly-interaction behaviors:

      ground_scout     — hover belly 2–5 mm above terrain; read contact texture
      belly_contact    — controlled touchdown; hold for N seconds
      thermal_rest     — extended belly-down rest on the silicone surface
      object_nudge     — detect nearby grounded object; gentle belly push
      substrate_scan   — systematic belly-drag traverse for tactile mapping

  • Behavior state machine with safe entry/exit for each behavior
  • Publishes EventBus topics consumed by UI/logging:
      payload.contact        — ContactStatus dict
      payload.behavior       — {name, state, progress}
      payload.drag_warning   — emitted when dragging detected
      payload.scan_result    — substrate_scan tile map

Requirements:
  capabilities: read_state, control_motion, publish_events
  min CERBERUS version: 2.2.0

Autonomous trigger conditions (in on_tick):
  • ground_scout:     curiosity > 0.5, flat terrain, not recently scouted
  • belly_contact:    direct API trigger only (safety-sensitive)
  • thermal_rest:     boredom > 0.6 AND speed < 0.05 AND battery > 20%
  • object_nudge:     obstacle_near AND playfulness > 0.7 AND not_estop
  • substrate_scan:   direct API trigger only
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

from cerberus.plugins.plugin_manager import (
    CerberusPlugin, PluginManifest, TrustLevel,
)
from cerberus.anatomy.payload import (
    PayloadConfig, PayloadCompensator, ContactState,
    ContactStatus, NOMINAL_BODY_HEIGHT, BELLY_OFFSET,
    PayloadMaterial,
)

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import RobotState
    from cerberus.core.engine import CerberusEngine

logger = logging.getLogger(__name__)


# ── Behavior state machine ────────────────────────────────────────────────────

class BehaviorState(str, Enum):
    IDLE             = "idle"
    GROUND_SCOUT     = "ground_scout"
    BELLY_CONTACT    = "belly_contact"
    THERMAL_REST     = "thermal_rest"
    OBJECT_NUDGE     = "object_nudge"
    SUBSTRATE_SCAN   = "substrate_scan"
    RESTORING        = "restoring"       # returning to pre-behavior height


@dataclass
class BehaviorContext:
    """Per-behavior transient state. Reset on each behavior entry."""
    behavior: BehaviorState    = BehaviorState.IDLE
    start_time: float          = field(default_factory=time.monotonic)
    phase: int                 = 0       # phase index within behavior
    phase_start: float         = field(default_factory=time.monotonic)
    params: dict               = field(default_factory=dict)
    pre_behavior_height: float = NOMINAL_BODY_HEIGHT
    scan_tiles: list[dict]     = field(default_factory=list)


# ── Substrate scan tile ───────────────────────────────────────────────────────

@dataclass
class ScanTile:
    """Single sample from a substrate_scan traverse."""
    x: float           # body-frame forward position (m) — integrated from velocity
    y: float           # body-frame lateral position (m)
    contact: bool      # payload in contact during this sample
    force_n: float     # estimated contact force (N)
    foot_forces: list[float]   # raw foot forces for context
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 3), "y": round(self.y, 3),
            "contact": self.contact,
            "force_n": round(self.force_n, 2),
            "foot_forces": [round(f, 1) for f in self.foot_forces],
        }


# ── Plugin ────────────────────────────────────────────────────────────────────

class UndercarriagePayloadPlugin(CerberusPlugin):
    """
    Manages undercarriage payload attachment, compensation, and autonomous
    belly-interaction behaviors for the Unitree Go2.
    """

    MANIFEST = PluginManifest(
        name        = "undercarriage_payload",
        version     = "1.0.0",
        description = "Undercarriage payload compensation and belly-interaction behaviors",
        author      = "CERBERUS",
        trust       = TrustLevel.TRUSTED,           # needs control_motion
        capabilities= {"read_state", "control_motion", "control_gait",
                       "publish_events", "modify_safety_limits"},
    )

    def __init__(self, engine: "CerberusEngine"):
        super().__init__(engine)
        self._config:      PayloadConfig     | None = None
        self._compensator: PayloadCompensator | None = None
        self._contact:     ContactStatus     = ContactStatus()
        self._ctx:         BehaviorContext   = BehaviorContext()
        self._behavior:    BehaviorState     = BehaviorState.IDLE
        self._last_scout:  float = 0.0
        self._attached:    bool = False

        # Scan traverse state
        self._scan_pos_x: float = 0.0
        self._scan_pos_y: float = 0.0
        self._scan_tiles: list[ScanTile] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        logger.info("[UndercarriagePayload] Plugin loaded. No payload attached.")

    async def on_unload(self) -> None:
        if self._attached:
            await self._restore_to_normal()
        logger.info("[UndercarriagePayload] Plugin unloaded.")

    # ── Payload attachment API ────────────────────────────────────────────────

    async def attach(self, config: PayloadConfig) -> dict:
        """
        Attach a payload.  Computes compensated limits and immediately
        raises body height to the safe standing clearance.

        Returns a dict describing the compensation applied.
        """
        if self._attached:
            await self.detach()

        self._config      = config
        self._compensator = PayloadCompensator(config)
        self._attached    = True

        # Apply payload-aware safety limits to the watchdog
        if self.engine.watchdog is not None:
            base_limits = self.engine.watchdog.limits
            adjusted    = self._compensator.adjusted_safety_limits(base_limits)
            self.engine.watchdog.limits = adjusted
            logger.info(
                "[UndercarriagePayload] Safety limits adjusted — "
                "max_vx=%.2f max_roll=%.1f° min_height=%.3f",
                adjusted.max_vx, adjusted.max_roll_deg, adjusted.min_body_height
            )

        # Raise body to recommended standing height
        stand_h = self._compensator.recommended_standing_height_m
        await self.bridge.set_body_height(stand_h)
        await asyncio.sleep(0.3)   # allow motion to settle

        # Apply gait recommendation
        gait_id = self._compensator.recommended_gait_id()
        if gait_id > 0:
            await self.bridge.switch_gait(gait_id)

        # Apply foot raise adjustment
        foot_adj = self._compensator.foot_raise_adjustment_m()
        if foot_adj > 0.005:
            await self.bridge.set_foot_raise_height(foot_adj)

        comp_dict = self._compensator.to_dict()
        await self.engine.bus.publish("payload.attached", comp_dict)
        logger.info("[UndercarriagePayload] Payload '%s' attached.", config.name)
        return comp_dict

    async def detach(self) -> None:
        """
        Remove the payload.  Restores original safety limits and default
        body height.  Any active behavior is aborted first.
        """
        if not self._attached:
            return

        if self._behavior != BehaviorState.IDLE:
            await self._abort_behavior()

        # Restore original limits
        if self.engine.watchdog is not None:
            from cerberus.core.safety import SafetyLimits
            self.engine.watchdog.limits = SafetyLimits()

        await self._restore_to_normal()

        self._config      = None
        self._compensator = None
        self._attached    = False

        await self.bridge.switch_gait(0)    # back to default trot
        await self.bridge.set_foot_raise_height(0.0)

        await self.engine.bus.publish("payload.detached", {})
        logger.info("[UndercarriagePayload] Payload detached, limits restored.")

    # ── Continuous monitoring ─────────────────────────────────────────────────

    async def on_tick(self, tick: int) -> None:
        if not self._attached or self._compensator is None:
            return

        state = await self.bridge.get_state()

        # ── Global E-stop gate ────────────────────────────────────────────────
        # Abort any active behavior immediately when E-stop fires.
        # This mirrors the StairClimber's estop handling and is a safety
        # invariant: no autonomous behavior should continue under E-stop.
        if state.estop_active:
            if self._behavior != BehaviorState.IDLE:
                logger.warning(
                    "[UndercarriagePayload] E-stop active — aborting behavior: %s",
                    self._behavior.value,
                )
                await self._abort_behavior()
            return

        # ── Contact detection ─────────────────────────────────────────────────
        vel_mag = math.hypot(state.velocity_x, state.velocity_y)
        self._contact = self._compensator.infer_contact(
            state.body_height, state.foot_force, vel_mag
        )

        # Drag warning — safety critical
        if self._contact.drag_detected:
            logger.warning(
                "[UndercarriagePayload] ⚠️  DRAG DETECTED  clearance=%.3fm v=%.2fm/s",
                self._contact.clearance_m, vel_mag
            )
            await self.engine.bus.publish("payload.drag_warning", self._contact.to_dict())
            # Stop lateral motion immediately
            if not state.estop_active:
                await self.bridge.stop_move()

        # Broadcast contact status at 5Hz
        if tick % 12 == 0:
            await self.engine.bus.publish("payload.contact", self._contact.to_dict())

        # ── Autonomous behavior triggers ──────────────────────────────────────
        if self._behavior == BehaviorState.IDLE:
            await self._check_autonomous_triggers(state, tick)

        # ── Active behavior step ──────────────────────────────────────────────
        elif self._behavior == BehaviorState.GROUND_SCOUT:
            await self._step_ground_scout(state)

        elif self._behavior == BehaviorState.BELLY_CONTACT:
            await self._step_belly_contact(state)

        elif self._behavior == BehaviorState.THERMAL_REST:
            await self._step_thermal_rest(state)

        elif self._behavior == BehaviorState.OBJECT_NUDGE:
            await self._step_object_nudge(state)

        elif self._behavior == BehaviorState.SUBSTRATE_SCAN:
            await self._step_substrate_scan(state)

        elif self._behavior == BehaviorState.RESTORING:
            await self._step_restore(state)

    # ── Autonomous trigger evaluation ─────────────────────────────────────────

    async def _check_autonomous_triggers(
        self, state: "RobotState", tick: int
    ) -> None:
        now = time.monotonic()
        be  = self.engine.behavior_engine

        # Ground scout: curious + hasn't scouted recently + slow
        if (
            be is not None
            and be.personality.curiosity > 0.5
            and now - self._last_scout > 120.0   # cooldown: 2 min
            and abs(state.velocity_x) < 0.1
            and not state.estop_active
            and tick % 300 == 0                   # check every ~5s
        ):
            logger.info("[UndercarriagePayload] Autonomous trigger: ground_scout")
            await self.trigger_ground_scout(duration_s=8.0)

        # Thermal rest: bored + stationary + good battery
        elif (
            be is not None
            and be.personality.playfulness < 0.5   # more sedate personality
            and state.battery_percent > 20.0
            and abs(state.velocity_x) < 0.05
            and now - getattr(be, "_boredom_timer", now) > 180.0
            and tick % 600 == 0                    # check every ~10s
        ):
            logger.info("[UndercarriagePayload] Autonomous trigger: thermal_rest")
            await self.trigger_thermal_rest(duration_s=20.0)

        # Object nudge: obstacle + playful personality
        elif (
            be is not None
            and be.personality.playfulness > 0.7
            and be.memory.get("obstacle_near", False)
            and not state.estop_active
            and tick % 300 == 0
        ):
            logger.info("[UndercarriagePayload] Autonomous trigger: object_nudge")
            await self.trigger_object_nudge()

    # ─────────────────────────────────────────────────────────────────────────
    # BEHAVIOR 1: GROUND SCOUT
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_ground_scout(self, duration_s: float = 6.0) -> dict:
        """
        Lower belly to within 3 mm of ground; pause for sensor reading;
        slowly traverse forward; raise and resume.

        Physics:
          Target height = contact_height + 0.003 m  (3 mm clearance)
          Traverse speed = 0.05 m/s
        """
        if not self._attached:
            return {"error": "no payload attached"}
        if self._behavior != BehaviorState.IDLE:
            return {"error": f"behavior active: {self._behavior.value}"}

        state = await self.bridge.get_state()
        self._ctx = BehaviorContext(
            behavior=BehaviorState.GROUND_SCOUT,
            pre_behavior_height=state.body_height,
            params={"duration_s": max(2.0, min(30.0, duration_s))},
        )
        self._behavior = BehaviorState.GROUND_SCOUT
        await self._publish_behavior("started")
        return {"behavior": "ground_scout", "duration_s": duration_s}

    async def _step_ground_scout(self, state: "RobotState") -> None:
        ctx    = self._ctx
        comp   = self._compensator
        elapsed = time.monotonic() - ctx.start_time
        phase_t = time.monotonic() - ctx.phase_start

        # Phase 0: Descend to scout height
        if ctx.phase == 0:
            # Slow descent — switch to stance walk for max stability
            await self.bridge.switch_gait(3)
            scout_h = comp.contact_height_m + 0.003
            await self.bridge.set_body_height(scout_h)
            ctx.phase = 1
            ctx.phase_start = time.monotonic()

        # Phase 1: Hover and traverse slowly
        elif ctx.phase == 1:
            # Check we're actually near the target height
            target_h = comp.contact_height_m + 0.003
            height_settled = abs(state.body_height - target_h) < 0.015

            if height_settled:
                # Slow creep forward
                await self.bridge.move(0.05, 0.0, 0.0)

                # Capture contact readings
                vel_mag = math.hypot(state.velocity_x, state.velocity_y)
                self._contact = comp.infer_contact(
                    state.body_height, state.foot_force, vel_mag
                )
                await self.engine.bus.publish("payload.scout_sample", {
                    "clearance_m": self._contact.clearance_m,
                    "contact": self._contact.state.value,
                    "foot_forces": state.foot_force,
                    "elapsed": elapsed,
                })

            if elapsed >= ctx.params["duration_s"]:
                await self.bridge.stop_move()
                ctx.phase = 2
                ctx.phase_start = time.monotonic()

        # Phase 2: Restore
        elif ctx.phase == 2:
            self._last_scout = time.monotonic()
            await self._start_restore(ctx.pre_behavior_height)

    # ─────────────────────────────────────────────────────────────────────────
    # BEHAVIOR 2: BELLY CONTACT
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_belly_contact(self, hold_s: float = 3.0) -> dict:
        """
        Slowly lower robot until the silicone substructure makes full contact
        with the ground surface.  Hold for hold_s seconds, then rise.

        Use case: ground sensor reading, object marking, terrain sampling.

        Safety: stops descent if drag detected or estop fires.
        """
        if not self._attached:
            return {"error": "no payload attached"}
        if self._behavior != BehaviorState.IDLE:
            return {"error": f"behavior active: {self._behavior.value}"}

        state = await self.bridge.get_state()
        self._ctx = BehaviorContext(
            behavior=BehaviorState.BELLY_CONTACT,
            pre_behavior_height=state.body_height,
            params={"hold_s": max(0.5, min(60.0, hold_s))},
        )
        self._behavior = BehaviorState.BELLY_CONTACT
        await self._publish_behavior("started")
        return {"behavior": "belly_contact", "hold_s": hold_s}

    async def _step_belly_contact(self, state: "RobotState") -> None:
        ctx  = self._ctx
        comp = self._compensator
        elapsed = time.monotonic() - ctx.start_time
        phase_t = time.monotonic() - ctx.phase_start

        # Abort if dragging or estop
        if self._contact.drag_detected or state.estop_active:
            logger.warning("[UndercarriagePayload] belly_contact aborted (drag/estop)")
            await self._start_restore(ctx.pre_behavior_height)
            return

        # Phase 0: Switch to stance walk and begin slow descent
        if ctx.phase == 0:
            await self.bridge.switch_gait(3)
            await asyncio.sleep(0.1)
            ctx.phase = 1
            ctx.phase_start = time.monotonic()

        # Phase 1: Descend to contact height
        elif ctx.phase == 1:
            target_h = comp.contact_height_m - comp.config.compliance_m * 0.5
            current_h = state.body_height
            step      = 0.005   # 5 mm per tick

            if current_h - step > target_h:
                new_h = current_h - step
                await self.bridge.set_body_height(new_h)
            else:
                await self.bridge.set_body_height(target_h)
                ctx.phase = 2
                ctx.phase_start = time.monotonic()
                await self._publish_behavior("contact_made")
                logger.info(
                    "[UndercarriagePayload] Belly contact — force=%.1fN clearance=%.3fm",
                    self._contact.contact_force_n, self._contact.clearance_m
                )

        # Phase 2: Hold contact
        elif ctx.phase == 2:
            if phase_t >= ctx.params["hold_s"]:
                ctx.phase = 3
                ctx.phase_start = time.monotonic()
            else:
                # Broadcast contact data during hold
                await self.engine.bus.publish("payload.contact_hold", {
                    "elapsed": phase_t,
                    "hold_s": ctx.params["hold_s"],
                    "contact": self._contact.to_dict(),
                })

        # Phase 3: Rise and restore
        elif ctx.phase == 3:
            await self._start_restore(ctx.pre_behavior_height)

    # ─────────────────────────────────────────────────────────────────────────
    # BEHAVIOR 3: THERMAL REST
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_thermal_rest(self, duration_s: float = 30.0) -> dict:
        """
        Execute stand_down so the robot lies on the silicone pad.
        Hold for duration_s, periodically publishing thermal status,
        then rise and resume.

        The silicone surface acts as a compliant, insulating pad.
        This behavior signals to nearby observers that the robot is
        resting (LED shift to amber during hold).
        """
        if not self._attached:
            return {"error": "no payload attached"}
        if self._behavior != BehaviorState.IDLE:
            return {"error": f"behavior active: {self._behavior.value}"}

        state = await self.bridge.get_state()
        self._ctx = BehaviorContext(
            behavior=BehaviorState.THERMAL_REST,
            pre_behavior_height=state.body_height,
            params={"duration_s": max(5.0, min(300.0, duration_s))},
        )
        self._behavior = BehaviorState.THERMAL_REST
        await self._publish_behavior("started")
        return {"behavior": "thermal_rest", "duration_s": duration_s}

    async def _step_thermal_rest(self, state: "RobotState") -> None:
        ctx     = self._ctx
        elapsed = time.monotonic() - ctx.start_time
        phase_t = time.monotonic() - ctx.phase_start

        # Abort if estop
        if state.estop_active:
            await self._abort_behavior()
            return

        # Phase 0: LED amber + stand_down
        if ctx.phase == 0:
            from cerberus.bridge.go2_bridge import SportMode
            await self.bridge.set_led(255, 140, 0)     # amber = resting
            await self.bridge.execute_sport_mode(SportMode.STAND_DOWN)
            await asyncio.sleep(0.5)
            ctx.phase = 1
            ctx.phase_start = time.monotonic()
            await self._publish_behavior("lying_down")

        # Phase 1: Rest hold
        elif ctx.phase == 1:
            if tick := int(phase_t * 60) % 60 == 0:   # ~1Hz status
                await self.engine.bus.publish("payload.thermal_rest", {
                    "elapsed_s": round(phase_t, 1),
                    "remaining_s": round(ctx.params["duration_s"] - phase_t, 1),
                    "contact": self._contact.to_dict(),
                    "battery_pct": state.battery_percent,
                })

            if phase_t >= ctx.params["duration_s"]:
                ctx.phase = 2
                ctx.phase_start = time.monotonic()

        # Phase 2: Rise and restore
        elif ctx.phase == 2:
            from cerberus.bridge.go2_bridge import SportMode
            await self.bridge.execute_sport_mode(SportMode.STAND_UP)
            await asyncio.sleep(1.0)
            await self.bridge.set_led(0, 0, 0)         # LED off
            await self._start_restore(ctx.pre_behavior_height)

    # ─────────────────────────────────────────────────────────────────────────
    # BEHAVIOR 4: OBJECT NUDGE
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_object_nudge(
        self,
        nudge_speed: float  = 0.08,
        nudge_dist_m: float = 0.12,
    ) -> dict:
        """
        Lower belly to contact height, slowly advance nudge_dist_m to
        push the detected object with the silicone surface, then retreat
        and rise.

        Uses high-friction silicone to exert a gentle, controlled push.
        Safe speed limit: 0.15 m/s while payload in contact with ground.
        """
        if not self._attached:
            return {"error": "no payload attached"}
        if self._behavior != BehaviorState.IDLE:
            return {"error": f"behavior active: {self._behavior.value}"}

        nudge_speed = max(0.02, min(0.12, nudge_speed))
        nudge_dist  = max(0.03, min(0.30, nudge_dist_m))

        state = await self.bridge.get_state()
        self._ctx = BehaviorContext(
            behavior=BehaviorState.OBJECT_NUDGE,
            pre_behavior_height=state.body_height,
            params={
                "nudge_speed": nudge_speed,
                "nudge_dist_m": nudge_dist,
                "nudge_start_time": 0.0,
            },
        )
        self._behavior = BehaviorState.OBJECT_NUDGE
        await self._publish_behavior("started")
        return {"behavior": "object_nudge", "nudge_speed": nudge_speed, "nudge_dist_m": nudge_dist}

    async def _step_object_nudge(self, state: "RobotState") -> None:
        ctx     = self._ctx
        comp    = self._compensator
        phase_t = time.monotonic() - ctx.phase_start

        if state.estop_active:
            await self.bridge.stop_move()
            await self._start_restore(ctx.pre_behavior_height)
            return

        # Phase 0: Switch gait + lower belly
        if ctx.phase == 0:
            await self.bridge.switch_gait(3)
            target_h = comp.contact_height_m + 0.003   # just above contact
            await self.bridge.set_body_height(target_h)
            ctx.phase = 1
            ctx.phase_start = time.monotonic()

        # Phase 1: Await height settle
        elif ctx.phase == 1:
            if phase_t > 1.0:
                ctx.phase = 2
                ctx.phase_start = time.monotonic()
                ctx.params["nudge_start_time"] = time.monotonic()

        # Phase 2: Nudge forward
        elif ctx.phase == 2:
            elapsed_nudge = time.monotonic() - ctx.params["nudge_start_time"]
            dist_covered  = elapsed_nudge * ctx.params["nudge_speed"]

            if dist_covered < ctx.params["nudge_dist_m"]:
                # Enforce drag safety: if dragging sideways, stop
                if self._contact.drag_detected and abs(state.velocity_y) > 0.02:
                    logger.warning("[UndercarriagePayload] Object nudge: drag abort")
                    await self.bridge.stop_move()
                    ctx.phase = 3
                    ctx.phase_start = time.monotonic()
                else:
                    await self.bridge.move(ctx.params["nudge_speed"], 0.0, 0.0)
            else:
                await self.bridge.stop_move()
                ctx.phase = 3
                ctx.phase_start = time.monotonic()
                await self._publish_behavior("nudge_complete")

        # Phase 3: Retreat
        elif ctx.phase == 3:
            retreat_dist = ctx.params["nudge_dist_m"] * 0.8
            elapsed_retreat = time.monotonic() - ctx.phase_start
            if elapsed_retreat * ctx.params["nudge_speed"] < retreat_dist:
                await self.bridge.move(-ctx.params["nudge_speed"], 0.0, 0.0)
            else:
                await self.bridge.stop_move()
                await self._start_restore(ctx.pre_behavior_height)

    # ─────────────────────────────────────────────────────────────────────────
    # BEHAVIOR 5: SUBSTRATE SCAN
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_substrate_scan(
        self,
        cols: int   = 3,
        col_width_m: float = 0.10,
        row_len_m: float   = 0.30,
    ) -> dict:
        """
        Systematic boustrophedon (back-and-forth) belly traverse to build a
        tactile map of the surface beneath the robot.

        At each traversal step the contact sensor reading is recorded as a
        ScanTile.  On completion, `payload.scan_result` is emitted with the
        full tile map.

        cols:        number of lateral columns
        col_width_m: lateral step between columns (m)
        row_len_m:   forward extent per column (m)
        """
        if not self._attached:
            return {"error": "no payload attached"}
        if self._behavior != BehaviorState.IDLE:
            return {"error": f"behavior active: {self._behavior.value}"}

        state = await self.bridge.get_state()
        self._scan_tiles = []
        self._scan_pos_x = 0.0
        self._scan_pos_y = 0.0
        self._ctx = BehaviorContext(
            behavior=BehaviorState.SUBSTRATE_SCAN,
            pre_behavior_height=state.body_height,
            params={
                "cols": max(1, min(6, cols)),
                "col_width_m": max(0.05, min(0.25, col_width_m)),
                "row_len_m": max(0.10, min(0.60, row_len_m)),
                "current_col": 0,
                "col_direction": 1,       # +1 forward, -1 backward
                "row_start_time": 0.0,
            },
        )
        self._behavior = BehaviorState.SUBSTRATE_SCAN
        await self._publish_behavior("started")
        return {"behavior": "substrate_scan", "cols": cols,
                "col_width_m": col_width_m, "row_len_m": row_len_m}

    async def _step_substrate_scan(self, state: "RobotState") -> None:
        ctx     = self._ctx
        comp    = self._compensator
        params  = ctx.params
        phase_t = time.monotonic() - ctx.phase_start

        if state.estop_active:
            await self._finalize_scan()
            return

        SCAN_SPEED = 0.04    # m/s — very slow for tactile resolution

        # Phase 0: Lower to scan height
        if ctx.phase == 0:
            await self.bridge.switch_gait(3)
            target_h = comp.contact_height_m + 0.002   # 2 mm — barely above contact
            await self.bridge.set_body_height(target_h)
            ctx.phase = 1
            ctx.phase_start = time.monotonic()
            params["row_start_time"] = time.monotonic()

        # Phase 1: Row traverse
        elif ctx.phase == 1:
            elapsed_row = time.monotonic() - params["row_start_time"]
            dist_covered = elapsed_row * SCAN_SPEED

            if dist_covered < params["row_len_m"]:
                await self.bridge.move(
                    SCAN_SPEED * params["col_direction"], 0.0, 0.0
                )
                # Record tile
                self._scan_pos_x += SCAN_SPEED * params["col_direction"] * (1/60.0)
                vel_mag = math.hypot(state.velocity_x, state.velocity_y)
                c = comp.infer_contact(state.body_height, state.foot_force, vel_mag)
                self._scan_tiles.append(ScanTile(
                    x=self._scan_pos_x,
                    y=self._scan_pos_y,
                    contact=c.state != ContactState.NO_CONTACT,
                    force_n=c.contact_force_n,
                    foot_forces=list(state.foot_force),
                ))
            else:
                # Row complete — step laterally if more columns remain
                await self.bridge.stop_move()
                params["current_col"] += 1

                if params["current_col"] >= params["cols"]:
                    await self._finalize_scan()
                else:
                    # Lateral step to next column
                    params["col_direction"] *= -1
                    await self.bridge.move(0.0, params["col_width_m"], 0.0)
                    self._scan_pos_y += params["col_width_m"]
                    await asyncio.sleep(0.5)
                    params["row_start_time"] = time.monotonic()

    async def _finalize_scan(self) -> None:
        """Emit scan result and restore."""
        result = {
            "tile_count": len(self._scan_tiles),
            "tiles": [t.to_dict() for t in self._scan_tiles],
            "contact_fraction": (
                sum(1 for t in self._scan_tiles if t.contact) /
                max(1, len(self._scan_tiles))
            ),
        }
        await self.engine.bus.publish("payload.scan_result", result)
        logger.info(
            "[UndercarriagePayload] Substrate scan complete: %d tiles, "
            "%.0f%% contact",
            result["tile_count"], result["contact_fraction"] * 100
        )
        await self.bridge.stop_move()
        await self._start_restore(self._ctx.pre_behavior_height)

    # ─────────────────────────────────────────────────────────────────────────
    # RESTORE / ABORT helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _start_restore(self, target_h: float) -> None:
        """Begin the RESTORING phase — raise body height back to pre-behavior value."""
        self._behavior = BehaviorState.RESTORING
        self._ctx.params["restore_target_h"] = max(
            target_h,
            self._compensator.recommended_standing_height_m if self._compensator else 0.27
        )
        self._ctx.phase = 0
        self._ctx.phase_start = time.monotonic()
        await self._publish_behavior("restoring")

    async def _step_restore(self, state: "RobotState") -> None:
        target_h = self._ctx.params.get("restore_target_h",
                       NOMINAL_BODY_HEIGHT)
        phase_t  = time.monotonic() - self._ctx.phase_start

        RESTORE_SPEED = 0.008   # m/s ascent — slow and safe

        if state.body_height < target_h - 0.01:
            new_h = min(target_h, state.body_height + RESTORE_SPEED)
            await self.bridge.set_body_height(new_h)
        else:
            # Restore gait
            gait_id = (
                self._compensator.recommended_gait_id()
                if self._compensator else 0
            )
            await self.bridge.switch_gait(gait_id)
            self._behavior = BehaviorState.IDLE
            await self._publish_behavior("idle")
            logger.info("[UndercarriagePayload] Behavior complete — back to IDLE")

    async def _restore_to_normal(self) -> None:
        """Unconditional restore — used on detach."""
        await self.bridge.set_body_height(NOMINAL_BODY_HEIGHT)
        await asyncio.sleep(0.5)

    async def _abort_behavior(self) -> None:
        logger.warning(
            "[UndercarriagePayload] Aborting behavior: %s", self._behavior.value
        )
        await self.bridge.stop_move()
        pre_h = self._ctx.pre_behavior_height
        self._behavior = BehaviorState.IDLE
        await self._restore_to_normal() if pre_h == 0 else await self._start_restore(pre_h)

    # ─────────────────────────────────────────────────────────────────────────
    # EventBus helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _publish_behavior(self, state_str: str) -> None:
        await self.engine.bus.publish("payload.behavior", {
            "behavior": self._behavior.value,
            "state": state_str,
            "timestamp": time.time(),
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Status
    # ─────────────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "attached": self._attached,
            "payload": self._config.to_dict() if self._config else None,
            "compensator": self._compensator.to_dict() if self._compensator else None,
            "contact": self._contact.to_dict(),
            "behavior": self._behavior.value,
            "scan_tiles": len(self._scan_tiles),
        }

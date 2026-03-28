"""
cerberus/simulation/simulator.py  — CERBERUS v3.1
==================================================
Simulation environment: generates realistic mock sensor data for
development and CI testing without physical hardware.

Simulates:
  - Robot physics (position, velocity, orientation update)
  - Battery discharge over time
  - Obstacle generation at random intervals
  - Person appearance events (triggers cognitive greet behavior)
  - Foot force distribution based on gait
  - IMU noise and drift

Used by: Go2Bridge mock transport, tests, development server.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SimConfig:
    update_hz:         float = 30.0
    battery_drain_v_s: float = 0.0001   # ~25.2V → 20.5V over ~13 hours
    obstacle_prob:     float = 0.001    # per tick
    person_prob:       float = 0.0005
    noise_scale:       float = 0.005    # IMU noise amplitude


class RobotSimulator:
    """
    Real-time physics-lite simulator for the Go2.

    Attach listeners to receive simulated RobotState events.
    """

    def __init__(self, config: SimConfig | None = None) -> None:
        self._cfg = config or SimConfig()

        # State
        self._x         = 0.0
        self._y         = 0.0
        self._yaw       = 0.0
        self._pitch     = 0.0
        self._roll      = 0.0
        self._vx        = 0.0
        self._vy        = 0.0
        self._vyaw      = 0.0
        self._height    = 0.38
        self._battery   = 25.2      # Start at ~full charge
        self._mode      = "balance_stand"
        self._obstacle  = False
        self._person    = False
        self._foot_phase = 0.0

        self._state_listeners:    list[Callable] = []
        self._obstacle_listeners: list[Callable] = []
        self._person_listeners:   list[Callable] = []

        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────── #

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Command interface (mirrors Go2Bridge API) ───────────────────────── #

    def command_move(self, vx: float, vy: float, vyaw: float) -> None:
        self._vx, self._vy, self._vyaw = vx, vy, vyaw

    def command_stop(self) -> None:
        self._vx = self._vy = self._vyaw = 0.0

    def command_mode(self, mode: str) -> None:
        self._mode = mode

    def command_height(self, h: float) -> None:
        self._height = h

    # ── Listeners ──────────────────────────────────────────────────────── #

    def on_state(self, cb: Callable) -> None:
        self._state_listeners.append(cb)

    def on_obstacle(self, cb: Callable) -> None:
        self._obstacle_listeners.append(cb)

    def on_person(self, cb: Callable) -> None:
        self._person_listeners.append(cb)

    # ── Physics loop ───────────────────────────────────────────────────── #

    async def _loop(self) -> None:
        dt = 1.0 / self._cfg.update_hz
        while self._running:
            self._tick(dt)
            await asyncio.sleep(dt)

    def _tick(self, dt: float) -> None:
        from cerberus.hardware.bridge import ConnectionState, RobotState

        # Integrate position
        cos_y = math.cos(self._yaw)
        sin_y = math.sin(self._yaw)
        self._x   += (self._vx * cos_y - self._vy * sin_y) * dt
        self._y   += (self._vx * sin_y + self._vy * cos_y) * dt
        self._yaw += self._vyaw * dt

        # IMU noise
        n = self._cfg.noise_scale
        self._pitch += random.gauss(0, n) * dt
        self._roll  += random.gauss(0, n) * dt
        # Decay noise back toward zero
        self._pitch *= (1 - 2.0 * dt)
        self._roll  *= (1 - 2.0 * dt)

        # Battery drain
        drain = self._cfg.battery_drain_v_s
        if abs(self._vx) > 0.1 or abs(self._vy) > 0.1:
            drain *= 3.0   # higher drain when moving
        self._battery = max(0.0, self._battery - drain * dt)

        # Foot force simulation (simple gait cycle)
        self._foot_phase += 2.0 * math.pi * 1.5 * dt  # 1.5 Hz gait
        feet = [
            abs(math.sin(self._foot_phase + i * math.pi / 2)) * 30.0
            for i in range(4)
        ]

        # Obstacle/person events
        if random.random() < self._cfg.obstacle_prob:
            self._obstacle = True
            for cb in self._obstacle_listeners:
                try: cb(True)
                except: pass
        elif self._obstacle and random.random() < 0.1:
            self._obstacle = False
            for cb in self._obstacle_listeners:
                try: cb(False)
                except: pass

        if random.random() < self._cfg.person_prob:
            self._person = True
            for cb in self._person_listeners:
                try: cb()
                except: pass

        # Emit state
        state = RobotState(
            timestamp=time.time(),
            position_x=self._x, position_y=self._y,
            yaw=self._yaw, pitch=self._pitch, roll=self._roll,
            body_height=self._height,
            vx=self._vx, vy=self._vy, vyaw=self._vyaw,
            battery_voltage=self._battery,
            battery_percent=max(0, (self._battery - 20.5) / (25.2 - 20.5) * 100),
            foot_force=feet,
            current_mode=self._mode,
            obstacle_avoidance=False,
            sport_mode_active=True,
            connection_state=ConnectionState.CONNECTED,
        )
        for cb in self._state_listeners:
            try: cb(state)
            except: pass

"""
CERBERUS — Go2 WebRTC Adapter
==============================
Connects to a Unitree Go2 PRO/AIR via the onboard WebRTC bridge.

The Go2 PRO/AIR models run a WebRTC signaling server on port 8082.
This adapter:
  1. Fetches an SDP answer via HTTP POST /offer
  2. Establishes a peer connection (aiortc)
  3. Opens a data channel for JSON command/state exchange
  4. Publishes robot state events to the bus
  5. Receives motion commands from the sport controller

Compatibility note:
  EDU owners using CycloneDDS should use the official unitree_sdk2_python
  SportClient instead.  This file is PRO/AIR-only.

Protocol reference:
  See https://github.com/legion1581/go2_python_sdk (WebRTC transport)
  and Unitree SDK2 sport API for command structure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Port and path used by the Go2 WebRTC bridge
WEBRTC_SIGNAL_PORT  = 8082
WEBRTC_SIGNAL_PATH  = "/offer"
HEARTBEAT_INTERVAL  = 0.5    # seconds — keep-alive ping to data channel

# Sport API IDs (matches unitree_sdk2 sport_api.h)
API_DAMP            = 1001
API_BALANCE_STAND   = 1002
API_STOP_MOVE       = 1003
API_STAND_UP        = 1004
API_STAND_DOWN      = 1005
API_RECOVERY_STAND  = 1006
API_MOVE            = 1008   # vx, vy, vyaw
API_EULER           = 1010   # body orientation
API_FOOT_RAISE      = 1011
API_BODY_HEIGHT     = 1012
API_SPEED_LEVEL     = 1013
API_WIGGLE_HIPS     = 1020
API_HEART           = 1021
API_DANCE1          = 1022
API_DANCE2          = 1023
API_STRETCH         = 1025


@dataclass
class RobotState:
    """Snapshot of last-known robot telemetry."""
    connected:       bool     = False
    battery_voltage: float    = 0.0
    battery_percent: int      = 0
    imu_roll:        float    = 0.0
    imu_pitch:       float    = 0.0
    imu_yaw:         float    = 0.0
    vx:              float    = 0.0
    vy:              float    = 0.0
    vyaw:            float    = 0.0
    foot_force:      list     = field(default_factory=lambda: [0]*4)
    gait_mode:       int      = 0
    mode_machine:    int      = 0
    timestamp:       float    = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Go2WebRTCAdapter:
    """
    Manages the WebRTC connection lifecycle to a single Go2 PRO/AIR unit.

    connect()    → SDP negotiation + data channel open
    disconnect() → graceful teardown
    move(vx, vy, vyaw) → velocity command
    stop()       → zero velocity
    emergency_stop() → DAMP mode (motors go limp then sit)
    """

    def __init__(self, robot_ip: str, simulation: bool = False) -> None:
        self.robot_ip   = robot_ip
        self.simulation = simulation
        self.connected  = False
        self.last_state: dict[str, Any] | None = None
        self._state     = RobotState()
        self._pc        = None     # aiortc RTCPeerConnection
        self._dc        = None     # RTCDataChannel
        self._hb_task: asyncio.Task | None = None
        self._msg_id    = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self.simulation:
            logger.info("Simulation mode — no WebRTC connection")
            self.connected = True
            self._start_sim_loop()
            return

        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription
        except ImportError as e:
            raise RuntimeError(
                "aiortc is required for WebRTC transport.  "
                "Install via: pip install aiortc"
            ) from e

        logger.info("Connecting to Go2 at %s:%d", self.robot_ip, WEBRTC_SIGNAL_PORT)
        self._pc = RTCPeerConnection()

        # Open data channel before creating offer (triggers DTLS negotiation)
        self._dc = self._pc.createDataChannel("cerberus_cmd")
        self._dc.on("open",    self._on_dc_open)
        self._dc.on("close",   self._on_dc_close)
        self._dc.on("message", self._on_dc_message)

        # Create offer
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)

        # Signal to robot
        url = f"http://{self.robot_ip}:{WEBRTC_SIGNAL_PORT}{WEBRTC_SIGNAL_PATH}"
        payload = {
            "sdp":  self._pc.localDescription.sdp,
            "type": self._pc.localDescription.type,
        }

        async with aiohttp.ClientSession() as session, \
                session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise ConnectionError(f"Signaling failed: HTTP {resp.status}")
                answer_data = await resp.json(content_type=None)

        answer = RTCSessionDescription(
            sdp=answer_data["sdp"],
            type=answer_data["type"],
        )
        await self._pc.setRemoteDescription(answer)
        logger.info("WebRTC SDP exchange complete — waiting for data channel")

        # Wait up to 10 s for the data channel to open
        for _ in range(100):
            if self.connected:
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("Data channel did not open within 10s")

    async def disconnect(self) -> None:
        self.connected = False
        if self._hb_task:
            self._hb_task.cancel()
        if self._dc:
            self._dc.close()
        if self._pc:
            await self._pc.close()
        logger.info("WebRTC disconnected")

    # ── Data channel callbacks ────────────────────────────────────────────────

    def _on_dc_open(self) -> None:
        self.connected = True
        logger.info("Data channel open")
        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="go2.heartbeat")

    def _on_dc_close(self) -> None:
        self.connected = False
        logger.warning("Data channel closed")

    def _on_dc_message(self, message: str) -> None:
        try:
            data = json.loads(message)
            self._ingest_state(data)
        except json.JSONDecodeError:
            logger.debug("Non-JSON message on DC: %r", message[:80])

    # ── State ingestion ───────────────────────────────────────────────────────

    def _ingest_state(self, data: dict[str, Any]) -> None:
        """Parse incoming robot telemetry into RobotState."""
        topic = data.get("topic", "")
        body  = data.get("data", {})

        if "lowstate" in topic or "state" in topic.lower():
            imu = body.get("imu_state", {})
            self._state.imu_roll    = imu.get("rpy", [0, 0, 0])[0]
            self._state.imu_pitch   = imu.get("rpy", [0, 0, 0])[1]
            self._state.imu_yaw     = imu.get("rpy", [0, 0, 0])[2]
            self._state.battery_voltage = body.get("battery_voltage", self._state.battery_voltage)
            self._state.battery_percent = int(
                max(0, min(100, (self._state.battery_voltage - 21.0) / (29.4 - 21.0) * 100))
            )
            self._state.gait_mode   = body.get("gait_type", 0)
            self._state.mode_machine = body.get("mode_machine", 0)
            self._state.timestamp   = time.monotonic()
            self.last_state         = self._state.to_dict()

    # ── State query ───────────────────────────────────────────────────────────

    async def get_state(self) -> dict[str, Any] | None:
        if self.simulation:
            self._sim_step()
        return self.last_state

    # ── Motion commands ───────────────────────────────────────────────────────

    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        """Velocity command.  vx/vy in m/s, vyaw in rad/s."""
        vx   = max(-1.5, min(1.5, vx))
        vy   = max(-0.8, min(0.8, vy))
        vyaw = max(-2.0, min(2.0, vyaw))
        await self._send_sport_api(API_MOVE, {"x": vx, "y": vy, "z": vyaw})

    async def stop(self) -> None:
        await self._send_sport_api(API_STOP_MOVE)

    async def emergency_stop(self) -> None:
        """DAMP mode — motors go compliant.  Use only in genuine emergencies."""
        await self._send_sport_api(API_DAMP)
        logger.critical("Emergency DAMP sent to robot")

    async def stand_up(self) -> None:
        await self._send_sport_api(API_STAND_UP)

    async def stand_down(self) -> None:
        await self._send_sport_api(API_STAND_DOWN)

    async def recovery_stand(self) -> None:
        await self._send_sport_api(API_RECOVERY_STAND)

    async def set_body_height(self, height: float) -> None:
        """height: -0.18 (low) to 0.03 (high), meters offset from default."""
        height = max(-0.18, min(0.03, height))
        await self._send_sport_api(API_BODY_HEIGHT, {"data": height})

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        """Body orientation in radians."""
        await self._send_sport_api(API_EULER, {"x": roll, "y": pitch, "z": yaw})

    async def set_speed_level(self, level: int) -> None:
        """0=slow, 1=normal, 2=fast."""
        level = max(0, min(2, level))
        await self._send_sport_api(API_SPEED_LEVEL, {"data": level})

    async def wiggle_hips(self) -> None:
        await self._send_sport_api(API_WIGGLE_HIPS)

    async def heart(self) -> None:
        await self._send_sport_api(API_HEART)

    async def dance(self, variant: int = 1) -> None:
        api = API_DANCE1 if variant == 1 else API_DANCE2
        await self._send_sport_api(api)

    async def stretch(self) -> None:
        await self._send_sport_api(API_STRETCH)

    # ── Low-level send ────────────────────────────────────────────────────────

    async def _send_sport_api(
        self, api_id: int, parameter: dict[str, Any] | None = None
    ) -> None:
        if not self.connected:
            logger.debug("Dropped command %d — not connected", api_id)
            return
        if self.simulation:
            logger.debug("[SIM] sport_api %d  param=%s", api_id, parameter)
            return

        self._msg_id += 1
        msg = {
            "type":  "req",
            "topic": "rt/api/sport/request",
            "data": {
                "header": {"identity": {"id": self._msg_id, "api_id": api_id}},
                "parameter": json.dumps(parameter or {}),
            },
        }
        try:
            self._dc.send(json.dumps(msg))
        except Exception:
            logger.exception("Data channel send failed")

    async def _heartbeat_loop(self) -> None:
        while self.connected:
            try:
                self._dc.send(json.dumps({"type": "ping"}))
            except Exception:
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── Simulation helpers ────────────────────────────────────────────────────

    def _start_sim_loop(self) -> None:
        self._sim_t = 0.0
        self._state = RobotState(connected=True, battery_voltage=25.0, battery_percent=80)
        self.last_state = self._state.to_dict()
        logger.info("[SIM] Simulation state initialised")

    def _sim_step(self) -> None:
        import math
        self._sim_t = getattr(self, "_sim_t", 0.0) + 0.033
        self._state.battery_voltage = 25.0 - self._sim_t * 0.001
        self._state.imu_roll  = math.sin(self._sim_t * 0.5) * 2.0
        self._state.imu_pitch = math.cos(self._sim_t * 0.3) * 1.5
        self._state.timestamp = time.monotonic()
        self.last_state = self._state.to_dict()

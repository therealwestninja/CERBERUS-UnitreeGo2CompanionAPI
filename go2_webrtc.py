"""
cerberus/bridge/go2_webrtc.py  — CERBERUS v3.2
================================================
Go2WebRTCAdapter: wraps go2_webrtc_connect / unitree_webrtc_connect.

The original architecture used WebRTC as the primary transport (not DDS),
because it works with ALL models (Air/Pro/EDU) without jailbreak or firmware
modification — matching what the official Unitree app uses.

Falls back to SimBridge when GO2_SIMULATION=true.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from cerberus.bridge.go2_bridge import RobotState, SimBridge

logger = logging.getLogger(__name__)


class Go2WebRTCAdapter:
    """
    Wraps go2_webrtc_connect for Go2 AIR/PRO/EDU via Wi-Fi WebRTC.

    Usage:
        adapter = Go2WebRTCAdapter(robot_ip="192.168.8.1")
        await adapter.connect()
        await adapter.move(0.3, 0.0, 0.0)
    """

    # WebRTC API IDs for all sport modes
    _SPORT_API = {
        "damp": 1001, "balance_stand": 1002, "stand_down": 1003, "stand_up": 1004,
        "stop_move": 1005, "rise_sit": 1040, "sit": 1043, "stretch": 1044,
        "wallow": 1045, "hello": 1046, "scrape": 1047, "front_flip": 1048,
        "front_jump": 1049, "front_pounce": 1050, "dance1": 1051, "dance2": 1052,
        "finger_heart": 1053,
    }

    def __init__(self, robot_ip: str = "", serial_number: str = "",
                 username: str = "", password: str = "",
                 connection_method: str = "local_sta",
                 simulation: bool = False) -> None:
        self._ip     = robot_ip or os.getenv("GO2_IP", "192.168.8.1")
        self._serial = serial_number or os.getenv("GO2_SERIAL", "")
        self._user   = username or os.getenv("GO2_USERNAME", "")
        self._pwd    = password or os.getenv("GO2_PASSWORD", "")
        self._method = connection_method
        self._sim    = simulation or os.getenv("GO2_SIMULATION", "false").lower() in ("true","1","yes")

        self._conn   = None
        self._sim_bridge: SimBridge | None = None
        self._state  = RobotState()
        self._last_state: RobotState | None = None
        self.connected = False

    async def connect(self) -> None:
        if self._sim:
            self._sim_bridge = SimBridge()
            await self._sim_bridge.connect()
            self.connected = True
            logger.info("Go2WebRTCAdapter: simulation mode")
            return

        try:
            from go2_webrtc_connect import Go2WebRTCConnection, WebRTCConnectionMethod as M  # type: ignore
        except ImportError:
            try:
                from unitree_webrtc_connect import UnitreeWebRTCConnection as Go2WebRTCConnection, WebRTCConnectionMethod as M  # type: ignore
            except ImportError:
                logger.warning("WebRTC package not found — falling back to simulation")
                self._sim = True
                self._sim_bridge = SimBridge()
                await self._sim_bridge.connect()
                self.connected = True
                return

        method_map = {"ap": M.LocalAP, "local_sta": M.LocalSTA, "remote": M.Remote}
        method = method_map.get(self._method, M.LocalSTA)
        kw = {}
        if self._ip:     kw["ip"]           = self._ip
        if self._serial: kw["serialNumber"] = self._serial
        if self._user and self._pwd:
            kw["username"] = self._user; kw["password"] = self._pwd

        self._conn = Go2WebRTCConnection(method, **kw)
        await asyncio.to_thread(self._conn.connect)
        self.connected = True
        logger.info("Go2WebRTCAdapter connected (%s method=%s)", self._ip, self._method)

    async def disconnect(self) -> None:
        self.connected = False
        if self._sim_bridge:
            await self._sim_bridge.disconnect()
        elif self._conn:
            try:
                await asyncio.to_thread(self._conn.disconnect)
            except Exception:
                pass

    async def get_state(self) -> RobotState:
        if self._sim_bridge:
            self._last_state = await self._sim_bridge.get_state()
            return self._last_state
        if not self._conn:
            return self._state
        try:
            raw = await asyncio.to_thread(self._conn.getState)
            if raw:
                self._state = RobotState(
                    timestamp=time.time(),
                    velocity_x=getattr(raw, "vx", 0.0),
                    velocity_y=getattr(raw, "vy", 0.0),
                    velocity_yaw=getattr(raw, "vyaw", 0.0),
                    battery_voltage=getattr(raw, "battery_voltage", 0.0),
                )
        except Exception:
            pass
        self._last_state = self._state
        return self._state

    @property
    def last_state(self) -> RobotState | None:
        return self._last_state

    async def _pub(self, topic: str, api_id: int, param: str = "{}") -> bool:
        if self._sim_bridge:
            return True
        if not self._conn:
            return False
        import json
        try:
            await asyncio.to_thread(
                self._conn.datachannel.pub, topic,
                {"api_id": api_id, "parameter": param}
            )
            return True
        except Exception as e:
            logger.warning("WebRTC pub error: %s", e)
            return False

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.move(vx, vy, vyaw)
        import json
        vx   = max(-1.5, min(1.5, vx))
        vy   = max(-0.8, min(0.8, vy))
        vyaw = max(-2.0, min(2.0, vyaw))
        return await self._pub("rt/api/sport/request", 1008,
                               json.dumps({"x": vx, "y": vy, "z": vyaw}))

    async def stop(self) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.stop_move()
        return await self.execute_sport_mode("stop_move")

    async def stand_up(self) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.stand_up()
        return await self.execute_sport_mode("stand_up")

    async def stand_down(self) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.stand_down()
        return await self.execute_sport_mode("stand_down")

    async def execute_sport_mode(self, mode: str) -> bool:
        if self._sim_bridge:
            from cerberus.bridge.go2_bridge import SportMode
            try:
                sm = SportMode(mode)
                return await self._sim_bridge.execute_sport_mode(sm)
            except ValueError:
                return False
        api_id = self._SPORT_API.get(mode)
        if api_id is None:
            logger.error("Unknown sport mode: %s", mode)
            return False
        return await self._pub("rt/api/sport/request", api_id)

    async def set_body_height(self, height: float) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_body_height(height)
        import json
        return await self._pub("rt/api/sport/request", 1013,
                               json.dumps({"data": max(-0.1, min(0.1, height))}))

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_euler(roll, pitch, yaw)
        import json
        return await self._pub("rt/api/sport/request", 1025,
                               json.dumps({"x": roll, "y": pitch, "z": yaw}))

    async def set_speed_level(self, level: int) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_speed_level(level)
        import json
        return await self._pub("rt/api/sport/request", 1015,
                               json.dumps({"data": max(-1, min(1, level))}))

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_obstacle_avoidance(enabled)
        import json
        return await self._pub("rt/api/obstacles_avoid/request", 1003,
                               json.dumps({"data": int(enabled)}))

    async def set_led(self, r: int, g: int, b: int) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_led(r, g, b)
        import json
        return await self._pub("rt/api/vui/request", 1002,
                               json.dumps({"r": r, "g": g, "b": b}))

    async def set_volume(self, level: int) -> bool:
        if self._sim_bridge:
            return await self._sim_bridge.set_volume(level)
        import json
        return await self._pub("rt/api/vui/request", 1001,
                               json.dumps({"volume": max(0, min(100, level))}))

    async def emergency_stop(self) -> bool:
        logger.critical("EMERGENCY STOP")
        self._state.estop_active = True
        if self._sim_bridge:
            return await self._sim_bridge.emergency_stop()
        return await self.execute_sport_mode("damp")

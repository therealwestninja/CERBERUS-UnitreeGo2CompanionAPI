"""
CERBERUS — Buttplug.io Plugin
==============================
Subscribes to FUNSCRIPT_TICK and ROBOT_MOTION_UPDATE events.
Robot is master: its motion state drives connected Buttplug devices via
Intiface Central (ws://127.0.0.1:12345 by default).

Requires:
  pip install buttplug    # official PyPI package v1.0.0+

Device mapping (robot-as-master):
  FUNSCRIPT_TICK.position  →  vibration intensity on all VIBRATE devices
  FUNSCRIPT_TICK.velocity  →  linear/oscillation on POSITION devices
  ESTOP_TRIGGERED          →  immediate device.stop() on all devices

Security:
  • Only connects to a local Intiface instance (127.0.0.1 default).
    Configurable but should never be a public internet address.
  • Devices are stopped before plugin unloads.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from cerberus.core.event_bus import Event, EventType
from cerberus.core.plugin_base import CERBERUSPlugin, PluginManifest, PluginTrustLevel

logger = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    name        = "Buttplug",
    version     = "1.0.0",
    description = "Drive Buttplug.io / Intiface Central devices from robot motion",
    author      = "CERBERUS Contributors",
    trust_level = PluginTrustLevel.SANDBOX,
    capabilities = ["peripheral_output"],
    config_keys = ["intiface_url", "scan_timeout_s"],
)


class ButtplugPlugin(CERBERUSPlugin):

    def __init__(self) -> None:
        super().__init__(MANIFEST)
        self._client   = None
        self._url      = "ws://127.0.0.1:12345"
        self._scan_s   = 5.0
        self._devices: dict[str, Any] = {}
        self._connected = False
        self._last_intensity = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self, config: dict[str, Any]) -> None:
        self._url    = config.get("intiface_url", self._url)
        self._scan_s = float(config.get("scan_timeout_s", self._scan_s))

        # Verify buttplug package is available
        try:
            from buttplug import ButtplugClient  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "buttplug package not installed.  Run: pip install buttplug"
            ) from e

        self.bus.subscribe(EventType.FUNSCRIPT_TICK,    self._on_fs_tick,  priority=5)
        self.bus.subscribe(EventType.ROBOT_MOTION_UPDATE, self._on_motion, priority=5)
        self.bus.subscribe(EventType.ESTOP_TRIGGERED,   self._on_estop,    priority=1)
        self.bus.subscribe(EventType.FUNSCRIPT_STOP,    self._on_fs_stop,  priority=5)
        self.bus.subscribe(EventType.FUNSCRIPT_PAUSE,   self._on_fs_stop,  priority=5)

        logger.info("Buttplug plugin loaded — Intiface URL: %s", self._url)

    async def on_start(self) -> None:
        self._spawn(self._connect_loop(), name="buttplug.connect")

    async def on_stop(self) -> None:
        await self._stop_all_devices()

    async def on_unload(self) -> None:
        await self._stop_all_devices()
        await self._disconnect()

    # ── Connection loop ────────────────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        """Retry connection to Intiface until connected."""
        from buttplug import ButtplugClient

        while True:
            try:
                self._client = ButtplugClient("CERBERUS")
                self._client.on_device_added     = self._on_device_added
                self._client.on_device_removed   = self._on_device_removed
                self._client.on_server_disconnect = self._on_server_disconnect

                await self._client.connect(self._url)
                self._connected = True
                logger.info("Connected to Intiface Central at %s", self._url)

                await self._emit(EventType.PERIPHERAL_CONNECTED,
                                 {"service": "Intiface", "url": self._url}, priority=9)

                await self._client.start_scanning()
                await asyncio.sleep(self._scan_s)
                await self._client.stop_scanning()
                logger.info("Scan complete — %d device(s) found", len(self._client.devices))

                # Keep alive
                while self._connected:
                    await asyncio.sleep(1.0)

            except Exception as e:
                logger.warning("Intiface connection failed: %s — retrying in 5s", e)
                self._connected = False
                await asyncio.sleep(5.0)

    async def _disconnect(self) -> None:
        import contextlib
        if self._client and self._connected:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._connected = False
        self._devices.clear()

    # ── Device management ──────────────────────────────────────────────────────

    def _on_device_added(self, device: Any) -> None:
        self._devices[device.name] = device
        logger.info("Device added: %s", device.name)
        self.bus.publish_sync(Event(
            type=EventType.PERIPHERAL_CONNECTED,
            source=self.manifest.name,
            data={"device": device.name},
            priority=9,
        ))

    def _on_device_removed(self, device: Any) -> None:
        self._devices.pop(device.name, None)
        logger.info("Device removed: %s", device.name)
        self.bus.publish_sync(Event(
            type=EventType.PERIPHERAL_DISCONNECTED,
            source=self.manifest.name,
            data={"device": device.name},
            priority=9,
        ))

    def _on_server_disconnect(self) -> None:
        self._connected = False
        logger.warning("Intiface server disconnected")
        self.bus.publish_sync(Event(
            type=EventType.PERIPHERAL_DISCONNECTED,
            source=self.manifest.name,
            data={"service": "Intiface"},
            priority=5,
        ))

    # ── Event handlers ─────────────────────────────────────────────────────────

    async def _on_fs_tick(self, event: Event) -> None:
        pos      = float(event.data.get("position", 0.0))
        velocity = float(event.data.get("velocity", 0.0))
        await self._drive_devices(intensity=pos, velocity=velocity)

    async def _on_motion(self, event: Event) -> None:
        # Fallback when no funscript — use raw robot velocity as intensity
        vx = abs(float(event.data.get("vx", 0.0)))
        # map 0–1.5 m/s → 0–1.0 intensity
        intensity = min(1.0, vx / 1.5)
        if intensity != self._last_intensity:
            await self._drive_devices(intensity=intensity)

    async def _on_estop(self, event: Event) -> None:
        await self._stop_all_devices()

    async def _on_fs_stop(self, event: Event) -> None:
        await self._stop_all_devices()

    # ── Device control ─────────────────────────────────────────────────────────

    async def _drive_devices(self, intensity: float, velocity: float = 0.0) -> None:
        if not self._connected or not self._client:
            return

        self._last_intensity = intensity

        from buttplug import DeviceOutputCommand, OutputType  # type: ignore

        for device in self._client.devices.values():
            try:
                if device.has_output(OutputType.VIBRATE):
                    await device.run_output(
                        DeviceOutputCommand(OutputType.VIBRATE, intensity)
                    )
                if device.has_output(OutputType.ROTATE):
                    speed = min(1.0, abs(velocity) * 2.0)
                    await device.run_output(
                        DeviceOutputCommand(OutputType.ROTATE, speed)
                    )
                if device.has_output(OutputType.POSITION_WITH_DURATION):
                    await device.run_output(
                        DeviceOutputCommand(OutputType.POSITION_WITH_DURATION,
                                            intensity, duration=100)
                    )
            except Exception as e:
                logger.debug("Device command error (%s): %s", device.name, e)

    async def _stop_all_devices(self) -> None:
        import contextlib
        if not self._client:
            return
        for device in list(getattr(self._client, "devices", {}).values()):
            with contextlib.suppress(Exception):
                await device.stop()
        self._last_intensity = 0.0

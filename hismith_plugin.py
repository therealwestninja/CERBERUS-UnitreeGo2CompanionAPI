"""
CERBERUS — Hismith BLE Plugin
==============================
Controls Hismith sex machines via BLE GATT.
Robot is master: FUNSCRIPT_TICK.position drives stroke speed.

Protocol notes (from community reverse engineering):
  • Hismith devices advertise under name prefix "Hismith" or "BM-"
  • Primary service:   0000fff0-0000-1000-8000-00805f9b34fb
  • Control char:      0000fff2-0000-1000-8000-00805f9b34fb (write-no-response)
  • Notify char:       0000fff1-0000-1000-8000-00805f9b34fb
  • Speed command:     bytes [0xFE, speed_byte, 0xFF]
    where speed_byte = 0x00 (stop) … 0x64 (100%, max speed)

Source references:
  https://github.com/samsmit362/HismithControl
  https://github.com/AleksandrPanarin/hismith
  https://github.com/maxim-emelyanov/HiSmith

Requires:
  pip install bleak
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from cerberus.core.event_bus import Event, EventType
from cerberus.core.plugin_base import CERBERUSPlugin, PluginManifest, PluginTrustLevel

logger = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    name        = "Hismith",
    version     = "1.0.0",
    description = "BLE control of Hismith machines from robot motion",
    author      = "CERBERUS Contributors",
    trust_level = PluginTrustLevel.SANDBOX,
    capabilities = ["peripheral_output", "ble"],
    config_keys = ["device_address", "scan_timeout_s", "max_speed_pct"],
)

# BLE UUIDs
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CTRL_CHAR    = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR  = "0000fff1-0000-1000-8000-00805f9b34fb"

# Device name prefixes to scan for
DEVICE_PREFIXES = ("Hismith", "BM-", "hismith")


def _speed_packet(speed_pct: float) -> bytes:
    """Encode speed (0.0–1.0) as the Hismith BLE command."""
    byte_val = int(max(0.0, min(1.0, speed_pct)) * 0x64)
    return bytes([0xFE, byte_val, 0xFF])


class HismithPlugin(CERBERUSPlugin):

    def __init__(self) -> None:
        super().__init__(MANIFEST)
        self._address:   str | None = None
        self._client     = None     # bleak.BleakClient
        self._connected  = False
        self._scan_s     = 10.0
        self._max_speed  = 1.0      # cap safety limit
        self._last_speed = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self, config: dict[str, Any]) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError as e:
            raise RuntimeError("bleak not installed.  Run: pip install bleak") from e

        self._address  = config.get("device_address")    # optional pre-set MAC
        self._scan_s   = float(config.get("scan_timeout_s", self._scan_s))
        self._max_speed = float(config.get("max_speed_pct", 100)) / 100.0

        self.bus.subscribe(EventType.FUNSCRIPT_TICK,  self._on_fs_tick, priority=5)
        self.bus.subscribe(EventType.ESTOP_TRIGGERED, self._on_estop,   priority=1)
        self.bus.subscribe(EventType.FUNSCRIPT_STOP,  self._on_stop,    priority=5)
        self.bus.subscribe(EventType.FUNSCRIPT_PAUSE, self._on_stop,    priority=5)

        logger.info("Hismith plugin loaded  max_speed=%.0f%%", self._max_speed * 100)

    async def on_start(self) -> None:
        self._spawn(self._connect_loop(), name="hismith.connect")

    async def on_stop(self) -> None:
        await self._set_speed(0.0)

    async def on_unload(self) -> None:
        await self._set_speed(0.0)
        await self._disconnect()

    # ── BLE connection loop ────────────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        from bleak import BleakClient, BleakScanner

        while True:
            try:
                address = self._address

                if not address:
                    logger.info("Scanning for Hismith device (%.0fs)…", self._scan_s)
                    address = await self._scan_for_device(BleakScanner)
                    if not address:
                        logger.warning("No Hismith device found — retrying in 10s")
                        await asyncio.sleep(10.0)
                        continue

                logger.info("Connecting to Hismith: %s", address)
                async with BleakClient(address, disconnected_callback=self._on_disconnect) as client:
                    self._client    = client
                    self._connected = True
                    logger.info("Hismith connected: %s", address)
                    await self._emit(EventType.PERIPHERAL_CONNECTED,
                                     {"device": "Hismith", "address": address}, priority=9)

                    # Subscribe to notify characteristic for status readback
                    try:
                        await client.start_notify(NOTIFY_CHAR, self._on_notify)
                    except Exception:
                        logger.debug("Notify not available on this device")

                    while self._connected:
                        await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning("Hismith BLE error: %s — retrying in 5s", e)
                self._connected = False
                self._client    = None
                await asyncio.sleep(5.0)

    async def _scan_for_device(self, BleakScanner: Any) -> str | None:
        devices = await BleakScanner.discover(timeout=self._scan_s)
        for d in devices:
            if d.name and any(d.name.startswith(p) for p in DEVICE_PREFIXES):
                logger.info("Found Hismith: %s (%s)", d.name, d.address)
                return d.address
        return None

    async def _disconnect(self) -> None:
        import contextlib
        self._connected = False
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._client = None

    def _on_disconnect(self, _: Any) -> None:
        self._connected = False
        self._client    = None
        logger.warning("Hismith disconnected")
        self.bus.publish_sync(Event(
            type=EventType.PERIPHERAL_DISCONNECTED,
            source=self.manifest.name,
            data={"device": "Hismith"},
            priority=5,
        ))

    def _on_notify(self, handle: int, data: bytearray) -> None:
        logger.debug("Hismith notify [%d]: %s", handle, data.hex())

    # ── Event handlers ─────────────────────────────────────────────────────────

    async def _on_fs_tick(self, event: Event) -> None:
        pos = float(event.data.get("position", 0.0))
        await self._set_speed(pos)

    async def _on_estop(self, event: Event) -> None:
        await self._set_speed(0.0)

    async def _on_stop(self, event: Event) -> None:
        await self._set_speed(0.0)

    # ── BLE write ──────────────────────────────────────────────────────────────

    async def _set_speed(self, speed: float) -> None:
        """Send speed command.  speed: 0.0 (stop) – 1.0 (max)."""
        speed = min(speed, self._max_speed)

        if abs(speed - self._last_speed) < 0.01:
            return                       # no meaningful change — skip write

        if not self._connected or not self._client:
            return

        self._last_speed = speed
        packet = _speed_packet(speed)
        try:
            await self._client.write_gatt_char(CTRL_CHAR, packet, response=False)
            logger.debug("Hismith speed → %.0f%%", speed * 100)
        except Exception as e:
            logger.warning("Hismith write failed: %s", e)
            self._connected = False

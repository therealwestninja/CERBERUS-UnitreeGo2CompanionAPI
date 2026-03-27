"""
CERBERUS — Samsung Galaxy Fit 2 Plugin
========================================
Reads heart rate from a Galaxy Fit 2 wearable via BLE.
Publishes HEARTRATE_UPDATE events which feed the SafetyManager.

Safety integration (robot-as-master, wearable as safety input):
  • HR > 180  →  HEARTRATE_ALARM  →  SafetyManager pauses interaction
  • HR > 200  →  ESTOP_TRIGGERED  (hard stop, priority-1)
  • HR < 40   →  ESTOP_TRIGGERED  (operator unresponsive / sensor loss)
  • Wearable disconnect while active → WEARABLE_DISCONNECTED (logged, no auto-stop)

Galaxy Fit 2 BLE notes (from msmuenchen's reverse-engineering gist):
  https://gist.github.com/msmuenchen/d2a738b85342f57e423c0b197f278fe3

  The Galaxy Fit 2 DOES support standard BLE Heart Rate Service (0x180D)
  when in "Accessory Mode".  The primary method uses:
    Service:   0000180d-0000-1000-8000-00805f9b34fb  (Heart Rate Service)
    Char:      00002a37-0000-1000-8000-00805f9b34fb  (Heart Rate Measurement)
    Notify:    subscribe with start_notify()
    Payload:   [flags_byte, hr_byte, ...]  per BLE GATT spec §3.106

  If the device does not expose standard HR service (older firmware),
  the plugin falls back to Samsung's proprietary SPP-over-BLE channel
  using the secondary service UUID defined below.

Requires:
  pip install bleak
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from cerberus.core.event_bus import Event, EventType
from cerberus.core.plugin_base import CERBERUSPlugin, PluginManifest, PluginTrustLevel

logger = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    name        = "GalaxyFit2",
    version     = "1.0.0",
    description = "Samsung Galaxy Fit 2 heart-rate monitor with safety integration",
    author      = "CERBERUS Contributors",
    trust_level = PluginTrustLevel.CORE,     # Can trigger safety / estop
    capabilities = ["bio_sensor", "safety_input"],
    config_keys = ["device_address", "device_name", "scan_timeout_s"],
)

# Standard BLE Heart Rate Service
BLE_HR_SERVICE  = "0000180d-0000-1000-8000-00805f9b34fb"
BLE_HR_CHAR     = "00002a37-0000-1000-8000-00805f9b34fb"

# Samsung proprietary fallback (Galaxy Fit 2 custom service)
# Ref: msmuenchen gist — 0x6217 service prefix
SAMSUNG_SERVICE  = "00006217-0000-1000-8000-00805f9b34fb"
SAMSUNG_CTRL_CHAR = "00006218-0000-1000-8000-00805f9b34fb"
SAMSUNG_DATA_CHAR = "00006219-0000-1000-8000-00805f9b34fb"

DEVICE_NAME_HINTS = ("Galaxy Fit2", "SM-R220", "Fit2")

# HR that triggers a single-sample outlier discard
HR_OUTLIER_THRESHOLD = 250


class GalaxyFit2Plugin(CERBERUSPlugin):

    def __init__(self) -> None:
        super().__init__(MANIFEST)
        self._address:    str | None = None
        self._name_hint:  str        = "Galaxy Fit2"
        self._scan_s:     float      = 15.0
        self._client      = None
        self._connected   = False
        self._mode:       str        = "standard"   # "standard" | "proprietary"
        self._hr_samples: list[int]  = []
        self._last_bpm:   int        = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self, config: dict[str, Any]) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError as e:
            raise RuntimeError("bleak not installed.  Run: pip install bleak") from e

        self._address   = config.get("device_address")
        self._name_hint = config.get("device_name", self._name_hint)
        self._scan_s    = float(config.get("scan_timeout_s", self._scan_s))

        # Register ESTOP subscription so wearable disconnect is logged cleanly
        self.bus.subscribe(EventType.ESTOP_TRIGGERED, self._on_estop, priority=1)
        logger.info("GalaxyFit2 plugin loaded")

    async def on_start(self) -> None:
        self._spawn(self._connect_loop(), name="galaxyfit2.connect")

    async def on_stop(self) -> None:
        await self._disconnect()

    async def on_unload(self) -> None:
        await self._disconnect()

    # ── BLE connection loop ────────────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        from bleak import BleakClient, BleakScanner

        while True:
            try:
                address = self._address or await self._scan(BleakScanner)
                if not address:
                    logger.warning("Galaxy Fit 2 not found — retrying in 15s")
                    await asyncio.sleep(15.0)
                    continue

                logger.info("Connecting to Galaxy Fit 2: %s", address)
                async with BleakClient(
                    address,
                    disconnected_callback=self._on_ble_disconnect,
                ) as client:
                    self._client    = client
                    self._connected = True

                    # Discover services and pick a mode
                    services = client.services
                    uuids    = {str(s.uuid) for s in services}
                    if BLE_HR_SERVICE in uuids:
                        self._mode = "standard"
                    elif SAMSUNG_SERVICE in uuids:
                        self._mode = "proprietary"
                    else:
                        logger.warning("No known HR service — trying standard anyway")
                        self._mode = "standard"

                    logger.info("Galaxy Fit 2 connected  mode=%s", self._mode)
                    await self._emit(EventType.WEARABLE_CONNECTED,
                                     {"address": address, "mode": self._mode}, priority=9)

                    await self._start_hr_notifications(client)

                    while self._connected:
                        await asyncio.sleep(1.0)

            except Exception as e:
                logger.warning("Galaxy Fit 2 BLE error: %s — retrying in 10s", e)
                self._connected = False
                self._client    = None
                await asyncio.sleep(10.0)

    async def _scan(self, BleakScanner: Any) -> str | None:
        logger.info("Scanning for Galaxy Fit 2 (%.0fs)…", self._scan_s)
        devices = await BleakScanner.discover(timeout=self._scan_s)
        for d in devices:
            if d.name and any(h in d.name for h in DEVICE_NAME_HINTS):
                logger.info("Found: %s (%s)", d.name, d.address)
                return d.address
        return None

    async def _start_hr_notifications(self, client: Any) -> None:
        if self._mode == "standard":
            await client.start_notify(BLE_HR_CHAR, self._on_hr_standard)
            logger.info("Subscribed to standard BLE HR notifications")
        else:
            # Proprietary mode: send init command then subscribe to data char
            await self._samsung_init(client)
            await client.start_notify(SAMSUNG_DATA_CHAR, self._on_hr_proprietary)
            logger.info("Subscribed to Samsung proprietary HR notifications")

    async def _samsung_init(self, client: Any) -> None:
        """
        Send the Samsung proprietary handshake to activate HR streaming.
        Based on msmuenchen's gist reverse engineering.
        Init command: 0x01 0x00 0x01 (start continuous HR measurement)
        """
        try:
            await client.write_gatt_char(
                SAMSUNG_CTRL_CHAR,
                bytes([0x01, 0x00, 0x01]),
                response=True,
            )
            logger.debug("Samsung HR init sent")
        except Exception as e:
            logger.warning("Samsung HR init failed: %s", e)

    async def _disconnect(self) -> None:
        import contextlib
        self._connected = False
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._client = None

    def _on_ble_disconnect(self, _: Any) -> None:
        self._connected = False
        self._client    = None
        logger.warning("Galaxy Fit 2 disconnected")
        self.bus.publish_sync(Event(
            type=EventType.WEARABLE_DISCONNECTED,
            source=self.manifest.name,
            data={},
            priority=5,
        ))

    # ── HR notification parsers ────────────────────────────────────────────────

    def _on_hr_standard(self, handle: int, data: bytearray) -> None:
        """
        BLE Heart Rate Measurement characteristic format (GATT spec):
          Byte 0: flags
            bit 0: 0 = HR is uint8,  1 = HR is uint16
          Byte 1 (or bytes 1–2): heart rate value
        """
        if len(data) < 2:
            return
        flags = data[0]
        bpm = struct.unpack_from("<H", data, 1)[0] if flags & 0x01 else data[1]
        self._handle_bpm(bpm)

    def _on_hr_proprietary(self, handle: int, data: bytearray) -> None:
        """
        Samsung proprietary format (from reverse engineering):
          Byte 0: message type (0x84 = HR data)
          Byte 1: HR value
        """
        if len(data) >= 2 and data[0] == 0x84:
            self._handle_bpm(data[1])
        elif len(data) >= 2:
            logger.debug("Proprietary packet type 0x%02x: %s", data[0], data.hex())

    def _handle_bpm(self, raw_bpm: int) -> None:
        """Validate, smooth, and publish the HR reading."""
        # Discard obvious sensor glitches
        if raw_bpm == 0 or raw_bpm > HR_OUTLIER_THRESHOLD:
            logger.debug("HR outlier discarded: %d", raw_bpm)
            return

        # Rolling 3-sample median for smoothing
        self._hr_samples.append(raw_bpm)
        if len(self._hr_samples) > 3:
            self._hr_samples.pop(0)
        bpm = sorted(self._hr_samples)[len(self._hr_samples) // 2]

        if bpm == self._last_bpm:
            return    # no change — don't flood the bus

        self._last_bpm = bpm
        logger.debug("HR: %d bpm", bpm)

        # Publish — SafetyManager will act if thresholds exceeded
        self.bus.publish_sync(Event(
            type=EventType.HEARTRATE_UPDATE,
            source=self.manifest.name,
            data={"bpm": bpm},
            priority=5,
        ))

    # ── Event handlers ─────────────────────────────────────────────────────────

    async def _on_estop(self, event: Event) -> None:
        logger.info("E-stop received — wearable continues monitoring")
        # Don't disconnect on estop — we need HR to know when it's safe to resume

    # ── Status ────────────────────────────────────────────────────────────────

    @property
    def current_bpm(self) -> int:
        return self._last_bpm

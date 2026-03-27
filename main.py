"""
CERBERUS main.py
=================
Entry point.  Wires all components and starts the runtime.

Usage:
  python main.py                   # real robot (reads .env / config/cerberus.yaml)
  python main.py --simulation      # no hardware required
  python main.py --no-ui           # headless / server mode

Components started:
  1. Event bus
  2. Safety manager
  3. Go2 WebRTC adapter
  4. Plugin registry (FunScript, Buttplug, Hismith, GalaxyFit2)
  5. FastAPI server (background thread)
  6. Dear PyGui UI (main thread or background thread)
  7. Runtime tick loop
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading
from typing import Any

import uvicorn
import yaml

from cerberus.core.event_bus import get_bus
from cerberus.core.runtime import CERBERUSRuntime
from cerberus.robot.go2_webrtc import Go2WebRTCAdapter
from plugins.buttplug.buttplug_plugin import ButtplugPlugin
from plugins.funscript.funscript_player import FunScriptPlugin
from plugins.galaxy_fit2.galaxy_fit2_plugin import GalaxyFit2Plugin
from plugins.hismith.hismith_plugin import HismithPlugin
from ui.ui_bridge import get_bridge

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("cerberus.main")


# ── Config loader ──────────────────────────────────────────────────────────────

def load_config(path: str = "config/cerberus.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Config not found at %s — using defaults", path)
        return {}


# ── Background API server ──────────────────────────────────────────────────────

def start_api_server(bridge: Any, runtime: Any, host: str = "0.0.0.0", port: int = 8080) -> None:
    from backend.api.server import create_app
    app = create_app(bridge=bridge, runtime=runtime)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


# ── Bus event wiring for UIBridge ─────────────────────────────────────────────

def wire_bridge_subscriptions(bridge: Any, bus: Any) -> None:
    """Wire bus events → UIBridge state updates."""
    from cerberus.core.event_bus import Event, EventType

    async def on_state_push(event: Event) -> None:
        bridge.push_state(event.data)

    async def on_heartrate(event: Event) -> None:
        bridge.update_hr(event.data.get("bpm", 0))

    async def on_hr_alarm(event: Event) -> None:
        bridge.update_hr(event.data.get("bpm", 0), alarm=True)

    async def on_fs_tick(event: Event) -> None:
        bridge.update_fs(
            position_ms   = event.data.get("position_ms", 0),
            position_norm = event.data.get("position", 0),
            playing       = True,
        )

    async def on_fs_loaded(event: Event) -> None:
        bridge.update_fs(
            loaded      = True,
            path        = event.data.get("path", ""),
            duration_ms = event.data.get("duration_ms", 0),
        )

    async def on_fs_stop(event: Event) -> None:
        bridge.update_fs(playing=False, position_ms=0, position_norm=0)

    async def on_peripheral_connected(event: Event) -> None:
        bridge.update_peripheral(
            event.data.get("service") or event.data.get("device", ""), True
        )

    async def on_peripheral_disconnected(event: Event) -> None:
        bridge.update_peripheral(
            event.data.get("service") or event.data.get("device", ""), False
        )

    bus.subscribe(EventType.UI_STATE_PUSH,         on_state_push,          priority=9)
    bus.subscribe(EventType.HEARTRATE_UPDATE,      on_heartrate,           priority=5)
    bus.subscribe(EventType.HEARTRATE_ALARM,       on_hr_alarm,            priority=2)
    bus.subscribe(EventType.FUNSCRIPT_TICK,        on_fs_tick,             priority=5)
    bus.subscribe(EventType.FUNSCRIPT_LOADED,      on_fs_loaded,           priority=5)
    bus.subscribe(EventType.FUNSCRIPT_STOP,        on_fs_stop,             priority=5)
    bus.subscribe(EventType.FUNSCRIPT_PAUSE,       on_fs_stop,             priority=5)
    bus.subscribe(EventType.PERIPHERAL_CONNECTED,  on_peripheral_connected, priority=9)
    bus.subscribe(EventType.PERIPHERAL_DISCONNECTED, on_peripheral_disconnected, priority=9)

    bridge.set_bus(bus)


# ── Main coroutine ─────────────────────────────────────────────────────────────

async def async_main(args: argparse.Namespace, config: dict) -> None:
    bus    = get_bus()
    bridge = get_bridge()

    wire_bridge_subscriptions(bridge, bus)

    # Robot adapter
    robot_ip  = config.get("robot", {}).get("ip", "192.168.123.1")
    sim_mode  = args.simulation or config.get("robot", {}).get("simulation", False)
    robot     = Go2WebRTCAdapter(robot_ip=robot_ip, simulation=sim_mode)

    # Runtime
    runtime = CERBERUSRuntime(robot_adapter=robot)

    # Plugins
    fs_plugin  = FunScriptPlugin(robot_adapter=robot)
    bp_plugin  = ButtplugPlugin()
    hi_plugin  = HismithPlugin()
    gf2_plugin = GalaxyFit2Plugin()

    await runtime.load_plugin(fs_plugin,  config.get("plugins", {}).get("funscript",   {}))
    await runtime.load_plugin(bp_plugin,  config.get("plugins", {}).get("buttplug",    {}))
    await runtime.load_plugin(hi_plugin,  config.get("plugins", {}).get("hismith",     {}))
    await runtime.load_plugin(gf2_plugin, config.get("plugins", {}).get("galaxy_fit2", {}))

    # Background API server
    if not args.no_api:
        api_host = config.get("api", {}).get("host", "0.0.0.0")
        api_port = int(config.get("api", {}).get("port", 8080))
        api_thread = threading.Thread(
            target   = start_api_server,
            args     = (bridge, runtime, api_host, api_port),
            daemon   = True,
            name     = "cerberus.api",
        )
        api_thread.start()
        logger.info("API server started on %s:%d", api_host, api_port)

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_running_loop()
    def _shutdown(*_: Any) -> None:
        asyncio.run_coroutine_threadsafe(runtime.shutdown(), loop)
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Starting runtime  [simulation=%s]", sim_mode)
    await runtime.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="CERBERUS Robotics Platform")
    parser.add_argument("--simulation", action="store_true",
                        help="Run without hardware (no robot / BLE connections)")
    parser.add_argument("--no-ui",     action="store_true",
                        help="Skip Dear PyGui (headless / server mode)")
    parser.add_argument("--no-api",    action="store_true",
                        help="Skip FastAPI server")
    parser.add_argument("--config",    default="config/cerberus.yaml",
                        help="Path to configuration YAML")
    args   = parser.parse_args()
    config = load_config(args.config)

    if args.no_ui:
        # Pure async — no UI thread needed
        asyncio.run(async_main(args, config))
    else:
        # UI owns the main thread; runtime gets a dedicated asyncio thread
        runtime_thread = threading.Thread(
            target = lambda: asyncio.run(async_main(args, config)),
            daemon = True,
            name   = "cerberus.runtime",
        )
        runtime_thread.start()

        from ui.cerberus_ui import launch_ui
        launch_ui(bridge=get_bridge())          # blocks until window closes

        runtime_thread.join(timeout=3)


if __name__ == "__main__":
    main()

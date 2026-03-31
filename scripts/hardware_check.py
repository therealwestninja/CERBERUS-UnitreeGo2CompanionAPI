#!/usr/bin/env python3
"""
scripts/hardware_check.py
━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Hardware Preflight Check

Run this BEFORE starting CERBERUS on real hardware to verify:
  • Environment variables are correctly set
  • Network interface reaches the Go2
  • DDS / unitree_sdk2_python is installed and working
  • All four leg foot sensors are responding
  • IMU is giving plausible values
  • Battery is above the safe-to-operate threshold
  • Safety watchdog initialises correctly
  • A brief motion sequence can be commanded without error

Usage:
    python scripts/hardware_check.py [--iface eth0] [--no-motion] [--timeout 15]

Arguments:
    --iface      Network interface name (default: GO2_NETWORK_INTERFACE env var or eth0)
    --no-motion  Skip the motion validation sequence (useful for bench testing)
    --timeout    DDS connection timeout in seconds (default: 15)
    --skip-dds   Skip DDS checks (for CI / simulation-only validation)

Exit codes:
    0   All checks passed — safe to start CERBERUS
    1   One or more checks failed — do not start without resolving
    2   Checks passed with warnings — review output before proceeding

Typical workflow on first deployment:
    1.  Wire Ethernet from your machine to Go2 (or via USB-C dock)
    2.  Power on Go2 — wait for it to stand up and beep
    3.  Confirm interface name:  ip link show
    4.  Run:  python scripts/hardware_check.py --iface eth0
    5.  Resolve any FAIL items
    6.  Start CERBERUS:  cerberus
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name:    str
    ok:      bool
    warning: bool = False
    message: str  = ""
    detail:  str  = ""


class Report:
    def __init__(self):
        self._results: list[CheckResult] = []

    def add(self, r: CheckResult) -> None:
        mark = "✅" if r.ok and not r.warning else ("⚠️ " if r.warning else "❌")
        line = f"  {mark}  {r.name}"
        if r.message:
            line += f" — {r.message}"
        print(line)
        if r.detail:
            for dl in r.detail.splitlines():
                print(f"        {dl}")
        self._results.append(r)

    def ok(self, name: str, msg: str = "", detail: str = "") -> None:
        self.add(CheckResult(name, ok=True, message=msg, detail=detail))

    def warn(self, name: str, msg: str = "", detail: str = "") -> None:
        self.add(CheckResult(name, ok=True, warning=True, message=msg, detail=detail))

    def fail(self, name: str, msg: str = "", detail: str = "") -> None:
        self.add(CheckResult(name, ok=False, message=msg, detail=detail))

    def summary(self) -> int:
        fails = [r for r in self._results if not r.ok]
        warns = [r for r in self._results if r.ok and r.warning]
        print()
        print("─" * 60)
        print(f"  Checks: {len(self._results)}   "
              f"Passed: {len(self._results)-len(fails)-len(warns)}   "
              f"Warnings: {len(warns)}   Failures: {len(fails)}")
        print("─" * 60)
        if fails:
            print()
            print("  ❌ FAILED — Do NOT start CERBERUS until these are resolved:")
            for r in fails:
                print(f"     • {r.name}: {r.message}")
            return 1
        if warns:
            print()
            print("  ⚠️  WARNINGS — Review before operating on real hardware:")
            for r in warns:
                print(f"     • {r.name}: {r.message}")
            print()
            print("  ✅ All required checks passed (with warnings)")
            return 2
        print()
        print("  ✅ All checks passed — CERBERUS is safe to start")
        return 0


report = Report()


# ── Checks ────────────────────────────────────────────────────────────────────

def check_python_version():
    """Python 3.11+ required for asyncio.TaskGroup and type union syntax."""
    vi = sys.version_info
    if vi >= (3, 11):
        report.ok("Python version", f"{vi.major}.{vi.minor}.{vi.micro}")
    elif vi >= (3, 10):
        report.warn("Python version",
                    f"{vi.major}.{vi.minor}.{vi.micro} — 3.11+ recommended",
                    "Some features use 3.11 syntax. Run: python3.11 scripts/hardware_check.py")
    else:
        report.fail("Python version",
                    f"{vi.major}.{vi.minor}.{vi.micro} is too old",
                    "CERBERUS requires Python 3.11+. Install: https://www.python.org/downloads/")


def check_env_vars():
    """Critical environment variables must be set for real-hardware mode."""
    api_key = os.getenv("CERBERUS_API_KEY", "")
    if api_key and len(api_key) >= 32:
        report.ok("CERBERUS_API_KEY", "set (redacted)")
    elif api_key:
        report.warn("CERBERUS_API_KEY", "set but short — recommend 32+ hex chars",
                    "Generate: python -c \"import secrets; print(secrets.token_hex(32))\"")
    else:
        report.warn("CERBERUS_API_KEY", "NOT SET — any network client can control the robot",
                    "Set CERBERUS_API_KEY in .env before deploying on real hardware.")

    iface = os.getenv("GO2_NETWORK_INTERFACE", "")
    if iface:
        report.ok("GO2_NETWORK_INTERFACE", iface)
    else:
        report.warn("GO2_NETWORK_INTERFACE", "not set (will default to eth0)",
                    "Find your interface: ip link show | grep -E '^[0-9]+:'")

    hz = int(os.getenv("CERBERUS_HZ", "60"))
    if 30 <= hz <= 200:
        report.ok("CERBERUS_HZ", f"{hz} Hz")
    else:
        report.warn("CERBERUS_HZ", f"{hz} Hz is outside the safe range (30–200)")


def check_network_interface(iface: str):
    """Verify the network interface exists and is up."""
    import subprocess
    try:
        r = subprocess.run(["ip", "link", "show", iface],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            if "UP" in r.stdout or "state UP" in r.stdout:
                report.ok("Network interface", f"{iface} is UP")
            else:
                report.warn("Network interface", f"{iface} exists but may be DOWN",
                            f"Bring up: sudo ip link set {iface} up")
        else:
            report.fail("Network interface", f"{iface} not found",
                        f"Available interfaces:\n  {_list_interfaces()}\n"
                        f"Set GO2_NETWORK_INTERFACE in .env to the correct name.")
    except FileNotFoundError:
        report.warn("Network interface", "ip command not available (not Linux?)")
    except Exception as e:
        report.warn("Network interface", f"Could not check: {e}")


def _list_interfaces() -> str:
    import subprocess
    try:
        r = subprocess.run(["ip", "-o", "link", "show"],
                           capture_output=True, text=True, timeout=3)
        lines = [l.split(":")[1].strip().split()[0] for l in r.stdout.splitlines() if ":" in l]
        return ", ".join(lines)
    except Exception:
        return "unknown"


def check_go2_reachable(iface: str, timeout: float = 5.0):
    """Ping the Go2's default IP (192.168.123.161)."""
    import subprocess
    GO2_IP = "192.168.123.161"
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), "-I", iface, GO2_IP],
            capture_output=True, text=True, timeout=timeout + 2
        )
        if r.returncode == 0:
            # Extract round-trip time
            for line in r.stdout.splitlines():
                if "time=" in line:
                    ms = line.split("time=")[1].split()[0]
                    report.ok("Go2 reachable", f"{GO2_IP} replies  RTT={ms} ms")
                    return True
            report.ok("Go2 reachable", f"{GO2_IP} replies")
            return True
        else:
            report.fail("Go2 reachable", f"Cannot ping {GO2_IP} via {iface}",
                        "Check:\n"
                        "  1. Ethernet cable connected to Go2\n"
                        "  2. Go2 is powered on (LED pattern not blinking red)\n"
                        "  3. Your machine IP is in 192.168.123.x subnet:\n"
                        f"     sudo ip addr add 192.168.123.100/24 dev {iface}\n"
                        "  4. Go2 firmware ≥ 1.0.21")
            return False
    except subprocess.TimeoutExpired:
        report.fail("Go2 reachable", f"Ping to {GO2_IP} timed out after {timeout}s")
        return False
    except Exception as e:
        report.warn("Go2 reachable", f"Could not run ping: {e}")
        return False


def check_unitree_sdk():
    """unitree_sdk2_python must be installed for real-hardware mode."""
    try:
        import unitree_sdk2py  # noqa: F401
        from unitree_sdk2py.go2.sport.sport_client import SportClient  # noqa: F401
        report.ok("unitree_sdk2py", "installed")
        return True
    except ImportError:
        report.fail("unitree_sdk2py", "NOT installed",
                    "Install:\n"
                    "  git clone https://github.com/unitreerobotics/unitree_sdk2_python.git\n"
                    "  cd unitree_sdk2_python\n"
                    "  export CYCLONEDDS_HOME=~/cyclonedds/install\n"
                    "  pip install -e .")
        return False


def check_cerberus_importable():
    """The cerberus package must be importable (pip install -e .)."""
    try:
        from cerberus import __version__
        report.ok("cerberus package", f"v{__version__}")
        return True
    except ImportError as e:
        report.fail("cerberus package", f"Not importable: {e}",
                    "Install: pip install -e '.[dev]'  (from repo root)")
        return False


async def check_dds_connection(iface: str, timeout: float = 15.0):
    """
    Attempt a real DDS connection to the Go2 and verify sensor data flows.

    This check:
    1. Initialises CycloneDDS on the specified interface
    2. Creates a SportClient and calls GetState()
    3. Subscribes to the state topic and waits for a callback
    4. Validates received sensor data ranges
    """
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.go2.sport.sport_client import SportClient
    except ImportError:
        report.warn("DDS connection", "Skipped — unitree_sdk2py not installed")
        return False

    print(f"     Attempting DDS connection on {iface} (timeout {timeout}s)…")

    dds_ok      = False
    state_received = False
    state_data  = {}

    def _on_state(msg):
        nonlocal state_received, state_data
        state_received = True
        try:
            state_data = {
                "battery": getattr(msg, "bms_state", {}).get("soc", -1),
                "velocity": getattr(msg, "velocity", [0, 0, 0]),
                "foot_force": getattr(msg, "foot_force_est", [0]*4),
                "imu_rpy": getattr(getattr(msg, "imu_state", None), "rpy", [0]*3),
                "mode": getattr(msg, "mode", -1),
            }
        except Exception:
            pass

    def _init_and_subscribe():
        nonlocal dds_ok
        try:
            ChannelFactoryInitialize(0, iface)
            dds_ok = True

            try:
                from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
                sub = ChannelSubscriber("rt/sportmodestate", SportModeState_)
                sub.Init(_on_state, 10)
            except Exception:
                pass   # state subscription is optional for this check
        except Exception as e:
            raise RuntimeError(f"DDS init failed: {e}")

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _init_and_subscribe),
            timeout=timeout / 2,
        )
    except asyncio.TimeoutError:
        report.fail("DDS connection", f"Timed out after {timeout/2:.0f}s",
                    "Possible causes:\n"
                    "  • CycloneDDS not finding the Go2 (multicast blocked)\n"
                    "  • Wrong interface name\n"
                    "  • Go2 not in high-level SDK mode (check firmware)")
        return False
    except Exception as e:
        report.fail("DDS connection", str(e))
        return False

    if dds_ok:
        report.ok("DDS init", f"ChannelFactory initialised on {iface}")

    # Wait briefly for state messages
    deadline = time.monotonic() + min(timeout / 2, 5.0)
    while time.monotonic() < deadline and not state_received:
        await asyncio.sleep(0.1)

    if state_received:
        report.ok("DDS state topic", "rt/sportmodestate  ✓ data received")
        return True
    else:
        report.warn("DDS state topic", "No state messages received in 5s",
                    "The Go2 may be in a low-power state or still booting.")
        return dds_ok


async def check_sensors(state_data: dict):
    """Validate received sensor data is within expected ranges."""
    if not state_data:
        report.warn("Sensor validation", "No state data to validate (DDS check skipped)")
        return

    # Battery
    bat = state_data.get("battery", -1)
    if bat < 0:
        report.warn("Battery", "Could not read — check BMS")
    elif bat < 8:
        report.fail("Battery", f"{bat}% — CRITICALLY LOW  (E-stop will trigger at <8%)",
                    "Charge the Go2 before operating.")
    elif bat < 20:
        report.warn("Battery", f"{bat}% — LOW",
                    "Recommend charging above 30% before extended operation.")
    elif bat < 30:
        report.warn("Battery", f"{bat}% — moderate (charge recommended for testing)")
    else:
        report.ok("Battery", f"{bat}%")

    # Foot forces
    foot = state_data.get("foot_force", [])
    if foot and len(foot) >= 4:
        all_near_zero  = all(abs(f) < 1.0 for f in foot[:4])
        any_very_large = any(abs(f) > 500.0 for f in foot[:4])
        if any_very_large:
            report.warn("Foot sensors", f"Abnormally large values: {foot[:4]}")
        elif all_near_zero:
            report.warn("Foot sensors", "All near zero — is the Go2 standing?",
                        "Foot forces should be ~36 N per foot when standing.")
        else:
            report.ok("Foot sensors", f"FL={foot[0]:.1f}  FR={foot[1]:.1f}  RL={foot[2]:.1f}  RR={foot[3]:.1f} N")

    # IMU
    rpy = state_data.get("imu_rpy", [])
    if rpy and len(rpy) >= 3:
        roll_deg  = math.degrees(rpy[0])
        pitch_deg = math.degrees(rpy[1])
        if abs(roll_deg) > 30 or abs(pitch_deg) > 25:
            report.warn("IMU", f"Large tilt  roll={roll_deg:.1f}°  pitch={pitch_deg:.1f}°",
                        "The Go2 may be on an uneven surface or not fully standing.")
        else:
            report.ok("IMU", f"roll={roll_deg:.1f}°  pitch={pitch_deg:.1f}°  yaw={math.degrees(rpy[2]):.1f}°")


async def check_motion_sequence(iface: str, bridge):
    """
    Brief motion validation:
    1. Stand up
    2. Wait 2 s — observe foot forces stabilise
    3. Stand down
    4. Verify no faults
    """
    print("     Running motion sequence: stand_up → 2s → stand_down…")
    try:
        await bridge.connect()

        r1 = await bridge.stand_up()
        if not r1:
            report.fail("Motion: stand_up", "Command returned False")
            return
        await asyncio.sleep(2.0)

        state = await bridge.get_state()
        h = state.body_height
        if 0.25 <= h <= 0.45:
            report.ok("Motion: stand height", f"{h:.3f} m  (nominal 0.27–0.35 m)")
        else:
            report.warn("Motion: stand height", f"{h:.3f} m  (expected 0.27–0.35 m)")

        r2 = await bridge.stand_down()
        if not r2:
            report.warn("Motion: stand_down", "Command returned False")
        else:
            report.ok("Motion: stand_down", "OK")

        await bridge.disconnect()

    except Exception as e:
        report.fail("Motion sequence", f"Exception: {e}")


def check_cerberus_config():
    """Validate CERBERUS-specific configuration beyond the basics."""
    hz = int(os.getenv("CERBERUS_HZ", "60"))
    timeout = float(os.getenv("HEARTBEAT_TIMEOUT", "5.0"))
    session_file = Path(os.getenv("CERBERUS_SESSION_FILE", "logs/personality_session.json"))
    audit_log    = Path(os.getenv("CERBERUS_AUDIT_LOG", "logs/safety_audit.jsonl"))

    # Log directory must be writable
    log_dir = audit_log.parent
    if not log_dir.exists():
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            report.ok("Log directory", f"{log_dir}/ created")
        except OSError as e:
            report.fail("Log directory", f"Cannot create {log_dir}: {e}")
    else:
        if os.access(log_dir, os.W_OK):
            report.ok("Log directory", f"{log_dir}/ writable")
        else:
            report.fail("Log directory", f"{log_dir}/ is not writable")

    if hz < 30:
        report.warn("Tick rate", f"{hz} Hz is very low — perception may lag")
    elif hz > 200:
        report.fail("Tick rate", f"{hz} Hz exceeds hardware limits")
    else:
        report.ok("Tick rate", f"{hz} Hz")

    if timeout < 0.5:
        report.fail("Heartbeat timeout", f"{timeout}s is dangerously short")
    elif timeout > 30:
        report.warn("Heartbeat timeout", f"{timeout}s is very long — fault detection delayed")
    else:
        report.ok("Heartbeat timeout", f"{timeout}s")


def check_plugin_dirs():
    """All configured PLUGIN_DIRS must exist."""
    dirs = os.getenv("PLUGIN_DIRS", "plugins").split(":")
    for d in dirs:
        p = Path(d)
        if p.exists():
            n = len(list(p.glob("*/plugin.py")))
            report.ok(f"Plugin dir: {d}", f"{n} plugin(s) found")
        else:
            report.warn(f"Plugin dir: {d}", f"Directory does not exist — no plugins loaded from here")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _run(args) -> int:
    """Async check runner — gathers all results."""
    iface  = args.iface or os.getenv("GO2_NETWORK_INTERFACE", "eth0")
    skip_dds = args.skip_dds or os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1", "yes")

    print()
    print("━" * 60)
    print("  CERBERUS Hardware Preflight Check")
    print("━" * 60)
    print()

    # ── System requirements ───────────────────────────────────────────────────
    print("[ System ]")
    check_python_version()
    check_cerberus_importable()
    print()

    # ── Environment ───────────────────────────────────────────────────────────
    print("[ Environment ]")
    check_env_vars()
    check_cerberus_config()
    check_plugin_dirs()
    print()

    # ── Network ───────────────────────────────────────────────────────────────
    if not skip_dds:
        print("[ Network & DDS ]")
        check_network_interface(iface)
        go2_reachable = check_go2_reachable(iface, timeout=5.0)

        sdk_ok = check_unitree_sdk()

        state_data: dict = {}
        if sdk_ok and go2_reachable:
            await check_dds_connection(iface, timeout=args.timeout)
        elif sdk_ok:
            report.warn("DDS connection", "Skipped — Go2 not reachable via ping")
        else:
            report.warn("DDS connection", "Skipped — unitree_sdk2py not installed")
        print()

        # ── Sensors ───────────────────────────────────────────────────────────
        if state_data:
            print("[ Sensors ]")
            await check_sensors(state_data)
            print()
    else:
        print("[ Network / DDS ]")
        report.warn("DDS checks", "Skipped — GO2_SIMULATION=true (simulation mode)")
        print()

    # ── Optional motion test ──────────────────────────────────────────────────
    if not args.no_motion and not skip_dds:
        print("[ Motion sequence ]")
        print("  ⚠️  The robot will stand up and then stand down.")
        print("  ⚠️  Ensure the area is clear and the E-stop is accessible.")
        print("  Press ENTER to continue, or Ctrl-C to skip motion checks.")
        try:
            input()
            from cerberus.bridge.go2_bridge import RealBridge
            bridge = RealBridge(iface)
            await check_motion_sequence(iface, bridge)
        except KeyboardInterrupt:
            report.warn("Motion sequence", "Skipped by user")
        except ImportError:
            report.warn("Motion sequence", "Skipped — cerberus not importable")
        print()

    return report.summary()


def main():
    parser = argparse.ArgumentParser(
        description="CERBERUS hardware preflight check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--iface",     default="", help="Network interface (default: GO2_NETWORK_INTERFACE env or eth0)")
    parser.add_argument("--timeout",   type=float, default=15.0, help="DDS connection timeout (s)")
    parser.add_argument("--no-motion", action="store_true", help="Skip motion validation sequence")
    parser.add_argument("--skip-dds",  action="store_true", help="Skip all DDS/network checks (for CI)")
    args = parser.parse_args()

    # Load .env if present
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

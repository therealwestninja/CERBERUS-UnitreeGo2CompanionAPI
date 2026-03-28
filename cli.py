"""
cerberus/cli.py  — CERBERUS v3.1  (NEW)
========================================
Command-line interface for CERBERUS.

Usage:
  cerberus serve              Start the API server
  cerberus status             Show robot state
  cerberus move VX VY VYAW   Send velocity command
  cerberus mode MODE          Execute a sport mode
  cerberus behavior NAME      Trigger a behavior
  cerberus nlu TEXT           Parse natural language command
  cerberus sessions           List recorded sessions
  cerberus replay FILE        Replay a session
  cerberus plugins list       List loaded plugins
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
import urllib.error
from typing import Optional


BASE_URL = "http://localhost:8080"


def _post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req  = urllib.request.Request(
        BASE_URL + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"ERROR: Could not reach CERBERUS server at {BASE_URL}")
        print(f"  Start it with: cerberus serve")
        sys.exit(1)


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE_URL + path)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        print(f"ERROR: Could not reach CERBERUS server at {BASE_URL}")
        sys.exit(1)


# ── Sub-commands ─────────────────────────────────────────────────────── #

def cmd_serve(args: argparse.Namespace) -> None:
    import os
    os.environ.setdefault("CERBERUS_HOST", args.host)
    os.environ.setdefault("CERBERUS_PORT", str(args.port))
    if args.dev:
        os.environ["CERBERUS_DEV"] = "true"
    import uvicorn
    uvicorn.run(
        "backend.api.server:app",
        host=args.host, port=args.port,
        reload=args.dev, log_level="info",
    )


def cmd_status(args: argparse.Namespace) -> None:
    d = _get("/api/v1/state")
    print(f"Connection:  {d.get('connection','?')}")
    print(f"Mode:        {d.get('current_mode','?')}")
    print(f"Behavior:    {d.get('current_behavior') or '—'}")
    print(f"Battery:     {d['battery']['voltage']:.2f} V  ({d['battery']['percent']:.0f} %)")
    pos = d.get("position", {})
    print(f"Position:    x={pos.get('x',0):.2f}  y={pos.get('y',0):.2f}")
    vel = d.get("velocity", {})
    print(f"Velocity:    vx={vel.get('vx',0):.2f}  vy={vel.get('vy',0):.2f}  vyaw={vel.get('vyaw',0):.2f}")
    if "personality" in d:
        p = d["personality"]
        print(f"Mood:        {p.get('mood_label','?')} (valence={p['mood']['valence']:.2f})")


def cmd_move(args: argparse.Namespace) -> None:
    r = _post("/api/v1/move", {"vx": args.vx, "vy": args.vy, "vyaw": args.vyaw})
    print(f"Move: vx={args.vx}  vy={args.vy}  vyaw={args.vyaw}  → {r.get('status','?')}")


def cmd_stop(args: argparse.Namespace) -> None:
    r = _post("/api/v1/stop", {})
    print(f"Stop → {r.get('status','?')}")


def cmd_mode(args: argparse.Namespace) -> None:
    r = _post("/api/v1/mode", {"mode": args.mode})
    print(f"Mode '{args.mode}' → {r.get('status','?')}")


def cmd_behavior(args: argparse.Namespace) -> None:
    params = json.loads(args.params) if args.params else {}
    r = _post("/api/v1/behavior", {"behavior": args.name, "params": params})
    print(f"Behavior '{args.name}' → {r.get('status','?')}")


def cmd_nlu(args: argparse.Namespace) -> None:
    r = _post("/api/v1/nlu/command", {
        "text": args.text,
        "execute": args.execute,
        "llm_fallback": args.llm,
    })
    print(f"Text:    {r['text']}")
    for act in r.get("actions", []):
        ex = "✓" if any(e["action_type"] == act["action_type"] for e in r.get("executed",[])) else "—"
        print(f"  {ex} {act['action_type']}({act['params']})  conf={act['confidence']:.2f}")


def cmd_sessions(args: argparse.Namespace) -> None:
    r = _get("/api/v1/sessions")
    sessions = r.get("sessions", [])
    if not sessions:
        print("No sessions recorded yet.")
        return
    for i, s in enumerate(sessions, 1):
        print(f"  {i:3}. {s}")


def cmd_replay(args: argparse.Namespace) -> None:
    r = _post("/api/v1/replay", {"session_file": args.file, "speed": args.speed})
    print(f"Replay '{args.file}' at {args.speed}× → {r.get('status','?')}")


def cmd_plugins(args: argparse.Namespace) -> None:
    r = _get("/api/v1/plugins")
    loaded = r.get("loaded", [])
    if not loaded:
        print("No plugins loaded.")
        return
    print(f"Loaded plugins ({len(loaded)}):")
    for p in r.get("plugins", []):
        print(f"  • {p['name']} v{p['version']} [{p['trust']}]  caps: {p['capabilities']}")


# ── Parser ────────────────────────────────────────────────────────────── #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cerberus",
        description="CERBERUS — Unitree Go2 Companion API CLI  (v3.1.0)",
    )
    p.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    p.add_argument("--port", default=8080, type=int, help="Server port (default: 8080)")

    sub = p.add_subparsers(dest="command", required=True)

    # serve
    s = sub.add_parser("serve", help="Start CERBERUS API server")
    s.add_argument("--dev", action="store_true", help="Enable hot-reload")
    s.set_defaults(func=cmd_serve)

    # status
    s = sub.add_parser("status", help="Show robot state")
    s.set_defaults(func=cmd_status)

    # move
    s = sub.add_parser("move", help="Send velocity command")
    s.add_argument("vx",   type=float, help="Forward velocity m/s")
    s.add_argument("vy",   type=float, nargs="?", default=0.0, help="Lateral velocity m/s")
    s.add_argument("vyaw", type=float, nargs="?", default=0.0, help="Yaw rate rad/s")
    s.set_defaults(func=cmd_move)

    # stop
    s = sub.add_parser("stop", help="Stop motion")
    s.set_defaults(func=cmd_stop)

    # mode
    s = sub.add_parser("mode", help="Execute a sport mode")
    s.add_argument("mode", help="Mode name (hello, dance1, sit, …)")
    s.set_defaults(func=cmd_mode)

    # behavior
    s = sub.add_parser("behavior", help="Trigger a behavior")
    s.add_argument("name", help="Behavior name")
    s.add_argument("--params", default=None, help="JSON params, e.g. '{\"speed\": 0.4}'")
    s.set_defaults(func=cmd_behavior)

    # nlu
    s = sub.add_parser("nlu", help="Parse and optionally execute a natural language command")
    s.add_argument("text", help="Natural language command")
    s.add_argument("--no-execute", dest="execute", action="store_false", default=True)
    s.add_argument("--llm", action="store_true", help="Enable LLM fallback")
    s.set_defaults(func=cmd_nlu)

    # sessions
    s = sub.add_parser("sessions", help="List recorded sessions")
    s.set_defaults(func=cmd_sessions)

    # replay
    s = sub.add_parser("replay", help="Replay a recorded session")
    s.add_argument("file", help="Session file path")
    s.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier")
    s.set_defaults(func=cmd_replay)

    # plugins
    s = sub.add_parser("plugins", help="Plugin management")
    s.add_argument("action", choices=["list"], help="Action")
    s.set_defaults(func=cmd_plugins)

    return p


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    global BASE_URL
    BASE_URL = f"http://{args.host}:{args.port}"
    args.func(args)


if __name__ == "__main__":
    main()

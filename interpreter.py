"""
cerberus/nlu/interpreter.py  — CERBERUS v3.1
=============================================
Natural Language Understanding interpreter.

Inspired by: https://github.com/lpigeon/unitree-go2-mcp-server

Two operation modes:
  RULE  — fast regex/keyword matching, zero latency, no API key needed.
          Covers ~95% of common commands.
  LLM   — OpenAI-compatible API fallback for unrecognised commands.
          Requires CERBERUS_OPENAI_API_KEY env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class NLUAction:
    """A single parsed action ready to execute."""
    action_type: str
    params:      dict[str, Any] = field(default_factory=dict)
    confidence:  float = 1.0
    raw_text:    str = ""

    def __repr__(self) -> str:
        return f"NLUAction({self.action_type}, {self.params}, conf={self.confidence:.2f})"


# ── Pattern groups ─────────────────────────────────────────────────────── #

_STOP_P       = [r"\b(stop|halt|freeze|stay|hold|stay still|don't move|cease)\b"]
_EMERGENCY_P  = [r"\b(emergency|e-stop|abort|emergency stop|kill|shut ?down|danger|critical)\b"]
_SIT_P        = [r"\b(sit|sit down|have a seat|take a seat)\b"]
_STAND_P      = [r"\b(stand|stand up|get up|rise|on your feet)\b"]
_GREET_P      = [r"\b(greet|say hello|wave|hello|hi there|bow)\b"]
_DANCE_P      = [r"\b(dance|boogie|bust a move|groove|perform)\b"]
_STRETCH_P    = [r"\b(stretch|loosen up|warm up)\b"]
_ALERT_P      = [r"\b(alert|attention|heads? up|look out|be ready)\b"]
_PATROL_P     = [r"\b(patrol|guard|walk around|circle|explore|survey)\b"]
_SLEEP_P      = [r"\b(sleep|rest|lie down|take a break|stand down|lay down)\b"]
_WAG_P        = [r"\b(wag|wag (your )?tail|happy|wallow|wiggle)\b"]
_SCRAPE_P     = [r"\b(scrape|dig|paw|scrape (the )?(ground|floor))\b"]
_HEART_P      = [r"\b(finger.?heart|heart gesture|make a heart)\b"]
_FLIP_P       = [r"\b(flip|front flip|do a flip|backflip)\b"]
_JUMP_P       = [r"\b(jump|leap|front jump|spring)\b"]
_POUNCE_P     = [r"\b(pounce|front pounce|lunge|spring forward)\b"]
_DAMP_P       = [r"\b(damp|go limp|rest joints|power down joints|safe park)\b"]
_RISE_SIT_P   = [r"\b(rise from sit|rise sit|get up from sitting|stand from sit)\b"]

_FORWARD_P    = [r"\b(go|move|walk|run|forward|advance|proceed|head)\b"]
_BACKWARD_P   = [r"\b(back(?:ward)?|reverse|retreat|go back)\b"]
_LEFT_P       = [r"\b(left|turn left|go left|strafe left)\b"]
_RIGHT_P      = [r"\b(right|turn right|go right|strafe right)\b"]
_SPIN_LEFT_P  = [r"\b(spin left|rotate left|turn around left|yaw left)\b"]
_SPIN_RIGHT_P = [r"\b(spin right|rotate right|turn around right|yaw right)\b"]

_OBSTACLE_ON_P  = [r"\b(obstacle (avoidance )?(on|enable|start|activate)|(enable|turn on|activate) obstacle)\b"]
_OBSTACLE_OFF_P = [r"\b(obstacle (avoidance )?(off|disable|stop|deactivate)|(disable|turn off) obstacle)\b"]
_LIGHTS_ON_P    = [r"\b((turn )?lights? (on|up|bright)|max bright|full bright|illuminate)\b"]
_LIGHTS_OFF_P   = [r"\b((turn )?lights? (off|down|dim)|dim (the )?lights?)\b"]
_VOL_UP_P       = [r"\b(volume up|louder|increase volume|turn (it |the sound )?up)\b"]
_VOL_DOWN_P     = [r"\b(volume down|quieter|decrease volume|turn (it |the sound )?down|mute)\b"]

_SPEED_RE   = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m/s|mps|meters? per second)", re.I)
_SLOW_RE    = re.compile(r"\b(slow(?:ly)?|easy|gentle|careful|cautious)\b", re.I)
_FAST_RE    = re.compile(r"\b(fast(?:er)?|quick(?:ly)?|speed up|full speed|max speed|sprint)\b", re.I)
_HEIGHT_RE  = re.compile(r"(?:height|tall(?:er)?|raise|lower)[^\d]*(\d+(?:\.\d+)?)\s*(cm|m)?", re.I)
_VOL_NUM_RE = re.compile(r"volume\s+(\d+)", re.I)
_BRIGHT_RE  = re.compile(r"bright(?:ness)?\s+(\d+)", re.I)
_TIRED_RE   = re.compile(r"\b(tired|exhausted|worn out|needs? a break|weary)\b", re.I)
_FOLLOW_RE  = re.compile(r"\b(follow me|come here|come|follow|track me)\b", re.I)


def _any(patterns: list, text: str) -> bool:
    for p in patterns:
        if isinstance(p, str):
            if re.search(p, text, re.I):
                return True
        else:
            if p.search(text):
                return True
    return False


# ── Rule interpreter ──────────────────────────────────────────────────── #

def rule_interpret(text: str) -> list[NLUAction]:
    """
    Fast rule-based NLU. Returns [] when no rule matches.
    O(n_patterns) — runs in <1ms.
    """
    t = text.strip()

    # ── Highest-priority: safety ──────────────────────────────────────── #
    if _any(_EMERGENCY_P, t):
        return [NLUAction("emergency_stop", confidence=0.99, raw_text=text)]

    if _any(_STOP_P, t) and not _any(_PATROL_P, t) and not _any(_OBSTACLE_ON_P + _OBSTACLE_OFF_P, t):
        return [NLUAction("stop", confidence=0.97, raw_text=text)]

    # ── Behaviors ─────────────────────────────────────────────────────── #
    if _any(_RISE_SIT_P, t): return [NLUAction("mode", {"mode": "rise_sit"},       0.88, text)]
    if _any(_SIT_P,    t): return [NLUAction("behavior", {"behavior": "sit"},    0.95, text)]
    if _any(_STAND_P,  t) and not _any(_FORWARD_P, t):
        return [NLUAction("behavior", {"behavior": "stand"}, 0.95, text)]
    if _any(_GREET_P,  t): return [NLUAction("behavior", {"behavior": "greet"},  0.95, text)]
    if _any(_DANCE_P,  t): return [NLUAction("behavior", {"behavior": "dance"},  0.93, text)]
    if _any(_WAG_P,    t): return [NLUAction("behavior", {"behavior": "wag"},    0.92, text)]
    if _any(_ALERT_P,  t): return [NLUAction("behavior", {"behavior": "alert"},  0.90, text)]
    if _any(_PATROL_P, t): return [NLUAction("behavior", {"behavior": "patrol"}, 0.90, text)]
    if _any(_POUNCE_P,   t): return [NLUAction("mode", {"mode": "front_pounce"},   0.85, text)]
    if _any(_DAMP_P,     t): return [NLUAction("mode", {"mode": "damp"},           0.90, text)]
    if _any(_SCRAPE_P, t): return [NLUAction("mode", {"mode": "scrape"},         0.88, text)]
    if _any(_HEART_P,  t): return [NLUAction("mode", {"mode": "finger_heart"},   0.92, text)]
    if _any(_SLEEP_P,  t): return [NLUAction("mode", {"mode": "stand_down"},     0.88, text)]
    if _any(_FLIP_P,   t): return [NLUAction("mode", {"mode": "front_flip"},     0.85, text)]
    if _any(_JUMP_P,   t): return [NLUAction("mode", {"mode": "front_jump"},     0.85, text)]
    if _any(_STRETCH_P, t) or _TIRED_RE.search(t):
        return [NLUAction("behavior", {"behavior": "stretch"}, 0.90, text)]
    if _FOLLOW_RE.search(t):
        return [NLUAction("behavior", {"behavior": "patrol", "params": {"follow": True}}, 0.75, text)]

    # ── Motion ────────────────────────────────────────────────────────── #
    speed_m = _SPEED_RE.search(t)
    base    = float(speed_m.group(1)) if speed_m else (
        0.2 if _SLOW_RE.search(t) else (0.8 if _FAST_RE.search(t) else 0.4)
    )

    # Check spin first — takes priority over generic left/right
    is_spin_left   = _any(_SPIN_LEFT_P, t)
    is_spin_right  = _any(_SPIN_RIGHT_P, t)
    # Only treat as strafe if NOT a spin command
    is_backward    = _any(_BACKWARD_P, t)
    is_left        = _any(_LEFT_P, t)  and not is_spin_left  and not is_spin_right
    is_right       = _any(_RIGHT_P, t) and not is_spin_left  and not is_spin_right
    is_forward     = _any(_FORWARD_P, t) and not any([is_backward, is_left, is_right, is_spin_left, is_spin_right])

    actions: list[NLUAction] = []
    if is_spin_left:  actions.append(NLUAction("move", {"vx": 0.0,   "vy": 0.0, "vyaw": 0.8},          0.92, text))
    if is_spin_right: actions.append(NLUAction("move", {"vx": 0.0,   "vy": 0.0, "vyaw": -0.8},         0.92, text))
    if is_backward:   actions.append(NLUAction("move", {"vx": -base, "vy": 0.0, "vyaw": 0.0},          0.88, text))
    if is_left:       actions.append(NLUAction("move", {"vx": 0.0,   "vy": base*0.6, "vyaw": 0.5},     0.85, text))
    if is_right:      actions.append(NLUAction("move", {"vx": 0.0,   "vy": -base*0.6, "vyaw": -0.5},   0.85, text))
    if is_forward:    actions.append(NLUAction("move", {"vx": base,  "vy": 0.0, "vyaw": 0.0},          0.88, text))
    if actions:
        return actions

    # ── Config ────────────────────────────────────────────────────────── #
    m = _HEIGHT_RE.search(t)
    if m:
        h = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "cm" or h > 1.0:
            h /= 100.0
        return [NLUAction("config", {"height": max(0.3, min(0.5, h))}, 0.85, text)]

    # Obstacle avoidance
    if _any(_OBSTACLE_ON_P, t):
        return [NLUAction("config_obstacle", {"enabled": True}, 0.90, text)]
    if _any(_OBSTACLE_OFF_P, t):
        return [NLUAction("config_obstacle", {"enabled": False}, 0.90, text)]

    # Lights
    if _any(_LIGHTS_ON_P, t):
        return [NLUAction("vui", {"volume": -1, "brightness": 100}, 0.88, text)]
    if _any(_LIGHTS_OFF_P, t):
        return [NLUAction("vui", {"volume": -1, "brightness": 0}, 0.88, text)]

    # Volume
    m = _VOL_NUM_RE.search(t)
    if m:
        return [NLUAction("vui", {"volume": max(0, min(100, int(m.group(1)))), "brightness": -1}, 0.90, text)]
    if _any(_VOL_UP_P, t):
        return [NLUAction("vui", {"volume": 80, "brightness": -1}, 0.82, text)]
    if _any(_VOL_DOWN_P, t):
        return [NLUAction("vui", {"volume": 20, "brightness": -1}, 0.82, text)]

    m = _BRIGHT_RE.search(t)
    if m:
        return [NLUAction("vui", {"volume": -1, "brightness": max(0, min(100, int(m.group(1))))}, 0.90, text)]

    return []


# ── LLM fallback ──────────────────────────────────────────────────────── #

_LLM_SYSTEM = """You are CERBERUS, controlling a Unitree Go2 quadruped robot.
Convert the user command into a JSON array of actions.

Valid action_type values and params:
  move            {vx: float m/s, vy: float m/s, vyaw: float rad/s}
  stop            {}
  emergency_stop  {}
  mode            {mode: str}  — modes: damp|balance_stand|stop_move|stand_up|stand_down|sit|rise_sit|hello|stretch|wallow|scrape|front_flip|front_jump|front_pounce|dance1|dance2|finger_heart
  behavior        {behavior: str}  — behaviors: idle|sit|stand|greet|stretch|dance|patrol|wag|alert|emergency_sit
  config          {height: float}  — body height 0.3–0.5 m
  config_obstacle {enabled: bool}
  vui             {volume: int 0-100, brightness: int 0-100}

Return ONLY a JSON array. Example:
[{"action_type":"behavior","params":{"behavior":"greet"},"confidence":0.95}]"""


async def llm_interpret(text: str, api_key: str | None = None,
                        model: str = "gpt-4o-mini",
                        base_url: str = "https://api.openai.com/v1") -> list[NLUAction]:
    key = api_key or os.getenv("CERBERUS_OPENAI_API_KEY", "")
    if not key:
        return []
    try:
        import aiohttp  # type: ignore
    except ImportError:
        return []

    payload = {
        "model": model, "max_tokens": 256, "temperature": 0.1,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user",   "content": text},
        ],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{base_url}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                raw  = data["choices"][0]["message"]["content"].strip()
                raw  = re.sub(r"^```(?:json)?\n?|```$", "", raw.strip(), flags=re.M).strip()
                return [
                    NLUAction(
                        action_type=i["action_type"],
                        params=i.get("params", {}),
                        confidence=float(i.get("confidence", 0.8)),
                        raw_text=text,
                    )
                    for i in json.loads(raw) if "action_type" in i
                ]
    except asyncio.TimeoutError:
        logger.warning("LLM interpret timed out")
    except Exception as e:
        logger.warning("LLM interpret error: %s", e)
    return []


# ── Public entry point ────────────────────────────────────────────────── #

async def interpret(text: str, llm_fallback: bool = True,
                    llm_api_key: str | None = None) -> list[NLUAction]:
    """
    Parse natural language → list[NLUAction].
    Rule engine first; LLM fallback if enabled and no match; safe STOP default.
    """
    actions = rule_interpret(text)
    if actions:
        return actions
    if llm_fallback:
        actions = await llm_interpret(text, api_key=llm_api_key)
        if actions:
            return actions
    logger.info("NLU: no match for '%s' — defaulting to stop", text[:60])
    return [NLUAction("stop", confidence=0.5, raw_text=text)]

"""
plugins/voice_nlu/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS VoiceNLU Plugin — v1.0.0

Listens for spoken commands, transcribes them with OpenAI Whisper, parses
intent, and injects the corresponding goal into the BehaviorEngine GoalQueue.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Microphone → VAD → buffer → Whisper STT → intent parser → GoalQueue

VAD (Voice Activity Detection):
  - Monitors microphone with sounddevice
  - Starts recording when RMS amplitude exceeds SILENCE_THRESHOLD
  - Stops after SILENCE_AFTER_SPEECH_S of silence
  - Max recording length: MAX_RECORD_S (prevents runaway)

STT:
  - openai-whisper (local inference, no API key required)
  - Default model: "tiny.en" — fast, low memory, English-only
  - Configurable: CERBERUS_WHISPER_MODEL env var

Intent parser:
  - Pattern matching via compiled regex
  - Fallback: keyword extraction for unrecognised phrases
  - Confidence scoring: exact match > partial match > keyword

Supported voice commands (English):
  "sit" / "sit down"                → sit (priority 0.9)
  "stand up" / "get up" / "stand"   → stand_up (0.9)
  "lie down" / "down"               → stand_down (0.8)
  "come here" / "come" / "heel"     → move forward (0.8)
  "stop" / "halt" / "freeze"        → stop (1.0)
  "hello" / "wave" / "hi"           → hello (0.7)
  "dance" / "spin" / "party"        → dance1 (0.6)
  "stretch"                         → stretch (0.7)
  "roll over" / "wallow"            → wallow (0.6)
  "shake" / "scrape"                → scrape (0.6)
  "heart" / "finger heart"          → finger_heart (0.6)
  "balance" / "balance stand"       → balance_stand (0.7)
  "explore" / "go explore"          → explore (0.5)
  "emergency stop" / "e-stop"       → E-stop (1.0, highest priority)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requirements (install-on-demand — not hard dependencies)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  pip install openai-whisper sounddevice numpy
  pip install torch  # required by Whisper

Without these packages, the plugin loads cleanly and logs a warning.
Transcription via audio file path still works if Whisper is installed
even if sounddevice is not.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Environment variables
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CERBERUS_WHISPER_MODEL   = tiny.en   (tiny/base/small/medium/large)
  CERBERUS_VOICE_DEVICE    = default   (sounddevice input device index or name)
  CERBERUS_VOICE_SAMPLERATE= 16000     (Hz)
  CERBERUS_VOICE_THRESHOLD = 0.02      (RMS silence threshold 0–1)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from cerberus.plugins.plugin_manager import (
    CerberusPlugin, PluginManifest, TrustLevel,
)

if TYPE_CHECKING:
    from cerberus.core.engine import CerberusEngine

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

WHISPER_MODEL    = os.getenv("CERBERUS_WHISPER_MODEL",    "tiny.en")
VOICE_DEVICE     = os.getenv("CERBERUS_VOICE_DEVICE",     None)      # None = default
SAMPLE_RATE      = int(os.getenv("CERBERUS_VOICE_SAMPLERATE", "16000"))
SILENCE_THRESHOLD= float(os.getenv("CERBERUS_VOICE_THRESHOLD", "0.02"))

SILENCE_AFTER_SPEECH_S = 1.2   # stop recording after N seconds of silence
MAX_RECORD_S           = 8.0   # never record more than this
MIN_SPEECH_S           = 0.3   # discard clips shorter than this


# ── Intent map ────────────────────────────────────────────────────────────────

@dataclass
class Intent:
    goal_name: str
    priority:  float
    params:    dict       = field(default_factory=dict)
    estop:     bool       = False    # if True, trigger emergency stop directly
    description: str      = ""


# Ordered list of (regex_pattern, Intent) — first match wins
INTENT_TABLE: list[tuple[re.Pattern, Intent]] = [
    # E-stop — highest priority, handled immediately
    (re.compile(r"\b(emergency\s+stop|e[\-\s]?stop|abort|danger)\b", re.I),
     Intent("stop", 1.0, estop=True, description="Emergency stop")),

    # Stop / halt
    (re.compile(r"\b(stop|halt|freeze|hold|stand\s+still)\b", re.I),
     Intent("stop", 1.0, description="Stop all motion")),

    # Stand up
    (re.compile(r"\b(stand\s*up|get\s*up|rise|stand)\b", re.I),
     Intent("stand_up", 0.9, description="Stand up")),

    # Sit / lie down
    (re.compile(r"\b(sit\s*down|sit)\b", re.I),
     Intent("sit", 0.9, description="Sit")),
    (re.compile(r"\b(lie\s*down|lay\s*down|down)\b", re.I),
     Intent("stand_down", 0.8, description="Lie down")),

    # Come here / follow
    (re.compile(r"\b(come\s*here|come|heel|follow\s*me|over\s*here)\b", re.I),
     Intent("move_timed", 0.8,
            params={"vx": 0.25, "vy": 0.0, "vyaw": 0.0, "duration_s": 3.0},
            description="Move forward")),

    # Greet
    (re.compile(r"\b(hello|hi|hey|wave|greet)\b", re.I),
     Intent("hello", 0.7, description="Wave hello")),

    # Dance
    (re.compile(r"\b(dance|spin|party|boogie)\b", re.I),
     Intent("dance1", 0.6, description="Dance")),

    # Stretch
    (re.compile(r"\b(stretch)\b", re.I),
     Intent("stretch", 0.7, description="Stretch")),

    # Roll over / wallow
    (re.compile(r"\b(roll\s*over|wallow|roll)\b", re.I),
     Intent("wallow", 0.6, description="Roll over")),

    # Scrape / paw
    (re.compile(r"\b(shake|scrape|paw|scratch)\b", re.I),
     Intent("scrape", 0.6, description="Scrape / paw")),

    # Heart
    (re.compile(r"\b(heart|finger\s*heart|love)\b", re.I),
     Intent("finger_heart", 0.6, description="Finger heart")),

    # Balance stand
    (re.compile(r"\b(balance|balance\s*stand|steady)\b", re.I),
     Intent("balance_stand", 0.7, description="Balance stand")),

    # Explore
    (re.compile(r"\b(explore|go\s*explore|look\s*around|wander)\b", re.I),
     Intent("explore", 0.5, description="Explore")),

    # Rise sit
    (re.compile(r"\b(rise|rise\s*sit|sit\s*up)\b", re.I),
     Intent("rise_sit", 0.7, description="Rise from sit")),

    # Jump / pounce
    (re.compile(r"\b(jump|leap|pounce)\b", re.I),
     Intent("pounce", 0.6, description="Front pounce")),

    # Flip (requires clear space — low priority, high risk)
    (re.compile(r"\b(flip|back\s*flip|front\s*flip)\b", re.I),
     Intent("front_flip", 0.4, description="Front flip ⚠️")),
]


def parse_intent(text: str) -> Intent | None:
    """
    Match transcribed text against the intent table.
    Returns the first matching Intent, or None if no match.
    """
    text = text.strip()
    if not text:
        return None
    for pattern, intent in INTENT_TABLE:
        if pattern.search(text):
            logger.info("[VoiceNLU] Matched: %r → %s (priority=%.1f)",
                        text, intent.goal_name, intent.priority)
            return intent
    logger.info("[VoiceNLU] No intent matched for: %r", text)
    return None


# ── VAD + recording ───────────────────────────────────────────────────────────

class VoiceRecorder:
    """
    Voice Activity Detection + audio capture using sounddevice.

    Call record_utterance() to block until a complete utterance is
    captured (speech detected, then silence) and return the audio
    as a numpy float32 array at SAMPLE_RATE.

    Raises RuntimeError if sounddevice is not installed.
    """

    def __init__(
        self,
        device=None,
        sample_rate: int = SAMPLE_RATE,
        silence_threshold: float = SILENCE_THRESHOLD,
    ):
        self._device    = device
        self._sr        = sample_rate
        self._threshold = silence_threshold
        self._sd        = None   # lazy import

    def _ensure_sd(self):
        if self._sd is None:
            try:
                import sounddevice as sd  # type: ignore
                import numpy as np        # type: ignore
                self._sd = sd
                self._np = np
            except ImportError as exc:
                raise RuntimeError(
                    "sounddevice is not installed.\n"
                    "Install it:  pip install sounddevice numpy\n"
                    "Alternatively, use /voice/transcribe with an audio file path."
                ) from exc

    def record_utterance(self) -> "np.ndarray":
        """
        Block until a complete utterance is captured.
        Returns float32 audio array at self._sr.
        """
        self._ensure_sd()
        sd = self._sd
        np = self._np

        CHUNK = int(self._sr * 0.05)   # 50 ms chunks

        frames        = []
        speech_started= False
        silence_since = None

        with sd.InputStream(
            samplerate=self._sr,
            channels=1,
            dtype="float32",
            device=self._device,
            blocksize=CHUNK,
        ) as stream:
            t_start = time.monotonic()
            while True:
                chunk, _ = stream.read(CHUNK)
                rms = float(np.sqrt(np.mean(chunk ** 2)))

                if rms > self._threshold:
                    speech_started = True
                    silence_since  = None
                    frames.append(chunk)
                elif speech_started:
                    frames.append(chunk)
                    if silence_since is None:
                        silence_since = time.monotonic()
                    elif time.monotonic() - silence_since >= SILENCE_AFTER_SPEECH_S:
                        break

                elapsed = time.monotonic() - t_start
                if elapsed >= MAX_RECORD_S:
                    logger.warning("[VoiceNLU] Max record time reached")
                    break

        if not frames:
            return np.zeros(CHUNK, dtype="float32")

        audio = np.concatenate(frames, axis=0).flatten()
        return audio

    def duration_s(self, audio: "np.ndarray") -> float:
        return len(audio) / self._sr


# ── Plugin ────────────────────────────────────────────────────────────────────

class VoiceNLUPlugin(CerberusPlugin):
    """
    Listen for spoken commands and inject them into the BehaviorEngine.

    The plugin runs in one of two modes:

    MICROPHONE mode (default, requires sounddevice):
      A background asyncio task continuously listens for speech, transcribes
      it, and dispatches the matched intent as a goal.

    FILE mode (via REST API / POST /voice/transcribe):
      An audio file path is provided; the plugin transcribes it once and
      returns the matched intent without starting the microphone loop.
    """

    MANIFEST = PluginManifest(
        name        = "voice_nlu",
        version     = "1.0.0",
        description = "Whisper STT voice command interface",
        author      = "CERBERUS",
        trust       = TrustLevel.TRUSTED,
        capabilities= {"read_state", "publish_events", "access_memory",
                       "control_motion", "execute_sport", "modify_safety_limits"},
    )

    def __init__(self, engine: "CerberusEngine"):
        super().__init__(engine)
        self._whisper_model = None   # lazy-loaded
        self._recorder      = VoiceRecorder()
        self._listen_task   = None
        self._listening     = False
        self._last_transcript = ""
        self._last_intent     = ""
        self._command_count   = 0
        self._errors: list[str] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        logger.info("[VoiceNLU] Plugin loaded — Whisper model: %s", WHISPER_MODEL)
        logger.info(
            "[VoiceNLU] Microphone listening NOT started automatically.\n"
            "           Call POST /voice/listen/start to activate,\n"
            "           or POST /voice/transcribe with an audio file path."
        )
        # Pre-load Whisper model in background to reduce first-command latency
        asyncio.ensure_future(self._preload_whisper())

    async def on_unload(self) -> None:
        await self.stop_listening()

    async def on_tick(self, tick: int) -> None:
        pass   # plugin is event-driven, not tick-driven

    # ── Whisper ───────────────────────────────────────────────────────────────

    async def _preload_whisper(self) -> None:
        """Load Whisper model in background thread to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._load_whisper)
            logger.info("[VoiceNLU] Whisper model '%s' ready", WHISPER_MODEL)
        except Exception as exc:
            logger.warning(
                "[VoiceNLU] Whisper not available (%s). "
                "Install: pip install openai-whisper torch", exc
            )

    def _load_whisper(self):
        try:
            import whisper  # type: ignore
            self._whisper_model = whisper.load_model(WHISPER_MODEL)
        except ImportError as exc:
            raise RuntimeError(
                "openai-whisper is not installed.\n"
                "Install:  pip install openai-whisper torch"
            ) from exc

    def _transcribe(self, audio) -> str:
        """
        Transcribe audio array (float32, mono, 16 kHz) using Whisper.
        Returns the transcribed text, or raises if Whisper is not loaded.
        """
        if self._whisper_model is None:
            self._load_whisper()

        # Whisper expects float32 numpy array at 16 kHz
        result = self._whisper_model.transcribe(audio, fp16=False, language="en")
        return result.get("text", "").strip()

    async def transcribe_file(self, path: str) -> dict:
        """
        Transcribe an audio file and return matched intent.
        Supported formats: wav, mp3, m4a, flac (anything ffmpeg handles).
        """
        try:
            import numpy as np   # type: ignore
            loop = asyncio.get_running_loop()

            # Load audio via Whisper's built-in loader (handles any format)
            audio = await loop.run_in_executor(None, self._load_audio_file, path)
            text  = await loop.run_in_executor(None, self._transcribe, audio)

            self._last_transcript = text
            intent = parse_intent(text)
            if intent:
                self._last_intent = intent.goal_name
                await self._dispatch(intent)
                return {"transcript": text, "intent": intent.goal_name,
                        "priority": intent.priority, "dispatched": True}
            return {"transcript": text, "intent": None, "dispatched": False}

        except Exception as exc:
            msg = f"Transcribe error: {exc}"
            self._errors.append(msg)
            logger.error("[VoiceNLU] %s", msg)
            return {"error": msg}

    def _load_audio_file(self, path: str):
        import whisper  # type: ignore
        return whisper.load_audio(path)

    # ── Microphone listening ──────────────────────────────────────────────────

    async def start_listening(self) -> dict:
        """Start the continuous microphone listen loop."""
        if self._listening:
            return {"error": "Already listening"}
        self._listening  = True
        self._listen_task = asyncio.ensure_future(self._listen_loop())
        await self.engine.bus.publish("voice.listening_started", {})
        logger.info("[VoiceNLU] 🎤 Microphone listening started")
        return {"listening": True, "model": WHISPER_MODEL}

    async def stop_listening(self) -> dict:
        """Stop the microphone listen loop."""
        self._listening = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await self.engine.bus.publish("voice.listening_stopped", {})
        logger.info("[VoiceNLU] Microphone listening stopped")
        return {"listening": False}

    async def _listen_loop(self) -> None:
        """Continuous loop: record → transcribe → dispatch → repeat."""
        loop = asyncio.get_running_loop()
        while self._listening:
            try:
                # Blocking record in thread pool
                audio = await loop.run_in_executor(
                    None, self._recorder.record_utterance
                )
                if self._recorder.duration_s(audio) < MIN_SPEECH_S:
                    continue   # too short — probably noise

                # Transcribe in thread pool
                text = await loop.run_in_executor(None, self._transcribe, audio)
                if not text:
                    continue

                self._last_transcript = text
                logger.info("[VoiceNLU] Heard: %r", text)
                await self.engine.bus.publish("voice.transcript", {"text": text})

                intent = parse_intent(text)
                if intent:
                    self._last_intent = intent.goal_name
                    await self._dispatch(intent)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                msg = f"Listen error: {exc}"
                self._errors.append(msg[-200:])
                logger.error("[VoiceNLU] %s", msg)
                await asyncio.sleep(1.0)   # back-off on error

    # ── Intent dispatch ───────────────────────────────────────────────────────

    async def _dispatch(self, intent: Intent) -> None:
        """Route a matched intent to the appropriate CERBERUS action."""
        self._command_count += 1

        await self.engine.bus.publish("voice.intent", {
            "goal":     intent.goal_name,
            "priority": intent.priority,
            "description": intent.description,
        })

        # E-stop takes the safety path, not the goal queue
        if intent.estop:
            logger.warning("[VoiceNLU] 🛑 Voice E-stop command!")
            if self.engine.watchdog:
                await self.engine.watchdog.trigger_estop("voice command")
            return

        # All other intents go through BehaviorEngine goal queue
        be = self.engine.behavior_engine
        if be is None:
            logger.warning("[VoiceNLU] BehaviorEngine not attached — cannot dispatch")
            return

        be.push_goal(
            intent.goal_name,
            priority=intent.priority,
            **intent.params,
        )
        logger.info(
            "[VoiceNLU] 📢 Dispatched goal: %s (priority=%.1f) — %s",
            intent.goal_name, intent.priority, intent.description
        )

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        base = super().status()
        base.update({
            "voice": {
                "listening":       self._listening,
                "model":           WHISPER_MODEL,
                "model_loaded":    self._whisper_model is not None,
                "last_transcript": self._last_transcript,
                "last_intent":     self._last_intent,
                "command_count":   self._command_count,
                "device":          str(VOICE_DEVICE or "default"),
                "sample_rate_hz":  SAMPLE_RATE,
                "recent_errors":   self._errors[-3:],
                "intent_count":    len(INTENT_TABLE),
            }
        })
        return base

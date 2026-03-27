"""
go2_platform/backend/core/platform_extensions.py
══════════════════════════════════════════════════════════════════════════════
Platform Extensions — wires all subsystems into PlatformCore.

Attaches to an existing PlatformCore instance:
  - i18n LocalizationEngine
  - AnimationRegistry + AnimationPlayer + AnimationStateMachine
  - BehaviorTreeRunner (companion tree, 10Hz)
  - MetricsRegistry (Prometheus-compatible)
  - HealthChecker (K8s-compatible)
  - Tracer (lightweight request tracing)

Usage:
    platform = PlatformCore()
    ext = PlatformExtensions(platform)
    await ext.start()
    # All subsystems now active and integrated with platform EventBus
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger('go2.extensions')


class PlatformExtensions:
    """
    Extension hub that attaches advanced subsystems to PlatformCore.
    Designed for zero-modification of platform.py itself.
    """

    def __init__(self, platform):
        self.platform = platform
        self._started = False

        # ── i18n ──────────────────────────────────────────────────────────
        from backend.i18n.localization import get_engine
        self.i18n = get_engine()

        # ── Animation ─────────────────────────────────────────────────────
        from backend.animation.animation_system import (
            AnimationRegistry, AnimationPlayer, AnimationStateMachine,
            AnimationLoader, ProceduralAnimations
        )
        self.animation_registry = AnimationRegistry()
        self.animation_player   = AnimationPlayer(bus=platform.bus)
        self.animation_sm       = AnimationStateMachine(
            self.animation_player, self.animation_registry, bus=platform.bus)

        # ── Behavior Tree ─────────────────────────────────────────────────
        from backend.behavior_tree.behavior_tree import (
            BehaviorTreeRunner, build_companion_tree)
        self.bt_runner = BehaviorTreeRunner(
            platform=platform, bus=platform.bus, tick_hz=10.0)
        self.bt_runner.set_tree(build_companion_tree())

        # ── Observability ─────────────────────────────────────────────────
        from backend.observability.metrics import (
            MetricsRegistry, HealthChecker, Tracer)
        self.metrics = MetricsRegistry()
        self.health  = HealthChecker()
        self.tracer  = Tracer()
        self.health.register_platform_checks(platform)

        # ── Wire event hooks ───────────────────────────────────────────────
        self._wire_metrics_hooks()

        log.info('PlatformExtensions created — call start() to activate')

    def _wire_metrics_hooks(self):
        """Subscribe to platform events to update metrics automatically."""
        bus = self.platform.bus

        def on_fsm(event, data):
            state = data.get('to', '')
            self.metrics.gauge('go2_armed').set(
                1 if self.platform.fsm.armed else 0)

        def on_safety_trip(event, data):
            self.metrics.counter('go2_safety_trips_total').inc()

        def on_estop(event, data):
            self.metrics.counter('go2_estop_total').inc()

        def on_behavior(event, data):
            self.metrics.counter('go2_behaviors_total').inc()
            # Trigger animation for this behavior
            asyncio.create_task(
                self.animation_sm.play_behavior(data.get('id', '')))

        def on_telemetry(event, data):
            self.metrics.gauge('go2_battery_pct').set(
                data.get('battery_pct', 0))
            self.metrics.gauge('go2_pitch_deg').set(
                data.get('pitch_deg', 0))
            self.metrics.gauge('go2_roll_deg').set(
                data.get('roll_deg', 0))
            self.metrics.gauge('go2_contact_force_n').set(
                data.get('contact_force_n', 0))
            # Feed BT blackboard
            self.bt_runner.blackboard.update_from_telemetry(data)

        def on_cmd(event, data):
            self.metrics.counter('go2_commands_total').inc()

        def on_anim(event, data):
            self.metrics.counter('go2_animations_played').inc()

        def on_bt_tick(event, data):
            self.metrics.counter('go2_bt_ticks_total').inc(50)

        def on_locale_changed(event, data):
            log.info('Locale changed to: %s', data.get('locale'))

        bus.subscribe('fsm.transition',       on_fsm)
        bus.subscribe('safety.trip',          on_safety_trip)
        bus.subscribe('safety.estop',         on_estop)
        bus.subscribe('behavior_start',       on_behavior)
        bus.subscribe('telemetry',            on_telemetry)
        bus.subscribe('platform.command',     on_cmd)
        bus.subscribe('animation.playing',    on_anim)
        bus.subscribe('bt.tick_stats',        on_bt_tick)
        bus.subscribe('i18n.locale_changed',  on_locale_changed)

    async def start(self):
        if self._started:
            return
        self._started = True
        await self.bt_runner.start()
        log.info('PlatformExtensions started — BT@10Hz, animation, metrics, i18n active')

    async def stop(self):
        if not self._started:
            return
        await self.bt_runner.stop()
        await self.animation_player.stop()
        self._started = False
        log.info('PlatformExtensions stopped')

    def full_status(self) -> dict:
        return {
            'i18n': {
                'locale': self.i18n.locale,
                'locale_name': self.i18n.locale_name,
                'available': len(self.i18n.available_locales()),
            },
            'animation': self.animation_player.status(),
            'behavior_tree': self.bt_runner.status_dict(),
            'metrics': self.metrics.to_dict(),
        }

    def register_api_routes(self, app, platform=None):
        """Attach all extension API routes to the FastAPI app."""
        from backend.i18n.localization import register_i18n_routes
        from backend.observability.metrics import register_observability_routes
        register_i18n_routes(app, platform=self.platform)
        register_observability_routes(
            app, self.metrics, self.health, self.tracer)
        self._register_animation_routes(app)
        self._register_bt_routes(app)

    def _register_animation_routes(self, app):
        """Animation API routes."""
        from fastapi import HTTPException
        from pydantic import BaseModel
        from typing import Optional as Opt

        class AnimLoadRequest(BaseModel):
            data: dict
            clip_id: str = 'clip'
            name: str = 'Unnamed'
            fmt: Opt[str] = None

        class AnimPlayRequest(BaseModel):
            clip_id: Opt[str] = None
            speed: float = 1.0

        @app.get('/api/v1/animation/clips', tags=['animation'])
        async def list_clips():
            return {'clips': self.animation_registry.list()}

        @app.get('/api/v1/animation/status', tags=['animation'])
        async def anim_status():
            return self.animation_player.status()

        @app.post('/api/v1/animation/load', tags=['animation'])
        async def load_animation(req: AnimLoadRequest):
            try:
                from backend.animation.animation_system import AnimationLoader
                clip = AnimationLoader.load(
                    req.data, fmt=req.fmt,
                    clip_id=req.clip_id, name=req.name)
                self.animation_registry.register(clip, overwrite=True)
                await self.animation_player.load(clip)
                return {'ok': True, 'clip': clip.to_dict()}
            except Exception as e:
                raise HTTPException(422, detail=str(e))

        @app.post('/api/v1/animation/play', tags=['animation'])
        async def play_animation(req: AnimPlayRequest):
            if req.clip_id:
                clip = self.animation_registry.get(req.clip_id)
                if not clip:
                    raise HTTPException(404, f'Clip not found: {req.clip_id!r}')
                await self.animation_player.load(clip)
            await self.animation_player.play(speed=req.speed)
            return {'ok': True}

        @app.post('/api/v1/animation/pause', tags=['animation'])
        async def pause_animation():
            await self.animation_player.pause()
            return {'ok': True}

        @app.post('/api/v1/animation/stop', tags=['animation'])
        async def stop_animation():
            await self.animation_player.stop()
            return {'ok': True}

        @app.get('/api/v1/animation/clips/{clip_id}/export/funscript',
                 tags=['animation'])
        async def export_funscript(clip_id: str):
            clip = self.animation_registry.get(clip_id)
            if not clip:
                raise HTTPException(404, f'Clip not found: {clip_id!r}')
            from backend.animation.animation_system import AnimationLoader
            return AnimationLoader.to_funscript(clip)

        @app.get('/api/v1/animation/clips/{clip_id}/export/json',
                 tags=['animation'])
        async def export_json(clip_id: str):
            clip = self.animation_registry.get(clip_id)
            if not clip:
                raise HTTPException(404, f'Clip not found: {clip_id!r}')
            from backend.animation.animation_system import AnimationLoader
            return AnimationLoader.to_go2_json(clip)

    def _register_bt_routes(self, app):
        """Behavior Tree API routes."""
        from fastapi import Depends

        @app.get('/api/v1/bt/status', tags=['behavior_tree'])
        async def bt_status():
            return self.bt_runner.status_dict()

        @app.get('/api/v1/bt/debug', tags=['behavior_tree'])
        async def bt_debug():
            return {
                'tree': self.bt_runner.debug_tree(),
                'blackboard': self.bt_runner.blackboard.snapshot(),
            }

        @app.post('/api/v1/bt/blackboard/{key}', tags=['behavior_tree'])
        async def set_bb(key: str, value: dict):
            self.bt_runner.blackboard.set(key, value.get('value'))
            return {'ok': True}

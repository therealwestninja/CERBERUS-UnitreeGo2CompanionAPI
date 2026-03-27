"""
go2_platform/backend/core/integration.py
PlatformRuntime — wires PlatformCore, i18n, Animation, BT, Observability into one object.
"""

import asyncio
import logging
import os
import sys
import time
from typing import Optional

log = logging.getLogger('go2.integration')

# Allow both package and direct imports
_BASE = os.path.dirname(os.path.dirname(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)


class PlatformRuntime:
    """Single integrated runtime. One instance per process."""

    VERSION = '2.0.0'

    def __init__(self, mode: Optional[str] = None):
        self.mode = mode or os.getenv('GO2_MODE', 'simulation')
        self._started = False
        self._start_time: float = 0.0

        # ── Core ──────────────────────────────────────────────────────────
        from core.platform import PlatformCore, SafetyConfig
        self.platform = PlatformCore(safety_cfg=SafetyConfig(
            pitch_limit_deg = float(os.getenv('GO2_PITCH_LIMIT', '10.0')),
            roll_limit_deg  = float(os.getenv('GO2_ROLL_LIMIT',  '10.0')),
            force_limit_n   = float(os.getenv('GO2_FORCE_LIMIT', '30.0')),
            temp_limit_c    = float(os.getenv('GO2_TEMP_LIMIT',  '72.0')),
            battery_min_pct = float(os.getenv('GO2_BATT_MIN',    '10.0')),
            watchdog_s      = float(os.getenv('GO2_WATCHDOG',    '2.0')),
        ))
        self.bus = self.platform.bus

        # ── i18n ──────────────────────────────────────────────────────────
        from i18n.localization import get_engine
        self.i18n = get_engine()
        self.i18n.set_locale(os.getenv('GO2_LOCALE', 'en'))

        # ── Animation ─────────────────────────────────────────────────────
        from animation.animation_system import (
            AnimationRegistry, AnimationPlayer, AnimationStateMachine)
        self.anim_registry     = AnimationRegistry()
        self.anim_player       = AnimationPlayer(bus=self.bus)
        self.anim_state_machine = AnimationStateMachine(
            self.anim_player, self.anim_registry, self.bus)

        # ── Behavior Tree ─────────────────────────────────────────────────
        from behavior_tree.behavior_tree import BehaviorTreeRunner
        self.bt_runner = BehaviorTreeRunner(
            platform=self.platform, bus=self.bus,
            tick_hz=float(os.getenv('GO2_BT_HZ', '10')))

        # ── Observability ──────────────────────────────────────────────────
        from observability.metrics import MetricsRegistry, HealthChecker, Tracer
        self.metrics = MetricsRegistry()
        self.health  = HealthChecker()
        self.tracer  = Tracer()
        self.health.register_platform_checks(self.platform)

        # ── Plugins ───────────────────────────────────────────────────────
        from core.plugin_system import PluginSystem
        self.plugins = PluginSystem(
            self.platform, plugin_dir=os.getenv('GO2_PLUGIN_DIR', 'plugins'))

        # ── Sim engine ref ─────────────────────────────────────────────────
        self._sim_engine = None

        self._wire_metrics()
        log.info('PlatformRuntime v%s created (mode=%s)', self.VERSION, self.mode)

    def _wire_metrics(self):
        """Bridge EventBus events to metric counters/gauges."""
        m = self.metrics
        b = self.bus
        b.subscribe('safety.estop',    lambda e,d: m.counter('go2_estop_total').inc())
        b.subscribe('safety.trip',     lambda e,d: m.counter('go2_safety_trips_total').inc())
        b.subscribe('fsm.transition',  lambda e,d: m.gauge('go2_armed').set(1 if d.get('armed') else 0))
        b.subscribe('telemetry',       lambda e,d: [
            m.gauge('go2_battery_pct').set(d.get('battery_pct', 0)),
            m.gauge('go2_pitch_deg').set(d.get('pitch_deg', 0)),
            m.gauge('go2_contact_force_n').set(d.get('contact_force_n', 0)),
        ])
        b.subscribe('behavior_start',  lambda e,d: m.counter('go2_behaviors_total').inc())
        b.subscribe('animation.complete', lambda e,d: m.counter('go2_animations_played').inc())
        b.subscribe('bt.tick_stats',   lambda e,d: m.counter('go2_bt_ticks_total').inc())

    async def start(self):
        if self._started:
            return
        self._start_time = time.time()

        await self.platform.start()
        log.info('  ✓ PlatformCore')

        if self.mode in ('simulation', 'sim'):
            await self._start_sim()
        elif self.mode in ('hardware', 'hw'):
            await self._start_hw()
        else:
            await self._start_sim()

        self.bt_runner.set_tree(None)
        await self.bt_runner.start()
        log.info('  ✓ BehaviorTree (%.0fHz)', self.bt_runner._tick_hz)

        result = await self.plugins.load_from_dir(os.getenv('GO2_PLUGIN_DIR', 'plugins'))
        log.info('  ✓ Plugins: %d loaded', result.get('loaded', 0))

        self._started = True
        await self.bus.emit('platform.started',
            {'version': self.VERSION, 'mode': self.mode}, 'integration')
        log.info('PlatformRuntime ready in %.2fs', time.time() - self._start_time)

    async def stop(self):
        await self.bt_runner.stop()
        if self._sim_engine:
            await self._sim_engine.stop()
        await self.platform.stop()
        self._started = False

    async def _start_sim(self):
        try:
            from sim.simulation_engine import SimulationEngine
            self._sim_engine = SimulationEngine(self.platform)
            await self._sim_engine.start()
            self.platform.sim_mode = True
            log.info('  ✓ SimulationEngine (200Hz)')
        except Exception as e:
            log.warning('  ⚠ SimEngine failed: %s — using basic sim', e)
            self.platform.sim_mode = True

    async def _start_hw(self):
        try:
            import rclpy  # noqa
            self.platform.sim_mode = False
            log.info('  ✓ Hardware mode (ROS2 bridge)')
        except ImportError:
            log.warning('  ⚠ rclpy not found — fallback to sim')
            await self._start_sim()

    def full_status(self) -> dict:
        base = self.platform.full_status()
        base.update({
            'runtime': {
                'version': self.VERSION,
                'mode': self.mode,
                'uptime_s': round(time.time() - self._start_time, 1),
            },
            'i18n': {
                'locale': self.i18n.locale,
                'name': self.i18n.locale_name,
                'locales': len(self.i18n.available_locales()),
            },
            'animation': self.anim_player.status(),
            'behavior_tree': self.bt_runner.status_dict(),
            'plugins_loaded': len(self.plugins.list()),
            'metrics': self.metrics.to_dict(),
        })
        return base

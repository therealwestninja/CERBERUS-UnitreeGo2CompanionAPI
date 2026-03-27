"""cerberus/__init__.py — CERBERUS integration facade"""
import logging
log = logging.getLogger('cerberus')
__version__ = '2.0.0'
__codename__ = 'CERBERUS'

class _EventBusBridge:
    def __init__(self, platform_bus):
        self._bus = platform_bus
    def subscribe(self, event_name, handler):
        self._bus.subscribe(event_name, handler)
    def unsubscribe(self, event_name, handler):
        self._bus.unsubscribe(event_name, handler)
    async def emit(self, name, data=None, source='cerberus', priority=None):
        import uuid
        await self._bus.emit(name, data, source)
        return str(uuid.uuid4())[:8]
    def recent(self, n=50):
        return self._bus.recent(n)

class Cerberus:
    """CERBERUS integration facade — attaches cognitive systems to PlatformCore."""
    def __init__(self, platform=None, enable_learning=True,
                 enable_logging=True, log_dir='/tmp/cerberus_logs'):
        from .runtime import CerberusRuntime
        from .cognitive.mind import CognitiveMind
        from .body.anatomy import DigitalAnatomy
        from .personality.engine import PersonalityEngine
        from .learning.adaptation import LearningSystem
        from .data.logging_pipeline import DataLogger

        self._bus = _EventBusBridge(platform.bus) if (platform and hasattr(platform,'bus')) else None
        if self._bus is None:
            from .runtime import SystemEventBus
            self._bus = SystemEventBus()

        self.runtime     = CerberusRuntime(platform)
        self.mind        = CognitiveMind(self._bus)
        self.anatomy     = DigitalAnatomy(self._bus)
        self.personality = PersonalityEngine(self._bus)
        self.learning    = LearningSystem(self._bus)

        for s in [self.mind, self.anatomy, self.personality]:
            self.runtime.register(s)
        if enable_learning:
            self.runtime.register(self.learning)
        if enable_logging:
            self.logger = DataLogger(self._bus, log_dir=log_dir)
            self.runtime.register(self.logger)

        self.runtime.watchdog.register('cognitive_mind', timeout_s=5.0)
        self.runtime.watchdog.register('digital_anatomy', timeout_s=3.0)
        self.runtime.share('cerberus.mind',        self.mind)
        self.runtime.share('cerberus.anatomy',     self.anatomy)
        self.runtime.share('cerberus.personality', self.personality)
        self.runtime.share('cerberus.learning',    self.learning)
        log.info('CERBERUS %s initialized', __version__)

    async def start(self):
        await self.runtime.start()
        log.info('CERBERUS fully operational')

    async def stop(self):
        await self.runtime.stop()

    def status(self):
        return {
            'version':     __version__,
            'codename':    __codename__,
            'runtime':     self.runtime.status(),
            'mind':        self.mind.status(),
            'anatomy':     self.anatomy.status(),
            'personality': self.personality.status(),
            'learning':    self.learning.status(),
        }

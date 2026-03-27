"""
plugins/examples/hello_world/hello_world.py
==========================================
Hello World — minimal CERBERUS plugin example.

This plugin:
1. Logs a greeting when loaded.
2. Registers a "hello_world" behavior that makes the robot greet.
3. Logs a farewell when unloaded.

Use as a template for new plugins.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class HelloWorldPlugin:
    """Minimal example plugin — demonstrates the CERBERUS plugin API."""

    async def on_load(self, context) -> None:
        logger.info("HelloWorldPlugin loaded! (trust_level=%s)", context.trust_level)

        # Register a behavior if we have motion capability
        if context.can("motion"):
            engine = context.behavior_engine
            from cerberus.behavior.engine import BehaviorDescriptor, Priority

            async def hello_world_behavior(ctx):
                await ctx.bridge.set_mode("hello")
                await asyncio.sleep(2.0)
                logger.info("HelloWorldPlugin: greeted!")

            engine.register(BehaviorDescriptor(
                name="hello_world",
                fn=hello_world_behavior,
                priority=Priority.NORMAL,
                cooldown_s=5.0,
                description="Plugin-injected greeting behavior",
            ))
            logger.info("HelloWorldPlugin: registered 'hello_world' behavior")

    async def on_unload(self) -> None:
        logger.info("HelloWorldPlugin: goodbye!")

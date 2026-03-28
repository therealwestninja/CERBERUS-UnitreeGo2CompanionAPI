"""
plugins/examples/hello_world/hello_world.py  — CERBERUS v3.1
=============================================================
Hello World plugin — minimal template demonstrating the plugin API.

On load: registers a "hello_world" behavior.
On unload: graceful teardown.

Copy this folder as a starting point for new plugins.
"""
from __future__ import annotations
import asyncio
import logging

logger = logging.getLogger(__name__)


class HelloWorldPlugin:

    async def on_load(self, context) -> None:
        logger.info("HelloWorldPlugin loaded (trust=%s)", context.trust_level)

        if context.can("motion"):
            from cerberus.behavior.engine import BehaviorDescriptor, Priority

            async def hello_world_behavior(ctx):
                await ctx.bridge.set_mode("hello")
                await asyncio.sleep(2.0)

            context.behavior_engine.register(BehaviorDescriptor(
                name="hello_world",
                fn=hello_world_behavior,
                priority=Priority.NORMAL,
                cooldown_s=5.0,
                description="Plugin-injected greeting behavior",
            ))
            logger.info("HelloWorldPlugin: 'hello_world' behavior registered")

    async def on_unload(self) -> None:
        logger.info("HelloWorldPlugin unloaded")

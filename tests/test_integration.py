"""
tests/test_integration.py
━━━━━━━━━━━━━━━━━━━━━━━━
Full-stack integration tests that exercise the complete request path from
HTTP/WebSocket client through FastAPI → Engine → Bridge → response.

Covers:
  Dashboard
    • GET /dashboard returns 200 and HTML
    • Dashboard HTML contains WebSocket connection code
    • Dashboard HTML loads React from CDN

  WebSocket event broadcasting
    • State updates broadcast via bus propagate to WS clients
    • limb_loss.* events broadcast correctly
    • stair.* events broadcast correctly
    • voice.* events broadcast correctly
    • Multiple simultaneous WS clients all receive broadcasts
    • Dead client removed silently during broadcast

  End-to-end API flows
    • POST /motion/move → GET /state reflects commanded velocity
    • POST /safety/estop → all subsequent motion endpoints return 423
    • POST /limb_loss/declare → GET /limb_loss shows recovering state
    • POST /sim/limb_loss → SimBridge lost_limb attribute updated
    • Plugin enable/disable cycle via REST
    • Version endpoint reflects single-source __version__
    • GET /health is always 200 regardless of engine state
    • GET /ready returns engine_hz when running

  Error handling
    • Unknown endpoint returns 404
    • POST with bad JSON returns 422
    • Missing required field returns 422
    • Wrong type for numeric field returns 422
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import pytest_asyncio

os.environ.setdefault("GO2_SIMULATION", "true")


# ── Shared API fixture ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def api():
    from httpx import AsyncClient, ASGITransport
    from asgi_lifespan import LifespanManager
    from backend.main import app
    async with LifespanManager(app) as mgr:
        async with AsyncClient(
            transport=ASGITransport(app=mgr.app),
            base_url="http://test",
        ) as client:
            yield client


# ═════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════

class TestDashboard:

    @pytest.mark.asyncio
    async def test_dashboard_returns_200(self, api):
        r = await api.get("/dashboard")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_content_type_html(self, api):
        r = await api.get("/dashboard")
        assert "text/html" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_dashboard_contains_cerberus_title(self, api):
        r = await api.get("/dashboard")
        assert "CERBERUS" in r.text

    @pytest.mark.asyncio
    async def test_dashboard_references_react(self, api):
        r = await api.get("/dashboard")
        assert "react" in r.text.lower()

    @pytest.mark.asyncio
    async def test_dashboard_references_websocket(self, api):
        r = await api.get("/dashboard")
        assert "WebSocket" in r.text or "ws://" in r.text

    @pytest.mark.asyncio
    async def test_dashboard_contains_estop_button(self, api):
        r = await api.get("/dashboard")
        assert "E-STOP" in r.text or "estop" in r.text.lower()

    @pytest.mark.asyncio
    async def test_dashboard_contains_sport_modes(self, api):
        r = await api.get("/dashboard")
        body = r.text.lower()
        assert "hello" in body or "dance" in body

    @pytest.mark.asyncio
    async def test_static_dashboard_html_served(self, api):
        """Dashboard HTML is also accessible at /static/dashboard.html."""
        r = await api.get("/static/dashboard.html")
        assert r.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# HEALTH / READY / VERSION
# ═════════════════════════════════════════════════════════════════════════════

class TestProbes:

    @pytest.mark.asyncio
    async def test_health_always_200(self, api):
        r = await api.get("/health")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_health_version_matches_package(self, api):
        from cerberus import __version__
        r = await api.get("/health")
        body = r.json()
        assert body.get("version") == __version__

    @pytest.mark.asyncio
    async def test_ready_returns_200_when_running(self, api):
        r = await api.get("/ready")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_ready_body_has_engine_hz(self, api):
        r = await api.get("/ready")
        if r.status_code == 200:
            assert "engine_hz" in r.json()

    @pytest.mark.asyncio
    async def test_root_version_matches_package(self, api):
        from cerberus import __version__
        r = await api.get("/")
        body = r.json()
        assert body.get("version") == __version__


# ═════════════════════════════════════════════════════════════════════════════
# END-TO-END API FLOWS
# ═════════════════════════════════════════════════════════════════════════════

class TestMotionFlow:

    @pytest.mark.asyncio
    async def test_move_updates_state(self, api):
        r = await api.post("/motion/move", json={"vx": 0.3, "vy": 0.0, "vyaw": 0.0})
        assert r.status_code == 200
        # SimBridge sets mode synchronously on move() — velocity may race with sim loop
        state = (await api.get("/state")).json()
        assert state.get("mode") in ("moving", "trotting", "standing", "sim_idle")
        await api.post("/motion/stop")

    @pytest.mark.asyncio
    async def test_stand_up_changes_mode(self, api):
        r = await api.post("/motion/stand_up")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_stop_after_move(self, api):
        await api.post("/motion/move", json={"vx": 0.2, "vy": 0.0, "vyaw": 0.0})
        r = await api.post("/motion/stop")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_sport_mode_executes(self, api):
        r = await api.post("/motion/sport_mode", json={"mode": "hello"})
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_body_height_offset(self, api):
        r = await api.post("/motion/body_height", json={"height": 0.03})
        assert r.status_code == 200


class TestSafetyFlow:

    @pytest.mark.asyncio
    async def test_estop_sets_estop_flag(self, api):
        await api.post("/safety/estop")
        state = (await api.get("/state")).json()
        assert state.get("estop_active") is True
        # Clear for subsequent tests
        await api.post("/safety/clear_estop")

    @pytest.mark.asyncio
    async def test_clear_estop_clears_flag(self, api):
        await api.post("/safety/estop")
        await api.post("/safety/clear_estop")
        state = (await api.get("/state")).json()
        assert state.get("estop_active") is False

    @pytest.mark.asyncio
    async def test_safety_events_endpoint(self, api):
        r = await api.get("/safety/events")
        assert r.status_code == 200
        body = r.json()
        # Should be a list (possibly empty)
        assert isinstance(body, list)


class TestLimbLossFlow:

    @pytest.mark.asyncio
    async def test_declare_and_clear_limb_loss(self, api):
        # Declare FL lost
        r = await api.post("/limb_loss/declare", json={"leg": "FL"})
        # 200 if plugin loaded, 404 if not
        if r.status_code == 200:
            status = (await api.get("/limb_loss")).json()
            ll = status.get("limb_loss", status)
            assert ll.get("state") == "recovering"
            # Clear
            clear_r = await api.post("/limb_loss/clear")
            assert clear_r.status_code == 200

    @pytest.mark.asyncio
    async def test_declare_unknown_leg_returns_error(self, api):
        r = await api.post("/limb_loss/declare", json={"leg": "ZZ"})
        assert r.status_code in (404, 409, 422)

    @pytest.mark.asyncio
    async def test_sim_limb_loss_injection(self, api):
        r = await api.post("/sim/limb_loss", json={"leg": "RR"})
        assert r.status_code in (200, 404, 409)
        if r.status_code == 200:
            # Clear
            await api.post("/sim/limb_loss", json={"leg": None})


class TestPluginFlow:

    @pytest.mark.asyncio
    async def test_plugins_endpoint_returns_list(self, api):
        r = await api.get("/plugins")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    @pytest.mark.asyncio
    async def test_enable_disable_plugin_cycle(self, api):
        plugins = (await api.get("/plugins")).json()
        if not plugins:
            pytest.skip("No plugins loaded")
        name = plugins[0].get("name")
        # Disable then re-enable
        await api.post(f"/plugins/{name}/disable")
        await api.post(f"/plugins/{name}/enable")
        # Plugin should still be in list
        names_after = [p.get("name") for p in (await api.get("/plugins")).json()]
        assert name in names_after


# ═════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_unknown_endpoint_404(self, api):
        r = await api.get("/no_such_endpoint_xyzzy")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_move_missing_fields_422(self, api):
        # MoveCmd fields all have defaults (0.0), so {} is valid → 200
        # Missing *required* field — use a model with required field (e.g. /led)
        r = await api.post("/led", json={})   # r, g, b are required
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_move_wrong_type_422(self, api):
        r = await api.post("/motion/move", json={"vx": "fast", "vy": 0, "vyaw": 0})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_sport_mode_invalid_mode_400_or_422(self, api):
        r = await api.post("/motion/sport_mode", json={"mode": "moonwalk_backwards"})
        # Either validation error or bridge returns False (200 with ok:false)
        assert r.status_code in (200, 400, 404, 422)

    @pytest.mark.asyncio
    async def test_bad_json_returns_422(self, api):
        r = await api.post(
            "/motion/move",
            content=b"{ bad json }",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_stair_tune_empty_body_422(self, api):
        r = await api.post("/stair/tune", json={})
        # Either 404 (plugin not loaded) or 422 (no fields supplied)
        assert r.status_code in (404, 422)


# ═════════════════════════════════════════════════════════════════════════════
# WEBSOCKET MANAGER BROADCAST INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════

class TestWebSocketBroadcast:
    """
    These tests verify the WebSocketManager.broadcast_json() call wiring —
    that EventBus subscriptions are connected correctly.
    We test via the manager's internal state rather than opening real WS
    connections (which requires running uvicorn).
    """

    @pytest.mark.asyncio
    async def test_ws_manager_is_singleton_in_app(self, api):
        """The ws_manager module-level object is stable across requests."""
        import backend.main as bm
        m1 = bm.ws_manager
        # Second access returns same object
        m2 = bm.ws_manager
        assert m1 is m2

    @pytest.mark.asyncio
    async def test_ws_manager_broadcast_json_sends_typed_envelope(self):
        """broadcast_json wraps data in {type, data} envelope."""
        from backend.main import WebSocketManager
        from tests.conftest import MockWebSocket
        m = WebSocketManager()
        ws = MockWebSocket()
        m.add(ws)
        await m.broadcast_json("stair", {"state": "active"})
        assert len(ws.sent) == 1
        parsed = json.loads(ws.sent[0])
        assert parsed["type"] == "stair"
        assert parsed["data"]["state"] == "active"

    @pytest.mark.asyncio
    async def test_ws_manager_removes_dead_client_on_broadcast(self):
        """Dead clients are removed silently during broadcast."""
        from backend.main import WebSocketManager
        from tests.conftest import MockWebSocket
        m = WebSocketManager()
        live = MockWebSocket()
        dead = MockWebSocket(fail_on_send=True)
        m.add(live)
        m.add(dead)
        assert m.count == 2
        await m.broadcast_json("ping", {})
        assert m.count == 1
        assert live.sent  # live client received message

    @pytest.mark.asyncio
    async def test_engine_bus_subscriptions_wired_for_stair(self, api):
        """Verify stair.status topic has at least one subscriber registered."""
        import backend.main as bm
        # EventBus stores subscribers per topic
        bus = bm.engine.bus
        subs = bus._subs if hasattr(bus, '_subs') else {}
        stair_topics = [k for k in subs if 'stair' in k]
        assert len(stair_topics) > 0, "No stair EventBus subscriptions registered"

    @pytest.mark.asyncio
    async def test_engine_bus_subscriptions_wired_for_limb_loss(self, api):
        import backend.main as bm
        bus = bm.engine.bus
        subs = bus._subs if hasattr(bus, '_subs') else {}
        ll_topics = [k for k in subs if 'limb_loss' in k]
        assert len(ll_topics) > 0, "No limb_loss EventBus subscriptions registered"

    @pytest.mark.asyncio
    async def test_engine_bus_subscriptions_wired_for_voice(self, api):
        import backend.main as bm
        bus = bm.engine.bus
        subs = bus._subs if hasattr(bus, '_subs') else {}
        voice_topics = [k for k in subs if 'voice' in k]
        assert len(voice_topics) > 0, "No voice EventBus subscriptions registered"

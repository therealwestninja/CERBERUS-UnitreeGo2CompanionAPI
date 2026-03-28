import json, pytest
from plugins.funscript.funscript_plugin import FunScriptPlugin

@pytest.mark.asyncio
async def test_load_and_inspect(tmp_path):
    script = {"version":"1.0","inverted":False,"range":90,
              "actions":[{"at":0,"pos":0},{"at":500,"pos":100},{"at":1000,"pos":0}]}
    f = tmp_path / "test.funscript"
    f.write_text(json.dumps(script))
    plugin = FunScriptPlugin(robot_adapter=None)
    await plugin.load({})
    assert await plugin.load_file(str(f)) is True
    assert plugin._script is not None
    assert len(plugin._script.actions) == 3
    assert plugin._script.duration_ms == 1000

@pytest.mark.asyncio
async def test_missing_file_returns_false():
    plugin = FunScriptPlugin()
    await plugin.load({})
    assert await plugin.load_file("/nonexistent/path.funscript") is False

@pytest.mark.asyncio
async def test_play_pause_stop(tmp_path):
    script = {"version":"1.0","inverted":False,"range":90,
              "actions":[{"at":0,"pos":0},{"at":5000,"pos":100}]}
    f = tmp_path / "s.funscript"
    f.write_text(json.dumps(script))
    plugin = FunScriptPlugin(robot_adapter=None)
    await plugin.load({})
    await plugin.load_file(str(f))
    await plugin.play()
    assert plugin.is_playing
    await plugin.pause()
    assert not plugin.is_playing
    await plugin.stop()
    assert not plugin.is_playing

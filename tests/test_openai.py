"""OpenAI realtime manager: a typed turn must not be cancelled by the cancel
start_new_turn queued for the send task."""
import asyncio

from config import get_config
from realtime.openai_rt import OpenAIRealtimeManager


async def test_text_turn_takes_the_pending_cancel_before_awaiting():
    mgr = OpenAIRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())
    mgr._session_active = True
    mgr._ws = object()
    mgr._pending_cancel = True
    seen = []

    async def ws_send(event):
        seen.append((event["type"], mgr._pending_cancel))
        await asyncio.sleep(0)  # let the send task look at the flag here

    mgr._ws_send = ws_send
    assert await mgr.send_text("hej")
    assert seen[0] == ("response.cancel", None)  # flag already taken
    assert [t for t, _ in seen][-2:] == ["conversation.item.create", "response.create"]


async def test_activity_end_starts_the_turn_watchdog(monkeypatch):
    # as in the Gemini manager: nothing else ends a turn the API never answers
    mgr = OpenAIRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())
    monkeypatch.setattr(mgr.live_cfg, "openai_commit_delay", 0.0)
    mgr._session_active = True
    mgr._ws = object()
    mgr._activity_started = True
    sent = []

    async def ws_send(event):
        sent.append(event["type"])

    mgr._ws_send = ws_send
    await mgr.send_activity_end()
    assert sent == ["input_audio_buffer.commit", "response.create"]
    assert mgr.awaiting_reply and mgr._watchdog_task is not None and not mgr._watchdog_task.done()
    mgr.cancel_watchdog()

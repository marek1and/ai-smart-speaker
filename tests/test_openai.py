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

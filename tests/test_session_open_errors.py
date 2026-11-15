"""Realtime session errors: a session that never came up is reported once,
by the caller that waited for it (it was also reported through on_error: two
error sounds and API_ERRORS counted twice)."""
import asyncio
from contextlib import asynccontextmanager

from config import AppConfig
from realtime.base import BaseRealtimeManager


class Manager(BaseRealtimeManager):
    def __init__(self, fail_connect_after=0):
        super().__init__(AppConfig(), asyncio.Queue(), asyncio.Queue())
        self.connects = 0
        self.fail_connect_after = fail_connect_after
        self.errors = 0
        self.set_callbacks(on_error=self._error)
        self.live_cfg.reconnect_backoff_max = 0.0

    def _error(self):
        self.errors += 1

    def initialize(self):
        self._running = True

    def shutdown(self):
        self._running = False

    @asynccontextmanager
    async def _connect(self):
        self.connects += 1
        if self.connects > self.fail_connect_after:
            raise OSError("network unreachable")
        yield

    async def _send_to_api_task(self):
        await asyncio.sleep(3600)

    async def _receive_from_api_task(self):
        await asyncio.sleep(3600)

    async def send_activity_start(self): pass
    async def send_activity_end(self): pass
    async def send_text(self, text): return False


async def test_a_session_that_never_opened_does_not_fire_on_error():
    mgr = Manager()
    mgr.initialize()
    await mgr.open_session()
    assert not mgr.session_active
    assert mgr.errors == 0


async def test_a_session_lost_after_it_opened_fires_on_error_once():
    mgr = Manager(fail_connect_after=1)
    mgr.initialize()

    async def serve_then_lose_the_connection():
        await asyncio.sleep(0)
        raise OSError("connection reset")

    mgr._serve_session = serve_then_lose_the_connection
    await asyncio.wait_for(mgr._run_conversation(), 5)
    # opened once, then every reconnect failed: reported once, at the end
    assert mgr.connects == mgr.live_cfg.max_reconnect_attempts
    assert mgr.errors == 1

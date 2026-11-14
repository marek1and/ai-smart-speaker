"""SessionKeeper: the one place that closes the API session and its recording."""
import pytest

from config import get_config
from session_keeper import SessionKeeper


class Api:
    def __init__(self, active=True):
        self.session_active = active
        self.closed = 0

    async def close_session(self):
        self.session_active = False
        self.closed += 1


class Recorder:
    def __init__(self):
        self.calls = []

    def close(self):
        self.calls.append("close")

    def close_as_false_trigger(self):
        self.calls.append("false_trigger")


@pytest.fixture
def keeper():
    k = SessionKeeper(get_config().live, Recorder())
    k.api = Api()
    return k


async def test_session_is_closed_with_its_recording_after_the_inactivity_timeout(keeper):
    timeout = get_config().live.session_inactivity_timeout
    await keeper.close_if_inactive(timeout - 1)
    assert keeper.api.closed == 0 and keeper.recorder.calls == []
    await keeper.close_if_inactive(timeout + 1)
    assert keeper.api.closed == 1 and keeper.recorder.calls == ["close"]


async def test_a_closed_session_is_not_closed_again(keeper):
    keeper.api.session_active = False
    await keeper.close_if_inactive(get_config().live.session_inactivity_timeout + 1)
    assert keeper.api.closed == 0 and keeper.recorder.calls == []


async def test_false_trigger_files_the_recording_as_one(keeper):
    await keeper.close("false_trigger")
    assert keeper.api.closed == 1 and keeper.recorder.calls == ["false_trigger"]


async def test_turn_limit_closes_only_a_live_session(keeper):
    keeper.turn_count = get_config().live.session_max_turns - 1
    keeper.api.session_active = False  # already dropped by the API
    await keeper.turn_finished()
    assert keeper.api.closed == 0


def test_no_session_before_the_api_manager_exists():
    assert SessionKeeper(get_config().live, Recorder()).active is False

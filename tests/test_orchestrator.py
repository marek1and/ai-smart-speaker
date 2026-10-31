"""Orchestrator state machine around a turn, with the API, MPD and MQTT faked.
Past bugs: a handler exception left the speaker deaf in RESPONDING until the
API dropped the session; a deferred radio play waited for the next wake word."""
import asyncio

import pytest

import orchestrator as orch
from audio.sounds import SoundEvent
from config import get_config
from state import SpeakerState


class FakeApi:
    def __init__(self, open_ok=True, send_ok=True):
        self.session_active = False
        self.open_ok, self.send_ok = open_ok, send_ok
        self.sent = []
        self.pending_actions = []
        self.user_transcript = ""
        self.ai_transcript = ""
        self.request_for_user_input = False
        self.activity_started = False
        self.chunks_received = 0
        self.closed = 0

    async def open_session(self):
        self.session_active = self.open_ok

    async def close_session(self):
        self.session_active = False
        self.closed += 1

    def start_new_turn(self):
        self.user_transcript = self.ai_transcript = ""
        self.request_for_user_input = False

    async def send_text(self, text):
        self.sent.append(text)
        return self.send_ok

    async def send_activity_start(self):
        pass


class FakeMPD:
    def __init__(self, playing=False):
        self.playing = playing
        self.calls = []

    async def is_playing(self):
        return self.playing

    async def duck(self):
        self.calls.append("duck")

    async def unduck(self):
        self.calls.append("unduck")

    async def play_station(self, url, name, key=None):
        self.calls.append(("play_station", url, name, key))
        self.playing = True

    async def play_music(self, uris, title):
        self.calls.append(("play_music", tuple(uris), title))
        self.playing = True

    async def resume(self, resolve):
        self.calls.append("resume")
        self.playing = True

    async def set_volume(self, v):
        self.calls.append(("set_volume", v))

    def get_current_station_name(self):
        return "Radio Eska"


class FakeBridge:
    def __init__(self):
        self.turns = []

    def publish_turn(self, event):
        self.turns.append(event)


class FakeRecorder:
    def start(self, **k): pass
    def close(self): pass
    def close_as_false_trigger(self): pass
    def write(self, data): pass


class FakeSounds:
    def __init__(self):
        self.played = []

    def play(self, event):
        self.played.append(event)


@pytest.fixture
def make():
    def factory(api=None, playing=False):
        o = orch.AudioOrchestrator(get_config())
        o._api_manager = api or FakeApi()
        o.mpd_client = FakeMPD(playing)
        o._mqtt_bridge = FakeBridge()
        o._recorder = FakeRecorder()
        o._sound_player = FakeSounds()
        o._hybrid_vad = None
        o._reset_wake_detector = lambda **k: None
        return o
    return factory


# --- typed requests ---------------------------------------------------------------

def test_text_is_accepted_only_when_the_speaker_can_answer(make):
    o = make()
    assert o.submit_text("a")
    assert not o.submit_text("b")  # one waits already
    for state, ok in [(SpeakerState.RESPONDING, False), (SpeakerState.LISTENING, False),
                      (SpeakerState.FOLLOW_UP, True)]:
        o = make()
        o._state = state
        assert o.submit_text("x") is ok


async def test_text_turn_opens_a_session_without_silence_timeout(make):
    o = make(playing=True)
    o.submit_text("zgaś światło")
    await o._handle_idle_state()
    assert o._api_manager.sent == ["zgaś światło"]
    assert o.state == SpeakerState.LISTENING
    assert o._listening_entry_time is None
    assert "duck" in o.mpd_client.calls


@pytest.mark.parametrize("api,error", [
    (FakeApi(open_ok=False), "session_failed"),
    (FakeApi(send_ok=False), "send_failed"),
])
async def test_text_turn_failures_go_back_to_idle_and_are_reported(make, api, error):
    o = make(api)
    o.submit_text("x")
    await o._handle_idle_state()
    assert o.state == SpeakerState.IDLE
    assert o._mqtt_bridge.turns[-1] == {"source": "text", "request": "x", "reply": "", "error": error}


async def test_turns_are_published_with_their_source(make):
    o = make()
    o.submit_text("ile to 2+2")
    await o._handle_idle_state()
    o._api_manager.ai_transcript = "Cztery."
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert o._mqtt_bridge.turns[-1] == {"source": "text", "request": "ile to 2+2", "reply": "Cztery."}
    o._api_manager.user_transcript = " która godzina "
    o._api_manager.ai_transcript = "Dwunasta."
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert o._mqtt_bridge.turns[-1] == {"source": "voice", "request": "która godzina", "reply": "Dwunasta."}


async def test_text_answers_a_follow_up_question(make):
    o = make()
    o._api_manager.session_active = True
    o._state = SpeakerState.FOLLOW_UP
    o.submit_text("tak")
    await o._open_session_and_listen(o._text_requests.get_nowait())
    assert o._api_manager.sent == ["tak"] and o.state == SpeakerState.LISTENING


# --- deferred actions ---------------------------------------------------------------

@pytest.mark.parametrize("action,expected", [
    ({"type": "play_internet_radio", "payload": {"url": "http://u", "name": "Radio Zet", "key": "zet"}},
     ("play_station", "http://u", "Radio Zet", "zet")),
    ({"type": "play_internet_radio", "payload": {"action": "play"}}, "resume"),
    ({"type": "play_music", "payload": {"uris": ["youtube:video:a"], "title": "T"}},
     ("play_music", ("youtube:video:a",), "T")),
])
async def test_playback_actions_run_and_end_the_turn_quietly(make, action, expected):
    o = make()
    o._api_manager.pending_actions.append(action)
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert expected in o.mpd_client.calls
    assert o.state == SpeakerState.IDLE
    assert SoundEvent.END_CONVERSATION not in o._sound_player.played
    assert o._api_manager.pending_actions == []


def test_every_deferred_tool_has_a_dispatch_branch():
    import inspect
    from realtime.base import BaseRealtimeManager
    src = inspect.getsource(orch.AudioOrchestrator._dispatch_deferred_actions)
    for name in BaseRealtimeManager.DEFERRED_FUNCTIONS:
        assert f'"{name}"' in src, name


async def test_volume_action_does_not_end_the_turn_by_itself(make):
    o = make()
    o._api_manager.pending_actions.append({"type": "set_playback_volume", "payload": {"volume_percentage": 30}})
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert ("set_volume", 30) in o.mpd_client.calls
    assert SoundEvent.END_CONVERSATION in o._sound_player.played


async def test_lookup_error_in_a_deferred_play_starts_nothing(make):
    o = make()
    o._api_manager.pending_actions.append({"type": "play_internet_radio", "payload": {"status": "error", "details": "x"}})
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(isinstance(c, tuple) for c in o.mpd_client.calls)


async def test_actions_that_arrived_in_idle_run_on_the_next_wake(make):
    o = make()
    o._api_manager.session_active = True
    o._api_manager.pending_actions.append({"type": "play_internet_radio", "payload": {"action": "play"}})
    await o._open_session_and_listen()
    assert "resume" in o.mpd_client.calls


# --- end of turn ----------------------------------------------------------------------

@pytest.mark.parametrize("transcript,flag,follow_up", [
    ("Którą lampę?", False, True),
    ("Gotowe.", True, True),
    ("Pytanie (czy?) już nie.", False, False),
    ("Gotowe.", False, False),
])
async def test_follow_up_decision(make, transcript, flag, follow_up):
    o = make()
    o._api_manager.ai_transcript = transcript
    o._api_manager.request_for_user_input = flag
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert (o.state == SpeakerState.FOLLOW_UP) is follow_up


async def test_finish_turn_does_not_stomp_a_barge_in(make):
    o = make()
    o._state = SpeakerState.LISTENING
    await o._finish_turn()
    assert o.state == SpeakerState.LISTENING


async def test_session_closes_after_the_turn_limit(make):
    o = make()
    o._api_manager.session_active = True
    o._session_turn_count = get_config().live.session_max_turns - 1
    o._state = SpeakerState.RESPONDING
    await o._finish_turn()
    assert o._api_manager.closed == 1


async def test_crashed_handler_returns_to_idle_with_the_error_sound(make):
    # 04.09.2026: an exception left the state machine in RESPONDING for minutes
    o = make()
    o._state = SpeakerState.RESPONDING
    await o._recover_from_state_error()
    assert o.state == SpeakerState.IDLE
    assert SoundEvent.ERROR in o._sound_player.played
    assert "unduck" in o.mpd_client.calls


async def test_state_machine_survives_a_handler_exception(make, monkeypatch):
    o = make()
    o._running = True
    o._state = SpeakerState.RESPONDING
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler bug")
        o._running = False

    monkeypatch.setattr(o, "_handle_responding_state", boom)
    monkeypatch.setattr(o, "_handle_idle_state", boom)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(orch.asyncio, "sleep", lambda s: real_sleep(0))
    await asyncio.wait_for(o._state_machine_task(), 2)
    assert calls == 2 and o.state == SpeakerState.IDLE

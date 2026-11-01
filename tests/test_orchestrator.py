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
        self.watchdogs = []

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

    def start_watchdog(self, timeout):
        self.watchdogs.append(timeout)


class FakeMPD:
    def __init__(self, playing=False):
        self.playing = playing
        self.calls = []
        self.music_starts = True
        self.playback_generation = 0

    async def is_playing(self):
        return self.playing

    async def duck(self):
        self.calls.append("duck")

    async def unduck(self):
        self.calls.append("unduck")

    async def play_station(self, url, name, key=None):
        self.calls.append(("play_station", url, name, key))
        self.playing = True

    async def play_music(self, uris, title, start_timeout=None, confirm_timeout=None):
        self.calls.append(("play_music", tuple(uris), title))
        self.start_timeout = start_timeout
        self.confirm_timeout = confirm_timeout
        self.playing = self.music_starts
        return self.music_starts

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


async def test_text_turn_starts_the_turn_watchdog(make):
    # a stalled socket must not leave the speaker deaf in LISTENING
    o = make()
    o.submit_text("x")
    await o._handle_idle_state()
    assert o._api_manager.watchdogs == [get_config().live.turn_watchdog_timeout]


async def test_unanswered_text_turn_ends_and_is_reported(make):
    o = make()
    o.submit_text("x")
    await o._handle_idle_state()
    o._on_turn_timeout()  # watchdog fired, no audio ever came
    await o._handle_listening_state()
    assert o.state == SpeakerState.IDLE
    assert o._mqtt_bridge.turns[-1] == {"source": "text", "request": "x", "reply": ""}


async def test_voice_turn_without_audio_still_waits_in_listening(make):
    # unchanged for speech: late audio after turn_complete still plays (05.09.2026)
    o = make()
    o._state = SpeakerState.LISTENING
    o._turn_complete_event.set()
    await o._handle_listening_state()
    assert o.state == SpeakerState.LISTENING


async def test_idle_loop_wakes_at_once_for_text(make):
    o = make()
    idle = asyncio.create_task(o._handle_idle_state())
    await asyncio.sleep(0.01)
    o.submit_text("x")
    await asyncio.wait_for(idle, 0.2)  # returns long before the 1 s poll
    await o._handle_idle_state()
    assert o._api_manager.sent == ["x"]


async def test_wake_word_still_opens_a_session(make):
    o = make()
    o._wake_event.set()
    o._idle_signal.set()
    await o._handle_idle_state()
    assert o.state == SpeakerState.LISTENING and o._api_manager.sent == []


@pytest.mark.parametrize("start", ["follow_up", "wake"])
async def test_voice_turn_answers_a_waiting_text_with_busy(make, start):
    o = make()
    o._api_manager.session_active = True
    o._state = SpeakerState.FOLLOW_UP
    o.submit_text("stare pytanie")
    if start == "follow_up":
        await o._start_followup_conversation()
    else:
        await o._open_session_and_listen()
    assert o._text_requests.empty()
    assert o._mqtt_bridge.turns[-1] == {"source": "text", "request": "stare pytanie", "reply": "", "error": "busy"}


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


async def test_music_gets_the_configured_start_timeout(make):
    o = make()
    o._api_manager.pending_actions.append({"type": "play_music", "payload": {"uris": ["youtube:video:a"], "title": "T"}})
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert o.mpd_client.start_timeout == o.config.music.start_timeout
    assert o.mpd_client.confirm_timeout == o.config.music.start_confirm_timeout
    assert "error" not in o._mqtt_bridge.turns[-1]
    assert SoundEvent.ERROR not in o._sound_player.played


async def test_music_that_fails_to_start_sounds_and_reports_an_error(make):
    # The AI has already said "Puszczam ..."; the room must not just stay silent.
    o = make()
    o.mpd_client.music_starts = False
    o._api_manager.pending_actions.append({"type": "play_music", "payload": {"uris": ["youtube:video:a"], "title": "T"}})
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert SoundEvent.ERROR in o._sound_player.played
    assert SoundEvent.END_CONVERSATION not in o._sound_player.played
    assert o._mqtt_bridge.turns[-1]["error"] == "music_start_failed"
    assert o.state == SpeakerState.IDLE


async def test_turn_is_published_as_it_was_when_a_barge_in_clears_it(make):
    # the deferred play can take seconds; a barge-in meanwhile starts a new turn
    o = make()
    o._api_manager.user_transcript, o._api_manager.ai_transcript = "puść Dżem", "Puszczam."
    o._api_manager.pending_actions.append({"type": "play_music", "payload": {"uris": ["local:track:a"], "title": "T"}})
    o._state = SpeakerState.RESPONDING
    real = o._dispatch_deferred_actions

    async def dispatch_with_barge_in():
        result = await real()
        o._api_manager.user_transcript = o._api_manager.ai_transcript = ""
        return result

    o._dispatch_deferred_actions = dispatch_with_barge_in
    await o._execute_post_response_actions()
    assert o._mqtt_bridge.turns[-1] == {"source": "voice", "request": "puść Dżem", "reply": "Puszczam."}


PREFETCHED = {"type": "play_music", "payload": {"uris": ["youtube:video:a"], "title": "T", "prefetched": True}}


async def test_prefetched_music_waits_for_the_download_then_plays(make, monkeypatch):
    waited = []

    async def wait(uri, timeout):
        waited.append((uri, timeout))
        return True

    monkeypatch.setattr(orch, "wait_for_music", wait)
    o = make()
    o._api_manager.pending_actions.append(dict(PREFETCHED))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert waited == [("youtube:video:a", o.config.music.start_timeout)]
    assert ("play_music", ("youtube:video:a",), "T") in o.mpd_client.calls


@pytest.mark.parametrize("interrupt", ["stop", "barge_in"])
async def test_music_start_is_dropped_when_superseded_while_downloading(make, monkeypatch, interrupt):
    o = make()

    async def wait(uri, timeout):
        if interrupt == "stop":
            o.mpd_client.playback_generation += 1   # "stop" or another playback meanwhile
        else:
            o._state = SpeakerState.LISTENING       # the user spoke over the reply
        return True

    monkeypatch.setattr(orch, "wait_for_music", wait)
    o._api_manager.pending_actions.append(dict(PREFETCHED))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(c[0] == "play_music" for c in o.mpd_client.calls if isinstance(c, tuple))
    assert SoundEvent.ERROR not in o._sound_player.played


async def test_stop_while_the_ai_still_speaks_cancels_the_music(make):
    # seen live 01.11.2026: power OFF before the deferred start, music played anyway
    o = make()
    o._api_manager.pending_actions.append(
        {"type": "play_music", "payload": {"uris": ["local:track:a"], "title": "T", "generation": 4}})
    o.mpd_client.playback_generation = 5  # the stop came after the tool call
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(isinstance(c, tuple) and c[0] == "play_music" for c in o.mpd_client.calls)


async def test_cached_music_does_not_wait(make, monkeypatch):
    async def wait(uri, timeout):
        raise AssertionError("must not wait for a track that was not prefetched")

    monkeypatch.setattr(orch, "wait_for_music", wait)
    o = make()
    o._api_manager.pending_actions.append({"type": "play_music", "payload": {"uris": ["local:track:a"], "title": "T"}})
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert ("play_music", ("local:track:a",), "T") in o.mpd_client.calls


async def test_turn_end_drops_leftover_prefetched_entries(make, monkeypatch):
    dropped = []

    async def drop():
        dropped.append(True)

    monkeypatch.setattr(orch, "drop_prefetched", drop)
    o = make()
    o._state = SpeakerState.RESPONDING
    await o._finish_turn()
    assert dropped == [True]


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

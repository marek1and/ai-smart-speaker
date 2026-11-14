"""Orchestrator state machine around a turn, with the API, MPD and MQTT faked.
Past bugs: a handler exception left the speaker deaf in RESPONDING until the
API dropped the session; a deferred radio play waited for the next wake word."""
import asyncio
import threading
from dataclasses import replace

import numpy as np
import pytest

import deferred_playback
import orchestrator as orch
import session_keeper
from audio.sounds import SoundEvent
from config import get_config
from state import DeferredAction, SpeakerState


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
        self.awaiting_reply = False
        self.playback_interrupted = False
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

    async def play_music(self, uris, title, start_timeout=None, confirm_timeout=None, start_ducked=False):
        self.calls.append(("play_music", tuple(uris), title))
        self.start_timeout = start_timeout
        self.confirm_timeout = confirm_timeout
        self.start_ducked = start_ducked
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
        o._api_manager = o._session.api = api or FakeApi()
        o.mpd_client = o._deferred_playback.mpd_client = FakeMPD(playing)
        o._mqtt_bridge = FakeBridge()
        o._recorder = o._session.recorder = FakeRecorder()
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
    o._api_manager.session_active = True
    o._state = SpeakerState.LISTENING
    o._turn_complete_event.set()
    await o._handle_listening_state()
    assert o.state == SpeakerState.LISTENING


async def test_voice_turn_without_audio_ends_when_nothing_follows(make, monkeypatch):
    # nothing left LISTENING: the microphone streamed to the API until somebody spoke
    o = make()
    o._api_manager.session_active = True
    o._state = SpeakerState.LISTENING
    closed_as_false = []
    o._recorder.close_as_false_trigger = lambda: closed_as_false.append(1)
    o._turn_complete_event.set()
    await o._handle_listening_state()
    now = orch.time.time()
    monkeypatch.setattr(orch.time, "time", lambda: now + get_config().live.initial_silence_timeout + 1)
    await o._handle_listening_state()
    assert o.state == SpeakerState.IDLE
    # the user did speak: not a false wake word, the session stays for the next one
    assert not closed_as_false and o._api_manager.closed == 0
    assert SoundEvent.END_CONVERSATION in o._sound_player.played


async def test_voice_turn_without_audio_keeps_listening_once_the_user_speaks(make, monkeypatch):
    o = make()
    o._api_manager.session_active = True
    o._state = SpeakerState.LISTENING
    o._turn_complete_event.set()
    await o._handle_listening_state()
    o._api_manager.activity_started = True
    now = orch.time.time()
    monkeypatch.setattr(orch.time, "time", lambda: now + get_config().live.initial_silence_timeout + 1)
    await o._handle_listening_state()
    assert o.state == SpeakerState.LISTENING


async def test_silence_after_the_wake_word_is_still_a_false_trigger(make, monkeypatch):
    o = make()
    o._wake_event.set()
    await o._handle_idle_state()
    closed_as_false = []
    o._recorder.close_as_false_trigger = lambda: closed_as_false.append(1)
    now = orch.time.time()
    monkeypatch.setattr(orch.time, "time", lambda: now + get_config().live.initial_silence_timeout + 1)
    await o._handle_listening_state()
    assert o.state == SpeakerState.IDLE and closed_as_false and o._api_manager.closed == 1


async def test_voice_turn_ends_after_an_api_error(make):
    # 07.11.2026: 1008 closed the session, LISTENING ignored the wake word until restart
    o = make()
    o._api_manager.session_active = False
    o._state = SpeakerState.LISTENING
    o._on_api_error()
    await o._handle_listening_state()
    assert o.state == SpeakerState.IDLE
    assert SoundEvent.ERROR in o._sound_player.played


async def test_voice_turn_the_api_never_answered_ends_on_the_watchdog(make):
    o = make()
    o._api_manager.session_active = True
    o._api_manager.awaiting_reply = True  # activity_end sent, no turn_complete
    o._state = SpeakerState.LISTENING
    o._on_turn_timeout()
    await o._handle_listening_state()
    assert o.state == SpeakerState.IDLE


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
    (DeferredAction("play_internet_radio", {"url": "http://u", "name": "Radio Zet", "key": "zet"}),
     ("play_station", "http://u", "Radio Zet", "zet")),
    (DeferredAction("play_internet_radio", {"action": "play"}), "resume"),
    (DeferredAction("play_music", {"uris": ["youtube:video:a"], "title": "T"}),
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
    o._api_manager.pending_actions.append(DeferredAction("play_music", {"uris": ["youtube:video:a"], "title": "T"}))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert abs(o.mpd_client.start_timeout - o.config.music.start_timeout) < 1.0
    assert o.mpd_client.confirm_timeout == o.config.music.start_confirm_timeout
    assert "error" not in o._mqtt_bridge.turns[-1]
    assert SoundEvent.ERROR not in o._sound_player.played


async def test_music_that_fails_to_start_sounds_and_reports_an_error(make):
    # The AI has already said "Puszczam ..."; the room must not just stay silent.
    o = make()
    o.mpd_client.music_starts = False
    o._api_manager.pending_actions.append(DeferredAction("play_music", {"uris": ["youtube:video:a"], "title": "T"}))
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
    o._api_manager.pending_actions.append(DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"}))
    o._state = SpeakerState.RESPONDING
    real = o._dispatch_deferred_actions

    async def dispatch_with_barge_in():
        result = await real()
        o._api_manager.user_transcript = o._api_manager.ai_transcript = ""
        return result

    o._dispatch_deferred_actions = dispatch_with_barge_in
    await o._execute_post_response_actions()
    assert o._mqtt_bridge.turns[-1] == {"source": "voice", "request": "puść Dżem", "reply": "Puszczam."}


PREFETCHED = DeferredAction("play_music", {"uris": ["youtube:video:a"], "title": "T", "prefetched": True})


async def test_prefetched_music_waits_for_the_download_then_plays(make, monkeypatch):
    waited = []

    async def wait(uri, timeout):
        waited.append((uri, timeout))
        return True

    monkeypatch.setattr(deferred_playback, "wait_for_music", wait)
    o = make()
    o._api_manager.pending_actions.append(replace(PREFETCHED))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert waited == [("youtube:video:a", o.config.music.start_timeout)]
    assert ("play_music", ("youtube:video:a",), "T") in o.mpd_client.calls


async def test_music_start_is_dropped_when_a_stop_comes_while_downloading(make, monkeypatch):
    o = make()

    async def wait(uri, timeout):
        o.mpd_client.playback_generation += 1   # "stop" or another playback meanwhile
        return True

    monkeypatch.setattr(deferred_playback, "wait_for_music", wait)
    o._api_manager.pending_actions.append(replace(PREFETCHED))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(c[0] == "play_music" for c in o.mpd_client.calls if isinstance(c, tuple))
    assert SoundEvent.ERROR not in o._sound_player.played


async def test_music_starts_at_the_tool_call_ducked_and_the_turn_end_waits_for_it(make, monkeypatch):
    # Gemini often needs 10-20 s to finish the reply after a tool call
    o = make()
    o._state = SpeakerState.RESPONDING
    action = DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"})
    o._api_manager.pending_actions.append(action)
    o._deferred_playback.on_action(action)
    await action.task
    assert ("play_music", ("local:track:a",), "T") in o.mpd_client.calls and o.mpd_client.start_ducked
    await o._execute_post_response_actions()
    assert [c for c in o.mpd_client.calls if isinstance(c, tuple) and c[0] == "play_music"] == \
        [("play_music", ("local:track:a",), "T")]  # started once
    assert o.state == SpeakerState.IDLE and SoundEvent.END_CONVERSATION not in o._sound_player.played


async def test_music_that_starts_after_the_turn_fades_in_normally(make):
    o = make()
    o._state = SpeakerState.IDLE  # the reply ended before the download did
    await o._deferred_playback.start_music({"uris": ["local:track:a"], "title": "T"})
    assert o.mpd_client.start_ducked is False


async def test_early_start_that_fails_sounds_and_reports(make):
    o = make()
    o.mpd_client.music_starts = False
    o._state = SpeakerState.RESPONDING
    action = DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"})
    o._api_manager.pending_actions.append(action)
    o._deferred_playback.on_action(action)
    await o._execute_post_response_actions()
    assert SoundEvent.ERROR in o._sound_player.played
    assert o._mqtt_bridge.turns[-1]["error"] == "music_start_failed"


async def test_stop_while_the_ai_still_speaks_cancels_the_music(make):
    # seen live 01.11.2026: power OFF before the deferred start, music played anyway
    o = make()
    o._api_manager.pending_actions.append(
        DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T", "generation": 4}))
    o.mpd_client.playback_generation = 5  # the stop came after the tool call
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(isinstance(c, tuple) and c[0] == "play_music" for c in o.mpd_client.calls)


async def test_cached_music_does_not_wait(make, monkeypatch):
    async def wait(uri, timeout):
        raise AssertionError("must not wait for a track that was not prefetched")

    monkeypatch.setattr(deferred_playback, "wait_for_music", wait)
    o = make()
    o._api_manager.pending_actions.append(DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"}))
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


async def test_a_new_session_gets_the_current_device_states(make, monkeypatch):
    async def states():
        return "Stan: light.salon_glowne: on"

    monkeypatch.setattr(session_keeper, "prompt_state_context", states)
    o = make()
    seen = []
    real_open = o._api_manager.open_session

    async def open_session():
        seen.append(o._api_manager.session_context)
        await real_open()

    o._api_manager.open_session = open_session
    await o._open_session_and_listen()
    assert seen == ["Stan: light.salon_glowne: on"]


async def test_room_audio_is_not_sent_while_the_reply_is_awaited(make):
    # music from the room after activity_end made Gemini stream ~20 s of silence
    o = make()
    o._api_manager.session_active = True
    o._hybrid_vad = None
    sent = []

    async def forward(audio):
        sent.append(audio)

    o._forward_audio_to_api = forward
    frame = np.zeros(512, dtype=np.int16)
    await o._process_listening_audio(frame, frame.tobytes())
    recorded = []
    o._recorder.write = recorded.append
    o._api_manager.awaiting_reply = True  # activity_end sent / a typed request
    await o._process_listening_audio(frame, frame.tobytes())
    assert len(sent) == 1
    assert recorded == [frame.tobytes()]  # kept for diagnosing cut-off commands


async def test_a_newer_music_request_wins_over_one_still_downloading(make, monkeypatch):
    # review 01.11.2026: "nie, puść Y" over the reply played X and dropped Y
    o = make()
    o._state = SpeakerState.RESPONDING
    release = asyncio.Event()

    async def wait(uri, timeout):
        if uri == "youtube:video:x":
            await release.wait()  # X is still downloading
        return True

    monkeypatch.setattr(deferred_playback, "wait_for_music", wait)
    x = DeferredAction("play_music", {"uris": ["youtube:video:x"], "title": "X", "prefetched": True})
    o._deferred_playback.on_action(x)
    o._api_manager.start_new_turn = lambda: None
    y = DeferredAction("play_music", {"uris": ["youtube:video:y"], "title": "Y", "prefetched": True})
    o._deferred_playback.on_action(y)
    assert await y.task is True
    release.set()
    assert await x.task is None
    played = [c[2] for c in o.mpd_client.calls if isinstance(c, tuple) and c[0] == "play_music"]
    assert played == ["Y"]


async def test_radio_asked_before_music_in_one_turn_does_not_replace_it(make):
    o = make()
    o._state = SpeakerState.RESPONDING
    radio = DeferredAction("play_internet_radio", {"url": "http://u", "name": "Zet", "key": "zet"})
    music = DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"})
    for a in (radio, music):
        o._api_manager.pending_actions.append(a)
        o._deferred_playback.on_action(a)
    await o._execute_post_response_actions()
    assert not any(isinstance(c, tuple) and c[0] == "play_station" for c in o.mpd_client.calls)
    assert ("play_music", ("local:track:a",), "T") in o.mpd_client.calls


async def test_play_gets_only_the_time_left_after_the_download_wait(make, monkeypatch):
    # review: 30 s wait + 30 s play + confirm held the turn ~63 s
    o = make()
    o._state = SpeakerState.RESPONDING
    monkeypatch.setattr(o.config.music, "start_timeout", 30.0)

    async def wait(uri, timeout):
        await asyncio.sleep(0.8)  # part of the deadline spent on the download
        return False

    monkeypatch.setattr(deferred_playback, "wait_for_music", wait)
    await o._deferred_playback.start_music({"uris": ["youtube:video:x"], "title": "X", "prefetched": True})
    assert 28.5 < o.mpd_client.start_timeout < 29.5  # the rest of the 30 s, not another 30 s


async def test_a_radio_call_that_changes_nothing_does_not_cancel_the_music(make, monkeypatch):
    # review 2: play_internet_radio() with the radio already playing has no url/action
    o = make()
    o._state = SpeakerState.RESPONDING
    music = DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"})
    noop_radio = DeferredAction("play_internet_radio", {"for_model": {"already_playing": True}})
    for a in (music, noop_radio):
        o._api_manager.pending_actions.append(a)
        o._deferred_playback.on_action(a)
    await o._execute_post_response_actions()
    assert ("play_music", ("local:track:a",), "T") in o.mpd_client.calls


async def test_a_failing_music_start_does_not_break_the_turn_end(make):
    # review 2: the exception went through await task into the state machine
    o = make()
    o._state = SpeakerState.RESPONDING

    async def boom(*a, **k):
        raise RuntimeError("mopidy says no")

    o.mpd_client.play_music = boom
    action = DeferredAction("play_music", {"uris": ["local:track:a"], "title": "T"})
    vol = DeferredAction("set_playback_volume", {"volume_percentage": 30})
    for a in (action, vol):
        o._api_manager.pending_actions.append(a)
        o._deferred_playback.on_action(a)
    await o._execute_post_response_actions()
    assert SoundEvent.ERROR in o._sound_player.played
    assert ("set_volume", 30) in o.mpd_client.calls  # the rest of the turn still ran
    assert o._mqtt_bridge.turns[-1]["error"] == "music_start_failed"


def test_every_deferred_tool_has_a_dispatch_branch():
    import inspect
    from realtime.base import BaseRealtimeManager
    src = inspect.getsource(deferred_playback.DeferredPlayback.dispatch)
    for name in BaseRealtimeManager.DEFERRED_FUNCTIONS:
        assert f'"{name}"' in src, name


async def test_volume_action_does_not_end_the_turn_by_itself(make):
    o = make()
    o._api_manager.pending_actions.append(DeferredAction("set_playback_volume", {"volume_percentage": 30}))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert ("set_volume", 30) in o.mpd_client.calls
    assert SoundEvent.END_CONVERSATION in o._sound_player.played


async def test_lookup_error_in_a_deferred_play_starts_nothing(make):
    o = make()
    o._api_manager.pending_actions.append(DeferredAction("play_internet_radio", {"status": "error", "details": "x"}))
    o._state = SpeakerState.RESPONDING
    await o._execute_post_response_actions()
    assert not any(isinstance(c, tuple) for c in o.mpd_client.calls)


async def test_actions_that_arrived_in_idle_run_on_the_next_wake(make):
    o = make()
    o._api_manager.session_active = True
    o._api_manager.pending_actions.append(DeferredAction("play_internet_radio", {"action": "play"}))
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


class SlowUnduckMPD(FakeMPD):
    """unduck() hangs until released: the window in which the next turn can start."""

    def __init__(self, playing=False):
        super().__init__(playing)
        self.release = asyncio.Event()

    async def unduck(self):
        await self.release.wait()
        self.calls.append("unduck")


async def _in_unduck(o, coro):
    task = asyncio.create_task(coro)
    await asyncio.sleep(0.01)
    assert not task.done()
    return task


async def test_barge_in_during_the_unduck_keeps_its_turn(make):
    o = make()
    o.mpd_client = SlowUnduckMPD()
    o._state = SpeakerState.RESPONDING
    finish = await _in_unduck(o, o._finish_turn())
    await o._execute_barge_in()
    o.mpd_client.release.set()
    await finish
    assert o.state == SpeakerState.LISTENING
    assert SoundEvent.END_CONVERSATION not in o._sound_player.played
    assert o._session.turn_count == 0


async def test_follow_up_answer_during_the_timeout_unduck_keeps_its_turn(make):
    o = make()
    o.mpd_client = SlowUnduckMPD()
    o._state = SpeakerState.FOLLOW_UP
    finish = await _in_unduck(o, o._finish_turn())
    await o._start_followup_conversation()
    o.mpd_client.release.set()
    await finish
    assert o.state == SpeakerState.LISTENING
    assert SoundEvent.END_CONVERSATION not in o._sound_player.played


async def test_barge_in_during_the_deferred_actions_stays_ducked(make):
    # the old turn used to unduck the radio under the new one before giving way
    o = make()
    o._api_manager.pending_actions.append(
        DeferredAction("play_internet_radio", {"url": "http://x.invalid/a.mp3", "name": "Eska"}))
    o._state = SpeakerState.RESPONDING
    release = asyncio.Event()

    async def slow_play_station(url, name, key=None):
        await release.wait()

    o.mpd_client.play_station = slow_play_station
    end = asyncio.create_task(o._execute_post_response_actions())
    await asyncio.sleep(0.01)
    await o._execute_barge_in()
    release.set()
    await end
    assert o.state == SpeakerState.LISTENING
    assert "unduck" not in o.mpd_client.calls


async def test_a_wake_word_repeated_while_listening_does_not_keep_the_turn_open(make):
    # only a new turn stops the cleanup, not the LISTENING state by itself
    o = make()
    o.mpd_client = SlowUnduckMPD()
    o._state = SpeakerState.LISTENING
    finish = await _in_unduck(o, o._finish_turn())
    await o._handle_wake_word_result(orch.WakeWordResult(True, 0.9, 0.9))
    o.mpd_client.release.set()
    await finish
    assert o.state == SpeakerState.IDLE


async def test_empty_reply_with_a_radio_action_still_waits_for_late_audio(make):
    o = make()
    o._api_manager.session_active = True
    o._api_manager.pending_actions.append(
        DeferredAction("play_internet_radio", {"url": "http://x.invalid/a.mp3", "name": "Eska"}))
    o._state = SpeakerState.LISTENING
    o._turn_complete_event.set()
    await o._handle_listening_state()
    assert o.state == SpeakerState.LISTENING
    assert o.mpd_client.calls[-1] == "unduck"
    assert o._session.turn_count == 0


async def test_session_closes_after_the_turn_limit(make):
    o = make()
    o._api_manager.session_active = True
    o._session.turn_count = get_config().live.session_max_turns - 1
    o._state = SpeakerState.RESPONDING
    await o._finish_turn()
    assert o._api_manager.closed == 1


@pytest.mark.parametrize("state", [SpeakerState.LISTENING, SpeakerState.RESPONDING, SpeakerState.FOLLOW_UP])
async def test_crashed_handler_returns_to_idle_with_the_error_sound(make, state):
    # 04.09.2026: an exception left the state machine in RESPONDING for minutes
    o = make()
    o._state = state
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


# --- microphone loop must not wait for MPD ------------------------------------------

class FakeDetector:
    last_verifier_score = None
    last_window_rms = 0.0

    def __init__(self):
        self.relaxed = []

    def process(self, mono, relaxed_mode=False):
        self.relaxed.append(relaxed_mode)
        return 0.0, 0.0, 0.0, False


class SlowDuckMPD(FakeMPD):
    """duck() hangs until released, like a Mopidy that stopped answering."""

    def __init__(self):
        super().__init__(playing=True)
        self.release = asyncio.Event()

    async def duck(self):
        await self.release.wait()
        self.calls.append("duck")


FRAME = np.zeros(512, dtype=np.int16)


async def test_wake_detection_does_not_wait_for_a_hung_mopidy(make, client, mopidy):
    # play state unknown + Mopidy not answering: every frame waited for the connect timeout
    release = threading.Event()
    mopidy.connect = lambda host, port: release.wait(5)
    o = make()
    o.mpd_client = client
    o._wake_detector = FakeDetector()
    try:
        await asyncio.wait_for(o._detect_wake_word_async(asyncio.get_running_loop(), FRAME), 0.5)
    finally:
        release.set()
    assert o._wake_detector.relaxed == [False]


async def test_wake_gates_relax_from_the_cached_play_state(make, client, mopidy, monkeypatch):
    o = make()
    monkeypatch.setattr(o.wake_cfg, "relaxed_verifier_threshold", 0.3)
    o.mpd_client = client
    o._wake_detector = FakeDetector()
    await client.get_status()  # what the watcher does on every poll
    await o._detect_wake_word_async(asyncio.get_running_loop(), FRAME)
    mopidy.queue, mopidy.state, mopidy.song = ["http://x.invalid/a.mp3"], "play", 0
    await client.get_status()
    await o._detect_wake_word_async(asyncio.get_running_loop(), FRAME)
    assert o._wake_detector.relaxed == [False, True]


async def test_wake_word_does_not_wait_for_the_duck(make):
    o = make()
    o.mpd_client = SlowDuckMPD()
    await asyncio.wait_for(o._handle_wake_word_result(orch.WakeWordResult(True, 0.9, 0.9)), 0.5)
    assert o._wake_event.is_set()
    assert SoundEvent.WAKE_WORD in o._sound_player.played
    o.mpd_client.release.set()
    await asyncio.sleep(0.01)
    assert o.mpd_client.calls == ["duck"]


async def test_turn_end_unducks_after_a_duck_still_in_flight(make):
    # an unduck that ran first found nothing to restore; the duck landed later and stayed
    o = make()
    o.mpd_client = SlowDuckMPD()
    await asyncio.wait_for(o._handle_wake_word_result(orch.WakeWordResult(True, 0.9, 0.9)), 0.5)
    o._state = SpeakerState.RESPONDING
    finish = asyncio.create_task(o._finish_turn())
    await asyncio.sleep(0.01)
    assert o.mpd_client.calls == []
    o.mpd_client.release.set()
    await finish
    assert o.mpd_client.calls == ["duck", "unduck"]
    assert o.state == SpeakerState.IDLE


async def test_a_failing_duck_does_not_reach_the_microphone_loop(make):
    o = make()

    async def boom():
        raise OSError("mpd gone")

    o.mpd_client.playing = True
    o.mpd_client.duck = boom
    await o._handle_wake_word_result(orch.WakeWordResult(True, 0.9, 0.9))
    await o._finish_turn()
    assert o.mpd_client.calls == ["unduck"]

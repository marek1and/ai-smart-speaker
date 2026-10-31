"""MPD client against a Mopidy-like fake: the code paths behind past incidents
(radio silent after restart, stream frozen for 37 h, state machine killed by a
cancelled fade, every play crashing on clearerror) and radio/music sharing one
queue."""
import asyncio
import time

import pytest

import mpd_client.client as mpd_module


async def resolve_eska(key):
    return ("http://pinned.invalid/eska.mp3", "Radio Eska", "eska")


# --- starting playback on Mopidy -------------------------------------------

async def test_play_station_survives_mopidy_refusing_clearerror(client, mopidy):
    # 28.08-06.09.2026: the ValueError from clearerror aborted every play_station
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    assert mopidy.state == "play"
    assert client.events[0] == {"state": "ON", "station": "Radio Eska", "volume": 50}


async def test_connect_unmutes_mopidy_instead_of_enabling_mute_output(client, mopidy):
    # Mopidy's only output is "Mute", enabled == muted; enabling it silenced everything
    mopidy.muted = True
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    assert ("disableoutput", "0") in mopidy.calls
    assert "enableoutput" not in mopidy.names()
    assert not mopidy.muted


async def test_start_queue_retries_once_after_connection_reset(client, mopidy):
    client.is_connected = True
    mopidy.fail_next["clear"] = BrokenPipeError()
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    assert mopidy.state == "play" and mopidy.names().count("clear") == 1


async def test_start_queue_mpd_error_leaves_state_untouched(client, mopidy):
    client.is_connected = True
    mopidy.fail_next["add"] = mpd_module.MPDError("boom")
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    assert client.events == []
    assert client.radio_state.get_current_key() is None


# --- volume ----------------------------------------------------------------

@pytest.mark.parametrize("pct", [0, 1, 20, 50, 99, 100])
def test_volume_curve_round_trip(pct):
    w = mpd_module.MPDClientWrapper
    assert abs(w._map_volume_from_curve(w._map_volume_to_curve(pct)) - pct) <= 1


async def test_volume_zero_stops_and_keeps_restore_volume(client, mopidy):
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    await client.set_volume(0)
    assert mopidy.state == "stop"
    assert client.get_restore_volume() == 50


async def test_volume_out_of_range_is_clamped(client):
    await client.set_volume(150)
    assert client.get_restore_volume() == 100


async def test_duck_cancelling_a_fade_does_not_cancel_the_caller(client, mopidy, monkeypatch):
    # 12.07.2026: duck() cancelled the fade awaited by _finish_turn and the
    # CancelledError killed the orchestrator's state machine task.
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    started = asyncio.Event()

    async def slow_fade(target, duration):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(client, "_fade_volume_async", slow_fade)
    caller = asyncio.create_task(client._fade_to(80, 1.0))
    await started.wait()
    await client.duck()
    await asyncio.wait_for(caller, 1.0)  # finishes, does not raise CancelledError
    assert not caller.cancelled()


async def test_fade_caller_cancellation_still_propagates(client, monkeypatch):
    async def slow_fade(target, duration):
        await asyncio.sleep(10)

    monkeypatch.setattr(client, "_fade_volume_async", slow_fade)
    caller = asyncio.create_task(client._fade_to(80, 1.0))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_unduck_returns_without_waiting_for_the_fade(client, monkeypatch):
    await client.play_station("http://x/s.mp3", "Radio Eska", key="eska")
    client._is_ducked = True

    async def slow_fade(target, duration):
        await asyncio.sleep(10)

    monkeypatch.setattr(client, "_fade_volume_async", slow_fade)
    await asyncio.wait_for(client.unduck(), 0.5)
    client._fade_task.cancel()


# --- dead and frozen streams ------------------------------------------------

def test_stop_inside_grace_window_is_a_dead_stream(client, eska):
    client._playback_started_at = time.monotonic() - 3
    client._started_key = "eska"
    client._report_silent_stop()
    assert client.radio_state.get_cached_url("eska") is None


def test_stop_after_listening_is_a_person_pressing_stop(client, eska):
    client._playback_started_at = time.monotonic() - 600
    client._seen_play_since_start = True
    client._started_key = "eska"
    client._report_silent_stop()
    assert client.radio_state.get_cached_url("eska") == eska[0]


def test_silent_stop_is_reported_once_per_attempt(client, eska):
    client._playback_started_at = time.monotonic() - 3
    client._started_key = "eska"
    client._report_silent_stop()
    client.radio_state.update_station("eska", eska[0], "Radio Eska")
    client._playback_started_at = time.monotonic() - 3
    client._report_silent_stop()
    assert client.radio_state.get_cached_url("eska") == eska[0]


def test_frozen_position_is_detected_and_a_moving_one_is_not(client):
    client._playback_started_at = None
    client._track_position({"state": "play", "songid": "1", "elapsed": "10.0"})
    client._track_position({"state": "play", "songid": "1", "elapsed": "12.0"})
    assert not client.is_stream_stalled()
    client._track_position({"state": "play", "songid": "1", "elapsed": "12.0"})
    assert client.is_stream_stalled()


def test_position_is_not_judged_while_the_stream_buffers(client):
    client._playback_started_at = time.monotonic()
    for _ in range(3):
        client._track_position({"state": "play", "songid": "1", "elapsed": "0.0"})
    assert not client.is_stream_stalled()


def test_next_track_restarting_elapsed_is_not_a_stall(client):
    client._playback_started_at = None
    client._track_position({"state": "play", "songid": "1", "elapsed": "230.0"})
    client._track_position({"state": "play", "songid": "2", "elapsed": "1.0"})
    client._track_position({"state": "play", "songid": "2", "elapsed": "3.0"})
    assert not client.is_stream_stalled()


async def test_frozen_radio_is_reloaded_then_stopped_after_max_restarts(client, mopidy, eska):
    await client.play_station(*eska)
    reloads = []

    async def resolve(key):
        reloads.append(key)
        return eska

    for _ in range(client.config.stream_stall_max_restarts):
        client._position_stalled_since = time.monotonic() - 100
        await client._recover_stalled_stream(resolve)
    assert reloads == ["eska", "eska"] and mopidy.state == "play"
    client._position_stalled_since = time.monotonic() - 100
    await client._recover_stalled_stream(resolve)
    assert mopidy.state == "stop"


async def test_frozen_stream_waits_while_ducked(client, eska):
    client._is_ducked = True
    client._position_stalled_since = time.monotonic() - 100
    called = []

    async def resolve(key):
        called.append(key)

    await client._recover_stalled_stream(resolve)
    assert called == [] and client.is_stream_stalled()


async def test_watcher_reports_external_stop_to_mqtt(client, mopidy, eska, mpd_config):
    mpd_config.state_poll_interval = 0.01
    await client.play_station(*eska)
    client._seen_play_since_start = True
    client._playback_started_at = time.monotonic() - 600
    client.events.clear()
    watcher = asyncio.create_task(client.watch_external_changes(resolve_eska))
    await asyncio.sleep(0.03)
    mopidy.state = "stop"  # e.g. the stream ended or another client stopped it
    await asyncio.sleep(0.05)
    watcher.cancel()
    assert {"state": "OFF"} in client.events


async def test_watcher_ignores_stop_while_a_new_stream_buffers(client, mopidy, eska, mpd_config):
    # 26.09.2026: "stop" 0.0 s after play flickered HA and flagged a live stream as dead
    mpd_config.state_poll_interval = 0.01
    await client.play_station(*eska)
    client.events.clear()
    mopidy.state = "stop"
    watcher = asyncio.create_task(client.watch_external_changes(resolve_eska))
    await asyncio.sleep(0.05)
    watcher.cancel()
    assert client.events == []
    assert client.radio_state.get_cached_url("eska") == eska[0]


# --- resume ------------------------------------------------------------------

async def test_resume_on_empty_queue_reloads_the_last_station(client, mopidy, eska):
    # 21.09.2026: after power cuts a bare play() on Mopidy's empty queue went silent
    await client.resume(resolve_eska)
    assert mopidy.queue == [eska[0]] and mopidy.state == "play"


async def test_resume_reloads_when_the_cached_url_was_dropped(client, mopidy, eska):
    mopidy.queue = ["http://old/dead.mp3"]
    client.radio_state.forget_url("eska")
    await client.resume(resolve_eska)
    assert mopidy.queue == [eska[0]]


async def test_resume_reloads_a_frozen_stream(client, mopidy, eska):
    await client.play_station(*eska)
    client._position_stalled_since = time.monotonic() - 5
    mopidy.calls.clear()
    await client.resume(resolve_eska)
    assert "clear" in mopidy.names()


async def test_resume_with_a_healthy_queue_just_plays(client, mopidy, eska):
    mopidy.queue = [eska[0]]
    client._is_playing = False
    await client.resume(resolve_eska)
    assert "clear" not in mopidy.names() and mopidy.state == "play"


async def test_resume_switches_a_music_queue_back_to_the_radio(client, mopidy, eska):
    await client.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    await client.stop()
    await client.resume(resolve_eska)
    assert mopidy.queue == [eska[0]] and client.source == "radio"


# --- music sharing the queue -------------------------------------------------

async def test_play_music_queues_all_tracks_and_leaves_station_alone(client, mopidy):
    await client.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    assert mopidy.queue == ["youtube:video:a", "youtube:video:b"]
    assert client.source == "music"
    assert client.events[-1] == {"state": "ON", "volume": 50}  # no "station": HA select


async def test_load_station_marks_the_queue_as_radio(client):
    await client.play_music(["youtube:video:a"], "Song")
    await client.load_station("http://x/s.mp3", "Radio Zet", key="zet")
    assert client.source == "radio"


async def test_frozen_music_skips_to_the_next_track(client, mopidy, eska):
    await client.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    called = []

    async def resolve(key):
        called.append(key)

    client._position_stalled_since = time.monotonic() - 100
    await client._recover_stalled_stream(resolve)
    assert called == [] and mopidy.state == "play" and mopidy.song == 1
    assert not client._is_ducked  # not a turn: nobody would unduck it


async def test_frozen_last_music_track_stops(client, mopidy, eska):
    await client.play_music(["youtube:video:a"], "Song")
    called = []

    async def resolve(key):
        called.append(key)

    client._position_stalled_since = time.monotonic() - 100
    await client._recover_stalled_stream(resolve)
    assert called == [] and mopidy.state == "stop"


async def test_dead_music_track_keeps_the_station_url(client, eska):
    await client.play_music(["youtube:video:a"], "Song")
    client._playback_started_at = time.monotonic() - 2
    client._report_silent_stop()
    assert client.radio_state.get_cached_url("eska") == eska[0]


async def test_next_track_plays_the_next_position_and_names_it(client, mopidy):
    await client.play_music([f"youtube:video:{i}" for i in range(3)], "Song")
    result, track, remaining = await client.next_track()
    assert result == "ok" and track["title"] == "T1" and remaining == 1
    assert ("play", 1) in mopidy.calls


async def test_next_track_names_the_target_even_when_status_lags(client, mopidy, monkeypatch):
    # Mopidy switches only once the track resolves; status/currentsong lag behind
    await client.play_music([f"youtube:video:{i}" for i in range(3)], "Song")
    monkeypatch.setattr(mopidy, "play", lambda pos=None: mopidy._rec("play", pos))
    result, track, remaining = await client.next_track()
    assert track["title"] == "T1" and remaining == 1


async def test_next_track_on_the_last_track(client, mopidy):
    await client.play_music(["youtube:video:a"], "Song")
    assert await client.next_track() == ("no_next", None, 0)


async def test_next_track_after_a_failed_track_uses_the_last_position(client, mopidy):
    # Mopidy drops "song" from status once playback stops
    await client.play_music([f"youtube:video:{i}" for i in range(15)], "Song")
    mopidy.song = 3
    client._track_position(mopidy.status())
    mopidy.state = "stop"
    result, track, remaining = await client.next_track()
    assert result == "ok" and ("play", 4) in mopidy.calls and remaining == 10
    assert client.events[-1]["state"] == "ON"


async def test_next_track_from_a_stop_starts_ducked_mid_turn(client, mopidy):
    # the tool runs before the spoken reply; music must not start at full volume under it
    await client.play_music([f"youtube:video:{i}" for i in range(3)], "Song")
    await client.stop()
    mopidy.calls.clear()
    await client.next_track(start_ducked=True)
    setvols = [c for c in mopidy.calls if c[0] == "setvol"]
    play_at = mopidy.calls.index(("play", 1))
    assert setvols and mopidy.calls.index(setvols[0]) < play_at
    assert setvols[0][1] == client._map_volume_to_curve(client.config.volume_duck_percentage)
    assert client._is_ducked


async def test_next_track_while_playing_keeps_the_current_level(client, mopidy):
    await client.play_music([f"youtube:video:{i}" for i in range(3)], "Song")
    mopidy.calls.clear()
    await client.next_track(start_ducked=True)
    assert not any(c[0] == "setvol" for c in mopidy.calls)


async def test_source_is_recovered_from_the_queue_after_an_app_restart(client, mopidy):
    mopidy.queue = ["youtube:video:a", "youtube:video:b"]
    await client.initialize_state()
    assert client.source == "music"


async def test_radio_queue_after_restart_stays_radio(client, mopidy):
    mopidy.queue = ["http://ic1.smcdn.pl/2170-1.mp3"]
    await client.initialize_state()
    assert client.source == "radio"


async def test_power_on_with_music_and_no_known_station_resumes_the_music(client, mopidy):
    await client.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    await client.stop()
    await client.resume(resolve_eska)
    assert mopidy.state == "play" and mopidy.queue[0] == "youtube:video:a"


async def test_next_track_refused_for_the_radio(client, eska):
    await client.play_station(*eska)
    assert await client.next_track() == ("no_next", None, 0)


async def test_next_track_mpd_error(client, mopidy):
    await client.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    mopidy.fail_next["play"] = mpd_module.MPDError("x")
    assert await client.next_track() == ("error", None, 0)


@pytest.mark.parametrize("status", [
    {}, {"song": "x", "playlistlength": "3"}, {"playlistlength": "3"},
    {"song": "0", "playlistlength": "1"}, {"song": "5", "playlistlength": "3"},
    {"song": "0", "playlistlength": "junk"},
])
def test_remaining_tracks_never_negative_or_crashing(client, status):
    assert client.remaining_tracks(status) == 0

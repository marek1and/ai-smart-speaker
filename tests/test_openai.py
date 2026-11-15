"""OpenAI realtime manager: a typed turn must not be cancelled by the cancel
start_new_turn queued for the send task."""
import asyncio

import numpy as np

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


def manager():
    mgr = OpenAIRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())
    mgr._session_active = True
    mgr._ws = object()
    mgr.sent = []

    async def ws_send(event):
        mgr.sent.append(event)

    mgr._ws_send = ws_send
    return mgr


def types(mgr):
    return [event["type"] for event in mgr.sent]


async def test_a_finished_reply_is_not_truncated_on_the_next_turn():
    # 14.11.2026: every turn truncated the previous reply at its full length, and
    # the first turn of a new session got "Item with item_id not found"
    mgr = manager()
    mgr._current_response_item_id, mgr._audio_bytes_received = "item_1", 48000
    await mgr._cancel_and_clear(cancel_response=False)
    assert types(mgr) == ["input_audio_buffer.clear"]


async def test_an_interrupted_reply_is_cancelled_and_truncated_once():
    mgr = manager()
    mgr._current_response_item_id, mgr._audio_bytes_received = "item_1", 48000
    await mgr._cancel_and_clear(cancel_response=True)
    assert types(mgr) == ["response.cancel", "conversation.item.truncate", "input_audio_buffer.clear"]
    assert mgr.sent[1]["item_id"] == "item_1" and mgr.sent[1]["audio_end_ms"] == 1000
    mgr.sent.clear()
    await mgr._cancel_and_clear(cancel_response=True)
    assert "conversation.item.truncate" not in types(mgr)


async def test_user_speech_is_transcribed_in_the_configured_language(monkeypatch):
    mgr = manager()
    monkeypatch.setattr(mgr.live_cfg, "lang", "pl-PL")
    await mgr._configure_session()
    transcription = mgr.sent[0]["session"]["audio"]["input"]["transcription"]
    assert transcription == {"model": mgr.live_cfg.openai_transcription_model, "language": "pl"}


async def test_transcripts_of_the_two_responses_around_a_tool_call_are_separated():
    mgr = manager()
    await mgr._process_event({"type": "response.created"})
    await mgr._process_event({"type": "response.output_audio_transcript.delta", "delta": "Sprawdzam."})
    await mgr._process_event({"type": "response.created"})
    await mgr._process_event({"type": "response.output_audio_transcript.delta", "delta": "Jest 12:47."})
    assert mgr.ai_transcript == "Sprawdzam. Jest 12:47."


async def test_reply_is_still_awaited_after_the_response_with_the_tool_call(monkeypatch):
    # the answer comes in a second response; until then room audio must not
    # reach the API, or local VAD commits it on top of the answer
    mgr = manager()
    mgr._waiting_for_turn_complete = True

    async def tool(item):
        pass

    mgr._handle_tool_call = tool
    await mgr._process_event({"type": "response.done", "response": {
        "status": "completed", "output": [{"type": "function_call", "name": "get_current_time"}]}})
    assert mgr.awaiting_reply
    mgr._frames_sent = mgr._min_frames_for_turn_complete
    await mgr._process_event({"type": "response.done", "response": {"status": "completed", "output": []}})
    assert not mgr.awaiting_reply


def test_frames_resampled_one_by_one_match_the_whole_signal():
    # each 32 ms frame resampled alone was zero-padded at both ends (a click
    # at every boundary); the stream now matches the signal resampled at once,
    # delayed by the context kept between frames
    from scipy.signal import resample_poly

    mgr = OpenAIRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())
    t = np.arange(512 * 8) / 16000
    signal = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    streamed = np.concatenate([
        np.frombuffer(mgr._resample_to_24k(signal[i:i + 512].tobytes()), dtype=np.int16)
        for i in range(0, len(signal), 512)
    ]).astype(float)
    assert len(streamed) == len(signal) * 3 // 2
    delay = mgr._resample_pad * 3 // 2
    whole = resample_poly(signal.astype(float), up=3, down=2)
    # past the first frame (the history starts as zeros)
    assert np.max(np.abs(streamed[768 + delay:] - whole[768:len(streamed) - delay])) < 2

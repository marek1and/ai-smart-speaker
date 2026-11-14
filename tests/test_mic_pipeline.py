"""MicPipeline: frames off the capture queue, the preroll and wake word clips."""
import asyncio

import numpy as np
import pytest

import mic_pipeline
from config import get_config
from mic_pipeline import MicPipeline
from state import WakeWordResult


@pytest.fixture
def saved(monkeypatch):
    clips = []

    def save_clip(frames, rate, kind, score, verifier_score, channels):
        clips.append((kind, len(frames), channels))

    monkeypatch.setattr(mic_pipeline, "save_clip", save_clip)
    return clips


@pytest.fixture
def mic(monkeypatch):
    wake = get_config().wake_word
    monkeypatch.setattr(wake, "save_trigger_clips", True)
    monkeypatch.setattr(wake, "save_candidate_clips", True)
    return MicPipeline(get_config())


def stereo(mic_level=100, ref_level=0, n=512):
    frame = np.zeros((n, 2), dtype=np.int16)
    frame[:, 0], frame[:, 1] = mic_level, ref_level
    return frame


async def settle():
    await asyncio.sleep(0.05)  # save_clip runs in the default executor


async def test_stereo_capture_yields_the_microphone_channel(mic):
    mic.queue.put(stereo(mic_level=7, ref_level=9))
    mono, mono_bytes = await mic.next_frame()
    assert mono.shape == (512,) and set(mono) == {7}
    assert mono_bytes == mono.tobytes()


async def test_mono_capture_and_empty_frames(mic):
    mic.queue.put(np.full((512, 1), 3, dtype=np.int16))
    mono, _ = await mic.next_frame()
    assert mono.shape == (512,)
    mic.queue.put(np.zeros((0, 2), dtype=np.int16))
    assert await mic.next_frame() is None


async def test_no_frame_in_time_returns_none(mic, monkeypatch):
    def empty(timeout):
        raise mic_pipeline.queue.Empty

    monkeypatch.setattr(mic.queue, "get", empty)
    assert await mic.next_frame() is None


async def test_preroll_is_handed_over_once_and_is_bounded(mic):
    limit = mic._preroll.maxlen
    for i in range(limit + 3):
        mic.queue.put(np.full(512, i, dtype=np.int16))
        await mic.next_frame()
    assert len(mic.preroll()) == limit  # reading for the verifier keeps it
    frames = mic.take_preroll()
    assert len(frames) == limit
    assert frames[-1] == np.full(512, limit + 2, dtype=np.int16).tobytes()
    assert mic.take_preroll() == []


async def test_trigger_clip_keeps_both_channels(mic, saved):
    mic.queue.put(stereo())
    await mic.next_frame()
    mic.save_trigger_clip(WakeWordResult(True, 0.9, 0.9))
    await settle()
    assert saved == [("trigger", 1, 2)]


async def test_no_clips_when_saving_is_off(monkeypatch, saved):
    wake = get_config().wake_word
    monkeypatch.setattr(wake, "save_trigger_clips", False)
    monkeypatch.setattr(wake, "save_candidate_clips", False)
    mic = MicPipeline(get_config())
    mic.queue.put(stereo())
    await mic.next_frame()
    mic.save_trigger_clip(WakeWordResult(True, 0.9, 0.9))
    mic.maybe_save_candidate_clip(WakeWordResult(False, 0.99, 0.99), 0.0)
    await settle()
    assert saved == []


async def test_candidate_clips_are_rate_limited_and_skip_the_tail_of_a_trigger(mic, saved, monkeypatch):
    wake = get_config().wake_word
    now = 1000.0
    monkeypatch.setattr(mic_pipeline.time, "time", lambda: now)
    mic.queue.put(stereo())
    await mic.next_frame()
    hot = WakeWordResult(False, wake.candidate_threshold + 0.01, 0.9)
    cold = WakeWordResult(False, wake.candidate_threshold - 0.01, 0.9)
    mic.maybe_save_candidate_clip(cold, 0.0)
    mic.maybe_save_candidate_clip(hot, now)  # right after a real trigger
    mic.maybe_save_candidate_clip(hot, 0.0)
    mic.maybe_save_candidate_clip(hot, 0.0)  # same hot streak
    now += wake.cooldown_seconds + 1
    mic.maybe_save_candidate_clip(hot, 0.0)
    await settle()
    assert [kind for kind, _, _ in saved] == ["candidate", "candidate"]


async def test_aec_metric_counts_only_frames_with_a_reference_signal(mic):
    floor = get_config().metrics.aec_ref_min_rms
    for ref in (0, int(floor) + 100):
        mic.queue.put(stereo(mic_level=10, ref_level=ref))
        await mic.next_frame()
    assert len(mic._aec_ratio_window) == 1

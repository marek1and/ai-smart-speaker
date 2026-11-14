"""ReplyAudio on its own: silence held and released, voice budget, short answers."""
from dataclasses import replace

import numpy as np
import pytest

from config import get_config
from realtime.reply_audio import ReplyAudio

RATE = 24000


def tone(seconds, amplitude=0.2, freq=1000):
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16).tobytes()


def silence(seconds):
    return bytes(int(seconds * RATE) * 2)


@pytest.fixture
def cfg():
    return replace(
        get_config().live,
        max_played_pause=1.0, trailing_silence_end=3.0,
        voice_budget_per_char=0.15, voice_budget_slack=3.0, min_voice_per_char=0.045,
    )


def test_voice_is_played_and_counted(cfg):
    reply = ReplyAudio(cfg, RATE)
    chunk = reply.take(tone(0.5), transcript_chars=0)
    assert not chunk.silent and chunk.play == [tone(0.5)]
    assert reply.voice_seen and reply.voice_played == pytest.approx(0.5)


@pytest.mark.parametrize("data", [b"", b"\x01\x00", silence(0.1), tone(0.1, amplitude=0.01, freq=100)])
def test_empty_short_silent_and_rumble_chunks_carry_no_voice(cfg, data):
    # rumble below 300 Hz has RMS like quiet voice; the high-pass tells them apart
    assert ReplyAudio(cfg, RATE).is_silent(data)


def test_a_pause_is_played_before_the_voice_that_follows_cut_to_the_limit(cfg):
    reply = ReplyAudio(cfg, RATE)
    reply.take(tone(0.2), 0)
    for _ in range(3):
        assert reply.take(silence(0.8), 0).play == []
    chunk = reply.take(tone(0.2), 0)
    pause = b"".join(chunk.play[:-1])
    assert reply.seconds(len(pause)) == pytest.approx(cfg.max_played_pause)
    assert chunk.play[-1] == tone(0.2) and reply.held == []


def test_trailing_silence_is_dropped_and_reported(cfg):
    reply = ReplyAudio(cfg, RATE)
    reply.take(tone(0.2), 0)
    reply.take(silence(1.5), 0)
    assert reply.drop_held() == pytest.approx(1.5)
    assert reply.held == [] and reply.drop_held() == 0.0


def test_long_silence_counts_only_after_voice(cfg):
    leading = ReplyAudio(cfg, RATE)
    leading.take(silence(5.0), 0)
    assert not leading.trailing_silence_is_long()
    trailing = ReplyAudio(cfg, RATE)
    trailing.take(tone(0.2), 0)
    trailing.take(silence(2.0), 0)
    assert not trailing.trailing_silence_is_long()
    trailing.take(silence(1.5), 0)
    assert trailing.trailing_silence_is_long()


def test_voice_past_the_transcript_budget_is_not_played(cfg):
    reply = ReplyAudio(cfg, RATE)
    chars = 10  # budget: 3.0 + 10 * 0.15 = 4.5 s
    assert reply.take(tone(4.0), chars).play
    over = reply.take(tone(1.0), chars)
    assert over.over_budget and over.play == []
    assert reply.voice_played == pytest.approx(4.0) and reply.voice_dropped == pytest.approx(1.0)
    # no transcript yet: nothing to judge by
    assert not ReplyAudio(cfg, RATE).take(tone(10.0), 0).over_budget


@pytest.mark.parametrize("voice,incomplete", [(1.0, True), (2.0, False)])
def test_too_little_voice_for_the_transcript_is_incomplete(cfg, voice, incomplete):
    reply = ReplyAudio(cfg, RATE)
    reply.take(tone(voice), 0)
    assert reply.is_incomplete(transcript_chars=35) is incomplete  # needs 35 * 0.045 = 1.6 s
    assert not reply.is_incomplete(transcript_chars=0)

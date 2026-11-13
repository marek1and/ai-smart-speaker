"""End of speech in HybridVAD: a pause after the first word must not end the turn."""
import pytest

from audio.vad import BaseVAD, HybridVAD
from config import VADConfig


class Scripted(BaseVAD):
    threshold = 0.5

    def __init__(self, verdicts):
        self._verdicts = iter(verdicts)

    def is_speech(self, audio):
        return next(self._verdicts)


def run(verdicts, **cfg):
    config = VADConfig(speech_frames_required=3, silence_frames_required=15, **cfg)
    vad = HybridVAD(Scripted(verdicts), config)
    events = []
    for i in range(len(verdicts)):
        started, ended = vad.process_frame(None)
        events += [("start", i)] * started + [("end", i)] * ended
    return events


SHORT = dict(short_utterance_frames=20, short_utterance_silence_frames=30)


def test_pause_after_a_short_first_word_does_not_end_the_turn():
    # "gaś" (10 frames), a 0.6 s pause, then the rest of the command
    verdicts = [True] * 10 + [False] * 20 + [True] * 30 + [False] * 15
    assert run(verdicts, **SHORT) == [("start", 2), ("end", 74)]


def test_short_utterance_ends_after_the_longer_silence():
    verdicts = [True] * 10 + [False] * 30
    assert run(verdicts, **SHORT) == [("start", 2), ("end", 39)]


def test_long_utterance_keeps_the_normal_silence():
    verdicts = [True] * 25 + [False] * 15
    assert run(verdicts, **SHORT) == [("start", 2), ("end", 39)]


def test_disabled_by_default():
    verdicts = [True] * 10 + [False] * 15
    assert run(verdicts) == [("start", 2), ("end", 24)]


@pytest.mark.parametrize("silence", [5, 14])
def test_speech_frames_count_across_pauses(silence):
    # two short bursts add up to a long utterance: the normal silence ends it
    verdicts = [True] * 12 + [False] * silence + [True] * 12 + [False] * 15
    assert run(verdicts, **SHORT)[-1] == ("end", 12 + silence + 12 + 14)

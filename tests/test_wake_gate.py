"""WakeGate: warmup after a reset and the cooldown between triggers. The
strict/relaxed decision is covered in test_orchestrator (cached play state)."""
import numpy as np

import wake_gate
from config import get_config
from wake_gate import WakeGate

FRAME = np.zeros(512, dtype=np.int16)


class Detector:
    last_verifier_score = 0.7
    last_window_rms = 100.0

    def __init__(self):
        self.calls = []

    def process(self, mono, relaxed_mode=False):
        return 0.9, 0.9, 0.5, True

    def reset(self):
        self.calls.append("reset")

    def set_cooldown(self):
        self.calls.append("cooldown")


class MPD:
    def is_playing_cached(self):
        return False

    def is_stream_stalled(self):
        return False


def make(responding=False):
    cfg = get_config()
    gate = WakeGate(cfg.wake_word, cfg.audio, MPD(), responding=lambda: responding)
    gate.detector = Detector()
    return gate


async def test_triggers_during_the_warmup_are_swallowed():
    gate = make()
    gate.reset(short_warmup=True)
    needed = gate.warmup_frames_needed
    assert needed > 0
    results = [await gate.detect(FRAME) for _ in range(needed + 1)]
    assert not any(r.triggered for r in results[:needed])
    assert results[needed].triggered and results[needed].verifier_score == 0.7


def test_a_full_reset_warms_up_longer_and_can_arm_the_detector_cooldown():
    gate = make()
    gate.reset(short_warmup=True)
    short = gate.warmup_frames_needed
    gate.reset(cooldown=True)
    assert gate.warmup_frames_needed > short
    assert gate.detector.calls == ["reset", "reset", "cooldown"]


def test_a_second_trigger_within_the_cooldown_is_the_same_wake_word(monkeypatch):
    gate = make()
    now = 1000.0
    monkeypatch.setattr(wake_gate.time, "time", lambda: now)
    assert gate.accept_trigger()
    now += wake_gate.TRIGGER_COOLDOWN / 2
    assert not gate.accept_trigger()
    now += wake_gate.TRIGGER_COOLDOWN
    assert gate.accept_trigger()


async def test_gates_relax_while_the_speaker_itself_talks(monkeypatch):
    monkeypatch.setattr(get_config().wake_word, "relaxed_verifier_threshold", 0.3)
    assert (await make(responding=True).detect(FRAME)).relaxed_mode is True
    assert (await make(responding=False).detect(FRAME)).relaxed_mode is False

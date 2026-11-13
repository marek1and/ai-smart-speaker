import os
import wave
from types import SimpleNamespace

import pytest

import audio.recorder as rec
from audio.recorder import SessionRecorder


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SessionRecorder(16000, 24000)


def wavs():
    return sorted(os.listdir("recordings"))


def test_model_audio_goes_to_its_own_file_at_its_rate(recorder):
    recorder.start(model_score=1.0)
    recorder.write(b"\x01\x00" * 160)
    recorder.write_model(b"\x02\x00" * 2400)
    recorder.write_model(b"\x03\x00" * 2400)
    recorder.close()
    mic, model = wavs()
    assert model == mic[: -len(".wav")] + "_model.wav"
    with wave.open(os.path.join("recordings", model)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4800


def test_no_model_file_without_model_audio(recorder):
    recorder.start()
    recorder.write(b"\x01\x00" * 160)
    recorder.close()
    assert len(wavs()) == 1


def test_model_audio_outside_a_session_is_ignored(recorder):
    recorder.write_model(b"\x02\x00" * 2400)
    recorder.start()
    recorder.close()
    recorder.write_model(b"\x02\x00" * 2400)
    assert len(wavs()) == 1


def test_false_trigger_renames_both_files(recorder):
    recorder.start()
    recorder.write_model(b"\x02\x00" * 2400)
    recorder.close_as_false_trigger()
    files = wavs()
    assert len(files) == 2 and all(f.startswith("false_wake_") for f in files)


def test_without_a_model_rate_nothing_is_recorded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recorder = SessionRecorder(16000)
    recorder.start()
    recorder.write_model(b"\x02\x00" * 2400)
    recorder.close()
    assert len(wavs()) == 1


def test_start_closes_a_recording_left_open(recorder, monkeypatch):
    # after an API error nothing closed the recorder: the next session's model
    # audio went on into the old file
    stamps = iter(["20261113_100000", "20261113_100005"])
    now = SimpleNamespace(strftime=lambda fmt: next(stamps))
    monkeypatch.setattr(rec, "datetime", SimpleNamespace(now=lambda: now))
    recorder.start()
    recorder.write_model(b"\x02\x00" * 2400)
    recorder.start()
    recorder.write_model(b"\x03\x00" * 1200)
    recorder.close()
    assert wavs() == [
        "session_20261113_100000.wav", "session_20261113_100000_model.wav",
        "session_20261113_100005.wav", "session_20261113_100005_model.wav",
    ]
    with wave.open(os.path.join("recordings", "session_20261113_100000_model.wav")) as w:
        assert w.getnframes() == 2400
    with wave.open(os.path.join("recordings", "session_20261113_100005_model.wav")) as w:
        assert w.getnframes() == 1200

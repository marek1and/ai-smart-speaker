"""MQTT bridge, wake word gating and config parsing.
Past bugs: a burst of replayed power/station commands thrashed MPD after a
broker reconnect; the relaxed VAD gate leaked into IDLE; config typos failed
with a bare TypeError."""
import asyncio
import json
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

import audio.wake_word as ww
from config import AppConfig, AudioConfig, WakeWordConfig, get_config
from mqtt_bridge.bridge import MQTTBridge

P = "home/living-room/speaker"


# --- MQTT bridge -------------------------------------------------------------------

class FakeMPD:
    def __init__(self):
        self.calls = []

    async def resume(self, resolve):
        self.calls.append("resume")
        await asyncio.sleep(0.05)

    async def stop(self):
        self.calls.append("stop")

    async def set_volume(self, v):
        self.calls.append(("volume", v))

    async def is_playing(self):
        return False

    async def load_station(self, url, name, key=None):
        self.calls.append(("load", name))

    def get_current_station_name(self):
        return "Radio Eska"


class FakeRadio:
    async def search_station(self, name):
        await asyncio.sleep(0.01)
        return (f"http://{name}", name, name.lower())


def message(topic, payload, retain=False):
    return SimpleNamespace(topic=topic, payload=payload.encode(), retain=retain)


class Client:
    def __init__(self, msgs):
        self._msgs = msgs
        self.published = []

    @property
    def messages(self):
        async def gen():
            for m in self._msgs:
                yield m
        return gen()

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


@pytest.fixture
def bridge():
    b = MQTTBridge(get_config().mqtt, FakeMPD(), FakeRadio())
    b.asked = []
    b.set_text_handler(lambda t: b.asked.append(t) or True)
    return b


async def run_commands(bridge, msgs):
    await bridge._command_loop(Client(msgs), f"{P}/radio", f"{P}/assistant")
    for t in (bridge._power_task, bridge._station_task):
        if t:
            await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0.02)


async def test_burst_of_power_commands_applies_only_the_last(bridge):
    await run_commands(bridge, [message(f"{P}/radio/command/power", p) for p in ("ON", "OFF", "ON", "OFF")])
    assert bridge._mpd.calls == ["stop"]


async def test_burst_of_station_commands_loads_only_the_last(bridge):
    await run_commands(bridge, [message(f"{P}/radio/command/station", s) for s in ("Radio Zet", "RMF FM")])
    assert bridge._mpd.calls == [("load", "RMF FM")]


async def test_invalid_volume_payload_is_ignored(bridge):
    await run_commands(bridge, [message(f"{P}/radio/command/volume", "loud"),
                                message(f"{P}/radio/command/volume", "30")])
    assert bridge._mpd.calls == [("volume", 30)]


async def test_text_requests_retained_and_empty_are_ignored(bridge):
    await run_commands(bridge, [message(f"{P}/assistant/ask", "stare", retain=True),
                                message(f"{P}/assistant/ask", "  "),
                                message(f"{P}/assistant/ask", "hej")])
    assert bridge.asked == ["hej"]


async def test_busy_speaker_answers_with_an_error_turn(bridge):
    bridge.set_text_handler(lambda t: False)
    await run_commands(bridge, [message(f"{P}/assistant/ask", "hej")])
    assert bridge._publish_queue.get_nowait()["turn"]["error"] == "busy"


async def test_turns_are_published_as_utf8_json_without_retain(bridge):
    client = Client([])
    bridge.publish_turn({"source": "text", "request": "zgaś", "reply": "Gaszę światło."})
    bridge.on_state_change({"state": "ON", "volume": 40})
    task = asyncio.create_task(bridge._publish_loop(client, f"{P}/radio", f"{P}/assistant"))
    await asyncio.sleep(0.02)
    task.cancel()
    topic, payload, retain = client.published[0]
    assert topic == f"{P}/assistant/reply" and not retain and "Gaszę" in payload
    assert json.loads(payload)["reply"] == "Gaszę światło."
    assert (f"{P}/radio/state", "ON", True) in client.published
    assert (f"{P}/radio/volume", "40", True) in client.published


# --- wake word gating -----------------------------------------------------------------

class FakeVad:
    def __init__(self):
        self.prediction_buffer = deque([0.9], maxlen=30)
        self.fed = 0
        self.resets = 0

    def __call__(self, frame):
        self.fed += 1

    def reset_states(self):
        self.resets += 1


class FakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.vad_threshold = None
        self.thresholds_seen = []
        self.vad = FakeVad()
        self.model_inputs = {"krystyna": 16}
        self.preprocessor = SimpleNamespace(get_features=lambda n: np.zeros((1, n, 96)))

    def predict(self, frame):
        self.thresholds_seen.append(self.vad_threshold)
        return {"krystyna": self.scores.pop(0)}

    def reset(self):
        pass


class FakeVerifier:
    classes_ = [0, 1]

    def __init__(self, p):
        self.p = p

    def predict_proba(self, features):
        return [[1 - self.p, self.p]]


def detector(scores, verifier_p=None, **cfg):
    defaults = dict(threshold=0.85, vad_threshold=0.5, verifier_threshold=0.8,
                    min_activation_frames=3, cooldown_seconds=2.0,
                    relaxed_verifier_threshold=0.6, relaxed_vad_threshold=0.0,
                    relaxed_min_activation_frames=2)
    defaults.update(cfg)
    d = ww.WakeWordDetector.__new__(ww.WakeWordDetector)
    d.cfg = WakeWordConfig(**defaults)
    d.window_size = 10
    d.score_window = deque(maxlen=10)
    d._rms_window = deque(maxlen=10)
    d._prediction_key = "krystyna"
    d._last_trigger_time = 0.0
    d._consecutive_above_threshold = 0
    d._last_verifier_score = None
    d.model = FakeModel(scores)
    d.model.vad_threshold = d.cfg.vad_threshold
    d._verifier = FakeVerifier(verifier_p) if verifier_p is not None else None
    return d


FRAME = np.zeros(512, dtype=np.int16)


def run_frames(d, n, relaxed=False):
    return [d.process(FRAME, relaxed_mode=relaxed)[3] for _ in range(n)]


def test_strict_mode_needs_the_full_frame_count():
    assert run_frames(detector([0.9] * 3, verifier_p=0.9), 3) == [False, False, True]


def test_relaxed_mode_uses_its_own_frame_count_and_verifier_threshold():
    d = detector([0.9] * 2, verifier_p=0.7)
    assert run_frames(d, 2, relaxed=True) == [False, True]
    d = detector([0.9] * 3, verifier_p=0.7)
    assert run_frames(d, 3) == [False, False, False]  # 0.7 < strict 0.8


def test_relaxed_vad_gate_is_restored_after_every_frame():
    d = detector([0.1, 0.1, 0.1])
    d.process(FRAME, relaxed_mode=True)
    d.process(FRAME)
    assert d.model.thresholds_seen == [0.0, 0.5]
    assert d.model.vad_threshold == 0.5


def test_gate_is_restored_even_when_predict_fails():
    d = detector([])
    with pytest.raises(IndexError):
        d.process(FRAME, relaxed_mode=True)
    assert d.model.vad_threshold == 0.5


def test_vad_is_kept_warm_while_its_gate_is_off():
    d = detector([0.1, 0.1])
    d.process(FRAME, relaxed_mode=True)
    d.process(FRAME)
    assert d.model.vad.fed == 1


def test_verifier_rejection_resets_the_frame_count():
    d = detector([0.9, 0.9, 0.9, 0.9], verifier_p=0.9)
    d._verifier.p = 0.9
    run_frames(d, 2)
    d._verifier.p = 0.1
    assert run_frames(d, 1) == [False]
    d._verifier.p = 0.9
    assert d._consecutive_above_threshold == 0 and run_frames(d, 1) == [False]


def test_cooldown_blocks_a_second_trigger():
    d = detector([0.9] * 6, min_activation_frames=1)
    assert run_frames(d, 2) == [True, False]


def test_reset_clears_stale_vad_scores():
    d = detector([])
    d.reset()
    assert len(d.model.vad.prediction_buffer) == 0 and d.model.vad.resets == 1


def test_verifier_with_classes_in_the_wrong_order_is_refused(tmp_path):
    import pickle
    path = tmp_path / "v.pkl"
    path.write_bytes(pickle.dumps(SimpleNamespace(classes_=[1, 0])))
    d = ww.WakeWordDetector.__new__(ww.WakeWordDetector)
    d.cfg = WakeWordConfig(verifier_path=str(path))
    assert d._load_verifier() is None


# --- config ------------------------------------------------------------------------------

def test_unknown_config_key_names_the_section(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("mpd:\n  hots: localhost\n")
    with pytest.raises(ValueError, match="mpd.*hots"):
        AppConfig.from_yaml(str(path))


def test_prompt_placeholders_get_station_and_channel_names():
    prompt = get_config().live.system_instruction
    assert "Radio Eska" in prompt and "Polsat" in prompt and "{" not in prompt


def test_example_configs_load():
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "application"
    for name in ("config.example.ha.yml", "config.example.openhab.yml"):
        cfg = AppConfig.from_yaml(str(app / name))
        assert cfg.music.enabled is False and cfg.mqtt.topic_prefix

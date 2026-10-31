"""HA REST client and the HA tools. Past bugs: a lost packet killed a voice
command (no retry); a retried ReadTimeout repeated a TV power-on; the model
confirmed actions HA never executed; "turn off the light in the living room"
on an already-off lamp was turned into "turn on"."""
import json

import pytest
import requests

import homeassistant.client as ha_module
from config import get_config
from functions import definitions as d


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._body


@pytest.fixture
def ha(monkeypatch):
    """HA client whose requests go to a scripted list of outcomes."""
    client = ha_module.HomeAssistantClient(get_config().home_assistant)
    script = []
    sent = []

    def request(method, url, **kwargs):
        sent.append((method, url, kwargs.get("json")))
        outcome = script.pop(0) if script else Resp(200, {})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(ha_module.requests, "request", request)
    monkeypatch.setattr(ha_module.time, "sleep", lambda s: None)
    client.script, client.sent = script, sent
    return client


# --- transport ---------------------------------------------------------------

def test_read_is_retried_after_a_timeout(ha):
    ha.script += [requests.exceptions.Timeout(), Resp(200, {"state": "on"})]
    assert ha.get_entity_state("light.x") == "on"
    assert len(ha.sent) == 2


def test_write_is_retried_when_it_never_reached_ha(ha):
    ha.script += [requests.exceptions.ConnectionError(), Resp(200, {})]
    assert ha.call_service("light", "turn_on", "light.x") is True
    assert len(ha.sent) == 2


def test_write_is_not_repeated_after_a_read_timeout(ha):
    # HA got the command and is still executing it (TV power-on)
    ha.script += [requests.exceptions.ReadTimeout(), Resp(200, {})]
    assert ha.call_service("media_player", "turn_on", "media_player.tv") is False
    assert len(ha.sent) == 1


def test_http_errors_are_not_retried(ha):
    ha.script += [Resp(500)]
    assert ha.get_entity_state("light.x") is None
    assert len(ha.sent) == 1


def test_area_lookup_failure_is_none_not_empty(ha):
    ha.script += [requests.exceptions.ConnectionError(), requests.exceptions.ConnectionError()]
    assert ha.get_area_states("light.x") is None
    ha.script += [Resp(200, text="not json")]
    assert ha.get_area_states("light.x") is None


# --- state mapping -------------------------------------------------------------

@pytest.mark.parametrize("entity,state,service,extra", [
    ("light.x", "ON", "light/turn_on", {}),
    ("light.x", "off", "light/turn_off", {}),
    ("light.x", "0", "light/turn_off", {}),
    ("light.x", "40", "light/turn_on", {"brightness_pct": 40}),
    ("light.x", "0,100,100", "light/turn_on", {"hs_color": [0.0, 100.0], "brightness_pct": 100}),
    ("switch.x", "ON", "switch/turn_on", {}),
    ("fan.x", "OFF", "fan/turn_off", {}),
    ("cover.x", "30", "cover/set_cover_position", {"position": 30}),
    ("media_player.tv", "MUTE", "media_player/volume_mute", {"is_volume_muted": True}),
    ("media_player.tv", "25", "media_player/volume_set", {"volume_level": 0.25}),
])
def test_state_strings_map_to_services(ha, entity, state, service, extra):
    assert ha.set_entity_state(entity, state) is True
    method, url, payload = ha.sent[-1]
    assert url.endswith(f"/services/{service}")
    assert payload == {"entity_id": entity, **extra}


@pytest.mark.parametrize("entity,state", [("light.x", "bright"), ("cover.x", "open"), ("sensor.t", "ON")])
def test_unmappable_state_sends_nothing(ha, entity, state):
    assert ha.set_entity_state(entity, state) is False
    assert ha.sent == []


# --- tools ----------------------------------------------------------------------

class FakeHA:
    def __init__(self, states=None, area=None, fail=()):
        self.states = states or {}
        self.area = area
        self.fail = set(fail)
        self.set_calls = []

    def get_entity_state(self, eid):
        return self.states.get(eid)

    def get_area_states(self, eid):
        return self.area

    def set_entity_state(self, eid, state):
        self.set_calls.append((eid, state))
        return eid not in self.fail


@pytest.fixture
def fake_ha(monkeypatch):
    def make(**kw):
        f = FakeHA(**kw)
        monkeypatch.setattr(d, "ha_client", f)
        return f
    return make


async def test_set_reports_error_when_ha_did_not_execute(fake_ha):
    fake_ha(states={"light.x": "off"}, fail={"light.x"})
    result = await d.set_ha_entity_state("light.x", "ON")
    assert result["status"] == "error"


async def test_set_already_on_sends_nothing(fake_ha):
    f = fake_ha(states={"light.x": "on"})
    result = await d.set_ha_entity_state("light.x", "ON")
    assert result["status"] == "no_change" and f.set_calls == []


async def test_off_on_an_off_lamp_points_at_lamps_still_on_in_the_room(fake_ha):
    f = fake_ha(states={"light.salon": "off"},
                area={"light.salon": "off", "light.kinkiety": "on", "light.led": "off"})
    result = await d.set_ha_entity_state("light.salon", "OFF")
    assert result["status"] == "no_change"
    assert result["on_in_same_room"] == ["light.kinkiety"]
    assert "Never turn anything on" in result["message"]
    assert f.set_calls == []


async def test_off_without_area_info_claims_nothing_about_the_room(fake_ha):
    fake_ha(states={"light.salon": "off"}, area=None)
    result = await d.set_ha_entity_state("light.salon", "OFF")
    assert result["on_in_same_room"] == []
    assert "nothing else in this room" not in result["message"]


async def test_off_on_a_switch_never_looks_at_the_room(fake_ha):
    # turning off "the rest of the room" could cut a TV or a pump
    fake_ha(states={"switch.pump": "off"}, area={"switch.tv": "on"})
    result = await d.set_ha_entity_state("switch.pump", "OFF")
    assert result["on_in_same_room"] == []


async def test_media_player_is_not_power_guarded(fake_ha):
    f = fake_ha(states={"media_player.tv": "off"})
    await d.set_ha_entity_state("media_player.tv", "OFF")
    assert f.set_calls == [("media_player.tv", "OFF")]


async def test_group_set_reports_partial_and_error(fake_ha):
    fake_ha(fail={"light.b"})
    partial = await d.set_ha_entities_state(["light.a", "light.b"], "OFF")
    assert partial["status"] == "partial" and partial["failed"] == ["light.b"]
    fake_ha(fail={"light.a", "light.b"})
    assert (await d.set_ha_entities_state(["light.a", "light.b"], "OFF"))["status"] == "error"


async def test_set_without_entity_is_an_error_not_a_crash(fake_ha):
    fake_ha()
    assert (await d.set_ha_entity_state(None, "ON", bogus=1))["status"] == "error"
    assert (await d.set_ha_entities_state([], "ON"))["status"] == "error"

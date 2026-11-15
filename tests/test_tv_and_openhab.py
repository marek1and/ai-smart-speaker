"""watch_tv on both smart home backends, and the results of the OpenHAB tools."""
import asyncio

import pytest

from functions import definitions as d

HA_TV = "media_player.tv"


class FakeHa:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_entity_state(self, entity_id):
        return self.state

    def set_entity_state(self, entity_id, state):
        self.calls.append(("power", entity_id, state))
        self.state = "on"
        return True

    def play_channel(self, entity_id, number):
        self.calls.append(("channel", entity_id, number))
        return True


class FakeOpenHab:
    def __init__(self, power="OFF", ok=True):
        self.items = {"TV_Power": power}
        self.ok = ok
        self.calls = []

    def get_openhab_item_state(self, item):
        return self.items.get(item)

    def set_openhab_item_state(self, item, state):
        self.calls.append((item, state))
        if self.ok is True or (isinstance(self.ok, set) and item in self.ok):
            self.items[item] = state
            return True
        return False


@pytest.fixture
def tv(monkeypatch):
    """Returns a factory: tv(backend, on) installs that backend and gives its fake."""
    cfg = d.ctx.config.tv
    monkeypatch.setattr(cfg, "channels", {"Polsat": 5})
    monkeypatch.setattr(cfg, "channel_item", "TV_Channel")
    monkeypatch.setattr(cfg, "boot_wait_timeout", 0.05)
    monkeypatch.setattr(cfg, "boot_poll_interval", 0.01)
    monkeypatch.setattr(cfg, "post_boot_delay", 0.0)

    def install(backend, on, power_item=None):
        if backend == "ha":
            fake = FakeHa("on" if on else "off")
            monkeypatch.setattr(cfg, "power_item", power_item or HA_TV)
            monkeypatch.setattr(d.ctx, "ha", fake)
            monkeypatch.setattr(d.ctx, "openhab", None)
        else:
            fake = FakeOpenHab("ON" if on else "OFF")
            monkeypatch.setattr(cfg, "power_item", "TV_Power")
            monkeypatch.setattr(d.ctx, "ha", None)
            monkeypatch.setattr(d.ctx, "openhab", fake)
        return fake

    return install


POWER_ON = {"ha": ("power", HA_TV, "ON"), "openhab": ("TV_Power", "ON")}
CHANNEL = {"ha": ("channel", HA_TV, 5), "openhab": ("TV_Channel", "5")}
BACKENDS = ["ha", "openhab"]


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tv_that_is_off_is_turned_on(tv, backend):
    fake = tv(backend, on=False)
    assert await d.watch_tv() == {"status": "success", "message": "TV turned on."}
    assert fake.calls == [POWER_ON[backend]]


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tv_already_on_gets_the_misheard_hint_and_no_command(tv, backend):
    fake = tv(backend, on=True)
    assert (await d.watch_tv())["message"] == d.TV_ALREADY_ON_HINT
    assert fake.calls == []


@pytest.mark.parametrize("backend", BACKENDS)
async def test_channel_on_a_running_tv_is_sent_at_once(tv, backend):
    fake = tv(backend, on=True)
    assert (await d.watch_tv("polsat"))["status"] == "success"
    assert fake.calls == [CHANNEL[backend]]


@pytest.mark.parametrize("backend", BACKENDS)
async def test_channel_on_a_tv_that_was_off_waits_for_the_boot(tv, backend):
    fake = tv(backend, on=False)
    assert (await d.watch_tv("Polsat"))["status"] == "success"
    assert fake.calls == [POWER_ON[backend]]  # the channel follows in the background
    await asyncio.gather(*d.ctx.bg_tasks)
    assert fake.calls == [POWER_ON[backend], CHANNEL[backend]]


@pytest.mark.parametrize("backend", BACKENDS)
async def test_unknown_channel_is_a_partial_success(tv, backend):
    fake = tv(backend, on=True)
    result = await d.watch_tv("TVP 17")
    assert result["status"] == "partial_success" and "TVP 17" in result["message"]
    assert fake.calls == []


async def test_ha_power_switch_cannot_switch_channels(tv):
    # a switch.* wired to Wake-on-LAN turns the TV on, but takes no channel
    fake = tv("ha", on=False, power_item="switch.tv_wol")
    result = await d.watch_tv("Polsat")
    assert result["status"] == "partial_success"
    assert fake.calls == [("power", "switch.tv_wol", "ON")]


async def test_tv_without_a_backend_is_an_error(monkeypatch):
    monkeypatch.setattr(d.ctx, "ha", None)
    monkeypatch.setattr(d.ctx, "openhab", None)
    assert (await d.watch_tv())["status"] == "error"


# --- OpenHAB tools ------------------------------------------------------------------

async def test_openhab_set_reports_success_and_failure_as_a_status(monkeypatch):
    # a bare False became {"result": "False"}, which the model read as done
    monkeypatch.setattr(d.ctx, "openhab", FakeOpenHab(ok=True))
    assert await d.set_openhab_item_state(item_name="Light", state="ON") == {"status": "success"}
    monkeypatch.setattr(d.ctx, "openhab", FakeOpenHab(ok=False))
    result = await d.set_openhab_item_state(item="Light", state="ON")  # hallucinated keyword
    assert result["status"] == "error" and "Light" in result["message"]


async def test_openhab_set_without_an_item_or_a_backend_is_an_error(monkeypatch):
    monkeypatch.setattr(d.ctx, "openhab", FakeOpenHab())
    assert (await d.set_openhab_item_state(state="ON"))["status"] == "error"
    monkeypatch.setattr(d.ctx, "openhab", None)
    assert (await d.set_openhab_item_state(item_name="Light", state="ON"))["status"] == "error"


@pytest.mark.parametrize("ok,status,failed", [
    (True, "success", None),
    ({"A"}, "partial", ["B"]),
    (False, "error", ["A", "B"]),
])
async def test_openhab_multi_set_names_what_failed(monkeypatch, ok, status, failed):
    monkeypatch.setattr(d.ctx, "openhab", FakeOpenHab(ok=ok))
    result = await d.set_openhab_items_state(item_names=["A", "B"], state="OFF")
    assert result["status"] == status and result.get("failed") == failed
    assert result["results"] == {"A": "A" not in (failed or []), "B": "B" not in (failed or [])}


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tv_that_did_not_turn_on_is_an_error_and_gets_no_channel(tv, backend):
    # the model used to hear "TV turned on." when the backend refused it
    fake = tv(backend, on=False)
    fake.set_entity_state = fake.set_openhab_item_state = lambda *a: False
    result = await d.watch_tv("Polsat")
    assert result["status"] == "error"
    await asyncio.sleep(0.1)  # no boot watcher may send the channel later
    assert fake.calls == []


async def test_tv_power_on_that_ha_did_not_confirm_still_counts_as_sent(tv):
    # a TV power-on outlasts HA's request timeout (ReadTimeout -> None)
    fake = tv("ha", on=False)
    fake.set_entity_state = lambda *a: None
    assert await d.watch_tv() == {"status": "success", "message": "TV turned on."}

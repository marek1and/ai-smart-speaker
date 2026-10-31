"""Station URL resolution: pinned URL > fresh cache > RadioBrowser > stale cache.
Past bugs: a pinned URL dropped on a 403 probe, a dead cached URL served until
its TTL expired, RadioBrowser's dead HLS winning over a working Icecast pin."""
import time
from types import SimpleNamespace

import pytest
import requests

import radio.client as radio_module
from config import get_config
from mpd_client.state import RadioStateManager


class Resp:
    def __init__(self, status):
        self.status_code = status

    def close(self):
        pass


@pytest.fixture
def http(monkeypatch):
    """Map URL -> HTTP status (or exception) for the reachability probe."""
    table = {}
    probed = []

    def get(url, **kwargs):
        probed.append(url)
        outcome = table.get(url, 200)
        if isinstance(outcome, BaseException):
            raise outcome
        return Resp(outcome)

    monkeypatch.setattr(radio_module.requests, "get", get)
    return SimpleNamespace(table=table, probed=probed)


class FakeBrowser:
    def __init__(self):
        self.by_uuid = {}
        self.search_results = []
        self.searched = []

    async def station(self, uuid):
        return self.by_uuid.get(uuid)

    async def search(self, **kwargs):
        self.searched.append(kwargs["name"])
        return self.search_results

    async def close(self):
        pass


@pytest.fixture
def radio(tmp_path):
    rc = radio_module.RadioClient(get_config().radio)
    rc.rb = FakeBrowser()
    rc.set_state(RadioStateManager(str(tmp_path / "state.json")))
    return rc


def station(name, url):
    return SimpleNamespace(name=name, url_resolved=url)


async def test_pinned_url_wins_and_is_mirrored_into_state(radio, http):
    url, name, key = await radio.search_station("Radio Eska")
    assert (url, name, key) == ("http://pinned.invalid/eska.mp3", "Radio Eska", "eska")
    assert radio._state.get_key_by_url(url) == "eska"
    assert radio.rb.searched == []


@pytest.mark.parametrize("status", [403, 405, 500, 503])
async def test_pinned_url_survives_a_refused_probe(radio, http, status):
    http.table["http://pinned.invalid/eska.mp3"] = status
    url, _, _ = await radio.search_station("eska")
    assert url == "http://pinned.invalid/eska.mp3"


@pytest.mark.parametrize("status", [404, 410])
async def test_pinned_url_the_server_calls_gone_falls_back_to_uuid(radio, http, status):
    http.table["http://pinned.invalid/eska.mp3"] = status
    radio.rb.by_uuid["11111111-1111-1111-1111-111111111111"] = station("ESKA", "http://rb/eska")
    url, name, key = await radio.search_station("eska")
    assert (url, name, key) == ("http://rb/eska", "Radio Eska", "eska")


async def test_fresh_cache_is_used_without_asking_radiobrowser(radio, http):
    radio._state.update_station("radio x", "http://cache/x", "Radio X")
    url, name, _ = await radio.search_station("Radio X")
    assert url == "http://cache/x" and radio.rb.searched == []


async def test_dead_cached_url_is_forgotten_and_resolved_again(radio, http):
    radio._state.update_station("radio x", "http://cache/x", "Radio X")
    http.table["http://cache/x"] = 404
    radio.rb.search_results = [station("Radio X", "http://new/x")]
    url, _, _ = await radio.search_station("Radio X")
    assert url == "http://new/x"
    assert radio._state.get_cached_url("radio x") == "http://new/x"


async def test_expired_cache_is_resolved_again(radio, http):
    radio._state.update_station("radio x", "http://cache/x", "Radio X")
    radio._state.get_entry("radio x").resolved_at = time.time() - 200 * 3600
    radio.rb.search_results = [station("Radio X", "http://new/x")]
    url, _, _ = await radio.search_station("Radio X")
    assert url == "http://new/x"


async def test_search_skips_dead_candidates(radio, http):
    http.table["http://a"] = 404
    radio.rb.search_results = [station("A", "http://a"), station("B", "http://b")]
    url, name, key = await radio.search_station("Something")
    assert (url, name, key) == ("http://b", "B", "something")


async def test_stale_cache_beats_silence_when_everything_online_fails(radio, http):
    radio._state.update_station("radio x", "http://cache/x", "Radio X")
    radio._state.get_entry("radio x").resolved_at = time.time() - 200 * 3600

    async def broken(**kwargs):
        raise OSError("RadioBrowser down")

    radio.rb.search = broken
    url, _, _ = await radio.search_station("Radio X")
    assert url == "http://cache/x"


async def test_unknown_station_with_nothing_found(radio, http):
    assert await radio.search_station("Nonexistent FM") is None


@pytest.mark.parametrize("exc", [requests.exceptions.ConnectionError(), requests.exceptions.Timeout(), ValueError("icy")])
def test_probe_is_fail_open_on_transport_trouble(radio, http, exc):
    http.table["http://s"] = exc
    assert radio._check_url("http://s") is True


def test_uuid_only_pin_parsed_from_plain_string():
    pin = get_config().radio.stations["zet"]
    assert pin.uuid == "22222222-2222-2222-2222-222222222222" and pin.url == ""

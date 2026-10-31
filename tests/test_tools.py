"""AI tools and how the realtime layer hands them to the model. Past bugs: the
model announced a station whose lookup had failed (fake "success"), and said
"turning on the radio" when it was already playing."""
import asyncio
from types import SimpleNamespace

import pytest

from config import MusicConfig, get_config
from functions import definitions as d
from functions import schemas
from functions.registry import FUNCTIONS
from music import providers
from realtime.gemini import GeminiRealtimeManager


@pytest.fixture
def mpd(client, monkeypatch):
    monkeypatch.setattr(d, "_mpd_client", client)
    return client


# --- radio tools -----------------------------------------------------------------

async def test_resume_reports_already_playing(mpd, mopidy, eska):
    await mpd.play_station(*eska)
    result = await d.play_internet_radio()
    assert "action" not in result  # nothing deferred: would restart a stopped radio
    assert result["for_model"]["already_playing"] is True


async def test_resume_of_a_frozen_stream_is_not_already_playing(mpd, mopidy, eska):
    await mpd.play_station(*eska)
    mpd._position_stalled_since = 1.0
    result = await d.play_internet_radio()
    assert result["action"] == "play" and result["for_model"]["already_playing"] is False


async def test_resume_without_any_station_asks_for_one(mpd):
    assert (await d.play_internet_radio())["status"] == "error"


async def test_resume_from_music_goes_back_to_the_radio(mpd, mopidy, eska):
    await mpd.play_music(["youtube:video:a"], "Song")
    result = await d.play_internet_radio()
    assert result["action"] == "play"


async def test_unknown_station_is_an_error(mpd, monkeypatch):
    async def none(name):
        return None
    monkeypatch.setattr(d.radio_client, "search_station", none)
    assert (await d.play_internet_radio("Nonexistent FM"))["status"] == "error"


async def test_volume_change_tells_the_model_the_previous_level(mpd):
    result = await d.set_playback_volume(30)
    assert result == {"volume_percentage": 30, "for_model": {"previous_volume": 50}}


async def test_status_while_music_plays(mpd, mopidy):
    await mpd.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    status = await d.get_radio_status()
    assert status["source"] == "music" and status["remaining_tracks"] == 1
    assert status["is_radio_on_playlist"] is False


# --- music tools -----------------------------------------------------------------

class FakeProvider:
    label = "YouTube"

    def __init__(self, selection=None, exc=None):
        self.selection, self.exc, self.kinds = selection, exc, []

    async def find(self, query, kind):
        self.kinds.append(kind)
        if self.exc:
            raise self.exc
        return self.selection


async def test_play_music_logs_the_traceback(monkeypatch, caplog):
    monkeypatch.setattr(d, "music_provider", FakeProvider(exc=KeyError("uri")))
    await d.play_music("x")
    assert any(r.exc_info for r in caplog.records if "Music search" in r.getMessage())


@pytest.mark.parametrize("provider,expected", [
    (None, "not configured"),
    (FakeProvider(exc=OSError("down")), "search failed"),
    (FakeProvider(selection=None), "Nothing found"),
])
async def test_play_music_failures_are_reported(monkeypatch, provider, expected):
    monkeypatch.setattr(d, "music_provider", provider)
    result = await d.play_music("x")
    assert result["status"] == "error" and expected in result["details"]


async def test_play_music_unknown_kind_falls_back_to_track(monkeypatch):
    p = FakeProvider(selection=providers.MusicSelection(["youtube:video:a"], "T", "A"))
    monkeypatch.setattr(d, "music_provider", p)
    result = await d.play_music("x", kind="podcast")
    assert p.kinds == ["track"] and result["uris"] == ["youtube:video:a"]
    assert result["for_model"] == {"title": "T", "artist": "A", "tracks": 1}


async def test_next_track_tool(mpd, mopidy, eska):
    await mpd.play_station(*eska)
    assert (await d.next_track())["status"] == "error"
    await mpd.play_music(["youtube:video:a", "youtube:video:b"], "Song")
    assert await d.next_track() == {"status": "success", "title": "T1", "artist": "A", "remaining_tracks": 0}
    assert (await d.next_track())["status"] == "no_next"


# --- music provider (Mopidy JSON-RPC) ---------------------------------------------

TRACKS = [{"uri": f"youtube:video:{i}", "name": f"T{i}", "artists": [{"name": "Artist"}]} for i in range(30)]
ALBUM = {"uri": "youtube:playlist:p", "name": "Album", "artists": [{"name": "Artist"}]}


@pytest.fixture
def rpc(monkeypatch):
    replies = {}
    sent = []

    class R:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def post(url, json, timeout):
        sent.append(json)
        return R(replies[json["method"]])

    monkeypatch.setattr(providers.requests, "post", post)
    return SimpleNamespace(replies=replies, sent=sent)


def provider(n=5):
    return providers.MopidyMusicProvider("YouTube", "youtube:", MusicConfig(enabled=True, artist_tracks=n))


async def test_search_is_limited_to_the_provider_scheme(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": TRACKS}]}
    await provider().find("q", "track")
    assert rpc.sent[0]["params"] == {"query": {"any": ["q"]}, "uris": ["youtube:"]}


@pytest.mark.parametrize("kind,count", [("track", 1), ("artist", 5)])
async def test_track_and_artist_pick(rpc, kind, count):
    rpc.replies["core.library.search"] = {"result": [{"tracks": TRACKS, "albums": [ALBUM]}]}
    sel = await provider().find("q", kind)
    assert len(sel.uris) == count and sel.title == "T0" and sel.artist == "Artist"


async def test_album_is_expanded_and_falls_back_to_tracks_when_empty(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": TRACKS, "albums": [ALBUM]}]}
    rpc.replies["core.library.lookup"] = {"result": {"youtube:playlist:p": [{"uri": "youtube:video:x"}]}}
    sel = await provider().find("q", "album")
    assert sel.uris == ["youtube:video:x"] and sel.title == "Album"
    rpc.replies["core.library.lookup"] = {"result": {"youtube:playlist:p": []}}
    assert (await provider().find("q", "album")).uris == ["youtube:video:0"]


@pytest.mark.parametrize("result", [None, [], [{"tracks": None, "albums": None}]])
async def test_nothing_found(rpc, result):
    rpc.replies["core.library.search"] = {"result": result}
    assert await provider().find("q", "track") is None


async def test_rpc_error_raises(rpc):
    rpc.replies["core.library.search"] = {"error": {"message": "boom"}}
    with pytest.raises(RuntimeError):
        await provider().find("q", "track")


def test_unknown_provider_rejected_and_disabled_gives_none():
    with pytest.raises(ValueError):
        providers.create_provider(MusicConfig(enabled=True, provider="tidal"))
    assert providers.create_provider(MusicConfig(enabled=False)) is None


# --- declarations ------------------------------------------------------------------

def test_every_declared_tool_is_registered():
    names = [decl.name for decl in schemas._ALL_DECLARATIONS]
    assert set(names) <= set(FUNCTIONS)
    assert len(names) == len(set(names))
    assert {"play_music", "next_track", "set_ha_entity_state"} <= set(names)


def test_gemini_tools_are_blocking():
    # gemini-3.8-live defaults to NON_BLOCKING; the deferred actions rely on the
    # tool result arriving before the spoken answer
    decls = schemas.GEMINI_TOOLS[0].function_declarations
    assert decls and all(decl.behavior == schemas.Behavior.BLOCKING for decl in decls)


def test_openai_tools_keep_enums():
    music = next(t for t in schemas.OPENAI_TOOLS if t["name"] == "play_music")
    assert music["parameters"]["properties"]["kind"]["enum"] == ["track", "artist", "album"]


# --- tool calls in the Gemini manager -------------------------------------------------

@pytest.fixture
def gemini():
    mgr = GeminiRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())
    sent = []

    class Session:
        async def send_tool_response(self, function_responses):
            sent.extend(function_responses)

    mgr._session = Session()
    mgr.sent = sent
    yield mgr
    mgr.cancel_watchdog()


def call(name, **args):
    return SimpleNamespace(name=name, args=args, id=f"id-{name}")


async def test_failed_lookup_reaches_the_model_and_is_not_deferred(gemini, mpd, monkeypatch):
    async def none(name):
        return None
    monkeypatch.setattr(d.radio_client, "search_station", none)
    await gemini._handle_tool_calls([call("play_internet_radio", station_name="Nope FM")])
    assert gemini.pending_actions == []
    assert gemini.sent[0].response["status"] == "error"


async def test_deferred_action_is_queued_and_facts_reach_the_model(gemini, mpd):
    await gemini._handle_tool_calls([call("set_playback_volume", volume_percentage=30)])
    assert gemini.pending_actions[0]["type"] == "set_playback_volume"
    assert gemini.sent[0].response == {"status": "success", "previous_volume": 50}


async def test_immediate_tool_result_and_exceptions_reach_the_model(gemini, monkeypatch):
    async def boom():
        raise RuntimeError("kaput")
    monkeypatch.setitem(FUNCTIONS, "get_current_date", boom)
    await gemini._handle_tool_calls([call("get_current_time"), call("get_current_date")])
    assert "result" in gemini.sent[0].response
    assert "kaput" in gemini.sent[1].response["error"]


async def test_request_for_user_input_sets_the_follow_up_flag(gemini):
    await gemini._handle_tool_calls([call("request_for_user_input")])
    assert gemini.request_for_user_input is True


def test_deferred_tools_exist():
    from realtime.base import BaseRealtimeManager
    assert BaseRealtimeManager.DEFERRED_FUNCTIONS <= set(FUNCTIONS)

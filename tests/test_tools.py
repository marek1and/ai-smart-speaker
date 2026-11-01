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

@pytest.fixture(autouse=True)
def music_needs_mpd(request, monkeypatch):
    """play_music reads the playback generation; tests that use the real
    client inject it through the mpd fixture."""
    if "mpd" not in request.fixturenames:
        monkeypatch.setattr(d, "_mpd_client", SimpleNamespace(playback_generation=0))


class FakeProvider:
    label = "YouTube"

    def __init__(self, selection=None, exc=None, prefetch_exc=None, cached=None):
        self.selection, self.exc, self.kinds = selection, exc, []
        self.prefetch_exc, self.prefetched, self.cached = prefetch_exc, [], cached

    def is_cached(self, uri):
        return self.cached

    async def find(self, query, kind):
        self.kinds.append(kind)
        if self.exc:
            raise self.exc
        return self.selection

    async def prefetch(self, uri):
        self.prefetched.append(uri)
        if self.prefetch_exc:
            raise self.prefetch_exc
        return 41

    async def drop(self, tlids):
        self.dropped = tlids


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


async def test_play_music_prefetches_only_the_first_track(monkeypatch):
    p = FakeProvider(selection=providers.MusicSelection(["youtube:video:a", "youtube:video:b"], "T", "A"))
    monkeypatch.setattr(d, "music_provider", p)
    await d.play_music("x", kind="artist")
    assert p.prefetched == ["youtube:video:a"]


@pytest.mark.parametrize("cached,starts", [(True, "now"), (False, "in_a_few_seconds"), (None, None)])
async def test_play_music_tells_the_model_when_it_starts(monkeypatch, cached, starts):
    p = FakeProvider(selection=providers.MusicSelection(["youtube:video:a"], "T", "A"), cached=cached)
    monkeypatch.setattr(d, "music_provider", p)
    result = await d.play_music("x")
    assert result["for_model"].get("starts") == starts


async def test_prefetched_entry_is_remembered_and_dropped_once(monkeypatch):
    p = FakeProvider(selection=providers.MusicSelection(["youtube:video:a"], "T", "A"), cached=False)
    monkeypatch.setattr(d, "music_provider", p)
    monkeypatch.setattr(d, "_prefetched_tlids", [])
    monkeypatch.setattr(d, "_mpd_client", SimpleNamespace(playback_generation=9))
    result = await d.play_music("x")
    assert result["prefetched"] is True and d._prefetched_tlids == [41]
    assert result["generation"] == 9
    await d.drop_prefetched()
    assert p.dropped == [41] and d._prefetched_tlids == []


async def test_stop_during_the_search_still_cancels(monkeypatch):
    # seen live 01.11.2026 (OpenAI): power OFF while the search ran
    client = SimpleNamespace(playback_generation=3)

    class Slow(FakeProvider):
        async def find(self, query, kind):
            client.playback_generation += 1  # the stop arrives meanwhile
            return await super().find(query, kind)

    monkeypatch.setattr(d, "_mpd_client", client)
    monkeypatch.setattr(d, "music_provider", Slow(selection=providers.MusicSelection(["local:track:a"], "T", "A")))
    assert (await d.play_music("x"))["generation"] == 3


async def test_cached_track_is_not_prefetched(monkeypatch):
    p = FakeProvider(selection=providers.MusicSelection(["youtube:video:a"], "T", "A"), cached=True)
    monkeypatch.setattr(d, "music_provider", p)
    await d.play_music("x")
    assert p.prefetched == []


async def test_failed_prefetch_still_plays_and_is_logged(monkeypatch, caplog):
    p = FakeProvider(
        selection=providers.MusicSelection(["youtube:video:a"], "T", "A"),
        prefetch_exc=OSError("mopidy down"),
    )
    monkeypatch.setattr(d, "music_provider", p)
    result = await d.play_music("x")
    assert result["uris"] == ["youtube:video:a"] and "status" not in result
    assert any(r.exc_info for r in caplog.records if "Prefetch" in r.getMessage())


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


async def test_track_pick_prefers_the_hit_that_matches_the_words(rpc):
    # seen 01.11.2026: "Harlem mój dom" played the service's first hit, another song
    hits = [{"uri": "youtube:video:b", "name": "Biegnę (O tobie sen błękitny)", "artists": [{"name": "Harlem"}]},
            {"uri": "youtube:video:d", "name": "Mój dom", "artists": [{"name": "Harlem"}]}]
    rpc.replies["core.library.search"] = {"result": [{"tracks": hits}]}
    assert (await provider().find("Harlem mój dom", "track")).uris == ["youtube:video:d"]
    assert (await provider().find("Harlem", "track")).uris == ["youtube:video:b"]  # tie keeps the first


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


@pytest.mark.parametrize("current,position", [(None, 0), (0, 1), (6, 7)])
async def test_prefetch_queues_right_after_the_current_track(rpc, current, position):
    # Mopidy-YouTube fetches only the tracks next to the current one, so an
    # entry at the end of a long music queue would never be fetched early.
    rpc.replies["core.tracklist.index"] = {"result": current}
    rpc.replies["core.tracklist.add"] = {"result": []}
    await provider().prefetch("youtube:video:z")
    assert rpc.sent[-1]["params"] == {"uris": ["youtube:video:z"], "at_position": position}


def test_cache_check_matches_mopidy_youtube_file_names(tmp_path):
    (tmp_path / "abc.webm").write_bytes(b"")
    p = providers.MopidyMusicProvider("YouTube", "youtube:", MusicConfig(enabled=True, cache_dir=str(tmp_path)))
    assert p.is_cached("youtube:video:abc") is True
    assert p.is_cached("youtube:video:other") is False
    assert provider().is_cached("youtube:video:abc") is None  # no cache_dir


async def test_prefetch_returns_the_entry_and_drop_removes_it(rpc):
    rpc.replies["core.tracklist.index"] = {"result": 0}
    rpc.replies["core.tracklist.add"] = {"result": [{"tlid": 7, "track": {}}]}
    rpc.replies["core.tracklist.remove"] = {"result": []}
    assert await provider().prefetch("youtube:video:z") == 7
    await provider().drop([7])
    assert rpc.sent[-1]["params"] == {"criteria": {"tlid": [7]}}


def youtube_with_cache(tmp_path):
    return providers.MopidyMusicProvider("YouTube", "youtube:", MusicConfig(enabled=True, cache_dir=str(tmp_path)))


async def test_wait_ready_on_metadata_or_a_finished_download(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_READY_POLL_SECONDS", 0.01)
    (tmp_path / "abc.json").write_text("{}")
    assert await youtube_with_cache(tmp_path).wait_ready("youtube:video:abc", 1.0)
    (tmp_path / "def.webm").write_bytes(b"x" * 10)  # no cover, so no .json; size stays
    assert await youtube_with_cache(tmp_path).wait_ready("youtube:video:def", 1.0)
    assert not await youtube_with_cache(tmp_path).wait_ready("youtube:video:none", 0.05)
    assert not await provider().wait_ready("youtube:video:abc", 0.05)  # no cache_dir


async def test_unreachable_collection_file_goes_to_the_next_provider(rpc, tmp_path):
    (tmp_path / "Dżem").mkdir()
    (tmp_path / "Dżem" / "whisky.mp3").write_bytes(b"")
    present = local_track("local:track:D%C5%BCem/whisky.mp3", "Whisky", "Dżem")
    gone = local_track("local:track:D%C5%BCem/gone.mp3", "Whisky", "Dżem")
    cfg = MusicConfig(enabled=True, local_media_dir=str(tmp_path))
    p = providers.LocalCollectionProvider("collection", "local:", cfg)
    rpc.replies["core.library.search"] = {"result": [{"tracks": [present]}]}
    assert (await p.find("Dżem Whisky", "track")).uris == [present["uri"]]
    rpc.replies["core.library.search"] = {"result": [{"tracks": [gone]}]}
    assert await p.find("Dżem Whisky", "track") is None


async def test_rpc_error_raises(rpc):
    rpc.replies["core.library.search"] = {"error": {"message": "boom"}}
    with pytest.raises(RuntimeError):
        await provider().find("q", "track")


# --- local collection first, YouTube as the fallback --------------------------------

def local_track(uri, name, artist, album="", album_uri="", disc=None, no=None):
    t = {"uri": uri, "name": name, "artists": [{"name": artist}],
         "album": {"uri": album_uri, "name": album, "artists": [{"name": artist}]}}
    if disc is not None:
        t["disc_no"] = disc
    if no is not None:
        t["track_no"] = no
    return t


COLLECTION = [
    local_track("local:track:1", "Whisky", "Dżem", "Cegła", "local:album:cegla"),
    local_track("local:track:2", "Czerwony jak cegła", "Dżem", "Cegła", "local:album:cegla"),
    local_track("local:track:3", "Wehikuł czasu", "Dżem", "Najemnik", "local:album:najemnik"),
    # matched "dzem" only through its path in Mopidy-Local
    local_track("local:track:4", "Intro", "Kult", "Tata Kazika", "local:album:tata"),
]


def local(n=5):
    return providers.LocalCollectionProvider("collection", "local:", MusicConfig(enabled=True, artist_tracks=n))


async def test_local_track_needs_every_word_in_its_tags(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": COLLECTION}]}
    sel = await local().find("Dżem Whisky", "track")
    assert sel.uris == ["local:track:1"] and sel.source == "collection"
    assert rpc.sent[0]["params"]["uris"] == ["local:"]
    assert await local().find("Dżem Rock", "track") is None


async def test_local_words_are_compared_without_diacritics(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": COLLECTION}]}
    assert (await local().find("dzem wehikul", "track")).uris == ["local:track:3"]


async def test_local_artist_queues_only_that_artist(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": COLLECTION}]}
    sel = await local(n=10).find("Dżem", "artist")
    assert sorted(sel.uris) == ["local:track:1", "local:track:2", "local:track:3"]
    assert (await local(n=2).find("Dżem", "artist")).uris.__len__() == 2


async def test_local_artist_with_only_a_guest_track_goes_to_the_provider(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": [
        local_track("local:track:g", "Nasze rendez vous", "Kombii, Liber i Doniu")]}]}
    assert await local().find("Kombii", "artist") is None


async def test_local_prefers_titled_tracks_over_untitled_rips(rpc):
    rips = [local_track(f"local:track:r{i}", f"AudioTrack 0{i}", "Hey") for i in range(1, 4)]
    named = local_track("local:track:n", "Teksański", "Hey")
    rpc.replies["core.library.search"] = {"result": [{"tracks": rips + [named]}]}
    sel = await local(n=15).find("Hey", "artist")
    assert sel.uris[0] == "local:track:n" and sel.title == "Teksański" and len(sel.uris) == 4
    assert len((await local(n=2).find("Hey", "artist")).uris) == 2
    rpc.replies["core.library.search"] = {"result": [{"tracks": rips}]}
    assert len((await local(n=15).find("Hey", "artist")).uris) == 3


async def test_local_artist_matches_whole_words(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": [
        local_track(f"local:track:q{i}", f"Song {i}", "Queensrÿche") for i in range(5)]}]}
    assert await local().find("Queen", "artist") is None


async def test_local_query_drops_full_text_syntax(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": COLLECTION}]}
    await local().find('"Dżem" -Whisky OR (Cegła)*', "track")
    assert rpc.sent[0]["params"]["query"] == {"any": ["Dżem Whisky or Cegła"]}


async def test_local_artist_threshold_zero_does_not_crash(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": []}]}
    p = providers.LocalCollectionProvider("collection", "local:", MusicConfig(enabled=True, local_min_artist_tracks=0))
    assert await p.find("Nikt", "artist") is None


async def test_local_album_plays_in_disc_and_track_order(rpc):
    rpc.replies["core.library.search"] = {"result": [{"tracks": COLLECTION}]}
    rpc.replies["core.library.lookup"] = {"result": {"local:album:cegla": [
        local_track("local:track:b", "B", "Dżem", disc=2, no=1),
        local_track("local:track:a2", "A2", "Dżem", disc=1, no=2),
        local_track("local:track:a1", "A1", "Dżem", disc=1, no=1),
    ]}}
    sel = await local().find("Dżem Cegła", "album")
    assert sel.uris == ["local:track:a1", "local:track:a2", "local:track:b"] and sel.title == "Cegła"


async def test_local_tracks_start_at_once_and_are_not_prefetched(rpc):
    assert local().is_cached("local:track:1") is True
    await local().prefetch("local:track:1")
    assert rpc.sent == []


class StubProvider:
    def __init__(self, label, scheme, selection=None, exc=None):
        self.label, self.uri_scheme, self.selection, self.exc = label, scheme, selection, exc
        self.prefetched = []

    async def find(self, query, kind):
        if self.exc:
            raise self.exc
        return self.selection

    def is_cached(self, uri):
        return self.label

    async def prefetch(self, uri):
        self.prefetched.append(uri)


async def test_fallback_uses_youtube_only_when_the_collection_has_nothing():
    yt_sel = providers.MusicSelection(["youtube:video:x"], "T", "A", "YouTube")
    local_sel = providers.MusicSelection(["local:track:1"], "Whisky", "Dżem", "collection")
    chain = providers.FallbackProvider([StubProvider("collection", "local:", local_sel),
                                        StubProvider("YouTube", "youtube:", yt_sel)])
    assert (await chain.find("q", "track")).source == "collection"
    chain = providers.FallbackProvider([StubProvider("collection", "local:", None),
                                        StubProvider("YouTube", "youtube:", yt_sel)])
    assert (await chain.find("q", "track")).source == "YouTube"


async def test_fallback_skips_a_broken_collection_but_not_a_broken_last_provider(caplog):
    yt_sel = providers.MusicSelection(["youtube:video:x"], "T", "A", "YouTube")
    chain = providers.FallbackProvider([StubProvider("collection", "local:", exc=OSError("db")),
                                        StubProvider("YouTube", "youtube:", yt_sel)])
    assert (await chain.find("q", "track")).source == "YouTube"
    assert any(r.exc_info for r in caplog.records if "trying the next provider" in r.getMessage())
    chain = providers.FallbackProvider([StubProvider("collection", "local:", None),
                                        StubProvider("YouTube", "youtube:", exc=OSError("down"))])
    with pytest.raises(OSError):
        await chain.find("q", "track")


async def test_fallback_routes_cache_and_prefetch_by_uri_scheme():
    loc, yt = StubProvider("collection", "local:"), StubProvider("YouTube", "youtube:")
    chain = providers.FallbackProvider([loc, yt])
    assert chain.is_cached("local:track:1") == "collection"
    assert chain.is_cached("youtube:video:x") == "YouTube"
    await chain.prefetch("youtube:video:x")
    assert yt.prefetched == ["youtube:video:x"] and loc.prefetched == []


def test_local_first_builds_the_chain():
    chain = providers.create_provider(MusicConfig(enabled=True, local_first=True))
    assert isinstance(chain, providers.FallbackProvider) and chain.label == "collection or YouTube"
    assert isinstance(providers.create_provider(MusicConfig(enabled=True)), providers.MopidyMusicProvider)


async def test_play_music_tells_the_model_the_source(monkeypatch):
    p = FakeProvider(selection=providers.MusicSelection(["local:track:1"], "Whisky", "Dżem", "collection"))
    monkeypatch.setattr(d, "music_provider", p)
    assert (await d.play_music("Dżem Whisky"))["for_model"]["source"] == "collection"


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


async def test_both_backends_report_deferred_actions_as_they_are_queued(gemini, mpd):
    # the orchestrator starts music from this callback, before the reply ends
    from realtime.openai_rt import OpenAIRealtimeManager
    openai = OpenAIRealtimeManager(get_config(), asyncio.Queue(), asyncio.Queue())

    async def ws_send(event):
        pass

    openai._ws_send = ws_send
    for mgr, run in (
        (gemini, lambda: gemini._handle_tool_calls([call("set_playback_volume", volume_percentage=30)])),
        (openai, lambda: openai._handle_tool_call(
            {"call_id": "c1", "name": "set_playback_volume", "arguments": '{"volume_percentage": 30}'})),
    ):
        seen = []
        mgr.set_callbacks(on_deferred=seen.append)
        await run()
        assert seen == mgr.pending_actions and seen[0]["type"] == "set_playback_volume", type(mgr).__name__


def server(**content):
    return SimpleNamespace(server_content=SimpleNamespace(**content))


async def test_gemini_turn_ends_after_generation_complete_when_turn_complete_lags(gemini, monkeypatch):
    # measured 01.11.2026: turn_complete 4-19 s after generation_complete
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.05)
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._chunks_received = 5
    gemini._frames_sent, gemini._response_started = 1000, True  # a turn with real audio
    gemini._waiting_for_turn_complete = True
    await gemini._process_api_response(server(generation_complete=True))
    await asyncio.sleep(0.1)
    assert ended == [True] and gemini._waiting_for_turn_complete is False
    # the next turn starts before the late turn_complete arrives
    gemini.start_new_turn()
    gemini._frames_sent, gemini._response_started = 1000, True
    gemini._waiting_for_turn_complete = True
    await gemini._process_api_response(server(turn_complete=True))  # the late one
    assert ended == [True] and gemini._waiting_for_turn_complete is True
    await gemini._process_api_response(audio_msg(8000))              # the new answer
    await gemini._process_api_response(server(turn_complete=True))  # its own
    assert ended == [True, True]


async def test_gemini_barge_in_cancels_the_previous_turns_grace_timer(gemini, monkeypatch):
    # review 01.11.2026: the timer of the old turn ended the new one mid-speech
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.05)
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._chunks_received = 5
    await gemini._process_api_response(server(generation_complete=True))
    gemini.start_new_turn()  # the user barges in within the grace
    await asyncio.sleep(0.1)
    assert ended == []
    assert gemini._late_turn_completes == 1  # the old turn's turn_complete is still to come


async def test_gemini_late_turn_complete_in_the_middle_of_the_next_answer_does_not_cut_it(gemini):
    # review 2: a late turn_complete arriving after the new turn's first audio
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._late_turn_completes = 1
    gemini.start_new_turn()
    gemini._frames_sent, gemini._response_started = 1000, True
    await gemini._process_api_response(audio_msg(8000))              # the new answer streams
    await gemini._process_api_response(server(turn_complete=True))  # the old turn's, late
    assert ended == [] and gemini._late_turn_completes == 0
    await gemini._process_api_response(server(turn_complete=True))  # its own
    assert ended == [True]


async def test_gemini_watchdog_drops_a_late_turn_complete_that_never_came(gemini):
    gemini.set_callbacks(on_turn_complete=lambda: None, on_turn_timeout=lambda: None)
    gemini._late_turn_completes = 1
    gemini.start_watchdog(timeout=0.05)
    await asyncio.sleep(0.1)
    assert gemini._late_turn_completes == 0
    gemini._late_turn_completes = 1
    gemini.start_watchdog(timeout=5)
    gemini.cancel_watchdog()  # a normal end: the count stays
    await asyncio.sleep(0)
    assert gemini._late_turn_completes == 1


async def test_gemini_logs_the_turn_timeline_when_turn_complete_comes(gemini, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.05)
    gemini.set_callbacks(on_turn_complete=lambda: None)
    gemini._frames_sent, gemini._response_started, gemini._chunks_received = 1000, True, 5
    gemini._timing_start()
    gemini._timing["audio"], gemini._timing["silent"] = 12.0, 10.0
    gemini._timing_mark("first_voice")
    await gemini._process_api_response(server(generation_complete=True))
    await asyncio.sleep(0.1)                          # the turn ends early...
    await gemini._process_api_response(server(turn_complete=True))  # ...its turn_complete comes late
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Turn timing")]
    assert len(lines) == 1 and lines[0].startswith("Turn timing (late)") and "silent=10.0s" in lines[0]


def audio_msg(level):
    import numpy as np
    pcm = (np.ones(2400) * level).astype(np.int16).tobytes()
    part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm), text=None)
    return server(model_turn=SimpleNamespace(parts=[part]))


async def test_gemini_trailing_silence_is_not_played_pauses_are(gemini, monkeypatch):
    # on the speaker Gemini sent ~2 s of speech and 12-20 s of silence, all of it
    # before generation_complete (01.11.2026)
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 5.0)
    gemini._frames_sent, gemini._response_started = 1000, True
    out = gemini._output_queue
    await gemini._process_api_response(audio_msg(0))      # leading silence: held...
    await gemini._process_api_response(audio_msg(8000))   # ...and played before the voice
    await gemini._process_api_response(audio_msg(0))      # a pause inside the answer
    await gemini._process_api_response(audio_msg(8000))
    assert out.qsize() == 4
    for _ in range(5):
        await gemini._process_api_response(audio_msg(0))  # trailing padding
    assert out.qsize() == 4
    await gemini._process_api_response(server(generation_complete=True))
    assert out.qsize() == 4 and gemini._held_silence == []
    gemini._cancel_generation_end()


async def test_gemini_trace_logs_one_line_per_turn_with_runs_merged(gemini, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(gemini.live_cfg, "trace_api_events", True)
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 5.0)
    gemini._frames_sent, gemini._response_started = 1000, True
    gemini._trace_event("→audio", 0.032)
    gemini._trace_event("→audio", 0.032)
    gemini._timing_start()
    gemini._trace_event("→activity_end")
    for level in (8000, 8000, 0, 0, 0):
        await gemini._process_api_response(audio_msg(level))
    await gemini._process_api_response(server(generation_complete=True))
    await gemini._process_api_response(server(turn_complete=True))
    gemini._cancel_generation_end()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Turn trace")]
    assert len(lines) == 1
    line = lines[0]
    assert "→audio x2" in line and "→activity_end" in line and "←voice x2" in line and "←silence x3" in line
    assert line.index("←voice") < line.index("←silence") < line.index("←generation_complete") < line.index("←turn_complete")


def test_gemini_trace_is_off_by_default(gemini):
    gemini._trace_event("→text")
    assert gemini._trace == []


async def test_gemini_long_silence_after_speech_ends_the_turn_once(gemini, monkeypatch):
    # once 76 s of silence came before generation_complete
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.05)
    monkeypatch.setattr(gemini.live_cfg, "trailing_silence_end", 0.3)  # 0.1 s per chunk below
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._frames_sent, gemini._response_started = 1000, True
    await gemini._process_api_response(audio_msg(8000))  # no _timing_start: works without diagnostics
    for _ in range(6):
        await gemini._process_api_response(audio_msg(0))
    await asyncio.sleep(0.1)
    assert ended == [True] and gemini._output_queue.qsize() == 1  # silence not played
    await gemini._process_api_response(server(generation_complete=True))  # comes later
    await asyncio.sleep(0.1)
    assert ended == [True]  # not a second end


async def test_gemini_pause_cut_keeps_whole_pcm16_samples(gemini, monkeypatch):
    # review: an odd byte budget split a sample and misaligned the audio
    monkeypatch.setattr(gemini.live_cfg, "max_played_pause", 0.3334)
    monkeypatch.setattr(gemini.live_cfg, "trailing_silence_end", 0)
    gemini._frames_sent, gemini._response_started = 1000, True
    await gemini._process_api_response(audio_msg(8000))
    for _ in range(5):
        await gemini._process_api_response(audio_msg(0))
    await gemini._process_api_response(audio_msg(8000))
    while not gemini._output_queue.empty():
        assert len(gemini._output_queue.get_nowait()) % 2 == 0


async def test_gemini_long_pause_before_more_speech_is_cut(gemini, monkeypatch):
    monkeypatch.setattr(gemini.live_cfg, "max_played_pause", 0.25)  # 2.5 chunks of 0.1 s
    monkeypatch.setattr(gemini.live_cfg, "trailing_silence_end", 0)
    gemini._frames_sent, gemini._response_started = 1000, True
    await gemini._process_api_response(audio_msg(8000))
    for _ in range(10):
        await gemini._process_api_response(audio_msg(0))   # 1 s of silence...
    await gemini._process_api_response(audio_msg(8000))   # ...then a blip
    played = []
    while not gemini._output_queue.empty():
        played.append(len(gemini._output_queue.get_nowait()))
    silence = sum(played[1:-1]) / 2 / 24000
    assert abs(silence - 0.25) < 0.01


async def test_gemini_turn_complete_within_the_grace_ends_the_turn_once(gemini, monkeypatch):
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.2)
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._chunks_received = 5
    gemini._frames_sent, gemini._response_started = 1000, True  # a turn with real audio
    await gemini._process_api_response(server(generation_complete=True))
    await gemini._process_api_response(server(turn_complete=True))
    await asyncio.sleep(0.3)
    assert ended == [True] and gemini._late_turn_completes == 0


async def test_gemini_more_content_after_generation_complete_keeps_the_turn(gemini, monkeypatch):
    monkeypatch.setattr(gemini.live_cfg, "generation_complete_grace", 0.05)
    ended = []
    gemini.set_callbacks(on_turn_complete=lambda: ended.append(True))
    gemini._chunks_received = 5
    gemini._frames_sent, gemini._response_started = 1000, True  # a turn with real audio
    await gemini._process_api_response(server(generation_complete=True))
    await gemini._process_api_response(SimpleNamespace(server_content=None, tool_call=SimpleNamespace(function_calls=[])))
    await asyncio.sleep(0.1)
    assert ended == []


# --- device states in the system prompt ------------------------------------------

class StatesHA:
    def __init__(self, items, delay=0.0):
        self.items, self.delay, self.asked = items, delay, None

    def get_state_objects(self, entity_ids):
        import time
        time.sleep(self.delay)
        self.asked = entity_ids
        return [i for i in self.items if i["entity_id"] in entity_ids]


PROMPT = "Encje: light.salon_glowne, switch.fontanna, media_player.telewizor_salon, binary_sensor.okno, cover.roleta."
ITEMS = [
    {"entity_id": "light.salon_glowne", "state": "on", "attributes": {"brightness": 128}},
    {"entity_id": "switch.fontanna", "state": "off", "attributes": {}},
    {"entity_id": "media_player.telewizor_salon", "state": "on", "attributes": {}},
]


@pytest.fixture
def prompt_cfg(monkeypatch):
    monkeypatch.setattr(d.config.live, "system_instruction", PROMPT)
    monkeypatch.setattr(d.config.home_assistant, "prompt_state_domains", ["light", "switch", "fan", "media_player"])
    monkeypatch.setattr(d.config.home_assistant, "prompt_state_timeout", 0.2)


async def test_prompt_states_cover_the_switchable_entities_the_prompt_lists(prompt_cfg, monkeypatch):
    ha = StatesHA(ITEMS)
    monkeypatch.setattr(d, "ha_client", ha)
    block = await d.prompt_state_context()
    assert sorted(ha.asked) == ["light.salon_glowne", "media_player.telewizor_salon", "switch.fontanna"]
    assert "- light.salon_glowne: on, jasność 50%" in block and "- switch.fontanna: off" in block
    assert "binary_sensor" not in block and "cover" not in block  # windows, covers: through the tools


@pytest.mark.parametrize("case", ["slow_ha", "disabled", "no_ha"])
async def test_session_starts_without_states_when_they_are_not_available(prompt_cfg, monkeypatch, case):
    monkeypatch.setattr(d, "ha_client", None if case == "no_ha" else StatesHA(ITEMS, delay=0.5 if case == "slow_ha" else 0))
    if case == "disabled":
        monkeypatch.setattr(d.config.home_assistant, "prompt_state_domains", [])
    assert await d.prompt_state_context() == ""


def test_both_backends_append_the_session_context_to_the_instruction():
    from realtime.openai_rt import OpenAIRealtimeManager
    for cls in (GeminiRealtimeManager, OpenAIRealtimeManager):
        mgr = cls(get_config(), asyncio.Queue(), asyncio.Queue())
        base = mgr.live_cfg.system_instruction or ""
        assert mgr._instructions() == base
        mgr.session_context = "Stan: light.x: on"
        assert mgr._instructions().endswith("\n\nStan: light.x: on") and mgr._instructions().startswith(base)


def test_both_backends_await_the_reply_after_activity_end_and_a_typed_request():
    from realtime.openai_rt import OpenAIRealtimeManager
    for cls in (GeminiRealtimeManager, OpenAIRealtimeManager):
        mgr = cls(get_config(), asyncio.Queue(), asyncio.Queue())
        assert mgr.awaiting_reply is False
        mgr._begin_text_turn("hej")
        assert mgr.awaiting_reply is True, cls.__name__


def test_deferred_tools_exist():
    from realtime.base import BaseRealtimeManager
    assert BaseRealtimeManager.DEFERRED_FUNCTIONS <= set(FUNCTIONS)

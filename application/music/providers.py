"""Music search through Mopidy's JSON-RPC API.

Playback stays on MPD (MPDClientWrapper), so ducking, fades and MQTT state
work the same as for the radio. A provider only turns a spoken request into
track URIs that Mopidy can queue.
"""
import asyncio
import logging
import os
import random
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests

from config import MusicConfig

logger = logging.getLogger(__name__)

KINDS = ("track", "artist", "album")
# Poll interval while waiting for a prefetched track to finish downloading.
_READY_POLL_SECONDS = 0.5
# Untitled CD rips ("AudioTrack 05", "Track 3"): queued only when nothing else is.
UNTITLED = re.compile(r"^((audio ?)?track\s*\d+|\d+)$", re.I)
# Extensions Mopidy-YouTube looks for when it checks its cache.
CACHED_EXTENSIONS = ("webm", "m4a", "mp3", "ogg")


@dataclass(frozen=True)
class MusicSelection:
    uris: list[str]
    title: str
    artist: Optional[str]
    source: str = ""  # label of the provider that found it


class MopidyMusicProvider:
    """Searches one Mopidy backend, selected by its URI scheme.

    Mopidy-YouTube answers only free-text ("any") queries and returns videos as
    tracks and playlists as albums; artists come back empty. Artist requests
    therefore queue the top track hits. A backend with real artist/album search
    (Spotify) can subclass and override _pick.
    """

    def __init__(self, label: str, uri_scheme: str, config: MusicConfig) -> None:
        self.label = label
        self.uri_scheme = uri_scheme
        self._config = config

    async def find(self, query: str, kind: str) -> Optional[MusicSelection]:
        tracks, albums = await self._search(query)
        return await self._pick(kind, tracks, albums, query)

    async def _search(self, query: str) -> tuple[list[dict], list[dict]]:
        result = await self._rpc(
            "core.library.search",
            {"query": {"any": [query]}, "uris": [self.uri_scheme]},
        )
        # One SearchResult per backend that answered.
        tracks = [t for r in result or [] for t in r.get("tracks") or []]
        albums = [a for r in result or [] for a in r.get("albums") or []]
        return tracks, albums

    def is_cached(self, uri: str) -> Optional[bool]:
        """Whether the track is already downloaded; None without cache_dir."""
        if not self._config.cache_dir:
            return None
        track_id = uri.rsplit(":", 1)[-1]
        return any(
            os.path.exists(os.path.join(self._config.cache_dir, f"{track_id}.{ext}"))
            for ext in CACHED_EXTENSIONS
        )

    async def prefetch(self, uri: str) -> Optional[int]:
        """Queues uri right after the current track, so the backend starts
        fetching it while the AI is still speaking.

        Mopidy-YouTube downloads the tracks around the current one when the
        queue changes (~8 s each). Returns the entry's tlid: play_music
        replaces the queue, and drop() removes the entry when it never ran.
        """
        current = await self._rpc("core.tracklist.index", {})
        position = 0 if current is None else current + 1
        added = await self._rpc("core.tracklist.add", {"uris": [uri], "at_position": position})
        return added[0].get("tlid") if added else None

    async def drop(self, tlids: list[int]) -> None:
        """Removes prefetched entries that are still queued."""
        await self._rpc("core.tracklist.remove", {"criteria": {"tlid": tlids}})

    async def wait_ready(self, uri: str, timeout: float) -> bool:
        """Waits until a prefetched track is downloaded, so MPD play does not
        block Mopidy's core (and our MPD lock) for the download.

        Done when Mopidy-YouTube wrote the track's .json (after the audio and
        the cover) or the audio file stopped growing (a failed cover leaves
        no .json). False when cache_dir is unset or on timeout.
        """
        if not self._config.cache_dir:
            return False
        track_id = uri.rsplit(":", 1)[-1]
        base = os.path.join(self._config.cache_dir, track_id)
        deadline = time.monotonic() + timeout
        last_size = None
        while time.monotonic() < deadline:
            meta = base + ".json"
            if os.path.exists(meta) and os.path.getsize(meta) > 0:
                return True
            sizes = [os.path.getsize(f"{base}.{ext}") for ext in CACHED_EXTENSIONS if os.path.exists(f"{base}.{ext}")]
            size = sizes[0] if sizes else None
            if size and size == last_size:
                return True
            last_size = size
            await asyncio.sleep(_READY_POLL_SECONDS)
        return False

    async def _pick(
        self, kind: str, tracks: list[dict], albums: list[dict], query: str = ""
    ) -> Optional[MusicSelection]:
        if kind == "album" and albums:
            album = albums[0]
            lookup = await self._rpc("core.library.lookup", {"uris": [album["uri"]]})
            uris = [t["uri"] for t in (lookup or {}).get(album["uri"], [])]
            if uris:
                return MusicSelection(uris, album.get("name", ""), _first_artist(album), self.label)
        if not tracks:
            return None
        if kind == "track" and query:
            # the service's first hit is often another song of the artist
            words = _fold(query).split()
            best = max(tracks, key=lambda t: _matches(words, f"{t.get('name', '')} {_artists(t)}"))
            tracks = [best] + [t for t in tracks if t is not best]
        count = self._config.artist_tracks if kind == "artist" else 1
        picked = tracks[:count]
        first = picked[0]
        return MusicSelection(
            [t["uri"] for t in picked], first.get("name", ""), _first_artist(first), self.label
        )

    async def _rpc(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = await asyncio.to_thread(
            requests.post,
            self._config.rpc_url,
            json=payload,
            timeout=self._config.search_timeout,
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"Mopidy {method}: {body['error']}")
        return body.get("result")


class LocalCollectionProvider(MopidyMusicProvider):
    """The Mopidy-Local collection. Its files play at once: nothing to prefetch.

    Mopidy-Local answers "any" with an SQLite full-text MATCH: whole words,
    in any field (the file path included), case folded for ASCII only, at most
    max_search_results tracks. A hit counts only when every word of the
    request is in the track's own tags, so a request the collection does not
    really have falls through to the next provider instead of playing
    whatever matched the path.
    """

    def is_cached(self, uri: str) -> Optional[bool]:
        return True

    async def prefetch(self, uri: str) -> Optional[int]:
        return None

    async def wait_ready(self, uri: str, timeout: float) -> bool:
        return True

    async def _reachable(self, uri: str) -> bool:
        """Whether the track's file is readable: the index lives on /data, the
        files on the NAS, so a NAS outage still returns hits."""
        if not self._config.local_media_dir:
            return True
        relpath = urllib.parse.unquote(uri.split(":", 2)[-1])
        path = os.path.join(self._config.local_media_dir, relpath)
        try:
            return await asyncio.wait_for(asyncio.to_thread(os.path.exists, path), self._config.local_media_timeout)
        except asyncio.TimeoutError:
            return False

    async def find(self, query: str, kind: str) -> Optional[MusicSelection]:
        selection = await self._find(query, kind)
        if selection and not await self._reachable(selection.uris[0]):
            logger.warning("Collection file for '%s' is not reachable (NAS down?), trying the next provider", query)
            return None
        return selection

    async def _find(self, query: str, kind: str) -> Optional[MusicSelection]:
        query = _plain_words(query)
        words = _fold(query).split()
        if not words:
            return None
        tracks, _ = await self._search(query)
        if kind == "artist":
            # whole words: "Queen" must not take Queensrÿche
            hits = [t for t in tracks if _has_all_words(words, _artists(t) + " " + _artists(t.get("album") or {}))]
            if not hits or len(hits) < self._config.local_min_artist_tracks:
                return None
            # titled tracks first (the model announces the first one), rips fill up
            titled = [t for t in hits if not _untitled(t)]
            rips = [t for t in hits if _untitled(t)]
            picked = (random.sample(titled, len(titled)) + random.sample(rips, len(rips)))[: max(1, self._config.artist_tracks)]
            return MusicSelection(
                [t["uri"] for t in picked], picked[0].get("name", ""), _first_artist(picked[0]), self.label
            )
        if kind == "album":
            selection = await self._album(words, tracks)
            if selection:
                return selection
        hits = _titled([t for t in tracks if _has_all(words, _describe(t))])
        if not hits:
            return None
        best = max(hits, key=lambda t: _matches(words, t.get("name", "")) * 2 + _matches(words, _artists(t)))
        return MusicSelection([best["uri"]], best.get("name", ""), _first_artist(best), self.label)

    async def _album(self, words: list[str], tracks: list[dict]) -> Optional[MusicSelection]:
        albums = {}
        for t in tracks:
            album = t.get("album") or {}
            described = f"{album.get('name', '')} {_artists(album)} {_artists(t)}"
            if album.get("uri") and _has_all(words, described):
                albums.setdefault(album["uri"], album)
        if not albums:
            return None
        uri, album = max(albums.items(), key=lambda kv: _matches(words, kv[1].get("name", "")))
        lookup = await self._rpc("core.library.lookup", {"uris": [uri]})
        album_tracks = sorted(
            (lookup or {}).get(uri) or [], key=lambda t: (t.get("disc_no") or 0, t.get("track_no") or 0)
        )
        if not album_tracks:
            return None
        return MusicSelection(
            [t["uri"] for t in album_tracks],
            album.get("name", ""),
            _first_artist(album) or _first_artist(album_tracks[0]),
            self.label,
        )


class FallbackProvider:
    """Asks the providers in order; the first that finds something wins.

    A failing provider that is not the last one is logged and skipped, so a
    broken local index never blocks YouTube.
    """

    def __init__(self, providers: list[MopidyMusicProvider]) -> None:
        self._providers = providers
        self.label = " or ".join(p.label for p in providers)

    async def find(self, query: str, kind: str) -> Optional[MusicSelection]:
        for provider in self._providers:
            try:
                selection = await provider.find(query, kind)
            except Exception:
                if provider is self._providers[-1]:
                    raise
                logger.warning("%s search for '%s' failed, trying the next provider",
                               provider.label, query, exc_info=True)
                continue
            if selection is not None:
                return selection
        return None

    def _owner(self, uri: str) -> MopidyMusicProvider:
        return next((p for p in self._providers if uri.startswith(p.uri_scheme)), self._providers[-1])

    def is_cached(self, uri: str) -> Optional[bool]:
        return self._owner(uri).is_cached(uri)

    async def prefetch(self, uri: str) -> Optional[int]:
        return await self._owner(uri).prefetch(uri)

    async def drop(self, tlids: list[int]) -> None:
        await self._providers[-1].drop(tlids)  # one Mopidy, one tracklist

    async def wait_ready(self, uri: str, timeout: float) -> bool:
        return await self._owner(uri).wait_ready(uri, timeout)


def _untitled(track: dict) -> bool:
    return bool(UNTITLED.match(track.get("name", "").strip()))


def _titled(tracks: list[dict]) -> list[dict]:
    titled = [t for t in tracks if not _untitled(t)]
    return titled or tracks


def _fold(text: str) -> str:
    """Lower case without diacritics, for comparing words with tags."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _artists(item: dict) -> str:
    return " ".join(a.get("name", "") for a in item.get("artists") or [])


def _describe(track: dict) -> str:
    album = track.get("album") or {}
    return f"{track.get('name', '')} {_artists(track)} {album.get('name', '')} {_artists(album)}"


def _matches(words: list[str], text: str) -> int:
    folded = _fold(text)
    return sum(w in folded for w in words)


def _has_all(words: list[str], text: str) -> bool:
    return _matches(words, text) == len(words)


def _has_all_words(words: list[str], text: str) -> bool:
    tokens = set(re.findall(r"\w+", _fold(text)))
    return all(w in tokens for w in words)


def _plain_words(query: str) -> str:
    """Drops what SQLite full-text search reads as syntax (quotes, a leading
    "-", "*", parentheses, upper-case AND/OR/NOT/NEAR) and keeps the words."""
    words = re.findall(r"\w+", query)
    return " ".join(w.lower() if w in ("AND", "OR", "NOT", "NEAR") else w for w in words)


def _first_artist(item: dict) -> Optional[str]:
    artists = item.get("artists") or []
    return artists[0].get("name") if artists else None


# name -> (label for the model, Mopidy URI scheme). Adding Spotify: enable
# mopidy-spotify with credentials and add ("Spotify", "spotify:") here.
PROVIDERS = {
    "youtube": ("YouTube", "youtube:"),
}


def create_provider(config: MusicConfig):
    if not config.enabled:
        return None
    if config.provider not in PROVIDERS:
        raise ValueError(
            f"Unknown music provider '{config.provider}'. Known: {sorted(PROVIDERS)}"
        )
    label, scheme = PROVIDERS[config.provider]
    provider = MopidyMusicProvider(label, scheme, config)
    if not config.local_first:
        return provider
    return FallbackProvider([LocalCollectionProvider("collection", "local:", config), provider])

"""Music search through Mopidy's JSON-RPC API.

Playback stays on MPD (MPDClientWrapper), so ducking, fades and MQTT state
work the same as for the radio. A provider only turns a spoken request into
track URIs that Mopidy can queue.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import MusicConfig

logger = logging.getLogger(__name__)

KINDS = ("track", "artist", "album")


@dataclass(frozen=True)
class MusicSelection:
    uris: list[str]
    title: str
    artist: Optional[str]


class MopidyMusicProvider:
    """Searches one Mopidy backend, selected by its URI scheme.

    Mopidy-YouTube answers only free-text ("any") queries and returns videos as
    tracks and playlists as albums; artists come back empty. Artist requests
    therefore queue the top track hits. A backend with real artist/album search
    (Spotify) can subclass and override _pick.
    """

    def __init__(self, label: str, uri_scheme: str, config: MusicConfig) -> None:
        self.label = label
        self._uri_scheme = uri_scheme
        self._config = config

    async def find(self, query: str, kind: str) -> Optional[MusicSelection]:
        result = await self._rpc(
            "core.library.search",
            {"query": {"any": [query]}, "uris": [self._uri_scheme]},
        )
        # One SearchResult per backend that answered.
        tracks = [t for r in result or [] for t in r.get("tracks") or []]
        albums = [a for r in result or [] for a in r.get("albums") or []]
        return await self._pick(kind, tracks, albums)

    async def _pick(
        self, kind: str, tracks: list[dict], albums: list[dict]
    ) -> Optional[MusicSelection]:
        if kind == "album" and albums:
            album = albums[0]
            lookup = await self._rpc("core.library.lookup", {"uris": [album["uri"]]})
            uris = [t["uri"] for t in (lookup or {}).get(album["uri"], [])]
            if uris:
                return MusicSelection(uris, album.get("name", ""), _first_artist(album))
        if not tracks:
            return None
        count = self._config.artist_tracks if kind == "artist" else 1
        picked = tracks[:count]
        first = picked[0]
        return MusicSelection(
            [t["uri"] for t in picked], first.get("name", ""), _first_artist(first)
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


def _first_artist(item: dict) -> Optional[str]:
    artists = item.get("artists") or []
    return artists[0].get("name") if artists else None


# name -> (label for the model, Mopidy URI scheme). Adding Spotify: enable
# mopidy-spotify with credentials and add ("Spotify", "spotify:") here.
PROVIDERS = {
    "youtube": ("YouTube", "youtube:"),
}


def create_provider(config: MusicConfig) -> Optional[MopidyMusicProvider]:
    if not config.enabled:
        return None
    if config.provider not in PROVIDERS:
        raise ValueError(
            f"Unknown music provider '{config.provider}'. Known: {sorted(PROVIDERS)}"
        )
    label, scheme = PROVIDERS[config.provider]
    return MopidyMusicProvider(label, scheme, config)

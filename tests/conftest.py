"""Shared fixtures. Runs before any application import: the application reads
its config once (get_config) at import time, so the test config must be in
place first. Nothing here talks to the network or the speaker."""
from pathlib import Path

import pytest

import config as app_config  # application/ is on pythonpath (pytest.ini)

app_config._shared_config = app_config.AppConfig.from_yaml(
    str(Path(__file__).with_name("config.test.yml"))
)

from config import MPDConfig  # noqa: E402
from mpd import CommandError, MPDError  # noqa: E402
import mpd_client.client as mpd_module  # noqa: E402


class FakeMopidy:
    """In-memory stand-in for python-mpd2's MPDClient talking to Mopidy.

    Records every command in `calls`. Behaves like mopidy-mpd where it matters:
    `clearerror` is refused the way python-mpd2 surfaces it (ValueError), the
    only output is the "Mute" switch, and `status` never carries `error`.
    """

    def __init__(self):
        self.calls = []
        self.timeout = 5
        self.queue: list[str] = []
        self.state = "stop"
        self.song: int | None = None
        self.volume = 100
        self.muted = False
        self.fail_next: dict[str, BaseException] = {}
        self.play_timeouts: list = []  # socket timeout seen by each play
        self.unplayable = False  # Mopidy skipped every track and stays stopped

    def _rec(self, name, *args):
        exc = self.fail_next.pop(name, None)
        if exc is not None:
            raise exc
        self.calls.append((name,) + args)

    def names(self):
        return [c[0] for c in self.calls]

    def connect(self, host, port): self._rec("connect")
    def close(self): pass
    def disconnect(self): pass

    def outputs(self):
        return [{"outputid": "0", "outputname": "Mute", "outputenabled": "1" if self.muted else "0"}]

    def disableoutput(self, oid):
        self._rec("disableoutput", oid)
        self.muted = False

    def enableoutput(self, oid):
        self._rec("enableoutput", oid)
        self.muted = True

    def clearerror(self):
        # mopidy-mpd answers "ACK [0@0] {clearerror} Not implemented"; python-mpd2
        # fails to map errno 0 and raises ValueError instead of CommandError.
        self._rec("clearerror")
        raise ValueError("0 is not a valid FailureResponseCode")

    def clear(self):
        self._rec("clear")
        self.queue = []
        self.song = None

    def add(self, uri):
        self._rec("add", uri)
        self.queue.append(uri)

    def setvol(self, v):
        self._rec("setvol", v)
        self.volume = int(v)

    def play(self, pos=None):
        self.play_timeouts.append(self.timeout)
        self._rec("play", pos)
        if not self.queue or self.unplayable:
            return
        self.song = 0 if pos is None else int(pos)
        self.state = "play"

    def stop(self):
        self._rec("stop")
        self.state = "stop"

    def status(self):
        st = {"volume": str(self.volume), "state": self.state,
              "playlistlength": str(len(self.queue))}
        if self.song is not None and self.state != "stop":
            st["song"] = str(self.song)
            st["songid"] = str(self.song + 1)
        return st

    def currentsong(self):
        if self.song is None or self.state == "stop":
            return {}
        return {"file": self.queue[self.song], "title": f"T{self.song}", "artist": "A"}

    def playlistinfo(self, pos=None):
        items = [{"file": u, "title": f"T{i}", "artist": "A"} for i, u in enumerate(self.queue)]
        return items if pos is None else items[int(pos):int(pos) + 1]


@pytest.fixture
def mopidy():
    return FakeMopidy()


@pytest.fixture
def mpd_config(tmp_path):
    return MPDConfig(
        state_file=str(tmp_path / "radio_state.json"),
        volume_fade_in_seconds=0.0,
        stream_start_grace=15.0,
        stream_stall_timeout=20.0,
        stream_stall_max_restarts=2,
    )


@pytest.fixture
def client(mopidy, mpd_config, monkeypatch):
    """MPDClientWrapper wired to FakeMopidy; state events land in client.events."""
    monkeypatch.setattr(mpd_module, "MPDClient", lambda: mopidy)
    c = mpd_module.MPDClientWrapper(mpd_config)
    c.client = mopidy
    c.events = []
    c.add_state_listener(c.events.append)
    return c


@pytest.fixture
def eska(client):
    """Radio Eska cached and current in the radio state."""
    client.radio_state.update_station("eska", "http://pinned.invalid/eska.mp3", "Radio Eska")
    client.radio_state.set_current("eska")
    return ("http://pinned.invalid/eska.mp3", "Radio Eska", "eska")


__all__ = ["FakeMopidy", "CommandError", "MPDError"]

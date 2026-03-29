from typing import Optional
from datetime import datetime
from functions.registry import register_function
from openhab.client import OpenHabClient
from config import AppConfig
from mpd_client.client import MPDClientWrapper
from radio.client import RadioClient

config = AppConfig.from_yaml()
openhab_client = OpenHabClient(config.openhab)
mpd_client = MPDClientWrapper(config.mpd)
radio_client = RadioClient(config.radio)


@register_function(name="play_internet_radio")
async def play_internet_radio(station_name: Optional[str] = None) -> dict:
    """
    Searches for an internet radio station and returns its information.
    If no station name is provided, it checks the radio status. If a radio station
    is on the playlist, it returns an action to play. Otherwise, it asks for a station name.
    """
    if not station_name:
        radio_status = get_radio_status()
        if radio_status.get("is_radio_on_playlist"):
            # If a station is already on the playlist, just play.
            return {"action": "play"}
        else:
            return {"error": "No radio station is on the playlist. What station would you like to play?"}

    url = await radio_client.search_station(station_name)
    if url:
        return {"url": url, "name": station_name}
    else:
        return {"error": f"Could not find a station named {station_name}."}

@register_function(name="stop_radio")
def stop_radio() -> str:
    """
    Stops the radio playback.
    """
    if not mpd_client.is_playing():
        return "Nothing is currently playing."

    # The action is deferred. The orchestrator will call mpd_client.stop().
    # This message is for the LLM.
    return "Radio will be stopped."


@register_function(name="get_radio_status")
def get_radio_status() -> dict:
    """
    Gets the current status of the radio, including playback state, volume,
    and whether a radio station is currently in the playlist.
    """
    status = mpd_client.client.status()
    playlist = mpd_client.get_playlist_info()
    volume = mpd_client.get_restore_volume()

    is_radio_on_playlist = False
    station_name_on_playlist = None
    if playlist:
        first_item = playlist[0]
        station_name = first_item.get("name") or first_item.get("title")
        url = first_item.get("file", "")

        # Check for "radio" keyword in name or URL
        is_radio_keyword = (station_name and "radio" in station_name.lower()) or \
                           ("radio" in url.lower()) or ("live" in url.lower())

        # Check if the station name is in the popular stations list
        is_popular_station = False
        if station_name:
            popular_stations = config.radio.popular_stations
            is_popular_station = any(popular.lower() in station_name.lower() for popular in popular_stations)

        if is_radio_keyword or is_popular_station:
            is_radio_on_playlist = True
            station_name_on_playlist = station_name or "Unknown Radio"


    current_song = mpd_client.get_current_song_info()
    currently_playing_station = None
    if current_song:
        currently_playing_station = current_song.get("title") or current_song.get("name")

    return {
        "playback_state": status.get("state"),  # play, stop, pause
        "volume": volume,
        "is_radio_on_playlist": is_radio_on_playlist,
        "station_name_on_playlist": station_name_on_playlist,
        "current_track_title": currently_playing_station or "Unknown",
    }


@register_function(name="get_current_time")
def get_current_time() -> str:
    """Gets the current time."""
    return datetime.now().strftime("%H:%M:%S")


@register_function(name="get_current_date")
def get_current_date() -> str:
    """Gets the current date."""
    return datetime.now().strftime("%Y-%m-%d")


@register_function(name="get_openhab_item_state")
def get_openhab_item_state(item_name: str) -> str | None:
    """Gets the state of an item in OpenHab."""
    return openhab_client.get_openhab_item_state(item_name)


@register_function(name="set_openhab_item_state")
def set_openhab_item_state(item_name: str, state: str) -> bool:
    """Sets the state of an item in OpenHab."""
    return openhab_client.set_openhab_item_state(item_name, state)


@register_function(name="set_playback_volume")
def set_playback_volume(volume_percentage: int) -> str:
    """
    Sets the playback volume of the MPD player.
    """
    mpd_client.set_volume(volume_percentage)
    return f"Playback volume set to {volume_percentage}%."

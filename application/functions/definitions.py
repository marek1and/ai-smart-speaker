from datetime import datetime
from functions.registry import register_function
from openhab.client import OpenHabClient
from config import AppConfig

config = AppConfig.from_yaml()
openhab_client = OpenHabClient(config.openhab)


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

from google.genai.types import FunctionDeclaration, Schema, Tool, Type

from functions.definitions import ACTIVE_SMARTHOME_BACKEND as _BACKEND


# ---------------------------------------------------------------------------
# Tool declarations (single source of truth)
# ---------------------------------------------------------------------------

GET_CURRENT_TIME_FUNC = FunctionDeclaration(
    name="get_current_time",
    description="Gets the current time.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
        required=[],
    ),
)

GET_CURRENT_DATE_FUNC = FunctionDeclaration(
    name="get_current_date",
    description="Gets the current date.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
        required=[],
    ),
)

GET_OPENHAB_ITEM_STATE_FUNC = FunctionDeclaration(
    name="get_openhab_item_state",
    description="Gets the state of an item in OpenHab.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "item_name": Schema(type=Type.STRING, description="The name of the item.")
        },
        required=["item_name"],
    ),
)

SET_OPENHAB_ITEM_STATE_FUNC = FunctionDeclaration(
    name="set_openhab_item_state",
    description="Sets the state of an item in OpenHab.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "item_name": Schema(type=Type.STRING, description="The name of the item."),
            "state": Schema(type=Type.STRING, description="The state to set."),
        },
        required=["item_name", "state"],
    ),
)

GET_OPENHAB_ITEMS_STATE_FUNC = FunctionDeclaration(
    name="get_openhab_items_state",
    description=(
        "Gets the states of multiple OpenHAB items in a single call. "
        "Use for aggregate questions like 'are all lights off?' or 'are any windows open?' "
        "instead of calling get_openhab_item_state separately for each item — pass every "
        "relevant item_name from your instructions at once and reason over the returned states."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "item_names": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Item names to check, e.g. ['GF_LivingRoom_Light', 'GF_Kitchen_Light'].",
            )
        },
        required=["item_names"],
    ),
)

SET_OPENHAB_ITEMS_STATE_FUNC = FunctionDeclaration(
    name="set_openhab_items_state",
    description=(
        "Sets the same state on multiple OpenHAB items in a single call. "
        "Use for group commands like 'turn off all lights in the living room' — pass every "
        "relevant item_name from your instructions at once instead of calling "
        "set_openhab_item_state separately for each one."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "item_names": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Item names to set, e.g. ['GF_LivingRoom_Light', 'GF_Kitchen_Light'].",
            ),
            "state": Schema(type=Type.STRING, description="The state to apply to all listed items."),
        },
        required=["item_names", "state"],
    ),
)

GET_HA_ENTITY_STATE_FUNC = FunctionDeclaration(
    name="get_ha_entity_state",
    description="Gets the state of any Home Assistant entity (sensor, light, switch, cover, media_player, binary_sensor, etc.).",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "entity_id": Schema(type=Type.STRING, description="Full HA entity ID, e.g. 'light.living_room_main' or 'sensor.temperature_bedroom'.")
        },
        required=["entity_id"],
    ),
)

SET_HA_ENTITY_STATE_FUNC = FunctionDeclaration(
    name="set_ha_entity_state",
    description=(
        "Sets the state of a Home Assistant entity by calling the appropriate service. "
        "Supported state formats per domain: "
        "light — 'ON'/'OFF', brightness '0'-'100', color 'H,S,B' (hue 0-360, sat 0-100, bri 0-100). "
        "switch/fan/input_boolean — 'ON'/'OFF'. "
        "cover — position '0'-'100' (0=closed, 100=open). "
        "media_player — 'ON'/'OFF', volume '0'-'100', 'MUTE'/'UNMUTE'."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "entity_id": Schema(type=Type.STRING, description="Full HA entity ID, e.g. 'light.living_room_main'."),
            "state": Schema(type=Type.STRING, description="Desired state value (see description for format)."),
        },
        required=["entity_id", "state"],
    ),
)

GET_HA_ENTITIES_STATE_FUNC = FunctionDeclaration(
    name="get_ha_entities_state",
    description=(
        "Gets the states of multiple Home Assistant entities in a single call. "
        "Use for aggregate questions like 'are all lights off?' or 'are any windows open?' "
        "instead of calling get_ha_entity_state separately for each entity — pass every "
        "relevant entity_id from your instructions at once and reason over the returned states."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "entity_ids": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Full HA entity IDs to check, e.g. ['light.salon_glowne', 'light.kuchnia_glowne'].",
            )
        },
        required=["entity_ids"],
    ),
)

SET_HA_ENTITIES_STATE_FUNC = FunctionDeclaration(
    name="set_ha_entities_state",
    description=(
        "Sets the same state on multiple Home Assistant entities in a single call. "
        "Use for group commands like 'turn off all lights in the living room' — pass every "
        "relevant entity_id from your instructions at once instead of calling "
        "set_ha_entity_state separately for each one. Same state formats per domain as set_ha_entity_state."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "entity_ids": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Full HA entity IDs to set, e.g. ['light.salon_glowne', 'light.salon_scienne'].",
            ),
            "state": Schema(type=Type.STRING, description="Desired state value to apply to all listed entities."),
        },
        required=["entity_ids", "state"],
    ),
)

PLAY_INTERNET_RADIO_FUNC = FunctionDeclaration(
    name="play_internet_radio",
    description="Searches for an internet radio station and plays it.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "station_name": Schema(type=Type.STRING, description="The name of the radio station.")
        },
        required=[],
    ),
)

STOP_RADIO_FUNC = FunctionDeclaration(
    name="stop_radio",
    description="Stops the radio playback.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
        required=[],
    ),
)

SET_PLAYBACK_VOLUME_FUNC = FunctionDeclaration(
    name="set_playback_volume",
    description="Sets the playback volume of the MPD player.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "volume_percentage": Schema(type=Type.INTEGER, description="The desired volume percentage (0-100).")
        },
        required=["volume_percentage"],
    ),
)

GET_RADIO_STATUS_FUNC = FunctionDeclaration(
    name="get_radio_status",
    description="Gets the current status of the radio: playback state, volume, and whether a station is in the playlist. Use ONLY when the user explicitly asks what is currently playing or for the radio status — do NOT call before play/stop commands.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
        required=[],
    ),
)

WATCH_TV_FUNC = FunctionDeclaration(
    name="watch_tv",
    description=(
        "Turns on the TV and/or switches to a channel by name. "
        "Use for: 'turn on TV', 'watch BBC One', 'switch to ITV', 'put on BBC Two'. "
        "Pass channel_name exactly as listed in your instructions. "
        "Do NOT use smarthome state functions for TV power or channel switching — always use this function."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "channel_name": Schema(
                type=Type.STRING,
                description="Channel name from the available list (optional — omit to just turn TV on).",
            )
        },
        required=[],
    ),
)

REQUEST_FOR_USER_INPUT_FUNC = FunctionDeclaration(
    name="request_for_user_input",
    description="Requests user input. Use this function when you need to ask the user a question or wait for their response.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
        required=[],
    ),
)

_COMMON_DECLARATIONS = [
    GET_CURRENT_TIME_FUNC,
    GET_CURRENT_DATE_FUNC,
    WATCH_TV_FUNC,
    PLAY_INTERNET_RADIO_FUNC,
    STOP_RADIO_FUNC,
    SET_PLAYBACK_VOLUME_FUNC,
    GET_RADIO_STATUS_FUNC,
    REQUEST_FOR_USER_INPUT_FUNC,
]

_OH_DECLARATIONS = [
    GET_OPENHAB_ITEM_STATE_FUNC,
    SET_OPENHAB_ITEM_STATE_FUNC,
    GET_OPENHAB_ITEMS_STATE_FUNC,
    SET_OPENHAB_ITEMS_STATE_FUNC,
]

_HA_DECLARATIONS = [
    GET_HA_ENTITY_STATE_FUNC,
    SET_HA_ENTITY_STATE_FUNC,
    GET_HA_ENTITIES_STATE_FUNC,
    SET_HA_ENTITIES_STATE_FUNC,
]


_ALL_DECLARATIONS = _COMMON_DECLARATIONS + (
    _HA_DECLARATIONS if _BACKEND == "home_assistant"
    else _OH_DECLARATIONS if _BACKEND == "openhab"
    else []
)

# ---------------------------------------------------------------------------
# Provider-specific tool lists (generated from declarations above)
# ---------------------------------------------------------------------------

GEMINI_TOOLS = [Tool(function_declarations=_ALL_DECLARATIONS)]

_GENAI_TYPE_MAP = {
    Type.OBJECT: "object",
    Type.STRING: "string",
    Type.INTEGER: "integer",
    Type.NUMBER: "number",
    Type.BOOLEAN: "boolean",
    Type.ARRAY: "array",
}


def _schema_to_json(schema: Schema) -> dict:
    result: dict = {"type": _GENAI_TYPE_MAP.get(schema.type, "object")}
    if getattr(schema, "description", None):
        result["description"] = schema.description
    props = getattr(schema, "properties", None)
    if props is not None:
        result["properties"] = {k: _schema_to_json(v) for k, v in props.items()}
    items = getattr(schema, "items", None)
    if items is not None:
        result["items"] = _schema_to_json(items)
    req = getattr(schema, "required", None)
    if req is not None:
        result["required"] = list(req)
    return result


def _decl_to_openai_tool(decl: FunctionDeclaration) -> dict:
    tool: dict = {"type": "function", "name": decl.name, "description": decl.description}
    if decl.parameters is not None:
        tool["parameters"] = _schema_to_json(decl.parameters)
    return tool


OPENAI_TOOLS = [_decl_to_openai_tool(d) for d in _ALL_DECLARATIONS]

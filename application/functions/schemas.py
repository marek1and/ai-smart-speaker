from google.genai.types import FunctionDeclaration, Schema, Tool, Type


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
    description="Gets the current status of the radio, including playback state, volume, and whether a radio station is currently in the playlist. This should be the first step for any radio related query.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={},
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

_ALL_DECLARATIONS = [
    GET_CURRENT_TIME_FUNC,
    GET_CURRENT_DATE_FUNC,
    GET_OPENHAB_ITEM_STATE_FUNC,
    SET_OPENHAB_ITEM_STATE_FUNC,
    PLAY_INTERNET_RADIO_FUNC,
    STOP_RADIO_FUNC,
    SET_PLAYBACK_VOLUME_FUNC,
    GET_RADIO_STATUS_FUNC,
    REQUEST_FOR_USER_INPUT_FUNC,
]

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

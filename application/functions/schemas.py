from google.genai.types import FunctionDeclaration, Schema, Tool, Type


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

GEMINI_TOOLS = [
    Tool(
        function_declarations=[
            GET_CURRENT_TIME_FUNC,
            GET_CURRENT_DATE_FUNC,
            GET_OPENHAB_ITEM_STATE_FUNC,
            SET_OPENHAB_ITEM_STATE_FUNC,
            PLAY_INTERNET_RADIO_FUNC,
            STOP_RADIO_FUNC,
            SET_PLAYBACK_VOLUME_FUNC,
            GET_RADIO_STATUS_FUNC,
        ]
    )
]

OPENAI_TOOLS = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Gets the current time.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_current_date",
        "description": "Gets the current date.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_openhab_item_state",
        "description": "Gets the state of an item in OpenHab.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "The name of the item.",
                }
            },
            "required": ["item_name"],
        },
    },
    {
        "type": "function",
        "name": "set_openhab_item_state",
        "description": "Sets the state of an item in OpenHab.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "The name of the item.",
                },
                "state": {
                    "type": "string",
                    "description": "The state to set.",
                },
            },
            "required": ["item_name", "state"],
        },
    },
    {
        "type": "function",
        "name": "play_internet_radio",
        "description": "Searches for an internet radio station and plays it.",
        "parameters": {
            "type": "object",
            "properties": {
                "station_name": {
                    "type": "string",
                    "description": "The name of the radio station.",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "stop_radio",
        "description": "Stops the radio playback.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "set_playback_volume",
        "description": "Sets the playback volume of the MPD player.",
        "parameters": {
            "type": "object",
            "properties": {
                "volume_percentage": {
                    "type": "integer",
                    "description": "The desired volume percentage (0-100).",
                }
            },
            "required": ["volume_percentage"],
        },
    },
    {
        "type": "function",
        "name": "get_radio_status",
        "description": "Gets the current status of the radio, including playback state, volume, and whether a radio station is currently in the playlist. This should be the first step for any radio related query.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

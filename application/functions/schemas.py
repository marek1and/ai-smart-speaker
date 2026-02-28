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

GEMINI_TOOLS = [
    Tool(
        function_declarations=[
            GET_CURRENT_TIME_FUNC,
            GET_CURRENT_DATE_FUNC,
            GET_OPENHAB_ITEM_STATE_FUNC,
            SET_OPENHAB_ITEM_STATE_FUNC,
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
]

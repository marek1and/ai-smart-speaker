from typing import Callable, Dict, Any

FUNCTIONS: Dict[str, Callable[..., Any]] = {}


def register_function(name: str):
    def decorator(func: Callable[..., Any]):
        FUNCTIONS[name] = func
        return func

    return decorator


def get_function(name: str | None) -> Callable[..., Any]:
    if name is None:
        raise ValueError("Function name cannot be None")
    if name not in FUNCTIONS:
        raise ValueError(f"Function '{name}' not found in registry")
    return FUNCTIONS[name]

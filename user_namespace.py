from typing import Any, Optional, cast

from appdaemon import adapi

from utils import str_to_bool


class UserNamespace:
    def __init__(self, api: adapi.ADAPI, name: str) -> None:
        self.api = api
        self.name = name

    def get_state(
        self,
        entity_id: str = None,
        attribute: str = None,
        default: Any = None,
        **kwargs: Optional[Any]
    ) -> Any:
        kwargs["namespace"] = self.name
        return self.api.get_state(entity_id, attribute, default, **kwargs)

    def set_state(self, entity_id: str, **kwargs: Optional[Any]) -> dict:
        kwargs["namespace"] = self.name
        return cast(dict, self.api.set_state(entity_id, **kwargs))

    def get_state_float(
        self,
        entity_id: str = None,
        attribute: str = None,
        default: Any = None,
        **kwargs: Optional[Any]
    ) -> Optional[float]:
        value = self.get_state(entity_id, attribute, default, **kwargs)
        if value is None:
            return default
        try:
            return float(value)
        except:
            self.api.log(
                "Failed to get float value for entity %s. Received: %s",
                entity_id,
                value,
            )
            return None

    def get_state_bool(
        self,
        entity_id: str = None,
        attribute: str = None,
        default: Any = None,
        **kwargs: Optional[Any]
    ) -> Optional[bool]:
        value = self.get_state(entity_id, attribute, default, **kwargs)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        try:
            return str_to_bool(value.lower())
        except:
            self.api.log(
                "Failed to get bool value for entity %s. Received: %s, type: %s",
                entity_id,
                value,
                type(value),
            )
            return None

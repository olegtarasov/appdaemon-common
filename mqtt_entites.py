import json
from email.policy import default

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from typing import Any, Callable, Protocol

from attr import dataclass

from event_hook import EventHook
from utils import get_state_bool, get_state_float

CH_NAMESPACE = "central_heating"


class MQTTEntityBase:
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
    ) -> None:
        self.api = api
        self.mqtt = mqtt
        self.room_code = room_code
        self.entity_code = entity_code
        self.entity_name = entity_name
        self.entity_id = f"{self.room_code}_{self.entity_code}"
        self.device_id = f"{self.room_code}_device"
        self.config_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/config"
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/state"

    @property
    def entity_type(self) -> str:
        raise Exception()

    def configure(self) -> None:
        raise Exception()

    def _mqtt_subscribe(self, handler: Callable, topic: str):
        self.mqtt.mqtt_subscribe(topic)
        self.mqtt.listen_event(handler, "MQTT_MESSAGE", topic=topic)

    # Persistence
    def _get_string_setting(self, name: str, default_value: str) -> str:
        return self.api.get_state(name, default=default_value, namespace=CH_NAMESPACE)

    def _get_float_setting(self, name: str, default_value: float) -> float:
        return get_state_float(
            self.api,
            name,
            default=default_value,
            namespace=CH_NAMESPACE,
        )

    def _get_bool_setting(self, name: str, default_value: bool) -> bool:
        return get_state_bool(
            self.api,
            name,
            default=default_value,
            namespace=CH_NAMESPACE,
        )

    def _set_setting(self, name: str, value: Any) -> None:
        self.api.set_state(name, state=value, namespace=CH_NAMESPACE)


class MQTTClimate(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        room_name: str,
        entity_code: str,
        entity_name: str,
    ) -> None:
        super().__init__(api, mqtt, room_code, entity_code, entity_name)

        self.room_name = room_name

        # Events
        self.on_mode_changed = EventHook()
        self.on_preset_changed = EventHook()
        self.on_temperature_changed = EventHook()

        # Topics
        self.mode_command_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/mode/set"
        )
        self.mode_state_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/mode/state"
        )
        self.preset_command_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/preset_mode/set"
        )
        self.preset_state_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/preset_mode/state"
        )
        self.temperature_command_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/temperature/set"
        )
        self.temperature_state_topic = (
            f"homeassistant/{self.entity_type}/{self.entity_id}/temperature/state"
        )
        self.current_temperature_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/current_temperature/state"

        # Non-persistent state
        self._current_temperature: float = 0

    @property
    def mode(self) -> str:
        return self._get_string_setting(f"{self.entity_id}.mode", "off")

    @mode.setter
    def mode(self, value: str) -> None:
        self._set_setting(f"{self.entity_id}.mode", value)
        self.mqtt.mqtt_publish(self.mode_state_topic, value)

    @property
    def preset(self) -> str:
        return self._get_string_setting(f"{self.entity_id}.preset", "home")

    @preset.setter
    def preset(self, value: str) -> None:
        self._set_setting(f"{self.entity_id}.preset", value)
        self.mqtt.mqtt_publish(self.preset_state_topic, value)

    @property
    def temperature(self) -> float:
        return self._get_float_setting(f"{self.entity_id}.temperature", 23.5)

    @temperature.setter
    def temperature(self, value: float) -> None:
        self._set_setting(f"{self.entity_id}.temperature", value)
        self.mqtt.mqtt_publish(self.temperature_state_topic, value)

    @property
    def current_temperature(self) -> float:
        return self._current_temperature

    @current_temperature.setter
    def current_temperature(self, value: float) -> None:
        self._current_temperature = value
        self.mqtt.mqtt_publish(self.current_temperature_topic, value)

    @property
    def entity_type(self) -> str:
        return "climate"

    def configure(self) -> None:
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(
                {
                    "mode_command_topic": self.mode_command_topic,
                    "mode_state_topic": self.mode_state_topic,
                    "preset_mode_command_topic": self.preset_command_topic,
                    "preset_mode_state_topic": self.preset_state_topic,
                    "temperature_command_topic": self.temperature_command_topic,
                    "temperature_state_topic": self.temperature_state_topic,
                    "current_temperature_topic": self.current_temperature_topic,
                    "unique_id": self.entity_id,
                    "modes": ["off", "heat"],
                    "preset_modes": ["home", "away", "sleep"],
                    "name": self.entity_name,
                    "device": {
                        "identifiers": [self.device_id],
                        "name": self.room_name,
                        "manufacturer": "Cats Ltd.",
                        "model": "Virtual Climate Device",
                    },
                }
            ),
        )
        self._mqtt_subscribe(self.handle_mode, self.mode_command_topic)
        self._mqtt_subscribe(self.handle_preset, self.preset_command_topic)
        self._mqtt_subscribe(self.handle_temperature, self.temperature_command_topic)

    # Handlers
    def handle_mode(self, event_name, data, cb_args):
        self.api.log(f"Climate mode changed: {data}")
        self.on_mode_changed()

    def handle_preset(self, event_name, data, cb_args):
        self.api.log(f"Climate preset changed: {data}")
        self.on_preset_changed()

    def handle_temperature(self, event_name, data, cb_args):
        self.api.log(f"Climate temperature changed: {data}")
        self.on_temperature_changed()


class MQTTNumber(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        default_value: float = 0,
        min_value: float = 0,
        max_value: float = 100,
        step: float = 0.1,
        mode: str = "box",
        icon: str = None,
        category: str = None,
    ) -> None:
        super().__init__(api, mqtt, room_code, entity_code, entity_name)

        # Config
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        self.mode = mode
        self.icon = icon
        self.category = category

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/state"

    @property
    def state(self) -> float:
        return self._get_float_setting(self.entity_id, self.default_value)

    @state.setter
    def state(self, value: float) -> None:
        self._set_setting(self.entity_id, value)
        self.mqtt.mqtt_publish(self.state_topic, value)

    @property
    def entity_type(self) -> str:
        return "number"

    def configure(self) -> None:
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(
                {
                    "platform": "number",
                    "command_topic": self.command_topic,
                    "state_topic": self.state_topic,
                    "entity_category": self.category,
                    "icon": self.icon,
                    "min": self.min_value,
                    "max": self.max_value,
                    "step": self.step,
                    "mode": self.mode,
                    "unique_id": self.entity_id,
                    "name": self.entity_name,
                    "device": {"identifiers": [self.device_id]},
                }
            ),
        )
        self._mqtt_subscribe(self.handle_state, self.command_topic)

    # Handlers
    def handle_state(self, event_name, data, cb_args):
        self.api.log(f"Number state changed: {data}")
        self.on_state_changed()


class MQTTSwitch(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        default_value: bool = False,
        icon: str = None,
        category: str = None,
    ) -> None:
        super().__init__(api, mqtt, room_code, entity_code, entity_name)

        # Config
        self.default_value = default_value
        self.icon = icon
        self.category = category

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/state"

    @property
    def state(self) -> bool:
        return self._get_bool_setting(self.entity_id, self.default_value)

    @state.setter
    def state(self, value: bool) -> None:
        self._set_setting(self.entity_id, value)
        self.mqtt.mqtt_publish(self.state_topic, value)

    @property
    def entity_type(self) -> str:
        return "switch"

    def configure(self) -> None:
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(
                {
                    "platform": "switch",
                    "command_topic": self.command_topic,
                    "state_topic": self.state_topic,
                    "entity_category": self.category,
                    "icon": self.icon,
                    "unique_id": self.entity_id,
                    "name": self.entity_name,
                    "device": {"identifiers": [self.device_id]},
                }
            ),
        )
        self._mqtt_subscribe(self.handle_state, self.command_topic)

    # Handlers
    def handle_state(self, event_name, data, cb_args):
        self.api.log(f"Switch state changed: {data}")
        self.on_state_changed()

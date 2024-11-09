import json
from email.policy import default

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from typing import Any, Callable, Iterable, Optional, Protocol
from event_hook import EventHook
from utils import get_state_bool, get_state_float, to_bool

CH_NAMESPACE = "central_heating"


class MQTTDevice:
    def __init__(
        self, device_id: str, device_name: str, device_model: str, entities: Iterable
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.device_model = device_model
        self.entities: list[MQTTEntityBase] = list(entities)

    def configure(self):
        for entity in self.entities:
            entity.configure(self)


class MQTTEntityBase:
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        icon: str,
        entity_category: str,
    ) -> None:
        self.api = api
        self.mqtt = mqtt
        self.entity_id = f"{room_code}_{entity_code}"
        self.full_entity_id = f"{self.entity_type}.{self.entity_id}"
        self.entity_name = entity_name
        self.config_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/config"

        self.icon = icon
        self.entity_category = entity_category

    @property
    def entity_type(self) -> str:
        raise Exception()

    def configure(self, device: MQTTDevice) -> None:
        raise Exception()

    def _mqtt_subscribe(self, handler: Callable, topic: str):
        self.mqtt.mqtt_subscribe(topic)
        self.mqtt.listen_event(handler, "MQTT_MESSAGE", topic=topic)

    def _add_optional_fields(self, config: dict[str, Any]):
        if self.icon is not None:
            config["icon"] = self.icon
        if self.entity_category is not None:
            config["entity_category"] = self.entity_category

    def _get_string_payload(
        self, data: dict[str, Any], default_value: Optional[str] = None
    ) -> Optional[str]:
        if not "payload" in data:
            self.api.error("No payload in MQTT data dict: %s", data)
            return default_value
        return data["payload"]

    def _get_float_payload(
        self, data: dict[str, Any], default_value: Optional[float] = None
    ) -> Optional[float]:
        if not "payload" in data:
            self.api.error("No payload in MQTT data dict: %s", data)
            return default_value
        payload = data["payload"]
        if isinstance(payload, float):
            return payload
        try:
            return float(payload)
        except:
            self.api.error("Failed to convert MQTT payload to float: %s", payload)
            return default_value

    def _get_bool_payload(
        self, data: dict[str, Any], default_value: Optional[bool] = None
    ) -> Optional[bool]:
        if not "payload" in data:
            self.api.error("No payload in MQTT data dict: %s", data)
            return default_value

        payload = data["payload"]
        if isinstance(payload, bool):
            return payload
        try:
            return to_bool(payload)
        except:
            self.api.error("Failed to convert MQTT payload to bool: %s", payload)
            return default_value


class MQTTClimate(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        has_presets=True,
        heat_only=False,
        default_temperature=23.5,
        icon: str = None,
        entity_category: str = None,
    ) -> None:
        super().__init__(
            api, mqtt, room_code, entity_code, entity_name, icon, entity_category
        )

        # Config
        self.has_presets = has_presets
        self.heat_only = heat_only
        self.default_temperature = default_temperature

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
        return self.api.get_state(
            self.full_entity_id, default="off" if not self.heat_only else "heat"
        )

    @mode.setter
    def mode(self, value: Optional[str]) -> None:
        if value is None:
            return
        self.api.set_state(self.full_entity_id, state=value, namespace=CH_NAMESPACE)
        self.mqtt.mqtt_publish(self.mode_state_topic, value)

    @property
    def preset(self) -> str:
        return self.api.get_state(self.full_entity_id, "preset", "home")

    @preset.setter
    def preset(self, value: Optional[str]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id, attributes={"preset": value}, namespace=CH_NAMESPACE
        )
        self.mqtt.mqtt_publish(self.preset_state_topic, value)

    @property
    def temperature(self) -> float:
        return get_state_float(
            self.api, self.full_entity_id, "temperature", self.default_temperature
        )

    @temperature.setter
    def temperature(self, value: Optional[float]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id,
            attributes={"temperature": value},
            namespace=CH_NAMESPACE,
        )
        self.mqtt.mqtt_publish(self.temperature_state_topic, value)

    @property
    def current_temperature(self) -> float:
        return self._current_temperature

    @current_temperature.setter
    def current_temperature(self, value: Optional[float]) -> None:
        if value is None:
            return
        self._current_temperature = value
        self.mqtt.mqtt_publish(self.current_temperature_topic, value)

    @property
    def entity_type(self) -> str:
        return "climate"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "mode_state_topic": self.mode_state_topic,
            "temperature_command_topic": self.temperature_command_topic,
            "temperature_state_topic": self.temperature_state_topic,
            "current_temperature_topic": self.current_temperature_topic,
            "precision": 0.1,
            "temp_step": 0.5,
            "unique_id": self.entity_id,
            "modes": ["heat"] if self.heat_only else ["off", "heat"],
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
        }

        if not self.heat_only:
            config["mode_command_topic"] = self.mode_command_topic

        if self.has_presets:
            config["preset_mode_command_topic"] = self.preset_command_topic
            config["preset_mode_state_topic"] = self.preset_state_topic
            config["preset_modes"] = ["home", "away", "sleep"]

        self._add_optional_fields(config)

        self.mqtt.mqtt_publish(self.config_topic, json.dumps(config))

        # Subscribe for commands
        if not self.heat_only:
            self._mqtt_subscribe(self.handle_mode, self.mode_command_topic)

        if self.has_presets:
            self._mqtt_subscribe(self.handle_preset, self.preset_command_topic)

        self._mqtt_subscribe(self.handle_temperature, self.temperature_command_topic)

        # Publish initial state
        self.mqtt.mqtt_publish(self.mode_state_topic, self.mode)

        if self.has_presets:
            self.mqtt.mqtt_publish(self.preset_state_topic, self.preset)

        self.mqtt.mqtt_publish(self.temperature_state_topic, self.temperature)
        self.mqtt.mqtt_publish(
            self.current_temperature_topic, self._current_temperature
        )

    # Handlers
    def handle_mode(self, event_name, data, cb_args):
        self.api.log("Climate %s mode changed: %s", self.entity_id, data)
        self.mode = self._get_string_payload(data)
        self.on_mode_changed()

    def handle_preset(self, event_name, data, cb_args):
        self.preset = self._get_string_payload(data)
        self.on_preset_changed()

    def handle_temperature(self, event_name, data, cb_args):
        self.temperature = self._get_float_payload(data)
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
        entity_category: str = None,
    ) -> None:
        super().__init__(
            api, mqtt, room_code, entity_code, entity_name, icon, entity_category
        )

        # Config
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        self.mode = mode

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}"

    @property
    def state(self) -> float:
        return get_state_float(
            self.api, self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[float]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id,
            state=value,
            namespace=CH_NAMESPACE,
        )
        self.mqtt.mqtt_publish(self.state_topic, value)

    @property
    def entity_type(self) -> str:
        return "number"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "number",
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "min": self.min_value,
            "max": self.max_value,
            "step": self.step,
            "mode": self.mode,
            "unique_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
        }

        self._add_optional_fields(config)

        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self._mqtt_subscribe(self.handle_state, self.command_topic)
        self.mqtt.mqtt_publish(self.state_topic, self.state)

    # Handlers
    def handle_state(self, event_name, data, cb_args):
        self.state = self._get_float_payload(data)
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
        entity_category: str = None,
    ) -> None:
        super().__init__(
            api, mqtt, room_code, entity_code, entity_name, icon, entity_category
        )

        # Config
        self.default_value = default_value

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self.entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}"

    @property
    def state(self) -> bool:
        return get_state_bool(self.api, self.full_entity_id, default=self.default_value)

    @state.setter
    def state(self, value: Optional[bool]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id,
            state="on" if value else "off",
            namespace=CH_NAMESPACE,
        )
        self.mqtt.mqtt_publish(self.state_topic, "on" if value else "off")

    @property
    def entity_type(self) -> str:
        return "switch"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "switch",
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
        }

        self._add_optional_fields(config)

        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self._mqtt_subscribe(self.handle_state, self.command_topic)
        self.mqtt.mqtt_publish(self.state_topic, self.state)

    # Handlers
    def handle_state(self, event_name, data, cb_args):
        self.state = self._get_bool_payload(data)
        self.on_state_changed()


class MQTTSensor(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        default_value: float = 0,
        icon: str = None,
        entity_category: str = None,
    ) -> None:
        super().__init__(
            api, mqtt, room_code, entity_code, entity_name, icon, entity_category
        )

        # Config
        self.default_value = default_value

        # Topics
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}"

    @property
    def state(self) -> float:
        return get_state_float(
            self.api, self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[float]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id,
            state=value,
            namespace=CH_NAMESPACE,
        )
        self.mqtt.mqtt_publish(self.state_topic, value)

    @property
    def entity_type(self) -> str:
        return "sensor"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "sensor",
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
        }

        self._add_optional_fields(config)

        self.api.log("Configuring sensor entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self.mqtt.mqtt_publish(self.state_topic, self.state)


class MQTTBinarySensor(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        entity_code: str,
        entity_name: str,
        default_value: bool = False,
        icon: str = None,
        entity_category: str = None,
    ) -> None:
        super().__init__(
            api, mqtt, room_code, entity_code, entity_name, icon, entity_category
        )

        # Config
        self.default_value = default_value

        # Topics
        self.state_topic = f"homeassistant/{self.entity_type}/{self.entity_id}"

    @property
    def state(self) -> bool:
        return get_state_bool(self.api, self.full_entity_id, default=self.default_value)

    @state.setter
    def state(self, value: Optional[bool]) -> None:
        if value is None:
            return
        self.api.set_state(
            self.full_entity_id,
            state="ON" if value else "OFF",
            namespace=CH_NAMESPACE,
        )
        self.mqtt.mqtt_publish(self.state_topic, "ON" if value else "OFF")

    @property
    def entity_type(self) -> str:
        return "binary_sensor"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "binary_sensor",
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
        }

        self._add_optional_fields(config)

        self.api.log("Configuring binary_sensor entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self.mqtt.mqtt_publish(self.state_topic, "ON" if self.state else "OFF")

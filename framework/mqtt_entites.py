import json
from typing import Any, Callable, Iterable, Optional

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi

from event_hook import EventHook
from user_namespace import UserNamespace
from utils import get_state_bool, get_state_float, str_to_bool


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
        namespace: UserNamespace,
        prefix: Optional[str],
        entity_code: str,
        entity_name: str,
        kwargs: dict[str, Any],
    ) -> None:
        self.api = api
        self.mqtt = mqtt
        self.namespace = namespace
        self.kwargs = kwargs
        self.entity_id = (
            f"{prefix}_{entity_code}" if prefix is not None else entity_code
        )
        self.full_entity_id = f"{self._entity_type}.{self.entity_id}"
        self.entity_name = entity_name
        self.config_topic = f"homeassistant/{self._entity_type}/{self.entity_id}/config"

    def configure(self, device: MQTTDevice) -> None:
        raise Exception()

    @property
    def _entity_type(self) -> str:
        raise Exception()

    def _mqtt_subscribe(self, handler: Callable, topic: str):
        self.mqtt.mqtt_subscribe(topic)
        self.mqtt.listen_event(handler, "MQTT_MESSAGE", topic=topic)

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
            return str_to_bool(payload.lower())
        except:
            self.api.error("Failed to convert MQTT payload to bool: %s", payload)
            return default_value


class MQTTClimate(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: str,
        entity_code: str,
        entity_name: str,
        has_presets=True,
        heat_only=False,
        default_temperature=23.5,
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

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
            f"homeassistant/{self._entity_type}/{self.entity_id}/mode/set"
        )
        self.mode_state_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/mode/state"
        )
        self.preset_command_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/preset_mode/set"
        )
        self.preset_state_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/preset_mode/state"
        )
        self.temperature_command_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/temperature/set"
        )
        self.temperature_state_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/temperature/state"
        )
        self.current_temperature_topic = f"homeassistant/{self._entity_type}/{self.entity_id}/current_temperature/state"

        # Non-persistent state
        self._current_temperature: float = 0

    @property
    def mode(self) -> str:
        return self.namespace.get_state(
            self.full_entity_id, default="off" if not self.heat_only else "heat"
        )

    @mode.setter
    def mode(self, value: Optional[str]) -> None:
        if value is None:
            return

        state = self.api.get_state(self.full_entity_id)
        if state == value:
            return

        self.namespace.set_state(self.full_entity_id, state=value)
        self.mqtt.mqtt_publish(self.mode_state_topic, value, retain=True)

    @property
    def preset(self) -> str:
        return self.namespace.get_state(self.full_entity_id, "preset", "home")

    @preset.setter
    def preset(self, value: Optional[str]) -> None:
        if value is None:
            return

        state = self.api.get_state(self.full_entity_id, "preset")
        if state == value:
            return

        self.namespace.set_state(self.full_entity_id, attributes={"preset": value})
        self.mqtt.mqtt_publish(self.preset_state_topic, value, retain=True)

    @property
    def temperature(self) -> float:
        return self.namespace.get_state_float(
            self.full_entity_id,
            "temperature",
            self.default_temperature,
        )

    @temperature.setter
    def temperature(self, value: Optional[float]) -> None:
        if value is None:
            return

        state = self.api.get_state(self.full_entity_id, "temperature")
        if state == value:
            return

        self.namespace.set_state(self.full_entity_id, attributes={"temperature": value})
        self.mqtt.mqtt_publish(self.temperature_state_topic, value, retain=True)

    @property
    def current_temperature(self) -> float:
        return self._current_temperature

    @current_temperature.setter
    def current_temperature(self, value: Optional[float]) -> None:
        if value is None:
            return

        if self._current_temperature == value:
            return

        self._current_temperature = value
        self.mqtt.mqtt_publish(self.current_temperature_topic, value, retain=True)

    @property
    def _entity_type(self) -> str:
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
            "object_id": self.entity_id,
            "modes": ["heat"] if self.heat_only else ["off", "heat"],
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        if not self.heat_only:
            config["mode_command_topic"] = self.mode_command_topic

        if self.has_presets:
            config["preset_mode_command_topic"] = self.preset_command_topic
            config["preset_mode_state_topic"] = self.preset_state_topic
            config["preset_modes"] = ["home", "away", "sleep"]

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(self.config_topic, json.dumps(config))

        # Subscribe for commands
        if not self.heat_only:
            self._mqtt_subscribe(self._handle_mode, self.mode_command_topic)

        if self.has_presets:
            self._mqtt_subscribe(self._handle_preset, self.preset_command_topic)

        self._mqtt_subscribe(self._handle_temperature, self.temperature_command_topic)

        # Publish initial state
        self.mqtt.mqtt_publish(self.mode_state_topic, self.mode, retain=True)

        if self.has_presets:
            self.mqtt.mqtt_publish(self.preset_state_topic, self.preset, retain=True)

        self.mqtt.mqtt_publish(
            self.temperature_state_topic, self.temperature, retain=True
        )
        self.mqtt.mqtt_publish(
            self.current_temperature_topic, self._current_temperature, retain=True
        )

    # Handlers
    def _handle_mode(self, event_name, data, cb_args):
        self.mode = self._get_string_payload(data)
        self.on_mode_changed()

    def _handle_preset(self, event_name, data, cb_args):
        self.preset = self._get_string_payload(data)
        self.on_preset_changed()

    def _handle_temperature(self, event_name, data, cb_args):
        self.temperature = self._get_float_payload(data)
        self.on_temperature_changed()


class MQTTNumber(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: str,
        entity_code: str,
        entity_name: str,
        default_value: float = 0,
        min_value: float = 0,
        max_value: float = 100,
        step: float = 0.1,
        mode: str = "box",
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

        # Config
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        self.mode = mode

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self._entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self._entity_type}/{self.entity_id}"

    @property
    def state(self) -> float:
        return self.namespace.get_state_float(
            self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[float]) -> None:
        if value is None:
            return

        state = self.api.get_state(self.full_entity_id)
        if state == value:
            return

        self.namespace.set_state(self.full_entity_id, state=value)
        self.mqtt.mqtt_publish(self.state_topic, value, retain=True)

    @property
    def _entity_type(self) -> str:
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
            "object_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self._mqtt_subscribe(self._handle_state, self.command_topic)
        self.mqtt.mqtt_publish(self.state_topic, self.state, retain=True)

    # Handlers
    def _handle_state(self, event_name, data, cb_args):
        self.state = self._get_float_payload(data)
        self.on_state_changed()


class MQTTSwitch(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: str,
        entity_code: str,
        entity_name: str,
        default_value: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

        # Config
        self.default_value = default_value

        # Events
        self.on_state_changed = EventHook()

        # Topics
        self.command_topic = f"homeassistant/{self._entity_type}/{self.entity_id}/set"
        self.state_topic = f"homeassistant/{self._entity_type}/{self.entity_id}"

    @property
    def state(self) -> bool:
        return self.namespace.get_state_bool(
            self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[bool]) -> None:
        if value is None:
            return

        state = get_state_bool(self.api, self.full_entity_id)
        if state == value:
            return

        self.api.log("Changing state of switch %s: %s", self.full_entity_id, value)
        self.namespace.set_state(self.full_entity_id, state="ON" if value else "OFF")
        self.mqtt.mqtt_publish(self.state_topic, "ON" if value else "OFF", retain=True)

    @property
    def _entity_type(self) -> str:
        return "switch"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "switch",
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "object_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self._mqtt_subscribe(self._handle_state, self.command_topic)
        self.mqtt.mqtt_publish(
            self.state_topic, "ON" if self.state else "OFF", retain=True
        )

    # Handlers
    def _handle_state(self, event_name, data, cb_args):
        self.api.log("Received data from switch %s: %s", self.full_entity_id, data)
        self.state = self._get_bool_payload(data)
        self.on_state_changed()


class MQTTSensor(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: str,
        entity_code: str,
        entity_name: str,
        default_value: float = 0,
        state_class: str = "measurement",
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

        # Config
        self.default_value = default_value
        self.state_class = state_class
        # If we are using the expire_after feature, we need to disable the behavior of checking
        # state value before sending the update. If we don't and sensor value doesn't change for more than
        # expire_after, we will get a false expiration.
        self.check_before_send = "expire_after" not in kwargs

        # Topics
        self.state_topic = f"homeassistant/{self._entity_type}/{self.entity_id}"

    @property
    def state(self) -> float:
        return self.namespace.get_state_float(
            self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[float]) -> None:
        if value is None:
            return

        if self.check_before_send:
            state = get_state_float(self.api, self.full_entity_id)
            if state == value:
                return

        self.namespace.set_state(self.full_entity_id, state=value)
        self.mqtt.mqtt_publish(self.state_topic, value, retain=True)

    @property
    def _entity_type(self) -> str:
        return "sensor"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "sensor",
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "object_id": self.entity_id,
            "name": self.entity_name,
            "state_class": self.state_class,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self.mqtt.mqtt_publish(self.state_topic, self.state, retain=True)


class MQTTBinarySensor(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: Optional[str],
        entity_code: str,
        entity_name: str,
        default_value: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

        # Config
        self.default_value = default_value

        # Topics
        self.state_topic = f"homeassistant/{self._entity_type}/{self.entity_id}"

    @property
    def state(self) -> bool:
        return self.namespace.get_state_bool(
            self.full_entity_id, default=self.default_value
        )

    @state.setter
    def state(self, value: Optional[bool]) -> None:
        if value is None:
            return

        state = get_state_bool(self.api, self.full_entity_id)
        if state == value:
            return

        self.namespace.set_state(self.full_entity_id, state="ON" if value else "OFF")
        self.mqtt.mqtt_publish(self.state_topic, "ON" if value else "OFF", retain=True)

    @property
    def _entity_type(self) -> str:
        return "binary_sensor"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "binary_sensor",
            "state_topic": self.state_topic,
            "unique_id": self.entity_id,
            "object_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self.mqtt.mqtt_publish(
            self.state_topic, "ON" if self.state else "OFF", retain=True
        )


class MQTTButton(MQTTEntityBase):
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        namespace: UserNamespace,
        prefix: Optional[str],
        entity_code: str,
        entity_name: str,
        **kwargs,
    ) -> None:
        super().__init__(api, mqtt, namespace, prefix, entity_code, entity_name, kwargs)

        # Events
        self.on_press = EventHook()

        # Topics
        self.command_topic = (
            f"homeassistant/{self._entity_type}/{self.entity_id}/command"
        )

    @property
    def _entity_type(self) -> str:
        return "button"

    def configure(self, device: MQTTDevice) -> None:
        config = {
            "platform": "button",
            "command_topic": self.command_topic,
            "unique_id": self.entity_id,
            "object_id": self.entity_id,
            "name": self.entity_name,
            "device": {
                "identifiers": [device.device_id],
                "name": device.device_name,
                "manufacturer": "Cats Ltd.",
                "model": device.device_model,
            },
            **self.kwargs,
        }

        self.api.log(f"Configuring {self._entity_type} entity %s", self.entity_id)
        self.mqtt.mqtt_publish(
            self.config_topic,
            json.dumps(config),
        )
        self._mqtt_subscribe(self._handle_press, self.command_topic)

    # Handlers
    def _handle_press(self, event_name, data, cb_args):
        self.on_press()

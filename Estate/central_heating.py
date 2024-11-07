import json
import math
from datetime import datetime, timedelta, time
from email.policy import default
from typing import Any, Optional, Tuple

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from simple_pid import PID
from utils import get_state_float, get_state_bool, to_bool, time_in_range

import appdaemon.plugins.hass.hassapi as hass
from appdaemon.appdaemon import AppDaemon

# TODO: Start pumps and open all TRVs on app shutdown or error
# TODO: Also do this when thermsostat gets disconnected
# TODO: When critical sensors are not available, open TRV and pause PID
# TODO: Disable PID for rooms where TRV is closed due to rules
# TODO: Add HA automation and sensor that disables HA PIDs when AppDaemon crashes (some kind of a deadman switch)
# BUG: TRV doesn't close according to rules
# BUG: Boiler starts to heat, but TRV is still closed due to hysteresis. Boiler and pumps should use the same hysteresis

CH_NAMESPACE = "central_heating"


# noinspection PyAttributeOutsideInit
class CentralHeating(hass.Hass):
    def initialize(self):
        mqtt: mqttapi.Mqtt = self.get_plugin_api("MQTT")
        if not mqtt.is_client_connected():
            raise Exception("MQTT not connected")

        if "pid_output_sensor" not in self.args:
            raise Exception("pid_output_sensor configuration is mandatory")

        self.therm_online = False
        self.therm_fault = False
        self.rooms: list[Room] = []
        self.pid_output_sensor: str = self.args["pid_output_sensor"]
        self.room_setpoint_output_sensor: Optional[str] = (
            self.args["room_setpoint_output_sensor"]
            if "room_setpoint_output_sensor" in self.args
            else None
        )
        self.pump_radiators: Optional[str] = (
            self.args["pump_radiators"] if "pump_radiators" in self.args else None
        )
        self.pump_floor: Optional[str] = (
            self.args["pump_floor"] if "pump_floor" in self.args else None
        )

        for item in self.args["rooms"]:
            room = Room(self, mqtt, self.control_heating, item)
            self.rooms.append(room)

        self.log("Room config:\n%s", self.rooms)

        self.run_every(self.on_control_timer, "now+1", 1)

        self.log("Initialized")

    def terminate(self):
        pass

    def control_heating(self):
        any_trv_open = False
        pid_output: float = 0
        setpoint: float = 0

        for room in filter(lambda x: x.enabled, self.rooms):
            room.control_room_temperature()
            if room.pid.auto_mode:
                pid_output = max(pid_output, room.pid_output)
            if room.trv_open:
                any_trv_open = True
            if room.pid.setpoint > setpoint:
                setpoint = room.pid.setpoint

        self.call_service(
            "virtual/set",
            entity_id=self.pid_output_sensor,
            value=pid_output,
        )
        self.call_service(
            "virtual/set",
            entity_id=self.room_setpoint_output_sensor,
            value=setpoint,
        )

        if pid_output > 0:
            self.start_pump(self.pump_floor)
            if any_trv_open:
                self.start_pump(self.pump_radiators)
        else:
            self.stop_pump(self.pump_floor)
            self.stop_pump(self.pump_radiators)

    # ========= Callbacks
    def on_control_timer(self, cb_args):
        self.control_heating()

    # ========= Processing
    def start_pump(self, pump: Optional[str]):
        if pump is None:
            return
        cur_state = self.get_state(pump)
        if cur_state != "on":
            self.call_service("switch/turn_on", entity_id=pump)

    def stop_pump(self, pump: Optional[str]):
        if pump is None:
            return
        cur_state = self.get_state(pump)
        if cur_state != "off":
            self.call_service("switch/turn_off", entity_id=pump)


class Rule:
    def __init__(self, config: dict[str, Any]):
        if "time_from" not in config or "time_to" not in config:
            raise Exception("Both time_from and time_to need to be specified")

        self.time_from = time.fromisoformat(config["time_from"])
        self.time_to = time.fromisoformat(config["time_to"])
        self.trv_open: Optional[bool] = (
            config["trv_open"] if "trv_open" in config else None
        )

    def __repr__(self):
        return f"{self.time_from}-{self.time_to}:\n" f"  TRV open: {self.trv_open}\n"


class RoomTopics:
    def __init__(self, room_code: str):
        self.room_code = room_code

        # Climate entity
        self.climate_id = f"{room_code}_climate"
        self.climate_config_topic = f"homeassistant/climate/{self.climate_id}/config"
        self.climate_mode_topic = (f"homeassistant/climate/{self.climate_id}/mode",)
        self.climate_preset_topic = (
            f"homeassistant/climate/{self.climate_id}/preset_mode",
        )
        self.climate_temperature_topic = (
            f"homeassistant/climate/{self.climate_id}/temperature",
        )
        self.climate_current_temperature_topic = (
            f"homeassistant/climate/{self.climate_id}/current_temperature",
        )

        # Floor settings
        self.floor_setpoint_id = f"{self.room_code}_floor_setpoint"
        self.floor_setpoint_config_topic = (
            f"homeassistant/number/{self.floor_setpoint_id}/config"
        )
        self.floor_setpoint_command_topic = (
            f"homeassistant/number/{self.floor_setpoint_id}"
        )

        # PID config
        self.pid_kp_id = f"{self.room_code}_pid_kp"
        self.pid_ki_id = f"{self.room_code}_pid_ki"
        self.pid_kp_config_topic = f"homeassistant/number/{self.pid_kp_id}/config"
        self.pid_ki_config_topic = f"homeassistant/number/{self.pid_ki_id}/config"
        self.pid_kp_command_topic = f"homeassistant/number/{self.pid_kp_id}"
        self.pid_ki_command_topic = f"homeassistant/number/{self.pid_ki_id}"


class RoomDevices:
    def __init__(self, config: dict[str, Any]):
        if "room_temp_sensor" not in config:
            raise Exception("Room temperature sensor")

        self.room_temp_sensor = config["room_temp_sensor"]
        self.floor_temp_sensor = (
            config["floor_temp_sensor"] if "floor_temp_sensor" in config else None
        )
        self.trv = config["trv"] if "trv" in config else None
        self.window_sensor = (
            config["window_sensor"] if "window_sensor" in config else None
        )


class RoomSettings:
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        room_code: str,
        config: dict[str, Any],
    ):
        self.api = api
        self.mqtt = mqtt
        self.room_code = room_code

        self.room_setpoint = get_state_float(
            self.api,
            f"{self.room_code}.room_setpoint",
            default=23.5,
            namespace=CH_NAMESPACE,
        )
        self.floor_setpoint = get_state_float(
            self.api,
            f"{self.room_code}.floor_setpoint",
            default=28,
            namespace=CH_NAMESPACE,
        )
        self.pid_kp = get_state_float(
            self.api, f"{self.room_code}.pid_kp", default=0.5, namespace=CH_NAMESPACE
        )
        self.pid_ki = get_state_float(
            self.api, f"{self.room_code}.pid_ki", default=0.001, namespace=CH_NAMESPACE
        )
        self.rules: list[Rule] = (
            [Rule(item) for item in config["rules"]] if "rules" in config else []
        )


class Room:
    def __init__(
        self,
        api: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        control_heating_callback,
        config: dict[str, Any],
    ):
        self.api = api
        self.mqtt = mqtt
        self.control_heating_callback = control_heating_callback

        if "name" not in config:
            raise Exception("Room name is mandatory")
        if "code" not in config:
            raise Exception("Room code is mandatory")

        self.name = config["name"]
        self.code = config["code"]

        self.devices = RoomDevices(config)
        self.topics = RoomTopics(self.code)
        self.settings = RoomSettings(self.code, config)

        # State
        self.window_open = False
        self.trv_open = True
        self.window_warmup_time: Optional[datetime] = None
        self.pid_output: float = 0
        self.enabled: bool = (
            get_state_bool(self.api, f"switch.{self.sensor_prefix}_enable_thermostat")
            or False
        )

        # PID
        self.pid = PID(
            self.pid_config[0],
            self.pid_config[1],
            self.pid_config[2],
            room_setpoint,
            1,
            (-1, 1),
        )

        # State callbacks
        self.api.listen_state(
            self.on_enabled, f"switch.{self.sensor_prefix}_enable_thermostat"
        )
        self.api.listen_state(
            self.on_room_setpoint, f"number.{self.sensor_prefix}_room_setpoint"
        )
        self.api.listen_state(self.on_room_temp, self.room_temp_sensor)
        if self.floor_temp_sensor is not None:
            self.api.listen_state(self.on_floor_temp, self.floor_temp_sensor)
        if self.window_sensor is not None:
            self.api.listen_state(self.on_window, self.window_sensor)

        # Create MQTT entities
        self.configure_mqtt()

    def control_room_temperature(self):
        pid_active_window = self.process_window_state()
        pid_active_rules = self.process_rules()
        self.update_pid(pid_active_window, pid_active_rules)
        self.update_trv()

    def process_window_state(self) -> bool:
        """
        Decides whether PID should be active based on whether window is open or closed
        :return: True is PID needs to be active
        """
        window_open = get_state_bool(self.api, self.window_sensor) or False

        if self.window_open == window_open:  # There was no change
            if (
                not window_open
                and self.window_warmup_time is not None
                and self.api.get_now() >= self.window_warmup_time
            ):
                # Window is closed and it stayed closed for warmup time after being open
                # We enable PID once again setting integral term to equal last output
                self.window_warmup_time = None

        else:  # Window state changed
            self.window_open = window_open
            self.report_window()

            if window_open:
                # If the window got opened, we stop the PID and reset warmup time
                self.window_warmup_time = None
            else:
                # If the window got closed, we calculate warmup time after which we should restart PID
                self.window_warmup_time = self.api.get_now() + timedelta(minutes=10)

        return not window_open and self.window_warmup_time is None

    def process_rules(self) -> bool:
        """
        Decides whether PID should be active based on rules. If any rule specifies that TRV should be closed
        at this time, disable room PID.
        :return: True if PID should be active
        """

        cur_time: datetime = self.api.get_now()
        # Process TRV rules if they exist
        for rule in self.rules:
            if rule.trv_open is not None and time_in_range(
                cur_time.time(), rule.time_from, rule.time_to
            ):
                if not rule.trv_open:
                    return False

        return True

    def update_pid(self, pid_active_window: bool, pid_active_rules: bool):
        if not pid_active_window or not pid_active_rules:  # PID should be disabled
            self.stop_pid()
        else:  # PID should be enabled
            self.start_pid()

        if not self.pid.auto_mode:
            return

        cur_temp = get_state_float(self.api, self.room_temp_sensor)
        if cur_temp is None:
            self.api.log("Failed to get room temperature for room %s", self.name)
            return

        self.pid_output = self.pid(cur_temp)
        self.report_float_sensor("pid_output", value=round(self.pid_output * 100, 2))
        self.report_float_sensor(
            "pid_proportional", value=round(self.pid.components[0] * 100, 2)
        )
        self.report_float_sensor(
            "pid_integral", value=round(self.pid.components[1] * 100, 2)
        )
        self.report_pid_status()
        self.mqtt.mqtt_publish(
            f"homeassistant/climate/{self.sensor_prefix}_climate/current_temperature/state",
            cur_temp,
        )

    def update_trv(self):
        if self.trv is None:
            return

        self.trv_open = self.api.get_state(self.trv) == "heat"
        self.report_trv()
        cur_time: datetime = self.api.get_now()

        # We need these margins so that TRVs don't jitter back and forth when PIDs are fluctuating around zero.
        if self.pid_output > 0.01:
            if not self.trv_open:
                self.open_trv()
        elif self.pid_output < -0.01:
            if self.trv_open:
                self.close_trv()

    def start_pid(self):
        if self.pid.auto_mode:
            return

        self.pid.set_auto_mode(True, self.pid_output)
        self.report_pid_status()

    def stop_pid(self):
        if not self.pid.auto_mode:
            return

        self.pid.set_auto_mode(False)
        self.report_pid_status()

    def open_trv(self):
        if not self.trv:
            return
        self.api.call_service(
            "climate/set_hvac_mode", entity_id=self.trv, hvac_mode="heat"
        )

    def close_trv(self):
        if not self.trv:
            return
        self.api.call_service(
            "climate/set_hvac_mode", entity_id=self.trv, hvac_mode="off"
        )

    # ========= Callbacks
    def on_enabled(self, entity, attribute, old: str, new: str, cb_args):
        self.control_heating_callback()

    def on_room_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.control_heating_callback()

    def on_floor_temp(self, entity, attribute, old: str, new: str, cb_args):
        pass

    def on_window(self, entity, attribute, old: str, new: str, cb_args):
        self.process_window_state()

    def on_room_setpoint(self, entity, attribute, old: str, new: str, cb_args):
        self.api.log("Room setpoint changed for room %s: %s", self.name, new)
        self.pid.setpoint = float(new)

    def on_climate_setpoint(self, event_name, data, cb_args):
        self.api.log("Setpoint changed!")
        self.api.log(type(data))
        self.api.log(data)

    # ========= Report sensors
    def report_pid_status(self):
        self.report_binary_sensor("pid_active", self.pid.auto_mode)

    def report_window(self):
        self.report_binary_sensor("window_open", self.window_open)

    def report_trv(self):
        self.report_binary_sensor("trv_open", self.trv_open)

    def report_binary_sensor(self, name_no_prefix: str, value: Optional[bool]):
        if value is None:
            return
        if value:
            self.api.call_service(
                "virtual/turn_on",
                entity_id=f"binary_sensor.{self.sensor_prefix}_{name_no_prefix}",
            )
        else:
            self.api.call_service(
                "virtual/turn_off",
                entity_id=f"binary_sensor.{self.sensor_prefix}_{name_no_prefix}",
            )

    def report_float_sensor(self, name_no_prefix: str, value: Optional[float]):
        if value is not None:
            self.api.call_service(
                "virtual/set",
                entity_id=f"sensor.{self.sensor_prefix}_{name_no_prefix}",
                value=value,
            )

    # ============ MQTT stuff
    def configure_mqtt(self):
        # Configure climate
        self.mqtt.mqtt_publish(
            self.topics.climate_config_topic,
            json.dumps(
                {
                    "mode_command_topic": self.topics.climate_mode_topic,
                    "preset_mode_command_topic": self.topics.climate_preset_topic,
                    "temperature_command_topic": self.topics.climate_temperature_topic,
                    "current_temperature_topic": self.topics.climate_current_temperature_topic,
                    "unique_id": self.topics.climate_id,
                    "modes": ["off", "heat"],
                    "preset_modes": ["home", "away", "sleep"],
                    "name": "Climate",
                    "device": {
                        "identifiers": [f"{self.topics.climate_id}_device"],
                        "name": self.name,
                        "manufacturer": "Cats Ltd.",
                        "model": "Virtual Climate Device",
                    },
                }
            ),
        )

        # Configure PID settings numbers
        self.mqtt.mqtt_publish(
            self.topics.pid_kp_config_topic,
            json.dumps(
                {
                    "platform": "number",
                    "command_topic": self.topics.pid_kp_command_topic,
                    "entity_category": "config",
                    "icon": "mdi:knob",
                    "min": "0.000001",
                    "max": "1000000",
                    "step": "0.001",
                    "mode": "box",
                    "unique_id": self.topics.pid_kp_id,
                    "name": "PID kp",
                    "device": {"identifiers": [f"{self.topics.climate_id}_device"]},
                }
            ),
        )
        self.mqtt.mqtt_publish(
            self.topics.pid_ki_config_topic,
            json.dumps(
                {
                    "platform": "number",
                    "command_topic": self.topics.pid_ki_command_topic,
                    "entity_category": "config",
                    "icon": "mdi:knob",
                    "min": "0.000001",
                    "max": "1000000",
                    "step": "0.001",
                    "mode": "box",
                    "unique_id": self.topics.pid_ki_id,
                    "name": "PID ki",
                    "device": {"identifiers": [f"{self.topics.climate_id}_device"]},
                }
            ),
        )

        # Configure floor setpoint number
        self.mqtt.mqtt_publish(
            self.topics.floor_setpoint_config_topic,
            json.dumps(
                {
                    "platform": "number",
                    "command_topic": self.topics.floor_setpoint_command_topic,
                    "entity_category": "config",
                    "icon": "mdi:thermometer",
                    "min": "15",
                    "max": "35",
                    "step": "0.5",
                    "mode": "box",
                    "unique_id": self.topics.floor_setpoint_id,
                    "name": "PID kp",
                    "device": {"identifiers": [f"{self.topics.climate_id}_device"]},
                }
            ),
        )

        # self.mqtt.mqtt_subscribe(f"homeassistant/climate/{climate_id}/temperature/set")
        # self.mqtt.listen_event(
        #     self.on_climate_setpoint,
        #     "MQTT_MESSAGE",
        #     topic=f"homeassistant/climate/{climate_id}/temperature/set",
        # )

    def __repr__(self):
        return (
            f"{self.name}:\n"
            f"  Room temperature sensor: {self.room_temp_sensor}\n"
            f"  Floor temperture sensor: {self.floor_temp_sensor}\n"
            f"  TRV: {self.trv}\n"
            f"  Window sensor: {self.window_sensor}\n"
            f"  Room setpoint: {self.pid.setpoint}\n"
            f"  Floor setpoint: {self.floor_setpoint}\n"
            f"  PID config: {self.pid_config}\n"
            f"  Rules: {self.rules}"
        )

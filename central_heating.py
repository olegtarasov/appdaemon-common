import json
import math
from datetime import datetime, timedelta, time
from email.policy import default
from typing import Any, Optional, Tuple

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from simple_pid import PID

from event_hook import EventHook
from mqtt_entites import MQTTSwitch, MQTTClimate, MQTTNumber
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


class RoomDevices:
    def __init__(self, api: adapi.ADAPI, config: dict[str, Any]):
        self.api = api

        # Events
        self.on_room_temp_changed = EventHook()
        self.on_floor_temp_changed = EventHook()
        self.on_window_changed = EventHook()

        # Config
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

        # Subscribe to state changes
        self.api.listen_state(self.on_room_temp, self.room_temp_sensor)
        if self.floor_temp_sensor is not None:
            self.api.listen_state(self.on_floor_temp, self.floor_temp_sensor)
        if self.window_sensor is not None:
            self.api.listen_state(self.on_window, self.window_sensor)

    @property
    def room_temp(self) -> Optional[float]:
        return get_state_float(self.api, self.room_temp_sensor)

    @property
    def floor_temp(self) -> Optional[float]:
        return get_state_float(self.api, self.floor_temp_sensor)

    @property
    def window_open(self) -> Optional[bool]:
        return get_state_bool(self.api, self.window_sensor)

    @property
    def trv_open(self) -> bool:
        return self.api.get_state(self.trv) == "heat"

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

    def on_room_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.on_room_temp_changed()

    def on_floor_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.on_floor_temp_changed()

    def on_window(self, entity, attribute, old: str, new: str, cb_args):
        self.on_window_changed()


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

        # Room sensors
        self.devices = RoomDevices(self.api, config)
        self.devices.on_room_temp_changed += self.on_room_temp_changed
        self.devices.on_floor_temp_changed += self.on_floor_temp_changed
        self.devices.on_window_changed += self.on_window_changed

        # MQTT entities
        self.climate = MQTTClimate(
            api, mqtt, self.code, self.name, "climate", "Climate"
        )
        self.floor_setpoint = MQTTNumber(
            api,
            mqtt,
            self.code,
            "floor_setpoint",
            "Floor setpoint",
            28,
            15,
            32,
            0.5,
            icon="mdi:thermometer",
            category="config",
        )
        self.pid_kp = MQTTNumber(
            api,
            mqtt,
            self.code,
            "pid_kp",
            "PID kp",
            0.5,
            0.000001,
            1000000,
            0.01,
            icon="mdi:knob",
            category="config",
        )
        self.pid_ki = MQTTNumber(
            api,
            mqtt,
            self.code,
            "pid_ki",
            "PID ki",
            0.001,
            0.000001,
            1000000,
            0.01,
            icon="mdi:knob",
            category="config",
        )
        self.enable = MQTTSwitch(
            api, mqtt, self.code, "enable", "Enable", True, "mdi:toggle-switch-variant"
        )

        self.climate.on_mode_changed += self.on_mode_changed
        self.climate.on_preset_changed += self.on_preset_changed
        self.climate.on_temperature_changed += self.on_temperature_changed
        self.pid_kp.on_state_changed += self.on_pid_kp_changed
        self.pid_ki.on_state_changed += self.on_pid_ki_changed
        self.enable.on_state_changed += self.on_enable_changed

        # Confgure MQTT entities
        self.climate.configure()
        self.floor_setpoint.configure()
        self.pid_kp.configure()
        self.pid_ki.configure()
        self.enable.configure()

        # Will migrate rules later
        self.rules: list[Rule] = (
            [Rule(item) for item in config["rules"]] if "rules" in config else []
        )

        # State
        self.last_window_open = False
        self.window_warmup_time: Optional[datetime] = None
        self.pid_output: float = 0

        # PID
        self.pid = PID(
            self.pid_kp.state,
            self.pid_ki.state,
            0,
            self.climate.temperature,
            1,
            (-1, 1),
        )

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
        window_open = self.devices.window_open or False

        if self.last_window_open == window_open:  # There was no change
            if (
                not window_open
                and self.window_warmup_time is not None
                and self.api.get_now() >= self.window_warmup_time
            ):
                # Window is closed and it stayed closed for warmup time after being open
                # We enable PID once again setting integral term to equal last output
                self.window_warmup_time = None

        else:  # Window state changed
            self.last_window_open = window_open

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

        cur_temp = self.devices.room_temp
        if cur_temp is None:
            self.api.log("Failed to get room temperature for room %s", self.name)
            return

        self.pid_output = self.pid(cur_temp)

    def update_trv(self):
        if self.devices.trv is None:
            return

        trv_open = self.devices.trv_open
        cur_time: datetime = self.api.get_now()

        # We need these margins so that TRVs don't jitter back and forth when PIDs are fluctuating around zero.
        if self.pid_output > 0.01:
            if not trv_open:
                self.devices.open_trv()
        elif self.pid_output < -0.01:
            if trv_open:
                self.devices.close_trv()

    def start_pid(self):
        if self.pid.auto_mode:
            return

        self.pid.set_auto_mode(True, self.pid_output)

    def stop_pid(self):
        if not self.pid.auto_mode:
            return

        self.pid.set_auto_mode(False)

    # New callbacks
    # MQTT entites
    def on_mode_changed(self):
        pass

    def on_preset_changed(self):
        pass

    def on_temperature_changed(self):
        pass

    def on_floor_setpoint_changed(self):
        pass

    def on_pid_kp_changed(self):
        pass

    def on_pid_ki_changed(self):
        pass

    def on_enable_changed(self):
        pass

    # Room sensors
    def on_room_temp_changed(self):
        self.control_heating_callback()

    def on_floor_temp_changed(self):
        pass

    def on_window_changed(self):
        self.process_window_state()

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

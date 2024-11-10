from datetime import datetime, timedelta
from typing import Any, Callable, Optional, TypeVar, cast

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
import appdaemon.plugins.hass.hassapi as hass
from simple_pid import PID

from event_hook import EventHook
from mqtt_entites import (
    MQTTBinarySensor,
    MQTTClimate,
    MQTTDevice,
    MQTTEntityBase,
    MQTTNumber,
    MQTTSensor,
)
from utils import get_state_bool, get_state_float


# TODO: Start pumps and open all TRVs on app shutdown or error
# TODO: Also do this when thermsostat gets disconnected
# TODO: When critical sensors are not available, open TRV and pause PID
# TODO: Disable PID for rooms where TRV is closed due to rules
# TODO: Add HA automation and sensor that disables HA PIDs when AppDaemon crashes (some kind of a deadman switch)
# BUG: TRV doesn't close according to rules
# BUG: Boiler starts to heat, but TRV is still closed due to hysteresis. Boiler and pumps should use the same hysteresis


# noinspection PyAttributeOutsideInit
class CentralHeating(hass.Hass):
    def initialize(self):
        mqtt: mqttapi.Mqtt = cast(mqttapi.Mqtt, self.get_plugin_api("MQTT"))
        if not mqtt.is_client_connected():
            raise Exception("MQTT not connected")

        # Config
        self.pump_radiators: Optional[str] = (
            self.args["pump_radiators"] if "pump_radiators" in self.args else None
        )
        self.pump_floor: Optional[str] = (
            self.args["pump_floor"] if "pump_floor" in self.args else None
        )

        # Init rooms
        self.rooms: list[Room] = []
        for item in self.args["rooms"]:
            room = Room(self, mqtt, self.control_heating, item)
            self.rooms.append(room)

        self.log("Room config:\n%s", self.rooms)

        # Master thermostat MQTT device
        self.master_room_climate = MQTTClimate(
            self, mqtt, "master", "room_climate", "Room", False
        )
        self.master_pid_output = MQTTSensor(
            self,
            mqtt,
            "master",
            "pid_output",
            "PID Output",
            icon="mdi:gauge",
            entity_category="diagnostic",
        )
        self.master_device = MQTTDevice(
            "master_thermostat",
            "Master Thermostat",
            "Virtual Room Thermostat",
            [self.master_room_climate, self.master_pid_output],
        )

        # Subscribe to HA MQTT status message and run room and master configuration
        mqtt.mqtt_subscribe("homeassistant/status")
        mqtt.listen_event(
            self._handle_ha_mqtt_start, "MQTT_MESSAGE", topic="homeassistant/status"
        )

        self.configure()

        # noinspection PyTypeChecker
        self.run_every(self.on_control_timer, "now+1", 1)

        self.log("Initialized")

    def terminate(self):
        pass

    def control_heating(self):
        pid_output, any_trv_open = self.update_state()
        if pid_output > 0:
            self.start_pump(self.pump_floor)
            if any_trv_open:
                self.start_pump(self.pump_radiators)
        else:
            self.stop_pump(self.pump_floor)
            self.stop_pump(self.pump_radiators)

    def update_state(self) -> (float, bool):
        room_temp = 100
        room_setpoint = 100
        pid_output = 0
        any_climate_on = False
        any_trv_open = False

        for room in self.rooms:
            room.control_room_temperature()

            room_temp = min(room_temp, room.room_climate.room_temp)
            room_setpoint = min(room_setpoint, room.room_climate.climate.temperature)
            pid_output = max(pid_output, room.room_climate.hinged_output)
            any_climate_on = any_climate_on or room.room_climate.climate.mode
            any_trv_open = any_trv_open or (
                room.trv.trv_open if room.trv is not None else True
            )

        self.master_room_climate.mode = "heat" if any_climate_on else "off"
        self.master_room_climate.temperature = room_setpoint
        self.master_room_climate.current_temperature = room_temp
        self.master_pid_output.state = pid_output

        return pid_output, any_trv_open

    def configure(self):
        for room in self.rooms:
            room.configure()

        self.master_device.configure()

    def _handle_ha_mqtt_start(self, event_name, data, cb_args):
        if not "payload" in data:
            self.error("No payload in MQTT data dict: %s", data)
            return

        if data["payload"] == "online":
            self.log(
                "HA MQTT integration restarted, resend discovery and initial state"
            )
            self.configure()

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


class RoomConfig:
    def __init__(self, config: dict[str, Any]):
        if "name" not in config:
            raise Exception("Room name is mandatory")
        if "code" not in config:
            raise Exception("Room code is mandatory")
        if "room_temp_sensor" not in config:
            raise Exception("Room temperature sensor")

        self.room_name = config["name"]
        self.room_code = config["code"]
        self.room_temp_sensor = config["room_temp_sensor"]
        self.floor_temp_sensor = (
            config["floor_temp_sensor"] if "floor_temp_sensor" in config else None
        )

        self.trvs: Optional[list[str]] = None
        if "trv" in config["trv"]:
            self.trvs = (
                config["trv"] if isinstance(config["trv"], list) else [config["trv"]]
            )

        self.window_sensors: Optional[list[str]] = None
        if "window_sensor" in config:
            self.window_sensors = (
                config["window_sensor"]
                if isinstance(config["window_sensor"], list)
                else [config["window_sensor"]]
            )


class EntityBase:
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        self.api = api
        self.mqtt = mqtt
        self.config = config

        self._entities: list[MQTTEntityBase] = []

    @property
    def mqtt_entities(self) -> list[MQTTEntityBase]:
        return self._entities

    TMQTTEntity = TypeVar("TMQTTEntity", bound=MQTTEntityBase)

    def register_mqtt_entity(self, entity: TMQTTEntity) -> TMQTTEntity:
        self._entities.append(entity)
        return entity

    def report_state(self) -> None:
        pass


class Window(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        if config.window_sensors is None:
            raise Exception("Window sensors are not configured")

        super().__init__(api, mqtt, config)

        # Events
        self.on_window_changed = EventHook()

        # MQTT
        self.window_open_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                config.room_code,
                "window_open",
                "Window",
                entity_category="diagnostic",
                device_class="window",
            )
        )

        # Subscribe
        for sensor in config.window_sensors:
            self.api.listen_state(self._on_window, sensor)

        # State
        self._last_open = False
        self._warmup_time: Optional[datetime] = None

    @property
    def window_open(self) -> bool:
        result = False
        for sensor in self.config.window_sensors:
            result = result or (get_state_bool(self.api, sensor) or False)

        return result

    def should_heat(self) -> bool:
        """
        Decides whether PID should be active based on whether window is open or closed
        :return: True is PID needs to be active
        """
        window_open = self.window_open

        if self._last_open == window_open:  # There was no change
            if (
                not window_open
                and self._warmup_time is not None
                and cast(timedelta, self.api.get_now()) >= self._warmup_time
            ):
                # Window is closed and it stayed closed for warmup time after being open
                # We enable PID once again setting integral term to equal last output
                self._warmup_time = None

        else:  # Window state changed
            self._last_open = window_open
            self.report_state()
            self.on_window_changed()

            if window_open:
                # If the window got opened, we stop the PID and reset warmup time
                self._warmup_time = None
            else:
                # If the window got closed, we calculate warmup time after which we should restart PID
                self._warmup_time = cast(timedelta, self.api.get_now()) + timedelta(
                    minutes=10
                )

        return not window_open and self._warmup_time is None

    def report_state(self) -> None:
        self.window_open_sensor.state = self.window_open

    # Handlers
    def _on_window(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()
        self.should_heat()
        self.on_window_changed()


class TRV(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        if config.trvs is None:
            raise Exception("TRV not configured")

        super().__init__(api, mqtt, config)

        # Events
        self.on_trv = EventHook()

        # MQTT
        self.trv_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                config.room_code,
                "trv_open",
                "TRV",
                True,
                "mdi:pipe-valve",
                "diagnostic",
                "opening",
            )
        )

        # Subscribe
        for trv in config.trvs:
            self.api.listen_state(self._on_trv, trv)

    @property
    def trv_open(self) -> bool:
        result = False
        for trv in self.config.trvs:
            result = result or self.api.get_state(trv) == "heat"

        return result

    def operate_trv(self, pid_output: float):
        if pid_output > 0:
            self.open_trvs()
        else:
            self.close_trvs()

        self.report_state()

    def open_trvs(self):
        for trv in self.config.trvs:
            self.api.call_service(
                "climate/set_hvac_mode", entity_id=trv, hvac_mode="heat"
            )

    def close_trvs(self):
        for trv in self.config.trvs:
            self.api.call_service(
                "climate/set_hvac_mode", entity_id=trv, hvac_mode="off"
            )

    def report_state(self) -> None:
        self.trv_sensor.state = self.trv_open

    # Handlers
    def _on_trv(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()
        self.on_trv()


class FloorClimate(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        if config.floor_temp_sensor is None:
            raise Exception("Floor temperature sensor not configured")

        super().__init__(api, mqtt, config)

        # Events
        self.on_floor_temp = EventHook()

        # MQTT
        self.floor_climate = self.register_mqtt_entity(
            MQTTClimate(
                api, mqtt, config.room_code, "floor_climate", "Floor", False, True, 28
            )
        )

        # Subscribe
        self.api.listen_state(self._on_floor_temp, self.config.floor_temp_sensor)

    @property
    def floor_temp(self) -> Optional[float]:
        return get_state_float(self.api, self.config.floor_temp_sensor)

    def report_state(self) -> None:
        self.floor_climate.current_temperature = self.floor_temp

    def _on_floor_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()
        self.on_floor_temp()


class PIDClimate(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        super().__init__(api, mqtt, config)

        self.api = api
        self.mqtt = mqtt
        self.config = config

        # Events
        self.on_room_temp = EventHook()

        # MQTT
        self.climate = self.register_mqtt_entity(
            MQTTClimate(api, mqtt, self.config.room_code, "room_climate", "Room")
        )
        self.pid_kp = self.register_mqtt_entity(
            MQTTNumber(
                api,
                mqtt,
                self.config.room_code,
                "pid_kp",
                "PID kp",
                0.5,
                0.000001,
                100000,
                0.001,
                icon="mdi:knob",
                entity_category="config",
            )
        )
        self.pid_ki = self.register_mqtt_entity(
            MQTTNumber(
                api,
                mqtt,
                self.config.room_code,
                "pid_ki",
                "PID ki",
                0.001,
                0.000001,
                100000,
                0.001,
                icon="mdi:knob",
                entity_category="config",
            )
        )
        self.pid_active_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                self.config.room_code,
                "pid_active",
                "PID active",
                entity_category="diagnostic",
                device_class="running"
            )
        )
        self.pid_proportional_sensor = self.register_mqtt_entity(
            MQTTSensor(
                api,
                mqtt,
                self.config.room_code,
                "pid_proportional",
                "PID Proportional",
                icon="mdi:gauge",
                entity_category="diagnostic",
            )
        )
        self.pid_integral_sensor = self.register_mqtt_entity(
            MQTTSensor(
                api,
                mqtt,
                self.config.room_code,
                "pid_integral",
                "PID Integral",
                icon="mdi:gauge",
                entity_category="diagnostic",
            )
        )
        self.pid_output_sensor = self.register_mqtt_entity(
            MQTTSensor(
                api,
                mqtt,
                self.config.room_code,
                "pid_output",
                "PID Output",
                icon="mdi:gauge",
                entity_category="diagnostic",
            )
        )

        # Events
        self.climate.on_mode_changed += self._on_room_mode_changed
        self.climate.on_temperature_changed += self._on_room_setpoint_changed
        self.pid_kp.on_state_changed += self._on_pid_kp_changed
        self.pid_ki.on_state_changed += self._on_pid_ki_changed

        # Private
        self._pid = PID(
            self.pid_kp.state,
            self.pid_ki.state,
            0,
            self.climate.temperature,
            1,
            (-1, 1),
        )
        self._output = 0
        self._pid_enablers: list[Callable[[], bool]] = [self._room_climate_enabled]

        # Subscribe to sensors
        self.api.listen_state(self._on_room_temp, self.config.floor_temp_sensor)

    @property
    def room_temp(self) -> Optional[float]:
        return get_state_float(self.api, self.config.room_temp_sensor)

    @property
    def hinged_output(self) -> float:
        return self._output if self._output > 0.01 or self._output < -0.01 else 0

    @property
    def enabled(self) -> bool:
        return self._pid.auto_mode

    def register_pid_enabler(self, enabler: Callable[[], bool]):
        self._pid_enablers.append(enabler)

    def claculate_output(self):
        # Check whether PID is enabled, just in case state change got missed
        self._recalculate_pid_enabled()

        if not self.enabled:
            return

        cur_temp = self.room_temp
        if cur_temp is None:
            self.api.log(
                "Failed to get room temperature for room %s", self.config.room_name
            )
            return

        self._output = self._pid(cur_temp)
        self.report_state()

    def report_state(self):
        self.pid_active_sensor.state = self._pid.auto_mode
        self.pid_proportional_sensor.state = self._pid.components[0]
        self.pid_integral_sensor.state = self._pid.components[1]
        self.pid_output_sensor.state = self._output
        self.climate.current_temperature = self.room_temp

    def _recalculate_pid_enabled(self):
        result = True
        for enabler in self._pid_enablers:
            result = result and enabler()

        self._set_pid_enabled(result)

    def _room_climate_enabled(self):
        return self.climate.mode == "heat"

    def _set_pid_enabled(self, value: bool) -> None:
        if self._pid.auto_mode == value:
            return
        if value:
            self._pid.set_auto_mode(True, self._output)
        else:
            self._pid.set_auto_mode(False)
            self._output = 0

        self.report_state()

    # Handlers
    def _on_room_mode_changed(self):
        self._recalculate_pid_enabled()

    def _on_room_setpoint_changed(self):
        self._pid.setpoint = self.climate.temperature

    def _on_pid_kp_changed(self):
        self._pid.Kp = self.pid_kp.state

    def _on_pid_ki_changed(self):
        self._pid.Ki = self.pid_ki.state

    def _on_room_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.climate.current_temperature = self.room_temp
        self.on_room_temp()


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
        self.config = RoomConfig(config)

        self.entities: list[EntityBase] = []

        # Create room entities based on config
        self.room_climate = self._add_entity(PIDClimate(api, mqtt, self.config))
        self.floor_climate = self._add_entity(
            FloorClimate(api, mqtt, self.config)
            if self.config.floor_temp_sensor is not None
            else None
        )
        self.window = self._add_entity(
            Window(api, mqtt, self.config)
            if self.config.window_sensors is not None
            else None
        )
        self.trv = self._add_entity(
            TRV(api, mqtt, self.config) if self.config.trvs is not None else None
        )

        # Entity-specific config
        if self.window is not None:
            self.room_climate.register_pid_enabler(self.window.should_heat)

        # Collect MQTT entities and perform entity-specific configuration
        entities_mqtt = []
        for entity in self.entities:
            entities_mqtt.extend(entity.mqtt_entities)

        # Create MQTT room device
        self.room_device = MQTTDevice(
            f"{self.config.room_code}_device",
            self.config.room_name,
            "Virtual Room Thermostat",
            entities_mqtt,
        )

    def control_room_temperature(self):
        self.room_climate.claculate_output()
        if self.trv is not None:
            self.trv.operate_trv(self.room_climate.hinged_output)
            self.trv.report_state()

    def configure(self):
        self.room_device.configure()
        for entity in self.entities:
            entity.report_state()

    TEntity = TypeVar("TEntity", bound=EntityBase)

    def _add_entity(self, entity: Optional[TEntity]) -> Optional[TEntity]:
        if entity is None:
            return None
        self.entities.append(entity)
        return entity

    def __repr__(self):
        return f"{self.config.room_name}:\n"

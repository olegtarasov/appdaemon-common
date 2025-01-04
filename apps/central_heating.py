from datetime import datetime, timedelta
from typing import Any, Callable, Optional, TypeVar, cast
import traceback

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
import appdaemon.plugins.hass.hassapi as hass

from common.framework.event_hook import EventHook
from common.framework.mqtt_entites import (
    MQTTBinarySensor,
    MQTTButton,
    MQTTClimate,
    MQTTDevice,
    MQTTEntityBase,
    MQTTNumber,
    MQTTSensor,
)
from common.framework.simple_awaiter import SimpleAwaiter
from common.framework.user_namespace import UserNamespace
from common.framework.utils import get_state_bool, get_state_float
from common.framework.simple_pid import PID


# BUG!!!: All PID integrals start oscillating for some reason
# TODO: When critical sensors are not available, open TRV and pause PID
# TODO: Set temperatures while opening/closing TRVs. There was a bug when temp was set to 4 deg and TRV didn't open
# TODO: Hinge doesn't help with oscillation, it just oscillates around hinge value. Maybe need a cooldown period for
#  opening/closing the TRV, or maybe we should smoothen PID output, like it's possible to do in esphome.

LAST_TERMINATE_KEY = "datetime.last_terminate"

CENTRAL_HEATING_NS = "central_heating"
MAX_CONTROL_FAULT_INTERVAL = 160
START_CONTROL_FAULT_INTERVAL = 10 # (20, 40, 80, 160)
PID_OUTPUT_AVERAGE_SAMPLES = 5

# noinspection PyAttributeOutsideInit
class CentralHeating(hass.Hass):
    def initialize(self):
        mqtt: mqttapi.Mqtt = cast(mqttapi.Mqtt, self.get_plugin_api("MQTT"))
        if not mqtt.is_client_connected():
            raise Exception("MQTT not connected")

        # Storage
        self._user_ns = UserNamespace(self, CENTRAL_HEATING_NS)

        # State
        self._boiler_fault = False
        # If boiler comes offline, we wait for 10s for it to come back, because it can be a simple wi-fi malfunction
        self._boiler_online_awaiter: Optional[SimpleAwaiter] = None
        self._control_fault_awaiter: Optional[SimpleAwaiter] = None
        self._control_fault_interval = 0 # seconds

        last_terminate = self._user_ns.get_state(LAST_TERMINATE_KEY, default=None)
        self.log("Got last terminate time: %s, %s", last_terminate, type(last_terminate))


        # Config
        self._global_config = GlobalConfig(self.args)

        # Init rooms
        self._rooms: list[Room] = []
        for item in self.args["rooms"]:
            room = Room(self, mqtt, item)
            self._rooms.append(room)

        self.log("Rooms:\n%s", ", ".join([room.name for room in self._rooms]))

        # Master thermostat MQTT device
        self._master_room_climate = MQTTClimate(
            self, mqtt, self._user_ns, "master", "room_climate", "Room", False
        )
        self._master_pid_output = MQTTSensor(
            self,
            mqtt,
            self._user_ns,
            "master",
            "pid_output",
            "PID Output",
            icon="mdi:gauge",
            entity_category="diagnostic",
            expire_after=60,
        )
        self._master_device = MQTTDevice(
            "master_thermostat",
            "Master Thermostat",
            "Virtual Room Thermostat",
            [self._master_room_climate, self._master_pid_output],
        )

        # Subscribe to HA MQTT status message and run room and master configuration
        mqtt.mqtt_subscribe("homeassistant/status")
        mqtt.listen_event(
            self._handle_ha_mqtt_start, "MQTT_MESSAGE", topic="homeassistant/status"
        )

        self.configure()
        # noinspection PyTypeChecker
        self._timer_handle = self.run_every(self._handle_timer, f"now+1", 1)

        self.log("Initialized")

    def terminate(self):
        # MQTT and HA connections are broken before terminate() is called, so we can't open TRVs and start the pumps.
        # We have to do this with a separate automation when master_pid_output sensor becomes unavailable due to
        # expire_after being set.
        self.log("Received termination signal")
        self._user_ns.set_state(LAST_TERMINATE_KEY, state=self.datetime())

    def configure(self):
        for room in self._rooms:
            room.configure()

        self._master_device.configure()

    def control_heating(self):
        # Check if boiler is connected. If not, we should open all TRVs and start pumps, so that
        # hardware thermostat can operate autonomously
        if self._global_config.boiler_online_sensor is not None:
            boiler_online = get_state_bool(
                self, self._global_config.boiler_online_sensor, default=False
            )
            if not boiler_online:
                if self._boiler_fault:
                    # Fault has already been set, nothing to do until boiler comes online
                    return
                if self._boiler_online_awaiter is None:
                    # This is a new development. Try to wait for 10 seconds for boiler to come online
                    self.log("Boiler became offline, waiting for 10 seconds to resolve itself")
                    self._boiler_online_awaiter = SimpleAwaiter(self, timedelta(seconds=10))
                else:
                    if self._boiler_online_awaiter.elapsed:
                        # Boiler didn't come back in 10 seconds, let's disable pids, start pumps and open TRVs
                        self._boiler_online_awaiter = None
                        self.log("Boiler didn't come back in 10 seconds")
                        self._open_trvs_start_pumps()
                        self._set_room_faults(True)
                        self._boiler_fault = True
            else:
                if self._boiler_fault:
                    self.log("Boiler has come back after the fault state")
                    self._set_room_faults(False)
                    self._boiler_fault = False
                    self._boiler_online_awaiter = None
                if self._boiler_online_awaiter is not None:
                    self.log("Boiler has come back in less than 10 seconds")
                    self._boiler_online_awaiter = None

        try:
            if self._control_fault_awaiter is not None:
                # There was a control fault, and we are waiting for increasingly long intervals for it to clear out
                if not self._control_fault_awaiter.elapsed:
                    return

                # Interval has elapsed, but we don't clear the awaiter just yet. If another exception occurs,
                # this will be a signal to increase wait duration. We clear room faults so that PIDs could be updated.
                self.log("Control fault interval elapsed, trying to restore control")
                self._set_room_faults(False)

            pid_output, any_trv_open = self.update_state()
            if pid_output > 0:
                self._start_pump(self._global_config.pump_floor)
                if any_trv_open:
                    self._start_pump(self._global_config.pump_radiators)
            else:
                self._stop_pump(self._global_config.pump_floor)
                self._stop_pump(self._global_config.pump_radiators)

            # If we got here with no exception, clear the fault awaiter
            if self._control_fault_awaiter is not None:
                self.log("Successfully recovered from the control fault")
                self._control_fault_awaiter = None

        except Exception:
            self.log("Exception occured while trying to control heating")
            self.log(traceback.format_exc())
            if self._control_fault_awaiter is None:
                self.log("This is a consecutive fault")
                self._control_fault_interval = START_CONTROL_FAULT_INTERVAL
            else:
                self.log("This is a new fault")
                self._control_fault_interval = min(self._control_fault_interval * 2, MAX_CONTROL_FAULT_INTERVAL)
            self.log("Wait time is set to %d seconds", self._control_fault_interval)
            self._control_fault_awaiter = SimpleAwaiter(self, timedelta(seconds=self._control_fault_interval))
            self._set_room_faults(True)
            self._open_trvs_start_pumps()

    def update_state(self) -> (float, bool):
        room_temp: Optional[float] = None
        room_setpoint: Optional[float] = None
        pid_output = 0
        any_climate_on = False
        any_trv_open = False

        for room in self._rooms:
            room.control_room_temperature()

            if room.room_climate.room_temp is not None:
                room_temp = (
                    min(room_temp, room.room_climate.room_temp)
                    if room_temp is not None
                    else room.room_climate.room_temp
                )
            if room.room_climate.climate.temperature is not None:
                room_setpoint = (
                    min(room_setpoint, room.room_climate.climate.temperature)
                    if room_setpoint is not None
                    else room.room_climate.climate.temperature
                )
            pid_output = max(pid_output, room.room_climate.output)
            any_climate_on = any_climate_on or room.room_climate.climate.mode == "heat"
            any_trv_open = any_trv_open or (
                room.trv.trv_open if room.trv is not None else True
            )

        if room_temp is not None:
            self._master_room_climate.current_temperature = room_temp
        if room_setpoint is not None:
            self._master_room_climate.temperature = room_setpoint
        self._master_pid_output.state = pid_output
        self._master_room_climate.mode = "heat" if any_climate_on else "off"

        return pid_output, any_trv_open

    def _set_room_faults(self, value: bool) -> None:
        for room in self._rooms:
            if room.room_climate is not None:
                room.room_climate.fault = value

    def _open_trvs_start_pumps(self):
        self.log("Starting pumps and opening TRVs")
        for room in self._rooms:
            if room.trv is not None:
                room.trv.operate_trv(1)
        self._start_pump(self._global_config.pump_radiators)
        self._start_pump(self._global_config.pump_floor)

    # Pump operations
    def _start_pump(self, pump: Optional[str]):
        if pump is None:
            return
        cur_state = self.get_state(pump)
        if cur_state != "on":
            self.call_service("switch/turn_on", entity_id=pump)

    def _stop_pump(self, pump: Optional[str]):
        if pump is None:
            return
        cur_state = self.get_state(pump)
        if cur_state != "off":
            self.call_service("switch/turn_off", entity_id=pump)

    # Handlers
    # noinspection PyUnusedLocal
    def _handle_ha_mqtt_start(self, event_name, data, cb_args):
        if not "payload" in data:
            self.error("No payload in MQTT data dict: %s", data)
            return

        if data["payload"] == "online":
            self.log(
                "HA MQTT integration restarted, resend discovery and initial state"
            )
            self.configure()

    # noinspection PyUnusedLocal
    def _handle_timer(self, cb_args):
        self.control_heating()


class GlobalConfig:
    def __init__(self, config: dict[str, Any]):
        self.pump_radiators: Optional[str] = (
            config["pump_radiators"] if "pump_radiators" in config else None
        )
        self.pump_floor: Optional[str] = (
            config["pump_floor"] if "pump_floor" in config else None
        )
        self.boiler_online_sensor = (
            config["boiler_online_sensor"] if "boiler_online_sensor" in config else None
        )


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
        if "trv" in config:
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
        self._api = api
        self._mqtt = mqtt
        self._config = config

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
        self._window_open_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                config.room_code,
                "window_open",
                "Window",
                entity_category="diagnostic",
                device_class="window",
            )
        )

        # Subscribe
        for sensor in config.window_sensors:
            self._api.listen_state(self._handle_window, sensor)

        # State
        self._last_open = False
        self._warmup_time: Optional[datetime] = None

    @property
    def window_open(self) -> bool:
        result = False
        for sensor in self._config.window_sensors:
            result = result or (get_state_bool(self._api, sensor) or False)

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
                and cast(timedelta, self._api.get_now()) >= self._warmup_time
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
                self._warmup_time = cast(timedelta, self._api.get_now()) + timedelta(
                    minutes=10
                )

        return not window_open and self._warmup_time is None

    def report_state(self) -> None:
        self._window_open_sensor.state = self.window_open

    # Handlers
    # noinspection PyUnusedLocal
    def _handle_window(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()
        self.should_heat()
        self.on_window_changed()


class TRV(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        if config.trvs is None:
            raise Exception("TRV not configured")

        super().__init__(api, mqtt, config)

        # MQTT
        self._trv_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                config.room_code,
                "trv_open",
                "TRV",
                True,
                icon="mdi:pipe-valve",
                entity_category="diagnostic",
                device_class="opening",
            )
        )

        # Subscribe
        for trv in config.trvs:
            self._api.listen_state(self._handle_trv, trv)

    @property
    def trv_open(self) -> bool:
        result = False
        for trv in self._config.trvs:
            result = result or self._api.get_state(trv) == "heat"

        return result

    def operate_trv(self, pid_output: float):
        if pid_output > 0:
            self._open_trvs()
        else:
            self._close_trvs()

        self.report_state()

    def report_state(self) -> None:
        self._trv_sensor.state = self.trv_open

    def _open_trvs(self):
        for trv in self._config.trvs:
            is_open = self._api.get_state(trv) == "heat"
            if not is_open:
                self._api.call_service(
                    "climate/set_hvac_mode", entity_id=trv, hvac_mode="heat"
                )

    def _close_trvs(self):
        for trv in self._config.trvs:
            is_open = self._api.get_state(trv) == "heat"
            if is_open:
                self._api.call_service(
                    "climate/set_hvac_mode", entity_id=trv, hvac_mode="off"
                )

    # Handlers
    # noinspection PyUnusedLocal
    def _handle_trv(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()


class FloorClimate(EntityBase):
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig):
        if config.floor_temp_sensor is None:
            raise Exception("Floor temperature sensor not configured")

        super().__init__(api, mqtt, config)

        # MQTT
        self.climate = self.register_mqtt_entity(
            MQTTClimate(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                config.room_code,
                "floor_climate",
                "Floor",
                False,
                True,
                28,
            )
        )

        # Subscribe
        self._api.listen_state(self._handle_floor_temp, self._config.floor_temp_sensor)

    @property
    def floor_temp(self) -> Optional[float]:
        return get_state_float(self._api, self._config.floor_temp_sensor)

    def apply_preset(self, preset: dict[str, Any]):
        self.climate.temperature = (
            preset["floor_setpoint"]
            if "floor_setpoint" in preset
            else self.climate.default_temperature
        )

    def report_state(self) -> None:
        self.climate.current_temperature = self.floor_temp

    # noinspection PyUnusedLocal
    def _handle_floor_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.report_state()


class PIDClimate(EntityBase):
    def __init__(
        self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, config: RoomConfig
    ):
        super().__init__(api, mqtt, config)

        # MQTT
        self.climate = self.register_mqtt_entity(
            MQTTClimate(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "room_climate",
                "Room",
            )
        )
        self.pid_kp = self.register_mqtt_entity(
            MQTTNumber(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "pid_kp",
                "PID kp",
                0.5,
                0,
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
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "pid_ki",
                "PID ki",
                0.001,
                0,
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
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "pid_active",
                "PID active",
                entity_category="diagnostic",
                device_class="running",
            )
        )
        self.pid_proportional_sensor = self.register_mqtt_entity(
            MQTTSensor(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
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
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
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
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "pid_output",
                "PID Output",
                icon="mdi:gauge",
                entity_category="diagnostic",
            )
        )
        self.fault_sensor = self.register_mqtt_entity(
            MQTTBinarySensor(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "fault",
                "Fault",
                icon="mdi:close-octagon-outline",
                entity_category="diagnostic",
            )
        )
        self.reset_pid_integral_button = self.register_mqtt_entity(
            MQTTButton(
                api,
                mqtt,
                UserNamespace(api, CENTRAL_HEATING_NS),
                self._config.room_code,
                "reset_pid",
                "Reset PID",
                entity_category="config",
            )
        )

        # Events
        self.climate.on_mode_changed += self._handle_mode
        self.climate.on_temperature_changed += self._handle_setpoint
        self.pid_kp.on_state_changed += self._handle_pid_kp
        self.pid_ki.on_state_changed += self._handle_pid_ki
        self.reset_pid_integral_button.on_press += self._handle_reset_pid

        # Private
        self._fault = False # Set from the outside when PID should be disabled due to a fault
        self._pid = PID(
            self.pid_kp.state,
            self.pid_ki.state,
            0,
            self.climate.temperature,
            1,
            (-1, 1),
        )
        self._output: list[float] = []
        self._pid_enablers: list[Callable[[], bool]] = [self._room_climate_enabled, self._no_fault]

        # Subscribe to sensors
        self._api.listen_state(self._handle_room_temp, self._config.room_temp_sensor)

    @property
    def room_temp(self) -> Optional[float]:
        return get_state_float(self._api, self._config.room_temp_sensor)

    @property
    def output(self) -> float:
        if not self._pid.auto_mode or len(self._output) == 0:
            return 0

        return sum(self._output) / len(self._output)

    @property
    def enabled(self) -> bool:
        return self._pid.auto_mode

    @property
    def fault(self) -> bool:
        return self._fault

    @fault.setter
    def fault(self, value: bool) -> None:
        self._api.log("Changing fault state for room %s to %s", self._config.room_name, value)
        self._fault = value
        self.fault_sensor.state = self._fault

    def register_pid_enabler(self, enabler: Callable[[], bool]):
        self._pid_enablers.append(enabler)

    def claculate_output(self):
        # Check whether PID is enabled, just in case state change got missed
        self._recalculate_pid_enabled()

        if not self.enabled:
            return

        cur_temp = self.room_temp
        if cur_temp is None:
            self._api.log(
                "Failed to get room temperature for room %s", self._config.room_name
            )
            return

        self._output.append(self._pid(cur_temp))
        if len(self._output) > PID_OUTPUT_AVERAGE_SAMPLES:
            self._output.pop(0)
        self.report_state()

    def report_state(self):
        self.pid_active_sensor.state = self._pid.auto_mode
        self.pid_proportional_sensor.state = self._pid.components[0]
        self.pid_integral_sensor.state = self._pid.components[1]
        self.pid_output_sensor.state = self.output
        self.climate.current_temperature = self.room_temp
        self.fault_sensor.state = self._fault

    def apply_preset(self, preset: dict[str, Any]):
        self.climate.mode = preset["mode"] if "mode" in preset else "off"
        self._handle_mode()

        self.climate.temperature = (
            preset["room_setpoint"]
            if "room_setpoint" in preset
            else self.climate.default_temperature
        )
        self._handle_setpoint()

        self.pid_kp.state = (
            preset["kp"] if "kp" in preset else self.pid_kp.default_value
        )
        self._handle_pid_kp()

        self.pid_ki.state = (
            preset["ki"] if "ki" in preset else self.pid_ki.default_value
        )
        self._handle_pid_ki()

    def _recalculate_pid_enabled(self):
        result = True
        for enabler in self._pid_enablers:
            result = result and enabler()

        self._set_pid_enabled(result)

    def _room_climate_enabled(self):
        return self.climate.mode == "heat"

    def _no_fault(self):
        return not self._fault

    def _set_pid_enabled(self, value: bool) -> None:
        if self._pid.auto_mode == value:
            return

        if value:
            self._api.log("Disabling PID for room %s", self._config.room_name)
            self._pid.auto_mode = True
        else:
            self._api.log("Enabling PID for room %s", self._config.room_name)
            self._pid.auto_mode = False
            self._output.clear()
            self._last_returned_output = 0

        self.report_state()

    # Handlers
    def _handle_mode(self):
        self._recalculate_pid_enabled()

    def _handle_setpoint(self):
        self._pid.setpoint = self.climate.temperature

    def _handle_pid_kp(self):
        self._pid.Kp = self.pid_kp.state

    def _handle_pid_ki(self):
        self._pid.Ki = self.pid_ki.state

    def _handle_reset_pid(self):
        self._pid.reset()

    # noinspection PyUnusedLocal
    def _handle_room_temp(self, entity, attribute, old: str, new: str, cb_args):
        self.climate.current_temperature = self.room_temp


class Room:
    def __init__(self, api: adapi.ADAPI, mqtt: mqttapi.Mqtt, room_config: dict[str, Any]):
        self._api = api
        self._mqtt = mqtt
        self._config = RoomConfig(room_config)
        self._namespace = UserNamespace(api, CENTRAL_HEATING_NS)

        self._entities: list[EntityBase] = []

        # Create room entities based on config
        self.room_climate = self._add_entity(
            PIDClimate(api, mqtt, self._config)
        )
        self.floor_climate = self._add_entity(
            FloorClimate(api, mqtt, self._config)
            if self._config.floor_temp_sensor is not None
            else None
        )
        self.window = self._add_entity(
            Window(api, mqtt, self._config)
            if self._config.window_sensors is not None
            else None
        )
        self.trv = self._add_entity(
            TRV(api, mqtt, self._config) if self._config.trvs is not None else None
        )

        # Entity-specific config
        if self.window is not None:
            self.room_climate.register_pid_enabler(self.window.should_heat)

        # Subscribe to events
        self.room_climate.climate.on_mode_changed += self._handle_room_mode
        self.room_climate.climate.on_preset_changed += self._handle_room_preset
        self.room_climate.climate.on_temperature_changed += self._handle_room_setpoint
        self.room_climate.pid_kp.on_state_changed += self._handle_room_pid_kp
        self.room_climate.pid_ki.on_state_changed += self._handle_room_pid_ki

        # Collect MQTT entities and perform entity-specific configuration
        entities_mqtt = []
        for entity in self._entities:
            entities_mqtt.extend(entity.mqtt_entities)

        # Create MQTT room device
        self._room_device = MQTTDevice(
            f"{self._config.room_code}_thermostat",
            f"{self._config.room_name} Thermostat",
            "Virtual Room Thermostat",
            entities_mqtt,
        )

    @property
    def name(self) -> str:
        return self._config.room_name

    def control_room_temperature(self):
        self.room_climate.claculate_output()
        if self.trv is not None:
            if self.window is not None and not self.window.should_heat():
                # Don't bother with TRV when window is open — just to save TRV battery
                return
            self.trv.operate_trv(self.room_climate.output)

    def configure(self):
        self._room_device.configure()
        self._load_preset()

    TEntity = TypeVar("TEntity", bound=EntityBase)

    def _add_entity(self, entity: Optional[TEntity]) -> Optional[TEntity]:
        if entity is None:
            return None
        self._entities.append(entity)
        return entity

    def _save_preset(self):
        attributes = {
            "mode": self.room_climate.climate.mode,
            "room_setpoint": self.room_climate.climate.temperature,
            "kp": self.room_climate.pid_kp.state,
            "ki": self.room_climate.pid_ki.state,
        }

        if self.floor_climate is not None:
            attributes["floor_setpoint"] = self.floor_climate.climate.temperature

        self._namespace.set_state(
            f"preset.{self._config.room_code}_{self.room_climate.climate.preset}",
            attributes=attributes,
        )

    def _load_preset(self):
        preset = cast(
            dict,
            self._namespace.get_state(
                f"preset.{self._config.room_code}_{self.room_climate.climate.preset}",
                attribute="all",
            ),
        )
        if (
            preset is None
            or "attributes" not in preset
            or len(preset["attributes"]) == 0
        ):
            self._api.log("Loaded empty preset, saving current values")
            self._save_preset()
            return

        attributes = preset["attributes"]
        self._api.log(
            "Loaded preset %s: %s", self.room_climate.climate.preset, attributes
        )

        self.room_climate.apply_preset(attributes)
        if self.floor_climate is not None:
            self.floor_climate.apply_preset(attributes)

    # Handlers
    def _handle_room_mode(self):
        self._save_preset()

    def _handle_room_preset(self):
        self._load_preset()

    def _handle_room_setpoint(self):
        self._save_preset()

    def _handle_room_pid_kp(self):
        self._save_preset()

    def _handle_room_pid_ki(self):
        self._save_preset()

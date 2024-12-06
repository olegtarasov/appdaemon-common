from typing import cast

from datetime import datetime, time
from appdaemon.plugins.mqtt import mqttapi
import appdaemon.plugins.hass.hassapi as hass

from framework.mqtt_entites import MQTTBinarySensor, MQTTDevice
from framework.user_namespace import UserNamespace

SLEEP_TRACKER_NS = "sleep_tracker"


# noinspection PyAttributeOutsideInit
class SleepTracker(hass.Hass):
    def initialize(self):
        if "person_name" not in self.args:
            raise Exception("Person name is mandatory")
        if "person_code" not in self.args:
            raise Exception("Person code is mandatory")

        self.user_ns = UserNamespace(self, SLEEP_TRACKER_NS)
        self.person_name = self.args["person_name"]
        self.person_code = self.args["person_code"]
        self.bed_time_entity = (
            self.args["bed_time_entity"] if "bed_time_entity" in self.args else None
        )
        self.wake_up_entity = (
            self.args["wake_up_entity"] if "wake_up_entity" in self.args else None
        )
        self.sleep_event_id = (
            self.args["sleep_event_id"] if "sleep_event_id" in self.args else None
        )

        self.bed_time_timer = None
        self.wake_up_timer = None

        mqtt: mqttapi.Mqtt = cast(mqttapi.Mqtt, self.get_plugin_api("MQTT"))
        if not mqtt.is_client_connected():
            raise Exception("MQTT not connected")

        self.sleep_sensor = MQTTBinarySensor(
            self,
            mqtt,
            self.user_ns,
            self.person_code,
            "asleep",
            f"{self.person_name} asleep",
            icon="mdi:power-sleep",
        )
        self.sleep_device = MQTTDevice(
            f"sleep_tracker_{self.person_code}",
            f"Sleep Tracker ({self.person_name})",
            "Virtual Device",
            [self.sleep_sensor],
        )

        self.sleep_device.configure()

        if self.bed_time_entity is not None:
            self.listen_state(self._update_timers, self.bed_time_entity)
        if self.wake_up_entity is not None:
            self.listen_state(self._update_timers, self.wake_up_entity)

        self._update_timers(None, None, None, None, None)

        if self.sleep_event_id is not None:
            self.listen_event(self.on_sleep_event, "ios.action_fired")

        # noinspection PyTypeChecker
        self.log("Initialized")

    @property
    def last_reported_sleep_ios(self) -> datetime:
        return datetime.fromtimestamp(
            self.user_ns.get_state_float(
                f"sleep_tracker.last_reported_ios_{self.person_code}"
            )
        ).astimezone(self.AD.tz)

    @last_reported_sleep_ios.setter
    def last_reported_sleep_ios(self, value: datetime):
        self.user_ns.set_state(
            f"sleep_tracker.last_reported_ios_{self.person_code}",
            state=value.timestamp(),
        )

    def on_bed_time(self, cb_args):
        self.sleep_sensor.state = True

    def on_wake_up(self, cb_args):
        self.sleep_sensor.state = False

    def on_sleep_event(self, event_name, data, cb_args):
        if "actionID" in data and data["actionID"] == self.sleep_event_id:
            self.log("Prev date: %s", self.last_reported_sleep_ios)
            self.last_reported_sleep_ios = cast(datetime, self.get_now())

    # Handlers
    def _update_timers(self, entity, attribute, old: str, new: str, cb_args):
        if self.bed_time_timer is not None:
            self.cancel_timer(self.bed_time_timer)
        if self.wake_up_timer is not None:
            self.cancel_timer(self.wake_up_timer)

        if self.bed_time_entity is not None:
            bed_time = time.fromisoformat(self.get_state(self.bed_time_entity))
            self.bed_time_timer = self.run_daily(self.on_bed_time, bed_time)

        if self.wake_up_entity is not None:
            wake_up = time.fromisoformat(self.get_state(self.wake_up_entity))
            self.wake_up_timer = self.run_daily(self.on_wake_up, wake_up)

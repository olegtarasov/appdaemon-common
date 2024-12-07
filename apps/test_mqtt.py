import json
from typing import cast

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
import appdaemon.plugins.hass.hassapi as hass


class TestMqtt(hass.Hass):
    def initialize(self):
        mqtt: mqttapi.Mqtt = cast(mqttapi.Mqtt, self.get_plugin_api("MQTT"))
        if not mqtt.is_client_connected():
            raise Exception("MQTT not connected")

        entity_id = "testroom_foo"

        config = {
            "platform": "sensor",
            "state_topic": f"homeassistant/sensor/{entity_id}",
            "unique_id": entity_id,
            "object_id": entity_id,
            "name": "Value",
            "state_class": "measurement",
            "device": {
                "identifiers": ["mqtt_test_device"],
                "name": "Test Device",
                "manufacturer": "Cats Ltd.",
                "model": "Model",
            },
        }

        config_topic = f"homeassistant/sensor/{entity_id}/config"

        mqtt.mqtt_publish(
            config_topic,
            json.dumps(config),
        )

        self.log("===== FCKN DONE")

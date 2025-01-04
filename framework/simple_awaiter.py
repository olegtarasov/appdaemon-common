from datetime import datetime, timedelta
from typing import cast

from appdaemon.adapi import ADAPI

class SimpleAwaiter:
    def __init__(self, api: ADAPI, wait_time: timedelta) -> None:
        self.api = api

        start_time = cast(datetime, api.datetime())
        self.target_time = start_time + wait_time

    @property
    def elapsed(self) -> bool:
        return cast(datetime, self.api.datetime()) >= self.target_time

from datetime import datetime, timedelta
import logging
from typing import Callable, Iterable
from aiohttp.client import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import event
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.dt import utcnow

DOMAIN = "nature_remo"

RESOURCE = "https://api.nature.global/1"

ICONS_MAP = {
    "ico_0": "mdi:numeric-0",
    "ico_1": "mdi:numeric-1",
    "ico_2": "mdi:numeric-2",
    "ico_3": "mdi:numeric-3",
    "ico_4": "mdi:numeric-4",
    "ico_5": "mdi:numeric-5",
    "ico_6": "mdi:numeric-6",
    "ico_7": "mdi:numeric-7",
    "ico_8": "mdi:numeric-8",
    "ico_9": "mdi:numeric-9",
    "ico_10": "mdi:numeric-10",
    "ico_ac_fan": "mdi:fan",
    "ico_arrow_bottom": "mdi:arrow-down-drop-circle",
    "ico_arrow_top": "mdi:arrow-up-drop-circle",
    "ico_blast": "mdi:weather-windy",
    "ico_io": "mdi:power",
    "ico_minus": "mdi:minus",
    "ico_night_light": "mdi:weather-night",
    "ico_off": "mdi:toggle-switch-off-outline",
    "ico_on": "mdi:toggle-switch",
    "ico_plus": "mdi:plus",
}


class NatureUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    _next_update: datetime = None

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        entry: ConfigEntry,
        session: ClientSession,
        rate_limit: DataUpdateCoordinator,
        path: str,
    ) -> None:
        super().__init__(
            hass,
            logger,
            name=f"Nature Remo {path} update",
        )
        self.entry = entry
        self.path = path
        self.rate_limit = rate_limit
        self.session = session
        self.update_interval = timedelta(seconds=30)

    async def _async_update_data(self):
        access_token: str = self.entry.data[CONF_ACCESS_TOKEN]
        headers = {"Authorization": f"Bearer {access_token}"}
        response = await self.session.get(f"{RESOURCE}/{self.path}", headers=headers)
        if response.status == 401:
            raise ConfigEntryAuthFailed()
        if "x-rate-limit-remaining" in response.headers:
            remaining = int(response.headers.get("x-rate-limit-remaining"))
            reset = datetime.fromtimestamp(
                int(response.headers.get("x-rate-limit-reset"))
            )
            self.rate_limit.async_set_updated_data(
                {"remaining": remaining, "reset": reset}
            )
            if response.status == 429:
                self._schedule_refresh(reset + timedelta(seconds=1))
        if response.status != 200:
            raise UpdateFailed(f"status code: {response.status}")
        data = await response.json()
        self._next_update = self._get_next_update(data)
        return {x["id"]: x for x in data}

    def _get_next_update(self, data):
        return None

    @callback
    def _schedule_refresh(self):
        """Schedule a refresh."""
        if self.update_interval is None:
            return

        if self.config_entry and self.config_entry.pref_disable_polling:
            return

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        time = utcnow().replace(microsecond=0)
        if self._next_update is not None:
            if time <= self._next_update:
                time = self._next_update
            else:
                time = time.replace(second=self._next_update.second)
                while time < utcnow():
                    time += self.update_interval
        else:
            time += self.update_interval

        # We _floor_ utcnow to create a schedule on a rounded second,
        # minimizing the time between the point and the real activation.
        # That way we obtain a constant update frequency,
        # as long as the update process takes less than a second
        self._unsub_refresh = event.async_track_point_in_utc_time(
            self.hass,
            self._handle_refresh_interval,
            time,
        )


class AppliancesUpdateCoordinator(NatureUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        entry: ConfigEntry,
        session: ClientSession,
        rate_limit: DataUpdateCoordinator,
    ) -> None:
        super().__init__(
            hass,
            logger,
            entry,
            session,
            rate_limit,
            "appliances",
        )

    def _get_next_update(self, appliances):
        val = max(
            (
                str_to_datetime(
                    x["smart_meter"]["echonetlite_properties"][0]["updated_at"]
                )
                for x in appliances
                if "smart_meter" in x
            ),
            default=None,
        )
        if val is not None:
            now = utcnow()
            val += timedelta(seconds=62)
            retry = val + timedelta(seconds=5)
            if val < now and retry >= now:
                return retry
            retry = val + timedelta(seconds=10)
            if val < now and retry >= now:
                return retry
            return val
        return super()._get_next_update(appliances)


class NatureEntity(CoordinatorEntity):
    coordinator: NatureUpdateCoordinator

    def __init__(
        self,
        coordinator: NatureUpdateCoordinator,
        remo_id: str,
        unique_id: str,
        device_info: DeviceInfo,
    ):
        super().__init__(coordinator)
        self._remo_id = remo_id
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id

    @property
    def available(self):
        return self._attr_available

    @callback
    def _handle_coordinator_update(self):
        if (
            not self.coordinator.last_update_success
            and self.coordinator.rate_limit.data["remaining"] <= 0
        ):
            return
        self._attr_available = (
            self.coordinator.last_update_success
            and self._remo_id in self.coordinator.data
        )
        if self._attr_available:
            self._on_data_update(self.coordinator.data[self._remo_id])
        self.async_write_ha_state()

    def _on_data_update(self, data: dict):
        pass


class RemoSensorEntity(NatureEntity):
    def __init__(
        self,
        coordinator: NatureUpdateCoordinator,
        device: dict,
        device_info: DeviceInfo,
        key: str,
    ):
        super().__init__(
            coordinator, device["id"], f"{device['id']}-{key}", device_info
        )
        self._attr_name = f"{device['name']} {key}"
        self._key = key
        self._on_data_update(device)

    def _on_data_update(self, device: dict):
        newest_events = device["newest_events"]
        self._attr_extra_state_attributes = {
            "created_at": modify_utc_z(newest_events[self._key]["created_at"]),
        }
        return super()._on_data_update(device)


def create_appliance_device_info(appliance: dict):
    info = DeviceInfo(
        identifiers={(DOMAIN, appliance["id"])},
        name=appliance["nickname"],
        via_device=(DOMAIN, appliance["device"]["id"]),
    )
    model = appliance["model"]
    if model is not None:
        info["manufacturer"] = appliance["model"]["manufacturer"]
        info["model"] = appliance["model"]["name"]
    return info


def create_device_device_info(device: dict):
    return DeviceInfo(
        connections={(CONNECTION_NETWORK_MAC, device["mac_address"])},
        identifiers={(DOMAIN, device["id"])},
        manufacturer="Nature Inc.",
        model=device["firmware_version"].split("/")[0],
        name=device["name"],
        sw_version=device["firmware_version"],
    )


def check_update(
    entry: ConfigEntry,
    async_add_entities: Callable,
    coordinator: NatureUpdateCoordinator,
    found: Callable[[dict], Iterable],
):
    added = []

    def updated():
        if not coordinator.last_update_success:
            return
        entries = []
        for x in coordinator.data.values():
            id = x["id"]
            if id in added:
                continue
            entries.extend(found(x))
            added.append(id)
        async_add_entities(entries)

    entry.async_on_unload(coordinator.async_add_listener(updated))
    updated()


def modify_utc_z(s: str):
    return s.replace("Z", "+00:00")


def str_to_datetime(s: str):
    return datetime.fromisoformat(modify_utc_z(s))

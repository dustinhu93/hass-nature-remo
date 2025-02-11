"""Support for Nature Remo AC."""

from datetime import timedelta  # noqa: I001
import logging
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.components.climate import (
    ClimateEntity,
    HVACMode,
    ClimateEntityFeature,
)

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from homeassistant.const import UnitOfTemperature

from .common import (
    DOMAIN,
    AppliancesUpdateCoordinator,
    NatureEntity,
    NatureUpdateCoordinator,
    check_update,
    create_appliance_device_info,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_COOL_TEMP = 28
DEFAULT_HEAT_TEMP = 20

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.SWING_MODE
)

MODE_HA_TO_REMO = {
    HVACMode.AUTO: "auto",
    HVACMode.FAN_ONLY: "blow",
    HVACMode.COOL: "cool",
    HVACMode.DRY: "dry",
    HVACMode.HEAT: "warm",
    HVACMode.OFF: "power-off",
}

MODE_REMO_TO_HA = {
    "auto": HVACMode.AUTO,
    "blow": HVACMode.FAN_ONLY,
    "cool": HVACMode.COOL,
    "dry": HVACMode.DRY,
    "warm": HVACMode.HEAT,
    "power-off": HVACMode.OFF,
}

TEMP_UNIT_REMO_TO_HA = {
    "c": UnitOfTemperature.CELSIUS,
    "f": UnitOfTemperature.FAHRENHEIT,
}

DEFAULT_TEMP = {
    HVACMode.AUTO: 23,
    HVACMode.FAN_ONLY: 23,
    HVACMode.COOL: 23,
    HVACMode.DRY: 23,
    HVACMode.HEAT: 23,
}


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    """Set up the Nature Remo AC."""
    _LOGGER.debug("Setting up climate platform.")
    appliances: AppliancesUpdateCoordinator = hass.data[DOMAIN]["appliances"]
    devices: NatureUpdateCoordinator = hass.data[DOMAIN]["devices"]
    post: Callable = hass.data[DOMAIN]["post"]

    def on_add(appliance: dict):
        if appliance["type"] != "AC":
            return
        device_info = create_appliance_device_info(appliance)
        yield AirconEntity(appliances, devices, post, appliance, device_info)

    check_update(entry, async_add_entities, appliances, on_add)


class AirconEntity(NatureEntity, ClimateEntity, RestoreEntity):
    """Implementation of a Nature Remo E sensor."""

    _attr_supported_features = SUPPORT_FLAGS
    _last_target_temperature = {}
    _next_settings = None
    _post_cancel = None
    _updated_at: str = None

    def __init__(
        self,
        appliances: AppliancesUpdateCoordinator,
        devices: NatureUpdateCoordinator,
        post: Callable,
        appliance: dict,
        device_info: DeviceInfo,
    ):
        super().__init__(appliances, appliance["id"], appliance["id"], device_info)
        self._attr_name: str = appliance["nickname"]
        self.devices = devices
        self._device_id: str = appliance["device"]["id"]
        self._post = post
        self._modes: dict = appliance["aircon"]["range"]["modes"]
        self._remo_mode = None
        self.async_on_remove(devices.async_add_listener(self._on_device_update))
        self._on_data_update(appliance)
        self._on_device_update()

    async def async_added_to_hass(self) -> None:
        state = await self.async_get_last_state()
        if state is not None and state.attributes is not None:
            self._last_target_temperature = state.attributes[
                "previous_target_temperature"
            ]

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        temp_range = self._current_mode_temp_range()
        if len(temp_range) == 0:
            return 0
        return min(temp_range)

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        temp_range = self._current_mode_temp_range()
        if len(temp_range) == 0:
            return 0
        return max(temp_range)

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        temp_range = self._current_mode_temp_range()
        if len(temp_range) >= 2:
            # determine step from the gap of first and second temperature
            step = round(temp_range[1] - temp_range[0], 1)
            if step in [1.0, 0.5]:  # valid steps
                return step
        return 1

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        remo_modes = list(self._modes.keys())
        ha_modes = list(map(lambda mode: MODE_REMO_TO_HA[mode], remo_modes))
        ha_modes.append(HVACMode.OFF)
        return ha_modes

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return self._modes[self._remo_mode]["vol"]

    @property
    def swing_modes(self):
        """List of available swing modes."""
        return self._modes[self._remo_mode]["dir"]

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        return {
            "previous_target_temperature": self._last_target_temperature,
            "updated_at": self._updated_at,
        }

    def set_temperature(self, temperature=None, hvac_mode=None, **kwargs):
        data = {}
        if hvac_mode is not None:
            _LOGGER.debug("Set hvac mode: %s", hvac_mode)
            mode = MODE_HA_TO_REMO[hvac_mode]
            if mode == MODE_HA_TO_REMO[HVACMode.OFF]:
                data["button"] = mode
            else:
                data["operation_mode"] = mode
                if self._last_target_temperature.get(mode):
                    data["temperature"] = self._last_target_temperature[mode]
                elif DEFAULT_TEMP.get(hvac_mode):
                    data["temperature"] = DEFAULT_TEMP[hvac_mode]

        if temperature is not None:
            if temperature.is_integer():
                # has to cast to whole number otherwise API will return an error
                temperature = int(temperature)
            _LOGGER.debug("Set temperature: %d", temperature)
            data["temperature"] = f"{temperature}"

        self._set_settings(data)

    def set_hvac_mode(self, hvac_mode):
        self.set_temperature(hvac_mode=hvac_mode)

    def set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        _LOGGER.debug("Set fan mode: %s", fan_mode)
        self._set_settings({"air_volume": fan_mode})

    def set_swing_mode(self, swing_mode):
        _LOGGER.debug("Set swing mode: %s", swing_mode)
        self._set_settings({"air_direction": swing_mode})

    def _on_data_update(self, appliance: dict):
        super()._on_data_update(appliance)
        self._on_settings_update(appliance["settings"])

    def _on_settings_update(self, ac_settings: dict):
        # hold this to determin the ac mode while it's turned-off
        self._remo_mode: str = ac_settings["mode"]
        try:
            self._attr_target_temperature = float(ac_settings["temp"])
            self._last_target_temperature[self._remo_mode] = ac_settings["temp"]
        except:
            self._attr_target_temperature = None

        if ac_settings["button"] == MODE_HA_TO_REMO[HVACMode.OFF]:
            self._attr_hvac_mode = HVACMode.OFF
        else:
            self._attr_hvac_mode = MODE_REMO_TO_HA[self._remo_mode]

        self._attr_fan_mode = ac_settings["vol"] or None
        self._attr_swing_mode = ac_settings["dir"] or None
        self._attr_temperature_unit = TEMP_UNIT_REMO_TO_HA[ac_settings["temp_unit"]]
        self._updated_at: str = ac_settings["updated_at"] or None

    @callback
    def _on_device_update(self):
        if not self.devices.last_update_success:
            return
        device: dict[str, dict[str, dict[str, str]]] = self.devices.data[
            self._device_id
        ]
        newest_events = device["newest_events"]
        self._attr_current_temperature = float(newest_events["te"]["val"])
        self._attr_current_humidity = int(newest_events["hu"]["val"])

    def _set_settings(self, data: dict):
        if self._next_settings is None:
            self._next_settings = data
        else:
            self._next_settings.update(data)
        if self._post_cancel is not None:
            self._post_cancel()
        self._post_cancel = async_call_later(
            self.hass, timedelta(milliseconds=100), self._on_post
        )

    async def _on_post(self, *_):
        self._post_cancel = None
        data = self._next_settings
        self._next_settings = None
        ac_settings = await self._post(
            f"appliances/{self._remo_id}/aircon_settings", data
        )
        self._on_settings_update(ac_settings)
        self._async_write_ha_state()

    def _current_mode_temp_range(self):
        temp_range = self._modes[self._remo_mode]["temp"]
        return list(map(float, filter(None, temp_range)))

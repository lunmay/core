"""Support for TP-Link thermostats."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any, cast

from kasa import Device
from kasa.smart.modules.temperaturecontrol import ThermostatState

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import PRECISION_TENTHS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TPLinkConfigEntry, legacy_device_id
from .const import DOMAIN, UNIT_MAPPING
from .coordinator import TPLinkDataUpdateCoordinator
from .entity import (
    CoordinatedTPLinkModuleEntity,
    TPLinkModuleEntityDescription,
    async_refresh_after,
)

# Coordinator is used to centralize the data updates
# For actions the integration handles locking of concurrent device request
PARALLEL_UPDATES = 0

# Upstream state to HVACAction
STATE_TO_ACTION = {
    ThermostatState.Idle: HVACAction.IDLE,
    ThermostatState.Heating: HVACAction.HEATING,
    ThermostatState.Off: HVACAction.OFF,
}


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class TPLinkClimateEntityDescription(
    ClimateEntityDescription, TPLinkModuleEntityDescription
):
    """Base class for climate entity description."""

    unique_id_fn: Callable[[Device, TPLinkModuleEntityDescription], str] = (
        lambda device, desc: f"{legacy_device_id(device)}_{desc.key}"
    )


CLIMATE_DESCRIPTIONS: tuple[TPLinkClimateEntityDescription, ...] = (
    TPLinkClimateEntityDescription(
        key="climate",
        exists_fn=lambda dev, _: dev.device_type is Device.Type.Thermostat,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: TPLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities."""
    data = config_entry.runtime_data
    parent_coordinator = data.parent_coordinator
    device = parent_coordinator.device

    known_child_device_ids: set[str] = set()
    first_check = True

    def _check_device() -> None:
        entities = CoordinatedTPLinkModuleEntity.entities_for_device_and_its_children(
            hass=hass,
            device=device,
            coordinator=parent_coordinator,
            entity_class=TPLinkClimateEntity,
            descriptions=CLIMATE_DESCRIPTIONS,
            platform_domain=CLIMATE_DOMAIN,
            known_child_device_ids=known_child_device_ids,
            first_check=first_check,
        )
        async_add_entities(entities)

    _check_device()
    first_check = False
    config_entry.async_on_unload(parent_coordinator.async_add_listener(_check_device))


class TPLinkClimateEntity(CoordinatedTPLinkModuleEntity, ClimateEntity):
    """Representation of a TPLink thermostat."""

    _attr_name = None
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_precision = PRECISION_TENTHS

    entity_description: TPLinkClimateEntityDescription

    # This disables the warning for async_turn_{on,off}, can be removed later.

    def __init__(
        self,
        device: Device,
        coordinator: TPLinkDataUpdateCoordinator,
        description: TPLinkClimateEntityDescription,
        *,
        parent: Device,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(device, coordinator, description, parent=parent)
        self._state_feature = device.features["state"]
        self._mode_feature = device.features["thermostat_mode"]
        self._temp_feature = device.features["temperature"]
        self._target_feature = device.features["target_temperature"]

        self._attr_min_temp = self._target_feature.minimum_value
        self._attr_max_temp = self._target_feature.maximum_value
        self._attr_temperature_unit = UNIT_MAPPING[cast(str, self._temp_feature.unit)]

    @async_refresh_after
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        await self._target_feature.set_value(int(kwargs[ATTR_TEMPERATURE]))

    @async_refresh_after
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set hvac mode (heat/off)."""
        if hvac_mode is HVACMode.HEAT:
            await self._state_feature.set_value(True)
        elif hvac_mode is HVACMode.OFF:
            await self._state_feature.set_value(False)
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_mode",
                translation_placeholders={
                    "mode": hvac_mode,
                },
            )

    @async_refresh_after
    async def async_turn_on(self) -> None:
        """Turn heating on."""
        await self._state_feature.set_value(True)

    @async_refresh_after
    async def async_turn_off(self) -> None:
        """Turn heating off."""
        await self._state_feature.set_value(False)

    @callback
    def _async_update_attrs(self) -> bool:
        """Update the entity's attributes."""
        self._attr_current_temperature = cast(float | None, self._temp_feature.value)
        self._attr_target_temperature = cast(float | None, self._target_feature.value)

        self._attr_hvac_mode = (
            HVACMode.HEAT if self._state_feature.value else HVACMode.OFF
        )

        if (
            self._mode_feature.value not in STATE_TO_ACTION
            and self._attr_hvac_action is not HVACAction.OFF
        ):
            _LOGGER.warning(
                "Unknown thermostat state, defaulting to OFF: %s",
                self._mode_feature.value,
            )
            self._attr_hvac_action = HVACAction.OFF
            return True

        self._attr_hvac_action = STATE_TO_ACTION[
            cast(ThermostatState, self._mode_feature.value)
        ]
        return True

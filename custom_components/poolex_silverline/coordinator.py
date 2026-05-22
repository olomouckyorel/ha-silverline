"""DataUpdateCoordinator for the Poolex Silverline."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pysilverline import (
    CannotConnect,
    DeviceInfo,
    DeviceState,
    InvalidAuth,
    SilverlineClient,
)

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type SilverlineConfigEntry = ConfigEntry[SilverlineCoordinator]


class SilverlineCoordinator(DataUpdateCoordinator[DeviceState]):
    """Coordinates polling and push updates from one heat pump."""

    config_entry: SilverlineConfigEntry
    device_info: DeviceInfo

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SilverlineConfigEntry,
        client: SilverlineClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            always_update=False,
        )
        self.client = client
        self._unsub_push: Callable[[], None] | None = None
        self._unsub_connection: Callable[[], None] | None = None
        # Set on first successful poll. Lets platforms skip entities whose
        # backing DP this firmware variant never reports.
        self.supported_dps: frozenset[str] = frozenset()

    async def _async_setup(self) -> None:
        try:
            await self.client.connect()
        except CannotConnect as err:
            raise UpdateFailed(f"connect failed: {err}") from err
        self._unsub_push = self.client.add_listener(self._handle_push)
        self._unsub_connection = self.client.add_connection_listener(
            self._handle_connection_change
        )
        self.device_info = await self.client.get_device_info()

    async def _async_update_data(self) -> DeviceState:
        try:
            state = await self.client.get_status()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(err) from err
        except CannotConnect as err:
            raise UpdateFailed(f"poll failed: {err}") from err
        # Snapshot the DPs the firmware actually emits, once. Platforms
        # read this in their async_setup_entry to skip entities that would
        # otherwise spend their whole lifetime `unavailable`.
        if not self.supported_dps:
            self.supported_dps = frozenset(state.raw.keys())
        return state

    @callback
    def _handle_push(self, state: DeviceState) -> None:
        self.async_set_updated_data(state)

    @callback
    def _handle_connection_change(self, connected: bool) -> None:
        # When the socket drops, mark the last update as failed so entities
        # surface `unavailable`. On recovery, request a fresh refresh so the
        # state caught between the drop and the next 30s poll lands fast.
        if connected:
            self.hass.async_create_task(self.async_request_refresh())
        else:
            self.last_update_success = False
            self.async_update_listeners()

    async def async_shutdown(self) -> None:
        if self._unsub_push is not None:
            self._unsub_push()
            self._unsub_push = None
        if self._unsub_connection is not None:
            self._unsub_connection()
            self._unsub_connection = None
        await self.client.disconnect()
        await super().async_shutdown()

"""Config and reauth/reconfigure flows for Poolex Silverline."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import config_validation as cv

from pysilverline import CannotConnect, InvalidAuth, SilverlineClient

from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_LOCAL_KEY): cv.string,
    }
)

_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_LOCAL_KEY): cv.string})

_DISCOVERY_CONFIRM_SCHEMA = vol.Schema({vol.Required(CONF_LOCAL_KEY): cv.string})


async def _validate(data: Mapping[str, Any]) -> None:
    """Open a connection with the supplied credentials and pull status once.

    Raises CannotConnect or InvalidAuth on failure; returns silently on
    success. Always closes the socket before returning.
    """
    client = SilverlineClient(
        host=data[CONF_HOST],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        device_id=data[CONF_DEVICE_ID],
        local_key=data[CONF_LOCAL_KEY],
    )
    try:
        await client.connect()
        await client.get_status()
    finally:
        await client.disconnect()


class SilverlineConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user, reauth, reconfigure, and discovery flows."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._discovery_host: str | None = None
        self._discovery_device_id: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured()
            error = await self._try_validate(user_input)
            if error is None:
                return self.async_create_entry(
                    title=f"Pool Heatpump ({user_input[CONF_HOST]})",
                    data=user_input,
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {**entry.data, CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY]}
            error = await self._try_validate(candidate)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY]}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REAUTH_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_mismatch(reason="device_id_mismatch")
            error = await self._try_validate(user_input)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_USER_SCHEMA, entry.data),
            errors=errors,
        )

    async def async_step_integration_discovery(
        self, discovery_info: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered by the background UDP listener when a Tuya broadcast
        names a device we haven't configured yet.

        discovery_info carries ``device_id`` and ``ip`` straight from
        the Tuya broadcast JSON. The local_key is the only thing the
        device doesn't announce, so we ask the user for that and create
        the entry.
        """
        device_id = discovery_info["device_id"]
        host = discovery_info["ip"]
        await self.async_set_unique_id(device_id)
        # Satisfies Gold `discovery-update-info`: a device that's already
        # configured but now appears at a new IP gets its host rewritten
        # in place instead of needing a manual reconfigure.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._discovery_host = host
        self._discovery_device_id = device_id
        self.context["title_placeholders"] = {
            "name": f"Pool Heatpump ({host})"
        }
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step of the discovery flow: ask for the local_key only,
        validate, and create the entry."""
        assert self._discovery_host is not None
        assert self._discovery_device_id is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {
                CONF_HOST: self._discovery_host,
                CONF_PORT: DEFAULT_PORT,
                CONF_DEVICE_ID: self._discovery_device_id,
                CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
            }
            error = await self._try_validate(candidate)
            if error is None:
                return self.async_create_entry(
                    title=f"Pool Heatpump ({self._discovery_host})",
                    data=candidate,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=_DISCOVERY_CONFIRM_SCHEMA,
            description_placeholders={"host": self._discovery_host},
            errors=errors,
        )

    @staticmethod
    async def _try_validate(data: Mapping[str, Any]) -> str | None:
        """Run _validate and translate errors to error keys.

        Returns ``None`` on success, or a translation key on failure.
        """
        try:
            await _validate(data)
        except CannotConnect:
            return "cannot_connect"
        except InvalidAuth:
            return "invalid_auth"
        except ValueError:
            # local_key length / format issue (must be 16 ASCII bytes)
            return "invalid_auth"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during validation")
            return "unknown"
        return None

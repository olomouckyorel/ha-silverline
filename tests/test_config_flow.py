"""Config flow tests — Bronze rule config-flow-test-coverage requires 100%."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pysilverline import CannotConnect, InvalidAuth
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.poolex_silverline.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    DEFAULT_PORT,
    DOMAIN,
)

from .conftest import DEVICE_ID, ENTRY_DATA, HOST, LOCAL_KEY


async def _start_user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_user_flow_happy_path(hass: HomeAssistant, mock_client_factory) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    assert result["title"].startswith("Pool Heatpump")
    assert result["result"].unique_id == DEVICE_ID


@pytest.mark.parametrize(
    "exc,expected_error",
    [
        (CannotConnect("nope"), "cannot_connect"),
        (InvalidAuth("bad key"), "invalid_auth"),
        (ValueError("local_key must be 16 ASCII characters"), "invalid_auth"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_validation_errors(
    hass: HomeAssistant, mock_client_factory, exc: Exception, expected_error: str
) -> None:
    mock_client_factory.get_status.side_effect = exc
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    # Recover: clear side_effect, retry, expect success
    mock_client_factory.get_status.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ENTRY_DATA
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_when_already_configured(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_happy_path(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_KEY: "fedcba9876543210"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_LOCAL_KEY] == "fedcba9876543210"


async def test_reauth_flow_rejects_bad_key(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    mock_client_factory.get_status.side_effect = InvalidAuth("still bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_KEY: "still_wrong_keyy"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_flow_happy_path(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_data = {**ENTRY_DATA, CONF_HOST: "10.0.0.99", CONF_PORT: 6669}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_data
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.0.0.99"
    assert config_entry.data[CONF_PORT] == 6669


async def test_reconfigure_flow_rejects_device_id_change(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    new_data = {**ENTRY_DATA, CONF_DEVICE_ID: "different_device_id_22"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_data
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_id_mismatch"


async def test_reconfigure_flow_validation_failure(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)

    mock_client_factory.get_status.side_effect = CannotConnect("offline")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ENTRY_DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_disconnect_called_after_validation(
    hass: HomeAssistant, mock_client_factory
) -> None:
    """The validation helper must close the socket even on success."""
    flow_id = await _start_user_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert mock_client_factory.disconnect.called


# ---------------------------------------------------------------------------
# Integration-discovery flow (Gold rules `discovery` + `discovery-update-info`)
# ---------------------------------------------------------------------------


async def test_discovery_flow_happy_path(
    hass: HomeAssistant, mock_client_factory
) -> None:
    """A UDP broadcast triggers the flow; user supplies local_key only;
    entry is created with host + device_id from the broadcast."""
    from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": DEVICE_ID, "ip": HOST, "version": "3.3"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    # description_placeholders carries the discovered host so the UI
    # can show "Discovered at <ip>".
    assert result["description_placeholders"] == {"host": HOST}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_KEY: LOCAL_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_LOCAL_KEY] == LOCAL_KEY
    assert result["result"].unique_id == DEVICE_ID


async def test_discovery_updates_host_on_existing_entry(
    hass: HomeAssistant, mock_client_factory, config_entry: MockConfigEntry
) -> None:
    """If the device is already configured but appears at a NEW IP, the
    discovery flow aborts as already_configured but rewrites
    entry.data[CONF_HOST] in place — that's Gold `discovery-update-info`."""
    from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY

    config_entry.add_to_hass(hass)
    assert config_entry.data[CONF_HOST] == HOST

    new_ip = "10.0.0.99"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": DEVICE_ID, "ip": new_ip, "version": "3.3"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == new_ip


async def test_discovery_invalid_key_re_prompts(
    hass: HomeAssistant, mock_client_factory
) -> None:
    """If the user enters a wrong local_key in the discovery confirm step,
    the form is shown again with the invalid_auth error key, not aborted."""
    from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": DEVICE_ID, "ip": HOST, "version": "3.3"},
    )
    mock_client_factory.get_status = AsyncMock(side_effect=InvalidAuth("nope"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_KEY: "wrong-key-123456"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["errors"] == {"base": "invalid_auth"}

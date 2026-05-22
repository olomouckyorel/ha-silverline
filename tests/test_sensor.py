"""Sensor tests — value_fn results, fault decoding, availability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pysilverline import DeviceState
from syrupy.assertion import SnapshotAssertion


async def test_diagnostic_sensors_populate(
    hass: HomeAssistant, init_integration
) -> None:
    state = hass.states.get("sensor.pool_heatpump_water_inlet_temperature")
    assert state is not None
    assert state.state == "26"

    state = hass.states.get("sensor.pool_heatpump_water_outlet_temperature")
    assert state is not None
    assert state.state == "28"

    state = hass.states.get("sensor.pool_heatpump_compressor_actual_frequency")
    assert state is not None
    assert state.state == "63"


async def test_fault_code_decoded_to_enum_state(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    coordinator = init_integration.runtime_data

    coordinator.async_set_updated_data(DeviceState.from_dps({"13": 0}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pool_heatpump_fault_code").state == "none"

    coordinator.async_set_updated_data(DeviceState.from_dps({"13": 1}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pool_heatpump_fault_code").state == "E03"

    coordinator.async_set_updated_data(DeviceState.from_dps({"13": 2}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pool_heatpump_fault_code").state == "E04"

    coordinator.async_set_updated_data(DeviceState.from_dps({"13": 1 << 25}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pool_heatpump_fault_code").state == "unknown"


async def test_sensor_unavailable_when_dp_missing(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    """If the firmware doesn't expose DPs 101–110, those sensors must
    surface as unavailable rather than blowing up the integration."""
    coordinator = init_integration.runtime_data
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "4": "Heat", "3": 25, "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_water_inlet_temperature")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_temperature_delta_positive(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    """target > current → positive delta."""
    coordinator = init_integration.runtime_data
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "2": 30, "3": 28, "4": "Heat", "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_temperature_delta")
    assert state is not None
    assert state.state == "2"


async def test_temperature_delta_negative(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    """target < current → negative delta."""
    coordinator = init_integration.runtime_data
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "2": 24, "3": 28, "4": "Heat", "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_temperature_delta")
    assert state is not None
    assert state.state == "-4"


async def test_temperature_delta_zero(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    """target == current → 0 delta."""
    coordinator = init_integration.runtime_data
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "2": 27, "3": 27, "4": "Heat", "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_temperature_delta")
    assert state is not None
    assert state.state == "0"


async def test_temperature_delta_unavailable_when_dp_missing(
    hass: HomeAssistant, mock_client_factory, init_integration
) -> None:
    """If DP 2 or DP 3 is missing, the delta sensor reports unavailable."""
    coordinator = init_integration.runtime_data
    # Missing DP 2 (target).
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "3": 28, "4": "Heat", "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_temperature_delta")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    # Missing DP 3 (current).
    coordinator.async_set_updated_data(
        DeviceState.from_dps({"1": True, "2": 28, "4": "Heat", "13": 0})
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.pool_heatpump_temperature_delta")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
def _heating_state(target: int = 28, current: int = 26) -> DeviceState:
    """Build a DeviceState that compute_hvac_action resolves to HEATING:
    power on, Heat mode, current<target, no DP 108 (so the temp-delta
    fallback path decides — keeps this test independent of frequency)."""
    return DeviceState.from_dps(
        {"1": True, "2": target, "3": current, "4": "Heat", "13": 0}
    )


def _idle_state() -> DeviceState:
    """Same Heat mode but current>=target -> compute_hvac_action == IDLE."""
    return DeviceState.from_dps(
        {"1": True, "2": 26, "3": 28, "4": "Heat", "13": 0}
    )


def _off_state() -> DeviceState:
    return DeviceState.from_dps({"1": False, "4": "Heat", "13": 0})


async def test_runtime_today_accumulates_while_heating(
    hass: HomeAssistant, init_integration
) -> None:
    """Two ticks 60s apart with hvac_action=HEATING should add ~60s to
    the accumulator. The first tick only anchors the clock (can't measure
    an interval from a single point), so the increment shows up after
    the second push."""
    coordinator = init_integration.runtime_data
    # Local-midnight-safe baseline: pick a time well away from a day
    # boundary so the reset-on-midnight branch doesn't fire here.
    t0 = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t0,
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()
    # First tick: clock anchored, nothing accumulated yet.
    assert coordinator._runtime_today_seconds == 0.0

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t0 + timedelta(seconds=60),
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 60.0

    # Verify the sensor surface picks up the accumulated value.
    state = hass.states.get("sensor.pool_heatpump_runtime_today")
    assert state is not None
    assert float(state.state) == 60.0


async def test_runtime_today_does_not_grow_when_idle(
    hass: HomeAssistant, init_integration
) -> None:
    """IDLE and OFF must not contribute to the accumulator regardless
    of how much wall time passes between ticks."""
    coordinator = init_integration.runtime_data
    t0 = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t0,
    ):
        coordinator.async_set_updated_data(_idle_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 0.0

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t0 + timedelta(seconds=120),
    ):
        coordinator.async_set_updated_data(_idle_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 0.0

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t0 + timedelta(seconds=300),
    ):
        coordinator.async_set_updated_data(_off_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 0.0


async def test_runtime_today_resets_at_local_midnight(
    hass: HomeAssistant, init_integration
) -> None:
    """When a tick lands on a calendar day different from the previous
    tick's local date, the accumulator resets to 0 — owners want today's
    runtime to mean exactly today, not a 24h rolling window."""
    coordinator = init_integration.runtime_data
    # HA's test config uses US/Pacific (UTC-7 in May), so the local
    # midnight rolling May 22 -> May 23 happens at 07:00 UTC on May 23.
    # 06:00 UTC May 23 and 08:00 UTC May 23 straddle that boundary.
    t_late = datetime(2026, 5, 23, 6, 0, 0, tzinfo=timezone.utc)

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t_late,
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()

    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t_late + timedelta(seconds=60),
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 60.0

    # Now cross local midnight by jumping past 07:00 UTC.
    t_next = datetime(2026, 5, 23, 8, 0, 0, tzinfo=timezone.utc)
    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t_next,
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()
    # Reset branch: counter zeroed, clock re-anchored on the new day.
    assert coordinator._runtime_today_seconds == 0.0

    # And the new day starts accumulating fresh from the re-anchor.
    with patch(
        "custom_components.poolex_silverline.coordinator.dt_util.utcnow",
        return_value=t_next + timedelta(seconds=45),
    ):
        coordinator.async_set_updated_data(_heating_state())
    await hass.async_block_till_done()
    assert coordinator._runtime_today_seconds == 45.0


async def test_entity_inventory_snapshot(
    hass: HomeAssistant,
    init_integration,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot the entity registry + canonical "powered + heating" states.

    Catches regressions where: an entity is renamed/removed, a default
    state changes shape, a unit/device_class is altered, or an attribute
    appears/disappears unexpectedly. Update with --snapshot-update if the
    change is intentional."""
    registry = er.async_get(hass)
    entries = sorted(
        (e for e in registry.entities.values() if e.config_entry_id == init_integration.entry_id),
        key=lambda e: e.entity_id,
    )
    assert {e.entity_id: registry.async_get(e.entity_id) for e in entries} == snapshot(
        name="entity_registry"
    )
    # Only entities that are actually enabled produce a state; some
    # diagnostic DPs are disabled-by-default.
    states = sorted(
        (
            s
            for e in entries
            if (s := hass.states.get(e.entity_id)) is not None
        ),
        key=lambda s: s.entity_id,
    )
    assert states == snapshot(name="entity_states")

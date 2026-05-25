"""Unit tests for util.compute_hvac_action — covers branches not
naturally exercised by the climate-entity integration tests, especially
the COOL and HEAT_COOL paths gated on DP-108 actual_frequency."""

from __future__ import annotations

from homeassistant.components.climate.const import HVACAction
from pysilverline import DeviceState

from custom_components.poolex_silverline.util import compute_hvac_action


def test_compute_hvac_action_cool_idle_when_actual_frequency_zero() -> None:
    """Cool mode + DP 108 == 0 is authoritative: compressor parked → IDLE,
    independent of the temp delta. Without this branch the temp-delta
    fallback would say COOLING any time the pool is over setpoint."""
    state = DeviceState.from_dps({"1": True, "4": "Cool", "2": 22, "3": 25, "108": 0})
    assert compute_hvac_action(state) is HVACAction.IDLE


def test_compute_hvac_action_heat_cool_idle_when_actual_frequency_zero() -> None:
    """HEAT_COOL/Auto + DP 108 == 0 → IDLE regardless of temp delta.
    Mirrors the COOL branch above so the climate icon doesn't claim
    HEATING/COOLING when the compressor is parked."""
    state = DeviceState.from_dps({"1": True, "4": "Auto", "2": 27, "3": 25, "108": 0})
    assert compute_hvac_action(state) is HVACAction.IDLE


def test_compute_hvac_action_heat_cool_idle_when_at_target() -> None:
    """HEAT_COOL with no DP 108 and current==target falls through to the
    IDLE return at the end of the HEAT_COOL block — neither heating nor
    cooling is needed."""
    state = DeviceState.from_dps({"1": True, "4": "Auto", "2": 27, "3": 27})
    assert compute_hvac_action(state) is HVACAction.IDLE

"""Typed data models for device state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import const
from .layouts import DpLayout, LAYOUT_STANDARD


@dataclass(slots=True, kw_only=True, frozen=True)
class DeviceState:
    """Snapshot of all known DPs at a point in time. Missing DPs are None."""

    power: bool | None = None
    temp_set: int | None = None
    temp_current: int | None = None
    mode: str | None = None
    fault: int | None = None
    suction_temp: int | None = None
    ambient_temp: int | None = None
    pool_temp: int | None = None
    discharge_temp: int | None = None
    inlet_temp: int | None = None
    outlet_temp: int | None = None
    outdoor_coil_temp: int | None = None
    indoor_coil_temp: int | None = None
    target_frequency: int | None = None
    actual_frequency: int | None = None
    eev_steps: int | None = None
    fan_speed: int | None = None
    aux_valve_opening: int | None = None
    water_pump: bool | None = None
    water_pump_rpm: int | None = None
    condensing_temp: int | None = None
    evaporating_temp: int | None = None
    superheat: int | None = None
    compressor_load: int | None = None
    total_hours: int | None = None
    target_superheat: int | None = None
    target_condensing: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dps(
        cls, dps: dict[str, Any], *, layout: DpLayout = LAYOUT_STANDARD
    ) -> DeviceState:
        """Build a DeviceState from a Tuya ``dps`` mapping (string keys)."""

        def _bool(dp: int) -> bool | None:
            value = dps.get(str(dp))
            return value if isinstance(value, bool) else None

        def _pump(dp: int | None) -> bool | None:
            if dp is None:
                return None
            value = dps.get(str(dp))
            if isinstance(value, bool):
                return value
            if isinstance(value, int):
                return value != 0
            return None

        def _int(dp: int | None) -> int | None:
            if dp is None:
                return None
            value = dps.get(str(dp))
            if isinstance(value, bool):
                return None
            return value if isinstance(value, int) else None

        def _str(dp: int) -> str | None:
            value = dps.get(str(dp))
            return value if isinstance(value, str) else None

        def _pump_rpm(dp: int | None) -> int | None:
            if dp is None:
                return None
            value = dps.get(str(dp))
            if isinstance(value, bool):
                return None
            return value if isinstance(value, int) else None

        return cls(
            power=_bool(const.DP_POWER),
            temp_set=_int(const.DP_TEMP_SET),
            temp_current=_int(const.DP_TEMP_CURRENT),
            mode=_str(const.DP_MODE),
            fault=_int(const.DP_FAULT),
            suction_temp=_int(layout.suction_temp),
            ambient_temp=_int(layout.ambient_temp),
            pool_temp=_int(layout.pool_temp),
            discharge_temp=_int(layout.discharge_temp),
            inlet_temp=_int(layout.inlet_temp),
            outlet_temp=_int(layout.outlet_temp),
            outdoor_coil_temp=_int(layout.outdoor_coil_temp),
            indoor_coil_temp=_int(layout.indoor_coil_temp),
            target_frequency=_int(layout.target_frequency),
            actual_frequency=_int(layout.actual_frequency),
            eev_steps=_int(layout.eev_steps),
            fan_speed=_int(layout.fan_speed),
            aux_valve_opening=_int(layout.aux_valve_opening),
            water_pump=_pump(layout.water_pump),
            water_pump_rpm=_pump_rpm(layout.water_pump),
            condensing_temp=_int(layout.condensing_temp),
            evaporating_temp=_int(layout.evaporating_temp),
            superheat=_int(layout.superheat),
            compressor_load=_int(layout.compressor_load),
            total_hours=_int(layout.total_hours),
            target_superheat=_int(layout.target_superheat),
            target_condensing=_int(layout.target_condensing),
            raw=dict(dps),
        )

    def merge(
        self, dps: dict[str, Any], *, layout: DpLayout = LAYOUT_STANDARD
    ) -> DeviceState:
        merged = {**self.raw, **dps}
        return DeviceState.from_dps(merged, layout=layout)

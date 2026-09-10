from __future__ import annotations

from math import ceil
from dataclasses import dataclass
from typing import Optional


@dataclass
class CapacityPlan:
    """Forward sizing: how many tables are needed to hit a DC:AC oversizing target."""
    ac_capacity_mw: float
    dc_oversizing_percent: float
    required_dc_capacity_mw: float
    required_modules: int
    required_tables: int


@dataclass
class CapacityCheck:
    """Reverse check: what an actual (built or generated) layout achieves."""
    total_modules: int
    dc_capacity_mw: float
    ac_capacity_mw: Optional[float]
    dc_ac_ratio: Optional[float]
    dc_ac_oversizing_percent: Optional[float]


@dataclass
class CapacityRequirement:
    """
    Full MMS/module/string breakdown for a given DC target -- used
    regardless of how that target was arrived at (direct DC entry, AC +
    oversizing %, or AC + DC entered together).
    """
    target_dc_mwp: float
    required_modules: int
    required_tables: int
    required_strings: Optional[int]


@dataclass
class CapacityPlanFromTargets:
    """Forward sizing when the user supplies BOTH the AC and DC targets
    directly, rather than deriving DC from an oversizing percentage."""
    target_ac_mw: float
    target_dc_mwp: float
    inverter_rating_kw: float
    num_inverters_required: int
    required_modules: int
    required_tables: int
    required_strings: Optional[int]
    dc_ac_ratio: float
    dc_ac_oversizing_percent: float


def compute_capacity_requirement(
    target_dc_mwp: float,
    module_power_wp: float,
    modules_per_table: int,
    modules_per_string: Optional[int] = None,
) -> CapacityRequirement:
    """
    tables = ceil( ceil(target_dc_mwp x 1,000,000 / module_Wp) / modules_per_table )
    strings = ceil( total_modules / modules_per_string ), if given.
    """
    if target_dc_mwp <= 0:
        raise ValueError("target_dc_mwp must be positive.")
    if module_power_wp <= 0 or modules_per_table <= 0:
        raise ValueError("module_power_wp and modules_per_table must be positive.")

    required_modules = ceil(target_dc_mwp * 1_000_000 / module_power_wp)
    required_tables = ceil(required_modules / modules_per_table)
    required_strings = ceil(required_modules / modules_per_string) if modules_per_string else None

    return CapacityRequirement(
        target_dc_mwp=target_dc_mwp,
        required_modules=required_modules,
        required_tables=required_tables,
        required_strings=required_strings,
    )


def compute_capacity_plan_from_targets(
    target_ac_mw: float,
    target_dc_mwp: float,
    module_power_wp: float,
    modules_per_table: int,
    inverter_rating_kw: float,
    modules_per_string: Optional[int] = None,
) -> CapacityPlanFromTargets:
    """
    Sizes the plant from BOTH a required AC (MW) and DC (MWp) target,
    entered directly by the user -- rather than deriving DC from AC via an
    oversizing percentage.

        Required modules  = ceil(DC_MWp x 1,000,000 / module_Wp)
        Required tables    = ceil(required_modules / modules_per_table)
        Required strings   = ceil(required_modules / modules_per_string)
        Inverters required = ceil(AC_MW x 1,000 / inverter_rating_kW)

    inverter_rating_kw comes from the project config (electrical section),
    not from the user -- only the AC/DC target megawatt figures are
    user-supplied; the inverter count needed to hit the AC target is
    derived, not entered.
    """
    if target_ac_mw <= 0:
        raise ValueError("target_ac_mw must be positive.")
    if inverter_rating_kw <= 0:
        raise ValueError("inverter_rating_kw (from config) must be positive.")

    requirement = compute_capacity_requirement(
        target_dc_mwp, module_power_wp, modules_per_table, modules_per_string
    )
    num_inverters_required = ceil(target_ac_mw * 1000 / inverter_rating_kw)
    dc_ac_ratio = target_dc_mwp / target_ac_mw
    dc_ac_oversizing_percent = (dc_ac_ratio - 1) * 100

    return CapacityPlanFromTargets(
        target_ac_mw=target_ac_mw,
        target_dc_mwp=target_dc_mwp,
        inverter_rating_kw=inverter_rating_kw,
        num_inverters_required=num_inverters_required,
        required_modules=requirement.required_modules,
        required_tables=requirement.required_tables,
        required_strings=requirement.required_strings,
        dc_ac_ratio=dc_ac_ratio,
        dc_ac_oversizing_percent=dc_ac_oversizing_percent,
    )


def compute_capacity_plan(
    module_power_wp: float,
    modules_per_table: int,
    inverter_rating_kw: float,
    num_inverters: int,
    dc_oversizing_percent: float,
) -> CapacityPlan:
    """
    Standard EPC-style DC sizing, driven by AC (inverter) capacity rather
    than an arbitrary DC target:

        AC capacity      = inverter_rating_kW x num_inverters
        Required DC      = AC capacity x (1 + oversizing% / 100)
        Required modules = Required DC (Wp) / module Wp
        Required tables  = ceil(required_modules / modules_per_table)

    Rounding is always up, so the built plant never falls short of the
    requested oversizing ratio.
    """
    if module_power_wp <= 0 or modules_per_table <= 0:
        raise ValueError("module_power_wp and modules_per_table must be positive.")
    if inverter_rating_kw <= 0 or num_inverters <= 0:
        raise ValueError("inverter_rating_kw and num_inverters must be positive.")

    ac_capacity_mw = (inverter_rating_kw * num_inverters) / 1000
    required_dc_capacity_mw = ac_capacity_mw * (1 + dc_oversizing_percent / 100)
    required_modules = ceil(required_dc_capacity_mw * 1_000_000 / module_power_wp)
    required_tables = ceil(required_modules / modules_per_table)

    return CapacityPlan(
        ac_capacity_mw=ac_capacity_mw,
        dc_oversizing_percent=dc_oversizing_percent,
        required_dc_capacity_mw=required_dc_capacity_mw,
        required_modules=required_modules,
        required_tables=required_tables,
    )


def evaluate_capacity(
    total_tables: int,
    modules_per_table: int,
    module_power_wp: float,
    inverter_rating_kw: Optional[float] = None,
    num_inverters: Optional[int] = None,
) -> CapacityCheck:
    """
    Reverse direction: given an actual table count (built or generated),
    reports the achieved DC capacity and -- if inverter data is available
    -- the resulting DC:AC ratio and oversizing percentage, exactly like
    "STEP 3-5" of the reference calculator.
    """
    total_modules = total_tables * modules_per_table
    dc_capacity_mw = (total_modules * module_power_wp) / 1_000_000

    ac_capacity_mw = None
    dc_ac_ratio = None
    dc_ac_oversizing_percent = None
    if inverter_rating_kw and num_inverters:
        ac_capacity_mw = (inverter_rating_kw * num_inverters) / 1000
        if ac_capacity_mw > 0:
            dc_ac_ratio = dc_capacity_mw / ac_capacity_mw
            dc_ac_oversizing_percent = (dc_ac_ratio - 1) * 100

    return CapacityCheck(
        total_modules=total_modules,
        dc_capacity_mw=dc_capacity_mw,
        ac_capacity_mw=ac_capacity_mw,
        dc_ac_ratio=dc_ac_ratio,
        dc_ac_oversizing_percent=dc_ac_oversizing_percent,
    )

# End of core/capacity.py

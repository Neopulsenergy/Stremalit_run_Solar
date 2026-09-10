"""
Solar PV plant sizing engine.

Implements the Solar PV Design Engineer specification exactly.

Key point: Total Modules is a USER INPUT, and

    Total Tables in Plant = Total Modules / Modules per Table

Total tables is therefore NOT derived from
(tables per inverter x number of inverters). Those two can legitimately
differ; the difference is reported rather than hidden. "Theoretical
Calculation" and "Actual Layout Values" are always kept separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional

STRING_INVERTER = "string_inverter"
CENTRALIZED_INVERTER = "centralized_inverter"


class MissingInputError(ValueError):
    """Raised when a required input is absent. Per the spec the engine asks
    for the value rather than assuming one."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _positive(value, name: str) -> float:
    if value is None:
        raise MissingInputError(f"{name} is required. Please supply it -- it will not be assumed.")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}.")
    if numeric <= 0:
        raise ValueError(f"{name} must be greater than zero, got {numeric}.")
    return numeric


def validate_inputs(plant_ac_mw, plant_dc_mwp, module_rating_wp, approved_string_size,
                    modules_per_table, strings_per_scb=None, plant_type=STRING_INVERTER) -> List[str]:
    """Runs the spec's validation checks. Returns the warning list."""
    warnings: List[str] = []

    _positive(plant_ac_mw, "Plant AC capacity (MW)")
    _positive(plant_dc_mwp, "Plant DC capacity (MWp)")
    _positive(module_rating_wp, "Module rating (Wp)")
    _positive(approved_string_size, "Approved string size")
    _positive(modules_per_table, "Modules per table")

    if float(plant_dc_mwp) <= float(plant_ac_mw):
        raise ValueError(
            f"DC capacity ({plant_dc_mwp} MWp) must be greater than AC capacity "
            f"({plant_ac_mw} MW). A utility-scale plant is DC-oversized."
        )

    if plant_type == CENTRALIZED_INVERTER:
        if strings_per_scb is None:
            raise MissingInputError(
                "Number of strings per SCB is required for a Centralized Inverter design. "
                "Please supply it -- it will not be assumed."
            )
        _positive(strings_per_scb, "Strings per SCB")

    if int(modules_per_table) % int(approved_string_size) != 0:
        warnings.append("Warning: Table contains partially filled strings.")

    return warnings


# ---------------------------------------------------------------------------
# Common calculations
# ---------------------------------------------------------------------------

def calculate_dc_ac_ratio(plant_dc_mwp, plant_ac_mw) -> float:
    """Step 1 -- DC/AC Ratio = Plant DC Capacity / Plant AC Capacity"""
    return (_positive(plant_dc_mwp, "Plant DC capacity (MWp)")
            / _positive(plant_ac_mw, "Plant AC capacity (MW)"))


def calculate_modules_per_table(mms_rows, mms_columns) -> int:
    """Step 2 -- Modules per Table = A x B (from an A x B MMS configuration)."""
    rows = int(_positive(mms_rows, "MMS rows"))
    cols = int(_positive(mms_columns, "MMS columns"))
    return rows * cols


def calculate_power_of_one_string_kw(approved_string_size, module_rating_wp) -> float:
    """Step 3 -- Power of One String (kW) = String Size x Module Rating (kW)"""
    size = _positive(approved_string_size, "Approved string size")
    rating_kw = _positive(module_rating_wp, "Module rating (Wp)") / 1000.0
    return size * rating_kw


def calculate_number_of_strings(total_modules, approved_string_size) -> int:
    """String Inverter Step 4 -- Number of Strings = Total Modules / Approved String Size"""
    return round(_positive(total_modules, "Total number of modules")
                 / _positive(approved_string_size, "Approved string size"))


def calculate_number_of_inverters(plant_ac_mw, inverter_rating_kw) -> int:
    """Number of Inverters = Plant AC Capacity (kW) / Single Inverter Rating (kW)"""
    ac_kw = _positive(plant_ac_mw, "Plant AC capacity (MW)") * 1000.0
    return round(ac_kw / _positive(inverter_rating_kw, "AC rating of one inverter (kW)"))


def calculate_strings_per_inverter(inverter_rating_kw, dc_ac_ratio, power_of_one_string_kw) -> int:
    """Strings per Inverter = (AC Rating x DC/AC Ratio) / Power of One String, nearest integer."""
    numerator = (_positive(inverter_rating_kw, "AC rating of one inverter (kW)")
                 * _positive(dc_ac_ratio, "DC/AC ratio"))
    return round(numerator / _positive(power_of_one_string_kw, "Power of one string (kW)"))


def calculate_strings_per_table(modules_per_table, approved_string_size) -> float:
    """Strings per Table = Modules per Table / Approved String Size"""
    return (_positive(modules_per_table, "Modules per table")
            / _positive(approved_string_size, "Approved string size"))


def calculate_tables_per_inverter(strings_per_inverter, strings_per_table) -> float:
    """String Inverter Step 8 -- Tables per Inverter = Strings per Inverter / Strings per Table"""
    return (_positive(strings_per_inverter, "Strings per inverter")
            / _positive(strings_per_table, "Strings per table"))


def calculate_total_tables(total_modules, modules_per_table) -> int:
    """Total Tables in Plant = Total Modules / Modules per Table"""
    return ceil(_positive(total_modules, "Total number of modules")
                / _positive(modules_per_table, "Modules per table"))


def calculate_scbs_per_inverter(strings_per_inverter, strings_per_scb) -> int:
    """Centralized Step 3 -- SCBs per Inverter = Strings per Inverter / Strings per SCB.
    Always rounds UP: half an SCB cannot exist."""
    return ceil(_positive(strings_per_inverter, "Strings per inverter")
                / _positive(strings_per_scb, "Strings per SCB"))


def calculate_tables_per_scb(strings_per_scb, strings_per_table) -> float:
    """Centralized Step 4 -- Tables per SCB = Strings per SCB / Strings per Table"""
    return (_positive(strings_per_scb, "Strings per SCB")
            / _positive(strings_per_table, "Strings per table"))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class PlantCalculation:
    plant_type: str

    plant_ac_mw: float
    plant_dc_mwp: float
    module_rating_wp: float
    total_modules: int
    inverter_rating_kw: float
    approved_string_size: int
    mms_rows: int
    mms_columns: int
    strings_per_scb: Optional[int] = None

    dc_ac_ratio: float = 0.0
    modules_per_table: int = 0
    power_of_one_string_kw: float = 0.0

    number_of_strings: int = 0
    number_of_inverters: int = 0
    strings_per_inverter: int = 0
    strings_per_table: float = 0.0
    tables_per_inverter: float = 0.0
    total_tables: int = 0
    scbs_per_inverter: Optional[int] = None
    tables_per_scb: Optional[float] = None

    warnings: List[str] = field(default_factory=list)

    # -- shims used by the layout pipeline --------------------------------
    @property
    def inverter_type(self) -> str:
        return self.plant_type

    @property
    def total_strings(self) -> int:
        return self.number_of_strings

    @property
    def total_dc_capacity_mwp(self) -> float:
        return self.total_modules * self.module_rating_wp / 1_000_000

    def project_summary(self) -> List[str]:
        return [
            f"Plant AC Capacity          : {self.plant_ac_mw:.3f} MW",
            f"Plant DC Capacity          : {self.plant_dc_mwp:.3f} MWp",
            f"DC/AC Ratio                : {self.dc_ac_ratio:.3f}",
            f"Module Rating              : {self.module_rating_wp:.0f} Wp",
            f"Approved String Size       : {self.approved_string_size} modules",
            f"Modules per Table          : {self.modules_per_table} ({self.mms_rows}x{self.mms_columns})",
            f"Power of One String        : {self.power_of_one_string_kw:.3f} kW",
        ]

    def result_lines(self) -> List[str]:
        if self.plant_type == CENTRALIZED_INVERTER:
            return [
                f"Number of Inverters        : {self.number_of_inverters}",
                f"Strings per Inverter       : {self.strings_per_inverter}",
                f"SCBs per Inverter          : {self.scbs_per_inverter}",
                f"Tables per SCB             : {self.tables_per_scb:.2f}",
                f"Tables per Inverter        : {self.tables_per_inverter:.2f}",
                f"Total Tables               : {self.total_tables}",
            ]
        return [
            f"Number of Strings          : {self.number_of_strings}",
            f"Number of Inverters        : {self.number_of_inverters}",
            f"Strings per Inverter       : {self.strings_per_inverter}",
            f"Strings per Table          : {self.strings_per_table:.2f}",
            f"Tables per Inverter        : {self.tables_per_inverter:.2f}",
            f"Total Tables               : {self.total_tables}",
        ]

    def layout_summary(self) -> List[str]:
        lines = ["Plant", f"  -> {self.number_of_inverters} Inverters"]
        if self.plant_type == CENTRALIZED_INVERTER:
            lines.append(f"     -> {self.scbs_per_inverter} SCBs per Inverter")
            lines.append(f"        -> {self.tables_per_scb:.2f} Tables per SCB")
        else:
            lines.append(f"     -> {self.tables_per_inverter:.2f} Tables per Inverter")
        lines.append(f"           -> {self.strings_per_table:.2f} Strings per Table")
        lines.append(f"              -> {self.approved_string_size} Modules per String")
        lines.append(
            f"Total: {self.total_tables} Tables / {self.number_of_strings} Strings "
            f"/ {self.total_modules} Modules"
        )
        return lines

    def summary_lines(self) -> List[str]:
        """THEORETICAL calculation only -- never mixed with actual layout values."""
        out = ["PROJECT SUMMARY"] + self.project_summary()
        label = ("CENTRALIZED INVERTER RESULTS" if self.plant_type == CENTRALIZED_INVERTER
                 else "STRING INVERTER RESULTS")
        out += ["", label] + self.result_lines()
        out += ["", "LAYOUT SUMMARY"] + self.layout_summary()
        return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def calculatePlant(
    plant_ac_mw: float,
    plant_dc_mwp: float,
    module_rating_wp: float,
    total_modules: int,
    inverter_rating_kw: float,
    approved_string_size: int,
    mms_rows: int,
    mms_columns: int,
    plant_type: str = STRING_INVERTER,
    strings_per_scb: Optional[int] = None,
) -> PlantCalculation:
    if plant_type not in (STRING_INVERTER, CENTRALIZED_INVERTER):
        raise ValueError(f"plant_type must be '{STRING_INVERTER}' or '{CENTRALIZED_INVERTER}'.")

    modules_per_table = calculate_modules_per_table(mms_rows, mms_columns)
    warnings = validate_inputs(plant_ac_mw, plant_dc_mwp, module_rating_wp, approved_string_size,
                                modules_per_table, strings_per_scb, plant_type)

    dc_ac_ratio = calculate_dc_ac_ratio(plant_dc_mwp, plant_ac_mw)
    power_string_kw = calculate_power_of_one_string_kw(approved_string_size, module_rating_wp)
    n_strings = calculate_number_of_strings(total_modules, approved_string_size)
    n_inverters = calculate_number_of_inverters(plant_ac_mw, inverter_rating_kw)
    strings_per_inv = calculate_strings_per_inverter(inverter_rating_kw, dc_ac_ratio, power_string_kw)
    strings_per_tbl = calculate_strings_per_table(modules_per_table, approved_string_size)
    total_tables = calculate_total_tables(total_modules, modules_per_table)

    result = PlantCalculation(
        plant_type=plant_type,
        plant_ac_mw=float(plant_ac_mw),
        plant_dc_mwp=float(plant_dc_mwp),
        module_rating_wp=float(module_rating_wp),
        total_modules=int(total_modules),
        inverter_rating_kw=float(inverter_rating_kw),
        approved_string_size=int(approved_string_size),
        mms_rows=int(mms_rows),
        mms_columns=int(mms_columns),
        strings_per_scb=int(strings_per_scb) if strings_per_scb else None,
        dc_ac_ratio=dc_ac_ratio,
        modules_per_table=modules_per_table,
        power_of_one_string_kw=power_string_kw,
        number_of_strings=n_strings,
        number_of_inverters=n_inverters,
        strings_per_inverter=strings_per_inv,
        strings_per_table=strings_per_tbl,
        total_tables=total_tables,
        warnings=warnings,
    )

    if plant_type == CENTRALIZED_INVERTER:
        result.scbs_per_inverter = calculate_scbs_per_inverter(strings_per_inv, strings_per_scb)
        result.tables_per_scb = calculate_tables_per_scb(strings_per_scb, strings_per_tbl)
        result.tables_per_inverter = result.tables_per_scb * result.scbs_per_inverter
        if strings_per_inv % int(strings_per_scb) != 0:
            warnings.append("Warning: Last SCB will contain fewer strings.")
    else:
        result.tables_per_inverter = calculate_tables_per_inverter(strings_per_inv, strings_per_tbl)

    if total_tables % n_inverters != 0:
        warnings.append("Warning: Last inverter will contain fewer tables.")

    return result


def generateTableGrouping(table_locations, calculation: PlantCalculation) -> List[Dict]:
    """
    Groups the ACTUAL placed tables: tables -> SCB -> Inverter for a
    centralized design, tables -> Inverter for a string-inverter design.
    """
    if calculation.plant_type == CENTRALIZED_INVERTER:
        from core.grouping import detect_and_group_centralized_2x6
        scbs_per_inv = max(1, calculation.scbs_per_inverter or 1)
        return detect_and_group_centralized_2x6(table_locations, scbs_per_inverter=scbs_per_inv)

    ordered = sorted(table_locations, key=lambda t: (-t.origin_y, t.origin_x))
    assignments: List[Dict] = []
    per_inv = max(1, round(calculation.tables_per_inverter or 1))
    for i, table in enumerate(ordered):
        assignments.append({
            "table": table,
            "scb_id": None,
            "inverter_id": i // per_inv + 1,
        })

    return assignments


def actual_layout_report(calculation: PlantCalculation, placed_tables: int) -> List[str]:
    """ACTUAL layout values, reported separately so the two are never mixed."""
    modules = placed_tables * calculation.modules_per_table
    strings = (round(modules / calculation.approved_string_size)
               if calculation.approved_string_size else 0)
    dc_mwp = modules * calculation.module_rating_wp / 1_000_000
    shortfall = calculation.total_tables - placed_tables
    lines = [
        f"Tables Placed              : {placed_tables}  (theoretical {calculation.total_tables})",
        f"Modules Placed             : {modules}  (theoretical {calculation.total_modules})",
        f"Strings Placed             : {strings}  (theoretical {calculation.number_of_strings})",
        f"DC Capacity Achieved       : {dc_mwp:.3f} MWp  (target {calculation.plant_dc_mwp:.3f} MWp)",
    ]
    if shortfall > 0:
        lines.append(
            f"Shortfall                  : {shortfall} tables "
            f"({100 * shortfall / calculation.total_tables:.1f}%) -- the land could not "
            f"accommodate the full theoretical layout."
        )
    return lines

# End of core/calculation_engine.py

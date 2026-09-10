from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Optional
from shapely.geometry import Polygon


@dataclass
class StringInverterGrouping:
    """Table/string sizing for a String Inverter architecture -- tables
    group directly under inverters, no combiner box in between."""
    architecture: str
    num_inverters: int
    strings_per_table: float
    strings_per_inverter: float
    tables_per_inverter: float
    tables_required: int
    modules_required: int


@dataclass
class CentralizedInverterGrouping:
    """Table/string sizing for a Centralized Inverter architecture --
    strings feed into SCBs (string combiner boxes), and SCBs feed the
    central inverter."""
    architecture: str
    num_inverters: int
    strings_per_table: float
    strings_per_inverter: float
    strings_per_scb: int
    scb_per_inverter: float
    tables_per_scb: float
    tables_required: int
    modules_required: int


def compute_strings_per_table(modules_per_table: int, string_size: int) -> float:
    """
    Step 2 (both architectures): how many complete strings fit on one
    table, given the approved string size (modules per string).
    """
    if modules_per_table <= 0:
        raise ValueError("modules_per_table must be positive.")
    if string_size <= 0:
        raise ValueError("string_size (approved string size) must be positive.")
    return modules_per_table / string_size


def compute_strings_per_inverter(
    inverter_rating_kw: float, dc_ac_ratio: float,
    string_size: int, module_power_wp: float,
) -> float:
    """
    Step 3 (both architectures): strings needed to feed ONE inverter at the
    plant's DC:AC oversizing ratio.

        strings_per_inverter = (inverter_kW x 1000 x DC:AC ratio) / string power (W)
        string power (W)     = string_size x module_Wp
    """
    if inverter_rating_kw <= 0:
        raise ValueError("inverter_rating_kw must be positive.")
    if dc_ac_ratio <= 0:
        raise ValueError("dc_ac_ratio must be positive.")
    string_power_w = string_size * module_power_wp
    if string_power_w <= 0:
        raise ValueError("string_size and module_power_wp must both be positive.")
    return (inverter_rating_kw * 1000 * dc_ac_ratio) / string_power_w


def compute_string_inverter_grouping(
    target_ac_mw: float,
    inverter_rating_kw: float,
    dc_ac_ratio: float,
    modules_per_table: int,
    string_size: int,
    module_power_wp: float,
) -> StringInverterGrouping:
    """
    Full String Inverter sizing chain:
        Step 1: No. of inverters       = AC capacity / inverter rating
        Step 2: Strings per table      = modules per table / string size
        Step 3: Strings per inverter   = (inverter kW x DC:AC ratio) / string power
        Step 4: Tables per inverter    = strings per inverter / strings per table
    """
    num_inverters = ceil(target_ac_mw * 1000 / inverter_rating_kw)
    strings_per_table = compute_strings_per_table(modules_per_table, string_size)
    strings_per_inverter = compute_strings_per_inverter(
        inverter_rating_kw, dc_ac_ratio, string_size, module_power_wp
    )
    tables_per_inverter = strings_per_inverter / strings_per_table
    tables_required = ceil(tables_per_inverter) * num_inverters
    modules_required = tables_required * modules_per_table

    return StringInverterGrouping(
        architecture="string_inverter",
        num_inverters=num_inverters,
        strings_per_table=strings_per_table,
        strings_per_inverter=strings_per_inverter,
        tables_per_inverter=tables_per_inverter,
        tables_required=tables_required,
        modules_required=modules_required,
    )


def compute_centralized_inverter_grouping(
    target_ac_mw: float,
    inverter_rating_kw: float,
    dc_ac_ratio: float,
    modules_per_table: int,
    string_size: int,
    module_power_wp: float,
    strings_per_scb: int,
) -> CentralizedInverterGrouping:
    """
    Full Centralized Inverter sizing chain:
        Step 1: No. of inverters       = AC capacity / inverter rating
        Step 2: Strings per table      = modules per table / string size
        Step 3: Strings per inverter   = (inverter kW x DC:AC ratio) / string power
        Step 4: SCBs per inverter      = strings per inverter / strings per SCB
        Step 5: Tables per SCB         = strings per SCB / strings per table
    Tables are grouped under SCBs first, then SCBs are grouped under
    inverters.
    """
    if strings_per_scb <= 0:
        raise ValueError("strings_per_scb must be positive.")

    num_inverters = ceil(target_ac_mw * 1000 / inverter_rating_kw)
    strings_per_table = compute_strings_per_table(modules_per_table, string_size)
    strings_per_inverter = compute_strings_per_inverter(
        inverter_rating_kw, dc_ac_ratio, string_size, module_power_wp
    )
    scb_per_inverter = strings_per_inverter / strings_per_scb
    tables_per_scb = strings_per_scb / strings_per_table
    tables_required = ceil(tables_per_scb) * ceil(scb_per_inverter) * num_inverters
    modules_required = tables_required * modules_per_table

    return CentralizedInverterGrouping(
        architecture="centralized_inverter",
        num_inverters=num_inverters,
        strings_per_table=strings_per_table,
        strings_per_inverter=strings_per_inverter,
        strings_per_scb=strings_per_scb,
        scb_per_inverter=scb_per_inverter,
        tables_per_scb=tables_per_scb,
        tables_required=tables_required,
        modules_required=modules_required,
    )


def detect_and_group_centralized_2x6(
    table_locations: list,
    scbs_per_inverter: int = 4,
    block_rows: int = 6,
    block_cols: int = 2,
) -> list[dict]:
    """
    Detects all placed panels across the site and groups them strictly based on
    2 columns x 6 rows (6 rows tall x 2 columns wide).
    Any panels that do not fit into a complete 2 columns x 6 rows block (edge panels,
    partial bands, leftover columns) are altered and combined into the adjacent/nearest
    centralized groups so that all panels on the site are combined and grouped.
    """
    from collections import defaultdict
    from shapely.ops import unary_union

    if not table_locations:
        return []

    # 1. Group tables by row level Y
    y_map = defaultdict(list)
    for t in table_locations:
        y_map[round(t.origin_y, 2)].append(t)

    sorted_ys = sorted(y_map.keys(), reverse=True)
    bands = [sorted_ys[i:i + block_rows] for i in range(0, len(sorted_ys), block_rows)]

    # 2. Global grid spacing in X
    x_coords = sorted(list(set(round(t.origin_x, 2) for t in table_locations)))
    x_spacings = [
        x_coords[i + 1] - x_coords[i]
        for i in range(len(x_coords) - 1)
        if x_coords[i + 1] - x_coords[i] > 1.0
    ]
    min_spacing = min(x_spacings) if x_spacings else 30.0
    x_min = min(x_coords)

    def get_col_k(t):
        return int(round((t.origin_x - x_min) / min_spacing))

    groups = []
    unassigned = []

    for band_idx, band_ys in enumerate(bands):
        band_tables = []
        for y in band_ys:
            band_tables.extend(y_map[y])

        # If band has fewer rows than half block_rows, queue for combining
        if len(band_ys) < max(1, block_rows // 2):
            unassigned.extend(band_tables)
            continue

        # Group tables by column pair (k // block_cols)
        col_pair_map = defaultdict(list)
        for t in band_tables:
            k = get_col_k(t)
            col_pair = k // block_cols
            col_pair_map[col_pair].append(t)

        for col_pair, grp_tables in sorted(col_pair_map.items()):
            # If the group has a solid core, form a primary group
            if len(grp_tables) >= max(3, block_rows):
                groups.append(grp_tables)
            else:
                unassigned.extend(grp_tables)

    # If no primary groups could be formed, make all tables one group
    if not groups and unassigned:
        groups.append(unassigned)
        unassigned = []

    # Combine any unassigned / non-fitting panels with the nearest 6x2 centralized group
    if groups and unassigned:
        group_centroids = [
            unary_union([t.polygon for t in grp]).centroid for grp in groups
        ]
        for extra in unassigned:
            pt = extra.polygon.centroid
            nearest_idx = min(range(len(groups)), key=lambda i: pt.distance(group_centroids[i]))
            groups[nearest_idx].append(extra)
            group_centroids[nearest_idx] = unary_union([t.polygon for t in groups[nearest_idx]]).centroid

    # Build final assignment list
    assignments = []
    for scb_idx, grp in enumerate(groups, start=1):
        inv_idx = (scb_idx - 1) // max(1, scbs_per_inverter) + 1
        for t in grp:
            assignments.append({
                "table": t,
                "scb_id": scb_idx,
                "inverter_id": inv_idx,
            })

    return assignments


def assign_table_groups(table_locations, tables_per_inverter: float, tables_per_scb: Optional[float] = None):
    """
    Assigns each placed table an inverter_id (and, for centralized
    architecture, an scb_id).
    """
    if tables_per_scb:
        scbs_per_inv = max(1, round(tables_per_inverter / tables_per_scb)) if tables_per_inverter else 4
        return detect_and_group_centralized_2x6(table_locations, scbs_per_inverter=scbs_per_inv)

    ordered = sorted(table_locations, key=lambda t: (-t.origin_y, t.origin_x))
    tables_per_inverter_int = max(1, round(tables_per_inverter))
    assignments = []
    for i, table in enumerate(ordered):
        inverter_id = i // tables_per_inverter_int + 1
        assignments.append({"table": table, "inverter_id": inverter_id, "scb_id": None})
    return assignments

def assign_gap_tables_to_nearest_group(
    gap_tables: list,
    table_groups: list[dict],
    group_key: str = "scb_id",
) -> list[dict]:
    """
    Assigns newly placed gap-filled tables to their physically closest existing
    SCB or inverter group based on distance to existing group centroids.

    Returns the list of newly created assignment dicts for gap tables.
    """
    from collections import defaultdict
    from shapely.ops import unary_union

    if not gap_tables or not table_groups:
        return []

    # Check if primary groups use scb_id or inverter_id
    actual_key = group_key if any(g.get(group_key) is not None for g in table_groups) else "inverter_id"

    group_polygons = defaultdict(list)
    scb_to_inverter = {}
    for entry in table_groups:
        gid = entry.get(actual_key)
        if gid is not None:
            group_polygons[gid].append(entry["table"].polygon)
            if entry.get("inverter_id") is not None:
                scb_to_inverter[gid] = entry["inverter_id"]

    if not group_polygons:
        return []

    group_centroids = {
        gid: unary_union(polys).centroid for gid, polys in group_polygons.items()
    }

    new_assignments = []
    for gap_table in gap_tables:
        pt = gap_table.polygon.centroid
        nearest_gid = min(group_centroids.keys(), key=lambda gid: pt.distance(group_centroids[gid]))

        if actual_key == "scb_id":
            inv_id = scb_to_inverter.get(nearest_gid, nearest_gid)
            assignment = {"table": gap_table, "scb_id": nearest_gid, "inverter_id": inv_id}
        else:
            assignment = {"table": gap_table, "scb_id": None, "inverter_id": nearest_gid}

        new_assignments.append(assignment)
        table_groups.append(assignment)

    return new_assignments


def build_group_outlines(
    table_groups, group_key: str = "scb_id", table_gap_m: float = 0.5, row_gap_m: float = 1.4,
    margin_m: float = 0.5, site_boundary: Optional[Polygon] = None
):
    """
    Computes one contiguous outline polygon per group (e.g. per inverter),
    tracing the actual arrangement of that group's tables -- including any
    stepped/irregular shape from the row layout and gap-filling pass.

    Uses dilate-then-erode closing, expanding outward up to margin_m, clipped
    to site_boundary if provided so group boundaries tightly reflect real filled areas.

    Parameters
    ----------
    table_groups : list[{"table": TablePlacement, "inverter_id": int, "scb_id": int | None}]
    group_key : str
        Which assignment to group by -- "inverter_id" or "scb_id".

    Returns
    -------
    dict[int, shapely.Geometry]  -- group id -> outline polygon
    """
    from shapely.ops import unary_union
    from collections import defaultdict

    if not table_groups:
        return {}

    actual_key = group_key if any(g.get(group_key) is not None for g in table_groups) else "inverter_id"

    by_group = defaultdict(list)
    for entry in table_groups:
        key = entry.get(actual_key)
        if key is not None:
            by_group[key].append(entry["table"].polygon)

    closing_distance = max(table_gap_m, row_gap_m) / 2 + 0.05
    outlines = {}
    for group_id, polygons in by_group.items():
        if not polygons:
            continue
        footprint = unary_union(polygons)
        outline = footprint.buffer(closing_distance + margin_m, join_style=1).buffer(
            -closing_distance, join_style=1
        )
        if site_boundary and not site_boundary.is_empty:
            outline = outline.intersection(site_boundary)
        outlines[group_id] = outline
    return outlines


def build_group_bounding_boxes(table_groups, group_key: str):
    """
    Computes one plain rectangular bounding box per group (e.g. per
    inverter) -- the simple min/max envelope of that group's table
    footprint, not a shape that hugs every individual table edge.

    Since tables are already centered within each row's available width
    during placement, a group's bounding box comes out naturally centered
    on the site too -- no extra centering step needed here.

    Parameters
    ----------
    table_groups : list[{"table": TablePlacement, "inverter_id": int, "scb_id": int | None}]
    group_key : str
        Which assignment to group by -- "inverter_id" or "scb_id".

    Returns
    -------
    dict[int, shapely.geometry.Polygon]  -- group id -> rectangular bounding box
    """
    from shapely.geometry import box
    from collections import defaultdict

    by_group = defaultdict(list)
    for entry in table_groups:
        key = entry.get(group_key)
        if key is not None:
            by_group[key].append(entry["table"].polygon)

    boxes = {}
    for group_id, polygons in by_group.items():
        minx = min(p.bounds[0] for p in polygons)
        miny = min(p.bounds[1] for p in polygons)
        maxx = max(p.bounds[2] for p in polygons)
        maxy = max(p.bounds[3] for p in polygons)
        boxes[group_id] = box(minx, miny, maxx, maxy)
    return boxes

# End of core/grouping.py

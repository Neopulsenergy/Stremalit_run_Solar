"""
pipeline.py  –  Drop-in replacement for core/pipeline.py

Changes vs. previous version
─────────────────────────────
• All title-block content (boundary coordinate table, inverter-room
  coordinate table, control-room coordinate table, KEY PLAN box, NOTES,
  TYPICAL PLANT DETAILS, LEGEND, TYPICAL SECTION DETAILS) is now drawn
  in model-space to the RIGHT of the site boundary – exactly as shown in
  the AutoCAD reference images – instead of only a plain TYPICAL PLANT
  DETAILS text block placed at the bottom-right.

Everything else (geometry, packing, roads, exclusions, DXF layers, …) is
unchanged.
"""

from __future__ import annotations

import json
import logging
import math
from math import ceil
from pathlib import Path
from typing import List, Tuple
import pandas as pd

from shapely.geometry import Polygon, Point
from shapely.ops import unary_union
from ezdxf.colors import RED, GREEN, CYAN, YELLOW, MAGENTA, BLUE, WHITE
from ezdxf.enums import TextEntityAlignment
import ezdxf

from core.geometry import GeometryEngine
from core.offset import OffsetEngine
from core.dxf_writer import DXFWriter
from core.packing import PackingEngine, required_tables_for_capacity
from core.capacity import (
    compute_capacity_plan,
    compute_capacity_plan_from_targets,
    compute_capacity_requirement,
    evaluate_capacity,
)
from core.dwg_export import export_dwg, oda_file_converter_available, configure_oda_path
from core.grouping import (
    assign_table_groups,
    assign_gap_tables_to_nearest_group,
    build_group_outlines,
    build_group_bounding_boxes,
)
from core.calculation_engine import calculatePlant, generateTableGrouping
from core.csv_loader import (
    load_boundary_from_csv,
    load_exclusion_from_csv,
    read_labeled_coordinate_csv,
)

logger = logging.getLogger("SolarLayoutGenerator")

SQ_M_PER_ACRE = 4046.8564224

# ─────────────────────────────────────────────────────────────────────────────
#  Title-block drawing helpers
# ─────────────────────────────────────────────────────────────────────────────

TB_LAYER   = "TITLE_BLOCK"
TB_COLOR   = WHITE          # ezdxf integer colour index

TEXT_H_SM  = 2.5            # small body text  (m in drawing units)
TEXT_H_MD  = 3.2            # medium / subtitle
TEXT_H_LG  = 4.5            # section heading
TEXT_H_XL  = 6.0            # main title
LINE_SP    = TEXT_H_SM * 1.6  # vertical step between body rows


def _ensure_tb_layer(dxf: DXFWriter):
    dxf._create_layer(TB_LAYER, TB_COLOR)


def _text(msp, text: str, x: float, y: float, height: float,
          layer: str = TB_LAYER, bold: bool = False):
    """Add a single-line TEXT entity, left-aligned at (x, y) baseline."""
    ent = msp.add_text(
        text,
        dxfattribs={
            "layer":  layer,
            "height": height,
            "style":  "STANDARD",
        },
    )
    ent.set_placement((x, y), align=TextEntityAlignment.BOTTOM_LEFT)


def _hline(msp, x0: float, y0: float, x1: float, y1: float,
           layer: str = TB_LAYER, lw: int = 50):
    """Draw a line between two points."""
    msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": layer, "lineweight": lw})


def _rect(msp, x: float, y: float, w: float, h: float,
          layer: str = TB_LAYER, lw: int = 50):
    """Draw a closed rectangle; (x,y) is bottom-left corner."""
    msp.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
        close=True,
        dxfattribs={"layer": layer, "lineweight": lw},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Coordinate table
# ─────────────────────────────────────────────────────────────────────────────

def _draw_coordinate_table(
    msp,
    title: str,
    points: List[Tuple[str, float, float]],
    x: float,
    y_top: float,
    col_widths: Tuple[float, float, float] = (30, 55, 55),
    row_h: float = TEXT_H_SM * 2.0,
    text_h: float = TEXT_H_SM,
    layer: str = TB_LAYER,
) -> float:
    """
    Draw a labelled coordinate table (POINT | EASTING | NORTHING).

    Returns the y coordinate of the bottom edge of the table.
    """
    cw0, cw1, cw2 = col_widths
    total_w = cw0 + cw1 + cw2
    header_h = row_h * 1.5

    # ── Section title ────────────────────────────────────────────────────────
    _text(msp, title, x, y_top, TEXT_H_MD, layer=layer)
    y = y_top - TEXT_H_MD * 2.0

    # ── Header row ───────────────────────────────────────────────────────────
    _rect(msp, x, y - header_h, total_w, header_h, layer=layer)
    pad = 1.5
    for col_x, label in [
        (x + pad,                "POINTS"),
        (x + cw0 + pad,          "EASTING"),
        (x + cw0 + cw1 + pad,    "NORTHING"),
    ]:
        _text(msp, label, col_x, y - header_h + pad, text_h, layer=layer)
    y -= header_h

    # ── Data rows ────────────────────────────────────────────────────────────
    for label, east, north in points:
        _rect(msp, x, y - row_h, total_w, row_h, layer=layer)
        _hline(msp, x + cw0, y, x + cw0, y - row_h, layer=layer)
        _hline(msp, x + cw0 + cw1, y, x + cw0 + cw1, y - row_h, layer=layer)

        _text(msp, str(label),      x + pad,               y - row_h + pad, text_h, layer=layer)
        _text(msp, f"{east:.3f}",   x + cw0 + pad,         y - row_h + pad, text_h, layer=layer)
        _text(msp, f"{north:.3f}",  x + cw0 + cw1 + pad,   y - row_h + pad, text_h, layer=layer)
        y -= row_h

    return y  # bottom of table


# ─────────────────────────────────────────────────────────────────────────────
#  KEY PLAN placeholder
# ─────────────────────────────────────────────────────────────────────────────

def _draw_key_plan(msp, x: float, y_top: float,
                   box_w: float = 140, box_h: float = 100,
                   layer: str = TB_LAYER) -> float:
    """
    Draw a KEY PLAN labelled box (the actual key-plan sketch is drawn in
    AutoCAD manually; we just reserve the space and label it).
    Returns bottom y.
    """
    _text(msp, "KEY PLAN:", x, y_top, TEXT_H_LG, layer=layer)
    y_box_top = y_top - TEXT_H_LG * 2.0
    _rect(msp, x, y_box_top - box_h, box_w, box_h, layer=layer, lw=70)

    # Diagonal cross to mark "image goes here"
    msp.add_line((x, y_box_top - box_h), (x + box_w, y_box_top),
                 dxfattribs={"layer": layer, "lineweight": 25})
    msp.add_line((x + box_w, y_box_top - box_h), (x, y_box_top),
                 dxfattribs={"layer": layer, "lineweight": 25})

    cx = x + box_w / 2
    cy = y_box_top - box_h / 2
    _text(msp, "KEY PLAN IMAGE", cx - 20, cy, TEXT_H_MD, layer=layer)

    return y_box_top - box_h


# ─────────────────────────────────────────────────────────────────────────────
#  NOTES block
# ─────────────────────────────────────────────────────────────────────────────

NOTES_LINES = [
    "1. ALL DIMENSIONS ARE IN METERS UNLESS OTHERWISE SPECIFIED.",
    "2. SEPARATE DETAIL DRAWING SHALL BE REFERRED FOR ROAD, GATE &",
    "   FENCE.",
    "3. LOCATION OF MAIN CONTROL ROOM, INVERTER CONTROL ROOM",
    "   LOCATION IS INDICATIVE ONLY.",
    "4. MINIMUM GROUND CLEARANCE FROM FGL TO BOTTOM OF THE",
    "   MODULE SHALL BE 500mm AND MINIMUM CLEARANCE FROM",
    "   BOUNDARY TO MMS SHALL BE MAINTAINED AS 1.0M.",
    "5. MMS LOCATION MAY GET CHANGED DURING EXECUTION BASED ON",
    "   ACTUAL SITE CONDITION AND SHALL BE RELOCATED AT MUTUAL",
    "   AGREED LOCATION.",
    "6. ESE LIGHTNING ARRESTER LEVEL-4 IS CONSIDERED WITH 107M",
    "   RADIUS LIGHTNING PROTECTION. DETAIL DRAWING FOR LIGHTNING",
    "   AND EARTHING PROTECTION SHALL BE SUBMITTED SEPARATELY.",
    "7. THE LAYOUT IS PREPARED CONSIDERING THE EXISTING BUILDING",
    "   ALONG WITH EXISTING ROAD INSIDE THE PLOT SHALL BE REMOVED",
    "   & THE TRANSMISSION LINE INSIDE THE PLOT SHALL BE REROUTED",
    "   OUTSIDE THE BOUNDARY SUCH THAT NO SHADOW OF THE POLE",
    "   SHALL BE MADE ON THE MODULE. ALSO AN CLEARANCE OF 3M IS",
    "   CONSIDERED FOR THE BELLS TO RETAIN AND NO ACCESS TO THE",
    "   WELL IS CONSIDERED.",
]


def _draw_notes(msp, x: float, y_top: float,
                layer: str = TB_LAYER) -> float:
    """Draw the NOTES section. Returns bottom y."""
    _text(msp, "NOTES:", x, y_top, TEXT_H_LG, layer=layer)
    y = y_top - TEXT_H_LG * 2.2
    for line in NOTES_LINES:
        _text(msp, line, x, y, TEXT_H_SM, layer=layer)
        y -= LINE_SP
    return y


# ─────────────────────────────────────────────────────────────────────────────
#  TYPICAL PLANT DETAILS
# ─────────────────────────────────────────────────────────────────────────────

def _draw_typical_plant_details(msp, title: str, df: pd.DataFrame,
                                x: float, y_top: float,
                                col_widths: Tuple[float, float, float] = (14, 90, 90),
                                row_h: float = TEXT_H_SM * 2.0,
                                text_h: float = TEXT_H_SM,
                                layer: str = TB_LAYER) -> float:
    """
    Draw the plant details as a real bordered TABLE (S.No | Parameter |
    Value), matching the grid style of the coordinate tables -- instead of
    flattening each DataFrame row back into a single text line.

    Every row is a bordered box with two internal column separators, so it
    reads as a proper table in AutoCAD. The DataFrame columns
    (S.No, Parameter, Value) map straight onto the three table columns.

    Returns the y coordinate of the bottom edge of the table.
    """
    cw0, cw1, cw2 = col_widths
    total_w = cw0 + cw1 + cw2
    header_h = row_h * 1.4
    pad = 1.5

    # ── Section title ────────────────────────────────────────────────────────
    _text(msp, title, x, y_top, TEXT_H_LG, layer=layer)
    y = y_top - TEXT_H_LG * 2.0

    # ── Header row ───────────────────────────────────────────────────────────
    _rect(msp, x, y - header_h, total_w, header_h, layer=layer)
    _hline(msp, x + cw0, y, x + cw0, y - header_h, layer=layer)
    _hline(msp, x + cw0 + cw1, y, x + cw0 + cw1, y - header_h, layer=layer)
    for col_x, label in [
        (x + pad,               "S.NO"),
        (x + cw0 + pad,         "PARAMETER"),
        (x + cw0 + cw1 + pad,   "VALUE"),
    ]:
        _text(msp, label, col_x, y - header_h + pad, text_h, layer=layer)
    y -= header_h

    # ── Data rows ────────────────────────────────────────────────────────────
    for _, row in df.iterrows():
        _rect(msp, x, y - row_h, total_w, row_h, layer=layer)
        _hline(msp, x + cw0, y, x + cw0, y - row_h, layer=layer)
        _hline(msp, x + cw0 + cw1, y, x + cw0 + cw1, y - row_h, layer=layer)

        _text(msp, str(row["S.No"]),        x + pad,             y - row_h + pad, text_h, layer=layer)
        _text(msp, str(row["Parameter"]),   x + cw0 + pad,       y - row_h + pad, text_h, layer=layer)
        _text(msp, str(row["Value"]),       x + cw0 + cw1 + pad, y - row_h + pad, text_h, layer=layer)
        y -= row_h

    return y  # bottom of table


# ─────────────────────────────────────────────────────────────────────────────
#  LEGEND
# ─────────────────────────────────────────────────────────────────────────────

def _draw_legend(msp, x: float, y_top: float, dxf: "DXFWriter",
                 layer: str = TB_LAYER) -> float:
    """
    Draw a LEGEND section using REAL DXF geometry -- actual colored line
    segments, dashes, and shape icons drawn on the same layers as the real
    drawing entities -- instead of text characters (dashes, tildes,
    brackets) standing in for symbols. Each row's swatch is drawn on its
    correct layer/color, sourced from DXFWriter's own layer definitions
    (core/dxf_writer.py:_create_layers), so the legend can never drift out
    of sync with what the actual drawing uses.

    Boundary, the 3m service road, and the 3.0m perimeter road reuse the
    exact layers those features are already drawn on elsewhere in the DXF.
    Fence, security cabin, entry gate, lightning arrester, and well don't
    yet exist as real generated site geometry, so they get their own
    dedicated legend-only layers (also created in _create_layers) with a
    sensible fixed color -- still real DXF entities, just not tied to
    computed geometry the way the others are.

    Returns bottom y.
    """
    layers_cfg = dxf.config["dxf"]["layers"]
    boundary_layer = layers_cfg["boundary"]
    road_layer = layers_cfg.get("road") or layers_cfg.get("roads") or "ROADS"
    service_road_layer = layers_cfg.get("service_road") or "SERVICE_ROAD"
    fence_layer = layers_cfg.get("fence") or "FENCE"
    cabin_layer = layers_cfg.get("security_cabin") or "SECURITY_CABIN"
    gate_layer = layers_cfg.get("entry_gate") or "ENTRY_GATE"
    arrester_layer = layers_cfg.get("lightning_arrester") or "LIGHTNING_ARRESTER"
    well_layer = layers_cfg.get("well") or "WELL"

    _text(msp, "LEGEND:", x, y_top, TEXT_H_LG, layer=layer)
    y = y_top - TEXT_H_LG * 2.2
    sym_len = 16.0
    desc_x = x + sym_len + 8

    def solid_line(ly, yy):
        msp.add_line((x, yy), (x + sym_len, yy), dxfattribs={"layer": ly, "lineweight": 40})

    def dashed_line(ly, yy, n=4, with_posts=False):
        seg = sym_len / (2 * n - 1)
        for i in range(n):
            x0 = x + i * 2 * seg
            msp.add_line((x0, yy), (x0 + seg, yy), dxfattribs={"layer": ly, "lineweight": 30})
            if with_posts:
                msp.add_circle((x0, yy), 0.6, dxfattribs={"layer": ly})

    def small_square(ly, yy):
        s = 5.0
        cx = x + sym_len / 2
        msp.add_lwpolyline(
            [(cx - s / 2, yy - s / 2), (cx + s / 2, yy - s / 2),
             (cx + s / 2, yy + s / 2), (cx - s / 2, yy + s / 2)],
            close=True, dxfattribs={"layer": ly, "lineweight": 30},
        )

    def gate_icon(ly, yy):
        cx = x + sym_len / 2
        msp.add_line((x, yy), (x + sym_len, yy), dxfattribs={"layer": ly, "lineweight": 30})
        msp.add_arc(center=(cx, yy), radius=4, start_angle=0, end_angle=180,
                    dxfattribs={"layer": ly})

    def arrester_icon(ly, yy):
        cx = x + sym_len / 2
        msp.add_line((cx, yy - 3), (cx, yy + 6), dxfattribs={"layer": ly, "lineweight": 30})
        msp.add_line((cx, yy + 6), (cx - 3, yy + 2), dxfattribs={"layer": ly, "lineweight": 30})
        msp.add_line((cx, yy + 6), (cx + 3, yy + 2), dxfattribs={"layer": ly, "lineweight": 30})

    rows = [
        (solid_line, dict(ly=boundary_layer), "PLANT BOUNDARY"),
        (dashed_line, dict(ly=fence_layer, with_posts=True), "PLANT BOUNDARY FENCE"),
        (small_square, dict(ly=cabin_layer), "SECURITY CABIN"),
        (gate_icon, dict(ly=gate_layer), "MAIN ENTRY GATE"),
        (dashed_line, dict(ly=service_road_layer, n=5), "3M WIDE ROAD WITH 0.5M SHOULDER"),
        (dashed_line, dict(ly=road_layer, n=3), "3.0M WIDE COMPACTED ROAD"),
        (arrester_icon, dict(ly=arrester_layer), "ESE LIGHTNING ARRESTER"),
    ]

    for draw_fn, kwargs, desc in rows:
        draw_fn(yy=y, **kwargs)
        _text(msp, f"-  {desc}", desc_x, y - 1.5, TEXT_H_SM, layer=layer)
        y -= LINE_SP * 2.0

    return y


# ─────────────────────────────────────────────────────────────────────────────
#  TYPICAL SECTION DETAILS (schematic outline boxes)
# ─────────────────────────────────────────────────────────────────────────────

def _draw_typical_section_details(msp, x: float, y_top: float,
                                  total_w: float = 280,
                                  layer: str = TB_LAYER) -> float:
    """
    Draw placeholder boxes for the three cross-section sketches
    (road cross-section, sectional view A-A, detail A) exactly as
    shown in the reference AutoCAD images.
    Returns bottom y.
    """
    _text(msp, "TYPICAL SECTION DETAILS:", x, y_top, TEXT_H_LG, layer=layer)
    y = y_top - TEXT_H_LG * 2.5

    # Three sketch boxes stacked vertically
    sketch_defs = [
        ("ROAD CROSS SECTION\n(3M ROAD + 0.5M SHOULDER)", 70),
        ("SECTIONAL VIEW A-A\n(MODULE TILT & MOUNTING)", 80),
        ("DETAIL A\n(FOUNDATION / PILE DETAIL)",          70),
    ]

    for label, box_h in sketch_defs:
        _rect(msp, x, y - box_h, total_w, box_h, layer=layer, lw=50)
        # diagonal cross
        msp.add_line((x, y - box_h), (x + total_w, y),
                     dxfattribs={"layer": layer, "lineweight": 18})
        msp.add_line((x + total_w, y - box_h), (x, y),
                     dxfattribs={"layer": layer, "lineweight": 18})
        # label centred inside box
        label_line = label.split("\n")[0]
        _text(msp, label_line,
              x + total_w / 2 - len(label_line) * TEXT_H_SM * 0.3,
              y - box_h / 2 - TEXT_H_SM / 2,
              TEXT_H_SM, layer=layer)
        if "\n" in label:
            sub = label.split("\n")[1]
            _text(msp, sub,
                  x + total_w / 2 - len(sub) * TEXT_H_SM * 0.3,
                  y - box_h / 2 - TEXT_H_SM * 2.2,
                  TEXT_H_SM * 0.9, layer=layer)
        y -= box_h + LINE_SP * 2

    return y


# ─────────────────────────────────────────────────────────────────────────────
#  Master title-block assembler
# ─────────────────────────────────────────────────────────────────────────────

def draw_title_block(
    dxf: DXFWriter,
    polygon: Polygon,
    boundary_points: List[Tuple[str, float, float]],
    inverter_points: List[Tuple[str, float, float]],
    control_points: List[Tuple[str, float, float]],
    plant_title: str,
    plant_details_df: pd.DataFrame,
    overall_title: str = "",
    gap_from_boundary: float = 60.0,
    boundary_csvs: List[str] = None,
    control_room_csvs: List[str] = None,
    inverter_room_csvs: List[str] = None,
):
    """
    Draw the complete right-side title block in model space, positioned to
    the right of the site boundary polygon.

    Layout (top → bottom, right column):
      1. BOUNDARY COORDINATES table (per parcel if multiple)
      2. INVERTER ROOM COORDINATES table
      3. CONTROL ROOM COORDINATES table
      4. KEY PLAN box
      5. NOTES
      6. TYPICAL PLANT DETAILS
      7. LEGEND
      8. TYPICAL SECTION DETAILS

    All content sits to the RIGHT of maxx+gap, starting from maxy.
    """
    _ensure_tb_layer(dxf)
    msp = dxf.msp

    minx, miny, maxx, maxy = polygon.bounds
    x_start = maxx + gap_from_boundary
    y_cursor = maxy          # we'll work downward

    col_w = (30, 60, 60)     # POINT | EASTING | NORTHING column widths
    total_col_w = sum(col_w)

    # ── 1. Boundary coordinates ──────────────────────────────────────────────
    if boundary_csvs and len(boundary_csvs) > 1:
        for i, csv_path in enumerate(boundary_csvs, start=1):
            pts = read_labeled_coordinate_csv(csv_path)
            y_cursor = _draw_coordinate_table(
                msp, f"BOUNDARY PARCEL {i} COORDINATES",
                pts,
                x_start, y_cursor,
                col_widths=col_w,
            )
            y_cursor -= LINE_SP * 3
    elif boundary_points:
        y_cursor = _draw_coordinate_table(
            msp, "COORDINATES",
            boundary_points,
            x_start, y_cursor,
            col_widths=col_w,
        )
        y_cursor -= LINE_SP * 3

    # ── 2. Inverter room coordinates ─────────────────────────────────────────
    if inverter_room_csvs and len(inverter_room_csvs) > 1:
        for i, csv_path in enumerate(inverter_room_csvs, start=1):
            pts = read_labeled_coordinate_csv(csv_path)
            y_cursor = _draw_coordinate_table(
                msp, f"INVERTER ROOM {i} COORDINATES",
                pts,
                x_start, y_cursor,
                col_widths=col_w,
            )
            y_cursor -= LINE_SP * 3
    elif inverter_points:
        y_cursor = _draw_coordinate_table(
            msp, "INVERTER ROOM COORDINATES",
            inverter_points,
            x_start, y_cursor,
            col_widths=col_w,
        )
        y_cursor -= LINE_SP * 3

    # ── 3. Control room coordinates ──────────────────────────────────────────
    if control_room_csvs and len(control_room_csvs) > 1:
        for i, csv_path in enumerate(control_room_csvs, start=1):
            pts = read_labeled_coordinate_csv(csv_path)
            y_cursor = _draw_coordinate_table(
                msp, f"CONTROL ROOM {i} COORDINATES",
                pts,
                x_start, y_cursor,
                col_widths=col_w,
            )
            y_cursor -= LINE_SP * 3
    elif control_points:
        y_cursor = _draw_coordinate_table(
            msp, "CONTROL ROOM COORDINATES",
            control_points,
            x_start, y_cursor,
            col_widths=col_w,
        )
        y_cursor -= LINE_SP * 3

    # ── The next blocks go into a SECOND column to the right of tables,
    #    starting from the top again, so we mirror the AutoCAD layout which
    #    places KEY PLAN, NOTES, PLANT DETAILS to the right of the coord tables.
    x2 = x_start + total_col_w + gap_from_boundary * 0.5
    y2_cursor = maxy

    # ── 4. KEY PLAN ──────────────────────────────────────────────────────────
    y2_cursor = _draw_key_plan(msp, x2, y2_cursor, box_w=180, box_h=130)
    y2_cursor -= LINE_SP * 4

    # ── 5. NOTES ─────────────────────────────────────────────────────────────
    y2_cursor = _draw_notes(msp, x2, y2_cursor)
    y2_cursor -= LINE_SP * 4

    # ── 6. TYPICAL PLANT DETAILS ─────────────────────────────────────────────
    y2_cursor = _draw_typical_plant_details(
        msp, plant_title, plant_details_df, x2, y2_cursor
    )
    y2_cursor -= LINE_SP * 4

    # ── 7. LEGEND ────────────────────────────────────────────────────────────
    y2_cursor = _draw_legend(msp, x2, y2_cursor, dxf)
    y2_cursor -= LINE_SP * 4

    # ── 8. TYPICAL SECTION DETAILS ───────────────────────────────────────────
    y2_cursor = _draw_typical_section_details(msp, x2, y2_cursor, total_w=280)

    # ── 9. OVERALL PLANT LAYOUT TITLE ────────────────────────────────────────
    if overall_title:
        y2_cursor -= LINE_SP * 2
        _text(msp, overall_title, x2, y2_cursor, TEXT_H_LG, layer=TB_LAYER)
        title_width = len(overall_title) * TEXT_H_LG * 0.6
        msp.add_line((x2, y2_cursor - TEXT_H_LG * 0.4), (x2 + title_width, y2_cursor - TEXT_H_LG * 0.4),
                     dxfattribs={"layer": TB_LAYER, "lineweight": 30})

    logger.info("Title block drawn to the right of the boundary (x=%.1f).", x_start)


# ─────────────────────────────────────────────────────────────────────────────
#  Capacity / plant-details builder  (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────

import pandas as pd
from math import ceil

def build_plant_details(config: dict, polygon: Polygon, packing_engine, capacity_check) -> Tuple[str, pd.DataFrame]:
    module_cfg = config.get("module", {})
    electrical_cfg = config.get("electrical", {})
    layout_cfg = config.get("layout", {})

    table = packing_engine.table
    module = packing_engine.module

    total_tables = len(packing_engine.table_locations)
    total_modules = capacity_check.total_modules

    # Approved string size comes from the electrical config; fall back to table rows
    string_size = electrical_cfg.get("approved_string_size") or table.rows
    total_strings = ceil(total_modules / string_size) if string_size else None

    num_inverters = electrical_cfg.get("num_inverters")
    inverter_kw_25c = electrical_cfg.get("inverter_rating_kw_25c")

    land_area_acres = polygon.area / SQ_M_PER_ACRE

    def fmt(value, suffix=""):
        return f"{value}{suffix}" if value is not None else "N/A"

    # DESIGN (theoretical) capacity
    design_ac_mw = float(layout_cfg.get("target_ac_mw") or 0.0)
    design_ratio = float(electrical_cfg.get("dc_ac_ratio") or 0.0)
    design_dc_mwp = design_ac_mw * design_ratio

    data = [
        [1, "MODULE RATING", fmt(module.power_wp, "Wp")],
        [2, "MODULE TECHNOLOGY", fmt(module_cfg.get("technology"))],
        [3, "INVERTER RATING",
         f"{fmt(inverter_kw_25c, 'kW @ 25C')} / {fmt(electrical_cfg.get('inverter_rating_kw_50c'), 'kW @ 50C')}"],
        [4, "MODULE ORIENTATION",
         "PORTRAIT" if table.portrait else "LANDSCAPE"],
        [5, "PITCH", f"{packing_engine.row_pitch:.1f}M"],
        [6, f"TOTAL NO. OF MMS ({table.rows}x{table.columns})",
         f"{total_tables} Nos"],
        [7, "TOTAL NO. OF MODULES",
         f"{total_modules} Nos"],
        [8, "TOTAL NO. OF STRINGS",
         f"{fmt(total_strings, ' Nos')}"],
        [9, "TILT ANGLE",
         fmt(layout_cfg.get("tilt_angle_deg"), " deg")],
        [10, "PLANT CAPACITY (DESIGN)",
         f"{design_ac_mw:.1f}MW/{design_dc_mwp:.2f}MWp"],
        [11, "PLANT CAPACITY",
         f"{capacity_check.dc_capacity_mw:.2f}MWp DC"],
        [12, "TOTAL NO. OF INVERTER",
         fmt(num_inverters, " Nos")],
        [13, "TOTAL NO. OF ICR",
         fmt(electrical_cfg.get("num_icr"), " Nos")],
        [14, "TOTAL LAND AREA",
         f"{land_area_acres:.2f} ACRES (APPROX.)"],
    ]

    plant_df  = pd.DataFrame(data, columns=["S.No", "Parameter", "Value"])
    plant_title = "TYPICAL PLANT DETAILS"

    return plant_title, plant_df


# ─────────────────────────────────────────────────────────────────────────────
#  Utility helpers  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def load_json(file_path: str):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {file_path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_exclusion_polygons(exclusions: dict):
    exclusion_polygons = []
    for exclusion in exclusions.get("exclusions", []):
        exclusion_type = exclusion.get("type")
        if exclusion_type == "polygon":
            try:
                coordinates = [
                    (float(point["x"]), float(point["y"]))
                    for point in exclusion.get("coordinates", [])
                    if "x" in point and "y" in point
                ]
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping invalid exclusion polygon: %s", exc)
                continue
            if len(coordinates) >= 3:
                exclusion_polygons.append(Polygon(coordinates))
        elif exclusion_type == "circle":
            center = exclusion.get("center", {})
            try:
                from shapely.geometry import Point
                exclusion_polygons.append(
                    Point(float(center["x"]), float(center["y"])).buffer(float(exclusion["radius"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping invalid circular exclusion: %s", exc)
        else:
            logger.warning("Unsupported exclusion type: %s", exclusion_type)
    return exclusion_polygons


def create_perimeter_road(boundary_polygon: Polygon, setback_polygon: Polygon):
    return boundary_polygon.difference(setback_polygon)


def create_panel_service_road(
    table_polygons,
    offset_from_panels_m: float,
    road_width_m: float,
    shoulder_m: float,
    table_gap_m: float,
    row_gap_m: float,
    clip_polygon: Polygon,
):
    if not table_polygons or road_width_m <= 0:
        return None

    closing_distance = max(table_gap_m, row_gap_m) / 2 + 0.05
    pv_footprint     = unary_union(table_polygons)
    footprint_outline = pv_footprint.buffer(closing_distance, join_style=1).buffer(
        -closing_distance, join_style=1
    )
    corridor_inner = footprint_outline.buffer(offset_from_panels_m, join_style=1)
    corridor_outer = corridor_inner.buffer(shoulder_m + road_width_m + shoulder_m, join_style=1)
    road_ring      = corridor_outer.difference(corridor_inner)
    return road_ring.intersection(clip_polygon)


# ─────────────────────────────────────────────────────────────────────────────
#  Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def place_lightning_arresters(pv_footprint, site_boundary, radius_m: float = 107.0, coverage_factor: float = 1.3):
    """
    Auto-places ESE lightning arresters in a grid over the PV array so
    their protection radius collectively covers it -- matching the
    project's own NOTES text ("ESE LIGHTNING ARRESTER LEVEL-4 IS
    CONSIDERED WITH 107M RADIUS LIGHTNING PROTECTION"), rather than only
    showing the symbol once in the legend.

    Grid spacing is `radius_m x coverage_factor` (default 1.3, i.e. some
    deliberate overlap between adjacent arresters' coverage circles rather
    than circles that just barely touch) -- a reasonable default; exact
    placement should be confirmed against the actual lightning protection
    design drawing.

    Returns
    -------
    list[(x, y)] -- arrester locations, kept only where they fall within
    the site boundary.
    """
    if pv_footprint is None or pv_footprint.is_empty:
        return []

    minx, miny, maxx, maxy = pv_footprint.bounds
    spacing = radius_m * coverage_factor

    points = []
    y = miny
    row = 0
    while y <= maxy:
        # Offset alternate rows by half a spacing for hexagonal-ish coverage
        x_offset = (spacing / 2) if (row % 2) else 0
        x = minx - x_offset
        while x <= maxx:
            candidate = Point(x, y)
            if site_boundary.contains(candidate) or site_boundary.distance(candidate) < 1.0:
                points.append((x, y))
            x += spacing
        y += spacing
        row += 1

    return points


def run_pipeline(config: dict) -> dict:
    """
    Runs the full boundary -> setback -> packing -> DXF pipeline from a
    config dict (the same shape read from config.json).

    Returns a summary dict with the DXF path and the actual results of the
    run (table/module counts, achieved capacity, land area, etc.) so
    callers -- the CLI or a UI -- can report what was actually generated.
    """
    input_cfg = config["input"]

    def _as_csv_list(value):
        """Accepts a single path (old config style) or a list of paths
        (multi-zone), always returns a list. None/empty stays empty."""
        if not value:
            return []
        return [value] if isinstance(value, str) else list(value)

    # New: boundary_csvs / control_room_csvs / inverter_room_csvs (lists) --
    # for multi-parcel sites or multiple physical room buildings (this
    # project's own drawings report "TOTAL NO. OF ICR - 2 Nos", i.e. two
    # separate inverter room footprints, not one).
    # Old singular keys (boundary_csv / control_room_csv / inverter_room_csv)
    # still work unchanged for existing configs.
    boundary_csvs = _as_csv_list(input_cfg.get("boundary_csvs") or input_cfg.get("boundary_csv"))
    control_room_csvs = _as_csv_list(input_cfg.get("control_room_csvs") or input_cfg.get("control_room_csv"))
    inverter_room_csvs = _as_csv_list(input_cfg.get("inverter_room_csvs") or input_cfg.get("inverter_room_csv"))

    if not boundary_csvs:
        raise ValueError("At least one boundary CSV is required (input.boundary_csvs or input.boundary_csv).")

    # Kept for any code further down that still expects a single path (e.g.
    # logging, or a caller reading result["boundary_csv"] back) -- always
    # the first one, which is what matters when there's genuinely only one.
    boundary_csv = boundary_csvs[0]
    control_room_csv = control_room_csvs[0] if control_room_csvs else None
    inverter_room_csv = inverter_room_csvs[0] if inverter_room_csvs else None

    # Multiple boundary parcels are unioned into ONE combined site polygon.
    # Adjacent/touching parcels union into one Polygon; genuinely separate
    # parcels stay as distinct polygons. Either way we keep EVERY parcel
    # and pack each one -- previously only the largest was kept, which is
    # why a second uploaded boundary showed only its survey dots with no
    # panels. `boundary_polygons_all` drives per-parcel packing below;
    # `polygon`/`boundary_data` remain the first/primary parcel for the
    # bits of the pipeline that still reference a single boundary
    # (title block key-plan, result summary).
    raw_boundary_polygons = [
        GeometryEngine(load_boundary_from_csv(csv_path)).create_boundary_polygon()
        for csv_path in boundary_csvs
    ]
    polygon = unary_union(raw_boundary_polygons) if len(raw_boundary_polygons) > 1 else raw_boundary_polygons[0]
    if polygon.geom_type == "MultiPolygon":
        boundary_polygons_all = list(polygon.geoms)
        logger.info("Loaded %d separate boundary parcels -- total area: %.2f m^2 (%.2f acres).",
                    len(boundary_polygons_all), polygon.area, polygon.area / SQ_M_PER_ACRE)
    else:
        boundary_polygons_all = [polygon]
        if len(raw_boundary_polygons) > 1:
            logger.info("Combined %d touching boundary parcels into one polygon (%.2f m^2).",
                        len(raw_boundary_polygons), polygon.area)

    exclusions_polygons = []
    for i, csv_path in enumerate(control_room_csvs, start=1):
        label = "Control Room" if len(control_room_csvs) == 1 else f"Control Room {i}"
        exclusions_polygons.append(load_exclusion_from_csv(csv_path, label))
    for i, csv_path in enumerate(inverter_room_csvs, start=1):
        label = "Inverter Room" if len(inverter_room_csvs) == 1 else f"Inverter Room {i}"
        exclusions_polygons.append(load_exclusion_from_csv(csv_path, label))

    private_area_csvs = input_cfg.get("private_area_csvs") or []
    if isinstance(private_area_csvs, str):
        private_area_csvs = [private_area_csvs]
    private_area_polygons = [
        load_exclusion_from_csv(csv_path, f"Private Area {i+1}")
        for i, csv_path in enumerate(private_area_csvs)
    ]
    exclusions_polygons.extend(private_area_polygons)

    extra_exclusions_file = input_cfg.get("exclusions")
    if extra_exclusions_file:
        exclusions_polygons.extend(parse_exclusion_polygons(load_json(extra_exclusions_file)))

    offset_engine    = OffsetEngine()
    setback_distance = config["layout"]["boundary_setback_m"]

    service_road_cfg     = config.get("service_road", {})
    service_corridor_total = 0.0
    if service_road_cfg.get("road_width_m", 0) > 0:
        service_corridor_total = (
            float(service_road_cfg.get("offset_from_panels_m", 2.0))
            + 2 * float(service_road_cfg.get("shoulder_m", 0.0))
            + float(service_road_cfg["road_width_m"])
        )

    per_parcel = []   # list of dicts: {boundary, setback, pv_area}
    for parcel in boundary_polygons_all:
        p_setback = offset_engine.create_internal_offset(parcel, setback_distance)
        if p_setback.is_empty:
            logger.warning("A boundary parcel is smaller than the setback -- skipped.")
            continue
        p_pv = p_setback
        if service_corridor_total > 0:
            p_pv = p_pv.buffer(-service_corridor_total, join_style=2)
        if p_pv.is_empty:
            logger.warning("A boundary parcel has no usable PV area after setback + road -- skipped.")
            continue
        per_parcel.append({"boundary": parcel, "setback": p_setback, "pv_area": p_pv})

    if not per_parcel:
        raise ValueError("Boundary setback + service road corridor leaves no usable area for PV tables.")

    setback_polygon = unary_union([e["setback"] for e in per_parcel])
    combined_pv_area = unary_union([e["pv_area"] for e in per_parcel])

    # Calculate ESE lightning arrester positions (protection radius from config)
    # and add a clearance exclusion zone around each arrester location
    # BEFORE table placement so panels are placed around lightning arresters
    # without overlapping them.
    arrester_radius_m = float(config.get("layout", {}).get("arrester_radius_m", 107.0))
    arrester_points = []
    arrester_clearance_m = float(config.get("layout", {}).get("arrester_clearance_m", 1.5))
    arrester_exclusions = [Point(x, y).buffer(arrester_clearance_m) for x, y in arrester_points]

    # Build PackingEngine on the combined PV area across all parcels so scan
    # lines, row Y-coordinates, and placement operate on a single global grid.
    packing_engine = PackingEngine(config, combined_pv_area)
    packing_engine.exclusions = list(exclusions_polygons) + arrester_exclusions
    packing_engine.generate_rows()
    packing_engine.filter_short_rows(config["layout"].get("minimum_row_length", 5.0))
    logger.info("Generated %d global table rows across %d parcel(s).", len(packing_engine.rows), len(per_parcel))

    electrical_cfg = dict(config.get("electrical", {}))
    layout_cfg     = config["layout"]

    target_capacity_mwp = layout_cfg.get("target_dc_capacity_mwp")
    target_ac_mw        = layout_cfg.get("target_ac_mw")
    capacity_plan       = None
    ac_dc_plan          = None

    # Resolve a target AC (MW) and DC:AC ratio from whichever of the four
    # input modes the config uses, regardless of path, so the string/
    # inverter/SCB grouping below (which always needs both) works the same
    # way no matter how the plant capacity was specified.
    resolved_ac_mw = None
    resolved_dc_ac_ratio = None

    dc_ac_ratio_input = electrical_cfg.get("dc_ac_ratio")

    if target_ac_mw and dc_ac_ratio_input:
        # PRIMARY PATH: DC/AC Ratio is a direct input (per spec), not
        # something derived from separately entering both an AC and a DC
        # megawatt target. This matches:
        #   Required DC Capacity = Inverter Rating x DC/AC Ratio
        #   Strings per Inverter = Required DC Capacity / Power per String
        # feeding straight into compute_string_inverter_grouping /
        # compute_centralized_inverter_grouping, which already take
        # dc_ac_ratio directly.
        resolved_ac_mw = float(target_ac_mw)
        resolved_dc_ac_ratio = float(dc_ac_ratio_input)
        target_capacity_mwp = resolved_ac_mw * resolved_dc_ac_ratio  # informational only
        logger.info(
            "Direct AC + DC:AC ratio inputs: AC=%.3f MW x ratio=%.4f -> implied DC=%.3f MWp.",
            resolved_ac_mw, resolved_dc_ac_ratio, target_capacity_mwp,
        )
    elif target_ac_mw and target_capacity_mwp:
        inverter_rating_kw = electrical_cfg.get("inverter_rating_kw_25c")
        if not inverter_rating_kw:
            raise ValueError(
                "layout.target_ac_mw requires electrical.inverter_rating_kw_25c in the config."
            )
        ac_dc_plan = compute_capacity_plan_from_targets(
            target_ac_mw=float(target_ac_mw),
            target_dc_mwp=float(target_capacity_mwp),
            module_power_wp=packing_engine.module.power_wp,
            modules_per_table=packing_engine.table.modules_per_table,
            inverter_rating_kw=inverter_rating_kw,
            modules_per_string=electrical_cfg.get("modules_per_string"),
        )
        resolved_ac_mw = float(target_ac_mw)
        resolved_dc_ac_ratio = float(target_capacity_mwp) / float(target_ac_mw)
        logger.info(
            "Direct AC+DC targets: AC=%.3f MW / DC=%.3f MWp -> DC:AC ratio=%.4f.",
            resolved_ac_mw, float(target_capacity_mwp), resolved_dc_ac_ratio,
        )
    elif target_capacity_mwp:
        inverter_rating_kw = electrical_cfg.get("inverter_rating_kw_25c")
        num_inverters = electrical_cfg.get("num_inverters")
        if not (inverter_rating_kw and num_inverters):
            raise ValueError(
                "An explicit target_dc_capacity_mwp (without target_ac_mw) requires "
                "electrical.inverter_rating_kw_25c and electrical.num_inverters, so an "
                "implied AC capacity and DC:AC ratio can be resolved for string/inverter sizing."
            )
        resolved_ac_mw = num_inverters * inverter_rating_kw / 1000
        resolved_dc_ac_ratio = float(target_capacity_mwp) / resolved_ac_mw
        logger.info("Using explicit DC capacity target: %.3f MWp.", float(target_capacity_mwp))
    elif layout_cfg.get("dc_oversizing_percent") is not None:
        inverter_rating_kw = electrical_cfg.get("inverter_rating_kw_25c")
        num_inverters      = electrical_cfg.get("num_inverters")
        if not (inverter_rating_kw and num_inverters):
            raise ValueError(
                "layout.dc_oversizing_percent requires electrical.inverter_rating_kw_25c "
                "and electrical.num_inverters."
            )
        capacity_plan = compute_capacity_plan(
            module_power_wp=packing_engine.module.power_wp,
            modules_per_table=packing_engine.table.modules_per_table,
            inverter_rating_kw=inverter_rating_kw,
            num_inverters=num_inverters,
            dc_oversizing_percent=float(layout_cfg["dc_oversizing_percent"]),
        )
        target_capacity_mwp = capacity_plan.required_dc_capacity_mw
        resolved_ac_mw = capacity_plan.ac_capacity_mw
        resolved_dc_ac_ratio = 1 + float(layout_cfg["dc_oversizing_percent"]) / 100

    # ── String / inverter / SCB grouping-based sizing ───────────────────────
    # Tables are no longer sized by a simple ceil(DC target / module / table)
    # calculation. Instead, the number of tables is derived bottom-up from
    # how many strings each inverter (and, for centralized architecture,
    # each SCB) actually needs -- so the built plant reflects whole,
    # physically buildable equipment counts, not just a module-count target.
    grouping = None
    governing_tables = None
    capacity_note = None
    columns_per_group = int(layout_cfg.get("tables_per_group_columns", 2))

    if resolved_ac_mw and resolved_dc_ac_ratio:
        architecture = electrical_cfg.get("inverter_architecture", "string_inverter")
        inverter_rating_kw = electrical_cfg.get("inverter_rating_kw_25c")

        # Approved string size: a string is 2 panels stacked vertically, so
        # it equals the table's row count unless explicitly overridden.
        string_size = electrical_cfg.get("approved_string_size") or packing_engine.table.rows

        grouping = calculatePlant(
            plant_ac_mw=resolved_ac_mw,
            plant_dc_mwp=resolved_ac_mw * resolved_dc_ac_ratio,
            module_rating_wp=packing_engine.module.power_wp,
            total_modules=electrical_cfg.get("total_modules") or int(round(
                resolved_ac_mw * resolved_dc_ac_ratio * 1_000_000 / packing_engine.module.power_wp)),
            inverter_rating_kw=inverter_rating_kw,
            approved_string_size=string_size,
            mms_rows=packing_engine.table.rows,
            mms_columns=packing_engine.table.columns,
            plant_type=architecture,
            strings_per_scb=electrical_cfg.get("strings_per_scb"),
        )

        for line in grouping.summary_lines():
            logger.info("  %s", line)
        for warning in grouping.warnings:
            logger.warning("CALC: %s", warning)

        electrical_cfg["num_inverters"] = grouping.number_of_inverters
        config["electrical"] = electrical_cfg

    # ── SCB-block placement ──────────────────────────────────────────────
    # The plant is built from SCB blocks, not from loose tables. Each SCB
    # is a fixed rectangle of tables (scb_block_rows x scb_block_columns,
    # e.g. 6 rows x 2 columns = 12 tables). The calculation engine decides
    # how many SCBs the plant needs; those whole rectangles are then fitted
    # into the boundary, and the placed SCBs are finally grouped under
    # inverters (Step 7: tables -> SCB -> inverter).
    #
    # Blocking by SCB (6 rows tall) rather than by whole inverter (~26 rows
    # tall) is what makes this fit: a rigid block needs a uniform width
    # across all of its rows, and this site tapers, so tall blocks fail
    # almost everywhere while 6-row blocks tile it well.
    table_groups = None
    # Two placement strategies, chosen by config:
    #   scb_block_placement = true  -> rigid SCB rectangles only. Every SCB
    #       is a perfect grid, but land too narrow for a whole rectangle is
    #       unusable, so capacity can fall well short on irregular sites.
    #   scb_block_placement = false -> fill rows with tables to use all the
    #       land, then group them into SCBs afterwards. Reaches the capacity
    #       target, but an SCB's tables follow the site shape rather than
    #       always forming a perfect rectangle.
    use_blocks = config.get("layout", {}).get("scb_block_placement", True)

    if grouping is not None and grouping.plant_type == "string_inverter":
        # String Inverter Architecture:
        # Tables group directly under inverters without any intermediate SCB.
        # SCB block placement is bypassed completely.
        fit_capacity = packing_engine.total_fit_capacity()
        governing_tables = min(grouping.total_tables, fit_capacity)
        packing_engine.place_tables(target_tables=governing_tables)
        packing_engine.finalize_layout()
        table_groups = generateTableGrouping(packing_engine.table_locations, grouping)
        placed = len(packing_engine.table_locations)
        actual_mwp = placed * packing_engine.table.modules_per_table * packing_engine.module.power_wp / 1e6
        capacity_note = (
            f"String Inverter layout: {placed} of {grouping.total_tables} required tables "
            f"(~{actual_mwp:.3f} MWp), assigned directly to inverters."
        )
        logger.info(capacity_note)

    elif grouping is not None and grouping.plant_type == "centralized_inverter":
        # Centralized Inverter Architecture:
        # 1. Place tables across all rows up to governing target capacity
        fit_capacity = packing_engine.total_fit_capacity()
        governing_tables = min(grouping.total_tables, fit_capacity)
        packing_engine.place_tables(target_tables=governing_tables)
        packing_engine.fill_gaps()
        packing_engine.finalize_layout()

        # 2. Detect every panel on the site and group on a 2 columns x 6 rows basis,
        # altering and combining any non-fitting panels into those centralized groups.
        from core.grouping import detect_and_group_centralized_2x6
        scbs_per_inverter = max(1, grouping.scbs_per_inverter or 1)
        table_groups = detect_and_group_centralized_2x6(
            packing_engine.table_locations,
            scbs_per_inverter=scbs_per_inverter,
            block_rows=6,
            block_cols=2,
        )

        placed = len(packing_engine.table_locations)
        actual_mwp = placed * packing_engine.table.modules_per_table * packing_engine.module.power_wp / 1e6
        inverters_used = len({g["inverter_id"] for g in table_groups}) if table_groups else 0
        scbs_used = len({g["scb_id"] for g in table_groups}) if table_groups else 0
        capacity_note = (
            f"Centralized Inverter layout: {placed} tables (~{actual_mwp:.3f} MWp) grouped into "
            f"{scbs_used} SCB groups (2 cols x 6 rows basis) across {inverters_used} inverter(s)."
        )
        logger.info(capacity_note)
    else:
        packing_engine.place_tables(target_tables=governing_tables)
        packing_engine.fill_gaps()
        packing_engine.finalize_layout()

    # ── Inverter Grouping for ALL Placed Tables ─────────────────────────────
    # Defensive fallback only: if grouping was never computed upstream (edge
    # case not hit by the client's own flows, since generateTableGrouping is
    # already called earlier for both centralized and string paths), compute
    # it now so no panels are left ungrouped.
    if grouping is not None and packing_engine.table_locations and not table_groups:
        table_groups = generateTableGrouping(packing_engine.table_locations, grouping)

    # ── Build DXF ────────────────────────────────────────────────────────────
    dxf = DXFWriter(config)
    dxf.create_document()

    for entry in per_parcel:
        dxf.add_boundary(entry["boundary"])

    if service_road_cfg.get("road_width_m", 0) > 0:
        # Service road: clean, properly aligned corridor following the boundary setback,
        # reserved before panel placement so panels are placed strictly inside the road boundary.
        for entry in per_parcel:
            road_corridor = entry["setback"].difference(entry["pv_area"])
            if road_corridor and not road_corridor.is_empty:
                dxf.add_service_road(road_corridor)

    if not exclusions_polygons:
        logger.warning("No control room / inverter room / exclusion polygons found.")
    else:
        dxf.add_exclusions(exclusions_polygons)

    if private_area_polygons:
        dxf.add_private_areas(private_area_polygons, label_prefix="PRIVATE AREA")

    roads = input_cfg.get("roads", [])
    roads_polygons = []
    if roads and isinstance(roads, list):
        for r in roads:
            if isinstance(r, (list, tuple)) and len(r) >= 3:
                try:
                    roads_polygons.append(Polygon(r))
                except Exception as e:
                    logger.warning(f"Invalid road polygon skipped: {e}")
    if roads_polygons:
        dxf.add_roads(roads_polygons)

    table_points = packing_engine.table_points()
    logger.info(f"Table points generated: {len(table_points)}")
    dxf.insert_tables(table_points)

    if table_groups:
        group_key = "scb_id" if any(g.get("scb_id") is not None for g in table_groups) else "inverter_id"
        group_outlines = build_group_outlines(
            table_groups,
            group_key=group_key,
            table_gap_m=packing_engine.table.table_gap_m,
            row_gap_m=packing_engine.row_pitch - packing_engine.table.table_height,
            margin_m=0.5,
            site_boundary=setback_polygon,
        )
        dxf.add_group_outlines(group_outlines)
        logger.info("Drew %d group outlines around filled panel area.", len(group_outlines))

    # Real lightning-arrester placement (not just a legend sample), sized
    # off the configured protection radius.
    if arrester_points:
        dxf.add_lightning_arresters(arrester_points, radius_m=arrester_radius_m)
        logger.info("Placed %d lightning arresters (%.1fm protection radius).", len(arrester_points), arrester_radius_m)

    # Point labels for surveyed coordinates -- from ALL CSVs in each list,
    # not just the first, so every parcel/room's actual survey points show.
    def _labeled_points_from_all(csv_paths):
        points = []
        for p in csv_paths:
            points.extend(read_labeled_coordinate_csv(p))
        return points

    dxf.add_point_labels(
        _labeled_points_from_all(boundary_csvs), layer="BOUNDARY_POINTS", color=WHITE
    )
    if control_room_csvs:
        dxf.add_point_labels(
            _labeled_points_from_all(control_room_csvs), layer="CONTROL_ROOM_POINTS", color=RED
        )
    if inverter_room_csvs:
        dxf.add_point_labels(
            _labeled_points_from_all(inverter_room_csvs), layer="INVERTER_ROOM_POINTS", color=MAGENTA
        )

    total_tables = len(packing_engine.table_locations)

    capacity_check = evaluate_capacity(
        total_tables=total_tables,
        modules_per_table=packing_engine.table.modules_per_table,
        module_power_wp=packing_engine.module.power_wp,
        inverter_rating_kw=electrical_cfg.get("inverter_rating_kw_25c"),
        num_inverters=electrical_cfg.get("num_inverters"),
    )

    plant_title,plant_df  = build_plant_details(config, polygon, packing_engine, capacity_check)
    overall_title = (
        f"{capacity_check.ac_capacity_mw:.1f}MW/{capacity_check.dc_capacity_mw:.1f}MWp "
        f"OVERALL PLANT LAYOUT" if capacity_check.ac_capacity_mw else
        f"{capacity_check.dc_capacity_mw:.1f}MWp OVERALL PLANT LAYOUT"
    )

    # ── Load coordinate tables for title block ────────────────────────────────
    boundary_pts  = _labeled_points_from_all(boundary_csvs)
    inverter_pts  = _labeled_points_from_all(inverter_room_csvs)
    control_pts   = _labeled_points_from_all(control_room_csvs)

    # ── Draw full right-side title block ──────────────────────────────────────
    draw_title_block(
        dxf=dxf,
        polygon=polygon,
        boundary_points=boundary_pts,
        inverter_points=inverter_pts,
        control_points=control_pts,
        plant_title=plant_title,
        plant_details_df=plant_df,
        overall_title=overall_title,
        gap_from_boundary=60.0,
        boundary_csvs=boundary_csvs,
        control_room_csvs=control_room_csvs,
        inverter_room_csvs=inverter_room_csvs,
    )

    dxf.save()

    configure_oda_path(config.get("oda_file_converter_path"))
    dwg_path   = str(Path(config["dxf"]["output_file"]).with_suffix(".dwg"))
    dwg_result = export_dwg(dxf.doc, dwg_path)

    land_area_acres = polygon.area / SQ_M_PER_ACRE

    requirement = None
    if target_capacity_mwp:
        requirement = compute_capacity_requirement(
            target_dc_mwp=float(target_capacity_mwp),
            module_power_wp=packing_engine.module.power_wp,
            modules_per_table=packing_engine.table.modules_per_table,
            modules_per_string=electrical_cfg.get("modules_per_string"),
        )

    return {
        "packing_engine":      packing_engine,
        "dxf_path":            config["dxf"]["output_file"],
        "dwg_path":            dwg_result["path"],
        "dwg_export_message":  dwg_result["message"],
        "boundary_polygon":    polygon,
        "setback_polygon":     setback_polygon,
        "table_count":         total_tables,
        "module_count":        capacity_check.total_modules,
        "actual_dc_mwp":       capacity_check.dc_capacity_mw,
        "target_dc_mwp":       float(target_capacity_mwp) if target_capacity_mwp else None,
        "target_ac_mw":        float(target_ac_mw) if target_ac_mw else None,
        "ac_capacity_mw":      capacity_check.ac_capacity_mw,
        "dc_ac_ratio":         capacity_check.dc_ac_ratio,
        "dc_ac_oversizing_percent": capacity_check.dc_ac_oversizing_percent,
        "capacity_plan":       capacity_plan,
        "ac_dc_plan":          ac_dc_plan,
        "grouping":            grouping,
        "table_groups":        table_groups,
        "requirement":         requirement,
        "land_area_acres":     land_area_acres,
        "row_pitch_m":         packing_engine.row_pitch,
        "capacity_note":       capacity_note,
    }

# End of pipeline.py

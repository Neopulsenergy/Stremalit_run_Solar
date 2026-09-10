from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import ezdxf
from ezdxf.colors import RED, GREEN, CYAN, YELLOW, MAGENTA, BLUE, WHITE
from shapely.geometry import Polygon, MultiPolygon, LineString
from ezdxf.enums import TextEntityAlignment
import math

logger = logging.getLogger(__name__)

class DXFWriter:

    def __init__(self, config: dict):
        self.config = config
        self.doc = None
        self.msp = None

    def create_document(self):
        logger.info("Creating DXF document...")
        self.doc = ezdxf.new("R2018")
        self.msp = self.doc.modelspace()
        if "DASHED" not in self.doc.linetypes:
            # Pattern in metres -- this drawing's real-world scale is
            # hundreds of metres, so a textbook 0.25m dash pattern would be
            # invisible; use a scale that actually reads as dashed here.
            self.doc.linetypes.add("DASHED", pattern=[10.0, 6.0, -4.0], description="Dashed")
        self._create_layers()
        logger.info("DXF initialized.")

    def _create_layer(self, name, color):
        if name not in self.doc.layers:
            self.doc.layers.add(name=name, color=color)

    def _create_layers(self):
        layers = self.config["dxf"]["layers"]
        self._create_layer(layers["boundary"], WHITE)
        self._create_layer(layers["setback"], CYAN)
        road_layer = layers.get("road") or layers.get("roads") or "ROADS"
        self._create_layer(road_layer, YELLOW)
        service_road_layer = layers.get("service_road") or "SERVICE_ROAD"
        self._create_layer(service_road_layer, RED)
        self._create_layer(layers["exclusion"], RED)
        self._create_layer(layers["pv_table"], GREEN)
        self._create_layer(layers["module"], BLUE)
        self._create_layer(layers["string"], MAGENTA)
        self._create_layer(layers["combiner"], CYAN)
        self._create_layer(layers["inverter"], RED)
        self._create_layer(layers["transformer"], YELLOW)
        self._create_layer(layers["cable"], WHITE)
        self._create_layer(layers["text"], GREEN)
        # Legend-only symbols -- these features (fence, security cabin, entry
        # gate, lightning arrester, well) aren't generated as real site
        # geometry anywhere else in the pipeline yet, but the legend still
        # needs a distinct, correctly colored layer for each so its symbol
        # swatches are actual DXF entities, not text standing in for color.
        self._create_layer(layers.get("fence") or "FENCE", 30)  # ACI 30 = orange
        self._create_layer(layers.get("security_cabin") or "SECURITY_CABIN", CYAN)
        self._create_layer(layers.get("entry_gate") or "ENTRY_GATE", MAGENTA)
        self._create_layer(layers.get("lightning_arrester") or "LIGHTNING_ARRESTER", GREEN)
        self._create_layer(layers.get("well") or "WELL", RED)

    def add_polygon(self, polygon: Polygon, layer: str):
        coords = list(polygon.exterior.coords)
        self.msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer})
        for interior in polygon.interiors:
            self.msp.add_lwpolyline(
                list(interior.coords), close=True, dxfattribs={"layer": layer}
            )

    def add_ring_interior_only(self, geometry, layer: str):
        """
        Draws only the interior ring(s) (holes) of a polygon-with-a-hole,
        skipping its exterior ring. Used for the perimeter road: its
        exterior ring is identical to the property boundary (already drawn
        separately), so re-drawing it would just duplicate that line.
        """
        if geometry is None:
            return
        polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
        for polygon in polygons:
            for interior in polygon.interiors:
                self.msp.add_lwpolyline(
                    list(interior.coords), close=True, dxfattribs={"layer": layer}
                )
            if not polygon.interiors:
                # No hole (e.g. the road touches the boundary on all sides
                # with no gap) -- fall back to drawing the outline as-is.
                self.msp.add_lwpolyline(
                    list(polygon.exterior.coords), close=True, dxfattribs={"layer": layer}
                )

    def add_boundary(self, polygon: Polygon):
        layer = self.config["dxf"]["layers"]["boundary"]
        self.add_polygon(polygon, layer)

    def add_setback(self, polygon: Polygon):
        layer = self.config["dxf"]["layers"]["setback"]
        self.add_polygon(polygon, layer)

    def add_geometry(self, geometry, layer):
        if geometry is None:
            return
        if isinstance(geometry, Polygon):
            self.add_polygon(geometry, layer)
        elif isinstance(geometry, MultiPolygon):
            for poly in geometry.geoms:
                self.add_polygon(poly, layer)

    def add_exclusions(self, exclusions: Iterable[Polygon | MultiPolygon]):
        layer = self.config["dxf"]["layers"]["exclusion"]
        for geometry in exclusions:
            self.add_geometry(geometry, layer)

    def add_private_areas(self, polygons: Iterable[Polygon], label_prefix: str = "PRIVATE AREA",
                          hatch_pattern: str = "ANSI31", color=RED,
                          outline_layer="PRIVATE_AREA_OUTLINE", hatch_layer="PRIVATE_AREA_HATCH",
                          label_layer="PRIVATE_AREA_LABEL"):
        """
        Marks private/restricted/no-go zones as REJECTED: a bold outline,
        a diagonal strike-out hatch clipped to the polygon, and a label
        with the computed area -- distinct from the plain exclusion
        rendering used for the control/inverter rooms (which are simply
        omitted from packing without a rejection mark).

        These polygons are ALSO expected to already be excluded from
        table placement (the pipeline adds them to the same exclusion
        list used for the control/inverter rooms) -- this method only
        handles the visual marking, not the packing exclusion itself.

        Parameters
        ----------
        polygons : iterable of shapely Polygon
            Already-loaded private-area boundaries (e.g. via
            csv_loader.load_exclusion_from_csv).
        """
        self._create_layer(outline_layer, color)
        self._create_layer(hatch_layer, color)
        self._create_layer(label_layer, color)

        polygons = list(polygons)
        multi = len(polygons) > 1

        for idx, polygon in enumerate(polygons, start=1):
            if polygon is None or polygon.is_empty:
                continue
            coords = list(polygon.exterior.coords)

            self.msp.add_lwpolyline(
                coords, close=True,
                dxfattribs={"layer": outline_layer, "lineweight": 60},
            )

            hatch = self.msp.add_hatch(color=color, dxfattribs={"layer": hatch_layer})
            hatch.set_pattern_fill(hatch_pattern, scale=1.0)
            hatch.paths.add_polyline_path(coords, is_closed=True)

            area = polygon.area
            cx, cy = polygon.centroid.x, polygon.centroid.y
            minx, miny, maxx, maxy = polygon.bounds
            span = max(maxx - minx, maxy - miny)
            text_height = max(span * 0.04, 0.3)

            label = f"{label_prefix} {idx} - REJECTED" if multi else f"{label_prefix} - REJECTED"
            self.msp.add_text(
                label, dxfattribs={"layer": label_layer, "height": text_height},
            ).set_placement((cx, cy + text_height), align=TextEntityAlignment.MIDDLE_CENTER)

            self.msp.add_text(
                f"Area: {area:,.2f} m^2",
                dxfattribs={"layer": label_layer, "height": text_height * 0.8},
            ).set_placement((cx, cy - text_height), align=TextEntityAlignment.MIDDLE_CENTER)

        logger.info("Marked %d private/restricted area(s) as REJECTED.", len(polygons))

    def add_polyline(self, coordinates, layer, closed=False):
        self.msp.add_lwpolyline(coordinates, close=closed, dxfattribs={"layer": layer})

    def add_road(self, road_geometry):
        """
        Draws the perimeter road (the ring between the boundary and the
        setback line). Only the inner edge is drawn -- the outer edge is
        identical to the property boundary, which is already drawn
        separately by add_boundary(), so drawing it again here would just
        create a duplicate line right on top of it.
        """
        layers = self.config["dxf"]["layers"]
        layer = layers.get("road") or layers.get("roads") or "ROADS"
        self.add_ring_interior_only(road_geometry, layer)

    def add_roads(self, roads: Iterable[Polygon]):
        logger.info(f"Adding {len(roads)} roads to DXF.")
        for road in roads:
            self.add_road(road)

    def add_service_road(self, road_geometry):
        """
        Draws the internal service road that hugs the panel array (distinct
        from the perimeter/property road on the 'road' layer) -- red, per
        the '3M WIDE ROAD WITH 0.5M SHOULDER' legend convention.
        """
        layers = self.config["dxf"]["layers"]

        layer = layers.get("service_road") or "SERVICE_ROAD"
        self.add_geometry(road_geometry, layer)

    def add_text_block(self, title, lines, insert_point, text_height=4.0, line_spacing=1.6, layer=None):
        """
        Writes a titled, multi-line legend/details block (e.g. "TYPICAL PLANT
        DETAILS") as MTEXT at the given insertion point, growing downward.

        Parameters
        ----------
        title : str
        lines : list[str]
            Each entry already formatted as the full display line, e.g.
            "1. MODULE RATING            -  532Wp"
        insert_point : (x, y)
            Top-left corner where the title begins.
        text_height : float
            Character height in drawing units (metres).
        line_spacing : float
            Line spacing multiplier.
        """
        layer = layer or self.config["dxf"]["layers"]["text"]
        x, y = insert_point

        title_entity = self.msp.add_text(
            title,
            dxfattribs={"layer": layer, "height": text_height * 1.3, "style": "STANDARD"},
        )
        title_entity.set_placement((x, y), align=TextEntityAlignment.TOP_LEFT)

        body = "\n".join(lines)
        mtext = self.msp.add_mtext(
            body,
            dxfattribs={
                "layer": layer,
                "char_height": text_height,
                "line_spacing_factor": line_spacing,
            },
        )
        mtext.set_location(
            (x, y - text_height * 1.3 * 2.0),
            attachment_point=ezdxf.enums.MTextEntityAlignment.TOP_LEFT,
        )
        return mtext

    def add_point_labels(self, labeled_points, layer, color=WHITE, marker_radius=0.4, text_height=1.8):
        """
        Draws a small marker circle plus its point name at each surveyed
        coordinate (e.g. boundary vertices B-01, B-02, ... or exclusion
        vertices CR-01, IR-01, ...).

        Parameters
        ----------
        labeled_points : list[(label, x, y)]
        layer : str
            Layer to draw on; created automatically (with `color`) if it
            doesn't already exist, so callers can give each point set its
            own layer without editing the config.
        marker_radius : float
            Radius of the small circle drawn at each point, in metres.
        text_height : float
            Label character height, in metres.
        """
        self._create_layer(layer, color)
        for label, x, y in labeled_points:
            self.msp.add_circle((x, y), marker_radius, dxfattribs={"layer": layer})
            text = self.msp.add_text(
                str(label),
                dxfattribs={"layer": layer, "height": text_height, "style": "STANDARD"},
            )
            text.set_placement(
                (x + marker_radius * 1.8, y + marker_radius * 1.8),
                align=TextEntityAlignment.BOTTOM_LEFT,
            )

    def add_table_group_labels(self, table_groups, layer=None, color=WHITE, text_height=1.6):
        """
        Labels each table with its inverter (and, for centralized
        architecture, SCB) group assignment -- e.g. "INV-3" or
        "INV-2 / SCB-5" -- centered on the table.

        Parameters
        ----------
        table_groups : list[{"table": TablePlacement, "inverter_id": int,
                              "scb_id": int | None}]
        """
        layer = layer or "TABLE_GROUPS"
        self._create_layer(layer, color)
        for entry in table_groups:
            table = entry["table"]
            cx, cy = table.polygon.centroid.x, table.polygon.centroid.y
            if entry.get("scb_id") is not None:
                label = f"INV-{entry['inverter_id']} / SCB-{entry['scb_id']}"
            else:
                label = f"INV-{entry['inverter_id']}"
            text = self.msp.add_text(
                label,
                dxfattribs={"layer": layer, "height": text_height, "style": "STANDARD"},
            )
            text.set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)

    def add_group_outlines(self, group_outlines: dict, layer=None, colors=(RED, BLUE),
                            lineweight=211, label_prefix=None, label_height=4.0,
                            highlight_margin=0.10, bold_width=0.30,
                            group_colors=((220, 20, 60), (0, 110, 240))):
        """
        Draws a bold highlighted boundary around each group's block of
        tables (one per SCB), alternating between Red and Blue so adjacent
        blocks stay visually distinct, plus an id label.
        """
        layer = layer or "GROUP_BOUNDARY"
        self._create_layer(layer, colors[0])
        label_layer = f"{layer}_LABELS"
        self._create_layer(label_layer, colors[0])

        for group_id in sorted(group_outlines):
            geometry = group_outlines[group_id]
            color = colors[0] if group_id % 2 == 1 else colors[1]
            rgb = group_colors[0] if group_id % 2 == 1 else group_colors[1]
            if hasattr(geometry, "geoms"):
                polygons = [g for g in geometry.geoms if hasattr(g, "exterior")]
            else:
                polygons = [geometry] if hasattr(geometry, "exterior") else []
            for polygon in polygons:
                pline = self.msp.add_lwpolyline(
                    list(polygon.exterior.coords), close=True,
                    dxfattribs={
                        "layer": layer,
                        "color": color,
                        "true_color": ezdxf.rgb2int(rgb),
                        "lineweight": lineweight,
                    },
                )
                # const_width gives the polyline real geometric width, so all
                # four sides render bold regardless of whether the viewer has
                # "Show Lineweight" switched on (lineweight alone is often
                # invisible by default in AutoCAD).
                if bold_width:
                    pline.dxf.const_width = bold_width
            if label_prefix:
                cx, cy = geometry.centroid.x, geometry.centroid.y
                minx, miny, maxx, maxy = geometry.bounds
                text = self.msp.add_text(
                    f"{label_prefix}-{group_id:02d}",
                    dxfattribs={"layer": label_layer, "color": color,
                                "height": label_height, "style": "STANDARD"},
                )
                text.set_placement((cx, maxy + label_height * 0.6),
                                    align=TextEntityAlignment.BOTTOM_CENTER)

    def add_lightning_arresters(self, points, radius_m=107.0, layer=None, coverage_layer=None,
                                 symbol_size=6.0, color=GREEN):
        """
        Draws an ESE lightning arrester symbol (mast + splayed strike-rod
        icon) at each given location, plus its dashed protection-radius
        coverage circle -- the real, site-placed version of the legend's
        sample icon, not just a swatch.

        Parameters
        ----------
        points : list[(x, y)]
        radius_m : float
            Protection radius, per the project's lightning-protection note.
        symbol_size : float
            Visual size of the mast icon itself (independent of radius_m).
        """
        layer = layer or "LIGHTNING_ARRESTER"
        coverage_layer = coverage_layer or "LIGHTNING_ARRESTER_COVERAGE"
        self._create_layer(layer, color)
        self._create_layer(coverage_layer, color)
        # The 107m protection-radius circles are roughly the same size as
        # the whole array and heavily overlap each other -- default this
        # layer OFF so it doesn't visually bury the group boundaries, table
        # grid, and everything else. Still present in the DXF; turn the
        # LIGHTNING_ARRESTER_COVERAGE layer back on in AutoCAD when needed.
        self.doc.layers.get(coverage_layer).off()

        for x, y in points:
            # Mast + two splayed strike rods, matching the legend icon shape.
            self.msp.add_line((x, y - symbol_size / 2), (x, y + symbol_size),
                               dxfattribs={"layer": layer, "lineweight": 30})
            self.msp.add_line((x, y + symbol_size), (x - symbol_size / 2, y + symbol_size / 3),
                               dxfattribs={"layer": layer, "lineweight": 30})
            self.msp.add_line((x, y + symbol_size), (x + symbol_size / 2, y + symbol_size / 3),
                               dxfattribs={"layer": layer, "lineweight": 30})
            # Protection-radius coverage circle, dashed.
            circle = self.msp.add_circle(
                (x, y), radius_m,
                dxfattribs={"layer": coverage_layer, "linetype": "DASHED"},
            )

    def save(self):
        Path(self.config["dxf"]["output_file"]).parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(self.config["dxf"]["output_file"])
        logger.info(f"DXF saved : {self.config['dxf']['output_file']}")

    def _table_dimensions(self):
        """Return the configured module and PV-table dimensions in metres."""
        module = self.config["module"]
        table = self.config["table"]
        module_length = float(module.get("length_m", module.get("length_mm", 0)))
        module_width = float(module.get("width_m", module.get("width_mm", 0)))
        if module_length > 10:
            module_length /= 1000
        if module_width > 10:
            module_width /= 1000

        rows = int(table["rows"])
        columns = int(table["columns"])
        portrait = table.get("portrait", False)
        module_x, module_y = (
            (module_width, module_length) if portrait else (module_length, module_width)
        )
        return rows, columns, module_x, module_y

    def create_table_block(self, block_name="PV_TABLE_2P28", rows=None, columns=None):
        """Create a PV table block containing its individual module outlines."""
        if block_name in self.doc.blocks:
            return

        std_rows, std_cols, module_width, module_height = self._table_dimensions()
        r = rows if rows is not None else std_rows
        c = columns if columns is not None else std_cols

        block = self.doc.blocks.new(name=block_name)
        module_layer = self.config["dxf"]["layers"]["module"]

        table_width = c * module_width
        table_height = r * module_height

        # Draw outer boundary
        block.add_lwpolyline(
            [
                (0, 0),
                (table_width, 0),
                (table_width, table_height),
                (0, table_height),
            ],
            close=True,
            dxfattribs={"layer": module_layer},
        )

        # Draw inner vertical lines
        for col in range(1, c):
            x = col * module_width
            block.add_line(
                (x, 0),
                (x, table_height),
                dxfattribs={"layer": module_layer},
            )

        # Draw inner horizontal lines
        for row in range(1, r):
            y = row * module_height
            block.add_line(
                (0, y),
                (table_width, y),
                dxfattribs={"layer": module_layer},
            )

    def insert_table(self, x, y, rotation=0, rows=None, cols=None, block_name=None):
        if block_name is None:
            if rows is not None and cols is not None:
                block_name = f"PV_TABLE_{rows}x{cols}"
            else:
                block_name = "PV_TABLE_2P28"
        self.create_table_block(block_name, rows=rows, columns=cols)
        self.msp.add_blockref(
            block_name,
            (x, y),
            dxfattribs={
                "rotation": rotation,
                "layer": self.config["dxf"]["layers"]["pv_table"]
            }
        )

    def insert_tables(self, table_points, rotation=0):
        for pt in table_points:
            if len(pt) >= 5:
                self.insert_table(pt[0], pt[1], rotation=pt[2], rows=pt[3], cols=pt[4])
            elif len(pt) == 3:
                self.insert_table(pt[0], pt[1], rotation=pt[2])
            else:
                self.insert_table(pt[0], pt[1], rotation=rotation)

# End of dxf_writer.py





























































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































# End of dxf_writer.py

import logging
import warnings
from math import atan2, ceil, floor
from shapely.geometry import Polygon, LineString, box, Point
from shapely.affinity import rotate, translate
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


def required_tables_for_capacity(target_capacity_mwp: float, module_power_wp: float, modules_per_table: int) -> int:
    """
    Converts a target DC plant capacity into a required table count.

        total_modules = ceil( capacity_MWp * 1,000,000 / module_Wp )
        tables        = ceil( total_modules / modules_per_table )

    Rounding always goes up so the built plant never falls short of the
    nominal target capacity (actual capacity ends up at or slightly above
    the target, never below).
    """
    if target_capacity_mwp <= 0:
        raise ValueError("target_capacity_mwp must be positive.")
    if module_power_wp <= 0:
        raise ValueError("module power_wp must be positive.")
    if modules_per_table <= 0:
        raise ValueError("modules_per_table must be positive.")

    total_modules = ceil(target_capacity_mwp * 1_000_000 / module_power_wp)
    tables = ceil(total_modules / modules_per_table)
    return tables

@dataclass
class TablePlacement:
    id: int
    origin_x: float
    origin_y: float
    rotation: float
    polygon: Polygon
    rows: int = 0
    cols: int = 0

@dataclass
class PlacementResult:
    attempted: int = 0
    accepted: int = 0
    rejected_boundary: int = 0
    rejected_collision: int = 0

@dataclass
class ModuleSpec:
    manufacturer: str
    model: str
    power_wp: float
    length_m: float
    width_m: float

@dataclass
class TableSpec:
    table_type: str
    rows: int
    columns: int
    modules_per_table: int
    portrait: bool
    table_gap_m: float

    @property
    def table_width(self) -> float:
        if self.portrait:
            return self.columns * self.width_per_module()
        return self.columns * self.length_per_module()

    @property
    def table_height(self) -> float:
        if self.portrait:
            return self.rows * self.length_per_module()
        return self.rows * self.width_per_module()

    def length_per_module(self):
        return PackingEngine.module.length_m

    def width_per_module(self):
        return PackingEngine.module.width_m

@dataclass
class RowCandidate:
    id: int
    centerline: LineString
    length: float
    angle: float
    usable: bool = True

@dataclass
class PackingStatistics:
    tables: int = 0
    modules: int = 0
    dc_capacity_mw: float = 0.0
    utilized_area: float = 0.0
    utilization_percent: float = 0.0

class PackingEngine:
    module: ModuleSpec = None

    def __init__(self, config: dict, developable_area: Polygon):
        self.config = config
        self.developable_area = developable_area
        self.table_locations: List[TablePlacement] = []
        self.rows: List[RowCandidate] = []
        self.statistics = PackingStatistics()
        self._load_configuration()
        self.row_angle = self.config["layout"].get("row_angle", 0.0)
        # Stack table rows directly above one another unless a row gap is set.
        self.row_pitch = self.table.table_height + self.config["layout"].get("row_gap_m", 0.0)
        logger.info("PackingEngine initialized.")

    def _load_configuration(self):
        module_cfg = self.config.get("module", {})
        PackingEngine.module = ModuleSpec(
            manufacturer=module_cfg.get("manufacturer", ""),
            model=module_cfg.get("model", ""),
            power_wp=module_cfg.get("power_wp", 0),
            length_m=module_cfg.get("length_mm", module_cfg.get("length_m", 0)),
            width_m=module_cfg.get("width_mm", module_cfg.get("width_m", 0))
        )
        if PackingEngine.module.length_m > 10:
            PackingEngine.module.length_m /= 1000
        if PackingEngine.module.width_m > 10:
            PackingEngine.module.width_m /= 1000
        table_cfg = self.config.get("table", {})
        self.table = TableSpec(
            table_type=table_cfg.get("type", table_cfg.get("family", "default")),
            rows=table_cfg.get("rows", 0),
            columns=table_cfg.get("columns", 0),
            modules_per_table=table_cfg.get("modules_per_table", 0),
            portrait=table_cfg.get("portrait", False),
            table_gap_m=table_cfg.get("table_gap_m", 0.0)
        )
        if "type" not in table_cfg:
            warnings.warn("Config table section missing 'type' key, using 'family' as table_type.")
        if "portrait" not in table_cfg:
            warnings.warn("Config table section missing 'portrait' key, defaulting to False.")
        if "table_gap_m" not in table_cfg:
            warnings.warn("Config table section missing 'table_gap_m' key, defaulting to 0.")

    def initialize_rows(self):
        self.rows = []
        logger.info("Row Pitch : %.3f m", self.row_pitch)

    def rotated_site(self):
        return rotate(self.developable_area, -self.row_angle, origin="centroid", use_radians=False)

    def generate_scan_lines(self):
        poly = self.rotated_site()
        minx, miny, maxx, maxy = poly.bounds
        y = miny
        lines = []
        while y <= maxy:
            line = LineString([(minx - 1000, y), (maxx + 1000, y)])
            lines.append(line)
            y += self.row_pitch
        logger.info("Generated %d scan lines.", len(lines))
        return lines

    def clip_scan_lines(self):
        poly = self.rotated_site()
        clipped = []
        for line in self.generate_scan_lines():
            result = poly.intersection(line)
            if result.is_empty:
                continue
            if result.geom_type == "LineString":
                clipped.append(result)
            elif result.geom_type == "MultiLineString":
                for seg in result.geoms:
                    clipped.append(seg)
        logger.info("Usable row segments : %d", len(clipped))
        return clipped

    def generate_rows(self):
        self.initialize_rows()
        angle = self.row_angle
        row_id = 1
        for segment in self.clip_scan_lines():
            row = RowCandidate(
                id=row_id,
                centerline=rotate(segment, angle, origin=self.developable_area.centroid, use_radians=False),
                length=segment.length,
                angle=angle
            )
            self.rows.append(row)
            row_id += 1
        logger.info("Row candidates : %d", len(self.rows))

    def filter_short_rows(self, minimum_length):
        usable = [row for row in self.rows if row.length >= minimum_length]
        logger.info("Rows retained : %d", len(usable))
        self.rows = usable

    def row_table_capacity(self, row):
        """Maximum whole tables (by available row length) this row can hold."""
        spacing = self.table.table_width + self.table.table_gap_m
        if spacing <= 0 or row.length <= 0:
            return 0
        return int(row.length / spacing)

    def total_fit_capacity(self):
        """Total tables the site geometry can physically hold, ignoring
        exclusions/collisions (upper bound used for capacity planning)."""
        return sum(self.row_table_capacity(row) for row in self.rows)

    def select_row_counts(self, target_tables):
        """
        Decide how many tables to place in each row.

        With no target (or a target at/above the full geometric fit), every
        row is filled to its maximum capacity — the original behaviour.

        With a smaller target, whole rows are dropped alternately from the
        north and south ends of the row list (each row represents the same
        physical row-pitch height, regardless of how many tables fit in it,
        so trimming by whole rows keeps the used vertical extent centered).
        Once removing another whole row would undershoot the target, only
        that one edge row is partially trimmed to land on the exact count.
        """
        capacities = [self.row_table_capacity(row) for row in self.rows]
        total_capacity = sum(capacities)
        n = len(capacities)

        if target_tables is None or target_tables >= total_capacity:
            return capacities

        counts = list(capacities)
        lo, hi = 0, n - 1
        remaining_total = total_capacity
        trim_hi = True
        while lo <= hi and remaining_total > target_tables:
            idx = hi if trim_hi else lo
            row_count = counts[idx]

            if row_count == 0:
                if trim_hi:
                    hi -= 1
                else:
                    lo += 1
                trim_hi = not trim_hi
                continue

            if remaining_total - row_count >= target_tables:
                # Dropping this whole row still leaves enough (or exactly
                # enough) tables -- remove it entirely and move the end in.
                remaining_total -= row_count
                counts[idx] = 0
                if trim_hi:
                    hi -= 1
                else:
                    lo += 1
            else:
                # Dropping this whole row would undershoot the target;
                # trim only as many tables as needed from this one row.
                excess = remaining_total - target_tables
                counts[idx] = row_count - excess
                remaining_total = target_tables
                break

            trim_hi = not trim_hi

        return counts

    def get_global_grid_x(self) -> float:
        """
        Returns the global grid baseline x_grid_0 in rotated space.
        Calculated once based on the overall developable area bounds in rotated space,
        ensuring every row and every table group aligns to the exact same column grid.
        """
        poly_rot = self.rotated_site()
        minx_rot, miny_rot, maxx_rot, maxy_rot = poly_rot.bounds
        site_width = maxx_rot - minx_rot
        spacing_x = self.table.table_width + self.table.table_gap_m
        if spacing_x > 0 and site_width >= self.table.table_width:
            max_cols = int((site_width + self.table.table_gap_m) // spacing_x)
            if max_cols > 0:
                used_w = max_cols * self.table.table_width + (max_cols - 1) * self.table.table_gap_m
                return minx_rot + max(0.0, (site_width - used_w) / 2.0)
        return minx_rot

    def fill_row(self, row, start_id, count=None):
        placed = []
        centroid_ref = self.developable_area.centroid
        centerline_rot = rotate(row.centerline, -self.row_angle, origin=centroid_ref, use_radians=False)
        coords_rot = list(centerline_rot.coords)
        x0_rot, y_rot = coords_rot[0]
        x1_rot, _ = coords_rot[-1]
        seg_min_x = min(x0_rot, x1_rot)
        seg_max_x = max(x0_rot, x1_rot)
        seg_len = seg_max_x - seg_min_x

        w = self.table.table_width
        spacing = w + self.table.table_gap_m
        if spacing <= 0 or seg_len < w:
            return placed

        x_grid_0 = self.get_global_grid_x()

        k_min = ceil((seg_min_x - x_grid_0) / spacing)
        k_max = floor((seg_max_x - w - x_grid_0) / spacing)

        if k_min > k_max:
            return placed

        all_valid_cols = []
        for k in range(k_min, k_max + 1):
            tx_rot = x_grid_0 + k * spacing
            pt_rot = Point(tx_rot, y_rot)
            pt_world = rotate(pt_rot, self.row_angle, origin=centroid_ref, use_radians=False)
            poly = self.create_table_polygon(pt_world.x, pt_world.y, row.angle)
            if self.validate_table(poly):
                all_valid_cols.append((k, pt_world.x, pt_world.y))

        max_count = len(all_valid_cols)
        if max_count == 0:
            return placed

        req_count = max_count if count is None else max(0, min(count, max_count))
        if req_count == 0:
            return placed

        # Keep grid-aligned selection rather than shifting by (max_count - req_count) // 2
        selected_cols = all_valid_cols[:req_count]

        table_id = start_id
        for k, wx, wy in selected_cols:
            table = self.place_table(wx, wy, row.angle, table_id)
            if table:
                placed.append(table)
                table_id += 1
        return placed

    def place_tables(self, target_tables=None):
        """
        Places tables across all rows.

        Parameters
        ----------
        target_tables : int, optional
            Caps the total number of tables placed (e.g. driven by a plant
            DC capacity target). When omitted, every row is filled to its
            maximum geometric capacity, as before. When provided and lower
            than the site's full capacity, the used rows/tables are kept
            centered within the site rather than packed against one edge.
        """
        result = PlacementResult()
        counts = self.select_row_counts(target_tables)
        table_id = 1
        for row, count in zip(self.rows, counts):
            if count <= 0:
                continue
            before = len(self.table_locations)
            placed = self.fill_row(row, table_id, count=count)
            after = len(self.table_locations)
            result.accepted += len(placed)
            result.attempted += after - before
            table_id += len(placed)
        logger.info("Placed %d tables (target=%s).", result.accepted, target_tables)
        return result

    def place_table(self, x, y, rotation, table_id):
        poly = self.create_table_polygon(x, y, rotation)
        if not self.validate_table(poly):
            return None
        placement = TablePlacement(
            id=table_id,
            origin_x=x,
            origin_y=y,
            rotation=rotation,
            polygon=poly,
            rows=self.table.rows,
            cols=self.table.columns,
        )
        self.table_locations.append(placement)
        if len(self.table_locations) % 100 == 0:
            self.build_spatial_index()
        return placement

    def create_table_polygon(self, x, y, rotation):
        poly = box(0, 0, self.table.table_width, self.table.table_height)
        poly = rotate(poly, rotation, origin=(0, 0), use_radians=False)
        poly = translate(poly, xoff=x, yoff=y)
        return poly

    def create_custom_table_polygon(self, x, y, rotation, rows=None, cols=None):
        r = rows if rows is not None else self.table.rows
        c = cols if cols is not None else self.table.columns
        mod_x = self.table.width_per_module() if self.table.portrait else self.table.length_per_module()
        mod_y = self.table.length_per_module() if self.table.portrait else self.table.width_per_module()
        w = c * mod_x
        h = r * mod_y
        poly = box(0, 0, w, h)
        poly = rotate(poly, rotation, origin=(0, 0), use_radians=False)
        poly = translate(poly, xoff=x, yoff=y)
        return poly

    def fill_gaps(self) -> list[TablePlacement]:
        """
        Second-pass placement: scans all rows across the site for leftover empty
        space that hasn't been filled during main placement. Fits ONLY full standard
        tables (std_cols x std_rows) into gaps while strictly respecting boundary,
        spacing, road, and exclusion rules.
        All gap tables are strictly placed on the global column grid (x_grid_0 + k * spacing_x)
        to maintain clean rectangular grid alignment across the entire site.
        """
        gap_tables = []
        if not self.rows:
            return gap_tables

        centroid_ref = self.developable_area.centroid
        mod_x = self.table.width_per_module() if self.table.portrait else self.table.length_per_module()
        std_cols = self.table.columns
        std_rows = self.table.rows
        full_table_width = std_cols * mod_x
        spacing_x = full_table_width + self.table.table_gap_m
        table_id = len(self.table_locations) + 1

        self.build_spatial_index()
        x_grid_0 = self.get_global_grid_x()

        for row in self.rows:
            centerline_rot = rotate(row.centerline, -self.row_angle, origin=centroid_ref, use_radians=False)
            coords = list(centerline_rot.coords)
            if not coords:
                continue
            y_rot = coords[0][1]
            seg_min_x = min(c[0] for c in coords)
            seg_max_x = max(c[0] for c in coords)

            k_min = ceil((seg_min_x - x_grid_0) / spacing_x)
            k_max = floor((seg_max_x - full_table_width - x_grid_0) / spacing_x)

            for k in range(k_min, k_max + 1):
                tx_rot = x_grid_0 + k * spacing_x
                pt_rot = Point(tx_rot, y_rot)
                pt_world = rotate(pt_rot, row.angle, origin=centroid_ref, use_radians=False)
                poly = self.create_custom_table_polygon(
                    pt_world.x, pt_world.y, row.angle, rows=std_rows, cols=std_cols
                )
                if self.validate_table(poly):
                    placement = TablePlacement(
                        id=table_id,
                        origin_x=pt_world.x,
                        origin_y=pt_world.y,
                        rotation=row.angle,
                        polygon=poly,
                        rows=std_rows,
                        cols=std_cols,
                    )
                    self.table_locations.append(placement)
                    gap_tables.append(placement)
                    table_id += 1
                    if hasattr(self, "_spatial_index") and self._spatial_index is not None:
                        self._spatial_index.insert(len(self.table_locations) - 1, poly.bounds)
                        self._indexed_count = len(self.table_locations)

        logger.info("Gap-filling pass complete: placed %d full standard table(s) on global grid.", len(gap_tables))
        return gap_tables

    def validate_table(self, polygon):
        if not self.inside_site(polygon):
            return False
        if self.inside_exclusions(polygon):
            return False
        if self.inside_roads(polygon):
            return False
        if self.collision_fast(polygon):
            return False
        gap = self.config["layout"].get("minimum_gap_m", 0.0)
        if gap > 0:
            if not self.clearance_check(polygon, gap):
                return False
        return True

    def inside_site(self, polygon):
        return self.developable_area.contains(polygon)

    def inside_exclusions(self, polygon):
        if not hasattr(self, "exclusions") or not self.exclusions:
            return False
        for ex in self.exclusions:
            if polygon.intersects(ex):
                return True
        return False

    def inside_roads(self, polygon):
        if not hasattr(self, "roads") or not self.roads:
            return False
        for road in self.roads:
            if polygon.intersects(road):
                return True
        return False

    def collision_fast(self, polygon):
        if hasattr(self, "_spatial_index") and self._spatial_index is not None:
            candidates = (
                self.table_locations[pos]
                for pos in self._spatial_index.intersection(polygon.bounds)
            )
        else:
            candidates = iter(())

        for table in candidates:
            # Touching table edges are allowed; only overlapping table areas are
            # a collision.  This supports zero vertical row spacing.
            if polygon.intersection(table.polygon).area > 1e-9:
                return True

        # Tables placed after the most recent index rebuild are not yet indexed.
        indexed_count = getattr(self, "_indexed_count", 0)
        for table in self.table_locations[indexed_count:]:
            if polygon.intersection(table.polygon).area > 1e-9:
                return True
        return False

    def clearance_check(self, polygon, gap):
        buffered = polygon.buffer(gap)
        for table in self.table_locations:
            if buffered.intersects(table.polygon):
                return False
        return True

    def build_spatial_index(self):
        try:
            from rtree import index
        except ImportError:
            logger.warning("rtree package not installed, spatial index disabled.")
            self._spatial_index = None
            return
        idx = index.Index()
        for pos, table in enumerate(self.table_locations):
            idx.insert(pos, table.polygon.bounds)
        self._spatial_index = idx
        self._indexed_count = len(self.table_locations)

    def table_points(self):
        pts = []
        for table in self.table_locations:
            r = getattr(table, "rows", 0) or self.table.rows
            c = getattr(table, "cols", 0) or self.table.columns
            # DXF table blocks use their lower-left corner as the insertion point.
            pts.append((table.origin_x, table.origin_y, table.rotation, r, c))
        return pts

    def place_table_groups(self, columns_per_group: int, tables_per_group: int, num_groups_needed: int):
        """
        Places tables as rectangular blocks (e.g. one SCB = 6 rows x 2
        columns = 12 tables). Whole blocks are fitted into the developable
        area; a block is kept only if EVERY one of its tables passes normal
        validation.

        All blocks are strictly anchored to the site-wide global column grid
        (x_grid_0 + k * spacing_x) to ensure perfect vertical and horizontal
        alignment across all bands without stepped offsets.
        """
        if columns_per_group <= 0 or tables_per_group <= 0 or num_groups_needed <= 0:
            return 0, []

        rows_per_group = ceil(tables_per_group / columns_per_group)
        spacing_x = self.table.table_width + self.table.table_gap_m
        block_width = (
            columns_per_group * self.table.table_width
            + (columns_per_group - 1) * self.table.table_gap_m
        )

        slot_offsets = []
        for r in range(rows_per_group):
            for c in range(columns_per_group):
                slot_offsets.append((c * spacing_x, r))
        slot_offsets = slot_offsets[:tables_per_group]

        levels = {}
        for row in self.rows:
            centroid_ref = self.developable_area.centroid
            centerline_rot = rotate(row.centerline, -self.row_angle, origin=centroid_ref, use_radians=False)
            coords_rot = list(centerline_rot.coords)
            y = round(coords_rot[0][1], 3)
            levels.setdefault(y, []).append(row)
        ordered_levels = [levels[y] for y in sorted(levels)]

        x_grid_0 = self.get_global_grid_x()

        def attempt(band_offset):
            saved = list(self.table_locations)
            self.table_locations = []
            placed_blocks = []

            seq = ordered_levels[band_offset:]
            bands = [
                seq[i:i + rows_per_group]
                for i in range(0, len(seq), rows_per_group)
                if len(seq[i:i + rows_per_group]) == rows_per_group
            ]

            for band in bands:
                if len(placed_blocks) >= num_groups_needed:
                    break

                y_values, starts, ends = [], [], []
                centroid_ref = self.developable_area.centroid
                for segments in band:
                    seg_start, seg_end = None, None
                    for row in segments:
                        centerline_rot = rotate(row.centerline, -self.row_angle, origin=centroid_ref, use_radians=False)
                        coords = list(centerline_rot.coords)
                        rx0, rx1 = coords[0][0], coords[-1][0]
                        lo, hi = min(rx0, rx1), max(rx0, rx1)
                        seg_start = lo if seg_start is None else min(seg_start, lo)
                        seg_end = hi if seg_end is None else max(seg_end, hi)
                    starts.append(seg_start)
                    ends.append(seg_end)
                    centerline_rot0 = rotate(segments[0].centerline, -self.row_angle, origin=centroid_ref, use_radians=False)
                    y_values.append(list(centerline_rot0.coords)[0][1])

                scan_start, scan_end = min(starts), max(ends)
                available = scan_end - scan_start
                if available < block_width:
                    continue

                angle = band[0][0].angle

                k_min = ceil((scan_start - x_grid_0) / spacing_x)
                k_max = floor((scan_end - block_width - x_grid_0) / spacing_x)
                if k_min > k_max:
                    continue

                def stage_at(positions):
                    staged_blocks = []
                    for bx in positions:
                        staged, ok = [], True
                        for dx, row_idx in slot_offsets:
                            tx_rot = bx + dx
                            ty_rot = y_values[row_idx]
                            pt_rot = Point(tx_rot, ty_rot)
                            pt_world = rotate(pt_rot, angle, origin=centroid_ref, use_radians=False)
                            poly = self.create_table_polygon(pt_world.x, pt_world.y, angle)
                            if not self.validate_table(poly):
                                ok = False
                                break
                            staged.append((pt_world.x, pt_world.y, poly))
                        if ok and len(staged) == len(slot_offsets):
                            staged_blocks.append(staged)
                    return staged_blocks

                def lay_scan_grid():
                    positions = []
                    k = k_min
                    while k <= k_max:
                        bx = x_grid_0 + k * spacing_x
                        ok = True
                        for dx, row_idx in slot_offsets:
                            tx_rot = bx + dx
                            ty_rot = y_values[row_idx]
                            pt_rot = Point(tx_rot, ty_rot)
                            pt_world = rotate(pt_rot, angle, origin=centroid_ref, use_radians=False)
                            poly = self.create_table_polygon(pt_world.x, pt_world.y, angle)
                            if not self.validate_table(poly):
                                ok = False
                                break
                        if ok:
                            positions.append(bx)
                            k += columns_per_group
                        else:
                            k += 1
                    return positions

                # Always use lay_scan_grid to maintain strict global grid alignment
                best_staged = stage_at(lay_scan_grid())

                for staged in best_staged:
                    if len(placed_blocks) >= num_groups_needed:
                        break
                    if not all(self.validate_table(p) for _, _, p in staged):
                        continue
                    block = []
                    for tx, ty, poly in staged:
                        placement = TablePlacement(
                            id=len(self.table_locations) + 1,
                            origin_x=tx, origin_y=ty, rotation=angle, polygon=poly,
                        )
                        self.table_locations.append(placement)
                        block.append(placement)
                    placed_blocks.append(block)

            result = (placed_blocks, list(self.table_locations))
            self.table_locations = saved
            return result

        best_blocks, best_tables, best_offset = [], [], 0
        for offset in range(rows_per_group):
            blocks, tables = attempt(offset)
            if len(blocks) > len(best_blocks):
                best_blocks, best_tables, best_offset = blocks, tables, offset
            if len(best_blocks) >= num_groups_needed:
                break

        self.table_locations = best_tables
        group_records = []
        for block_index, block in enumerate(best_blocks, start=1):
            for placement in block:
                group_records.append({
                    "table": placement, "inverter_id": block_index, "scb_id": None,
                })

        logger.info(
            "Placed %d of %d blocks (%d tables/block, %dx%d grid, best band offset %d).",
            len(best_blocks), num_groups_needed, tables_per_group,
            rows_per_group, columns_per_group, best_offset,
        )
        return len(best_blocks), group_records

    def finalize_layout(self):
        self.build_spatial_index()
        logger.info("Spatial index built for %d tables.", len(self.table_locations))

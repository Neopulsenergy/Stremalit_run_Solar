from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shapely.geometry import Polygon

logger = logging.getLogger(__name__)


def read_labeled_coordinate_csv(file_path: str) -> List[Tuple[str, float, float]]:
    """
    Reads a survey coordinate CSV with columns: POINT, EASTING, NORTHING.

    Returns (label, x, y) tuples in file order, preserving each point's
    original name (e.g. "B-01", "CR-02", "IR-05") for on-drawing labeling.
    EASTING maps to x, NORTHING maps to y.

    Raises
    ------
    FileNotFoundError
        If the CSV does not exist.
    ValueError
        If the required columns are missing, or fewer than 3 valid points
        are found.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    points: List[Tuple[str, float, float]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        # Match column names case-insensitively (e.g. "Easting" / "EASTING"
        # / "easting" all work), since real-world survey exports vary.
        field_lookup = {name.strip().upper(): name for name in fieldnames}
        required = {"EASTING", "NORTHING"}
        if not required.issubset(field_lookup.keys()):
            raise ValueError(
                f"{file_path}: expected columns {sorted(required)}, found {fieldnames}"
            )
        easting_col = field_lookup["EASTING"]
        northing_col = field_lookup["NORTHING"]
        point_col = field_lookup.get("POINT")

        for row_num, row in enumerate(reader, start=2):  # header is line 1
            try:
                x = float(row[easting_col])
                y = float(row[northing_col])
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping invalid row %d in %s: %s", row_num, file_path, exc)
                continue
            label = (row.get(point_col) or "").strip() if point_col else ""
            label = label or f"P-{row_num - 1:02d}"
            points.append((label, x, y))

    if len(points) < 3:
        raise ValueError(
            f"{file_path}: at least 3 valid coordinate points are required, found {len(points)}"
        )

    return points


def read_coordinate_csv(file_path: str) -> List[Tuple[float, float]]:
    """
    Reads a survey coordinate CSV with columns: POINT, EASTING, NORTHING.

    EASTING maps to x, NORTHING maps to y. Rows are returned in file order,
    since that order defines the polygon winding for boundaries and
    exclusion zones alike. Point labels are dropped here; use
    read_labeled_coordinate_csv() when labels are needed (e.g. for
    on-drawing annotation).
    """
    return [(x, y) for _, x, y in read_labeled_coordinate_csv(file_path)]


def load_boundary_from_csv(file_path: str) -> Dict:
    """
    Loads a site boundary CSV and returns it in the dict shape GeometryEngine
    expects: {"boundary": [{"x": .., "y": ..}, ...]}
    """
    points = read_coordinate_csv(file_path)
    logger.info("Loaded boundary CSV '%s' with %d vertices.", file_path, len(points))
    return {
        "project": Path(file_path).stem,
        "coordinate_system": "Local Grid (Easting/Northing)",
        "boundary": [{"x": x, "y": y} for x, y in points],
    }


def load_exclusion_from_csv(file_path: str, name: Optional[str] = None) -> Polygon:
    """
    Loads a CSV of closed-polygon coordinates (e.g. control room, inverter
    room) and returns a Shapely Polygon exclusion zone.
    """
    points = read_coordinate_csv(file_path)
    label = name or Path(file_path).stem

    polygon = Polygon(points)
    if not polygon.is_valid:
        logger.warning("Exclusion '%s' from %s is invalid, attempting repair.", label, file_path)
        polygon = polygon.buffer(0)

    logger.info(
        "Loaded exclusion CSV '%s' (%s): %d vertices, area=%.2f m^2",
        file_path, label, len(points), polygon.area
    )
    return polygon

# End of core/csv_loader.py

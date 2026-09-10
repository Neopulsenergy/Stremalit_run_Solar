from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from shapely.geometry import Polygon
from shapely.validation import explain_validity

logger = logging.getLogger(__name__)


class GeometryEngine:
    """
    Geometry processing engine.

    Parameters
    ----------
    boundary_data : dict
        Boundary JSON loaded from boundary.json
    """

    def __init__(self, boundary_data: Dict):

        self.boundary_data = boundary_data

        self.project = boundary_data.get("project", "Unknown")

         # Read the boundary vertices
        self.coordinates = boundary_data.get("boundary", [])

        self.total_vertices = len(self.coordinates)

        self._polygon = None

    # ---------------------------------------------------------
    # Coordinate Extraction
    # ---------------------------------------------------------

    def get_coordinate_list(self) -> List[Tuple[float, float]]:
        """
        Returns coordinates as list of tuples.

        Returns
        -------
        [(x,y), (x,y), ...]
        """

        if len(self.coordinates) < 3:
            raise ValueError(
                "Boundary requires at least 3 vertices."
            )

        pts = []

        for p in self.coordinates:

            pts.append(
                (
                    float(p["x"]),
                    float(p["y"])
                )
            )

        return pts

    # ---------------------------------------------------------
    # Polygon Creation
    # ---------------------------------------------------------

    def create_boundary_polygon(self) -> Polygon:
        """
        Creates Shapely polygon from JSON coordinates.

        Returns
        -------
        Polygon
        """

        pts = self.get_coordinate_list()

        polygon = Polygon(pts)

        self._polygon = polygon

        logger.info(
            "Boundary polygon created (%d vertices).",
            len(pts)
        )

        return polygon

    # ---------------------------------------------------------
    # Validation
    # ---------------------------------------------------------

    def validate(self):
        """
        Validate boundary geometry.

        Raises
        ------
        ValueError
        """

        if self._polygon is None:
            self.create_boundary_polygon()

        if self._polygon.is_empty:
            raise ValueError("Polygon is empty.")

        if not self._polygon.is_valid:

            reason = explain_validity(self._polygon)

            raise ValueError(
                f"Invalid Polygon : {reason}"
            )

        logger.info("Polygon validation successful.")

    # ---------------------------------------------------------
    # Getter
    # ---------------------------------------------------------

    def polygon(self) -> Polygon:

        if self._polygon is None:
            self.create_boundary_polygon()

        return self._polygon

    # ---------------------------------------------------------
    # Basic Information
    # ---------------------------------------------------------

    def vertex_count(self) -> int:

        return self.total_vertices

    def project_name(self) -> str:

        return self.project

    def coordinate_system(self) -> str:

        return self.boundary_data.get(
            "coordinate_system",
            "Unknown"
        )

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------

    def summary(self):

        p = self.polygon()

        print("-----------------------------------")
        print("Project :", self.project)
        print("Vertices :", self.total_vertices)
        print("Area :", round(p.area, 2), "sq.m")
        print("Perimeter :", round(p.length, 2), "m")
        print("-----------------------------------")
        
        
     # ---------------------------------------------------------
    # Geometry Properties
    # ---------------------------------------------------------

    def area(self) -> float:
        """
        Returns polygon area in square meters.
        """
        return self.polygon().area

    def perimeter(self) -> float:
        """
        Returns polygon perimeter in meters.
        """
        return self.polygon().length

    def centroid(self):
        """
        Returns centroid Point.
        """
        return self.polygon().centroid

    def bounds(self):
        """
        Returns polygon bounding box.

        Returns
        -------
        (minx, miny, maxx, maxy)
        """
        return self.polygon().bounds

    def bounding_box(self) -> dict:

        minx, miny, maxx, maxy = self.bounds()

        return {
            "min_x": minx,
            "min_y": miny,
            "max_x": maxx,
            "max_y": maxy,
            "width": maxx - minx,
            "height": maxy - miny
        }

    # ---------------------------------------------------------
    # Polygon Transformations
    # ---------------------------------------------------------

    def rotate(
        self,
        angle_deg: float,
        origin: str = "centroid"
    ):
        """
        Rotate polygon.

        Parameters
        ----------
        angle_deg : float
        origin : centroid | center | tuple
        """

        from shapely.affinity import rotate

        return rotate(
            self.polygon(),
            angle_deg,
            origin=origin
        )

    def translate(
        self,
        dx: float,
        dy: float
    ):
        """
        Translate polygon.
        """

        from shapely.affinity import translate

        return translate(
            self.polygon(),
            xoff=dx,
            yoff=dy
        )

    def scale(
        self,
        sx: float,
        sy: float = None
    ):
        """
        Scale polygon.
        """

        from shapely.affinity import scale

        if sy is None:
            sy = sx

        return scale(
            self.polygon(),
            xfact=sx,
            yfact=sy,
            origin="centroid"
        )

    # ---------------------------------------------------------
    # Convex Hull
    # ---------------------------------------------------------

    def convex_hull(self):
        """
        Returns convex hull.
        """

        return self.polygon().convex_hull

    # ---------------------------------------------------------
    # Minimum Rotated Rectangle
    # ---------------------------------------------------------

    def minimum_rotated_rectangle(self):
        """
        Returns minimum rotated rectangle.
        """

        return self.polygon().minimum_rotated_rectangle

    # ---------------------------------------------------------
    # Point Tests
    # ---------------------------------------------------------

    def contains_point(
        self,
        x: float,
        y: float
    ) -> bool:

        from shapely.geometry import Point

        return self.polygon().contains(
            Point(x, y)
        )

    def touches_point(
        self,
        x: float,
        y: float
    ) -> bool:

        from shapely.geometry import Point

        return self.polygon().touches(
            Point(x, y)
        )

    # ---------------------------------------------------------
    # Distance Utilities
    # ---------------------------------------------------------

    def distance_to_boundary(
        self,
        x: float,
        y: float
    ) -> float:

        from shapely.geometry import Point

        return Point(x, y).distance(
            self.polygon().boundary
        )

    def nearest_boundary_point(
        self,
        x: float,
        y: float
    ):

        from shapely.geometry import Point
        from shapely.ops import nearest_points

        p = Point(x, y)

        _, nearest = nearest_points(
            p,
            self.polygon().boundary
        )

        return nearest

    # ---------------------------------------------------------
    # Buffer
    # ---------------------------------------------------------

    def buffer(
        self,
        distance: float
    ):
        """
        Positive = outward
        Negative = inward
        """

        return self.polygon().buffer(
            distance
        )

    # ---------------------------------------------------------
    # Polygon Export
    # ---------------------------------------------------------

    def exterior_coordinates(self):
        """
        Returns polygon exterior coordinates.

        Returns
        -------
        List[(x,y)]
        """

        return list(
            self.polygon().exterior.coords
        )

    # ---------------------------------------------------------
    # Statistics
    # ---------------------------------------------------------

    def statistics(self):

        box = self.bounding_box()

        return {

            "project": self.project,

            "vertices": self.vertex_count(),

            "area": round(
                self.area(),
                3
            ),

            "perimeter": round(
                self.perimeter(),
                3
            ),

            "bounding_box": box,

            "centroid": {

                "x": self.centroid().x,

                "y": self.centroid().y

            }

        }

    # ---------------------------------------------------------
    # Spatial Relationship Tests
    # ---------------------------------------------------------

    def contains_geometry(self, geometry) -> bool:
        """
        Returns True if the boundary completely contains
        the supplied geometry.
        """
        return self.polygon().contains(geometry)

    def intersects(self, geometry) -> bool:
        """
        Check whether geometry intersects boundary.
        """
        return self.polygon().intersects(geometry)

    def within(self, geometry) -> bool:
        """
        Returns True if boundary lies within geometry.
        """
        return self.polygon().within(geometry)

    def overlaps(self, geometry) -> bool:
        """
        Returns True if polygon overlaps another geometry.
        """
        return self.polygon().overlaps(geometry)

    def intersection(self, geometry):
        """
        Returns intersection geometry.
        """
        return self.polygon().intersection(geometry)

    def difference(self, geometry):
        """
        Returns polygon after removing supplied geometry.
        """
        return self.polygon().difference(geometry)

    def union(self, geometry):
        """
        Returns union geometry.
        """
        return self.polygon().union(geometry)

    # ---------------------------------------------------------
    # Clipping
    # ---------------------------------------------------------

    def clip(self, geometry):
        """
        Clips geometry to site boundary.
        """
        return geometry.intersection(self.polygon())

    # ---------------------------------------------------------
    # Envelope
    # ---------------------------------------------------------

    def envelope(self):
        """
        Returns rectangular envelope.
        """
        return self.polygon().envelope

    # ---------------------------------------------------------
    # Simplification
    # ---------------------------------------------------------

    def simplify(
        self,
        tolerance: float = 0.01,
        preserve_topology: bool = True
    ):
        """
        Simplify polygon.
        """
        return self.polygon().simplify(
            tolerance,
            preserve_topology=preserve_topology
        )

    # ---------------------------------------------------------
    # Explode Boundary
    # ---------------------------------------------------------

    def boundary_segments(self):
        """
        Returns all boundary line segments.

        Returns
        -------
        List[((x1,y1),(x2,y2))]
        """

        coords = list(self.polygon().exterior.coords)

        segments = []

        for i in range(len(coords)-1):

            segments.append(
                (
                    coords[i],
                    coords[i+1]
                )
            )

        return segments

    # ---------------------------------------------------------
    # Longest Edge
    # ---------------------------------------------------------

    def longest_edge(self):

        from math import hypot

        longest = None
        max_length = 0.0

        for p1, p2 in self.boundary_segments():

            length = hypot(
                p2[0]-p1[0],
                p2[1]-p1[1]
            )

            if length > max_length:
                max_length = length
                longest = (p1, p2)

        return longest, max_length

    # ---------------------------------------------------------
    # Orientation Angle
    # ---------------------------------------------------------

    def dominant_orientation(self):
        """
        Returns orientation angle (degrees)
        of longest boundary edge.
        """

        from math import atan2, degrees

        edge, _ = self.longest_edge()

        p1, p2 = edge

        return degrees(
            atan2(
                p2[1]-p1[1],
                p2[0]-p1[0]
            )
        )

    # ---------------------------------------------------------
    # Grid Generator
    # ---------------------------------------------------------

    def generate_grid(
        self,
        spacing_x: float,
        spacing_y: float
    ):
        """
        Generate grid points inside boundary.

        Used later for PV table placement.
        """

        from shapely.geometry import Point

        minx, miny, maxx, maxy = self.bounds()

        x = minx

        points = []

        while x <= maxx:

            y = miny

            while y <= maxy:

                p = Point(x, y)

                if self.polygon().contains(p):
                    points.append(p)

                y += spacing_y

            x += spacing_x

        return points

    # ---------------------------------------------------------
    # GeoJSON Export
    # ---------------------------------------------------------

    def to_geojson(self):
        """
        Export boundary as GeoJSON dictionary.
        """

        from shapely.geometry import mapping

        return mapping(self.polygon())

    # ---------------------------------------------------------
    # WKT Export
    # ---------------------------------------------------------

    def to_wkt(self):

        return self.polygon().wkt

    # ---------------------------------------------------------
    # WKB Export
    # ---------------------------------------------------------

    def to_wkb(self):

        return self.polygon().wkb

    # ---------------------------------------------------------
    # Debug Plot
    # ---------------------------------------------------------

    def plot(self):

        import matplotlib.pyplot as plt

        x, y = self.polygon().exterior.xy

        plt.figure(figsize=(8, 8))
        plt.plot(x, y)
        plt.gca().set_aspect("equal")
        plt.title(self.project)
        plt.grid(True)
        plt.show()
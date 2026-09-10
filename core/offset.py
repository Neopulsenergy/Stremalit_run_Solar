from __future__ import annotations

import logging
from enum import Enum

from shapely.geometry import Polygon, MultiPolygon
from shapely.validation import explain_validity
from shapely.ops import unary_union

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# Join Styles
# ------------------------------------------------------------

class JoinStyle(Enum):
    ROUND = 1
    MITRE = 2
    BEVEL = 3


# ------------------------------------------------------------
# Offset Engine
# ------------------------------------------------------------

class OffsetEngine:

    def __init__(self):

        logger.info("OffsetEngine initialized.")

    # --------------------------------------------------------

    def create_internal_offset(
        self,
        polygon: Polygon,
        distance: float,
        join_style: JoinStyle = JoinStyle.MITRE
    ) -> Polygon:
        """
        Creates an inward offset.

        Parameters
        ----------
        polygon : Polygon
        distance : float (meters)
        join_style : JoinStyle

        Returns
        -------
        Polygon
        """

        logger.info(
            "Generating internal offset : %.2f m",
            distance
        )

        result = polygon.buffer(
            -distance,
            join_style=join_style.value
        )

        return self._process_result(result)

    # --------------------------------------------------------

    def create_external_offset(
        self,
        polygon: Polygon,
        distance: float,
        join_style: JoinStyle = JoinStyle.MITRE
    ) -> Polygon:
        """
        Creates an outward offset.
        """

        logger.info(
            "Generating external offset : %.2f m",
            distance
        )

        result = polygon.buffer(
            distance,
            join_style=join_style.value
        )

        return self._process_result(result)

    # --------------------------------------------------------

    def validate(
        self,
        geometry
    ) -> bool:
        """
        Validate geometry.
        """

        if geometry.is_empty:

            logger.error("Geometry is empty.")

            return False

        if not geometry.is_valid:

            logger.error(
                explain_validity(geometry)
            )

            return False

        return True

    # --------------------------------------------------------

    def repair(
        self,
        geometry
    ):
        """
        Attempts to repair invalid geometry.
        """

        logger.warning(
            "Repairing invalid geometry..."
        )

        repaired = geometry.buffer(0)

        if repaired.is_valid:

            logger.info(
                "Geometry repaired."
            )

        return repaired

    # --------------------------------------------------------

    def _process_result(
        self,
        geometry
    ) -> Polygon:
        """
        Internal processing.

        Handles Polygon / MultiPolygon
        """

        if geometry.is_empty:

            raise ValueError(
                "Offset operation produced empty geometry."
            )

        if not geometry.is_valid:

            geometry = self.repair(geometry)

        if isinstance(
            geometry,
            MultiPolygon
        ):

            logger.warning(
                "MultiPolygon generated."
            )

            geometry = max(
                geometry.geoms,
                key=lambda g: g.area
            )

        if not isinstance(
            geometry,
            Polygon
        ):

            raise TypeError(
                "Expected Polygon."
            )

        return geometry

    # --------------------------------------------------------

    def offset_area_loss(
        self,
        original: Polygon,
        offset: Polygon
    ) -> dict:
        """
        Calculate area statistics.
        """

        original_area = original.area
        offset_area = offset.area

        loss = original_area - offset_area

        loss_percent = (
            loss / original_area
        ) * 100.0

        return {

            "original_area": original_area,

            "offset_area": offset_area,

            "area_loss": loss,

            "loss_percent": round(
                loss_percent,
                3
            )

        }
     # --------------------------------------------------------
    # MultiPolygon Processing
    # --------------------------------------------------------

    def split_multipolygon(
        self,
        geometry
    ):
        """
        Convert Polygon/MultiPolygon into a list of polygons.
        """

        if isinstance(geometry, Polygon):
            return [geometry]

        if isinstance(geometry, MultiPolygon):
            return list(geometry.geoms)

        raise TypeError(
            "Unsupported geometry type."
        )


    # --------------------------------------------------------
    # Area Filtering
    # --------------------------------------------------------

    def filter_by_minimum_area(
        self,
        geometry,
        minimum_area: float = 100.0
    ):
        """
        Remove polygons smaller than the specified area.

        Parameters
        ----------
        minimum_area : float
            Square meters.
        """

        polygons = self.split_multipolygon(geometry)

        filtered = []

        for poly in polygons:

            if poly.area >= minimum_area:
                filtered.append(poly)

        logger.info(
            "Filtered polygons : %d -> %d",
            len(polygons),
            len(filtered)
        )

        return filtered


    # --------------------------------------------------------
    # Largest Polygon
    # --------------------------------------------------------

    def largest_polygon(
        self,
        geometry
    ):
        """
        Returns the largest polygon.
        """

        polygons = self.split_multipolygon(geometry)

        return max(
            polygons,
            key=lambda p: p.area
        )


    # --------------------------------------------------------
    # Remove Slivers
    # --------------------------------------------------------

    def remove_slivers(
        self,
        geometry,
        minimum_area: float = 10.0
    ):
        """
        Removes tiny polygons created by offset operation.
        """

        polygons = self.filter_by_minimum_area(
            geometry,
            minimum_area
        )

        if len(polygons) == 0:
            raise ValueError(
                "No usable polygons remain."
            )

        if len(polygons) == 1:
            return polygons[0]

        return MultiPolygon(polygons)


    # --------------------------------------------------------
    # Minimum Width Check
    # --------------------------------------------------------

    def minimum_width_check(
        self,
        polygon: Polygon,
        minimum_width: float
    ):
        """
        Approximate width check using bounding box.

        Returns
        -------
        bool
        """

        minx, miny, maxx, maxy = polygon.bounds

        width = maxx - minx
        height = maxy - miny

        smallest = min(width, height)

        return smallest >= minimum_width


    # --------------------------------------------------------
    # Offset Diagnostics
    # --------------------------------------------------------

    def diagnostics(
        self,
        original: Polygon,
        offset: Polygon
    ):
        """
        Returns engineering diagnostics.
        """

        loss = self.offset_area_loss(
            original,
            offset
        )

        return {

            "original_area":
                round(loss["original_area"], 3),

            "offset_area":
                round(loss["offset_area"], 3),

            "area_loss":
                round(loss["area_loss"], 3),

            "loss_percent":
                loss["loss_percent"],

            "original_perimeter":
                round(original.length, 3),

            "offset_perimeter":
                round(offset.length, 3),

            "valid":
                offset.is_valid,

            "empty":
                offset.is_empty

        }


    # --------------------------------------------------------
    # Polygon Statistics
    # --------------------------------------------------------

    def polygon_statistics(
        self,
        geometry
    ):
        """
        Returns statistics for Polygon or MultiPolygon.
        """

        polygons = self.split_multipolygon(
            geometry
        )

        stats = []

        for index, poly in enumerate(polygons):

            stats.append({

                "polygon":

                    index + 1,

                "area":

                    round(poly.area, 3),

                "perimeter":

                    round(poly.length, 3),

                "vertices":

                    len(poly.exterior.coords)

            })

        return stats


    # --------------------------------------------------------
    # Debug Print
    # --------------------------------------------------------

    def print_statistics(
        self,
        geometry
    ):

        stats = self.polygon_statistics(
            geometry
        )

        print("--------------------------------")

        for item in stats:

            print(item)

        print("--------------------------------")
    
    # --------------------------------------------------------
    # Exclusion Processing
    # --------------------------------------------------------


    def apply_exclusions(
        self,
        site_polygon: Polygon,
        exclusions: list
    ):
        """
        Remove exclusion geometries from the site.

        Parameters
        ----------
        site_polygon : Polygon

        exclusions : list[Polygon]

        Returns
        -------
        Polygon | MultiPolygon
        """

        if not exclusions:
            return site_polygon

        merged = unary_union(exclusions)

        developable = site_polygon.difference(merged)

        logger.info(
            "Applied %d exclusion polygons.",
            len(exclusions)
        )

        return developable


    # --------------------------------------------------------
    # Buffer Exclusions
    # --------------------------------------------------------

    def buffer_exclusions(
        self,
        exclusions,
        clearance: float
    ):
        """
        Apply safety clearance to all exclusions.
        """

        buffered = []

        for geom in exclusions:

            buffered.append(
                geom.buffer(clearance)
            )

        logger.info(
            "Buffered %d exclusions.",
            len(buffered)
        )

        return buffered


    # --------------------------------------------------------
    # Combined Setback
    # --------------------------------------------------------

    def generate_developable_area(
        self,
        boundary: Polygon,
        exclusions: list,
        setback: float,
        exclusion_clearance: float
    ):
        """
        Generate final developable polygon.

        Workflow

        Boundary
            ↓
        Internal Offset
            ↓
        Buffer Exclusions
            ↓
        Difference
        """

        site = self.create_internal_offset(
            boundary,
            setback
        )

        buffered = self.buffer_exclusions(
            exclusions,
            exclusion_clearance
        )

        developable = self.apply_exclusions(
            site,
            buffered
        )

        return developable


    # --------------------------------------------------------
    # Batch Offset
    # --------------------------------------------------------

    def batch_offset(
        self,
        polygons,
        distance
    ):
        """
        Offset multiple polygons.
        """

        result = []

        for poly in polygons:

            result.append(
                self.create_internal_offset(
                    poly,
                    distance
                )
            )

        return result


    # --------------------------------------------------------
    # Area Summary
    # --------------------------------------------------------

    def report(
        self,
        original,
        developable
    ):
        """
        Engineering summary.
        """

        return {

            "original_area":

                round(original.area, 2),

            "developable_area":

                round(developable.area, 2),

            "usable_percent":

                round(
                    developable.area /
                    original.area * 100,
                    2
                ),

            "lost_area":

                round(
                    original.area -
                    developable.area,
                    2
                )
        }


    # --------------------------------------------------------
    # Plot
    # --------------------------------------------------------

    def plot(
        self,
        original,
        offset=None,
        exclusions=None
    ):
        """
        Debug plotting.
        """

        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(
            figsize=(10,10)
        )

        x, y = original.exterior.xy

        ax.plot(
            x,
            y,
            linewidth=2,
            label="Boundary"
        )

        if offset is not None:

            x, y = offset.exterior.xy

            ax.plot(
                x,
                y,
                linewidth=2,
                label="Setback"
            )

        if exclusions:

            for ex in exclusions:

                if ex.geom_type == "Polygon":

                    x, y = ex.exterior.xy

                    ax.fill(
                        x,
                        y,
                        alpha=0.4
                    )

        ax.set_aspect("equal")

        ax.legend()

        plt.show()


    # --------------------------------------------------------
    # Export Diagnostics
    # --------------------------------------------------------

    def export_statistics(
        self,
        original,
        developable,
        filename
    ):
        """
        Save statistics JSON.
        """

        import json

        data = self.report(
            original,
            developable
        )

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as fp:

            json.dump(
                data,
                fp,
                indent=4
            )

        logger.info(
            "Statistics exported : %s",
            filename
        )
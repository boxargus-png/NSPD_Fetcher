"""Buffered server requests and strict local selection in the original coverage."""
import json
import math

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsFeature, QgsGeometry, QgsJsonUtils, QgsSpatialIndex,
)

from .coverage import primitive_coordinates
from .msk_transform import make_gost2001_crs


# Approximate ground metres, deliberately independent of the output CRS.
# Web Mercator's spherical scale is sec(latitude) = cosh(northing / radius).
# Use the largest scale over the polygon, so large coverages are not narrower
# at their poleward edge. This is a search allowance, not a survey tolerance.
COVERAGE_MARGIN_METERS = 5.0
WEB_MERCATOR_RADIUS = 6378137.0
FILTER_KINDS = ("inside", "outside", "unchecked")


class CoverageQuery:
    def __init__(self, coordinates):
        self.original = QgsJsonUtils.geometryFromGeoJson(json.dumps(
            {"type": "Polygon", "coordinates": coordinates}, allow_nan=False))
        if (self.original.isNull() or self.original.isEmpty()
                or not self.original.isGeosValid()):
            raise ValueError("Охват содержит пустую или некорректную геометрию")
        bbox = self.original.boundingBox()
        northing = max(abs(bbox.yMinimum()), abs(bbox.yMaximum()))
        self.margin_web_units = COVERAGE_MARGIN_METERS * math.cosh(northing / WEB_MERCATOR_RADIUS)
        self.buffered = self.original.buffer(self.margin_web_units, 16)
        if self.buffered.isEmpty() or not self.buffered.isGeosValid():
            raise ValueError("Не удалось построить буфер охвата")
        parts = primitive_coordinates([self.buffered])
        if len(parts) != 1:
            raise ValueError("Буфер охвата необходимо разбить на отдельные полигоны")
        self.coordinates = parts[0]

    def classify(self, feature):
        return CoverageSelection([self.original]).classify(feature)

    def filter_features(self, features, is_canceled=lambda: False):
        return CoverageSelection([self.original]).filter_features(features, is_canceled)


class CoverageSelection:
    """Check all original polygons before rounding, without changing raw records."""
    def __init__(self, geometries, source_crs=None, transform_context=None):
        web = QgsCoordinateReferenceSystem("EPSG:3857")
        source_crs = web if source_crs is None else source_crs
        if not source_crs.isValid():
            raise ValueError("У охвата не задана корректная система координат.")
        try:
            # Match the existing NSPD -> GOST 2001 -> assign GOST 2008 chain.
            # Recognition uses the definition, never a CRS name or output CRS.
            comparison_crs = make_gost2001_crs(source_crs)
        except ValueError:
            comparison_crs = source_crs
        self.transform = QgsCoordinateTransform(
            web, comparison_crs,
            transform_context if transform_context is not None else QgsCoordinateTransformContext())
        self.geometries = [QgsGeometry(geometry) for geometry in geometries]
        self.index = QgsSpatialIndex()
        for index, geometry in enumerate(self.geometries):
            if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
                raise ValueError("Охват содержит пустую или некорректную геометрию")
            feature = QgsFeature(index)
            feature.setGeometry(geometry)
            if not self.index.addFeature(feature):
                raise ValueError("Не удалось подготовить пространственную проверку охвата")

    def classify(self, feature):
        """Only demonstrably disjoint, valid geometries can be discarded."""
        try:
            raw = feature.get("geometry")
            if raw is None:
                return "unchecked"
            geometry = QgsJsonUtils.geometryFromGeoJson(json.dumps(raw, allow_nan=False))
            if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
                return "unchecked"
            if geometry.transform(self.transform) != 0:
                return "unchecked"
            if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
                return "unchecked"
            unchecked = False
            for index in self.index.intersects(geometry.boundingBox()):
                original = self.geometries[index]
                if original.intersects(geometry):
                    return "inside"
                unchecked = unchecked or bool(original.lastError())
            return "unchecked" if unchecked else "outside"
        except Exception:
            # A failed geometry check is not evidence that the feature lies
            # outside the coverage. Retain the untouched server record.
            return "unchecked"

    def filter_features(self, features, is_canceled=lambda: False):
        selected = []
        counts = dict.fromkeys(FILTER_KINDS, 0)
        for index, feature in enumerate(features):
            if is_canceled():
                # The entire response was already received: cancellation must
                # preserve even the records which have not been checked yet.
                selected.extend(features[index:])
                counts["unchecked"] += len(features) - index
                break
            kind = self.classify(feature)
            counts[kind] += 1
            if kind != "outside":
                selected.append(feature)
        return selected, counts

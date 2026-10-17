import copy
import json
import math
import unittest
from unittest.mock import patch

from tests.qgis_support import APP
from tests.support import ROOT, plugin_module
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
import processing

msk = plugin_module("msk_transform")
layers = plugin_module("layer_manager")


def reference_data():
    return json.loads((ROOT / "tests" / "fixtures" / "msk52_52_18_0050232_698.json")
                      .read_text(encoding="utf-8"))


def crs_from_proj(definition):
    crs = QgsCoordinateReferenceSystem()
    if not crs.createFromProj(definition):
        raise AssertionError("Invalid test CRS: " + definition)
    return crs


class MskTransformTests(unittest.TestCase):
    def setUp(self):
        self.reference = reference_data()
        self.target = crs_from_proj(self.reference["target_proj"])

    def tearDown(self):
        QgsProject.instance().clear()

    def test_preserves_projection_parameters_and_does_not_modify_target(self):
        for longitude, easting, northing in (("44.55", "2250000", "-5714743.504"),
                                             ("50.55", "2250000", "-5914743.504"),
                                             ("41.55", "1250000", "-5714743.504")):
            with self.subTest(longitude=longitude):
                definition = (self.reference["target_proj"].replace("44.55", longitude)
                              .replace("2250000", easting).replace("-5714743.504", northing))
                target = crs_from_proj(definition)
                before = target.toWkt()
                working = msk.make_gost2001_crs(target)
                original_tokens = set(target.toProj().split())
                working_tokens = set(working.toProj().split())
                self.assertEqual(
                    {t for t in original_tokens if not t.startswith("+towgs84=")},
                    {t for t in working_tokens if not t.startswith("+towgs84=")})
                self.assertIn("+towgs84=23.92,-141.27,-80.9,0,0.35,0.82,-0.12", working_tokens)
                self.assertEqual(target.toWkt(), before)
                self.assertEqual(working.srsid(), 0)  # No CRS saved in the user database.

    def test_accepts_wkt_and_serialization_precision_without_using_crs_name(self):
        target = QgsCoordinateReferenceSystem()
        wkt = self.target.toWkt().replace('PROJCS["unknown"', 'PROJCS["Test local grid"')
        wkt = wkt.replace(",-0.22]", ",-0.219999999995]")
        self.assertTrue(target.createFromWkt(wkt))
        working = msk.make_gost2001_crs(target)
        legacy = crs_from_proj(self.reference["legacy_working_proj"])
        for crs in (working, legacy):
            transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:3857"),
                                               crs, QgsProject.instance())
            point = transform.transform(QgsPointXY(*self.reference["nspd_geometry"]["coordinates"][0][0]))
            self.assertAlmostEqual(point.x(), 2212223.659912397, places=7)
            self.assertAlmostEqual(point.y(), 525621.6899814242, places=7)

    def test_rejects_missing_unknown_or_incompatible_transformations(self):
        target_proj = self.reference["target_proj"]
        shift = "+towgs84=23.57,-140.95,-79.8,0,0.35,0.79,-0.22"
        invalid = {
            "no selection": QgsCoordinateReferenceSystem(),
            "WGS84": QgsCoordinateReferenceSystem("EPSG:4326"),
            "Web Mercator": QgsCoordinateReferenceSystem("EPSG:3857"),
            "no datum shift": crs_from_proj(target_proj.replace(shift, "")),
            "different shift": crs_from_proj(target_proj.replace("23.57,", "24.57,")),
            "2001 selected as final": crs_from_proj(self.reference["legacy_working_proj"]),
            "different ellipsoid": crs_from_proj(target_proj.replace("ellps=krass", "ellps=WGS84")),
            "different units": crs_from_proj(target_proj.replace("units=m", "units=ft")),
            "grid shift": crs_from_proj(target_proj + " +nadgrids=@null"),
        }
        for name, crs in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                msk.make_gost2001_crs(crs)

    def source_layer(self):
        return layers.load_geojson_layer({"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"cad_num": self.reference["cadastral_number"]},
            "geometry": copy.deepcopy(self.reference["nspd_geometry"]),
        }]})

    def matched_distances(self, layer):
        self.assertEqual(layer.featureCount(), 1)
        feature = next(layer.getFeatures())
        geometry = feature.geometry()
        ring = [(point.x(), point.y()) for point in geometry.vertices()]
        self.assertEqual(len(ring), 5)
        self.assertEqual(ring[0], ring[-1])
        expected = self.reference["expected_ring_easting_northing"][:-1]
        # Server and extract have opposite winding. Preserve topology while
        # allowing either winding and any starting vertex.
        orders = [[ring[(start + direction * i) % 4] for i in range(4)]
                  for direction in (1, -1) for start in range(4)]
        aligned = min(orders, key=lambda pts: sum(math.dist(a, b) ** 2 for a, b in zip(pts, expected)))
        return [math.dist(a, b) for a, b in zip(aligned, expected)]

    def test_reference_parcel_matches_extract_and_legacy_chain(self):
        source = self.source_layer()
        generated = msk.make_gost2001_crs(self.target)
        legacy = crs_from_proj(self.reference["legacy_working_proj"])
        for rounding in (False, True):
            with self.subTest(rounding=rounding):
                with patch.object(processing, "run", wraps=processing.run) as run:
                    actual = layers.process_result_layer(source, generated, self.target, rounding)
                expected_order = ["native:reprojectlayer"]
                if rounding:
                    expected_order.append("native:snappointstogrid")
                expected_order.append("native:assignprojection")
                self.assertEqual([call.args[0] for call in run.call_args_list], expected_order)
                # Explicit old pipeline anchors the comparison across changes
                # to result-layer preparation and deduplication.
                previous = processing.run('native:deleteduplicategeometries', {
                    'INPUT': source, 'OUTPUT': 'memory:',
                })['OUTPUT']
                previous = processing.run('native:reprojectlayer', {
                    'INPUT': previous, 'TARGET_CRS': legacy, 'OUTPUT': 'memory:',
                })['OUTPUT']
                if rounding:
                    previous = processing.run('native:snappointstogrid', {
                        'INPUT': previous, 'HSPACING': 0.01, 'VSPACING': 0.01,
                        'ZSPACING': 0, 'MSPACING': 0, 'OUTPUT': 'memory:',
                    })['OUTPUT']
                previous = processing.run('native:assignprojection', {
                    'INPUT': previous, 'CRS': self.target, 'OUTPUT': 'memory:',
                })['OUTPUT']
                actual_feature = next(actual.getFeatures())
                previous_feature = next(previous.getFeatures())
                self.assertEqual(bytes(actual_feature.geometry().asWkb()),
                                 bytes(previous_feature.geometry().asWkb()))
                self.assertEqual(actual_feature["cad_num"], self.reference["cadastral_number"])
                distances = self.matched_distances(actual)
                self.assertLess(max(distances), 1e-8 if rounding else 0.0002)
                if not rounding:
                    self.assertGreater(min(distances), 1e-5)
                # A selected custom CRS may be serialized by Processing. Check
                # both forward mappings; an extra 2D Helmert round trip itself
                # has a small residual and is not an identity test.
                web = QgsCoordinateReferenceSystem("EPSG:3857")
                actual_transform = QgsCoordinateTransform(actual.crs(), web, QgsProject.instance())
                target_transform = QgsCoordinateTransform(self.target, web, QgsProject.instance())
                point = QgsPointXY(2212223.66, 525621.69)
                actual_point = actual_transform.transform(point)
                target_point = target_transform.transform(point)
                self.assertLess(math.hypot(actual_point.x() - target_point.x(),
                                           actual_point.y() - target_point.y()), 1e-6)

    def test_direct_2008_does_not_accidentally_pass_reference(self):
        direct = layers.process_result_layer(self.source_layer(), self.target)
        for distance in self.matched_distances(direct):
            self.assertGreater(distance, 0.638)
            self.assertLess(distance, 0.640)

    def test_repeated_reference_parcel_is_published_once_with_original_coordinates(self):
        feature = {
            "type": "Feature", "id": 291607188,
            "properties": {"category": 36368, "cad_num": self.reference["cadastral_number"]},
            "geometry": copy.deepcopy(self.reference["nspd_geometry"]),
        }
        collection = {"type": "FeatureCollection", "features": [feature, copy.deepcopy(feature)]}
        success, error = layers.add_result_layer(
            collection, msk.make_gost2001_crs(self.target), self.target, True)
        self.assertTrue(success, error)
        result = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertLess(max(self.matched_distances(result)), 1e-8)
        self.assertEqual(next(result.getFeatures())["nspd_id"], 291607188)
        self.assertEqual(len(collection["features"]), 2)

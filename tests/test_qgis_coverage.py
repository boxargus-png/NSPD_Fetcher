import unittest

from tests.qgis_support import APP
from tests.support import plugin_module
from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

coverage = plugin_module("coverage")


class CoverageTests(unittest.TestCase):
    def tearDown(self):
        QgsProject.instance().clear()

    def test_polygon_holes_and_multipart_are_preserved(self):
        polygon = QgsGeometry.fromWkt(
            "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 3, 3 3, 3 2, 2 2))")
        other = QgsGeometry.fromWkt("POLYGON ((20 20, 30 20, 30 30, 20 20))")
        multipart = QgsGeometry.collectGeometry([polygon, other])
        coordinates = coverage.primitive_coordinates([multipart])
        self.assertEqual(len(coordinates), 2)
        self.assertEqual([len(rings) for rings in coordinates], [2, 1])
        self.assertEqual(coordinates[0][1][0], [2.0, 2.0])

    def test_oversized_polygon_does_not_silently_produce_partial_coverage(self):
        small = QgsGeometry.fromWkt("POLYGON ((0 0, 10 0, 10 10, 0 0))")
        large = QgsGeometry.fromWkt("POLYGON ((0 0, 2000000 0, 2000000 1, 0 0))")
        with self.assertRaises(ValueError):
            coverage.primitive_coordinates([small, large])

    def test_selected_point_features_are_buffered_and_source_is_unchanged(self):
        layer = QgsVectorLayer("Point?crs=EPSG:3857", "Test", "memory")
        features = []
        for x in (0, 100):
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromWkt("POINT ({} 0)".format(x)))
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        layer.selectByIds([next(layer.getFeatures()).id()])
        geometries = coverage.geometries_from_layer(layer)
        self.assertEqual(len(geometries), 1)
        self.assertGreater(geometries[0].area(), 0)
        self.assertAlmostEqual(geometries[0].boundingBox().width(), 2)
        self.assertEqual(next(layer.getFeatures()).geometry().asWkt(), "Point (0 0)")

    def test_grid_split_preserves_area_including_hole(self):
        geometry = QgsGeometry.fromWkt(
            "POLYGON ((0 0, 45000 0, 45000 45000, 0 45000, 0 0), "
            "(1000 1000, 1000 2000, 2000 2000, 2000 1000, 1000 1000))")
        self.assertTrue(coverage.needs_grid_split([geometry]))
        split = coverage.split_with_grid([geometry])
        self.assertGreater(len(split), 1)
        self.assertAlmostEqual(sum(part.area() for part in split), geometry.area())
        self.assertTrue(all(part.boundingBox().width() <= coverage.GRID_SPACING for part in split))
        self.assertTrue(coverage.primitive_coordinates(split))

    def test_reprojection_does_not_modify_original_geometry(self):
        geometry = QgsGeometry.fromWkt("POINT (30 60)")
        projected = coverage.to_web_mercator(geometry, QgsCoordinateReferenceSystem("EPSG:4326"))
        self.assertEqual(geometry.asWkt(), "Point (30 60)")
        self.assertGreater(projected.asPoint().x(), 3000000)

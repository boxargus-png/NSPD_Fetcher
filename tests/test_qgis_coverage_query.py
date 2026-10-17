import copy
import json
import unittest
from unittest.mock import Mock

from tests.qgis_support import APP
from tests.support import plugin_module
from tests.test_qgis_tasks import COVERAGE, ScriptedClient
from tests.manual_coverage_boundary import make_mask, current_vector_query
from tests.test_qgis_msk import reference_data, crs_from_proj
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
    QgsGeometry, QgsJsonUtils, QgsPointXY, QgsProject, QgsRectangle,
)

queries = plugin_module("coverage_query")
tasks = plugin_module("api_task")
layers = plugin_module("layer_manager")
msk = plugin_module("msk_transform")
coverage = plugin_module("coverage")


def point(x, y, identity=1):
    return {"type": "Feature", "id": identity, "properties": {"category": 36368},
            "geometry": {"type": "Point", "coordinates": [x, y]}}


class BufferedCoverageTests(unittest.TestCase):
    def tearDown(self):
        QgsProject.instance().clear()

    def test_inside_and_touching_stay_but_buffer_only_and_far_geometry_are_removed(self):
        query = queries.CoverageQuery(COVERAGE)
        features = [point(5, 5), point(10, 5, 2), point(14, 5, 3), point(16, 5, 4)]
        before = copy.deepcopy(features)
        selected, counts = query.filter_features(features)
        self.assertEqual(selected, features[:2])
        self.assertEqual(counts, {"inside": 2, "outside": 2, "unchecked": 0})
        self.assertEqual(features, before)
        self.assertTrue(all(a is b for a, b in zip(selected, features)))
        self.assertGreater(query.buffered.area(), query.original.area())

    def test_holes_and_actual_geometry_are_used_instead_of_bounding_boxes(self):
        donut = queries.CoverageQuery([
            [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
            [[20, 20], [20, 80], [80, 80], [80, 20], [20, 20]],
        ])
        self.assertEqual(donut.classify(point(50, 50)), "outside")
        self.assertEqual(donut.classify(point(22, 50)), "outside")
        self.assertEqual(donut.classify(point(10, 50)), "inside")
        triangle = queries.CoverageQuery([[[0, 0], [100, 0], [0, 100], [0, 0]]])
        self.assertEqual(triangle.classify(point(99, 99)), "outside")
        multipart = point(1, 1)
        multipart["geometry"] = {"type": "MultiPoint", "coordinates": [[99, 99], [1, 1]]}
        self.assertEqual(triangle.classify(multipart), "inside")

    def test_missing_empty_or_invalid_geometry_is_retained(self):
        features = [dict(point(1, 1), geometry=geometry) for geometry in (
            None, {"type": "Point", "coordinates": []},
            {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [0, 10], [10, 0], [0, 0]]]},
        )]
        selected, counts = queries.CoverageQuery(COVERAGE).filter_features(features)
        self.assertEqual(selected, features)
        self.assertEqual(counts["unchecked"], len(features))

    def test_cancellation_during_filtering_preserves_unchecked_received_records(self):
        features = [point(1, 1), point(1000, 1000), point(2000, 2000)]
        selected, counts = queries.CoverageQuery(COVERAGE).filter_features(
            features, Mock(side_effect=[False, True]))
        self.assertEqual(selected, features)
        self.assertEqual(counts, {"inside": 1, "outside": 0, "unchecked": 2})

    def test_all_coverage_parts_are_checked_even_if_later_request_fails(self):
        second = [[[100, 0], [110, 0], [110, 10], [100, 10], [100, 0]]]
        features = [point(5, 5), point(105, 5, 2), point(114, 5, 3)]
        api = plugin_module("api_client")
        client = ScriptedClient([features, api.NspdApiError("timeout", "network")])
        task = tasks.FetchNspdDataTask("Test", [COVERAGE, second], [{"name": "land", "id": 1}],
                                       client_factory=lambda: client)
        self.assertTrue(task.run())
        self.assertEqual(task.result.collections["land"]["features"], features[:2])
        self.assertEqual(task.coverage_counts, {"inside": 2, "outside": 1, "unchecked": 0})

    def test_invalid_coverage_is_reported_before_any_request(self):
        client = ScriptedClient([])
        task = tasks.FetchNspdDataTask("Test", [[[]]], [{"name": "land", "id": 1}],
                                       client_factory=lambda: client)
        self.assertFalse(task.run())
        self.assertEqual(client.calls, [])
        self.assertIn("некорректную геометрию", str(task.result.errors[0].error))

    def test_buffer_is_about_five_ground_metres_at_different_latitudes(self):
        web = QgsCoordinateReferenceSystem("EPSG:3857")
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), web, QgsProject.instance())
        distance = QgsDistanceArea()
        distance.setSourceCrs(web, QgsProject.instance().transformContext())
        distance.setEllipsoid("WGS84")
        for latitude in (0, 40, 56, 70, 80):
            with self.subTest(latitude=latitude):
                center = transform.transform(QgsPointXY(45, latitude))
                x, y = center.x(), center.y()
                query = queries.CoverageQuery([[[x, y], [x+10, y], [x+10, y+10], [x, y+10], [x, y]]])
                measured = distance.measureLine(QgsPointXY(x, y+5), QgsPointXY(x-query.margin_web_units, y+5))
                self.assertGreater(measured, 4.95)
                self.assertLess(measured, 5.1)

    def test_reference_boundary_parcel_survives_buffer_filter_and_msk_chain(self):
        reference = reference_data()
        target = crs_from_proj(reference["target_proj"])
        mask = make_mask(reference, 0.25)
        original_web = current_vector_query(mask, target)
        coordinates = json.loads(original_web.asJson(15))["coordinates"]
        original = {"type": "Feature", "id": 291607188,
                    "properties": {"category": 36368, "cad_num": reference["cadastral_number"]},
                    "geometry": reference["nspd_geometry"]}
        self.assertFalse(original_web.intersects(QgsJsonUtils.geometryFromGeoJson(json.dumps(original["geometry"]))))
        # A nearby parcel is still in the server query buffer, but does not
        # intersect the original mask after the MSK chain.
        nearby = copy.deepcopy(original)
        nearby["id"] = 998
        for coordinate in nearby["geometry"]["coordinates"][0]:
            coordinate[1] -= 5
        distant = copy.deepcopy(original)
        distant["id"] = 999
        for coordinate in distant["geometry"]["coordinates"][0]:
            coordinate[0] += 10000
        received = [original, copy.deepcopy(original), nearby, distant]
        before = copy.deepcopy(received)
        client = ScriptedClient([received])
        task = tasks.FetchNspdDataTask("Boundary", [coordinates], [{"name": "land", "id": 36368}],
                                       client_factory=lambda: client,
                                       coverage_input=coverage.CoverageInput([mask], target))
        self.assertTrue(task.run())
        self.assertEqual(task.result.received_count, 4)
        self.assertEqual(task.result.feature_count, 2)
        self.assertEqual(task.coverage_counts, {"inside": 2, "outside": 2, "unchecked": 0})
        self.assertEqual(received, before)
        sent = QgsJsonUtils.geometryFromGeoJson(json.dumps({"type": "Polygon", "coordinates": client.calls[0][0]}))
        self.assertTrue(sent.contains(original_web))
        self.assertTrue(sent.intersects(QgsJsonUtils.geometryFromGeoJson(json.dumps(nearby["geometry"]))))
        for rounding in (False, True):
            with self.subTest(rounding=rounding):
                QgsProject.instance().clear()
                success, error = layers.add_result_layer(
                    task.result.collections["land"], msk.make_gost2001_crs(target), target, rounding)
                self.assertTrue(success, error)
                result_layers = list(QgsProject.instance().mapLayers().values())
                self.assertEqual(len(result_layers), 1)
                self.assertEqual(result_layers[0].featureCount(), 1)
                actual = next(result_layers[0].getFeatures())
                self.assertEqual(actual["nspd_id"], 291607188)
                for vertex in actual.geometry().vertices():
                    residual = min(((vertex.x()-x)**2+(vertex.y()-y)**2)**0.5
                                   for x, y in reference["expected_ring_easting_northing"])
                    self.assertLess(residual, 1e-8 if rounding else 0.0002)

    def test_ordinary_source_crs_selection_is_independent_of_output_msk(self):
        reference = reference_data()
        target = crs_from_proj(reference["target_proj"])
        feature = {"type": "Feature", "id": 1, "properties": {}, "geometry": reference["nspd_geometry"]}
        raw = QgsJsonUtils.geometryFromGeoJson(json.dumps(feature["geometry"]))
        outside = current_vector_query(make_mask(reference, 0.25), target)
        web = QgsCoordinateReferenceSystem("EPSG:3857")
        for crs_id in ("EPSG:3857", "EPSG:4326", "EPSG:32638"):
            for mask, expected in ((outside, 0), (raw.centroid().buffer(0.1, 8), 1)):
                with self.subTest(crs=crs_id, expected=expected):
                    source = QgsCoordinateReferenceSystem(crs_id)
                    original = QgsGeometry(mask)
                    original.transform(QgsCoordinateTransform(web, source, QgsProject.instance()))
                    captured = coverage.CoverageInput([original], source)
                    client = ScriptedClient([[feature]])
                    task = tasks.FetchNspdDataTask(
                        "Ordinary CRS", coverage.primitive_coordinates(captured.request_geometries),
                        [{"name": "land", "id": 36368}], crs_reproject=msk.make_gost2001_crs(target),
                        crs_assign=target, client_factory=lambda: client, coverage_input=captured)
                    self.assertTrue(task.run())
                    self.assertEqual(task.result.feature_count, expected)
                    self.assertEqual(task.coverage_counts["unchecked"], 0)

    def test_msk_selection_precedes_rounding_for_both_sides_of_boundary(self):
        target = crs_from_proj(reference_data()["target_proj"])
        working = msk.make_gost2001_crs(target)
        mask = QgsGeometry.fromRect(QgsRectangle(2212230.003, 525620, 2212231.003, 525621))
        captured = coverage.CoverageInput([mask], target)
        inverse = QgsCoordinateTransform(working, QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
        features = []
        for identity, x in ((1, 2212230.004), (2, 2212231.004)):
            raw = inverse.transform(QgsPointXY(x, 525620.5))
            features.append(point(raw.x(), raw.y(), identity))
        for rounding in (False, True):
            with self.subTest(rounding=rounding):
                QgsProject.instance().clear()
                client = ScriptedClient([features])
                task = tasks.FetchNspdDataTask(
                    "Rounding boundary", coverage.primitive_coordinates(captured.request_geometries),
                    [{"name": "land", "id": 36368}], crs_reproject=working, crs_assign=target,
                    round_mm=rounding, client_factory=lambda: client, coverage_input=captured)
                self.assertTrue(task.run())
                self.assertEqual(task.result.collections["land"]["features"], features[:1])
                self.assertEqual(task.coverage_counts, {"inside": 1, "outside": 1, "unchecked": 0})
                success, error = layers.add_result_layer(task.result.collections["land"], working, target, rounding)
                self.assertTrue(success, error)
                actual = next(next(iter(QgsProject.instance().mapLayers().values())).getFeatures())
                self.assertEqual(actual["nspd_id"], 1)
                # Rounding moves the retained point out of this sub-centimetre
                # boundary. Selection must keep the unrounded decision.
                self.assertEqual(mask.intersects(actual.geometry()), not rounding)

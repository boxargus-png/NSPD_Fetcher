import copy
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from tests.support import plugin_module

api = plugin_module("api_client")
results = plugin_module("fetch_result")
FEATURE = {"type": "Feature", "geometry": None, "properties": {"id": "test"}}


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.respond({"features": [FEATURE]})
        self.client = api.NspdClient(self.transport)

    def respond(self, payload, status=200):
        self.transport.request.return_value = api.NetworkResponse(status, json.dumps(payload).encode("utf-8"))

    def test_coverage_wire_format_and_holes(self):
        rings = [[[0, 0], [10, 0], [10, 10], [0, 0]], [[1, 1], [2, 1], [2, 2], [1, 1]]]
        self.assertEqual(self.client.intersects(rings, 36368), [FEATURE])
        args, kwargs = self.transport.request.call_args
        self.assertEqual(args, ("POST", api.INTERSECTS_URL))
        body = json.loads(kwargs["body"])
        self.assertEqual(body["categories"], [{"id": 36368}])
        geometry = body["geom"]["features"][0]["geometry"]
        self.assertEqual(geometry["coordinates"], rings)
        self.assertEqual(geometry["crs"]["properties"]["name"], "EPSG:3857")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

    def test_cadastral_formats_and_filter(self):
        payloads = [
            {"features": [FEATURE]}, {"data": {"features": [FEATURE]}},
            {"data": {"elements": [{"geometry": None, "attributes": {"id": "test"}}]}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.respond(payload)
                self.assertEqual(self.client.search("test-number", [36048, 36049]), [FEATURE])
        self.assertEqual(parse_qs(urlsplit(self.transport.request.call_args.args[1]).query), {
            "query": ["test-number"], "layersId": ["36048", "36049"],
        })

    def test_query_is_url_encoded_and_unfiltered_search_omits_layers(self):
        self.client.search("52:18:0050232:698 &тест", [])
        self.assertEqual(parse_qs(urlsplit(self.transport.request.call_args.args[1]).query),
                         {"query": ["52:18:0050232:698 &тест"]})

    def test_elements_format_preserves_source_id_category_and_metadata(self):
        element = {"id": 291607188, "geometry": None,
                   "attributes": {"category": 36368, "name": "test"}, "extra": "keep"}
        original = copy.deepcopy(element)
        self.respond({"data": {"elements": [element]}})
        feature = self.client.search("test", [36048])[0]
        self.assertEqual(feature['id'], 291607188)
        self.assertEqual(feature['properties'], element['attributes'])
        self.assertEqual(feature['extra'], 'keep')
        self.assertEqual(element, original)

    def test_empty_search_is_not_an_error(self):
        for payload in ({"features": []}, {"data": {"elements": []}}):
            self.respond(payload)
            self.assertEqual(self.client.search("missing", [36048]), [])

    def test_transport_error_is_not_an_empty_response(self):
        self.transport.request.side_effect = api.NspdApiError("connection reset", "network")
        with self.assertRaises(api.NspdApiError) as caught:
            self.client.search("test", [36048])
        self.assertEqual(caught.exception.kind, "network")
        self.assertIsNone(caught.exception.status_code)

    def test_http_error_preserves_diagnostics(self):
        self.transport.request.return_value = api.NetworkResponse(403, b"server response")
        with self.assertRaises(api.NspdApiError) as caught:
            self.client.search("test", [36048])
        self.assertEqual(caught.exception.kind, "http")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.response_text, "server response")

    def test_invalid_json_and_feature_schema_are_errors(self):
        self.transport.request.return_value = api.NetworkResponse(200, b"<html>not json</html>")
        with self.assertRaises(api.NspdApiError) as caught:
            self.client.search("test", [36048])
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(caught.exception.response_text, "<html>not json</html>")
        for payload in ([], {"features": "wrong"}, {"features": [None]}):
            self.respond(payload)
            with self.subTest(payload=payload), self.assertRaises(api.NspdApiError):
                self.client.search("test", [36048])

    def test_schema_error_preserves_bounded_server_diagnostics(self):
        self.respond({"error": "failure " * 500})
        with self.assertRaises(api.NspdApiError) as caught:
            self.client.search("test", [36048])
        self.assertEqual(caught.exception.kind, "response")
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(len(caught.exception.response_text), 1000)


class ResponseTests(unittest.TestCase):
    def test_server_errors_and_unknown_envelopes_are_not_empty_results(self):
        payloads = [{}, {"message": "service unavailable"}, {"error": "failure"},
                    {"errors": ["failure"], "features": []},
                    {"success": False, "features": []}, {"status": 503, "features": []},
                    {"status": "error", "features": []}, {"features": None},
                    {"type": "Feature", "features": []}]
        for payload in payloads:
            for wrapped in (payload, {"data": payload}):
                with self.subTest(payload=wrapped), self.assertRaises(api.NspdApiError) as caught:
                    api.parse_response(wrapped, search=True)
                self.assertEqual(caught.exception.kind, "response")
        for payload in ({"data": None}, {"data": []}, {"error": "failure", "data": {"features": []}}):
            with self.subTest(payload=payload), self.assertRaises(api.NspdApiError):
                api.parse_response(payload, search=True)
        with self.assertRaises(api.NspdApiError):
            api.parse_response({"error": "failure"})

    def test_invalid_geometry_attributes_and_crs_are_reported(self):
        invalid_geometries = [[], {}, {"type": "unknown", "coordinates": []},
                              {"type": [], "coordinates": []},
                              {"type": "Point", "coordinates": [1]},
                              {"type": "Point", "coordinates": [True, 1]},
                              {"type": "Point", "coordinates": [1, float("nan")]},
                              {"type": "Point", "coordinates": [1, float("inf")]},
                              {"type": "Polygon", "coordinates": [[1, 2]]},
                              {"type": "Polygon", "coordinates": "bad"},
                              {"type": "GeometryCollection", "geometries": [None]},
                              {"type": "Point", "coordinates": [1, 2],
                               "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}}]
        for geometry in invalid_geometries:
            with self.subTest(geometry=geometry), self.assertRaises(api.NspdApiError):
                api.parse_response({"features": [dict(FEATURE, geometry=geometry)]})
        for feature in ({"type": "Feature"}, dict(FEATURE, properties=[]), dict(FEATURE, type="other")):
            with self.subTest(feature=feature), self.assertRaises(api.NspdApiError):
                api.parse_response({"features": [feature]})
        with self.assertRaises(api.NspdApiError):
            api.parse_response({"features": [], "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}})

    def test_valid_geometries_and_null_are_preserved_without_mutating_input(self):
        geometries = [None, {"type": "Point", "coordinates": [1, 2, 3]},
                      {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
                      {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
                      {"type": "MultiLineString", "coordinates": [[[1, 2], [3, 4]]]},
                      {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                      {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]},
                      {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1, 2]}]}]
        for geometry in geometries:
            data = {"features": [dict(FEATURE, geometry=geometry)]}
            original = copy.deepcopy(data)
            with self.subTest(geometry=geometry):
                self.assertEqual(api.parse_response(data), original["features"])
                self.assertEqual(data, original)


class ResultTests(unittest.TestCase):
    def test_spatially_filtered_records_are_not_reported_as_empty_server_responses(self):
        result = results.FetchResult()
        result.record("outside", "land", [], received_count=2)
        result.finish()
        self.assertEqual(result.received_count, 2)
        self.assertEqual(result.feature_count, 0)
        self.assertEqual(result.not_found, [])

    def test_partial_result_separates_absence_from_failure(self):
        result = results.FetchResult()
        result.record("found", "", [FEATURE])
        result.record("missing", "", [])
        result.record("failed", "", error=api.NspdApiError("timeout", "network"))
        result.finish()
        self.assertEqual(result.status, results.FetchStatus.PARTIAL)
        self.assertEqual(result.not_found, ["missing"])
        self.assertEqual([outcome.key for outcome in result.errors], ["failed"])
        self.assertEqual(result.feature_count, 1)

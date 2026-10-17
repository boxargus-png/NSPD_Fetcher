"""Real Qt/QGIS HTTP requests against a local server; no NSPD dependency."""
import json
import queue
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tests.qgis_support import APP
from tests.support import plugin_module
from tests.test_qgis_msk import crs_from_proj, reference_data
from qgis.core import QgsBlockingNetworkRequest, QgsFeedback, QgsJsonUtils, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtTest import QTest

api = plugin_module("api_client")
network = plugin_module("qgis_network")
tasks = plugin_module("api_task")
layers = plugin_module("layer_manager")
msk = plugin_module("msk_transform")
Controller = plugin_module("task_controller").FetchController
FetchStatus = plugin_module("fetch_result").FetchStatus


class NetworkErrorTests(unittest.TestCase):
    def test_unsupported_qgis_is_rejected_before_calling_unsafe_native_api(self):
        with patch.object(network, "Qgis", QGIS_VERSION_INT=33414), \
                patch.object(network.QgsNetworkAccessManager, "setRequestPreprocessor") as register:
            with self.assertRaisesRegex(RuntimeError, "QGIS 3.40"):
                network.prepare_network()
            register.assert_not_called()

    def test_timeout_tls_network_and_http_failures_remain_distinct(self):
        cases = [
            (QgsBlockingNetworkRequest.TimeoutError, QNetworkReply.OperationCanceledError, None, "timeout"),
            (QgsBlockingNetworkRequest.ServerExceptionError, QNetworkReply.SslHandshakeFailedError, None, "tls"),
            (QgsBlockingNetworkRequest.ServerExceptionError, QNetworkReply.HostNotFoundError, None, "network"),
            # A broken transfer with HTTP 200 must not accept a partial body.
            (QgsBlockingNetworkRequest.ServerExceptionError, QNetworkReply.RemoteHostClosedError, 200, "network"),
            (QgsBlockingNetworkRequest.ServerExceptionError, QNetworkReply.ContentAccessDenied, 403, "http"),
        ]
        for code, qt_error, status, kind in cases:
            reply = Mock()
            reply.attribute.return_value = status
            reply.content.return_value = b"diagnostic"
            reply.error.return_value = qt_error
            reply.errorString.return_value = "Qt diagnostic"
            blocking = Mock()
            blocking.get.return_value = code
            blocking.reply.return_value = reply
            blocking.errorMessage.return_value = "QGIS diagnostic"
            factory = Mock(return_value=blocking, NoError=QgsBlockingNetworkRequest.NoError,
                           TimeoutError=QgsBlockingNetworkRequest.TimeoutError)
            with self.subTest(kind=kind), patch.object(network, "QgsBlockingNetworkRequest", factory):
                client = api.NspdClient()
                with self.assertRaises(api.NspdApiError) as caught:
                    client.search("test", [36048])
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.response_text, "diagnostic")

    def test_pre_canceled_feedback_does_not_open_a_connection(self):
        feedback = QgsFeedback()
        feedback.cancel()
        with patch.object(network, "QgsBlockingNetworkRequest") as factory:
            with self.assertRaises(api.NspdApiError) as caught:
                api.NspdClient(feedback=feedback).search("test", [36048])
            self.assertEqual(caught.exception.kind, "canceled")
            factory.assert_not_called()


class LocalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = reference_data()
        cls.feature = {"type": "Feature", "geometry": cls.reference["nspd_geometry"],
                       "properties": {"cad_num": cls.reference["cadastral_number"]}}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                self.server.requests.put(("GET", query, dict(self.headers)))
                number = query.get("query", [""])[0]
                if number == "slow":
                    self.server.received.set()
                    self.server.release.wait(10)
                if number == "denied":
                    self.respond(403, b"access denied")
                elif number == "bad-schema":
                    self.respond(200, b'{"error":"service unavailable"}')
                else:
                    self.respond(200, json.dumps({"data": {"features": [cls.feature]}}).encode("utf-8"))

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                self.server.requests.put(("POST", json.loads(body), dict(self.headers)))
                self.respond(200, json.dumps({"features": [cls.feature]}).encode("utf-8"))

            def respond(self, status, body):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when QGIS has canceled the request.

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.requests = queue.Queue()
        cls.server.received = threading.Event()
        cls.server.release = threading.Event()
        cls.base_url = "http://127.0.0.1:{}".format(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.received.clear()
        self.server.release.clear()
        self.server.requests = queue.Queue()
        self.controller = Controller()
        self.finished = []
        self.controller.taskFinished.connect(lambda task: self.finished.append(task.result))
        self.patches = [patch.object(api, "SEARCH_URL", self.base_url + "/search"),
                        patch.object(api, "INTERSECTS_URL", self.base_url + "/intersects?typeIntersect=fullObject")]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        self.server.release.set()
        self.controller.shutdown()
        deadline = time.monotonic() + 3
        while self.controller._manager.activeTasks() and time.monotonic() < deadline:
            QTest.qWait(10)
        APP.processEvents()
        QgsProject.instance().clear()
        for patcher in self.patches:
            patcher.stop()

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "Background HTTP task did not complete in time")

    def run_task(self, task):
        self.assertTrue(self.controller.start(task))
        self.wait_until(lambda: bool(self.finished))
        return self.finished[0]

    def test_search_over_qgis_network_keeps_msk_reference_coordinates(self):
        task = tasks.FetchNspdByCadastralTask("Local test", [self.reference["cadastral_number"]], [36048, 36049])
        result = self.run_task(task)
        self.assertEqual(result.status, FetchStatus.SUCCESS, [str(outcome.error) for outcome in result.errors])
        self.assertEqual(result.feature_count, 1)
        method, query, headers = self.server.requests.get_nowait()
        self.assertEqual(method, "GET")
        self.assertEqual(query, {"query": [self.reference["cadastral_number"]], "layersId": ["36048", "36049"]})
        self.assertEqual(headers["Referer"], "https://nspd.gov.ru/map?thematic=PKK")
        self.assertEqual(headers["User-Agent"], plugin_module("constants").DEFAULT_HEADERS["User-Agent"])
        source = layers.load_geojson_layer(result.collections[""])
        target = crs_from_proj(self.reference["target_proj"])
        working = msk.make_gost2001_crs(target)
        actual = layers.process_result_layer(source, working, target, True)
        feature = next(actual.getFeatures())
        expected = self.reference["expected_ring_easting_northing"]
        vertices = [(point.x(), point.y()) for point in feature.geometry().vertices()]
        self.assertEqual(len(vertices), 5)
        for x, y in vertices:
            self.assertLess(min(((x - ex) ** 2 + (y - ey) ** 2) ** 0.5 for ex, ey in expected), 1e-8)

    def test_user_agent_hook_does_not_change_unrelated_qgis_requests(self):
        network.prepare_network()
        request = QNetworkRequest(QUrl(self.base_url + "/search?query=unrelated"))
        blocking = QgsBlockingNetworkRequest()
        self.assertEqual(blocking.get(request, True), QgsBlockingNetworkRequest.NoError)
        _, _, headers = self.server.requests.get_nowait()
        self.assertIn("QGIS/", headers["User-Agent"])
        # Unload/reload removes and reinstalls only this plugin's callback.
        network.release_network()
        self.assertIsNone(network._preprocessor_id)
        network.prepare_network()
        self.assertIsNotNone(network._preprocessor_id)

    def test_post_over_qgis_network_preserves_polygon_holes_and_category(self):
        x, y = self.reference["nspd_geometry"]["coordinates"][0][0]
        rings = [[[x-100, y-100], [x+100, y-100], [x+100, y+100], [x-100, y+100], [x-100, y-100]],
                 [[x-80, y-80], [x-80, y-40], [x-40, y-40], [x-40, y-80], [x-80, y-80]]]
        task = tasks.FetchNspdDataTask("Local test", [rings], [{"name": "land", "id": 36368}])
        result = self.run_task(task)
        self.assertEqual(result.status, FetchStatus.SUCCESS, [str(outcome.error) for outcome in result.errors])
        method, body, headers = self.server.requests.get_nowait()
        self.assertEqual(method, "POST")
        sent_geometry = body["geom"]["features"][0]["geometry"]
        self.assertEqual(len(sent_geometry["coordinates"]), 2)
        sent = QgsJsonUtils.geometryFromGeoJson(json.dumps(sent_geometry))
        original = QgsJsonUtils.geometryFromGeoJson(json.dumps({"type": "Polygon", "coordinates": rings}))
        self.assertTrue(sent.contains(original))
        self.assertGreater(sent.area(), original.area())
        self.assertFalse(sent.contains(QgsPointXY(x-60, y-60)))
        self.assertEqual(result.feature_count, 1)
        self.assertEqual(body["categories"], [{"id": 36368}])
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_http_and_schema_failures_are_not_reported_as_missing_parcels(self):
        task = tasks.FetchNspdByCadastralTask("Local test", ["denied", "bad-schema"], [36048])
        result = self.run_task(task)
        self.assertEqual(result.status, FetchStatus.FAILED)
        self.assertEqual(result.not_found, [])
        self.assertEqual([outcome.error.kind for outcome in result.errors], ["http", "response"])
        self.assertEqual(result.errors[0].error.status_code, 403)

    def test_cancel_interrupts_http_and_publishes_previously_received_objects(self):
        task = tasks.FetchNspdByCadastralTask(
            "Local test", [self.reference["cadastral_number"], "slow", "must-not-be-requested"], [36048])
        published = []
        self.controller.layerReady.connect(lambda task, name, collection: published.append(
            (collection["features"], task.result.status, QThread.currentThread() == APP.thread())))
        self.assertTrue(self.controller.start(task))
        self.wait_until(self.server.received.is_set)
        started = time.monotonic()
        task.cancel()
        self.wait_until(lambda: bool(self.finished), timeout=2)
        self.assertLess(time.monotonic() - started, 2)
        result = self.finished[0]
        self.assertEqual(result.status, FetchStatus.CANCELED)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.not_found, [])
        self.assertEqual(result.feature_count, 1)
        self.assertEqual(published, [([self.feature], FetchStatus.CANCELED, True)])
        self.assertEqual(self.server.requests.qsize(), 2)

    def test_shutdown_interrupts_http_without_late_ui_callbacks(self):
        task = tasks.FetchNspdByCadastralTask("Local test", ["slow"], [36048])
        result = task.result
        terminated = []
        task.taskTerminated.connect(lambda: terminated.append(True))
        self.assertTrue(self.controller.start(task))
        self.wait_until(self.server.received.is_set)
        self.controller.shutdown()
        network.release_network()
        self.wait_until(lambda: bool(terminated), timeout=2)
        self.assertEqual(result.status, FetchStatus.CANCELED)
        self.assertEqual(self.finished, [])

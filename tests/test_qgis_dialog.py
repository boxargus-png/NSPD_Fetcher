import copy
import unittest
from unittest.mock import Mock, patch

from tests.qgis_support import APP
from tests.support import plugin_module
from tests.test_qgis_baseline import feature_collection
from tests.test_qgis_tasks import ScriptedClient, cadastral_task, tasks, FetchStatus
from tests.test_qgis_msk import crs_from_proj, reference_data
from tests.manual_coverage_boundary import make_mask
from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt

ui = plugin_module("NSPD_Fetcher_dialog")
api = plugin_module("api_client")
msk = plugin_module("msk_transform")


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.dialog = ui.NSPD_FetcherDialog()
        self.dialog.controller._manager = Mock()
        self.messages = []
        self.patches = [patch.object(ui.QMessageBox, name, side_effect=lambda *args: self.messages.append(args))
                        for name in ("information", "warning", "critical")]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.deleteLater()
        APP.processEvents()
        for patcher in self.patches:
            patcher.stop()
        QgsProject.instance().clear()

    def test_delay_change_cannot_enable_fetch_while_task_is_active(self):
        task = cadastral_task(ScriptedClient([[], []]))
        self.dialog.controller.start(task)
        self.dialog.spinDelayMin.setValue(6)
        self.assertFalse(self.dialog.btnFetch.isEnabled())
        with patch.object(self.dialog, "_prepare_fetch") as prepare:
            self.dialog.fetch_data()
            prepare.assert_not_called()
        task.run()
        task.taskCompleted.emit()
        APP.processEvents()
        self.assertTrue(self.dialog.btnFetch.isEnabled())

    def test_cancel_button_preserves_received_objects_and_restores_controls(self):
        task = cadastral_task(ScriptedClient([feature_collection()["features"], []]),
                              delay_min=1, delay_max=1)
        self.dialog.controller.start(task)
        self.assertFalse(self.dialog.tabWidgetQueryMode.isEnabled())
        self.assertFalse(self.dialog.btnCancelFetch.isHidden())
        with patch.object(tasks.time, "sleep", side_effect=lambda _: self.dialog.btnCancelFetch.click()):
            self.assertFalse(task.run())
        self.assertFalse(self.dialog.btnCancelFetch.isEnabled())
        task.taskTerminated.emit()
        APP.processEvents()
        self.assertEqual(task.result.status, FetchStatus.CANCELED)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 1)
        self.assertIn("неполная загрузка", next(iter(QgsProject.instance().mapLayers().values())).name())
        self.assertTrue(self.dialog.tabWidgetQueryMode.isEnabled())
        self.assertTrue(self.dialog.btnCancelFetch.isHidden())
        self.assertTrue(self.dialog.btnFetch.isEnabled())

    def test_unavailable_categories_are_explained_and_selection_restored_on_mode_change(self):
        items = self.dialog.treeWidget.findItems("Территориальные зоны", Qt.MatchExactly | Qt.MatchRecursive)
        item = items[0]
        item.setCheckState(0, Qt.Checked)
        self.assertIn("Территориальные зоны", self.dialog.get_checked_category_names())
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.assertTrue(item.isDisabled())
        self.assertTrue(item.toolTip(0))
        self.assertIn("Недоступно в этом режиме: 1", self.dialog.labelSelectedCategories.text())
        self.assertNotIn("Территориальные зоны", self.dialog.get_checked_category_names())
        self.dialog.tabWidgetQueryMode.setCurrentIndex(0)
        self.assertFalse(item.isDisabled())
        self.assertEqual(item.checkState(0), Qt.Checked)
        self.assertIn("Территориальные зоны", self.dialog.get_checked_category_names())

    def test_invalid_delay_stays_disabled_after_completion(self):
        task = cadastral_task(ScriptedClient([[], []]))
        self.dialog.controller.start(task)
        self.dialog.spinDelayMin.setValue(11)
        task.run()
        task.taskCompleted.emit()
        APP.processEvents()
        self.assertFalse(self.dialog.btnFetch.isEnabled())

    def test_unsupported_search_does_not_fall_back_to_unfiltered_request(self):
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.dialog.textEditCadastral.setText("test-number")
        with patch.object(self.dialog, "get_checked_category_names", return_value=["Территориальные зоны"]):
            self.dialog.fetch_data()
        self.dialog.controller._manager.addTask.assert_not_called()
        self.assertIn("не поддерживаются", self.messages[-1][2])

    def test_one_msk_selection_prepares_both_crs_for_each_query_mode(self):
        reference = reference_data()
        target = crs_from_proj(reference["target_proj"])
        selectors = self.dialog.widgetMskOptions.findChildren(QgsProjectionSelectionWidget)
        self.assertEqual(selectors, [self.dialog.crsMsk])
        self.dialog.chkEnableMsk.setChecked(True)
        self.dialog.crsMsk.setCrs(target)
        self.dialog.textEditCadastral.setText(reference["cadastral_number"])
        for mode, rounding in ((0, True), (1, False)):
            with self.subTest(mode=mode):
                self.dialog.tabWidgetQueryMode.setCurrentIndex(mode)
                self.dialog.chkRoundMm.setChecked(rounding)
                with patch.object(self.dialog, "get_checked_category_names", return_value=["Земельные участки из ЕГРН"]), \
                     patch.object(self.dialog, "_prepare_coverage", return_value=([[[[0, 0], [1, 0], [0, 0]]]], None)), \
                     patch.object(self.dialog.controller, "start") as start:
                    self.dialog.fetch_data()
                start.assert_called_once()
                task = start.call_args[0][0]
                self.assertEqual(task.crs_assign.toWkt(), target.toWkt())
                self.assertEqual(task.crs_reproject.toProj(), reference["legacy_working_proj"])
                self.assertEqual(task.round_mm, rounding)
                # Changes in the form cannot change an already prepared task.
                self.dialog.crsMsk.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
                self.assertEqual(task.crs_assign.toWkt(), target.toWkt())
                self.dialog.crsMsk.setCrs(target)
        self.assertEqual(self.messages, [])

    def test_missing_or_unsupported_msk_stops_before_creating_task(self):
        self.dialog.chkEnableMsk.setChecked(True)
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.dialog.textEditCadastral.setText("52:18:0050232:698")
        for crs in (QgsCoordinateReferenceSystem(), QgsCoordinateReferenceSystem("EPSG:3857")):
            with self.subTest(crs=crs.authid()):
                self.dialog.crsMsk.setCrs(crs)
                with patch.object(self.dialog, "get_checked_category_names", return_value=["Земельные участки из ЕГРН"]):
                    self.dialog.fetch_data()
                self.dialog.controller._manager.addTask.assert_not_called()
                self.assertIn("МСК", self.messages[-1][2])
                self.assertFalse(self.dialog.controller.busy)
                self.assertTrue(self.dialog.btnFetch.isEnabled())

    def test_disabled_msk_does_not_validate_or_transform(self):
        self.dialog.chkEnableMsk.setChecked(False)
        self.dialog.crsMsk.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.dialog.textEditCadastral.setText("52:18:0050232:698")
        with patch.object(self.dialog, "get_checked_category_names", return_value=["Земельные участки из ЕГРН"]), \
             patch.object(self.dialog.controller, "start") as start:
            self.dialog.fetch_data()
        start.assert_called_once()
        task = start.call_args[0][0]
        self.assertIsNone(task.crs_reproject)
        self.assertIsNone(task.crs_assign)
        self.assertFalse(task.round_mm)

    def test_partial_search_adds_success_and_reports_failure_separately(self):
        features = feature_collection()["features"]
        task = cadastral_task(ScriptedClient([features, api.NspdApiError("timeout", "network")]))
        self.dialog.controller.start(task)
        task.run()
        task.taskCompleted.emit()
        APP.processEvents()
        self.assertEqual(len(QgsProject.instance().mapLayers()), 1)
        text = self.messages[-1][2]
        self.assertIn("частично", text)
        self.assertIn("second: timeout", text)
        self.assertNotIn("Не найдены:", text)

    def test_failure_and_cancellation_preserve_partial_msk_results_in_both_modes(self):
        reference = reference_data()
        target = crs_from_proj(reference["target_proj"])
        feature = {"type": "Feature", "id": 291607188,
                   "properties": {"category": 36368, "cad_num": reference["cadastral_number"]},
                   "geometry": reference["nspd_geometry"]}
        # Repeated responses must still use conservative deduplication.
        features = [feature, copy.deepcopy(feature)]
        before = copy.deepcopy(features)
        for coverage_mode in (False, True):
            for canceled in (False, True):
                with self.subTest(coverage_mode=coverage_mode, canceled=canceled):
                    QgsProject.instance().clear()
                    self.messages.clear()
                    client = ScriptedClient([features, api.NspdApiError("timeout", "network")])
                    options = dict(crs_reproject=msk.make_gost2001_crs(target),
                                   crs_assign=target, round_mm=True,
                                   delay_min=int(canceled), delay_max=int(canceled))
                    if coverage_mode:
                        task = ui.FetchNspdDataTask(
                            "Test", [reference["nspd_geometry"]["coordinates"]] * 2, [{"name": "land", "id": 36368}],
                            client_factory=lambda: client, **options)
                    else:
                        task = cadastral_task(client, **options)
                    self.dialog.controller.start(task)
                    with patch.object(tasks.time, "sleep", side_effect=lambda _: task.cancel()):
                        succeeded = task.run()
                    (task.taskCompleted if succeeded else task.taskTerminated).emit()
                    APP.processEvents()
                    result_layers = list(QgsProject.instance().mapLayers().values())
                    self.assertEqual(len(result_layers), 1)
                    layer = result_layers[0]
                    self.assertTrue(layer.customProperty("nspd_fetcher/incomplete"))
                    self.assertIn("неполная загрузка", layer.name())
                    self.assertEqual(layer.featureCount(), 1)
                    actual = next(layer.getFeatures())
                    self.assertEqual(actual["nspd_id"], 291607188)
                    self.assertEqual(actual["cad_num"], reference["cadastral_number"])
                    vertices = list(actual.geometry().vertices())
                    self.assertEqual(len(vertices), 5)
                    for point in vertices:
                        self.assertLess(min(((point.x() - x) ** 2 + (point.y() - y) ** 2) ** 0.5
                                            for x, y in reference["expected_ring_easting_northing"]), 1e-8)
                    self.assertEqual(features, before)
                    self.assertEqual(task.result.not_found, [])
                    self.assertEqual(task.result.status, FetchStatus.CANCELED if canceled else FetchStatus.PARTIAL)
                    self.assertEqual(len(client.calls), 1 if canceled else 2)
                    self.assertEqual(self.dialog._incomplete_layers_added, 1)
                    self.assertEqual(len(self.messages), 1)
                    message = self.messages[0][2]
                    self.assertIn("неполная загрузка", message)
                    self.assertNotIn("не добавлены", message)
                    self.assertNotIn("Не найдены:", message)
                    if canceled:
                        self.assertIn("отменён", message)
                        self.assertEqual(task.result.errors, [])
                        self.assertNotIn("Ошибки запросов:", message)
                    else:
                        self.assertIn("timeout", message)

    def test_completed_category_stays_complete_when_next_category_fails(self):
        features = feature_collection()["features"]
        client = ScriptedClient([features, features, features, api.NspdApiError("HTTP 503", "http", 503)])
        task = ui.FetchNspdDataTask(
            "Test", [features[0]["geometry"]["coordinates"]] * 2,
            [{"name": "complete", "id": 1}, {"name": "partial", "id": 2}],
            client_factory=lambda: client)
        self.dialog.controller.start(task)
        # Simulate an earlier category being published while the worker runs.
        with patch.object(task, "_pause", side_effect=lambda: (APP.processEvents() or True)):
            self.assertTrue(task.run())
        self.assertEqual(len(QgsProject.instance().mapLayers()), 1)
        task.taskCompleted.emit()
        APP.processEvents()
        result_layers = {layer.name().split(" - ")[0]: layer
                         for layer in QgsProject.instance().mapLayers().values()}
        self.assertEqual(set(result_layers), {"complete", "partial"})
        self.assertFalse(result_layers["complete"].customProperty("nspd_fetcher/incomplete"))
        self.assertTrue(result_layers["partial"].customProperty("nspd_fetcher/incomplete"))
        self.assertEqual([layer.featureCount() for layer in result_layers.values()], [1, 1])
        self.assertEqual(self.dialog._layers_added, 2)
        self.assertEqual(self.dialog._incomplete_layers_added, 1)

    def test_failure_or_cancellation_without_received_objects_creates_no_layer(self):
        for canceled in (False, True):
            with self.subTest(canceled=canceled):
                client = ScriptedClient([api.NspdApiError("timeout", "network")] * 2)
                task = cadastral_task(client)
                if canceled:
                    task.cancel()
                self.dialog.controller.start(task)
                self.assertFalse(task.run())
                task.taskTerminated.emit()
                APP.processEvents()
                self.assertEqual(len(QgsProject.instance().mapLayers()), 0)
                self.assertEqual(self.dialog._incomplete_layers_added, 0)
                self.assertNotIn("Неполные результаты сохранены", self.messages[-1][2])

    def test_success_after_cancel_resets_partial_result_counters(self):
        features = feature_collection()["features"]
        canceled = cadastral_task(ScriptedClient([features, []]), delay_min=1, delay_max=1)
        self.dialog.controller.start(canceled)
        with patch.object(tasks.time, "sleep", side_effect=lambda _: canceled.cancel()):
            canceled.run()
        canceled.taskTerminated.emit()
        APP.processEvents()
        self.assertEqual(self.dialog._incomplete_layers_added, 1)
        completed = cadastral_task(ScriptedClient([features, []]))
        self.dialog.controller.start(completed)
        completed.run()
        completed.taskCompleted.emit()
        APP.processEvents()
        self.assertEqual(len(QgsProject.instance().mapLayers()), 2)
        self.assertEqual(self.dialog._layers_added, 1)
        self.assertEqual(self.dialog._incomplete_layers_added, 0)
        self.assertNotIn("неполная загрузка", self.messages[-1][2])

    def test_duplicate_responses_are_safe_in_both_query_modes(self):
        first = feature_collection()["features"][0]
        other_id = dict(copy.deepcopy(first), id=102)
        other_attributes = copy.deepcopy(first)
        other_attributes["properties"]["name"] = "Изменённые атрибуты"
        other_category = copy.deepcopy(first)
        other_category["properties"]["category"] = 36369
        responses = [[first, other_id], [copy.deepcopy(first), other_attributes, other_category]]
        before = copy.deepcopy(responses)
        expected = {(f["id"], f["properties"]["category"], f["properties"]["name"])
                    for f in (first, other_id, other_attributes, other_category)}
        for coverage_mode in (False, True):
            with self.subTest(coverage_mode=coverage_mode):
                QgsProject.instance().clear()
                client = ScriptedClient(responses)
                if coverage_mode:
                    task = ui.FetchNspdDataTask(
                        "Test", [first["geometry"]["coordinates"]] * 2, [{"name": "category", "id": 1}],
                        client_factory=lambda: client)
                else:
                    task = cadastral_task(client)
                self.assertTrue(self.dialog.controller.start(task))
                self.assertTrue(task.run())
                task.taskCompleted.emit()
                APP.processEvents()
                result_layers = list(QgsProject.instance().mapLayers().values())
                self.assertEqual(len(result_layers), 1)
                result = result_layers[0]
                self.assertEqual(result.featureCount(), 4)
                self.assertEqual({(f["nspd_id"], f["category"], f["name"])
                                  for f in result.getFeatures()}, expected)
                self.assertEqual(result.crs().authid(), "EPSG:3857")
                self.assertEqual(len(client.calls), 2)
                self.assertEqual(task.result.feature_count, 5)
                self.assertEqual(task.result.not_found, [])
                self.assertEqual(responses, before)

    def test_layer_failure_is_not_reported_as_success(self):
        task = ui.FetchNspdDataTask("Test", [feature_collection()["features"][0]["geometry"]["coordinates"]],
                                   [{"name": "a", "id": 1}],
                                   client_factory=lambda: ScriptedClient([feature_collection()["features"]]))
        self.dialog.controller.start(task)
        with patch.object(ui, "add_result_layer", return_value=(False, "processing failure")):
            task.run()
            task.taskCompleted.emit()
            APP.processEvents()
        self.assertEqual(self.messages[-1][1], "Ошибка")
        self.assertIn("processing failure", self.messages[-1][2])

    def test_captured_crs_survives_project_crs_change(self):
        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.dialog.iface = Mock()
        self.dialog.iface.mapCanvas.return_value = canvas
        self.dialog.activate_polygon_tool()
        self.dialog.on_polygon_captured(QgsGeometry.fromWkt("POLYGON ((0 0, 10 0, 10 10, 0 0))"))
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        coordinates, captured = self.dialog._prepare_coverage()
        self.assertEqual(coordinates[0][0][1], [10.0, 0.0])
        self.assertEqual(captured.crs.authid(), "EPSG:3857")
        self.dialog.shutdown()
        canvas.deleteLater()

    def test_manual_and_selected_vector_msk_coverage_reach_worker_unchanged(self):
        reference = reference_data()
        target = crs_from_proj(reference["target_proj"])
        mask = make_mask(reference, 0.25)
        feature = {"type": "Feature", "id": 291607188, "properties": {"category": 36368},
                   "geometry": reference["nspd_geometry"]}
        layer = QgsVectorLayer("Polygon", "coverage", "memory")
        layer.setCrs(target)
        for geometry in (mask, QgsGeometry.fromWkt("POLYGON ((0 0, 1 0, 1 1, 0 0))")):
            item = QgsFeature()
            item.setGeometry(geometry)
            layer.dataProvider().addFeatures([item])
        layer.selectByIds([next(layer.getFeatures()).id()])
        QgsProject.instance().addMapLayer(layer)
        self.dialog.tabWidgetQueryMode.setCurrentIndex(0)
        self.dialog.chkEnableMsk.setChecked(True)
        self.dialog.crsMsk.setCrs(target)
        for manual in (True, False):
            with self.subTest(manual=manual):
                self.dialog.rbManualCoverage.setChecked(manual)
                self.dialog.coverage_geometry = QgsGeometry(mask)
                self.dialog.coverage_crs = QgsCoordinateReferenceSystem(target)
                self.dialog.cbLayer.setLayer(layer)
                with patch.object(self.dialog, "get_checked_category_names", return_value=["Земельные участки из ЕГРН"]), \
                     patch.object(self.dialog.controller, "start") as start:
                    self.dialog.fetch_data()
                start.assert_called_once()
                task = start.call_args.args[0]
                self.assertEqual(len(task.coverage_input.original_geometries), 1)
                self.assertEqual(bytes(task.coverage_input.original_geometries[0].asWkb()), bytes(mask.asWkb()))
                self.assertEqual(task.coverage_input.crs.toWkt(), target.toWkt())
                # Later edits to the capture, project or vector CRS cannot
                # change the snapshot which the worker will use.
                self.dialog.coverage_geometry.translate(1000, 1000)
                self.dialog.coverage_crs = QgsCoordinateReferenceSystem("EPSG:3857")
                QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
                layer.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
                task.client_factory = lambda: ScriptedClient([[feature]])
                self.assertTrue(task.run())
                self.assertEqual(task.coverage_counts, {"inside": 1, "outside": 0, "unchecked": 0})
                self.assertEqual(task.result.feature_count, 1)
                layer.setCrs(target)
        self.assertEqual(self.messages, [])

    def test_canceling_new_capture_keeps_previous_geometry_crs(self):
        canvas = QgsMapCanvas()
        self.dialog.iface = Mock()
        self.dialog.iface.mapCanvas.return_value = canvas
        self.dialog.coverage_geometry = QgsGeometry.fromWkt("POLYGON ((0 0, 10 0, 10 10, 0 0))")
        self.dialog.coverage_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.dialog.activate_polygon_tool()
        canvas.unsetMapTool(self.dialog.capture_tool)
        self.assertEqual(self.dialog._prepare_coverage()[0][0][0][1], [10.0, 0.0])
        self.dialog.shutdown()
        canvas.deleteLater()

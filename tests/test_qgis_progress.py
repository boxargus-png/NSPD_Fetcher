import unittest
from unittest.mock import Mock, patch

from tests.qgis_support import APP
from tests.support import plugin_module
from tests.test_qgis_tasks import COVERAGE, FEATURE, ScriptedClient, cadastral_task, tasks, api, FetchStatus
from tests.test_qgis_baseline import feature_collection
from qgis.core import QgsProject

ui = plugin_module("NSPD_Fetcher_dialog")


class ProgressTests(unittest.TestCase):
    def test_coverage_reports_each_request_and_filter_counts_without_mutating_old_snapshots(self):
        inside = {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [5, 5]}}
        outside = dict(inside, geometry={"type": "Point", "coordinates": [100, 100]})
        client = ScriptedClient([[inside, outside], [FEATURE], [], []])
        task = tasks.FetchNspdDataTask("Coverage", [COVERAGE, COVERAGE],
                                       [{"name": "land", "id": 1}, {"name": "buildings", "id": 2}],
                                       client_factory=lambda: client)
        snapshots = []
        task.progressUpdated.connect(snapshots.append)
        self.assertTrue(task.run())
        requests = [p for p in snapshots if p.phase == "request"]
        self.assertEqual([p.current for p in requests], [1, 2, 3, 4])
        self.assertIn("buildings", requests[2].detail)
        self.assertIn("Полигон охвата 1 из 2", requests[2].detail)
        self.assertEqual([p.completed for p in snapshots if p.phase == "response"], [1, 2, 3, 4])
        self.assertTrue(all(p.total == 4 for p in snapshots))
        self.assertEqual(snapshots[0].received, 0)
        filtering = next(p for p in snapshots if p.phase == "filtering")
        self.assertEqual((filtering.received, filtering.retained), (2, 0))
        final = task.progress_snapshot
        self.assertEqual((final.completed, final.received, final.retained, final.excluded, final.unchecked, final.empty),
                         (4, 3, 2, 1, 1, 2))

    def test_cadastral_progress_counts_unique_numbers_empty_responses_and_errors(self):
        task = cadastral_task(ScriptedClient([[FEATURE], [], api.NspdApiError("HTTP 503", "http")]),
                              ["found", "found", "missing", "failed"])
        snapshots = []
        task.progressUpdated.connect(snapshots.append)
        task.run()
        requests = [p for p in snapshots if p.phase == "request"]
        self.assertEqual([p.detail for p in requests], ["Кадастровый номер: found", "Кадастровый номер: missing", "Кадастровый номер: failed"])
        final = task.progress_snapshot
        self.assertEqual((final.total, final.completed, final.received, final.errors, final.empty), (3, 3, 1, 1, 1))
        self.assertEqual(task.result.status, FetchStatus.PARTIAL)

    def test_coverage_error_stops_progress_at_last_attempted_request(self):
        client = ScriptedClient([[FEATURE], api.NspdApiError("timeout", "network")])
        task = tasks.FetchNspdDataTask("Coverage", [COVERAGE] * 4, [{"name": "land", "id": 1}], client_factory=lambda: client)
        task.run()
        final = task.progress_snapshot
        self.assertEqual((final.completed, final.total, final.errors, final.received), (2, 4, 1, 1))
        self.assertEqual(task.progress(), 50)

    def test_canceled_inflight_request_is_not_counted_as_a_completed_response(self):
        task = cadastral_task(ScriptedClient([]), ["first", "slow", "never"])
        client = ScriptedClient([])

        def search(number, category_ids):
            if number == "first":
                return [FEATURE]
            task.cancel()
            raise api.NspdApiError("canceled", "canceled")

        client.search = search
        task.client_factory = lambda: client
        self.assertFalse(task.run())
        final = task.progress_snapshot
        self.assertEqual((final.current, final.completed, final.total, final.errors, final.received), (2, 1, 3, 0, 1))

    def test_delay_countdown_has_whole_seconds_and_is_cancellable(self):
        task = cadastral_task(ScriptedClient([[FEATURE], []]), delay_min=3, delay_max=3)
        snapshots = []
        task.progressUpdated.connect(snapshots.append)
        clock = [0.0]

        def sleep(duration):
            clock[0] += duration
            if clock[0] >= 2.05:
                task.cancel()

        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock[0]), patch.object(tasks.time, "sleep", side_effect=sleep):
            self.assertFalse(task.run())
        waiting = [p for p in snapshots if p.phase == "waiting"]
        self.assertEqual([p.wait_seconds for p in waiting], [3, 2, 1])
        self.assertTrue(all(p.completed == 1 and p.received == 1 for p in waiting))
        self.assertEqual(task.progress_snapshot.wait_seconds, 0)


class ProgressDialogTests(unittest.TestCase):
    def setUp(self):
        self.dialog = ui.NSPD_FetcherDialog()
        self.dialog.controller._manager = Mock()
        self.patches = [patch.object(ui.QMessageBox, name) for name in ("information", "warning", "critical")]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.deleteLater()
        APP.processEvents()
        for patcher in self.patches:
            patcher.stop()
        QgsProject.instance().clear()

    def test_late_progress_does_not_overwrite_partial_result_and_new_task_resets_counts(self):
        task = tasks.FetchNspdDataTask(
            "Coverage", [feature_collection()["features"][0]["geometry"]["coordinates"]] * 4,
            [{"name": "land", "id": 1}], client_factory=lambda: ScriptedClient(
                [feature_collection()["features"], api.NspdApiError("timeout", "network")]))
        self.dialog.controller.start(task)
        self.assertEqual(self.dialog.progressRequests.maximum(), 4)
        task.run()
        # Completion overtakes all queued progress signals.
        task.taskCompleted.emit()
        final_text = self.dialog.labelTaskStatus.text()
        APP.processEvents()
        self.assertEqual(self.dialog.labelTaskStatus.text(), final_text)
        self.assertIn("частично", final_text)
        self.assertEqual(self.dialog.progressRequests.value(), 2)
        self.assertIn("Ошибок: 1", self.dialog.labelTaskCounts.text())
        self.assertIn("Слоёв добавлено: 1", self.dialog.labelTaskCounts.text())
        self.assertFalse(self.dialog._elapsed_timer.isActive())
        self.assertFalse(self.dialog.groupTaskProgress.isHidden())
        next_task = cadastral_task(ScriptedClient([[], []]))
        self.dialog.controller.start(next_task)
        self.assertEqual(self.dialog.progressRequests.value(), 0)
        self.assertIn("Получено записей: 0", self.dialog.labelTaskCounts.text())
        self.dialog.controller._relay_progress(task, task.progress_snapshot)
        self.assertEqual(self.dialog.progressRequests.value(), 0)

    def test_cancel_message_survives_queued_updates_and_final_percent_is_not_forced_to_100(self):
        task = cadastral_task(ScriptedClient([feature_collection()["features"], []]), delay_min=1, delay_max=1)
        self.dialog.controller.start(task)
        with patch.object(tasks.time, "sleep", side_effect=lambda _: self.dialog.cancel_fetch()):
            self.assertFalse(task.run())
        APP.processEvents()
        self.assertIn("отмен", self.dialog.labelTaskStatus.text().lower())
        task.taskTerminated.emit()
        APP.processEvents()
        self.assertEqual(self.dialog.progressRequests.value(), 1)
        self.assertEqual(self.dialog.progressRequests.maximum(), 2)
        self.assertIn("отменена", self.dialog.labelTaskStatus.text())
        self.assertEqual(len(QgsProject.instance().mapLayers()), 1)

    def test_elapsed_time_updates_during_request_and_stops_on_shutdown(self):
        task = cadastral_task(ScriptedClient([[], []]))
        self.dialog.controller.start(task)
        with patch.object(ui.time, "monotonic", return_value=self.dialog._progress_started_at + 65):
            self.dialog._update_elapsed()
        self.assertEqual(self.dialog.labelTaskElapsed.text(), "Прошло: 01:05")
        self.dialog.shutdown()
        self.assertFalse(self.dialog._elapsed_timer.isActive())

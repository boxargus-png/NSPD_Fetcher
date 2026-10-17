import unittest
from unittest.mock import Mock, patch

from tests.qgis_support import APP
from tests.support import plugin_module
from qgis.PyQt.QtCore import QThread
from qgis.PyQt.QtTest import QTest

tasks = plugin_module("api_task")
api = plugin_module("api_client")
FetchStatus = plugin_module("fetch_result").FetchStatus
Controller = plugin_module("task_controller").FetchController
FEATURE = {"type": "Feature", "geometry": None, "properties": {"id": "test"}}
COVERAGE = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]


class ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.closed = False
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def request(self, *args):
        self.calls.append(args)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    search = intersects = request


def cadastral_task(client, numbers=None, **kwargs):
    return tasks.FetchNspdByCadastralTask(
        "Test", numbers or ["first", "second"], [36048], client_factory=lambda: client, **kwargs)


class TaskTests(unittest.TestCase):
    def test_all_network_failures_are_failed_not_not_found(self):
        client = ScriptedClient([api.NspdApiError("timeout", "network")] * 2)
        task = cadastral_task(client)
        self.assertFalse(task.run())
        self.assertEqual(task.result.status, FetchStatus.FAILED)
        self.assertEqual(task.result.not_found, [])
        self.assertEqual(len(task.result.errors), 2)
        self.assertTrue(client.closed)

    def test_mixed_cadastral_batch_keeps_features_and_distinct_failures(self):
        client = ScriptedClient([[FEATURE], [], api.NspdApiError("HTTP 503", "http", 503)])
        task = cadastral_task(client, ["found", "missing", "failed"])
        self.assertTrue(task.run())
        self.assertEqual(task.result.status, FetchStatus.PARTIAL)
        self.assertEqual(task.result.not_found, ["missing"])
        self.assertEqual(task.result.feature_count, 1)

    def test_worker_keeps_partial_category_for_publication_on_completion(self):
        client = ScriptedClient([[FEATURE], api.NspdApiError("timeout", "network")])
        task = tasks.FetchNspdDataTask("Test", [COVERAGE, COVERAGE], [{"name": "category", "id": 1}],
                                       client_factory=lambda: client)
        published = []
        task.layerReady.connect(lambda *args: published.append(args))
        self.assertTrue(task.run())
        self.assertEqual(task.result.status, FetchStatus.PARTIAL)
        self.assertEqual(task.result.feature_count, 1)
        self.assertEqual(task.result.completed_categories, [])
        self.assertEqual(published, [])

    def test_coverage_publishes_each_completed_category_once(self):
        client = ScriptedClient([[FEATURE], []])
        task = tasks.FetchNspdDataTask("Test", [COVERAGE],
                                       [{"name": "a", "id": 1}, {"name": "b", "id": 2}],
                                       client_factory=lambda: client)
        published = []
        task.layerReady.connect(lambda name, _: published.append(name))
        self.assertTrue(task.run())
        self.assertEqual(published, ["a"])
        self.assertEqual(task.result.completed_categories, ["a", "b"])

    def test_cancel_during_delay_preserves_received_data(self):
        client = ScriptedClient([[FEATURE], [FEATURE]])
        task = cadastral_task(client, delay_min=1, delay_max=1)
        with patch.object(tasks.time, "sleep", side_effect=lambda _: task.cancel()):
            self.assertFalse(task.run())
        self.assertEqual(task.result.status, FetchStatus.CANCELED)
        self.assertEqual(task.result.feature_count, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.closed)

    def test_cancel_before_start_makes_no_requests(self):
        factory = Mock()
        task = tasks.FetchNspdByCadastralTask("Test", ["one"], [1], client_factory=factory)
        task.cancel()
        self.assertFalse(task.run())
        factory.assert_not_called()
        self.assertEqual(task.result.status, FetchStatus.CANCELED)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.controller = Controller(task_manager=self.manager)

    def tearDown(self):
        self.controller.shutdown()
        APP.processEvents()

    def test_active_task_blocks_reentry_and_stale_completion(self):
        first = cadastral_task(ScriptedClient([[], []]))
        second = cadastral_task(ScriptedClient([[], []]))
        finished = []
        self.controller.taskFinished.connect(finished.append)
        self.assertTrue(self.controller.start(first))
        self.assertFalse(self.controller.start(second))
        first.run()
        first.taskCompleted.emit()
        APP.processEvents()
        self.assertEqual(finished, [first])
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.controller.start(second))
        self.controller._finish(first)
        self.assertIs(self.controller.active_task, second)
        self.assertEqual(finished, [first])

    def test_shutdown_cancels_and_suppresses_late_callbacks(self):
        task = cadastral_task(ScriptedClient([[], []]))
        finished = []
        progress = []
        self.controller.taskFinished.connect(finished.append)
        self.controller.progressChanged.connect(lambda task, snapshot: progress.append(snapshot))
        self.controller.start(task)
        self.controller.shutdown()
        task.progressUpdated.emit(task.progress_snapshot)
        task.taskCompleted.emit()
        APP.processEvents()
        self.assertTrue(task.isCanceled())
        self.assertEqual(finished, [])
        self.assertEqual(len(progress), 1)  # Only the initial snapshot before shutdown.

    def test_completion_delivers_completed_and_partial_categories_exactly_once(self):
        client = ScriptedClient([[FEATURE], [FEATURE], [FEATURE],
                                 api.NspdApiError("timeout", "network")])
        task = tasks.FetchNspdDataTask(
            "Test", [COVERAGE, COVERAGE], [{"name": "complete", "id": 1}, {"name": "partial", "id": 2}],
            client_factory=lambda: client)
        delivered = []
        events = []
        self.controller.layerReady.connect(lambda task, name, data: delivered.append((name, data)))
        self.controller.layerReady.connect(lambda *args: events.append("layer"))
        self.controller.taskFinished.connect(lambda task: events.append("finished"))
        self.controller.start(task)
        self.assertTrue(task.run())
        # Complete before the queued layerReady signal is processed.
        task.taskCompleted.emit()
        APP.processEvents()
        # A repeated or stale completion must not create another result layer.
        self.controller._finish(task)
        self.assertEqual([name for name, _ in delivered], ["complete", "partial"])
        self.assertEqual([len(data["features"]) for _, data in delivered], [2, 1])
        self.assertEqual(events, ["layer", "layer", "finished"])
        self.assertEqual(task.result.completed_categories, ["complete"])

    def test_canceled_cadastral_search_delivers_received_data_before_finishing(self):
        task = cadastral_task(ScriptedClient([[FEATURE], []]), delay_min=1, delay_max=1)
        events = []
        self.controller.layerReady.connect(
            lambda task, name, data: events.append((name, data["features"], task.result.status)))
        self.controller.taskFinished.connect(lambda task: events.append("finished"))
        self.controller.start(task)
        with patch.object(tasks.time, "sleep", side_effect=lambda _: task.cancel()):
            self.assertFalse(task.run())
        task.taskTerminated.emit()
        APP.processEvents()
        self.assertEqual(events, [("", [FEATURE], FetchStatus.CANCELED), "finished"])
        self.assertFalse(self.controller.busy)

    def test_real_background_task_delivers_layers_on_gui_thread(self):
        controller = Controller()
        client = ScriptedClient([[FEATURE]])
        task = tasks.FetchNspdDataTask("Test", [COVERAGE], [{"name": "a", "id": 1}],
                                       client_factory=lambda: client)
        delivered = []
        finished = []
        progress = []
        controller.layerReady.connect(lambda *args: delivered.append(QThread.currentThread() == APP.thread()))
        controller.progressChanged.connect(lambda task, snapshot: progress.append((QThread.currentThread() == APP.thread(), snapshot)))
        controller.taskFinished.connect(lambda task: finished.append(task.result.status))
        try:
            controller.start(task)
            for _ in range(300):
                if finished:
                    break
                QTest.qWait(10)
            self.assertEqual(finished, [FetchStatus.SUCCESS])
            self.assertEqual(delivered, [True])
            self.assertTrue(progress)
            self.assertTrue(all(on_gui for on_gui, _ in progress))
            self.assertEqual(progress[-1][1].phase, "publishing")
            self.assertEqual(progress[-1][1].received, 1)
            self.assertFalse(controller.busy)
        finally:
            controller.shutdown()

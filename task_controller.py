"""Own the active task and deliver its signals on the GUI thread."""
from functools import partial
from dataclasses import replace

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal

from .fetch_result import FetchStatus


class FetchController(QObject):
    busyChanged = pyqtSignal(bool)
    layerReady = pyqtSignal(object, str, dict)
    taskFinished = pyqtSignal(object)
    progressChanged = pyqtSignal(object, object)

    def __init__(self, parent=None, task_manager=None):
        super().__init__(parent)
        self._manager = task_manager if task_manager is not None else QgsApplication.taskManager()
        self.active_task = None
        self._connections = []
        self._published = set()
        self._closed = False
        self._finishing = False

    @property
    def busy(self):
        return self.active_task is not None

    def _connect(self, signal, callback, connection=Qt.AutoConnection):
        signal.connect(callback, connection)
        self._connections.append((signal, callback))

    def _disconnect(self):
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass  # The sender may already have been destroyed by QGIS.
        self._connections.clear()

    def start(self, task):
        if self.busy or self._closed:
            return False
        self.active_task = task
        self._published.clear()
        self._finishing = False
        # QGIS emits completion on the GUI thread. Handle it before QGIS deletes
        # the task; only the custom worker signal needs an explicit queued call.
        self._connect(task.taskCompleted, partial(self._finish, task))
        self._connect(task.taskTerminated, partial(self._finish, task))
        self._connect(task.progressUpdated, partial(self._relay_progress, task), Qt.QueuedConnection)
        if hasattr(task, "layerReady"):
            self._connect(task.layerReady, partial(self._relay_layer, task), Qt.QueuedConnection)
        self.busyChanged.emit(True)
        self.progressChanged.emit(task, task.progress_snapshot)
        try:
            self._manager.addTask(task)
        except Exception:
            self._disconnect()
            self.active_task = None
            self.busyChanged.emit(False)
            raise
        return True

    def _relay_progress(self, task, snapshot):
        if self._closed or self._finishing or task is not self.active_task:
            return
        self.progressChanged.emit(task, snapshot)

    def _relay_layer(self, task, name, collection):
        if self._closed or task is not self.active_task or name in self._published:
            return
        self._published.add(name)
        self.layerReady.emit(task, name, collection)

    def _finish(self, task):
        if self._closed or self._finishing or task is not self.active_task:
            return
        self._finishing = True
        if task.isCanceled():
            task.result.status = FetchStatus.CANCELED
        # The worker has stopped. Synchronize the last snapshot before layer
        # creation, even if completion overtakes queued progress updates.
        self.progressChanged.emit(task, replace(task.progress_snapshot, phase="publishing", wait_seconds=0))
        # The worker has stopped: publish every received collection, including
        # unfinished coverage categories and canceled cadastral searches.
        # Already delivered categories are skipped by _relay_layer, regardless
        # of the order of queued layer/completion signals.
        for name, collection in task.result.collections.items():
            if collection["features"]:
                self._relay_layer(task, name, collection)
        self._disconnect()
        try:
            self.taskFinished.emit(task)
        finally:
            self.active_task = None
            self.busyChanged.emit(False)

    def shutdown(self):
        self._closed = True
        self._disconnect()
        if self.active_task is not None:
            self.active_task.cancel()
            self.active_task = None

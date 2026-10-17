# -*- coding: utf-8 -*-
"""Cancellable QGIS workers. HTTP details live in api_client."""
from random import SystemRandom
import time
import math
from dataclasses import replace

from qgis.core import QgsFeedback, QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .api_client import NspdApiError, NspdClient
from .coverage_query import CoverageQuery, CoverageSelection, FILTER_KINDS
from .fetch_result import FetchProgress, FetchResult, FetchStatus
from .logger import log_info, log_error, log_warning
from .qgis_network import prepare_network


class NspdTask(QgsTask):
    progressUpdated = pyqtSignal(object)

    def __init__(self, description, delay_min=0, delay_max=0,
                 crs_reproject=None, crs_assign=None, round_mm=False, client_factory=None):
        super().__init__(description, QgsTask.CanCancel)
        if not 0 <= delay_min <= delay_max:
            raise ValueError("Некорректный интервал задержки")
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.crs_reproject = crs_reproject
        self.crs_assign = crs_assign
        self.round_mm = round_mm
        self.feedback = QgsFeedback()
        self.client_factory = client_factory
        if client_factory is None:
            prepare_network()
        self.result = FetchResult()
        self.progress_snapshot = FetchProgress()

    def _report_progress(self, phase, **changes):
        counts = getattr(self, "coverage_counts", {})
        values = dict(phase=phase, received=self.result.received_count,
                      retained=self.result.feature_count, errors=len(self.result.errors),
                      empty=len(self.result.not_found), excluded=counts.get("outside", 0),
                      unchecked=counts.get("unchecked", 0), wait_seconds=0)
        values.update(changes)
        self.progress_snapshot = replace(self.progress_snapshot, **values)
        self.progressUpdated.emit(self.progress_snapshot)

    def _complete_request(self):
        completed = self.progress_snapshot.completed + 1
        self._report_progress("response", completed=completed)
        if self.progress_snapshot.total:
            self.setProgress(100 * completed / self.progress_snapshot.total)

    def cancel(self):
        super().cancel()
        self.feedback.cancel()

    def _pause(self):
        deadline = time.monotonic() + SystemRandom().uniform(self.delay_min, self.delay_max)
        last_second = None
        while not self.isCanceled():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            seconds = math.ceil(remaining)
            if seconds != last_second:
                self._report_progress("waiting", wait_seconds=seconds)
                last_second = seconds
            time.sleep(min(0.1, remaining))
        return False

    def _record_error(self, key, category, error):
        self.result.record(key, category, error=error)
        log_error("{}: {}\n{}".format(key, error, error.response_text))

    def run(self):
        self.result.status = FetchStatus.RUNNING
        self._report_progress("preparing")
        log_info("Начало задачи «{}», задержка {}–{} сек.".format(
            self.description(), self.delay_min, self.delay_max))
        try:
            if not self.isCanceled():
                # The real client shares this task's cancellation feedback.
                client = (self.client_factory() if self.client_factory is not None
                          else NspdClient(feedback=self.feedback))
                with client:
                    self._fetch(client)
        except Exception as exc:
            if not self.isCanceled():
                error = NspdApiError("Непредвиденная ошибка: {}".format(exc), "internal")
                self._record_error(self.description(), "", error)
                log_error("Ошибка выполнения задачи", exc_info=exc)
        self.result.finish(canceled=self.isCanceled())
        self._report_progress("ready")
        log_info("Задача «{}»: {}, получено объектов: {}, после отбора: {}".format(
            self.description(), self.result.status.value,
            self.result.received_count, self.result.feature_count))
        return self.result.status not in (FetchStatus.FAILED, FetchStatus.CANCELED)


class FetchNspdDataTask(NspdTask):
    """Query each category/coverage polygon, publishing completed categories."""
    layerReady = pyqtSignal(str, dict)

    def __init__(self, description, primitive_coords_list, categories_info,
                 delay_min=0, delay_max=0, crs_reproject=None, crs_assign=None,
                 round_mm=False, client_factory=None, coverage_input=None):
        super().__init__(description, delay_min, delay_max, crs_reproject,
                         crs_assign, round_mm, client_factory)
        self.primitive_coords_list = primitive_coords_list
        self.categories_info = categories_info
        self.coverage_input = coverage_input
        self.coverage_counts = dict.fromkeys(FILTER_KINDS, 0)
        self.progress_snapshot = FetchProgress(total=len(primitive_coords_list) * len(categories_info))

    def _fetch(self, client):
        # Build and use geometries in the worker, not as live GUI layer objects.
        queries = []
        for coordinates in self.primitive_coords_list:
            if self.isCanceled():
                return
            queries.append(CoverageQuery(coordinates))
        if self.coverage_input is None:
            selection = CoverageSelection([query.original for query in queries])
        else:
            selection = CoverageSelection(
                self.coverage_input.original_geometries, self.coverage_input.crs,
                self.coverage_input.transform_context)
        total = len(queries) * len(self.categories_info)
        completed = 0
        for category_index, category in enumerate(self.categories_info, 1):
            name = category["name"]
            for index, query in enumerate(queries):
                if self.isCanceled():
                    return
                key = "{}, полигон {}".format(name, index + 1)
                self._report_progress(
                    "request", current=completed + 1,
                    detail="{} · категория {} из {}\nПолигон охвата {} из {}".format(
                        name, category_index, len(self.categories_info), index + 1, len(queries)))
                try:
                    features = client.intersects(query.coordinates, category["id"])
                except NspdApiError as error:
                    if self.isCanceled():
                        return
                    self._record_error(key, name, error)
                    self._complete_request()
                    return
                self._report_progress("filtering", received=self.result.received_count + len(features))
                selected, counts = selection.filter_features(features, self.isCanceled)
                self.result.record(key, name, selected, received_count=len(features))
                for kind, count in counts.items():
                    self.coverage_counts[kind] += count
                completed += 1
                self._complete_request()
                log_info("{}: получено {}, в исходном охвате {}, вне охвата {}, без проверки {}".format(
                    key, len(features), counts["inside"], counts["outside"], counts["unchecked"]))
                if counts["unchecked"] and not self.isCanceled():
                    log_warning("{}: объекты без проверяемой геометрии сохранены без пространственного отбора".format(key))
                if self.isCanceled():
                    return
                if index == len(queries) - 1:
                    self.result.completed_categories.append(name)
                    collection = self.result.collections[name]
                    if collection["features"]:
                        self.layerReady.emit(name, collection)
                if completed < total and not self._pause():
                    return


class FetchNspdByCadastralTask(NspdTask):
    def __init__(self, description, cadastral_numbers, category_ids,
                 delay_min=0, delay_max=0, crs_reproject=None, crs_assign=None,
                 round_mm=False, client_factory=None):
        super().__init__(description, delay_min, delay_max, crs_reproject,
                         crs_assign, round_mm, client_factory)
        self.cadastral_numbers = list(dict.fromkeys(
            str(number).strip() for number in cadastral_numbers if str(number).strip()))
        self.category_ids = category_ids
        self.progress_snapshot = FetchProgress(total=len(self.cadastral_numbers))

    def _fetch(self, client):
        total = len(self.cadastral_numbers)
        for index, number in enumerate(self.cadastral_numbers):
            if self.isCanceled():
                return
            self._report_progress("request", current=index + 1, detail="Кадастровый номер: {}".format(number))
            try:
                features = client.search(number, self.category_ids)
            except NspdApiError as error:
                if self.isCanceled():
                    return
                self._record_error(number, "", error)
            else:
                self.result.record(number, "", features)
                log_info("КН {}: получено {} объектов".format(number, len(features)))
            self._complete_request()
            if index < total - 1 and not self._pause():
                return

"""Render the actual Qt form and canvas offscreen without changing a user project."""
from types import SimpleNamespace
import os
import copy
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.qgis_support import APP
from tests.support import ROOT, plugin_module
from tests.test_qgis_msk import crs_from_proj, reference_data
from tests.manual_coverage_boundary import polygon
from tests.test_qgis_tasks import ScriptedClient
from qgis.core import QgsFeature, QgsFillSymbol, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QTreeWidgetItemIterator


def main():
    # Windows' offscreen Qt platform does not discover system fonts itself.
    font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'segoeui.ttf'
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            APP.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 9))
    output = ROOT / 'build' / 'ui_preview'
    output.mkdir(parents=True, exist_ok=True)
    reference = reference_data()
    target = crs_from_proj(reference['target_proj'])
    canvas = QgsMapCanvas()
    canvas.resize(780, 520)
    canvas.setCanvasColor(QColor('#f6f7f8'))
    canvas.setDestinationCrs(target)
    layer = QgsVectorLayer('Polygon', 'Тестовый участок 52:18:0050232:698', 'memory')
    layer.setCrs(target)
    parcel = polygon([reference['expected_ring_easting_northing']])
    feature = QgsFeature()
    feature.setGeometry(parcel)
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    layer.renderer().setSymbol(QgsFillSymbol.createSimple({'color': '#e5edf5', 'outline_color': '#315a7d', 'outline_width': '0.5'}))
    QgsProject.instance().addMapLayer(layer)
    QgsProject.instance().setCrs(target)
    canvas.setLayers([layer])
    extent = parcel.boundingBox()
    extent.scale(1.6)
    canvas.setExtent(extent)
    canvas.show()
    canvas.refresh()
    dialog = plugin_module('NSPD_Fetcher_dialog').NSPD_FetcherDialog(iface=SimpleNamespace(mapCanvas=lambda: canvas))
    dialog.chkEnableMsk.setChecked(True)
    dialog.crsMsk.setCrs(target)
    iterator = QTreeWidgetItemIterator(dialog.treeWidget)
    while iterator.value():
        item = iterator.value()
        if item.text(0) == 'Земельные участки':
            item.setExpanded(True)
        if item.text(0) == 'Земельные участки из ЕГРН':
            item.setCheckState(0, Qt.Checked)
        iterator += 1
    dialog.coverage_geometry = parcel.buffer(1, 5)
    dialog.coverage_crs = target
    dialog.on_coverage_mode_changed(True)
    dialog.resize(660, 640)
    dialog.show()
    QTest.qWait(150)
    dialog.grab().save(str(output / 'coverage.png'))
    canvas.grab().save(str(output / 'coverage_outline.png'))
    dialog.rbLayerCoverage.setChecked(True)
    dialog.cbLayer.setLayer(layer)
    APP.processEvents()
    dialog.grab().save(str(output / 'layer.png'))
    dialog.tabWidgetQueryMode.setCurrentIndex(1)
    dialog.textEditCadastral.setText(reference['cadastral_number'])
    APP.processEvents()
    dialog.grab().save(str(output / 'cadastral.png'))
    dialog.btnLoadSettings.click()
    APP.processEvents()
    dialog.grab().save(str(output / 'settings.png'))
    dialog.tabWidgetQueryMode.setCurrentIndex(0)
    dialog.rbManualCoverage.setChecked(True)
    dialog.activate_polygon_tool()
    tool = dialog.capture_tool
    tool.points = [QgsPointXY(x, y) for x, y in reference['expected_ring_easting_northing'][:3]]
    tool._draw(QgsPointXY(*reference['expected_ring_easting_northing'][3]))
    QTest.qWait(150)
    canvas.grab().save(str(output / 'drawing.png'))
    tool.cancel_capture()
    dialog.btnLoadSettings.setChecked(False)
    dialog.spinDelayMin.setValue(2)
    dialog.spinDelayMax.setValue(2)
    coverage = plugin_module('coverage')
    tasks = plugin_module('api_task')
    api = plugin_module('api_client')
    ui = plugin_module('NSPD_Fetcher_dialog')
    captured = coverage.CoverageInput([dialog.coverage_geometry], target)
    raw = {'type': 'Feature', 'id': 291607188, 'properties': {'category': 36368}, 'geometry': reference['nspd_geometry']}
    outside = copy.deepcopy(raw)
    outside['id'] = 999
    for coordinate in outside['geometry']['coordinates'][0]:
        coordinate[0] += 10000
    client = ScriptedClient([[raw, copy.deepcopy(raw), outside], [], api.NspdApiError('Тестовая ошибка сервера', 'network')])
    task = tasks.FetchNspdDataTask(
        'Предпросмотр прогресса', coverage.primitive_coordinates(captured.request_geometries) * 4,
        [{'name': 'Земельные участки из ЕГРН', 'id': 36368}], delay_min=2, delay_max=2,
        client_factory=lambda: client, coverage_input=captured,
        crs_reproject=plugin_module('msk_transform').make_gost2001_crs(target), crs_assign=target, round_mm=True)
    saved = []

    def save_progress(task, snapshot):
        if snapshot.phase == 'waiting' and snapshot.completed == 1 and not saved:
            saved.append(True)
            dialog.layout().activate()
            dialog.grab().save(str(output / 'progress_waiting.png'))

    dialog.controller.progressChanged.connect(save_progress)
    # Exercise the real worker and real GUI slots with local scripted replies.
    with ExitStack() as stack:
        for name in ('information', 'warning', 'critical'):
            stack.enter_context(patch.object(ui.QMessageBox, name))
        dialog.controller.start(task)
        for _ in range(500):
            if not dialog.controller.busy:
                break
            QTest.qWait(20)
        if dialog.controller.busy or not saved:
            raise RuntimeError('Progress preview did not finish')
        dialog.grab().save(str(output / 'progress_partial.png'))
    dialog.shutdown()
    canvas.stopRendering()
    canvas.close()
    QgsProject.instance().clear()
    dialog.deleteLater()
    canvas.deleteLater()
    APP.processEvents()
    print(output)


if __name__ == '__main__':
    main()

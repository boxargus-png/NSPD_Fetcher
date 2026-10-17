import unittest
from unittest.mock import Mock

from tests.qgis_support import APP
from tests.support import plugin_module
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject
from qgis.gui import QgsMapCanvas, QgsRubberBand
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QKeyEvent

ui = plugin_module("NSPD_Fetcher_dialog")


class CoverageInteractionTests(unittest.TestCase):
    def setUp(self):
        self.canvas = QgsMapCanvas()
        self.canvas.resize(640, 480)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.iface = Mock()
        self.iface.mapCanvas.return_value = self.canvas
        self.dialog = ui.NSPD_FetcherDialog(iface=self.iface)
        self.dialog.show()
        APP.processEvents()

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.deleteLater()
        self.canvas.deleteLater()
        APP.processEvents()
        QgsProject.instance().clear()

    def capture_triangle(self):
        self.dialog.activate_polygon_tool()
        tool = self.dialog.capture_tool
        tool.points = [QgsPointXY(1000, 2000), QgsPointXY(1100, 2000), QgsPointXY(1050, 2100)]
        tool._draw(QgsPointXY(1000, 2050))
        self.assertEqual(len(tool.points), 3)  # Cursor preview is not a stored vertex.
        tool.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
        APP.processEvents()
        self.assertTrue(self.dialog.isVisible())
        self.assertIsNotNone(self.dialog.coverage_geometry)
        return bytes(self.dialog.coverage_geometry.asWkb())

    def test_finished_outline_follows_map_crs_without_changing_query(self):
        original = self.capture_triangle()
        preview = self.dialog.coverage_preview
        self.assertTrue(preview.band.isVisible())
        self.assertEqual(preview.band.fillColor().alpha(), 0)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        expected = QgsGeometry(self.dialog.coverage_geometry)
        expected.transform(QgsCoordinateTransform(self.dialog.coverage_crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance()))
        self.assertTrue(preview.band.asGeometry().isGeosEqual(expected))
        self.dialog.show_coverage()
        self.assertTrue(self.canvas.extent().contains(expected.boundingBox()))
        self.assertEqual(bytes(self.dialog.coverage_geometry.asWkb()), original)
        self.assertEqual(self.dialog.coverage_crs.authid(), "EPSG:3857")

    def test_escape_on_redraw_keeps_original_and_restores_preview(self):
        original = self.capture_triangle()
        self.dialog.activate_polygon_tool()
        self.assertFalse(self.dialog.coverage_preview.band.isVisible())
        tool = self.dialog.capture_tool
        self.assertTrue(tool.hint.isVisibleTo(self.canvas))
        self.assertLess(tool.rubberBand.fillColor().alpha(), 30)
        tool.points = [QgsPointXY(1, 1)]
        tool.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog.coverage_preview.band.isVisible())
        self.assertEqual(bytes(self.dialog.coverage_geometry.asWkb()), original)
        self.assertFalse(tool.hint.isVisibleTo(self.canvas))

    def test_invalid_redraw_cannot_replace_previous_coverage(self):
        original = self.capture_triangle()
        self.dialog.activate_polygon_tool()
        tool = self.dialog.capture_tool
        for points in ([QgsPointXY(1, 1)], [QgsPointXY(0, 0), QgsPointXY(1, 1), QgsPointXY(2, 2)]):
            tool.points = points
            tool.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Enter, Qt.NoModifier))
            self.assertIs(self.canvas.mapTool(), tool)
            self.assertEqual(bytes(self.dialog.coverage_geometry.asWkb()), original)

    def test_change_map_crs_while_drawing_cancels_mixed_coordinate_capture(self):
        original = self.capture_triangle()
        self.dialog.activate_polygon_tool()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.assertFalse(self.dialog._capturing)
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(bytes(self.dialog.coverage_geometry.asWkb()), original)

    def test_modes_close_reopen_and_shutdown_leave_no_stale_outline(self):
        original = self.capture_triangle()
        preview = self.dialog.coverage_preview
        self.dialog.rbLayerCoverage.setChecked(True)
        self.assertFalse(preview.band.isVisible())
        self.assertTrue(self.dialog.btnSetCoverage.isHidden())
        self.dialog.rbManualCoverage.setChecked(True)
        self.assertTrue(preview.band.isVisible())
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.assertFalse(preview.band.isVisible())
        self.dialog.tabWidgetQueryMode.setCurrentIndex(0)
        self.dialog.close()
        self.assertFalse(preview.band.isVisible())
        self.dialog.show()
        self.assertTrue(preview.band.isVisible())
        self.assertEqual(bytes(self.dialog.coverage_geometry.asWkb()), original)
        self.assertTrue(self.dialog.btnShowCoverage.isEnabled())
        self.dialog.shutdown()
        self.assertFalse(any(isinstance(item, QgsRubberBand) for item in self.canvas.scene().items()))

    def test_input_modes_and_settings_disclosure_do_not_change_values(self):
        self.assertTrue(self.dialog.cbLayer.isHidden())
        self.assertTrue(self.dialog.groupBoxSettings.isHidden())
        self.dialog.btnLoadSettings.click()
        self.assertFalse(self.dialog.groupBoxSettings.isHidden())
        self.dialog.btnLoadSettings.click()
        self.assertEqual((self.dialog.spinDelayMin.value(), self.dialog.spinDelayMax.value()), (5, 10))
        self.dialog.spinDelayMin.setValue(11)
        self.assertFalse(self.dialog.groupBoxSettings.isHidden())
        self.assertFalse(self.dialog.btnFetch.isEnabled())
        self.dialog.tabWidgetQueryMode.setCurrentIndex(1)
        self.dialog.rbLayerCadastral.setChecked(True)
        self.assertTrue(self.dialog.textEditCadastral.isHidden())
        self.assertFalse(self.dialog.widgetCadastralLayer.isHidden())

from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QFrame, QLabel
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject, QgsWkbTypes,
)


class CoveragePreview:
    """A canvas-only outline. Original geometry and CRS remain untouched."""
    def __init__(self, canvas):
        self.canvas = canvas
        self.geometry = None
        self.crs = None
        self.visible = False
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setStrokeColor(QColor(210, 45, 45))
        self.band.setFillColor(QColor(255, 0, 0, 0))
        self.band.setWidth(2)
        self.canvas.destinationCrsChanged.connect(self.refresh)

    def set_geometry(self, geometry, crs):
        self.geometry = QgsGeometry(geometry) if geometry is not None else None
        self.crs = QgsCoordinateReferenceSystem(crs) if crs is not None else None
        self.refresh()

    def geometry_on_map(self):
        if self.geometry is None or self.crs is None:
            return None
        result = QgsGeometry(self.geometry)
        if result.transform(QgsCoordinateTransform(
                self.crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance())) != 0:
            raise ValueError("Не удалось отобразить охват в системе координат карты.")
        return result

    def set_visible(self, visible):
        self.visible = visible
        self.refresh()

    def refresh(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        if not self.visible or self.geometry is None:
            return
        try:
            geometry = self.geometry_on_map()
            self.band.setToGeometry(geometry, None)
        except Exception as error:
            from .logger import log_warning
            log_warning("Не удалось показать контур охвата: {}".format(error))

    def zoom(self):
        geometry = self.geometry_on_map()
        if geometry is None or geometry.isEmpty():
            return
        extent = geometry.boundingBox()
        extent.scale(1.2)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def dispose(self):
        self.canvas.destinationCrsChanged.disconnect(self.refresh)
        self.canvas.scene().removeItem(self.band)
        self.band = None


class PolygonCaptureTool(QgsMapToolEmitPoint):
    polygonCompleted = pyqtSignal(QgsGeometry)
    captureCanceled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self._finished = False
        self.rubberBand = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubberBand.setStrokeColor(QColor(Qt.red))
        self.rubberBand.setFillColor(QColor(255, 0, 0, 24))
        self.rubberBand.setWidth(2)
        self.hint = QLabel("Щелчок — вершина · Enter — завершить · Esc — отменить", canvas)
        self.hint.setMargin(8)
        self.hint.setFrameStyle(QFrame.StyledPanel)
        self.hint.setAutoFillBackground(True)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hint.move(10, 10)
        self.hint.adjustSize()
        self.hint.hide()
        self.canvas.destinationCrsChanged.connect(self.cancel_capture)

    def activate(self):
        super().activate()
        self.hint.show()
        self.hint.raise_()

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        point = self.toMapCoordinates(event.pos())
        self.points.append(point)
        self._draw()

    def canvasMoveEvent(self, event):
        self._draw(self.toMapCoordinates(event.pos()))

    def _draw(self, cursor=None):
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        for point in self.points + ([cursor] if cursor is not None and self.points else []):
            self.rubberBand.addPoint(point, False)
        self.rubberBand.show()
        self.rubberBand.updatePosition()
        self.rubberBand.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if len(self.points) < 3:
                self.hint.setText("Добавьте минимум три вершины · Esc — отменить")
                self.hint.adjustSize()
                return
            polygon = QgsGeometry.fromPolygonXY([self.points])
            if not polygon.isGeosValid() or polygon.area() <= 0:
                self.hint.setText("Некорректный полигон. Esc — отменить и нарисовать заново")
                self.hint.adjustSize()
                return
            self._finished = True
            self.cleanup()
            self.canvas.unsetMapTool(self)
            self.polygonCompleted.emit(polygon)
        elif event.key() == Qt.Key_Escape:
            self.cancel_capture()

    def cancel_capture(self):
        if self.canvas.mapTool() is self:
            self.canvas.unsetMapTool(self)

    def deactivate(self):
        self.cleanup()
        super().deactivate()
        if not self._finished:
            self._finished = True
            self.captureCanceled.emit()

    def cleanup(self):
        self.points.clear()
        self.hint.hide()
        if self.rubberBand is not None:
            self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)

    def dispose(self):
        self.cleanup()
        self.canvas.destinationCrsChanged.disconnect(self.cancel_capture)
        self.hint.deleteLater()
        if self.rubberBand is not None:
            self.canvas.scene().removeItem(self.rubberBand)
            self.rubberBand = None

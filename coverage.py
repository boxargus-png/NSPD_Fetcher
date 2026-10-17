"""Snapshot the original coverage separately from the server query geometry."""
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsFeature, QgsGeometry,
    QgsProject, QgsVectorLayer, QgsWkbTypes,
)

MAX_SIZE = 1000000
SPLIT_AREA = 400000000
GRID_SPACING = 20000


def to_web_mercator(geometry, source_crs):
    result = QgsGeometry(geometry)
    transform = QgsCoordinateTransform(
        source_crs, QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
    result.transform(transform)
    return result


def geometries_from_layer(layer):
    return CoverageInput.from_layer(layer).request_geometries


class CoverageInput:
    """Detached geometries, CRS and transform context captured on the GUI thread.

    Polygon coordinates used for selection never make a round trip through
    Web Mercator. Grid splitting and the search margin only affect requests.
    """
    def __init__(self, geometries, source_crs):
        if source_crs is None or not source_crs.isValid():
            raise ValueError("У охвата не задана корректная система координат.")
        self.crs = QgsCoordinateReferenceSystem(source_crs)
        self.transform_context = QgsCoordinateTransformContext(QgsProject.instance().transformContext())
        web = QgsCoordinateReferenceSystem("EPSG:3857")
        forward = QgsCoordinateTransform(self.crs, web, self.transform_context)
        reverse = QgsCoordinateTransform(web, self.crs, self.transform_context)
        self.original_geometries = []
        self.request_geometries = []
        for geometry in geometries:
            parts = geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]
            for part in parts:
                original = QgsGeometry(part)
                request = QgsGeometry(part)
                if request.transform(forward) != 0:
                    raise ValueError("Не удалось преобразовать охват в EPSG:3857.")
                if QgsWkbTypes.geometryType(part.wkbType()) in (
                        QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry):
                    # Preserve the existing 1-Web-Mercator-unit footprint for
                    # points/lines. This is distinct from the 5 m query margin.
                    request = request.buffer(1.0, 5)
                    original = QgsGeometry(request)
                    if original.transform(reverse) != 0:
                        raise ValueError("Не удалось подготовить охват точек или линий.")
                self.original_geometries.append(original)
                self.request_geometries.append(request)

    @classmethod
    def from_layer(cls, layer):
        features = layer.selectedFeatures() or layer.getFeatures()
        return cls([feature.geometry() for feature in features], layer.crs())


def needs_grid_split(geometries):
    return any(geometry.area() > SPLIT_AREA for geometry in geometries)


def split_with_grid(geometries):
    import processing

    layer = QgsVectorLayer("Polygon?crs=epsg:3857", "TempCoverage", "memory")
    features = []
    for geometry in geometries:
        feature = QgsFeature()
        feature.setGeometry(geometry)
        features.append(feature)
    success, _ = layer.dataProvider().addFeatures(features)
    if not success:
        raise ValueError("Не удалось создать слой охвата для разбиения")
    layer.updateExtents()
    extent = layer.extent()
    grid_extent = "{},{},{},{} [EPSG:3857]".format(
        extent.xMinimum(), extent.xMaximum(), extent.yMinimum(), extent.yMaximum())
    grid = processing.run("native:creategrid", {
        "TYPE": 1, "EXTENT": grid_extent,
        "HSPACING": GRID_SPACING, "VSPACING": GRID_SPACING,
        "HOVERLAY": 0, "VOVERLAY": 0,
        "CRS": QgsCoordinateReferenceSystem("EPSG:3857"), "OUTPUT": "memory:",
    })["OUTPUT"]
    split = processing.run("native:splitwithlines", {
        "INPUT": layer, "LINES": grid, "OUTPUT": "memory:",
    })["OUTPUT"]
    return [feature.geometry() for feature in split.getFeatures()
            if not feature.geometry().isEmpty()]


def primitive_coordinates(geometries, max_size=MAX_SIZE):
    """Serialize every polygon and ring; never silently drop part of a coverage."""
    coordinates = []
    for geometry in geometries:
        parts = geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]
        for part in parts:
            bbox = part.boundingBox()
            if bbox.width() > max_size or bbox.height() > max_size:
                raise ValueError("Охват слишком большой: необходимо разбить полигоны")
            polygon = part.asPolygon()
            if not polygon:
                raise ValueError("В охвате обнаружена пустая или неподдерживаемая геометрия")
            coordinates.append([[[point.x(), point.y()] for point in ring] for ring in polygon])
    return coordinates

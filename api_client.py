"""NSPD request construction and validation, with an injectable transport."""
from collections import namedtuple
import json
import math
import time
from urllib.parse import urlencode

from .constants import DEFAULT_HEADERS
from .logger import log_debug

INTERSECTS_URL = "https://nspd.gov.ru/api/geoportal/v1/intersects?typeIntersect=fullObject"
SEARCH_URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
NetworkResponse = namedtuple("NetworkResponse", "status_code body")


class NspdApiError(Exception):
    def __init__(self, message, kind, status_code=None, response_text=""):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.response_text = response_text[:1000]


def _response_error(message):
    raise NspdApiError(message, "response")


def _validate_crs(value):
    # Missing CRS is permitted by the NSPD endpoint contract. Explicitly
    # different coordinates must never be silently labelled EPSG:3857.
    if value is None:
        return
    if not isinstance(value, dict) or value.get("type") != "name":
        _response_error("Некорректная система координат в ответе НСПД")
    properties = value.get("properties")
    name = properties.get("name") if isinstance(properties, dict) else None
    if name not in ("EPSG:3857", "urn:ogc:def:crs:EPSG::3857",
                    "http://www.opengis.net/def/crs/EPSG/0/3857"):
        _response_error("НСПД вернула неожиданную систему координат: {}".format(name))


def _position(value):
    if not isinstance(value, list) or len(value) < 2:
        _response_error("Некорректные координаты геометрии в ответе НСПД")
    try:
        valid = all(type(number) in (int, float) and math.isfinite(number) for number in value)
    except OverflowError:
        valid = False
    if not valid:
        _response_error("Координаты НСПД должны быть конечными числами")


def _coordinates(value, depth):
    if not isinstance(value, list):
        _response_error("Некорректная структура координат в ответе НСПД")
    for child in value:
        if depth == 1:
            _position(child)
        else:
            _coordinates(child, depth - 1)


def _geometry(value, depth=0):
    # GeoJSON permits a null geometry (e.g. a parcel without surveyed bounds).
    if value is None:
        return
    if not isinstance(value, dict) or depth > 16:
        _response_error("Некорректная геометрия в ответе НСПД")
    _validate_crs(value.get("crs"))
    kind = value.get("type")
    if kind == "GeometryCollection":
        geometries = value.get("geometries")
        if not isinstance(geometries, list) or any(item is None for item in geometries):
            _response_error("Некорректная коллекция геометрий в ответе НСПД")
        for geometry in geometries:
            _geometry(geometry, depth + 1)
        return
    dimensions = {"Point": 0, "MultiPoint": 1, "LineString": 1,
                  "MultiLineString": 2, "Polygon": 2, "MultiPolygon": 3}
    if not isinstance(kind, str) or kind not in dimensions:
        _response_error("Неизвестный тип геометрии НСПД: {}".format(kind))
    coordinates = value.get("coordinates")
    if coordinates == []:
        return  # Empty GeoJSON geometries are allowed.
    if kind == "Point":
        _position(coordinates)
    else:
        _coordinates(coordinates, dimensions[kind])


def _features(value, elements=False):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _response_error("Некорректный список объектов в ответе НСПД")
    for item in value:
        if "geometry" not in item or (not elements and item.get("type") != "Feature"):
            _response_error("Некорректный объект в ответе НСПД")
        _validate_crs(item.get("crs"))
        _geometry(item["geometry"])
        properties = item.get("attributes" if elements else "properties")
        if properties is not None and not isinstance(properties, dict):
            _response_error("Некорректные атрибуты объекта в ответе НСПД")
    if elements:
        features = []
        for item in value:
            feature = dict(item, type="Feature", properties=item.get("attributes") or {})
            feature.pop("attributes", None)
            features.append(feature)
        return features
    return value


def _check_envelope(data):
    if not isinstance(data, dict):
        _response_error("Ответ НСПД должен быть JSON-объектом")
    status = data.get("status")
    if (data.get("error") or data.get("errors") or data.get("success") is False
            or (isinstance(status, str) and status.lower() in ("error", "failed"))
            or (type(status) in (int, float) and status >= 400)):
        _response_error("НСПД сообщила об ошибке в теле ответа")


def parse_response(data, search=False):
    _check_envelope(data)
    _validate_crs(data.get("crs"))
    if search and "data" in data:
        data = data["data"]
        _check_envelope(data)
    if "type" in data and data["type"] != "FeatureCollection":
        _response_error("Неожиданный тип ответа НСПД")
    _validate_crs(data.get("crs"))
    if "features" in data:
        features = _features(data["features"])
        if features or not search or "elements" not in data:
            return features
    if search and "elements" in data:
        return _features(data["elements"], elements=True)
    _response_error("Неизвестный формат ответа НСПД: отсутствует список объектов")


class NspdClient:
    """Build the same NSPD requests; QGIS owns networking and TLS policy."""
    def __init__(self, transport=None, feedback=None):
        if transport is None:
            from .qgis_network import QgisNetworkTransport
            transport = QgisNetworkTransport(feedback)
        self.transport = transport

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # Each transport call returns detached bytes, not an open reply.

    def _request(self, method, url, search=False, headers=None, body=b""):
        started = time.monotonic()
        response = self.transport.request(method, url, headers=headers, body=body)
        log_debug("{} {}: HTTP {}, {:.2f} с".format(
            method, url.split("?")[0], response.status_code, time.monotonic() - started))
        diagnostic = response.body[:4000].decode("utf-8", errors="replace")[:1000]
        if response.status_code != 200:
            raise NspdApiError(
                "Сервер НСПД вернул ошибку HTTP {}".format(response.status_code),
                "http", response.status_code, diagnostic)
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise NspdApiError("Ошибка разбора JSON-ответа НСПД", "response",
                               response.status_code, diagnostic) from exc
        try:
            return parse_response(data, search=search)
        except NspdApiError as exc:
            raise NspdApiError(str(exc), exc.kind, response.status_code, diagnostic) from exc

    def intersects(self, coordinates, category_id):
        headers = dict(DEFAULT_HEADERS)
        headers.update({"Content-Type": "application/json", "Referer": "https://nspd.gov.ru/map"})
        body = {
            "geom": {"type": "FeatureCollection", "features": [{
                "type": "Feature",
                "geometry": {
                    "crs": {"properties": {"name": "EPSG:3857"}, "type": "name"},
                    "type": "Polygon", "coordinates": coordinates,
                },
                "properties": {},
            }]},
            "categories": [{"id": category_id}],
        }
        return self._request("POST", INTERSECTS_URL, headers=headers,
                             body=json.dumps(body).encode("utf-8"))

    def search(self, cadastral_number, category_ids):
        headers = dict(DEFAULT_HEADERS)
        headers["Referer"] = "https://nspd.gov.ru/map?thematic=PKK"
        params = {"query": cadastral_number}
        if category_ids:
            params["layersId"] = [str(category) for category in category_ids]
        url = SEARCH_URL + "?" + urlencode(params, doseq=True)
        return self._request("GET", url, search=True, headers=headers)

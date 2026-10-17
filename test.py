"""Manual live NSPD check: run explicitly with QGIS Python, outside unittest."""
import json
import math
import sys

from tests.qgis_support import APP
from tests.support import ROOT, plugin_module

api = plugin_module("api_client")
layers = plugin_module("layer_manager")
msk = plugin_module("msk_transform")
network = plugin_module("qgis_network")


def main():
    from qgis.core import QgsCoordinateReferenceSystem

    reference = json.loads((ROOT / "tests/fixtures/msk52_52_18_0050232_698.json").read_text(encoding="utf-8"))
    failed = False
    try:
        with api.NspdClient() as client:
            try:
                features = client.search(reference["cadastral_number"], [36048])
                print("Поиск: получено объектов:", len(features))
                if len(features) != 1:
                    raise ValueError("Для эталона ожидался один объект")
                target = QgsCoordinateReferenceSystem()
                if not target.createFromProj(reference["target_proj"]):
                    raise ValueError("Не удалось создать МСК эталона")
                source = layers.load_geojson_layer({"type": "FeatureCollection", "features": features})
                output = layers.process_result_layer(source, msk.make_gost2001_crs(target), target, True)
                ring = [(v.x(), v.y()) for v in next(output.getFeatures()).geometry().vertices()]
                expected = reference["expected_ring_easting_northing"]
                if len(ring) != len(expected):
                    raise ValueError("Изменилось число вершин эталонного участка")
                residual = max(min(math.dist(point, ref) for ref in expected) for point in ring)
                print("Максимальное отличие от выписки после округления, м:", residual)
                if residual >= 1e-8:
                    raise ValueError("Живая геометрия не совпадает с сохранённым эталоном")
            except (api.NspdApiError, ValueError) as error:
                print("Ошибка поиска/МСК:", str(error))
                failed = True
            try:
                features = client.intersects(reference["nspd_geometry"]["coordinates"], 36368)
                print("Охват: получено объектов:", len(features))
                if not features:
                    raise ValueError("Охват тестового участка вернул пустой результат")
            except (api.NspdApiError, ValueError) as error:
                print("Ошибка охвата:", str(error))
                failed = True
    finally:
        network.release_network()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

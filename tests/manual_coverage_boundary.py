"""Reproduce the MSK coverage mismatch; live requests require explicit --live.

Run with QGIS Python: python -B -m tests.manual_coverage_boundary --live
Diagnostic only: does not change the plugin's transformations or user profile.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from tests.qgis_support import APP
from tests.support import ROOT, plugin_module
from tests.test_qgis_msk import crs_from_proj, reference_data
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsGeometry, QgsPointXY, QgsProject, QgsVectorFileWriter, QgsVectorLayer,
)

coverage = plugin_module("coverage")
msk = plugin_module("msk_transform")
api = plugin_module("api_client")
network = plugin_module("qgis_network")


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def polygon(rings):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(*point[:2]) for point in ring] for ring in rings])


def make_mask(reference, penetration):
    # A four-metre segment in the middle of edge 1, extending one metre outside
    # and penetration metres inside. This is a positive-area intersection,
    # not a point/line touch subject to server predicate boundary conventions.
    ring = reference["expected_ring_easting_northing"]
    parcel = polygon([ring])
    a, b = ring[1:3]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    center = parcel.centroid().asPoint()
    if (center.x() - mx) * nx + (center.y() - my) * ny < 0:
        nx, ny = -nx, -ny
    return polygon([[[mx + u * ux + v * nx, my + u * uy + v * ny]
                     for u, v in ((-2, -1), (2, -1), (2, penetration),
                                  (-2, penetration), (-2, -1))]])


def current_vector_query(mask, target):
    layer = QgsVectorLayer("Polygon", "coverage", "memory")
    layer.setCrs(target)
    feature = QgsFeature()
    feature.setGeometry(mask)
    success, _ = layer.dataProvider().addFeatures([feature])
    if not success:
        raise RuntimeError("Cannot prepare the test coverage layer")
    actual = coverage.geometries_from_layer(layer)[0]
    manual = coverage.to_web_mercator(mask, target)
    if bytes(actual.asWkb()) != bytes(manual.asWkb()):
        raise AssertionError("Vector and manually drawn coverage paths differ")
    return actual


def recover_msk(geometry, working):
    result = QgsGeometry(geometry)
    result.transform(QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:3857"), working, QgsProject.instance()))
    return result


class RecordingTransport:
    def __init__(self, output):
        self.native = network.QgisNetworkTransport()
        self.output = output
        self.records = []

    def request(self, method, url, headers=None, body=b""):
        # Keep exact payloads and raw responses locally, without printing the
        # server's unrelated attributes or changing the user's plugin log.
        index = len(self.records) + 1
        record = {"index": index, "method": method, "url": url,
                  "started_at_utc": datetime.now(timezone.utc).isoformat()}
        if body:
            record["request_file"] = f"request_{index:02d}.json"
            (self.output / record["request_file"]).write_bytes(body)
        self.records.append(record)
        started = time.monotonic()
        try:
            response = self.native.request(method, url, headers=headers, body=body)
            record.update(status=response.status_code, response_file=f"response_{index:02d}.json",
                          response_sha256=hashlib.sha256(response.body).hexdigest())
            (self.output / record["response_file"]).write_bytes(response.body)
            return response
        except Exception as error:
            record["error"] = str(error)
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            save_json(self.output / "http_records.json", self.records)


def write_geopackage(output, geometries, target):
    path = output / "boundary_case.gpkg"
    for index, (name, geometry) in enumerate(geometries.items()):
        layer = QgsVectorLayer("Polygon", name, "memory")
        layer.setCrs(target)
        feature = QgsFeature()
        feature.setGeometry(geometry)
        success, _ = layer.dataProvider().addFeatures([feature])
        if not success:
            raise RuntimeError("Cannot create diagnostic layer: " + name)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteFile if index == 0
                                        else QgsVectorFileWriter.CreateOrOverwriteLayer)
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, str(path), QgsProject.instance().transformContext(), options)
        if result[0] != QgsVectorFileWriter.NoError:
            raise RuntimeError(str(result))


def draw_case(output, parcel, mask, apparent, confirmed):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    origin = mask.centroid().asPoint()
    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    for ax in axes:
        for geometry, color, label, style in (
                (parcel, "#2864a0", "Участок из выписки", "-"),
                (mask, "#159267", "Заданный охват МСК", "-"),
                (apparent, "#c53636", "Текущий запрос, возвращённый в МСК", "--")):
            vertices = list(geometry.vertices())
            xs = [point.x() - origin.x() for point in vertices]
            ys = [point.y() - origin.y() for point in vertices]
            ax.fill(xs, ys, color=color, alpha=0.10)
            ax.plot(xs, ys, color=color, label=label, linestyle=style, linewidth=2)
        overlap = parcel.intersection(mask)
        vertices = list(overlap.vertices())
        ax.fill([p.x() - origin.x() for p in vertices], [p.y() - origin.y() for p in vertices],
                color="#159267", alpha=0.5)
        ax.set_aspect("equal")
        ax.set_xlabel("Восточное смещение, м")
        ax.set_ylabel("Северное смещение, м")
        ax.grid(alpha=0.2)
    axes[0].set_title("Участок и охват")
    axes[1].set_title("Граница крупным планом")
    axes[1].set_xlim(-3.5, 3.5)
    axes[1].set_ylim(-2.3, 3.3)
    axes[1].text(0.02, 0.98, "Заход внутрь: 0,25 м\nПересечение: 1,00 м²\nЗазор после пересчёта: 0,322 м",
                 transform=axes[1].transAxes, va="top", fontsize=10,
                 bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "none"})
    title = "Пропуск участка подтверждён ответами НСПД" if confirmed else "Локальная проверка геометрии охвата"
    fig.suptitle(title + "\n52:18:0050232:698", fontsize=15)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, fontsize=9)
    fig.subplots_adjust(top=0.82, bottom=0.17, wspace=0.25)
    fig.savefig(output / "boundary_case.png", dpi=170)
    fig.savefig(output / "boundary_case.svg")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send six read-only NSPD requests, two seconds apart")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "build" / "coverage_boundary_52_18_0050232_698" / stamp
    output.mkdir(parents=True, exist_ok=False)
    reference = reference_data()
    target = crs_from_proj(reference["target_proj"])
    working = msk.make_gost2001_crs(target)
    parcel = polygon([reference["expected_ring_easting_northing"]])
    server = polygon(reference["nspd_geometry"]["coordinates"])
    mask = make_mask(reference, 0.25)
    current = current_vector_query(mask, target)
    reverse = coverage.to_web_mercator(mask, working)
    deep = current_vector_query(make_mask(reference, 0.75), target)
    apparent = recover_msk(current, working)
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "qgis_version": Qgis.QGIS_VERSION,
        "cadastral_number": reference["cadastral_number"], "category": 36368,
        "target_proj": reference["target_proj"], "working_proj": working.toProj(),
        "mask_geometry_msk": json.loads(mask.asJson(15)),
        "measurements": {
            "mask_area_m2": mask.area(), "penetration_m": 0.25,
            "intersection_with_extract_m2": parcel.intersection(mask).area(),
            "current_query_intersects_saved_nspd_geometry": server.intersects(current),
            "reverse_query_intersects_saved_nspd_geometry": server.intersects(reverse),
            "current_query_gap_3857_units": server.distance(current),
            "current_query_gap_in_result_msk_m": parcel.distance(apparent),
        }, "live": [], "confirmed_by_server": False,
    }
    if not (parcel.intersection(mask).area() > 0.99 and not server.intersects(current)
            and server.intersects(reverse) and server.intersects(deep)):
        raise AssertionError("Reference geometry no longer reproduces the boundary case")
    write_geopackage(output, {
        "parcel_extract_msk": parcel, "coverage_intended_msk": mask,
        "current_query_recovered_msk": apparent,
        "reverse_query_recovered_msk": recover_msk(reverse, working),
    }, target)
    error = None
    try:
        if args.live:
            transport = RecordingTransport(output)
            with api.NspdClient(transport=transport) as client:
                found = client.search(reference["cadastral_number"], [36048])
                if len(found) != 1 or found[0]["geometry"]["type"] != "Polygon":
                    raise AssertionError("Live reference parcel is ambiguous or changed geometry type")
                source = found[0]
                if source["properties"].get("category") != 36368 or "id" not in source:
                    raise AssertionError("Cannot establish live parcel identity")
                identity = source["id"]
                summary["nspd_id"] = identity
                live_geometry = polygon(source["geometry"]["coordinates"])
                summary["live_geometry_max_vertex_change_3857"] = max(
                    min(math.hypot(a.x()-b.x(), a.y()-b.y()) for b in server.vertices())
                    for a in live_geometry.vertices())
                if summary["live_geometry_max_vertex_change_3857"] > 0.001:
                    raise AssertionError("Live parcel changed: saved extract must be revalidated")
                for name, query in (("current", current), ("reverse", reverse), ("deep_control", deep),
                                    ("reverse_repeat", reverse), ("current_repeat", current)):
                    time.sleep(2)
                    features = client.intersects(coverage.primitive_coordinates([query])[0], 36368)
                    match = any(f.get("id") == identity and f.get("properties", {}).get("category") == 36368
                                for f in features)
                    item = {"case": name, "response_index": len(transport.records),
                            "returned_count": len(features), "target_present": match,
                            "local_intersects_live_parcel": live_geometry.intersects(query),
                            "returned_ids": [f.get("id") for f in features]}
                    summary["live"].append(item)
                    save_json(output / "summary.json", summary)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
                summary["confirmed_by_server"] = [item["target_present"] for item in summary["live"]] == [False, True, True, True, False]
    except Exception as exc:
        error = exc
        summary["error"] = str(exc)
    finally:
        network.release_network()
        save_json(output / "summary.json", summary)
    draw_case(output, parcel, mask, apparent, summary["confirmed_by_server"])
    print(json.dumps({"output": str(output), "measurements": summary["measurements"],
                      "confirmed_by_server": summary["confirmed_by_server"], "error": str(error) if error else None},
                     ensure_ascii=False, indent=2), flush=True)
    return 1 if error or (args.live and not summary["confirmed_by_server"]) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

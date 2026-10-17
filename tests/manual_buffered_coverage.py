"""Check the buffered worker and unchanged MSK pipeline; --live sends three NSPD requests."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from tests.manual_coverage_boundary import (
    RecordingTransport, current_vector_query, make_mask, save_json,
)
from tests.qgis_support import APP
from tests.support import ROOT, plugin_module
from tests.test_qgis_msk import reference_data, crs_from_proj
from tests.test_qgis_tasks import ScriptedClient
from qgis.core import QgsProject

api = plugin_module("api_client")
coverage = plugin_module("coverage")
tasks = plugin_module("api_task")
layers = plugin_module("layer_manager")
msk = plugin_module("msk_transform")
network = plugin_module("qgis_network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "build" / "buffered_coverage_52_18_0050232_698" / stamp
    output.mkdir(parents=True, exist_ok=False)
    reference = reference_data()
    target = crs_from_proj(reference["target_proj"])
    mask = make_mask(reference, 0.25)
    captured = coverage.CoverageInput([mask], target)
    original_web = current_vector_query(mask, target)
    coordinates = coverage.primitive_coordinates([original_web])
    feature = {"type": "Feature", "id": 291607188, "geometry": reference["nspd_geometry"],
               "properties": {"category": 36368, "cad_num": reference["cadastral_number"]}}
    summary = {"live": args.live, "cadastral_number": reference["cadastral_number"]}
    try:
        if args.live:
            client = api.NspdClient(transport=RecordingTransport(output))
            found = client.search(reference["cadastral_number"], [36048])
            if len(found) != 1 or found[0]["id"] != feature["id"]:
                raise AssertionError("Live reference parcel identity changed")
            time.sleep(2)
            baseline = client.intersects(coordinates[0], 36368)
            summary["unbuffered_target_present"] = any(f.get("id") == feature["id"] for f in baseline)
            time.sleep(2)
        else:
            client = ScriptedClient([[feature]])
        received = []
        intersects = client.intersects

        def remember_response(*arguments):
            features = intersects(*arguments)
            received.extend(features)
            return features

        client.intersects = remember_response
        task = tasks.FetchNspdDataTask("Buffered reference", coordinates, [{"name": "land", "id": 36368}],
                                       client_factory=lambda: client, coverage_input=captured)
        if not task.run():
            raise AssertionError([str(outcome.error) for outcome in task.result.errors])
        collection = task.result.collections["land"]
        summary.update(received_count=task.result.received_count, retained_count=task.result.feature_count,
                       spatial_counts=task.coverage_counts,
                       buffered_target_present=any(f.get("id") == feature["id"] for f in collection["features"]))
        if not summary["buffered_target_present"]:
            raise AssertionError("Buffered coverage lost the reference parcel")
        working = msk.make_gost2001_crs(target)
        # Independent check through the actual output processing pipeline,
        # with rounding disabled, for EVERY received parcel (also rejected ones).
        raw_layer = layers.load_geojson_layer(layers.prepare_geojson(
            {"type": "FeatureCollection", "features": received}))
        unrounded = layers.process_result_layer(raw_layer, working, target, False)
        checks = [{"id": f["nspd_id"], "intersects_original": mask.intersects(f.geometry()),
                   "overlap_m2": mask.intersection(f.geometry()).area(),
                   "distance_m": mask.distance(f.geometry())} for f in unrounded.getFeatures()]
        expected_ids = {f["id"] for f in checks if f["intersects_original"]}
        retained_ids = {f["id"] for f in collection["features"]}
        if retained_ids != expected_ids:
            raise AssertionError("Selection differs from the unrounded MSK output intersection")
        summary.update(unrounded_checks=checks, retained_ids=sorted(retained_ids),
                       selection_matches_unrounded_pipeline=True)
        success, error = layers.add_result_layer(collection, working, target, True)
        if not success:
            raise AssertionError(error)
        result_layers = list(QgsProject.instance().mapLayers().values())
        if len(result_layers) != 1:
            raise AssertionError("Expected a single result layer")
        result = result_layers[0]
        actual = next(f for f in result.getFeatures() if f["nspd_id"] == feature["id"])
        ring = [(point.x(), point.y()) for point in actual.geometry().vertices()]
        residual = max(min(((x-ex)**2+(y-ey)**2)**0.5
                           for ex, ey in reference["expected_ring_easting_northing"]) for x, y in ring)
        summary.update(result_layer_count=1, result_feature_count=result.featureCount(),
                       reference_coordinates_msk=ring, reference_max_residual_m=residual)
        if len(ring) != 5 or residual >= 1e-8:
            raise AssertionError("MSK coordinates no longer match the extract")
    except Exception as error:
        summary["error"] = str(error)
    finally:
        network.release_network()
        save_json(output / "summary.json", summary)
    print(json.dumps(dict(summary, output=str(output)), ensure_ascii=False, indent=2), flush=True)
    return int("error" in summary)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

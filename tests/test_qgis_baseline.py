import copy
import unittest
from unittest.mock import patch

from tests.qgis_support import APP
from tests.support import plugin_module
from qgis.core import QgsCoordinateReferenceSystem, QgsProject
import processing

layers = plugin_module("layer_manager")


def feature_collection():
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": 101,
        "geometry": {"type": "Polygon", "coordinates": [[
            [1000.1234, 2000.5678], [1100.1234, 2000.5678],
            [1100.1234, 2100.5678], [1000.1234, 2000.5678],
        ]]},
        "properties": {"category": 36368, "name": "outer", "options": {"name": "inner", "area": 10}},
    }]}


class LayerPipelineTests(unittest.TestCase):
    def tearDown(self):
        QgsProject.instance().clear()

    def test_msk_algorithm_order_parameters_and_assigned_crs(self):
        source = feature_collection()
        source["features"].append(copy.deepcopy(source["features"][0]))
        reproject = QgsCoordinateReferenceSystem("EPSG:3395")
        assign = QgsCoordinateReferenceSystem("EPSG:32637")
        with patch.object(processing, "run", wraps=processing.run) as run:
            success, error = layers.add_result_layer(source, reproject, assign, True, "Test")
        self.assertTrue(success, error)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            "native:reprojectlayer",
            "native:snappointstogrid", "native:assignprojection",
        ])
        rounding = run.call_args_list[1].args[1]
        self.assertEqual((rounding["HSPACING"], rounding["VSPACING"]), (0.01, 0.01))
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(layer.crs(), assign)
        self.assertEqual(layer.featureCount(), 1)
        feature = next(layer.getFeatures())
        self.assertEqual(feature["name"], "outer")
        self.assertEqual(feature["options_name"], "inner")
        self.assertEqual(feature["nspd_id"], 101)
        # Assignment must not transform the coordinates a second time.
        point = next(feature.geometry().vertices())
        self.assertAlmostEqual(point.x(), 1000.12, places=2)
        self.assertLess(point.y(), 3000)

    def test_msk_disabled_removes_only_exact_records_without_geometry_processing(self):
        data = feature_collection()
        data['features'].append(copy.deepcopy(data['features'][0]))
        with patch.object(processing, "run", wraps=processing.run) as run:
            success, error = layers.add_result_layer(data)
        self.assertTrue(success, error)
        run.assert_not_called()
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(layer.crs().authid(), "EPSG:3857")
        self.assertEqual(layer.featureCount(), 1)

    def test_empty_result_does_not_add_a_layer(self):
        success, error = layers.add_result_layer({"features": []})
        self.assertFalse(success)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 0)

    def test_input_is_unchanged_and_result_is_a_memory_layer(self):
        source = feature_collection()
        original = copy.deepcopy(source)
        success, error = layers.add_result_layer(source)
        self.assertTrue(success, error)
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(layer.providerType(), "memory")
        self.assertEqual(layer.featureCount(), 1)
        self.assertEqual(source, original)

    def test_geojson_attributes_preserve_uri_separators_and_unicode(self):
        source = feature_collection()
        value = 'Адрес | корпус "А"; 10%'
        source["features"][0]["properties"]["name"] = value
        success, error = layers.add_result_layer(source)
        self.assertTrue(success, error)
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(next(layer.getFeatures())["name"], value)

    def test_coincident_geometry_with_distinct_ids_categories_or_attributes_is_kept(self):
        data = feature_collection()
        original = data['features'][0]
        changed_id = dict(copy.deepcopy(original), id=102)
        changed_category = copy.deepcopy(original)
        changed_category['properties']['category'] = 36369
        changed_attributes = copy.deepcopy(original)
        changed_attributes['properties']['name'] = 'another record'
        data['features'].extend([changed_id, changed_category, changed_attributes, copy.deepcopy(original)])
        success, error = layers.add_result_layer(data)
        self.assertTrue(success, error)
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(layer.featureCount(), 4)
        actual = {(f['nspd_id'], f['category'], f['name']) for f in layer.getFeatures()}
        self.assertEqual(actual, {(101, 36368, 'outer'), (102, 36368, 'outer'),
                                 (101, 36369, 'outer'), (101, 36368, 'another record')})

    def test_missing_identity_is_kept_even_if_geometry_is_identical(self):
        data = feature_collection()
        del data['features'][0]['id']
        data['features'].append(copy.deepcopy(data['features'][0]))
        success, error = layers.add_result_layer(data)
        self.assertTrue(success, error)
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        self.assertEqual(layer.featureCount(), 2)

    def test_distinct_raw_geometries_survive_becoming_equal_after_rounding(self):
        data = feature_collection()
        second = copy.deepcopy(data['features'][0])
        for point in second['geometry']['coordinates'][0]:
            point[0] += 0.000001
        data['features'].append(second)
        success, error = layers.add_result_layer(data, round_mm=True)
        self.assertTrue(success, error)
        layer = next(iter(QgsProject.instance().mapLayers().values()))
        features = list(layer.getFeatures())
        self.assertEqual(len(features), 2)
        self.assertEqual(bytes(features[0].geometry().asWkb()), bytes(features[1].geometry().asWkb()))
        self.assertEqual([f['nspd_id'] for f in features], [101, 101])

    def test_source_id_field_does_not_overwrite_existing_attributes(self):
        data = feature_collection()
        data['features'][0]['properties']['nspd_id'] = 'server attribute'
        data['features'][0]['properties']['options']['nspd_id_2'] = 'nested attribute'
        prepared = layers.prepare_geojson(data)
        properties = prepared['features'][0]['properties']
        self.assertEqual(properties['nspd_id'], 'server attribute')
        self.assertEqual(properties['nspd_id_2'], 'nested attribute')
        self.assertEqual(properties['nspd_id_3'], 101)
        self.assertEqual(data['features'][0]['id'], 101)

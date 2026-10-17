# -*- coding: utf-8 -*-
"""Normalize GeoJSON, process its geometry and publish a QGIS layer."""
import copy
import json

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsProject, QgsVectorLayer,
)

from .feature_dedup import deduplicate_features
from .logger import log_info, log_error, log_success


def prepare_geojson(geojson_data):
    """Deduplicate raw records, then normalize a copy for OGR."""
    features = geojson_data.get('features', [])
    unique = deduplicate_features(features)
    result = copy.deepcopy(dict(geojson_data, features=unique))
    removed = len(features) - len(unique)
    if removed:
        log_info('Удалено точных повторов объектов НСПД: {}'.format(removed))
    for feature in result.get('features', []):
        properties = feature.get('properties') or {}
        feature['properties'] = properties
        options = properties.pop('options', {})
        if isinstance(options, dict):
            for key, value in options.items():
                target = key if key not in properties else 'options_' + key
                properties[target] = value
    # OGR treats top-level numeric IDs as FIDs and may renumber repeated IDs.
    # Preserve the server ID as an ordinary attribute on every retained variant.
    fields = {name for feature in result['features'] for name in feature['properties']}
    id_field = 'nspd_id'
    suffix = 2
    while id_field in fields:
        id_field = 'nspd_id_{}'.format(suffix)
        suffix += 1
    for feature in result['features']:
        if 'id' in feature:
            feature['properties'][id_field] = feature.pop('id')
    return result


def load_geojson_layer(geojson_data):
    """Read inline GeoJSON through OGR; no temporary file or persistent handle."""
    options = QgsVectorLayer.LayerOptions()
    if hasattr(options, 'forceReadOnly'):
        options.forceReadOnly = True
    source = QgsVectorLayer(json.dumps(geojson_data, ensure_ascii=False),
                           'Временные_Результаты', 'ogr', options)
    if not source.isValid():
        raise ValueError('Не удалось загрузить слой из GeoJSON (невалидный слой).')
    source.setCrs(QgsCoordinateReferenceSystem('EPSG:3857'), True)
    return source


def process_result_layer(layer, crs_reproject=None, crs_assign=None, round_mm=False):
    """Preserve the existing MSK chain and the 0.01 coordinate grid exactly."""
    import processing

    # Keep an editable memory result even when no coordinate operations run.
    # Geometric coincidence alone must never discard distinct NSPD records.
    current = layer.materialize(QgsFeatureRequest())
    if crs_reproject and crs_reproject.isValid():
        log_info('Перепроецирование слоя в {}'.format(crs_reproject.description()))
        current = processing.run('native:reprojectlayer', {
            'INPUT': current, 'TARGET_CRS': crs_reproject, 'OUTPUT': 'memory:',
        })['OUTPUT']
    if round_mm:
        current = processing.run('native:snappointstogrid', {
            'INPUT': current, 'HSPACING': 0.01, 'VSPACING': 0.01,
            'ZSPACING': 0, 'MSPACING': 0, 'OUTPUT': 'memory:',
        })['OUTPUT']
    if crs_assign and crs_assign.isValid():
        log_info('Назначение проекции слою: {}'.format(crs_assign.description()))
        current = processing.run('native:assignprojection', {
            'INPUT': current, 'CRS': crs_assign, 'OUTPUT': 'memory:',
        })['OUTPUT']
    return current


def add_result_layer(geojson_data, crs_reproject=None, crs_assign=None, round_mm=False,
                     prefix=None, incomplete=False):
    label = '{} - Результаты запроса НСПД'.format(prefix) if prefix else 'Результаты запроса НСПД'
    if incomplete:
        label += ' (неполная загрузка)'
    try:
        if not geojson_data.get('features'):
            return False, 'Слой пуст (API не вернул объектов для данного запроса).'
        source = load_geojson_layer(prepare_geojson(geojson_data))
        layer = process_result_layer(source, crs_reproject, crs_assign, round_mm)
        if not layer or not layer.isValid():
            raise ValueError('Ошибка в цепочке обработки геометрий.')
        layer.setName(label)
        layer.setCustomProperty('nspd_fetcher/incomplete', bool(incomplete))
        QgsProject.instance().addMapLayer(layer)
        log_success('Слой «{}» ({} объектов) добавлен на карту QGIS.'.format(label, layer.featureCount()))
        return True, None
    except Exception as error:
        log_error('Ошибка создания слоя «{}»'.format(label), exc_info=error)
        return False, 'Произошла ошибка при создании слоя:\n{}'.format(error)

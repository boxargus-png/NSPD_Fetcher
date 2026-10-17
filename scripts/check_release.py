"""Smoke-check a built ZIP with QGIS Python in a new isolated offscreen profile."""
import argparse
import hashlib
import importlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_archive(archive):
    from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem,
                           QgsCoordinateTransform, QgsPointXY, QgsProject)
    from qgis.analysis import QgsNativeAlgorithms
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import QCoreApplication, QEvent, QTimer
    from qgis.PyQt.QtWidgets import QMainWindow

    require(34000 <= Qgis.QGIS_VERSION_INT < 40000, "Use QGIS 3.40+ from the 3.x series")
    base = ROOT / "build" / "release-checks"
    base.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="qgis-{}-".format(Qgis.QGIS_VERSION_INT), dir=base))
    profile = run / "profile"
    plugins = profile / "python" / "plugins"
    plugins.mkdir(parents=True)
    with ZipFile(archive) as bundle:
        require(bundle.testzip() is None, "Corrupt ZIP")
        names = bundle.namelist()
        require(len(names) == len(set(names)), "Duplicate ZIP entries")
        roots = set()
        for name in names:
            path = PurePosixPath(name)
            require(not path.is_absolute() and ".." not in path.parts
                    and "\\" not in name and ":" not in name and len(path.parts) >= 2,
                    "Unsafe ZIP entry: " + name)
            roots.add(path.parts[0])
        require(len(roots) == 1, "ZIP must contain one plugin directory")
        package_name = roots.pop()
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package_name), "Invalid plugin directory")
        for required in ("metadata.txt", "__init__.py", "LICENSE"):
            require(package_name + "/" + required in names, "Missing " + required)
        bundle.extractall(plugins)

    os.environ["QGIS_AUTH_DB_DIR_PATH"] = str(profile)
    app = QgsApplication([], True, str(profile))
    app.initQgis()
    sys.path.insert(0, str(plugins))
    sys.path.append(str(Path(QgsApplication.prefixPath()) / "python" / "plugins"))
    app.processingRegistry().addProvider(QgsNativeAlgorithms())
    import qgis.utils as utils

    window = QMainWindow()
    canvas = QgsMapCanvas(window)

    class Interface:
        def __init__(self):
            self.menu_actions = []
            self.toolbar_actions = []

        def mainWindow(self):
            return window

        def mapCanvas(self):
            return canvas

        def addPluginToMenu(self, menu, action):
            self.menu_actions.append(action)

        def removePluginMenu(self, menu, action):
            self.menu_actions.remove(action)

        def addToolBarIcon(self, action):
            self.toolbar_actions.append(action)

        def removeToolBarIcon(self, action):
            self.toolbar_actions.remove(action)

    def fail_on_plugin_error(exc_type, value, trace, *args, **kwargs):
        raise RuntimeError("QGIS plugin lifecycle failed") from value

    iface = Interface()
    utils.iface = iface
    utils.showException = fail_on_plugin_error
    utils.plugin_paths = [str(plugins)]
    utils.updateAvailablePlugins()
    require(package_name in utils.available_plugins, "QGIS did not discover the installed plugin")
    checked = []
    try:
        for cycle in range(2):
            require(utils.loadPlugin(package_name), "loadPlugin failed")
            require(utils.startPlugin(package_name), "startPlugin failed")
            plugin = utils.plugins[package_name]
            require(len(iface.menu_actions) == len(iface.toolbar_actions) == 1,
                    "Missing or duplicate menu/toolbar action")
            require(not plugin.actions[0].icon().isNull(), "Missing plugin icon")
            # Run the real modal entry point, closing only after its event loop starts.
            QTimer.singleShot(0, lambda: plugin.dlg.reject())
            plugin.run()
            require(plugin.dlg.windowTitle() == "Загрузка данных НСПД", "Unexpected dialog")
            require(hasattr(plugin.dlg, "groupTaskProgress"), "Missing progress UI")
            for module_name, module in list(sys.modules.items()):
                if module_name == package_name or module_name.startswith(package_name + "."):
                    require((plugins / package_name).resolve() in Path(module.__file__).resolve().parents,
                            "Plugin module loaded from outside archive: " + module_name)
            if cycle == 0:
                layers = importlib.import_module(package_name + ".layer_manager")
                msk = importlib.import_module(package_name + ".msk_transform")
                reference = json.loads((ROOT / "tests" / "fixtures" /
                                        "msk52_52_18_0050232_698.json").read_text(encoding="utf-8"))
                target = QgsCoordinateReferenceSystem()
                require(target.createFromProj(reference["target_proj"]), "Invalid reference CRS")
                source = layers.load_geojson_layer({"type": "FeatureCollection", "features": [{
                    "type": "Feature", "properties": {}, "geometry": reference["nspd_geometry"],
                }]})
                result = layers.process_result_layer(source, msk.make_gost2001_crs(target), target, True)
                require(result.featureCount() == 1, "Reference feature count changed")
                feature = next(result.getFeatures())
                geometry = feature.geometry()
                vertices = [(p.x(), p.y()) for p in geometry.vertices()]
                require(len(vertices) == 5 and vertices[0] == vertices[-1], "Reference topology changed")
                expected = reference["expected_ring_easting_northing"][:-1]
                error = min(max(math.dist(vertices[(start + direction * i) % 4], point)
                                for i, point in enumerate(expected))
                            for start in range(4) for direction in (1, -1))
                require(error < 1e-7, "MSK reference coordinates changed: {} m".format(error))
                # Processing can serialize a custom CRS with different metadata.
                # Compare its mapping, as in the full reference regression test.
                web = QgsCoordinateReferenceSystem("EPSG:3857")
                actual_transform = QgsCoordinateTransform(result.crs(), web, QgsProject.instance())
                target_transform = QgsCoordinateTransform(target, web, QgsProject.instance())
                for coordinates in expected:
                    point = QgsPointXY(*coordinates)
                    actual_point = actual_transform.transform(point)
                    target_point = target_transform.transform(point)
                    require(math.hypot(actual_point.x() - target_point.x(),
                                       actual_point.y() - target_point.y()) < 1e-6,
                            "Output CRS mapping changed")
                del result, source
                checked.append("reference parcel 52:18:0050232:698, maximum residual {} m".format(error))
            require(utils.unloadPlugin(package_name), "unloadPlugin failed")
            require(not iface.menu_actions and not iface.toolbar_actions, "Actions survived unload")
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            checked.append("load, initGui, open dialog and unload: cycle {}".format(cycle + 1))
        report = {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "qgis_version": Qgis.QGIS_VERSION, "status": "passed", "checks": checked,
                  "profile": str(profile)}
        report_path = run / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("Report: " + str(report_path))
    finally:
        if package_name in utils.active_plugins:
            utils.unloadPlugin(package_name)
        QgsProject.instance().clear()
        canvas.stopRendering()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        # QGIS exits with this process; avoid destroying its provider registry
        # before remaining Python wrappers and Qt objects have been released.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    check_archive(args.archive.resolve())


if __name__ == "__main__":
    main()

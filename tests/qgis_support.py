"""One offscreen QGIS application shared by integration tests."""
import os
import sys
import unittest
from pathlib import Path

from tests.support import ROOT

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from qgis.core import Qgis, QgsApplication
except ImportError as exc:
    raise unittest.SkipTest("Run with QGIS Python to enable integration tests") from exc

APP = QgsApplication.instance()
if APP is None:
    profile = ROOT / "build" / "test-profiles" / str(Qgis.QGIS_VERSION_INT)
    profile.mkdir(parents=True, exist_ok=True)
    os.environ["QGIS_AUTH_DB_DIR_PATH"] = str(profile)
    APP = QgsApplication([], True, str(profile))
    APP.initQgis()
sys.path.append(str(Path(QgsApplication.prefixPath()) / "python" / "plugins"))
from qgis.analysis import QgsNativeAlgorithms

# Only the native provider used by the plugin is needed. Loading every optional
# provider also loads unrelated GRASS/GDAL installations and user scripts.
if QgsApplication.processingRegistry().providerById("native") is None:
    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

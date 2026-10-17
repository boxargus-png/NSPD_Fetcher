"""A failing log destination must not prevent other destinations from working."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


class LoggerFallbackTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "nspd_logger_under_test", Path(__file__).resolve().parents[1] / "logger.py")
        self.logger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.logger)
        temporary_root = Path(__file__).resolve().parents[1] / "build" / "logger-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.logger.LOG_FILE_PATH = str(Path(self.temp.name) / "plugin.log")
        self.qgis_log = Mock()
        core = ModuleType("qgis.core")
        core.QgsMessageLog = SimpleNamespace(logMessage=self.qgis_log)
        core.Qgis = SimpleNamespace(Info=0, Success=3, Warning=1, Critical=2)
        self.modules = patch.dict(sys.modules, {"qgis.core": core})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_unwritable_file_does_not_lose_message_or_stop_qgis_logging(self):
        with patch("builtins.open", side_effect=PermissionError("read-only")):
            with self.assertLogs(self.logger.TAG, level="INFO") as captured:
                self.logger.log_info("download finished")
        self.qgis_log.assert_called_once_with("download finished", self.logger.TAG, level=0)
        self.assertTrue(any("Could not write" in line for line in captured.output))
        self.assertIn("INFO:NSPD_Fetcher:download finished", captured.output)

    def test_failed_qgis_log_keeps_file_and_python_messages_for_all_levels(self):
        self.qgis_log.side_effect = RuntimeError("QGIS is shutting down")
        with self.assertLogs(self.logger.TAG, level="DEBUG") as captured:
            for name in ("info", "success", "warning", "error", "debug"):
                getattr(self.logger, "log_" + name)("message " + name)
        contents = Path(self.logger.LOG_FILE_PATH).read_text(encoding="utf-8")
        for name in ("info", "success", "warning", "error", "debug"):
            self.assertIn("message " + name, contents)
            self.assertTrue(any("message " + name in line for line in captured.output))
        self.assertTrue(any("QGIS message log unavailable" in line for line in captured.output))

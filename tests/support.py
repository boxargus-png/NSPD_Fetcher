"""Load the checkout as a package, independent of its directory name."""
import importlib
import importlib.util
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "nspd_fetcher_under_test"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)


def plugin_module(name):
    return importlib.import_module("{}.{}".format(PACKAGE, name))


# Simulated failures must not be appended to the user's real plugin log.
_plugin_logger = plugin_module("logger")
_plugin_logger._write_to_file = lambda *args: None
_test_logger = logging.getLogger(_plugin_logger.TAG)
_test_logger.addHandler(logging.NullHandler())
_test_logger.propagate = False

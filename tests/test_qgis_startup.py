import unittest
from unittest.mock import Mock, patch

from tests.qgis_support import APP
from tests.support import plugin_module

entry = plugin_module("NSPD_Fetcher")


class StartupTests(unittest.TestCase):
    def test_missing_or_invalid_locale_uses_system_locale(self):
        for value in (None, "", "  ", 123):
            with self.subTest(value=value), patch.object(entry, "QSettings") as settings, \
                    patch.object(entry, "QLocale") as locale, \
                    patch.object(entry.os.path, "exists", return_value=False) as exists:
                settings.return_value.value.return_value = value
                locale.system.return_value.name.return_value = "ru_RU"
                plugin = entry.NSPD_Fetcher(Mock())
                self.assertTrue(exists.call_args.args[0].endswith("NSPD_Fetcher_ru.qm"))
                self.assertIsNone(plugin.translator)
                plugin.unload()

    def test_configured_locale_is_used_and_translator_removed_on_unload(self):
        with patch.object(entry, "QSettings") as settings, \
                patch.object(entry.os.path, "exists", return_value=True) as exists, \
                patch.object(entry, "QTranslator") as translator, \
                patch.object(entry.QCoreApplication, "installTranslator") as install, \
                patch.object(entry.QCoreApplication, "removeTranslator") as remove:
            settings.return_value.value.return_value = "en_US"
            translator.return_value.load.return_value = True
            plugin = entry.NSPD_Fetcher(Mock())
            self.assertTrue(exists.call_args.args[0].endswith("NSPD_Fetcher_en.qm"))
            install.assert_called_once_with(translator.return_value)
            plugin.unload()
            plugin.unload()
            remove.assert_called_once_with(translator.return_value)

    def test_dialog_can_be_retried_after_constructor_failure(self):
        with patch.object(entry, "QSettings") as settings, \
                patch.object(entry, "NSPD_FetcherDialog") as dialog_class:
            settings.return_value.value.return_value = "ru_RU"
            plugin = entry.NSPD_Fetcher(Mock())
            plugin.first_start = True
            dialog = Mock()
            dialog_class.side_effect = [RuntimeError("initialization failed"), dialog]
            with self.assertRaises(RuntimeError):
                plugin.run()
            self.assertTrue(plugin.first_start)
            plugin.run()
            self.assertFalse(plugin.first_start)
            plugin.run()
            self.assertEqual(dialog_class.call_count, 2)
            self.assertEqual(dialog.exec_.call_count, 2)
            plugin.unload()

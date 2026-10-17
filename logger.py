# -*- coding: utf-8 -*-
"""
Модуль логирования для плагина NSPD_Fetcher.
Записывает сообщения в панель сообщений QGIS (QgsMessageLog)
и в локальный файл журнала nspd_fetcher.log.
"""

import os
import logging
import tempfile
import datetime
import traceback

TAG = "NSPD_Fetcher"
LOG_FILENAME = "nspd_fetcher.log"

def _resolve_log_path():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        test_file = os.path.join(plugin_dir, ".test_write")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return os.path.join(plugin_dir, LOG_FILENAME)
    except Exception:
        return os.path.join(tempfile.gettempdir(), LOG_FILENAME)

LOG_FILE_PATH = _resolve_log_path()

def get_log_file_path():
    """Возвращает путь к текущему файлу журнала."""
    return LOG_FILE_PATH

def _write_to_file(level_str, message):
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] [{level_str}] {message}\n")
    except Exception:
        pass

def log_info(message):
    """Информационное сообщение."""
    _write_to_file("INFO", message)
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(str(message), TAG, level=Qgis.Info)
    except Exception:
        pass
    logging.getLogger(TAG).info(str(message))

def log_success(message):
    """Сообщение об успехе."""
    _write_to_file("SUCCESS", message)
    try:
        from qgis.core import QgsMessageLog, Qgis
        level = getattr(Qgis, "Success", Qgis.Info)
        QgsMessageLog.logMessage(str(message), TAG, level=level)
    except Exception:
        pass
    logging.getLogger(TAG).info(f"[SUCCESS] {message}")

def log_warning(message):
    """Предупреждение."""
    _write_to_file("WARNING", message)
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(str(message), TAG, level=Qgis.Warning)
    except Exception:
        pass
    logging.getLogger(TAG).warning(str(message))

def log_error(message, exc_info=None):
    """Сообщение об ошибке, с возможностью вывода трассировки исключения."""
    full_msg = str(message)
    if exc_info:
        if isinstance(exc_info, Exception):
            full_msg += "\n" + "".join(traceback.format_exception(
                type(exc_info), exc_info, exc_info.__traceback__))
        else:
            full_msg += f"\n{traceback.format_exc()}"
    
    _write_to_file("ERROR", full_msg)
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(full_msg, TAG, level=Qgis.Critical)
    except Exception:
        pass
    logging.getLogger(TAG).error(full_msg)

def log_debug(message):
    """Отладочное сообщение."""
    _write_to_file("DEBUG", message)
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(str(message), TAG, level=Qgis.Info)
    except Exception:
        pass
    logging.getLogger(TAG).debug(str(message))

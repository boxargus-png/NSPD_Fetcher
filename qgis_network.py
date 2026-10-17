"""Synchronous networking for a QgsTask, using QGIS proxy/TLS/timeout settings."""
from qgis.core import Qgis, QgsApplication, QgsBlockingNetworkRequest, QgsFeedback, QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from .api_client import NetworkResponse, NspdApiError

_preprocessor_id = None
_USER_AGENT_ATTRIBUTE = "nspd_fetcher/user_agent"


def _preserve_user_agent(request):
    # QGIS replaces User-Agent in createRequest(). Restore the existing NSPD
    # header only on our tagged requests, without changing other plugins or
    # the user's global network settings.
    options = request.attribute(QNetworkRequest.User)
    if isinstance(options, dict) and _USER_AGENT_ATTRIBUTE in options:
        request.setRawHeader(b"User-Agent", options[_USER_AGENT_ATTRIBUTE].encode("utf-8"))
    # QGIS 3.x Python bindings convert the ignored callback result to QString.
    return ""


def prepare_network():
    """Register once on the GUI thread, before any NSPD worker is submitted."""
    global _preprocessor_id
    # Older Python bindings can crash while registering the callback (QGIS
    # issue #55329). Reject them before calling into C++, even if the plugin
    # was copied manually instead of installed through the plugin manager.
    if Qgis.QGIS_VERSION_INT < 34000:
        raise RuntimeError("Для загрузки НСПД требуется QGIS 3.40 или новее: "
                           "в старых версиях есть ошибка сетевого API QGIS.")
    if _preprocessor_id is None:
        if QThread.currentThread() != QgsApplication.instance().thread():
            raise RuntimeError("Сетевой клиент НСПД должен быть подготовлен в потоке интерфейса")
        _preprocessor_id = QgsNetworkAccessManager.setRequestPreprocessor(_preserve_user_agent)


def release_network():
    global _preprocessor_id
    if _preprocessor_id is not None:
        QgsNetworkAccessManager.removeRequestPreprocessor(_preprocessor_id)
        _preprocessor_id = None


class QgisNetworkTransport:
    def __init__(self, feedback=None):
        prepare_network()
        self.feedback = feedback if feedback is not None else QgsFeedback()

    def _check_canceled(self):
        if self.feedback.isCanceled():
            raise NspdApiError("Запрос НСПД отменён", "canceled")

    def request(self, method, url, headers=None, body=b""):
        self._check_canceled()
        request = QNetworkRequest(QUrl(url))
        for name, value in (headers or {}).items():
            request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))
        if headers and "User-Agent" in headers:
            request.setAttribute(QNetworkRequest.User,
                                 {_USER_AGENT_ATTRIBUTE: headers["User-Agent"]})

        # Construct in the worker thread. forceRefresh avoids stale cached
        # cadastral data; QGIS handles proxy, certificate trust and timeouts.
        blocking = QgsBlockingNetworkRequest()
        if method == "GET":
            error = blocking.get(request, True, self.feedback)
        elif method == "POST":
            error = blocking.post(request, QByteArray(body), True, self.feedback)
        else:
            raise ValueError("Unsupported NSPD request method: " + method)
        self._check_canceled()

        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        status = int(status) if status is not None else None
        body = bytes(reply.content())
        diagnostic = body[:4000].decode("utf-8", errors="replace")[:1000]
        detail = blocking.errorMessage() or reply.errorString()
        if error == QgsBlockingNetworkRequest.TimeoutError or reply.error() == QNetworkReply.TimeoutError:
            raise NspdApiError("Превышено время ожидания ответа НСПД", "timeout", status, diagnostic)
        if reply.error() == QNetworkReply.SslHandshakeFailedError:
            raise NspdApiError(
                "Ошибка TLS при подключении к НСПД. Проверьте сертификаты в настройках QGIS. " + detail,
                "tls", status, diagnostic)
        # Qt also reports HTTP 4xx/5xx as network errors. Preserve their status
        # and body so the client reports HTTP failures accurately.
        if status is not None and status != 200:
            return NetworkResponse(status, body)
        if error != QgsBlockingNetworkRequest.NoError or reply.error() != QNetworkReply.NoError or status is None:
            raise NspdApiError("Сетевая ошибка НСПД: " + detail, "network", status, diagnostic)
        return NetworkResponse(status, body)

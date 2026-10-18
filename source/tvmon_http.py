"""Persistent Windows WinHTTP transport; the downloader owns retries."""

from __future__ import annotations

import ctypes
import queue
import re
import threading
import time
from urllib.parse import urlsplit


# Values and signatures from Microsoft's Windows SDK winhttp.h.
# https://learn.microsoft.com/windows/win32/winhttp/option-flags
DWORD = ctypes.c_uint32
HINTERNET = ctypes.c_void_p
LPDWORD = ctypes.POINTER(DWORD)
WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY = 4
WINHTTP_FLAG_SECURE = 0x00800000
WINHTTP_FLAG_ASYNC = 0x10000000
WINHTTP_OPTION_CONTEXT_VALUE = 45
WINHTTP_OPTION_URL = 34
WINHTTP_OPTION_MAX_CONNS_PER_SERVER = 73
WINHTTP_OPTION_REDIRECT_POLICY = 88
WINHTTP_OPTION_ENABLE_HTTP_PROTOCOL = 133
WINHTTP_OPTION_HTTP_PROTOCOL_USED = 134
WINHTTP_OPTION_EXPIRE_CONNECTION = 143
WINHTTP_PROTOCOL_FLAG_HTTP2 = 1
WINHTTP_QUERY_CONTENT_LENGTH = 5
WINHTTP_QUERY_STATUS_CODE = 19
WINHTTP_QUERY_CUSTOM = 65535
WINHTTP_QUERY_FLAG_NUMBER = 0x20000000
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_WINHTTP_HEADER_NOT_FOUND = 12150
READ_SIZE = 65536
WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING = 0x00000800
WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE = 0x00020000
WINHTTP_CALLBACK_STATUS_READ_COMPLETE = 0x00080000
WINHTTP_CALLBACK_STATUS_REQUEST_ERROR = 0x00200000
WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE = 0x00400000


class _AsyncResult(ctypes.Structure):
    _fields_ = [("result", ctypes.c_size_t), ("error", DWORD)]


class TransferError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class HttpStatusError(TransferError):
    def __init__(self, status: int):
        super().__init__(f"영상 서버가 HTTP {status} 오류를 반환했습니다.")
        self.status = status


class BrowserChallengeError(TransferError):
    def __init__(self, url: str):
        super().__init__("사이트에서 브라우저 검증을 요청했습니다.")
        self.url = url


class TransferCancelled(Exception):
    pass


def _validate_url(url: str):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(char) < 32 for char in url)):
            raise ValueError()
        port = 443 if parsed.port is None else parsed.port
        if not 1 <= port <= 65535:
            raise ValueError()
        return parsed, port
    except ValueError:
        raise TransferError("서버에서 올바르지 않은 영상 주소를 받았습니다.") from None


def get_with_ech(url: str, headers: dict, cancel: threading.Event, limit: int,
                 on_bytes=None, *, on_wait=None) -> tuple[bytes, str]:
    """Retry a site request through bundled TLS/ECH, without starting a browser."""
    _validate_url(url)
    if cancel.is_set():
        raise TransferCancelled()
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    request_headers = []
    for name, value in headers.items():
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or any(char in value for char in "\r\n\0")):
            raise TransferError("요청 헤더가 올바르지 않습니다.")
        if name.lower() != "accept-encoding":
            request_headers.append(f"{name}: {value}".encode("utf-8"))
    request_headers.append(b"Accept-Encoding: identity")
    # Load the native library only when the normal site connection failed.
    import asyncio
    try:
        from curl_cffi import AsyncCurl, Curl, CurlError, CurlHttpVersion, CurlInfo, CurlOpt
    except (ImportError, OSError):
        raise TransferError("내장 사이트 접속 라이브러리를 불러오지 못했습니다.") from None

    chunks = []
    received = 0
    response_headers = {}

    def receive(data):
        nonlocal received
        if cancel.is_set():
            raise TransferCancelled()
        received += len(data)
        if received > limit:
            raise TransferError("서버 응답이 허용 크기를 초과했습니다.")
        chunks.append(data)
        if on_bytes is not None:
            on_bytes(len(data))
        return len(data)

    def header(data):
        nonlocal received
        if cancel.is_set():
            raise TransferCancelled()
        if data.startswith(b"HTTP/"):
            response_headers.clear()
            chunks.clear()
            received = 0
        elif b":" in data:
            name, value = data.split(b":", 1)
            response_headers[name.strip().lower()] = value.strip()
        return len(data)

    curl = None
    loop = None
    try:
        curl = Curl()
        curl.setopt(CurlOpt.URL, url)
        curl.setopt(CurlOpt.HTTPHEADER, request_headers)
        curl.setopt(CurlOpt.PROXY, "")
        curl.setopt(CurlOpt.HTTP_VERSION, CurlHttpVersion.V2TLS)
        curl.setopt(CurlOpt.PROTOCOLS_STR, "https")
        curl.setopt(CurlOpt.REDIR_PROTOCOLS_STR, "https")
        curl.setopt(CurlOpt.FOLLOWLOCATION, 1)
        curl.setopt(CurlOpt.MAXREDIRS, 5)
        curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, 10000)
        curl.setopt(CurlOpt.TIMEOUT_MS, 20000)
        curl.setopt(CurlOpt.MAXFILESIZE_LARGE, limit)
        curl.setopt(CurlOpt.SSL_VERIFYPEER, 1)
        curl.setopt(CurlOpt.SSL_VERIFYHOST, 2)
        curl.setopt(CurlOpt.DOH_SSL_VERIFYPEER, 1)
        curl.setopt(CurlOpt.DOH_SSL_VERIFYHOST, 2)
        curl.setopt(CurlOpt.DOH_URL, "https://cloudflare-dns.com/dns-query")
        curl.setopt(CurlOpt.ECH, "true")
        curl.setopt(CurlOpt.WRITEFUNCTION, receive)
        curl.setopt(CurlOpt.HEADERFUNCTION, header)
        # A separate selector loop keeps cancellation responsive in Tk workers
        # without changing the application's event-loop policy or other requests.
        loop = asyncio.SelectorEventLoop()

        async def transfer():
            multi = AsyncCurl(loop=loop)
            try:
                completion = multi.add_handle(curl)
                while not completion.done():
                    if cancel.is_set():
                        raise TransferCancelled()
                    if on_wait is not None:
                        on_wait()
                    await asyncio.wait((completion,), timeout=.1)
                await completion
            finally:
                await multi.close()

        loop.run_until_complete(transfer())
        if cancel.is_set():
            raise TransferCancelled()
        final_url = curl.getinfo(CurlInfo.EFFECTIVE_URL).decode("utf-8")
        _validate_url(final_url)
        if response_headers.get(b"cf-mitigated", b"").lower() == b"challenge":
            raise BrowserChallengeError(final_url)
        status = curl.getinfo(CurlInfo.RESPONSE_CODE)
        if not 200 <= status < 300:
            raise HttpStatusError(status)
        return b"".join(chunks), final_url
    except CurlError as error:
        if cancel.is_set():
            raise TransferCancelled() from None
        if error.code == 63:
            raise TransferError("서버 응답이 허용 크기를 초과했습니다.") from None
        raise TransferError(f"내장 사이트 접속에 실패했습니다 (라이브러리 오류 {int(error.code)}).") from None
    finally:
        if loop is not None:
            loop.close()
        if curl is not None:
            curl.close()


class WinHttpClient:
    """One native session shared by workers, with a distinct handle per GET."""

    def __init__(self, user_agent: str):
        try:
            api = ctypes.WinDLL("winhttp.dll", use_last_error=True)
        except (AttributeError, OSError):
            raise TransferError("이 다운로드 프로그램은 Windows 10 이상에서 실행해 주세요.") from None
        self._api = api
        api.WinHttpOpen.argtypes = [ctypes.c_wchar_p, DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD]
        api.WinHttpOpen.restype = HINTERNET
        api.WinHttpConnect.argtypes = [HINTERNET, ctypes.c_wchar_p, ctypes.c_ushort, DWORD]
        api.WinHttpConnect.restype = HINTERNET
        api.WinHttpOpenRequest.argtypes = [HINTERNET, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                          ctypes.c_wchar_p, ctypes.c_wchar_p,
                                          ctypes.POINTER(ctypes.c_wchar_p), DWORD]
        api.WinHttpOpenRequest.restype = HINTERNET
        api.WinHttpSendRequest.argtypes = [HINTERNET, ctypes.c_wchar_p, DWORD, ctypes.c_void_p,
                                          DWORD, DWORD, ctypes.c_size_t]
        api.WinHttpSendRequest.restype = ctypes.c_int
        api.WinHttpReceiveResponse.argtypes = [HINTERNET, ctypes.c_void_p]
        api.WinHttpReceiveResponse.restype = ctypes.c_int
        api.WinHttpQueryHeaders.argtypes = [HINTERNET, DWORD, ctypes.c_wchar_p,
                                           ctypes.c_void_p, LPDWORD, LPDWORD]
        api.WinHttpQueryHeaders.restype = ctypes.c_int
        api.WinHttpReadData.argtypes = [HINTERNET, ctypes.c_void_p, DWORD, LPDWORD]
        api.WinHttpReadData.restype = ctypes.c_int
        api.WinHttpSetTimeouts.argtypes = [HINTERNET, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        api.WinHttpSetTimeouts.restype = ctypes.c_int
        api.WinHttpSetOption.argtypes = [HINTERNET, DWORD, ctypes.c_void_p, DWORD]
        api.WinHttpSetOption.restype = ctypes.c_int
        api.WinHttpQueryOption.argtypes = [HINTERNET, DWORD, ctypes.c_void_p, LPDWORD]
        api.WinHttpQueryOption.restype = ctypes.c_int
        api.WinHttpCloseHandle.argtypes = [HINTERNET]
        api.WinHttpCloseHandle.restype = ctypes.c_int
        callback_type = ctypes.WINFUNCTYPE(None, HINTERNET, ctypes.c_size_t, DWORD, ctypes.c_void_p, DWORD)
        api.WinHttpSetStatusCallback.argtypes = [HINTERNET, callback_type, DWORD, ctypes.c_size_t]
        api.WinHttpSetStatusCallback.restype = ctypes.c_void_p
        self._lock = threading.Condition()
        self._contexts = {}
        self._sequence = 0
        self._callback = callback_type(self._status_changed)
        self._connections = {}
        self._active_addresses = {}
        self._active = 0
        self._closing = False
        self.protocols_used = set()
        self._session = api.WinHttpOpen(user_agent, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, None, None, WINHTTP_FLAG_ASYNC)
        self._check(self._session)
        try:
            self._check(api.WinHttpSetTimeouts(self._session, 12000, 12000, 12000, 12000))
            self._set_option(WINHTTP_OPTION_MAX_CONNS_PER_SERVER, 8)
            # Reject HTTPS -> HTTP redirects; TLS verification remains WinHTTP's default.
            self._set_option(WINHTTP_OPTION_REDIRECT_POLICY, 1)
            protocol = DWORD(WINHTTP_PROTOCOL_FLAG_HTTP2)
            enabled = api.WinHttpSetOption(
                self._session, WINHTTP_OPTION_ENABLE_HTTP_PROTOCOL, ctypes.byref(protocol), ctypes.sizeof(protocol))
            if not enabled and ctypes.get_last_error() == 12009:
                raise TransferError("이 Windows 버전에서는 HTTP/2 다운로드를 지원하지 않습니다. Windows 10 이상을 사용해 주세요.", 12009)
            self._check(enabled)
            self.http2_enabled = True
        except BaseException:
            api.WinHttpCloseHandle(self._session)
            self._session = None
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closing = True
            while self._active:
                self._lock.wait()
            if self._session is None:
                return
            for handle in self._connections.values():
                self._api.WinHttpCloseHandle(handle)
            self._connections.clear()
            self._api.WinHttpCloseHandle(self._session)
            self._session = None

    def _check(self, result, cancel: threading.Event | None = None) -> None:
        if not result:
            code = ctypes.get_last_error()
            if cancel is not None and cancel.is_set():
                raise TransferCancelled()
            raise TransferError("영상 서버 연결이 끊겼거나 응답이 지연됩니다.", code) from None

    def _set_option(self, option: int, value: int) -> None:
        data = DWORD(value)
        self._check(self._api.WinHttpSetOption(self._session, option, ctypes.byref(data), ctypes.sizeof(data)))

    def _status_changed(self, request, context, status, information, length):
        # Native callbacks only copy completion data; request logic stays in its worker.
        with self._lock:
            state = self._contexts.get(context)
        if state is None:
            return
        if status == WINHTTP_CALLBACK_STATUS_REQUEST_ERROR:
            error = (ctypes.cast(information, ctypes.POINTER(_AsyncResult)).contents.error
                     if information and length >= ctypes.sizeof(_AsyncResult) else 87)
            state["events"].put((status, error))
        elif status in (WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE, WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE,
                        WINHTTP_CALLBACK_STATUS_READ_COMPLETE):
            state["events"].put((status, length))
        elif status == WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING:
            with self._lock:
                self._contexts.pop(context, None)
            state["events"].put((status, 0))
            state["closed"].set()

    def _content_length(self, request) -> int | None:
        size = DWORD()
        result = self._api.WinHttpQueryHeaders(request, WINHTTP_QUERY_CONTENT_LENGTH, None, None,
                                               ctypes.byref(size), None)
        error = ctypes.get_last_error()
        if not result and error == ERROR_WINHTTP_HEADER_NOT_FOUND:
            return None
        if not result and error != ERROR_INSUFFICIENT_BUFFER:
            self._check(False)
        if not size.value or size.value > 65536:
            raise TransferError("서버 응답의 크기 정보가 올바르지 않습니다.")
        buffer = ctypes.create_unicode_buffer(size.value // ctypes.sizeof(ctypes.c_wchar) + 1)
        size = DWORD(ctypes.sizeof(buffer))
        self._check(self._api.WinHttpQueryHeaders(request, WINHTTP_QUERY_CONTENT_LENGTH, None, buffer,
                                                  ctypes.byref(size), None))
        if not re.fullmatch(r"[0-9]+", buffer.value.strip()):
            raise TransferError("서버 응답의 크기 정보가 올바르지 않습니다.")
        try:
            return int(buffer.value.strip())
        except ValueError:
            raise TransferError("서버 응답의 크기 정보가 올바르지 않습니다.") from None

    def _requires_browser(self, request) -> bool:
        size = DWORD()
        result = self._api.WinHttpQueryHeaders(request, WINHTTP_QUERY_CUSTOM, "cf-mitigated", None,
                                               ctypes.byref(size), None)
        error = ctypes.get_last_error()
        if not result and error == ERROR_WINHTTP_HEADER_NOT_FOUND:
            return False
        if not result and error != ERROR_INSUFFICIENT_BUFFER:
            self._check(False)
        if not 0 < size.value <= 65536:
            raise TransferError("서버 응답 헤더의 크기가 올바르지 않습니다.")
        buffer = ctypes.create_unicode_buffer(size.value // ctypes.sizeof(ctypes.c_wchar) + 1)
        size = DWORD(ctypes.sizeof(buffer))
        self._check(self._api.WinHttpQueryHeaders(request, WINHTTP_QUERY_CUSTOM, "cf-mitigated", buffer,
                                                  ctypes.byref(size), None))
        return buffer.value.strip().lower() == "challenge"

    def _final_url(self, request) -> str:
        size = DWORD()
        result = self._api.WinHttpQueryOption(request, WINHTTP_OPTION_URL, None, ctypes.byref(size))
        if not result and ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            self._check(False)
        if not size.value or size.value > 1024 * 1024:
            raise TransferError("서버에서 올바르지 않은 영상 주소를 받았습니다.")
        buffer = ctypes.create_unicode_buffer(size.value // ctypes.sizeof(ctypes.c_wchar) + 1)
        size = DWORD(ctypes.sizeof(buffer))
        self._check(self._api.WinHttpQueryOption(request, WINHTTP_OPTION_URL, buffer, ctypes.byref(size)))
        _validate_url(buffer.value)
        return buffer.value

    def get(self, url: str, headers: dict, cancel: threading.Event, limit: int,
            on_bytes=None, *, on_wait=None) -> tuple[bytes, str]:
        parsed, port = _validate_url(url)
        if cancel.is_set():
            raise TransferCancelled()
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        request_headers = []
        for name, value in headers.items():
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or any(char in value for char in "\r\n\0")):
                raise TransferError("요청 헤더가 올바르지 않습니다.")
            if name.lower() != "accept-encoding":
                request_headers.append(f"{name}: {value}\r\n")
        request_headers.append("Accept-Encoding: identity\r\n")
        header_text = "".join(request_headers)
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        started = time.monotonic()
        retirement_attempted = False
        def check_transfer():
            if cancel.is_set():
                raise TransferCancelled()

        def wait_for(expected):
            while True:
                check_transfer()
                try:
                    status, value = state["events"].get(timeout=.1)
                except queue.Empty:
                    if on_wait is not None:
                        on_wait()
                    continue
                if status == WINHTTP_CALLBACK_STATUS_REQUEST_ERROR:
                    check_transfer()
                    raise TransferError("영상 서버 연결이 끊겼거나 응답이 지연됩니다.", value)
                if status == expected:
                    return value
                raise TransferError("영상 서버 요청이 완료되기 전에 연결이 종료되었습니다.")

        with self._lock:
            if self._closing or self._session is None:
                raise TransferError("다운로드 연결이 이미 닫혔습니다.")
            address = (parsed.hostname, port)
            connection = self._connections.get(address)
            if connection is None:
                connection = self._api.WinHttpConnect(self._session, parsed.hostname, port, 0)
                self._check(connection, cancel)
                self._connections[address] = connection
            request = self._api.WinHttpOpenRequest(connection, "GET", path, None, None, None, WINHTTP_FLAG_SECURE)
            self._check(request, cancel)
            self._active += 1
            self._active_addresses[address] = self._active_addresses.get(address, 0) + 1
            self._sequence += 1
            context = self._sequence
            state = {"events": queue.Queue(), "closed": threading.Event()}
            self._contexts[context] = state
        callback_set = False
        try:
            context_value = ctypes.c_size_t(context)
            self._check(self._api.WinHttpSetOption(request, WINHTTP_OPTION_CONTEXT_VALUE,
                        ctypes.byref(context_value), ctypes.sizeof(context_value)), cancel)
            flags = (0x00000C00 | WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE | WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE
                     | WINHTTP_CALLBACK_STATUS_READ_COMPLETE | WINHTTP_CALLBACK_STATUS_REQUEST_ERROR)
            previous = self._api.WinHttpSetStatusCallback(request, self._callback, flags, 0)
            self._check(previous != ctypes.c_void_p(-1).value, cancel)
            callback_set = True
            check_transfer()
            header_length = len(header_text.encode("utf-16-le")) // 2
            header_buffer = ctypes.create_unicode_buffer(header_text)
            self._check(self._api.WinHttpSendRequest(request, header_buffer, header_length, None, 0, 0, context), cancel)
            wait_for(WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE)
            check_transfer()
            self._check(self._api.WinHttpReceiveResponse(request, None), cancel)
            wait_for(WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE)
            check_transfer()
            status = DWORD()
            size = DWORD(ctypes.sizeof(status))
            self._check(self._api.WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                                      None, ctypes.byref(status), ctypes.byref(size), None), cancel)
            if self._requires_browser(request):
                raise BrowserChallengeError(self._final_url(request))
            if not 200 <= status.value < 300:
                raise HttpStatusError(status.value)
            final_url = self._final_url(request)
            expected = self._content_length(request)
            if expected is not None and expected > limit:
                raise TransferError("서버 응답이 허용 크기를 초과했습니다.")
            protocol = DWORD()
            size = DWORD(ctypes.sizeof(protocol))
            if self._api.WinHttpQueryOption(request, WINHTTP_OPTION_HTTP_PROTOCOL_USED, ctypes.byref(protocol), ctypes.byref(size)):
                with self._lock:
                    self.protocols_used.add("HTTP/2" if protocol.value & WINHTTP_PROTOCOL_FLAG_HTTP2 else "HTTP/1.1")
            buffer = ctypes.create_string_buffer(READ_SIZE)
            chunks = []
            received = 0
            while True:
                check_transfer()
                # Async reads require a NULL byte-count pointer. Keep buffer alive through HANDLE_CLOSING.
                self._check(self._api.WinHttpReadData(request, buffer, READ_SIZE, None), cancel)
                count = wait_for(WINHTTP_CALLBACK_STATUS_READ_COMPLETE)
                check_transfer()
                if not 0 <= count <= READ_SIZE:
                    raise TransferError("서버 응답 조각의 크기가 올바르지 않습니다.")
                if not count:
                    break
                if not retirement_attempted and time.monotonic() - started >= 12:
                    with self._lock:
                        # Finish this body, then renew a slow pooled connection.
                        # Other requests on the same server must finish undisturbed.
                        if self._active_addresses[address] == 1:
                            self._api.WinHttpSetOption(request, WINHTTP_OPTION_EXPIRE_CONNECTION, None, 0)
                            retirement_attempted = True
                received += count
                if received > limit:
                    raise TransferError("서버 응답이 허용 크기를 초과했습니다.")
                chunks.append(buffer.raw[:count])
                if on_bytes is not None:
                    on_bytes(count)
            if expected is not None and received != expected:
                raise TransferError("서버 응답을 모두 받지 못했습니다.")
            check_transfer()
            return b"".join(chunks), final_url
        finally:
            self._api.WinHttpCloseHandle(request)
            if callback_set:
                # Closing an async request cancels pending I/O. Its final callback owns native teardown.
                state["closed"].wait()
            with self._lock:
                self._contexts.pop(context, None)
                self._active -= 1
                self._active_addresses[address] -= 1
                self._lock.notify_all()

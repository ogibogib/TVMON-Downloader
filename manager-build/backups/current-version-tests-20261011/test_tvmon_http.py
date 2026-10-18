"""Native-call fixtures verify transfer contracts without external networking."""

import concurrent.futures
import asyncio
import ctypes
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import tvmon_http as http


URL = "https://media.example/segment.gif?token=fixture"


def dword(pointer):
    return ctypes.cast(pointer, ctypes.POINTER(http.DWORD)).contents


class NativeFixture:
    def __init__(self, payload=b"abc", length="3", status=200, final_url=URL):
        self.payload = payload
        self.length = length
        self.status = status
        self.mitigated = None
        self.final_url = final_url
        self.closed = []
        self.connections = []
        self.requests = {}
        self.callbacks = {}
        self.contexts = {}
        self.next_handle = 2 ** 40
        self.fail_receive = False
        self.fail_timeouts = False
        self.http2_supported = True
        self.barrier = None
        self.lock = threading.Lock()
        self.WinHttpOpen = Mock(return_value=self.next_handle)
        self.WinHttpConnect = Mock(side_effect=self.connect)
        self.WinHttpOpenRequest = Mock(side_effect=self.open_request)
        self.WinHttpSetTimeouts = Mock(side_effect=self.set_timeouts)
        self.WinHttpSetOption = Mock(side_effect=self.set_option)
        self.WinHttpSetStatusCallback = Mock(side_effect=self.set_callback)
        self.WinHttpSendRequest = Mock(side_effect=self.send)
        self.WinHttpReceiveResponse = Mock(side_effect=self.receive)
        self.WinHttpQueryHeaders = Mock(side_effect=self.query_headers)
        self.WinHttpQueryOption = Mock(side_effect=self.query_option)
        self.WinHttpReadData = Mock(side_effect=self.read)
        self.WinHttpCloseHandle = Mock(side_effect=self.close)

    def connect(self, session, hostname, port, reserved):
        with self.lock:
            self.next_handle += 1
            self.connections.append(self.next_handle)
            return self.next_handle

    def open_request(self, *args):
        with self.lock:
            self.next_handle += 1
            self.requests[self.next_handle] = 0
            return self.next_handle

    def set_timeouts(self, *args):
        if self.fail_timeouts:
            ctypes.set_last_error(87)
            return 0
        return 1

    def set_option(self, handle, option, value, size):
        if option == http.WINHTTP_OPTION_ENABLE_HTTP_PROTOCOL and not self.http2_supported:
            ctypes.set_last_error(12009)
            return 0
        if option == http.WINHTTP_OPTION_CONTEXT_VALUE:
            self.contexts[handle] = ctypes.cast(value, ctypes.POINTER(ctypes.c_size_t)).contents.value
        return 1

    def set_callback(self, request, callback, flags, reserved):
        self.callbacks[request] = callback
        return None

    def notify(self, request, status, information=None, length=0):
        self.callbacks[request](request,self.contexts[request],status,information,length)

    def send(self, request, *args):
        self.notify(request,http.WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE)
        return 1

    def receive(self, request, reserved):
        if self.barrier:
            self.barrier.wait(timeout=3)
        if self.fail_receive:
            ctypes.set_last_error(12002)
            return 0
        self.notify(request,http.WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE)
        return 1

    def write_text(self, value, buffer, size):
        text = ctypes.create_unicode_buffer(value)
        needed = ctypes.sizeof(text)
        if buffer is None or dword(size).value < needed:
            dword(size).value = needed
            ctypes.set_last_error(http.ERROR_INSUFFICIENT_BUFFER)
            return 0
        ctypes.memmove(buffer, text, needed)
        dword(size).value = needed - ctypes.sizeof(ctypes.c_wchar)
        return 1

    def query_headers(self, request, info, name, buffer, size, index):
        if info == http.WINHTTP_QUERY_CUSTOM and name == "cf-mitigated":
            if self.mitigated is None:
                ctypes.set_last_error(http.ERROR_WINHTTP_HEADER_NOT_FOUND)
                return 0
            return self.write_text(self.mitigated, buffer, size)
        if info == http.WINHTTP_QUERY_STATUS_CODE | http.WINHTTP_QUERY_FLAG_NUMBER:
            dword(buffer).value = self.status
            return 1
        if info == http.WINHTTP_QUERY_CONTENT_LENGTH:
            if self.length is None:
                ctypes.set_last_error(http.ERROR_WINHTTP_HEADER_NOT_FOUND)
                return 0
            return self.write_text(self.length, buffer, size)
        raise AssertionError("Unexpected query")

    def query_option(self, request, option, buffer, size):
        if option == http.WINHTTP_OPTION_HTTP_PROTOCOL_USED:
            dword(buffer).value = 1 if self.http2_supported else 0
            return 1
        if option == http.WINHTTP_OPTION_URL:
            return self.write_text(self.final_url, buffer, size)
        raise AssertionError("Unexpected option")

    def read(self, request, buffer, size, count):
        cursor = self.requests[request]
        chunk = self.payload[cursor:cursor + size]
        ctypes.memmove(buffer, chunk, len(chunk))
        if count is not None:
            raise AssertionError('Async reads must not pass a byte-count pointer')
        self.requests[request] += len(chunk)
        self.notify(request,http.WINHTTP_CALLBACK_STATUS_READ_COMPLETE,buffer,len(chunk))
        return 1

    def close(self, handle):
        self.closed.append(handle)
        if handle in self.callbacks:
            self.notify(handle,http.WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING)
        return 1


class WinHttpTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture()
        self.dll_patch = patch.object(ctypes, "WinDLL", return_value=self.native)
        self.dll_patch.start()
        self.addCleanup(self.dll_patch.stop)
        self.cancel = threading.Event()

    def client(self):
        client = http.WinHttpClient("TVMON fixture")
        self.addCleanup(client.close)
        return client

    def get(self, client, **kwargs):
        return client.get(URL, {"Referer": "https://player.example/"}, self.cancel,
                          kwargs.pop("limit", 1024 * 1024), **kwargs)

    def test_native_signatures_preserve_64_bit_handles_and_context(self):
        self.client()
        self.assertIs(self.native.WinHttpOpen.restype, ctypes.c_void_p)
        self.assertIs(self.native.WinHttpConnect.restype, ctypes.c_void_p)
        self.assertIs(self.native.WinHttpOpenRequest.restype, ctypes.c_void_p)
        self.assertIs(self.native.WinHttpReadData.argtypes[0], ctypes.c_void_p)
        self.assertIs(self.native.WinHttpSendRequest.argtypes[-1], ctypes.c_size_t)
        self.assertEqual(ctypes.sizeof(http.DWORD), 4)

    def test_session_settings_preserve_proxies_and_tls_checks(self):
        client = self.client()
        self.native.WinHttpOpen.assert_called_once_with("TVMON fixture", 4, None, None, http.WINHTTP_FLAG_ASYNC)
        self.native.WinHttpSetTimeouts.assert_called_once_with(client._session, 12000, 12000, 12000, 12000)
        values = {call.args[1]: dword(call.args[2]).value
                  for call in self.native.WinHttpSetOption.call_args_list}
        self.assertEqual(values, {73: 8, 88: 1, 133: 1})
        self.assertNotIn(31, values)  # No certificate-validation ignore flags.
        self.assertTrue(client.http2_enabled)

    def test_payload_callback_final_url_and_persistent_host_handle(self):
        self.native.payload = b"x" * (http.READ_SIZE + 7)
        self.native.length = str(len(self.native.payload))
        self.native.final_url = "https://cdn.example/final.gif"
        client = self.client()
        sizes = []
        self.assertEqual(self.get(client, on_bytes=sizes.append),
                         (self.native.payload, self.native.final_url))
        self.assertEqual(sizes, [http.READ_SIZE, 7])
        self.get(client)
        self.assertEqual(self.native.WinHttpConnect.call_count, 1)
        self.assertEqual(self.native.WinHttpOpenRequest.call_count, 2)
        self.assertEqual(client.protocols_used, {"HTTP/2"})
        self.assertEqual(set(self.native.closed), set(self.native.requests))
        self.assertTrue(all(call.args[-1] == http.WINHTTP_FLAG_SECURE
                            for call in self.native.WinHttpOpenRequest.call_args_list))

    def test_one_session_supports_eight_independent_requests(self):
        self.native.barrier = threading.Barrier(8)
        client = self.client()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: self.get(client), range(8)))
        self.assertEqual(results, [(b"abc", URL)] * 8)
        self.assertEqual(self.native.WinHttpConnect.call_count, 1)
        self.assertEqual(len(self.native.requests), 8)
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_each_distinct_host_port_has_its_own_connection(self):
        client = self.client()
        for url in (URL, "https://other.example/a", "https://media.example:444/a"):
            client.get(url, {}, self.cancel, 10)
        self.assertEqual(self.native.WinHttpConnect.call_count, 3)

    def test_status_error_closes_request_without_reading(self):
        self.native.status = 503
        with self.assertRaises(http.HttpStatusError) as failure:
            self.get(self.client())
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(self.native.WinHttpReadData.call_count, 0)
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_cloudflare_challenge_is_distinct_from_http_errors_even_with_status_200(self):
        self.native.mitigated = "challenge"
        for status in (403, 200):
            self.native.status = status
            with self.subTest(status=status), self.assertRaises(http.BrowserChallengeError) as failure:
                self.get(self.client())
            self.assertEqual(failure.exception.url, URL)
        self.assertEqual(self.native.WinHttpReadData.call_count, 0)
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_truncated_payload_is_rejected_and_request_closed(self):
        self.native.length = "4"
        with self.assertRaisesRegex(http.TransferError, "모두 받지"):
            self.get(self.client())
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_missing_content_length_still_enforces_actual_size(self):
        self.native.length = None
        self.assertEqual(self.get(self.client()), (b"abc", URL))
        with self.assertRaisesRegex(http.TransferError, "허용 크기"):
            self.get(self.client(), limit=2)

    def test_advertised_oversized_response_is_rejected_before_read(self):
        with self.assertRaisesRegex(http.TransferError, "허용 크기"):
            self.get(self.client(), limit=2)
        self.assertEqual(self.native.WinHttpReadData.call_count, 0)

    def test_malformed_content_length_is_not_treated_as_complete(self):
        client = self.client()
        for length in ("-3", "NaN", "3, 3", "999999999999999999999999999999"):
            self.native.length = length
            with self.subTest(length=length), self.assertRaises(http.TransferError):
                self.get(client)

    def test_cancelled_request_never_starts(self):
        self.cancel.set()
        with self.assertRaises(http.TransferCancelled):
            self.get(self.client())
        self.assertEqual(self.native.WinHttpOpenRequest.call_count, 0)

    def test_cancel_after_first_block_closes_request_and_stops_reads(self):
        self.native.payload = b"x" * (http.READ_SIZE * 2)
        self.native.length = str(len(self.native.payload))
        with self.assertRaises(http.TransferCancelled):
            self.get(self.client(), on_bytes=lambda count: self.cancel.set())
        self.assertEqual(self.native.WinHttpReadData.call_count, 1)
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_network_error_retains_native_code_and_closes_request(self):
        self.native.fail_receive = True
        with self.assertRaises(http.TransferError) as failure:
            self.get(self.client())
        self.assertEqual(failure.exception.code, 12002)
        self.assertNotIn("token", str(failure.exception))
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_ready_peer_interrupts_pending_headers_and_releases_the_worker(self):
        client = self.client()
        self.native.WinHttpReceiveResponse.side_effect = lambda request, reserved: 1
        class PeerReady(Exception):
            pass
        began = time.monotonic()
        with self.assertRaises(PeerReady):
            self.get(client, on_wait=Mock(side_effect=PeerReady))
        self.assertLess(time.monotonic() - began, .5)
        self.assertEqual(self.native.WinHttpReadData.call_count, 0)
        self.assertEqual(client._contexts, {})
        self.assertEqual(client._active, 0)
        self.assertEqual(self.native.closed, list(self.native.requests))
        self.native.WinHttpReceiveResponse.side_effect = self.native.receive
        self.assertEqual(self.get(client)[0], b'abc')

    def test_cancel_during_pending_headers_closes_only_its_request(self):
        client = self.client()
        def pending(request, reserved):
            self.cancel.set()
            return 1
        self.native.WinHttpReceiveResponse.side_effect = pending
        with self.assertRaises(http.TransferCancelled):
            self.get(client)
        self.assertEqual(self.native.closed, list(self.native.requests))
        self.assertNotIn(client._session, self.native.closed)
        self.assertEqual(client._contexts, {})

    def test_late_read_completion_retains_buffer_until_final_close_callback(self):
        import weakref
        client = self.client()
        buffers, callbacks = [], []
        class PeerReady(Exception):
            pass
        def pending_read(request, buffer, size, count):
            self.assertIsNone(count)
            buffers.append(weakref.ref(buffer))
            return 1
        def closing(request):
            self.native.closed.append(request)
            if request not in self.native.callbacks:
                return 1
            def finish():
                buffer = buffers[-1]()
                callbacks.append(buffer is not None)
                if buffer is not None:
                    ctypes.memmove(buffer, b'abc', 3)
                    self.native.notify(request, http.WINHTTP_CALLBACK_STATUS_READ_COMPLETE, buffer, 3)
                self.native.notify(request, http.WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING)
            timer = threading.Timer(.03, finish)
            timer.start()
            self.addCleanup(timer.join)
            return 1
        self.native.WinHttpReadData.side_effect = pending_read
        self.native.WinHttpCloseHandle.side_effect = closing
        with self.assertRaises(PeerReady):
            self.get(client, on_wait=Mock(side_effect=PeerReady))
        self.assertEqual(callbacks, [True])
        self.assertEqual(client._contexts, {})
        self.assertEqual(client._active, 0)

    def test_async_error_callback_preserves_native_error_code(self):
        def failed(request, reserved):
            result = http._AsyncResult(1, 12002)
            self.native.notify(request, http.WINHTTP_CALLBACK_STATUS_REQUEST_ERROR,
                               ctypes.byref(result), ctypes.sizeof(result))
            return 1
        self.native.WinHttpReceiveResponse.side_effect = failed
        client = self.client()
        with self.assertRaises(http.TransferError) as failure:
            self.get(client)
        self.assertEqual(failure.exception.code, 12002)
        self.assertEqual(client._contexts, {})

    def test_progressing_transfer_is_not_restarted_by_total_elapsed_time(self):
        self.native.payload = b"x" * (http.READ_SIZE * 2)
        self.native.length = str(len(self.native.payload))
        client = self.client()
        with patch("time.monotonic", side_effect=(0, 61, 122, 183, 244, 305)):
            self.assertEqual(self.get(client)[0], self.native.payload)
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_slow_connection_is_expired_once_during_body_without_discarding_data(self):
        self.native.payload = b"x" * (http.READ_SIZE * 2 + 7)
        self.native.length = str(len(self.native.payload))
        now = 0.0
        expired = []
        received = []

        def read_with_elapsed_time(request, buffer, size, count):
            nonlocal now
            before = self.native.requests[request]
            result = self.native.read(request, buffer, size, count)
            if self.native.requests[request] > before:
                now += 13
            return result

        def record_expiration(handle, option, value, size):
            if option == 143:
                expired.append((handle, self.native.requests[handle]))
            return self.native.set_option(handle, option, value, size)

        self.native.WinHttpReadData.side_effect = read_with_elapsed_time
        self.native.WinHttpSetOption.side_effect = record_expiration
        client = self.client()
        with patch("time.monotonic", side_effect=lambda: now):
            self.assertEqual(self.get(client, on_bytes=received.append), (self.native.payload, URL))
        self.assertEqual(expired, [(next(iter(self.native.requests)), http.READ_SIZE)])
        self.assertLess(expired[0][1], len(self.native.payload))
        self.assertEqual(received, [http.READ_SIZE, http.READ_SIZE, 7])
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_fast_requests_reuse_connection_without_expiring_it(self):
        client = self.client()
        with patch("time.monotonic", return_value=5.0):
            self.assertEqual(self.get(client), (b"abc", URL))
            self.assertEqual(self.get(client), (b"abc", URL))
        expired = [call for call in self.native.WinHttpSetOption.call_args_list if call.args[1] == 143]
        self.assertEqual(expired, [])
        self.assertEqual(self.native.WinHttpConnect.call_count, 1)
        self.assertEqual(self.native.WinHttpOpenRequest.call_count, 2)

    def test_connection_expiration_failure_preserves_complete_body(self):
        self.native.payload = b"x" * (http.READ_SIZE * 2 + 7)
        self.native.length = str(len(self.native.payload))
        now = 0.0
        expired = []

        def read_with_elapsed_time(request, buffer, size, count):
            nonlocal now
            before = self.native.requests[request]
            result = self.native.read(request, buffer, size, count)
            if self.native.requests[request] > before:
                now += 13
            return result

        def fail_expiration(handle, option, value, size):
            if option == 143:
                expired.append(handle)
                ctypes.set_last_error(87)
                return 0
            return self.native.set_option(handle, option, value, size)

        self.native.WinHttpReadData.side_effect = read_with_elapsed_time
        self.native.WinHttpSetOption.side_effect = fail_expiration
        client = self.client()
        with patch("time.monotonic", side_effect=lambda: now):
            self.assertEqual(self.get(client), (self.native.payload, URL))
        self.assertEqual(expired, list(self.native.requests))
        self.assertEqual(set(self.native.closed), set(self.native.requests))

    def test_untrusted_input_and_redirect_urls_are_rejected(self):
        client = self.client()
        for url in ("http://media.example/a", "https://u:p@media.example/a",
                    "https://@media.example/a", "https://media.example:bad/a", "https://media.example:0/a",
                    "https://media.example/a\n"):
            with self.subTest(url=url), self.assertRaises(http.TransferError):
                client.get(url, {}, self.cancel, 10)
        self.assertEqual(self.native.WinHttpSendRequest.call_count, 0)
        for url in ("http://media.example/a", "https://u:p@media.example/a"):
            self.native.final_url = url
            with self.subTest(redirect=url), self.assertRaises(http.TransferError):
                self.get(client)
        self.assertEqual(self.native.WinHttpReadData.call_count, 0)

    def test_accept_encoding_is_identity_and_header_injection_rejected(self):
        client = self.client()
        client.get(URL, {"Accept-Encoding": "gzip", "Origin": "https://player.example"}, self.cancel, 10)
        sent = self.native.WinHttpSendRequest.call_args.args[1].value
        self.assertIn("Accept-Encoding: identity\r\n", sent)
        self.assertNotIn("gzip", sent)
        with self.assertRaises(http.TransferError):
            client.get(URL, {"Origin": "x\r\nFake: value"}, self.cancel, 10)

    def test_constructor_failure_frees_session(self):
        self.native.fail_timeouts = True
        with self.assertRaises(http.TransferError):
            http.WinHttpClient("fixture")
        self.assertEqual(self.native.closed, [2 ** 40])

    def test_http2_unavailable_reports_windows_support_and_frees_session(self):
        self.native.http2_supported = False
        with self.assertRaisesRegex(http.TransferError, "Windows 버전") as failure:
            http.WinHttpClient("fixture")
        self.assertEqual(failure.exception.code, 12009)
        self.assertEqual(self.native.closed, [2 ** 40])

    def test_close_is_idempotent_and_rejects_further_requests(self):
        client = self.client()
        self.get(client)
        client.close()
        closed = self.native.closed[:]
        client.close()
        self.assertEqual(self.native.closed, closed)
        self.assertEqual(len(closed), 3)
        with self.assertRaises(http.TransferError):
            self.get(client)


class EchHttpTests(unittest.TestCase):
    def setUp(self):
        import curl_cffi
        self.library = curl_cffi
        self.options = {}
        self.body = b"abc"
        self.headers = [b"HTTP/2 200\r\n", b"Content-Length: 3\r\n", b"\r\n"]
        self.status = 200
        self.final_url = "https://tvwiki54.net/"
        self.stall = False
        self.future = None
        self.cancel = threading.Event()
        self.curl = Mock()
        self.curl.setopt.side_effect = lambda option, value: self.options.__setitem__(option, value)
        self.curl.getinfo.side_effect = lambda option: (self.final_url.encode() if option == curl_cffi.CurlInfo.EFFECTIVE_URL else self.status)
        self.multi = Mock()
        self.multi.add_handle.side_effect = self.start
        self.multi.close = AsyncMock(side_effect=self.close_multi)
        for name, value in (("Curl", self.curl), ("AsyncCurl", self.multi)):
            patcher = patch.object(curl_cffi, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, curl):
        loop = asyncio.get_running_loop()
        self.future = loop.create_future()
        if not self.stall:
            loop.call_soon(self.deliver)
        return self.future

    def deliver(self):
        try:
            for value in self.headers:
                self.options[self.library.CurlOpt.HEADERFUNCTION](value)
            if self.body:
                self.options[self.library.CurlOpt.WRITEFUNCTION](self.body)
            self.future.set_result(None)
        except BaseException as error:
            self.future.set_exception(error)

    def close_multi(self):
        if self.future is not None and not self.future.done():
            self.future.cancel()

    def get(self, *, limit=1024, **kwargs):
        return http.get_with_ech("https://tvwiki54.net/", {"Referer": "https://tvwiki54.net/"},
                                 self.cancel, limit, **kwargs)

    def test_verified_ech_response_preserves_body_callback_and_https_options(self):
        received = []
        self.assertEqual(self.get(on_bytes=received.append), (b"abc", self.final_url))
        self.assertEqual(received, [3])
        opt = self.library.CurlOpt
        for key, value in ((opt.SSL_VERIFYPEER, 1), (opt.SSL_VERIFYHOST, 2),
                           (opt.DOH_SSL_VERIFYPEER, 1), (opt.DOH_SSL_VERIFYHOST, 2),
                           (opt.PROTOCOLS_STR, "https"), (opt.REDIR_PROTOCOLS_STR, "https"),
                           (opt.ECH, "true"), (opt.DOH_URL, "https://cloudflare-dns.com/dns-query")):
            self.assertEqual(self.options[key], value)
        self.multi.close.assert_awaited_once()
        self.curl.close.assert_called_once()

    def test_cancelled_before_request_never_creates_a_handle(self):
        self.cancel.set()
        with self.assertRaises(http.TransferCancelled):
            self.get()
        self.library.Curl.assert_not_called()

    def test_inflight_cancel_closes_pending_transfer_promptly(self):
        self.stall = True
        timer = threading.Timer(.03, self.cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(http.TransferCancelled):
                self.get()
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, .8)
        self.assertTrue(self.future.cancelled())
        self.multi.close.assert_awaited_once()
        self.curl.close.assert_called_once()

    def test_oversize_response_is_rejected_and_freed(self):
        with self.assertRaisesRegex(http.TransferError, "허용 크기"):
            self.get(limit=2)
        self.multi.close.assert_awaited_once()
        self.curl.close.assert_called_once()

    def test_http_error_and_challenge_are_distinct(self):
        self.status = 403
        with self.assertRaises(http.HttpStatusError) as error:
            self.get()
        self.assertEqual(error.exception.status, 403)
        self.headers.insert(1, b"cf-mitigated: challenge\r\n")
        with self.assertRaises(http.BrowserChallengeError):
            self.get()

    def test_redirect_discards_intermediate_body_and_rejects_insecure_final_url(self):
        self.headers = [b"HTTP/2 301\r\n", b"HTTP/2 200\r\n", b"\r\n"]
        self.assertEqual(self.get()[0], b"abc")
        self.final_url = "http://tvwiki54.net/"
        with self.assertRaises(http.TransferError):
            self.get()

    def test_tls_failure_does_not_expose_request_query_or_native_curl_details(self):
        self.multi.add_handle.side_effect = self.library.CurlError("https://tvwiki54.net/?token=secret reset", 35)
        with self.assertRaisesRegex(http.TransferError, "라이브러리 오류 35") as error:
            self.get()
        self.assertNotIn("secret", str(error.exception))
        self.curl.close.assert_called_once()

    def test_invalid_url_and_header_fail_before_native_handle(self):
        for url, headers in (("http://tvwiki54.net/", {}), ("https://u:p@tvwiki54.net/", {}),
                             ("https://tvwiki54.net/", {"Origin": "x\r\nCookie: private"})):
            with self.subTest(url=url), self.assertRaises(http.TransferError):
                http.get_with_ech(url, headers, self.cancel, 10)
        self.library.Curl.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Chrome protocol and verification lifecycle checks without desktop interaction."""
import base64
import hashlib
import json
import itertools
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import tvmon_browser as browser
from tvmon_http import BrowserChallengeError, HttpStatusError, TransferCancelled, TransferError


class ChromeSocketTests(unittest.TestCase):
    def connection(self, frames):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(3)
        self.addCleanup(server.close)
        errors = []
        connected = threading.Event()
        def serve():
            try:
                with server.accept()[0] as peer:
                    peer.settimeout(3)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(peer.recv(4096))
                    key = next(line.split(":", 1)[1].strip() for line in request.decode().splitlines()
                               if line.startswith("Sec-WebSocket-Key:"))
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    peer.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode() + frames)
                    connected.wait(timeout=3)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            channel = browser._ChromeSocket(server.getsockname()[1], "/devtools/browser/test", threading.Event())
        finally:
            connected.set()
        self.addCleanup(channel.close)
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return channel

    def test_fragmented_response_is_reassembled(self):
        channel = self.connection(b'\x01\x05{"id"' + b'\x80\x03:1}')
        self.assertEqual(channel.read(time.monotonic() + 2, threading.Event()), {"id": 1})

    def test_oversized_response_is_rejected_before_body_read(self):
        channel = self.connection(b"\x81\x7f" + struct.pack("!Q", 16 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(TransferError, "허용 크기"):
            channel.read(time.monotonic() + 2, threading.Event())

    def test_cancelled_read_stops_and_invalid_handshake_closes_socket(self):
        channel = self.connection(b"")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TransferCancelled):
            channel.read(time.monotonic() + 2, cancel)
        sock = Mock(recv=Mock(return_value=b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: wrong\r\n\r\n"))
        with patch.object(browser.socket, "create_connection", return_value=sock), self.assertRaises(TransferError):
            browser._ChromeSocket(9999, "/devtools/browser/test", threading.Event())
        sock.close.assert_called_once()


class BrowserFlowTests(unittest.TestCase):
    URL = "https://toki34.com/"
    HTML = "<html><title>NEWTOKI</title><body>Verified content</body></html>"

    def test_chrome_detection_uses_each_machine_environment_or_registered_custom_path(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "Google/Chrome/Application/chrome.exe"
            local.parent.mkdir(parents=True)
            local.touch()
            with patch.dict(browser.os.environ, {"LOCALAPPDATA": directory}, clear=True):
                self.assertEqual(browser.TextSiteBrowser._chrome_path(), local)
            custom = Path(directory) / "custom-chrome.exe"
            custom.touch()
            key = Mock()
            opened = Mock(__enter__=Mock(return_value=key), __exit__=Mock(return_value=False))
            with patch.dict(browser.os.environ, {}, clear=True), patch.object(browser.winreg, "OpenKey", return_value=opened), \
                    patch.object(browser.winreg, "QueryValueEx", return_value=(str(custom), browser.winreg.REG_SZ)):
                self.assertEqual(browser.TextSiteBrowser._chrome_path(), custom)
            with patch.dict(browser.os.environ, {}, clear=True), patch.object(browser.winreg, "OpenKey", side_effect=FileNotFoundError()), \
                    self.assertRaisesRegex(TransferError, "Chrome을 찾지 못했습니다"):
                browser.TextSiteBrowser._chrome_path()

    def client(self, states):
        client = browser.TextSiteBrowser(Path("fixture-text-profile"))
        client._start = Mock()
        states = iter(states)
        def call(method, params, cancel, timeout=5):
            if method == "Target.createTarget":
                return {"targetId": "seed" if params.get("windowState") == "minimized" else "target"}
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": "target"}]}
            if method == "Target.attachToTarget":
                return {"sessionId": "session"}
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "frame"}}}
            if method == "Runtime.evaluate":
                if params["expression"].startswith("JSON.stringify"):
                    current = next(states)
                    status, challenge = current[:2]
                    client.document = (status, self.URL, challenge)
                    state = {"url": self.URL, "ready": "complete", "title": "NEWTOKI"}
                    if len(current) == 3:
                        state.update(current[2])
                    return {"result": {"value": json.dumps(state)}}
                return {"result": {"value": self.HTML}}
            return {}
        client._call = Mock(side_effect=call)
        return client

    def test_challenge_is_not_accepted_as_success_and_verified_session_is_reused(self):
        client = self.client([(403, True), (200, False), (200, False)])
        cancel = threading.Event()
        with patch.object(cancel, "wait", return_value=False):
            for _ in range(2):
                self.assertEqual(client.get(self.URL, cancel, limit=1024), (self.HTML.encode(), self.URL))
        creates = [call for call in client._call.call_args_list if call.args[0] == "Target.createTarget"]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0].args[1], {"url": "about:blank", "newWindow": True, "background": True, "windowState": "minimized"})
        self.assertEqual(creates[1].args[1], {"url": "about:blank", "hidden": True, "background": True})
        closes = [call for call in client._call.call_args_list if call.args[0] == "Target.closeTarget"]
        self.assertEqual([call.args[1] for call in closes], [{"targetId": "seed"}])

    def test_browser_redirect_returns_final_toki35_homepage(self):
        client = self.client([])
        original = client._call.side_effect
        def call(method, params, cancel, timeout=5):
            if method == "Runtime.evaluate" and params["expression"].startswith("JSON.stringify"):
                client.document = (200, "https://toki35.com/", False)
                return {"result": {"value": json.dumps({"url": "https://toki35.com/", "ready": "complete", "title": "NEWTOKI"})}}
            return original(method, params, cancel, timeout)
        client._call.side_effect = call
        self.assertEqual(client.get(self.URL, threading.Event(), limit=1024), (self.HTML.encode(), "https://toki35.com/"))
        navigations = [item.args[1]["url"] for item in client._call.call_args_list if item.args[0] == "Page.navigate"]
        self.assertEqual(navigations, [self.URL])

    def test_redirected_challenge_preserves_final_verification_url_without_logging_query(self):
        rows = []
        client = self.client([])
        client.on_event = lambda event, **fields: rows.append({"event": event, **fields})
        original = client._call.side_effect
        destination = "https://toki35.com/?token=secret"
        def call(method, params, cancel, timeout=5):
            if method == "Runtime.evaluate":
                client.document = (403, destination, True)
                client._challenge_started = 0
                return {"result": {"value": json.dumps({"url": destination, "ready": "complete", "title": "Just a moment..."})}}
            return original(method, params, cancel, timeout)
        client._call.side_effect = call
        cancel = threading.Event()
        with patch.object(browser.time, "monotonic", side_effect=[0, 1, 31]), patch.object(cancel, "wait", return_value=False), \
                self.assertRaises(BrowserChallengeError) as caught:
            client.get(self.URL, cancel, limit=1024)
        self.assertEqual(caught.exception.url, destination)
        self.assertEqual(rows[-1], {"event": "verification_pending", "url": "https://toki35.com/"})
        self.assertNotIn("secret", json.dumps(rows))

    def test_protocol_records_redirect_without_tokens_or_response_headers(self):
        rows = []
        client = browser.TextSiteBrowser(Path("fixture-text-profile"), on_event=lambda event, **fields: rows.append({"event": event, **fields}))
        client.session, client.frame = "session", "frame"
        client.channel = Mock(read=Mock(side_effect=[
            {"method": "Network.requestWillBeSent", "sessionId": "session", "params": {"type": "Document", "frameId": "frame",
                "redirectResponse": {"url": "https://toki34.com/?token=secret", "status": 302, "headers": {"Set-Cookie": "secret"}},
                "request": {"url": "https://toki35.com/?token=secret", "headers": {"Cookie": "secret"}}}},
            {"id": 1, "result": {}}]))
        client._call("Page.navigate", {"url": self.URL}, threading.Event())
        self.assertEqual(rows[0], {"event": "http_redirect", "previous_url": "https://toki34.com/", "url": "https://toki35.com/", "status": 302})
        self.assertNotIn("secret", json.dumps(rows))

    def test_search_html_reuses_authenticated_origin_without_navigating_each_page(self):
        records = []
        client = browser.TextSiteBrowser(Path("fixture-text-profile"), on_event=lambda event, **fields: records.append({"event": event, **fields}))
        client._start = Mock()
        def warm(url, cancel, **kwargs):
            client.session, client.target = "session", "target"
            client.document = (200, url, False)
            return self.HTML.encode(), url
        client.get = Mock(side_effect=warm)
        def call(method, params, cancel, **kwargs):
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": "target"}]}
            return {"result": {"value": {"status": 200, "url": self.URL + "search?q=test", "challenge": False, "body": self.HTML}}}
        client._call = Mock(side_effect=call)
        cancel = threading.Event()
        for page in (1, 2):
            self.assertEqual(client.get_html(self.URL + f"search?q=test&page={page}", cancel, limit=1024),
                             (self.HTML.encode(), self.URL + "search?q=test"))
        client.get.assert_called_once_with(self.URL, cancel, limit=1024)
        self.assertTrue(all(call.args[0] != "Page.navigate" for call in client._call.call_args_list))
        self.assertEqual(sum(record["event"] == "http_response" for record in records), 2)
        self.assertFalse(any("?" in record.get("url", "") for record in records))

    def test_search_html_manual_verification_opens_only_the_requested_page(self):
        client = browser.TextSiteBrowser(Path("fixture-text-profile"))
        client.get = Mock(return_value=(self.HTML.encode(), self.URL))
        cancel = threading.Event()
        url = self.URL + "search?q=test"
        client.get_html(url, cancel, limit=1024, visible=True)
        client.get.assert_called_once_with(url, cancel, limit=1024, visible=True)

    def test_search_html_rejects_challenges_errors_oversize_and_cancelled_requests(self):
        client = browser.TextSiteBrowser(Path("fixture-text-profile"))
        client._start = Mock()
        client.get = Mock()
        cancel = threading.Event()
        values = [("challenge", {"status": 403, "url": self.URL, "challenge": True, "body": "verify"}, BrowserChallengeError),
                  ("http", {"status": 403, "url": self.URL, "challenge": False, "body": "denied"}, HttpStatusError),
                  ("size", {"error": "size"}, TransferError),
                  ("encoded_size", {"status": 200, "url": self.URL, "body": "가" * 400}, TransferError),
                  ("transport", None, TransferError)]
        for name, value, expected in values:
            client._call = Mock(return_value={"result": {"value": value}})
            with self.subTest(name=name), self.assertRaises(expected):
                client.get_html(self.URL + "search?q=test", cancel, limit=1024)
        client._call.reset_mock()
        cancel.set()
        with self.assertRaises(TransferCancelled):
            client.get_html(self.URL + "search?q=test", cancel, limit=1024)
        client._call.assert_not_called()

    def test_manual_verification_uses_a_visible_window_and_pending_state_is_preserved(self):
        client = self.client([(403, True)])
        cancel = threading.Event()
        with patch.object(browser.time, "monotonic", side_effect=[0, 1, 1, 181]), patch.object(cancel, "wait", return_value=False), \
                self.assertRaises(BrowserChallengeError) as error:
            client.get(self.URL, cancel, limit=1024, visible=True)
        self.assertEqual(error.exception.url, self.URL)
        self.assertFalse(client.closed)
        calls = client._call.call_args_list
        self.assertTrue(any(call.args[:2] == ("Target.createTarget", {"url": self.URL, "newWindow": True}) for call in calls))
        self.assertTrue(any(call.args[0] == "Target.activateTarget" for call in calls))

    def test_automatic_challenge_waits_30_seconds_from_detection(self):
        for start in (0, 45):
            clock, records = [0.0], []
            client = self.client(itertools.repeat((403, True)))
            client.on_event = lambda event, **fields: records.append({"event": event, **fields})
            original = client._call.side_effect
            def call(method, params, cancel, timeout=5):
                if method == "Page.navigate":
                    clock[0] = start
                return original(method, params, cancel, timeout)
            client._call.side_effect = call
            cancel = threading.Event()
            def wait(seconds):
                clock[0] += 1
                return False
            with self.subTest(start=start), patch.object(browser.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(cancel, "wait", side_effect=wait), self.assertRaises(BrowserChallengeError):
                client.get(self.URL, cancel, limit=1024)
            self.assertEqual(clock[0], start + 30)
            progress = [record for record in records if record['event'] == 'verification_wait']
            self.assertTrue(progress)
            self.assertTrue(all(record['limit'] == 30 and record['mode'] == 'automatic' for record in progress))
            self.assertEqual(progress[-1]['remaining'], 1)

    def test_challenge_header_timeout_uses_30_seconds_and_cleared_challenge_can_finish_later(self):
        clock = [0.0]
        client = self.client([])
        original = client._call.side_effect
        def call(method, params, cancel, timeout=5):
            if method == "Page.navigate":
                client.document = (403, self.URL, True)
                client._challenge_started = clock[0]
            if method == "Runtime.evaluate":
                clock[0] += timeout
                raise TimeoutError('delayed')
            return original(method, params, cancel, timeout)
        client._call.side_effect = call
        cancel = threading.Event()
        with patch.object(browser.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(cancel, "wait", return_value=False), self.assertRaises(BrowserChallengeError):
            client.get(self.URL, cancel, limit=1024)
        self.assertEqual(clock[0], 30)
        clock[0] = 0
        client = self.client([(403, True), (200, False, {"ready": "interactive"}), (200, False)])
        def wait(seconds):
            clock[0] += 20
            return False
        with patch.object(browser.time, "monotonic", side_effect=lambda: clock[0]), patch.object(cancel, "wait", side_effect=wait):
            self.assertEqual(client.get(self.URL, cancel, limit=1024), (self.HTML.encode(), self.URL))
        self.assertEqual(clock[0], 40)

    def test_startup_tab_is_closed_even_when_hidden_page_creation_fails(self):
        client = self.client([])
        original = client._call.side_effect
        def call(method, params, cancel, timeout=5):
            if method == "Target.createTarget" and params.get("hidden"):
                raise TransferError("hidden page failed")
            return original(method, params, cancel, timeout)
        client._call.side_effect = call
        with self.assertRaisesRegex(TransferError, "hidden page failed"):
            client.get(self.URL, threading.Event(), limit=1024)
        self.assertTrue(any(call.args[:2] == ("Target.closeTarget", {"targetId": "seed"}) for call in client._call.call_args_list))
        self.assertIsNone(client.target)

    def test_http_success_waits_for_old_challenge_dom_and_loading_to_finish(self):
        client = self.client([(200, False, {"title": "Just a moment..."}),
                              (200, False, {"challenge": True}),
                              (200, False, {"ready": "interactive"}), (200, False)])
        cancel = threading.Event()
        with patch.object(cancel, "wait", return_value=False) as waiting:
            self.assertEqual(client.get(self.URL, cancel, limit=1024), (self.HTML.encode(), self.URL))
        self.assertEqual(waiting.call_count, 3)

    def test_delayed_navigation_and_document_reads_recover_without_reloading(self):
        for stage in ("navigation", "state", "markup"):
            client = self.client([(200, False), (200, False)])
            original = client._call.side_effect
            delayed = []
            def call(method, params, cancel, timeout=5):
                selected = ((stage == "navigation" and method == "Page.navigate")
                            or (stage == "state" and method == "Runtime.evaluate" and params["expression"].startswith("JSON.stringify"))
                            or (stage == "markup" and method == "Runtime.evaluate" and params["expression"] == "document.documentElement.outerHTML"))
                if selected and not delayed:
                    delayed.append(True)
                    raise TimeoutError("delayed Chrome response")
                return original(method, params, cancel, timeout)
            client._call.side_effect = call
            cancel = threading.Event()
            with self.subTest(stage=stage), patch.object(cancel, "wait", return_value=False):
                self.assertEqual(client.get(self.URL, cancel, limit=1024), (self.HTML.encode(), self.URL))
            self.assertEqual(sum(call.args[0] == "Page.navigate" for call in client._call.call_args_list), 1)
            self.assertEqual(delayed, [True])

    def test_timeout_without_challenge_is_a_connection_error_and_is_bounded(self):
        client = self.client([])
        original = client._call.side_effect
        def call(method, params, cancel, timeout=5):
            if method == "Runtime.evaluate":
                raise TimeoutError("no response")
            return original(method, params, cancel, timeout)
        client._call.side_effect = call
        cancel = threading.Event()
        with patch.object(browser.time, "monotonic", side_effect=[0, 1, 61]), patch.object(cancel, "wait", return_value=False), \
                self.assertRaisesRegex(TransferError, "응답이 지연") as error:
            client.get(self.URL, cancel, limit=1024)
        self.assertNotIsInstance(error.exception, BrowserChallengeError)

    def test_protocol_log_records_status_and_timeout_without_tokens_or_headers(self):
        rows = []
        client = browser.TextSiteBrowser(Path("fixture-text-profile"), on_event=lambda event, **fields: rows.append({"event": event, **fields}))
        client.session, client.frame = "session", "frame"
        client.channel = Mock(read=Mock(side_effect=[
            {"method": "Network.responseReceived", "sessionId": "session", "params": {"type": "Document", "frameId": "frame",
             "response": {"url": self.URL + "?token=secret", "status": 403, "headers": {"cf-mitigated": "challenge", "Set-Cookie": "secret"}}}},
            {"id": 1, "result": {}}]))
        client._call("Network.enable", {}, threading.Event())
        self.assertEqual(rows[0], {"event": "http_response", "url": self.URL, "status": 403, "challenge": True})
        self.assertNotIn("secret", json.dumps(rows))
        client.channel.read.side_effect = TimeoutError("late response")
        with self.assertRaises(TimeoutError):
            client._call("Page.navigate", {"url": self.URL}, threading.Event())
        self.assertEqual(rows[-1]["event"], "chrome_timeout")
        self.assertEqual(rows[-1]["method"], "Page.navigate")

    def test_normal_http_error_and_oversized_content_are_not_success(self):
        client = self.client([(503, False)])
        with self.assertRaises(HttpStatusError) as error:
            client.get(self.URL, threading.Event(), limit=1024)
        self.assertEqual(error.exception.status, 503)
        client = self.client([(200, False)])
        with self.assertRaisesRegex(TransferError, "허용 크기"):
            client.get(self.URL, threading.Event(), limit=1)

    def test_novel_body_waits_for_renderer_and_returns_paragraphs_only(self):
        url = self.URL + "novel/1/2"
        client = browser.TextSiteBrowser(Path("fixture-text-profile"))
        client.get = Mock(return_value=(b"<article class=novel-viewer>Loading</article>", url))
        client._call = Mock(side_effect=[{"result": {"value": {"url": url, "reader": True, "text": "", "loading": "본문 준비 중"}}},
            {"result": {"value": {"url": url, "reader": True, "text": "  시험 문단\r\n\r\n다음 문단  ", "loading": ""}}}])
        cancel = threading.Event()
        with patch.object(cancel, "wait", return_value=False):
            body, final = client.get_novel_text(url, cancel, limit=1024)
        self.assertEqual(body, "시험 문단\n\n다음 문단".encode())
        self.assertEqual(final, url)
        client.get.assert_called_once_with(url, cancel, limit=1024, visible=False)

    def test_novel_reader_rejects_challenge_wrong_episode_missing_reader_and_oversize(self):
        url = self.URL + "novel/1/2"
        for state, error in (({"loading": "인증이 필요합니다"}, BrowserChallengeError),
                             ({"url": url + "3"}, TransferError), ({"reader": False}, TransferError),
                             ({"text": "가" * 1000}, TransferError)):
            client = browser.TextSiteBrowser(Path("fixture-text-profile"))
            client.get = Mock()
            client._call = Mock(return_value={"result": {"value": {"url": url, "reader": True, "text": "", "loading": "", **state}}})
            with self.subTest(state=state), self.assertRaises(error):
                client.get_novel_text(url, threading.Event(), limit=1024)

    def test_close_stops_owned_browser_and_rejects_later_calls(self):
        client = browser.TextSiteBrowser(Path("fixture-text-profile"))
        channel, process = Mock(), Mock()
        client.channel, client.process = channel, process
        client.close()
        channel.send.assert_called_once_with({"id": 0, "method": "Browser.close"})
        process.wait.assert_called_once_with(timeout=2)
        with self.assertRaises(TransferCancelled):
            client.get(self.URL, threading.Event(), limit=1024)

    def test_start_uses_owned_profile_and_nonzero_loopback_port_without_security_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            client = browser.TextSiteBrowser(Path(directory) / "text-browser")
            self.addCleanup(client.close)
            process = Mock(pid=12345, poll=Mock(return_value=None))
            captured = {}
            def launch(args, **kwargs):
                captured["args"], captured["kwargs"] = args, kwargs
                captured["port"] = int(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--remote-debugging-port=")))
                return process
            def connect(host, port, timeout):
                self.assertEqual(host, "127.0.0.1")
                self.assertEqual(port, captured["port"])
                response = Mock(status=200, read=Mock(return_value=json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/test"}).encode()))
                return Mock(getresponse=Mock(return_value=response))
            with patch.object(client, "_chrome_path", return_value=Path("chrome.exe")), \
                    patch.object(browser.subprocess, "Popen", side_effect=launch), \
                    patch.object(browser.http.client, "HTTPConnection", side_effect=connect), \
                    patch.object(browser, "_ChromeSocket"), \
                    patch.object(client, "_call", return_value={"processInfo": [{"type": "browser", "id": 12345}]}):
                client._start(threading.Event())
            self.assertGreater(captured["port"], 0)
            self.assertIn("--user-data-dir=" + str(client.profile), captured["args"])
            self.assertIn("--no-startup-window", captured["args"])
            self.assertNotIn("about:blank", captured["args"])
            self.assertEqual(captured["kwargs"]["startupinfo"].wShowWindow, browser.subprocess.SW_HIDE)
            self.assertFalse(any(flag in " ".join(captured["args"]) for flag in ("--headless", "--no-sandbox", "--ignore-certificate-errors", "--enable-automation")))

    def test_existing_browser_requires_exact_profile_flags_executable_and_dead_parent(self):
        with tempfile.TemporaryDirectory(prefix="browser profile ") as directory:
            client = browser.TextSiteBrowser(Path(directory) / "text-browser")
            client.profile.mkdir()
            (client.profile / "Local State").write_text("{}", encoding="utf-8")
            chrome = Path(directory) / "chrome.exe"
            args = [str(chrome), "--no-startup-window", "--no-first-run", "--no-default-browser-check",
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=28950", "--user-data-dir=" + str(client.profile)]
            record = {"pid": 12345, "path": str(chrome), "command": browser.subprocess.list2cmdline(args), "parent_alive": False}
            def inspect(record):
                with patch.object(browser.subprocess, "run", return_value=Mock(stdout=json.dumps([record]))) as run:
                    result = client._existing_browser(chrome)
                self.assertEqual(run.call_args.kwargs["creationflags"], browser.subprocess.CREATE_NO_WINDOW)
                return result
            self.assertEqual(inspect(record), (12345, 28950))
            legacy = '"' + str(chrome) + '" --no-startup-window --no-first-run --no-default-browser-check --remote-debugging-address=127.0.0.1 --remote-debugging-port=28950 --user-data-dir="' + str(client.profile) + '" /prefetch:5'
            self.assertEqual(inspect({**record, "command": legacy}), (12345, 28950))
            for changes in ({"parent_alive": True}, {"path": str(Path(directory) / "other.exe")},
                            {"command": record["command"].replace("127.0.0.1", "0.0.0.0")},
                            {"command": record["command"].replace("28950", "0")},
                            {"command": record["command"].replace("--no-startup-window", "")}):
                with self.subTest(changes=changes), self.assertRaises(TransferError):
                    inspect({**record, **changes})
            for command in (record["command"] + " --type=renderer",
                            browser.subprocess.list2cmdline(args[:-1] + ["--user-data-dir=" + str(Path(directory) / "other-profile")])):
                self.assertIsNone(inspect({**record, "command": command}))

    def test_reconnect_verifies_browser_pid_without_launching_or_closing_existing_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            client = browser.TextSiteBrowser(Path(directory))
            channel = Mock()
            response = Mock(status=200, read=Mock(return_value=b'{"webSocketDebuggerUrl":"ws://127.0.0.1:28950/devtools/browser/test"}'))
            with patch.object(client, "_chrome_path", return_value=Path("chrome.exe")), \
                    patch.object(client, "_existing_browser", return_value=(12345, 28950)), \
                    patch.object(browser.subprocess, "Popen") as launch, \
                    patch.object(browser.http.client, "HTTPConnection", return_value=Mock(getresponse=Mock(return_value=response))), \
                    patch.object(browser, "_ChromeSocket", return_value=channel), \
                    patch.object(client, "_call", return_value={"processInfo": [{"type": "browser", "id": 12345}]}) as call:
                client._start(threading.Event())
                client._start(threading.Event())
                call.assert_called_once()
                launch.assert_not_called()
                self.assertEqual(client._attached_pid, 12345)
                client.target = "new-owned-target"
                client.close()
            channel.send.assert_called_once_with({"id": 0, "method": "Target.closeTarget", "params": {"targetId": "new-owned-target"}})

    def test_reconnect_rejects_wrong_endpoint_and_browser_pid_without_closing_it(self):
        for endpoint, pid in (("ws://127.0.0.1:28951/devtools/browser/test", 12345),
                              ("ws://other.example:28950/devtools/browser/test", 12345),
                              ("ws://127.0.0.1:28950/devtools/browser/test", 54321)):
            with self.subTest(endpoint=endpoint, pid=pid), tempfile.TemporaryDirectory() as directory:
                client = browser.TextSiteBrowser(Path(directory))
                channel = Mock()
                response = Mock(status=200, read=Mock(return_value=json.dumps({"webSocketDebuggerUrl": endpoint}).encode()))
                with patch.object(client, "_chrome_path", return_value=Path("chrome.exe")), \
                        patch.object(client, "_existing_browser", return_value=(12345, 28950)), \
                        patch.object(browser.subprocess, "Popen") as launch, \
                        patch.object(browser.http.client, "HTTPConnection", return_value=Mock(getresponse=Mock(return_value=response))), \
                        patch.object(browser, "_ChromeSocket", return_value=channel), \
                        patch.object(client, "_call", return_value={"processInfo": [{"type": "browser", "id": pid}]}), \
                        self.assertRaises(TransferError):
                    client._start(threading.Event())
                channel.send.assert_not_called()
                launch.assert_not_called()
                self.assertTrue(client.closed)

    def test_start_failure_records_exit_code_and_bounded_redacted_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            client = browser.TextSiteBrowser(Path(directory), on_event=lambda event, **fields: events.append((event, fields)))
            process = Mock(pid=12345, poll=Mock(return_value=23))
            def launch(args, **kwargs):
                kwargs["stderr"].write(b"x" * 4096 + b" startup failed https://example.com/?token=private-token#fragment\n")
                kwargs["stderr"].flush()
                return process
            with patch.object(client, "_chrome_path", return_value=Path("chrome.exe")), \
                    patch.object(browser.subprocess, "Popen", side_effect=launch), self.assertRaisesRegex(TransferError, "종료 코드 23"):
                client._start(threading.Event())
            failure = next(fields for event, fields in events if event == "chrome_start_failed")
            self.assertEqual(failure["exit_code"], 23)
            self.assertLessEqual(len(failure["diagnostic"]), 1000)
            self.assertIn("startup failed", failure["diagnostic"])
            self.assertNotIn("private-token", failure["diagnostic"])
            self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()

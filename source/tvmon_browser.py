"""B-site pages in an isolated, persistent installed-Chrome session."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import winreg
from urllib.parse import urlsplit

from tvmon_http import BrowserChallengeError, HttpStatusError, TransferCancelled, TransferError


CHALLENGE_WAIT_SECONDS = 30


class _ChromeSocket:
    def __init__(self, port, path, cancel):
        if not 1 <= port <= 65535 or not path.startswith("/devtools/browser/") or "\r" in path or "\n" in path:
            raise TransferError("Chrome 연결 주소를 읽지 못했습니다.")
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.sock.settimeout(.2)
        self.buffer = bytearray()
        self.fragment = bytearray()
        self.send_lock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            deadline = time.monotonic() + 5
            while b"\r\n\r\n" not in self.buffer:
                self._receive(deadline, cancel)
                if len(self.buffer) > 8192:
                    raise TransferError("Chrome 연결 응답이 올바르지 않습니다.")
            header, rest = self.buffer.split(b"\r\n\r\n", 1)
            lines = header.decode("ascii").split("\r\n")
            fields = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            # Header values are case-sensitive, so read the accept value without lowercasing it.
            accept = next((line.split(":", 1)[1].strip() for line in lines[1:]
                           if line.lower().startswith("sec-websocket-accept:")), "")
            if lines[0].split()[1] != "101" or accept != expected or fields.get("upgrade", "").strip() != "websocket":
                raise TransferError("Chrome 연결을 확인하지 못했습니다.")
            self.buffer = bytearray(rest)
        except BaseException:
            self.sock.close()
            raise

    def _receive(self, deadline, cancel):
        if cancel.is_set():
            raise TransferCancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("Chrome 응답 시간이 초과되었습니다.")
        try:
            block = self.sock.recv(65536)
        except socket.timeout:
            return
        if not block:
            raise TransferError("Chrome 연결이 종료되었습니다.")
        self.buffer.extend(block)

    def send(self, message, opcode=1):
        data = json.dumps(message).encode() if opcode == 1 else message
        size = len(data)
        length = bytes([0x80 | size]) if size < 126 else b"\xfe" + struct.pack("!H", size) if size < 65536 else b"\xff" + struct.pack("!Q", size)
        mask = os.urandom(4)
        frame = bytes([0x80 | opcode]) + length + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        with self.send_lock:
            self.sock.sendall(frame)

    def read(self, deadline, cancel):
        while True:
            while len(self.buffer) < 2:
                self._receive(deadline, cancel)
            first, second = self.buffer[:2]
            size, offset = second & 127, 2
            if second & 128 or first & 0x70:
                raise TransferError("Chrome 메시지 형식이 올바르지 않습니다.")
            extra = 2 if size == 126 else 8 if size == 127 else 0
            while len(self.buffer) < offset + extra:
                self._receive(deadline, cancel)
            if extra:
                size = int.from_bytes(self.buffer[offset:offset + extra], "big")
                offset += extra
            if size + len(self.fragment) > 16 * 1024 * 1024:
                raise TransferError("Chrome 응답이 허용 크기를 초과했습니다.")
            while len(self.buffer) < offset + size:
                self._receive(deadline, cancel)
            payload = bytes(self.buffer[offset:offset + size])
            del self.buffer[:offset + size]
            opcode = first & 15
            if opcode == 8:
                raise TransferError("Chrome 연결이 종료되었습니다.")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0 and not self.fragment) or (opcode == 1 and self.fragment):
                raise TransferError("Chrome 메시지 형식이 올바르지 않습니다.")
            self.fragment.extend(payload)
            if first & 0x80:
                message = json.loads(self.fragment)
                self.fragment.clear()
                return message

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class TextSiteBrowser:
    def __init__(self, profile: Path, *, on_event=None):
        self.profile = Path(profile).resolve()
        self.on_event = on_event
        self.process = None
        self._attached_pid = None
        self.channel = None
        self.session = None
        self.target = None
        self.frame = None
        self.visible = False
        self.closed = False
        self.sequence = 0
        self.document = None
        self._challenge_started = None

    def _log(self, event, **fields):
        if self.on_event:
            self.on_event(event, **fields)

    @staticmethod
    def _chrome_path():
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(name)
            if base:
                path = Path(base) / "Google/Chrome/Application/chrome.exe"
                if path.is_file():
                    return path
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                    value = winreg.QueryValueEx(key, None)[0]
                    path = Path(os.path.expandvars(value.strip().strip('"')))
                    if path.is_file():
                        return path
            except (OSError, TypeError, AttributeError):
                continue
        raise TransferError("설치된 Chrome을 찾지 못했습니다. Chrome 설치 상태를 확인해 주세요.")

    def _check(self, cancel):
        if self.closed or cancel.is_set():
            raise TransferCancelled()

    def _existing_browser(self, chrome):
        if not (self.profile / "Local State").is_file():
            return None
        # Only inspect Chrome processes, and never take over a live app's child.
        script = r'''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$profile = $env:TVMON_TEXT_BROWSER_PROFILE.Replace('/', '\')
$items = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -notmatch '--type=' -and
    $_.CommandLine.Replace('/', '\').IndexOf($profile, [StringComparison]::OrdinalIgnoreCase) -ge 0
} | ForEach-Object {
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $_.ParentProcessId)
    @{ pid = $_.ProcessId; path = $_.ExecutablePath; command = $_.CommandLine;
       parent_alive = ($null -ne $parent -and $parent.CreationDate -le $_.CreationDate) }
})
ConvertTo-Json -InputObject $items -Compress'''
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    encoding="utf-8", timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                    env={**os.environ, "TVMON_TEXT_BROWSER_PROFILE": str(self.profile)})
            processes = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise TransferError("기존 전용 Chrome 프로세스를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.") from error
        split = ctypes.windll.shell32.CommandLineToArgvW
        split.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        split.restype = ctypes.POINTER(ctypes.c_wchar_p)
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        for process in processes:
            command = process.get("command")
            if not command:
                continue
            count = ctypes.c_int()
            argv = split(command, ctypes.byref(count))
            if not argv:
                raise TransferError("기존 Chrome 실행 정보를 읽지 못했습니다.")
            try:
                args = [argv[index] for index in range(count.value)]
            finally:
                free(argv)
            if any(arg.startswith("--type=") for arg in args):
                continue
            options = dict(arg.split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
            profile = options.get("--user-data-dir")
            if not profile or Path(profile).resolve() != self.profile:
                continue
            port = options.get("--remote-debugging-port", "")
            if (Path(process.get("path") or "").resolve() != chrome.resolve()
                    or process.get("parent_alive") is not False
                    or not all(flag in args for flag in ("--no-startup-window", "--no-first-run", "--no-default-browser-check"))
                    or options.get("--remote-debugging-address") != "127.0.0.1"
                    or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535):
                raise TransferError("전용 Chrome 프로필이 사용 중입니다. 다른 실행 중인 프로그램의 B 접속 확인을 마친 뒤 다시 시도해 주세요.")
            return int(process["pid"]), int(port)
        return None

    def _call(self, method, params, cancel, timeout=5):
        self._check(cancel)
        channel = self.channel
        if channel is None:
            self._check(cancel)
            raise TransferError("Chrome 연결이 종료되었습니다.")
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params}
        if self.session and not method.startswith(("Target.", "Browser.", "SystemInfo.")):
            message["sessionId"] = self.session
        channel.send(message)
        started = time.monotonic()
        deadline = started + timeout
        while True:
            try:
                event = channel.read(deadline, cancel)
            except TimeoutError:
                self._log("chrome_timeout", method=method, seconds=round(time.monotonic() - started, 2))
                raise
            values = event.get("params", {})
            if (event.get("method") == "Network.requestWillBeSent" and event.get("sessionId") == self.session
                    and values.get("type") == "Document" and values.get("frameId") == self.frame
                    and values.get("redirectResponse")):
                source = urlsplit(values["redirectResponse"]["url"])
                target = urlsplit(values["request"]["url"])
                self._log("http_redirect", previous_url=f"{source.scheme}://{source.netloc}{source.path}",
                          url=f"{target.scheme}://{target.netloc}{target.path}",
                          status=int(values["redirectResponse"]["status"]))
            if (event.get("method") == "Network.responseReceived" and event.get("sessionId") == self.session
                    and values.get("type") == "Document" and values.get("frameId") == self.frame):
                response = values["response"]
                headers = {key.lower(): value for key, value in response.get("headers", {}).items()}
                self.document = (response["status"], response["url"], headers.get("cf-mitigated") == "challenge")
                if self.document[2] and self._challenge_started is None:
                    self._challenge_started = time.monotonic()
                elif not self.document[2] and 200 <= response["status"] < 300:
                    self._challenge_started = None
                parsed = urlsplit(response["url"])
                self._log("http_response", url=f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                          status=int(response["status"]), challenge=self.document[2])
            if event.get("id") == message["id"]:
                if "error" in event:
                    self._log("chrome_error", method=method, code=event["error"].get("code"))
                    raise TransferError("Chrome에서 페이지 정보를 읽지 못했습니다.")
                elapsed = time.monotonic() - started
                if method != "Runtime.evaluate" or elapsed >= 1:
                    self._log("chrome_step", method=method, seconds=round(elapsed, 2))
                return event.get("result", {})

    def _start(self, cancel):
        self._check(cancel)
        if self.channel and (self._attached_pid or self.process and self.process.poll() is None):
            return
        if self.channel:
            self.channel.close()
        self.channel = self.session = self.target = self.frame = None
        self._attached_pid = None
        self.visible = False
        chrome = self._chrome_path()
        self.profile.mkdir(parents=True, exist_ok=True)
        diagnostic = tempfile.TemporaryFile()
        try:
            existing = self._existing_browser(chrome)
            self._check(cancel)
            if existing:
                pid, port = existing
                self.process = None
                self._log("chrome_reconnect", pid=pid)
            else:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                self.process = subprocess.Popen([str(chrome), "--no-startup-window", f"--remote-debugging-port={port}",
                                                 "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(self.profile),
                                                 "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-sync"],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostic,
                                                startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
                pid = self.process.pid
                self._log("chrome_start", pid=pid)
            deadline = time.monotonic() + 12
            while True:
                self._check(cancel)
                exit_code = self.process.poll() if self.process else None
                if exit_code is not None or time.monotonic() >= deadline:
                    detail = f" (종료 코드 {exit_code})" if exit_code is not None else " (연결 시간 초과)"
                    raise TransferError(f"전용 Chrome에 연결하지 못했습니다{detail}. 접속 기록을 확인하고 다시 시도해 주세요.")
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.5)
                endpoint = None
                try:
                    connection.request("GET", "/json/version")
                    response = connection.getresponse()
                    if response.status == 200:
                        data = response.read(65537)
                        if len(data) <= 65536:
                            endpoint = urlsplit(json.loads(data).get("webSocketDebuggerUrl", ""))
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                if endpoint is not None:
                    if endpoint.scheme != "ws" or endpoint.hostname != "127.0.0.1" or endpoint.port != port:
                        raise TransferError("전용 Chrome 연결 주소가 올바르지 않습니다.")
                    self.channel = _ChromeSocket(port, endpoint.path, cancel)
                    processes = self._call("SystemInfo.getProcessInfo", {}, cancel).get("processInfo", [])
                    if not any(item.get("type") == "browser" and item.get("id") == pid for item in processes):
                        channel, self.channel = self.channel, None
                        channel.close()
                        raise TransferError("전용 Chrome 프로세스를 확인하지 못했습니다.")
                    if existing:
                        self._attached_pid = pid
                    self._check(cancel)
                    break
                if cancel.wait(.1):
                    raise TransferCancelled()
        except BaseException as error:
            if not isinstance(error, TransferCancelled):
                diagnostic.seek(0, os.SEEK_END)
                diagnostic.seek(max(0, diagnostic.tell() - 2048))
                detail = diagnostic.read().decode("utf-8", "replace")
                detail = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1", detail)
                self._log("chrome_start_failed", reason=str(error),
                          exit_code=self.process.poll() if self.process else None,
                          diagnostic=" ".join(detail.split())[-1000:])
            self.close()
            raise
        finally:
            diagnostic.close()

    def get(self, url, cancel, *, limit, visible=False):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise TransferError("브라우저 접속 주소가 올바르지 않습니다.")
        self._log("browser_request", url=f"{parsed.scheme}://{parsed.netloc}{parsed.path}", visible=visible)
        self._start(cancel)
        if self.target:
            targets = self._call("Target.getTargets", {}, cancel).get("targetInfos", [])
            if not any(target.get("targetId") == self.target for target in targets):
                self.target = self.session = self.frame = None
                self.visible = False
        if self.target and visible and not self.visible:
            self._call("Target.closeTarget", {"targetId": self.target}, cancel)
            self.target = self.session = self.frame = None
        if not self.target:
            seed = None
            if not visible:
                seed = self._call("Target.createTarget", {"url": "about:blank", "newWindow": True,
                                                          "background": True, "windowState": "minimized"}, cancel)["targetId"]
            options = {"url": url if visible else "about:blank"}
            options.update({"newWindow": True} if visible else {"hidden": True, "background": True})
            try:
                self.target = self._call("Target.createTarget", options, cancel)["targetId"]
            finally:
                if seed:
                    self._call("Target.closeTarget", {"targetId": seed}, cancel)
            self.visible = visible
            self.session = self._call("Target.attachToTarget", {"targetId": self.target, "flatten": True}, cancel)["sessionId"]
            self._call("Network.enable", {"maxTotalBufferSize": 4 * 1024 * 1024, "maxResourceBufferSize": 2 * 1024 * 1024}, cancel)
            self._call("Page.enable", {}, cancel)
            if not visible:
                self._call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False}, cancel)
            self._call("Browser.setDownloadBehavior", {"behavior": "deny"}, cancel)
            self.frame = self._call("Page.getFrameTree", {}, cancel)["frameTree"]["frame"]["id"]
        if visible:
            self._call("Target.activateTarget", {"targetId": self.target}, cancel)
        self.document = None
        self._challenge_started = None
        deadline = time.monotonic() + (180 if visible else 60)
        try:
            navigation = self._call("Page.navigate", {"url": url}, cancel, timeout=15)
        except TimeoutError:
            # Navigation can still finish after its command response is delayed.
            # Keep observing the same request instead of starting it again.
            navigation = {}
        if navigation.get("errorText"):
            raise TransferError("Chrome에서 사이트에 접속하지 못했습니다.")
        challenge = False
        last_progress = -1
        while True:
            self._check(cancel)
            now = time.monotonic()
            active_deadline = (self._challenge_started + CHALLENGE_WAIT_SECONDS
                               if not visible and self._challenge_started is not None else deadline)
            if now >= active_deadline:
                break
            if self._challenge_started is not None:
                elapsed = int(now - self._challenge_started)
                if elapsed != last_progress:
                    self._log("verification_wait", mode="manual" if visible else "automatic",
                              elapsed=elapsed, remaining=math.ceil(active_deadline - now),
                              limit=180 if visible else CHALLENGE_WAIT_SECONDS)
                    last_progress = elapsed
            try:
                result = self._call("Runtime.evaluate", {"expression": "JSON.stringify({url:location.href,ready:document.readyState,title:document.title,challenge:!!document.querySelector('#challenge-running, #cf-challenge-running, #challenge-form, #turnstile-wrapper')})", "returnByValue": True}, cancel,
                                    timeout=min(5, active_deadline - now))
            except TimeoutError:
                if cancel.wait(.3):
                    raise TransferCancelled()
                continue
            value = result.get("result", {}).get("value")
            if value and self.document:
                state = json.loads(value)
                status, response_url, challenge = self.document
                challenge = challenge or state.get("challenge") or state.get("title", "").strip().lower() in ("just a moment...", "잠시만 기다리십시오…", "잠시만 기다리십시오...")
                if challenge and self._challenge_started is None:
                    self._challenge_started = time.monotonic()
                elif not challenge:
                    self._challenge_started = None
                    last_progress = -1
                if not challenge and state.get("ready") == "complete" and state.get("url") == response_url:
                    if not 200 <= status < 300:
                        raise HttpStatusError(int(status))
                    if urlsplit(response_url).scheme != "https":
                        raise TransferError("브라우저가 HTTPS가 아닌 주소로 이동했습니다.")
                    try:
                        markup = self._call("Runtime.evaluate", {"expression": "document.documentElement.outerHTML", "returnByValue": True}, cancel)
                    except TimeoutError:
                        if cancel.wait(.3):
                            raise TransferCancelled()
                        continue
                    body = markup.get("result", {}).get("value")
                    if not isinstance(body, str) or len(body.encode("utf-8")) > limit:
                        raise TransferError("브라우저 응답이 허용 크기를 초과했습니다.")
                    self._log("browser_success", status=int(status), bytes=len(body.encode("utf-8")))
                    return body.encode("utf-8"), response_url
            if cancel.wait(.3):
                raise TransferCancelled()
        if challenge or (self.document and self.document[2]):
            verification_url = self.document[1] if self.document else url
            parsed = urlsplit(verification_url)
            self._log("verification_pending", url=f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
            raise BrowserChallengeError(verification_url)
        self._log("page_timeout")
        raise TransferError("Chrome 페이지 응답이 지연되어 확인을 완료하지 못했습니다. 잠시 후 다시 확인해 주세요.")

    def get_html(self, url, cancel, *, limit, visible=False):
        """Read search HTML in the authenticated origin without rendering each result page."""
        self._check(cancel)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise TransferError("브라우저 접속 주소가 올바르지 않습니다.")
        if visible:
            return self.get(url, cancel, limit=limit, visible=True)
        self._start(cancel)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        ready = self.session and self.target and self.document and 200 <= self.document[0] < 300
        if ready:
            document_url = urlsplit(self.document[1])
            targets = self._call("Target.getTargets", {}, cancel).get("targetInfos", [])
            ready = (f"{document_url.scheme}://{document_url.netloc}" == origin
                     and any(target.get("targetId") == self.target for target in targets))
        if not ready:
            self.get(origin + "/", cancel, limit=limit)
        self._log("browser_request", url=origin + parsed.path, visible=False)
        expression = """(async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 55000);
            try {
                const response = await fetch(URL_VALUE, {credentials: 'same-origin', signal: controller.signal});
                if (Number(response.headers.get('content-length')) > BYTE_LIMIT) return {error: 'size'};
                const reader = response.body && response.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let total = 0, body = '';
                if (reader) while (true) {
                    const chunk = await reader.read();
                    if (chunk.done) break;
                    total += chunk.value.byteLength;
                    if (total > BYTE_LIMIT) return {error: 'size'};
                    body += decoder.decode(chunk.value, {stream: true});
                }
                body += decoder.decode();
                return {status: response.status, url: response.url,
                        challenge: response.headers.get('cf-mitigated') === 'challenge', body};
            } finally { clearTimeout(timer); controller.abort(); }
        })()""".replace("BYTE_LIMIT", str(int(limit))).replace("URL_VALUE", json.dumps(url))
        result = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, cancel, timeout=60)
        self._check(cancel)
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict) or "exceptionDetails" in result:
            raise TransferError("Chrome에서 검색 응답을 읽지 못했습니다. 잠시 후 다시 검색해 주세요.")
        if value.get("error") == "size":
            raise TransferError("브라우저 응답이 허용 크기를 초과했습니다.")
        status, final_url, body = value.get("status"), value.get("url"), value.get("body")
        if not isinstance(status, (int, float)) or not isinstance(final_url, str) or not isinstance(body, str):
            raise TransferError("Chrome 검색 응답 형식이 올바르지 않습니다.")
        final = urlsplit(final_url)
        self._log("http_response", url=f"{final.scheme}://{final.netloc}{final.path}",
                  status=int(status), challenge=bool(value.get("challenge")))
        if value.get("challenge"):
            raise BrowserChallengeError(url)
        if not 200 <= status < 300:
            raise HttpStatusError(int(status))
        if final.scheme != "https":
            raise TransferError("브라우저가 HTTPS가 아닌 주소로 이동했습니다.")
        body = body.encode("utf-8")
        if len(body) > limit:
            raise TransferError("브라우저 응답이 허용 크기를 초과했습니다.")
        self._log("browser_success", status=int(status), bytes=len(body))
        return body, final_url

    def get_novel_text(self, url, cancel, *, limit, visible=False):
        """Wait for the site's rendered novel reader and return only its reading text."""
        deadline = time.monotonic() + (180 if visible else 60)
        self.get(url, cancel, limit=limit, visible=visible)
        expected = urlsplit(url)
        expression = """({url: location.href,
            reader: !!document.querySelector('article.novel-viewer'),
            text: typeof window.__novelTTSText === 'string' ? window.__novelTTSText : '',
            loading: document.querySelector('article.novel-viewer .novel-loading')?.textContent || ''})"""
        while time.monotonic() < deadline:
            self._check(cancel)
            try:
                result = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, cancel)
            except TimeoutError:
                if cancel.wait(.3):
                    raise TransferCancelled()
                continue
            state = result.get("result", {}).get("value")
            if not isinstance(state, dict):
                raise TransferError("소설 본문 상태를 읽지 못했습니다.")
            actual = urlsplit(state.get("url", ""))
            if (actual.scheme, actual.netloc, actual.path.rstrip("/")) != (expected.scheme, expected.netloc, expected.path.rstrip("/")):
                raise TransferError("소설 회차가 다른 페이지로 이동했습니다. 로그인과 접속 주소를 확인해 주세요.")
            if not state.get("reader"):
                raise TransferError("소설 본문 뷰어를 찾지 못했습니다. 사이트에서 해당 회차의 이용 권한을 확인해 주세요.")
            text = state.get("text")
            if isinstance(text, str) and text.strip() and not state.get("loading"):
                body = text.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")
                if len(body) > limit:
                    raise TransferError("소설 본문이 허용 크기를 초과했습니다.")
                self._log("novel_body_ready", bytes=len(body))
                return body, state["url"]
            loading = state.get("loading", "")
            if not visible and "인증" in loading:
                self._log("verification_pending")
                raise BrowserChallengeError(url)
            if not visible and ("오류" in loading or "원본 페이지" in loading):
                raise TransferError("사이트에서 소설 본문을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
            if cancel.wait(.3):
                raise TransferCancelled()
        raise TransferError("소설 본문 로딩 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.")

    def close(self):
        self.closed = True
        channel, self.channel = self.channel, None
        if channel:
            try:
                if self.process:
                    channel.send({"id": 0, "method": "Browser.close"})
                elif self.target:
                    channel.send({"id": 0, "method": "Target.closeTarget", "params": {"targetId": self.target}})
            except OSError:
                pass
            channel.close()
        if self.process:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

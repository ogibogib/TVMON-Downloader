"""TVMON Windows downloader. Run with Python 3.11+ or the packaged EXE."""

from __future__ import annotations

import concurrent.futures
import atexit
from collections import Counter, deque
from contextlib import ExitStack
from functools import lru_cache
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
import uuid
import zipfile
from tkinter import filedialog, font as tkfont, messagebox, ttk
from urllib.parse import urlencode, urljoin, urlsplit

from tvmon_crypto import decode_key, decrypt_aes128
from tvmon_browser import CHALLENGE_WAIT_SECONDS, TextSiteBrowser
from tvmon_http import BrowserChallengeError, HttpStatusError, TransferCancelled, TransferError, WinHttpClient, get_with_ech
from tvmon_resume import ResumeCache, ResumeError, discard_cache
from tvmon_p2p import PEER_WAIT_SECONDS, P2P_SCRIPT_URL, P2PVideoReceiver, video_swarm_id
from tvmon_domains import (CANONICAL_ORIGIN, DEFAULT_DOMAINS, DOMAIN_GUIDE_URL, TEXT_DOMAIN_GUIDE_URL, domain_requests,
                           is_site_url, is_text_site_url, next_numbered_domains, normalize_domain, normalize_domains,
                           normalize_text_guide_url, resolve_current_domain, resolve_current_text_domain, set_domains, verify_domain)


EXAMPLE_URL = "https://tvmon1.com/ent/986/406784622"
PLAYER_ORIGIN = "https://player.bunny-frame.online"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
WORKERS = 8
STORAGE_BUFFER_BYTES = 32 * 1024 * 1024
SERVER_STALL_SECONDS = 6
SERVER_SLOW_SECONDS = 10
SERVER_COOLDOWN_SECONDS = 30
SERVER_HEDGE_SECONDS = 3
PART_COLORS = {"waiting": "#E2E8F0", "http_active": "#2874D0", "http_complete": "#3D9B72",
               "p2p_active": "#9B7BDF", "p2p_complete": "#6D28D9", "error": "#D97730",
               "unknown_complete": "#8291A3"}
DOWNLOAD_RETRY_SECONDS = 30
TEXT_ZIP_RETRY_DELAYS = (3, 10, 30)


class DownloadError(Exception):
    pass


class _ConnectionDownloadError(DownloadError):
    pass


class BrowserVerificationRequired(DownloadError):
    def __init__(self, url: str, detail=""):
        message = "브라우저 검증이 필요합니다. ‘브라우저 검증 열기’에서 검증을 완료해 주세요. 번호 탐색은 중단했습니다."
        super().__init__(message + ("\n" + detail if detail else ""))
        self.url = url


class Cancelled(Exception):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled()


def parse_page_url(value: str) -> tuple[str, str, str, str]:
    """Return canonical page URL and its board, post, episode identifiers."""
    try:
        parsed = urlsplit(value.strip())
        valid_host = is_site_url(value.strip())
        valid_port = parsed.port in (None, 443)
    except ValueError:
        raise DownloadError("올바른 TVMON 영상 링크를 입력해 주세요.") from None
    match = re.fullmatch(r"/([A-Za-z0-9_]+)/([0-9]+)/([0-9]+)/?", parsed.path)
    if (parsed.scheme != "https" or not valid_host or not valid_port
            or parsed.username or parsed.password or not match):
        raise DownloadError("등록된 도메인의 /ent/986/406784622 형태 영상 링크를 입력해 주세요.")
    board, post, episode = match.groups()
    return f"{CANONICAL_ORIGIN}/{board}/{post}/{episode}", board, post, episode


def parse_text_page_url(value: str) -> tuple[str, str, str]:
    """Validate a B novel episode without binding saved jobs to a numbered host."""
    try:
        parsed = urlsplit(value.strip())
        origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
        match = re.fullmatch(r"/novel/([0-9]+)/([0-9]+)/?", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise ValueError()
    except (ValueError, AttributeError):
        raise DownloadError("올바른 B 소설 회차 주소를 확인해 주세요.") from None
    work, episode = match.groups()
    return f"{origin}/novel/{work}/{episode}", work, episode


@lru_cache(maxsize=1)
def _get_http_client() -> WinHttpClient:
    client = WinHttpClient(USER_AGENT)
    atexit.register(client.close)
    return client


def _p2p_script(cancel):
    body, _ = fetch(P2P_SCRIPT_URL, cancel, PLAYER_ORIGIN + "/", limit=512 * 1024, retry=False)
    return body.decode("utf-8")


def fetch(url: str, cancel: threading.Event, referer: str, *, limit: int = 32 * 1024 * 1024, on_bytes=None, retry: bool = True, on_event=None, on_wait=None) -> tuple[bytes, str]:
    """Try registered site domains in order; keep external media routing intact."""
    candidates = domain_requests(url, referer)
    failures = []
    for candidate, candidate_referer in candidates:
        check_cancel(cancel)
        try:
            result = _fetch_single(candidate, cancel, candidate_referer, limit=limit,
                                   on_bytes=on_bytes, retry=retry and len(candidates) == 1,
                                   **({"on_event": on_event} if on_event is not None else {}),
                                   **({"on_wait": on_wait} if on_wait is not None else {}))
            if is_site_url(url) and not is_site_url(result[1]):
                raise DownloadError("등록하지 않은 도메인으로 연결되었습니다.")
            return result
        except DownloadError as error:
            if len(candidates) == 1:
                raise
            failures.append(f"{urlsplit(candidate).hostname}: {error}")
    check_cancel(cancel)
    raise DownloadError("등록된 도메인에 모두 접속하지 못했습니다.\n" + "\n".join(failures))


def _fetch_single(url: str, cancel: threading.Event, referer: str, *, limit: int,
                  on_bytes=None, retry: bool = True, on_event=None, on_wait=None) -> tuple[bytes, str]:
    """Read with WinHTTP; retry failed site connections through bundled ECH TLS."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError("서버에서 올바르지 않은 영상 주소를 받았습니다.")
    origin_parts = urlsplit(referer)
    headers = {"User-Agent": USER_AGENT, "Referer": referer,
               "Origin": f"{origin_parts.scheme}://{origin_parts.netloc}", "Accept": "*/*",
               "Accept-Encoding": "identity"}
    attempts = 3 if retry else 1
    def trace(event, **fields):
        if on_event is not None:
            on_event(event, attempt=attempt + 1, host=parsed.hostname,
                     seconds=round(time.monotonic() - request_started, 4), **fields)
    for attempt in range(attempts):
        check_cancel(cancel)
        if on_wait is not None:
            on_wait()
        request_started = time.monotonic()
        trace("http_request")
        try:
            try:
                result = _get_http_client().get(url, headers, cancel, limit, on_bytes,
                                                **({"on_wait": on_wait} if on_wait is not None else {}))
            except (BrowserChallengeError, HttpStatusError):
                raise
            except (TransferError, TimeoutError, OSError) as native_error:
                check_cancel(cancel)
                if not (is_site_url(url) or re.fullmatch(r"(?:www\.)?tvwiki(?:[0-9]+|url)\.net", parsed.hostname)):
                    raise
                trace("http_ech_request", native_code=getattr(native_error, "code", None))
                try:
                    result = get_with_ech(url, headers, cancel, limit, on_bytes,
                                          **({"on_wait": on_wait} if on_wait is not None else {}))
                except (TransferCancelled, BrowserChallengeError, HttpStatusError):
                    raise
                except (TransferError, TimeoutError, OSError) as library_error:
                    trace("http_ech_error", detail=str(library_error))
                    raise native_error from None
                trace("http_ech_response", bytes=len(result[0]))
            trace("http_response", bytes=len(result[0]))
            return result
        except TransferCancelled:
            trace("http_cancelled")
            raise Cancelled() from None
        except BrowserChallengeError as error:
            trace("http_error", error="browser_verification")
            check_cancel(cancel)
            raise BrowserVerificationRequired(error.url) from None
        except HttpStatusError as error:
            trace("http_error", error="http_status", status=error.status)
            check_cancel(cancel)
            if error.status not in (408, 429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise DownloadError(f"{parsed.hostname} 서버가 HTTP {error.status} 오류를 반환했습니다. 링크를 확인하고 다시 시도해 주세요.") from None
        except (TransferError, TimeoutError, OSError, ValueError) as error:
            trace("http_error", error=type(error).__name__, code=getattr(error, "code", None))
            check_cancel(cancel)
            if attempt == attempts - 1:
                detail = f" (Windows 오류 {error.code})" if isinstance(error, TransferError) and error.code else ""
                error_type = DownloadError if isinstance(error, ValueError) else _ConnectionDownloadError
                raise error_type(f"{parsed.hostname} 연결이 끊겼거나 응답이 지연됩니다{detail}. 네트워크를 확인하고 다시 시도해 주세요.") from None
        trace("http_retry", delay_seconds=0.5 * (attempt + 1))
        if cancel.wait(0.5 * (attempt + 1)):
            raise Cancelled()
    raise DownloadError("영상 서버에 연결할 수 없습니다.")


def _find_available_domain(domains: tuple[str, ...], cancel: threading.Event, *, report=None, text_site=False,
                           browser_fetch=None, text_guide_url=TEXT_DOMAIN_GUIDE_URL,
                           video_guide_url=DOMAIN_GUIDE_URL) -> tuple[str, str | None, str]:
    """Check saved domains, the guide, then at most three adjacent numbered hosts."""
    failures = []
    pending = None
    browser_url = None
    verification = None
    attempt = 0
    phase = "등록된 도메인"
    guide_url = normalize_text_guide_url(text_guide_url if text_site else video_guide_url)
    resolve_guide = resolve_current_text_domain if text_site else resolve_current_domain

    def finish(state, detail):
        nonlocal pending
        if pending is not None:
            if pending.get("final_url", pending["url"]) != pending["url"]:
                detail += f" → {pending['final_url']}"
            if report:
                report({**pending, "state": state, "detail": detail})
            pending = None

    def observed_fetch(url, *args, **kwargs):
        nonlocal pending, attempt, browser_url, verification
        check_cancel(cancel)
        finish("success", "다음 주소 안내 확인")
        attempt += 1
        pending = {"id": str(attempt), "phase": phase, "url": url,
                   "state": "checking", "detail": "연결 및 응답 확인 중"}
        if report:
            report(dict(pending))

        def connection_event(event, **fields):
            if report and event in ("http_ech_request", "http_ech_response", "http_ech_error"):
                detail = {"http_ech_request": "연결 오류 · 내장 라이브러리(ECH/DoH)로 같은 주소를 다시 확인합니다.",
                          "http_ech_response": "내장 라이브러리로 응답 수신 · 콘텐츠 홈페이지 확인 중",
                          "http_ech_error": "내장 라이브러리 접속 실패"}[event]
                report({**pending, "detail": detail})

        try:
            body, final_url = _fetch_single(url, *args, on_event=connection_event, **kwargs)
        except Cancelled:
            cancel.set()
            raise
        except (BrowserVerificationRequired, _ConnectionDownloadError) as error:
            if not text_site:
                raise
            request_url = error.url if isinstance(error, BrowserVerificationRequired) else url
            if browser_fetch is None:
                if isinstance(error, BrowserVerificationRequired):
                    browser_url = request_url
                    verification = (error, dict(pending))
                raise
            browser_url = request_url
            if report:
                reason = "연결 오류 · 같은 주소를 Chrome으로 다시 확인합니다." if isinstance(error, _ConnectionDownloadError) else "브라우저 확인 요청 · 저장된 인증 정보로 접속합니다."
                report({**pending, "detail": reason})
            try:
                body, final_url = browser_fetch(request_url, cancel, limit=kwargs["limit"])
            except BrowserChallengeError as challenge:
                if not is_text_site_url(challenge.url):
                    raise DownloadError("지원하지 않는 B 사이트로 이동했습니다. toki숫자.com 주소만 사용할 수 있습니다.") from None
                pending["final_url"] = challenge.url
                required = BrowserVerificationRequired(challenge.url)
                verification = (required, dict(pending))
                raise required from None
            except TransferCancelled:
                cancel.set()
                raise Cancelled() from None
            except HttpStatusError as response:
                raise DownloadError(f"브라우저에서 HTTP {response.status} 오류를 반환했습니다.") from None
            except (TransferError, TimeoutError, OSError, ValueError) as browser_error:
                check_cancel(cancel)
                raise DownloadError(f"Chrome에서 페이지를 확인하지 못했습니다. {browser_error}") from None
            if verification and verification[0].url == request_url:
                verification = None
        pending["final_url"] = final_url
        return body, final_url

    def stop_browser_search(error):
        if not text_site or not browser_url:
            return
        if isinstance(error, BrowserVerificationRequired):
            finish("verification", str(error))
            raise error
        if verification:
            finish("error", str(error))
            required, event = verification
            if report:
                report({**event, "state": "verification", "detail": str(required)})
            raise required
        detail = f"브라우저 확인을 포함해 안내된 주소의 홈페이지를 확인하지 못했습니다. 번호 탐색은 중단했습니다. {error}"
        finish("error", detail)
        raise DownloadError(detail) from None

    for domain in domains:
        check_cancel(cancel)
        try:
            origin = verify_domain(domain, observed_fetch, cancel, check_cancel=check_cancel,
                                   on_failure=lambda detail: finish("error", detail), text_site=text_site, guide_url=guide_url)
            finish("success", "콘텐츠 홈페이지 확인")
            return origin, domain, "등록된 도메인"
        except Cancelled:
            finish("cancelled", "확인을 취소했습니다.")
            raise
        except (DownloadError, ValueError, OSError) as error:
            stop_browser_search(error)
            if text_site and isinstance(error, BrowserVerificationRequired):
                finish("verification", str(error))
                raise
            finish("error", str(error))
            failures.append(f"{urlsplit(domain).hostname}: {error}")

    guide_origin = None

    def guide_fetch(url, *args, **kwargs):
        nonlocal guide_origin
        if url != guide_url:
            parsed = urlsplit(url)
            guide_origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
        body, final_url = observed_fetch(url, *args, **kwargs)
        if url != guide_url:
            parsed = urlsplit(final_url)
            guide_origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
        return body, final_url

    check_cancel(cancel)
    phase = "안내 주소"
    try:
        origin = resolve_guide(guide_fetch, cancel, check_cancel=check_cancel,
                               on_failure=lambda detail: finish("error", detail), guide_url=guide_url)
        finish("success", "콘텐츠 홈페이지 확인")
        return origin, None, "안내 주소"
    except Cancelled:
        finish("cancelled", "확인을 취소했습니다.")
        raise
    except (DownloadError, ValueError, OSError) as error:
        stop_browser_search(error)
        if text_site and isinstance(error, BrowserVerificationRequired):
            finish("verification", str(error))
            raise
        finish("error", str(error))
        failures.append(f"안내 주소: {error}")
    check_cancel(cancel)
    for offset, domain in enumerate(next_numbered_domains(guide_origin) if guide_origin else (), 1):
        check_cancel(cancel)
        phase = f"안내 주소 +{offset}"
        try:
            origin = verify_domain(domain, observed_fetch, cancel, check_cancel=check_cancel,
                                   on_failure=lambda detail: finish("error", detail), text_site=text_site, guide_url=guide_url)
            finish("success", "콘텐츠 홈페이지 확인")
            return origin, None, phase
        except Cancelled:
            finish("cancelled", "확인을 취소했습니다.")
            raise
        except (DownloadError, ValueError, OSError) as error:
            stop_browser_search(error)
            if text_site and isinstance(error, BrowserVerificationRequired):
                finish("verification", str(error))
                raise
            finish("error", str(error))
            failures.append(f"{urlsplit(domain).hostname}: {error}")
    check_cancel(cancel)
    raise DownloadError("등록된 도메인과 안내 주소, 확인 가능한 다음 번호 주소에 모두 접속하지 못했습니다.\n"
                        + "\n".join(failures))


def resolve_episode(value: str, cancel: threading.Event) -> tuple[str, str]:
    page, board, post, episode_id = parse_page_url(value)
    page = domain_requests(page, page)[0][0]
    query = urlencode({"bo_table": board, "wr_id": post, "ep_idx": episode_id})
    body, _ = fetch(urljoin(page, f"/bbs/get_episode.php?{query}"), cancel, page, limit=4 * 1024 * 1024)
    try:
        result = json.loads(body)
        episode = result["episode"]
        if not result.get("success") or not episode.get("hls_url"):
            raise ValueError()
        title = html.unescape(str(episode["title"]))
        return title, str(episode["hls_url"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DownloadError("이 링크에서 영상을 찾지 못했습니다. 회차가 포함된 영상 링크를 확인해 주세요.") from None


def attributes(line: str) -> dict[str, str]:
    return {match[1]: match[2] if match[2] is not None else match[3]
            for match in re.finditer(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


def parse_manifest(body: bytes, base_url: str) -> list[tuple[str, str | None, bytes, float]]:
    """Parse this site's complete MPEG-TS VOD playlist, including key rotation/IVs."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DownloadError("영상 목록을 읽을 수 없습니다.") from None
    if not text.strip().startswith("#EXTM3U"):
        raise DownloadError("영상 서버가 재생 목록 대신 다른 응답을 반환했습니다.")
    if "#EXT-X-ENDLIST" not in text:
        raise DownloadError("완료된 다시보기 영상만 저장할 수 있습니다.")
    segments = []
    key_url = None
    explicit_iv = None
    sequence = 0
    duration = None
    try:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("#EXT-X-MAP:", "#EXT-X-BYTERANGE:", "#EXT-X-STREAM-INF:", "#EXT-X-GAP")):
                raise DownloadError("이 영상의 재생 목록 형식은 지원하지 않습니다. MPEG-TS 다시보기 영상을 사용해 주세요.")
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sequence = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-KEY:"):
                fields = attributes(line)
                method = fields.get("METHOD")
                if method == "NONE":
                    key_url = explicit_iv = None
                elif method == "AES-128" and fields.get("KEYFORMAT", "identity") == "identity":
                    key_url = urljoin(base_url, fields["URI"])
                    iv_text = fields.get("IV")
                    explicit_iv = int(iv_text, 16).to_bytes(16, "big") if iv_text else None
                else:
                    raise DownloadError("이 영상의 암호화 형식은 지원하지 않습니다.")
            elif line.startswith("#EXTINF:"):
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError()
            elif not line.startswith("#"):
                if duration is None:
                    raise ValueError()
                segments.append((urljoin(base_url, line), key_url,
                                 explicit_iv if explicit_iv is not None else sequence.to_bytes(16, "big"), duration))
                sequence += 1
                duration = None
        if not segments or duration is not None:
            raise ValueError()
        return segments
    except (ValueError, KeyError, OverflowError):
        raise DownloadError("영상 목록의 조각 또는 키 정보가 올바르지 않습니다.") from None


def safe_filename(title: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")[:120].rstrip(". ")
    if not value:
        value = "TVMON 영상"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value.split(".")[0]):
        value = "_" + value
    return value


def validate_ts(data: bytes) -> None:
    if not data or len(data) % 188 or any(data[i] != 0x47 for i in range(0, len(data), 188)):
        raise DownloadError("영상 조각이 손상되었거나 서버가 영상 대신 오류 페이지를 반환했습니다.")


def _load_video_source(manifest_url: str, cancel: threading.Event, report):
    """Keep the advertised source first; validate same-content alternatives on failure."""
    original = urlsplit(manifest_url)
    known_source = (original.scheme == "https" and original.netloc == original.hostname
                    and re.fullmatch(r"every[1-9]\.poorcdn\.com", original.hostname or "")
                    and re.fullmatch(r"/v/[a-z]+/[0-9a-f]{32}/c\.html", original.path))
    candidates = [manifest_url]
    if known_source:
        candidates += [original._replace(netloc=f"every{server}.poorcdn.com").geturl()
                       for server in range(1, 10) if original.hostname != f"every{server}.poorcdn.com"]
    first_error = None
    for candidate in candidates:
        check_cancel(cancel)
        alternate = candidate != manifest_url
        host = urlsplit(candidate).hostname
        if alternate:
            report("status", f"기본 영상 서버에 연결하지 못해 다른 서버를 확인합니다… {host}")
        try:
            manifest, base_url = fetch(candidate, cancel, PLAYER_ORIGIN + "/", limit=4 * 1024 * 1024,
                                       **({"retry": False} if alternate else {}))
            segments = parse_manifest(manifest, base_url)
            if alternate:
                resolved = urlsplit(base_url)
                content_path = original.path.rsplit("/", 1)[0] + "/"
                if (resolved.scheme != "https" or resolved.netloc != host or resolved.path != original.path
                        or any(not urlsplit(item[0]).path.startswith(content_path) for item in segments)):
                    raise DownloadError("대체 서버의 영상 식별자가 원래 영상과 일치하지 않습니다.")
            keys = {}
            for key_url in dict.fromkeys(item[1] for item in segments if item[1]):
                url = key_url + ("&" if "?" in key_url else "?") + "mode=obfuscated" if "/v/key7?" in key_url and "mode=" not in key_url else key_url
                payload, _ = fetch(url, cancel, PLAYER_ORIGIN + "/", limit=1024 * 1024,
                                   **({"retry": False} if alternate else {}))
                try:
                    keys[key_url] = decode_key(payload)
                except (ValueError, TypeError, KeyError):
                    raise DownloadError("사이트의 영상 키 응답을 읽지 못했습니다.") from None
            if alternate:
                # A playlist alone can exist while its media is missing or blocked.
                for index in sorted({0, len(segments) // 2, len(segments) - 1}):
                    url, key_url, iv, _ = segments[index]
                    data, _ = fetch(url, cancel, PLAYER_ORIGIN + "/", retry=False)
                    if key_url:
                        try:
                            data = decrypt_aes128(data, keys[key_url], iv)
                        except (ValueError, OSError):
                            raise DownloadError("대체 서버의 영상 조각을 복호화하지 못했습니다.") from None
                    validate_ts(data)
                report("status", f"사용 가능한 영상 서버를 찾았습니다: {host} · 다운로드를 시작합니다.")
            check_cancel(cancel)
            return segments, base_url, keys
        except BrowserVerificationRequired:
            raise
        except DownloadError as error:
            if first_error is None:
                first_error = error
            if not known_source:
                raise
            if alternate:
                report("status", f"{host} 확인 실패: {error}")
    raise DownloadError(f"기본 서버와 다른 영상 서버에서 다운로드 가능한 영상을 찾지 못했습니다.\n{first_error}") from None


def discard_resume(folder: Path, resume_id: str) -> None:
    """Remove an inactive job's owned checkpoints without touching its video."""
    try:
        discard_cache(folder, resume_id)
    except ResumeError as error:
        raise DownloadError(str(error)) from None


def download_video(value: str, folder: Path, cancel: threading.Event, report, *, resume_id: str | None = None,
                   log_dir: Path | None = None, fastest_only: threading.Event | None = None) -> Path:
    """Save each validated segment and optionally keep durable resume checkpoints."""
    check_cancel(cancel)
    log_handle = None
    log_lock = threading.Lock()
    log_started = time.monotonic()
    log_last_flush = log_last_sample = 0.0

    def trace(event, **fields):
        nonlocal log_handle, log_last_flush
        if log_handle is None:
            return
        now = time.monotonic()
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "elapsed": round(now - log_started, 4), "event": event, **fields}
        try:
            with log_lock:
                if log_handle is None:
                    return
                log_handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                if now - log_last_flush >= 2 or event == "run_end":
                    log_handle.flush()
                    log_last_flush = now
        except OSError:
            with log_lock:
                handle, log_handle = log_handle, None
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
            report("status", "상세 로그 저장에 실패했습니다. 다운로드는 계속합니다. 로그 폴더의 여유 공간과 권한을 확인하세요.")

    if log_dir is not None:
        try:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_id = resume_id if isinstance(resume_id, str) and re.fullmatch(r"[0-9a-f]{32}", resume_id) else uuid.uuid4().hex
            log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{log_id}-{uuid.uuid4().hex[:8]}.jsonl"
            log_handle = log_path.open("x", encoding="utf-8")
            trace("run_start", job=resume_id, workers=WORKERS, scheduler="ready_first", p2p_window_limit=32)
            if log_handle is not None:
                report("status", f"상세 로그 저장: {log_path}")
        except OSError:
            report("status", "상세 로그 파일을 만들지 못했습니다. 다운로드는 계속합니다. 로그 폴더의 여유 공간과 권한을 확인하세요.")
    stack = ExitStack()
    cache = None
    output = None
    partial = None
    pool = None
    storage_pool = None
    http_pool = None
    pending = {}
    storing = {}
    storage_bytes = 0
    p2p_saved = set()
    cache_verified = False
    published = False
    try:
        if resume_id is not None:
            cache = stack.enter_context(ResumeCache(folder, resume_id))
            folder = cache.folder
        page = domain_requests(value, value)[0][0]
        report("status", f"영상 정보를 확인하고 있습니다… 접속 주소: {page}")
        title, manifest_url = resolve_episode(value, cancel)
        trace("episode", page=parse_page_url(value)[0], title=title, manifest_host=urlsplit(manifest_url).hostname)
        report("status", f"영상 목록을 확인하고 있습니다… 서버: {urlsplit(manifest_url).hostname}")
        segments, base_url, keys = _load_video_source(manifest_url, cancel, report)
        trace("source_selected", manifest_host=urlsplit(base_url).hostname, alternate=base_url != manifest_url)
        total = len(segments)
        servers = dict(Counter(urlsplit(item[0]).hostname for item in segments))
        report("info", {"title": title, "duration": sum(item[3] for item in segments), "segments": total,
                        "server_count": len(servers), "servers": servers})
        trace("manifest", segments=total, duration=sum(item[3] for item in segments), servers=servers)
        check_cancel(cancel)
        folder = folder.expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(title)
        number = 0
        while True:
            target = folder / (stem + (f" ({number})" if number else "") + ".ts")
            partial = target.with_suffix(".ts.part")
            if target.exists() or (cache is not None and partial.exists()):
                number += 1
                continue
            if cache is not None:
                break
            try:
                output = partial.open("xb")
                break
            except FileExistsError:
                number += 1
        verified = {}
        if cache is not None:
            fingerprint = hashlib.sha256(json.dumps([
                [urlsplit(url).path, duration, iv.hex(),
                 hashlib.sha256(keys[key_url]).hexdigest() if key_url else None]
                for url, key_url, iv, duration in segments
            ], separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
            cache.bind(parse_page_url(value)[0], fingerprint, title, total, target.name)
            target = cache.target_path()
            published = cache.recover_publication(lambda: check_cancel(cancel))
            if published is not None:
                size = published.stat().st_size
                report("file", str(published))
                report("parts", {"total": total, "completed": list(range(total))})
                report("progress", {"done": total, "total": total, "bytes": size, "eta": 0, "resumed": total})
                report("complete", str(published))
                return published
            report("status", "저장된 영상 조각을 확인하고 있습니다…")
            verified = cache.verified_parts(validate_ts, lambda: check_cancel(cancel))
            cache_verified = True
            spool_dir = cache.root
            partial = cache.merge_path()
        else:
            spool_name = stack.enter_context(tempfile.TemporaryDirectory(prefix="tvmon-parts-", dir=folder))
            spool_dir = Path(spool_name).resolve()
            if spool_dir.parent != folder:
                raise DownloadError("임시 영상 폴더의 저장 위치가 올바르지 않습니다.")
        report("file", str(target))
        report("parts", {"total": total, "completed": sorted(verified)})
        trace("resume", verified_segments=len(verified), verified_bytes=sum(verified.values()))
        transfer_lock = threading.Lock()
        received = 0
        http_received = 0
        slots = [{"slot": slot, "index": None, "host": "", "state": "waiting", "source": "",
                  "bytes": 0, "http_bytes": 0, "p2p_bytes": 0, "transition": "", "speed": 0.0}
                 for slot in range(8)]
        slot_samples = [deque() for _ in slots]
        source = urlsplit(base_url)
        source_number = re.fullmatch(r"every([1-9])\.poorcdn\.com", source.hostname or "")
        content_path = source.path.rsplit("/", 1)[0] + "/"
        adaptive_hosts = list(servers) if (source_number and len(servers) > 1
            and re.fullmatch(r"/v/[a-z]+/[0-9a-f]{32}/", content_path)
            and all(urlsplit(item[0]).scheme == "https" and urlsplit(item[0]).netloc == urlsplit(item[0]).hostname
                    and (urlsplit(item[0]).hostname or "").startswith(f"c{source_number[1]}.")
                    and urlsplit(item[0]).path.startswith(content_path) for item in segments)) else []
        server_health = {host: {"active": 0, "speed": 0.0, "samples": 0, "slow_until": 0.0,
                                "reason": "", "switches": 0, "slow_count": 0} for host in adaptive_hosts}
        if adaptive_hosts:
            http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="video-http")

        def slow_server(host, reason):
            with transfer_lock:
                health = server_health[host]
                health.update(slow_until=time.monotonic() + SERVER_COOLDOWN_SECONDS, reason=reason)
                health["slow_count"] += 1
            trace("server_slow", host=host, reason=reason, cooldown_seconds=SERVER_COOLDOWN_SECONDS)

        def reserve_server(original, excluded, *, spare=False):
            now = time.monotonic()
            with transfer_lock:
                if spare and sum(item["active"] for item in server_health.values()) >= WORKERS:
                    return None
                candidates = [host for host in adaptive_hosts if host not in excluded]
                if not candidates:
                    return None
                healthy = [host for host in candidates if server_health[host]["slow_until"] <= now]
                candidates = healthy or candidates
                measured = [host for host in candidates if server_health[host]["samples"]]
                if fastest_only is not None and fastest_only.is_set() and measured:
                    host = max(measured, key=lambda name: (server_health[name]["speed"], -server_health[name]["active"]))
                    server_health[host]["active"] += 1
                    return host
                limit = max(1, math.ceil(WORKERS / max(len(healthy), 1)))
                rates = [item["speed"] for item in server_health.values() if item["samples"]]
                typical = sum(rates) / len(rates) if rates else 1024 * 1024
                def score(host):
                    health = server_health[host]
                    recovering = health["reason"] and health["slow_until"] <= now and not health["active"]
                    return (not recovering, health["active"] >= limit,
                            (health["active"] + 1) / max(health["speed"] or typical, 1), host != original)
                host = min(candidates, key=score)
                server_health[host]["active"] += 1
                return host

        def receive_p2p_bytes(size):
            nonlocal received
            with transfer_lock:
                received += size

        p2p = None
        if len(verified) < total and video_swarm_id(base_url, segments):
            def peer_status(message):
                trace("p2p_status", message=message)
                report("status", message)
            p2p = P2PVideoReceiver(base_url, segments, verified, cancel=cancel, load_script=_p2p_script,
                                   on_bytes=receive_p2p_bytes, on_status=peer_status, on_event=trace)
            stack.callback(p2p.close)

        class PeerSegmentReady(Exception):
            pass

        def load_segment(index, slot):
            nonlocal received, http_received
            check_cancel(cancel)
            url, key_url, iv, _ = segments[index]
            peer_body = None
            peer_finished = False
            allow_peer_switch = True
            peer_retry_allowed = False
            http_attempt = 0
            http_started = time.monotonic()
            first_http_byte = False
            http_verified = False
            with transfer_lock:
                slots[slot]["started"] = time.monotonic()

            def http_event(event, **fields):
                nonlocal http_attempt, http_started, first_http_byte
                if event == "http_request":
                    http_attempt = fields["attempt"]
                    http_started = time.monotonic()
                    first_http_byte = False
                trace(event, segment=index + 1, worker=slot + 1, **fields)

            def check_peer_ready():
                nonlocal peer_body, peer_retry_allowed
                if p2p is not None and allow_peer_switch:
                    if not peer_retry_allowed and time.monotonic() - http_started >= PEER_WAIT_SECONDS:
                        p2p.release_http(index)
                        peer_retry_allowed = True
                        trace("http_peer_fallback", segment=index + 1, worker=slot + 1,
                              seconds=round(time.monotonic() - http_started, 4))
                    peer_body = p2p.get(index, wait=False)
                    if peer_body is not None:
                        raise PeerSegmentReady()

            def receive_bytes(size):
                nonlocal received, http_received, peer_body, first_http_byte
                with transfer_lock:
                    received += size
                    http_received += size
                    slots[slot]["bytes"] += size
                    slots[slot]["http_bytes"] += size
                    slots[slot]["state"] = "receiving"
                if not first_http_byte:
                    first_http_byte = True
                    trace("http_first_byte", segment=index + 1, worker=slot + 1, attempt=http_attempt,
                          seconds=round(time.monotonic() - http_started, 4))
                if not adaptive_hosts:
                    check_peer_ready()

            def verify(data, source):
                verify_started, input_size = time.monotonic(), len(data)
                try:
                    if key_url:
                        try:
                            data = decrypt_aes128(data, keys[key_url], iv)
                        except (ValueError, OSError):
                            raise DownloadError("영상 조각의 복호화에 실패했습니다. 다시 시도해 주세요.") from None
                    validate_ts(data)
                except DownloadError:
                    trace("segment_verify", segment=index + 1, source=source, valid=False, encrypted=bool(key_url), input_bytes=input_size)
                    raise
                trace("segment_verify", segment=index + 1, source=source, valid=True, encrypted=bool(key_url),
                      input_bytes=input_size, output_bytes=len(data), seconds=round(time.monotonic() - verify_started, 4))
                return data

            def fetch_http():
                nonlocal http_verified
                if not adaptive_hosts:
                    return fetch(url, cancel, PLAYER_ORIGIN + "/", on_bytes=receive_bytes,
                                 **({"on_event": http_event} if log_handle is not None else {}),
                                 **({"on_wait": check_peer_ready} if p2p is not None else {}))[0]

                class HostSlow(Exception):
                    pass

                class RequestStopped(Exception):
                    pass

                original = urlsplit(url).hostname
                requests = {}
                attempted = set()
                original_retried = hedged = False
                last_error = None

                def request_body(request):
                    host = request["host"]
                    request["started"] = request["last_byte"] = time.monotonic()
                    def watch():
                        check_cancel(cancel)
                        if request["stop"].is_set():
                            raise RequestStopped()
                        if request["original_retry"]:
                            return
                        now = time.monotonic()
                        if now - request["last_byte"] >= SERVER_STALL_SECONDS:
                            raise HostSlow("수신 정체")
                        elapsed = now - request["started"]
                        with transfer_lock:
                            other_speed = max((item["speed"] for name, item in server_health.items()
                                               if name != host and item["samples"]), default=0)
                        if elapsed >= SERVER_SLOW_SECONDS and request["received"] / elapsed < other_speed / 4:
                            raise HostSlow("저속 수신")
                    def receive(size):
                        request["received"] += size
                        request["last_byte"] = time.monotonic()
                        receive_bytes(size)
                        watch()
                    try:
                        watch()
                        data, _ = fetch(urlsplit(url)._replace(netloc=host).geturl(), cancel, PLAYER_ORIGIN + "/",
                                        on_bytes=receive, on_wait=watch, retry=request["original_retry"],
                                        **({"on_event": http_event} if log_handle is not None else {}))
                        data = verify(data, "http")
                        check_cancel(cancel)
                        if request["stop"].is_set():
                            raise RequestStopped()
                        rate = request["received"] / max(time.monotonic() - request["started"], .001)
                        with transfer_lock:
                            health = server_health[host]
                            health.update(speed=rate if not health["samples"] else health["speed"] * .5 + rate * .5,
                                          samples=health["samples"] + 1, slow_until=0, reason="")
                        return data
                    except HostSlow as error:
                        slow_server(host, str(error))
                        raise
                    except BrowserVerificationRequired:
                        raise
                    except DownloadError:
                        if not cancel.is_set():
                            slow_server(host, "요청·검증 실패")
                        raise
                    finally:
                        with transfer_lock:
                            server_health[host]["active"] -= 1

                def start_request(*, rescue=False, original_only=False):
                    excluded = set(adaptive_hosts) - {original} if original_only else attempted
                    host = reserve_server(original, excluded, spare=rescue)
                    if host is None:
                        return False
                    request = {"host": host, "started": time.monotonic(), "last_byte": time.monotonic(),
                               "received": 0, "stop": threading.Event(), "original_retry": original_only}
                    attempted.add(host)
                    with transfer_lock:
                        previous = slots[slot]["host"]
                        if rescue:
                            slots[slot]["rescue_host"] = host
                        else:
                            slots[slot]["host"] = host
                        if host != original:
                            server_health[host]["switches"] += 1
                            slots[slot]["server_switched"] = True
                    trace("server_switch" if host != previous else "server_request", segment=index + 1,
                          worker=slot + 1, host=host, previous_host=previous, rescue=rescue,
                          fastest_only=fastest_only is not None and fastest_only.is_set())
                    requests[http_pool.submit(request_body, request)] = request
                    return True

                start_request()
                try:
                    while requests:
                        check_cancel(cancel)
                        completed, _ = concurrent.futures.wait(requests, timeout=.1,
                                                              return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in completed:
                            request = requests[future]
                            try:
                                data = future.result()
                            except (Cancelled, BrowserVerificationRequired):
                                raise
                            except (HostSlow, DownloadError) as error:
                                last_error = error
                                del requests[future]
                            else:
                                with transfer_lock:
                                    slots[slot].update(host=request["host"], rescue_host="")
                                http_verified = True
                                return data
                        check_peer_ready()
                        if not requests:
                            if start_request():
                                continue
                            if not original_retried:
                                original_retried = True
                                if start_request(original_only=True):
                                    continue
                            raise DownloadError(f"이 영상의 서버에서 {index + 1}번 조각을 받지 못했습니다.") from last_error
                        if not hedged and not (fastest_only is not None and fastest_only.is_set()) and not queued_indices and any(
                                time.monotonic() - item["last_byte"] >= SERVER_HEDGE_SECONDS for item in requests.values()):
                            primary = next(iter(requests.values()))
                            if start_request(rescue=True):
                                hedged = True
                                slow_server(primary["host"], "후반 수신 지연")
                finally:
                    for future, request in requests.items():
                        request["stop"].set()
                        if future.cancel():
                            with transfer_lock:
                                server_health[request["host"]]["active"] -= 1
                    for future in requests:
                        try:
                            future.result()
                        except Exception:
                            pass

            def begin_http(*, replacing_peer=False):
                nonlocal http_started
                if p2p is not None:
                    claim_started = time.monotonic()
                    p2p.claim_http(index)
                    check_cancel(cancel)
                    trace("http_claim", segment=index + 1, worker=slot + 1,
                          seconds=round(time.monotonic() - claim_started, 4))
                http_started = time.monotonic()
                with transfer_lock:
                    slots[slot].update(source="http", state="requesting", bytes=slots[slot]["http_bytes"])
                    if replacing_peer:
                        slots[slot]["transition"] = "p2p_to_http"
                report("segment", {"index": index, "state": "active", "worker": slot})

            try:
                peer_started = time.monotonic()
                data = p2p.get(index) if p2p is not None else None
                trace("p2p_check", segment=index + 1, worker=slot + 1, available=data is not None,
                      enabled=p2p is not None, seconds=round(time.monotonic() - peer_started, 4))
                check_cancel(cancel)
                from_peer = data is not None
                if data is None:
                    begin_http()
                    try:
                        data = fetch_http()
                    except PeerSegmentReady:
                        trace("http_superseded", segment=index + 1, worker=slot + 1,
                              http_bytes=slots[slot]["http_bytes"], seconds=round(time.monotonic() - http_started, 4))
                        data, from_peer = peer_body, True
                    except DownloadError:
                        data = p2p.get(index, wait=False) if p2p is not None else None
                        if data is None:
                            raise
                        from_peer = True
                    check_cancel(cancel)
                if from_peer:
                    with transfer_lock:
                        if slots[slot]["source"] == "http":
                            slots[slot]["transition"] = "http_to_p2p"
                        slots[slot].update(source="p2p", state="checking", bytes=len(data), p2p_bytes=len(data))
                    report("segment", {"index": index, "state": "p2p_active", "worker": slot})
                    try:
                        data = verify(data, "p2p")
                        with transfer_lock:
                            p2p_saved.add(index)
                    except DownloadError:
                        allow_peer_switch = False
                        report("status", f"{index + 1}번 P2P 조각의 검증에 실패해 HTTP로 다시 받습니다.")
                        begin_http(replacing_peer=True)
                        data = fetch_http()
                        check_cancel(cancel)
                        if not http_verified:
                            data = verify(data, "http")
                else:
                    if not http_verified:
                        data = verify(data, "http")
                if p2p is not None:
                    # A verified body is already in memory; stop duplicate peer work
                    # before disk/checkpoint waits. Durable completion still follows store.
                    p2p.finish(index)
                    peer_finished = True
                with transfer_lock:
                    slots[slot]["state"] = "checking"
                return data
            except BaseException as error:
                trace("segment_cancelled" if cancel.is_set() else "segment_error", segment=index + 1,
                      worker=slot + 1, error=type(error).__name__)
                with transfer_lock:
                    slots[slot]["state"] = "error"
                raise
            finally:
                if p2p is not None and not peer_finished:
                    p2p.finish(index)

        def store_segment(index, slot, data):
            try:
                check_cancel(cancel)
                if cache is not None:
                    cache.store(index, data, **({"on_event": lambda event, **fields:
                        trace(event, segment=index + 1, worker=slot + 1, **fields)} if log_handle is not None else {}))
                else:
                    (spool_dir / f"{index:06d}.ts").write_bytes(data)
                return hashlib.sha256(data).hexdigest() if log_handle is not None else None
            except BaseException as error:
                trace("segment_cancelled" if cancel.is_set() else "segment_error", segment=index + 1,
                      worker=slot + 1, error=type(error).__name__)
                raise

        started = time.monotonic()
        saved = sum(verified.values())
        resumed = len(verified)
        done = resumed
        samples = deque([(started, 0, 0)])
        last_transfer_report = started
        last_workers_report = started - 0.25

        def report_transfer(force=False):
            nonlocal last_transfer_report, last_workers_report, log_last_sample
            now = time.monotonic()
            if not force and now - last_transfer_report < 0.25:
                return
            with transfer_lock:
                current = received
                current_http = http_received
                worker_snapshot = []
                if now - last_workers_report >= 0.25:
                    for slot, worker in enumerate(slots):
                        history = slot_samples[slot]
                        history.append((now, worker["http_bytes"]))
                        while len(history) > 2 and history[1][0] < now - 2:
                            history.popleft()
                        speed = ((worker["http_bytes"] - history[0][1]) / max(now - history[0][0], 0.001)
                                 if worker["index"] is not None and worker["source"] == "http" else 0.0)
                        worker_snapshot.append({**worker, "speed": speed})
                server_snapshot = {host: {"active": item["active"], "speed": item["speed"],
                    "samples": item["samples"], "switches": item["switches"], "slow_count": item["slow_count"],
                    "cooldown": max(0, math.ceil(item["slow_until"] - now)), "reason": item["reason"],
                    "state": "slow" if item["slow_until"] > now else "recovering" if item["reason"]
                             else "healthy" if item["samples"] else "waiting"}
                    for host, item in server_health.items()}
            samples.append((now, current, current_http))
            while len(samples) > 2 and samples[1][0] < now - 2:
                samples.popleft()
            elapsed = max(now - samples[0][0], 0.001)
            transfer = {**(p2p.stats() if p2p is not None else {}),
                                "received": current, "speed": (current - samples[0][1]) / elapsed,
                                "server_health": server_snapshot,
                                "http_bytes": current_http, "p2p_bytes": current - current_http,
                                "http_speed": (current_http - samples[0][2]) / elapsed,
                                "p2p_speed": ((current - current_http) - (samples[0][1] - samples[0][2])) / elapsed}
            report("transfer", transfer)
            if force or now - log_last_sample >= 2:
                trace("transfer", **{key: value for key, value in transfer.items() if key != "p2p_peer_details"},
                      saved_bytes=saved, completed=done, queued=len(queued_indices), workers=worker_snapshot,
                      storage_pending=len(storing), storage_buffered_bytes=storage_bytes, storage_buffer_limit=STORAGE_BUFFER_BYTES)
                log_last_sample = now
            if worker_snapshot:
                report("workers", worker_snapshot)
                last_workers_report = now
            last_transfer_report = now

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
        storage_pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
        host_queues = {}
        for index, item in enumerate(segments):
            if index not in verified:
                host_queues.setdefault(urlsplit(item[0]).hostname, deque()).append(index)
        active_hosts = dict.fromkeys(host_queues, 0)
        queued_indices = set(range(total)) - set(verified)

        def fill_slots():
            while (len(pending) < WORKERS and len(storing) < WORKERS * 2
                   and storage_bytes < STORAGE_BUFFER_BYTES):
                check_cancel(cancel)
                peer_state = p2p.stats() if p2p is not None else {}
                ready = [item["index"] for item in peer_state.get("p2p_parts", [])
                         if item["state"] == "ready" and item["index"] in queued_indices]
                if ready:
                    index = min(ready)
                    host = urlsplit(segments[index][0]).hostname
                else:
                    # Keep HTTP busy with other pieces while peers are receiving theirs.
                    arriving = set(peer_state.get("p2p_active_indices", []))
                    candidates = [(host, next((index for index in indices if index not in arriving), None))
                                  for host, indices in host_queues.items() if indices]
                    available = [(host, index) for host, index in candidates if index is not None]
                    if not available:
                        available = [(host, indices[0]) for host, indices in host_queues.items() if indices]
                    host, index = min(available, key=lambda item: (active_hosts[item[0]], item[1]), default=(None, None))
                if index is None:
                    return
                host_queues[host].remove(index)
                queued_indices.remove(index)
                busy = {entry[1] for entry in pending.values()}
                slot = next(slot for slot in range(WORKERS) if slot not in busy)
                with transfer_lock:
                    slots[slot] = {"slot": slot, "index": index, "host": host or "",
                                   "state": "peer_wait" if p2p is not None else "requesting",
                                   "source": "p2p_wait" if p2p is not None else "http",
                                   "bytes": 0, "http_bytes": 0, "p2p_bytes": 0, "transition": "", "speed": 0.0}
                    slot_samples[slot] = deque([(time.monotonic(), 0)])
                report("segment", {"index": index, "state": "p2p_wait" if p2p is not None else "active", "worker": slot})
                trace("segment_assigned", segment=index + 1, worker=slot + 1, host=host,
                      reason="p2p_ready" if ready else "p2p_arriving" if index in peer_state.get("p2p_active_indices", []) else "http_available")
                pending[pool.submit(load_segment, index, slot)] = (index, slot, host)
                active_hosts[host] += 1

        if cache is not None:
            report("progress", {"done": done, "total": total, "bytes": saved, "eta": 0, "resumed": resumed})
        fill_slots()
        report_transfer(force=True)
        report("status", "영상을 다운로드하고 있습니다…")
        storage_blocked = False
        while pending or storing:
            check_cancel(cancel)
            # Completed network bodies keep their slot until storage has room.
            # Only unfinished futures participate in the wait, avoiding a busy loop.
            waiting = [future for future in pending if not future.done()] + list(storing)
            if waiting and (storage_blocked or not any(future.done() for future in pending)):
                concurrent.futures.wait(waiting, timeout=0.2, return_when=concurrent.futures.FIRST_COMPLETED)
            report_transfer()
            for future in list(storing):
                if not future.done():
                    continue
                check_cancel(cancel)
                index, slot, size, transition, segment_metrics, segment_started = storing.pop(future)
                storage_bytes -= size
                try:
                    digest = future.result()
                except BaseException:
                    report("segment", {"index": index, "state": "error", "worker": slot})
                    raise
                saved += size
                done += 1
                segment_metrics["seconds"] = round(time.monotonic() - segment_started, 4)
                if log_handle is not None:
                    trace("segment_saved", segment=index + 1, worker=slot + 1, source="p2p" if index in p2p_saved else "http",
                          transition=transition, bytes=size, sha256=digest, **segment_metrics)
                report("segment", {"index": index, "state": "complete", "worker": slot,
                                   "source": "p2p" if index in p2p_saved else "http", "transition": transition})
                elapsed = max(time.monotonic() - started, 0.001)
                report("progress", {"done": done, "total": total, "bytes": saved,
                                    "eta": elapsed / (done - resumed) * (total - done), "resumed": resumed})
            storage_blocked = False
            for future in list(pending):
                if not future.done():
                    continue
                check_cancel(cancel)
                index, slot, host = pending[future]
                try:
                    data = future.result()
                except BaseException:
                    report("segment", {"index": index, "state": "error", "worker": slot})
                    raise
                if len(data) > STORAGE_BUFFER_BYTES:
                    raise DownloadError("영상 조각의 크기가 저장 대기 허용 크기를 초과했습니다.")
                if len(storing) >= WORKERS * 2 or storage_bytes + len(data) > STORAGE_BUFFER_BYTES:
                    storage_blocked = True
                    data = None
                    continue
                pending.pop(future)
                active_hosts[host] -= 1
                with transfer_lock:
                    transition = slots[slot]["transition"]
                    segment_metrics = {key: slots[slot][key] for key in ("http_bytes", "p2p_bytes")}
                    segment_started = slots[slot]["started"]
                    slots[slot] = {"slot": slot, "index": None, "host": "", "state": "waiting", "source": "",
                                   "bytes": 0, "http_bytes": 0, "p2p_bytes": 0, "transition": "", "speed": 0.0}
                    slot_samples[slot].clear()
                size = len(data)
                storing[storage_pool.submit(store_segment, index, slot, data)] = (
                    index, slot, size, transition, segment_metrics, segment_started)
                storage_bytes += size
                trace("segment_received", segment=index + 1, worker=slot + 1, bytes=size,
                      seconds=round(time.monotonic() - segment_started, 4),
                      storage_pending=len(storing), storage_buffered_bytes=storage_bytes)
                data = None
            fill_slots()
        report_transfer(force=True)
        report("status", "받은 영상 조각을 순서대로 합치고 있습니다…")
        merge_started = time.perf_counter()
        if cache is not None:
            output = partial.open("wb")
        digest = hashlib.sha256()
        with output:
            for index in range(total):
                check_cancel(cancel)
                segment_path = spool_dir / f"{index:06d}.ts"
                with segment_path.open("rb") as segment_file:
                    while block := segment_file.read(1024 * 1024):
                        check_cancel(cancel)
                        output.write(block)
                        digest.update(block)
                if cache is None:
                    segment_path.unlink()
            if output.tell() != saved:
                raise DownloadError("저장한 영상 조각의 크기가 일치하지 않습니다.")
            trace("merge_write", bytes=saved, seconds=round(time.perf_counter() - merge_started, 6))
            if cache is not None:
                sync_started = time.perf_counter()
                output.flush()
                os.fsync(output.fileno())
                trace("merge_sync", seconds=round(time.perf_counter() - sync_started, 6))
        check_cancel(cancel)
        publication_started = time.perf_counter()
        if cache is not None:
            cache.begin_publication(saved, digest.hexdigest())
            cache.target_path()
        partial.rename(target)
        published = True
        if cache is not None:
            cache.finish_publication()
        trace("publication_finish", seconds=round(time.perf_counter() - publication_started, 6))
        report("complete", str(target))
        trace("file_verified", bytes=saved, sha256=digest.hexdigest(), segments=total)
        return target
    except ResumeError as error:
        raise DownloadError(str(error)) from None
    finally:
        log_error = sys.exc_info()[0]
        log_outcome = "cancelled" if log_error is not None and issubclass(log_error, Cancelled) else "error" if log_error else "completed"
        if pool is not None:
            if any(not future.done() for future in (*pending, *storing)):
                cancel.set()
            for future in (*pending, *storing):
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            if storage_pool is not None:
                storage_pool.shutdown(wait=True, cancel_futures=True)
            if cache is not None and cache_verified and not published:
                records = cache.state["parts"]
                completed_indices = sorted(int(index) for index in records)
                report("parts", {"total": total, "completed": completed_indices,
                                 "p2p_completed": sorted(p2p_saved.intersection(completed_indices))})
                report("progress", {"done": len(completed_indices), "total": total,
                                    "bytes": sum(record["size"] for record in records.values()),
                                    "eta": 0, "resumed": resumed})
                for index, slot, host in pending.values():
                    report("segment", {"index": index,
                                       "state": "complete" if str(index) in records else "waiting", "worker": None})
                for index, slot, size, transition, segment_metrics, segment_started in storing.values():
                    report("segment", {"index": index,
                                       "state": "complete" if str(index) in records else "waiting", "worker": None})
        if http_pool is not None:
            http_pool.shutdown(wait=True, cancel_futures=True)
        if output is not None:
            output.close()
        if cache is None and output is not None and partial is not None and partial.exists():
            partial.unlink()
        try:
            stack.close()
        finally:
            closing_error = sys.exc_info()[0]
            trace("run_end", outcome="error" if closing_error and log_outcome == "completed" else log_outcome,
                  error=(log_error or closing_error).__name__ if log_error or closing_error else None, published=bool(published))
            if log_handle is not None:
                with log_lock:
                    handle, log_handle = log_handle, None
                    try:
                        handle.close()
                    except OSError:
                        report("status", "상세 로그의 마지막 기록을 저장하지 못했습니다. 로그 폴더를 확인하세요.")


def download_text(value: str, folder: Path, cancel: threading.Event, report, *, title: str, browser_fetch) -> Path:
    """Publish one complete UTF-8 episode; keep paragraph breaks and existing files."""
    value, _, _ = parse_text_page_url(value)
    check_cancel(cancel)
    report("info", {"title": title, "duration": 0, "segments": 1})
    report("parts", {"total": 1, "completed": []})
    report("progress", {"done": 0, "total": 1, "bytes": 0, "eta": 0})
    report("status", "소설 본문을 불러오고 있습니다…")
    try:
        body, final_url = browser_fetch(value, cancel, limit=8 * 1024 * 1024)
    except TransferCancelled:
        raise Cancelled() from None
    except BrowserChallengeError as error:
        raise BrowserVerificationRequired(error.url) from None
    except (TransferError, TimeoutError) as error:
        check_cancel(cancel)
        raise DownloadError(f"소설 본문을 불러오지 못했습니다. {error}") from None
    check_cancel(cancel)
    if parse_text_page_url(final_url)[0] != value:
        raise DownloadError("요청한 소설 회차와 본문 주소가 다릅니다.")
    try:
        text = body.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
    except UnicodeError:
        raise DownloadError("소설 본문의 글자를 읽지 못했습니다.") from None
    if not text or len(body) > 8 * 1024 * 1024:
        raise DownloadError("저장할 소설 본문이 없거나 허용 크기를 초과했습니다.")
    payload = (text + "\n").encode("utf-8")
    folder = folder.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(title)
    number = 0
    while True:
        target = folder / (stem + (f" ({number})" if number else "") + ".txt")
        partial = target.with_suffix(".txt.part")
        if target.exists():
            number += 1
            continue
        try:
            stream = partial.open("xb")
            break
        except FileExistsError:
            number += 1
    try:
        with stream:
            check_cancel(cancel)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancel)
        partial.rename(target)
    finally:
        if partial.exists():
            partial.unlink()
    report("file", str(target))
    report("progress", {"done": 1, "total": 1, "bytes": len(payload), "eta": 0})
    report("complete", str(target))
    return target


def text_archive_title(items):
    numbers = [item.get("number") for item in items]
    if not numbers or any(type(number) is not int or number < 0 for number in numbers):
        raise DownloadError("ZIP 파일 이름에 사용할 회차 번호를 확인하지 못했습니다. 작품 목록을 다시 불러와 주세요.")
    first, last = min(numbers), max(numbers)
    episode_range = str(first) if first == last else f"{first}~{last}"
    suffix = f" · {episode_range}화" + (" · 완결" if all(item.get("series_status") == "완결" for item in items) else "")
    title = safe_filename(items[0].get("series_title") or "소설")
    return title[:120 - len(suffix)].rstrip(". ") + suffix


def save_text_zip(files: list[Path], folder: Path, cancel: threading.Event, report, *, title: str, temporary_folder: Path | None = None) -> Path:
    """Publish an archive only after every expected TXT has been included and verified."""
    check_cancel(cancel)
    folder = folder.expanduser().resolve()
    files = [path.resolve() for path in files]
    allowed = {folder}
    if temporary_folder is not None:
        temporary_folder = temporary_folder.resolve()
        if temporary_folder.parent != folder / ".tvmon-zip" or not re.fullmatch(r"[0-9a-f]{32}", temporary_folder.name):
            raise DownloadError("ZIP 임시 폴더를 확인하지 못했습니다.")
        allowed.add(temporary_folder)
    if not files or len(set(files)) != len(files):
        raise DownloadError("ZIP에 넣을 전체 회차 파일을 확인하지 못했습니다.")
    for path in files:
        if path.parent not in allowed or path.suffix.lower() != ".txt" or not path.is_file() or not path.stat().st_size:
            raise DownloadError("ZIP에 필요한 TXT 파일이 없습니다. 전체 ZIP 작업을 다시 시작해 주세요.")
    report("info", {"title": title, "duration": 0, "segments": len(files)})
    report("parts", {"total": len(files), "completed": []})
    report("status", "전체 회차를 ZIP으로 압축하고 있습니다…")
    stem, number = safe_filename(title), 0
    while True:
        target = folder / (stem + (f" ({number})" if number else "") + ".zip")
        partial = target.with_suffix(".zip.part")
        if target.exists():
            number += 1
            continue
        try:
            stream = partial.open("x+b")
            break
        except FileExistsError:
            number += 1
    try:
        with stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                names = set()
                for index, path in enumerate(files):
                    check_cancel(cancel)
                    name, duplicate = path.name, 0
                    while name.casefold() in names:
                        duplicate += 1
                        name = f"{path.stem} ({duplicate}).txt"
                    names.add(name.casefold())
                    archive.write(path, arcname=name)
                    report("segment", {"index": index, "state": "complete"})
                    report("progress", {"done": index + 1, "total": len(files), "bytes": stream.tell(), "eta": 0})
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancel)
        with zipfile.ZipFile(partial) as archive:
            if len(archive.infolist()) != len(files):
                raise DownloadError("ZIP의 회차 개수가 일치하지 않습니다.")
            for entry in archive.infolist():
                check_cancel(cancel)
                with archive.open(entry) as contents:
                    while contents.read(1024 * 1024):
                        check_cancel(cancel)
        check_cancel(cancel)
        partial.rename(target)
    except zipfile.BadZipFile:
        raise DownloadError("ZIP 검증에 실패했습니다. 전체 ZIP 작업을 다시 시작해 주세요.") from None
    finally:
        if partial.exists():
            partial.unlink()
    report("file", str(target))
    report("progress", {"done": len(files), "total": len(files), "bytes": target.stat().st_size, "eta": 0})
    report("complete", str(target))
    return target


def cleanup_text_zip(archive_path: Path, sources, cancel: threading.Event):
    """Verify the entire ZIP, then delete only explicitly selected, matching source files."""
    deleted, errors = [], []
    def digest(stream):
        result = hashlib.sha256()
        while True:
            check_cancel(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                return result.digest()
            result.update(block)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) != len(sources):
                raise DownloadError("ZIP의 회차 개수가 달라 TXT를 정리하지 않았습니다.")
            hashes = []
            for entry in entries:
                with archive.open(entry) as stream:
                    hashes.append(digest(stream))
        for index, (job_id, path, remove) in enumerate(sources):
            check_cancel(cancel)
            if not remove or path is None:
                continue
            try:
                if path.is_symlink():
                    raise OSError("링크 파일은 정리하지 않습니다")
                if path.exists():
                    before = path.stat()
                    with path.open("rb") as stream:
                        matches = digest(stream) == hashes[index]
                    after = path.stat()
                    if not matches or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        errors.append(f"ZIP과 내용이 달라 보관: {path.name}")
                        continue
                    check_cancel(cancel)
                    path.unlink()
                deleted.append(job_id)
            except OSError:
                errors.append(f"TXT 정리 실패: {path.name}")
    except Cancelled:
        errors.append("TXT 정리를 중단했습니다. ZIP 작업에서 다시 시작할 수 있습니다.")
    except (OSError, zipfile.BadZipFile, DownloadError):
        errors.append("ZIP 검증에 실패해 TXT 정리를 중단했습니다.")
    return {"deleted": deleted, "errors": errors}


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _set_scrollbar(scrollbar, first, last):
    """Keep a gridded scrollbar visible only while its content overflows."""
    scrollbar.set(first, last)
    needed = float(first) > 0 or float(last) < 1
    if needed and not scrollbar.winfo_manager():
        scrollbar.grid()
    elif not needed and scrollbar.winfo_manager():
        scrollbar.grid_remove()


class WindowsSearchEntry(ttk.Frame):
    """Keep native IME composition, with a thin caret after the composing text."""

    def __init__(self, master, *, textvariable, command=None, right_click=None):
        import ctypes
        from ctypes import wintypes as w

        super().__init__(master, style="Card.TFrame", height=34, takefocus=True)
        self.variable = textvariable
        self.command = command
        self.right_click = right_click
        self._closed = False
        self._sized = False
        self._composing = False
        self._native_changed = False
        self._native_actions = deque()
        self._ctypes = ctypes
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self._common = ctypes.WinDLL("comctl32", use_last_error=True)

        class CaretInfo(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("flags", w.DWORD), ("active", w.HWND),
                        ("focus", w.HWND), ("capture", w.HWND), ("menu", w.HWND),
                        ("move_size", w.HWND), ("caret", w.HWND), ("rect", w.RECT)]

        self._caret_info_type = CaretInfo
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM,
                                          w.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        signatures = (
            (self._user.CreateWindowExW, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
            (self._user.SendMessageW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t),
            (self._user.GetWindowTextLengthW, [w.HWND], ctypes.c_int),
            (self._user.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self._user.SetWindowTextW, [w.HWND, w.LPCWSTR], w.BOOL),
            (self._user.SetFocus, [w.HWND], w.HWND),
            (self._user.GetFocus, [], w.HWND),
            (self._user.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self._user.GetGUIThreadInfo, [w.DWORD, ctypes.POINTER(CaretInfo)], w.BOOL),
            (self._user.CreateCaret, [w.HWND, w.HBITMAP, ctypes.c_int, ctypes.c_int], w.BOOL),
            (self._user.ShowCaret, [w.HWND], w.BOOL),
            (self._user.SetCaretPos, [ctypes.c_int, ctypes.c_int], w.BOOL),
            (self._user.GetDC, [w.HWND], w.HDC),
            (self._user.ReleaseDC, [w.HWND, w.HDC], ctypes.c_int),
            (self._user.GetKeyState, [ctypes.c_int], ctypes.c_short),
            (self._user.MoveWindow, [w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.BOOL], w.BOOL),
            (self._user.DestroyWindow, [w.HWND], w.BOOL),
            (self._gdi.CreateFontW, [ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR], w.HANDLE),
            (self._gdi.DeleteObject, [w.HANDLE], w.BOOL),
            (self._gdi.SelectObject, [w.HDC, w.HANDLE], w.HANDLE),
            (self._gdi.GetTextExtentPoint32W, [w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.SIZE)], w.BOOL),
            (self._common.SetWindowSubclass, [w.HWND, callback_type, ctypes.c_size_t, ctypes.c_size_t], w.BOOL),
            (self._common.RemoveWindowSubclass, [w.HWND, callback_type, ctypes.c_size_t], w.BOOL),
            (self._common.DefSubclassProc, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t),
        )
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result
        self._parent_hwnd = self.winfo_id()
        # WS_EX_CLIENTEDGE; WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_AUTOHSCROLL.
        self._hwnd = self._user.CreateWindowExW(0x200, "EDIT", self.variable.get(), 0x50010080,
                                               0, 0, 1, 1, self._parent_hwnd, None, None, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._thread_id = self._user.GetWindowThreadProcessId(self._hwnd, None)
        self._font = self._gdi.CreateFontW(-round(self.winfo_fpixels("10p")), 0, 0, 0, 400,
                                          0, 0, 0, 1, 0, 0, 5, 0, "맑은 고딕")
        self._user.SendMessageW(self._hwnd, 0x30, self._font, 1)  # WM_SETFONT
        self._user.SendMessageW(self._hwnd, 0xD3, 3, 6 | (6 << 16))  # EM_SETMARGINS
        self._callback = callback_type(self._window_message)
        for hwnd in (self._parent_hwnd, self._hwnd):
            if not self._common.SetWindowSubclass(hwnd, self._callback, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        self._trace = self.variable.trace_add("write", self._from_variable)
        self.bind("<Configure>", self._resize_native)
        self.bind("<FocusIn>", lambda _: self._user.SetFocus(self._hwnd))
        self.bind("<Destroy>", self._dispose)
        self._poll_after = self._root().after(25, self._poll_native)

    def _resize_native(self, event):
        self._user.MoveWindow(self._hwnd, 0, 0, event.width, event.height, True)
        if not self._sized and event.width > 1 and event.height > 1:
            self._sized = True
            # Text assigned while EDIT is 1px wide needs its first real layout.
            start, end = self._ctypes.c_uint(), self._ctypes.c_uint()
            self._user.SendMessageW(self._hwnd, 0xB0, self._ctypes.addressof(start), self._ctypes.addressof(end))
            self._user.SetWindowTextW(self._hwnd, self.get())
            self._user.SendMessageW(self._hwnd, 0xB1, start.value, end.value)

    def _window_message(self, hwnd, message, wparam, lparam, subclass_id, _):
        try:
            if hwnd == self._parent_hwnd and message == 0x111 and lparam == self._hwnd and wparam >> 16 == 0x300:
                # Never enter Tcl from a Win32 callback: its thread state belongs to Tk's event loop.
                self._native_changed = True
            elif hwnd == self._hwnd:
                if message == 0x10D:  # WM_IME_STARTCOMPOSITION
                    self._composing = True
                elif message == 0x10E:  # WM_IME_ENDCOMPOSITION
                    self._composing = False
                elif message == 0x100:  # WM_KEYDOWN
                    if wparam == 13 and not self._composing:
                        if self.command is not None:
                            self._native_actions.append("submit")
                        return 0
                    if wparam == 9:
                        self._native_actions.append("previous" if self._user.GetKeyState(16) < 0 else "next")
                        return 0
                    if wparam == 65 and self._user.GetKeyState(17) < 0:
                        self._user.SendMessageW(hwnd, 0xB1, 0, -1)  # EM_SETSEL
                        return 0
                elif message == 0x102 and wparam in (1, 9, 13):  # Keep single-line input free of control characters.
                    return 0
                elif message == 0x7B and self.right_click is not None:  # WM_CONTEXTMENU
                    self._native_actions.append("context")
                    return 0
            if message == 0x82:  # WM_NCDESTROY
                self._common.RemoveWindowSubclass(hwnd, self._callback, subclass_id)
        except Exception:
            self._native_actions.append(sys.exc_info())
        result = self._common.DefSubclassProc(hwnd, message, wparam, lparam)
        if hwnd == self._hwnd and self._composing and message == 0x10F:
            try:
                self._update_ime_caret()
            except Exception:
                self._native_actions.append(sys.exc_info())
        return result

    def _update_ime_caret(self):
        """Change only the caret pixels; never commit or rewrite the IME buffer."""
        from ctypes import wintypes as w

        if self._user.GetFocus() != self._hwnd:
            return
        info = self._caret_info_type(size=self._ctypes.sizeof(self._caret_info_type))
        if not self._user.GetGUIThreadInfo(self._thread_id, self._ctypes.byref(info)) or info.caret != self._hwnd:
            return
        height = info.rect.bottom - info.rect.top
        if height <= 0:
            return
        start, end = w.DWORD(), w.DWORD()
        self._user.SendMessageW(self._hwnd, 0xB0, self._ctypes.addressof(start), self._ctypes.addressof(end))
        if start.value != end.value:
            return
        index = end.value
        position = self._user.SendMessageW(self._hwnd, 0xD6, index, 0)  # EM_POSFROMCHAR
        extra = 0
        if position == -1 and index:
            # EDIT does not return a position for the end-of-buffer index.
            prefix = self.get().encode("utf-16-le", "surrogatepass")[:index * 2].decode("utf-16-le", "surrogatepass")
            if not prefix:
                return
            char = prefix[-1]
            units = len(char.encode("utf-16-le", "surrogatepass")) // 2
            position = self._user.SendMessageW(self._hwnd, 0xD6, index - units, 0)
            dc = self._user.GetDC(self._hwnd)
            previous_font = self._gdi.SelectObject(dc, self._font)
            try:
                size = w.SIZE()
                if not self._gdi.GetTextExtentPoint32W(dc, char, units, self._ctypes.byref(size)):
                    return
                extra = size.cx
            finally:
                self._gdi.SelectObject(dc, previous_font)
                self._user.ReleaseDC(self._hwnd, dc)
        x = info.rect.left if position == -1 else self._ctypes.c_short(position & 0xFFFF).value + extra
        area = w.RECT()
        self._user.SendMessageW(self._hwnd, 0xB2, 0, self._ctypes.addressof(area))  # EM_GETRECT
        x = max(area.left, min(x, area.right - 2))
        if info.rect.right - info.rect.left > 2:
            if not self._user.CreateCaret(self._hwnd, None, 2, height):
                return
            self._user.SetCaretPos(x, info.rect.top)
            self._user.ShowCaret(self._hwnd)
        elif x != info.rect.left:
            self._user.SetCaretPos(x, info.rect.top)

    def get(self):
        buffer = self._ctypes.create_unicode_buffer(self._user.GetWindowTextLengthW(self._hwnd) + 1)
        self._user.GetWindowTextW(self._hwnd, buffer, len(buffer))
        return buffer.value

    def _from_variable(self, *_):
        value = self.variable.get()
        if not self._closed and value != self.get():
            self._user.SetWindowTextW(self._hwnd, value)
            end = self._user.GetWindowTextLengthW(self._hwnd)
            self._user.SendMessageW(self._hwnd, 0xB1, end, end)

    def _sync_variable(self):
        if not self._closed:
            value = self.get()
            if value != self.variable.get():
                self.variable.set(value)

    def _poll_native(self):
        self._poll_after = None
        if self._closed:
            return
        if self._native_changed:
            self._native_changed = False
            self._sync_variable()
        while self._native_actions and not self._closed:
            action = self._native_actions.popleft()
            if action == "submit":
                self.command()
            elif action == "context":
                self.right_click()
            elif action in ("previous", "next"):
                target = self.tk_focusPrev() if action == "previous" else self.tk_focusNext()
                target.focus_set()
            else:
                self._root().report_callback_exception(*action)
        if not self._closed:
            self._poll_after = self._root().after(25, self._poll_native)

    def focus_set(self):
        super().focus_set()
        self._user.SetFocus(self._hwnd)

    def icursor(self, index):
        position = self._user.GetWindowTextLengthW(self._hwnd) if index == tk.END else int(index)
        self._user.SendMessageW(self._hwnd, 0xB1, position, position)

    def state(self, statespec=None):
        result = super().state(statespec)
        if statespec is not None:
            self._user.SendMessageW(self._hwnd, 0xCF, int(self.instate(["readonly"])), 0)  # EM_SETREADONLY
        return result

    def _dispose(self, event):
        if event.widget is self and not self._closed:
            self._closed = True
            if self._poll_after is not None:
                self._root().after_cancel(self._poll_after)
                self._poll_after = None
            self._native_actions.clear()
            self.variable.trace_remove("write", self._trace)
            self._common.RemoveWindowSubclass(self._parent_hwnd, self._callback, 1)
            self._user.DestroyWindow(self._hwnd)
            self._gdi.DeleteObject(self._font)


class SearchDialog:
    """Browse the catalogue inside the manager's persistent search tab."""

    def __init__(self, app):
        self.app = app
        self.window = ttk.Frame(app.search_tab)
        self.window.pack(fill="both", expand=True)
        self.closed = False
        self.events = queue.Queue()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="catalog")
        self.cancel = {side: threading.Event() for side in ("contents", "episodes")}
        self.generation = {side: 0 for side in self.cancel}
        self.items = {side: [] for side in self.cancel}
        self.images = {}
        self.requested_images = set()
        self.image_attempts = {}
        self.image_retry_at = {}
        self.search_term = ""
        self.loading = False
        self.active_content = None
        self.source = "A"
        self.source_states = {}
        self.text_verification = ""
        self.pending_text_query = None
        self.query = tk.StringVar(master=self.window)
        self.recent_query = tk.StringVar(master=self.window)
        self.category = tk.StringVar(master=self.window, value="전체")
        self.result_status = tk.StringVar(master=self.window, value="검색어를 입력하세요. 예: 삼국지")
        self.episode_title = tk.StringVar(master=self.window, value="콘텐츠를 선택하세요")
        self.episode_status = tk.StringVar(master=self.window, value="검색 결과를 클릭하면 전체 회차를 불러옵니다.")
        self.selection_status = tk.StringVar(master=self.window, value="0개 선택")
        self.message = tk.StringVar(master=self.window, value="Q 추가는 대기열에 등록합니다. 실행 중인 대기열은 순서대로 이어집니다.")
        self._build()
        self.window.bind("<Map>", lambda _: (self._load_visible_images("contents"), self._load_visible_images("episodes")))
        self._poll_id = self.window.after(80, self._poll)

    def _build(self):
        main = ttk.Frame(self.window, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        heading = ttk.Frame(main)
        heading.grid(row=0, sticky="ew", pady=(0, 12))
        ttk.Label(heading, text="콘텐츠 검색", font=("맑은 고딕", 16, "bold")).pack(side="left")
        self.retry_images_button = ttk.Button(heading, text="콘텐츠 썸네일 다시 받기", command=self.retry_images)
        self.retry_images_button.pack(side="right")
        form = ttk.Frame(main, style="Card.TFrame", padding=10)
        form.grid(row=1, sticky="ew", pady=(0, 12))
        form.columnconfigure(1, weight=1)
        sources = ttk.Frame(form, style="Card.TFrame")
        sources.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.source_buttons = {}
        for source, label in (("A", "TVWIKI"), ("B", "뉴토끼")):
            button = ttk.Button(sources, text=label, style="Primary.TButton" if source == "A" else "TButton",
                                command=lambda source=source: self.switch_source(source))
            button.pack(side="left", padx=(0, 6))
            self.source_buttons[source] = button
        self.text_verify_button = ttk.Button(sources, text="브라우저 검증 열기", command=self.verify_text_search)
        ttk.Label(form, text="검색어", style="Card.TLabel").grid(row=1, column=0, padx=(0, 12))
        self.query_entry = WindowsSearchEntry(form, textvariable=self.query, command=self.search)
        self.query_entry.grid(row=1, column=1, sticky="ew")
        self.find_button = ttk.Button(form, text="검색", style="Primary.TButton", command=self.search)
        self.find_button.grid(row=1, column=2, padx=(10, 0))
        ttk.Label(form, text="최근 검색", style="Card.TLabel").grid(row=2, column=0, padx=(0, 12), pady=(8, 0))
        recent = ttk.Frame(form, style="Card.TFrame")
        recent.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        recent.columnconfigure(0, weight=1)
        self.recent_selector = ttk.Combobox(recent, textvariable=self.recent_query, height=10)
        self.recent_selector.grid(row=0, column=0, sticky="ew")
        self.recent_selector.bind("<<ComboboxSelected>>", self.use_recent_search)
        self.delete_recent_button = ttk.Button(recent, text="선택 삭제", command=self.delete_recent_search)
        self.delete_recent_button.grid(row=0, column=1, padx=(10, 0))
        self.clear_recent_button = ttk.Button(recent, text="전체 삭제", command=lambda: self.delete_recent_search(all_items=True))
        self.clear_recent_button.grid(row=0, column=2, padx=(6, 0))
        self._refresh_recent_searches()
        panes = ttk.Panedwindow(main, orient="horizontal")
        panes.grid(row=2, sticky="nsew")
        left = ttk.Frame(panes, style="Card.TFrame", padding=10)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        ttk.Label(left, textvariable=self.result_status, style="Card.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        category_bar = ttk.Frame(left, style="Card.TFrame")
        category_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(category_bar, text="카테고리", style="Card.TLabel").pack(side="left", padx=(0, 10))
        self.category_selector = ttk.Combobox(category_bar, textvariable=self.category, values=("전체",), state="disabled", width=18)
        self.category_selector.pack(side="left", fill="x", expand=True)
        self.category_selector.bind("<<ComboboxSelected>>", self.filter_contents)
        style = ttk.Style(self.window)
        style.configure("Catalog.Treeview", rowheight=120)
        style.configure("Episode.Treeview", rowheight=82)
        style.configure("TextEpisode.Treeview", rowheight=34)
        for item_style in ("Catalog.Treeview.Item", "Episode.Treeview.Item"):
            style.layout(item_style, [("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        self.content_tree = ttk.Treeview(left, style="Catalog.Treeview", columns=("title",), show="tree headings", selectmode="browse")
        self.content_tree.heading("#0", text="썸네일")
        self.content_tree.column("#0", width=110, minwidth=110, stretch=False)
        self.content_tree.heading("title", text="콘텐츠 / 분류")
        self.content_tree.column("title", width=250, minwidth=180)
        self.content_tree.grid(row=2, column=0, sticky="nsew")
        content_scroll = ttk.Scrollbar(left, command=self.content_tree.yview)
        content_scroll.grid(row=2, column=1, sticky="ns")
        self.content_tree.configure(yscrollcommand=lambda first, last: self._scrolled("contents", content_scroll, first, last))
        self.content_tree.bind("<<TreeviewSelect>>", self.select_content)
        self.content_tree.bind("<Configure>", lambda _: self._load_visible_images("contents"))
        panes.add(left, weight=2)
        right = ttk.Frame(panes, style="Card.TFrame", padding=10)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        ttk.Label(right, textvariable=self.episode_title, style="Card.TLabel", font=("맑은 고딕", 12, "bold"), wraplength=520).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(right, textvariable=self.episode_status, style="Muted.TLabel", wraplength=520).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 8))
        selection = ttk.Frame(right, style="Card.TFrame")
        selection.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Button(selection, text="전체 선택", command=self.select_all).pack(side="left")
        ttk.Button(selection, text="선택 해제", command=self.clear_selection).pack(side="left", padx=6)
        ttk.Label(selection, textvariable=self.selection_status, style="Card.TLabel").pack(side="right")
        self.episode_tree = ttk.Treeview(right, style="Episode.Treeview", columns=("title",), show="tree headings", selectmode="extended")
        self.episode_tree.heading("#0", text="썸네일")
        self.episode_tree.column("#0", width=152, minwidth=152, stretch=False)
        self.episode_tree.heading("title", text="회차 / 영상 제목")
        self.episode_tree.column("title", width=360, minwidth=260)
        self.episode_tree.grid(row=4, column=0, sticky="nsew")
        episode_scroll = ttk.Scrollbar(right, command=self.episode_tree.yview)
        episode_scroll.grid(row=4, column=1, sticky="ns")
        self.episode_tree.configure(yscrollcommand=lambda first, last: self._scrolled("episodes", episode_scroll, first, last))
        self.episode_tree.bind("<<TreeviewSelect>>", lambda _: self._update_actions())
        self.episode_tree.bind("<Control-a>", self.select_all)
        self.episode_tree.bind("<Configure>", lambda _: self._load_visible_images("episodes"))
        selection_help = ttk.Label(right, text="Ctrl + 클릭: 여러 회차 선택  ·  Shift + 클릭: 구간 선택", style="Muted.TLabel")
        selection_help.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 8))
        actions = ttk.Frame(right, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.queue_selected_button = ttk.Button(actions, text="선택 Q 추가", command=self.queue_episodes)
        self.queue_selected_button.pack(side="left")
        self.queue_all_button = ttk.Button(actions, text="전체 Q 추가", command=lambda: self.queue_episodes(all_items=True))
        self.queue_all_button.pack(side="left", padx=6)
        self.download_button = ttk.Button(actions, text="선택 다운로드", style="Primary.TButton", command=lambda: self.queue_episodes(start=True))
        self.download_button.pack(side="right")
        self.zip_button = ttk.Button(actions, text="전체 ZIP으로 저장", command=self.queue_text_zip)
        self.zip_cleanup_originals = tk.BooleanVar(master=self.window, value=False)
        self.zip_cleanup_check = ttk.Checkbutton(right, text="ZIP 완료 후 원본 TXT 정리", variable=self.zip_cleanup_originals)
        self.video_controls = (selection, self.episode_tree, episode_scroll, selection_help, actions)
        self.text_detail = ttk.Frame(right, style="Card.TFrame")
        self.text_detail.columnconfigure(1, weight=1)
        self.text_detail_values = {}
        for index, (field, label) in enumerate((("category", "분류"), ("status", "상태"),
                                             ("episodes", "표시 회차"), ("genre", "장르"))):
            row, column = divmod(index, 2)
            ttk.Label(self.text_detail, text=label, style="Muted.TLabel").grid(row=row, column=column * 2, sticky="nw", padx=(0, 8), pady=3)
            value = tk.StringVar(master=self.window, value="—")
            self.text_detail_values[field] = value
            detail_label = ttk.Label(self.text_detail, textvariable=value, style="Card.TLabel", wraplength=360)
            detail_label.grid(row=row, column=column * 2 + 1, sticky="nw", padx=(0, 12), pady=3)
            detail_label.configure(wraplength=180)
        self.text_open_button = ttk.Button(self.text_detail, text="사이트에서 열기", command=self.open_text_content, state="disabled")
        self.text_open_button.grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 10))
        panes.add(right, weight=3)
        ttk.Label(main, textvariable=self.message, wraplength=1080, font=("맑은 고딕", 9)).grid(row=3, sticky="ew", pady=(12, 0))
        self._update_actions()

    def switch_source(self, source):
        if self.closed or source == self.source or source not in ("A", "B"):
            return
        self.source_states[self.source] = {
            "items": self.items.copy(), "images": self.images.copy(), "search_term": self.search_term,
            "active": self.active_content, "category": self.category.get(),
            "selected": {side: tree.selection() for side, tree in (("contents", self.content_tree), ("episodes", self.episode_tree))},
            "view": {side: tree.yview()[0] for side, tree in (("contents", self.content_tree), ("episodes", self.episode_tree))},
            "result": self.result_status.get() if not self.loading else "검색이 중단되었습니다. 다시 검색해 주세요.",
            "title": self.episode_title.get(), "status": self.episode_status.get(),
            "message": self.message.get() if not self.loading else "검색 대상이 바뀌어 진행 중이던 검색을 중단했습니다. 다시 검색해 주세요.",
            "detail": {field: value.get() for field, value in self.text_detail_values.items()},
            "verification": self.text_verification,
        }
        for side in self.cancel:
            self._reset(side)
        self.source = source
        self.loading = False
        self.pending_text_query = None
        self.text_verification = ""
        self.search_term = ""
        self._clear_content_detail()
        for name, button in self.source_buttons.items():
            button.configure(style="Primary.TButton" if name == source else "TButton")
        self._update_source_controls()
        state = self.source_states.get(source)
        if state:
            self.images = state["images"].copy()
            self.requested_images = set(self.images)
            self.search_term = state["search_term"]
            for side in self.cancel:
                self._show_rows(side, state["items"][side])
            self.category.set(state["category"])
            self.filter_contents()
            self.active_content = state["active"]
            self.result_status.set(state["result"])
            self.episode_title.set(state["title"])
            self.episode_status.set(state["status"])
            self.message.set(state["message"])
            self.text_verification = state["verification"]
            for field, value in state["detail"].items():
                self.text_detail_values[field].set(value)
            for side, tree in (("contents", self.content_tree), ("episodes", self.episode_tree)):
                tree.selection_set(state["selected"][side])
                tree.yview_moveto(state["view"][side])
            if self.active_content and not self.items["episodes"] and not self.text_verification:
                # An episode request cancelled by the switch needs to run again.
                self.active_content = None
                self.select_content()
        else:
            self.result_status.set("검색어를 입력하세요. 예: 삼국지")
            self.message.set("B의 작품명을 검색하고 결과를 선택하세요." if source == "B" else "검색 결과를 선택하면 전체 회차를 불러옵니다.")
        self._update_actions()
        self._load_visible_images("contents")
        self._load_visible_images("episodes")

    def _clear_content_detail(self):
        self.active_content = None
        self.episode_title.set("작품을 선택하세요" if self.source == "B" else "콘텐츠를 선택하세요")
        self.episode_status.set("소설을 선택하면 전체 회차를 불러옵니다." if self.source == "B"
                                else "검색 결과를 클릭하면 전체 회차를 불러옵니다.")
        for value in self.text_detail_values.values():
            value.set("—")

    def _show_rows(self, side, rows):
        tree = self.content_tree if side == "contents" else self.episode_tree
        for index in range(len(rows), len(self.items[side])):
            if tree.exists(str(index)):
                tree.delete(str(index))
        self.items[side] = rows
        for index, item in enumerate(rows):
            label = item["title"]
            if side == "contents" and item.get("meta"):
                label += "\n" + item["meta"]
            if side == "contents" and item.get("views"):
                label += "\n조회 " + item["views"]
            photo = self.images.get((side, index))
            values = {"text": "" if photo else "이미지 없음", "values": (label,)}
            if side == "episodes" and self.source == "B":
                values = {"text": "", "values": (label, item.get("meta", ""))}
            if photo:
                values["image"] = photo
            if tree.exists(str(index)):
                tree.item(str(index), **values)
            else:
                tree.insert("", "end", iid=str(index), **values)
        if side == "contents":
            categories = list(dict.fromkeys(item["category"] for item in rows if item.get("category")))
            self.category_selector.configure(values=["소설"] if self.source == "B" else ["전체", *categories])

    def open_text_content(self):
        if self.source != "B" or not self.active_content:
            return
        try:
            os.startfile(self.active_content)
        except OSError as error:
            self.message.set(f"사이트를 열지 못했습니다. {error}")

    def verify_text_search(self):
        if self.source != "B" or not self.text_verification or self.loading:
            return
        if self.text_verification == "domain":
            self.pending_text_query = self.search_term
            self.loading = True
            self.app.refresh_text_domain(browser_verification=True)
            self.result_status.set("B 접속 검증 중…")
            self._update_actions()
        else:
            if self.text_verification == "episodes":
                self.select_content(force=True, browser_verification=True)
            else:
                self.search(query=self.search_term, browser_verification=True)

    def _reset(self, side):
        self.cancel[side].set()
        self.cancel[side] = threading.Event()
        self.generation[side] += 1
        tree = self.content_tree if side == "contents" else self.episode_tree
        tree.delete(*(str(index) for index in range(len(self.items[side])) if tree.exists(str(index))))
        self.items[side] = []
        if side == "contents":
            self.category.set("전체")
            self.category_selector.configure(values=("전체",), state="disabled")
        self.images = {key: image for key, image in self.images.items() if key[0] != side}
        self.requested_images = {key for key in self.requested_images if key[0] != side}
        self.image_attempts = {key: value for key, value in self.image_attempts.items() if key[0] != side}
        self.image_retry_at = {key: value for key, value in self.image_retry_at.items() if key[0] != side}

    def _submit(self, side, kind, callback, index=None):
        token, cancel = self.generation[side], self.cancel[side]
        events = self.events
        source = self.source

        def run():
            try:
                result = callback(cancel)
                if not cancel.is_set():
                    events.put((side, token, kind, result, None))
            except BrowserVerificationRequired as error:
                if not cancel.is_set():
                    if source == "B" and kind == "list":
                        events.put((side, token, "verification", error.url, str(error)))
                    else:
                        events.put((side, token, kind, (index, None), str(error)))
            except Exception as error:
                if not cancel.is_set():
                    events.put((side, token, kind, (index, None), str(error)))

        self.executor.submit(run)

    def _refresh_recent_searches(self):
        terms = self.app.recent_searches
        self.recent_selector.configure(values=terms, state="readonly" if terms else "disabled")
        self.recent_query.set(terms[0] if terms else "최근 검색어가 없습니다")
        for button in (self.delete_recent_button, self.clear_recent_button):
            button.configure(state="normal" if terms else "disabled")

    def delete_recent_search(self, all_items=False):
        if self.closed or self.app.closing:
            return
        previous = self.app.recent_searches
        query = self.recent_query.get()
        if not previous or (not all_items and query not in previous):
            return
        self.app.recent_searches = [] if all_items else [term for term in previous if term != query]
        self.app._dirty = True
        if not self.app._save_jobs(force=True):
            self.app.recent_searches = previous
            self.message.set("최근 검색 기록을 저장하지 못해 삭제하지 않았습니다. 디스크 공간과 저장 권한을 확인해 주세요.")
            return
        self._refresh_recent_searches()
        self.message.set("최근 검색 기록을 모두 삭제했습니다." if all_items else f"‘{query}’ 검색 기록을 삭제했습니다.")

    def use_recent_search(self, _=None):
        query = self.recent_query.get()
        if query in self.app.recent_searches:
            self.query.set(query)
            self.search()
            self.query_entry.focus_set()

    def search(self, *, query=None, browser_verification=False):
        if self.closed or self.app.closing:
            return
        query = self.query_entry.get().strip() if query is None else query.strip()
        if not query:
            self.result_status.set("검색어를 입력해 주세요.")
            self.query_entry.focus_set()
            return
        self.app.recent_searches = ([query] + [term for term in self.app.recent_searches if term != query])[:20]
        self.app._dirty = True
        self.app._save_jobs(force=True)
        self._refresh_recent_searches()
        self._reset("contents")
        self._reset("episodes")
        self._clear_content_detail()
        self.search_term = query
        self.loading = True
        self.pending_text_query = None
        self.text_verification = ""
        self.result_status.set(f"‘{query}’ 검색 중…")
        self.message.set("검색 결과의 모든 페이지를 불러오는 중입니다.")
        self._update_actions()
        if self.source == "B":
            if not self.app.text_domain or self.app.text_domain_check_worker is not None:
                self.pending_text_query = query
                self.result_status.set("B 접속 확인 중…")
                self.message.set("B 주소를 확인한 뒤 같은 검색어로 이어서 검색합니다.")
                if self.app.text_domain_check_worker is None:
                    self.app.refresh_text_domain()
                return
            origin = self.app.text_domain
            token = self.generation["contents"]
            log_run = uuid.uuid4().hex[:8]

            def lookup_text(cancel):
                from tvmon_catalog import search_text_catalog
                results, seen, page = [], set(), 1
                self.app._write_text_domain_log("search_start", run=log_run, url=origin, visible=browser_verification)
                try:
                    while True:
                        check_cancel(cancel)
                        rows, has_next = search_text_catalog(query, origin, cancel, page,
                            browser_fetch=lambda url, cancel, **kwargs: self.app._fetch_text_in_browser(url, cancel, log_run=log_run, request_only=True, **kwargs),
                            visible=browser_verification and page == 1)
                        check_cancel(cancel)
                        added = 0
                        for item in rows:
                            if item["url"] not in seen:
                                seen.add(item["url"])
                                added += 1
                                if item.get("category") == "소설" and re.fullmatch(r"/novel/[0-9]+/?", urlsplit(item["url"]).path):
                                    results.append(item)
                        self.app._write_text_domain_log("search_page", run=log_run, page=page, results=len(results))
                        if not has_next:
                            self.app._write_text_domain_log("search_success", run=log_run, results=len(results))
                            return results
                        if not added:
                            raise DownloadError("B 검색 결과의 다음 페이지가 반복됩니다. 전체 결과를 확인하지 못했습니다.")
                        self.events.put(("contents", token, "partial", results.copy(), None))
                        page += 1
                except Exception as error:
                    self.app._write_text_domain_log("search_cancelled" if cancel.is_set() else "search_error", run=log_run, detail=str(error))
                    raise

            self._submit("contents", "list", lookup_text)
            return

        domains = self.app.domains

        def lookup(cancel):
            from tvmon_catalog import search_catalog
            failures = []
            for origin in domains:
                check_cancel(cancel)
                # The sites use different page sizes; restart each candidate at page 1.
                results, seen, page = [], set(), 1
                try:
                    while True:
                        check_cancel(cancel)
                        rows, has_next = search_catalog(query, cancel, page, origin=origin)
                        check_cancel(cancel)
                        added = 0
                        for item in rows:
                            if item["url"] not in seen:
                                seen.add(item["url"])
                                results.append(item)
                                added += 1
                        if not has_next:
                            return results
                        if not added:
                            raise DownloadError("검색 결과의 다음 페이지가 반복됩니다. 전체 결과를 확인하지 못했습니다.")
                        page += 1
                except DownloadError as error:
                    failures.append(str(error))
            check_cancel(cancel)
            raise DownloadError("모든 도메인에서 검색에 실패했습니다.\n" + "\n".join(failures))

        self._submit("contents", "list", lookup)

    def filter_contents(self, _=None):
        if self.source == "B":
            self.category.set("소설")
        category = self.category.get()
        visible = []
        visible_urls = set()
        for index, item in enumerate(self.items["contents"]):
            row = str(index)
            if category == "전체" or item.get("category") == category:
                self.content_tree.move(row, "", "end")
                visible.append(row)
                visible_urls.add(item["url"])
            else:
                self.content_tree.detach(row)
        hidden_selection = [row for row in self.content_tree.selection() if row not in visible]
        if hidden_selection:
            self.content_tree.selection_remove(hidden_selection)
        if self.active_content is not None and self.active_content not in visible_urls:
            self._reset("episodes")
            self._clear_content_detail()
        total = len(self.items["contents"])
        detail = f"전체 {total}개" if category == "전체" else f"{category} {len(visible)}개 / 전체 {total}개"
        self.result_status.set(f"‘{self.search_term}’ · {detail}")
        self.content_tree.yview_moveto(0)
        self._load_visible_images("contents")
        self._update_actions()

    def select_content(self, _=None, *, force=False, browser_verification=False):
        selection = self.content_tree.selection()
        if not selection or self.closed:
            return
        content = self.items["contents"][int(selection[0])]
        if self.active_content == content["url"] and not force:
            return
        self._reset("episodes")
        self.active_content = content["url"]
        self.text_verification = ""
        self.episode_title.set(content["title"])
        if self.source == "B":
            self.episode_status.set("B · 작품 정보")
            for field, value in self.text_detail_values.items():
                value.set(content.get(field) or ("완결 표시 없음" if field == "status" else "표시 없음"))
            self._update_actions()
            if not re.fullmatch(r"/novel/[0-9]+/?", urlsplit(content["url"]).path):
                self.episode_status.set("TXT 다운로드는 소설 작품에서 지원합니다.")
                return
        self.episode_status.set("전체 회차를 불러오는 중…")
        self._update_actions()

        def lookup(cancel):
            if content.get("kind") == "novel" or re.fullmatch(r"/novel/[0-9]+/?", urlsplit(content["url"]).path):
                from tvmon_catalog import list_text_episodes
                log_run = uuid.uuid4().hex[:8]
                self.app._write_text_domain_log("episodes_start", run=log_run, url=content["url"])
                try:
                    rows = list_text_episodes(content["url"], cancel,
                        browser_fetch=lambda url, cancel, **kwargs: self.app._fetch_text_in_browser(
                            url, cancel, log_run=log_run, request_only=True, **kwargs), visible=browser_verification)
                    self.app._write_text_domain_log("episodes_success", run=log_run, results=len(rows))
                    return [{**row, "series_status": content.get("status", "")} for row in rows]
                except Exception as error:
                    self.app._write_text_domain_log("episodes_cancelled" if cancel.is_set() else "episodes_error", run=log_run, detail=str(error))
                    raise
            from tvmon_catalog import list_episodes
            return list_episodes(content["url"], cancel)

        self._submit("episodes", "list", lookup)

    def _scrolled(self, side, scrollbar, first, last):
        _set_scrollbar(scrollbar, first, last)
        self._load_visible_images(side)

    def _load_visible_images(self, side):
        if self.closed or not self.window.winfo_ismapped():
            return
        tree = self.content_tree if side == "contents" else self.episode_tree
        for index, item in enumerate(self.items[side]):
            key = (side, index)
            if key in self.requested_images or not item.get("thumbnail") or not tree.bbox(str(index)):
                continue
            max_attempts = 3 if side == "contents" else 1
            if self.image_attempts.get(key, 0) >= max_attempts or time.monotonic() < self.image_retry_at.get(key, 0):
                continue
            self.requested_images.add(key)
            self.image_attempts[key] = self.image_attempts.get(key, 0) + 1
            tree.item(str(index), text="불러오는 중")
            size = (86, 112) if side == "contents" else (128, 72)

            def load(cancel, index=index, item=item, size=size, retry=side == "contents"):
                from tvmon_images import thumbnail_ppm
                body, _ = fetch(item["thumbnail"], cancel, item["url"], limit=8 * 1024 * 1024, retry=retry)
                check_cancel(cancel)
                return index, thumbnail_ppm(body, *size)

            self._submit(side, "image", load, index=index)

    def retry_images(self):
        for index, item in enumerate(self.items["contents"]):
            key = ("contents", index)
            if item.get("thumbnail") and key not in self.images and key not in self.requested_images:
                self.image_attempts.pop(key, None)
                self.image_retry_at.pop(key, None)
        self._load_visible_images("contents")
        self.message.set("받지 못한 콘텐츠 썸네일을 다시 불러옵니다. 회차 썸네일은 재시도하지 않습니다.")

    def _poll(self):
        if self.closed:
            return
        for _ in range(50):
            try:
                side, token, kind, result, error = self.events.get_nowait()
            except queue.Empty:
                break
            if token != self.generation[side]:
                continue
            tree = self.content_tree if side == "contents" else self.episode_tree
            if kind == "verification":
                if side == "contents":
                    self.loading = False
                    self.text_verification = result
                    self.result_status.set("B 검색에 브라우저 검증이 필요합니다.")
                else:
                    self.text_verification = "episodes"
                    self.episode_status.set("회차 목록에 브라우저 검증이 필요합니다.")
                self.message.set("‘브라우저 검증 열기’에서 검증을 완료하면 요청한 목록을 다시 불러옵니다.")
                self._update_actions()
                continue
            if kind == "partial":
                self._show_rows(side, result)
                self.result_status.set(f"‘{self.search_term}’ · 현재 {len(result)}개 확인 · 추가 결과 읽는 중…")
                self.message.set("작품을 선택해 정보를 볼 수 있습니다. 나머지 검색 결과를 계속 불러옵니다.")
                self._update_actions()
                self._load_visible_images(side)
                continue
            if kind == "image":
                index, body = result
                key = (side, index)
                if not error:
                    try:
                        photo = tk.PhotoImage(master=self.window, data=body, format="PPM")
                        self.images[key] = photo
                        tree.item(str(index), image=photo, text="")
                        self.image_retry_at.pop(key, None)
                    except tk.TclError:
                        error = "썸네일을 표시하지 못했습니다."
                if error:
                    self.requested_images.discard(key)
                    if side == "contents":
                        self.image_retry_at[key] = time.monotonic() + 1.5 * self.image_attempts.get(key, 1)
                        tree.item(str(index), text="다시 받는 중" if self.image_attempts.get(key, 0) < 3 else "다시 받기 필요")
                    else:
                        tree.item(str(index), text="이미지 없음")
                continue
            if side == "contents":
                self.loading = False
            if error:
                if side == "contents":
                    self.result_status.set(f"{len(self.items[side])}개 확인 · 전체 결과를 불러오지 못했습니다. 다시 검색해 주세요."
                                           if self.source == "B" and self.items[side] else "검색하지 못했습니다. 다시 검색해 주세요.")
                else:
                    self.active_content = None
                    self.episode_status.set("회차를 불러오지 못했습니다. 콘텐츠를 다시 선택해 주세요.")
                self.message.set(error)
                self._update_actions()
                continue
            if side == "contents":
                self.items[side] = result
                self.result_status.set(f"‘{self.search_term}’ · 전체 {len(result)}개")
                self.message.set("원하는 콘텐츠를 클릭하세요." if self.items[side] else "검색 결과가 없습니다. 다른 검색어를 입력해 주세요.")
            else:
                self.items[side] = result
                self.episode_status.set((f"다운로드 가능 {len(result)}개 회차 · 준비중/링크 없는 회차 제외" if self.source == "B"
                                         else f"전체 {len(result)}개 회차 · 첫 회차부터 표시") if result else "다운로드할 회차가 없습니다.")
                self.message.set("회차를 선택한 뒤 Q에 추가하거나 다운로드하세요. 기존 항목은 중복 추가하지 않습니다.")
            self._show_rows(side, result)
            self._update_actions()
            self._load_visible_images(side)
        if any(key[0] == "contents" and self.image_attempts.get(key, 0) < 3 for key in self.image_retry_at):
            self._load_visible_images("contents")
        if self.pending_text_query is not None and self.app.text_domain_check_worker is None:
            query, self.pending_text_query = self.pending_text_query, None
            if self.app._text_domain_result == "success" and self.app.text_domain:
                self.search(query=query)
            else:
                self.loading = False
                self.text_verification = "domain" if self.app.text_domain_verification_url else ""
                self.result_status.set("B 접속 검증이 필요합니다." if self.text_verification else "B 접속 주소를 확인하지 못했습니다.")
                self.message.set(self.app.text_domain_status.get())
                self._update_actions()
        self._poll_id = self.window.after(80, self._poll)

    def select_all(self, _=None):
        self.episode_tree.selection_set(self.episode_tree.get_children())
        self._update_actions()
        return "break"

    def clear_selection(self):
        self.episode_tree.selection_remove(self.episode_tree.selection())
        self._update_actions()

    def _update_actions(self):
        self._update_source_controls()
        selected = len(self.episode_tree.selection())
        self.selection_status.set(f"{selected} / {len(self.items['episodes'])}개 선택")
        self.queue_selected_button.configure(state="normal" if selected else "disabled")
        self.download_button.configure(state="normal" if selected else "disabled")
        self.queue_all_button.configure(state="normal" if self.items["episodes"] else "disabled")
        self.zip_button.configure(state="normal" if self.source == "B" and self.items["episodes"] else "disabled")
        self.retry_images_button.configure(state="normal" if self.items["contents"] else "disabled")
        if self.source == "B":
            self.category.set("소설")
            self.category_selector.configure(values=("소설",), state="disabled")
        else:
            self.category_selector.configure(state="readonly" if self.items["contents"] and not self.loading else "disabled")
        self.text_open_button.configure(state="normal" if self.source == "B" and self.active_content else "disabled")
        if self.source == "B" and self.text_verification:
            self.text_verify_button.pack(side="right")
            self.text_verify_button.configure(state="disabled" if self.loading else "normal")
        else:
            self.text_verify_button.pack_forget()

    def _update_source_controls(self):
        text = self.source == "B"
        novel = text and self.active_content and re.fullmatch(r"/novel/[0-9]+/?", urlsplit(self.active_content).path)
        for control in self.video_controls:
            control.grid() if not text or novel else control.grid_remove()
        if text:
            self.text_detail.grid(row=2, column=0, columnspan=2, sticky="ew")
        else:
            self.text_detail.grid_remove()
        self.episode_tree.configure(style="TextEpisode.Treeview" if text else "Episode.Treeview",
                                    columns=("title", "date") if text else ("title",), show="headings" if text else "tree headings")
        self.episode_tree.heading("title", text="회차 / 제목" if text else "회차 / 영상 제목")
        self.episode_tree.column("title", width=360, minwidth=180 if text else 260)
        if text:
            self.episode_tree.heading("date", text="등록일")
            self.episode_tree.column("date", width=110, minwidth=90, stretch=False)
        self.download_button.configure(text="선택 TXT 저장" if text else "선택 다운로드")
        if text:
            self.zip_button.pack(side="right", before=self.download_button, padx=(6, 0))
        else:
            self.zip_button.pack_forget()
        if novel:
            self.zip_cleanup_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        else:
            self.zip_cleanup_check.grid_remove()

    def queue_text_zip(self):
        if self.source != "B" or not self.items["episodes"]:
            return
        try:
            archive = self.app.add_text_archive(self.items["episodes"], cleanup_originals=self.zip_cleanup_originals.get())
            self.message.set("전체 ZIP이 저장되어 있습니다. 다운로드 탭에서 완료 파일을 열 수 있습니다." if archive["status"] == "completed"
                             else "전체 회차를 차례로 받고 오류 회차를 최대 3회 재시도한 뒤 ZIP으로 저장합니다. 다운로드 탭에서 확인하세요.")
        except (DownloadError, OSError) as error:
            self.message.set(str(error))

    def queue_episodes(self, *, all_items=False, start=False):
        selected = set(self.episode_tree.selection())
        items = [item for index, item in enumerate(self.items["episodes"]) if all_items or str(index) in selected]
        if not items:
            return
        try:
            added, skipped = self.app.add_episodes(items, start=start)
            text = f"{added}개 회차를 Q에 추가했습니다. 기존 항목 {skipped}개 건너뜀."
            if start and added:
                text += " 매니저에서 다운로드 진행 상태를 확인하세요."
            elif not added:
                text += " 기존 작업은 매니저에서 시작 / 이어받기로 재개할 수 있습니다."
            self.message.set(text)
        except (DownloadError, OSError) as error:
            self.message.set(str(error))

    def close(self):
        if self.closed:
            return
        self.closed = True
        for cancel in self.cancel.values():
            cancel.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.images.clear()
        self.source_states.clear()
        try:
            self.window.after_cancel(self._poll_id)
            self.window.destroy()
        except tk.TclError:
            pass


class DownloaderApp:
    STATE_LABELS = {"queued": "대기", "resolving": "정보 확인", "downloading": "다운로드 중",
                    "merging": "병합 중", "pausing": "일시정지 중", "paused": "일시정지",
                    "completed": "완료", "error": "오류", "cleaning": "TXT 정리 중"}
    JOB_FIELDS = ("id", "url", "folder", "title", "status", "total", "done", "bytes",
                  "completed_indices", "file", "message", "duration", "server_count", "servers", "kind", "verification_url",
                  "archive_jobs", "retry_round", "archive_urls", "archive_owner", "cleanup_originals", "cleanup_pending",
                  "p2p_completed_indices", "http_completed_indices")

    def __init__(self, root: tk.Tk, *, state_path: Path | None = None):
        self.root = root
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.active_id = None
        self.run_counter = 0
        self.queue_running = False
        self.closing = False
        self.jobs = []
        self._resume_cleanup_pending = set()
        self.saved_file = None
        self.output_folder = None
        self._dirty = False
        self._last_save = 0
        self._last_draw = 0
        self._map_signature = None
        self._log_signature = None
        self._server_signature = None
        self._table_rows = {}
        self._queue_title_signature = None
        self._drag_selection_anchor = None
        self._job_menu_open = False
        self._state_lock = None
        self._destroyed = False
        self.search_dialog = None
        self.recent_searches = []
        self.domains = tuple(DEFAULT_DOMAINS)
        self.domain_guide_url = DOMAIN_GUIDE_URL
        self.text_domain = ""
        self.text_domain_guide_url = TEXT_DOMAIN_GUIDE_URL
        app_folder = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        legacy_folder = Path(os.environ.get("LOCALAPPDATA", str(app_folder))) / "TVMON-Download-Manager"
        self.state_path = Path(state_path) if state_path else app_folder / "data" / "jobs.json"
        self.text_domain_log_path = self.state_path.parent / "text-domain.log"
        self.program_log_path = self.state_path.parent / "program.log"
        self._program_logger = None
        self._program_log_error = ""
        self._program_log_warned = False
        # Chrome authentication belongs to this PC; retain its existing profile.
        self._text_browser_profile = (self.state_path.parent if state_path else legacy_folder) / "text-browser"
        self._text_log_lock = threading.Lock()
        self._text_browser_lock = threading.Lock()
        self._text_log_error = ""
        self._text_log_run = ""
        self._text_domain_result = None
        self.url = tk.StringVar()
        self.folder = tk.StringVar(value=str(app_folder / "다운로드"))
        self.filter = tk.StringVar(value="전체")
        self.list_query = tk.StringVar()
        self.status = tk.StringVar()
        self.selection_status = tk.StringVar()
        self.video_title = tk.StringVar(value="다운로드할 영상을 추가하세요")
        self.video_meta = tk.StringVar(value="목록에서 작업을 선택하면 상세 상태를 볼 수 있습니다.")
        self.progress_meta = tk.StringVar(value="목록에서 작업을 선택하세요")
        self.percent = tk.StringVar(value="0%")
        self.transferred = tk.StringVar(value="0 MB")
        self.speed = tk.StringVar(value="—")
        self.remaining = tk.StringVar(value="—")
        self.file_name = tk.StringVar(value="완료한 조각부터 이어받습니다.")
        self.part_summary = tk.StringVar(value="아직 영상 조각 정보가 없습니다.")
        self.map_summary = tk.StringVar(value="아직 영상 조각 정보가 없습니다.")
        self.server_summary = tk.StringVar(value="영상 조각 지도")
        self.worker_host = tk.StringVar(value="작업을 선택하면 연결 중인 서버를 표시합니다.")
        self.server_status = tk.StringVar()
        self.peer_summary = tk.StringVar(value="P2P 미리받기 · 대기 중")
        self.p2p_connection_status = tk.StringVar(value="P2P · 대기")
        self.p2p_overview = tk.StringVar(value="대기")
        self.domain_input = tk.StringVar()
        self.domain_status = tk.StringVar(value="위에서부터 접속합니다. 실패하면 다음 도메인으로 이동합니다.")
        self.auto_domain = tk.BooleanVar(value=True)
        self.auto_retry = tk.BooleanVar(value=True)
        self.fastest_server_only = tk.BooleanVar(value=False)
        self._fastest_server_only_event = threading.Event()
        self.domain_check_cancel = threading.Event()
        self.domain_check_worker = None
        self.domain_check_events = queue.Queue()
        self.auto_text_domain = tk.BooleanVar(value=True)
        self.text_domain_value = tk.StringVar(value="아직 접속을 확인한 주소가 없습니다.")
        self.text_domain_status = tk.StringVar(value="안내 페이지의 hud-url 주소부터 접속을 확인합니다.")
        self.text_domain_check_cancel = threading.Event()
        self.text_domain_check_worker = None
        self.text_domain_check_events = queue.Queue()
        self.text_domain_verification_url = ""
        self._text_browser = None
        self._domain_check_timer = None
        self._lock_state()
        self._init_program_log()
        try:
            if state_path is None:
                self._migrate_legacy_state(legacy_folder)
            self._load_jobs()
            self._build()
        except BaseException:
            self._write_program_log("startup_error", level=logging.ERROR, detail=traceback.format_exc())
            self._close_program_log()
            self._release_state_lock()
            raise
        self._previous_callback_exception = root.report_callback_exception
        root.report_callback_exception = self._report_callback_exception
        self._write_program_log("settings_loaded", domains=self.domains, jobs=len(self.jobs),
                                auto_domain=self.auto_domain.get(), auto_text_domain=self.auto_text_domain.get())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Destroy>", self._on_destroy, add="+")
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Delete>", lambda _: self.remove_jobs())
        self.tree.bind("<space>", self._toggle_selected)
        self._refresh_table()
        if self.jobs:
            self.tree.selection_set(self.jobs[0]["id"])
            self.tree.focus(self.jobs[0]["id"])
        self._prepare_resume_caches()
        self._refresh_details(force=True)
        root.after(100, self.poll)
        self._domain_check_timer = root.after(250, self._start_domain_check)

    def _init_program_log(self):
        try:
            handler = RotatingFileHandler(self.program_log_path, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                                                   datefmt="%Y-%m-%d %H:%M:%S"))
            handler.handleError = lambda record: setattr(self, "_program_log_error", "프로그램 로그 파일에 기록하지 못했습니다.")
            self._program_logger = logging.Logger("TVMON", level=logging.INFO)
            self._program_logger.addHandler(handler)
            self._write_program_log("program_start", python=sys.version.split()[0],
                                    frozen=bool(getattr(sys, "frozen", False)))
        except OSError:
            self._program_log_error = "프로그램 로그 파일을 만들지 못했습니다. 저장 공간과 권한을 확인해 주세요."

    def _write_program_log(self, event, *, level=logging.INFO, **fields):
        if self._program_logger is None:
            return
        def clean(value):
            if isinstance(value, str):
                def url_only(match):
                    try:
                        parsed = urlsplit(match[0])
                        host = parsed.netloc.rsplit("@", 1)[-1]
                        return f"{parsed.scheme}://{host}{parsed.path}"
                    except ValueError:
                        return "[주소 생략]"
                return " ".join(re.sub(r"(?:https?|wss?)://[^\s<>\"']+", url_only, value).split())[:4000]
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value[:40]]
            if isinstance(value, dict):
                return {str(key)[:80]:clean(item) for key, item in list(value.items())[:40]}
            return value if value is None or isinstance(value, (int, float, bool)) else clean(str(value))
        try:
            record = {"pid":os.getpid(), "thread":threading.current_thread().name,
                      "event":event, **{key:clean(value) for key, value in fields.items()}}
            self._program_logger.log(level, json.dumps(record, ensure_ascii=False))
        except Exception:
            self._program_log_error = "프로그램 로그 파일에 기록하지 못했습니다."

    def _close_program_log(self):
        logger, self._program_logger = self._program_logger, None
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except OSError:
                    self._program_log_error = "프로그램 로그를 닫는 중 기록을 저장하지 못했습니다."

    def _report_callback_exception(self, error_type, error, stack):
        self._write_program_log("ui_error", level=logging.ERROR,
                                detail="".join(traceback.format_exception(error_type, error, stack)))
        self.status.set("화면 처리 중 오류가 발생했습니다. 상단 ‘프로그램 로그’에서 확인해 주세요.")
        try:
            self._previous_callback_exception(error_type, error, stack)
        except Exception:
            pass

    def _lock_state(self):
        import msvcrt
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.state_path.with_suffix(".lock").open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise DownloadError("다운로드 매니저가 이미 실행 중입니다. 기존 매니저 창을 사용해 주세요.") from None
        self._state_lock = handle

    def _migrate_legacy_state(self, legacy_folder):
        """Copy legacy records once, keeping both the original and portable records intact."""
        import msvcrt
        source = legacy_folder / "jobs.json"
        if self.state_path.exists() or not source.is_file():
            return
        if source.stat().st_size > 8 * 1024 * 1024:
            raise DownloadError(f"기존 작업 목록의 크기가 너무 커 이전하지 않았습니다. 원본은 보존했습니다.\n{source}")
        with source.with_suffix(".lock").open("a+b") as legacy_lock:
            if legacy_lock.tell() == 0:
                legacy_lock.write(b"0")
                legacy_lock.flush()
            legacy_lock.seek(0)
            try:
                msvcrt.locking(legacy_lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise DownloadError("기존 기록을 이전하려면 이전 버전의 다운로드 매니저를 먼저 종료해 주세요.") from None
            # Publish jobs.json last so an interrupted migration can be retried.
            for name in ("text-domain.log", "text-domain.previous.log", "jobs.json"):
                old, target = legacy_folder / name, self.state_path.parent / name
                if not old.is_file() or target.exists():
                    continue
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.state_path.parent, prefix="migration-", suffix=".tmp", delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(old.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.rename(target)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()

    def _release_state_lock(self):
        if self._state_lock is not None:
            self._state_lock.close()
            self._state_lock = None

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._write_program_log("program_stopped")
            self._close_program_log()
            self._destroyed = True
            self._cancel_domain_check()
            if self.search_dialog is not None:
                self.search_dialog.close()
            self._release_state_lock()

    def _load_jobs(self):
        if not self.state_path.exists():
            set_domains(self.domains)
            return
        try:
            if self.state_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError()
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("jobs"), list):
                raise ValueError()
            self.domains = normalize_domains(data.get("domains", list(DEFAULT_DOMAINS)))
            self.domain_guide_url = normalize_text_guide_url(data.get("domain_guide_url", DOMAIN_GUIDE_URL))
            if not isinstance(data.get("auto_domain", True), bool):
                raise ValueError()
            self.auto_domain.set(data.get("auto_domain", True))
            if type(data.get("auto_retry", True)) is not bool:
                raise ValueError()
            self.auto_retry.set(data.get("auto_retry", True))
            if type(data.get("fastest_server_only", False)) is not bool:
                raise ValueError()
            self.fastest_server_only.set(data.get("fastest_server_only", False))
            if self.fastest_server_only.get():
                self._fastest_server_only_event.set()
            if not isinstance(data.get("auto_text_domain", True), bool) or not isinstance(data.get("text_domain", ""), str):
                raise ValueError()
            self.auto_text_domain.set(data.get("auto_text_domain", True))
            self.text_domain = normalize_domain(data["text_domain"]) if data.get("text_domain") else ""
            if self.text_domain and not is_text_site_url(self.text_domain):
                self.text_domain = ""
            self.text_domain_guide_url = normalize_text_guide_url(data.get("text_domain_guide_url", TEXT_DOMAIN_GUIDE_URL))
            recent_searches = data.get("recent_searches", [])
            if not isinstance(recent_searches, list) or any(not isinstance(term, str) for term in recent_searches):
                raise ValueError()
            self.recent_searches = list(dict.fromkeys(term.strip() for term in recent_searches if term.strip()))[:20]
            set_domains(self.domains)
            seen = set()
            loaded = []
            for item in data["jobs"]:
                if not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{32}", item.get("id", "")) or item["id"] in seen:
                    raise ValueError()
                if not isinstance(item.get("url"), str) or not isinstance(item.get("folder"), str):
                    raise ValueError()
                kind = item.get("kind", "video")
                if kind not in ("video", "text", "text_zip"):
                    raise ValueError()
                page, *_ = (parse_text_page_url if kind in ("text", "text_zip") else parse_page_url)(item["url"])
                members = item.get("archive_jobs", [])
                retry_round = item.get("retry_round", 0)
                paths = item.get("archive_urls", [])
                owner = item.get("archive_owner")
                if (not isinstance(paths, list) or any(not isinstance(path, str) or not re.fullmatch(r"/novel/[0-9]+/[0-9]+", path) for path in paths)
                        or len(set(paths)) != len(paths) or (paths and len(paths) != len(members))
                        or (owner is not None and (kind != "text" or not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner)))
                        or type(item.get("cleanup_originals", False)) is not bool or type(item.get("cleanup_pending", False)) is not bool):
                    raise ValueError()
                if (not isinstance(members, list) or any(not isinstance(member, str) or not re.fullmatch(r"[0-9a-f]{32}", member) for member in members)
                        or len(set(members)) != len(members) or item["id"] in members
                        or type(retry_round) is not int or not 0 <= retry_round <= len(TEXT_ZIP_RETRY_DELAYS)
                        or (kind == "text_zip" and not members) or (kind != "text_zip" and members)):
                    raise ValueError()
                folder = str(Path(item["folder"]).expanduser().resolve())
                total = int(item.get("total", 0))
                duration = float(item.get("duration", 0))
                server_count = int(item.get("server_count", 0))
                servers = item.get("servers", {})
                if (not 0 <= total <= 1000000 or not math.isfinite(duration) or duration < 0
                        or not 0 <= server_count <= total or not isinstance(item.get("title", ""), str)):
                    raise ValueError()
                if (not isinstance(servers, dict)
                        or any(not isinstance(host, str) or not host or type(count) is not int or count <= 0
                               for host, count in servers.items())
                        or (servers and (len(servers) != server_count or sum(servers.values()) != total))):
                    raise ValueError()
                completed = sorted(set(int(index) for index in item.get("completed_indices", []) if 0 <= int(index) < total))
                p2p_completed = item.get("p2p_completed_indices", [])
                http_completed = item.get("http_completed_indices", [])
                if (not isinstance(p2p_completed, list)
                        or not isinstance(http_completed, list)
                        or any(type(index) is not int or not 0 <= index < total for index in p2p_completed + http_completed)
                        or set(p2p_completed).intersection(http_completed)):
                    raise ValueError()
                job = self._new_job(page, folder, item["id"])
                job.update({key: item[key] for key in self.JOB_FIELDS if key in item})
                job.update(url=page, folder=folder, total=total, duration=duration, server_count=server_count, servers=servers, completed_indices=completed,
                           done=len(completed), bytes=max(0, int(item.get("bytes", 0))),
                           p2p_completed_indices=sorted(set(p2p_completed).intersection(completed)),
                           http_completed_indices=sorted(set(http_completed).intersection(completed)),
                           status="completed" if item.get("status") == "completed" else "paused")
                if job.get("file"):
                    target = Path(job["file"]).resolve()
                    if target.parent != Path(folder) or target.suffix.lower() != {"text": ".txt", "text_zip": ".zip", "video": ".ts"}[kind]:
                        raise ValueError()
                    job["file"] = str(target)
                    if kind == "text_zip" and job["cleanup_pending"]:
                        job["status"] = "completed"
                loaded.append(job)
                seen.add(item["id"])
            self.jobs = loaded
            for job in self.jobs:
                if job["kind"] == "text_zip" and not job["archive_urls"]:
                    paths = [urlsplit(self._job(member)["url"]).path for member in job["archive_jobs"] if self._job(member)]
                    if len(paths) == len(job["archive_jobs"]):
                        job["archive_urls"] = paths
            if isinstance(data.get("folder"), str) and data["folder"].strip():
                self.folder.set(data["folder"])
        except (ValueError, TypeError, KeyError, OverflowError, OSError, DownloadError):
            raise DownloadError(f"작업 목록을 읽을 수 없습니다. 원본 목록은 보존했습니다.\n{self.state_path}") from None

    def _prepare_resume_caches(self):
        for job in self.jobs:
            if job.get("kind") != "video":
                continue
            if job["status"] == "completed":
                self._resume_cleanup_pending.add(job["id"])
                continue
            try:
                ResumeCache(Path(job["folder"]), job["id"]).migrate_legacy()
            except (OSError, ResumeError) as error:
                self._log(job, f"이어받기 임시폴더 이전을 보류했습니다: {error}")
                self.status.set(job["message"])
                self._dirty = True
        self._cleanup_resume_caches()

    def _cleanup_resume_caches(self):
        for job_id in tuple(self._resume_cleanup_pending):
            if job_id == self.active_id:
                continue
            self._resume_cleanup_pending.discard(job_id)
            job = self._job(job_id)
            if not job or job.get("kind") != "video" or job["status"] != "completed" or not job.get("file"):
                continue
            try:
                if Path(job["file"]).is_file():
                    discard_resume(Path(job["folder"]), job_id)
            except (OSError, DownloadError) as error:
                self._log(job, f"완료된 작업의 임시폴더 정리를 보류했습니다: {error}")
                self.status.set(job["message"])
                self._dirty = True

    def _save_jobs(self, force=False):
        if not self._dirty or (not force and time.monotonic() - self._last_save < 1):
            return not self._dirty
        self.folder_entry._sync_variable()
        data = {"version": 1, "folder": self.folder.get(), "domains": list(self.domains),
                "domain_guide_url": self.domain_guide_url,
                "auto_domain": self.auto_domain.get(),
                "auto_retry": self.auto_retry.get(),
                "fastest_server_only": self.fastest_server_only.get(),
                "text_domain": self.text_domain, "auto_text_domain": self.auto_text_domain.get(),
                "text_domain_guide_url": self.text_domain_guide_url,
                "recent_searches": self.recent_searches,
                "jobs": [{key: job.get(key) for key in self.JOB_FIELDS} for job in self.jobs]}
        temporary = self.state_path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            self._dirty = False
            self._last_save = time.monotonic()
            return True
        except OSError:
            self._last_save = time.monotonic()
            self.status.set("작업 목록 저장에 실패했습니다. 디스크 공간과 저장 권한을 확인해 주세요.")
            return False

    @staticmethod
    def _new_job(url, folder, job_id):
        return {"id": job_id, "url": url, "folder": folder, "title": url,
                "kind": "video", "verification_url": None, "archive_jobs": [], "retry_round": 0, "retry_at": 0, "auto_retry_at": 0,
                "archive_urls": [], "archive_owner": None, "cleanup_originals": False, "cleanup_pending": False,
                "status": "queued", "total": 0, "done": 0, "bytes": 0,
                "completed_indices": [], "file": None, "message": "대기 중", "duration": 0, "server_count": 0, "servers": {},
                "p2p_completed_indices": [], "http_completed_indices": [], "p2p_active_indices": [], "p2p_parts": [],
                "http_bytes": 0, "p2p_bytes": 0, "http_speed": 0, "p2p_speed": 0,
                "p2p_peers": 0, "p2p_status": "disabled", "p2p_trackers": [],
                "speed": 0, "eta": 0, "workers": [], "segment_states": {}, "server_health": {}, "log": deque(maxlen=200)}

    def _build(self):
        root = self.root
        root.title("TVMON Download Manager — 이어받기")
        root.configure(bg="#F3F5F9")
        root.geometry(f"{min(1200, root.winfo_screenwidth() - 80)}x{min(900, root.winfo_screenheight() - 80)}")
        root.minsize(960, 850)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#F3F5F9")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("TLabel", background="#F3F5F9", foreground="#182638", font=("맑은 고딕", 11))
        style.configure("Card.TLabel", background="#FFFFFF")
        style.configure("Muted.TLabel", background="#FFFFFF", foreground="#586A7D", font=("맑은 고딕", 10))
        style.configure("Detail.Section.TLabel", background="#FFFFFF", foreground="#182638", font=("맑은 고딕", 11, "bold"))
        style.configure("Detail.Meta.TLabel", background="#FFFFFF", foreground="#526478", font=("맑은 고딕", 9))
        style.configure("Metric.TFrame", background="#F1F5FA")
        style.configure("Metric.Caption.TLabel", background="#F1F5FA", foreground="#526478", font=("맑은 고딕", 9))
        style.configure("Metric.Value.TLabel", background="#F1F5FA", foreground="#182638", font=("맑은 고딕", 18, "bold"))
        style.configure("Metric.Status.TLabel", background="#F1F5FA", foreground="#182638", font=("맑은 고딕", 11, "bold"))
        style.configure("Path.TEntry", padding=(8, 4), font=("맑은 고딕", 9))
        style.map("Path.TEntry", fieldbackground=[("readonly", "#F7F9FC")], foreground=[("readonly", "#354C64")])
        style.configure("TCheckbutton", background="#FFFFFF", foreground="#20364D", font=("맑은 고딕", 10), padding=(0, 4))
        style.map("TCheckbutton", background=[("active", "#FFFFFF")])
        self._retry_check_images = []
        indicator_size = max(20, round(root.winfo_fpixels("14p")))
        scale = indicator_size / 20
        for checked, color in ((False, "#8294AC"), (True, "#245EB8"), (False, "#BAC4D2"), (True, "#BAC4D2")):
            indicator = tk.PhotoImage(master=root, width=indicator_size, height=indicator_size)
            indicator.put("#F3F5F9", to=(0, 0, indicator_size, indicator_size))
            indicator.put(color, to=(round(scale), round(scale), round(19 * scale), round(19 * scale)))
            if checked:
                for x, y in ((5, 9), (6, 10), (7, 11), (8, 12), (9, 11), (10, 10), (11, 9), (12, 8), (13, 7), (14, 6)):
                    indicator.put("#FFFFFF", to=(round(x * scale), round(y * scale), round((x + 2) * scale), round((y + 2) * scale)))
            else:
                indicator.put("#F3F5F9", to=(round(2 * scale), round(2 * scale), round(18 * scale), round(18 * scale)))
            self._retry_check_images.append(indicator)
        unchecked, checked, disabled, disabled_checked = self._retry_check_images
        style.element_create("Retry.indicator", "image", unchecked,
                             ("disabled", "selected", disabled_checked), ("disabled", disabled), ("selected", checked),
                             width=indicator_size + 8, sticky="w")
        style.layout("Retry.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Retry.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        style.configure("Retry.TCheckbutton", background="#F3F5F9", foreground="#20364D",
                        font=("맑은 고딕", 10), padding=(0, 5), focusthickness=1, focuscolor="#245EB8")
        style.map("Retry.TCheckbutton", background=[("active", "#F3F5F9")],
                  foreground=[("disabled", "#7F8C9B"), ("active", "#245EB8")])
        style.configure("Card.Retry.TCheckbutton", background="#FFFFFF")
        style.map("Card.Retry.TCheckbutton", background=[("active", "#FFFFFF")])
        style.configure("TButton", font=("맑은 고딕", 10), padding=(12, 8), background="#E6EBF3", foreground="#20364D", borderwidth=0)
        style.map("TButton", background=[("disabled", "#EDF1F7"), ("pressed", "#CAD9EC"), ("active", "#D5E0EF")], foreground=[("disabled", "#7F8C9B")])
        style.configure("Primary.TButton", background="#245EB8", foreground="#FFFFFF")
        style.map("Primary.TButton", background=[("disabled", "#BACBE3"), ("pressed", "#123D79"), ("active", "#184E9F")], foreground=[("disabled", "#F7F9FD")])
        style.configure("TEntry", padding=7, font=("맑은 고딕", 10), bordercolor="#CFD9E6", lightcolor="#FFFFFF", darkcolor="#FFFFFF")
        style.map("TEntry", bordercolor=[("focus", "#245EB8")])
        style.configure("TCombobox", padding=(8, 7), arrowsize=12, bordercolor="#CFD9E6", lightcolor="#FFFFFF", darkcolor="#FFFFFF")
        style.map("TCombobox", fieldbackground=[("readonly", "#FFFFFF")], background=[("readonly", "#EDF2F8")],
                  foreground=[("disabled", "#7F8C9B"), ("readonly", "#20364D")], bordercolor=[("focus", "#245EB8")],
                  selectbackground=[("readonly", "#FFFFFF")], selectforeground=[("readonly", "#20364D")])
        style.configure("Treeview", font=("맑은 고딕", 10), rowheight=29, background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#20364D", borderwidth=0,
                        bordercolor="#DEE5ED", lightcolor="#DEE5ED", darkcolor="#DEE5ED")
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"), background="#E8EEF6", foreground="#354C64", padding=(7, 7), relief="flat")
        style.configure("Worker.Treeview", font=("맑은 고딕", 9), rowheight=23)
        style.configure("Worker.Treeview.Heading", padding=(5, 5))
        style.map("Treeview", background=[("selected", "#DCEAFE")], foreground=[("selected", "#123D79")])
        style.configure("TNotebook", background="#F3F5F9", borderwidth=0, bordercolor="#F3F5F9", lightcolor="#F3F5F9", darkcolor="#F3F5F9")
        style.configure("Main.TNotebook", tabmargins=(18, 8, 18, 0))
        style.configure("TNotebook.Tab", padding=(18, 9), font=("맑은 고딕", 11), background="#E9EEF5", foreground="#52687E", borderwidth=0,
                        bordercolor="#F3F5F9", lightcolor="#E9EEF5", darkcolor="#E9EEF5")
        style.map("TNotebook.Tab", padding=[("selected", (24, 12)), ("!selected", (18, 9))],
                  font=[("selected", ("맑은 고딕", 12, "bold"))],
                  background=[("selected", "#245EB8"), ("active", "#DCEAFE")], foreground=[("selected", "#FFFFFF")],
                  lightcolor=[("selected", "#245EB8"), ("!selected", "#E9EEF5")], darkcolor=[("selected", "#245EB8")])
        style.configure("Detail.TNotebook.Tab", padding=(16, 9), font=("맑은 고딕", 10))
        style.map("Detail.TNotebook.Tab", padding=[("selected", (16, 9)), ("!selected", (16, 9))],
                  font=[("selected", ("맑은 고딕", 10, "bold")), ("!selected", ("맑은 고딕", 10))])
        style.configure("Queue.Treeview", rowheight=32, font=("맑은 고딕", 11), padding=(0, 0, 10, 0))
        style.configure("Quiet.TButton", background="#FFFFFF", foreground="#245EB8", padding=(10, 5))
        style.configure("Nav.TButton", anchor="w", padding=(12, 10), background="#FFFFFF", font=("맑은 고딕", 11, "bold"))
        style.configure("Selected.Nav.TButton", background="#FFFFFF", foreground="#184E9F")
        style.configure("Filter.TButton", padding=(12, 5), background="#EDF2F8")
        style.configure("Selected.Filter.TButton", background="#DCEAFE", foreground="#184E9F", font=("맑은 고딕", 10, "bold"))
        style.configure("Queue.Filter.TButton", padding=(12, 9), background="#EDF2F8")
        style.configure("Selected.Queue.Filter.TButton", background="#DCEAFE", foreground="#184E9F", font=("맑은 고딕", 10, "bold"))
        style.layout("Sites.TNotebook.Tab", [])
        style.configure("Horizontal.TProgressbar", background="#2874D0", troughcolor="#E7EDF5", bordercolor="#E7EDF5",
                        lightcolor="#2874D0", darkcolor="#2874D0", borderwidth=0, thickness=8)
        style.configure("TScrollbar", background="#C3D0E0", troughcolor="#F3F5F9", bordercolor="#F3F5F9",
                        lightcolor="#C3D0E0", darkcolor="#C3D0E0", arrowsize=11, borderwidth=0)
        style.map("TScrollbar", background=[("pressed", "#8FA7C4"), ("active", "#A6BAD2")])
        self.main_tabs = ttk.Notebook(root, style="Main.TNotebook")
        self.main_tabs.pack(fill="both", expand=True)
        self.download_tab = ttk.Frame(self.main_tabs)
        self.search_tab = ttk.Frame(self.main_tabs)
        self.domains_tab = ttk.Frame(self.main_tabs)
        self.main_tabs.add(self.download_tab, text="다운로드")
        self.main_tabs.add(self.search_tab, text="콘텐츠 검색")
        self.main_tabs.add(self.domains_tab, text="도메인 설정")
        connection_status = ttk.Frame(self.main_tabs)
        connection_status.place(relx=1, x=-18, y=14, anchor="ne")
        self.program_log_button = ttk.Button(connection_status, text="프로그램 로그", style="Quiet.TButton",
                                             command=self.open_program_log)
        self.program_log_button.pack(side="left", padx=(0, 6))
        self.header_site_badges = {}
        for site, title in (("A", "TVWIKI"), ("B", "뉴토끼")):
            badge = ttk.Label(connection_status, text=f"● {title} · 확인 전", foreground="#586A7D",
                              background="#F1F5F9", padding=(10, 6), font=("맑은 고딕", 9, "bold"))
            badge.pack(side="left", padx=(8, 0))
            self.header_site_badges[site] = badge
        self._compact_header = None
        def layout_header(_):
            tab_font = tkfont.Font(root, font=("맑은 고딕", 12, "bold"))
            badge_font = tkfont.Font(root, font=("맑은 고딕", 9, "bold"))
            needed = sum(tab_font.measure(self.main_tabs.tab(tab, "text")) + 48 for tab in self.main_tabs.tabs())
            needed += sum(badge_font.measure(badge["text"]) + 28 for badge in self.header_site_badges.values())
            needed += tab_font.measure("프로그램 로그") + 78
            compact = self.main_tabs.winfo_width() < needed
            if compact != self._compact_header:
                self._compact_header = compact
                self.program_log_button.configure(text="로그" if compact else "프로그램 로그", width=0,
                                                  padding=(6 if compact else 10, 5))
                for badge in self.header_site_badges.values():
                    badge.configure(font=("맑은 고딕", 8 if compact else 9, "bold"), padding=(8 if compact else 10, 6))
        self.main_tabs.bind("<Configure>", layout_header)
        connection_status.bind("<Configure>", layout_header)
        self._build_domains()
        self.main_tabs.enable_traversal()
        self.main_tabs.bind("<<NotebookTabChanged>>", self._main_tab_changed)
        main = ttk.Frame(self.download_tab, padding=(18, 16))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        form = ttk.Frame(main, style="Card.TFrame", padding=14)
        form.grid(row=0, sticky="ew", pady=(0, 12))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="영상 주소", style="Card.TLabel").grid(row=0, column=0, padx=(0, 12))
        self.url_entry = WindowsSearchEntry(form, textvariable=self.url, command=self.add_job,
                                            right_click=self.paste_tvmon_url)
        self.url_entry.grid(row=0, column=1, sticky="ew")
        self.paste_button = ttk.Button(form, text="붙여넣기", command=self.paste)
        self.paste_button.grid(row=0, column=2, padx=8)
        self.add_button = ttk.Button(form, text="추가하고 시작", style="Primary.TButton", command=self.add_job)
        self.add_button.grid(row=0, column=3, sticky="ew")
        ttk.Label(form, text="저장 폴더", style="Card.TLabel").grid(row=1, column=0, pady=(8, 0), padx=(0, 12))
        self.folder_entry = WindowsSearchEntry(form, textvariable=self.folder)
        self.folder_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(10, 0))
        self.browse_button = ttk.Button(form, text="변경", command=self.browse)
        self.browse_button.grid(row=1, column=3, sticky="ew", pady=(10, 0))
        listing = ttk.Frame(main, style="Card.TFrame")
        listing.grid(row=1, sticky="nsew")
        listing.rowconfigure(2, weight=1)
        listing.columnconfigure(0, weight=1)
        list_bar = ttk.Frame(listing, padding=(0, 0, 0, 12))
        list_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        list_bar.columnconfigure(0, weight=1)
        filters = ttk.Frame(list_bar)
        filters.grid(row=0, column=0, sticky="w")
        self.filter_buttons = {}
        for name in ("전체", "진행 중", "대기", "완료", "일시정지", "오류"):
            button = ttk.Button(filters, text=name, style="Queue.Filter.TButton", width=0,
                                command=lambda name=name: self._select_filter(name))
            button.pack(side="left", padx=(0, 4))
            self.filter_buttons[name] = button
        list_tools = ttk.Frame(list_bar)
        list_tools.grid(row=0, column=1, sticky="e")
        self.list_search = WindowsSearchEntry(list_tools, textvariable=self.list_query)
        self.list_search.configure(width=round(root.winfo_fpixels("140p")))
        self.list_search.pack(side="left", padx=(8, 12))
        self.auto_retry_check = ttk.Checkbutton(list_tools, text="자동 재시도", style="Retry.TCheckbutton",
                                               variable=self.auto_retry, command=self._save_auto_retry)
        self.auto_retry_check.pack(side="left")
        def layout_list_tools(event):
            wrapped = filters.winfo_reqwidth() + list_tools.winfo_reqwidth() > event.width
            list_tools.grid_configure(row=1 if wrapped else 0, column=0 if wrapped else 1,
                                      columnspan=2 if wrapped else 1, pady=(8, 0) if wrapped else 0)
        list_bar.bind("<Configure>", layout_list_tools)
        # Native EDIT cue banners preserve normal Korean IME and clipboard behavior.
        for entry, hint in ((self.url_entry, "영상 주소를 입력하세요"), (self.list_search, "목록 검색")):
            cue = entry._ctypes.create_unicode_buffer(hint)
            entry._user.SendMessageW(entry._hwnd, 0x1501, 1, entry._ctypes.addressof(cue))
        self.list_query.trace_add("write", lambda *_: self._filter_changed())
        self.verification_notice = tk.Frame(listing, bg="#FFF1F0", padx=12, pady=10)
        self.verification_notice.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.verification_notice.columnconfigure(0, weight=1)
        self.verification_notice_title = tk.Label(self.verification_notice, bg="#FFF1F0", fg="#A12622",
                                                font=("맑은 고딕", 11, "bold"), anchor="w")
        self.verification_notice_title.grid(row=0, column=0, sticky="ew")
        notice_detail = tk.Label(self.verification_notice, bg="#FFF1F0", fg="#8B3733", font=("맑은 고딕", 10),
                                text="인증 대기 작업은 자동 재시도하지 않습니다. 브라우저에서 인증을 완료해 주세요.",
                                anchor="w", justify="left", wraplength=620)
        notice_detail.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.verification_notice_button = ttk.Button(self.verification_notice, text="브라우저 인증 열기",
                                                    style="Primary.TButton", command=self.verify_text_job)
        self.verification_notice_button.grid(row=0, column=1, rowspan=2, padx=(16, 0))
        self.verification_notice.bind("<Configure>", lambda event: notice_detail.configure(
            wraplength=max(180, event.width - self.verification_notice_button.winfo_reqwidth() - 44)))
        self.verification_notice.grid_remove()
        self.tree = ttk.Treeview(listing, style="Queue.Treeview", columns=("title", "status", "progress", "size", "speed", "eta"), show="headings", height=12, selectmode="extended")
        heading_font = tkfont.Font(root, font=style.lookup("Queue.Treeview.Heading", "font"))
        row_font = tkfont.Font(root, font=style.lookup("Queue.Treeview", "font"))
        eta_width = max(104, heading_font.measure("남은 시간") + 24, row_font.measure("999:59:59") + 24)
        for name, title, width, anchor in (("title", "파일 / 제목", 220, "w"), ("status", "상태", 112, "w"),
                                           ("progress", "진행률", 75, "e"), ("size", "저장 용량", 100, "e"),
                                           ("speed", "수신 속도", 95, "e"), ("eta", "남은 시간", eta_width, "center")):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=width if name != "title" else 220, anchor=anchor, stretch=name == "title")
        self.tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=3, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=lambda first, last: _set_scrollbar(scroll, first, last),
                            xscrollcommand=lambda first, last: _set_scrollbar(horizontal, first, last))
        self.tree.tag_configure("completed", foreground="#23704E")
        self.tree.tag_configure("error", foreground="#A84320")
        self.tree.tag_configure("verification", foreground="#A12622", background="#FFF1F0")
        self.tree.tag_configure("paused", foreground="#66758A")
        self.tree.bind("<<TreeviewSelect>>", self._queue_selection_changed)
        self.tree.bind("<Button-3>", self._show_job_menu)
        self.tree.bind("<Shift-F10>", self._show_job_menu)
        self.tree.bind("<App>", self._show_job_menu)
        self.tree.bind("<ButtonPress-1>", self._start_drag_selection)
        self.tree.bind("<B1-Motion>", self._drag_selection)
        self.tree.bind("<ButtonRelease-1>", self._end_drag_selection)
        atlas = tk.PhotoImage(master=root, file=str(Path(__file__).resolve().parent / "assets" / "download-menu.png"))
        self.menu_icons = {}
        for index, name in enumerate(("play", "pause", "details", "folder", "link", "delete", "more")):
            icon = tk.PhotoImage(master=root, width=64, height=64)
            icon.tk.call(icon, "copy", atlas, "-from", index * 64, 0, (index + 1) * 64, 64)
            icon = icon.subsample(max(1, round(64 / root.winfo_fpixels("9p"))))
            if name != "more":
                padded = tk.PhotoImage(master=root, width=icon.width() + 4, height=icon.height() + 4)
                padded.tk.call(padded, "copy", icon, "-to", 2, 2)
                icon = padded
            self.menu_icons[name] = icon
        self.job_menu = tk.Menu(root, tearoff=False, font=style.lookup("Queue.Treeview", "font"), background="#FFFFFF",
                                foreground="#20364D", activebackground="#E8EEF6", activeforeground="#20364D",
                                disabledforeground="#98A3B2", relief="solid", borderwidth=1,
                                postcommand=self._update_actions)
        self.job_menu.add_command(label="재생", image=self.menu_icons["play"], compound="left", command=self.open_file)
        self.job_menu.add_separator()
        self.job_menu.add_command(label="시작 / 이어받기", image=self.menu_icons["play"], compound="left", command=self.start)
        self.job_menu.add_command(label="일시정지", image=self.menu_icons["pause"], compound="left", command=self.pause)
        self.job_menu.add_separator()
        self.job_menu.add_command(label="상세 진행 보기", image=self.menu_icons["details"], compound="left", command=self.show_details)
        self.job_menu.add_command(label="저장 폴더 열기", image=self.menu_icons["folder"], compound="left", command=self.open_folder)
        self.job_menu.add_command(label="영상 주소 복사", image=self.menu_icons["link"], compound="left", command=self.copy_job_urls)
        self.job_menu.add_separator()
        self.job_menu.add_command(label="목록에서 제거", image=self.menu_icons["delete"], compound="left", foreground="#C73535", command=self.remove_jobs)
        self.empty_label = ttk.Label(self.tree, text="영상 주소를 추가하세요. 여러 링크는 차례로 다운로드합니다.", style="Muted.TLabel")
        summary = ttk.Frame(main, style="Card.TFrame", padding=(12, 10))
        summary.grid(row=2, sticky="ew", pady=(12, 0))
        self.detail_button = ttk.Button(summary, text="상세 보기", command=self.show_details)
        self.detail_button.pack(side="right", padx=(18, 0))
        self.queue_title = ttk.Label(summary, style="Card.TLabel", font=("맑은 고딕", 12, "bold"))
        self.queue_title.pack(side="left")
        self.queue_state = ttk.Label(summary, textvariable=self.selection_status, style="Card.TLabel", font=("맑은 고딕", 11, "bold"))
        self.queue_state.pack(side="left", padx=20)
        self.queue_size = ttk.Label(summary, style="Card.TLabel", font=("맑은 고딕", 12, "bold"))
        self.queue_size.pack(side="left")
        summary.bind("<Configure>", lambda _: self._fit_queue_title())
        self.detail_window = tk.Toplevel(root)
        self.detail_window.withdraw()
        self.detail_window.title("선택한 작업 · 상세 진행")
        self.detail_window.geometry(f"{min(1540, root.winfo_screenwidth() - 80)}x{min(660, root.winfo_screenheight() - 80)}")
        self.detail_window.minsize(1000, 620)
        self.detail_window.protocol("WM_DELETE_WINDOW", self.detail_window.withdraw)
        detail_head = ttk.Frame(self.detail_window, style="Card.TFrame", padding=(16, 10))
        detail_head.pack(fill="x")
        detail_head.columnconfigure(0, weight=1)
        ttk.Label(detail_head, textvariable=self.video_title, style="Card.TLabel", width=1,
                  font=("맑은 고딕", 14, "bold")).grid(row=0, column=0, sticky="ew")
        ttk.Label(detail_head, textvariable=self.progress_meta, style="Detail.Meta.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(detail_head, textvariable=self.percent, style="Card.TLabel", foreground="#245EB8",
                  font=("맑은 고딕", 20, "bold")).grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        notebook = ttk.Notebook(self.detail_window, style="Detail.TNotebook")
        notebook.pack(fill="both", expand=True)
        notebook.enable_traversal()
        self.detail_tabs = notebook
        detail = ttk.Frame(notebook, style="Card.TFrame", padding=(12, 10))
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(2, weight=1)
        notebook.add(detail, text="진행 상태")
        log_frame = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        log_frame.rowconfigure(2, weight=1)
        log_frame.columnconfigure(0, weight=1)
        ttk.Label(log_frame, text="작업 기록", style="Detail.Section.TLabel").grid(row=0, column=0, sticky="w")
        log_meta = ttk.Label(log_frame, textvariable=self.video_meta, style="Detail.Meta.TLabel", width=1, wraplength=940)
        log_meta.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        log_meta.bind("<Configure>", lambda event: log_meta.configure(wraplength=max(40, event.width)))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", font=("맑은 고딕", 10), bg="#FFFFFF", fg="#354C64",
                                relief="flat", state="disabled", padx=8, pady=8, spacing1=3, spacing3=5)
        self.log_text.grid(row=2, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.grid(row=2, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lambda first, last: _set_scrollbar(log_scroll, first, last))
        log_tools = ttk.Frame(log_frame, style="Card.TFrame", padding=(10, 7))
        log_tools.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(log_tools, text="실행별 상세 전송 기록은 로그 폴더에서 확인할 수 있습니다.", style="Detail.Meta.TLabel").pack(side="left")
        ttk.Button(log_tools, text="상세 로그 폴더 열기", command=self.open_transfer_logs).pack(side="right")
        notebook.add(log_frame, text="작업 기록")
        self.server_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        self.server_tab.rowconfigure(2, weight=1)
        self.server_tab.columnconfigure(0, weight=1)
        server_controls = ttk.Frame(self.server_tab, style="Card.TFrame")
        server_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(server_controls, text="영상 서버 선택", style="Detail.Section.TLabel").pack(side="left")
        self.fastest_server_check = ttk.Checkbutton(server_controls, text="가장 빠른 서버에서만 받기", style="Card.Retry.TCheckbutton",
            variable=self.fastest_server_only, command=self._save_fastest_server_only)
        self.fastest_server_check.pack(side="right")
        ttk.Label(self.server_tab, textvariable=self.server_status, style="Detail.Meta.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 12))
        self.server_tree = ttk.Treeview(self.server_tab, columns=("host", "parts", "state", "speed", "active"), show="headings", selectmode="browse")
        self.server_tree.heading("host", text="영상 서버 주소")
        self.server_tree.heading("parts", text="조각 수")
        self.server_tree.column("host", width=360, minwidth=260)
        self.server_tree.column("parts", width=100, minwidth=100, stretch=False, anchor="e")
        for name, title, width in (("state", "서버 상태", 230), ("speed", "최근 평균 속도", 150), ("active", "요청 중", 90)):
            self.server_tree.heading(name, text=title)
            self.server_tree.column(name, width=width, minwidth=width, stretch=False, anchor="w" if name == "state" else "e")
        self.server_tree.tag_configure("slow", foreground="#8B4600", background="#FFF3DA")
        self.server_tree.grid(row=2, column=0, sticky="nsew")
        server_scroll = ttk.Scrollbar(self.server_tab, orient="vertical", command=self.server_tree.yview)
        server_scroll.grid(row=2, column=1, sticky="ns")
        server_horizontal = ttk.Scrollbar(self.server_tab, orient="horizontal", command=self.server_tree.xview)
        server_horizontal.grid(row=3, column=0, sticky="ew")
        self.server_tree.configure(yscrollcommand=lambda first, last: _set_scrollbar(server_scroll, first, last),
                                   xscrollcommand=lambda first, last: _set_scrollbar(server_horizontal, first, last))
        self.server_empty = ttk.Label(self.server_tree, text="영상 정보를 읽으면 서버 목록이 표시됩니다.", style="Muted.TLabel")
        server_help = ttk.Label(self.server_tab, text="느린 서버는 잠시 회피한 뒤 다시 확인합니다. 상태와 속도는 이번 실행 기준입니다.\n"
                                "조각 수는 원래 배정 수이며, 대체 요청으로 다른 서버에서 받을 수 있습니다.",
                                style="Detail.Meta.TLabel", width=1, wraplength=940)
        server_help.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        server_help.bind("<Configure>", lambda event: server_help.configure(wraplength=max(40, event.width)))
        notebook.add(self.server_tab, text="영상 서버")
        self.transfer_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        self.transfer_tab.columnconfigure(0, weight=1)
        self.transfer_tab.rowconfigure(4, weight=1)
        notebook.insert(1, self.transfer_tab, text="HTTP · P2P 상세")
        transfer_heading = ttk.Frame(self.transfer_tab, style="Card.TFrame")
        transfer_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(transfer_heading, text="수신 방식별 진행", style="Detail.Section.TLabel").pack(side="left")
        ttk.Button(transfer_heading, text="피어 · 트래커 확인", style="Quiet.TButton",
                   command=lambda: self.show_details(self.peer_connections_tab)).pack(side="right")
        self.source_tree = ttk.Treeview(self.transfer_tab, style="Worker.Treeview",
            columns=("method", "received", "speed", "saved", "active"), show="headings", height=3, selectmode="none")
        for name, title, width in (("method", "방식", 125), ("received", "이번 실행 수신량", 155),
                                  ("speed", "현재 수신 속도", 140), ("saved", "저장 완료 조각", 135), ("active", "현재 상태", 320)):
            self.source_tree.heading(name, text=title)
            self.source_tree.column(name, width=width, minwidth=width, stretch=name == "active",
                                    anchor="w" if name in ("method", "active") else "e")
        for source in ("http", "p2p", "unknown"):
            self.source_tree.insert("", "end", iid=source)
        self.source_tree.grid(row=1, column=0, sticky="ew")
        transfer_help = ttk.Label(self.transfer_tab, textvariable=self.part_summary,
                                 style="Detail.Meta.TLabel", width=1, wraplength=940)
        transfer_help.grid(row=2, column=0, sticky="ew", pady=(8, 18))
        transfer_help.bind("<Configure>", lambda event: transfer_help.configure(wraplength=max(40, event.width)))
        ttk.Label(self.transfer_tab, textvariable=self.peer_summary, style="Card.TLabel",
                  font=("맑은 고딕", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 8))
        peer_frame = ttk.Frame(self.transfer_tab, style="Card.TFrame")
        peer_frame.grid(row=4, column=0, sticky="nsew")
        peer_frame.rowconfigure(0, weight=1)
        peer_frame.columnconfigure(0, weight=1)
        self.peer_tree = ttk.Treeview(peer_frame, style="Worker.Treeview", columns=("part", "state", "bytes"),
                                     show="headings", height=4, selectmode="browse")
        for name, title, width in (("part", "조각 번호", 125), ("state", "P2P 진행 상태", 360), ("bytes", "전달된 데이터", 180)):
            self.peer_tree.heading(name, text=title)
            self.peer_tree.column(name, width=width, minwidth=width, stretch=name == "state", anchor="w")
        self.peer_tree.grid(row=0, column=0, sticky="nsew")
        peer_scroll = ttk.Scrollbar(peer_frame, command=self.peer_tree.yview)
        peer_scroll.grid(row=0, column=1, sticky="ns")
        self.peer_tree.configure(yscrollcommand=lambda first, last: _set_scrollbar(peer_scroll, first, last))
        self.peer_empty = ttk.Label(self.peer_tree, text="현재 P2P로 미리 받고 있는 조각이 없습니다.", style="Muted.TLabel")
        peer_help = ttk.Label(self.transfer_tab, text="수신량·속도는 이번 실행 기준이며 중복·재시도를 포함합니다. 저장 완료에는 이어받은 조각도 포함됩니다.\n"
                             "P2P는 최대 5개 조각을 미리 받습니다. ‘전달된 데이터’는 처리 작업으로 넘어온 양이며 조각별 속도는 제공되지 않습니다.",
                             style="Detail.Meta.TLabel", width=1, wraplength=940)
        peer_help.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        peer_help.bind("<Configure>", lambda event: peer_help.configure(wraplength=max(40, event.width)))
        self.peer_connections_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        self.peer_connections_tab.rowconfigure(3, weight=1)
        self.peer_connections_tab.columnconfigure(0, weight=1)
        notebook.add(self.peer_connections_tab, text="연결된 피어")
        tracker_status = ttk.Label(self.peer_connections_tab, textvariable=self.p2p_connection_status,
                                  style="Detail.Section.TLabel", width=1, wraplength=940)
        tracker_status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tracker_status.bind("<Configure>", lambda event: tracker_status.configure(wraplength=max(40, event.width)))
        self.tracker_tree = ttk.Treeview(self.peer_connections_tab, style="Worker.Treeview",
            columns=("host", "state", "peers", "detail"), show="headings", height=3, selectmode="none")
        worker_heading_font = tkfont.Font(root, font=style.lookup("Worker.Treeview.Heading", "font"))
        worker_font = tkfont.Font(root, font=style.lookup("Worker.Treeview", "font"))
        for name, title, width in (("host", "P2P 트래커", 260), ("state", "연결 상태", 150),
                                  ("peers", "다른 참여자", 115), ("detail", "확인 내용", 350)):
            width = max(width, worker_heading_font.measure(title) + 18,
                        worker_font.measure("오류 · 재시도 예정") + 18 if name == "state" else 0)
            self.tracker_tree.heading(name, text=title)
            self.tracker_tree.column(name, width=width, minwidth=width, stretch=name == "detail", anchor="w")
        self.tracker_tree.tag_configure("error", foreground="#8B4600", background="#FFF3DA")
        self.tracker_tree.grid(row=1, column=0, sticky="ew")
        self.peer_connections_heading = ttk.Label(self.peer_connections_tab, text="연결된 피어 · 0개",
            style="Card.TLabel", font=("맑은 고딕", 11, "bold"))
        self.peer_connections_heading.grid(row=2, column=0, sticky="w", pady=(18, 8))
        self.peer_connections_tree = ttk.Treeview(self.peer_connections_tab, style="Worker.Treeview",
            columns=("peer", "address", "country"), show="headings", height=14, selectmode="browse")
        for name, title, width in (("peer", "피어", 90), ("address", "IP 주소", 450), ("country", "국가 (IP 기준 추정)", 300)):
            self.peer_connections_tree.heading(name, text=title)
            self.peer_connections_tree.column(name, width=width, minwidth=width,
                                               stretch=name == "address", anchor="w")
        self.peer_connections_tree.grid(row=3, column=0, sticky="nsew")
        peer_connections_scroll = ttk.Scrollbar(self.peer_connections_tab, command=self.peer_connections_tree.yview)
        peer_connections_scroll.grid(row=3, column=1, sticky="ns")
        self.peer_connections_tree.configure(yscrollcommand=lambda first, last: _set_scrollbar(peer_connections_scroll, first, last))
        self.peer_connections_empty = ttk.Label(self.peer_connections_tree,
            text="다운로드 중인 영상을 선택하면 연결된 피어를 표시합니다.", style="Muted.TLabel")
        connection_help = ttk.Label(self.peer_connections_tab,
            text="국가 조회 시 공개 IP를 country.is에 전송합니다. 주소가 공개되지 않으면 ‘미공개’로 표시합니다.\n"
                 "국가는 접속 IP 기준 추정값이며, VPN·중계 연결은 해당 서버의 국가로 표시될 수 있습니다.",
            style="Detail.Meta.TLabel", width=1, wraplength=940)
        connection_help.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        connection_help.bind("<Configure>", lambda event: connection_help.configure(wraplength=max(40, event.width)))
        self.detail_progress = ttk.Progressbar(detail, maximum=100)
        self.detail_progress.grid(row=0, sticky="ew", pady=(0, 10))
        stats = ttk.Frame(detail, style="Metric.TFrame", padding=(4, 8))
        stats.grid(row=1, sticky="ew", pady=(0, 12))
        self.detail_metrics = []
        for column, (label, variable) in enumerate((("저장한 용량", self.transferred), ("수신 속도", self.speed), ("남은 시간", self.remaining))):
            stats.columnconfigure(column, weight=1, uniform="metric")
            metric = ttk.Frame(stats, style="Metric.TFrame", padding=(12, 0))
            metric.grid(row=0, column=column, sticky="ew")
            ttk.Label(metric, text=label, style="Metric.Caption.TLabel").pack(anchor="w")
            value = ttk.Label(metric, textvariable=variable, style="Metric.Value.TLabel")
            value.pack(anchor="w", pady=(2, 0))
            self.detail_metrics.append(value)
        stats.columnconfigure(3, weight=1, uniform="metric")
        peer_metric = ttk.Frame(stats, style="Metric.TFrame", padding=(12, 0))
        peer_metric.grid(row=0, column=3, sticky="nsew")
        ttk.Label(peer_metric, text="P2P 상태", style="Metric.Caption.TLabel").pack(anchor="w")
        self.p2p_overview_label = ttk.Label(peer_metric, textvariable=self.p2p_overview, style="Metric.Status.TLabel", width=1, wraplength=240)
        self.p2p_overview_label.pack(fill="x", pady=(4, 0))
        self.p2p_overview_label.bind("<Configure>", lambda event: self.p2p_overview_label.configure(wraplength=max(40, event.width)))
        lower = ttk.Panedwindow(detail, orient="horizontal")
        lower.grid(row=2, sticky="nsew")
        lane_frame = ttk.Frame(lower, style="Card.TFrame")
        ttk.Label(lane_frame, text="조각 처리 · 최대 8개", style="Detail.Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.worker_tree = ttk.Treeview(lane_frame, style="Worker.Treeview", columns=("slot", "part", "source", "state", "host", "bytes", "speed"), show="headings", height=8, selectmode="browse")
        for name, title, width in (("slot", "작업", 42), ("part", "조각", 54), ("source", "방식", 78), ("state", "상태", 92), ("host", "서버 / 수신처", 200), ("bytes", "수신량", 86), ("speed", "HTTP 속도", 95)):
            width = max(width, worker_heading_font.measure(title) + 18)
            self.worker_tree.heading(name, text=title)
            self.worker_tree.column(name, width=width, minwidth=width, stretch=False, anchor="w" if name in ("slot", "source", "state", "host") else "e")
        self.worker_tree.pack(fill="both", expand=True)
        self.worker_tree.tag_configure("slow", foreground="#8B4600", background="#FFF3DA")
        for index in range(WORKERS):
            self.worker_tree.insert("", "end", iid=str(index), values=(f"{index + 1}", "—", "—", "대기", "—", "—", "—"))
        self.worker_tree.bind("<<TreeviewSelect>>", lambda _: self._refresh_worker_host())
        worker_detail = ttk.Label(lane_frame, textvariable=self.worker_host, style="Detail.Meta.TLabel", width=1, wraplength=630)
        worker_detail.pack(fill="x", pady=(8, 0))
        worker_detail.bind("<Configure>", lambda event: worker_detail.configure(wraplength=max(40, event.width)))
        lower.add(lane_frame, weight=0)
        map_frame = ttk.Frame(lower, style="Card.TFrame", padding=(12, 0, 0, 0))
        server_heading = ttk.Frame(map_frame, style="Card.TFrame")
        server_heading.pack(fill="x", pady=(0, 6))
        self.server_count_label = ttk.Label(server_heading, textvariable=self.server_summary, style="Card.TLabel",
                                            font=("맑은 고딕", 11, "bold"), width=1, wraplength=390)
        self.server_count_label.pack(side="left", fill="x", expand=True)
        self.server_count_label.bind("<Configure>", lambda event: self.server_count_label.configure(wraplength=max(40, event.width)))
        self.server_info_button = ttk.Button(server_heading, text="서버 정보", padding=(8, 2),
                                             command=lambda: self.show_details(self.server_tab))
        self.server_info_button.pack(side="right")
        legend = ttk.Frame(map_frame, style="Card.TFrame")
        legend.pack(fill="x", pady=(0, 5))
        for position, (key, label) in enumerate((("waiting", "대기"), ("http_active", "서버 수신 중"),
                ("http_complete", "서버 저장 완료"), ("error", "오류"),
                ("p2p_active", "P2P 수신/검증"), ("p2p_complete", "P2P 저장 완료"), ("unknown_complete", "저장 완료 · 방식 미기록"))):
            item = ttk.Frame(legend, style="Card.TFrame")
            item.grid(row=position // 2, column=position % 2, columnspan=2 if key == "unknown_complete" else 1, sticky="w", padx=(0, 9))
            tk.Label(item, text="■", fg=PART_COLORS[key], bg="#FFFFFF", font=("Segoe UI", 10)).pack(side="left")
            ttk.Label(item, text=label, style="Detail.Meta.TLabel").pack(side="left", padx=(2, 0))
        self.map_canvas = tk.Canvas(map_frame, bg="#F8FAFD", highlightthickness=0, height=150)
        self.map_canvas.bind("<Configure>", lambda _: self._draw_map(force=True))
        part_summary_label = ttk.Label(map_frame, textvariable=self.map_summary, style="Detail.Meta.TLabel", width=1, wraplength=430)
        part_summary_label.pack(side="bottom", fill="x", pady=(5, 0))
        part_summary_label.bind("<Configure>", lambda event: part_summary_label.configure(wraplength=max(40, event.width)))
        self.map_canvas.pack(fill="both", expand=True)
        lower.add(map_frame, weight=3)
        file_row = ttk.Frame(detail, style="Card.TFrame")
        file_row.grid(row=3, sticky="ew", pady=(12, 0))
        file_row.columnconfigure(1, weight=1)
        ttk.Label(file_row, text="저장 위치", style="Detail.Meta.TLabel").grid(row=0, column=0, padx=(0, 10))
        self.detail_path = ttk.Entry(file_row, textvariable=self.file_name, state="readonly", style="Path.TEntry")
        self.detail_path.grid(row=0, column=1, sticky="ew")
        ttk.Button(file_row, text="폴더 열기", command=self.open_folder, style="Quiet.TButton").grid(row=0, column=2, padx=(8, 0))
        self.status_label = ttk.Label(main, textvariable=self.status, font=("맑은 고딕", 9), wraplength=1120)
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(200, event.width)))
        self.status.trace_add("write", self._show_status_notice)
        self.url_entry.focus_set()

    def show_details(self, tab=None):
        self.detail_window.attributes("-alpha", self.root.attributes("-alpha") if self.root.winfo_ismapped() else 0.0)
        self.detail_window.attributes("-disabled", self.root.attributes("-disabled"))
        self.detail_window.deiconify()
        if self.root.winfo_ismapped():
            self.detail_window.transient(self.root)
        self.detail_tabs.select(0 if tab is None else tab)
        self._refresh_details(force=True)

    def _select_filter(self, name):
        self.filter.set(name)
        self._filter_changed()

    def _job(self, job_id):
        return next((job for job in self.jobs if job["id"] == job_id), None)

    def _selected_job(self):
        selected = self.tree.selection()
        focused = self.tree.focus()
        return self._job(focused if focused in selected else selected[0]) if selected else None

    def _queue_selection_changed(self, _=None):
        self.status.set("")
        self._refresh_details(force=True)

    def _show_job_menu(self, event=None):
        if self.closing or self._job_menu_open:
            return "break"
        if event is not None and getattr(event, "num", None) == 3:
            row = self.tree.identify_row(event.y)
            if not row:
                return "break"
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.tree.focus(row)
            x, y = event.x_root, event.y_root
        else:
            job = self._selected_job()
            if not job:
                return "break"
            self.tree.see(job["id"])
            box = self.tree.bbox(job["id"], "title")
            if not box:
                return "break"
            x = self.tree.winfo_rootx() + min(box[0], self.tree.winfo_width() - 30)
            y = self.tree.winfo_rooty() + box[1] + box[3]
        self._drag_selection_anchor = None
        self.tree.focus_set()
        self._refresh_details(force=True)
        self._job_menu_open = True
        try:
            self.job_menu.tk_popup(x, y)
        finally:
            self._job_menu_open = False
            if not self._destroyed:
                self.job_menu.grab_release()
                self._update_actions()
        return "break"

    def copy_job_urls(self):
        selected = set(self.tree.selection())
        urls = []
        for job in self.jobs:
            if job["id"] not in selected:
                continue
            if job.get("kind") in ("text", "text_zip"):
                url = self.text_domain + urlsplit(job["url"]).path if self.text_domain else job["url"]
            else:
                url = domain_requests(job["url"], job["url"])[0][0]
            urls.append(url)
        if not urls or self.closing:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(urls))
        self.status.set(f"주소 {len(urls)}개를 복사했습니다.")

    def _fit_queue_title(self):
        summary = self.queue_title.master
        if summary.winfo_width() <= 1:
            return
        available = max(40, summary.winfo_width() - self.detail_button.winfo_reqwidth()
                        - self.queue_state.winfo_reqwidth() - self.queue_size.winfo_reqwidth() - 88)
        title = self.video_title.get()
        signature = (available, title, self.queue_title["font"])
        if signature == self._queue_title_signature:
            return
        font = tkfont.Font(self.root, font=signature[2])
        if font.measure(title) > available:
            while title and font.measure(title + "…") > available:
                title = title[:-1]
            title += "…"
        self.queue_title.configure(text=title)
        self._queue_title_signature = signature

    def _show_status_notice(self, *_):
        if self.status.get():
            self.status_label.grid(row=3, sticky="ew", pady=(8, 0))
        else:
            self.status_label.grid_remove()

    def _select_all(self, _=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _start_drag_selection(self, event):
        self._drag_selection_anchor = None
        if self.tree.identify_region(event.x, event.y) == "cell" and not event.state & 0x0005:
            self._drag_selection_anchor = self.tree.identify_row(event.y)

    def _drag_selection(self, event):
        if not self._drag_selection_anchor:
            return
        rows = self.tree.get_children()
        if self._drag_selection_anchor not in rows:
            self._drag_selection_anchor = None
            return
        row = self.tree.identify_row(event.y)
        if row in rows:
            first, last = sorted((rows.index(self._drag_selection_anchor), rows.index(row)))
            selection = rows[first:last + 1]
            if self.tree.selection() != selection:
                self.tree.selection_set(selection)
        return "break"

    def _end_drag_selection(self, _=None):
        self._drag_selection_anchor = None

    def _toggle_selected(self, _=None):
        job = self._selected_job()
        if job and job["id"] == self.active_id:
            self.pause()
        else:
            self.start()
        return "break"

    def _log(self, job, text):
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {text}")
        job["message"] = text
        self._write_program_log("job_message", job=job["id"], status=job["status"], detail=text,
                                level=logging.ERROR if job["status"] == "error" else logging.INFO)

    def paste(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            self.status.set("클립보드에 영상 주소가 없습니다.")

    def paste_tvmon_url(self, _event=None):
        try:
            page, *_ = parse_page_url(self.root.clipboard_get())
        except (tk.TclError, DownloadError):
            self.status.set("클립보드에 유효한 TVMON 회차 주소가 없습니다.")
            return "break"
        self.url.set(page)
        self.url_entry.focus_set()
        self.url_entry.icursor(tk.END)
        self.status.set("영상 주소를 입력했습니다. ‘추가하고 시작’을 누르세요.")
        return "break"

    def browse(self):
        self.folder_entry._sync_variable()
        folder = filedialog.askdirectory(parent=self.root, title="새 작업의 저장 폴더", initialdir=self.folder.get())
        if folder:
            self.folder.set(folder)
            self._dirty = True
            self._save_jobs(force=True)

    def add_job(self):
        if self.closing:
            return
        self.url_entry._sync_variable()
        self.folder_entry._sync_variable()
        try:
            page, *_ = parse_page_url(self.url.get())
            if not self.folder.get().strip():
                raise DownloadError("저장 폴더를 선택해 주세요.")
            folder = Path(self.folder.get().strip()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
        except (DownloadError, OSError) as error:
            self.status.set(str(error))
            return
        duplicate = next((job for job in self.jobs if job["url"] == page and job["folder"] == str(folder) and job["status"] != "completed"), None)
        if duplicate:
            self.filter.set("전체")
            self._refresh_table()
            self.tree.selection_set(duplicate["id"])
            self.tree.focus(duplicate["id"])
            self.status.set("이미 목록에 있는 영상입니다. ‘시작 / 이어받기’로 재개할 수 있습니다.")
            self._refresh_details(force=True)
            return
        job = self._new_job(page, str(folder), uuid.uuid4().hex)
        self.jobs.append(job)
        self._log(job, "대기열에 추가했습니다.")
        self._dirty = True
        persisted = self._save_jobs(force=True)
        if not persisted:
            job["status"] = "error"
            self._log(job, "작업 목록을 저장하지 못해 다운로드를 시작하지 않았습니다.")
        self.filter.set("전체")
        self._refresh_table()
        self.tree.selection_set(job["id"])
        self.tree.focus(job["id"])
        if persisted:
            self.url.set("")
            self.queue_running = True
            self._start_next()
        self._refresh_details(force=True)

    def _build_domains(self):
        shell = ttk.Frame(self.domains_tab)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        navigation = ttk.Frame(shell, width=195, padding=(12, 22))
        navigation.grid(row=0, column=0, sticky="ns")
        navigation.pack_propagate(False)
        ttk.Label(navigation, text="사이트 연결", font=("맑은 고딕", 13, "bold")).pack(anchor="w", padx=8, pady=(0, 20))
        self.domain_sites = ttk.Notebook(shell, style="Sites.TNotebook")
        self.domain_sites.grid(row=0, column=1, sticky="nsew")
        self.video_domains_tab = ttk.Frame(self.domain_sites, style="Card.TFrame")
        self.text_domains_tab = ttk.Frame(self.domain_sites, style="Card.TFrame")
        self.domain_sites.add(self.video_domains_tab, text="A · 영상")
        self.domain_sites.add(self.text_domains_tab, text="B · 텍스트")
        self.video_domain_value = tk.StringVar(value=self.domains[0])
        self.text_domain_value.set(self.text_domain or "저장된 주소가 없습니다.")
        self.site_buttons, self.site_badges, self.site_badge_labels = {}, {}, {}
        self.site_cards, self.site_nav_badges = {}, {}
        self.site_messages, self.site_checked, self.site_steps = {}, {}, {}
        self.domain_sections, self.domain_section_buttons = {}, {}
        self.domain_canvases = {}
        for site, page, title, address, automatic, refresh, save in (
            ("A", self.video_domains_tab, "TVWIKI", self.video_domain_value, self.auto_domain, self.refresh_current_domain, self._save_auto_domain),
            ("B", self.text_domains_tab, "뉴토끼", self.text_domain_value, self.auto_text_domain, self.refresh_text_domain, self._save_auto_text_domain),
        ):
            card = tk.Frame(navigation, bg="#FFFFFF", highlightthickness=2, highlightbackground="#DEE5ED")
            card.pack(fill="x", pady=(0, 12))
            self.site_cards[site] = card
            button = ttk.Button(card, text=title, style="Nav.TButton", width=0, command=lambda page=page: self.domain_sites.select(page))
            button.pack(fill="x", padx=2, pady=(2, 0))
            self.site_buttons[site] = button
            status_badge = ttk.Label(card, text="● 확인 전", background="#F1F5F9", foreground="#586A7D",
                                     font=("맑은 고딕", 10, "bold"), padding=(8, 5), cursor="hand2")
            status_badge.pack(fill="x", padx=10, pady=(0, 10))
            status_badge.bind("<Button-1>", lambda _, button=button: button.invoke())
            card.bind("<Button-1>", lambda _, button=button: button.invoke())
            self.site_nav_badges[site] = status_badge
            page.columnconfigure(0, weight=1)
            page.rowconfigure(0, weight=1)
            canvas = tk.Canvas(page, background="#FFFFFF", highlightthickness=0, yscrollincrement=24)
            canvas.grid(row=0, column=0, sticky="nsew")
            page_scroll = ttk.Scrollbar(page, command=canvas.yview)
            page_scroll.grid(row=0, column=1, sticky="ns")
            canvas.configure(yscrollcommand=lambda first, last, bar=page_scroll: _set_scrollbar(bar, first, last))
            self.domain_canvases[site] = canvas
            main = ttk.Frame(canvas, style="Card.TFrame", padding=(24, 22))
            content = canvas.create_window(0, 0, window=main, anchor="nw")
            canvas.bind("<Configure>", lambda event, view=canvas, item=content: view.itemconfigure(item, width=event.width))
            main.bind("<Configure>", lambda _, view=canvas: view.configure(scrollregion=view.bbox("all")))
            heading = ttk.Frame(main, style="Card.TFrame")
            heading.pack(fill="x", pady=(0, 14))
            ttk.Label(heading, text=title + " 연결", style="Card.TLabel", font=("맑은 고딕", 19, "bold")).pack(side="left")
            self.site_badges[site] = tk.StringVar(value="확인 전")
            badge = ttk.Label(heading, textvariable=self.site_badges[site], style="Card.TLabel", foreground="#586A7D",
                              background="#F1F5F9", font=("맑은 고딕", 10, "bold"), padding=(12, 6))
            badge.pack(side="left", padx=12)
            self.site_badge_labels[site] = badge
            self.site_messages[site] = tk.StringVar(value="접속 확인을 누르면 현재 주소가 사용 가능한지 확인합니다.")
            banner = ttk.Label(main, textvariable=self.site_messages[site], background="#EEF4FB", foreground="#284A70", padding=(14, 12), wraplength=640)
            banner.pack(fill="x", pady=(0, 18))
            main.bind("<Configure>", lambda event, label=banner: label.configure(wraplength=max(250, event.width - 80)), add="+")
            current = ttk.Frame(main, style="Card.TFrame")
            current.pack(fill="x", pady=(0, 16))
            current.columnconfigure(1, weight=1)
            ttk.Label(current, text="사용 중인 주소", style="Card.TLabel").grid(row=0, column=0, padx=(0, 16))
            ttk.Entry(current, textvariable=address, state="readonly", font=("Segoe UI", 11)).grid(row=0, column=1, sticky="ew")
            refresh_button = ttk.Button(current, text="접속 확인", style="Primary.TButton", command=refresh)
            refresh_button.grid(row=0, column=2, padx=(12, 0))
            automatic_row = ttk.Frame(main, style="Card.TFrame")
            automatic_row.pack(fill="x", pady=(0, 16))
            ttk.Label(automatic_row, text="자동으로 주소 확인", style="Card.TLabel", font=("맑은 고딕", 11, "bold")).pack(anchor="w")
            automatic_check = ttk.Checkbutton(automatic_row, text="프로그램을 시작할 때 최신 주소 확인", variable=automatic, command=save)
            automatic_check.pack(anchor="w", pady=(6, 0))
            ttk.Separator(main).pack(fill="x", pady=(0, 14))
            ttk.Label(main, text="접속 확인 단계", style="Card.TLabel", font=("맑은 고딕", 12, "bold")).pack(anchor="w", pady=(0, 8))
            steps = []
            for label in ("주소 찾기", "콘텐츠 홈페이지 확인"):
                row = ttk.Frame(main, style="Card.TFrame")
                row.pack(fill="x", pady=5)
                ttk.Label(row, text=label, style="Card.TLabel").pack(side="left")
                value = tk.StringVar(value="대기")
                ttk.Label(row, textvariable=value, style="Card.TLabel", foreground="#245EB8").pack(side="right")
                steps.append(value)
            self.site_steps[site] = steps
            self.site_checked[site] = tk.StringVar(value="아직 확인하지 않았습니다.")
            ttk.Label(main, textvariable=self.site_checked[site], style="Muted.TLabel").pack(anchor="w", pady=(8, 16))
            self.domain_sections[site], self.domain_section_buttons[site] = {}, {}
            sections = [("guide", "주소 안내 페이지 설정")]
            if site == "A":
                sections.append(("manage", "등록된 주소 관리"))
            sections += [("help", "접속 문제 해결"), ("history", "자세한 접속 기록")]
            for key, label in sections:
                ttk.Separator(main).pack(fill="x")
                holder = ttk.Frame(main, style="Card.TFrame")
                holder.pack(fill="x")
                control = ttk.Button(holder, text=label + " · 펼치기", style="Quiet.TButton",
                                     command=lambda site=site, key=key: self._toggle_domain_section(site, key))
                control.pack(anchor="w", pady=7)
                self.domain_section_buttons[site][key] = (control, label)
                self.domain_sections[site][key] = ttk.Frame(holder, style="Card.TFrame", padding=(0, 4, 0, 10))
            history = self.domain_sections[site]["history"]
            history.columnconfigure(0, weight=1)
            tree = ttk.Treeview(history, columns=("phase", "url", "result", "detail"), show="headings", selectmode="browse", height=3)
            for name, label, width in (("phase", "확인 단계", 105), ("url", "시도한 주소", 230), ("result", "결과", 80), ("detail", "상세 내용", 300)):
                tree.heading(name, text=label)
                tree.column(name, width=width, minwidth=width, stretch=name == "detail")
            tree.grid(row=0, column=0, sticky="ew")
            scroll = ttk.Scrollbar(history, command=tree.yview)
            scroll.grid(row=0, column=1, sticky="ns")
            horizontal = ttk.Scrollbar(history, orient="horizontal", command=tree.xview)
            horizontal.grid(row=1, column=0, sticky="ew")
            tree.configure(yscrollcommand=lambda first, last, bar=scroll: _set_scrollbar(bar, first, last),
                           xscrollcommand=lambda first, last, bar=horizontal: _set_scrollbar(bar, first, last))
            for state, color in (("checking", "#245EB8"), ("success", "#23704E"), ("error", "#A84320"), ("cancelled", "#66758A"), ("verification", "#7354A3")):
                tree.tag_configure(state, foreground=color)
            status = self.domain_status if site == "A" else self.text_domain_status
            status_label = ttk.Label(history, textvariable=status, style="Muted.TLabel", wraplength=620)
            status_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            history.bind("<Configure>", lambda event, label=status_label: label.configure(wraplength=max(250, event.width - 24)))
            help_panel = self.domain_sections[site]["help"]
            ttk.Label(help_panel, text="접속 확인을 다시 실행해 주세요. 사용할 수 있는 주소를 찾아 저장합니다.",
                      style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(0, 8))
            if site == "A":
                self.domain_refresh_button, self.domain_auto_check = refresh_button, automatic_check
                self.domain_check_tree, self.domain_status_label = tree, status_label
                ttk.Label(help_panel, text="주소 추가와 접속 순서 변경은 ‘등록된 주소 관리’에서 할 수 있습니다.",
                          style="Muted.TLabel", wraplength=580).pack(anchor="w")
            else:
                self.text_domain_refresh_button, self.text_domain_auto_check = refresh_button, automatic_check
                self.text_domain_check_tree, self.text_domain_status_label = tree, status_label
                ttk.Label(help_panel, text="인증이 필요한 경우 아래 버튼으로 브라우저 검증을 진행하세요.",
                          style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
                self.text_domain_browser_button = ttk.Button(help_panel, text="브라우저 검증 열기", state="disabled",
                                                             command=lambda: self.refresh_text_domain(browser_verification=True))
                self.text_domain_browser_button.pack(anchor="w")
        for site, guide_url, save in (("A", self.domain_guide_url, self._save_domain_guide),
                                      ("B", self.text_domain_guide_url, self._save_text_domain_guide)):
            guide_panel = self.domain_sections[site]["guide"]
            guide_help = ("등록된 주소에 접속하지 못하면 이 안내 페이지에서 최신 주소를 찾습니다." if site == "A"
                          else "최신 주소를 읽어 올 안내 페이지입니다. 저장 후 ‘접속 확인’을 눌러 확인하세요.")
            ttk.Label(guide_panel, text=guide_help,
                      style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(0, 8))
            guide_form = ttk.Frame(guide_panel, style="Card.TFrame")
            guide_form.pack(fill="x")
            ttk.Label(guide_form, text="안내 URL", style="Card.TLabel").pack(side="left", padx=(0, 10))
            guide_input = tk.StringVar(value=guide_url)
            guide_entry = WindowsSearchEntry(guide_form, textvariable=guide_input, command=save)
            guide_entry.pack(side="left", fill="x", expand=True)
            guide_save = ttk.Button(guide_form, text="저장", command=save)
            guide_save.pack(side="left", padx=(10, 0))
            guide_message = tk.StringVar(value="HTTPS 주소를 사용합니다. 기존 안내 페이지와 같은 주소 안내 형식을 지원합니다.")
            guide_label = ttk.Label(guide_panel, textvariable=guide_message, style="Muted.TLabel", wraplength=580)
            guide_label.pack(anchor="w", pady=(8, 0))
            if site == "A":
                self.domain_guide_input, self.domain_guide_entry, self.domain_guide_save_button = guide_input, guide_entry, guide_save
                self.domain_guide_message, self.domain_guide_message_label = guide_message, guide_label
            else:
                self.text_domain_guide_input, self.text_domain_guide_entry, self.text_domain_guide_save_button = guide_input, guide_entry, guide_save
                self.text_domain_guide_message, self.text_domain_guide_message_label = guide_message, guide_label
        management = self.domain_sections["A"]["manage"]
        form = ttk.Frame(management, style="Card.TFrame")
        form.pack(fill="x", pady=(0, 8))
        ttk.Label(form, text="도메인 주소", style="Card.TLabel").pack(side="left", padx=(0, 10))
        self.domain_entry = WindowsSearchEntry(form, textvariable=self.domain_input, command=self.add_domain)
        self.domain_entry.pack(side="left", fill="x", expand=True)
        self.domain_add_button = ttk.Button(form, text="추가", command=self.add_domain)
        self.domain_add_button.pack(side="left", padx=(10, 0))
        listing = ttk.Frame(management, style="Card.TFrame")
        listing.pack(fill="x")
        self.domain_tree = ttk.Treeview(listing, columns=("priority", "domain"), show="headings", selectmode="browse", height=3)
        self.domain_tree.heading("priority", text="접속 순서")
        self.domain_tree.column("priority", width=80, minwidth=80, stretch=False, anchor="center")
        self.domain_tree.heading("domain", text="도메인")
        self.domain_tree.column("domain", width=540, minwidth=250)
        listing.columnconfigure(0, weight=1)
        self.domain_tree.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(listing, command=self.domain_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.domain_tree.configure(yscrollcommand=lambda first, last: _set_scrollbar(scroll, first, last))
        self.domain_tree.bind("<<TreeviewSelect>>", lambda _: self._domain_actions())
        actions = ttk.Frame(management, style="Card.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        self.domain_up_button = ttk.Button(actions, text="위로", command=lambda: self.move_domain(-1))
        self.domain_up_button.pack(side="left")
        self.domain_down_button = ttk.Button(actions, text="아래로", command=lambda: self.move_domain(1))
        self.domain_down_button.pack(side="left", padx=8)
        self.domain_remove_button = ttk.Button(actions, text="선택 제거", command=self.remove_domain)
        self.domain_remove_button.pack(side="left")
        self.domain_sites.bind("<<NotebookTabChanged>>", self._sync_domain_navigation)
        self.root.bind("<MouseWheel>", self._scroll_domain_page, add="+")
        self.root.bind("<FocusIn>", lambda event: self.root.after_idle(self._reveal_domain_control, event.widget), add="+")
        self._sync_domain_navigation()
        self._refresh_domains()

    def _scroll_domain_page(self, event):
        if self.main_tabs.select() != str(self.domains_tab) or event.state & 0x0001:
            return
        if isinstance(event.widget, ttk.Treeview) and event.widget.yview() != (0.0, 1.0):
            return
        for canvas in self.domain_canvases.values():
            if canvas.winfo_ismapped() and str(event.widget).startswith(str(canvas)) and canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
                return "break"

    def _reveal_domain_control(self, widget):
        if self._destroyed or not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        for canvas in self.domain_canvases.values():
            if canvas.winfo_ismapped() and str(widget).startswith(str(canvas) + "."):
                bounds = canvas.bbox("all")
                if not bounds:
                    return
                height = canvas.winfo_height()
                top = canvas.canvasy(widget.winfo_rooty() - canvas.winfo_rooty())
                bottom = top + min(widget.winfo_height(), max(1, height - 24))
                current = canvas.canvasy(0)
                target = top - 12 if top < current else bottom - height + 12 if bottom > current + height else current
                if target != current:
                    canvas.yview_moveto(max(0, target) / max(1, bounds[3]))
                return

    def _sync_domain_navigation(self, _=None):
        current = self.domain_sites.select()
        for site, page in (("A", self.video_domains_tab), ("B", self.text_domains_tab)):
            self.site_buttons[site].configure(style="Selected.Nav.TButton" if current == str(page) else "Nav.TButton")
            self.site_cards[site].configure(highlightbackground="#245EB8" if current == str(page) else "#DEE5ED")

    def _toggle_domain_section(self, site, key, *, opened=None):
        target = self.domain_sections[site][key]
        show = not bool(target.winfo_manager()) if opened is None else opened
        for name, panel in self.domain_sections[site].items():
            button, title = self.domain_section_buttons[site][name]
            if name == key and show:
                panel.pack(fill="x")
                button.configure(text=title + " · 접기")
            else:
                panel.pack_forget()
                button.configure(text=title + " · 펼치기")
        if show:
            target.update_idletasks()
            self.root.after_idle(self._reveal_domain_control, target.master)

    def _set_connection_state(self, site, state):
        labels = {
            "unknown": ("확인 전", "저장된 주소의 접속 상태를 확인해 주세요.", "#586A7D", "#F1F5F9"),
            "checking": ("확인 중", "사용 가능한 주소를 확인하고 있습니다.", "#245EB8", "#EAF2FF"),
            "verifying": ("자동 인증 중", "사이트의 보안 인증이 자동으로 완료되기를 기다리고 있습니다.", "#245EB8", "#EAF2FF"),
            "manual": ("직접 인증 중", "열린 Chrome 창에서 인증을 완료해 주세요. 완료되면 자동으로 접속을 확인합니다.", "#7354A3", "#F2ECFA"),
            "success": ("정상 연결", "콘텐츠 홈페이지 접속과 주소 저장을 확인했습니다.", "#16713D", "#E5F5EB"),
            "error": ("확인 필요", "접속 또는 주소 저장에 실패했습니다. 자세한 기록을 확인해 주세요.", "#B42318", "#FEECEB"),
            "verification": ("인증 필요", "보안 인증을 완료하지 못했습니다. 아래 ‘브라우저 검증 열기’를 눌러 직접 인증해 주세요.", "#B42318", "#FEECEB"),
            "cancelled": ("확인 중단", "주소 확인을 중단했습니다. 접속 확인으로 다시 시작할 수 있습니다.", "#586A7D", "#F1F5F9"),
        }
        label, message, color, background = labels[state]
        self.site_badges[site].set(label)
        self.site_badge_labels[site].configure(foreground=color, background=background)
        self.site_nav_badges[site].configure(text="● " + label, foreground=color, background=background)
        self.header_site_badges[site].configure(text=f"● {'TVWIKI' if site == 'A' else '뉴토끼'} · {label}",
                                                foreground=color, background=background)
        self.site_messages[site].set(message)
        if state in ("checking", "manual"):
            for step in self.site_steps[site]:
                step.set("대기")
            self.site_checked[site].set("접속 확인 시작 · " + time.strftime("%H:%M:%S"))
        elif state == "success":
            for step in self.site_steps[site]:
                step.set("성공")
        if state in ("success", "error", "verification", "cancelled"):
            self.site_checked[site].set("마지막 확인 · " + time.strftime("%Y.%m.%d %H:%M:%S"))
        if state in ("error", "verification"):
            self._toggle_domain_section(site, "help" if state == "verification" else "history", opened=True)

    def _connection_step(self, site, event):
        guide = self.text_domain_guide_url if site == "B" else self.domain_guide_url
        index = 0 if urlsplit(event["url"]).hostname.removeprefix("www.") == urlsplit(guide).hostname.removeprefix("www.") else 1
        self.site_steps[site][index].set({"checking": "확인 중", "success": "성공", "error": "실패", "verification": "인증 필요", "cancelled": "중단"}[event["state"]])
        if event["state"] == "checking":
            stage = "최신 주소 찾는 중" if index == 0 else "홈페이지 접속 확인 중"
            self.site_messages[site].set(f"{stage} · {event['phase']}\n{event['url']}\n{event['detail']}")

    def _text_browser_progress(self, event, fields):
        if event == "browser_waiting":
            detail = "브라우저 연결 준비 중 · 다른 검색·다운로드가 사용 중이면 완료될 때까지 기다립니다."
        elif event == "browser_request":
            detail = ("직접 인증 중 · 열린 Chrome 창에서 인증을 완료해 주세요." if fields.get("visible")
                      else "저장된 인증 정보로 접속 중 · 사이트 응답을 기다립니다.")
            detail += "\n" + fields.get("url", "")
        elif event == "chrome_start":
            detail = "Chrome 준비 중 · 이 PC에 저장된 인증 정보를 불러오고 있습니다."
        elif event == "http_response":
            if fields.get("challenge"):
                detail = ("직접 인증 중 · 열린 Chrome 창에서 인증을 완료해 주세요."
                          if self.site_badges["B"].get() == "직접 인증 중"
                          else f"보안 인증 감지 · 자동 통과를 최대 {CHALLENGE_WAIT_SECONDS}초 기다립니다.")
            elif 200 <= fields.get("status", 0) < 300:
                detail = "사이트 응답 수신 완료 · 홈페이지 표시가 끝나기를 기다립니다."
            else:
                detail = f"사이트 응답 확인 중 · HTTP {fields.get('status')}"
        elif event == "verification_wait":
            if fields.get("mode") == "manual":
                detail = f"직접 인증 중 · 열린 Chrome 창에서 인증을 완료해 주세요.\n남은 대기 시간 {fields['remaining']}초 · 인증 후 자동으로 이어집니다."
            else:
                self._set_connection_state("B", "verifying")
                detail = (f"보안 인증 자동 확인 중 · 경과 {fields['elapsed']}초 / 최대 {fields['limit']}초"
                          f" · 남은 {fields['remaining']}초\n완료되지 않으면 ‘브라우저 검증 열기’로 직접 인증할 수 있습니다.")
            self.site_steps["B"][1].set("직접 인증 중" if fields.get("mode") == "manual" else "자동 인증 중")
        elif event == "browser_success":
            detail = "홈페이지 응답 확인 완료 · 사용 가능한 주소인지 확인하고 저장합니다."
        elif event == "chrome_timeout":
            detail = "Chrome 응답이 지연되고 있습니다. 페이지가 완료되는지 계속 확인합니다."
        else:
            return
        self.site_messages["B"].set(detail)
        self.text_domain_status.set(detail)
        rows = self.text_domain_check_tree.get_children()
        if rows:
            values = list(self.text_domain_check_tree.item(rows[-1], "values"))
            if values[2] == "확인 중":
                values[3] = " ".join(detail.splitlines())
                self.text_domain_check_tree.item(rows[-1], values=values)

    def _save_domain_guide(self):
        if self.closing or self.domain_check_worker is not None:
            return
        self.domain_guide_entry._sync_variable()
        try:
            guide_url = normalize_text_guide_url(self.domain_guide_input.get())
        except ValueError as error:
            self.domain_guide_message.set(str(error))
            self.domain_guide_message_label.configure(foreground="#B42318")
            return
        previous = self.domain_guide_url
        self.domain_guide_url = guide_url
        self._dirty = True
        if not self._save_jobs(force=True):
            self.domain_guide_url = previous
            self.domain_guide_message.set("저장하지 못했습니다. 기존 안내 URL을 유지합니다. 저장 공간과 쓰기 권한을 확인해 주세요.")
            self.domain_guide_message_label.configure(foreground="#B42318")
            return
        self.domain_guide_input.set(guide_url)
        self.domain_guide_message.set("저장했습니다. 등록된 주소가 연결되지 않으면 이 안내 페이지를 확인합니다.")
        self.domain_guide_message_label.configure(foreground="#16713D")
        self._write_program_log("domain_guide_saved", site="TVWIKI", url=guide_url)
        if guide_url != previous:
            self._set_connection_state("A", "unknown")
            self.site_messages["A"].set("안내 페이지를 변경했습니다. ‘접속 확인’을 눌러 최신 주소를 확인해 주세요.")
            self.domain_status.set("안내 페이지를 변경했습니다. 기존 사이트 주소는 다음 접속 확인에 성공하면 갱신합니다.")

    def _save_text_domain_guide(self):
        if self.closing or self.text_domain_check_worker is not None:
            return
        self.text_domain_guide_entry._sync_variable()
        try:
            guide_url = normalize_text_guide_url(self.text_domain_guide_input.get())
        except ValueError as error:
            self.text_domain_guide_message.set(str(error))
            self.text_domain_guide_message_label.configure(foreground="#B42318")
            return
        previous = self.text_domain_guide_url
        self.text_domain_guide_url = guide_url
        self._dirty = True
        if not self._save_jobs(force=True):
            self.text_domain_guide_url = previous
            self.text_domain_guide_message.set("저장하지 못했습니다. 기존 안내 URL을 유지합니다. 저장 공간과 쓰기 권한을 확인해 주세요.")
            self.text_domain_guide_message_label.configure(foreground="#B42318")
            return
        self.text_domain_guide_input.set(guide_url)
        self.text_domain_guide_message.set("저장했습니다. 다음 ‘접속 확인’부터 이 안내 페이지를 사용합니다.")
        self.text_domain_guide_message_label.configure(foreground="#16713D")
        self._write_text_domain_log("guide_saved", url=guide_url)
        if guide_url != previous:
            self.text_domain_verification_url = ""
            self.text_domain_browser_button.configure(state="disabled")
            self._text_domain_result = None
            self._set_connection_state("B", "unknown")
            self.site_messages["B"].set("안내 페이지를 변경했습니다. ‘접속 확인’을 눌러 최신 주소를 확인해 주세요.")
            self.text_domain_status.set("안내 페이지를 변경했습니다. 기존 사이트 주소는 다음 접속 확인에 성공하면 갱신합니다.")

    def _save_auto_text_domain(self):
        if self.closing:
            return
        self._dirty = True
        if not self._save_jobs(force=True):
            self.auto_text_domain.set(not self.auto_text_domain.get())
            self.text_domain_status.set("B 자동 주소 확인 설정을 저장하지 못했습니다.")
            return
        self.text_domain_status.set("다음 실행부터 B 주소를 자동 확인합니다." if self.auto_text_domain.get()
                                    else "B 자동 확인을 껐습니다. ‘B 접속 확인’으로 직접 확인할 수 있습니다.")

    def _write_text_domain_log(self, event, **fields):
        self._write_program_log("text_" + event, **fields)
        # Only connection metadata is supplied; strip URL queries and fold lines.
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run": self._text_log_run, "event": event}
        for key, value in fields.items():
            if isinstance(value, str):
                value = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1", value)
                value = " ".join(value.split())[:1500]
            record[key] = value
        with self._text_log_lock:
            try:
                path = self.text_domain_log_path
                if path.exists() and path.stat().st_size >= 1024 * 1024:
                    path.replace(path.with_suffix(".previous.log"))
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._text_log_error = ""
            except OSError as error:
                self._text_log_error = str(error)

    def _fetch_text_in_browser(self, url, cancel, *, limit, visible=False, log_run=None, request_only=False, novel_body=False, on_progress=None):
        # Address checks and catalogue searches share one authenticated page.
        check_cancel(cancel)
        if not is_text_site_url(url):
            raise TransferError("B 접속은 toki숫자.com 주소만 지원합니다. 도메인 설정에서 접속 주소를 다시 확인해 주세요.")
        def browser_event(event, **fields):
            self._write_text_domain_log(event, **({"run": log_run} if log_run is not None else {}), **fields)
            if on_progress:
                on_progress(event, fields)

        if on_progress:
            browser_event("browser_waiting")
        while not self._text_browser_lock.acquire(timeout=.1):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            if self.closing or self._destroyed:
                raise TransferCancelled()
            if self._text_browser is None or self._text_browser.closed:
                self._text_browser = TextSiteBrowser(self._text_browser_profile, on_event=self._write_text_domain_log)
            if log_run is not None or on_progress:
                self._text_browser.on_event = browser_event
            fetch = self._text_browser.get_novel_text if novel_body else self._text_browser.get_html if request_only else self._text_browser.get
            return fetch(url, cancel, limit=limit, visible=visible)
        finally:
            if self._text_browser is not None:
                self._text_browser.on_event = self._write_text_domain_log
            self._text_browser_lock.release()

    def refresh_text_domain(self, browser_verification=False):
        if self.closing or self._destroyed or self.text_domain_check_worker is not None:
            return
        if browser_verification and not self.text_domain_verification_url:
            return
        verification_url = self.text_domain_verification_url if browser_verification else ""
        verification_domains = ((normalize_domain("https://" + urlsplit(verification_url).netloc),)
                                if is_text_site_url(verification_url) else ())
        self.text_domain_check_cancel.clear()
        self._text_domain_result = None
        self.text_domain_check_tree.delete(*self.text_domain_check_tree.get_children())
        self._set_connection_state("B", "manual" if browser_verification else "checking")
        self.text_domain_status.set("전용 Chrome 창에서 검증을 완료해 주세요. 완료되면 자동으로 확인하고 저장합니다."
                                    if browser_verification else "뉴토끼 안내 페이지에서 최신 접속 주소를 찾고 있습니다…")
        self.text_domain_refresh_button.configure(state="disabled")
        self.text_domain_browser_button.configure(state="disabled")
        self.text_domain_guide_save_button.configure(state="disabled")
        self.text_domain_verification_url = ""
        self._text_log_run = uuid.uuid4().hex[:8]
        log_run = self._text_log_run
        guide_url = self.text_domain_guide_url
        self._write_text_domain_log("check_start", mode="manual_verification" if browser_verification else "automatic")

        def progress(event):
            self._write_text_domain_log("progress", **event)
            self.text_domain_check_events.put(("progress", event))

        def run():
            started = time.monotonic()
            try:
                result = _find_available_domain(verification_domains, self.text_domain_check_cancel, text_site=True, text_guide_url=guide_url,
                                                browser_fetch=lambda url, cancel, limit: self._fetch_text_in_browser(
                                                    url, cancel, limit=limit, visible=browser_verification, log_run=log_run,
                                                    on_progress=lambda event, fields: self.text_domain_check_events.put(
                                                        ("browser_progress", (log_run, event, fields)))),
                                                report=progress)
                self._write_text_domain_log("check_success", url=result[0], seconds=round(time.monotonic() - started, 2))
                self.text_domain_check_events.put(("success", result))
            except BrowserVerificationRequired as error:
                self._write_text_domain_log("verification_required", url=error.url, detail=str(error), seconds=round(time.monotonic() - started, 2))
                self.text_domain_check_events.put(("verification", (error.url, str(error))))
            except Cancelled:
                self._write_text_domain_log("check_cancelled", seconds=round(time.monotonic() - started, 2))
                self.text_domain_check_events.put(("cancelled", None))
            except (DownloadError, ValueError, OSError) as error:
                self._write_text_domain_log("check_error", detail=str(error), seconds=round(time.monotonic() - started, 2))
                self.text_domain_check_events.put(("error", str(error)))

        self.text_domain_check_worker = threading.Thread(target=run, daemon=True)
        self.text_domain_check_worker.start()

    def _poll_text_domain_check(self):
        for _ in range(100):
            try:
                kind, value = self.text_domain_check_events.get_nowait()
            except queue.Empty:
                return
            if kind not in ("progress", "browser_progress"):
                self.text_domain_check_worker = None
                self._text_domain_result = kind
            if self.closing or self._destroyed:
                continue
            if kind == "browser_progress":
                log_run, event, fields = value
                if log_run == self._text_log_run and self.text_domain_check_worker is not None:
                    self._text_browser_progress(event, fields)
                continue
            if kind == "progress":
                self._connection_step("B", value)
                state = value["state"]
                label = {"checking": "확인 중", "success": "성공", "error": "실패", "cancelled": "취소", "verification": "검증 필요"}[state]
                values = (value["phase"], value["url"], label, value["detail"])
                if self.text_domain_check_tree.exists(value["id"]):
                    self.text_domain_check_tree.item(value["id"], values=values, tags=(state,))
                else:
                    self.text_domain_check_tree.insert("", "end", iid=value["id"], values=values, tags=(state,))
                self.text_domain_check_tree.see(value["id"])
                if state == "checking":
                    self.text_domain_status.set(f"B 확인 중: {value['url']} · {value['detail']}")
                continue
            self.text_domain_refresh_button.configure(state="normal")
            self.text_domain_guide_save_button.configure(state="normal")
            if kind == "verification":
                self._set_connection_state("B", "verification")
                self.text_domain_verification_url, detail = value
                self.text_domain_browser_button.configure(state="normal")
                self.text_domain_status.set(detail)
            elif kind == "success":
                origin, _, source = value
                previous = self.text_domain
                self.text_domain = origin
                self._dirty = True
                if not self._save_jobs(force=True):
                    self.text_domain = previous
                    self._write_text_domain_log("save_failed", url=origin)
                    self.text_domain_status.set("B 주소를 저장하지 못했습니다. 기존 B 주소를 유지합니다.")
                    self._set_connection_state("B", "error")
                    continue
                self._write_text_domain_log("address_saved", url=origin)
                self.text_domain_value.set(origin)
                self._set_connection_state("B", "success")
                self.site_messages["B"].set("홈페이지 접속과 주소 저장 완료 · " + origin
                                            + "\n소설 본문에서는 별도 인증이 필요할 수 있습니다.")
                self.text_domain_status.set(f"B 접속 확인 완료: {origin} · {source}에서 확인한 주소를 저장했습니다.")
            elif kind == "error":
                self.text_domain_status.set(f"B 접속 확인 실패: {value.splitlines()[-1]}\n기존 B 주소를 유지합니다. 위 기록에서 각 주소의 결과를 확인하세요.")
                self._set_connection_state("B", "error")
                self.site_messages["B"].set("접속 확인 실패 · " + value.splitlines()[-1]
                                            + "\n‘접속 확인’을 눌러 다시 시도해 주세요.")
            elif kind == "cancelled":
                self._set_connection_state("B", "cancelled")
            if self._text_log_error:
                self.text_domain_status.set(self.text_domain_status.get() + "\n접속 로그 파일을 저장하지 못했습니다.")

    def _refresh_domains(self, selected=0):
        self.video_domain_value.set(self.domains[0])
        self._set_connection_state("A", "unknown")
        self.domain_tree.delete(*self.domain_tree.get_children())
        for index, domain in enumerate(self.domains):
            self.domain_tree.insert("", "end", iid=str(index), values=(index + 1, domain))
        selected = min(max(0, selected), len(self.domains) - 1)
        self.domain_tree.selection_set(str(selected))
        self.domain_tree.focus(str(selected))
        self._domain_actions()

    def _domain_actions(self):
        selection = self.domain_tree.selection()
        index = int(selection[0]) if selection else -1
        enabled = not self.closing and index >= 0
        self.domain_up_button.configure(state="normal" if enabled and index > 0 else "disabled")
        self.domain_down_button.configure(state="normal" if enabled and index < len(self.domains) - 1 else "disabled")
        self.domain_remove_button.configure(state="normal" if enabled and len(self.domains) > 1 else "disabled")
        self.domain_add_button.configure(state="disabled" if self.closing else "normal")
        self.domain_refresh_button.configure(state="disabled" if self.closing or self.domain_check_worker else "normal")
        self.domain_guide_save_button.configure(state="disabled" if self.closing or self.domain_check_worker else "normal")
        self.domain_auto_check.configure(state="disabled" if self.closing else "normal")

    def _save_auto_domain(self):
        if self.closing:
            return
        self._dirty = True
        if not self._save_jobs(force=True):
            self.auto_domain.set(not self.auto_domain.get())
            self.domain_status.set("자동 주소 확인 설정을 저장하지 못했습니다.")
            return
        self.domain_status.set("다음 실행부터 최신 주소를 자동 확인합니다." if self.auto_domain.get()
                               else "자동 확인을 껐습니다. ‘최신 주소 확인’으로 직접 갱신할 수 있습니다.")

    def _save_fastest_server_only(self):
        if self.closing:
            return
        self._dirty = True
        if not self._save_jobs(force=True):
            self.fastest_server_only.set(self._fastest_server_only_event.is_set())
            self.status.set("다운로드 옵션을 저장하지 못했습니다. 이전 설정을 유지합니다.")
            return
        if self.fastest_server_only.get():
            self._fastest_server_only_event.set()
        else:
            self._fastest_server_only_event.clear()
        self.status.set("가장 빠른 서버에 새 HTTP 요청을 집중합니다. 측정 전에는 서버 속도를 먼저 확인합니다."
                        if self.fastest_server_only.get() else "서버 속도와 연결 수를 고려한 분산 다운로드로 전환합니다.")
        self._refresh_details(force=True)

    def _save_auto_retry(self):
        if self.closing:
            return
        self._dirty = True
        if not self._save_jobs(force=True):
            self.auto_retry.set(not self.auto_retry.get())
            self.status.set("자동 재시도 설정을 저장하지 못했습니다. 이전 설정을 유지합니다.")
            return
        if not self.auto_retry.get():
            for job in self.jobs:
                job["auto_retry_at"] = 0
        self.status.set("오류 작업은 대기열 실행 중 30초 간격으로 다시 시도합니다. 직접 인증이 필요한 작업은 기다립니다."
                        if self.auto_retry.get() else "오류 작업의 계속 재시도를 껐습니다. 진행 중인 다운로드는 계속됩니다.")
        self._refresh_table()
        self._refresh_details(force=True)

    def _start_domain_check(self):
        self._domain_check_timer = None
        if self.auto_domain.get():
            self.refresh_current_domain()
        if self.auto_text_domain.get():
            self.refresh_text_domain()

    def _cancel_domain_check(self):
        self.domain_check_cancel.set()
        self.text_domain_check_cancel.set()
        if self._text_browser is not None:
            self._text_browser.close()
        if self._domain_check_timer is not None:
            self.root.after_cancel(self._domain_check_timer)
            self._domain_check_timer = None

    def refresh_current_domain(self):
        if self.closing or self._destroyed or self.domain_check_worker is not None:
            return
        self.domain_check_cancel.clear()
        self.domain_check_tree.delete(*self.domain_check_tree.get_children())
        self._set_connection_state("A", "checking")
        self.domain_status.set("등록된 도메인부터 접속을 확인하고 있습니다…")
        domains = self.domains
        guide_url = self.domain_guide_url
        check_id = uuid.uuid4().hex[:8]
        self._write_program_log("domain_check_start", site="TVWIKI", check=check_id, order=domains)

        def progress(event):
            self._write_program_log("domain_check_progress", site="TVWIKI", check=check_id,
                                    level=logging.WARNING if event["state"] == "error" else logging.INFO, **event)
            self.domain_check_events.put(("progress", event))

        def run():
            try:
                result = _find_available_domain(domains, self.domain_check_cancel, report=progress, video_guide_url=guide_url)
                self._write_program_log("domain_check_success", site="TVWIKI", check=check_id, url=result[0])
                self.domain_check_events.put(("success", result))
            except Cancelled:
                self._write_program_log("domain_check_cancelled", site="TVWIKI", check=check_id)
                self.domain_check_events.put(("cancelled", None))
            except (DownloadError, ValueError, OSError) as error:
                self._write_program_log("domain_check_error", site="TVWIKI", check=check_id,
                                        level=logging.ERROR, detail=str(error))
                self.domain_check_events.put(("error", str(error)))

        self.domain_check_worker = threading.Thread(target=run, daemon=True)
        self._domain_actions()
        self.domain_check_worker.start()

    def _poll_domain_check(self):
        for _ in range(100):
            try:
                kind, value = self.domain_check_events.get_nowait()
            except queue.Empty:
                return
            if kind != "progress":
                self.domain_check_worker = None
            if self.closing or self._destroyed:
                continue
            if kind == "progress":
                self._connection_step("A", value)
                state = value["state"]
                label = {"checking": "확인 중", "success": "성공", "error": "실패", "cancelled": "취소"}[state]
                values = (value["phase"], value["url"], label, value["detail"])
                if self.domain_check_tree.exists(value["id"]):
                    self.domain_check_tree.item(value["id"], values=values, tags=(state,))
                else:
                    self.domain_check_tree.insert("", "end", iid=value["id"], values=values, tags=(state,))
                self.domain_check_tree.see(value["id"])
                if state == "checking":
                    self.domain_status.set(f"현재 확인 중: {value['url']} · {value['phase']}")
                continue
            self._domain_actions()
            if kind == "success":
                value, checked_domain, source = value
                if checked_domain is not None and checked_domain not in self.domains:
                    self._set_connection_state("A", "error")
                    self.domain_status.set("확인 중 도메인 목록이 변경되었습니다. ‘최신 주소 확인’을 다시 눌러 주세요.")
                    return
                key = urlsplit(value).hostname.removeprefix("www.")
                remaining = [domain for domain in self.domains if urlsplit(domain).hostname.removeprefix("www.") != key]
                if self._apply_domains([value, *remaining], 0):
                    self._set_connection_state("A", "success")
                    self.domain_status.set(f"접속 확인 완료: {value} · {source}에서 정상 접속을 확인해 첫 번째로 저장했습니다.")
                else:
                    self._set_connection_state("A", "error")
            elif kind == "error":
                self._set_connection_state("A", "error")
                self.domain_status.set(f"최신 주소 확인 실패: {value.splitlines()[-1]}\n기존 도메인 목록을 유지합니다.")
            elif kind == "cancelled":
                self._set_connection_state("A", "cancelled")

    def _apply_domains(self, domains, selected):
        if self.closing:
            return False
        try:
            normalized = normalize_domains(domains)
        except ValueError as error:
            self.domain_status.set(str(error))
            return False
        previous = self.domains
        self.domains = normalized
        self._dirty = True
        if not self._save_jobs(force=True):
            self.domains = previous
            self._dirty = True
            self.domain_status.set("도메인 설정을 저장하지 못했습니다. 이전 접속 순서를 유지합니다.")
            self._write_program_log("domain_order_save_error", level=logging.ERROR, order=normalized, kept_order=previous)
            return False
        set_domains(normalized)
        self._refresh_domains(selected)
        self._write_program_log("domain_order_saved", previous_order=previous, order=normalized)
        self.domain_status.set("도메인 순서를 저장했습니다. 다음 접속부터 이 순서로 시도합니다.")
        return True

    def add_domain(self):
        self.domain_entry._sync_variable()
        if self._apply_domains([*self.domains, self.domain_input.get()], len(self.domains)):
            self.domain_input.set("")

    def remove_domain(self):
        selected = self.domain_tree.selection()
        if selected:
            index = int(selected[0])
            self._apply_domains([domain for position, domain in enumerate(self.domains) if position != index], index)

    def move_domain(self, direction):
        selected = self.domain_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        target = index + direction
        if 0 <= target < len(self.domains):
            domains = list(self.domains)
            domains[index], domains[target] = domains[target], domains[index]
            self._apply_domains(domains, target)

    def show_search(self):
        if self.closing:
            return
        if self.search_dialog is None or self.search_dialog.closed:
            self.search_dialog = SearchDialog(self)
        self.main_tabs.select(self.search_tab)
        self.search_dialog.query_entry.focus_set()

    def _main_tab_changed(self, _=None):
        if not self.closing and self.main_tabs.select() == str(self.search_tab):
            self.show_search()

    def add_episodes(self, items, *, start=False):
        if self.closing:
            raise DownloadError("매니저를 종료하는 중입니다.")
        self.folder_entry._sync_variable()
        pages = []
        for item in items:
            kind = item.get("kind", "video")
            if kind not in ("video", "text"):
                raise DownloadError("지원하지 않는 다운로드 형식입니다.")
            page = (parse_text_page_url if kind == "text" else parse_page_url)(item["url"])[0]
            title = " · ".join(part for part in (item.get("series_title"), item["title"]) if part) if kind == "text" else item["title"]
            pages.append((page, title, kind))
        if not self.folder.get().strip():
            raise DownloadError("매니저에서 저장 폴더를 선택해 주세요.")
        try:
            folder = Path(self.folder.get().strip()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise DownloadError("저장 폴더를 만들 수 없습니다. 경로와 저장 권한을 확인해 주세요.") from None
        existing = {(job.get("kind", "video"), urlsplit(job["url"]).path if job.get("kind") == "text" else job["url"])
                    for job in self.jobs if job["folder"] == str(folder)}
        added = []
        skipped = 0
        for page, title, kind in pages:
            identity = (kind, urlsplit(page).path if kind == "text" else page)
            if identity in existing:
                skipped += 1
                continue
            job = self._new_job(page, str(folder), uuid.uuid4().hex)
            job["title"] = title or page
            job["kind"] = kind
            self.jobs.append(job)
            self._log(job, "콘텐츠 검색에서 대기열에 추가했습니다.")
            existing.add(identity)
            added.append(job)
        if added:
            self._dirty = True
            persisted = self._save_jobs(force=True)
            if not persisted:
                for job in added:
                    job["status"] = "error"
                    self._log(job, "작업 목록을 저장하지 못해 다운로드를 시작하지 않았습니다.")
            self.filter.set("전체")
            self._refresh_table()
            self.tree.selection_set([job["id"] for job in added])
            self.tree.focus(added[0]["id"])
            self.tree.see(added[0]["id"])
            if persisted and start:
                self.queue_running = True
                self._start_next()
            self._refresh_details(force=True)
            if not persisted:
                raise DownloadError("작업 목록 저장에 실패했습니다. 매니저에서 저장 공간을 확인한 뒤 다시 시작해 주세요.")
        self.status.set(f"{len(added)}개 회차를 대기열에 추가했습니다. 기존 항목 {skipped}개는 건너뛰었습니다.")
        return len(added), skipped

    @staticmethod
    def _text_file_ready(job):
        if not job or job.get("kind") != "text" or job["status"] != "completed" or not job.get("file"):
            return False
        try:
            path = Path(job["file"]).resolve()
            return path.parent == Path(job["folder"]).resolve() and path.suffix.lower() == ".txt" and path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _archive_member_valid(archive, job):
        if not job or job.get("kind") != "text":
            return False
        folder = Path(archive["folder"]).resolve()
        if job.get("archive_owner") == archive["id"]:
            return Path(job["folder"]).resolve() == folder / ".tvmon-zip" / archive["id"]
        return job.get("archive_owner") is None and Path(job["folder"]).resolve() == folder

    def add_text_archive(self, items, *, cleanup_originals=False):
        if self.closing:
            raise DownloadError("매니저를 종료하는 중입니다.")
        if not items or any(item.get("kind") != "text" for item in items):
            raise DownloadError("소설의 전체 회차 목록을 먼저 불러와 주세요.")
        archive_title = text_archive_title(items)
        pages = [parse_text_page_url(item["url"])[0] for item in items]
        paths = [urlsplit(page).path for page in pages]
        if len(set(paths)) != len(paths) or len({path.rsplit("/", 1)[0] for path in paths}) != 1:
            raise DownloadError("한 소설의 전체 회차 목록을 확인해 주세요.")
        self.folder_entry._sync_variable()
        if not self.folder.get().strip():
            raise DownloadError("매니저에서 저장 폴더를 선택해 주세요.")
        folder = str(Path(self.folder.get().strip()).expanduser().resolve())
        Path(folder).mkdir(parents=True, exist_ok=True)
        archive = next((job for job in self.jobs if job.get("kind") == "text_zip" and job["folder"] == folder
                        and job["archive_urls"] == paths), None)
        if archive is None:
            archive = self._new_job(pages[0], folder, uuid.uuid4().hex)
            archive.update(kind="text_zip", title=archive_title,
                           archive_urls=paths, total=len(paths))
            self.jobs.append(archive)
            self._dirty = True
        if archive["id"] == self.active_id:
            return archive
        if archive["status"] == "completed" and (not archive.get("file") or not Path(archive["file"]).is_file() or not Path(archive["file"]).stat().st_size):
            archive["status"] = "paused"
        archive["cleanup_originals"] = bool(cleanup_originals)
        if archive["status"] == "completed":
            if cleanup_originals and any(self._job(member) for member in archive["archive_jobs"]):
                archive["cleanup_pending"] = True
        else:
            archive["title"] = archive_title
            existing = {urlsplit(job["url"]).path: job for job in self.jobs if self._archive_member_valid(archive, job)}
            members = []
            for page, path, item in zip(pages, paths, items):
                job = existing.get(path)
                if job is None:
                    temporary = Path(folder) / ".tvmon-zip" / archive["id"]
                    job = self._new_job(page, str(temporary), uuid.uuid4().hex)
                    job.update(kind="text", archive_owner=archive["id"],
                               title=" · ".join(part for part in (item.get("series_title"), item["title"]) if part))
                    self.jobs.insert(self.jobs.index(archive), job)
                    self._log(job, "ZIP용 임시 TXT입니다. ZIP 검증 후 파일과 회차 행을 정리합니다.")
                members.append(job["id"])
            archive["archive_jobs"] = members
        self._dirty = True
        if not self._save_jobs(force=True):
            self.queue_running = False
            archive["status"] = "paused"
            raise DownloadError("작업 목록 저장에 실패해 ZIP 다운로드와 TXT 정리를 시작하지 않았습니다.")
        self.filter.set("전체")
        self._refresh_table()
        self.tree.selection_set(archive["id"])
        self.tree.focus(archive["id"])
        self.tree.see(archive["id"])
        if archive["status"] != "completed" or archive["cleanup_pending"]:
            self.start()
        self._refresh_details(force=True)
        return archive

    def _resume_text_archive(self, archive):
        archive.update(retry_round=0, retry_at=0)
        for member in archive["archive_jobs"]:
            job = self._job(member)
            if (self._archive_member_valid(archive, job)
                    and member != self.active_id and not self._text_file_ready(job)):
                job.update(status="queued", retry_at=0)
        self._log(archive, f"전체 {len(archive['archive_jobs'])}개 회차가 완료되면 ZIP으로 저장합니다. 오류 회차는 최대 3회 재시도합니다.")

    def _next_text_archive_job(self, archive):
        members = [self._job(member) for member in archive["archive_jobs"]]
        if any(not self._archive_member_valid(archive, job) for job in members):
            archive["status"] = "error"
            self._log(archive, "ZIP에 필요한 회차 작업이 제거되었습니다. 검색 화면에서 전체 ZIP으로 저장을 다시 눌러 주세요.")
        else:
            for job in members:
                if job["status"] == "completed" and not self._text_file_ready(job):
                    job["status"] = "error"
                    self._log(job, "완료된 TXT 파일이 없어 다시 받아야 합니다.")
            pending = [job for job in members if job["status"] == "queued"]
            if pending:
                return next((job for job in pending if job.get("retry_at", 0) <= time.monotonic()), None)
            if any(job["status"] == "paused" for job in members):
                archive["status"] = "paused"
                self._log(archive, "일시정지한 회차가 있습니다. 이 ZIP 작업을 시작하면 남은 회차부터 이어서 받습니다.")
            else:
                failed = [job for job in members if job["status"] == "error"]
                if not failed:
                    return archive
                verification = next((job for job in failed if job.get("verification_url")), None)
                if verification:
                    archive["status"] = "error"
                    self._log(archive, "브라우저 인증이 필요한 회차가 있습니다. ‘브라우저 인증 열기’에서 인증을 완료하면 이어서 다운로드합니다.")
                elif archive["retry_round"] < len(TEXT_ZIP_RETRY_DELAYS):
                    delay = TEXT_ZIP_RETRY_DELAYS[archive["retry_round"]]
                    archive["retry_round"] += 1
                    archive["retry_at"] = time.monotonic() + delay
                    for job in failed:
                        job.update(status="queued", retry_at=archive["retry_at"])
                        self._log(job, f"ZIP 자동 재시도 {archive['retry_round']}/3 · {delay}초 후 다시 받습니다.")
                    self._log(archive, f"오류 {len(failed)}개 회차 자동 재시도 {archive['retry_round']}/3 · {delay}초 후 시작합니다.")
                    self._write_text_domain_log("text_zip_retry", job=archive["id"], attempt=archive["retry_round"], failed=len(failed))
                else:
                    archive["status"] = "error"
                    self._log(archive, f"ZIP 미완성: {len(failed)}개 회차가 3회 재시도 후에도 실패했습니다. 이 작업의 시작 / 이어받기로 다시 시도하세요.")
        self._dirty = True
        self.status.set(archive["message"])
        return None

    def start(self):
        if self.closing:
            return
        selected = self.tree.selection()
        if not selected:
            self.add_job()
            return
        first = None
        for job_id in selected:
            job = self._job(job_id)
            if job and job_id != self.active_id and (job["status"] != "completed" or job.get("cleanup_pending")):
                if job.get("kind") == "text_zip" and job["status"] != "completed":
                    self._resume_text_archive(job)
                if job["status"] != "completed":
                    job["status"] = "queued"
                job["retry_at"] = 0
                job["auto_retry_at"] = 0
                first = first or job_id
        if first is None:
            return
        self.queue_running = True
        self._dirty = True
        if self._save_jobs(force=True):
            self._start_next(preferred=first)
        else:
            self.queue_running = False
            for job_id in selected:
                job = self._job(job_id)
                if job and job_id != self.active_id and job["status"] == "queued":
                    job["status"] = "paused"
        self._refresh_table()
        self._refresh_details(force=True)

    def _verification_job(self, job):
        if not job or job["status"] == "completed":
            return None
        if job.get("kind") == "text" and job.get("verification_url"):
            return job
        if job.get("kind") == "text_zip":
            for member in job["archive_jobs"]:
                item = self._job(member)
                if self._archive_member_valid(job, item) and item["status"] != "completed" and item.get("verification_url"):
                    return item
        return None

    def verify_text_job(self):
        if self.closing or self.active_id:
            return
        job = self._verification_job(self._selected_job())
        if job is None:
            job = next((item for item in self.jobs if item.get("kind") == "text" and self._verification_job(item)), None)
        if job is None:
            return
        self.filter.set("전체")
        self.list_query.set("")
        self._refresh_table()
        self.tree.selection_set(job["id"])
        self.tree.focus(job["id"])
        self.tree.see(job["id"])
        job["status"] = "queued"
        self.queue_running = True
        self._start_next(preferred=job["id"], visible=True)
        self._refresh_table()
        self._refresh_details(force=True)

    def _start_next(self, preferred=None, *, visible=False):
        if self.closing or not self.queue_running or self.active_id or (self.worker and self.worker.is_alive()):
            return
        retry_job = None
        if self.auto_retry.get():
            members = {member for archive in self.jobs if archive.get("kind") == "text_zip" and archive["status"] != "completed"
                       for member in archive["archive_jobs"]}
            now, scheduled = time.monotonic(), False
            for item in self.jobs:
                if item["status"] != "error" or item.get("verification_url") or item["id"] in members:
                    continue
                if item.get("kind") == "text_zip" and any((self._job(member) or {}).get("verification_url") for member in item["archive_jobs"]):
                    continue
                if not item.get("auto_retry_at"):
                    item["auto_retry_at"] = now + DOWNLOAD_RETRY_SECONDS
                    self._log(item, "30초 후 자동 재시도합니다. 오류 원인은 앞선 작업 기록에서 확인할 수 있습니다.")
                    scheduled = self._dirty = True
                elif item["auto_retry_at"] <= now and retry_job is None:
                    retry_job = item
            if scheduled:
                self._refresh_table()
        job = self._job(preferred) if preferred else None
        cleanup_only = bool(job and job.get("kind") == "text_zip" and job["status"] == "completed" and job["cleanup_pending"])
        if not cleanup_only and (not job or job["status"] != "queued" or job.get("retry_at", 0) > time.monotonic()):
            job = next((item for item in self.jobs if item["status"] == "queued" and item.get("retry_at", 0) <= time.monotonic()), None)
        if job is None and retry_job is not None:
            job = retry_job
            if job.get("kind") == "text_zip":
                self._resume_text_archive(job)
            job.update(status="queued", auto_retry_at=0)
            self._log(job, "오류 작업을 자동으로 다시 시도합니다.")
        if not job:
            return
        if job.get("kind") == "text_zip" and not cleanup_only:
            job = self._next_text_archive_job(job)
            if job is None:
                if self._dirty:
                    if not self._save_jobs(force=True):
                        self.queue_running = False
                    self._refresh_table()
                    self._refresh_details(force=True)
                return
        self.active_id = job["id"]
        self.run_counter += 1
        run_id = self.run_counter
        job["run_id"] = run_id
        job.update(status="cleaning" if cleanup_only else "resolving", speed=0, eta=0, workers=[], segment_states={},
                   http_bytes=0, p2p_bytes=0, http_speed=0, p2p_speed=0, p2p_parts=[], p2p_status="disabled", p2p_trackers=[], server_health={})
        self.cancel_event = threading.Event()
        token = self.cancel_event
        text_job = job.get("kind") == "text"
        zip_job = job.get("kind") == "text_zip"
        if text_job:
            url = self.text_domain + urlsplit(job["url"]).path if self.text_domain else job["url"]
        else:
            url = job["url"] if zip_job else domain_requests(job["url"], job["url"])[0][0]
        archive_files = [Path(self._job(member)["file"]) for member in job["archive_jobs"]] if zip_job and not cleanup_only else []
        cleanup_gate = threading.Event()
        if zip_job:
            job["cleanup_gate"] = cleanup_gate
            job["cleanup_allowed"] = False
        job_id, title, folder = job["id"], job["title"], Path(job["folder"])
        log_run = uuid.uuid4().hex[:8]
        job["verification_url"] = None
        self._log(job, "검증된 ZIP에 포함된 TXT를 정리합니다." if cleanup_only else "전체 회차 TXT를 ZIP으로 압축합니다." if zip_job else "소설 본문을 확인합니다. 회차별 TXT로 저장합니다." if text_job else "이어받기 정보를 확인합니다.")
        selected = self._selected_job()
        if not selected or selected["status"] == "completed":
            self.tree.selection_set(job_id)
            self.tree.focus(job_id)
        self._dirty = True
        if not self._save_jobs(force=True):
            self.active_id = None
            self.queue_running = False
            job["status"] = "error"
            self._log(job, "작업 목록을 저장하지 못해 다운로드를 시작하지 않았습니다.")
            return

        def emit(kind, value):
            self.events.put((job_id, run_id, kind, value))

        def run():
            published = cleanup_only
            try:
                if zip_job:
                    if not cleanup_only:
                        self._write_text_domain_log("text_zip_start", run=log_run, job=job_id, episodes=len(archive_files))
                        save_text_zip(archive_files, folder, token, emit, title=title, temporary_folder=folder / ".tvmon-zip" / job_id)
                        published = True
                        self._write_text_domain_log("text_zip_success", run=log_run, job=job_id, episodes=len(archive_files))
                        while not cleanup_gate.wait(.1):
                            check_cancel(token)
                        if not job["cleanup_allowed"]:
                            emit("cleanup", {"deleted": [], "errors": ["ZIP 완료 기록을 저장하지 못해 TXT를 보관했습니다. 정리 재시도를 눌러 주세요."]})
                            return
                    sources, blocked = [], []
                    for member in job["archive_jobs"]:
                        item = self._job(member)
                        path, remove = None, False
                        if item:
                            if not self._archive_member_valid(job, item) or not item.get("file"):
                                blocked.append("회차 파일 위치를 확인하지 못해 보관했습니다.")
                            else:
                                path = Path(item["file"])
                                remove = item.get("archive_owner") == job_id or job["cleanup_originals"]
                                if path.resolve().parent != Path(item["folder"]).resolve() or path.suffix.lower() != ".txt":
                                    blocked.append("회차 파일이 저장 폴더를 벗어나 보관했습니다.")
                                    remove = False
                                elif remove and any(other["id"] != job_id and other.get("kind") == "text_zip"
                                                    and other["status"] != "completed" and member in other["archive_jobs"] for other in self.jobs):
                                    blocked.append("다른 미완성 ZIP에서 사용하는 TXT를 보관했습니다.")
                                    remove = False
                        sources.append((member, path, remove))
                    result = cleanup_text_zip(Path(job["file"]), sources, token)
                    result["errors"].extend(blocked)
                    temporary = folder.resolve() / ".tvmon-zip" / job_id
                    # Empty directories only; never recursively remove source files.
                    if temporary.resolve().parent == folder.resolve() / ".tvmon-zip":
                        for directory in (temporary, temporary.parent):
                            try:
                                directory.rmdir()
                            except OSError:
                                pass
                    emit("cleanup", result)
                elif text_job:
                    self._write_text_domain_log("text_download_start", run=log_run, url=url, visible=visible)
                    download_text(url, folder, token, emit, title=title,
                        browser_fetch=lambda url, cancel, **kwargs: self._fetch_text_in_browser(
                            url, cancel, log_run=log_run, novel_body=True, visible=visible, **kwargs))
                    self._write_text_domain_log("text_download_success", run=log_run, url=url)
                else:
                    download_video(url, folder, token, emit, resume_id=job_id, log_dir=self.state_path.parent / "transfer-logs",
                                   fastest_only=self._fastest_server_only_event)
            except Cancelled:
                if text_job or zip_job:
                    self._write_text_domain_log("text_download_cancelled", run=log_run, url=url)
                emit("cleanup", {"deleted": [], "errors": ["TXT 정리를 중단했습니다. ZIP 작업에서 정리 재시도를 누르세요."]}) if published else emit("paused", None)
            except BrowserVerificationRequired as error:
                self._write_text_domain_log("text_download_verification", run=log_run, url=error.url)
                emit("verification", error.url)
            except DownloadError as error:
                if text_job or zip_job:
                    self._write_text_domain_log("text_download_error", run=log_run, url=url, detail=str(error))
                emit("error", str(error))
            except FileExistsError:
                emit("error", "같은 이름의 파일이 이미 있습니다. 기존 파일의 이름을 바꾼 후 이어받기를 눌러 주세요. 받은 조각은 보관했습니다.")
            except OSError:
                self._write_program_log("job_storage_error", job=job_id, level=logging.ERROR, detail=traceback.format_exc())
                emit("cleanup", {"deleted": [], "errors": ["TXT 정리에 실패했습니다. 파일 권한을 확인한 뒤 다시 시도하세요."]}) if published else emit("error", "TXT/ZIP 저장 위치, 디스크 공간, 쓰기 권한을 확인해 주세요." if text_job or zip_job else "저장 위치, 디스크 공간, 쓰기 권한을 확인해 주세요. 받은 조각은 보관되어 있습니다.")
            except Exception:
                self._write_program_log("job_unexpected_error", job=job_id, level=logging.ERROR, detail=traceback.format_exc())
                emit("cleanup", {"deleted": [], "errors": ["TXT 정리를 완료하지 못했습니다. 정리 재시도를 누르세요."]}) if published else emit("error", "소설 파일을 저장하지 못했습니다. 다시 시작해 주세요." if text_job or zip_job else "영상을 처리하지 못했습니다. 받은 조각을 보관했습니다. 다시 이어받기를 눌러 주세요.")
            finally:
                emit("idle", None)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def pause(self):
        selected = set(self.tree.selection())
        if not selected and self.active_id:
            selected.add(self.active_id)
        for job_id in tuple(selected):
            job = self._job(job_id)
            if job and job.get("kind") == "text_zip" and job["status"] != "completed":
                selected.update(job["archive_jobs"])
        if self.active_id in selected or not self.active_id:
            self.queue_running = False
        for job_id in selected:
            job = self._job(job_id)
            if not job or job["status"] == "completed":
                continue
            if job_id == self.active_id:
                job["status"] = "pausing"
                self.cancel_event.set()
                self._log(job, "일시정지 중입니다. 받은 조각을 보관합니다.")
            elif job["status"] == "queued" or (job["status"] == "error" and job.get("auto_retry_at")):
                job["status"] = "paused"
                job["auto_retry_at"] = 0
        self.status.set("선택한 작업을 일시정지합니다. 완료한 조각은 보관합니다.")
        self._dirty = True
        self._save_jobs(force=True)
        self._refresh_table()
        self._refresh_details(force=True)

    def cancel(self):
        self.pause()

    def remove_jobs(self):
        selected = set(self.tree.selection())
        if not selected or self.active_id in selected:
            self.status.set("진행 중인 작업은 일시정지가 끝난 뒤 제거할 수 있습니다.")
            return
        if any(job.get("kind") == "text_zip" and job["status"] != "completed" and job["id"] not in selected
               and selected.intersection(job["archive_jobs"]) for job in self.jobs):
            self.status.set("미완성 ZIP에 필요한 회차입니다. ZIP 작업을 함께 선택하거나 먼저 제거해 주세요.")
            return
        if not messagebox.askyesno("목록에서 제거", f"선택한 {len(selected)}개 항목을 목록에서 제거할까요?\n완료된 파일은 그대로 유지됩니다.\n이어받기용 임시 조각은 함께 삭제됩니다.", parent=self.root):
            return
        if self.closing or self.active_id in selected:
            self.status.set("선택한 작업이 시작됐습니다. 일시정지가 끝난 뒤 제거해 주세요.")
            return
        for job_id in selected:
            job = self._job(job_id)
            if job is None or job_id == self.active_id:
                continue
            try:
                if job.get("kind") not in ("text", "text_zip"):
                    discard_resume(Path(job["folder"]), job_id)
            except (OSError, DownloadError) as error:
                self.status.set(f"작업을 제거하지 못했습니다: {error}")
                continue
            self.jobs.remove(job)
            self.tree.delete(job_id)
        self._dirty = True
        self._save_jobs(force=True)
        self._refresh_table()
        self._refresh_details(force=True)

    def open_folder(self):
        job = self._selected_job()
        self.folder_entry._sync_variable()
        try:
            folder = Path(job["folder"] if job else self.folder.get()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            self.status.set("저장 폴더를 열지 못했습니다. 경로를 확인해 주세요.")

    def open_program_log(self):
        try:
            if self._program_logger is None or not self.program_log_path.is_file():
                raise OSError()
            self._write_program_log("program_log_opened")
            os.startfile(str(self.program_log_path))
        except OSError:
            self.status.set(f"프로그램 로그를 열지 못했습니다. 저장 위치: {self.program_log_path}")

    def open_transfer_logs(self):
        try:
            folder = self.state_path.parent / "transfer-logs"
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            self.status.set("상세 로그 폴더를 열지 못했습니다. 경로와 권한을 확인해 주세요.")

    def open_file(self):
        job = self._selected_job()
        if not job or job["status"] != "completed" or not job.get("file"):
            return
        path = Path(job["file"]).resolve()
        try:
            suffix = {"text": ".txt", "text_zip": ".zip"}.get(job.get("kind"), ".ts")
            if path.parent != Path(job["folder"]).resolve() or path.suffix.lower() != suffix or not path.is_file():
                raise OSError()
            os.startfile(str(path))
        except OSError:
            self.status.set("완료 파일을 열지 못했습니다. 파일 위치와 연결 프로그램을 확인해 주세요.")

    def _filter_changed(self):
        self._refresh_table()
        if not self.tree.selection() and self.tree.get_children():
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
        self._refresh_details(force=True)

    def _refresh_table(self):
        states = {"진행 중": {"resolving", "downloading", "merging", "cleaning", "pausing"}, "대기": {"queued"},
                  "일시정지": {"paused"}, "완료": {"completed"}, "오류": {"error"}}
        allowed = states.get(self.filter.get())
        query = self.list_query.get().strip().casefold()
        visible = []
        rows = {}
        for job in self.jobs:
            job_id = job["id"]
            if job.get("kind") == "text_zip" and job_id != self.active_id and job["status"] != "completed":
                job["completed_indices"] = [index for index, member in enumerate(job["archive_jobs"]) if self._text_file_ready(self._job(member))]
                job["done"] = len(job["completed_indices"])
            progress = "100%" if job["status"] == "completed" else f"{100 * job['done'] / job['total']:.1f}%" if job["total"] else "—"
            running = job_id == self.active_id
            receiving = running and job["status"] == "downloading"
            state_label = "TXT 정리 대기" if job["status"] == "completed" and job.get("cleanup_pending") else self.STATE_LABELS.get(job["status"], "대기")
            verification = self._verification_job(job)
            if verification:
                state_label = "인증 필요"
            elif job["status"] == "error" and self.queue_running and self.auto_retry.get() and job.get("auto_retry_at"):
                state_label = "재시도 대기"
            values = (job["title"], state_label, progress,
                      format_bytes(job["bytes"]), format_bytes(job.get("speed", 0)) + "/s" if receiving else "—",
                      format_time(job.get("eta", 0)) if receiving and job["done"] and job.get("eta") else "—")
            tags = ("verification" if verification else job["status"],)
            rows[job_id] = (values, tags)
            if self.tree.exists(job_id):
                if self._table_rows.get(job_id) != rows[job_id]:
                    self.tree.item(job_id, values=values, tags=tags)
            else:
                self.tree.insert("", "end", iid=job_id, values=values, tags=tags)
            if (allowed is None or job["status"] in allowed) and (not query or query in job["title"].casefold()):
                visible.append(job_id)
        current = self.tree.get_children()
        if current != tuple(visible):
            hidden = set(current) - set(visible)
            if hidden:
                self.tree.detach(*hidden)
            for job_id in visible:
                self.tree.move(job_id, "", "end")
        self._table_rows = rows
        selection = self.tree.selection()
        visible_selection = tuple(job_id for job_id in selection if job_id in visible)
        if selection != visible_selection:
            self.tree.selection_set(visible_selection)
            if self.tree.focus() not in visible:
                self.tree.focus(visible_selection[0] if visible_selection else "")
        if visible:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="조건에 맞는 작업이 없습니다." if self.jobs else "영상 주소를 추가하세요. 여러 링크는 차례로 다운로드합니다.")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        active = int(self.active_id is not None)
        counts = {name: sum(job["status"] in statuses for job in self.jobs) for name, statuses in states.items()}
        counts["전체"] = len(self.jobs)
        for name, button in self.filter_buttons.items():
            button.configure(text=f"{name} {counts[name]}", style="Selected.Queue.Filter.TButton" if self.filter.get() == name else "Queue.Filter.TButton")
        self.add_button.configure(text="대기열에 추가" if active else "추가하고 시작")
        verification_count = sum(job.get("kind") == "text" and bool(self._verification_job(job)) for job in self.jobs)
        if verification_count:
            self.verification_notice_title.configure(text=f"브라우저 인증 필요 · {verification_count}개 작업")
            self.verification_notice.grid()
        else:
            self.verification_notice.grid_remove()

    def _update_actions(self):
        selected_ids = set(self.tree.selection())
        selected = [job for job in self.jobs if job["id"] in selected_ids]
        can_start = any((job["status"] in ("paused", "queued", "error") or job.get("cleanup_pending")) and job["id"] != self.active_id for job in selected)
        can_pause = any(job["status"] in ("resolving", "downloading", "merging", "cleaning", "queued")
                        or (job["status"] == "error" and job.get("auto_retry_at")) for job in selected)
        self.auto_retry_check.configure(state="disabled" if self.closing else "normal")
        self.fastest_server_check.configure(state="disabled" if self.closing else "normal")
        self.verification_notice_button.configure(state="disabled" if self.closing or self.active_id else "normal")
        job = self._selected_job()
        self.detail_button.configure(state="normal" if job and not self.closing else "disabled")
        # Reconfiguring a posted Windows menu redraws it and clears the active item.
        if self._job_menu_open:
            return
        single = len(selected) == 1
        self.job_menu.entryconfigure(0, label="재생" if not job or job.get("kind") == "video" else "파일 열기",
                                     state="normal" if single and job["status"] == "completed" and job.get("file") and not self.closing else "disabled")
        self.job_menu.entryconfigure(2, label="TXT 정리 재시도" if job and job["status"] == "completed" and job.get("cleanup_pending") else "시작 / 이어받기",
                                     state="normal" if can_start and not self.closing else "disabled")
        self.job_menu.entryconfigure(3, state="normal" if can_pause and not self.closing else "disabled")
        for index in (5, 6):
            self.job_menu.entryconfigure(index, state="normal" if single and not self.closing else "disabled")
        self.job_menu.entryconfigure(7, label="영상 주소 복사" if all(item.get("kind") == "video" for item in selected) else "원본 주소 복사",
                                     state="normal" if selected and not self.closing else "disabled")
        self.job_menu.entryconfigure(9, state="normal" if selected and all(item["id"] != self.active_id for item in selected) and not self.closing else "disabled")
        verification = bool(single and self._verification_job(job))
        if verification and self.job_menu.index("end") == 9:
            self.job_menu.add_separator()
            self.job_menu.add_command(label="브라우저 인증 열기", command=self.verify_text_job)
        elif not verification and self.job_menu.index("end") > 9:
            self.job_menu.delete(10, "end")
        if verification:
            self.job_menu.entryconfigure(11, state="normal" if not self.closing and not self.active_id else "disabled")

    def _refresh_details(self, force=False):
        job = self._selected_job()
        self._update_actions()
        if not job:
            self.saved_file = None
            self.output_folder = None
            self.video_title.set("목록에서 작업을 선택하세요" if self.jobs else "다운로드할 영상을 추가하세요")
            self.video_meta.set("목록에서 작업을 선택하면 8개 작업과 조각 상태를 볼 수 있습니다.")
            self.progress_meta.set("목록에서 작업을 선택하세요")
            self.percent.set("0%")
            self.detail_progress.configure(value=0)
            self.selection_status.set("")
            self.queue_size.configure(text="")
            self.transferred.set("0 MB")
            self.speed.set("—")
            self.remaining.set("—")
            self.file_name.set("")
        else:
            self.saved_file = Path(job["file"]) if job["status"] == "completed" and job.get("file") else None
            self.output_folder = Path(job["folder"])
            self.video_title.set(job["title"][:85] + ("…" if len(job["title"]) > 85 else ""))
            duration = format_time(job["duration"]) if job.get("duration") else "길이 확인 중"
            if job.get("kind") in ("text", "text_zip"):
                duration = "전체 회차 ZIP" if job["kind"] == "text_zip" else "소설 TXT · UTF-8"
                page = self.text_domain + urlsplit(job["url"]).path if self.text_domain else job["url"]
            else:
                page = domain_requests(job["url"], job["url"])[0][0]
            state_label = "브라우저 인증 필요" if self._verification_job(job) else "TXT 정리 대기" if job.get("cleanup_pending") and job["status"] == "completed" else self.STATE_LABELS[job['status']]
            self.video_meta.set(f"{state_label}  ·  {duration}  ·  우선 접속: {page}")
            self.progress_meta.set(f"{state_label}   ·   {duration}")
            percentage = 100 * job["done"] / job["total"] if job["total"] else 0
            self.percent.set("100%" if job["status"] == "completed" else f"{percentage:.1f}%" if job["total"] else "…" if job["id"] == self.active_id else "대기")
            self.detail_progress.configure(value=100 if job["status"] == "completed" else percentage)
            self.transferred.set(format_bytes(job["bytes"]) if job["bytes"] else "0 MB")
            self.speed.set(format_bytes(job.get("speed", 0)) + "/s" if job["id"] == self.active_id else "—")
            self.remaining.set("인증 대기" if self._verification_job(job) else "완료" if job["status"] == "completed" else format_time(job["eta"]) if job["id"] == self.active_id and job.get("eta") else "—")
            self.file_name.set(job.get('file') or job['folder'])
            self.selection_status.set(state_label + (" · " + self.percent.get() if job["status"] == "downloading" else ""))
            self.queue_state.configure(foreground={"completed": "#23704E", "error": "#A84320", "paused": "#66758A"}.get(job["status"], "#245EB8"))
            self.queue_size.configure(text=self.transferred.get())
        self._fit_queue_title()
        if not force and not self.detail_window.winfo_viewable():
            return
        count = job.get("server_count", 0) if job else 0
        self.server_summary.set("영상 조각 지도")
        self.server_info_button.configure(text=f"서버 {count}개" if count else "서버 정보")
        if job and job.get("kind") == "text":
            self.server_summary.set("소설 TXT · 회차별 본문 저장")
        elif job and job.get("kind") == "text_zip":
            self.server_summary.set("전체 회차 ZIP · 모든 TXT 완료 후 자동 압축")
        servers = job.get("servers", {}) if job else {}
        health = job.get("server_health", {}) if job else {}
        running = bool(job and job["id"] == self.active_id)
        slow_count = sum(item.get("state") == "slow" for item in health.values())
        if health:
            mode = "가장 빠른 서버 집중" if self.fastest_server_only.get() else "서버 자동 선택"
            self.server_status.set(f"{mode} · 느린 서버 {slow_count}개 회피 중 · 대체 요청 {sum(item.get('switches', 0) for item in health.values())}회"
                                   if running else f"이번 실행 · 느림 감지 {sum(item.get('slow_count', 0) for item in health.values())}회")
        else:
            self.server_status.set("")
        server_signature = (job["id"] if job else None, tuple(servers.items()), running,
                            tuple((host, tuple(item.items())) for host, item in health.items()))
        if server_signature != self._server_signature:
            for row in self.server_tree.get_children():
                if row not in servers:
                    self.server_tree.delete(row)
            for host, parts in sorted(servers.items()):
                item = health.get(host, {})
                state = {"healthy": "정상 · 회복" if item.get("slow_count") else "정상", "waiting": "아직 수신 전",
                         "recovering": "회복 확인" if item.get("active") else "재확인 대기"}.get(item.get("state"), "—")
                if item.get("state") == "slow":
                    state = f"느림 · 회피 {item.get('cooldown', 0)}초" if running else "느림 기록"
                values = (host, parts, state, format_bytes(item.get("speed", 0)) + "/s" if item.get("samples") else "—",
                          item.get("active", 0) if running else 0)
                if not self.server_tree.exists(host):
                    self.server_tree.insert("", "end", iid=host)
                self.server_tree.item(host, values=values, tags=("slow",) if item.get("state") == "slow" else ())
            self.server_empty.place_forget()
            if not servers:
                self.server_empty.place(relx=0.5, rely=0.5, anchor="center")
            self._server_signature = server_signature
        workers = {item["slot"]: item for item in job.get("workers", [])} if job else {}
        labels = {"waiting": "대기", "peer_wait": "P2P 대기", "requesting": "서버 연결", "receiving": "서버 수신", "checking": "검증·저장", "error": "오류"}
        for index in range(WORKERS):
            item = workers.get(index, {})
            part = item.get("index")
            source = item.get("source", "http" if part is not None else "")
            method = {"http": "HTTP", "p2p": "P2P", "p2p_wait": "P2P 확인"}.get(source, "—")
            state = labels.get(item.get("state"), "대기")
            if item.get("transition"):
                state = "전환·" + {"checking": "검증", "requesting": "연결", "receiving": "수신"}.get(item.get("state"), state)
            slow = source == "http" and health.get(item.get("host"), {}).get("state") == "slow"
            if source == "http" and item.get("rescue_host"):
                state = "대체 병행"
            elif slow:
                state = "느림·" + {"requesting": "연결", "receiving": "수신", "checking": "검증"}.get(item.get("state"), state)
            elif source == "http" and item.get("server_switched"):
                state = "대체·" + {"requesting": "연결", "receiving": "수신", "checking": "검증"}.get(item.get("state"), state)
            host = (item.get("host") or "—") if source == "http" else {"p2p": "P2P 피어", "p2p_wait": "HTTP 요청 전"}.get(source, "—")
            values = (str(index + 1), str(part + 1) if part is not None else "—", method, state, host if part is not None else "—",
                      format_bytes(item.get("bytes", 0)) if part is not None else "—",
                      format_bytes(item.get("speed", 0)) + "/s" if part is not None and source == "http" else "—")
            self.worker_tree.item(str(index), values=values, tags=("slow",) if slow else ())
        self._refresh_worker_host()
        self._refresh_transfer_details(job)
        self._draw_map(force=force)
        log = tuple(job["log"]) if job else ()
        log_signature = (job["id"] if job else None, log)
        if log_signature != self._log_signature:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", "\n".join(log) if log else "작업의 시작, 일시정지, 이어받기 결과가 여기에 표시됩니다.")
            self.log_text.configure(state="disabled")
            self.log_text.see("end")
            self._log_signature = log_signature

    def _refresh_worker_host(self):
        job = self._selected_job()
        selection = self.worker_tree.selection()
        if job and selection:
            item = next((item for item in job.get("workers", []) if item["slot"] == int(selection[0])), {})
            source = item.get("source", "http")
            transition = {"http_to_p2p": "HTTP → P2P 전환", "p2p_to_http": "P2P 검증 실패 → HTTP 재수신"}.get(item.get("transition"), "")
            if not item.get("host"):
                text = "선택한 작업은 대기 중입니다."
            elif source == "p2p_wait":
                text = f"P2P 조각을 기다리는 중 · 아직 HTTP 요청 전\nHTTP 대체 서버: {item['host']}"
            elif source == "p2p":
                text = f"{transition or 'P2P 조각 검증·저장'} · 연결된 피어 {job.get('p2p_peers', 0)}개\n이 작업의 HTTP 수신량: {format_bytes(item.get('http_bytes', 0))}"
            else:
                text = f"{transition or 'HTTP 서버 다운로드'} · {item['host']}"
                if item.get("rescue_host"):
                    text += f"\n다른 서버로도 수신 중: {item['rescue_host']}"
                elif item.get("server_switched"):
                    text += " · 대체 서버로 전환"
            self.worker_host.set(text)
        else:
            self.worker_host.set("작업 행을 선택하면 수신처와 전환 내역을 확인할 수 있습니다.")

    def _refresh_transfer_details(self, job):
        completed = set(job.get("completed_indices", [])) if job else set()
        p2p_completed = set(job.get("p2p_completed_indices", [])).intersection(completed) if job else set()
        http_completed = set(job.get("http_completed_indices", [])).intersection(completed) if job else set()
        unknown = completed - p2p_completed - http_completed
        running = bool(job and job["id"] == self.active_id)
        workers = job.get("workers", []) if job else []
        http_workers = [item for item in workers if item.get("index") is not None and item.get("source", "http") == "http"]
        parts = [item for item in job.get("p2p_parts", []) if item["index"] not in completed] if job and running else []
        peers = job.get("p2p_peers", 0) if job and running else 0
        p2p_state = {"initializing": "P2P 준비 중", "searching": "트래커 응답·피어 검색 중",
                     "no_peers": "트래커 정상 · 연결 가능한 피어 없음", "peer_wait": "참여자 발견 · 피어 연결 대기",
                     "tracker_error": "트래커 연결 오류 · 피어 유무 확인 불가",
                     "tracker_partial": "트래커 일부 오류 · 피어 유무 확인 제한",
                     "unavailable": "P2P 실행 불가 · HTTP로 계속", "disabled": "미사용"}.get(
            job.get("p2p_status", "disabled") if job else "disabled", "피어 연결됨")
        trackers = job.get("p2p_trackers", []) if job and running else []
        healthy = sum(item["state"] == "announced" and not item["error"] for item in trackers)
        errors = sum(bool(item["error"]) for item in trackers)
        connection_text = (f"피어 {peers}개 연결됨" if peers else p2p_state) if running else "대기"
        if trackers:
            connection_text += f" · 트래커 응답 {healthy}/{len(trackers)}개"
            if errors:
                connection_text += f" · 오류 {errors}개 (자동 재시도)"
        self.p2p_connection_status.set("P2P · " + connection_text)
        overview = {"initializing": "준비 중", "searching": "피어 검색 중", "no_peers": "연결할 피어 없음",
                    "peer_wait": "피어 연결 대기", "tracker_error": "트래커 연결 오류", "tracker_partial": "트래커 일부 오류",
                    "unavailable": "HTTP로 다운로드", "disabled": "사용 안 함"}.get(
            job.get("p2p_status", "disabled") if job else "disabled", "피어 연결됨")
        self.p2p_overview.set((f"피어 {peers}개 연결" + (" · 트래커 오류" if errors else "") if peers else overview) if running
                              else "다운로드 완료" if job and job["status"] == "completed" else "대기")
        self.p2p_overview_label.configure(foreground="#8B4600" if running and errors else "#182638")
        present_trackers = {item["url"] for item in trackers}
        for row in set(self.tracker_tree.get_children()) - present_trackers:
            self.tracker_tree.delete(row)
        for item in trackers:
            state = {"connecting": "재접속 중" if item["error"] else "연결 중", "connected": "응답 대기",
                     "announced": "정상 응답", "error": "오류 · 재시도 예정"}[item["state"]]
            detail = {"connection_failed": "트래커 접속 실패 · HTTP로 계속",
                      "disconnected": "트래커 연결 끊김 · 자동 재시도",
                      "announce_rejected": "트래커가 피어 검색 요청을 거부함"}.get(item["error"],
                "현재 응답 기준 · 연결 가능 여부는 별도" if item["remote_peers"]
                else "현재 응답 기준 · 다른 참여자 없음" if item["remote_peers"] == 0
                else "피어 검색 결과를 기다리고 있습니다.")
            values = (urlsplit(item["url"]).hostname, state,
                      f"{item['remote_peers']}개" if item["state"] == "announced" and item["remote_peers"] is not None else "—", detail)
            if item["url"] in self.tracker_tree.get_children():
                self.tracker_tree.item(item["url"], values=values, tags=("error",) if item["error"] else ())
            else:
                self.tracker_tree.insert("", "end", iid=item["url"], values=values, tags=("error",) if item["error"] else ())
        for source, name, saved, state in (
                ("http", "HTTP 서버", len(http_completed), f"처리 작업 {len(http_workers)}개 · 수신 중 {sum(item.get('state') == 'receiving' for item in http_workers)}개" if running else "대기"),
                ("p2p", "P2P 피어", len(p2p_completed), f"피어 {peers}개 · 미리받기/전달 {len(parts)}개" if peers else p2p_state if running else "대기"),
                ("unknown", "방식 미기록", len(unknown), "이전 저장 조각의 수신 방식 기록 없음" if unknown else "없음")):
            self.source_tree.item(source, values=(name,
                format_bytes(job.get(source + "_bytes", 0)) if job and source != "unknown" else "—",
                format_bytes(job.get(source + "_speed", 0)) + "/s" if job and running and source != "unknown" else "—",
                f"{saved:,}개", "  " + state))
        self.peer_summary.set(f"P2P 미리받기 · 피어 {peers}개 · 진행 {len(parts)}개 · 저장 대기 {sum(item.get('state') == 'ready' for item in parts)}개")
        self.peer_connections_heading.configure(text=f"연결된 피어 · {peers}개")
        connections = job.get("p2p_peer_details", []) if job and running else []
        existing_connections = set(self.peer_connections_tree.get_children())
        present_connections = {item["id"] for item in connections}
        for row in existing_connections - present_connections:
            self.peer_connections_tree.delete(row)
        for number, item in enumerate(connections, 1):
            address = item.get("address") or ("확인 중" if item.get("state") == "checking" else "미공개")
            values = (str(number), address, item.get("country", "조회 불가"))
            if item["id"] in existing_connections:
                self.peer_connections_tree.item(item["id"], values=values)
            else:
                self.peer_connections_tree.insert("", "end", iid=item["id"], values=values)
            self.peer_connections_tree.move(item["id"], "", "end")
        self.peer_connections_empty.place_forget()
        if not connections:
            self.peer_connections_empty.configure(text=("연결된 피어의 주소 정보가 아직 없습니다." if peers
                else "아직 연결된 피어가 없습니다.\n" + p2p_state if running else "다운로드 중인 영상을 선택하면 연결된 피어를 표시합니다."))
            self.peer_connections_empty.place(relx=.5, rely=.5, anchor="center")
        existing = set(self.peer_tree.get_children())
        present = {str(item["index"]) for item in parts}
        for row in existing - present:
            self.peer_tree.delete(row)
        for item in parts:
            row = str(item["index"])
            state = {"receiving": "피어 수신 / 전달 대기", "transferring": "수신한 조각을 전달 중", "ready": "수신 완료 · 검증·저장 대기"}.get(item["state"], "대기")
            size = format_bytes(item["bytes"]) if item["bytes"] else "—"
            if item.get("total"):
                size += " / " + format_bytes(item["total"])
            values = (item["index"] + 1, state, size)
            if row in existing:
                self.peer_tree.item(row, values=values)
            else:
                self.peer_tree.insert("", "end", iid=row, values=values)
            self.peer_tree.move(row, "", "end")
        self.peer_empty.place_forget()
        if not parts:
            self.peer_empty.place(relx=.5, rely=.5, anchor="center")

    def _draw_map(self, force=False):
        if not hasattr(self, "map_canvas") or self._destroyed:
            return
        job = self._selected_job()
        total = job["total"] if job else 0
        completed = set(job["completed_indices"]) if job else set()
        p2p_completed = set(job.get("p2p_completed_indices", [])).intersection(completed) if job else set()
        http_completed = set(job.get("http_completed_indices", [])).intersection(completed) if job else set()
        p2p_active = (set(job.get("p2p_active_indices", []))
                      | {index for index, state in job.get("segment_states", {}).items() if state == "p2p_active"}) - completed if job else set()
        active = {index for index, state in job.get("segment_states", {}).items() if state == "active"} if job else set()
        errors = {index for index, state in job.get("segment_states", {}).items() if state == "error"} if job else set()
        width, height = max(10, self.map_canvas.winfo_width()), max(10, self.map_canvas.winfo_height())
        active -= completed
        signature = (job["id"] if job else None, total, frozenset(completed), frozenset(active), frozenset(errors),
                     frozenset(p2p_completed), frozenset(http_completed), frozenset(p2p_active), width, height)
        if not force and (signature == self._map_signature or time.monotonic() - self._last_draw < 0.2):
            return
        self._map_signature = signature
        self._last_draw = time.monotonic()
        self.map_canvas.delete("all")
        if not total:
            self.part_summary.set("아직 영상 조각 정보가 없습니다.")
            self.map_summary.set("아직 영상 조각 정보가 없습니다.")
            self.map_canvas.create_text(width / 2, height / 2, text="영상 정보 확인 후 조각 지도가 표시됩니다.", fill="#697C90", font=("맑은 고딕", 9), width=max(50, width - 20))
            return
        group = max(1, math.ceil(total / 1200))
        cells = math.ceil(total / group)
        columns = max(1, math.ceil(math.sqrt(cells * width / height)))
        rows = math.ceil(cells / columns)
        cell_w, cell_h = (width - 8) / columns, min(18, (height - 8) / rows)
        for cell in range(cells):
            indices = range(cell * group, min(total, (cell + 1) * group))
            colors = Counter()
            for index in indices:
                if index in completed:
                    key = "p2p_complete" if index in p2p_completed else "http_complete" if index in http_completed else "unknown_complete"
                    colors[PART_COLORS[key]] += 1
                elif index in p2p_active and index in active:
                    colors[PART_COLORS["http_active"]] += .5
                    colors[PART_COLORS["p2p_active"]] += .5
                else:
                    key = "p2p_active" if index in p2p_active else "http_active" if index in active else "error" if index in errors else "waiting"
                    colors[PART_COLORS[key]] += 1
            x, y = 4 + cell % columns * cell_w, 4 + cell // columns * cell_h
            offset = 0
            for color, count in colors.items():
                left = x + max(1, cell_w - 2) * offset / len(indices)
                offset += count
                self.map_canvas.create_rectangle(left, y, x + max(1, cell_w - 2) * offset / len(indices),
                                                 y + max(1, cell_h - 2), fill=color, outline="")
        self.part_summary.set(f"저장 {len(completed):,}/{total:,} · HTTP {len(http_completed):,} · P2P 완료 {len(p2p_completed):,} · 미기록 {len(completed - p2p_completed - http_completed):,}\n"
                              f"HTTP 요청/수신 {len(active)} · P2P 수신/검증 {len(p2p_active)} · 동시 {len(active & p2p_active)}"
                              + (f" · 한 칸 최대 {group}개" if group > 1 else ""))
        self.map_summary.set(f"{len(completed):,} / {total:,} 조각 저장"
                             + (f" · 한 칸당 최대 {group}조각" if group > 1 else ""))

    def poll(self):
        if self._destroyed:
            return
        self._poll_domain_check()
        self._poll_text_domain_check()
        table_changed = False
        for _ in range(600):
            try:
                job_id, run_id, kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            job = self._job(job_id)
            if not job or job.get("run_id") != run_id or self.active_id != job_id:
                continue
            if kind not in ("workers", "segment", "file"):
                table_changed = True
            if kind == "status":
                if job["status"] != "pausing":
                    if "합치" in value or job.get("kind") == "text_zip":
                        job["status"] = "merging"
                    self._log(job, value)
            elif kind == "info":
                job.update(title=value["title"], duration=value["duration"], total=value["segments"],
                           server_count=value.get("server_count", 0), servers=value.get("servers", {}),
                           http_bytes=0, p2p_bytes=0, http_speed=0, p2p_speed=0,
                           p2p_peers=0, p2p_active_indices=[], p2p_parts=[], p2p_peer_details=[], p2p_status="disabled", p2p_trackers=[], server_health={})
            elif kind == "file":
                job["file"] = str(value)
            elif kind == "parts":
                job.update(total=value["total"], completed_indices=list(value["completed"]), done=len(value["completed"]))
                job["p2p_completed_indices"] = sorted((set(job.get("p2p_completed_indices", []))
                    | set(value.get("p2p_completed", []))).intersection(value["completed"]))
                job["http_completed_indices"] = sorted((set(job.get("http_completed_indices", []))
                    | set(value.get("http_completed", []))).intersection(value["completed"]) - set(job["p2p_completed_indices"]))
                job["segment_states"] = {}
                if value["completed"]:
                    self._log(job, f"저장된 {len(value['completed'])}개 조각을 확인했습니다. 나머지를 이어받습니다.")
            elif kind == "segment":
                index = value["index"]
                job["segment_states"][index] = value["state"]
                if value["state"] == "complete" and index not in job["completed_indices"]:
                    job["completed_indices"].append(index)
                if value["state"] == "complete" and value.get("source") in ("p2p", "http"):
                    for source in ("p2p", "http"):
                        key = source + "_completed_indices"
                        sources = set(job.get(key, []))
                        sources.add(index) if value["source"] == source else sources.discard(index)
                        job[key] = sorted(sources)
                    transition = {"http_to_p2p": "HTTP → P2P", "p2p_to_http": "P2P → HTTP"}.get(value.get("transition"))
                    if transition:
                        self._log(job, f"{index + 1}번 조각: {transition} 전환 후 검증·저장 완료")
            elif kind == "workers":
                job["workers"] = value
            elif kind == "progress":
                job.update(done=value["done"], total=value["total"], bytes=value["bytes"], eta=value["eta"])
                if job["status"] != "pausing":
                    job["status"] = "merging" if job.get("kind") == "text_zip" else "downloading"
            elif kind == "transfer":
                if (job.get("p2p_status") != value.get("p2p_status", "disabled")
                        or job.get("p2p_peers", 0) != value.get("p2p_peers", 0)
                        or job.get("p2p_trackers", []) != value.get("p2p_trackers", [])):
                    self._write_program_log("p2p_state", job=job_id, status=value.get("p2p_status", "disabled"),
                                            peers=value.get("p2p_peers", 0), trackers=value.get("p2p_trackers", []))
                job["speed"] = value["speed"]
                job.update(http_bytes=value.get("http_bytes", 0), p2p_bytes=value.get("p2p_bytes", 0),
                           http_speed=value.get("http_speed", 0), p2p_speed=value.get("p2p_speed", 0),
                           server_health=value.get("server_health", {}),
                           p2p_peers=value.get("p2p_peers", 0), p2p_status=value.get("p2p_status", "disabled"),
                           p2p_peer_details=value.get("p2p_peer_details", []),
                           p2p_trackers=value.get("p2p_trackers", []),
                           p2p_active_indices=value.get("p2p_active_indices", []), p2p_parts=value.get("p2p_parts", []))
            elif kind == "complete":
                job.update(status="completed", file=str(value), done=job["total"], speed=0, eta=0,
                           completed_indices=list(range(job["total"])), segment_states={}, workers=[])
                self._log(job, "전체 회차 ZIP 검증이 완료됐습니다. TXT 정리를 준비합니다." if job.get("kind") == "text_zip" else "소설 본문을 TXT로 저장했습니다." if job.get("kind") == "text" else "다운로드 완료. 모든 조각을 순서대로 저장했습니다.")
                if job.get("kind") == "text_zip":
                    job["cleanup_pending"] = True
                    self._dirty = True
                    job["cleanup_allowed"] = self._save_jobs(force=True)
                    job["status"] = "cleaning" if job["cleanup_allowed"] else "completed"
                    job["cleanup_gate"].set()
            elif kind == "cleanup":
                deleted = set(value["deleted"])
                for member in tuple(self.jobs):
                    if member["id"] in deleted:
                        self.jobs.remove(member)
                        if self.tree.exists(member["id"]):
                            self.tree.delete(member["id"])
                if not self.tree.selection():
                    self.tree.selection_set(job_id)
                    self.tree.focus(job_id)
                job.update(status="completed", cleanup_pending=bool(value["errors"]), speed=0)
                detail = f"전체 {job['total']}회차 ZIP 완료 · TXT {len(deleted)}개와 해당 목록 행 정리"
                if value["errors"]:
                    detail += " · 정리 대기: " + value["errors"][0]
                else:
                    detail += " · 보관한 원본 TXT는 목록에 유지합니다."
                self._log(job, detail)
                self._write_text_domain_log("text_zip_cleanup", job=job["id"], deleted=len(deleted), pending=job["cleanup_pending"])
                self._dirty = True
                self._save_jobs(force=True)
            elif kind == "paused":
                job.update(status="paused", speed=0, workers=[], segment_states={})
                self._log(job, "ZIP 압축을 일시정지했습니다. 원본 TXT는 보관합니다." if job.get("kind") == "text_zip" else "일시정지했습니다. 다시 시작하면 이 회차의 본문을 다시 불러옵니다." if job.get("kind") == "text" else "일시정지했습니다. 완료한 조각을 보관했습니다.")
            elif kind == "verification":
                self.queue_running = False
                job.update(status="error", speed=0, workers=[], verification_url=value)
                self._log(job, "브라우저 인증이 필요해 다운로드와 자동 재시도를 멈췄습니다. ‘브라우저 인증 열기’에서 인증을 완료하면 이어서 다운로드합니다.")
            elif kind == "error":
                job.update(status="error", speed=0, workers=[], auto_retry_at=0)
                self._log(job, str(value))
                self.status.set(f"{job['title']} · {value}")
            elif kind == "idle":
                if self.active_id == job_id:
                    self.active_id = None
                    job["workers"] = []
                    job["p2p_peers"] = 0
                    job["p2p_active_indices"] = []
                    job["p2p_parts"] = []
                    job["p2p_trackers"] = []
                    job["http_speed"] = job["p2p_speed"] = 0
                self._dirty = True
                if job["status"] == "completed" and job.get("kind") == "video":
                    self._resume_cleanup_pending.add(job_id)
                if self._save_jobs(force=True):
                    self._cleanup_resume_caches()
            if kind not in ("transfer", "workers"):
                self._dirty = True
        if table_changed:
            self._refresh_table()
        if self._program_log_error and not self._program_log_warned:
            self._program_log_warned = True
            self.status.set(self._program_log_error + " 다운로드는 계속합니다.")
        self._refresh_details()
        if self._save_jobs():
            self._cleanup_resume_caches()
        if self.closing and not self.active_id and (not self.worker or not self.worker.is_alive()):
            self._dirty = True
            self._save_jobs(force=True)
            self.root.destroy()
            return
        self._start_next()
        self.root.after(100, self.poll)

    def close(self):
        self._write_program_log("program_shutdown_requested", active_job=self.active_id)
        self.closing = True
        self._cancel_domain_check()
        if self.search_dialog is not None:
            self.search_dialog.close()
        self.main_tabs.select(self.download_tab)
        self.queue_running = False
        if self.active_id:
            job = self._job(self.active_id)
            job["status"] = "pausing"
            self.cancel_event.set()
            self.status.set("이어받을 수 있도록 작업을 정리한 뒤 창을 닫습니다…")
            self._dirty = True
            self._save_jobs(force=True)
            self._refresh_details(force=True)
        else:
            self._dirty = True
            self._save_jobs(force=True)
            self.root.destroy()



def main():
    if os.name == "nt":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    try:
        app = DownloaderApp(root)
    except (DownloadError, OSError) as error:
        messagebox.showerror("TVMON Download Manager", str(error), parent=root)
        root.destroy()
        return
    previous_thread_hook = threading.excepthook
    def thread_error(args):
        app._write_program_log("thread_error", level=logging.ERROR, worker=args.thread.name,
                                detail="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        if sys.stderr is not None:
            previous_thread_hook(args)
    threading.excepthook = thread_error
    try:
        root.mainloop()
    except Exception:
        app._write_program_log("program_error", level=logging.ERROR, detail=traceback.format_exc())
        raise
    finally:
        threading.excepthook = previous_thread_hook
        app._close_program_log()


if __name__ == "__main__":
    sys.modules.setdefault("tvmon_downloader", sys.modules[__name__])
    main()

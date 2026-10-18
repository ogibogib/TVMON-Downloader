"""TVMON title search and complete episode lists from public catalogue pages."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import re
import threading
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from tvmon_domains import CANONICAL_ORIGIN, canonical_site_url, domain_requests, normalize_domain
from tvmon_downloader import BrowserVerificationRequired, Cancelled, DownloadError, _fetch_single, check_cancel, fetch, parse_page_url
from tvmon_http import BrowserChallengeError, HttpStatusError, TransferCancelled, TransferError


ORIGIN = CANONICAL_ORIGIN


def _content_url(value: str, base_url: str = ORIGIN + "/") -> str:
    try:
        parsed = urlsplit(canonical_site_url(value, base_url=base_url))
        valid = re.fullmatch(r"/[A-Za-z0-9_]+/[0-9]+/?", parsed.path)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DownloadError("올바른 TVMON 작품 링크를 찾지 못했습니다.")
    return ORIGIN + parsed.path.rstrip("/")


def _thumbnail(value: str, base_url: str = ORIGIN + "/") -> str:
    if not value:
        return ""
    try:
        value = urljoin(base_url, value)
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                and not parsed.username and not parsed.password):
            return value
    except (TypeError, ValueError):
        pass
    return ""


def _text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", value)).split())


def search_catalog(query: str, cancel: threading.Event, page: int = 1, *, origin: str | None = None) -> tuple[list[dict], bool]:
    """Read one valid search page from origin, or try registered origins in order."""
    check_cancel(cancel)
    query = query.strip()
    if not query:
        raise DownloadError("검색할 작품 이름을 입력해 주세요.")
    if not isinstance(page, int) or page < 1:
        raise DownloadError("검색 페이지 번호가 올바르지 않습니다.")
    failures = []
    candidates = domain_requests(ORIGIN, ORIGIN + "/search?" + urlencode({"stx": query}))
    if origin is not None:
        candidates = [(domain, referer) for domain, referer in candidates if domain == origin]
        if not candidates:
            raise DownloadError("검색할 도메인이 현재 등록된 목록에 없습니다.")
    for domain, referer in candidates:
        check_cancel(cancel)
        host = urlsplit(domain).hostname
        if host.removeprefix("www.") == "tvmon1.com":
            endpoint = "/bbs/ajax.searchList.php"
            params = {"sfl": "wr_subject", "stx": query, "sop": "and", "sst": "subIdx", "page": page}
        else:
            endpoint = "/bbs/search.php"
            params = {"ajax": 1, "stx": query, "sst": "subIdx", "page": page}
        url = domain + endpoint + "?" + urlencode(params)
        try:
            body, final_url = _fetch_single(url, cancel, referer, limit=8 * 1024 * 1024, retry=False)
            check_cancel(cancel)
            try:
                final = urlsplit(canonical_site_url(final_url))
            except (TypeError, ValueError):
                raise DownloadError("검색 응답이 등록된 사이트에서 벗어났습니다.") from None
            if final.path != endpoint:
                raise DownloadError("검색 응답이 요청한 검색 API에서 벗어났습니다.")
            payload = json.loads(body)
            rows, pages = payload["list"], payload["pages"]
            if not isinstance(rows, list) or not isinstance(pages, str):
                raise ValueError()
            results = []
            seen = set()
            for row in rows:
                check_cancel(cancel)
                title = _text(row["wr_subject"])
                content_url = _content_url(row["href"], final_url)
                if not title:
                    raise ValueError()
                if content_url in seen:
                    continue
                seen.add(content_url)
                thumb = row.get("img") or (row.get("thumb") if row.get("bo_table") == "ani" else row.get("wr_4"))
                category = _text(row.get("board_name") or "")
                meta = " · ".join(part for part in (category, str(row.get("wr_6") or "")[:4]) if part)
                hit_match = re.match(r"[+-]?[0-9]+", _text(str(row.get("wr_hit") or "")))
                hits = int(hit_match[0]) if hit_match else 0
                results.append({"title": title, "url": content_url,
                                "thumbnail": _thumbnail(thumb, final_url) if thumb else "", "meta": meta,
                                "views": f"{hits:,}" if hits > 0 else "", "category": category})
            next_pages = re.findall(r"[?&]page=([0-9]+)(?![0-9])", html.unescape(pages))
            return results, any(int(value) > page for value in next_pages)
        except (ValueError, KeyError, TypeError, AttributeError):
            failures.append(f"{host}: 작품 검색 결과를 읽지 못했습니다.")
        except DownloadError as error:
            failures.append(f"{host}: {error}")
    check_cancel(cancel)
    if origin is not None:
        raise DownloadError(failures[0])
    raise DownloadError("등록된 도메인에서 작품 검색 결과를 확인하지 못했습니다.\n" + "\n".join(failures))


class _EpisodeParser(HTMLParser):
    """Read the full #other_list, excluding the player's shortened next-up list."""

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, content_url: str, page_url: str | None = None):
        super().__init__(convert_charrefs=True)
        self.content_url = content_url
        self.page_url = page_url or content_url
        self.stack = []
        self.list_depth = 0
        self.list_complete = False
        self.row_depth = 0
        self.title_depth = 0
        self.date_depth = 0
        self.row = None
        self.rows = []
        self.pages = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in self._VOID_TAGS:
            self.stack.append(tag)
        depth = len(self.stack)
        classes = (attrs.get("class") or "").split()
        if attrs.get("id") == "other_list":
            self.list_depth = depth
        if not self.list_depth:
            return
        if tag == "li" and "searchText" in classes:
            if self.row is not None:
                raise DownloadError("회차 목록 형식이 올바르지 않습니다.")
            self.row = {"title": "", "url": "", "thumbnail": "", "meta": ""}
            self.row_depth = depth
        if tag == "a" and "pg_page" in classes:
            target = urljoin(self.page_url, attrs.get("href") or "")
            if _content_url(target) != self.content_url:
                raise DownloadError("회차 목록의 다음 페이지 주소가 올바르지 않습니다.")
            page = parse_qs(urlsplit(target).query).get("page", [""])[0]
            if not page.isdigit() or int(page) < 1:
                raise DownloadError("회차 목록의 다음 페이지 번호가 올바르지 않습니다.")
            self.pages.add(int(page))
        if self.row is None:
            return
        if tag == "a" and "title" in classes:
            try:
                target = canonical_site_url(attrs.get("href") or "", base_url=self.page_url)
            except (TypeError, ValueError):
                raise DownloadError("올바른 회차 링크를 찾지 못했습니다.") from None
            episode_url, board, post, _ = parse_page_url(target)
            if ORIGIN + f"/{board}/{post}" != self.content_url:
                raise DownloadError("다른 작품의 회차 링크가 포함되어 있습니다.")
            self.row["url"] = episode_url
            self.title_depth = depth
        if tag == "img":
            self.row["thumbnail"] = _thumbnail(attrs.get("data-src") or attrs.get("data-original") or attrs.get("src") or "", self.page_url)
        if "date" in classes:
            self.date_depth = depth

    def handle_data(self, data):
        if self.row is not None:
            if self.title_depth:
                self.row["title"] += data
            if self.date_depth:
                self.row["meta"] += data

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.row is not None and depth == self.row_depth:
            self.row["title"] = " ".join(self.row["title"].split())
            self.row["meta"] = " ".join(self.row["meta"].split())
            if not self.row["title"] or not self.row["url"]:
                raise DownloadError("회차 이름 또는 링크를 읽지 못했습니다.")
            self.rows.append(self.row)
            self.row = None
            self.row_depth = self.title_depth = self.date_depth = 0
        if depth == self.title_depth:
            self.title_depth = 0
        if depth == self.date_depth:
            self.date_depth = 0
        if depth == self.list_depth:
            self.list_complete = True
            self.list_depth = 0
        del self.stack[depth - 1:]


def list_episodes(content_url: str, cancel: threading.Event) -> list[dict]:
    """Return every episode, oldest first, following advertised list pages."""
    content_url = _content_url(content_url)
    pending = {1}
    visited = set()
    seen = set()
    rows = []
    while pending:
        check_cancel(cancel)
        page = min(pending)
        pending.remove(page)
        visited.add(page)
        url = content_url + ("?" + urlencode({"page": page}) if page > 1 else "")
        body, final_url = fetch(url, cancel, content_url, limit=16 * 1024 * 1024)
        check_cancel(cancel)
        if _content_url(final_url) != content_url:
            raise DownloadError("회차 목록이 다른 작품으로 이동했습니다.")
        parser = _EpisodeParser(content_url, final_url)
        try:
            parser.feed(body.decode("utf-8"))
            parser.close()
        except UnicodeError:
            raise DownloadError("회차 목록의 글자를 읽지 못했습니다.") from None
        if not parser.list_complete or not parser.rows:
            raise DownloadError("이 작품의 전체 회차 목록을 찾지 못했습니다.")
        added = 0
        for row in parser.rows:
            if row["url"] not in seen:
                rows.append(row)
                seen.add(row["url"])
                added += 1
        if not added:
            raise DownloadError("회차 목록의 다음 페이지가 반복됩니다. 전체 목록을 확인하지 못했습니다.")
        pending.update(parser.pages - visited)
    check_cancel(cancel)
    rows.reverse()
    return rows


class _TextSearchParser(HTMLParser):
    """Read B's search cards and pager, excluding navigation and recommendations."""

    def __init__(self, origin, query):
        super().__init__(convert_charrefs=True)
        self.origin, self.query = origin, query
        self.stack = []
        self.grid_depth = self.form_depth = self.row_depth = 0
        self.fields = {}
        self.row = None
        self.rows = []
        self.pages = set()
        self.search_form = False
        self.empty = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in _EpisodeParser._VOID_TAGS:
            self.stack.append(tag)
        depth = len(self.stack)
        classes = (attrs.get("class") or "").split()
        if tag == "form" and "search-page-form" in classes:
            self.form_depth = depth
        if self.form_depth and tag == "input" and attrs.get("name") == "q":
            self.search_form = attrs.get("value", "").strip() == self.query
        if "ep-empty" in classes:
            self.empty = True
        if "search-results-grid" in classes:
            self.grid_depth = depth
        if tag == "a" and any(value in classes for value in ("pager-num", "pager-btn")):
            target = urlsplit(urljoin(self.origin, attrs.get("href") or ""))
            params = parse_qs(target.query)
            number = params.get("page", [""])[0]
            if (f"{target.scheme}://{target.netloc}" == self.origin and target.path == "/search"
                    and params.get("q") == [self.query] and number.isdigit()):
                self.pages.add(int(number))
        if self.grid_depth and tag == "a" and "card" in classes:
            target = urlsplit(urljoin(self.origin, attrs.get("href") or ""))
            if (f"{target.scheme}://{target.netloc}" != self.origin or target.query or target.fragment
                    or not re.fullmatch(r"/[a-zA-Z0-9_-]+/[0-9]+/?", target.path)):
                raise DownloadError("B 검색 결과의 작품 주소가 올바르지 않습니다.")
            if self.row is not None:
                raise DownloadError("B 검색 결과 형식이 올바르지 않습니다.")
            self.row = {"title": "", "url": self.origin + target.path.rstrip("/"), "thumbnail": "",
                        "category": "", "genre": "", "episodes": "", "status": ""}
            self.row_depth = depth
        if self.row is None:
            return
        for class_name, field in (("subject", "title"), ("search-kind-badge", "category"),
                                  ("genre", "genre"), ("ep", "episodes"), ("badge", "status")):
            if class_name in classes:
                self.fields[depth] = field
        if tag == "img" and not self.row["thumbnail"]:
            self.row["thumbnail"] = _thumbnail(attrs.get("data-src") or attrs.get("src") or "", self.origin + "/")

    def handle_data(self, data):
        if self.row is not None:
            for field in self.fields.values():
                self.row[field] += data

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.row is not None and depth == self.row_depth:
            for field in ("title", "category", "genre", "episodes", "status"):
                self.row[field] = " ".join(self.row[field].split())
            if not self.row["title"] or not self.row["category"]:
                raise DownloadError("B 검색 결과의 작품명 또는 분류를 읽지 못했습니다.")
            # An absent completion badge does not establish that a work is ongoing.
            self.row["status"] = "완결" if self.row["status"] == "완결" else ""
            self.row["meta"] = " · ".join(self.row[field] for field in ("category", "status", "episodes") if self.row[field])
            self.rows.append(self.row)
            self.row = None
            self.row_depth = 0
        self.fields = {level: field for level, field in self.fields.items() if level < depth}
        if depth == self.grid_depth:
            self.grid_depth = 0
        if depth == self.form_depth:
            self.form_depth = 0
        del self.stack[depth - 1:]


def search_text_catalog(query: str, origin: str, cancel: threading.Event, page: int = 1,
                        *, browser_fetch, visible: bool = False) -> tuple[list[dict], bool]:
    """Search B using the manager's authenticated Chrome session and current domain."""
    check_cancel(cancel)
    query = query.strip()
    if not query:
        raise DownloadError("검색할 작품 이름을 입력해 주세요.")
    if not isinstance(page, int) or page < 1:
        raise DownloadError("검색 페이지 번호가 올바르지 않습니다.")
    origin = normalize_domain(origin)
    url = origin + "/search?" + urlencode({"q": query, "kind": "novel", "field": "title", "match": "contains", "page": page})
    try:
        body, final_url = browser_fetch(url, cancel, limit=8 * 1024 * 1024, visible=visible)
    except TransferCancelled:
        raise Cancelled() from None
    except BrowserChallengeError as error:
        raise BrowserVerificationRequired(error.url) from None
    except HttpStatusError as error:
        raise DownloadError(f"B 검색 서버가 HTTP {error.status} 오류를 반환했습니다.") from None
    except (TransferError, TimeoutError, OSError) as error:
        check_cancel(cancel)
        raise DownloadError(f"B 검색 페이지에 접속하지 못했습니다. {error}") from None
    check_cancel(cancel)
    final = urlsplit(final_url)
    if (f"{final.scheme}://{final.netloc}" != origin or final.path != "/search"
            or parse_qs(final.query).get("q") != [query]):
        raise DownloadError("B 검색 응답이 요청한 검색 페이지에서 벗어났습니다. B 접속 주소를 다시 확인해 주세요.")
    parser = _TextSearchParser(origin, query)
    try:
        parser.feed(body.decode("utf-8"))
        parser.close()
    except UnicodeError:
        raise DownloadError("B 검색 결과의 글자를 읽지 못했습니다.") from None
    if not parser.search_form or parser.row is not None or (not parser.rows and not parser.empty):
        raise DownloadError("B 검색 결과 형식을 확인하지 못했습니다. 접속 상태를 확인한 뒤 다시 검색해 주세요.")
    rows = list({row["url"]: row for row in parser.rows}.values())
    return rows, any(number > page for number in parser.pages)


class _NovelPageParser(HTMLParser):
    """Read the novel heading and the server-provided initial episode window."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.info_depth = self.title_depth = self.count_depth = 0
        self.title = self.count = ""
        self.script = False
        self.packets = []
        self.episode_links = {}
        self.episode_id = None
        self.episode_depth = 0
        self.episode_disabled = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in _EpisodeParser._VOID_TAGS:
            self.stack.append(tag)
        depth = len(self.stack)
        classes = (attrs.get("class") or "").split()
        if "nd-info" in classes:
            self.info_depth = depth
        if self.info_depth and tag == "h1":
            self.title_depth = depth
        if "novel-episode-head" in classes:
            self.count_depth = depth
        if tag == "script":
            self.script = True
        if "novel-ep-row" in classes:
            self.episode_id = attrs.get("data-episode-id")
            self.episode_depth = depth
            self.episode_disabled = "novel-ep--not-ready" in classes
            self.episode_links[self.episode_id] = ""
        if self.episode_id and tag == "a" and "novel-ep-link" in classes:
            if not self.episode_disabled and attrs.get("aria-disabled") != "true":
                self.episode_links[self.episode_id] = attrs.get("href") or ""

    def handle_data(self, data):
        if self.title_depth:
            self.title += data
        if self.count_depth:
            self.count += data
        marker = "self.__next_f.push("
        if self.script and marker in data:
            position = 0
            while (position := data.find(marker, position)) >= 0:
                position += len(marker)
                try:
                    packet, position = json.JSONDecoder().raw_decode(data, position)
                except ValueError:
                    break
                if isinstance(packet, list) and len(packet) > 1 and packet[0] == 1 and isinstance(packet[1], str):
                    self.packets.append(packet[1])

    def handle_endtag(self, tag):
        if tag == "script":
            self.script = False
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if depth == self.episode_depth:
            self.episode_id = None
            self.episode_depth = 0
        for name in ("info_depth", "title_depth", "count_depth"):
            if getattr(self, name) == depth:
                setattr(self, name, 0)
        del self.stack[depth - 1:]


def list_text_episodes(content_url: str, cancel: threading.Event, *, browser_fetch, visible=False) -> list[dict]:
    """Follow the site's older/newer cursors and return every novel episode in order."""
    parsed = urlsplit(content_url)
    origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
    match = re.fullmatch(r"/novel/([0-9]+)/?", parsed.path)
    if not match or parsed.query or parsed.fragment:
        raise DownloadError("TXT 회차 목록은 B 소설 작품에서 지원합니다.")
    work = match[1]
    content_url = origin + f"/novel/{work}"

    def read(url, show=False):
        check_cancel(cancel)
        try:
            body, final_url = browser_fetch(url, cancel, limit=8 * 1024 * 1024, visible=show)
        except TransferCancelled:
            raise Cancelled() from None
        except BrowserChallengeError as error:
            raise BrowserVerificationRequired(error.url) from None
        except (TransferError, TimeoutError, OSError) as error:
            check_cancel(cancel)
            raise DownloadError(f"B 회차 목록을 불러오지 못했습니다. {error}") from None
        check_cancel(cancel)
        final = urlsplit(final_url)
        if f"{final.scheme}://{final.netloc}" != origin or final.path != urlsplit(url).path:
            raise DownloadError("회차 목록 응답이 요청한 작품에서 벗어났습니다.")
        return body

    parser = _NovelPageParser()
    try:
        parser.feed(read(content_url, visible).decode("utf-8"))
        parser.close()
        payload = "".join(parser.packets)
        root = re.search(r'"rootId"\s*:\s*"novel-episode-list-' + work + r'"', payload)
        start = re.search(r'"initial"\s*:\s*', payload[root.end():]) if root else None
        if not start:
            raise ValueError()
        window, _ = json.JSONDecoder().raw_decode(payload, root.end() + start.end())
        title = " ".join(parser.title.split())
        count = re.search(r"([0-9][0-9,]*)\s*화", parser.count)
        if not title or not count:
            raise ValueError()
        expected = int(count[1].replace(",", ""))
    except (ValueError, UnicodeError, AttributeError):
        raise DownloadError("소설 작품의 회차 목록 형식을 읽지 못했습니다.") from None
    rows, pending, visited, seen = {}, [], set(), set()

    def consume(data, direction=None):
        if (not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("items"), list)
                or type(data.get("hasOlder")) is not bool or type(data.get("hasNewer")) is not bool):
            raise ValueError()
        added = 0
        for item in data["items"]:
            check_cancel(cancel)
            episode, number = item["id"], item["number"]
            if not isinstance(episode, str) or not re.fullmatch(r"[0-9]+", episode) or type(number) is not int or number < 0:
                raise ValueError()
            label, heading = _text(item["episodeLabel"]), _text(item["title"])
            if not label:
                raise ValueError()
            # Numeric display labels can lag behind the actual episode number.
            if re.fullmatch(r"[0-9]+\s*화", label):
                if heading == label:
                    heading = ""
                label = f"{number}화"
            url = content_url + "/" + episode
            if url not in seen:
                seen.add(url)
                added += 1
            if type(item.get("isNotReady")) is not bool:
                raise ValueError()
            available = not item["isNotReady"]
            if direction is None:
                if episode not in parser.episode_links:
                    raise ValueError()
                link = parser.episode_links[episode]
                available = available and bool(link) and urljoin(content_url, link) == url
            if not available:
                rows.pop(url, None)
                continue
            if url not in rows:
                rows[url] = {"title": label if not heading or heading == label else f"{label} · {heading}",
                             "url": url, "thumbnail": "", "meta": _text(item.get("publishedAtLabel") or ""),
                             "number": number, "series_title": title, "kind": "text"}
        for name in ((direction,) if direction else ("older", "newer")):
            if data["has" + name.capitalize()]:
                cursor = data.get(name + "Cursor")
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                    raise ValueError()
                if (name, cursor) in visited:
                    raise DownloadError("소설 회차 목록의 다음 요청이 반복됩니다. 전체 목록을 확인하지 못했습니다.")
                pending.append((name, cursor))
        if direction and not added:
            raise DownloadError("소설 회차 목록이 반복됩니다. 전체 목록을 확인하지 못했습니다.")

    try:
        consume(window)
        while pending:
            direction, cursor = pending.pop(0)
            visited.add((direction, cursor))
            url = origin + f"/api/novel/{work}/episodes/window?" + urlencode({"direction": direction, "cursor": cursor})
            consume(json.loads(read(url)), direction)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DownloadError("소설 회차 목록의 응답 형식을 읽지 못했습니다.") from None
    if len(seen) != expected:
        raise DownloadError(f"소설 회차 {len(seen)} / {expected}개를 확인했습니다. 전체 목록을 다시 불러와 주세요.")
    check_cancel(cancel)
    return sorted(rows.values(), key=lambda row: (row["number"], int(row["url"].rsplit("/", 1)[-1])))

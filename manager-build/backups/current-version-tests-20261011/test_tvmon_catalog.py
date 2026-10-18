"""Catalogue parsing checks using small fixtures shaped like the public site."""

import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import tvmon_catalog as catalog
import tvmon_downloader as downloader
from tvmon_downloader import Cancelled, DownloadError


CONTENT = "https://tvmon1.com/world/10180"
API = "https://tvmon1.com/bbs/ajax.searchList.php"
MIRROR = "https://tvwiki52.net"


def result(title="삼국지 &amp; 영웅", href="/world/10180"):
    return {"wr_subject": title, "href": href, "wr_4": "https://images.example/poster.webp",
            "board_name": "해외드라마", "wr_6": "2010-05-02", "wr_hit": "5975"}


def episode(number, *, url=None):
    url = url or f"{CONTENT}/{number}"
    return (f'<li class="searchText 삼국지 {number}화" data-ep-idx="{number}">'
            f'<a class="ep-link" href="{url}"><div><img src="/lazy.svg" '
            f'data-src="https://images.example/{number}.png"></div></a>'
            '<div class="date">10.10.2026</div>'
            f'<a class="title" href="{url}"><div class="clamp"> 삼국지 {number}화 </div></a></li>')


def episode_page(*numbers, pagination=""):
    # The player overlay deliberately includes a link absent from the full list.
    return (f'<html><div id="epListScroll"><a class="ep-item" href="{CONTENT}/999">999화</a></div>'
            '<div id="other_list"><h2>전체회차</h2><ul>'
            + "".join(episode(number) for number in numbers) + '</ul>' + pagination + '</div></html>').encode()


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.domains = self.enterContext(patch("tvmon_domains.get_domains", return_value=[catalog.ORIGIN, MIRROR]))

    def search(self, rows, pages="", page=1):
        body = json.dumps({"list": rows, "pages": pages}).encode()
        with patch.object(catalog, "_fetch_single", return_value=(body, API)) as request:
            response = catalog.search_catalog(" 삼국지 ", self.cancel, page)
        return response, request

    def test_search_decodes_title_metadata_and_pagination(self):
        (rows, more), request = self.search([result()], '<a href="/bbs/search.php?stx=x&amp;page=2">2</a>')
        self.assertEqual(rows, [{"title": "삼국지 & 영웅", "url": CONTENT,
                                "thumbnail": "https://images.example/poster.webp", "meta": "해외드라마 · 2010",
                                "views": "5,975", "category": "해외드라마"}])
        self.assertTrue(more)
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["stx"], ["삼국지"])
        self.assertEqual(query["page"], ["1"])
        self.assertEqual(urlsplit(request.call_args.args[0]).path, "/bbs/ajax.searchList.php")
        self.assertEqual(query["sfl"], ["wr_subject"])
        self.assertEqual(query["sop"], ["and"])
        self.assertNotIn("ajax", query)

    def test_current_site_search_poster_category_and_likes_are_not_views(self):
        self.domains.return_value = [MIRROR, catalog.ORIGIN]
        row = {"wr_subject": "스캔들", "href": "/drama/4147", "img": "https://images.example/current.webp",
               "board_name": "드라마", "sub_content": "작품 소개", "wr_good": 14}
        body = json.dumps({"list": [row], "pages": '<a href="?page=3">3</a>', "total_page": 3}).encode()
        with patch.object(catalog, "_fetch_single", return_value=(body, MIRROR + "/bbs/search.php")) as request:
            rows, more = catalog.search_catalog("스캔들", self.cancel)
        self.assertEqual(rows, [{"title": "스캔들", "url": "https://tvmon1.com/drama/4147",
                                "thumbnail": "https://images.example/current.webp", "meta": "드라마",
                                "views": "", "category": "드라마"}])
        self.assertTrue(more)
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query),
                         {"ajax": ["1"], "stx": ["스캔들"], "sst": ["subIdx"], "page": ["1"]})

    def test_tvwiki_response_from_wrong_search_endpoint_is_rejected(self):
        self.domains.return_value = [MIRROR]
        body = json.dumps({"list": [result("관련 없는 제목")], "pages": ""}).encode()
        with patch.object(catalog, "_fetch_single", return_value=(body, MIRROR + "/bbs/ajax.searchList.php")):
            with self.assertRaises(DownloadError):
                catalog.search_catalog("스캔들", self.cancel)

    def test_category_is_normalized_and_independent_of_release_year(self):
        row = result()
        row["board_name"] = " <span>추억의&nbsp; 예능</span> "
        row["wr_6"] = "2026-10-10"
        (rows, _), _ = self.search([row])
        self.assertEqual(rows[0]["category"], "추억의 예능")
        self.assertEqual(rows[0]["meta"], "추억의 예능 · 2026")
        row.pop("board_name")
        (rows, _), _ = self.search([row])
        self.assertEqual(rows[0]["category"], "")
        self.assertEqual(rows[0]["meta"], "2026")

    def test_mirror_search_links_are_canonical_and_relative_poster_uses_response_host(self):
        self.domains.return_value = [MIRROR, catalog.ORIGIN]
        row = result(href=MIRROR + "/world/10180")
        row["wr_4"] = "../data/poster.webp"
        body = json.dumps({"list": [row, result()], "pages": ""}).encode()
        with patch.object(catalog, "_fetch_single", return_value=(body, MIRROR + "/bbs/search.php?stx=test")):
            rows, more = catalog.search_catalog("삼국지", self.cancel)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], CONTENT)
        self.assertEqual(rows[0]["thumbnail"], MIRROR + "/data/poster.webp")
        self.assertEqual(rows[0]["category"], "해외드라마")
        self.assertFalse(more)

    def test_unregistered_search_response_origin_is_rejected(self):
        body = json.dumps({"list": [result()], "pages": ""}).encode()
        with patch.object(catalog, "_fetch_single", return_value=(body, "https://unknown.example/bbs/search.php")):
            with self.assertRaises(DownloadError):
                catalog.search_catalog("삼국지", self.cancel)

    def test_views_match_site_number_format_after_text_normalization(self):
        for value, expected in ((7120, "7,120"), ("1234567", "1,234,567"),
                                (" <span>&nbsp;11184</span> ", "11,184")):
            with self.subTest(value=value):
                row = result()
                row["wr_hit"] = value
                (rows, _), _ = self.search([row])
                self.assertEqual(rows[0]["views"], expected)
                self.assertEqual(rows[0]["meta"], "해외드라마 · 2010")

    def test_missing_or_nonpositive_views_are_omitted_like_site(self):
        for value in (None, "", "0", "-1", "unavailable"):
            with self.subTest(value=value):
                row = result()
                if value is None:
                    row.pop("wr_hit")
                else:
                    row["wr_hit"] = value
                (rows, _), _ = self.search([row])
                self.assertEqual(rows[0]["views"], "")

    def test_last_page_and_empty_results(self):
        (rows, more), _ = self.search([result()], '<a href="?page=1">1</a>', page=2)
        self.assertEqual(len(rows), 1)
        self.assertFalse(more)
        self.assertEqual(self.search([])[0], ([], False))

    def test_duplicate_titles_use_distinct_urls_and_duplicate_urls_removed(self):
        (rows, _), _ = self.search([result(), result(), result(href="/world/123")])
        self.assertEqual([row["url"] for row in rows], [CONTENT, "https://tvmon1.com/world/123"])

    def test_non_tvmon_result_rejected(self):
        for url in ("https://evil.example/world/1", "https://tvmon1.com@evil.example/world/1",
                    "http://tvmon1.com/world/1", "https://tvmon1.com:123/world/1", "/world/1/2"):
            with self.subTest(url=url), self.assertRaises(DownloadError):
                self.search([result(href=url)])

    def test_invalid_thumbnail_is_omitted(self):
        row = result()
        row["wr_4"] = "javascript:alert(1)"
        (rows, _), _ = self.search([row])
        self.assertEqual(rows[0]["thumbnail"], "")

    def test_malformed_search_response_is_reported(self):
        for body in (b"<html>upstream failure</html>", b'{}', b'{"list":null,"pages":""}'):
            with self.subTest(body=body), patch.object(catalog, "_fetch_single", return_value=(body, API)):
                with self.assertRaises(DownloadError):
                    catalog.search_catalog("삼국지", self.cancel)

    def test_cancelled_and_blank_queries_do_not_fetch(self):
        with patch.object(catalog, "_fetch_single") as request:
            with self.assertRaises(DownloadError):
                catalog.search_catalog(" ", self.cancel)
            self.cancel.set()
            with self.assertRaises(Cancelled):
                catalog.search_catalog("삼국지", self.cancel)
            request.assert_not_called()

    def test_domain_order_switch_uses_each_api_query_page_and_headers(self):
        requests = []

        def get(url, headers, cancel, limit, on_bytes):
            requests.append((url, headers))
            return json.dumps({"list": [result()], "pages": '<a href="?page=3">3</a>'}).encode(), url

        client = Mock(get=Mock(side_effect=get))
        with patch.object(downloader, "_get_http_client", return_value=client):
            for origins in ([MIRROR, catalog.ORIGIN], [catalog.ORIGIN, MIRROR],
                            [MIRROR, catalog.ORIGIN], ["https://www.tvmon1.com", MIRROR]):
                self.domains.return_value = origins
                rows, more = catalog.search_catalog(" 삼국지 ", self.cancel, 2)
                self.assertEqual(rows[0]["url"], CONTENT)
                self.assertTrue(more)
                url, headers = requests[-1]
                self.assertEqual("https://" + urlsplit(url).netloc, origins[0])
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["stx"], ["삼국지"])
                self.assertEqual(query["page"], ["2"])
                self.assertEqual(query["sst"], ["subIdx"])
                if origins[0] == MIRROR:
                    self.assertEqual(urlsplit(url).path, "/bbs/search.php")
                    self.assertEqual(query["ajax"], ["1"])
                else:
                    self.assertEqual(urlsplit(url).path, "/bbs/ajax.searchList.php")
                    self.assertEqual(query["sfl"], ["wr_subject"])
                    self.assertEqual(query["sop"], ["and"])
                self.assertEqual(headers["Origin"], origins[0])
                self.assertTrue(headers["Referer"].startswith(origins[0] + "/search?"))
                self.assertEqual(parse_qs(urlsplit(headers["Referer"]).query), {"stx": ["삼국지"]})
        self.assertEqual(len(requests), 4)

    def test_invalid_first_search_response_tries_next_domain_in_both_orders(self):
        valid = json.dumps({"list": [result("정상 결과")], "pages": ""}).encode()
        malformed = (b"<html>search page</html>", b'{}', b'{"list":null,"pages":""}',
                     b'{"list":[],"pages":null}',
                     json.dumps({"list": [result("부분 결과"), {}], "pages": ""}).encode(),
                     json.dumps({"list": [result(href="https://unknown.example/world/1")], "pages": ""}).encode())
        for origins in ([catalog.ORIGIN, MIRROR], [MIRROR, catalog.ORIGIN]):
            self.domains.return_value = origins
            for body in malformed:
                requests = []

                def get(url, *args):
                    requests.append(url)
                    return (body if len(requests) == 1 else valid), url

                with self.subTest(origins=origins, body=body), \
                        patch.object(downloader, "_get_http_client", return_value=Mock(get=Mock(side_effect=get))):
                    rows, more = catalog.search_catalog("삼국지", self.cancel, 2)
                    self.assertEqual([row["title"] for row in rows], ["정상 결과"])
                    self.assertFalse(more)
                    self.assertEqual(["https://" + urlsplit(url).netloc for url in requests], origins)
                    self.assertTrue(all(parse_qs(urlsplit(url).query)["page"] == ["2"] for url in requests))

    def test_wrong_response_location_tries_next_domain(self):
        body = json.dumps({"list": [result()], "pages": ""}).encode()
        for final_url in ("https://unknown.example/bbs/ajax.searchList.php", catalog.ORIGIN + "/search"):
            with self.subTest(final_url=final_url), patch.object(catalog, "_fetch_single", side_effect=[
                    (body, final_url), (body, MIRROR + "/bbs/search.php")]) as request:
                rows, _ = catalog.search_catalog("삼국지", self.cancel)
                self.assertEqual(rows[0]["url"], CONTENT)
                self.assertEqual(request.call_count, 2)

    def test_empty_valid_response_does_not_try_next_domain(self):
        with patch.object(catalog, "_fetch_single", return_value=(b'{"list":[],"pages":""}', API)) as request:
            self.assertEqual(catalog.search_catalog("없는 작품", self.cancel), ([], False))
        self.assertEqual(request.call_count, 1)

    def test_explicit_origin_pins_pagination_and_does_not_fail_over(self):
        with patch.object(catalog, "_fetch_single", return_value=(b"<html>bad</html>", API)) as request, \
                self.assertRaises(DownloadError):
            catalog.search_catalog("삼국지", self.cancel, 2, origin=catalog.ORIGIN)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].startswith(API + "?"))
        with patch.object(catalog, "_fetch_single") as request, self.assertRaises(DownloadError):
            catalog.search_catalog("삼국지", self.cancel, origin="https://unregistered.example")
        request.assert_not_called()

    def test_transport_error_tries_next_domain_once_and_all_failures_name_hosts(self):
        body = json.dumps({"list": [result()], "pages": ""}).encode()
        with patch.object(catalog, "_fetch_single", side_effect=[DownloadError("연결 실패"),
                (body, MIRROR + "/bbs/search.php")]) as request:
            self.assertEqual(catalog.search_catalog("삼국지", self.cancel)[0][0]["url"], CONTENT)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.kwargs["retry"] is False for call in request.call_args_list))
        with patch.object(catalog, "_fetch_single", side_effect=[(b"<html>bad</html>", API),
                DownloadError("연결 실패")]) as request, self.assertRaises(DownloadError) as error:
            catalog.search_catalog("삼국지", self.cancel)
        self.assertEqual(request.call_count, 2)
        self.assertIn("tvmon1.com", str(error.exception))
        self.assertIn("tvwiki52.net", str(error.exception))
        self.assertIn("검색 결과를 읽지 못했습니다", str(error.exception))
        self.assertIn("연결 실패", str(error.exception))

    def test_cancellation_during_response_stops_before_next_domain(self):
        def get(*args, **kwargs):
            self.cancel.set()
            return b"<html>bad</html>", API

        with patch.object(catalog, "_fetch_single", side_effect=get) as request, self.assertRaises(Cancelled):
            catalog.search_catalog("삼국지", self.cancel)
        self.assertEqual(request.call_count, 1)

    def test_invalid_page_does_not_send_request(self):
        for page in (0, -1, "2"):
            with self.subTest(page=page), patch.object(catalog, "_fetch_single") as request, self.assertRaises(DownloadError):
                catalog.search_catalog("삼국지", self.cancel, page)
            request.assert_not_called()


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.enterContext(patch("tvmon_domains.get_domains", return_value=[catalog.ORIGIN, MIRROR]))

    def test_full_list_excludes_player_overlay_and_is_oldest_first(self):
        with patch.object(catalog, "fetch", return_value=(episode_page(3, 2, 1), CONTENT)):
            rows = catalog.list_episodes(CONTENT, self.cancel)
        self.assertEqual([row["title"] for row in rows], ["삼국지 1화", "삼국지 2화", "삼국지 3화"])
        self.assertEqual(rows[0], {"title": "삼국지 1화", "url": CONTENT + "/1",
                                   "thumbnail": "https://images.example/1.png", "meta": "10.10.2026"})

    def test_mirror_episode_page_keeps_identity_and_resolves_relative_artwork(self):
        mirror_content = MIRROR + "/world/10180"
        body = episode_page(2, 1).replace(CONTENT.encode(), mirror_content.encode())
        body = body.replace((mirror_content + "/1").encode(), b"/world/10180/1")
        body = body.replace(b"https://images.example/1.png", b"/thumbs/1.png")
        with patch.object(catalog, "fetch", return_value=(body, mirror_content)) as request:
            rows = catalog.list_episodes(mirror_content, self.cancel)
        self.assertEqual(request.call_args.args[0], CONTENT)
        self.assertEqual([row["url"] for row in rows], [CONTENT + "/1", CONTENT + "/2"])
        self.assertEqual(rows[0]["thumbnail"], MIRROR + "/thumbs/1.png")
        self.assertEqual(rows[1]["thumbnail"], "https://images.example/2.png")

    def test_mirror_pagination_retains_canonical_content_identity(self):
        mirror_content = MIRROR + "/world/10180"
        pagination = f'<a class="pg_page" href="{mirror_content}?page=2">2</a>'
        pages = [(episode_page(2, pagination=pagination), mirror_content),
                 (episode_page(1), mirror_content + "?page=2")]
        with patch.object(catalog, "fetch", side_effect=pages) as request:
            rows = catalog.list_episodes(CONTENT, self.cancel)
        self.assertEqual([row["url"] for row in rows], [CONTENT + "/1", CONTENT + "/2"])
        self.assertEqual([call.args[0] for call in request.call_args_list], [CONTENT, CONTENT + "?page=2"])

    def test_mirror_episode_link_to_different_content_is_rejected(self):
        body = ('<div id="other_list"><ul>' + episode(1, url=MIRROR + "/world/123/1") + '</ul></div>').encode()
        with patch.object(catalog, "fetch", return_value=(body, MIRROR + "/world/10180")):
            with self.assertRaises(DownloadError):
                catalog.list_episodes(CONTENT, self.cancel)

    def test_all_advertised_pages_are_followed_once_and_deduplicated(self):
        pagination = '<nav><a class="pg_page" href="?page=1">1</a><a class="pg_page pg_end" href="?page=2">2</a></nav>'
        pages = [(episode_page(4, 3, pagination=pagination), CONTENT),
                 (episode_page(3, 2, 1, pagination=pagination), CONTENT + "?page=2")]
        with patch.object(catalog, "fetch", side_effect=pages) as request:
            rows = catalog.list_episodes(CONTENT, self.cancel)
        self.assertEqual([row["url"] for row in rows], [CONTENT + f"/{number}" for number in (1, 2, 3, 4)])
        self.assertEqual([call.args[0] for call in request.call_args_list], [CONTENT, CONTENT + "?page=2"])

    def test_repeated_page_fails_instead_of_returning_partial_list(self):
        pagination = '<a class="pg_page" href="?page=2">2</a>'
        with patch.object(catalog, "fetch", return_value=(episode_page(3, 2, pagination=pagination), CONTENT)):
            with self.assertRaisesRegex(DownloadError, "반복"):
                catalog.list_episodes(CONTENT, self.cancel)

    def test_missing_or_truncated_full_list_fails(self):
        for body in (b"<html>Unavailable</html>", episode_page(1).replace(b"</div></html>", b""), episode_page()):
            with self.subTest(body=body), patch.object(catalog, "fetch", return_value=(body, CONTENT)):
                with self.assertRaises(DownloadError):
                    catalog.list_episodes(CONTENT, self.cancel)

    def test_episode_from_other_content_rejected(self):
        body = ('<div id="other_list"><ul>' + episode(1, url="https://tvmon1.com/world/123/1") + '</ul></div>').encode()
        with patch.object(catalog, "fetch", return_value=(body, CONTENT)):
            with self.assertRaises(DownloadError):
                catalog.list_episodes(CONTENT, self.cancel)

    def test_invalid_content_and_pagination_sources_are_rejected(self):
        with patch.object(catalog, "fetch") as request:
            with self.assertRaises(DownloadError):
                catalog.list_episodes("https://evil.example/world/10180", self.cancel)
            request.assert_not_called()
        body = episode_page(2, pagination='<a class="pg_page" href="https://evil.example/world/10180?page=2">2</a>')
        with patch.object(catalog, "fetch", return_value=(body, CONTENT)):
            with self.assertRaises(DownloadError):
                catalog.list_episodes(CONTENT, self.cancel)

    def test_cancellation_between_pages_stops_fetching(self):
        def request(url, *args, **kwargs):
            self.cancel.set()
            return episode_page(2, 1), url

        with patch.object(catalog, "fetch", side_effect=request) as fetch:
            with self.assertRaises(Cancelled):
                catalog.list_episodes(CONTENT, self.cancel)
            self.assertEqual(fetch.call_count, 1)


class TextCatalogTests(unittest.TestCase):
    ORIGIN = "https://toki92.com"

    def setUp(self):
        self.cancel = threading.Event()

    @staticmethod
    def page(cards="", extra="", query="삼국지"):
        return (f'<form class="search-page-form"><input name="q" value="{query}"></form>'
                f'<div class="card-grid search-results-grid">{cards}</div>{extra}').encode()

    @staticmethod
    def card(number=1, category="소설", completed=True):
        return (f'<a class="card" href="/novel/{number}"><div class="thumb">'
                + ('<span class="badge">완결</span>' if completed else '')
                + f'<span class="kind-badge search-kind-badge">{category}</span><img src="/cover.jpg"></div>'
                  '<div class="info"><p class="subject">삼국지 &amp; <b>작품</b></p>'
                  '<p class="genre">판타지&nbsp; 역사</p><p class="ep">512화</p></div></a>')

    def search(self, body, **kwargs):
        request = Mock(side_effect=lambda url, *args, **options: (body, url))
        result = catalog.search_text_catalog("삼국지", self.ORIGIN, self.cancel, browser_fetch=request, **kwargs)
        return result, request

    def test_search_uses_current_domain_query_and_same_browser_with_card_metadata(self):
        body = self.page(self.card() + self.card() + self.card(2, "웹툰", False),
                         '<a class="pager-num" href="/search?q=삼국지&amp;page=2">2</a>')
        (rows, more), request = self.search(body)
        self.assertTrue(more)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"title": "삼국지 & 작품", "url": self.ORIGIN + "/novel/1",
            "thumbnail": self.ORIGIN + "/cover.jpg", "category": "소설", "genre": "판타지 역사",
            "episodes": "512화", "status": "완결", "meta": "소설 · 완결 · 512화"})
        self.assertEqual(rows[1]["status"], "")
        self.assertEqual(rows[1]["meta"], "웹툰 · 512화")
        params = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(params, {"q": ["삼국지"], "kind": ["novel"], "field": ["title"], "match": ["contains"], "page": ["1"]})
        self.assertFalse(request.call_args.kwargs["visible"])

    def test_empty_search_is_valid_but_homepage_login_or_truncated_cards_are_not(self):
        (rows, more), _ = self.search(self.page(extra='<div class="ep-empty">검색 결과가 없습니다</div>'))
        self.assertEqual(rows, [])
        self.assertFalse(more)
        for body in (b'<title>Homepage</title>', self.page(), self.page(self.card(), query="다른 검색어"),
                     self.page().replace(b'</div>', b'<a class="card" href="/novel/1">')):
            with self.subTest(body=body[:60]), self.assertRaises(DownloadError):
                self.search(body)

    def test_pager_ignores_other_queries_and_hosts_and_manual_mode_is_explicit(self):
        body = self.page(self.card(), '<a class="pager-num" href="/search?q=다른검색&amp;page=8">8</a>'
                         '<a class="pager-btn" href="https://evil.example/search?q=삼국지&amp;page=4">4</a>')
        (_, more), request = self.search(body, page=2, visible=True)
        self.assertFalse(more)
        self.assertTrue(request.call_args.kwargs["visible"])
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query)["page"], ["2"])

    def test_search_rejects_external_cards_and_unexpected_final_search(self):
        with self.assertRaises(DownloadError):
            self.search(self.page(self.card().replace('href="/novel/1"', 'href="https://evil.example/novel/1"')))
        for final in ("https://evil.example/search?q=삼국지", self.ORIGIN + "/login?q=삼국지", self.ORIGIN + "/search?q=다른검색"):
            request = Mock(return_value=(self.page(self.card()), final))
            with self.subTest(final=final), self.assertRaises(DownloadError):
                catalog.search_text_catalog("삼국지", self.ORIGIN, self.cancel, browser_fetch=request)

    def test_challenge_timeout_http_failure_and_cancellation_remain_distinct(self):
        for error, expected in ((catalog.BrowserChallengeError(self.ORIGIN), catalog.BrowserVerificationRequired),
                                (TimeoutError("지연"), DownloadError), (catalog.HttpStatusError(403), DownloadError),
                                (catalog.TransferCancelled(), Cancelled)):
            with self.subTest(error=type(error)), self.assertRaises(expected) as caught:
                catalog.search_text_catalog("삼국지", self.ORIGIN, self.cancel, browser_fetch=Mock(side_effect=error))
            if expected is DownloadError:
                self.assertNotIsInstance(caught.exception, catalog.BrowserVerificationRequired)
        self.cancel.set()
        request = Mock()
        with self.assertRaises(Cancelled):
            catalog.search_text_catalog("삼국지", self.ORIGIN, self.cancel, browser_fetch=request)
        request.assert_not_called()


class TextEpisodeTests(unittest.TestCase):
    URL = "https://toki92.com/novel/123"

    @staticmethod
    def window(numbers, older=None, newer=None):
        return {"ok": True, "items": [{"id": str(1000 + n), "number": n, "episodeLabel": f"{n}화",
                "title": f"시험 {n}", "publishedAtLabel": "2026.10.11", "isNotReady": False} for n in numbers],
                "hasOlder": older is not None, "hasNewer": newer is not None,
                "olderCursor": older, "newerCursor": newer}

    @staticmethod
    def page(window, count):
        import json
        payload = 'a:' + json.dumps({"rootId": "novel-episode-list-123", "initial": window}, ensure_ascii=False)
        links = ''.join(f'<li class="novel-ep-row" data-episode-id="{item["id"]}"><a class="novel-ep-link" '
                        f'href="/novel/123/{item["id"]}">{item["episodeLabel"]}</a></li>' for item in window.get("items", []))
        return ('<div class="nd-info"><h1>시험 작품 &amp; 제목</h1></div>'
                f'<section class="novel-episode-head"><h2>전체 {count}화</h2></section>'
                + links +
                '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>').encode()

    def test_not_ready_entries_are_excluded_from_initial_and_later_windows_without_count_error(self):
        import json
        initial = self.window([4, 3], older="older")
        initial["items"][0]["isNotReady"] = True
        older = self.window([2, 1])
        older["items"][1]["isNotReady"] = True
        bodies = [self.page(initial, 4), json.dumps(older).encode()]
        request = Mock(side_effect=lambda url, *a, **kw: (bodies.pop(0), url))
        rows = catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request)
        self.assertEqual([row["number"] for row in rows], [2, 3])
        self.assertEqual(request.call_count, 2)

    def test_missing_or_disabled_actual_episode_links_are_not_invented_from_ids(self):
        page = self.page(self.window([1, 2, 3, 4]), 4)
        page = page.replace(b'href="/novel/123/1002"', b'aria-disabled="true" href="/novel/123"')
        page = page.replace(b'href="/novel/123/1003"', b'')
        page = page.replace(b'href="/novel/123/1004"', b'href="/novel/123/9999"')
        request = Mock(return_value=(page, self.URL))
        rows = catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request)
        self.assertEqual([row["number"] for row in rows], [1])

    def test_all_preparing_entries_return_empty_but_unknown_readiness_fails(self):
        window = self.window([1])
        window["items"][0]["isNotReady"] = True
        request = Mock(return_value=(self.page(window, 1), self.URL))
        self.assertEqual(catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request), [])
        window["items"][0].pop("isNotReady")
        request.return_value = self.page(window, 1), self.URL
        with self.assertRaises(DownloadError):
            catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request)

    def test_all_windows_follow_direction_and_deduplicate_in_reading_order(self):
        import json
        bodies = [self.page(self.window([4, 3], older="old", newer="new"), 6),
                  json.dumps(self.window([3, 2, 1], newer="ignored")).encode(),
                  json.dumps(self.window([5, 6], older="ignored")).encode()]
        request = Mock(side_effect=lambda url, *args, **kwargs: (bodies.pop(0), url))
        rows = catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request, visible=True)
        self.assertEqual([r["number"] for r in rows], list(range(1, 7)))
        self.assertEqual(rows[0]["url"], self.URL + "/1001")
        self.assertEqual(rows[0]["title"], "1화 · 시험 1")
        self.assertEqual(rows[0]["series_title"], "시험 작품 & 제목")
        self.assertEqual(rows[0]["kind"], "text")
        self.assertEqual([c.kwargs["visible"] for c in request.call_args_list], [True, False, False])
        self.assertEqual([parse_qs(urlsplit(c.args[0]).query).get("direction") for c in request.call_args_list[1:]], [["older"], ["newer"]])

    def test_numeric_episode_labels_use_actual_numbers_across_windows(self):
        initial = self.window([25, 24], older="older")
        initial["items"][1]["episodeLabel"] = "23화"
        older = self.window([18, 17, 16])
        older["items"][0].update(episodeLabel="17화", title="17화")
        older["items"][2]["episodeLabel"] = "외전"
        bodies = [self.page(initial, 5), json.dumps(older).encode()]
        request = Mock(side_effect=lambda url, *a, **kw: (bodies.pop(0), url))
        rows = catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request)
        self.assertEqual([row["number"] for row in rows], [16, 17, 18, 24, 25])
        self.assertEqual([row["url"] for row in rows], [self.URL + f"/{1000 + n}" for n in (16, 17, 18, 24, 25)])
        self.assertEqual([row["title"] for row in rows],
                         ["외전 · 시험 16", "17화 · 시험 17", "18화", "24화 · 시험 24", "25화 · 시험 25"])

    def test_incomplete_repeated_or_malformed_windows_never_return_partial_success(self):
        import json
        cases = [[self.page(self.window([1]), 2)],
                 [self.page(self.window([2], older="a"), 3), json.dumps(self.window([2], older="a")).encode()],
                 [self.page({"ok": False}, 3)], [b"<html>Login</html>"]]
        for bodies in cases:
            request = Mock(side_effect=lambda url, *args, **kwargs: (bodies.pop(0), url))
            with self.subTest(count=len(bodies)), self.assertRaises(DownloadError):
                catalog.list_text_episodes(self.URL, threading.Event(), browser_fetch=request)

    def test_cancel_redirect_verification_and_non_novel_are_rejected(self):
        cancel = threading.Event()
        for request, expected in ((Mock(return_value=(b"", self.URL + "/login")), DownloadError),
                                  (Mock(side_effect=catalog.BrowserChallengeError(self.URL)), catalog.BrowserVerificationRequired),
                                  (Mock(side_effect=TimeoutError()), DownloadError)):
            with self.assertRaises(expected):
                catalog.list_text_episodes(self.URL, cancel, browser_fetch=request)
        request = Mock()
        with self.assertRaises(DownloadError):
            catalog.list_text_episodes(self.URL.replace("novel", "webtoon"), cancel, browser_fetch=request)
        cancel.set()
        with self.assertRaises(Cancelled):
            catalog.list_text_episodes(self.URL, cancel, browser_fetch=request)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

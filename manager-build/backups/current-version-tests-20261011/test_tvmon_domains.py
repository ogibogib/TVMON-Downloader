"""Domain validation and ordered, origin-only site URL replacement."""

import threading
import unittest
from unittest.mock import Mock

import tvmon_domains as domains


class DomainTests(unittest.TestCase):
    def setUp(self):
        previous = domains.get_domains()
        self.addCleanup(domains.set_domains, previous)
        domains.set_domains(domains.DEFAULT_DOMAINS)

    def test_defaults_and_list_snapshot_are_immutable(self):
        self.assertEqual(domains.get_domains(), ("https://tvmon1.com", "https://tvwiki52.net"))
        old = domains.get_domains()
        result = domains.set_domains(["tvwiki52.net", "next.example"])
        self.assertIsInstance(result, tuple)
        self.assertEqual(result, ("https://tvwiki52.net", "https://next.example"))
        self.assertEqual(old, domains.DEFAULT_DOMAINS)

    def test_next_numbered_hosts_are_bounded_and_preserve_prefix_and_suffix(self):
        self.assertEqual(domains.next_numbered_domains("https://tvwiki52.net"),
                         ("https://tvwiki53.net", "https://tvwiki54.net", "https://tvwiki55.net"))
        self.assertEqual(domains.next_numbered_domains("https://www.tvwiki99.net"),
                         ("https://www.tvwiki100.net", "https://www.tvwiki101.net", "https://www.tvwiki102.net"))
        self.assertEqual(domains.next_numbered_domains("https://tvwiki.store"), ())
        self.assertEqual(domains.next_numbered_domains("https://tvwikiurl.net"), ())
        with self.assertRaises(ValueError):
            domains.next_numbered_domains("https://tvwiki52.net/content/123")

    def test_normalizes_domain_origin(self):
        for value, expected in ((" TVWiki52.NET/ ", "https://tvwiki52.net"),
                                ("HTTPS://TVMON1.COM:443/", "https://tvmon1.com"),
                                ("www.TVWiki52.net", "https://www.tvwiki52.net"),
                                ("https://예시.한국", "https://xn--vv4b11d.xn--3e0b707e")):
            with self.subTest(value=value):
                self.assertEqual(domains.normalize_domain(value), expected)

    def test_rejects_non_origins_and_invalid_hosts(self):
        for value in (None, 42, "", "http://tvwiki52.net", "ftp://tvwiki52.net", "https://",
                      "https://tvwiki52.net/movie/4047", "https://tvwiki52.net//", "tvwiki52.net?",
                      "tvwiki52.net#", "https://user:pass@tvwiki52.net", "https://@tvwiki52.net",
                      "https://tvwiki52.net:80", "https://tvwiki52.net:bad", "tv wiki52.net",
                      "tvwiki\n52.net", "https://tvwiki52.net\\evil", "bad..example", "-bad.example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                domains.normalize_domain(value)

    def test_invalid_list_does_not_partially_replace_settings(self):
        for values in ([], None, "tvwiki52.net", {"tvwiki52.net"}, ["next.example", "http://invalid.example"],
                       ["tvwiki52.net", "HTTPS://TVWIKI52.NET:443/"],
                       ["www.tvwiki52.net", "tvwiki52.net"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                domains.set_domains(values)
            self.assertEqual(domains.get_domains(), domains.DEFAULT_DOMAINS)

    def test_list_normalization_does_not_publish_unpersisted_changes(self):
        self.assertEqual(domains.normalize_domains([" TVWIKI52.NET/", "next.example"]),
                         ("https://tvwiki52.net", "https://next.example"))
        self.assertEqual(domains.get_domains(), domains.DEFAULT_DOMAINS)

    def test_site_recognition_includes_www_and_legacy_origin(self):
        domains.set_domains(["www.tvwiki52.net"])
        for url in ("https://tvwiki52.net/movie/4047", "https://www.tvwiki52.net:443/world/2558",
                    "https://tvmon1.com/world/2558/1", "https://www.tvmon1.com/world/2558/1"):
            with self.subTest(url=url):
                self.assertTrue(domains.is_site_url(url))
        for url in ("http://tvwiki52.net/movie/4047", "https://tvwiki52.net:444/movie/4047",
                    "https://tvwiki52.net.evil.example/movie/4047", "https://evil.example/movie/4047",
                    "https://tvwiki52.net@evil.example/movie/4047", "https://@tvwiki52.net/movie/4047",
                    "//tvwiki52.net/movie/4047", "https://tvwiki\n52.net/movie/4047", None):
            with self.subTest(url=url):
                self.assertFalse(domains.is_site_url(url))

    def test_canonical_identity_survives_removal_of_old_domain(self):
        domains.set_domains(["tvwiki52.net"])
        expected = "https://tvmon1.com/world/2558/1?x=1&x=2#play"
        for origin in ("https://tvmon1.com", "https://tvwiki52.net", "https://www.tvwiki52.net:443"):
            with self.subTest(origin=origin):
                self.assertEqual(domains.canonical_site_url(origin + "/world/2558/1?x=1&x=2#play"), expected)

    def test_relative_links_preserve_query_and_fragment(self):
        base = "https://tvwiki52.net/world/2558/"
        self.assertEqual(domains.canonical_site_url("1?token=a%2Fb&x=2#player", base),
                         "https://tvmon1.com/world/2558/1?token=a%2Fb&x=2#player")
        self.assertEqual(domains.canonical_site_url("/movie/4047"), "https://tvmon1.com/movie/4047")
        for value in ("https://evil.example/world/2558", "http://tvmon1.com/world/2558", None,
                      "https://tvwiki\n52.net/world/2558"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                domains.canonical_site_url(value)

    def test_candidates_keep_configured_order_and_rewrite_site_referer(self):
        domains.set_domains(["tvwiki52.net", "next.example", "tvmon1.com"])
        path = "/bbs/get_episode.php?bo_table=world&token=a%2Fb#part"
        referer_path = "/world/2558/1?from=search#player"
        result = domains.domain_requests("https://tvmon1.com" + path, "https://www.tvwiki52.net:443" + referer_path)
        self.assertEqual(result, [(origin + path, origin + referer_path) for origin in domains.get_domains()])
        self.assertEqual(domains.domain_requests("https://next.example" + path, "https://tvmon1.com" + referer_path), result)

    def test_external_media_and_referer_are_not_rewritten(self):
        external = "https://media.example/segment.gif?token=123#x"
        referer = "https://player.example/player?id=1"
        self.assertEqual(domains.domain_requests(external, referer), [(external, referer)])
        self.assertEqual(domains.domain_requests("https://tvmon1.com/world/2558", referer),
                         [(origin + "/world/2558", referer) for origin in domains.DEFAULT_DOMAINS])


class CurrentDomainTests(unittest.TestCase):
    GUIDE = ('<html><a href="https://ads.example/">광고</a><dl>'
             '<dt>다른 주소</dt><dd>old.example</dd>'
             '<dt>현재 <span>안내 주소</span></dt><dd>tvwiki.store</dd></dl>'
             '<a href="https://tvwiki.store/">현재 주소 열기</a></html>').encode()
    HOME = ('<html><head><title>티비위키 | TVWIKI</title></head>'
            '<body><a href="/drama/123">드라마</a></body></html>').encode()

    def setUp(self):
        self.cancel = threading.Event()
        self.original = domains.get_domains()
        self.addCleanup(domains.set_domains, self.original)

    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():
            raise InterruptedError("cancelled")

    @staticmethod
    def refresh(url):
        return f'<meta http-equiv="Refresh" content="0; URL=\'{url}\'">'.encode()

    def lookup(self, responses):
        fetcher = Mock(side_effect=responses)
        result = domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        return result, fetcher

    def test_labelled_guide_and_meta_refresh_return_verified_final_origin(self):
        domains.set_domains(["custom.example"])
        result, fetcher = self.lookup([
            (self.GUIDE, domains.DOMAIN_GUIDE_URL),
            (self.refresh("https://tvwiki52.net/"), "https://tvwiki.store/"),
            (self.HOME, "https://tvwiki52.net/")])
        self.assertEqual(result, "https://tvwiki52.net")
        self.assertEqual(domains.get_domains(), ("https://custom.example",))
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.DOMAIN_GUIDE_URL, "https://tvwiki.store/", "https://tvwiki52.net/"])
        self.assertEqual([call.args[2] for call in fetcher.call_args_list],
                         [domains.DOMAIN_GUIDE_URL, domains.DOMAIN_GUIDE_URL, "https://tvwiki.store/"])
        for call in fetcher.call_args_list:
            self.assertIs(call.args[1], self.cancel)
            self.assertEqual(call.kwargs, {"limit": 2 * 1024 * 1024, "retry": False})

    def test_http_redirect_final_url_is_used_without_redundant_home_request(self):
        result, fetcher = self.lookup([(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                      (self.HOME, "https://tvwiki53.net/index.php")])
        self.assertEqual(result, "https://tvwiki53.net")
        self.assertEqual(fetcher.call_count, 2)

    def test_root_without_homepage_tries_index_once_and_returns_origin(self):
        failed = Mock()
        fetcher = Mock(side_effect=[(b'<title>TVWIKI</title><p>Unavailable</p>', 'https://tvwiki53.net/'),
                                   (self.HOME, 'https://tvwiki53.net/index.php')])
        result = domains.verify_domain('https://tvwiki53.net', fetcher, self.cancel,
                                       check_cancel=self.check_cancel, on_failure=failed)
        self.assertEqual(result, 'https://tvwiki53.net')
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         ['https://tvwiki53.net/', 'https://tvwiki53.net/index.php'])
        self.assertEqual(fetcher.call_args_list[1].args[2], 'https://tvwiki53.net/')
        failed.assert_called_once()
        self.assertIn('/index.php', failed.call_args.args[0])
        self.assertEqual(domains.get_domains(), self.original)

    def test_guide_landing_homepage_also_tries_index(self):
        result, fetcher = self.lookup([(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                      (self.refresh('https://tvwiki53.net/'), 'https://tvwiki.store/'),
                                      (b'<title>TVWIKI</title>', 'https://tvwiki53.net/'),
                                      (self.HOME, 'https://tvwiki53.net/index.php')])
        self.assertEqual(result, 'https://tvwiki53.net')
        self.assertEqual(fetcher.call_args_list[-1].args[0], 'https://tvwiki53.net/index.php')

    def test_invalid_index_is_rejected_without_repeating_it(self):
        for final_url in ('https://tvwiki53.net/index.php', 'https://tvwiki53.net/'):
            fetcher = Mock(side_effect=[(b'<title>Error</title>', 'https://tvwiki53.net/'),
                                       (b'<title>TVWIKI</title><a href="https://ads.example/drama/123">ad</a>', final_url)])
            with self.subTest(final_url=final_url), self.assertRaises(ValueError):
                domains.verify_domain('https://tvwiki53.net', fetcher, self.cancel, check_cancel=self.check_cancel)
            self.assertEqual(fetcher.call_count, 2)

    def test_cancel_between_root_failure_and_index_stops_before_second_request(self):
        fetcher = Mock(return_value=(b'<title>Error</title>', 'https://tvwiki53.net/'))
        with self.assertRaises(InterruptedError):
            domains.verify_domain('https://tvwiki53.net', fetcher, self.cancel, check_cancel=self.check_cancel,
                                  on_failure=lambda _: self.cancel.set())
        self.assertEqual(fetcher.call_count, 1)

    def test_unlabelled_or_conflicting_guide_links_are_rejected(self):
        for guide in (b'<a href="https://wrong.example">open</a>',
                      self.GUIDE.replace(b'https://tvwiki.store/', b'https://wrong.example/'),
                      self.GUIDE + '<dt>현재 안내 주소</dt><dd>other.example</dd>'.encode(),
                      self.GUIDE.replace(b'tvwiki.store</dd>', b'http://tvwiki.store</dd>')):
            fetcher = Mock(return_value=(guide, domains.DOMAIN_GUIDE_URL))
            with self.subTest(guide=guide), self.assertRaises(ValueError):
                domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
            self.assertEqual(fetcher.call_count, 1)

    def test_guide_redirect_to_an_unrelated_site_is_rejected(self):
        fetcher = Mock(return_value=(self.GUIDE, "https://other.example/"))
        with self.assertRaisesRegex(ValueError, "안내 페이지"):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        self.assertEqual(fetcher.call_count, 1)

    def test_custom_video_guide_url_and_referer_are_used_without_default_guide(self):
        guide_url = "https://www.guide.example/current?site=tvwiki"
        final_guide = "https://guide.example/current?site=tvwiki"
        fetcher = Mock(side_effect=[(self.GUIDE, final_guide), (self.HOME, "https://tvwiki54.net/")])
        result = domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel,
                                                guide_url=guide_url)
        self.assertEqual(result, "https://tvwiki54.net")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list], [guide_url, "https://tvwiki.store/"])
        self.assertEqual([call.args[2] for call in fetcher.call_args_list], [guide_url, final_guide])

    def test_custom_video_guide_rejects_redirect_to_a_different_guide_host(self):
        fetcher = Mock(return_value=(self.GUIDE, domains.DOMAIN_GUIDE_URL))
        with self.assertRaisesRegex(ValueError, "안내 페이지"):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel,
                                           guide_url="https://guide.example/current")
        self.assertEqual(fetcher.call_count, 1)

    def test_unsafe_meta_refresh_targets_are_not_requested(self):
        for target in ("http://tvwiki52.net/", "https://user:pass@tvwiki52.net/",
                       "https://tvwiki52.net:444/", "javascript:alert(1)", "https://bad host/"):
            fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                        (self.refresh(target), "https://tvwiki.store/")])
            with self.subTest(target=target), self.assertRaises(ValueError):
                domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
            self.assertEqual(fetcher.call_count, 2)

    def test_non_https_http_redirect_result_is_rejected(self):
        fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                    (self.HOME, "http://tvwiki52.net/")])
        with self.assertRaises(ValueError):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)

    def test_meta_refresh_loop_and_hop_count_are_bounded(self):
        fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                    (self.refresh("https://next.example/"), "https://tvwiki.store/"),
                                    (self.refresh("https://tvwiki.store/"), "https://next.example/")])
        with self.assertRaisesRegex(ValueError, "반복"):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        self.assertEqual(fetcher.call_count, 3)
        replies = [(self.GUIDE, domains.DOMAIN_GUIDE_URL)]
        previous = "https://tvwiki.store/"
        for index in range(5):
            target = f"https://next{index}.example/"
            replies.append((self.refresh(target), previous))
            previous = target
        fetcher = Mock(side_effect=replies)
        with self.assertRaisesRegex(ValueError, "횟수"):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        self.assertEqual(fetcher.call_count, 6)

    def test_malformed_or_conflicting_refresh_is_rejected(self):
        for body in (b'<meta http-equiv="refresh" content="5">',
                     self.refresh("https://first.example/") + self.refresh("https://other.example/")):
            fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                        (body, "https://tvwiki.store/")])
            with self.subTest(body=body), self.assertRaises(ValueError):
                domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
            self.assertEqual(fetcher.call_count, 2)

    def test_error_or_script_only_page_is_not_registered_as_a_homepage(self):
        for body in (b'<title>Just a moment...</title>', b'<title>TVWIKI</title><p>Error</p>',
                     b'<script>location.replace("https://tvwiki52.net/")</script>',
                     b'<title>TVWIKI</title><a href="https://ads.example/drama/123">ad</a>'):
            fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL),
                                        (body, "https://tvwiki.store/"), (body, "https://tvwiki.store/index.php")])
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "홈페이지"):
                domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
            self.assertEqual(fetcher.call_count, 3)

    def test_cancellation_before_and_after_request_propagates(self):
        self.cancel.set()
        fetcher = Mock()
        with self.assertRaises(InterruptedError):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        fetcher.assert_not_called()
        self.cancel.clear()

        def cancelled_guide(*args, **kwargs):
            self.cancel.set()
            return self.GUIDE, domains.DOMAIN_GUIDE_URL

        fetcher = Mock(side_effect=cancelled_guide)
        with self.assertRaises(InterruptedError):
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        self.assertEqual(fetcher.call_count, 1)

    def test_native_transport_error_propagates_without_setting_changes(self):
        error = TimeoutError("fixture")
        fetcher = Mock(side_effect=[(self.GUIDE, domains.DOMAIN_GUIDE_URL), error])
        with self.assertRaises(TimeoutError) as caught:
            domains.resolve_current_domain(fetcher, self.cancel, check_cancel=self.check_cancel)
        self.assertIs(caught.exception, error)
        self.assertEqual(domains.get_domains(), self.original)


class TextDomainTests(unittest.TestCase):
    GUIDE = ('<div class="hud-url-row"><span class="hud-url current"><b>toki34</b>.com</span></div>'
             '<span class="hud-url">newtoki1.org</span><a href="https://newtoki1.org/">다른 사이트</a>'
             '<span class="hud-url">toki36.com</span><a href="https://toki36.com/">다른 주소</a>'
             '<a href="https://toki34.com/">현재 주소</a>').encode()
    HOME = '<html><title>뉴토끼 - NEWTOKI</title><body>콘텐츠 목록</body></html>'.encode()

    def test_supported_text_hosts_exclude_unrelated_domains_and_invalid_origins(self):
        for url in ("https://toki34.com/", "https://toki35.com/novel/1", "https://www.toki36.com/search?q=test"):
            self.assertTrue(domains.is_text_site_url(url), url)
        for url in ("https://newtoki1.org/", "https://toki35.com.evil.example/", "http://toki35.com/",
                    "https://user@toki35.com/", "https://toki35.com:8443/", "https://toki.example/", "", None):
            self.assertFalse(domains.is_text_site_url(url), url)

    def test_unsupported_guide_address_is_not_requested_or_saved_after_toki_failure(self):
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a><span class="hud-url">newtoki1.org</span><a href="https://newtoki1.org/">Other</a>'
        error = OSError("offline")
        fetcher = Mock(side_effect=[(guide, domains.TEXT_DOMAIN_GUIDE_URL), error])
        with self.assertRaises(OSError) as caught:
            domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)
        self.assertIs(caught.exception, error)
        self.assertEqual([call.args[0] for call in fetcher.call_args_list], [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/"])

    def test_unsupported_text_host_is_rejected_before_request_and_after_redirect(self):
        fetcher = Mock()
        with self.assertRaisesRegex(ValueError, "toki숫자.com"):
            domains.verify_domain("https://newtoki1.org", fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel, text_site=True)
        fetcher.assert_not_called()
        fetcher = Mock(return_value=(self.HOME, "https://newtoki1.org/"))
        with self.assertRaisesRegex(ValueError, "지원하지 않는"):
            domains.verify_domain("https://toki34.com", fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel, text_site=True)
        self.assertEqual(fetcher.call_count, 1)

    def test_http_redirect_returns_verified_toki35_without_requesting_other_guide_hosts(self):
        fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), (self.HOME, "https://toki35.com/")])
        self.assertEqual(domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel), "https://toki35.com")
        self.assertEqual(fetcher.call_count, 2)

    def test_configurable_guide_normalizes_https_page_paths_and_rejects_invalid_urls(self):
        self.assertEqual(domains.normalize_text_guide_url(" guide.example/notice?site=newtoki#current "),
                         "https://guide.example/notice?site=newtoki")
        self.assertEqual(domains.normalize_text_guide_url("https://뉴토끼주소.com/"), "https://xn--h10b90bi5zuhh79k.com/")
        for value in (None, "", " ", "http://guide.example/", "https://user:password@guide.example/",
                      "https://guide.example:8443/", "https://guide.example/a b", "https://guide.example/a\nb",
                      "https:/guide.example", "javascript:alert(1)", "/notice"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HTTPS"):
                domains.normalize_text_guide_url(value)

    def test_custom_guide_url_and_referer_are_used_without_requesting_default_guide(self):
        guide_url = "https://www.guide.example/notice?site=newtoki"
        final_guide = "https://guide.example/notice?site=newtoki"
        fetcher = Mock(side_effect=[(self.GUIDE, final_guide), (self.HOME, "https://toki34.com/")])
        result = domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel,
                                                     guide_url=guide_url)
        self.assertEqual(result, "https://toki34.com")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list], [guide_url, "https://toki34.com/"])
        self.assertEqual([call.args[2] for call in fetcher.call_args_list], [guide_url, final_guide])

    def test_custom_guide_rejects_foreign_redirects_and_its_own_homepage_as_content(self):
        guide_url = "https://guide.example/notice"
        fetcher = Mock(return_value=(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL))
        with self.assertRaisesRegex(ValueError, "다른 사이트"):
            domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel,
                                                guide_url=guide_url)
        self.assertEqual(fetcher.call_count, 1)
        guide = b'<span class="hud-url">guide.example</span><a href="https://guide.example/">Current</a>'
        fetcher = Mock(side_effect=[(guide, guide_url), (self.HOME, "https://guide.example/"),
                                   (self.HOME, "https://guide.example/index.php")])
        with self.assertRaisesRegex(ValueError, "지원하는 toki숫자.com"):
            domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel,
                                                guide_url=guide_url)

    def test_first_announced_address_is_preferred_when_available(self):
        fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), (self.HOME, "https://toki34.com/")])
        original = domains.get_domains()
        result = domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)
        self.assertEqual(result, "https://toki34.com")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/"])
        self.assertEqual(domains.get_domains(), original)
        self.assertTrue(all(call.kwargs == {"limit": 2 * 1024 * 1024, "retry": False} for call in fetcher.call_args_list))

    def test_second_announced_address_is_tried_after_first_connection_failure(self):
        failures = []
        fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), OSError("connection reset"),
                                   (self.HOME, "https://toki36.com/")])
        result = domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel,
                                                     on_failure=failures.append)
        self.assertEqual(result, "https://toki36.com")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/", "https://toki36.com/"])
        self.assertEqual(failures, ["connection reset"])

    def test_duplicate_announced_addresses_are_not_requested_twice(self):
        guide = self.GUIDE + b'<span class="hud-url">toki34.com</span>'
        failure = OSError("offline")
        fetcher = Mock(side_effect=[(guide, domains.TEXT_DOMAIN_GUIDE_URL), failure, failure])
        with self.assertRaises(OSError) as caught:
            domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)
        self.assertIs(caught.exception, failure)
        self.assertEqual(fetcher.call_count, 3)

    def test_cancelling_first_announced_address_prevents_second_request(self):
        cancel = threading.Event()
        def response(url, *args, **kwargs):
            if url == domains.TEXT_DOMAIN_GUIDE_URL:
                return self.GUIDE, url
            cancel.set()
            raise OSError("cancelled")
        fetcher = Mock(side_effect=response)
        with self.assertRaises(InterruptedError):
            domains.resolve_current_text_domain(fetcher, cancel, check_cancel=CurrentDomainTests.check_cancel)
        self.assertEqual(fetcher.call_count, 2)

    def test_homepage_title_is_not_replaced_by_login_or_signup_icon_titles(self):
        home = ('<html><head><title>뉴토끼 — 무료 웹툰 미리보기 | 뉴토끼</title></head><body>'
                '<a href="/ing">연재</a><svg><title>로그인</title></svg>'
                '<svg><title>회원가입</title></svg></body></html>').encode()
        fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), (home, "https://toki34.com/")])
        self.assertEqual(domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel),
                         "https://toki34.com")
        self.assertEqual(fetcher.call_count, 2)

    def test_invalid_hud_or_mismatched_link_stops_before_following_an_address(self):
        for guide in (b'<span class="hud-url-row">toki34.com</span>',
                      b'<span class="hud-url"></span><span class="hud-url">toki34.com</span>',
                      b'<span class="hud-url">http://toki34.com</span>',
                      b'<span class="hud-url">https://user:pass@toki34.com</span>',
                      b'<span class="hud-url">toki34.com/path</span>',
                      b'<span class="hud-url">toki34.com</span><a href="https://ads.example">ad</a>'):
            with self.subTest(guide=guide):
                fetcher = Mock(return_value=(guide, domains.TEXT_DOMAIN_GUIDE_URL))
                with self.assertRaises(ValueError):
                    domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)
                self.assertEqual(fetcher.call_count, 1)

    def test_guide_redirect_to_an_unrelated_host_is_rejected(self):
        fetcher = Mock(return_value=(self.GUIDE, "https://ads.example/"))
        with self.assertRaisesRegex(ValueError, "다른 사이트"):
            domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)
        self.assertEqual(fetcher.call_count, 1)

    def test_text_verification_rejects_video_home_and_challenge_page(self):
        for home in (CurrentDomainTests.HOME, b'<title>Just a moment...</title>', b'<title>404 Not Found</title>',
                     '<title>뉴토끼 최신주소 안내</title>'.encode()):
            with self.subTest(home=home):
                fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL),
                                            (home, "https://toki34.com/"), (home, "https://toki34.com/index.php"),
                                            (home, "https://toki36.com/"), (home, "https://toki36.com/index.php")])
                with self.assertRaisesRegex(ValueError, "홈페이지를 확인하지 못했습니다"):
                    domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel)

    def test_text_redirect_returns_verified_final_origin(self):
        fetcher = Mock(side_effect=[(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL),
                                   (CurrentDomainTests.refresh("https://toki35.com/"), "https://toki34.com/"),
                                   (self.HOME, "https://toki35.com/")])
        self.assertEqual(domains.resolve_current_text_domain(fetcher, threading.Event(), check_cancel=CurrentDomainTests.check_cancel),
                         "https://toki35.com")

    def test_text_guide_cancellation_does_not_fetch_the_announced_address(self):
        cancel = threading.Event()
        def response(*args, **kwargs):
            cancel.set()
            return self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL
        fetcher = Mock(side_effect=response)
        with self.assertRaises(InterruptedError):
            domains.resolve_current_text_domain(fetcher, cancel, check_cancel=CurrentDomainTests.check_cancel)
        self.assertEqual(fetcher.call_count, 1)


if __name__ == "__main__":
    unittest.main()

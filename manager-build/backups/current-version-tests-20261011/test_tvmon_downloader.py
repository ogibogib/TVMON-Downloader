"""Focused downloader checks; all HTTP responses are local test fixtures."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import Mock, patch

import tvmon_downloader as downloader
import tvmon_domains as domains


BASE_URL = "https://media.example/v/e/video/c.html?token=test"
PAGE_URL = "https://tvmon1.com/ent/986/406784622"


def playlist(count=1, prefix=""):
    lines = ["#EXTM3U", prefix]
    for index in range(count):
        lines.extend(["#EXTINF:2.002,", f"segment{index}.gif"])
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines).encode()


def packet(marker):
    return b"\x47" + bytes([marker]) * 187


class TextDownloadTests(unittest.TestCase):
    URL = "https://toki92.com/novel/123/456"

    def test_utf8_paragraphs_safe_filename_collision_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            existing = folder / "시험_ 작품.txt"
            existing.write_bytes(b"existing")
            events = []
            request = Mock(return_value=("첫 문단\r\n\r\n둘째 문단\r끝".encode(), self.URL))
            target = downloader.download_text(self.URL, folder, threading.Event(), lambda *item: events.append(item),
                                              title="시험: 작품", browser_fetch=request)
            self.assertEqual(target.name, "시험_ 작품 (1).txt")
            self.assertEqual(target.read_bytes(), "첫 문단\n\n둘째 문단\n끝\n".encode())
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertFalse(list(folder.glob("*.part")))
            self.assertEqual(events[-1], ("complete", str(target)))

    def test_failed_or_cancelled_read_never_publishes_a_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for body, final in ((b"", self.URL), (b"\xff", self.URL), (b"text", self.URL + "1")):
                with self.subTest(body=body), self.assertRaises(downloader.DownloadError):
                    downloader.download_text(self.URL, folder, threading.Event(), Mock(), title="test",
                                             browser_fetch=Mock(return_value=(body, final)))
            cancel = threading.Event()
            def request(url, *args, **kwargs):
                cancel.set()
                return b"text", url
            with self.assertRaises(downloader.Cancelled):
                downloader.download_text(self.URL, folder, cancel, Mock(), title="test", browser_fetch=request)
            self.assertEqual(list(folder.iterdir()), [])

    def test_verification_and_disk_failure_preserve_existing_files_and_remove_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaises(downloader.BrowserVerificationRequired):
                downloader.download_text(self.URL, folder, threading.Event(), Mock(), title="test",
                    browser_fetch=Mock(side_effect=downloader.BrowserChallengeError(self.URL)))
            with patch.object(downloader.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                downloader.download_text(self.URL, folder, threading.Event(), Mock(), title="test",
                    browser_fetch=Mock(return_value=(b"sample", self.URL)))
            self.assertEqual(list(folder.iterdir()), [])


class TextZipTests(unittest.TestCase):
    def test_cleanup_deletes_only_selected_files_matching_verified_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = [folder / f"{n}.txt" for n in range(3)]
            for n, path in enumerate(files):
                path.write_bytes(f"chapter {n}".encode())
            archive = downloader.save_text_zip(files, folder, threading.Event(), Mock(), title="test")
            files[2].write_bytes(b"edited after archiving")
            result = downloader.cleanup_text_zip(archive, [(str(n), p, n != 1) for n, p in enumerate(files)], threading.Event())
            self.assertEqual(result["deleted"], ["0"])
            self.assertEqual(len(result["errors"]), 1)
            self.assertFalse(files[0].exists())
            self.assertEqual(files[1].read_bytes(), b"chapter 1")
            self.assertEqual(files[2].read_bytes(), b"edited after archiving")

    def test_cleanup_verifies_whole_zip_before_any_delete_and_cancel_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = [folder / f"{n}.txt" for n in range(2)]
            for path in files:
                path.write_bytes(b"chapter")
            archive = downloader.save_text_zip(files, folder, threading.Event(), Mock(), title="test")
            sources = [(str(n), path, True) for n, path in enumerate(files)]
            cancel = threading.Event()
            cancel.set()
            self.assertTrue(downloader.cleanup_text_zip(archive, sources, cancel)["errors"])
            archive.write_bytes(b"corrupted ZIP")
            result = downloader.cleanup_text_zip(archive, sources, threading.Event())
            self.assertEqual(result["deleted"], [])
            self.assertTrue(result["errors"])
            self.assertTrue(all(path.read_bytes() == b"chapter" for path in files))

    def test_archive_accepts_owned_temp_folder_and_avoids_duplicate_entry_names(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            temp = folder / ".tvmon-zip" / ("a" * 32)
            temp.mkdir(parents=True)
            original, transient = folder / "same.txt", temp / "same.txt"
            original.write_bytes(b"original")
            transient.write_bytes(b"temporary")
            target = downloader.save_text_zip([original, transient], folder, threading.Event(), Mock(), title="test", temporary_folder=temp)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.namelist(), ["same.txt", "same (1).txt"])
                self.assertEqual(archive.read("same (1).txt"), b"temporary")
            with self.assertRaises(downloader.DownloadError):
                downloader.save_text_zip([original], folder, threading.Event(), Mock(), title="test", temporary_folder=folder.parent)

    def test_zip_contains_every_utf8_file_in_order_and_preserves_existing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = [folder / f"시험 {n}화.txt" for n in (1, 2, 3)]
            for n, path in enumerate(files):
                path.write_bytes(f"시험 문단 {n}\n\n다음 문단\n".encode())
            (folder / "작품.zip").write_bytes(b"existing archive")
            events = []
            target = downloader.save_text_zip(files, folder, threading.Event(), lambda *item: events.append(item), title="작품")
            self.assertEqual(target.name, "작품 (1).zip")
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.namelist(), [p.name for p in files])
                self.assertIsNone(archive.testzip())
                for path in files:
                    self.assertEqual(archive.read(path.name), path.read_bytes())
            self.assertEqual((folder / "작품.zip").read_bytes(), b"existing archive")
            self.assertEqual(events[-1], ("complete", str(target)))
            self.assertFalse(list(folder.glob("*.part")))

    def test_missing_empty_or_duplicate_input_cannot_create_partial_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            valid, empty, missing = folder / "one.txt", folder / "empty.txt", folder / "missing.txt"
            valid.write_text("test", encoding="utf-8")
            empty.touch()
            for files in ([], [valid, empty], [valid, missing], [valid, valid]):
                with self.subTest(files=files), self.assertRaises(downloader.DownloadError):
                    downloader.save_text_zip(files, folder, threading.Event(), Mock(), title="test")
                self.assertFalse(list(folder.glob("*.zip*")))

    def test_cancel_and_disk_failure_remove_zip_part_but_keep_txt(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = [folder / f"{n}.txt" for n in range(3)]
            for path in files:
                path.write_bytes(b"synthetic text")
            cancel = threading.Event()
            def report(kind, value):
                if kind == "progress":
                    cancel.set()
            with self.assertRaises(downloader.Cancelled):
                downloader.save_text_zip(files, folder, cancel, report, title="test")
            with patch.object(downloader.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                downloader.save_text_zip(files, folder, threading.Event(), Mock(), title="test")
            self.assertEqual(sorted(folder.iterdir()), files)
            self.assertTrue(all(path.read_bytes() == b"synthetic text" for path in files))


class PageAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(domains.set_domains, domains.get_domains())
        domains.set_domains(domains.DEFAULT_DOMAINS)

    def test_example_and_canonical_url(self):
        self.assertEqual(downloader.parse_page_url(PAGE_URL),
                         (PAGE_URL, "ent", "986", "406784622"))
        self.assertEqual(downloader.parse_page_url(
            "  https://WWW.TVmon1.com:443/ent/986/406784622/?tracking=1#video  "),
            (PAGE_URL, "ent", "986", "406784622"))

    def test_invalid_or_unrelated_page_urls(self):
        for value in ("", "http://tvmon1.com/ent/986/406784622",
                      "https://tvmon1.com.evil.example/ent/986/406784622",
                      "https://tvmon1.com@evil.example/ent/986/406784622",
                      "https://user:pass@tvmon1.com/ent/986/406784622",
                      "https://tvmon1.com:444/ent/986/406784622",
                      "https://tvmon1.com:bad/ent/986/406784622",
                      "https://tvmon1.com/ent/986", "https://tvmon1.com/ent/no/1"):
            with self.subTest(value=value), self.assertRaises(downloader.DownloadError):
                downloader.parse_page_url(value)

    def test_registered_mirror_page_has_same_queue_identity(self):
        self.assertEqual(downloader.parse_page_url(
            "https://WWW.TVwiki52.net:443/ent/986/406784622/?tracking=1#video"),
            (PAGE_URL, "ent", "986", "406784622"))

    def test_custom_domain_and_legacy_page_work_after_removing_default_domains(self):
        domains.set_domains(["next.example"])
        self.assertEqual(downloader.parse_page_url("https://next.example/ent/986/406784622"),
                         (PAGE_URL, "ent", "986", "406784622"))
        self.assertEqual(downloader.parse_page_url(PAGE_URL),
                         (PAGE_URL, "ent", "986", "406784622"))
        with self.assertRaises(downloader.DownloadError):
            downloader.parse_page_url("https://tvwiki52.net/ent/986/406784622")

    def test_aes_sequence_ivs_and_relative_key(self):
        body = playlist(2, '#EXT-X-MEDIA-SEQUENCE:7\n'
                        '#EXT-X-KEY:METHOD=AES-128,URI="/v/key7?c=test"')
        segments = downloader.parse_manifest(body, BASE_URL)
        self.assertEqual(segments[0],
                         ("https://media.example/v/e/video/segment0.gif",
                          "https://media.example/v/key7?c=test", (7).to_bytes(16, "big"), 2.002))
        self.assertEqual(segments[1][2], (8).to_bytes(16, "big"))

    def test_explicit_iv_and_key_rotation_to_unencrypted(self):
        body = (b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:19\n'
                b'#EXT-X-KEY:METHOD=AES-128,URI="keys/first",IV=0x112233\n'
                b'#EXTINF:1,\na.gif\n#EXTINF:1,\nb.gif\n'
                b'#EXT-X-KEY:METHOD=AES-128,URI="keys/second"\n'
                b'#EXTINF:1,\nc.gif\n#EXT-X-KEY:METHOD=NONE\n'
                b'#EXTINF:1,\nd.gif\n#EXT-X-ENDLIST\n')
        segments = downloader.parse_manifest(body, BASE_URL)
        self.assertEqual(segments[0][2], int("112233", 16).to_bytes(16, "big"))
        self.assertEqual(segments[1][2], segments[0][2])
        self.assertEqual(segments[2][1], "https://media.example/v/e/video/keys/second")
        self.assertEqual(segments[2][2], (21).to_bytes(16, "big"))
        self.assertIsNone(segments[3][1])

    def test_invalid_and_unsupported_playlists(self):
        bodies = (b"<html>blocked</html>", b"#EXTM3U\n#EXTINF:2,\na.gif\n",
                  b"#EXTM3U\na.gif\n#EXT-X-ENDLIST",
                  playlist(prefix='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"'),
                  playlist(prefix='#EXT-X-KEY:METHOD=AES-128,URI="key",IV=bad-IV'),
                  playlist(prefix='#EXT-X-STREAM-INF:BANDWIDTH=1000'),
                  playlist(prefix='#EXT-X-MAP:URI="init.mp4"'))
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(downloader.DownloadError):
                downloader.parse_manifest(body, BASE_URL)

    def test_invalid_segment_durations_are_rejected(self):
        for value in ("nan", "inf", "-inf", "-2.002", "0"):
            body = f"#EXTM3U\n#EXTINF:{value},\nsegment.gif\n#EXT-X-ENDLIST\n".encode()
            with self.subTest(duration=value), self.assertRaises(downloader.DownloadError):
                downloader.parse_manifest(body, BASE_URL)

    def test_windows_safe_filenames(self):
        self.assertEqual(downloader.safe_filename('  나는 <솔로>: "회차"? /\\|*\x00. '),
                         '나는 _솔로__ _회차__ _____')
        self.assertEqual(downloader.safe_filename(" ... "), "TVMON 영상")
        self.assertEqual(downloader.safe_filename("con.txt"), "_con.txt")
        self.assertEqual(downloader.safe_filename("LPT9"), "_LPT9")
        self.assertEqual(len(downloader.safe_filename("가" * 200)), 120)


class AvailableDomainTests(unittest.TestCase):
    GUIDE = ('<dl><dt>현재 안내 주소</dt><dd>tvwiki.store</dd></dl>'
             '<a href="https://tvwiki.store/">바로가기</a>').encode()
    BRIDGE = b'<meta http-equiv="refresh" content="0;url=https://tvwiki52.net/">'
    HOME = b'<title>TVWIKI</title><a href="/drama/123">Drama</a>'

    def run_lookup(self, registered, replies):
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
            result = downloader._find_available_domain(tuple(registered), threading.Event())
        return result, [call.args[0] for call in fetcher.call_args_list]

    def test_registered_success_stops_before_other_domains_or_guide(self):
        result, urls = self.run_lookup(["https://tvwiki53.net", "https://tvwiki52.net"],
                                       [(self.HOME, "https://tvwiki53.net/")])
        self.assertEqual(result, ("https://tvwiki53.net", "https://tvwiki53.net", "등록된 도메인"))
        self.assertEqual(urls, ["https://tvwiki53.net/"])

    def test_registered_domains_are_checked_in_saved_order(self):
        result, urls = self.run_lookup(["https://old.example", "https://tvwiki53.net"],
                                       [downloader.DownloadError("offline"), (self.HOME, "https://tvwiki53.net/")])
        self.assertEqual(result[0], "https://tvwiki53.net")
        self.assertEqual(urls, ["https://old.example/", "https://tvwiki53.net/"])

    def guide_replies(self, final):
        return [downloader.DownloadError("registered offline"), (self.GUIDE, domains.DOMAIN_GUIDE_URL),
                (self.BRIDGE, "https://tvwiki.store/"), final]

    def test_guide_is_used_only_after_all_registered_domains_fail(self):
        result, urls = self.run_lookup(["https://old.example"],
                                       self.guide_replies((self.HOME, "https://tvwiki52.net/")))
        self.assertEqual(result, ("https://tvwiki52.net", None, "안내 주소"))
        self.assertEqual(urls, ["https://old.example/", domains.DOMAIN_GUIDE_URL,
                                "https://tvwiki.store/", "https://tvwiki52.net/"])

    def test_numbered_addresses_start_from_final_guide_domain_and_stop_on_success(self):
        replies = self.guide_replies(downloader.DownloadError("52 offline"))
        replies += [downloader.DownloadError("53 offline"), (self.HOME, "https://tvwiki54.net/")]
        result, urls = self.run_lookup(["https://old.example"], replies)
        self.assertEqual(result, ("https://tvwiki54.net", None, "안내 주소 +2"))
        self.assertEqual(urls[-3:], ["https://tvwiki52.net/", "https://tvwiki53.net/", "https://tvwiki54.net/"])
        self.assertEqual(len(urls), 6)

    def test_progress_reports_each_attempt_before_fetch_and_validated_result(self):
        events = []
        replies = self.guide_replies(downloader.DownloadError("52 offline"))
        replies.append((self.HOME, "https://tvwiki53.net/"))

        def fetch(url, *args, **kwargs):
            self.assertEqual(events[-1]["url"], url)
            self.assertEqual(events[-1]["state"], "checking")
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with patch.object(downloader, "_fetch_single", side_effect=fetch):
            result = downloader._find_available_domain(("https://tvwiki52.net",), threading.Event(), report=events.append)
        self.assertEqual(result, ("https://tvwiki53.net", None, "안내 주소 +1"))
        self.assertEqual([event["state"] for event in events],
                         ["checking", "error", "checking", "success", "checking", "success",
                          "checking", "error", "checking", "success"])
        self.assertEqual([event["id"] for event in events[::2]], ["1", "2", "3", "4", "5"])
        self.assertEqual(events[0]["url"], events[6]["url"])
        self.assertEqual(events[3]["detail"], "다음 주소 안내 확인")
        self.assertEqual(events[-1]["detail"], "콘텐츠 홈페이지 확인")
        self.assertEqual(events[-1]["phase"], "안내 주소 +1")

    def test_progress_rejects_invalid_homepage_and_preserves_http_redirect_address(self):
        events = []
        replies = [(b'<title>Error</title>', "https://bad.example/"),
                   (b'<title>Error</title>', "https://bad.example/index.php"),
                   (self.HOME, "https://tvwiki53.net/")]
        with patch.object(downloader, "_fetch_single", side_effect=replies):
            downloader._find_available_domain(("https://bad.example", "https://old.example"),
                                              threading.Event(), report=events.append)
        self.assertEqual(events[1]["state"], "error")
        self.assertIn("홈페이지를 확인하지 못했습니다", events[1]["detail"])
        self.assertEqual(events[3]["state"], "error")
        self.assertEqual(events[-1]["url"], "https://old.example/")
        self.assertEqual(events[-1]["state"], "success")
        self.assertIn("→ https://tvwiki53.net/", events[-1]["detail"])

    def test_numbered_domain_root_failure_tries_index_before_next_domain(self):
        events = []
        replies = self.guide_replies(downloader.DownloadError("52 offline"))
        replies += [(b'<title>TVWIKI</title>', "https://tvwiki53.net/"),
                    (self.HOME, "https://tvwiki53.net/index.php")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
            result = downloader._find_available_domain(("https://old.example",), threading.Event(), report=events.append)
        self.assertEqual(result, ("https://tvwiki53.net", None, "안내 주소 +1"))
        self.assertEqual([call.args[0] for call in fetcher.call_args_list][-2:],
                         ["https://tvwiki53.net/", "https://tvwiki53.net/index.php"])
        self.assertEqual(events[-3]["state"], "error")
        self.assertIn("/index.php", events[-3]["detail"])
        self.assertEqual(events[-1]["state"], "success")
        self.assertEqual(events[-1]["detail"], "콘텐츠 홈페이지 확인")
        self.assertEqual(events[-1]["phase"], "안내 주소 +1")

    def test_cancelled_attempt_reports_cancellation_without_success(self):
        events = []
        with patch.object(downloader, "_fetch_single", side_effect=downloader.Cancelled()), \
                self.assertRaises(downloader.Cancelled):
            downloader._find_available_domain(("https://tvwiki53.net",), threading.Event(), report=events.append)
        self.assertEqual([event["state"] for event in events], ["checking", "cancelled"])

    def test_three_numbered_failures_stop_before_fourth_and_reject_non_homepage(self):
        replies = self.guide_replies(downloader.DownloadError("52 offline"))
        replies += [(b'<title>Error</title>', "https://tvwiki53.net/"),
                    (b'<title>Error</title>', "https://tvwiki53.net/index.php"),
                    downloader.DownloadError("54 offline"), downloader.DownloadError("55 offline")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, \
                self.assertRaises(downloader.DownloadError) as caught:
            downloader._find_available_domain(("https://old.example",), threading.Event())
        self.assertEqual([call.args[0] for call in fetcher.call_args_list][-4:],
                         ["https://tvwiki53.net/", "https://tvwiki53.net/index.php", "https://tvwiki54.net/", "https://tvwiki55.net/"])
        self.assertEqual(fetcher.call_count, 8)
        self.assertIn("모두 접속하지 못했습니다", str(caught.exception))
        for call in fetcher.call_args_list:
            self.assertFalse(call.kwargs["retry"])

    def test_missing_numeric_guide_domain_does_not_guess_a_hostname(self):
        replies = [downloader.DownloadError("registered offline"), (self.GUIDE, domains.DOMAIN_GUIDE_URL),
                   downloader.DownloadError("store offline")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, \
                self.assertRaises(downloader.DownloadError):
            downloader._find_available_domain(("https://old.example",), threading.Event())
        self.assertEqual(fetcher.call_count, 3)

    def test_cancellation_stops_each_stage_without_trying_later_addresses(self):
        for cancel_index in (0, 3, 4):
            cancel = threading.Event()
            replies = self.guide_replies(downloader.DownloadError("52 offline"))
            replies.append(downloader.DownloadError("53 offline"))
            calls = []

            def response(url, *args, **kwargs):
                index = len(calls)
                calls.append(url)
                if index == cancel_index:
                    cancel.set()
                    raise downloader.DownloadError("cancelled network request")
                reply = replies[index]
                if isinstance(reply, Exception):
                    raise reply
                return reply

            with self.subTest(stage=cancel_index), patch.object(downloader, "_fetch_single", side_effect=response), \
                    self.assertRaises(downloader.Cancelled):
                downloader._find_available_domain(("https://old.example",), cancel)
            self.assertEqual(len(calls), cancel_index + 1)


class AvailableTextDomainTests(unittest.TestCase):
    GUIDE = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
    HOME = b'<html><title>NEWTOKI</title><body>Contents</body></html>'

    def test_native_connection_reset_uses_chrome_and_saves_redirect_destination(self):
        events = []
        def native(url, *args):
            if url == domains.TEXT_DOMAIN_GUIDE_URL:
                return self.GUIDE, url
            raise downloader.TransferError("connection reset", 12030)
        client = Mock(get=Mock(side_effect=native))
        browser = Mock(return_value=(self.HOME, "https://toki35.com/"))
        with patch.object(downloader, "_get_http_client", return_value=client):
            result = downloader._find_available_domain((), threading.Event(), text_site=True,
                                                       browser_fetch=browser, report=events.append)
        self.assertEqual(result, ("https://toki35.com", None, "안내 주소"))
        self.assertEqual(client.get.call_count, 2)
        browser.assert_called_once_with("https://toki34.com/", unittest.mock.ANY, limit=2 * 1024 * 1024)
        self.assertTrue(any("연결 오류" in event["detail"] for event in events))
        self.assertEqual(events[-1]["final_url"], "https://toki35.com/")

    def test_second_announced_address_is_used_after_chrome_failure_or_pending_verification(self):
        guide = self.GUIDE + b'<span class="hud-url">toki35.com</span><a href="https://toki35.com/">Mirror</a>'
        for failure in (downloader.TransferError("Chrome unavailable"), TimeoutError("timeout"),
                        downloader.BrowserChallengeError("https://toki34.com/")):
            events = []
            replies = [(guide, domains.TEXT_DOMAIN_GUIDE_URL), downloader._ConnectionDownloadError("Windows 12030"),
                       (self.HOME, "https://toki35.com/")]
            browser = Mock(side_effect=failure)
            with self.subTest(failure=failure), patch.object(downloader, "_fetch_single", side_effect=replies) as request:
                result = downloader._find_available_domain((), threading.Event(), text_site=True,
                                                           browser_fetch=browser, report=events.append)
            self.assertEqual(result, ("https://toki35.com", None, "안내 주소"))
            self.assertEqual([call.args[0] for call in request.call_args_list],
                             [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/", "https://toki35.com/"])
            self.assertEqual([event["state"] for event in events if event["state"] != "checking"],
                             ["success", "error", "success"])

    def test_first_pending_verification_survives_failure_of_other_announced_address(self):
        guide = self.GUIDE + b'<span class="hud-url">toki35.com</span><a href="https://toki35.com/">Mirror</a>'
        replies = [(guide, domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/"),
                   downloader.DownloadError("HTTP 404")]
        events = []
        with patch.object(downloader, "_fetch_single", side_effect=replies) as request, \
                self.assertRaises(downloader.BrowserVerificationRequired) as caught:
            downloader._find_available_domain((), threading.Event(), text_site=True,
                browser_fetch=Mock(side_effect=downloader.BrowserChallengeError("https://toki34.com/")), report=events.append)
        self.assertEqual(caught.exception.url, "https://toki34.com/")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(events[-1]["url"], "https://toki34.com/")
        self.assertEqual(events[-1]["state"], "verification")

    def test_http_status_error_uses_next_announced_address_without_chrome(self):
        guide = self.GUIDE + b'<span class="hud-url">toki35.com</span><a href="https://toki35.com/">Mirror</a>'
        client = Mock(get=Mock(side_effect=[(guide, domains.TEXT_DOMAIN_GUIDE_URL), downloader.HttpStatusError(404),
                                           (self.HOME, "https://toki35.com/")]))
        browser = Mock()
        with patch.object(downloader, "_get_http_client", return_value=client):
            result = downloader._find_available_domain((), threading.Event(), text_site=True, browser_fetch=browser)
        self.assertEqual(result[0], "https://toki35.com")
        browser.assert_not_called()

    def test_browser_cancellation_prevents_other_announced_addresses(self):
        guide = self.GUIDE + b'<span class="hud-url">toki35.com</span><a href="https://toki35.com/">Mirror</a>'
        replies = [(guide, domains.TEXT_DOMAIN_GUIDE_URL), downloader._ConnectionDownloadError("Windows 12030")]
        cancel = threading.Event()
        with patch.object(downloader, "_fetch_single", side_effect=replies) as request, self.assertRaises(downloader.Cancelled):
            downloader._find_available_domain((), cancel, text_site=True, browser_fetch=Mock(side_effect=downloader.TransferCancelled()))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(cancel.is_set())

    def test_custom_guide_is_used_for_numbered_fallback_and_browser_verification(self):
        guide_url = "https://guide.example/notice?site=newtoki"
        for challenged in (False, True):
            events = []
            replies = [(self.GUIDE, guide_url), downloader.DownloadError("offline")]
            replies.append(downloader.BrowserVerificationRequired("https://toki35.com/") if challenged
                           else (self.HOME, "https://toki35.com/"))
            browser = Mock(side_effect=downloader.BrowserChallengeError("https://toki35.com/"))
            with self.subTest(challenged=challenged), patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
                if challenged:
                    with self.assertRaises(downloader.BrowserVerificationRequired):
                        downloader._find_available_domain((), threading.Event(), text_site=True, text_guide_url=guide_url,
                                                          browser_fetch=browser, report=events.append)
                    browser.assert_called_once()
                    self.assertEqual(events[-1]["state"], "verification")
                else:
                    result = downloader._find_available_domain((), threading.Event(), text_site=True, text_guide_url=guide_url,
                                                               report=events.append)
                    self.assertEqual(result, ("https://toki35.com", None, "안내 주소 +1"))
                self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                                 [guide_url, "https://toki34.com/", "https://toki35.com/"])

    def test_browser_challenge_stops_before_numbered_hosts(self):
        events = []
        replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, \
                self.assertRaises(downloader.BrowserVerificationRequired):
            downloader._find_available_domain((), threading.Event(), text_site=True, report=events.append)
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(events[-1]["state"], "verification")
        self.assertNotIn("+1", events[-1]["phase"])

    def test_browser_success_verifies_same_domain_and_pending_challenge_never_increments(self):
        for result in ((self.HOME, "https://toki34.com/"), downloader.BrowserChallengeError("https://toki34.com/")):
            browser = Mock(return_value=result) if isinstance(result, tuple) else Mock(side_effect=result)
            replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/")]
            with self.subTest(result=result), patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
                if isinstance(result, tuple):
                    self.assertEqual(downloader._find_available_domain((), threading.Event(), text_site=True, browser_fetch=browser),
                                     ("https://toki34.com", None, "안내 주소"))
                else:
                    with self.assertRaises(downloader.BrowserVerificationRequired):
                        downloader._find_available_domain((), threading.Event(), text_site=True, browser_fetch=browser)
                self.assertEqual(fetcher.call_count, 2)
                self.assertEqual(browser.call_count, 1)
                self.assertEqual(browser.call_args.args[0], "https://toki34.com/")

    def test_challenge_on_numbered_candidate_stops_before_later_numbers(self):
        replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.DownloadError("offline"),
                   downloader.BrowserVerificationRequired("https://toki35.com/")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, \
                self.assertRaises(downloader.BrowserVerificationRequired):
            downloader._find_available_domain((), threading.Event(), text_site=True)
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/", "https://toki35.com/"])

    def test_browser_http_and_homepage_errors_never_move_to_next_number(self):
        for result in (downloader.HttpStatusError(404), downloader.TransferError("Chrome unavailable"), TimeoutError("Chrome timeout"),
                       (b"<title>Unknown page</title>", "https://toki34.com/")):
            replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/"),
                       downloader.DownloadError("HTTP 404")]
            browser = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            events = []
            with self.subTest(result=result), patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, \
                    self.assertRaisesRegex(downloader.DownloadError, "번호 탐색은 중단") as error:
                downloader._find_available_domain((), threading.Event(), text_site=True, browser_fetch=browser, report=events.append)
            self.assertNotIsInstance(error.exception, downloader.BrowserVerificationRequired)
            self.assertTrue(all("toki35" not in call.args[0] for call in fetcher.call_args_list))
            self.assertEqual(events[-1]["state"], "error")
            self.assertNotIn("+1", events[-1]["phase"])

    def test_browser_cancellation_is_not_reported_as_failure_or_retried(self):
        replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/")]
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, self.assertRaises(downloader.Cancelled):
            downloader._find_available_domain((), threading.Event(), text_site=True,
                                              browser_fetch=Mock(side_effect=downloader.TransferCancelled()))
        self.assertEqual(fetcher.call_count, 2)

    def test_hud_then_incremented_domains_stop_on_verified_success(self):
        replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL), downloader.DownloadError("HTTP 403"),
                   downloader.DownloadError("HTTP 403"), (self.HOME, "https://toki36.com/")]
        events = []
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
            result = downloader._find_available_domain((), threading.Event(), text_site=True, report=events.append)
        self.assertEqual(result, ("https://toki36.com", None, "안내 주소 +2"))
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/", "https://toki35.com/", "https://toki36.com/"])
        self.assertEqual([event["state"] for event in events],
                         ["checking", "success", "checking", "error", "checking", "error", "checking", "success"])
        self.assertEqual(events[-1]["detail"], "콘텐츠 홈페이지 확인")

    def test_four_failed_hosts_stop_without_trying_plus_four(self):
        replies = [(self.GUIDE, domains.TEXT_DOMAIN_GUIDE_URL)] + [downloader.DownloadError("HTTP 403")] * 4
        with patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher, self.assertRaises(downloader.DownloadError):
            downloader._find_available_domain((), threading.Event(), text_site=True)
        self.assertEqual([call.args[0] for call in fetcher.call_args_list],
                         [domains.TEXT_DOMAIN_GUIDE_URL, *[f"https://toki{number}.com/" for number in range(34, 38)]])
        self.assertTrue(all(call.kwargs["retry"] is False for call in fetcher.call_args_list))

    def test_invalid_guide_does_not_guess_from_previous_video_domain(self):
        with patch.object(downloader, "_fetch_single", return_value=(b'<title>Error</title>', domains.TEXT_DOMAIN_GUIDE_URL)) as fetcher, \
                self.assertRaises(downloader.DownloadError):
            downloader._find_available_domain((), threading.Event(), text_site=True)
        self.assertEqual(fetcher.call_count, 1)

    def test_cancellation_after_announced_failure_prevents_incremented_requests(self):
        cancel = threading.Event()
        calls = []
        def response(url, *args, **kwargs):
            calls.append(url)
            if url == domains.TEXT_DOMAIN_GUIDE_URL:
                return self.GUIDE, url
            cancel.set()
            raise downloader.DownloadError("cancelled")
        with patch.object(downloader, "_fetch_single", side_effect=response), self.assertRaises(downloader.Cancelled):
            downloader._find_available_domain((), cancel, text_site=True)
        self.assertEqual(calls, [domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(domains.set_domains, domains.get_domains())
        domains.set_domains(domains.DEFAULT_DOMAINS)
        ech_patch = patch.object(downloader, "get_with_ech", side_effect=downloader.TransferError("fixture offline"))
        self.ech = ech_patch.start()
        self.addCleanup(ech_patch.stop)

    def test_failed_site_connection_uses_ech_before_next_domain(self):
        domains.set_domains(["tvwiki54.net", "tvmon1.com"])
        url = "https://tvwiki54.net/bbs/search.php?stx=fixture"
        client = Mock(get=Mock(side_effect=downloader.TransferError("reset", 12030)))
        self.ech.side_effect = None
        self.ech.return_value = (b"site response", url)
        events = []
        with patch.object(downloader, "_get_http_client", return_value=client):
            self.assertEqual(downloader.fetch(url, threading.Event(), url, on_event=lambda event, **fields: events.append(event)),
                             (b"site response", url))
        self.assertEqual(client.get.call_count, 1)
        self.ech.assert_called_once()
        self.assertEqual(self.ech.call_args.args[0], url)
        self.assertIn("http_ech_request", events)
        self.assertIn("http_ech_response", events)

    def test_ech_cancel_stops_before_next_domain(self):
        client = Mock(get=Mock(side_effect=downloader.TransferError("reset", 12030)))
        self.ech.side_effect = downloader.TransferCancelled()
        with patch.object(downloader, "_get_http_client", return_value=client), self.assertRaises(downloader.Cancelled):
            downloader.fetch(PAGE_URL, threading.Event(), PAGE_URL)
        self.assertEqual(client.get.call_count, 1)
        self.ech.assert_called_once()

    def test_domain_check_logs_ech_retry_and_validates_same_domain_without_browser(self):
        url = "https://tvwiki54.net/"
        client = Mock(get=Mock(side_effect=downloader.TransferError("reset", 12030)))
        self.ech.side_effect = None
        self.ech.return_value = (AvailableDomainTests.HOME, url)
        events = []
        with patch.object(downloader, "_get_http_client", return_value=client):
            result = downloader._find_available_domain(("https://tvwiki54.net", "https://tvmon1.com"),
                                                       threading.Event(), report=events.append)
        self.assertEqual(result, ("https://tvwiki54.net", "https://tvwiki54.net", "등록된 도메인"))
        self.assertEqual(client.get.call_count, 1)
        self.assertTrue(any("ECH/DoH" in event["detail"] for event in events))
        self.assertEqual(events[-1]["state"], "success")

    def test_http_errors_and_external_media_do_not_use_ech(self):
        for url, error in ((PAGE_URL, downloader.HttpStatusError(403)),
                           (BASE_URL, downloader.TransferError("reset", 12030))):
            client = Mock(get=Mock(side_effect=error))
            self.ech.reset_mock()
            with patch.object(downloader, "_get_http_client", return_value=client), self.assertRaises(downloader.DownloadError):
                downloader._fetch_single(url, threading.Event(), PAGE_URL, limit=1024, retry=False)
            self.ech.assert_not_called()

    def test_connection_error_identifies_host_and_native_code_without_private_query(self):
        client = Mock(get=Mock(side_effect=downloader.TransferError("interrupted", 12030)))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                self.assertRaises(downloader.DownloadError) as error:
            downloader._fetch_single("https://tvwiki52.net/?token=private-token", threading.Event(),
                                     "https://tvwiki.store/", limit=1024, retry=False)
        self.assertIn("tvwiki52.net", str(error.exception))
        self.assertIn("12030", str(error.exception))
        self.assertNotIn("private-token", str(error.exception))

    def test_native_browser_challenge_propagates_without_network_retry(self):
        client = Mock(get=Mock(side_effect=downloader.BrowserChallengeError("https://toki34.com/")))
        with patch.object(downloader, "_get_http_client", return_value=client), self.assertRaises(downloader.BrowserVerificationRequired):
            downloader._fetch_single("https://toki34.com/", threading.Event(), domains.TEXT_DOMAIN_GUIDE_URL, limit=1024)
        self.assertEqual(client.get.call_count, 1)

    def test_site_failure_tries_next_domain_with_identical_address_suffix_and_headers(self):
        path = "/bbs/get_episode.php?bo_table=world&token=a%2Fb&x=1&x=2#part"
        referer_path = "/world/2558/1?from=search#player"
        for error in (downloader.HttpStatusError(503), downloader.TransferError("connection interrupted")):
            cancel = threading.Event()
            on_bytes = Mock()
            client = Mock(get=Mock(side_effect=[error, (b"episode", domains.DEFAULT_DOMAINS[1] + path)]))
            with self.subTest(error=type(error).__name__):
                with patch.object(downloader, "_get_http_client", return_value=client), \
                        patch.object(cancel, "wait", return_value=False) as retry_wait:
                    result = downloader.fetch(domains.CANONICAL_ORIGIN + path, cancel,
                                              domains.CANONICAL_ORIGIN + referer_path,
                                              limit=1024, on_bytes=on_bytes)
                self.assertEqual(result, (b"episode", domains.DEFAULT_DOMAINS[1] + path))
                self.assertEqual(client.get.call_count, 2)
                retry_wait.assert_not_called()
                for call, origin in zip(client.get.call_args_list, domains.DEFAULT_DOMAINS):
                    self.assertEqual(call.args[0], origin + path)
                    self.assertEqual(call.args[1]["Referer"], origin + referer_path)
                    self.assertEqual(call.args[1]["Origin"], origin)
                    self.assertEqual(call.args[1]["Accept-Encoding"], "identity")
                    self.assertIs(call.args[2], cancel)
                    self.assertEqual(call.args[3], 1024)
                    self.assertIs(call.args[4], on_bytes)

    def test_successful_first_domain_does_not_request_remaining_domains(self):
        domains.set_domains(["tvwiki52.net", "tvmon1.com"])
        expected = "https://tvwiki52.net/ent/986/406784622"
        client = Mock(get=Mock(return_value=(b"page", expected)))
        with patch.object(downloader, "_get_http_client", return_value=client):
            result = downloader.fetch(PAGE_URL, threading.Event(), PAGE_URL)
        self.assertEqual(result, (b"page", expected))
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.args[0], expected)

    def test_next_request_starts_at_saved_first_domain_after_mirror_success(self):
        mirror_url = "https://tvwiki52.net/ent/986/406784622"
        client = Mock(get=Mock(side_effect=[downloader.TransferError("connection interrupted"),
                                           (b"mirror", mirror_url), (b"primary", PAGE_URL)]))
        cancel = threading.Event()
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait", return_value=False):
            self.assertEqual(downloader.fetch(PAGE_URL, cancel, PAGE_URL), (b"mirror", mirror_url))
            self.assertEqual(downloader.fetch(PAGE_URL, cancel, PAGE_URL), (b"primary", PAGE_URL))
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         [PAGE_URL, mirror_url, PAGE_URL])

    def test_single_custom_domain_is_used_for_an_existing_canonical_queue_url(self):
        domains.set_domains(["next.example"])
        expected = "https://next.example/ent/986/406784622"
        client = Mock(get=Mock(return_value=(b"page", expected)))
        with patch.object(downloader, "_get_http_client", return_value=client):
            result = downloader.fetch(PAGE_URL, threading.Event(), PAGE_URL)
        self.assertEqual(result, (b"page", expected))
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.args[0], expected)
        self.assertEqual(client.get.call_args.args[1]["Referer"], expected)

    def test_all_site_domains_fail_once_in_saved_order_and_error_omits_url_token(self):
        domains.set_domains(["next.example", "tvwiki52.net", "tvmon1.com"])
        url = "https://tvmon1.com/bbs/get_episode.php?token=private-token"
        cancel = threading.Event()
        client = Mock(get=Mock(side_effect=downloader.TransferError("connection interrupted")))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait", return_value=False) as retry_wait, \
                self.assertRaises(downloader.DownloadError) as error:
            downloader.fetch(url, cancel, PAGE_URL)
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         [origin + "/bbs/get_episode.php?token=private-token" for origin in domains.get_domains()])
        retry_wait.assert_not_called()
        for hostname in ("next.example", "tvwiki52.net", "tvmon1.com"):
            self.assertIn(hostname, str(error.exception))
        self.assertNotIn("private-token", str(error.exception))

    def test_site_cancellation_stops_before_next_domain(self):
        for native_cancel in (False, True):
            cancel = threading.Event()

            def get_response(*args):
                if native_cancel:
                    raise downloader.TransferCancelled()
                cancel.set()
                raise downloader.TransferError("connection interrupted")

            client = Mock(get=Mock(side_effect=get_response))
            with self.subTest(native_cancel=native_cancel), \
                    patch.object(downloader, "_get_http_client", return_value=client), \
                    patch.object(cancel, "wait") as retry_wait, \
                    self.assertRaises(downloader.Cancelled):
                downloader.fetch(PAGE_URL, cancel, PAGE_URL)
            self.assertEqual(client.get.call_count, 1)
            retry_wait.assert_not_called()

    def test_site_retry_disabled_still_tries_each_domain_without_repeating_a_host(self):
        cancel = threading.Event()
        client = Mock(get=Mock(side_effect=downloader.HttpStatusError(503)))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait") as retry_wait, \
                self.assertRaises(downloader.DownloadError):
            downloader.fetch("https://tvmon1.com/images/episode.webp", cancel, PAGE_URL, retry=False)
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         [origin + "/images/episode.webp" for origin in domains.DEFAULT_DOMAINS])
        retry_wait.assert_not_called()

    def test_external_media_keeps_url_and_site_referer_with_existing_three_attempts(self):
        domains.set_domains(["tvwiki52.net", "next.example"])
        cancel = threading.Event()
        client = Mock(get=Mock(side_effect=downloader.TransferError("connection interrupted")))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait", return_value=False) as retry_wait, \
                self.assertRaises(downloader.DownloadError):
            downloader.fetch(BASE_URL, cancel, PAGE_URL)
        self.assertEqual(client.get.call_count, 3)
        self.assertEqual(retry_wait.call_count, 2)
        for call in client.get.call_args_list:
            self.assertEqual(call.args[0], BASE_URL)
            self.assertEqual(call.args[1]["Referer"], PAGE_URL)
            self.assertEqual(call.args[1]["Origin"], domains.CANONICAL_ORIGIN)

    def test_retry_disabled_makes_one_request_even_for_transient_failures(self):
        for error in (downloader.HttpStatusError(503), downloader.TransferError("interrupted"), TimeoutError()):
            cancel = threading.Event()
            client = Mock(get=Mock(side_effect=error))
            with self.subTest(error=type(error).__name__), \
                    patch.object(downloader, "_get_http_client", return_value=client), \
                    patch.object(cancel, "wait") as retry_wait, \
                    self.assertRaises(downloader.DownloadError):
                downloader.fetch(BASE_URL, cancel, downloader.PLAYER_ORIGIN + "/", retry=False)
            self.assertEqual(client.get.call_count, 1)
            retry_wait.assert_not_called()

    def test_cancellation_during_final_failed_attempt_is_reported_as_cancelled(self):
        for error_type in (downloader.HttpStatusError, downloader.TransferError):
            cancel = threading.Event()
            attempts = []

            def get_response(url, headers, request_cancel, limit, on_bytes):
                attempts.append(url)
                if len(attempts) == 3:
                    cancel.set()
                if error_type is downloader.HttpStatusError:
                    code = 403 if len(attempts) == 3 else 503
                    raise downloader.HttpStatusError(code)
                raise downloader.TransferError("test connection interrupted")

            client = Mock(get=Mock(side_effect=get_response))
            with self.subTest(error=error_type.__name__), \
                    patch.object(downloader, "_get_http_client", return_value=client), \
                    patch.object(cancel, "wait", return_value=False) as retry_wait, \
                    self.assertRaises(downloader.Cancelled):
                downloader.fetch(BASE_URL, cancel, downloader.PLAYER_ORIGIN + "/")
            self.assertEqual(len(attempts), 3)
            self.assertEqual(retry_wait.call_count, 2)

    def test_truncated_attempts_are_retried_and_transfer_callback_is_preserved(self):
        attempts = iter((b"abc", b"ab", b"abcde"))
        received = []

        def get_response(url, headers, request_cancel, limit, on_bytes):
            data = next(attempts)
            on_bytes(len(data))
            if len(data) != 5:
                raise downloader.TransferError("incomplete response")
            return data, BASE_URL

        client = Mock(get=Mock(side_effect=get_response))
        cancel = threading.Event()
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait", return_value=False):
            result = downloader.fetch(BASE_URL, cancel, downloader.PLAYER_ORIGIN + "/",
                                      limit=1024, on_bytes=received.append)
        self.assertEqual(result, (b"abcde", BASE_URL))
        self.assertEqual(client.get.call_count, 3)
        self.assertEqual(received, [3, 2, 5])
        for call in client.get.call_args_list:
            self.assertEqual(call.args[0], BASE_URL)
            self.assertIs(call.args[2], cancel)
            self.assertEqual(call.args[3], 1024)
            self.assertEqual(call.args[1]["Referer"], downloader.PLAYER_ORIGIN + "/")
            self.assertEqual(call.args[1]["Accept-Encoding"], "identity")

    def test_native_cancellation_does_not_retry(self):
        cancel = threading.Event()
        client = Mock(get=Mock(side_effect=downloader.TransferCancelled()))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait") as retry_wait, \
                self.assertRaises(downloader.Cancelled):
            downloader.fetch(BASE_URL, cancel, downloader.PLAYER_ORIGIN + "/")
        self.assertEqual(client.get.call_count, 1)
        retry_wait.assert_not_called()

    def test_non_transient_http_failure_does_not_retry(self):
        cancel = threading.Event()
        client = Mock(get=Mock(side_effect=downloader.HttpStatusError(403)))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                patch.object(cancel, "wait") as retry_wait, \
                self.assertRaisesRegex(downloader.DownloadError, "HTTP 403"):
            downloader.fetch(BASE_URL, cancel, downloader.PLAYER_ORIGIN + "/")
        self.assertEqual(client.get.call_count, 1)
        retry_wait.assert_not_called()

    def test_http_failure_names_media_host_without_exposing_query(self):
        client = Mock(get=Mock(side_effect=downloader.HttpStatusError(403)))
        with patch.object(downloader, "_get_http_client", return_value=client), \
                self.assertRaises(downloader.DownloadError) as caught:
            downloader.fetch(BASE_URL, threading.Event(), downloader.PLAYER_ORIGIN + "/")
        self.assertIn("media.example", str(caught.exception))
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("token=test", str(caught.exception))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.cancel = threading.Event()
        self.events = []

    def test_legacy_queue_download_uses_current_mirror_api_and_original_media_server(self):
        previous = domains.get_domains()
        self.addCleanup(domains.set_domains, previous)
        domains.set_domains(["tvwiki53.net", "tvmon1.com"])
        requests = []

        def get(url, headers, cancel, limit, on_bytes):
            requests.append((url, headers))
            if url.startswith("https://tvwiki53.net/bbs/get_episode.php?"):
                body = json.dumps({"success": True, "episode": {"title": "미러 영상", "hls_url": BASE_URL}}).encode()
            elif url == BASE_URL:
                body = playlist()
            elif url == "https://media.example/v/e/video/segment0.gif":
                body = packet(7)
            else:
                raise AssertionError(f"Unexpected download host or path: {url}")
            if on_bytes:
                on_bytes(len(body))
            return body, url

        client = Mock(get=Mock(side_effect=get))
        with patch.object(downloader, "_get_http_client", return_value=client):
            output = downloader.download_video(PAGE_URL, self.folder, self.cancel,
                                               lambda kind, value: self.events.append((kind, value)))
        self.assertEqual(output.read_bytes(), packet(7))
        self.assertEqual(requests[0][0], "https://tvwiki53.net/bbs/get_episode.php?bo_table=ent&wr_id=986&ep_idx=406784622")
        self.assertEqual(requests[0][1]["Referer"], "https://tvwiki53.net/ent/986/406784622")
        self.assertEqual(requests[0][1]["Origin"], "https://tvwiki53.net")
        self.assertEqual([row[0] for row in requests[1:]], [BASE_URL, "https://media.example/v/e/video/segment0.gif"])
        self.assertTrue(all(row[1]["Referer"] == downloader.PLAYER_ORIGIN + "/" for row in requests[1:]))
        statuses = [value for kind, value in self.events if kind == "status"]
        self.assertIn("https://tvwiki53.net/ent/986/406784622", statuses[0])
        self.assertIn("media.example", statuses[1])

    def run_download(self, body, segment_fetch, title="테스트 영상", report=None):
        def fake_fetch(url, cancel, referer, **kwargs):
            if url == BASE_URL:
                return body, BASE_URL
            data = segment_fetch(url, cancel)
            if kwargs.get("on_bytes"):
                kwargs["on_bytes"](len(data))
            return data, url

        with patch.object(downloader, "resolve_episode", return_value=(title, BASE_URL)), \
                patch.object(downloader, "fetch", side_effect=fake_fetch):
            return downloader.download_video(
                PAGE_URL, self.folder, self.cancel,
                report or (lambda kind, value: self.events.append((kind, value))))

    def test_four_workers_save_in_playlist_order(self):
        barrier = threading.Barrier(4)
        three_finished = threading.Event()
        lock = threading.Lock()
        completion_order = []
        active = 0
        peak = 0

        def fetch_segment(url, cancel):
            nonlocal active, peak
            index = int(Path(url).stem.removeprefix("segment"))
            with lock:
                active += 1
                peak = max(peak, active)
            if index < 4:
                barrier.wait(timeout=3)
                if index == 0:
                    if not three_finished.wait(timeout=3):
                        raise AssertionError("Other segment requests did not finish")
                else:
                    with lock:
                        completion_order.append(index)
                        if all(initial in completion_order for initial in (1, 2, 3)):
                            three_finished.set()
            with lock:
                if index == 0 or index >= 4:
                    completion_order.append(index)
                active -= 1
            return packet(index)

        with patch.object(downloader, "WORKERS", 4):
            target = self.run_download(playlist(8), fetch_segment)
        self.assertEqual(peak, 4)
        self.assertTrue(all(completion_order.index(index) < completion_order.index(0)
                            for index in (1, 2, 3)))
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(8)))
        progress = [value for kind, value in self.events if kind == "progress"]
        self.assertEqual([value["done"] for value in progress], list(range(1, 9)))
        self.assertEqual(progress[-1]["bytes"], 8 * 188)
        self.assertEqual(self.events[-1], ("complete", str(target)))
        self.assertFalse(list(self.folder.glob("*.part")))

    def test_eight_workers_replenish_before_slow_first_segment_finishes(self):
        barrier = threading.Barrier(8)
        later_started = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def fetch_segment(url, cancel):
            nonlocal active, peak
            index = int(Path(url).stem.removeprefix("segment"))
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if index < 8:
                    barrier.wait(timeout=5)
                    if index == 0 and not later_started.wait(timeout=5):
                        raise AssertionError("Completed workers did not start later segments")
                else:
                    later_started.set()
                return packet(index)
            finally:
                with lock:
                    active -= 1

        target = self.run_download(playlist(24), fetch_segment)
        self.assertEqual(peak, 8)
        self.assertTrue(later_started.is_set())
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(24)))
        self.assertFalse(list(self.folder.glob("*.part")))

    def test_slow_first_segment_does_not_stall_completed_segment_spooling(self):
        count = 24
        barrier = threading.Barrier(8)
        first_release = threading.Event()
        others_saved = threading.Event()
        lock = threading.Lock()
        started = []
        results = []
        errors = []
        active = 0
        peak = 0

        def fetch_segment(url, cancel):
            nonlocal active, peak
            index = int(Path(url).stem.removeprefix("segment"))
            with lock:
                started.append(index)
                active += 1
                peak = max(peak, active)
            try:
                if index < 8:
                    barrier.wait(timeout=5)
                if index == 0 and not first_release.wait(timeout=8):
                    raise AssertionError("First segment was not released")
                return packet(index)
            finally:
                with lock:
                    active -= 1

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "progress" and value["done"] == count - 1:
                others_saved.set()

        def run():
            try:
                results.append(self.run_download(playlist(count), fetch_segment, report=report))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(others_saved.wait(timeout=5),
                            "Later segments stalled while the first segment was slow")
            with lock:
                self.assertEqual(set(started), set(range(count)))
                self.assertEqual(peak, 8)
                self.assertEqual(active, 1)
            saved = [path.read_bytes() for path in self.folder.rglob("*")
                     if path.is_file() and path.stat().st_size]
            self.assertCountEqual(saved, [packet(index) for index in range(1, count)])
        finally:
            first_release.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive(), "Download worker did not finish")
        if errors:
            raise errors[0]
        target = results[0]
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(count)))
        self.assertEqual(list(self.folder.iterdir()), [target])

    def test_transfer_reports_received_bytes_while_segments_are_in_flight(self):
        count = 8
        expected_received = count * 188 + 13
        barrier = threading.Barrier(count)
        release = threading.Event()
        received = threading.Event()
        results = []
        errors = []

        def fake_fetch(url, cancel, referer, **kwargs):
            if url == BASE_URL:
                return playlist(count), BASE_URL
            index = int(Path(url).stem.removeprefix("segment"))
            callback = kwargs.get("on_bytes")
            if callback:
                if index == 0:
                    callback(13)  # Bytes from an interrupted request still used the network.
                callback(188)
            barrier.wait(timeout=5)
            if not release.wait(timeout=5):
                raise AssertionError("In-flight segment requests were not released")
            return packet(index), url

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "transfer" and value["received"] == expected_received:
                received.set()

        def run():
            try:
                with patch.object(downloader, "resolve_episode", return_value=("테스트 영상", BASE_URL)), \
                        patch.object(downloader, "fetch", side_effect=fake_fetch):
                    results.append(downloader.download_video(PAGE_URL, self.folder, self.cancel, report))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(received.wait(timeout=4),
                            "Network bytes were hidden until segment completion")
            self.assertFalse(any(kind == "progress" for kind, _ in self.events))
        finally:
            release.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive(), "Download worker did not finish")
        if errors:
            raise errors[0]
        transfers = [value for kind, value in self.events if kind == "transfer"]
        self.assertEqual(transfers[-1]["received"], expected_received)
        self.assertGreater(transfers[-1]["speed"], 0)
        progress = [value for kind, value in self.events if kind == "progress"]
        self.assertEqual(progress[-1]["bytes"], count * 188)
        self.assertEqual(results[0].read_bytes(), b"".join(packet(index) for index in range(count)))

    def test_slow_cdn_host_does_not_occupy_other_hosts_download_slots(self):
        count = 27
        lines = ["#EXTM3U"]
        for index in range(count):
            lines.extend(["#EXTINF:2.002,",
                          f"https://media{index % 9}.example/segment{index}.gif"])
        lines.append("#EXT-X-ENDLIST")
        body = "\n".join(lines).encode()
        barrier = threading.Barrier(8)
        first_release = threading.Event()
        others_saved = threading.Event()
        lock = threading.Lock()
        started = set()
        finished = set()
        active = 0
        peak = 0
        results = []
        errors = []
        expected_others = {index for index in range(count) if index % 9}

        def fetch_segment(url, cancel):
            nonlocal active, peak
            index = int(Path(url).stem.removeprefix("segment"))
            with lock:
                started.add(index)
                active += 1
                peak = max(peak, active)
            try:
                if index < 8:
                    barrier.wait(timeout=5)
                if index == 0 and not first_release.wait(timeout=8):
                    raise AssertionError("Slow CDN host was not released")
                with lock:
                    finished.add(index)
                return packet(index)
            finally:
                with lock:
                    active -= 1

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "progress":
                with lock:
                    if expected_others <= finished:
                        others_saved.set()

        def run():
            try:
                results.append(self.run_download(body, fetch_segment, report=report))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(others_saved.wait(timeout=5),
                            "Other CDN hosts stalled behind the slow host")
            with lock:
                self.assertTrue(expected_others | {0} <= started)
                self.assertTrue(expected_others <= finished)
                self.assertNotIn(0, finished)
                self.assertEqual(peak, 8)
        finally:
            first_release.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive(), "Download worker did not finish")
        if errors:
            raise errors[0]
        target = results[0]
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(count)))
        self.assertEqual(list(self.folder.iterdir()), [target])

    def test_remaining_hosts_fill_all_eight_slots_after_other_hosts_finish(self):
        for remaining, expected_active in (({0: 8, 1: 8}, {0: 4, 1: 4}),
                                           ({0: 8}, {0: 8}),
                                           ({0: 2, 1: 8}, {0: 2, 1: 6})):
            with self.subTest(remaining=remaining):
                hosts = list(range(13))
                for host, count in remaining.items():
                    hosts.extend([host] * (count - 1))
                lines = ['#EXTM3U']
                for index, host in enumerate(hosts):
                    lines.extend(['#EXTINF:2.002,', f'https://media{host}.example/segment{index}.gif'])
                lines.append('#EXT-X-ENDLIST')
                release = threading.Event()
                full = threading.Event()
                lock = threading.Lock()
                active = dict.fromkeys(remaining, 0)
                fast_finished = set()
                results, errors = [], []

                def fetch_segment(url, cancel):
                    index = int(Path(url).stem.removeprefix('segment'))
                    host = hosts[index]
                    if host not in remaining:
                        with lock:
                            fast_finished.add(host)
                        return packet(index)
                    with lock:
                        active[host] += 1
                        if sum(active.values()) == 8:
                            full.set()
                    try:
                        if not release.wait(timeout=8):
                            raise AssertionError('Remaining-host downloads were not released')
                        return packet(index)
                    finally:
                        with lock:
                            active[host] -= 1

                def run():
                    try:
                        results.append(self.run_download('\n'.join(lines).encode(), fetch_segment))
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(full.wait(timeout=5), 'Idle workers did not pick up remaining host queues')
                    with lock:
                        self.assertEqual(active, expected_active)
                        self.assertEqual(fast_finished, set(range(13)) - set(remaining))
                finally:
                    release.set()
                    worker.join(timeout=8)
                self.assertFalse(worker.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(results[0].read_bytes(), b''.join(packet(index) for index in range(len(hosts))))
                info = [value for kind, value in self.events if kind == 'info'][-1]
                self.assertEqual(info['server_count'], 13)
                self.assertEqual(info['servers'], {f'media{host}.example': hosts.count(host) for host in range(13)})
                self.assertEqual(info['segments'], len(hosts))

    @unittest.skipUnless(os.name == "nt", "Native AES implementation requires Windows")
    def test_fixed_encrypted_ts_download_uses_sequence_iv(self):
        # Independently generated AES-CBC fixture: key 00..0f, IV 7, one TS packet.
        ciphertext = bytes.fromhex(
            "6f386aa8e7faec540c3b4fe825f940b20cc8b6161985ec907f1698ad95da98be0"
            "04fca70eca7e6f67ffc007cae36a6b93641b555d82609bcf6ae3f8ecd8a2160a9"
            "b4c8546f1597f6b654dc19061068aedcb6576d290669de00baca2a0959827cd995"
            "62e148e146f0a82bfb6fd50a06ca487b7958289de97719ab7fd5b47e69e667c"
            "bf8f3ee67924cfe0ff0da12f210bf0ddc9189605a7f66bf358f5646c4d41c118"
            "05d7623140dbd301325a1aceedec44d7c84391c8b2fcb7050bce691570bdc")
        requested_keys = []

        def fetch_segment(url, cancel):
            if "/v/key7?" in url:
                requested_keys.append(url)
                return bytes(range(16))
            return ciphertext

        body = playlist(prefix='#EXT-X-MEDIA-SEQUENCE:7\n'
                        '#EXT-X-KEY:METHOD=AES-128,URI="/v/key7?c=test"')
        target = self.run_download(body, fetch_segment)
        self.assertEqual(target.read_bytes(), b"\x47" + bytes(range(187)))
        self.assertEqual(requested_keys, ["https://media.example/v/key7?c=test&mode=obfuscated"])

    def test_existing_files_and_partial_files_are_preserved(self):
        existing = self.folder / "unrelated.ts"
        existing.write_bytes(b"original")
        reserved_existing = self.folder / "_con.ts"
        reserved_existing.write_bytes(b"existing video")
        prior_partial = self.folder / "_con (1).ts.part"
        prior_partial.write_bytes(b"prior interrupted download")
        target = self.run_download(playlist(), lambda url, cancel: packet(2), title="con")
        self.assertEqual(target.name, "_con (2).ts")
        self.assertEqual(target.read_bytes(), packet(2))
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(reserved_existing.read_bytes(), b"existing video")
        self.assertEqual(prior_partial.read_bytes(), b"prior interrupted download")

    def test_invalid_segment_removes_unfinished_download(self):
        def fetch_segment(url, cancel):
            return b"<html>blocked</html>" if "segment1" in url else packet(1)

        with self.assertRaises(downloader.DownloadError):
            self.run_download(playlist(6), fetch_segment)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(any(kind == "complete" for kind, _ in self.events))

    def test_network_failure_removes_unfinished_download(self):
        def fetch_segment(url, cancel):
            if "segment1" in url:
                raise downloader.DownloadError("network unavailable")
            return packet(3)

        with self.assertRaisesRegex(downloader.DownloadError, "network unavailable"):
            self.run_download(playlist(6), fetch_segment)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_failure_cleans_only_this_download_files(self):
        existing = self.folder / "테스트 영상.ts"
        existing.write_bytes(b"existing video")
        prior_partial = self.folder / "테스트 영상 (1).ts.part"
        prior_partial.write_bytes(b"prior interrupted download")
        prior_folder = self.folder / "prior-parts"
        prior_folder.mkdir()
        prior_segment = prior_folder / "000001"
        prior_segment.write_bytes(b"prior segment")

        def fetch_segment(url, cancel):
            if "segment1" in url:
                raise downloader.DownloadError("network unavailable")
            return packet(3)

        with self.assertRaisesRegex(downloader.DownloadError, "network unavailable"):
            self.run_download(playlist(6), fetch_segment)
        self.assertEqual(set(self.folder.iterdir()), {existing, prior_partial, prior_folder})
        self.assertEqual(existing.read_bytes(), b"existing video")
        self.assertEqual(prior_partial.read_bytes(), b"prior interrupted download")
        self.assertEqual(prior_segment.read_bytes(), b"prior segment")

    @unittest.skipUnless(os.name == "nt", "Windows rename refuses to overwrite a destination")
    def test_file_created_during_download_is_not_overwritten(self):
        def report(kind, value):
            self.events.append((kind, value))
            if kind == "file":
                Path(value).write_bytes(b"new existing video")

        with self.assertRaises(FileExistsError):
            self.run_download(playlist(8), lambda url, cancel: packet(4), report=report)
        target = self.folder / "테스트 영상.ts"
        self.assertEqual(list(self.folder.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b"new existing video")
        self.assertFalse(any(kind == "complete" for kind, _ in self.events))

    def test_cancel_after_progress_removes_unfinished_download(self):
        def report(kind, value):
            self.events.append((kind, value))
            if kind == "progress":
                self.cancel.set()

        with self.assertRaises(downloader.Cancelled):
            self.run_download(playlist(8), lambda url, cancel: packet(5), report=report)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(any(kind == "complete" for kind, _ in self.events))

    def test_cancel_before_file_creation(self):
        self.cancel.set()
        with self.assertRaises(downloader.Cancelled):
            self.run_download(playlist(), lambda url, cancel: packet(5))
        self.assertEqual(list(self.folder.iterdir()), [])


@unittest.skipUnless(os.name == "nt", "Windows GUI integration check")
class GuiIntegrationTests(unittest.TestCase):
    def test_live_transfer_updates_gui_before_completion_and_restores_controls(self):
        root = downloader.tk.Tk()
        root.withdraw()
        release = threading.Event()
        all_started = threading.Event()
        barrier = threading.Barrier(8)
        lock = threading.Lock()
        started = set()
        finished = []
        callback_errors = []
        responsive = []
        app = None
        root.report_callback_exception = lambda error_type, error, traceback: callback_errors.append(error)

        def fake_fetch(url, cancel, referer, **kwargs):
            if url == BASE_URL:
                return playlist(8), BASE_URL
            index = int(Path(url).stem.removeprefix("segment"))
            kwargs["on_bytes"](94)
            with lock:
                started.add(index)
                if len(started) == 8:
                    all_started.set()
            barrier.wait(timeout=5)
            if not release.wait(timeout=8):
                raise AssertionError("GUI test did not release in-flight requests")
            kwargs["on_bytes"](94)
            with lock:
                finished.append(index)
            return packet(index), url

        try:
            with tempfile.TemporaryDirectory() as folder_name, \
                    patch.object(downloader, "resolve_episode", return_value=("GUI 테스트", BASE_URL)), \
                    patch.object(downloader, "fetch", side_effect=fake_fetch):
                try:
                    storage = Path(folder_name)
                    folder = storage / "videos"
                    app = downloader.DownloaderApp(root, state_path=storage / "state" / "jobs.json")
                    app.url.set(PAGE_URL)
                    app.folder.set(str(folder))
                    app.start()
                    self.assertTrue(app.start_button.instate(["disabled"]))
                    root.after(0, lambda: responsive.append(True))
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        root.update()
                        speed = app.speed.get()
                        if (all_started.is_set() and speed.endswith("/s")
                                and float(speed.split()[0]) > 0):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("GUI did not display received bytes while requests were in flight")
                    self.assertTrue(responsive, "Tk callbacks did not run during download")
                    self.assertEqual(callback_errors, [])
                    with lock:
                        self.assertEqual(started, set(range(8)))
                        self.assertEqual(finished, [])
                    self.assertIsNone(app.saved_file)
                    self.assertEqual(app.transferred.get(), "0 MB")
                    self.assertEqual(app.percent.get(), "0.0%")
                    release.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        root.update()
                        if (app.saved_file and app.active_id is None
                                and not app.add_button.instate(["disabled"])):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail(f"GUI did not complete: {app.status.get()}")
                    self.assertEqual(callback_errors, [])
                    self.assertEqual(app.saved_file.read_bytes(),
                                     b"".join(packet(index) for index in range(8)))
                    receipt = downloader.ResumeCache(folder, app.jobs[0]['id']).root
                    self.assertEqual(set(folder.iterdir()), {app.saved_file, receipt.parent})
                    self.assertFalse(receipt.exists())
                    self.assertEqual(app.percent.get(), "100%")
                    self.assertEqual(app.remaining.get(), "완료")
                    self.assertIn("다운로드 완료", app.status.get())
                    for widget in (app.url_entry, app.folder_entry, app.paste_button,
                                   app.browse_button, app.add_button):
                        self.assertFalse(widget.instate(["disabled"]))
                    self.assertTrue(app.start_button.instate(["disabled"]))
                    self.assertTrue(app.cancel_button.instate(["disabled"]))
                finally:
                    release.set()
                    if app and app.worker:
                        if app.worker.is_alive():
                            app.cancel_event.set()
                        app.worker.join(timeout=8)
                        self.assertFalse(app.worker.is_alive(), "GUI download worker did not stop")
                    root.destroy()
        finally:
            if app is None or not app._destroyed:
                root.destroy()


class P2PDownloadTests(unittest.TestCase):
    BASE = "https://every9.poorcdn.com/v/e/" + "a" * 32 + "/c.html?token=test"

    def run_video(self, bodies, *, cancel=None, http_error=False, log_dir=None, peer_details=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        receiver = Mock()
        receiver.get.side_effect = bodies
        receiver.stats.return_value = {"p2p_bytes": 188, "p2p_peers": 2}
        if peer_details is not None:
            receiver.stats.return_value["p2p_peer_details"] = peer_details
        events, http = [], []
        def receive_peers(*args, **kwargs):
            kwargs['on_bytes'](188)
            return receiver
        def fetch(url, event, referer, **kwargs):
            if url == self.BASE:
                return playlist(2), url
            index = int(url.rsplit("segment", 1)[1].split(".")[0])
            http.append(index)
            if kwargs.get('on_event'):
                kwargs['on_event']('http_request', attempt=1, host='every9.poorcdn.com', seconds=0)
            if http_error:
                raise downloader.DownloadError("HTTP timeout")
            data = packet(index)
            if kwargs.get("on_bytes"):
                kwargs["on_bytes"](len(data))
            return data, url
        with patch.object(downloader, "resolve_episode", return_value=("P2P test", self.BASE)), \
                patch.object(downloader, "fetch", side_effect=fetch), \
                patch.object(downloader, "P2PVideoReceiver", side_effect=receive_peers):
            path = downloader.download_video(PAGE_URL, folder, cancel or threading.Event(),
                                               lambda *item: events.append(item), log_dir=log_dir)
        self.assertEqual(path.read_bytes(), packet(0)+packet(1))
        receiver.close.assert_called_once()
        return http, events

    def test_p2p_and_http_segments_merge_in_original_order(self):
        http, events = self.run_video(lambda index, **kwargs: packet(0) if index == 0 else None)
        self.assertEqual(http, [1])
        self.assertTrue(any(kind == "transfer" and value.get("p2p_bytes") == 188 for kind, value in events))
        completed = {value['index']: value.get('source') for kind, value in events
                     if kind == 'segment' and value['state'] == 'complete'}
        self.assertEqual(completed, {0: 'p2p', 1: 'http'})
        transfer = [value for kind, value in events if kind == 'transfer'][-1]
        self.assertEqual((transfer['http_bytes'], transfer['p2p_bytes'], transfer['received']), (188, 188, 376))
        self.assertAlmostEqual(transfer['http_speed'] + transfer['p2p_speed'], transfer['speed'])

    def test_invalid_p2p_body_is_replaced_by_valid_http_body(self):
        http, events = self.run_video(lambda index, **kwargs: b"corrupt" if index == 0 else packet(1))
        self.assertEqual(http, [0])
        self.assertTrue(any(kind == "status" and "HTTP로 다시" in value for kind, value in events))
        self.assertTrue(any(kind == 'segment' and value.get('index') == 0 and value.get('source') == 'http'
                            for kind, value in events))
        self.assertTrue(any(kind == 'segment' and value.get('transition') == 'p2p_to_http' for kind, value in events))

    def test_no_peers_keeps_existing_http_download_path(self):
        http, _ = self.run_video(lambda index, **kwargs: None)
        self.assertEqual(sorted(http), [0, 1])

    def test_verified_http_body_stops_peer_work_before_durable_storage(self):
        receiver = Mock()
        receiver.get.return_value = None
        receiver.stats.return_value = {}
        finished = set()
        receiver.finish.side_effect = finished.add
        stored = []
        original_store = downloader.ResumeCache.store
        def store(cache, index, data, **kwargs):
            self.assertIn(index, finished, 'Duplicate peer work must stop before disk waits')
            original_store(cache, index, data, **kwargs)
            stored.append(index)
        def fetch(url, event, referer, **kwargs):
            if url == self.BASE:
                return playlist(2), url
            index = int(url.rsplit('segment', 1)[1].split('.')[0])
            self.assertNotIn(index, finished, 'Do not finish the peer before obtaining a valid body')
            receiver.claim_http.assert_any_call(index)
            kwargs['on_bytes'](188)
            return packet(index), url
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(downloader, 'resolve_episode', return_value=('Peer completion', self.BASE)), \
                patch.object(downloader, 'P2PVideoReceiver', return_value=receiver), \
                patch.object(downloader, 'fetch', side_effect=fetch), \
                patch.object(downloader.ResumeCache, 'store', store):
            target = downloader.download_video(PAGE_URL, Path(temporary), threading.Event(), lambda *args: None,
                                               resume_id='1' * 32)
            self.assertEqual(target.read_bytes(), packet(0) + packet(1))
        self.assertEqual(sorted(stored), [0, 1])
        self.assertEqual(receiver.finish.call_count, 2)

    def test_only_slow_http_releases_ownership_for_peer_rescue(self):
        for deadline, source in ((3600, 'http'), (0, 'p2p')):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                receiver = Mock()
                receiver.stats.return_value = {}
                released = set()
                receiver.release_http.side_effect = released.add
                receiver.get.side_effect = lambda index, **kwargs: packet(index) if index in released else None
                events = []

                def fetch(url, cancel, referer, **kwargs):
                    if url == self.BASE:
                        return playlist(1), url
                    receiver.claim_http.assert_called_once_with(0)
                    kwargs['on_wait']()
                    kwargs['on_bytes'](188)
                    return packet(0), url

                with patch.object(downloader, 'PEER_WAIT_SECONDS', deadline), \
                        patch.object(downloader, 'resolve_episode', return_value=('Peer rescue', self.BASE)), \
                        patch.object(downloader, 'P2PVideoReceiver', return_value=receiver), \
                        patch.object(downloader, 'fetch', side_effect=fetch):
                    target = downloader.download_video(PAGE_URL, Path(temporary), threading.Event(),
                                                       lambda *item: events.append(item))
                self.assertEqual(target.read_bytes(), packet(0))
                completed = [value for kind, value in events if kind == 'segment' and value['state'] == 'complete']
                self.assertEqual(completed[0]['source'], source)
                self.assertEqual(released, {0} if source == 'p2p' else set())
                receiver.finish.assert_called_once_with(0)

    def test_peer_completion_before_failed_store_does_not_mark_resume_complete(self):
        receiver = Mock()
        receiver.get.return_value = None
        receiver.stats.return_value = {}
        fetched = []
        def fetch(url, event, referer, **kwargs):
            if url == self.BASE:
                return playlist(1), url
            fetched.append(url)
            kwargs['on_bytes'](188)
            return packet(0), url
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(downloader, 'resolve_episode', return_value=('Failed store', self.BASE)), \
                patch.object(downloader, 'P2PVideoReceiver', return_value=receiver), \
                patch.object(downloader, 'fetch', side_effect=fetch):
            folder = Path(temporary)
            resume_id = '2' * 32
            with patch.object(downloader.ResumeCache, 'store', side_effect=OSError('disk unavailable')):
                with self.assertRaisesRegex(OSError, 'disk unavailable'):
                    downloader.download_video(PAGE_URL, folder, threading.Event(), lambda *args: None,
                                               resume_id=resume_id)
            receiver.finish.assert_called_once_with(0)
            state = json.loads((downloader.ResumeCache(folder, resume_id).root / 'state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['parts'], {})
            self.assertFalse(list(folder.glob('*.ts')))
            target = downloader.download_video(PAGE_URL, folder, threading.Event(), lambda *args: None,
                                               resume_id=resume_id)
            self.assertEqual(target.read_bytes(), packet(0))
            self.assertEqual(len(fetched), 2, 'A peer acknowledgement is not a durable resume record')

    def test_late_peer_segment_replaces_slow_or_failed_http_request(self):
        for http_error in (False, True):
            with self.subTest(http_error=http_error):
                def receive(index, **kwargs):
                    return packet(index) if index == 1 or kwargs.get('wait') is False else None
                http, events = self.run_video(receive, http_error=http_error)
                self.assertEqual(http, [0])
                self.assertTrue(any(kind == 'segment' and value.get('index') == 0
                    and value.get('transition') == 'http_to_p2p' for kind, value in events))

    def test_peer_wait_is_reported_before_any_http_request(self):
        release = threading.Event()
        entered = threading.Event()
        receiver = Mock()
        receiver.stats.return_value = {'p2p_bytes': 0, 'p2p_peers': 1}
        def receive(index, **kwargs):
            entered.set()
            if not release.wait(4):
                raise AssertionError('Peer wait was not reported')
            return packet(index)
        receiver.get.side_effect = receive
        events = []
        def report(kind, value):
            events.append((kind, value))
            if kind == 'workers' and any(item['state'] == 'peer_wait' for item in value):
                release.set()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(downloader, 'resolve_episode', return_value=('P2P wait', self.BASE)), \
                patch.object(downloader, 'fetch', return_value=(playlist(2), self.BASE)) as fetcher, \
                patch.object(downloader, 'P2PVideoReceiver', return_value=receiver):
            downloader.download_video(PAGE_URL, Path(temporary), threading.Event(), report)
        self.assertTrue(entered.is_set())
        self.assertEqual(fetcher.call_count, 1)
        waiting = [item for kind, value in events if kind == 'workers' for item in value if item['state'] == 'peer_wait']
        self.assertTrue(waiting)
        self.assertTrue(all(item['source'] == 'p2p_wait' and item['bytes'] == 0 for item in waiting))

    def test_ready_peer_segments_are_saved_before_slow_earlier_segments(self):
        receiver = Mock()
        receiver.stats.return_value = {'p2p_bytes':188, 'p2p_peers':2, 'p2p_active_indices':[0],
                                      'p2p_parts':[{'index':5,'state':'ready','bytes':188}]}
        receiver.get.side_effect = lambda index, **kwargs: packet(5) if index == 5 else None
        completed = []
        def fetch(url, event, referer, **kwargs):
            if url == self.BASE:
                return playlist(6),url
            index = int(url.rsplit('segment',1)[1].split('.')[0])
            body=packet(index)
            kwargs['on_bytes'](len(body))
            return body,url
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(downloader,'WORKERS',1), \
                patch.object(downloader,'resolve_episode',return_value=('Ready first',self.BASE)), \
                patch.object(downloader,'fetch',side_effect=fetch), \
                patch.object(downloader,'P2PVideoReceiver',return_value=receiver):
            target=downloader.download_video(PAGE_URL,Path(temporary),threading.Event(),
                lambda kind,value: completed.append(value['index']) if kind=='segment' and value['state']=='complete' else None)
            self.assertEqual(target.read_bytes(),b''.join(packet(index) for index in range(6)))
        self.assertEqual(completed[0],5)
        self.assertEqual(completed[-1],0)
        self.assertEqual(sorted(completed),list(range(6)))

    def test_durable_trace_records_validation_fallback_and_contains_no_signed_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            self.run_video(lambda index, **kwargs: b'bad' if index==0 else packet(1), log_dir=folder)
            logs=list(folder.glob('*.jsonl'))
            self.assertEqual(len(logs),1)
            text=logs[0].read_text(encoding='utf-8')
            records=[json.loads(line) for line in text.splitlines()]
            self.assertNotIn('token=test',text)
            self.assertEqual(records[-1]['event'],'run_end')
            self.assertEqual(records[-1]['outcome'],'completed')
            self.assertTrue(any(row['event']=='segment_verify' and not row['valid'] and row['source']=='p2p' for row in records))
            saved={row['segment']:row for row in records if row['event']=='segment_saved'}
            self.assertEqual(set(saved),{1,2})
            self.assertEqual(saved[1]['source'],'http')
            self.assertEqual(saved[1]['transition'],'p2p_to_http')
            self.assertEqual(saved[1]['sha256'],hashlib.sha256(packet(0)).hexdigest())
            self.assertTrue(any(row['event']=='http_first_byte' for row in records))
            self.assertTrue(any(row['event']=='transfer' and row['received']==row['http_bytes']+row['p2p_bytes'] for row in records))

    def test_peer_addresses_reach_display_without_being_written_to_trace(self):
        peers = [{'id': 'aa', 'address': '8.8.8.8', 'country': '미국 (US)', 'state': 'ready'}]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            _, events = self.run_video(lambda index, **kwargs: packet(index), log_dir=folder, peer_details=peers)
            self.assertTrue(any(kind == 'transfer' and value.get('p2p_peer_details') == peers for kind, value in events))
            text = next(folder.glob('*.jsonl')).read_text(encoding='utf-8')
            self.assertNotIn('p2p_peer_details', text)
            self.assertNotIn('8.8.8.8', text)
            self.assertNotIn('미국', text)

    def test_unwritable_trace_directory_does_not_break_a_valid_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocked=Path(temporary)/'file'
            blocked.write_text('keep',encoding='utf-8')
            _, events=self.run_video(lambda index, **kwargs: packet(index),log_dir=blocked)
            self.assertTrue(any(kind=='status' and '상세 로그 파일을 만들지 못했습니다' in value for kind,value in events))
            self.assertEqual(blocked.read_text(encoding='utf-8'),'keep')

    def test_failed_transfer_is_logged_as_error_despite_cleanup_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises(downloader.DownloadError):
                self.run_video(lambda index, **kwargs: None, http_error=True, log_dir=folder)
            records = [json.loads(line) for line in next(folder.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(records[-1]['event'], 'run_end')
            self.assertEqual(records[-1]['outcome'], 'error')
            self.assertEqual(records[-1]['error'], 'DownloadError')
            self.assertFalse(records[-1]['published'])

    def test_http_retry_trace_exposes_attempts_without_request_tokens(self):
        client=Mock()
        client.get.side_effect=[downloader.TransferError('slow',12002),(b'data',self.BASE)]
        cancel=Mock()
        cancel.is_set.return_value=False
        cancel.wait.return_value=False
        events=[]
        with patch.object(downloader,'_get_http_client',return_value=client):
            data,_=downloader._fetch_single(self.BASE,cancel,'https://player.example/',limit=1024,
                on_event=lambda event,**fields: events.append({'event':event,**fields}))
        self.assertEqual(data,b'data')
        self.assertEqual([item['attempt'] for item in events if item['event']=='http_request'],[1,2])
        self.assertTrue(any(item['event']=='http_error' and item['code']==12002 for item in events))
        self.assertTrue(any(item['event']=='http_retry' for item in events))
        self.assertNotIn('token=test',json.dumps(events))

    def test_cancel_during_p2p_wait_closes_receiver_and_does_not_publish(self):
        cancel = threading.Event()
        receiver = Mock()
        def receive(index):
            cancel.set()
            return packet(index)
        receiver.get.side_effect = receive
        receiver.stats.return_value = {"p2p_bytes": 0, "p2p_peers": 0}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(downloader, "resolve_episode", return_value=("P2P test", self.BASE)), \
                patch.object(downloader, "fetch", return_value=(playlist(2), self.BASE)) as fetcher, \
                patch.object(downloader, "P2PVideoReceiver", return_value=receiver):
            folder = Path(temporary)
            with self.assertRaises(downloader.Cancelled):
                downloader.download_video(PAGE_URL, folder, cancel, Mock())
            self.assertEqual(list(folder.iterdir()), [])
            self.assertEqual(fetcher.call_count, 1)
        receiver.close.assert_called_once()


class AdaptiveServerTests(unittest.TestCase):
    BASE = 'https://every9.poorcdn.com/v/e/' + 'a' * 32 + '/c.html?token=private'
    HOSTS = ('c9.slow.example', 'c9.fast.example', 'c9.third.example')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.cancel = threading.Event()
        self.events = []
        self.requests = []
        self.lock = threading.Lock()
        self.active = self.peak = 0
        for name, value in (('WORKERS', 2), ('SERVER_STALL_SECONDS', .08),
                            ('SERVER_HEDGE_SECONDS', 10), ('SERVER_COOLDOWN_SECONDS', 1)):
            fixture = patch.object(downloader, name, value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def run_video(self, handler, count=6, *, hosts=None, resume=False, peers=None,
                  fastest_only=None, on_report=None, log_dir=None):
        hosts = hosts or self.HOSTS
        lines = ['#EXTM3U']
        for index in range(count):
            lines += ['#EXTINF:2,', f'https://{hosts[index % len(hosts)]}/v/e/{"a" * 32}/segment{index}.gif?token=keep-me']
        manifest = ('\n'.join(lines + ['#EXT-X-ENDLIST']) + '\n').encode()
        def fetch(url, cancel, referer, **kwargs):
            parsed = downloader.urlsplit(url)
            if parsed.path.endswith('/c.html'):
                return manifest, url
            index = int(Path(parsed.path).stem.removeprefix('segment'))
            self.assertEqual(parsed.query, 'token=keep-me')
            self.assertIn(parsed.hostname, hosts)
            self.assertEqual(referer, downloader.PLAYER_ORIGIN + '/')
            with self.lock:
                self.requests.append((index, parsed.hostname))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                body = handler(index, parsed.hostname, kwargs)
                kwargs['on_bytes'](len(body))
                return body, url
            finally:
                with self.lock:
                    self.active -= 1
        def report(kind, value):
            self.events.append((kind, value))
            if on_report:
                on_report(kind, value)
        with patch.object(downloader, 'resolve_episode', return_value=('Adaptive test', self.BASE)), \
                patch.object(downloader, 'fetch', side_effect=fetch), \
                patch.object(downloader, 'video_swarm_id', return_value='test' if peers else None), \
                patch.object(downloader, 'P2PVideoReceiver', return_value=peers):
            path = downloader.download_video(PAGE_URL, self.folder, self.cancel, report,
                resume_id='a' * 32 if resume else None, fastest_only=fastest_only, log_dir=log_dir)
        self.assertEqual(path.read_bytes(), b''.join(packet(i) for i in range(count)))
        self.assertLessEqual(self.peak, downloader.WORKERS)
        self.assertEqual(self.active, 0)
        final_health = [value['server_health'] for kind, value in self.events if kind == 'transfer'][-1]
        self.assertTrue(all(item['active'] == 0 for item in final_health.values()))
        return path

    def stall(self, kwargs):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            kwargs['on_wait']()
            time.sleep(.005)
        self.fail('Stalled HTTP request was not stopped')

    def test_stall_switches_host_and_avoids_slow_host_for_later_pieces(self):
        def handler(index, host, kwargs):
            if host == self.HOSTS[0]:
                self.stall(kwargs)
            return packet(index)
        self.run_video(handler, count=18)
        health = [value['server_health'] for kind, value in self.events if kind == 'transfer'][-1]
        self.assertEqual(health[self.HOSTS[0]]['state'], 'slow')
        self.assertGreater(health[self.HOSTS[0]]['slow_count'], 0)
        self.assertLessEqual(sum(host == self.HOSTS[0] for _, host in self.requests), 2)
        self.assertGreater(sum(row['switches'] for row in health.values()), 0)

    def test_tail_hedge_cancels_loser_and_stores_each_piece_once(self):
        stopped = threading.Event()
        stored = []
        original_store = downloader.ResumeCache.store
        def store(cache, index, body, **kwargs):
            stored.append(index)
            return original_store(cache, index, body, **kwargs)
        def handler(index, host, kwargs):
            if index == 0 and host == self.HOSTS[0]:
                try:
                    kwargs['on_bytes'](32)
                    self.stall(kwargs)
                finally:
                    stopped.set()
            return packet(index)
        with patch.object(downloader, 'SERVER_HEDGE_SECONDS', .03), \
                patch.object(downloader, 'SERVER_STALL_SECONDS', 2), \
                patch.object(downloader.ResumeCache, 'store', store):
            self.run_video(handler, count=2, resume=True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(sorted(stored), [0, 1])
        self.assertFalse(self.cancel.is_set())
        self.assertGreater(sum(index == 0 for index, _ in self.requests), 1)
        transfer = [value for kind, value in self.events if kind == 'transfer'][-1]
        self.assertEqual(transfer['http_bytes'], 188 * 2 + 32)

    def test_missing_alternatives_retry_original_without_skipping_piece(self):
        def handler(index, host, kwargs):
            if index == 0:
                if host == self.HOSTS[0] and self.requests.count((index, host)) == 1:
                    self.stall(kwargs)
                if host != self.HOSTS[0]:
                    raise downloader.DownloadError('HTTP 404')
                self.assertTrue(kwargs['retry'])
                for _ in range(5):
                    kwargs['on_wait']()
                    time.sleep(.02)
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1):
            self.run_video(handler, count=3)
        self.assertEqual([host for index, host in self.requests if index == 0],
                         [*self.HOSTS, self.HOSTS[0]])

    def test_corrupt_alternative_is_rejected_before_a_valid_alternative_wins(self):
        def handler(index, host, kwargs):
            if index == 0 and host == self.HOSTS[0]:
                raise downloader.DownloadError('HTTP 503')
            if index == 0 and host == self.HOSTS[1]:
                return b'<html>not video</html>'
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1):
            self.run_video(handler, count=3)
        self.assertEqual([host for index, host in self.requests if index == 0], list(self.HOSTS))

    def test_all_hosts_fail_with_bounded_attempts_and_no_published_video(self):
        def handler(*args):
            raise downloader.DownloadError('HTTP 404')
        with patch.object(downloader, 'WORKERS', 1), self.assertRaises(downloader.DownloadError):
            self.run_video(handler, count=3)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_cancellation_does_not_retry_on_another_host(self):
        def handler(index, host, kwargs):
            self.cancel.set()
            kwargs['on_wait']()
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1), self.assertRaises(downloader.Cancelled):
            self.run_video(handler, count=3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_late_peer_still_supersedes_adaptive_http_and_finishes_once(self):
        peers = Mock()
        peers.stats.return_value = {'p2p_bytes': 188, 'p2p_peers': 1}
        peers.get.side_effect = lambda index, **kwargs: packet(index) if kwargs.get('wait') is False else None
        def handler(index, host, kwargs):
            self.stall(kwargs)
        with patch.object(downloader, 'SERVER_STALL_SECONDS', 2):
            self.run_video(handler, count=3, peers=peers)
        self.assertEqual(peers.finish.call_count, 3)
        peers.close.assert_called_once()
        sources = [value.get('source') for kind, value in self.events if kind == 'segment' and value['state'] == 'complete']
        self.assertEqual(sources, ['p2p'] * 3)

    def test_recovered_host_is_probed_after_cooldown_and_becomes_healthy(self):
        def handler(index, host, kwargs):
            if index == 0 and host == self.HOSTS[0]:
                raise downloader.DownloadError('HTTP 503')
            time.sleep(.04)
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1), patch.object(downloader, 'SERVER_COOLDOWN_SECONDS', .02):
            self.run_video(handler, count=3)
        self.assertTrue(any(index > 0 and host == self.HOSTS[0] for index, host in self.requests))
        health = [value['server_health'] for kind, value in self.events if kind == 'transfer'][-1]
        self.assertEqual(health[self.HOSTS[0]]['state'], 'healthy')
        self.assertGreater(health[self.HOSTS[0]]['slow_count'], 0)

    def test_sustained_low_rate_switches_even_while_bytes_keep_arriving(self):
        def handler(index, host, kwargs):
            if host == self.HOSTS[0]:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    kwargs['on_bytes'](1)
                    time.sleep(.005)
                self.fail('Low-rate stream was not replaced')
            return packet(index)
        with patch.object(downloader, 'SERVER_SLOW_SECONDS', .04), patch.object(downloader, 'SERVER_STALL_SECONDS', 5):
            self.run_video(handler, count=6)
        health = [value['server_health'] for kind, value in self.events if kind == 'transfer'][-1]
        self.assertEqual(health[self.HOSTS[0]]['reason'], '저속 수신')

    def test_slow_history_is_reset_for_the_next_download(self):
        def handler(index, host, kwargs):
            if host == self.HOSTS[0]:
                raise downloader.DownloadError('HTTP 503')
            return packet(index)
        self.run_video(handler, count=3)
        self.events.clear()
        self.requests.clear()
        self.run_video(lambda index, host, kwargs: packet(index), count=3)
        health = [value['server_health'] for kind, value in self.events if kind == 'transfer'][-1]
        self.assertTrue(all(item['slow_count'] == 0 for item in health.values()))

    def test_peer_rescue_releases_host_reservation_for_a_queued_request(self):
        peers = Mock()
        peers.stats.return_value = {'p2p_bytes': 188, 'p2p_peers': 1}
        peers.get.side_effect = lambda index, **kwargs: packet(index) if kwargs.get('wait') is False else None
        original_executor = downloader.concurrent.futures.ThreadPoolExecutor
        queued = Mock()
        futures = []
        def submit(*args):
            future = downloader.concurrent.futures.Future()
            futures.append(future)
            return future
        queued.submit.side_effect = submit
        def executor(*args, **kwargs):
            return queued if kwargs.get('thread_name_prefix') == 'video-http' else original_executor(*args, **kwargs)
        with patch.object(downloader.concurrent.futures, 'ThreadPoolExecutor', side_effect=executor):
            self.run_video(lambda index, host, kwargs: packet(index), count=3, peers=peers)
        self.assertTrue(futures)
        self.assertTrue(all(future.cancelled() for future in futures))
        self.assertEqual(self.requests, [])

    def test_all_eight_http_slots_are_used_without_exceeding_the_limit(self):
        barrier = threading.Barrier(8)
        def handler(index, host, kwargs):
            if index < 8:
                barrier.wait(timeout=3)
            return packet(index)
        with patch.object(downloader, 'WORKERS', 8):
            self.run_video(handler, count=16)
        self.assertEqual(self.peak, 8)

    def test_fastest_mode_concentrates_new_requests_on_the_fastest_measured_host(self):
        fastest = threading.Event()
        def handler(index, host, kwargs):
            time.sleep(.03 if index == 0 else .005)
            if index == 1:
                fastest.set()
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1):
            self.run_video(handler, count=7, fastest_only=fastest)
        self.assertEqual(self.requests[:2], [(0, self.HOSTS[0]), (1, self.HOSTS[1])])
        self.assertTrue(all(host == self.HOSTS[1] for index, host in self.requests if index >= 2))

    def test_fastest_mode_switches_on_and_off_during_the_same_download(self):
        fastest = threading.Event()
        initial = threading.Barrier(2)
        release = threading.Event()
        modes = {}
        def report(kind, value):
            if kind == 'segment' and value['state'] == 'active':
                if value['index'] == 2:
                    fastest.set()
                elif value['index'] == 3:
                    fastest.clear()
        def handler(index, host, kwargs):
            modes[index] = fastest.is_set()
            if index < 2:
                initial.wait(timeout=3)
                time.sleep(.05 if index == 0 else .005)
            elif index == 2:
                self.assertTrue(release.wait(timeout=3), 'Distributed request did not start')
            elif index == 3:
                release.set()
            return packet(index)
        self.run_video(handler, count=4, fastest_only=fastest, on_report=report)
        hosts = dict(self.requests)
        self.assertEqual(hosts[2], self.HOSTS[1])
        self.assertNotEqual(hosts[3], self.HOSTS[1])
        self.assertTrue(modes[2])
        self.assertFalse(modes[3])
        self.assertEqual(len(self.requests), 4)

    def test_fastest_mode_falls_back_when_the_fastest_host_lacks_a_piece(self):
        fastest = threading.Event()
        def handler(index, host, kwargs):
            if index == 2 and host == self.HOSTS[1]:
                raise downloader.DownloadError('HTTP 404')
            time.sleep(.03 if index == 0 else .005)
            if index == 1:
                fastest.set()
            return packet(index)
        with patch.object(downloader, 'WORKERS', 1):
            self.run_video(handler, count=5, fastest_only=fastest)
        alternatives = [host for index, host in self.requests if index == 2]
        self.assertEqual(alternatives[0], self.HOSTS[1])
        self.assertGreater(len(alternatives), 1)
        self.assertNotEqual(alternatives[1], self.HOSTS[1])

    def test_fastest_mode_does_not_start_parallel_tail_requests_on_other_hosts(self):
        fastest = threading.Event()
        fastest.set()
        initial = threading.Barrier(2)
        def handler(index, host, kwargs):
            if len(self.requests) <= 2:
                initial.wait(timeout=3)
            if index == 0 and host == self.HOSTS[0]:
                self.stall(kwargs)
            return packet(index)
        logs = self.folder / 'logs'
        with patch.object(downloader, 'SERVER_HEDGE_SECONDS', .03), \
                patch.object(downloader, 'SERVER_STALL_SECONDS', .3):
            self.run_video(handler, count=2, fastest_only=fastest, log_dir=logs)
        records = [json.loads(line) for line in next(logs.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
        attempts = [row for row in records if row['event'] in ('server_request', 'server_switch')]
        self.assertGreater(len(attempts), 2)
        self.assertTrue(all(row['fastest_only'] and not row['rescue'] for row in attempts))


if __name__ == "__main__":
    unittest.main()

"""Durable download integration checks using local manifest and segment fixtures."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import hashlib
from unittest.mock import patch
from urllib.parse import urlsplit

import tvmon_downloader as downloader
import tvmon_resume
from test_tvmon_downloader import PAGE_URL, packet, playlist


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.resume_id = "0123456789abcdef0123456789abcdef"
        self.cache = tvmon_resume.cache_path(self.folder, self.resume_id)
        self.events = []
        self.requests = []

    def manifest(self, count=4, host="media.example", token="old", prefix=""):
        body = playlist(count, prefix)
        for index in range(count):
            body = body.replace(f"segment{index}.gif".encode(),
                                f"https://{host}/v/e/video/segment{index}.gif?token={token}".encode())
        return body

    def run_download(self, *, body=None, host="media.example", token="old", cancel=None,
                     report=None, segment_fetch=None, key=bytes(range(16)), workers=1):
        body = self.manifest(host=host, token=token) if body is None else body
        manifest_url = f"https://{host}/v/e/video/c.html?token={token}"
        requests = []
        self.requests = requests

        def fake_fetch(url, request_cancel, referer, **kwargs):
            if url == manifest_url:
                return body, manifest_url
            if urlsplit(url).path.startswith("/keys/"):
                return key, url
            requests.append(url)
            index = int(Path(urlsplit(url).path).stem.removeprefix("segment"))
            data = segment_fetch(index, request_cancel) if segment_fetch else packet(index)
            if kwargs.get("on_bytes"):
                kwargs["on_bytes"](len(data))
            return data, url

        with patch.object(downloader, "WORKERS", workers), \
                patch.object(downloader, "resolve_episode", return_value=("이어받기 테스트", manifest_url)), \
                patch.object(downloader, "fetch", side_effect=fake_fetch):
            return downloader.download_video(
                PAGE_URL, self.folder, cancel or threading.Event(),
                report or (lambda kind, value: self.events.append((kind, value))),
                resume_id=self.resume_id)

    def pause_after_first_segment(self, **kwargs):
        cancel = threading.Event()

        def fetch_until_pause(index, event):
            if index:
                self.assertTrue(event.wait(5), "First durable piece did not trigger pause")
                raise downloader.Cancelled()
            return packet(index)

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "progress" and value["done"] == 1:
                cancel.set()

        with self.assertRaises(downloader.Cancelled):
            self.run_download(cancel=cancel, report=report, segment_fetch=fetch_until_pause, **kwargs)
        self.assertEqual((self.cache / "000000.ts").read_bytes(), packet(0))
        self.assertTrue((self.cache / "state.json").is_file())

    def requested_indices(self):
        return [int(Path(urlsplit(url).path).stem.removeprefix("segment")) for url in self.requests]

    def assert_published_receipt(self, target):
        self.assertEqual(set(self.folder.iterdir()), {target, self.cache.parent})
        self.assertEqual(set(self.cache.parent.iterdir()), {self.cache})
        self.assertEqual(list(self.cache.glob("[0-9]*.ts")), [])
        state = json.loads((self.cache / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["publication"]["phase"], "published")

    def test_restarted_download_skips_verified_cached_segments(self):
        self.pause_after_first_segment()
        target = self.run_download()
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))
        self.assert_published_receipt(target)

    def test_same_size_valid_ts_corruption_is_redownloaded(self):
        self.pause_after_first_segment()
        (self.cache / "000000.ts").write_bytes(packet(99))
        target = self.run_download()
        self.assertEqual(self.requested_indices(), [0, 1, 2, 3])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_network_failure_preserves_completed_segments_for_retry(self):
        def fail_second_segment(index, cancel):
            if index == 1:
                raise downloader.DownloadError("fixture network failure")
            return packet(index)

        with self.assertRaisesRegex(downloader.DownloadError, "fixture network failure"):
            self.run_download(segment_fetch=fail_second_segment)
        self.assertEqual((self.cache / "000000.ts").read_bytes(), packet(0))
        self.assertTrue((self.cache / "state.json").is_file())
        target = self.run_download()
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_refreshed_signed_urls_and_cdn_hosts_keep_cached_segments(self):
        self.pause_after_first_segment()
        target = self.run_download(host="new-cdn.example", token="new-signed-token")
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assertTrue(all("new-cdn.example" in url and "new-signed-token" in url
                            for url in self.requests))
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_changed_manifest_path_duration_or_sequence_cannot_mix_with_cache(self):
        self.pause_after_first_segment()
        original = self.manifest()
        changed = (
            original.replace(b"/segment0.gif", b"/different0.gif"),
            original.replace(b"#EXTINF:2.002,", b"#EXTINF:3.002,", 1),
            self.manifest(prefix="#EXT-X-MEDIA-SEQUENCE:1"),
        )
        for body in changed:
            with self.subTest(body=body), self.assertRaises(downloader.DownloadError):
                self.run_download(body=body)
            self.assertEqual(self.requests, [])
            self.assertEqual((self.cache / "000000.ts").read_bytes(), packet(0))
        target = self.run_download()
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_changed_key_material_cannot_mix_with_decrypted_cache(self):
        body = self.manifest(prefix='#EXT-X-KEY:METHOD=AES-128,URI="/keys/video",IV=0x7')
        with patch.object(downloader, "decrypt_aes128", side_effect=lambda data, key, iv: data):
            self.pause_after_first_segment(body=body)
            with self.assertRaises(downloader.DownloadError):
                self.run_download(body=body, key=bytes(reversed(range(16))))
            self.assertEqual(self.requests, [])
            self.assertEqual((self.cache / "000000.ts").read_bytes(), packet(0))
            target = self.run_download(body=body)
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_pause_during_merge_preserves_all_segments_and_restart_needs_no_network(self):
        cancel = threading.Event()
        original_open = Path.open
        merge_writes = []

        class InterruptingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, data):
                count = self.stream.write(data)
                merge_writes.append(count)
                cancel.set()
                return count

        def interrupt_merge(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path.name == "_merge.ts.part" and any(flag in mode for flag in "wx"):
                return InterruptingWriter(stream)
            return stream

        with patch.object(Path, "open", new=interrupt_merge), self.assertRaises(downloader.Cancelled):
            self.run_download(cancel=cancel)
        self.assertTrue(merge_writes, "Cancellation did not interrupt an actual merge write")
        for index in range(4):
            self.assertEqual((self.cache / f"{index:06d}.ts").read_bytes(), packet(index))
        target = self.run_download()
        self.assertEqual(self.requests, [])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))
        self.assert_published_receipt(target)

    def test_restart_recovers_publication_interrupted_after_rename(self):
        original_rename = Path.rename
        published = []

        def interrupt_after_publication(path, target):
            result = original_rename(path, target)
            if path.name == "_merge.ts.part":
                published.append(result)
                raise OSError("fixture interruption after publication")
            return result

        with patch.object(Path, "rename", new=interrupt_after_publication), \
                self.assertRaisesRegex(OSError, "fixture interruption after publication"):
            self.run_download()
        self.assertEqual(len(published), 1)
        expected = b"".join(packet(index) for index in range(4))
        self.assertEqual(published[0].read_bytes(), expected)
        for index in range(4):
            self.assertEqual((self.cache / f"{index:06d}.ts").read_bytes(), packet(index))
        state = json.loads((self.cache / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["publication"]["phase"], "ready")
        target = self.run_download()
        self.assertEqual(target, published[0])
        self.assertEqual(self.requests, [])
        self.assertEqual(target.read_bytes(), expected)
        self.assert_published_receipt(target)

    @unittest.skipUnless(os.name == "nt", "Windows publication refuses to overwrite a destination")
    def test_file_created_before_publication_is_preserved_and_cache_can_retry(self):
        targets = []

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "file":
                target = Path(value)
                targets.append(target)
                target.write_bytes(b"existing unrelated video")

        with self.assertRaises(FileExistsError):
            self.run_download(report=report)
        self.assertEqual(targets[0].read_bytes(), b"existing unrelated video")
        for index in range(4):
            self.assertEqual((self.cache / f"{index:06d}.ts").read_bytes(), packet(index))
        targets[0].unlink()
        target = self.run_download()
        self.assertEqual(self.requests, [])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(4)))

    def test_active_cache_cannot_be_used_or_discarded_by_a_second_download(self):
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def wait_for_release(index, cancel):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Active resume cache test was not released")
            return packet(index)

        def run():
            try:
                results.append(self.run_download(body=self.manifest(1), segment_fetch=wait_for_release))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5), "First download did not obtain its cache")
            with self.assertRaises(downloader.DownloadError):
                self.run_download(body=self.manifest(1))
            with self.assertRaises(downloader.DownloadError):
                downloader.discard_resume(self.folder, self.resume_id)
            self.assertTrue(self.cache.exists())
        finally:
            release.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive(), "Active download did not stop")
        if errors:
            raise errors[0]
        self.assertEqual(results[0].read_bytes(), packet(0))

    def test_discard_removes_only_the_owned_download_cache(self):
        self.pause_after_first_segment()
        existing = self.folder / "existing.ts"
        existing.write_bytes(b"existing video")
        unrelated = self.folder / ".tvmon-resume-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        unrelated.mkdir()
        (unrelated / "notes.txt").write_text("unrelated", encoding="utf-8")
        downloader.discard_resume(self.folder, self.resume_id)
        self.assertFalse(self.cache.exists())
        with self.assertRaises(downloader.DownloadError):
            downloader.discard_resume(self.folder, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(existing.read_bytes(), b"existing video")
        self.assertEqual((unrelated / "notes.txt").read_text(encoding="utf-8"), "unrelated")
        self.assertEqual(set(self.folder.iterdir()), {existing, unrelated, self.cache.parent})

    def test_legacy_cache_moves_into_the_container_and_resumes_verified_parts(self):
        self.pause_after_first_segment()
        legacy = self.folder / self.cache.name
        self.assertEqual(legacy.resolve().parent, self.folder.resolve())
        self.cache.rename(legacy)
        target = self.run_download()
        self.assertFalse(legacy.exists())
        self.assertEqual(self.cache.parent.name, '이어받기 임시')
        self.assertEqual(target.parent, self.folder)
        self.assertEqual(self.requested_indices(), [1, 2, 3])
        self.assert_published_receipt(target)

    def test_legacy_cache_in_use_is_preserved_without_moving_it(self):
        import msvcrt
        self.pause_after_first_segment()
        legacy = self.folder / self.cache.name
        self.assertEqual(legacy.resolve().parent, self.folder.resolve())
        self.cache.rename(legacy)
        with (legacy / '.lock').open('r+b') as held:
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaises(tvmon_resume.ResumeError):
                    with tvmon_resume.ResumeCache(self.folder, self.resume_id):
                        self.fail('A locked legacy cache was opened')
                self.assertTrue(legacy.is_dir())
                self.assertFalse(self.cache.exists())
                self.assertEqual((legacy / '000000.ts').read_bytes(), packet(0))
            finally:
                held.seek(0)
                msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)

    def test_legacy_migration_preserves_unexpected_files_and_conflicting_caches(self):
        self.pause_after_first_segment()
        legacy = self.folder / self.cache.name
        self.assertEqual(legacy.resolve().parent, self.folder.resolve())
        legacy.mkdir()
        notes = legacy / 'notes.txt'
        notes.write_text('keep me', encoding='utf-8')
        with self.assertRaisesRegex(tvmon_resume.ResumeError, '모두'):
            tvmon_resume.ResumeCache(self.folder, self.resume_id).migrate_legacy()
        self.assertEqual((self.cache / '000000.ts').read_bytes(), packet(0))
        notes.unlink()
        legacy.rmdir()
        self.cache.rename(legacy)
        notes = legacy / 'notes.txt'
        notes.write_text('keep me', encoding='utf-8')
        with self.assertRaisesRegex(tvmon_resume.ResumeError, '다른 파일'):
            tvmon_resume.ResumeCache(self.folder, self.resume_id).migrate_legacy()
        self.assertEqual(notes.read_text(encoding='utf-8'), 'keep me')
        self.assertEqual((legacy / '000000.ts').read_bytes(), packet(0))
        self.assertFalse(self.cache.exists())

    def test_receiving_continues_during_storage_but_pending_data_stays_bounded(self):
        for buffer_limit in (188 * 3 + 94, 32 * 1024 * 1024):
            with self.subTest(buffer_limit=buffer_limit), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                release = threading.Event()
                prefetched = threading.Event()
                fetched, events, results, errors = [], [], [], []
                original_store = tvmon_resume.ResumeCache.store

                def fetch(url, cancel, referer, **kwargs):
                    if url.endswith('manifest'):
                        return playlist(12), url
                    index = int(Path(url).stem.removeprefix('segment'))
                    fetched.append(index)
                    if len(fetched) > 2:
                        prefetched.set()
                    kwargs['on_bytes'](188)
                    return packet(index), url

                def blocked_store(cache, index, data, **kwargs):
                    if not release.wait(5):
                        raise AssertionError('Storage was not released')
                    original_store(cache, index, data, **kwargs)

                def run():
                    try:
                        results.append(downloader.download_video(PAGE_URL, folder, threading.Event(),
                            lambda *item: events.append(item), resume_id=self.resume_id, log_dir=folder/'logs'))
                    except BaseException as error:
                        errors.append(error)

                with patch.object(downloader, 'WORKERS', 2), \
                        patch.object(downloader, 'STORAGE_BUFFER_BYTES', buffer_limit), \
                        patch.object(downloader, 'resolve_episode', return_value=('Pipeline', 'https://media.example/manifest')), \
                        patch.object(downloader, 'fetch', side_effect=fetch), \
                        patch.object(tvmon_resume.ResumeCache, 'store', blocked_store):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(prefetched.wait(2), 'Network workers stalled behind blocked storage')
                        time.sleep(.15)
                        count = len(fetched)
                        time.sleep(.15)
                        self.assertEqual(len(fetched), count, 'Backpressure failed to bound pending bodies')
                        self.assertLessEqual(count, min(4, buffer_limit // 188) + 2)
                        self.assertFalse(any(kind == 'progress' and value['done'] for kind, value in events))
                        state = json.loads((tvmon_resume.cache_path(folder, self.resume_id)/'state.json').read_text(encoding='utf-8'))
                        self.assertEqual(state['parts'], {})
                    finally:
                        release.set()
                        thread.join(8)
                self.assertFalse(thread.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(results[0].read_bytes(), b''.join(packet(index) for index in range(12)))
                rows = [json.loads(line) for line in next((folder/'logs').glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
                receiving = [row for row in rows if row['event'] == 'segment_received']
                self.assertEqual(len(receiving), 12)
                self.assertLessEqual(max(row['storage_buffered_bytes'] for row in receiving), buffer_limit)
                self.assertLessEqual(max(row['storage_pending'] for row in receiving), 4)

    def test_storage_failure_discards_queued_bodies_and_retry_uses_only_durable_pieces(self):
        prefetched = threading.Event()
        fetched, errors = [], []
        original_store = tvmon_resume.ResumeCache.store

        def fetch(index, cancel):
            fetched.append(index)
            if len(fetched) > 2:
                prefetched.set()
            return packet(index)

        def failed_store(cache, index, data):
            if not prefetched.wait(3):
                raise AssertionError('Receiving did not continue during storage')
            if index == 0:
                raise OSError('fixture storage failure')
            original_store(cache, index, data)

        with patch.object(tvmon_resume.ResumeCache, 'store', failed_store):
            with self.assertRaisesRegex(OSError, 'fixture storage failure'):
                self.run_download(body=self.manifest(12), segment_fetch=fetch, workers=2)
        state = json.loads((self.cache/'state.json').read_text(encoding='utf-8'))
        durable = {int(index) for index in state['parts']}
        self.assertNotIn(0, durable)
        self.assertFalse(list(self.folder.glob('*.ts')))
        target = self.run_download(body=self.manifest(12))
        self.assertEqual(set(self.requested_indices()), set(range(12)) - durable)
        self.assertEqual(target.read_bytes(), b''.join(packet(index) for index in range(12)))

    def test_pause_reports_piece_saved_while_other_workers_are_shutting_down(self):
        import tvmon_resume

        cancel = threading.Event()
        pausing = threading.Event()
        second_storing = threading.Event()
        original_store = tvmon_resume.ResumeCache.store

        def delayed_store(cache, index, data):
            if index == 1:
                second_storing.set()
                if not pausing.wait(timeout=5):
                    raise AssertionError("Download did not enter pause shutdown")
            elif index == 0 and not second_storing.wait(timeout=5):
                raise AssertionError("Second worker did not enter validated storage")
            original_store(cache, index, data)

        def report(kind, value):
            self.events.append((kind, value))
            if kind == "progress" and value["done"] == 1:
                cancel.set()
                pausing.set()

        def fetch_until_pause(index, event):
            if index == 2:
                self.assertTrue(event.wait(5), "Durable completion did not trigger pause")
                raise downloader.Cancelled()
            return packet(index)

        with patch.object(tvmon_resume.ResumeCache, "store", new=delayed_store), \
                self.assertRaises(downloader.Cancelled):
            self.run_download(body=self.manifest(3), workers=2, cancel=cancel, report=report,
                              segment_fetch=fetch_until_pause)
        parts = [value for kind, value in self.events if kind == "parts"]
        progress = [value for kind, value in self.events if kind == "progress"]
        self.assertEqual(parts[-1]["completed"], [0, 1])
        self.assertEqual(progress[-1]["done"], 2)
        self.assertEqual(progress[-1]["bytes"], 2 * 188)
        for index in (0, 1):
            self.assertEqual((self.cache / f"{index:06d}.ts").read_bytes(), packet(index))
        target = self.run_download(body=self.manifest(3))
        self.assertEqual(self.requested_indices(), [2])
        self.assertEqual(target.read_bytes(), b"".join(packet(index) for index in range(3)))

    @unittest.skipUnless(os.name == "nt", "Windows open segment handles block deletion")
    def test_failed_discard_preserves_ownership_until_locked_piece_can_be_removed(self):
        self.pause_after_first_segment()
        piece = self.cache / "000000.ts"
        with piece.open("rb") as held_piece:
            with self.assertRaises(OSError):
                downloader.discard_resume(self.folder, self.resume_id)
            self.assertTrue((self.cache / "owner.json").is_file())
            self.assertEqual(held_piece.read(), packet(0))
        downloader.discard_resume(self.folder, self.resume_id)
        self.assertFalse(self.cache.exists())

    def test_different_piece_can_commit_while_another_piece_is_syncing(self):
        first_sync = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        failures = []
        real_sync = tvmon_resume.os.fsync
        def held_sync(fd):
            if threading.current_thread().name == 'held-piece':
                first_sync.set()
                if not release.wait(3):
                    raise AssertionError('The first piece was not released')
            real_sync(fd)
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as cache:
            cache.bind(PAGE_URL, 'fixture', 'storage', 2, 'storage.ts')
            def store(index, done=None):
                try:
                    cache.store(index, packet(index))
                except BaseException as error:
                    failures.append(error)
                finally:
                    if done:
                        done.set()
            first = threading.Thread(target=store, args=(0,), name='held-piece')
            second = threading.Thread(target=store, args=(1, second_done), name='independent-piece')
            with patch.object(tvmon_resume.os, 'fsync', side_effect=held_sync):
                try:
                    first.start()
                    self.assertTrue(first_sync.wait(2))
                    second.start()
                    self.assertTrue(second_done.wait(2), 'Independent piece was blocked by another piece fsync')
                    self.assertEqual(set(cache.state['parts']), {'1'})
                    durable = json.loads((self.cache/'state.json').read_text(encoding='utf-8'))
                    self.assertEqual(set(durable['parts']), {'1'})
                finally:
                    release.set()
                    first.join(4)
                    if second.ident is not None:
                        second.join(4)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(cache.verified_parts(downloader.validate_ts, lambda: None), {0:188, 1:188})
            self.assertEqual(cache._writing, set())

    def test_checkpoint_failure_leaves_previous_records_durable_and_orphan_untrusted(self):
        events = []
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as cache:
            cache.bind(PAGE_URL, 'fixture', 'storage', 2, 'storage.ts')
            cache.store(0, packet(0))
            before = (self.cache/'state.json').read_bytes()
            with patch.object(cache, '_write_state', side_effect=OSError('state write failed')):
                with self.assertRaises(OSError):
                    cache.store(1, packet(1), on_event=lambda event, **fields: events.append((event, fields)))
            self.assertEqual((self.cache/'state.json').read_bytes(), before)
            self.assertEqual(set(cache.state['parts']), {'0'})
            self.assertEqual(cache._writing, set())
            self.assertEqual(events[-1][0], 'segment_storage_error')
            self.assertEqual(events[-1][1]['phase'], 'checkpoint')
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as recovered:
            self.assertEqual(recovered.verified_parts(downloader.validate_ts, lambda: None), {0:188})
            recovered.store(1, packet(1))
            self.assertEqual(recovered.verified_parts(downloader.validate_ts, lambda: None), {0:188, 1:188})

    def test_same_piece_has_one_writer_and_finishes_with_matching_checkpoint(self):
        first_sync = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        second_done = threading.Event()
        failures = []
        real_sync = tvmon_resume.os.fsync
        def held_sync(fd):
            if threading.current_thread().name == 'first-same-piece':
                first_sync.set()
                if not release.wait(3):
                    raise AssertionError('The first writer was not released')
            real_sync(fd)
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as cache:
            cache.bind(PAGE_URL, 'fixture', 'storage', 1, 'storage.ts')
            def store(data, second=False):
                try:
                    if second:
                        second_entered.set()
                    cache.store(0, data)
                except BaseException as error:
                    failures.append(error)
                finally:
                    if second:
                        second_done.set()
            first = threading.Thread(target=store, args=(packet(0),), name='first-same-piece')
            second = threading.Thread(target=store, args=(packet(1), True), name='second-same-piece')
            with patch.object(tvmon_resume.os, 'fsync', side_effect=held_sync):
                try:
                    first.start()
                    self.assertTrue(first_sync.wait(2))
                    second.start()
                    self.assertTrue(second_entered.wait(2))
                    self.assertFalse(second_done.wait(.05))
                finally:
                    release.set()
                    first.join(4)
                    if second.ident is not None:
                        second.join(4)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual((self.cache/'000000.ts').read_bytes(), packet(1))
            self.assertEqual(cache.state['parts']['0']['sha256'], hashlib.sha256(packet(1)).hexdigest())
            self.assertEqual(cache._writing, set())

    def test_piece_sync_failure_never_publishes_a_checkpoint_and_releases_its_writer(self):
        events = []
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as cache:
            cache.bind(PAGE_URL, 'fixture', 'storage', 2, 'storage.ts')
            cache.store(0, packet(0))
            with patch.object(tvmon_resume.os, 'fsync', side_effect=OSError('piece sync failed')):
                with self.assertRaises(OSError):
                    cache.store(1, packet(1), on_event=lambda event, **fields: events.append((event, fields)))
            self.assertEqual(set(cache.state['parts']), {'0'})
            self.assertEqual(cache._writing, set())
            self.assertEqual(events[-1][1]['phase'], 'piece_sync')
            cache.store(1, packet(1))
            self.assertEqual(cache.verified_parts(downloader.validate_ts, lambda: None), {0:188, 1:188})

    def test_storage_timings_follow_a_durable_successful_checkpoint(self):
        records = []
        with tvmon_resume.ResumeCache(self.folder, self.resume_id) as cache:
            cache.bind(PAGE_URL, 'fixture', 'storage', 1, 'storage.ts')
            def report(event, **fields):
                durable = json.loads((self.cache/'state.json').read_text(encoding='utf-8'))
                self.assertEqual(durable['parts']['0']['sha256'], hashlib.sha256(packet(0)).hexdigest())
                records.append((event, fields))
            cache.store(0, packet(0), on_event=report)
        self.assertEqual(len(records), 1)
        event, fields = records[0]
        self.assertEqual(event, 'segment_storage')
        self.assertTrue(all(isinstance(value, float) and value >= 0
                            for key, value in fields.items() if key.endswith('_seconds') or key == 'seconds'))
        self.assertIs(fields['checkpoint_writer'], True)
        self.assertEqual(fields['checkpoint_parts'], 1)
        self.assertIn('piece_sync_seconds', fields)
        self.assertIn('checkpoint_wait_seconds', fields)
        self.assertIn('state_sync_seconds', fields)
        self.assertGreaterEqual(fields['seconds'], fields['checkpoint_seconds'])

    def test_group_checkpoint_is_durable_and_failed_members_are_not_published(self):
        for fail_group in (False, True):
            with self.subTest(fail_group=fail_group):
                folder = self.folder / str(fail_group)
                entered = [threading.Event() for _ in range(3)]
                release = [threading.Event() for _ in range(3)]
                done = {index: threading.Event() for index in range(1, 6)}
                returned, errors, records, snapshots = set(), {}, {}, []
                threads = []
                with tvmon_resume.ResumeCache(folder, self.resume_id) as cache:
                    cache.bind(PAGE_URL, 'fixture', 'grouped storage', 6, 'storage.ts')
                    cache.store(0, packet(0))
                    original_write = cache._write_state

                    def held_checkpoint(state=None):
                        batch = len(snapshots)
                        snapshots.append(set(state['parts']))
                        entered[batch].set()
                        if not release[batch].wait(5):
                            raise AssertionError('Checkpoint was not released')
                        if fail_group and batch == 1:
                            with patch.object(tvmon_resume.os, 'replace', side_effect=OSError('group replace failed')):
                                return original_write(state)
                        return original_write(state)

                    def store(index):
                        try:
                            def report(event, **fields):
                                if event != 'segment_storage':
                                    return
                                durable = json.loads((cache.root/'state.json').read_text(encoding='utf-8'))
                                self.assertEqual(durable['parts'][str(index)]['sha256'],
                                                 hashlib.sha256(packet(index)).hexdigest())
                                records[index] = fields
                            cache.store(index, packet(index), on_event=report)
                            returned.add(index)
                        except BaseException as error:
                            errors[index] = error
                        finally:
                            done[index].set()

                    def start(index):
                        thread = threading.Thread(target=store, args=(index,))
                        threads.append(thread)
                        thread.start()

                    def wait_pending(indices):
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            with cache._guard:
                                if set(cache._pending_checkpoints) == set(indices):
                                    return
                            time.sleep(.005)
                        self.fail('Independent pieces could not queue during checkpoint I/O')

                    with patch.object(cache, '_write_state', side_effect=held_checkpoint):
                        try:
                            start(1)
                            self.assertTrue(entered[0].wait(3))
                            for index in (2, 3, 4):
                                start(index)
                            wait_pending((2, 3, 4))
                            self.assertEqual(set(cache.state['parts']), {'0'})
                            self.assertEqual(set(json.loads((cache.root/'state.json').read_text())['parts']), {'0'})
                            self.assertFalse(any(done[index].is_set() for index in (1, 2, 3, 4)))
                            release[0].set()
                            self.assertTrue(entered[1].wait(3))
                            self.assertTrue(done[1].wait(3))
                            self.assertEqual(set(cache.state['parts']), {'0', '1'})
                            self.assertFalse(any(done[index].is_set() for index in (2, 3, 4)))
                            start(5)
                            wait_pending((5,))
                            release[1].set()
                            self.assertTrue(entered[2].wait(3))
                            for index in (2, 3, 4):
                                self.assertTrue(done[index].wait(3))
                            self.assertFalse(done[5].is_set())
                            committed = {'0', '1'} if fail_group else {'0', '1', '2', '3', '4'}
                            self.assertEqual(set(cache.state['parts']), committed)
                            self.assertEqual(set(json.loads((cache.root/'state.json').read_text())['parts']), committed)
                        finally:
                            for event in release:
                                event.set()
                            for thread in threads:
                                thread.join(5)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                    self.assertEqual(set(errors), {2, 3, 4} if fail_group else set())
                    self.assertTrue(all(isinstance(error, OSError) for error in errors.values()), errors)
                    self.assertEqual(returned, {1, 5} if fail_group else {1, 2, 3, 4, 5})
                    expected = {'0', '1', '5'} if fail_group else {str(index) for index in range(6)}
                    self.assertEqual(set(cache.state['parts']), expected)
                    self.assertEqual(snapshots, [{'0', '1'}, {'0', '1', '2', '3', '4'}, expected])
                    self.assertFalse(cache._checkpointing)
                    self.assertEqual(cache._pending_checkpoints, {})
                    self.assertEqual(cache._writing, set())
                    if not fail_group:
                        self.assertEqual(sum(fields['checkpoint_writer'] for fields in records.values()), 3)
                        self.assertEqual(sum(fields['checkpoint_parts'] for fields in records.values()
                                             if fields['checkpoint_writer']), 5)
                        for index in (2, 3, 4):
                            self.assertEqual(records[index]['checkpoint_parts'], 3)
                            if not records[index]['checkpoint_writer']:
                                self.assertEqual(records[index]['checkpoint_seconds'], 0)
                                self.assertEqual(records[index]['state_sync_seconds'], 0)
                with tvmon_resume.ResumeCache(folder, self.resume_id) as recovered:
                    self.assertEqual(recovered.verified_parts(downloader.validate_ts, lambda: None),
                                     {int(index): 188 for index in expected})
                    if fail_group:
                        for index in (2, 3, 4):
                            recovered.store(index, packet(index))
                        self.assertEqual(recovered.verified_parts(downloader.validate_ts, lambda: None),
                                         {index: 188 for index in range(6)})


if __name__ == "__main__":
    unittest.main()

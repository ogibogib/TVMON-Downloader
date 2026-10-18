"""Manager integration checks with withdrawn Tk windows and local video fixtures."""

import gc
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import tvmon_downloader as downloader
import tvmon_domains
from test_tvmon_downloader import PAGE_URL, packet, playlist


SECOND_URL = "https://tvmon1.com/animation/10771/406785171"
FIRST_EPISODE = PAGE_URL.rsplit("/", 1)[-1]
SECOND_EPISODE = SECOND_URL.rsplit("/", 1)[-1]
TEXT_CONTENT = {"title": "삼국지 작품", "url": "https://toki92.com/novel/1", "thumbnail": "",
                "category": "소설", "genre": "대체역사", "status": "완결", "episodes": "512화",
                "meta": "소설 · 완결 · 512화"}


@unittest.skipUnless(os.name == "nt", "Windows Tk manager integration checks")
class ManagerTests(unittest.TestCase):
    def setUp(self):
        previous_domains = tvmon_domains.get_domains()
        tvmon_domains.set_domains(tvmon_domains.DEFAULT_DOMAINS)
        self.addCleanup(tvmon_domains.set_domains, previous_domains)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = Path(temporary.name)
        self.videos = self.storage / "videos"
        self.state_path = self.storage / "state" / "jobs.json"
        self.apps = []
        self.callback_errors = []
        self.gates = []
        self.requests = []
        self.resolutions = []
        self.segment_handler = None
        self.lock = threading.Lock()
        self.active_fetches = 0
        self.peak_fetches = 0
        for target, value in (("WORKERS", 1), ("resolve_episode", self.resolve), ("fetch", self.fetch)):
            fixture = patch.object(downloader, target, value)
            fixture.start()
            self.addCleanup(fixture.stop)
        domain_fixture = patch.object(downloader, "resolve_current_domain",
                                      side_effect=downloader.DownloadError("테스트에서는 자동 네트워크 확인을 사용하지 않습니다."))
        self.domain_resolver = domain_fixture.start()
        self.addCleanup(domain_fixture.stop)
        text_domain_fixture = patch.object(downloader, "resolve_current_text_domain",
                                           side_effect=downloader.DownloadError("테스트 B 연결 실패"))
        self.text_domain_resolver = text_domain_fixture.start()
        self.addCleanup(text_domain_fixture.stop)
        verify_fixture = patch.object(downloader, "verify_domain",
                                      side_effect=downloader.DownloadError("테스트 연결 실패"))
        self.domain_verifier = verify_fixture.start()
        self.addCleanup(verify_fixture.stop)
        self.addCleanup(self.cleanup_apps)

    def resolve(self, page, cancel):
        episode = page.rsplit("/", 1)[-1]
        self.resolutions.append(episode)
        return f"영상 {episode}", f"https://media.example/{episode}/c.html"

    def fetch(self, url, cancel, referer, **kwargs):
        path = Path(urlsplit(url).path)
        if path.name == "c.html":
            return playlist(3), url
        episode = path.parent.name
        index = int(path.stem.removeprefix("segment"))
        with self.lock:
            self.requests.append((episode, index))
            self.active_fetches += 1
            self.peak_fetches = max(self.peak_fetches, self.active_fetches)
        try:
            if self.segment_handler:
                self.segment_handler(episode, index, cancel)
            marker = index + (10 if episode == SECOND_EPISODE else 0)
            data = packet(marker)
            if kwargs.get("on_bytes"):
                kwargs["on_bytes"](len(data))
            return data, url
        finally:
            with self.lock:
                self.active_fetches -= 1

    def create_app(self, *, scaling=None):
        root = downloader.tk.Tk()
        root.withdraw()
        if scaling is not None:
            previous_scaling = root.tk.call("tk", "scaling")
            self.addCleanup(lambda: root.tk.call("tk", "scaling", previous_scaling))
            root.tk.call("tk", "scaling", scaling)
        # Invisible test windows must not consume desktop keyboard input.
        root.attributes("-disabled", True)
        root.report_callback_exception = lambda error_type, error, traceback: self.callback_errors.append(error)
        try:
            app = downloader.DownloaderApp(root, state_path=self.state_path)
        except BaseException:
            root.update_idletasks()
            root.destroy()
            raise
        self.apps.append(app)
        app.folder.set(str(self.videos))
        return app

    def test_program_log_records_real_domain_order_failure_and_success_promotion(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        order = ['https://tvwiki54.net','https://tvwiki53.net','https://tvmon1.com']
        app._apply_domains(order, 0)
        home = b'<title>TVMON</title><a href="/drama/123">Drama</a>'
        with patch.object(downloader,'verify_domain',tvmon_domains.verify_domain), \
                patch.object(downloader,'_fetch_single',side_effect=[downloader.DownloadError('54 error 12030'),
                    downloader.DownloadError('53 error 12030'),(home,'https://tvmon1.com/')]) as fetch:
            app.domain_refresh_button.invoke()
            self.pump(app,lambda:app.domain_check_worker is None)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],[url+'/' for url in order])
        rows=[json.loads(line[line.index('{'):]) for line in app.program_log_path.read_text(encoding='utf-8').splitlines()]
        started=next(r for r in rows if r['event']=='domain_check_start')
        self.assertEqual(started['order'],order)
        progress=[r for r in rows if r['event']=='domain_check_progress']
        self.assertEqual([r['url'] for r in progress if r['state']=='checking'],[url+'/' for url in order])
        self.assertEqual([r['state'] for r in progress],['checking','error','checking','error','checking','success'])
        self.assertEqual(rows[-1]['event'],'domain_order_saved')
        self.assertEqual(rows[-1]['order'][0],'https://tvmon1.com')
        self.assertTrue(all('pid' in r and 'thread' in r for r in rows))

    def test_program_log_redacts_signed_urls_and_rotates_with_bounded_backups(self):
        app=self.create_app()
        app._write_program_log('redaction_test',detail='한글 https://user:password@media.example/path?token=SECRET#fragment',
                               trackers=[{'url':'wss://tracker.example/?secret=HIDDEN#x'}])
        text=app.program_log_path.read_text(encoding='utf-8')
        self.assertIn('한글',text)
        self.assertIn('https://media.example/path',text)
        self.assertIn('wss://tracker.example/',text)
        for secret in ['user:password','SECRET','HIDDEN','#fragment']:
            self.assertNotIn(secret,text)
        handler=app._program_logger.handlers[0]
        handler.maxBytes=800
        for number in range(20):
            app._write_program_log('rotation_test',number=number,detail='x'*120)
        files=list(app.program_log_path.parent.glob('program.log*'))
        self.assertEqual(len(files),4)
        self.assertIn('"number": 19',app.program_log_path.read_text(encoding='utf-8'))
        self.assertTrue(all(p.stat().st_size<800 for p in files))

    def test_program_log_failure_does_not_stop_a_download(self):
        app=self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        with patch.object(app._program_logger,'log',side_effect=OSError('disk full')):
            job=self.add(app)
            self.pump(app,lambda:job['status']=='completed' and app.active_id is None)
        self.assertTrue(Path(job['file']).is_file())
        self.assertTrue(app._program_log_error)
        self.assertTrue(app._program_log_warned)

    def test_program_log_button_and_ui_exception_write_persistent_records(self):
        app=self.create_app()
        with patch.object(downloader.os,'startfile') as opened:
            app.program_log_button.invoke()
        opened.assert_called_once_with(str(app.program_log_path))
        previous=Mock()
        app._previous_callback_exception=previous
        try:
            raise ValueError('failed https://media.example/path?token=SECRET')
        except ValueError:
            exception=sys.exc_info()
            app.root.report_callback_exception(*exception)
        previous.assert_called_once_with(*exception)
        text=app.program_log_path.read_text(encoding='utf-8')
        self.assertIn('ui_error',text)
        self.assertIn('ValueError',text)
        self.assertNotIn('SECRET',text)
        self.assertIn('프로그램 로그',app.status.get())
        app.close()
        self.assertIsNone(app._program_logger)
        self.assertIn('program_stopped',app.program_log_path.read_text(encoding='utf-8'))

    def test_program_log_records_p2p_changes_without_every_transfer_tick(self):
        app=self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        job=app._new_job(PAGE_URL,str(self.videos),'f'*32)
        job.update(status='downloading',run_id=1)
        app.jobs.append(job)
        app.active_id=job['id']
        for _ in range(3):
            app.events.put((job['id'],1,'transfer',{'speed':10,'p2p_status':'no_peers'}))
        self.pump(app,app.events.empty)
        rows=[json.loads(line[line.index('{'):]) for line in app.program_log_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len([r for r in rows if r['event']=='p2p_state']),1)

    def test_portable_default_migrates_legacy_records_and_keeps_auth_profile(self):
        program = self.storage / "portable-program"
        legacy_root = self.storage / "local-app-data"
        legacy = legacy_root / "TVMON-Download-Manager"
        legacy.mkdir(parents=True)
        original = json.dumps({"version": 1, "jobs": [], "recent_searches": ["이전 검색어"],
                               "auto_domain": False, "auto_text_domain": False,
                               "text_domain": "https://toki92.com"}, ensure_ascii=False).encode()
        (legacy / "jobs.json").write_bytes(original)
        (legacy / "text-domain.log").write_bytes(b"old connection log\n")
        (legacy / "text-domain.previous.log").write_bytes(b"previous connection log\n")
        root = downloader.tk.Tk()
        root.withdraw()
        root.attributes("-disabled", True)
        root.report_callback_exception = lambda error_type, error, traceback: self.callback_errors.append(error)
        try:
            with patch.object(downloader.sys, "frozen", True, create=True), \
                    patch.object(downloader.sys, "executable", str(program / "TVMON-Download-Manager.exe")), \
                    patch.dict(downloader.os.environ, {"LOCALAPPDATA": str(legacy_root)}):
                app = downloader.DownloaderApp(root)
        except BaseException:
            root.destroy()
            raise
        self.apps.append(app)
        self.assertEqual(app.state_path, program / "data" / "jobs.json")
        self.assertEqual(app.text_domain_log_path, program / "data" / "text-domain.log")
        self.assertEqual(app._text_browser_profile, legacy / "text-browser")
        self.assertEqual(app.state_path.read_bytes(), original)
        self.assertEqual(app.recent_searches, ["이전 검색어"])
        self.assertEqual(app.text_domain, "https://toki92.com")
        self.assertEqual(app.text_domain_log_path.read_bytes(), b"old connection log\n")
        self.assertEqual(app.text_domain_log_path.with_suffix(".previous.log").read_bytes(), b"previous connection log\n")
        app._write_text_domain_log("portable_check")
        self.assertIn("portable_check", app.text_domain_log_path.read_text())
        self.assertEqual((legacy / "jobs.json").read_bytes(), original)
        self.assertEqual((legacy / "text-domain.log").read_bytes(), b"old connection log\n")

    def test_portable_existing_state_and_explicit_state_override_are_not_migrated(self):
        legacy_root = self.storage / "legacy"
        legacy = legacy_root / "TVMON-Download-Manager"
        legacy.mkdir(parents=True)
        (legacy / "jobs.json").write_text("legacy records", encoding="utf-8")
        (legacy / "text-domain.log").write_text("legacy log", encoding="utf-8")
        portable = self.storage / "portable" / "data"
        portable.mkdir(parents=True)
        (portable / "jobs.json").write_text("portable records", encoding="utf-8")
        (portable / "text-domain.log").write_text("portable log", encoding="utf-8")
        app = downloader.DownloaderApp.__new__(downloader.DownloaderApp)
        app.state_path = portable / "jobs.json"
        app._migrate_legacy_state(legacy)
        self.assertEqual(app.state_path.read_text(), "portable records")
        self.assertEqual((portable / "text-domain.log").read_text(), "portable log")
        with patch.dict(downloader.os.environ, {"LOCALAPPDATA": str(legacy_root)}):
            isolated = self.create_app()
        self.assertEqual(isolated.state_path, self.state_path)
        self.assertEqual(isolated.recent_searches, [])
        self.assertEqual(isolated._text_browser_profile, self.state_path.parent / "text-browser")

    def test_portable_migration_waits_for_legacy_writer_and_cleans_failed_copy(self):
        import msvcrt
        legacy = self.storage / "legacy"
        portable = self.storage / "portable" / "data"
        legacy.mkdir()
        portable.mkdir(parents=True)
        original = b'{"version":1,"jobs":[]}'
        (legacy / "jobs.json").write_bytes(original)
        app = downloader.DownloaderApp.__new__(downloader.DownloaderApp)
        app.state_path = portable / "jobs.json"
        with (legacy / "jobs.lock").open("w+b") as lock:
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            with self.assertRaisesRegex(downloader.DownloadError, "먼저 종료"):
                app._migrate_legacy_state(legacy)
            self.assertFalse(app.state_path.exists())
        with patch.object(downloader.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            app._migrate_legacy_state(legacy)
        self.assertEqual(list(portable.iterdir()), [])
        self.assertEqual((legacy / "jobs.json").read_bytes(), original)
        app._migrate_legacy_state(legacy)
        self.assertEqual(app.state_path.read_bytes(), original)
        self.assertEqual((legacy / "jobs.json").read_bytes(), original)

    def test_portable_migration_retains_an_existing_destination_log(self):
        legacy = self.storage / "legacy"
        portable = self.storage / "portable" / "data"
        legacy.mkdir()
        portable.mkdir(parents=True)
        (legacy / "jobs.json").write_bytes(b'{"version":1,"jobs":[]}')
        (legacy / "text-domain.log").write_bytes(b"old log")
        (portable / "text-domain.log").write_bytes(b"current log")
        app = downloader.DownloaderApp.__new__(downloader.DownloaderApp)
        app.state_path = portable / "jobs.json"
        app._migrate_legacy_state(legacy)
        self.assertEqual((portable / "text-domain.log").read_bytes(), b"current log")
        self.assertEqual(app.state_path.read_bytes(), (legacy / "jobs.json").read_bytes())

    def cancel_timers(self, app):
        for timer in app.root.tk.call("after", "info"):
            app.root.after_cancel(timer)

    def cleanup_apps(self):
        for gate in self.gates:
            gate.set()
        for app in self.apps:
            app.domain_check_cancel.set()
            if app.domain_check_worker and app.domain_check_worker.is_alive():
                app.domain_check_worker.join(timeout=8)
                self.assertFalse(app.domain_check_worker.is_alive(), "Domain checker did not stop")
            app.text_domain_check_cancel.set()
            if app.text_domain_check_worker and app.text_domain_check_worker.is_alive():
                app.text_domain_check_worker.join(timeout=8)
                self.assertFalse(app.text_domain_check_worker.is_alive(), "Text domain checker did not stop")
            if app.search_dialog is not None:
                app.search_dialog.close()
                app.search_dialog.executor.shutdown(wait=True, cancel_futures=True)
            if app.worker and app.worker.is_alive():
                app.cancel_event.set()
                app.worker.join(timeout=8)
                self.assertFalse(app.worker.is_alive(), "Manager worker did not stop")
            self.cancel_timers(app)
            if not app._destroyed:
                app.root.update_idletasks()
                app.root.destroy()
        self.apps.clear()
        app = None
        # Finalize Tcl objects here, before a later test starts a worker thread.
        gc.collect()

    def pump(self, app, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not app._destroyed:
                app.root.update()
            if condition():
                self.assertEqual(self.callback_errors, [])
                return
            time.sleep(0.01)
        self.fail(f"Manager condition timed out: {app.status.get()}")

    def add(self, app, url=PAGE_URL):
        app.url.set(url)
        app.add_job()
        return app.jobs[-1]

    def create_search_dialog(self, app):
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.show_search()
        app.search_dialog.query_entry.state(["readonly"])
        app.root.update()
        return app.search_dialog

    def hold_after_first_cached_segment(self):
        entered = threading.Event()

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 1:
                entered.set()
                if not cancel.wait(timeout=8):
                    raise AssertionError("Manager did not pause the download")
                raise downloader.Cancelled()

        self.segment_handler = handler
        return entered

    def test_two_links_download_sequentially_with_one_active_video(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 0:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Queued video test did not release the first download")

        self.segment_handler = handler
        first = self.add(app)
        self.pump(app, entered.is_set)
        second = self.add(app, SECOND_URL)
        self.assertEqual(app.active_id, first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.resolutions, [FIRST_EPISODE])
        release.set()
        self.pump(app, lambda: app.active_id is None and all(job["status"] == "completed" for job in app.jobs))
        self.assertEqual(self.resolutions, [FIRST_EPISODE, SECOND_EPISODE])
        self.assertEqual(self.peak_fetches, 1)
        self.assertEqual(Path(first["file"]).read_bytes(), b"".join(packet(index) for index in range(3)))
        self.assertEqual(Path(second["file"]).read_bytes(), b"".join(packet(index + 10) for index in range(3)))

    def test_pausing_one_queued_link_does_not_stop_other_queued_links(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        third_url = "https://tvmon1.com/ent/986/406784623"
        third_episode = third_url.rsplit("/", 1)[-1]

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 0:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Queued pause test did not release the active download")

        self.segment_handler = handler
        first = self.add(app)
        self.pump(app, entered.is_set)
        second = self.add(app, SECOND_URL)
        third = self.add(app, third_url)
        app.tree.selection_set(second["id"])
        app.pause()
        self.assertTrue(app.queue_running)
        self.assertEqual(app.active_id, first["id"])
        self.assertFalse(app.cancel_event.is_set())
        self.assertEqual(second["status"], "paused")
        self.assertEqual(third["status"], "queued")
        release.set()
        self.pump(app, lambda: app.active_id is None and first["status"] == "completed"
                  and third["status"] == "completed")
        self.assertEqual(second["status"], "paused")
        self.assertEqual(self.resolutions, [FIRST_EPISODE, third_episode])
        self.assertFalse(any(episode == SECOND_EPISODE for episode, index in self.requests))
        self.assertEqual(Path(third["file"]).read_bytes(), b"".join(packet(index) for index in range(3)))

    def test_pause_and_restart_restore_jobs_without_network_until_explicit_resume(self):
        app = self.create_app()
        entered = self.hold_after_first_cached_segment()
        first = self.add(app)
        self.pump(app, lambda: entered.is_set() and first["done"] == 1)
        second = self.add(app, SECOND_URL)
        app.tree.selection_set((first["id"], second["id"]))
        app.pause()
        self.pump(app, lambda: app.active_id is None and first["status"] == "paused")
        self.assertEqual(second["status"], "paused")
        app.close()
        self.assertTrue(app._destroyed)
        self.cancel_timers(app)
        cache = downloader.ResumeCache(self.videos, first['id']).root
        legacy = self.videos / cache.name
        self.assertEqual(legacy.resolve().parent, self.videos.resolve())
        cache.rename(legacy)
        before_requests = list(self.requests)
        before_resolutions = list(self.resolutions)
        restarted = self.create_app()
        restarted.root.update()
        self.assertFalse(legacy.exists())
        self.assertEqual((cache / '000000.ts').read_bytes(), packet(0))
        self.assertIsNone(restarted.active_id)
        self.assertIsNone(restarted.worker)
        self.assertFalse(restarted.queue_running)
        self.assertEqual([job["status"] for job in restarted.jobs], ["paused", "paused"])
        self.assertEqual(restarted.jobs[0]["completed_indices"], [0])
        self.assertEqual(self.requests, before_requests)
        self.assertEqual(self.resolutions, before_resolutions)
        self.segment_handler = None
        restarted.tree.selection_set(first["id"])
        restarted.start()
        self.pump(restarted, lambda: restarted.active_id is None and restarted.jobs[0]["status"] == "completed")
        self.assertEqual(self.requests.count((FIRST_EPISODE, 0)), 1)
        self.assertEqual(restarted.jobs[1]["status"], "paused")
        self.assertEqual(Path(restarted.jobs[0]["file"]).read_bytes(),
                         b"".join(packet(index) for index in range(3)))
        self.assertFalse(cache.exists())

    def test_video_cache_cleanup_waits_for_persisted_completion_and_retries(self):
        app = self.create_app()
        original_save = app._save_jobs
        rejected = True
        def save(force=False):
            if rejected and any(job['status'] == 'completed' for job in app.jobs):
                return False
            return original_save(force=force)
        with patch.object(app, '_save_jobs', side_effect=save):
            job = self.add(app)
            self.pump(app, lambda: app.active_id is None and job['status'] == 'completed')
            cache = downloader.ResumeCache(self.videos, job['id']).root
            self.assertTrue(cache.is_dir())
            self.assertNotEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['jobs'][0]['status'], 'completed')
            expected = Path(job['file']).read_bytes()
            rejected = False
            self.pump(app, lambda: not cache.exists())
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['jobs'][0]['status'], 'completed')
        self.assertEqual(Path(job['file']).read_bytes(), expected)
        self.assertEqual(app._resume_cleanup_pending, set())

    def test_restart_cleans_completed_legacy_cache_and_keeps_final_video(self):
        app = self.create_app()
        with patch.object(downloader, 'discard_resume', side_effect=OSError('fixture locked cache')):
            job = self.add(app)
            self.pump(app, lambda: app.active_id is None and job['status'] == 'completed')
        cache = downloader.ResumeCache(self.videos, job['id']).root
        self.assertTrue(cache.is_dir())
        self.assertIn('정리를 보류', job['message'])
        target = Path(job['file'])
        expected = target.read_bytes()
        app.close()
        self.cancel_timers(app)
        legacy = self.videos / cache.name
        self.assertEqual(legacy.resolve().parent, self.videos.resolve())
        cache.rename(legacy)
        requests = list(self.requests)
        restarted = self.create_app()
        self.assertEqual(restarted.jobs[0]['status'], 'completed')
        self.assertFalse(legacy.exists())
        self.assertFalse(cache.exists())
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(self.requests, requests)

    def test_stale_job_and_run_events_cannot_mutate_completed_or_active_jobs(self):
        app = self.create_app()
        first = self.add(app)
        self.pump(app, lambda: app.active_id is None and first["status"] == "completed")
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def handler(episode, index, cancel):
            if episode == SECOND_EPISODE and index == 0:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Stale event test did not release the active download")

        self.segment_handler = handler
        second = self.add(app, SECOND_URL)
        self.pump(app, lambda: entered.is_set() and second["total"] == 3)
        app.events.put((second["id"], second["run_id"] + 100, "error", "stale error"))
        app.events.put(("f" * 32, second["run_id"], "idle", None))
        app.events.put((first["id"], first["run_id"], "progress",
                        {"done": 0, "total": 999, "bytes": 0, "eta": 0}))
        self.pump(app, app.events.empty)
        self.assertEqual((first["status"], first["done"], first["total"]), ("completed", 3, 3))
        self.assertEqual(app.active_id, second["id"])
        self.assertNotEqual(second["status"], "error")
        release.set()
        self.pump(app, lambda: app.active_id is None and second["status"] == "completed")

    def test_resumed_job_keeps_its_saved_folder_when_form_folder_changes(self):
        app = self.create_app()
        entered = self.hold_after_first_cached_segment()
        job = self.add(app)
        self.pump(app, lambda: entered.is_set() and job["done"] == 1)
        app.pause()
        self.pump(app, lambda: app.active_id is None and job["status"] == "paused")
        alternative = self.storage / "new-videos"
        app.folder.set(str(alternative))
        self.segment_handler = None
        app.start()
        self.pump(app, lambda: app.active_id is None and job["status"] == "completed")
        self.assertEqual(Path(job["folder"]), self.videos.resolve())
        self.assertEqual(Path(job["file"]).parent, self.videos.resolve())
        self.assertFalse(alternative.exists())
        self.assertEqual(self.requests.count((FIRST_EPISODE, 0)), 1)

    def test_close_waits_for_worker_checkpoint_and_persists_paused_queue(self):
        app = self.create_app()
        entered = threading.Event()
        cancelled = threading.Event()
        allow_flush = threading.Event()
        self.gates.append(allow_flush)

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 1:
                entered.set()
                if not cancel.wait(timeout=8):
                    raise AssertionError("Close did not cancel the active download")
                cancelled.set()
                if not allow_flush.wait(timeout=8):
                    raise AssertionError("Close test did not allow checkpoint cleanup")
                raise downloader.Cancelled()

        self.segment_handler = handler
        first = self.add(app)
        self.pump(app, lambda: entered.is_set() and first["done"] == 1)
        self.add(app, SECOND_URL)
        app.close()
        self.pump(app, cancelled.is_set)
        self.assertFalse(app._destroyed)
        self.assertEqual(app.active_id, first["id"])
        allow_flush.set()
        self.pump(app, lambda: app._destroyed)
        self.assertFalse(app.worker.is_alive())
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["jobs"][0]["status"], "paused")
        self.assertEqual(saved["jobs"][0]["completed_indices"], [0])
        cache = downloader.ResumeCache(self.videos, first['id']).root
        self.assertEqual((cache / "000000.ts").read_bytes(), packet(0))
        self.assertEqual(self.resolutions, [FIRST_EPISODE])

    def test_failed_queue_persistence_prevents_add_and_resume_network_calls(self):
        app = self.create_app()
        with patch.object(app, "_save_jobs", return_value=False), \
                patch.object(downloader, "download_video") as engine:
            job = self.add(app)
            self.assertIsNone(app.active_id)
            self.assertEqual(job["status"], "error")
            app.start()
            self.assertIsNone(app.active_id)
            self.assertEqual(job["status"], "paused")
            self.assertFalse(app.queue_running)
            engine.assert_not_called()
        self.assertEqual(self.resolutions, [])

    def test_episode_batch_waits_idle_and_restores_supplied_titles_in_order(self):
        app = self.create_app()
        items = [{"title": "작품 1화", "url": PAGE_URL, "thumbnail": "", "meta": "2026.10.01"},
                 {"title": "작품 2화", "url": SECOND_URL, "thumbnail": "", "meta": "2026.10.02"}]
        with patch.object(app, "_save_jobs", wraps=app._save_jobs) as save, \
                patch.object(downloader, "download_video") as engine:
            self.assertEqual(app.add_episodes(items), (2, 0))
            save.assert_called_once_with(force=True)
            app.root.update()
            self.assertFalse(app.queue_running)
            self.assertIsNone(app.active_id)
            self.assertIsNone(app.worker)
            self.assertEqual([job["status"] for job in app.jobs], ["queued", "queued"])
            self.assertEqual([job["url"] for job in app.jobs], [PAGE_URL, SECOND_URL])
            self.assertEqual([job["title"] for job in app.jobs], ["작품 1화", "작품 2화"])
            engine.assert_not_called()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual([job["title"] for job in saved["jobs"]], ["작품 1화", "작품 2화"])
        self.assertEqual([job["status"] for job in saved["jobs"]], ["queued", "queued"])
        app.close()
        self.assertTrue(app._destroyed)
        self.cancel_timers(app)
        restarted = self.create_app()
        restarted.root.update()
        self.assertEqual([job["url"] for job in restarted.jobs], [PAGE_URL, SECOND_URL])
        self.assertEqual([job["title"] for job in restarted.jobs], ["작품 1화", "작품 2화"])
        self.assertEqual([job["status"] for job in restarted.jobs], ["paused", "paused"])
        self.assertFalse(restarted.queue_running)
        self.assertIsNone(restarted.active_id)
        self.assertEqual(self.resolutions, [])
        self.assertEqual(self.requests, [])

    def test_episode_batch_start_persists_all_jobs_before_downloading_in_order(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 0:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Episode batch test did not release the first download")

        self.segment_handler = handler
        start_next = app._start_next

        def check_saved_then_start(*args, **kwargs):
            save.assert_called_once_with(force=True)
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.assertEqual([job["url"] for job in saved["jobs"]], [PAGE_URL, SECOND_URL])
            self.assertEqual([job["status"] for job in saved["jobs"]], ["queued", "queued"])
            self.assertTrue(app.queue_running)
            return start_next(*args, **kwargs)

        with patch.object(app, "_save_jobs", wraps=app._save_jobs) as save, \
                patch.object(app, "_start_next", side_effect=check_saved_then_start) as start:
            result = app.add_episodes([{"title": "작품 1화", "url": PAGE_URL},
                                       {"title": "작품 2화", "url": SECOND_URL}], start=True)
            self.assertEqual(result, (2, 0))
            start.assert_called_once()
        first, second = app.jobs
        self.pump(app, entered.is_set)
        self.assertEqual(app.active_id, first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.resolutions, [FIRST_EPISODE])
        release.set()
        self.pump(app, lambda: app.active_id is None and all(job["status"] == "completed" for job in app.jobs))
        self.assertEqual(self.resolutions, [FIRST_EPISODE, SECOND_EPISODE])
        self.assertEqual(self.peak_fetches, 1)
        self.assertEqual(Path(first["file"]).read_bytes(), b"".join(packet(index) for index in range(3)))
        self.assertEqual(Path(second["file"]).read_bytes(), b"".join(packet(index + 10) for index in range(3)))

    def test_episode_batch_added_to_running_queue_follows_active_download(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def handler(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 0:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Running batch test did not release the active download")

        self.segment_handler = handler
        first = self.add(app)
        self.pump(app, entered.is_set)
        self.assertEqual(app.add_episodes([{"title": "작품 2화", "url": SECOND_URL}]), (1, 0))
        second = app.jobs[-1]
        self.assertTrue(app.queue_running)
        self.assertEqual(app.active_id, first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.resolutions, [FIRST_EPISODE])
        release.set()
        self.pump(app, lambda: app.active_id is None and all(job["status"] == "completed" for job in app.jobs))
        self.assertEqual(self.resolutions, [FIRST_EPISODE, SECOND_EPISODE])
        self.assertEqual(self.peak_fetches, 1)

    def test_episode_batch_skips_existing_completed_and_repeated_urls_per_folder(self):
        app = self.create_app()
        self.assertEqual(app.add_episodes([{"title": "완료된 1화", "url": PAGE_URL}]), (1, 0))
        app.jobs[0]["status"] = "completed"
        canonical_alias = SECOND_URL.replace("https://tvmon1.com/", "https://www.tvmon1.com/") + "/?from=search"
        items = [{"title": "중복 1화", "url": PAGE_URL},
                 {"title": "작품 2화", "url": SECOND_URL},
                 {"title": "중복 2화", "url": canonical_alias}]
        self.assertEqual(app.add_episodes(items), (1, 2))
        self.assertEqual([job["url"] for job in app.jobs], [PAGE_URL, SECOND_URL])
        self.assertEqual([job["title"] for job in app.jobs], ["완료된 1화", "작품 2화"])
        self.assertEqual([job["status"] for job in app.jobs], ["completed", "queued"])
        alternative = self.storage / "other-videos"
        app.folder.set(str(alternative))
        self.assertEqual(app.add_episodes([{"title": "다른 폴더 1화", "url": PAGE_URL}]), (1, 0))
        self.assertEqual(len(app.jobs), 3)
        self.assertEqual(Path(app.jobs[0]["folder"]), self.videos.resolve())
        self.assertEqual(Path(app.jobs[2]["folder"]), alternative.resolve())
        self.assertFalse(app.queue_running)
        self.assertEqual(self.resolutions, [])

    def test_episode_batch_invalid_url_leaves_existing_queue_and_saved_file_unchanged(self):
        app = self.create_app()
        app.add_episodes([{"title": "작품 1화", "url": PAGE_URL}])
        previous_jobs = list(app.jobs)
        original = self.state_path.read_bytes()
        with patch.object(app, "_save_jobs", wraps=app._save_jobs) as save, \
                patch.object(app, "_start_next") as start:
            with self.assertRaises(downloader.DownloadError):
                app.add_episodes([{"title": "작품 2화", "url": SECOND_URL},
                                  {"title": "잘못된 주소", "url": "https://example.com/ent/986/1"}], start=True)
            save.assert_not_called()
            start.assert_not_called()
        self.assertEqual(app.jobs, previous_jobs)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertFalse(app.queue_running)
        self.assertEqual(self.resolutions, [])

    def test_episode_batch_invalid_folder_leaves_queue_unchanged(self):
        app = self.create_app()
        app.add_episodes([{"title": "작품 1화", "url": PAGE_URL}])
        previous_jobs = list(app.jobs)
        original = self.state_path.read_bytes()
        file_path = self.storage / "not-a-directory"
        file_path.write_text("existing file", encoding="utf-8")
        for folder in (" ", str(file_path)):
            with self.subTest(folder=folder), patch.object(app, "_start_next") as start:
                app.folder.set(folder)
                with self.assertRaises(downloader.DownloadError):
                    app.add_episodes([{"title": "작품 2화", "url": SECOND_URL}], start=True)
                start.assert_not_called()
                self.assertEqual(app.jobs, previous_jobs)
                self.assertEqual(self.state_path.read_bytes(), original)
                self.assertFalse(app.queue_running)
        self.assertEqual(file_path.read_text(encoding="utf-8"), "existing file")
        self.assertEqual(self.resolutions, [])

    def test_episode_batch_save_failure_marks_new_jobs_failed_without_starting(self):
        app = self.create_app()
        app.add_episodes([{"title": "작품 1화", "url": PAGE_URL}])
        existing = app.jobs[0]
        original = self.state_path.read_bytes()
        third_url = "https://tvmon1.com/ent/986/406784623"
        with patch.object(app, "_save_jobs", return_value=False) as save, \
                patch.object(app, "_start_next") as start, \
                patch.object(downloader, "download_video") as engine:
            with self.assertRaises(downloader.DownloadError):
                app.add_episodes([{"title": "작품 2화", "url": SECOND_URL},
                                  {"title": "작품 3화", "url": third_url}], start=True)
            save.assert_called_once_with(force=True)
            start.assert_not_called()
            engine.assert_not_called()
        self.assertEqual(len(app.jobs), 3)
        self.assertEqual(existing["status"], "queued")
        self.assertEqual([job["status"] for job in app.jobs[1:]], ["error", "error"])
        self.assertIsNone(app.active_id)
        self.assertIsNone(app.worker)
        self.assertFalse(app.queue_running)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(self.resolutions, [])

    def test_catalog_search_loads_images_and_queues_selected_or_all_episodes(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        contents = [{"title": "삼국지", "url": "https://tvmon1.com/ent/986",
                     "thumbnail": "https://images.example/content.jpg", "meta": "예능 · 2026", "views": "12,345"}]
        episodes = [{"title": f"삼국지 {index + 1}화", "url": f"https://tvmon1.com/ent/986/{406784622 + index}",
                     "thumbnail": f"https://images.example/episode-{index}.jpg", "meta": ""}
                    for index in range(3)]
        ppm = b"P6\n1 1\n255\n\x20\x60\xa0"
        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)) as search, \
                patch.object(tvmon_catalog, "list_episodes", return_value=episodes) as listing, \
                patch.object(downloader, "fetch", side_effect=lambda url, *args, **kwargs: (b"image", url)) as images, \
                patch.object(tvmon_images, "thumbnail_ppm", return_value=ppm) as thumbnail, \
                patch.object(app, "add_episodes", wraps=app.add_episodes) as add, \
                patch.object(downloader, "download_video") as engine:
            dialog.query.set("삼국지")
            dialog.find_button.invoke()
            self.pump(app, lambda: len(dialog.items["contents"]) == 1 and ("contents", 0) in dialog.images)
            search.assert_called_once_with("삼국지", dialog.cancel["contents"], 1, origin=app.domains[0])
            self.assertEqual(dialog.content_tree.item("0", "values"), ("삼국지\n예능 · 2026\n조회 12,345",))
            self.assertTrue(dialog.content_tree.item("0", "image"))
            self.assertTrue(dialog.queue_all_button.instate(["disabled"]))
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: len(dialog.items["episodes"]) == 3 and ("episodes", 2) in dialog.images)
            listing.assert_called_once_with(contents[0]["url"], dialog.cancel["episodes"])
            self.assertEqual(dialog.episode_tree.get_children(), ("0", "1", "2"))
            self.assertEqual(dialog.episode_title.get(), "삼국지")
            self.assertEqual(dialog.episode_status.get(), "전체 3개 회차 · 첫 회차부터 표시")
            self.assertFalse(dialog.queue_all_button.instate(["disabled"]))
            self.assertTrue(dialog.queue_selected_button.instate(["disabled"]))
            dialog.episode_tree.selection_set(("2", "0"))
            self.pump(app, lambda: not dialog.queue_selected_button.instate(["disabled"]))
            self.assertEqual(dialog.selection_status.get(), "2 / 3개 선택")
            dialog.queue_selected_button.invoke()
            add.assert_called_once_with([episodes[0], episodes[2]], start=False)
            self.assertEqual([job["url"] for job in app.jobs], [episodes[0]["url"], episodes[2]["url"]])
            self.assertIn("2개 회차를 Q에 추가", dialog.message.get())
            dialog.queue_all_button.invoke()
            self.assertEqual(add.call_args.args, (episodes,))
            self.assertEqual(add.call_args.kwargs, {"start": False})
            self.assertEqual([job["url"] for job in app.jobs],
                             [episodes[0]["url"], episodes[2]["url"], episodes[1]["url"]])
            self.assertIn("기존 항목 2개 건너뜀", dialog.message.get())
            self.assertEqual([job["status"] for job in app.jobs], ["queued"] * 3)
            self.assertFalse(app.queue_running)
            self.assertIsNone(app.active_id)
            engine.assert_not_called()
            self.assertEqual(images.call_count, 4)
            self.assertEqual(thumbnail.call_count, 4)
            for call in images.call_args_list:
                if call.args[0] == contents[0]["thumbnail"]:
                    self.assertTrue(call.kwargs.get("retry", True))
                else:
                    self.assertIs(call.kwargs["retry"], False)
            dialog.clear_selection()
            self.assertEqual(dialog.selection_status.get(), "0 / 3개 선택")
            self.assertTrue(dialog.download_button.instate(["disabled"]))
        self.assertEqual(self.resolutions, [])

    def test_text_search_shows_pages_progressively_details_filters_and_site_action(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        dialog.source_buttons["B"].invoke()
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        second = {**TEXT_CONTENT, "url": "https://toki92.com/webtoon/2", "title": "다른 작품", "category": "웹툰", "status": ""}
        def search(query, origin, cancel, page, **kwargs):
            if page == 1:
                return [TEXT_CONTENT], True
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Text search second page was not released")
            return [TEXT_CONTENT, second], False
        with patch.object(tvmon_catalog, "search_text_catalog", side_effect=search) as lookup, \
                patch.object(tvmon_catalog, "list_text_episodes", return_value=[]) as text_episodes, \
                patch.object(tvmon_catalog, "list_episodes") as episodes, patch.object(app, "add_episodes") as add, \
                patch.object(downloader.os, "startfile") as open_site:
            dialog.query.set("삼국지")
            dialog.find_button.invoke()
            self.pump(app, lambda: entered.is_set() and len(dialog.items["contents"]) == 1)
            self.assertTrue(dialog.loading)
            self.assertIn("현재 1개 확인", dialog.result_status.get())
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: dialog.active_content == TEXT_CONTENT["url"])
            self.assertEqual(dialog.text_detail_values["genre"].get(), "대체역사")
            self.assertEqual(dialog.text_detail_values["status"].get(), "완결")
            self.assertEqual(dialog.episode_tree.winfo_ismapped(), 1)
            dialog.text_open_button.invoke()
            open_site.assert_called_once_with(TEXT_CONTENT["url"])
            dialog.queue_episodes(all_items=True, start=True)
            add.assert_not_called()
            episodes.assert_not_called()
            release.set()
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual(len(dialog.items["contents"]), 1)
            self.assertEqual(dialog.content_tree.selection(), ("0",))
            self.assertEqual(tuple(dialog.category_selector["values"]), ("소설",))
            self.assertEqual(dialog.category.get(), "소설")
            self.assertTrue(dialog.category_selector.instate(["disabled"]))
            self.assertEqual([(call.args[0], call.args[1], call.args[3]) for call in lookup.call_args_list],
                             [("삼국지", app.text_domain, 1), ("삼국지", app.text_domain, 2)])
            self.assertTrue(all(not call.kwargs["visible"] for call in lookup.call_args_list))
            dialog.category.set("웹툰")
            dialog.filter_contents()
            self.assertEqual(dialog.category.get(), "소설")
            self.assertEqual(dialog.content_tree.get_children(), ("0",))
            self.assertEqual(dialog.active_content, TEXT_CONTENT["url"])
        log = [json.loads(line) for line in app.text_domain_log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["event"] == "search_success" and row["results"] == 1 for row in log))

    def test_search_source_switch_preserves_results_query_and_selected_video_episodes(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        video = {"title": "영상 작품", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}
        episode = {"title": "1회", "url": PAGE_URL, "thumbnail": "", "meta": ""}
        with patch.object(tvmon_catalog, "search_catalog", return_value=([video], False)) as a, \
                patch.object(tvmon_catalog, "list_episodes", return_value=[episode]) as episodes, \
                patch.object(tvmon_catalog, "list_text_episodes", return_value=[]), \
                patch.object(tvmon_catalog, "search_text_catalog", return_value=([TEXT_CONTENT], False)) as b:
            dialog.query.set("공통 검색어")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: len(dialog.items["episodes"]) == 1)
            dialog.episode_tree.selection_set("0")
            dialog.source_buttons["B"].invoke()
            self.assertEqual(dialog.query.get(), "공통 검색어")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: dialog.active_content == TEXT_CONTENT["url"])
            dialog.source_buttons["A"].invoke()
            self.pump(app, lambda: dialog.episode_tree.winfo_ismapped())
            self.assertEqual(dialog.items["contents"], [video])
            self.assertEqual(dialog.items["episodes"], [episode])
            self.assertEqual(dialog.episode_tree.selection(), ("0",))
            self.assertFalse(dialog.download_button.instate(["disabled"]))
            dialog.source_buttons["B"].invoke()
            self.assertEqual(dialog.items["contents"], [TEXT_CONTENT])
            self.assertEqual(dialog.active_content, TEXT_CONTENT["url"])
            self.assertEqual(dialog.content_tree.selection(), ("0",))
            self.assertEqual(dialog.text_detail_values["episodes"].get(), "512화")
            self.assertTrue(dialog.download_button.instate(["disabled"]))
            a.assert_called_once()
            b.assert_called_once()
            episodes.assert_called_once()

    def test_search_uses_reordered_domain_api_and_recovers_from_invalid_response(self):
        app = self.create_app()
        app.auto_domain.set(False)
        dialog = self.create_search_dialog(app)
        mirror, primary = "https://tvwiki53.net", "https://tvmon1.com"
        requests = []
        invalid_primary = False

        def get(url, headers, cancel, limit, on_bytes):
            requests.append(url)
            origin = "https://" + urlsplit(url).netloc
            self.assertEqual(urlsplit(url).path, "/bbs/ajax.searchList.php" if origin == primary else "/bbs/search.php")
            self.assertEqual(headers["Origin"], origin)
            if origin == primary and invalid_primary:
                return b"<html>wrong search response</html>", url
            body = {"list": [{"wr_subject": origin, "href": "/ent/986"}], "pages": ""}
            return json.dumps(body).encode(), url

        with patch.object(downloader, "_get_http_client", return_value=Mock(get=Mock(side_effect=get))):
            for order, expected in (([mirror, primary], mirror), ([primary, mirror], primary),
                                    ([mirror, primary], mirror)):
                self.assertTrue(app._apply_domains(order, 0))
                dialog.query.set("같은 검색어")
                dialog.search()
                self.pump(app, lambda: not dialog.loading)
                self.assertEqual([row["title"] for row in dialog.items["contents"]], [expected])
            invalid_primary = True
            self.assertTrue(app._apply_domains([primary, mirror], 0))
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual([row["title"] for row in dialog.items["contents"]], [mirror])
            self.assertEqual(dialog.content_tree.item("0", "values"), (mirror,))
        self.assertEqual(["https://" + urlsplit(url).netloc for url in requests],
                         [mirror, primary, mirror, primary, mirror])
        self.assertEqual(app.jobs, [])

    def test_search_later_page_failover_restarts_at_first_page_with_new_page_size(self):
        app = self.create_app()
        app.auto_domain.set(False)
        mirror, primary = "https://tvwiki53.net", "https://tvmon1.com"
        self.assertTrue(app._apply_domains([mirror, primary], 0))
        dialog = self.create_search_dialog(app)
        requests = []

        def get(url, headers, cancel, limit, on_bytes):
            origin = "https://" + urlsplit(url).netloc
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            requests.append((origin, page))
            if origin == mirror:
                if page == 2:
                    return b"<html>second page is unavailable</html>", url
                indices, more = [*range(1, 30), 999], True
            else:
                indices, more = range((page - 1) * 20 + 1, min(page * 20, 45) + 1), page < 3
            body = {"list": [{"wr_subject": f"작품 {index}", "href": f"/ent/{index}"} for index in indices],
                    "pages": f'<a href="?page={page + 1}">다음</a>' if more else ""}
            return json.dumps(body).encode(), url

        with patch.object(downloader, "_get_http_client", return_value=Mock(get=Mock(side_effect=get))):
            dialog.query.set("작품")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
        self.assertEqual(requests, [(mirror, 1), (mirror, 2), (primary, 1), (primary, 2), (primary, 3)])
        self.assertEqual([row["url"] for row in dialog.items["contents"]],
                         [f"https://tvmon1.com/ent/{index}" for index in range(1, 46)])
        self.assertEqual(len(dialog.content_tree.get_children()), 45)
        self.assertEqual(app.jobs, [])

    def test_text_episode_selection_saves_only_selected_utf8_and_restores_mixed_queue(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        episodes = [{"title": f"{n}화 · 제목", "url": TEXT_CONTENT["url"] + f"/{n}", "kind": "text",
                     "series_title": "시험 작품", "thumbnail": "", "meta": "2026.10.11"} for n in range(1, 4)]
        with patch.object(tvmon_catalog, "search_text_catalog", return_value=([TEXT_CONTENT], False)), \
                patch.object(tvmon_catalog, "list_text_episodes", return_value=episodes) as listing, \
                patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *args, **kw: ("첫 문단\n\n둘째 문단".encode(), url)) as body:
            dialog.switch_source("B")
            dialog.search(query="시험")
            self.pump(app, lambda: not dialog.loading)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: len(dialog.items["episodes"]) == 3)
            self.assertEqual(dialog.episode_tree.item("2", "values"), ("3화 · 제목", "2026.10.11"))
            self.assertEqual(str(dialog.episode_tree["style"]), "TextEpisode.Treeview")
            self.assertEqual(dialog.download_button["text"], "선택 TXT 저장")
            dialog.episode_tree.selection_set(("0", "2"))
            dialog._update_actions()
            dialog.switch_source("A")
            dialog.switch_source("B")
            self.assertEqual(dialog.episode_tree.selection(), ("0", "2"))
            dialog.download_button.invoke()
            self.pump(app, lambda: len(app.jobs) == 2 and app.active_id is None and all(j["status"] == "completed" for j in app.jobs))
            listing.assert_called_once()
            self.assertEqual([j["title"] for j in app.jobs], ["시험 작품 · 1화 · 제목", "시험 작품 · 3화 · 제목"])
            for job in app.jobs:
                self.assertEqual(Path(job["file"]).suffix, ".txt")
                self.assertEqual(Path(job["file"]).read_bytes(), "첫 문단\n\n둘째 문단\n".encode())
            self.assertTrue(all(c.kwargs["novel_body"] and not c.kwargs["visible"] for c in body.call_args_list))
        app.add_episodes([{"title": "영상", "url": PAGE_URL}])
        app.tree.selection_set(app.jobs[0]["id"])
        with patch.object(downloader.os, "startfile") as open_file:
            app.open_file()
            open_file.assert_called_once_with(app.jobs[0]["file"])
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertEqual([j["kind"] for j in restarted.jobs], ["text", "text", "video"])
        self.assertEqual([j["status"] for j in restarted.jobs], ["completed", "completed", "paused"])
        self.assertEqual(restarted.add_episodes([{**episodes[0], "url": episodes[0]["url"].replace("92", "93")}]), (0, 1))
        restarted.tree.selection_set(restarted.jobs[0]["id"])
        target = Path(restarted.jobs[0]["file"])
        with patch.object(downloader.messagebox, "askyesno", return_value=True), patch.object(downloader, "discard_resume") as discard:
            restarted.remove_jobs()
            discard.assert_not_called()
        self.assertTrue(target.exists())

    def test_text_download_verification_stops_queue_and_only_button_opens_browser(self):
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki93.com"
        items = [{"title": f"{n}화", "kind": "text", "url": f"https://toki92.com/novel/1/{n}"} for n in (1, 2)]
        def read(url, *args, **kwargs):
            if not kwargs["visible"] and url.endswith("/1"):
                raise downloader.BrowserChallengeError(url)
            return b"synthetic text", url
        with patch.object(app, "_fetch_text_in_browser", side_effect=read) as browser:
            app.add_episodes(items, start=True)
            self.pump(app, lambda: app.jobs[0]["status"] == "error" and app.active_id is None)
            self.assertFalse(app.queue_running)
            self.assertEqual(app.jobs[1]["status"], "queued")
            browser.assert_called_once()
            self.assertEqual(browser.call_args.args[0], "https://toki93.com/novel/1/1")
            self.assertFalse(browser.call_args.kwargs["visible"])
            self.assertFalse(app.verification_notice_button.instate(["disabled"]))
            blocked = app.jobs[0]
            self.assertEqual(app.tree.set(blocked["id"], "status"), "인증 필요")
            self.assertIn("verification", app.tree.item(blocked["id"], "tags"))
            self.assertEqual(app.remaining.get(), "인증 대기")
            self.assertIn("브라우저 인증 필요", app.video_meta.get())
            self.assertIn("1개 작업", app.verification_notice_title["text"])
            self.assertTrue(app.verification_notice.winfo_manager())
            app.filter.set("완료")
            app.list_query.set("없는 제목")
            app._filter_changed()
            self.assertFalse(app.tree.get_children())
            self.assertTrue(app.verification_notice.winfo_manager())
            app.verification_notice_button.invoke()
            self.assertEqual(app.tree.selection(), (blocked["id"],))
            self.assertEqual(app.filter.get(), "전체")
            self.assertEqual(app.list_query.get(), "")
            self.pump(app, lambda: app.active_id is None and all(j["status"] == "completed" for j in app.jobs))
            self.assertEqual([c.kwargs["visible"] for c in browser.call_args_list], [False, True, False])
            self.assertFalse(app.verification_notice.winfo_manager())
            self.assertIn("completed", app.tree.item(blocked["id"], "tags"))
        log = [json.loads(line) for line in app.text_domain_log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["event"] == "text_download_verification" for row in log))
        self.assertEqual(sum(row["event"] == "text_download_success" for row in log), 2)
        self.assertNotIn("synthetic text", app.text_domain_log_path.read_text(encoding="utf-8"))

    def test_download_authentication_notice_survives_restart_without_marking_normal_errors(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.add_episodes([{"title": f"{n}화", "kind": "text", "url": f"https://toki92.com/novel/1/{n}"} for n in (1, 2)])
        blocked, ordinary = app.jobs
        blocked.update(status="error", verification_url=blocked["url"])
        ordinary.update(status="error", message="일시적 연결 실패")
        app._refresh_table()
        self.assertEqual(app.tree.set(ordinary["id"], "status"), "오류")
        self.assertIn("error", app.tree.item(ordinary["id"], "tags"))
        self.assertIn("1개 작업", app.verification_notice_title["text"])
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertEqual(restarted.jobs[0]["status"], "paused")
        self.assertEqual(restarted.tree.set(blocked["id"], "status"), "인증 필요")
        self.assertEqual(restarted.tree.set(ordinary["id"], "status"), "일시정지")
        self.assertTrue(restarted.verification_notice.winfo_manager())
        self.assertFalse(restarted.queue_running)
        restarted.tree.selection_set(ordinary["id"])
        restarted._refresh_details(force=True)
        with patch.object(restarted, "_fetch_text_in_browser", return_value=(b"fixture", blocked["url"])) as browser:
            restarted.verification_notice_button.invoke()
            self.pump(restarted, lambda: restarted.active_id is None and restarted.jobs[0]["status"] == "completed")
            browser.assert_called_once()
            self.assertTrue(browser.call_args.kwargs["visible"])
        self.assertFalse(restarted.verification_notice.winfo_manager())
        self.assertEqual(restarted.jobs[1]["status"], "paused")

    def test_text_episode_list_verification_resumes_selected_work_and_ignores_cancelled_list(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        episode = {"title": "1화", "url": TEXT_CONTENT["url"] + "/1", "kind": "text", "thumbnail": ""}
        with patch.object(tvmon_catalog, "search_text_catalog", return_value=([TEXT_CONTENT], False)) as search, \
                patch.object(tvmon_catalog, "list_text_episodes", side_effect=[downloader.BrowserVerificationRequired(TEXT_CONTENT["url"]), [episode]]) as listing:
            dialog.switch_source("B")
            dialog.search(query="시험")
            self.pump(app, lambda: not dialog.loading)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: dialog.text_verification == "episodes")
            self.assertEqual(dialog.items["episodes"], [])
            dialog.text_verify_button.invoke()
            self.pump(app, lambda: len(dialog.items["episodes"]) == 1)
            search.assert_called_once()
            self.assertEqual([c.kwargs["visible"] for c in listing.call_args_list], [False, True])
            token = dialog.generation["episodes"]
            dialog.switch_source("A")
            dialog.events.put(("episodes", token, "list", [episode], None))
            app.root.update()
            self.assertEqual(dialog.items["episodes"], [])

    def test_text_download_cancel_leaves_no_file_and_can_restart(self):
        app = self.create_app()
        app.auto_text_domain.set(False)
        entered = threading.Event()
        def read(url, cancel, **kwargs):
            entered.set()
            if not cancel.wait(4):
                raise AssertionError("cancel not received")
            raise downloader.TransferCancelled()
        with patch.object(app, "_fetch_text_in_browser", side_effect=read):
            app.add_episodes([{"title": "시험 1화", "kind": "text", "url": TEXT_CONTENT["url"] + "/1"}], start=True)
            self.pump(app, entered.is_set)
            app.pause()
            self.pump(app, lambda: app.active_id is None and app.jobs[0]["status"] == "paused")
            self.assertEqual(list(self.videos.iterdir()), [])
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *args, **kwargs: (b"sample", url)):
            app.start()
            self.pump(app, lambda: app.active_id is None and app.jobs[0]["status"] == "completed")

    @staticmethod
    def text_zip_items():
        return [{"title": f"{n}화", "number": n, "kind": "text", "series_title": "ZIP 시험 작품", "thumbnail": "",
                 "url": f"https://toki92.com/novel/1/{n}"} for n in (1, 2, 3)]

    def test_text_zip_filename_uses_episode_numbers_and_explicit_completion(self):
        items = self.text_zip_items()
        self.assertEqual(downloader.text_archive_title(items), "ZIP 시험 작품 · 1~3화")
        complete = [{**item, "series_status": "완결", "series_title": "긴 작품" * 50} for item in items]
        title = downloader.text_archive_title(complete)
        self.assertEqual(len(title), 120)
        self.assertTrue(title.endswith(" · 1~3화 · 완결"))
        self.assertEqual(downloader.text_archive_title([{**items[0], "number": 0}, {**items[1], "number": 19}]), "ZIP 시험 작품 · 0~19화")
        with self.assertRaises(downloader.DownloadError):
            downloader.text_archive_title([{**items[0], "number": None}])

    def test_text_zip_button_uses_all_episodes_and_retries_after_first_pass(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        requests = []
        def fetch(url, *args, **kwargs):
            number = int(url.rsplit("/", 1)[1])
            requests.append(number)
            if number == 2 and requests.count(2) == 1:
                raise downloader.TransferError("temporary failure")
            return f"시험 본문 {number}\n\n다음 문단".encode(), url
        with patch.object(tvmon_catalog, "search_text_catalog", return_value=([TEXT_CONTENT], False)), \
                patch.object(tvmon_catalog, "list_text_episodes", return_value=self.text_zip_items()), \
                patch.object(app, "_fetch_text_in_browser", side_effect=fetch), \
                patch.object(downloader, "TEXT_ZIP_RETRY_DELAYS", (0, 0, 0)):
            dialog.switch_source("B")
            dialog.search(query="시험")
            self.pump(app, lambda: not dialog.loading)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: len(dialog.items["episodes"]) == 3)
            dialog.episode_tree.selection_set("1")
            dialog._update_actions()
            self.assertFalse(dialog.zip_button.instate(["disabled"]))
            self.assertTrue(dialog.zip_button.winfo_ismapped())
            dialog.zip_button.invoke()
            self.pump(app, lambda: len(app.jobs) == 1 and app.jobs[-1]["status"] == "completed" and app.active_id is None)
        archive = app.jobs[-1]
        self.assertEqual(requests, [1, 2, 3, 2])
        self.assertEqual(archive["kind"], "text_zip")
        self.assertTrue(Path(archive["file"]).name.endswith(" · 1~3화 · 완결.zip"))
        self.assertEqual(archive["retry_round"], 1)
        self.assertEqual(len(archive["archive_jobs"]), 3)
        self.assertFalse(list(self.videos.rglob("*.txt")))
        self.assertFalse((self.videos / ".tvmon-zip").exists())
        with zipfile.ZipFile(archive["file"]) as result:
            self.assertEqual(len(result.namelist()), 3)
            for number, name in enumerate(result.namelist(), 1):
                self.assertEqual(result.read(name), f"시험 본문 {number}\n\n다음 문단\n".encode())
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["jobs"][-1]["status"], "completed")
        self.assertEqual(saved["jobs"][-1]["archive_jobs"], archive["archive_jobs"])
        dialog.switch_source("A")
        self.assertFalse(dialog.zip_button.winfo_ismapped())
        self.assertEqual(self.resolutions, [])

    def test_text_zip_permanent_failure_is_bounded_and_manual_resume_only_retries_missing(self):
        app = self.create_app()
        attempts = []
        def fetch(url, *args, **kwargs):
            number = int(url.rsplit("/", 1)[1])
            attempts.append(number)
            if number == 2:
                raise downloader.TransferError("still unavailable")
            return b"sample", url
        with patch.object(app, "_fetch_text_in_browser", side_effect=fetch), \
                patch.object(downloader, "TEXT_ZIP_RETRY_DELAYS", (0, 0, 0)):
            archive = app.add_text_archive(self.text_zip_items())
            self.pump(app, lambda: archive["status"] == "error" and app.active_id is None)
        self.assertEqual(attempts, [1, 2, 3, 2, 2, 2])
        self.assertIn("1개 회차", archive["message"])
        self.assertEqual(archive["retry_round"], 3)
        self.assertFalse(list(self.videos.glob("*.zip*")))
        completed = {j["id"]: Path(j["file"]).read_bytes() for j in app.jobs[:3] if j["status"] == "completed"}
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"recovered", url)) as recovery:
            app.tree.selection_set(archive["id"])
            app.start()
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            recovery.assert_called_once()
            self.assertTrue(recovery.call_args.args[0].endswith("/2"))
        self.assertTrue(all(app._job(key) is None for key in completed))
        self.assertFalse(list(self.videos.rglob("*.txt")))

    def test_text_zip_reuses_completed_txt_repairs_missing_file_and_deduplicates_clicks(self):
        app = self.create_app()
        items = self.text_zip_items()
        app.add_episodes(items)
        first, failed, missing = app.jobs
        old = self.videos / "이미 받은 1화.txt"
        old.write_bytes(b"existing chapter")
        first.update(status="completed", file=str(old), total=1, done=1, completed_indices=[0])
        failed["status"] = "error"
        missing.update(status="completed", file=str(self.videos / "missing.txt"))
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"new chapter", url)) as browser:
            archive = app.add_text_archive(items)
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            self.assertEqual([c.args[0].rsplit("/", 1)[1] for c in browser.call_args_list], ["2", "3"])
            self.assertIs(app.add_text_archive(items), archive)
            self.assertEqual(browser.call_count, 2)
            self.assertEqual(len(app.jobs), 4)
        self.assertEqual(old.read_bytes(), b"existing chapter")
        with patch.object(downloader.os, "startfile") as opened:
            app.open_file()
            opened.assert_called_once_with(archive["file"])

    def test_text_zip_pause_restart_preserves_members_and_completed_txt(self):
        app = self.create_app()
        entered = threading.Event()
        def fetch(url, cancel, **kwargs):
            if url.endswith("/2"):
                entered.set()
                if not cancel.wait(4):
                    raise AssertionError("pause not received")
                raise downloader.TransferCancelled()
            return b"before restart", url
        with patch.object(app, "_fetch_text_in_browser", side_effect=fetch):
            archive = app.add_text_archive(self.text_zip_items())
            self.pump(app, entered.is_set)
            app.tree.selection_set(archive["id"])
            app.pause()
            self.pump(app, lambda: app.active_id is None)
        self.assertEqual([j["status"] for j in app.jobs], ["completed", "paused", "paused", "paused"])
        members = archive["archive_jobs"][:]
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        archive = restarted.jobs[-1]
        self.assertEqual(archive["archive_jobs"], members)
        self.assertFalse(restarted.queue_running)
        with patch.object(restarted, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"after restart", url)) as fetcher:
            restarted.tree.selection_set(archive["id"])
            restarted.start()
            self.pump(restarted, lambda: archive["status"] == "completed" and restarted.active_id is None)
            self.assertEqual([c.args[0].rsplit("/", 1)[1] for c in fetcher.call_args_list], ["2", "3"])
        with zipfile.ZipFile(archive["file"]) as result:
            self.assertEqual(result.read(result.namelist()[0]), b"before restart\n")

    def test_text_zip_authentication_stops_and_explicit_verification_finishes_archive(self):
        app = self.create_app()
        def fetch(url, *args, **kwargs):
            if url.endswith("/2") and not kwargs["visible"]:
                raise downloader.BrowserChallengeError(url)
            return b"fixture", url
        with patch.object(app, "_fetch_text_in_browser", side_effect=fetch) as browser:
            archive = app.add_text_archive(self.text_zip_items())
            self.pump(app, lambda: app.jobs[1]["status"] == "error" and app.active_id is None)
            self.assertFalse(app.queue_running)
            self.assertEqual(browser.call_count, 2)
            self.assertFalse(list(self.videos.glob("*.zip*")))
            self.assertEqual(app.tree.set(archive["id"], "status"), "인증 필요")
            self.assertIn("verification", app.tree.item(archive["id"], "tags"))
            self.assertIn("1개 작업", app.verification_notice_title["text"])
            app.tree.selection_set(archive["id"])
            app._refresh_details(force=True)
            self.assertFalse(app.verification_notice_button.instate(["disabled"]))
            app.verification_notice_button.invoke()
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            self.assertEqual([c.kwargs["visible"] for c in browser.call_args_list], [False, False, True, False])
            self.assertFalse(app.verification_notice.winfo_manager())

    def test_text_zip_retry_wait_can_pause_and_members_cannot_be_removed_alone(self):
        app = self.create_app()
        with patch.object(app, "_fetch_text_in_browser", side_effect=downloader.TransferError("temporary")) as browser, \
                patch.object(downloader, "TEXT_ZIP_RETRY_DELAYS", (60, 60, 60)):
            archive = app.add_text_archive(self.text_zip_items()[:1])
            self.pump(app, lambda: archive["retry_round"] == 1)
            for _ in range(3):
                app._start_next()
            browser.assert_called_once()
            app.tree.selection_set(archive["id"])
            app.pause()
            self.assertFalse(app.queue_running)
        app.tree.selection_set(app.jobs[0]["id"])
        with patch.object(downloader.messagebox, "askyesno") as confirmation:
            app.remove_jobs()
            confirmation.assert_not_called()
            self.assertEqual(len(app.jobs), 2)
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"recovered", url)) as browser:
            app.tree.selection_set(archive["id"])
            app.start()
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            browser.assert_called_once()

    def test_text_zip_save_failure_prevents_download_and_compression(self):
        app = self.create_app()
        with patch.object(app, "_save_jobs", return_value=False), patch.object(app, "_fetch_text_in_browser") as browser, \
                patch.object(downloader, "save_text_zip") as compress:
            with self.assertRaises(downloader.DownloadError):
                app.add_text_archive(self.text_zip_items())
            browser.assert_not_called()
            compress.assert_not_called()
        self.assertIsNone(app.active_id)

    def test_zip_cleanup_preserves_existing_txt_and_removes_temp_rows_across_restart(self):
        app = self.create_app()
        items = self.text_zip_items()
        app.add_episodes(items[:1])
        original = app.jobs[0]
        path = self.videos / "original.txt"
        path.write_bytes(b"user original")
        original.update(status="completed", file=str(path), total=1, done=1, completed_indices=[0])
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"temporary", url)) as browser:
            archive = app.add_text_archive(items)
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            self.assertEqual(browser.call_count, 2)
            self.assertEqual(app.jobs, [original, archive])
            self.assertIs(app.add_text_archive(items), archive)
            self.assertEqual(browser.call_count, 2)
            self.assertEqual(len(app.jobs), 2)
        self.assertEqual(path.read_bytes(), b"user original")
        self.assertFalse((self.videos / ".tvmon-zip").exists())
        self.assertFalse(archive["cleanup_pending"])
        self.assertEqual(len(archive["archive_urls"]), 3)
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        with patch.object(restarted, "_fetch_text_in_browser") as browser:
            self.assertIs(restarted.add_text_archive(items), restarted.jobs[-1])
            browser.assert_not_called()
        self.assertEqual(len(restarted.jobs), 2)

    def test_zip_original_cleanup_checkbox_reuses_existing_zip_and_keeps_unrelated_files(self):
        app = self.create_app()
        items = self.text_zip_items()
        app.add_episodes(items)
        originals = []
        for n, job in enumerate(app.jobs):
            path = self.videos / f"original-{n}.txt"
            path.write_bytes(f"existing {n}".encode())
            originals.append(path)
            job.update(status="completed", file=str(path), total=1, done=1, completed_indices=[0])
        unrelated = self.videos / "unrelated.txt"
        unrelated.write_bytes(b"leave alone")
        with patch.object(app, "_fetch_text_in_browser") as browser:
            archive = app.add_text_archive(items)
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            self.assertTrue(all(path.exists() for path in originals))
            old_zip = Path(archive["file"]).read_bytes()
            dialog = self.create_search_dialog(app)
            dialog.switch_source("B")
            dialog.items["episodes"] = items
            dialog.zip_cleanup_originals.set(True)
            dialog.queue_text_zip()
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
            browser.assert_not_called()
        self.assertEqual(app.jobs, [archive])
        self.assertEqual(app.tree.get_children(), (archive["id"],))
        self.assertTrue(archive["cleanup_originals"])
        self.assertFalse(any(path.exists() for path in originals))
        self.assertEqual(unrelated.read_bytes(), b"leave alone")
        self.assertEqual(Path(archive["file"]).read_bytes(), old_zip)
        self.assertEqual(len(list(self.videos.glob("*.zip"))), 1)

    def test_zip_cleanup_waits_for_persisted_completion_and_resumes_without_download(self):
        app = self.create_app()
        save = app._save_jobs
        rejected = []
        def persist(*args, **kwargs):
            if not rejected and any(j.get("kind") == "text_zip" and j["status"] == "completed" and j["cleanup_pending"] for j in app.jobs):
                rejected.append(True)
                return False
            return save(*args, **kwargs)
        with patch.object(app, "_save_jobs", side_effect=persist), \
                patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"protected", url)):
            archive = app.add_text_archive(self.text_zip_items())
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
        self.assertTrue(rejected)
        self.assertTrue(archive["cleanup_pending"])
        self.assertEqual(len(app.jobs), 4)
        self.assertEqual(len(list(self.videos.rglob("*.txt"))), 3)
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        archive = restarted.jobs[-1]
        restarted.tree.selection_set(archive["id"])
        restarted._refresh_details(force=True)
        self.assertEqual(restarted.job_menu.entrycget(2, "label"), "TXT 정리 재시도")
        with patch.object(restarted, "_fetch_text_in_browser") as browser:
            restarted.job_menu.invoke(2)
            self.pump(restarted, lambda: not archive["cleanup_pending"] and restarted.active_id is None)
            browser.assert_not_called()
        self.assertEqual(restarted.jobs, [archive])
        self.assertFalse(list(self.videos.rglob("*.txt")))

    def test_zip_cleanup_partial_delete_keeps_failed_row_and_retries_only_cleanup(self):
        app = self.create_app()
        original_unlink = Path.unlink
        blocked = []
        def unlink(path, *args, **kwargs):
            if path.suffix == ".txt" and "2화" in path.name:
                blocked.append(path)
                raise PermissionError("open file")
            return original_unlink(path, *args, **kwargs)
        with patch.object(app, "_fetch_text_in_browser", side_effect=lambda url, *a, **kw: (b"example", url)), \
                patch.object(Path, "unlink", new=unlink):
            archive = app.add_text_archive(self.text_zip_items())
            self.pump(app, lambda: archive["status"] == "completed" and app.active_id is None)
        self.assertTrue(blocked)
        self.assertTrue(archive["cleanup_pending"])
        self.assertEqual(len(app.jobs), 2)
        self.assertTrue(blocked[0].exists())
        self.assertEqual(app.tree.item(archive["id"], "values")[1], "TXT 정리 대기")
        with patch.object(app, "_fetch_text_in_browser") as browser:
            app.tree.selection_set(archive["id"])
            app.start()
            self.pump(app, lambda: not archive["cleanup_pending"] and app.active_id is None)
            browser.assert_not_called()
        self.assertFalse(blocked[0].exists())
        self.assertEqual(app.jobs, [archive])

    def test_text_search_verification_resumes_original_query_only_after_explicit_button(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        dialog.switch_source("B")
        with patch.object(tvmon_catalog, "search_text_catalog", side_effect=[
                downloader.BrowserVerificationRequired(app.text_domain + "/search?q=test"), ([TEXT_CONTENT], False)]) as lookup:
            dialog.query.set("삼국지")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
            self.assertTrue(dialog.text_verification)
            self.assertEqual(lookup.call_count, 1)
            self.assertFalse(lookup.call_args.kwargs["visible"])
            dialog.query.set("아직 검색하지 않은 제목")
            dialog.text_verify_button.invoke()
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual(lookup.call_args.args[0], "삼국지")
            self.assertTrue(lookup.call_args.kwargs["visible"])
            self.assertFalse(dialog.text_verification)
            self.assertEqual(dialog.items["contents"], [TEXT_CONTENT])

    def test_text_search_empty_failure_and_partial_failure_have_distinct_states(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        dialog.switch_source("B")
        dialog.query.set("삼국지")
        for replies, expected_count, expected_status in (([([], False)], 0, "전체 0개"),
                ([downloader.DownloadError("접속 시간 초과")], 0, "검색하지 못했습니다"),
                ([([TEXT_CONTENT], True), downloader.DownloadError("두 번째 페이지 오류")], 1, "전체 결과를 불러오지 못했습니다")):
            with self.subTest(status=expected_status), patch.object(tvmon_catalog, "search_text_catalog", side_effect=replies):
                dialog.search()
                self.pump(app, lambda: not dialog.loading)
                self.assertEqual(len(dialog.items["contents"]), expected_count)
                self.assertIn(expected_status, dialog.result_status.get())
                self.assertFalse(dialog.text_verification)

    def test_text_search_waits_for_address_and_uses_original_query_after_domain_verification(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        dialog = self.create_search_dialog(app)
        dialog.switch_source("B")
        def refresh(**kwargs):
            app._text_domain_result = None
            app.text_domain_check_worker = object()
        with patch.object(app, "refresh_text_domain", side_effect=refresh) as check, \
                patch.object(tvmon_catalog, "search_text_catalog", return_value=([TEXT_CONTENT], False)) as lookup:
            dialog.query.set("삼국지")
            dialog.search()
            self.assertEqual(dialog.pending_text_query, "삼국지")
            lookup.assert_not_called()
            app.text_domain_verification_url = "https://toki92.com/"
            app._text_domain_result = "verification"
            app.text_domain_check_worker = None
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual(dialog.text_verification, "domain")
            dialog.text_verify_button.invoke()
            check.assert_called_with(browser_verification=True)
            app.text_domain = "https://toki92.com"
            app.text_domain_verification_url = ""
            app._text_domain_result = "success"
            app.text_domain_check_worker = None
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual(lookup.call_args.args[:2], ("삼국지", "https://toki92.com"))
            self.assertEqual(dialog.items["contents"], [TEXT_CONTENT])

    def test_switching_away_cancels_text_search_and_discards_late_response(self):
        import tvmon_catalog
        app = self.create_app()
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki92.com"
        dialog = self.create_search_dialog(app)
        dialog.switch_source("B")
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        def search(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Stale text search was not released")
            return [TEXT_CONTENT], False
        with patch.object(tvmon_catalog, "search_text_catalog", side_effect=search):
            dialog.query.set("삼국지")
            dialog.search()
            cancel = dialog.cancel["contents"]
            self.pump(app, entered.is_set)
            dialog.switch_source("A")
            self.assertTrue(cancel.is_set())
            release.set()
            dialog.executor.shutdown(wait=True)
            app.root.update()
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.source, "A")
            dialog.switch_source("B")
            self.assertEqual(dialog.items["contents"], [])
            self.assertIn("중단", dialog.result_status.get())
            self.assertIn("중단", dialog.message.get())

    def test_shared_text_browser_serializes_requests_and_waiting_search_can_cancel(self):
        app = self.create_app()
        app.auto_text_domain.set(False)
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        calls, errors = [], []
        def get(url, *args, **kwargs):
            calls.append(url)
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Shared browser request was not released")
            return b"page", url
        app._text_browser = Mock(closed=False, get=Mock(side_effect=get))
        def run(url, cancel):
            try:
                app._fetch_text_in_browser(url, cancel, limit=1024)
            except Exception as error:
                errors.append(error)
        first = threading.Thread(target=run, args=("https://toki92.com/", threading.Event()))
        cancel = threading.Event()
        second = threading.Thread(target=run, args=("https://toki92.com/search?q=x", cancel))
        first.start()
        self.assertTrue(entered.wait(timeout=3))
        second.start()
        cancel.set()
        second.join(timeout=3)
        release.set()
        first.join(timeout=3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(calls, ["https://toki92.com/"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], downloader.Cancelled)

    def test_catalog_thumbnail_automatically_retries_fetch_or_decode_failure(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        thumbnail_url = "https://images.example/content.jpg"
        contents = [{"title": "작품", "url": "https://tvmon1.com/ent/986",
                     "thumbnail": thumbnail_url, "meta": ""}]
        key = ("contents", 0)
        ppm = b"P6\n1 1\n255\n\x20\x60\xa0"
        for failure in ("fetch", "decode"):
            with self.subTest(failure=failure), \
                    patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)), \
                    patch.object(downloader, "fetch", return_value=(b"image", thumbnail_url)) as fetch, \
                    patch.object(tvmon_images, "thumbnail_ppm", return_value=ppm) as decode:
                if failure == "fetch":
                    fetch.side_effect = [downloader.DownloadError("일시적인 연결 실패"), (b"image", thumbnail_url)]
                else:
                    decode.side_effect = [ValueError("잘못된 이미지 응답"), ppm]
                dialog.query.set(failure)
                dialog.search()
                self.pump(app, lambda: key in dialog.image_retry_at and key not in dialog.requested_images)
                self.assertEqual(dialog.image_attempts[key], 1)
                self.assertEqual(dialog.content_tree.item("0", "text"), "다시 받는 중")
                self.assertNotIn(key, dialog.images)
                self.assertGreater(dialog.image_retry_at[key], time.monotonic())
                self.assertEqual(fetch.call_count, 1)
                dialog.image_retry_at[key] = 0
                self.pump(app, lambda: key in dialog.images)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(decode.call_count, 1 if failure == "fetch" else 2)
                self.assertEqual(dialog.image_attempts[key], 2)
                self.assertNotIn(key, dialog.image_retry_at)
                self.assertEqual(dialog.content_tree.item("0", "text"), "")
                self.assertTrue(dialog.content_tree.item("0", "image"))

    def test_episode_thumbnail_fetch_or_decode_failure_is_never_retried(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        contents = [{"title": "작품", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}]
        thumbnail_url = "https://images.example/episode.jpg"
        episodes = [{"title": "작품 1화", "url": PAGE_URL, "thumbnail": thumbnail_url, "meta": ""}]
        key = ("episodes", 0)
        for failure in ("fetch", "decode"):
            with self.subTest(failure=failure), \
                    patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)), \
                    patch.object(tvmon_catalog, "list_episodes", return_value=episodes), \
                    patch.object(downloader, "fetch", return_value=(b"image", thumbnail_url)) as fetch, \
                    patch.object(tvmon_images, "thumbnail_ppm", return_value=b"P6\n1 1\n255\n\x20\x60\xa0") as decode:
                if failure == "fetch":
                    fetch.side_effect = downloader.DownloadError("회차 썸네일 연결 실패")
                else:
                    decode.side_effect = ValueError("회차 썸네일 변환 실패")
                dialog.query.set(failure)
                dialog.search()
                self.pump(app, lambda: dialog.items["contents"] == contents and not dialog.loading)
                dialog.content_tree.selection_set("0")
                self.pump(app, lambda: dialog.image_attempts.get(key) == 1
                          and key not in dialog.requested_images
                          and dialog.episode_tree.item("0", "text") == "이미지 없음")
                self.assertNotIn(key, dialog.images)
                self.assertNotIn(key, dialog.image_retry_at)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(decode.call_count, 0 if failure == "fetch" else 1)
                for _ in range(2):
                    dialog.episode_tree.yview_scroll(1, "units")
                    dialog.episode_tree.event_generate("<Configure>")
                    dialog._load_visible_images("episodes")
                    dialog.retry_images_button.invoke()
                polled = threading.Event()
                app.root.after(180, polled.set)
                self.pump(app, polled.is_set)
                fetch.assert_called_once_with(thumbnail_url, dialog.cancel["episodes"], PAGE_URL,
                                              limit=8 * 1024 * 1024, retry=False)
                self.assertEqual(decode.call_count, 0 if failure == "fetch" else 1)
                self.assertEqual(dialog.image_attempts[key], 1)
                self.assertNotIn(key, dialog.image_retry_at)
                self.assertEqual(dialog.episode_tree.item("0", "text"), "이미지 없음")
                self.assertFalse(dialog.episode_tree.item("0", "image"))

    def test_catalog_thumbnail_exhaustion_requires_manual_retry_without_duplicate_requests(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        thumbnail_url = "https://images.example/content.jpg"
        contents = [{"title": "작품", "url": "https://tvmon1.com/ent/986",
                     "thumbnail": thumbnail_url, "meta": ""}]
        key = ("contents", 0)
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        requests = []

        def fetch(url, cancel, referer, **kwargs):
            requests.append(url)
            if len(requests) <= 3:
                raise downloader.DownloadError("일시적인 이미지 오류")
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Manual thumbnail retry test did not release the image")
            return b"image", url

        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)), \
                patch.object(downloader, "fetch", side_effect=fetch), \
                patch.object(tvmon_images, "thumbnail_ppm", return_value=b"P6\n1 1\n255\n\x20\x60\xa0"):
            dialog.query.set("작품")
            dialog.search()
            for attempt in range(1, 4):
                self.pump(app, lambda: dialog.image_attempts.get(key) == attempt
                          and key in dialog.image_retry_at and key not in dialog.requested_images)
                self.assertEqual(len(requests), attempt)
                dialog.image_retry_at[key] = 0
            self.assertEqual(dialog.content_tree.item("0", "text"), "다시 받기 필요")
            self.assertNotIn(key, dialog.images)
            dialog._load_visible_images("contents")
            polled = threading.Event()
            app.root.after(120, polled.set)
            self.pump(app, polled.is_set)
            self.assertEqual(len(requests), 3)
            dialog.retry_images_button.invoke()
            self.pump(app, entered.is_set)
            self.assertEqual(len(requests), 4)
            self.assertEqual(dialog.image_attempts[key], 1)
            self.assertIn(key, dialog.requested_images)
            dialog.retry_images_button.invoke()
            dialog.retry_images_button.invoke()
            dialog._load_visible_images("contents")
            polled.clear()
            app.root.after(120, polled.set)
            self.pump(app, polled.is_set)
            self.assertEqual(len(requests), 4)
            self.assertEqual(dialog.image_attempts[key], 1)
            release.set()
            self.pump(app, lambda: key in dialog.images)
            self.assertNotIn(key, dialog.image_retry_at)
            self.assertTrue(dialog.content_tree.item("0", "image"))
            dialog.retry_images_button.invoke()
            self.assertEqual(len(requests), 4)

    def test_catalog_thumbnail_failure_after_reset_or_close_cannot_mutate_current_images(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        key = ("contents", 0)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        close_entered = threading.Event()
        close_finished = threading.Event()
        self.gates.append(release)

        def search(query, cancel, page, *, origin=None):
            return [{"title": query, "url": "https://tvmon1.com/ent/986",
                     "thumbnail": f"https://images.example/{query}.jpg", "meta": ""}], False

        def fetch(url, cancel, referer, **kwargs):
            if url.endswith("/old.jpg"):
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Reset thumbnail test did not release the old request")
                finished.set()
                raise downloader.DownloadError("이전 썸네일 실패")
            if url.endswith("/closing.jpg"):
                close_entered.set()
                if not cancel.wait(timeout=8):
                    raise AssertionError("Closing dialog did not cancel the thumbnail request")
                close_finished.set()
                raise downloader.DownloadError("닫힌 창의 썸네일 실패")
            return b"image", url

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search), \
                patch.object(downloader, "fetch", side_effect=fetch), \
                patch.object(tvmon_images, "thumbnail_ppm", return_value=b"P6\n1 1\n255\n\x20\x60\xa0"):
            dialog.query.set("old")
            dialog.search()
            self.pump(app, entered.is_set)
            old_generation = dialog.generation["contents"]
            old_cancel = dialog.cancel["contents"]
            dialog.query.set("new")
            dialog.search()
            self.assertTrue(old_cancel.is_set())
            self.pump(app, lambda: key in dialog.images)
            displayed = dialog.content_tree.item("0", "image")
            release.set()
            self.pump(app, finished.is_set)
            dialog.events.put(("contents", old_generation, "image", (0, None), "늦게 도착한 이미지 실패"))
            self.pump(app, dialog.events.empty)
            self.assertEqual(dialog.content_tree.item("0", "image"), displayed)
            self.assertEqual(dialog.content_tree.item("0", "text"), "")
            self.assertEqual(dialog.image_attempts, {key: 1})
            self.assertEqual(dialog.image_retry_at, {})
            self.assertEqual(dialog.requested_images, {key})
            dialog.query.set("closing")
            dialog.search()
            self.pump(app, close_entered.is_set)
            generation = dict(dialog.generation)
            attempts = dict(dialog.image_attempts)
            dialog.close()
            self.pump(app, close_finished.is_set)
            dialog.executor.shutdown(wait=True, cancel_futures=True)
            self.assertEqual(dialog.generation, generation)
            self.assertEqual(dialog.image_attempts, attempts)
            self.assertEqual(dialog.image_retry_at, {})
            self.assertEqual(dialog.images, {})
            self.assertTrue(dialog.events.empty())
            self.assertEqual(self.callback_errors, [])

    def test_catalog_replacement_search_ignores_late_and_stale_results(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.gates.append(release)
        old_items = [{"title": "이전 결과", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}]
        new_items = [{"title": "새 결과", "url": "https://tvmon1.com/animation/10771", "thumbnail": "", "meta": ""}]

        def search(query, cancel, page, *, origin=None):
            if query == "이전":
                if page == 1:
                    return old_items, True
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Replacement search test did not release the old response")
                finished.set()
                return [{"title": "늦은 결과", "url": "https://tvmon1.com/ent/987", "thumbnail": "", "meta": ""}], True
            return new_items, False

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("이전")
            dialog.search()
            self.pump(app, entered.is_set)
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.content_tree.get_children(), ())
            old_cancel = dialog.cancel["contents"]
            old_generation = dialog.generation["contents"]
            dialog.query.set("새 검색")
            dialog.search()
            self.assertTrue(old_cancel.is_set())
            self.pump(app, lambda: dialog.items["contents"] == new_items)
            release.set()
            self.pump(app, finished.is_set)
            dialog.events.put(("contents", old_generation, "list", old_items, None))
            self.pump(app, dialog.events.empty)
            self.assertEqual(dialog.items["contents"], new_items)
            self.assertEqual(dialog.content_tree.item("0", "values"), ("새 결과",))
            self.assertEqual(dialog.result_status.get(), "‘새 검색’ · 전체 1개")
            self.assertEqual([(call.args[0], call.args[2]) for call in lookup.call_args_list],
                             [("이전", 1), ("이전", 2), ("새 검색", 1)])
        self.assertEqual(app.jobs, [])

    def test_catalog_search_aggregates_all_22_unique_matches_with_original_query(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        first = [{"title": f"삼국지 검색 결과 {index + 1}", "url": f"https://tvmon1.com/ent/{1000 + index}",
                  "thumbnail": "", "meta": ""} for index in range(20)]
        remaining = [{"title": "삼국지", "url": "https://tvmon1.com/world/2558", "thumbnail": "", "meta": ""},
                     {"title": "삼국지 영화", "url": "https://tvmon1.com/movie/4047", "thumbnail": "", "meta": ""}]
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def search(query, cancel, page, *, origin=None):
            if page == 1:
                return first, True
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Complete search test did not release the second page")
            return [{**first[0], "title": "중복 결과"}, *remaining], False

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("삼국지")
            dialog.find_button.invoke()
            self.pump(app, entered.is_set)
            self.assertTrue(dialog.loading)
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.content_tree.get_children(), ())
            self.assertNotIn("전체 20개", dialog.result_status.get())
            dialog.query.set("아직 검색하지 않은 제목")
            release.set()
            self.pump(app, lambda: len(dialog.items["contents"]) == 22 and not dialog.loading)
            self.assertEqual([(call.args[0], call.args[2]) for call in lookup.call_args_list],
                             [("삼국지", 1), ("삼국지", 2)])
            self.assertEqual(dialog.items["contents"], first + remaining)
            self.assertEqual(len({item["url"] for item in dialog.items["contents"]}), 22)
            self.assertEqual(len(dialog.content_tree.get_children()), 22)
            self.assertEqual(dialog.content_tree.item("0", "values"), ("삼국지 검색 결과 1",))
            self.assertEqual(dialog.search_term, "삼국지")
            self.assertEqual(dialog.result_status.get(), "‘삼국지’ · 전체 22개")
        self.assertEqual(app.jobs, [])

    def test_catalog_later_page_error_does_not_display_partial_search_results(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        first = [{"title": "첫 페이지 결과", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}]
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def search(query, cancel, page, *, origin=None):
            if page == 1:
                return first, True
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Search error test did not release the second page")
            raise downloader.DownloadError("두 번째 검색 페이지 오류")

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("삼국지")
            dialog.search()
            self.pump(app, entered.is_set)
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.content_tree.get_children(), ())
            release.set()
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual([(call.kwargs["origin"], call.args[2]) for call in lookup.call_args_list],
                             [(origin, page) for origin in app.domains for page in (1, 2)])
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.content_tree.get_children(), ())
            self.assertEqual(dialog.result_status.get(), "검색하지 못했습니다. 다시 검색해 주세요.")
            self.assertIn("두 번째 검색 페이지 오류", dialog.message.get())
            self.assertTrue(dialog.queue_all_button.instate(["disabled"]))
        self.assertEqual(app.jobs, [])

    def test_catalog_repeated_next_page_fails_after_one_repeat(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        repeated = [{"title": "반복 결과", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}]

        def search(query, cancel, page, *, origin=None):
            if page > 3:
                raise downloader.DownloadError("테스트 페이지 상한에 도달했습니다.")
            return repeated, True

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("삼국지")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
            self.assertEqual([(call.kwargs["origin"], call.args[2]) for call in lookup.call_args_list],
                             [(origin, page) for origin in app.domains for page in (1, 2)])
            self.assertEqual(dialog.items["contents"], [])
            self.assertEqual(dialog.content_tree.get_children(), ())
            self.assertEqual(dialog.result_status.get(), "검색하지 못했습니다. 다시 검색해 주세요.")
            self.assertIn("검색 결과의 다음 페이지가 반복됩니다.", dialog.message.get())
        self.assertEqual(app.jobs, [])

    def test_catalog_content_change_clears_selection_and_actions_while_loading(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        contents = [{"title": "첫 작품", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""},
                    {"title": "다음 작품", "url": "https://tvmon1.com/animation/10771", "thumbnail": "", "meta": ""}]
        first_items = [{"title": "첫 작품 1화", "url": PAGE_URL, "thumbnail": "", "meta": ""}]
        second_items = [{"title": "다음 작품 1화", "url": SECOND_URL, "thumbnail": "", "meta": ""}]
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def listing(url, cancel):
            if url == contents[1]["url"]:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Content change test did not release the episode list")
                return second_items
            return first_items

        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)), \
                patch.object(tvmon_catalog, "list_episodes", side_effect=listing):
            dialog.query.set("작품")
            dialog.search()
            self.pump(app, lambda: dialog.items["contents"] == contents)
            dialog.content_tree.selection_set("0")
            self.pump(app, lambda: dialog.items["episodes"] == first_items)
            dialog.select_all()
            self.assertFalse(dialog.queue_selected_button.instate(["disabled"]))
            previous_cancel = dialog.cancel["episodes"]
            dialog.content_tree.selection_set("1")
            self.pump(app, entered.is_set)
            self.assertTrue(previous_cancel.is_set())
            self.assertEqual(dialog.items["episodes"], [])
            self.assertEqual(dialog.episode_tree.get_children(), ())
            self.assertEqual(dialog.episode_tree.selection(), ())
            self.assertEqual(dialog.selection_status.get(), "0 / 0개 선택")
            self.assertEqual(dialog.episode_title.get(), "다음 작품")
            for button in (dialog.queue_selected_button, dialog.queue_all_button, dialog.download_button):
                self.assertTrue(button.instate(["disabled"]))
            release.set()
            self.pump(app, lambda: dialog.items["episodes"] == second_items)
            self.assertEqual(dialog.selection_status.get(), "0 / 1개 선택")
            self.assertTrue(dialog.queue_selected_button.instate(["disabled"]))
            self.assertTrue(dialog.download_button.instate(["disabled"]))
            self.assertFalse(dialog.queue_all_button.instate(["disabled"]))
        self.assertEqual(app.jobs, [])

    def test_catalog_category_filter_preserves_original_rows_images_and_visible_episode_selection(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        categories = ("예능", "영화", "드라마", "예능", "영화", "예능", "예능", "")
        contents = [{"title": f"로또 결과 {index + 1}", "url": f"https://tvmon1.com/ent/{1000 + index}",
                     "thumbnail": f"https://images.example/{index}.jpg" if index < 3 else "",
                     "meta": category, "category": category} for index, category in enumerate(categories)]
        episodes = [{"title": "로또 드라마 1화", "url": "https://tvmon1.com/ent/1002/123456",
                     "thumbnail": "", "meta": ""}]
        self.assertTrue(dialog.category_selector.instate(["disabled"]))
        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)) as search, \
                patch.object(tvmon_catalog, "list_episodes", return_value=episodes) as listing, \
                patch.object(downloader, "fetch", side_effect=lambda url, *args, **kwargs: (b"image", url)) as fetch, \
                patch.object(tvmon_images, "thumbnail_ppm", return_value=b"P6\n1 1\n255\n\x20\x60\xa0"):
            dialog.query.set("로또")
            dialog.search()
            self.pump(app, lambda: all(("contents", index) in dialog.images for index in range(3)))
            self.assertEqual(dialog.category.get(), "전체")
            self.assertEqual(dialog.category_selector.cget("values"), ("전체", "예능", "영화", "드라마"))
            self.assertTrue(dialog.category_selector.instate(["readonly", "!disabled"]))
            images = dict(dialog.images)
            row_images = {str(index): dialog.content_tree.item(str(index), "image") for index in range(3)}
            contents_generation = dialog.generation["contents"]
            dialog.category.set("드라마")
            dialog.category_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: dialog.content_tree.get_children() == ("2",))
            self.assertEqual(dialog.content_tree.item("2", "values"), ("로또 결과 3\n드라마",))
            self.assertEqual(dialog.result_status.get(), "‘로또’ · 드라마 1개 / 전체 8개")
            self.assertEqual(dialog.items["contents"], contents)
            self.assertEqual(dialog.generation["contents"], contents_generation)
            search.assert_called_once()
            dialog.content_tree.selection_set("2")
            self.pump(app, lambda: dialog.items["episodes"] == episodes)
            listing.assert_called_once_with(contents[2]["url"], dialog.cancel["episodes"])
            dialog.select_all()
            episode_cancel = dialog.cancel["episodes"]
            episode_generation = dialog.generation["episodes"]
            self.assertFalse(dialog.queue_selected_button.instate(["disabled"]))
            dialog.category.set("전체")
            dialog.category_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: dialog.content_tree.get_children() == tuple(str(index) for index in range(8)))
            self.assertEqual(dialog.result_status.get(), "‘로또’ · 전체 8개")
            self.assertEqual(dialog.active_content, contents[2]["url"])
            self.assertEqual(dialog.content_tree.selection(), ("2",))
            self.assertEqual(dialog.items["episodes"], episodes)
            self.assertEqual(dialog.episode_tree.selection(), ("0",))
            self.assertEqual(dialog.generation["episodes"], episode_generation)
            self.assertFalse(episode_cancel.is_set())
            self.assertFalse(dialog.queue_selected_button.instate(["disabled"]))
            self.assertEqual(dialog.images, images)
            for row, image in row_images.items():
                self.assertEqual(dialog.content_tree.item(row, "image"), image)
            self.assertEqual(fetch.call_count, 3)
            search.assert_called_once()
            listing.assert_called_once()
        self.assertEqual(app.jobs, [])

    def test_catalog_category_filter_cancels_hidden_content_and_ignores_late_episode_list(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        contents = [{"title": "로또 예능", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": "", "category": "예능"},
                    {"title": "로또 영화", "url": "https://tvmon1.com/movie/4047", "thumbnail": "", "meta": "", "category": "영화"}]
        hidden_episodes = [{"title": "예능 1화", "url": PAGE_URL, "thumbnail": "", "meta": ""}]
        visible_episodes = [{"title": "영화 1화", "url": "https://tvmon1.com/movie/4047/123456", "thumbnail": "", "meta": ""}]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.gates.append(release)

        def listing(url, cancel):
            if url == contents[0]["url"]:
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError("Category filter test did not release the hidden episode list")
                finished.set()
                return hidden_episodes
            return visible_episodes

        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)) as search, \
                patch.object(tvmon_catalog, "list_episodes", side_effect=listing) as episodes:
            dialog.query.set("로또")
            dialog.search()
            self.pump(app, lambda: dialog.items["contents"] == contents)
            dialog.content_tree.selection_set("0")
            self.pump(app, entered.is_set)
            old_cancel = dialog.cancel["episodes"]
            old_generation = dialog.generation["episodes"]
            dialog.category.set("영화")
            dialog.category_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: dialog.content_tree.get_children() == ("1",))
            self.assertTrue(old_cancel.is_set())
            self.assertIsNone(dialog.active_content)
            self.assertEqual(dialog.content_tree.selection(), ())
            self.assertEqual(dialog.items["episodes"], [])
            self.assertEqual(dialog.episode_tree.get_children(), ())
            self.assertEqual(dialog.episode_title.get(), "콘텐츠를 선택하세요")
            self.assertEqual(dialog.selection_status.get(), "0 / 0개 선택")
            for button in (dialog.queue_selected_button, dialog.queue_all_button, dialog.download_button):
                self.assertTrue(button.instate(["disabled"]))
            dialog.content_tree.selection_set("1")
            self.pump(app, lambda: dialog.items["episodes"] == visible_episodes)
            dialog.select_all()
            release.set()
            self.pump(app, finished.is_set)
            dialog.events.put(("episodes", old_generation, "list", hidden_episodes, None))
            self.pump(app, dialog.events.empty)
            self.assertEqual(dialog.active_content, contents[1]["url"])
            self.assertEqual(dialog.items["episodes"], visible_episodes)
            self.assertEqual(dialog.episode_tree.item("0", "values"), ("영화 1화",))
            self.assertEqual(dialog.episode_tree.selection(), ("0",))
            self.assertFalse(dialog.queue_selected_button.instate(["disabled"]))
            self.assertEqual([call.args[0] for call in episodes.call_args_list], [item["url"] for item in contents])
            search.assert_called_once()
        self.assertEqual(app.jobs, [])

    def test_catalog_new_search_removes_detached_rows_and_resets_category_choices(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        first = [{"title": f"이전 {category}", "url": f"https://tvmon1.com/ent/{1000 + index}",
                  "thumbnail": "", "meta": "", "category": category}
                 for index, category in enumerate(("예능", "영화", "드라마"))]
        second = [{"title": f"새 애니 {index + 1}", "url": f"https://tvmon1.com/animation/{2000 + index}",
                   "thumbnail": "", "meta": "", "category": "애니"} for index in range(2)]
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def search(query, cancel, page, *, origin=None):
            if query == "이전":
                return first, False
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Category reset test did not release the new search")
            return second, False

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("이전")
            dialog.search()
            self.pump(app, lambda: dialog.items["contents"] == first)
            dialog.category.set("드라마")
            dialog.category_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: dialog.content_tree.get_children() == ("2",))
            self.assertTrue(dialog.content_tree.exists("0"))
            self.assertTrue(dialog.content_tree.exists("1"))
            dialog.query.set("새 검색")
            dialog.search()
            self.pump(app, entered.is_set)
            self.assertEqual(dialog.category.get(), "전체")
            self.assertEqual(dialog.category_selector.cget("values"), ("전체",))
            self.assertTrue(dialog.category_selector.instate(["disabled"]))
            self.assertEqual(dialog.content_tree.get_children(), ())
            for row in ("0", "1", "2"):
                self.assertFalse(dialog.content_tree.exists(row))
            release.set()
            self.pump(app, lambda: dialog.items["contents"] == second and not dialog.loading)
            self.assertEqual(dialog.content_tree.get_children(), ("0", "1"))
            self.assertFalse(dialog.content_tree.exists("2"))
            self.assertEqual(dialog.content_tree.item("0", "values"), ("새 애니 1",))
            self.assertEqual(dialog.category.get(), "전체")
            self.assertEqual(dialog.category_selector.cget("values"), ("전체", "애니"))
            self.assertTrue(dialog.category_selector.instate(["readonly", "!disabled"]))
            self.assertEqual(dialog.result_status.get(), "‘새 검색’ · 전체 2개")
            self.assertEqual([call.args[0] for call in lookup.call_args_list], ["이전", "새 검색"])
            self.assertEqual(self.callback_errors, [])
        self.assertEqual(app.jobs, [])

    def test_catalog_close_cancels_pending_search_and_stops_polling(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entered = threading.Event()
        cancelled = threading.Event()

        def search(query, cancel, page, *, origin=None):
            if page == 1:
                return [{"title": "첫 페이지", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}], True
            entered.set()
            if not cancel.wait(timeout=8):
                raise AssertionError("Closing the search dialog did not cancel its request")
            cancelled.set()
            return [], False

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("작품")
            dialog.search()
            self.pump(app, entered.is_set)
            self.assertEqual([call.args[2] for call in lookup.call_args_list], [1, 2])
            self.assertEqual(dialog.items["contents"], [])
            poll_id = dialog._poll_id
            dialog.close()
            self.pump(app, cancelled.is_set)
            dialog.executor.shutdown(wait=True, cancel_futures=True)
            self.assertTrue(dialog.closed)
            self.assertTrue(all(cancel.is_set() for cancel in dialog.cancel.values()))
            self.assertFalse(dialog.window.winfo_exists())
            self.assertNotIn(poll_id, app.root.tk.call("after", "info"))
            self.assertTrue(dialog.events.empty())
            self.assertEqual(self.callback_errors, [])
            dialog.close()
        self.assertEqual(app.jobs, [])

    def test_native_search_entry_keeps_caret_after_insert_and_supports_middle_edits(self):
        import ctypes
        from ctypes import wintypes

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entry = dialog.query_entry
        entry.state(["!readonly"])

        def selection():
            start, end = wintypes.DWORD(), wintypes.DWORD()
            entry._user.SendMessageW(entry._hwnd, 0xB0, ctypes.addressof(start), ctypes.addressof(end))
            return start.value, end.value

        for index, char in enumerate("스캔들", 1):
            entry._user.SendMessageW(entry._hwnd, 0x102, ord(char), 0)
            self.pump(app, lambda: dialog.query.get() == "스캔들"[:index])
            self.assertEqual(entry.get(), "스캔들"[:index])
            self.assertEqual(dialog.query.get(), entry.get())
            self.assertEqual(selection(), (index, index))
        entry._user.SendMessageW(entry._hwnd, 0xB1, 1, 1)
        entry._user.SendMessageW(entry._hwnd, 0x102, ord("X"), 0)
        self.pump(app, lambda: dialog.query.get() == "스X캔들")
        self.assertEqual(dialog.query.get(), "스X캔들")
        self.assertEqual(selection(), (2, 2))
        entry._user.SendMessageW(entry._hwnd, 0x102, 8, 0)
        self.pump(app, lambda: dialog.query.get() == "스캔들")
        self.assertEqual(dialog.query.get(), "스캔들")
        self.assertEqual(selection(), (1, 1))
        dialog.query.set("삼국지")
        self.assertEqual(entry.get(), "삼국지")
        self.assertEqual(selection(), (3, 3))
        entry._user.SendMessageW(entry._hwnd, 0x102, ord("2"), 0)
        self.assertTrue(entry._native_changed)
        dialog.close()
        app.root.update()
        self.assertTrue(entry._closed)
        self.assertIsNone(entry._poll_after)
        self.assertEqual(dialog.query.trace_info(), [])

    def test_ime_block_caret_becomes_thin_after_text_without_changing_the_buffer(self):
        import ctypes
        from ctypes import wintypes as w

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entry = dialog.query_entry
        entry._user.DestroyCaret.argtypes = []
        entry._user.DestroyCaret.restype = w.BOOL

        def caret():
            info = entry._caret_info_type(size=ctypes.sizeof(entry._caret_info_type))
            self.assertTrue(entry._user.GetGUIThreadInfo(entry._thread_id, ctypes.byref(info)))
            self.assertEqual(info.caret, entry._hwnd)
            return info.rect

        try:
            for text in ('스', '스캔', '스캔들', '가😀'):
                with self.subTest(text=text):
                    units = len(text.encode('utf-16-le')) // 2
                    # Let EDIT itself provide the expected position after the text.
                    dialog.query.set(text + '|')
                    position = entry._user.SendMessageW(entry._hwnd, 0xD6, units, 0)
                    expected_x = ctypes.c_short(position & 0xFFFF).value
                    dialog.query.set(text)
                    self.assertTrue(entry._user.CreateCaret(entry._hwnd, None, 16, 20))
                    entry._user.SetCaretPos(expected_x - 16, 1)
                    # Only the focus check is replaced; caret geometry and text layout are real Win32 calls.
                    with patch.object(entry._user, 'GetFocus', return_value=entry._hwnd):
                        entry._update_ime_caret()
                        self.assertEqual((caret().left, caret().right - caret().left), (expected_x, 2))
                        entry._update_ime_caret()
                        self.assertEqual((caret().left, caret().right - caret().left), (expected_x, 2))
                    self.assertEqual(entry.get(), text)
                    self.assertEqual(dialog.query.get(), text)
                    selected = entry._user.SendMessageW(entry._hwnd, 0xB0, 0, 0)
                    self.assertEqual((selected & 0xFFFF, selected >> 16), (units, units))

            dialog.query.set('스캔들')
            entry._user.SendMessageW(entry._hwnd, 0xB1, 1, 1)
            expected_x = ctypes.c_short(entry._user.SendMessageW(entry._hwnd, 0xD6, 1, 0) & 0xFFFF).value
            self.assertTrue(entry._user.CreateCaret(entry._hwnd, None, 16, 20))
            entry._user.SetCaretPos(6, 1)
            with patch.object(entry._user, 'GetFocus', return_value=entry._hwnd):
                entry._update_ime_caret()
            self.assertEqual(caret().left, expected_x)
            self.assertEqual(entry.get(), '스캔들')
        finally:
            entry._user.DestroyCaret()

    def test_ime_caret_update_preserves_selection_and_other_focus(self):
        import ctypes
        from ctypes import wintypes as w

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entry = dialog.query_entry
        entry._user.DestroyCaret.argtypes = []
        entry._user.DestroyCaret.restype = w.BOOL
        dialog.query.set('스캔들')
        self.assertTrue(entry._user.CreateCaret(entry._hwnd, None, 16, 20))
        entry._user.SetCaretPos(6, 1)
        try:
            with patch.object(entry._user, 'GetFocus', return_value=None), \
                    patch.object(entry._user, 'CreateCaret') as recreate:
                entry._update_ime_caret()
                recreate.assert_not_called()
            entry._user.SendMessageW(entry._hwnd, 0xB1, 0, 2)
            with patch.object(entry._user, 'GetFocus', return_value=entry._hwnd), \
                    patch.object(entry._user, 'CreateCaret') as recreate:
                entry._update_ime_caret()
                recreate.assert_not_called()
            self.assertEqual(entry.get(), '스캔들')
            selected = entry._user.SendMessageW(entry._hwnd, 0xB0, 0, 0)
            self.assertEqual((selected & 0xFFFF, selected >> 16), (0, 2))
        finally:
            entry._user.DestroyCaret()

    def test_all_text_inputs_use_the_same_thin_ime_caret(self):
        import ctypes
        from ctypes import wintypes as w

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        fields = ((dialog.query_entry, dialog.query), (app.url_entry, app.url),
                  (app.folder_entry, app.folder), (app.domain_entry, app.domain_input))
        for entry, variable in fields:
            with self.subTest(widget=str(entry)):
                self.assertIsInstance(entry, downloader.WindowsSearchEntry)
                variable.set('스캔들|')
                expected_x = ctypes.c_short(entry._user.SendMessageW(entry._hwnd, 0xD6, 3, 0) & 0xFFFF).value
                variable.set('스캔들')
                self.assertTrue(entry._user.CreateCaret(entry._hwnd, None, 16, 20))
                try:
                    entry._user.SetCaretPos(expected_x - 16, 1)
                    with patch.object(entry._user, 'GetFocus', return_value=entry._hwnd):
                        entry._update_ime_caret()
                    info = entry._caret_info_type(size=ctypes.sizeof(entry._caret_info_type))
                    self.assertTrue(entry._user.GetGUIThreadInfo(entry._thread_id, ctypes.byref(info)))
                    self.assertEqual(info.caret, entry._hwnd)
                    self.assertEqual(info.rect.right - info.rect.left, 2)
                    self.assertEqual(entry.get(), '스캔들')
                finally:
                    entry._user.DestroyCaret.argtypes = []
                    entry._user.DestroyCaret.restype = w.BOOL
                    entry._user.DestroyCaret()

    def test_native_url_and_folder_inputs_preserve_submit_and_latest_text(self):
        app = self.create_app()
        folder = self.storage / '한글 저장 폴더'
        with patch.object(app, '_start_next'):
            app.folder_entry._user.SetWindowTextW(app.folder_entry._hwnd, str(folder))
            app.url_entry._user.SetWindowTextW(app.url_entry._hwnd, PAGE_URL)
            # Invoke before the native-to-Tk poll, as a fast click can do.
            app.add_button.invoke()
            self.assertEqual(app.jobs[0]['url'], PAGE_URL)
            self.assertEqual(app.jobs[0]['folder'], str(folder.resolve()))
            self.assertEqual(app.folder.get(), str(folder))
            self.assertEqual(app.url_entry.get(), '')
            app.url_entry._user.SetWindowTextW(app.url_entry._hwnd, SECOND_URL)
            app.url_entry._user.SendMessageW(app.url_entry._hwnd, 0x100, 13, 0)
            self.pump(app, lambda: len(app.jobs) == 2)
            self.assertEqual(app.jobs[1]['url'], SECOND_URL)
            app.folder_entry._user.SendMessageW(app.folder_entry._hwnd, 0x100, 13, 0)
            app.folder_entry._user.SendMessageW(app.folder_entry._hwnd, 0x102, 13, 0)
            app.root.update()
            self.assertEqual(len(app.jobs), 2)
            self.assertEqual(app.folder_entry.get(), str(folder))
            self.assertEqual(self.callback_errors, [])

    def test_native_domain_enter_and_url_context_paste_preserve_existing_actions(self):
        app = self.create_app()
        app.domain_entry._user.SetWindowTextW(app.domain_entry._hwnd, 'mirror.example')
        app.domain_entry._user.SendMessageW(app.domain_entry._hwnd, 0x100, 13, 0)
        self.pump(app, lambda: 'https://mirror.example' in app.domains)
        self.assertEqual(app.domain_entry.get(), '')
        self.assertIn('https://mirror.example', json.loads(self.state_path.read_text(encoding='utf-8'))['domains'])
        app.url.set(PAGE_URL)
        app.url_entry.icursor(0)
        with patch.object(app.root, 'clipboard_get', return_value=PAGE_URL):
            app.url_entry._user.SendMessageW(app.url_entry._hwnd, 0x7B, app.url_entry._hwnd, -1)
            self.pump(app, lambda: '영상 주소를 입력했습니다' in app.status.get())
        selected = app.url_entry._user.SendMessageW(app.url_entry._hwnd, 0xB0, 0, 0)
        self.assertEqual((selected & 0xFFFF, selected >> 16), (len(PAGE_URL), len(PAGE_URL)))
        self.assertEqual(app.jobs, [])
        with patch.object(app.root, 'clipboard_get', return_value='invalid clipboard text'):
            app.url_entry._user.SendMessageW(app.url_entry._hwnd, 0x7B, app.url_entry._hwnd, -1)
            self.pump(app, lambda: '유효한 TVMON' in app.status.get())
        self.assertEqual(app.url_entry.get(), PAGE_URL)

    def test_native_folder_value_is_used_by_browse_save_catalog_queue_and_open(self):
        app = self.create_app()
        folder = self.storage / '새 폴더'
        app.folder_entry._user.SetWindowTextW(app.folder_entry._hwnd, str(folder))
        with patch.object(downloader.filedialog, 'askdirectory', return_value='') as browse:
            app.browse()
            self.assertEqual(browse.call_args.kwargs['initialdir'], str(folder))
        saved_folder = self.storage / '저장한 폴더'
        app.folder_entry._user.SetWindowTextW(app.folder_entry._hwnd, str(saved_folder))
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['folder'], str(saved_folder))
        queue_folder = self.storage / '회차 폴더'
        app.folder_entry._user.SetWindowTextW(app.folder_entry._hwnd, str(queue_folder))
        app.add_episodes([{'title': '회차', 'url': PAGE_URL}])
        self.assertEqual(app.jobs[0]['folder'], str(queue_folder.resolve()))
        app.tree.selection_remove(*app.tree.selection())
        open_folder = self.storage / '열 폴더'
        app.folder_entry._user.SetWindowTextW(app.folder_entry._hwnd, str(open_folder))
        with patch.object(downloader.os, 'startfile') as opened:
            app.open_folder()
            opened.assert_called_once_with(str(open_folder.resolve()))

    def test_recent_searches_are_unique_bounded_and_restored_after_restart(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        self.assertTrue(dialog.recent_selector.instate(["disabled"]))
        with patch.object(tvmon_catalog, "search_catalog", return_value=([], False)):
            dialog.query.set("   ")
            dialog.search()
            self.assertEqual(app.recent_searches, [])
            for index in range(22):
                dialog.query.set(f" 작품 {index} ")
                dialog.search()
                self.pump(app, lambda: not dialog.loading)
            dialog.query.set("작품 7")
            dialog.search()
            self.pump(app, lambda: not dialog.loading)
        expected = ["작품 7"] + [f"작품 {index}" for index in range(21, 1, -1) if index != 7]
        self.assertEqual(app.recent_searches, expected)
        self.assertEqual(tuple(dialog.recent_selector['values']), tuple(expected))
        self.assertTrue(dialog.recent_selector.instate(["readonly", "!disabled"]))
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['recent_searches'], expected)
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        restored_dialog = self.create_search_dialog(restored)
        self.assertEqual(restored.recent_searches, expected)
        self.assertEqual(tuple(restored_dialog.recent_selector['values']), tuple(expected))

    def test_delete_selected_recent_search_persists_without_changing_results_or_jobs(self):
        app = self.create_app()
        app.recent_searches = ["스캔들", "삼국지", "다른 작품"]
        app.jobs.append(app._new_job(PAGE_URL, str(self.videos), "a" * 32))
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        dialog = self.create_search_dialog(app)
        dialog.query.set("입력 중인 검색어")
        dialog.content_tree.insert("", "end", iid="result", values=("기존 검색 결과",))
        dialog.recent_query.set("삼국지")
        with patch.object(dialog, "search") as search:
            dialog.delete_recent_button.invoke()
            search.assert_not_called()
        self.assertEqual(app.recent_searches, ["스캔들", "다른 작품"])
        self.assertEqual(tuple(dialog.recent_selector["values"]), ("스캔들", "다른 작품"))
        self.assertEqual(dialog.query_entry.get(), "입력 중인 검색어")
        self.assertEqual(dialog.content_tree.get_children(), ("result",))
        saved["recent_searches"] = ["스캔들", "다른 작품"]
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), saved)
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        self.assertEqual(restored.recent_searches, ["스캔들", "다른 작품"])
        self.assertEqual(restored.jobs[0]["id"], "a" * 32)

    def test_clear_recent_searches_persists_empty_state_and_new_search_enables_actions(self):
        app = self.create_app()
        app.recent_searches = ["스캔들", "삼국지"]
        dialog = self.create_search_dialog(app)
        with patch.object(dialog, "search") as search:
            dialog.clear_recent_button.invoke()
            search.assert_not_called()
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["recent_searches"], [])
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        dialog = self.create_search_dialog(restored)
        self.assertEqual(restored.recent_searches, [])
        self.assertEqual(dialog.recent_query.get(), "최근 검색어가 없습니다")
        for widget in (dialog.recent_selector, dialog.delete_recent_button, dialog.clear_recent_button):
            self.assertTrue(widget.instate(["disabled"]))
        with patch.object(restored, "_save_jobs") as save:
            dialog.delete_recent_button.invoke()
            dialog.clear_recent_button.invoke()
            save.assert_not_called()
        with patch.object(dialog, "_submit"):
            dialog.query.set("새 작품")
            dialog.search()
        self.assertEqual(restored.recent_searches, ["새 작품"])
        for widget in (dialog.recent_selector, dialog.delete_recent_button, dialog.clear_recent_button):
            self.assertTrue(widget.instate(["!disabled"]))
        dialog.delete_recent_button.invoke()
        self.assertEqual(restored.recent_searches, [])
        self.assertTrue(dialog.clear_recent_button.instate(["disabled"]))

    def test_recent_search_deletion_save_failure_preserves_history_and_file(self):
        app = self.create_app()
        app.recent_searches = ["스캔들", "삼국지"]
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        original = self.state_path.read_bytes()
        dialog = self.create_search_dialog(app)
        dialog.recent_query.set("삼국지")
        for button in (dialog.delete_recent_button, dialog.clear_recent_button):
            with self.subTest(button=button["text"]), patch.object(downloader.os, "replace", side_effect=OSError("disk full")):
                button.invoke()
            self.assertEqual(app.recent_searches, ["스캔들", "삼국지"])
            self.assertEqual(tuple(dialog.recent_selector["values"]), ("스캔들", "삼국지"))
            self.assertEqual(dialog.recent_query.get(), "삼국지")
            self.assertEqual(self.state_path.read_bytes(), original)
            self.assertIn("삭제하지 않았습니다", dialog.message.get())
        self.assertTrue(app._save_jobs(force=True))
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_recent_selection_searches_immediately_and_native_enter_uses_current_text(self):
        import tvmon_catalog

        app = self.create_app()
        app.recent_searches = ["스캔들", "삼국지"]
        dialog = self.create_search_dialog(app)
        with patch.object(tvmon_catalog, "search_catalog", return_value=([], False)) as search:
            dialog.query.set("다른 입력")
            dialog.recent_query.set("삼국지")
            dialog.recent_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: search.call_count == 1 and not dialog.loading)
            self.assertEqual(search.call_args.args[0], "삼국지")
            self.assertEqual(dialog.query_entry.get(), "삼국지")
            self.assertEqual(app.recent_searches, ["삼국지", "스캔들"])
            entry = dialog.query_entry
            entry.state(["!readonly"])
            entry._user.SendMessageW(entry._hwnd, 0x102, ord("2"), 0)
            entry._user.SendMessageW(entry._hwnd, 0x100, 13, 0)
            self.pump(app, lambda: search.call_count == 2 and not dialog.loading)
            self.assertEqual(search.call_args.args[0], "삼국지2")
            self.assertEqual(app.recent_searches, ["삼국지2", "삼국지", "스캔들"])

    def test_search_input_and_recent_selector_fit_minimum_window_size(self):
        app = self.create_app()
        dialog = self.create_search_dialog(app)
        app.root.geometry('960x850')
        app.root.update()
        for widget in (dialog.query_entry, dialog.find_button, dialog.recent_selector,
                       dialog.delete_recent_button, dialog.clear_recent_button,
                       dialog.content_tree, dialog.episode_tree, dialog.download_button):
            self.assertGreater(widget.winfo_width(), 1)
            self.assertGreater(widget.winfo_height(), 1)
            self.assertGreaterEqual(widget.winfo_rootx(), app.root.winfo_rootx())
            self.assertGreaterEqual(widget.winfo_rooty(), app.root.winfo_rooty())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 app.root.winfo_rootx() + app.root.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 app.root.winfo_rooty() + app.root.winfo_height())
        self.assertLessEqual(dialog.query_entry.winfo_rootx() + dialog.query_entry.winfo_width(),
                             dialog.find_button.winfo_rootx())
        self.assertLessEqual(dialog.recent_selector.winfo_rooty() + dialog.recent_selector.winfo_height(),
                             dialog.content_tree.winfo_rooty())
        self.assertLessEqual(dialog.recent_selector.winfo_rootx() + dialog.recent_selector.winfo_width(),
                             dialog.delete_recent_button.winfo_rootx())
        self.assertLessEqual(dialog.delete_recent_button.winfo_rootx() + dialog.delete_recent_button.winfo_width(),
                             dialog.clear_recent_button.winfo_rootx())

    def test_main_tabs_embed_one_search_panel_and_preserve_browsing_and_queue_state(self):
        import tvmon_catalog
        import tvmon_images

        app = self.create_app()
        self.assertEqual([app.main_tabs.tab(tab, "text") for tab in app.main_tabs.tabs()],
                         ["다운로드", "콘텐츠 검색", "도메인 설정"])
        self.assertEqual(app.main_tabs.select(), str(app.download_tab))
        self.assertIsNone(app.search_dialog)
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.main_tabs.select(app.search_tab)
        self.pump(app, lambda: app.search_dialog is not None)
        dialog = app.search_dialog
        dialog.query_entry.state(["readonly"])
        self.assertEqual(app.main_tabs.select(), str(app.search_tab))
        self.assertIs(dialog.window.master, app.search_tab)
        self.assertIs(dialog.window.winfo_toplevel(), app.root)
        self.assertEqual([widget for widget in app.root.winfo_children() if isinstance(widget, downloader.tk.Toplevel)], [app.detail_window])
        self.assertFalse(app.detail_window.winfo_ismapped())
        contents = [{"title": "로또 예능", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": "", "category": "예능"},
                    {"title": "로또 드라마", "url": "https://tvmon1.com/drama/2000",
                     "thumbnail": "https://images.example/content.jpg", "meta": "", "category": "드라마"}]
        episodes = [{"title": "로또 드라마 1화", "url": "https://tvmon1.com/drama/2000/123456",
                     "thumbnail": "https://images.example/episode.jpg", "meta": ""}]
        with patch.object(tvmon_catalog, "search_catalog", return_value=(contents, False)) as search, \
                patch.object(tvmon_catalog, "list_episodes", return_value=episodes) as listing, \
                patch.object(downloader, "fetch", side_effect=lambda url, *args, **kwargs: (b"image", url)) as fetch, \
                patch.object(tvmon_images, "thumbnail_ppm", return_value=b"P6\n1 1\n255\n\x20\x60\xa0"):
            dialog.query.set("로또")
            dialog.search()
            self.pump(app, lambda: ("contents", 1) in dialog.images)
            dialog.category.set("드라마")
            dialog.category_selector.event_generate("<<ComboboxSelected>>")
            self.pump(app, lambda: dialog.content_tree.get_children() == ("1",))
            dialog.content_tree.selection_set("1")
            self.pump(app, lambda: ("episodes", 0) in dialog.images)
            dialog.select_all()
            dialog.queue_selected_button.invoke()
            jobs = [(job["id"], job["url"], job["status"]) for job in app.jobs]
            images = dict(dialog.images)
            generation = dict(dialog.generation)
            app.main_tabs.select(app.download_tab)
            app.root.update()
            self.assertFalse(dialog.closed)
            app.main_tabs.select(app.search_tab)
            app.root.update()
            app.show_search()
            app.show_search()
            app.root.update()
            self.assertIs(app.search_dialog, dialog)
            self.assertEqual(app.search_tab.winfo_children(), [dialog.window])
            self.assertEqual(dialog.query.get(), "로또")
            self.assertEqual(dialog.category.get(), "드라마")
            self.assertEqual(dialog.result_status.get(), "‘로또’ · 드라마 1개 / 전체 2개")
            self.assertEqual(dialog.items, {"contents": contents, "episodes": episodes})
            self.assertEqual(dialog.content_tree.get_children(), ("1",))
            self.assertEqual(dialog.content_tree.selection(), ("1",))
            self.assertEqual(dialog.episode_tree.selection(), ("0",))
            self.assertEqual(dialog.images, images)
            self.assertEqual(dialog.generation, generation)
            self.assertFalse(any(cancel.is_set() for cancel in dialog.cancel.values()))
            self.assertEqual([(job["id"], job["url"], job["status"]) for job in app.jobs], jobs)
            self.assertEqual(jobs[0][2], "queued")
            self.assertFalse(app.queue_running)
            self.assertIsNone(app.active_id)
            search.assert_called_once()
            listing.assert_called_once()
            self.assertEqual(fetch.call_count, 2)

    def test_search_and_downloads_keep_running_when_main_tabs_change(self):
        import tvmon_catalog

        app = self.create_app()
        video_entered = threading.Event()
        release_video = threading.Event()
        search_entered = threading.Event()
        release_search = threading.Event()
        self.gates.extend((release_video, release_search))
        contents = [{"title": "검색 결과", "url": "https://tvmon1.com/ent/986", "thumbnail": "", "meta": ""}]

        def segment(episode, index, cancel):
            if episode == FIRST_EPISODE and index == 0:
                video_entered.set()
                if not release_video.wait(timeout=8):
                    raise AssertionError("Tab switching test did not release the active download")

        def search(query, cancel, page, *, origin=None):
            search_entered.set()
            if not release_search.wait(timeout=8):
                raise AssertionError("Tab switching test did not release the search")
            return contents, False

        self.segment_handler = segment
        first = self.add(app)
        self.pump(app, video_entered.is_set)
        dialog = self.create_search_dialog(app)
        with patch.object(tvmon_catalog, "search_catalog", side_effect=search) as lookup:
            dialog.query.set("작품")
            dialog.search()
            self.pump(app, search_entered.is_set)
            app.main_tabs.select(app.download_tab)
            app.root.update()
            self.assertTrue(app.queue_running)
            self.assertEqual(app.active_id, first["id"])
            self.assertFalse(app.cancel_event.is_set())
            self.assertFalse(dialog.cancel["contents"].is_set())
            second = self.add(app, SECOND_URL)
            app.main_tabs.select(app.search_tab)
            app.root.update()
            self.assertIs(app.search_dialog, dialog)
            release_video.set()
            self.pump(app, lambda: app.active_id is None and first["status"] == second["status"] == "completed")
            self.assertEqual(app.main_tabs.select(), str(app.search_tab))
            self.assertEqual(self.resolutions, [FIRST_EPISODE, SECOND_EPISODE])
            self.assertTrue(dialog.loading)
            app.main_tabs.select(app.download_tab)
            app.root.update()
            release_search.set()
            self.pump(app, lambda: dialog.items["contents"] == contents and not dialog.loading)
            self.assertEqual(app.main_tabs.select(), str(app.download_tab))
            app.show_search()
            app.root.update()
            self.assertEqual(dialog.content_tree.item("0", "values"), ("검색 결과",))
            self.assertEqual(dialog.query.get(), "작품")
            self.assertFalse(dialog.closed)
            lookup.assert_called_once()
        self.assertEqual(self.peak_fetches, 1)

    def test_closing_main_window_cancels_search_from_hidden_tab(self):
        import tvmon_catalog

        app = self.create_app()
        dialog = self.create_search_dialog(app)
        entered = threading.Event()
        cancelled = threading.Event()

        def search(query, cancel, page, *, origin=None):
            entered.set()
            if not cancel.wait(timeout=8):
                raise AssertionError("Closing the main window did not cancel its search")
            cancelled.set()
            return [], False

        with patch.object(tvmon_catalog, "search_catalog", side_effect=search):
            dialog.query.set("작품")
            dialog.search()
            self.pump(app, entered.is_set)
            app.main_tabs.select(app.download_tab)
            app.root.update()
            app.close()
            self.pump(app, cancelled.is_set)
            dialog.executor.shutdown(wait=True, cancel_futures=True)
            self.assertTrue(app._destroyed)
            self.assertTrue(dialog.closed)
            self.assertTrue(all(cancel.is_set() for cancel in dialog.cancel.values()))
            self.assertTrue(dialog.events.empty())
            self.assertIsNone(app._state_lock)
            self.assertEqual(self.callback_errors, [])

    def test_domain_add_reorder_remove_persist_before_publish_and_restore_with_jobs(self):
        app = self.create_app()
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        self.assertEqual(app.domains, tvmon_domains.DEFAULT_DOMAINS)
        save_jobs = app._save_jobs

        def persist_before_publish(*args, **kwargs):
            self.assertEqual(tvmon_domains.get_domains(), tvmon_domains.DEFAULT_DOMAINS)
            return save_jobs(*args, **kwargs)

        app.domain_input.set(" Mirror.Example/ ")
        with patch.object(app, "_save_jobs", side_effect=persist_before_publish) as save:
            app.domain_add_button.invoke()
            save.assert_called_once_with(force=True)
        self.assertEqual(app.domains, (*tvmon_domains.DEFAULT_DOMAINS, "https://mirror.example"))
        self.assertEqual(tvmon_domains.get_domains(), app.domains)
        self.assertEqual(app.domain_input.get(), "")
        self.assertEqual(app.domain_tree.selection(), ("2",))
        app.domain_up_button.invoke()
        app.domain_up_button.invoke()
        self.assertEqual(app.domains[0], "https://mirror.example")
        self.assertEqual(app.domain_tree.selection(), ("0",))
        self.assertTrue(app.domain_up_button.instate(["disabled"]))
        app.domain_down_button.invoke()
        app.domain_tree.selection_set("0")
        app.remove_domain()
        expected = ("https://mirror.example", "https://tvwiki52.net")
        self.assertEqual(app.domains, expected)
        self.assertEqual(tvmon_domains.get_domains(), expected)
        self.assertEqual([app.domain_tree.item(row, "values") for row in app.domain_tree.get_children()],
                         [("1", expected[0]), ("2", expected[1])])
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["domains"], list(expected))
        self.assertEqual(saved["jobs"][0]["url"], PAGE_URL)
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertEqual(restarted.domains, expected)
        self.assertEqual(tvmon_domains.get_domains(), expected)
        self.assertEqual(restarted.jobs[0]["title"], "기존 회차")
        self.assertEqual(restarted.jobs[0]["status"], "paused")
        self.assertEqual([restarted.domain_tree.item(row, "values")[1]
                          for row in restarted.domain_tree.get_children()], list(expected))
        self.assertEqual(self.resolutions, [])

    def test_legacy_queue_without_domain_settings_loads_defaults(self):
        self.state_path.parent.mkdir(parents=True)
        legacy = {"version": 1, "folder": str(self.videos), "jobs": [
            {"id": "a" * 32, "title": "기존 작업", "url": PAGE_URL, "folder": str(self.videos)}]}
        self.state_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        original = self.state_path.read_bytes()
        tvmon_domains.set_domains(("https://previous.example",))
        app = self.create_app()
        self.assertEqual(app.domains, tvmon_domains.DEFAULT_DOMAINS)
        self.assertEqual(tvmon_domains.get_domains(), tvmon_domains.DEFAULT_DOMAINS)
        self.assertTrue(app.auto_domain.get())
        self.assertEqual(app.jobs[0]["url"], PAGE_URL)
        self.assertEqual(app.jobs[0]["status"], "paused")
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertFalse(app.queue_running)
        self.assertEqual(self.resolutions, [])

    def test_invalid_duplicate_and_last_domain_changes_leave_saved_settings_unchanged(self):
        app = self.create_app()
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        original = self.state_path.read_bytes()
        for value in ("", "http://mirror.example", "https://mirror.example/content", "https://user:pass@mirror.example",
                      "tvmon1.com", "https://www.tvwiki52.net/"):
            with self.subTest(value=value), patch.object(app, "_save_jobs") as save, \
                    patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
                app.domain_input.set(value)
                app.add_domain()
                save.assert_not_called()
                publish.assert_not_called()
                self.assertEqual(app.domains, tvmon_domains.DEFAULT_DOMAINS)
                self.assertEqual(tvmon_domains.get_domains(), tvmon_domains.DEFAULT_DOMAINS)
                self.assertEqual(self.state_path.read_bytes(), original)
                self.assertEqual(len(app.domain_tree.get_children()), 2)
        app.domain_tree.selection_set("1")
        app.remove_domain()
        self.assertEqual(app.domains, ("https://tvmon1.com",))
        self.assertTrue(app.domain_remove_button.instate(["disabled"]))
        original = self.state_path.read_bytes()
        with patch.object(app, "_save_jobs") as save, \
                patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
            app.remove_domain()
            app.move_domain(-1)
            app.move_domain(1)
            save.assert_not_called()
            publish.assert_not_called()
        self.assertEqual(app.domains, ("https://tvmon1.com",))
        self.assertEqual(tvmon_domains.get_domains(), app.domains)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("하나 이상", app.domain_status.get())

    def test_domain_save_failure_keeps_previous_order_global_routing_and_saved_file(self):
        app = self.create_app()
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        original = self.state_path.read_bytes()
        expected = app.domains
        original_rows = [app.domain_tree.item(row, "values") for row in app.domain_tree.get_children()]
        with patch.object(app, "_save_jobs", return_value=False) as save, \
                patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
            app.domain_input.set("mirror.example")
            app.add_domain()
            self.assertEqual(app.domain_input.get(), "mirror.example")
            app.move_domain(1)
            app.remove_domain()
            self.assertEqual(save.call_count, 3)
            for call in save.call_args_list:
                self.assertEqual(call.kwargs, {"force": True})
            publish.assert_not_called()
        self.assertEqual(app.domains, expected)
        self.assertEqual(tvmon_domains.get_domains(), expected)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual([app.domain_tree.item(row, "values") for row in app.domain_tree.get_children()], original_rows)
        self.assertIn("이전 접속 순서를 유지", app.domain_status.get())
        self.assertEqual(self.resolutions, [])

    def test_download_worker_and_details_use_current_domain_without_changing_saved_identity(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app._apply_domains(["https://tvwiki53.net", "https://tvmon1.com"], 0)
        app.add_episodes([{"title": "53 검색 결과", "url": "https://tvwiki53.net/ent/986/406784622"}])
        job = app.jobs[0]
        self.assertEqual(job['url'], PAGE_URL)
        self.assertIn('우선 접속: https://tvwiki53.net/ent/986/406784622', app.video_meta.get())
        self.assertNotIn('tvmon1.com', app.video_meta.get())
        with patch.object(downloader, 'download_video', side_effect=downloader.Cancelled()) as run:
            app.start()
            self.pump(app, lambda: app.active_id is None and job['status'] == 'paused')
        self.assertEqual(run.call_args.args[0], 'https://tvwiki53.net/ent/986/406784622')
        self.assertEqual(run.call_args.kwargs['resume_id'], job['id'])
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['jobs'][0]['url'], PAGE_URL)
        self.assertEqual(app.add_episodes([{"title": "같은 영상", "url": PAGE_URL}]), (0, 1))

    def test_aliased_video_deduplicates_and_resumes_after_domain_reorder_and_removal(self):
        app = self.create_app()
        app.domain_input.set("mirror.example")
        app.add_domain()
        alias = PAGE_URL.replace("https://tvmon1.com", "https://mirror.example")
        entered = self.hold_after_first_cached_segment()
        job = self.add(app, alias)
        self.pump(app, lambda: entered.is_set() and job["done"] == 1)
        app.pause()
        self.pump(app, lambda: app.active_id is None and job["status"] == "paused")
        self.assertEqual(job["url"], PAGE_URL)
        app.domain_tree.selection_set("2")
        app.move_domain(-1)
        app.move_domain(-1)
        self.assertEqual(app.add_episodes([{"title": "같은 영상", "url": alias}]), (0, 1))
        app.domain_tree.selection_set("1")
        app.remove_domain()
        self.assertNotIn("https://tvmon1.com", app.domains)
        self.assertEqual(app.add_episodes([{"title": "기준 주소", "url": PAGE_URL}]), (0, 1))
        app.domain_tree.selection_set("0")
        app.remove_domain()
        self.assertEqual(app.domains, ("https://tvwiki52.net",))
        alternate = PAGE_URL.replace("https://tvmon1.com", "https://tvwiki52.net")
        self.assertEqual(app.add_episodes([{"title": "다른 도메인", "url": alternate}]), (0, 1))
        self.assertEqual(len(app.jobs), 1)
        self.assertEqual(job["completed_indices"], [0])
        app.close()
        self.cancel_timers(app)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["jobs"][0]["url"], PAGE_URL)
        restarted = self.create_app()
        self.assertEqual(restarted.domains, ("https://tvwiki52.net",))
        self.assertEqual(restarted.jobs[0]["id"], job["id"])
        self.assertEqual(restarted.jobs[0]["url"], PAGE_URL)
        self.segment_handler = None
        restarted.start()
        self.pump(restarted, lambda: restarted.active_id is None and restarted.jobs[0]["status"] == "completed")
        self.assertEqual(self.requests.count((FIRST_EPISODE, 0)), 1)
        self.assertEqual(Path(restarted.jobs[0]["file"]).read_bytes(), b"".join(packet(index) for index in range(3)))
        self.assertEqual(restarted.add_episodes([{"title": "완료된 같은 영상", "url": alternate}]), (0, 1))

    def test_domain_mutations_are_blocked_while_manager_is_closing(self):
        app = self.create_app()
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        original = self.state_path.read_bytes()
        expected = app.domains
        app.closing = True
        app._domain_actions()
        with patch.object(app, "_save_jobs") as save, \
                patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
            app.domain_input.set("mirror.example")
            app.add_domain()
            app.move_domain(1)
            app.remove_domain()
            save.assert_not_called()
            publish.assert_not_called()
        self.assertEqual(app.domains, expected)
        self.assertEqual(tvmon_domains.get_domains(), expected)
        self.assertEqual(self.state_path.read_bytes(), original)
        for button in (app.domain_add_button, app.domain_remove_button, app.domain_up_button, app.domain_down_button):
            self.assertTrue(button.instate(["disabled"]))
        self.assertEqual(self.resolutions, [])

    def test_automatic_domain_check_runs_in_background_and_persists_new_origin_first(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        initial = app.domains

        def resolve(fetch, cancel, *, check_cancel, on_failure=None, guide_url=tvmon_domains.DOMAIN_GUIDE_URL):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Automatic domain test did not release the latest address")
            check_cancel(cancel)
            return "https://current.example"

        with patch.object(downloader, "resolve_current_domain", side_effect=resolve) as resolver:
            self.assertTrue(app.auto_domain.get())
            self.assertIsNone(app.domain_check_worker)
            self.pump(app, entered.is_set)
            self.assertIsNone(app._domain_check_timer)
            self.assertTrue(app.domain_check_worker.is_alive())
            self.assertTrue(app.domain_refresh_button.instate(["disabled"]))
            self.assertEqual(app.domains, initial)
            self.assertEqual(tvmon_domains.get_domains(), initial)
            app.refresh_current_domain()
            resolver.assert_called_once()
            self.assertTrue(callable(resolver.call_args.args[0]))
            self.assertIs(resolver.call_args.args[1], app.domain_check_cancel)
            release.set()
            self.pump(app, lambda: app.domain_check_worker is None and app.domains[0] == "https://current.example")
            expected = ("https://current.example", *initial)
            self.assertEqual(app.domains, expected)
            self.assertEqual(tvmon_domains.get_domains(), expected)
            self.assertFalse(app.domain_refresh_button.instate(["disabled"]))
            self.assertEqual(app.domain_tree.item("0", "values"), ("1", "https://current.example"))
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["domains"], list(expected))
            self.assertTrue(saved["auto_domain"])
            self.assertIn("접속 확인 완료", app.domain_status.get())
            self.assertFalse(app.queue_running)
            self.assertEqual(app.jobs, [])
        self.domain_resolver.assert_not_called()

    def test_auto_domain_preference_persists_and_disabled_startup_does_not_check(self):
        app = self.create_app()
        self.assertTrue(app.auto_domain.get())
        app.domain_auto_check.invoke()
        self.assertFalse(app.auto_domain.get())
        self.assertFalse(json.loads(self.state_path.read_text(encoding="utf-8"))["auto_domain"])
        pending_timer = app._domain_check_timer
        self.assertIsNotNone(pending_timer)
        app.root.update_idletasks()
        app.close()
        self.assertIsNone(app._domain_check_timer)
        self.assertNotIn(pending_timer, app.root.tk.call("after", "info"))
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertFalse(restarted.auto_domain.get())
        elapsed = threading.Event()
        restarted.root.after(350, elapsed.set)
        self.pump(restarted, elapsed.is_set)
        self.domain_resolver.assert_not_called()
        self.assertIsNone(restarted.domain_check_worker)
        original = self.state_path.read_bytes()
        with patch.object(restarted, "_save_jobs", return_value=False) as save:
            restarted.domain_auto_check.invoke()
            save.assert_called_once_with(force=True)
        self.assertFalse(restarted.auto_domain.get())
        self.assertEqual(self.state_path.read_bytes(), original)
        restarted.domain_auto_check.invoke()
        self.assertTrue(restarted.auto_domain.get())
        self.assertTrue(json.loads(self.state_path.read_text(encoding="utf-8"))["auto_domain"])

    def test_manual_domain_check_promotes_existing_www_alias_without_duplicate(self):
        app = self.create_app()
        app.auto_domain.set(False)
        with patch.object(downloader, "resolve_current_domain", return_value="https://www.tvwiki52.net") as resolver:
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
            resolver.assert_called_once()
            self.assertTrue(callable(resolver.call_args.args[0]))
            self.assertIs(resolver.call_args.args[1], app.domain_check_cancel)
        expected = ("https://www.tvwiki52.net", "https://tvmon1.com")
        self.assertEqual(app.domains, expected)
        self.assertEqual(tvmon_domains.get_domains(), expected)
        self.assertEqual(len({urlsplit(domain).hostname.removeprefix("www.") for domain in app.domains}), 2)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["domains"], list(expected))
        self.assertFalse(saved["auto_domain"])

    def test_registered_domain_success_does_not_lookup_guide_and_is_saved_first(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app._apply_domains(["https://tvwiki52.net", "https://tvwiki53.net"], 0)
        with patch.object(downloader, "verify_domain", side_effect=[downloader.DownloadError("52 failed"),
                                                                  "https://tvwiki53.net"]) as verify:
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
        self.assertEqual([call.args[0] for call in verify.call_args_list],
                         ["https://tvwiki52.net", "https://tvwiki53.net"])
        self.domain_resolver.assert_not_called()
        self.assertEqual(app.domains, ("https://tvwiki53.net", "https://tvwiki52.net"))
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["domains"], list(app.domains))
        self.assertIn("접속 확인 완료: https://tvwiki53.net", app.domain_status.get())
        self.assertIn("등록된 도메인", app.domain_status.get())

    def test_blank_root_and_working_index_show_separate_results_and_save_only_origin(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app._apply_domains(["https://tvwiki53.net"], 0)
        replies = [(b'', 'https://tvwiki53.net/'),
                   (b'<title>TVWIKI</title><a href="/drama/123">Drama</a>', 'https://tvwiki53.net/index.php')]
        with patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "_fetch_single", side_effect=replies) as request:
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['https://tvwiki53.net/', 'https://tvwiki53.net/index.php'])
        rows = [app.domain_check_tree.item(row, 'values') for row in app.domain_check_tree.get_children()]
        self.assertEqual([(row[1], row[2]) for row in rows],
                         [('https://tvwiki53.net/', '실패'), ('https://tvwiki53.net/index.php', '성공')])
        self.assertIn('/index.php', rows[0][3])
        self.assertEqual(rows[1][3], '콘텐츠 홈페이지 확인')
        self.assertEqual(app.domains, ('https://tvwiki53.net',))
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['domains'], ['https://tvwiki53.net'])
        self.assertIn('접속 확인 완료', app.domain_status.get())
        self.assertFalse(app.domain_refresh_button.instate(['disabled']))
        self.domain_resolver.assert_not_called()

    def test_domain_progress_is_visible_while_busy_and_new_check_clears_history(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app._apply_domains(["https://old.example", "https://tvwiki53.net"], 0)
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)

        def fetch(url, *args, **kwargs):
            if url == "https://old.example/":
                raise downloader.DownloadError("연결 실패 (Windows 오류 12030)")
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Progress test did not release the response")
            return b'<title>TVWIKI</title><a href="/drama/123">Drama</a>', url

        with patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "_fetch_single", side_effect=fetch) as request:
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: entered.is_set() and app.domain_check_tree.exists("2"))
            self.assertTrue(app.domain_check_worker.is_alive())
            self.assertTrue(app.domain_refresh_button.instate(["disabled"]))
            self.assertEqual(app.domain_check_tree.item("1", "values")[2], "실패")
            self.assertIn("12030", app.domain_check_tree.item("1", "values")[3])
            self.assertEqual(app.domain_check_tree.item("2", "values")[2], "확인 중")
            self.assertIn("현재 확인 중: https://tvwiki53.net/", app.domain_status.get())
            app.refresh_current_domain()
            self.assertEqual(request.call_count, 2)
            release.set()
            self.pump(app, lambda: app.domain_check_worker is None)
            self.assertEqual(app.domain_check_tree.item("2", "values")[2:], ("성공", "콘텐츠 홈페이지 확인"))
            self.assertEqual(app.domains[0], "https://tvwiki53.net")
            self.assertFalse(app.domain_refresh_button.instate(["disabled"]))
            release.clear()
            entered.clear()
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: entered.is_set() and app.domain_check_tree.exists("1"))
            self.assertEqual(app.domain_check_tree.get_children(), ("1",))
            self.assertEqual(app.domain_check_tree.item("1", "values")[1:3], ("https://tvwiki53.net/", "확인 중"))
            release.set()
            self.pump(app, lambda: app.domain_check_worker is None)
            self.assertEqual(app.domain_check_tree.item("1", "values")[2], "성공")
        self.assertEqual(self.resolutions, [])

    def test_numbered_success_is_added_once_and_save_failure_preserves_existing_settings(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app._apply_domains(["https://tvwiki52.net"], 0)
        original = self.state_path.read_bytes()
        result = ("https://tvwiki53.net", None, "안내 주소 +1")
        with patch.object(downloader, "_find_available_domain", return_value=result), \
                patch.object(app, "_save_jobs", return_value=False):
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
        self.assertEqual(app.domains, ("https://tvwiki52.net",))
        self.assertEqual(tvmon_domains.get_domains(), app.domains)
        self.assertEqual(self.state_path.read_bytes(), original)
        with patch.object(downloader, "_find_available_domain", return_value=result):
            for _ in range(2):
                app.domain_refresh_button.invoke()
                self.pump(app, lambda: app.domain_check_worker is None)
        self.assertEqual(app.domains, ("https://tvwiki53.net", "https://tvwiki52.net"))
        self.assertIn("안내 주소 +1", app.domain_status.get())

    def test_failed_domain_check_retains_existing_settings_and_reports_reason(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        initial = app.domains
        original = self.state_path.read_bytes()
        with patch.object(downloader, "resolve_current_domain",
                          side_effect=downloader.DownloadError("안내 페이지에서 최신 주소를 찾지 못했습니다.")):
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
        self.assertEqual(app.domains, initial)
        self.assertEqual(tvmon_domains.get_domains(), initial)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("안내 페이지에서 최신 주소를 찾지 못했습니다.", app.domain_status.get())
        self.assertIn("기존 도메인 목록을 유지", app.domain_status.get())
        self.assertFalse(app.domain_refresh_button.instate(["disabled"]))
        self.assertEqual(self.resolutions, [])

    def test_current_domain_is_not_published_if_saving_resolved_address_fails(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        initial = app.domains
        original = self.state_path.read_bytes()
        with patch.object(downloader, "resolve_current_domain", return_value="https://current.example"), \
                patch.object(app, "_save_jobs", return_value=False) as save, \
                patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
            app.domain_refresh_button.invoke()
            self.pump(app, lambda: app.domain_check_worker is None)
            self.assertTrue(any(call.kwargs.get("force") is True for call in save.call_args_list))
            publish.assert_not_called()
        self.assertEqual(app.domains, initial)
        self.assertEqual(tvmon_domains.get_domains(), initial)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertNotIn("https://current.example", [app.domain_tree.item(row, "values")[1]
                                                    for row in app.domain_tree.get_children()])
        self.assertIn("이전 접속 순서를 유지", app.domain_status.get())

    def test_closing_manager_cancels_domain_check_and_ignores_late_success(self):
        app = self.create_app()
        app.auto_domain.set(False)
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        initial = app.domains

        def resolve(fetch, cancel, *, check_cancel, on_failure=None, guide_url=tvmon_domains.DOMAIN_GUIDE_URL):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Closing test did not release the domain response")
            return "https://late.example"

        with patch.object(downloader, "resolve_current_domain", side_effect=resolve):
            app.refresh_current_domain()
            self.pump(app, entered.is_set)
            worker = app.domain_check_worker
            app.close()
            self.assertTrue(app._destroyed)
            self.assertIsNone(app._domain_check_timer)
            self.assertTrue(app.domain_check_cancel.is_set())
            original = self.state_path.read_bytes()
            release.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            with patch.object(app, "_save_jobs") as save, \
                    patch.object(downloader, "set_domains", wraps=tvmon_domains.set_domains) as publish:
                app._poll_domain_check()
                app.refresh_current_domain()
                save.assert_not_called()
                publish.assert_not_called()
            self.assertEqual(app.domains, initial)
            self.assertEqual(tvmon_domains.get_domains(), initial)
            self.assertEqual(self.state_path.read_bytes(), original)
            self.assertEqual(self.callback_errors, [])

    def test_video_guide_setting_persists_and_startup_uses_configured_page(self):
        app = self.create_app()
        app.auto_text_domain.set(False)
        original_domains = app.domains
        self.assertEqual(app.domain_guide_url, tvmon_domains.DOMAIN_GUIDE_URL)
        guide_url = "https://guide.example/current?site=tvwiki"
        entry = app.domain_guide_entry
        entry._user.SetWindowTextW(entry._hwnd, guide_url.removeprefix("https://"))
        entry._user.SendMessageW(entry._hwnd, 0x100, 13, 0)
        entry._poll_native()
        self.assertEqual(app.domain_guide_url, guide_url)
        self.assertEqual(app.domain_guide_input.get(), guide_url)
        self.assertEqual(app.domains, original_domains)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["domain_guide_url"], guide_url)
        self.assertEqual(saved["text_domain_guide_url"], tvmon_domains.TEXT_DOMAIN_GUIDE_URL)
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        self.assertEqual(restored.domain_guide_input.get(), guide_url)
        guide = '<dt>현재 안내 주소</dt><dd>tvwiki54.net</dd><a href="https://tvwiki54.net/">현재 주소</a>'.encode()
        home = b'<title>TVWIKI</title><a href="/drama/123">Drama</a>'
        with patch.object(downloader, "resolve_current_domain", tvmon_domains.resolve_current_domain), \
                patch.object(downloader, "_fetch_single", side_effect=[(guide, guide_url), (home, "https://tvwiki54.net/")]) as fetcher:
            self.pump(restored, lambda: restored.domains[0] == "https://tvwiki54.net")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list], [guide_url, "https://tvwiki54.net/"])
        self.assertEqual(restored.text_domain_guide_url, tvmon_domains.TEXT_DOMAIN_GUIDE_URL)
        self.assertEqual(self.callback_errors, [])

    def test_invalid_video_guide_and_failed_save_keep_committed_settings(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        original = self.state_path.read_bytes()
        for value in ("", "http://guide.example/", "https://user:password@guide.example/", "https://guide.example:8443/"):
            with self.subTest(value=value):
                app.domain_guide_input.set(value)
                app.domain_guide_save_button.invoke()
                self.assertEqual(app.domain_guide_url, tvmon_domains.DOMAIN_GUIDE_URL)
                self.assertEqual(self.state_path.read_bytes(), original)
                self.assertIn("HTTPS", app.domain_guide_message.get())
        app.domain_guide_input.set("https://guide.example/current")
        with patch.object(app, "_save_jobs", return_value=False):
            app.domain_guide_save_button.invoke()
        self.assertEqual(app.domain_guide_url, tvmon_domains.DOMAIN_GUIDE_URL)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("기존 안내 URL을 유지", app.domain_guide_message.get())

    def test_video_guide_check_uses_saved_url_while_editing(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        guide_url = "https://guide.example/current?site=tvwiki"
        app.domain_guide_input.set(guide_url)
        app.domain_guide_save_button.invoke()
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        guide = '<dt>현재 안내 주소</dt><dd>tvwiki54.net</dd><a href="https://tvwiki54.net/">현재 주소</a>'.encode()
        home = b'<title>TVWIKI</title><a href="/drama/123">Drama</a>'
        def fetch(url, *args, **kwargs):
            if url == guide_url:
                entered.set()
                if not release.wait(8):
                    raise AssertionError("Video guide response was not released")
                return guide, url
            return home, url
        with patch.object(downloader, "resolve_current_domain", tvmon_domains.resolve_current_domain), \
                patch.object(downloader, "_fetch_single", side_effect=fetch) as request:
            app.refresh_current_domain()
            self.pump(app, entered.is_set)
            app._poll_domain_check()
            self.assertTrue(app.domain_guide_save_button.instate(["disabled"]))
            self.assertEqual(app.site_steps["A"][0].get(), "확인 중")
            self.assertIn("최신 주소 찾는 중", app.site_messages["A"].get())
            app.domain_guide_input.set("https://unsaved.example/")
            app._save_domain_guide()
            self.assertEqual(app.domain_guide_url, guide_url)
            release.set()
            self.pump(app, lambda: app.domain_check_worker is None)
            self.assertFalse(app.domain_guide_save_button.instate(["disabled"]))
            self.assertEqual(app.domains[0], "https://tvwiki54.net")
            self.assertEqual([call.args[0] for call in request.call_args_list], [guide_url, "https://tvwiki54.net/"])
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["domain_guide_url"], guide_url)

    def test_text_guide_setting_persists_and_startup_uses_configured_page(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.text_domain = "https://toki33.com"
        app.text_domain_verification_url = "https://toki33.com/"
        original_domains = app.domains
        self.assertEqual(app.text_domain_guide_url, tvmon_domains.TEXT_DOMAIN_GUIDE_URL)
        guide_url = "https://guide.example/current?site=newtoki"
        app.text_domain_guide_input.set(guide_url.removeprefix("https://"))
        app.text_domain_guide_save_button.invoke()
        self.assertEqual(app.text_domain_guide_url, guide_url)
        self.assertEqual(app.text_domain_guide_input.get(), guide_url)
        self.assertEqual(app.text_domain, "https://toki33.com")
        self.assertEqual(app.text_domain_verification_url, "")
        self.assertTrue(app.text_domain_browser_button.instate(["disabled"]))
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["text_domain_guide_url"], guide_url)
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        self.assertEqual(restored.text_domain_guide_input.get(), guide_url)
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
        with patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=[(guide, guide_url), (b'<title>NEWTOKI</title>', "https://toki34.com/")]) as fetcher:
            self.pump(restored, lambda: restored._text_domain_result == "success")
        self.assertEqual([call.args[0] for call in fetcher.call_args_list], [guide_url, "https://toki34.com/"])
        self.assertEqual(restored.text_domain, "https://toki34.com")
        self.assertEqual(restored.domains, original_domains)

    def test_invalid_text_guide_and_failed_save_keep_committed_settings(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        original = self.state_path.read_bytes()
        for value in ("", "http://guide.example/", "https://user:password@guide.example/", "https://guide.example:8443/"):
            with self.subTest(value=value):
                app.text_domain_guide_input.set(value)
                app.text_domain_guide_save_button.invoke()
                self.assertEqual(app.text_domain_guide_url, tvmon_domains.TEXT_DOMAIN_GUIDE_URL)
                self.assertEqual(self.state_path.read_bytes(), original)
                self.assertIn("HTTPS", app.text_domain_guide_message.get())
        app.text_domain_guide_input.set("https://guide.example/current")
        with patch.object(app, "_save_jobs", return_value=False):
            app.text_domain_guide_save_button.invoke()
        self.assertEqual(app.text_domain_guide_url, tvmon_domains.TEXT_DOMAIN_GUIDE_URL)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("기존 안내 URL을 유지", app.text_domain_guide_message.get())

    def test_text_guide_check_uses_saved_url_while_editing_and_during_manual_verification(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        guide_url = "https://guide.example/current?site=newtoki"
        app.text_domain_guide_input.set(guide_url)
        app.text_domain_guide_save_button.invoke()
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
        def fetch(url, *args, **kwargs):
            if url == guide_url:
                entered.set()
                if not release.wait(8):
                    raise AssertionError("Guide response was not released")
                return guide, url
            raise downloader.BrowserVerificationRequired(url)
        browser_replies = [downloader.BrowserChallengeError("https://toki34.com/"), (b'<title>NEWTOKI</title>', "https://toki34.com/")]
        with patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=fetch) as request, \
                patch.object(app, "_fetch_text_in_browser", side_effect=browser_replies) as browser:
            app.refresh_text_domain()
            self.pump(app, entered.is_set)
            app._poll_text_domain_check()
            self.assertTrue(app.text_domain_guide_save_button.instate(["disabled"]))
            self.assertEqual(app.site_steps["B"][0].get(), "확인 중")
            self.assertIn("최신 주소 찾는 중", app.site_messages["B"].get())
            app.text_domain_guide_input.set("https://unsaved.example/")
            app._save_text_domain_guide()
            self.assertEqual(app.text_domain_guide_url, guide_url)
            release.set()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app._text_domain_result, "verification")
            self.assertFalse(app.text_domain_guide_save_button.instate(["disabled"]))
            app.text_domain_browser_button.invoke()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app._text_domain_result, "success")
            self.assertEqual([call.kwargs["visible"] for call in browser.call_args_list], [False, True])
            self.assertEqual([call.args[0] for call in request.call_args_list], [guide_url, "https://toki34.com/", "https://toki34.com/"])
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["text_domain_guide_url"], guide_url)

    def test_text_domain_auto_check_runs_independently_and_persists_separately_from_video(self):
        app = self.create_app()
        app.recent_searches = ["기존 검색"]
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        original = json.loads(self.state_path.read_text(encoding="utf-8"))
        initial = app.domains
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        def video_resolve(fetch, cancel, **kwargs):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Video check was not released")
            raise downloader.DownloadError("A 테스트 실패")
        with patch.object(downloader, "resolve_current_domain", side_effect=video_resolve), \
                patch.object(downloader, "resolve_current_text_domain", return_value="https://toki35.com") as resolve:
            self.pump(app, lambda: entered.is_set() and app.text_domain == "https://toki35.com")
            self.assertTrue(app.domain_check_worker.is_alive())
            self.assertIsNone(app.text_domain_check_worker)
            resolve.assert_called_once()
            self.assertIs(resolve.call_args.args[1], app.text_domain_check_cancel)
            self.assertEqual(app.domains, initial)
            self.assertEqual(tvmon_domains.get_domains(), initial)
            original["text_domain"] = "https://toki35.com"
            self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), original)
            self.assertFalse(tvmon_domains.is_site_url("https://toki35.com/webtoon/123"))
            release.set()
            self.pump(app, lambda: app.domain_check_worker is None)
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        self.assertEqual(restored.text_domain, "https://toki35.com")
        self.assertEqual(restored.text_domain_value.get(), "https://toki35.com")
        self.assertEqual(restored.domains, initial)
        self.assertEqual(restored.recent_searches, ["기존 검색"])
        self.assertEqual(restored.jobs[0]["url"], PAGE_URL)

    def test_text_domain_failures_are_visible_and_preserve_saved_address(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki33.com"
        app.text_domain_value.set(app.text_domain)
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        original = self.state_path.read_bytes()
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
        replies = [(guide, tvmon_domains.TEXT_DOMAIN_GUIDE_URL)] + [downloader.DownloadError("HTTP 403")] * 4
        with patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "_fetch_single", side_effect=replies) as fetcher:
            app.text_domain_refresh_button.invoke()
            self.assertTrue(app.text_domain_refresh_button.instate(["disabled"]))
            app.refresh_text_domain()
            self.pump(app, lambda: app.text_domain_check_worker is None)
        self.assertEqual(fetcher.call_count, 5)
        rows = [app.text_domain_check_tree.item(row, "values") for row in app.text_domain_check_tree.get_children()]
        self.assertEqual(len(rows), 5)
        self.assertEqual([row[2] for row in rows], ["성공", "실패", "실패", "실패", "실패"])
        self.assertTrue(all("HTTP 403" in row[3] for row in rows[1:]))
        self.assertEqual(app.text_domain, "https://toki33.com")
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("B 접속 확인 실패", app.text_domain_status.get())
        with patch.object(downloader, "resolve_current_text_domain", return_value="https://toki35.com"), \
                patch.object(app, "_save_jobs", return_value=False):
            app.text_domain_refresh_button.invoke()
            self.pump(app, lambda: app.text_domain_check_worker is None)
        self.assertEqual(app.text_domain, "https://toki33.com")
        self.assertEqual(app.text_domain_value.get(), "https://toki33.com")
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertIn("저장하지 못했습니다", app.text_domain_status.get())
        self.assertFalse(app.text_domain_refresh_button.instate(["disabled"]))

    def test_second_announced_text_domain_is_saved_after_chrome_start_failure(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.text_domain = "https://toki33.com"
        app.text_domain_value.set(app.text_domain)
        guide = (b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
                 b'<span class="hud-url">toki35.com</span><a href="https://toki35.com/">Mirror</a>')
        replies = [(guide, tvmon_domains.TEXT_DOMAIN_GUIDE_URL), downloader._ConnectionDownloadError("Windows 12030"),
                   (b'<title>NEWTOKI</title>', "https://toki35.com/")]
        with patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=replies) as request, \
                patch.object(app, "_fetch_text_in_browser", side_effect=downloader.TransferError("Chrome start failed")):
            app.refresh_text_domain()
            self.pump(app, lambda: app.text_domain_check_worker is None)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(app.text_domain, "https://toki35.com")
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["text_domain"], "https://toki35.com")
        rows = [app.text_domain_check_tree.item(row, "values") for row in app.text_domain_check_tree.get_children()]
        self.assertEqual([row[2] for row in rows], ["성공", "실패", "성공"])
        self.assertIn("Chrome start failed", rows[1][3])
        self.assertTrue(app.text_domain_browser_button.instate(["disabled"]))

    def test_redirected_toki35_verification_opens_final_host_and_saves_it_after_success(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        guide = (b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
                 b'<span class="hud-url">newtoki1.org</span><a href="https://newtoki1.org/">Other</a>')
        replies = [(guide, tvmon_domains.TEXT_DOMAIN_GUIDE_URL), downloader._ConnectionDownloadError("Windows 12030"),
                   downloader._ConnectionDownloadError("Windows 12030")]
        browser_replies = [downloader.BrowserChallengeError("https://toki35.com/"), (b'<title>NEWTOKI</title>', "https://toki35.com/")]
        with patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=replies) as request, \
                patch.object(app, "_fetch_text_in_browser", side_effect=browser_replies) as browser:
            app.refresh_text_domain()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app._text_domain_result, "verification")
            self.assertEqual(app.text_domain_verification_url, "https://toki35.com/")
            self.assertIn("https://toki35.com/", app.text_domain_check_tree.item("2", "values")[3])
            self.assertTrue(app.text_domain_browser_button.instate(["!disabled"]))
            app.text_domain_browser_button.invoke()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app._text_domain_result, "success")
            self.assertEqual(app.text_domain, "https://toki35.com")
            self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["text_domain"], "https://toki35.com")
        self.assertEqual([item.args[0] for item in request.call_args_list],
                         [tvmon_domains.TEXT_DOMAIN_GUIDE_URL, "https://toki34.com/", "https://toki35.com/"])
        self.assertEqual([item.args[0] for item in browser.call_args_list], ["https://toki34.com/", "https://toki35.com/"])
        self.assertEqual([item.kwargs["visible"] for item in browser.call_args_list], [False, True])

    def test_unsupported_saved_text_host_is_cleared_without_losing_jobs_or_recent_searches(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        app.recent_searches = ["보관할 검색"]
        app.text_domain = "https://newtoki1.org"
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        restored.auto_domain.set(False)
        restored.auto_text_domain.set(False)
        self.assertEqual(restored.text_domain, "")
        self.assertEqual(restored.jobs[0]["url"], PAGE_URL)
        self.assertEqual(restored.recent_searches, ["보관할 검색"])

    def test_unsupported_text_host_is_rejected_before_browser_access(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        with patch.object(downloader, "TextSiteBrowser") as constructor, self.assertRaisesRegex(downloader.TransferError, "toki숫자.com"):
            app._fetch_text_in_browser("https://newtoki1.org/search?q=test", threading.Event(), limit=1024)
        constructor.assert_not_called()

    def test_text_browser_verification_stops_numbers_and_manual_completion_saves_same_domain(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.recent_searches = ["보관할 검색"]
        app.add_episodes([{"title": "기존 회차", "url": PAGE_URL}])
        original = json.loads(self.state_path.read_text(encoding="utf-8"))
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
        home = b'<html><title>NEWTOKI</title><body>Contents</body></html>'
        browser = Mock(closed=False)
        browser.get.side_effect = [downloader.BrowserChallengeError("https://toki34.com/"), (home, "https://toki34.com/")]
        def fetch(url, *args, **kwargs):
            if url == tvmon_domains.TEXT_DOMAIN_GUIDE_URL:
                return guide, url
            self.assertEqual(url, "https://toki34.com/")
            raise downloader.BrowserVerificationRequired(url)
        with patch.object(downloader, "TextSiteBrowser", return_value=browser) as constructor, \
                patch.object(downloader, "verify_domain", tvmon_domains.verify_domain), \
                patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=fetch) as request:
            app.text_domain_refresh_button.invoke()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app.text_domain_check_tree.item("2", "values")[2], "검증 필요")
            self.assertIn("번호 탐색은 중단", app.text_domain_status.get())
            self.assertTrue(app.text_domain_browser_button.instate(["!disabled"]))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), original)
            app.text_domain_browser_button.invoke()
            self.pump(app, lambda: app.text_domain_check_worker is None)
            self.assertEqual(app.text_domain, "https://toki34.com")
            self.assertEqual(app.text_domain_check_tree.item("1", "values")[2], "성공")
            self.assertTrue(app.text_domain_browser_button.instate(["disabled"]))
            self.assertEqual(request.call_count, 3)
            constructor.assert_called_once_with(self.state_path.parent / "text-browser", on_event=app._write_text_domain_log)
            self.assertEqual([call.kwargs["visible"] for call in browser.get.call_args_list], [False, True])
            original["text_domain"] = "https://toki34.com"
            self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), original)
            app.close()
            self.assertTrue(app.text_domain_check_cancel.is_set())
            browser.close.assert_called()
        records = [json.loads(line) for line in app.text_domain_log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(row["event"] == "check_start" for row in records), 2)
        self.assertTrue(any(row["event"] == "verification_required" for row in records))
        self.assertTrue(any(row["event"] == "check_success" and row["url"] == "https://toki34.com" for row in records))
        self.assertTrue(any(row["event"] == "address_saved" for row in records))

    def test_text_connection_log_rotates_redacts_queries_and_tolerates_write_errors(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.text_domain_log_path.write_bytes(b"x" * (1024 * 1024))
        app._write_text_domain_log("test", url="https://toki34.com/?token=secret#fragment", detail="line1\nline2")
        self.assertEqual(app.text_domain_log_path.with_suffix(".previous.log").stat().st_size, 1024 * 1024)
        record = json.loads(app.text_domain_log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["url"], "https://toki34.com/")
        self.assertEqual(record["detail"], "line1 line2")
        self.assertNotIn("secret", json.dumps(record))
        blocked = self.storage / "not-a-folder"
        blocked.touch()
        app.text_domain_log_path = blocked / "text-domain.log"
        app._write_text_domain_log("unwritable")
        self.assertTrue(app._text_log_error)

    def test_domain_browser_progress_shows_countdown_and_ignores_stale_runs(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app._text_log_run = "current"
        app.text_domain_check_worker = Mock()
        app._set_connection_state("B", "checking")
        event = {"id": "2", "phase": "안내 주소", "url": "https://toki34.com/", "state": "checking", "detail": "응답 대기"}
        app.text_domain_check_events.put(("progress", event))
        app.text_domain_check_events.put(("browser_progress", ("current", "verification_wait",
            {"mode": "automatic", "elapsed": 12, "remaining": 18, "limit": 30})))
        app._poll_text_domain_check()
        self.assertIsNotNone(app.text_domain_check_worker)
        self.assertEqual(app.site_badges['B'].get(), '자동 인증 중')
        self.assertIn('경과 12초 / 최대 30초', app.site_messages['B'].get())
        self.assertIn('남은 18초', app.site_messages['B'].get())
        self.assertIn('남은 18초', app.text_domain_check_tree.item('2', 'values')[3])
        self.assertTrue(app.text_domain_browser_button.instate(['disabled']))
        previous = app.site_messages['B'].get()
        app.text_domain_check_events.put(("browser_progress", ("previous", "chrome_start", {})))
        app._poll_text_domain_check()
        self.assertEqual(app.site_messages['B'].get(), previous)
        app.text_domain_check_events.put(("verification", ("https://toki34.com/", "검증 필요")))
        app._poll_text_domain_check()
        self.assertEqual(app.site_badges['B'].get(), '인증 필요')
        self.assertIn('브라우저 검증 열기', app.site_messages['B'].get())
        self.assertTrue(app.text_domain_browser_button.instate(['!disabled']))
        app.text_domain_check_events.put(("browser_progress", ("current", "chrome_start", {})))
        app._poll_text_domain_check()
        self.assertEqual(app.site_badges['B'].get(), '인증 필요')

    def test_failed_download_keeps_retrying_and_reuses_saved_segments(self):
        app = self.create_app()
        self.assertTrue(app.auto_retry.get())
        failures = []
        def segment(episode, index, cancel):
            if index == 1 and len(failures) < 4:
                failures.append(index)
                raise downloader.DownloadError('일시적인 전송 오류')
        self.segment_handler = segment
        job = self.add(app)
        for attempt in range(4):
            self.pump(app, lambda: app.active_id is None and job['status'] == 'error'
                      and len(failures) == attempt + 1 and job.get('auto_retry_at', 0) > time.monotonic())
            self.assertEqual(len(failures), attempt + 1)
            self.assertGreater(job['auto_retry_at'], time.monotonic() + 25)
            self.assertEqual(app.tree.item(job['id'], 'values')[1], '재시도 대기')
            job['auto_retry_at'] = time.monotonic() - 1
        self.pump(app, lambda: job['status'] == 'completed' and app.active_id is None)
        self.assertEqual(self.requests.count((FIRST_EPISODE, 0)), 1)
        self.assertEqual(Path(job['file']).read_bytes(), b''.join(packet(i) for i in range(3)))

    def test_auto_retry_setting_persists_and_off_pause_and_authentication_stop_retries(self):
        app = self.create_app()
        self.assertTrue(app.auto_retry.get())
        app.auto_retry_check.invoke()
        self.assertFalse(json.loads(self.state_path.read_text(encoding='utf-8'))['auto_retry'])
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        app = self.create_app()
        self.assertFalse(app.auto_retry.get())
        job = self.add(app)
        self.pump(app, lambda: job['status'] == 'completed' and app.active_id is None)
        job.update(status='error', message='검사 오류', auto_retry_at=0)
        app.queue_running = True
        app._start_next()
        self.assertEqual(job['auto_retry_at'], 0)
        app.auto_retry_check.invoke()
        app._start_next()
        self.assertGreater(job['auto_retry_at'], time.monotonic())
        app.queue_running = False
        app._refresh_table()
        self.assertEqual(app.tree.item(job['id'], 'values')[1], '오류')
        app.queue_running = True
        app.auto_retry_check.invoke()
        self.assertEqual(job['auto_retry_at'], 0)
        with patch.object(app, '_save_jobs', return_value=False):
            app.auto_retry_check.invoke()
        self.assertFalse(app.auto_retry.get())
        app.auto_retry_check.invoke()
        app._start_next()
        app.tree.selection_set(job['id'])
        app.pause()
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(job['auto_retry_at'], 0)
        job.update(status='error', verification_url='https://toki34.com/novel/1/1')
        app.queue_running = True
        with patch.object(downloader, 'download_video') as run:
            app._start_next()
        run.assert_not_called()
        self.assertEqual(job['auto_retry_at'], 0)

    def test_fastest_server_option_persists_and_failed_save_keeps_previous_mode(self):
        app = self.create_app()
        self.assertFalse(app.fastest_server_only.get())
        self.assertFalse(app._fastest_server_only_event.is_set())
        self.assertEqual(app.fastest_server_check['text'], '가장 빠른 서버에서만 받기')
        app.fastest_server_check.invoke()
        self.assertTrue(app._fastest_server_only_event.is_set())
        self.assertTrue(json.loads(self.state_path.read_text(encoding='utf-8'))['fastest_server_only'])
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        app = self.create_app()
        self.assertTrue(app.fastest_server_check.instate(['selected']))
        self.assertTrue(app._fastest_server_only_event.is_set())
        with patch.object(app, '_save_jobs', return_value=False):
            app.fastest_server_check.invoke()
        self.assertTrue(app.fastest_server_only.get())
        self.assertTrue(app._fastest_server_only_event.is_set())
        self.assertIn('이전 설정', app.status.get())
        app.fastest_server_check.invoke()
        self.assertFalse(app._fastest_server_only_event.is_set())
        self.assertFalse(json.loads(self.state_path.read_text(encoding='utf-8'))['fastest_server_only'])
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        self.assertFalse(self.create_app().fastest_server_only.get())

    def test_fastest_server_checkbox_updates_the_running_worker_without_restart(self):
        app = self.create_app()
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        observed = []
        def download(value, folder, cancel, report, **kwargs):
            observed.append(kwargs['fastest_only'])
            entered.set()
            release.wait(timeout=5)
            raise downloader.Cancelled()
        with patch.object(downloader, 'download_video', side_effect=download):
            job = self.add(app)
            self.pump(app, entered.is_set)
            worker = app.worker
            run_id = job['run_id']
            self.assertIs(observed[0], app._fastest_server_only_event)
            self.assertFalse(observed[0].is_set())
            app.fastest_server_check.invoke()
            self.assertTrue(observed[0].is_set())
            app.fastest_server_check.invoke()
            self.assertFalse(observed[0].is_set())
            self.assertIs(app.worker, worker)
            self.assertTrue(worker.is_alive())
            self.assertEqual(job['run_id'], run_id)
            self.assertFalse(app.cancel_event.is_set())
            release.set()
            self.pump(app, lambda: app.active_id is None)

    def test_top_connection_badges_are_visible_on_every_tab_and_fit(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.root.attributes('-alpha', 0.0)
        app.root.attributes('-toolwindow', True)
        app.root.deiconify()
        self.assertTrue(app.auto_retry_check.instate(['selected']))
        for width, height in ((960, 850), (1200, 939)):
            app.root.geometry(f'{width}x{height}')
            for state in ('checking', 'verifying', 'manual', 'verification', 'success', 'error'):
                app._set_connection_state('A', 'success')
                app._set_connection_state('B', state)
                for page in (app.download_tab, app.search_tab, app.domains_tab):
                    app.main_tabs.select(page)
                    app.root.update()
                    for site, badge in app.header_site_badges.items():
                        self.assertTrue(badge.winfo_ismapped())
                        self.assertIn(app.site_badges[site].get(), badge['text'])
                        self.assertLessEqual(badge.winfo_rootx() + badge.winfo_width(), app.root.winfo_rootx() + width)
                        self.assertLess(badge.winfo_rooty() + badge.winfo_height(), page.winfo_rooty() + 2)
                    end = 0
                    for x in range(app.main_tabs.winfo_width()):
                        try:
                            if app.main_tabs.index(f'@{x},25') == 2:
                                end = x
                        except downloader.tk.TclError:
                            pass
                    self.assertGreater(app.header_site_badges['A'].winfo_rootx(), app.main_tabs.winfo_rootx() + end)
    def test_chrome_timeout_is_logged_as_error_without_requesting_human_verification(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        guide = b'<span class="hud-url">toki34.com</span><a href="https://toki34.com/">Current</a>'
        replies = [(guide, tvmon_domains.TEXT_DOMAIN_GUIDE_URL), downloader.BrowserVerificationRequired("https://toki34.com/")]
        with patch.object(downloader, "resolve_current_text_domain", tvmon_domains.resolve_current_text_domain), \
                patch.object(downloader, "_fetch_single", side_effect=replies) as request, \
                patch.object(app, "_fetch_text_in_browser", side_effect=TimeoutError("Chrome 응답 시간 초과")):
            app.refresh_text_domain()
            self.pump(app, lambda: app.text_domain_check_worker is None)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(app.text_domain_check_tree.item("2", "values")[2], "실패")
        self.assertTrue(app.text_domain_browser_button.instate(["disabled"]))
        records = [json.loads(line) for line in app.text_domain_log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["event"] == "check_error" and "시간 초과" in row["detail"] for row in records))
        self.assertFalse(any(row["event"] == "verification_required" for row in records))

    def test_text_auto_preference_is_independent_persistent_and_rolls_back_on_save_failure(self):
        app = self.create_app()
        self.assertTrue(app.auto_text_domain.get())
        app.text_domain_auto_check.invoke()
        self.assertFalse(app.auto_text_domain.get())
        self.assertTrue(app.auto_domain.get())
        self.assertFalse(json.loads(self.state_path.read_text(encoding="utf-8"))["auto_text_domain"])
        app.root.update_idletasks()
        app.close()
        self.cancel_timers(app)
        restored = self.create_app()
        self.assertFalse(restored.auto_text_domain.get())
        elapsed = threading.Event()
        restored.root.after(350, elapsed.set)
        self.pump(restored, elapsed.is_set)
        self.text_domain_resolver.assert_not_called()
        self.assertIsNone(restored.text_domain_check_worker)
        original = self.state_path.read_bytes()
        with patch.object(restored, "_save_jobs", return_value=False):
            restored.text_domain_auto_check.invoke()
        self.assertFalse(restored.auto_text_domain.get())
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_closing_cancels_text_domain_check_and_ignores_late_success(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        entered = threading.Event()
        release = threading.Event()
        self.gates.append(release)
        def resolve(fetch, cancel, **kwargs):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError("Text check was not released")
            return "https://toki35.com"
        with patch.object(downloader, "resolve_current_text_domain", side_effect=resolve):
            app.refresh_text_domain()
            self.pump(app, entered.is_set)
            worker = app.text_domain_check_worker
            app.close()
            self.assertTrue(app.text_domain_check_cancel.is_set())
            original = self.state_path.read_bytes()
            release.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            with patch.object(app, "_save_jobs") as save:
                app._poll_text_domain_check()
                app.refresh_text_domain()
                save.assert_not_called()
        self.assertEqual(app.text_domain, "")
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_domain_site_tabs_and_controls_fit_supported_window_sizes(self):
        for scaling in (1.5, 2.0):
            self.state_path = self.storage / f"domain-scale-{scaling}" / "jobs.json"
            app = self.create_app(scaling=scaling)
            app.auto_domain.set(False)
            app.auto_text_domain.set(False)
            self.assertEqual([button["text"] for button in app.site_buttons.values()], ["TVWIKI", "뉴토끼"])
            app.root.attributes("-alpha", 0.0)
            app.root.attributes("-toolwindow", True)
            app.root.deiconify()
            app.main_tabs.select(app.domains_tab)
            for width, height in ((960, 850), (1200, 939)):
                app.root.geometry(f"{width}x{height}")
                for site, page in (("A", app.video_domains_tab), ("B", app.text_domains_tab)):
                    app.site_buttons[site].invoke()
                    app.root.update()
                    self.assertEqual(app.domain_sites.select(), str(page))
                    self.assertEqual(app.site_buttons[site]["style"], "Selected.Nav.TButton")
                    for key, panel in app.domain_sections[site].items():
                        app._toggle_domain_section(site, key, opened=True)
                        app.root.update()
                        self.assertTrue(panel.winfo_ismapped())
                        self.assertEqual(sum(bool(item.winfo_manager()) for item in app.domain_sections[site].values()), 1)
                        widgets = [panel, app.site_buttons[site], app.domain_refresh_button if site == "A" else app.text_domain_refresh_button]
                        widgets += [pair[0] for pair in app.domain_section_buttons[site].values()]
                        if key == "manage":
                            widgets += [app.domain_entry, app.domain_tree, app.domain_remove_button]
                        if key == "history":
                            widgets += [app.domain_check_tree if site == "A" else app.text_domain_check_tree,
                                        app.domain_status_label if site == "A" else app.text_domain_status_label]
                        if site == "B" and key == "help":
                            widgets.append(app.text_domain_browser_button)
                        if key == "guide":
                            widgets += ([app.domain_guide_entry, app.domain_guide_save_button, app.domain_guide_message_label]
                                        if site == "A" else [app.text_domain_guide_entry, app.text_domain_guide_save_button, app.text_domain_guide_message_label])
                        for widget in widgets:
                            with self.subTest(scaling=scaling, size=(width, height), site=site, section=key, widget=str(widget)):
                                app._reveal_domain_control(widget)
                                app.root.update()
                                self.assertTrue(widget.winfo_ismapped())
                                self.assertGreater(widget.winfo_width(), 1)
                                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
                                self.assertGreaterEqual(widget.winfo_rootx(), app.root.winfo_rootx())
                                self.assertGreaterEqual(widget.winfo_rooty(), app.domain_canvases[site].winfo_rooty())
                                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), app.root.winfo_rootx() + width)
                                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), app.root.winfo_rooty() + height)
                        app._toggle_domain_section(site, key, opened=False)
            app.root.withdraw()
        self.assertEqual(self.callback_errors, [])

    def test_domain_page_scroll_and_keyboard_focus_reveal_full_controls(self):
        app = self.create_app(scaling=2.0)
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.root.geometry("960x850")
        app.main_tabs.select(app.domains_tab)
        app.root.update()
        app._toggle_domain_section("A", "manage", opened=True)
        app.root.update()
        self.assertGreaterEqual(app.domain_remove_button.winfo_height(), app.domain_remove_button.winfo_reqheight())
        self.assertLessEqual(app.domain_remove_button.winfo_rooty() + app.domain_remove_button.winfo_height(), app.root.winfo_rooty() + 850)
        canvas = app.domain_canvases["A"]
        canvas.yview_moveto(0)
        app.root.update()
        canvas.event_generate("<MouseWheel>", delta=-120, x=10, y=10)
        app.root.update()
        self.assertGreater(canvas.yview()[0], 0)
        canvas.yview_moveto(0)
        app.domain_remove_button.event_generate("<FocusIn>")
        app.root.update()
        self.assertGreater(canvas.yview()[0], 0)
        self.assertLessEqual(app.domain_remove_button.winfo_rooty() + app.domain_remove_button.winfo_height(), app.root.winfo_rooty() + 850)
        offset = canvas.yview()
        app.site_buttons["B"].invoke()
        app.root.update()
        self.assertEqual(canvas.yview(), offset)
        self.assertEqual(app.domain_canvases["B"].yview()[0], 0)
        self.assertEqual(self.callback_errors, [])

    def test_server_count_is_shown_and_retained_after_completion_and_restart(self):
        app = self.create_app()
        app.show_details()
        self.assertEqual(app.server_info_button['text'], '서버 정보')
        job = self.add(app)
        self.pump(app, lambda: app.active_id is None and job['status'] == 'completed')
        self.assertEqual(job['server_count'], 1)
        self.assertEqual(job['servers'], {'media.example': 3})
        self.assertEqual(app.server_summary.get(), '영상 조각 지도')
        self.assertEqual(app.server_info_button['text'], '서버 1개')
        self.assertEqual([app.server_tree.item(row, 'values') for row in app.server_tree.get_children()],
                         [('media.example', '3', '—', '—', '0')])
        app.server_info_button.invoke()
        self.assertEqual(app.detail_tabs.select(), str(app.server_tab))
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['jobs'][0]['server_count'], 1)
        self.assertEqual(saved['jobs'][0]['servers'], {'media.example': 3})
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertEqual(restarted.server_info_button['text'], '서버 1개')
        self.assertEqual([restarted.server_tree.item(row, 'values') for row in restarted.server_tree.get_children()],
                         [('media.example', '3', '—', '—', '0')])
        restarted.add_episodes([{'title': '미조회 작품', 'url': SECOND_URL}])
        self.assertEqual(restarted.server_info_button['text'], '서버 정보')
        self.assertEqual(restarted.server_tree.get_children(), ())
        restarted.tree.selection_set(job['id'])
        restarted.tree.focus(job['id'])
        restarted._refresh_details(force=True)
        self.assertEqual(restarted.server_info_button['text'], '서버 1개')
        self.assertEqual(len(restarted.server_tree.get_children()), 1)

    def test_tracker_states_refresh_in_details_and_clear_on_next_video(self):
        from tvmon_p2p import P2P_TRACKERS
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'f'*32)
        job.update(total=2, status='downloading', run_id=1)
        app.jobs.append(job)
        app.active_id = job['id']
        app._refresh_table()
        app.tree.selection_set(job['id'])
        trackers = [{'url':url, 'state':'announced', 'error':'', 'remote_peers':0} for url in P2P_TRACKERS]
        def transfer(status, peers=0):
            app.events.put((job['id'], 1, 'transfer', {'speed':1, 'p2p_status':status,
                           'p2p_peers':peers, 'p2p_trackers':[dict(item) for item in trackers]}))
            self.pump(app, app.events.empty)
            app._refresh_details(force=True)
        transfer('no_peers')
        self.assertIn('트래커 정상', app.p2p_connection_status.get())
        self.assertIn('연결 가능한 피어 없음', app.p2p_connection_status.get())
        self.assertEqual(len(app.tracker_tree.get_children()), 3)
        self.assertEqual(app.tracker_tree.item(P2P_TRACKERS[0], 'values')[1:3], ('정상 응답', '0개'))
        trackers[0].update(state='error', error='connection_failed', remote_peers=None)
        transfer('tracker_partial')
        self.assertIn('트래커 일부 오류', app.p2p_connection_status.get())
        self.assertIn('확인 제한', app.peer_connections_empty.cget('text'))
        self.assertEqual(app.tracker_tree.item(P2P_TRACKERS[0], 'tags'), ('error',))
        transfer('connected', 2)
        self.assertIn('피어 2개 연결됨', app.p2p_connection_status.get())
        self.assertIn('오류 1개', app.p2p_connection_status.get())
        trackers[0].update(state='announced', error='', remote_peers=0)
        transfer('no_peers')
        self.assertFalse(app.tracker_tree.item(P2P_TRACKERS[0], 'tags'))
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        self.assertNotIn('p2p_trackers', self.state_path.read_text(encoding='utf-8'))
        app.events.put((job['id'], 1, 'info', {'title':job['title'],'duration':4,'segments':2}))
        self.pump(app, app.events.empty)
        app._refresh_details(force=True)
        self.assertEqual(job['p2p_trackers'], [])
        self.assertEqual(app.tracker_tree.get_children(), ())
        self.assertNotIn('트래커 정상', app.p2p_connection_status.get())

    def test_p2p_transfer_summary_is_visible_and_cleared_by_next_video_info(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'a' * 32)
        job.update(status='downloading', run_id=1, total=3, done=1)
        app.jobs.append(job)
        app.active_id = job['id']
        app.cancel_event = threading.Event()
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app.show_details()
        app.root.update_idletasks()
        app.events.put((job['id'], job['run_id'], 'transfer',
                        {'speed': 2000000, 'p2p_bytes': 4*1024*1024, 'p2p_peers': 3}))
        self.pump(app, app.events.empty)
        self.assertEqual(app.source_tree.item('p2p', 'values')[1], '4.0 MB')
        self.assertIn('피어 3개', app.p2p_overview.get())
        self.assertEqual(app.server_summary.get(), '영상 조각 지도')
        app.events.put((job['id'], job['run_id'], 'info',
                        {'title': job['title'], 'duration': 6, 'segments': 3, 'server_count': 1, 'servers': {'media.example':3}}))
        self.pump(app, app.events.empty)
        self.assertEqual(job['p2p_bytes'], 0)
        self.assertEqual(app.server_summary.get(), '영상 조각 지도')
        self.assertEqual(app.server_info_button['text'], '서버 1개')
        job['p2p_peers'] = 3
        app.pause()
        self.assertTrue(app.cancel_event.is_set())
        app.events.put((job['id'], job['run_id'], 'paused', None))
        app.events.put((job['id'], job['run_id'], 'idle', None))
        self.pump(app, lambda: app.active_id is None)
        self.assertEqual(job['p2p_peers'], 0)

    def test_p2p_map_colors_prefetch_and_saved_source_survive_restart(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'a'*32)
        job.update(total=7, status='downloading', completed_indices=[0, 1, 6], p2p_completed_indices=[1], http_completed_indices=[0],
                   p2p_active_indices=[3], segment_states={2:'active', 4:'error'}, run_id=1)
        app.jobs.append(job)
        app.active_id = job['id']
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app.tree.focus(job['id'])
        app.show_details()
        app.root.update_idletasks()
        app._draw_map(force=True)
        colors = {app.map_canvas.itemcget(item, 'fill') for item in app.map_canvas.find_all()}
        self.assertEqual(colors, set(downloader.PART_COLORS.values()))
        self.assertIn('P2P 완료 1', app.part_summary.get())
        self.assertIn('P2P 수신/검증 1', app.part_summary.get())
        app.events.put((job['id'], 1, 'segment', {'index':3, 'state':'complete', 'source':'p2p'}))
        self.pump(app, app.events.empty)
        self.assertEqual(job['p2p_completed_indices'], [1,3])
        app.events.put((job['id'], 1, 'parts', {'total':7, 'completed':[0,1,3,5,6], 'p2p_completed':[5]}))
        self.pump(app, app.events.empty)
        self.assertEqual(job['p2p_completed_indices'], [1,3,5])
        app.events.put((job['id'], 1, 'paused', None))
        app.events.put((job['id'], 1, 'idle', None))
        self.pump(app, lambda: app.active_id is None)
        self.assertEqual(job['p2p_active_indices'], [])
        app.close()
        self.cancel_timers(app)
        restarted = self.create_app()
        self.assertEqual(restarted.jobs[0]['p2p_completed_indices'], [1,3,5])
        self.assertEqual(restarted.jobs[0]['http_completed_indices'], [0])
        self.assertEqual(restarted.source_tree.item('unknown', 'values')[3], '1개')
        self.assertEqual(restarted.jobs[0]['p2p_active_indices'], [])

    def test_grouped_map_keeps_http_and_p2p_colors_in_the_same_cell(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'b'*32)
        job.update(total=1201, status='paused', completed_indices=[0,1], p2p_completed_indices=[1], http_completed_indices=[0])
        app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app.tree.focus(job['id'])
        app.show_details()
        app.root.update_idletasks()
        app._draw_map(force=True)
        cells = app.map_canvas.find_all()
        self.assertEqual([app.map_canvas.itemcget(item, 'fill') for item in cells[:2]],
                         [downloader.PART_COLORS['http_complete'], downloader.PART_COLORS['p2p_complete']])
        first, second = (app.map_canvas.coords(item) for item in cells[:2])
        self.assertEqual(first[2], second[0])
        self.assertEqual(first[1], second[1])

    def test_simultaneous_http_and_p2p_receiving_share_the_same_map_cell(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'b'*32)
        job.update(total=2, p2p_active_indices=[0], segment_states={0:'active'})
        app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app.show_details()
        app.root.update_idletasks()
        app._draw_map(force=True)
        cells = app.map_canvas.find_all()
        self.assertEqual([app.map_canvas.itemcget(item, 'fill') for item in cells[:2]],
                         [downloader.PART_COLORS['http_active'], downloader.PART_COLORS['p2p_active']])
        first, second = (app.map_canvas.coords(item) for item in cells[:2])
        self.assertAlmostEqual(first[2]-first[0], second[2]-second[0])
        self.assertAlmostEqual(first[2], second[0])
        self.assertIn('동시 1', app.part_summary.get())

    def test_transfer_details_distinguish_waiting_http_peer_and_unknown_history(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'c'*32)
        job.update(total=8, status='downloading', completed_indices=[0,1,2], http_completed_indices=[0],
                   p2p_completed_indices=[1], http_bytes=2048, p2p_bytes=4096, http_speed=1024, p2p_speed=2048,
                   p2p_peers=3, p2p_status='connected', p2p_parts=[{'index':6, 'state':'ready', 'bytes':188}],
                   workers=[{'slot':0, 'index':3, 'source':'p2p_wait', 'state':'peer_wait', 'host':'media.example', 'bytes':0}])
        app.jobs.append(job)
        app.active_id = job['id']
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app._refresh_details(force=True)
        self.assertEqual(app.worker_tree.item('0', 'values')[2:4], ('P2P 확인', 'P2P 대기'))
        app.worker_tree.selection_set('0')
        app._refresh_worker_host()
        self.assertIn('아직 HTTP 요청 전', app.worker_host.get())
        self.assertEqual(app.source_tree.item('http', 'values')[1:4], ('2.0 KB', '1.0 KB/s', '1개'))
        self.assertEqual(app.source_tree.item('p2p', 'values')[1:4], ('4.0 KB', '2.0 KB/s', '1개'))
        self.assertEqual(app.source_tree.item('unknown', 'values')[3], '1개')
        self.assertEqual(app.peer_tree.item('6', 'values'), ('7', '수신 완료 · 검증·저장 대기', '188.0 B'))
        job['workers'][0].update(source='p2p', state='checking', bytes=188, http_bytes=20, transition='http_to_p2p')
        app._refresh_details(force=True)
        self.assertIn('HTTP → P2P 전환', app.worker_host.get())
        self.assertIn('20.0 B', app.worker_host.get())
        self.assertEqual(app.worker_tree.item('0', 'values')[-1], '—')
        app.active_id = None
        app._refresh_details(force=True)
        self.assertEqual(app.peer_tree.get_children(), ())
        self.assertEqual(app.source_tree.item('p2p', 'values')[2], '—')

    def test_peer_addresses_countries_refresh_and_remain_only_in_current_display(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'd'*32)
        job.update(total=2, status='downloading', run_id=1)
        app.jobs.append(job)
        app.active_id = job['id']
        app._refresh_table()
        app.tree.selection_set(job['id'])
        peers = [{'id': 'aa', 'address': '8.8.8.8', 'country': '미국 (US)', 'state': 'ready'},
                 {'id': 'bb', 'address': '2606:4700:4700::1111', 'country': '조회 중', 'state': 'ready'},
                 {'id': 'cc', 'address': '', 'country': '조회 불가', 'state': 'unavailable'}]
        app.events.put((job['id'], 1, 'transfer', {'speed': 1, 'p2p_peers': 3, 'p2p_peer_details': peers}))
        self.pump(app, app.events.empty)
        app._refresh_details(force=True)
        tree = app.peer_connections_tree
        self.assertEqual(tree.get_children(), ('aa', 'bb', 'cc'))
        self.assertEqual(tree.item('aa', 'values'), ('1', '8.8.8.8', '미국 (US)'))
        self.assertEqual(tree.item('bb', 'values')[1:], ('2606:4700:4700::1111', '조회 중'))
        self.assertEqual(tree.item('cc', 'values')[1], '미공개')
        tree.selection_set('bb')
        job['p2p_peer_details'] = [dict(peers[1], country='미국 (US)'), peers[0]]
        job['p2p_peers'] = 2
        app._refresh_details(force=True)
        self.assertEqual(tree.get_children(), ('bb', 'aa'))
        self.assertEqual(tree.selection(), ('bb',))
        self.assertEqual(tree.item('bb', 'values'), ('1', '2606:4700:4700::1111', '미국 (US)'))
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        saved = self.state_path.read_text(encoding='utf-8')
        self.assertNotIn('p2p_peer_details', saved)
        self.assertNotIn('8.8.8.8', saved)
        job['p2p_peer_details'] = []
        job['p2p_peers'] = 0
        app._refresh_details(force=True)
        self.assertEqual(tree.get_children(), ())
        self.assertIn('아직 연결된 피어', app.peer_connections_empty.cget('text'))
        job['p2p_peer_details'] = peers
        app.active_id = None
        app._refresh_details(force=True)
        self.assertEqual(tree.get_children(), ())
        self.assertIn('다운로드 중인 영상', app.peer_connections_empty.cget('text'))

    def test_legacy_job_without_server_count_remains_unknown(self):
        self.state_path.parent.mkdir(parents=True)
        data = {'version': 1, 'jobs': [{'id': 'a' * 32, 'url': PAGE_URL, 'folder': str(self.videos),
                                      'title': '기존 작품', 'status': 'completed', 'total': 13}]}
        self.state_path.write_text(json.dumps(data), encoding='utf-8')
        app = self.create_app()
        self.assertEqual(app.jobs[0]['server_count'], 0)
        self.assertEqual(app.recent_searches, [])
        self.assertEqual(app.text_domain, "")
        self.assertTrue(app.auto_text_domain.get())
        self.assertEqual(app.server_info_button['text'], '서버 정보')
        self.assertEqual(app.jobs[0]['servers'], {})
        self.assertEqual(app.server_tree.get_children(), ())

    def test_malformed_queue_is_rejected_without_modifying_original_file(self):
        self.state_path.parent.mkdir(parents=True)
        valid_job = {"id": "a" * 32, "url": PAGE_URL, "folder": str(self.videos)}
        malformed = ([], {"version": 1, "jobs": [{**valid_job, "url": 123}]},
                     {"version": 1, "jobs": [{**valid_job, "duration": "bad-duration"}]},
                     {"version": 1, "jobs": [{**valid_job, "total": 3, "server_count": 4}]},
                     {"version": 1, "jobs": [{**valid_job, "server_count": -1}]},
                     {"version": 1, "jobs": [{**valid_job, "servers": []}]},
                     {"version": 1, "jobs": [{**valid_job, "total": 3, "server_count": 1, "servers": {"media.example": "3"}}]},
                     {"version": 1, "jobs": [{**valid_job, "total": 3, "server_count": 1, "servers": {"media.example": 2}}]},
                     {"version": 1, "jobs": [], "recent_searches": "invalid"},
                     {"version": 1, "jobs": [], "recent_searches": ["valid", 123]},
                     {"version": 1, "jobs": [], "text_domain": 123},
                     {"version": 1, "jobs": [], "text_domain": "http://toki34.com"},
                     {"version": 1, "jobs": [], "auto_text_domain": "yes"},
                     {"version": 1, "jobs": [], "text_domain_guide_url": 123},
                     {"version": 1, "jobs": [], "text_domain_guide_url": "http://guide.example/"})
        for data in malformed:
            original = json.dumps(data).encode("utf-8")
            self.state_path.write_bytes(original)
            with self.subTest(data=data), self.assertRaises(downloader.DownloadError):
                self.create_app()
            self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(self.resolutions, [])

    def test_remove_rechecks_active_job_after_modal_confirmation(self):
        app = self.create_app()
        with patch.object(app, "_start_next"):
            job = self.add(app)

        def nested_start(*args, **kwargs):
            app.active_id = job["id"]
            job["status"] = "resolving"
            return True

        try:
            with patch.object(downloader.messagebox, "askyesno", side_effect=nested_start), \
                    patch.object(downloader, "discard_resume") as discard:
                app.remove_jobs()
                discard.assert_not_called()
                self.assertIn(job, app.jobs)
        finally:
            app.active_id = None
            app.queue_running = False
            job["status"] = "queued"
        app.filter.set("완료")
        app._filter_changed()
        with patch.object(downloader.messagebox, "askyesno") as confirmation, \
                patch.object(downloader, "discard_resume") as discard:
            app.remove_jobs()
            confirmation.assert_not_called()
            discard.assert_not_called()

    def test_copy_job_urls_uses_current_domains_without_changing_saved_identities(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app._apply_domains(["https://tvwiki54.net", "https://tvmon1.com"], 0)
        app.text_domain = "https://toki92.com"
        for number, (kind, url) in enumerate((("video", PAGE_URL), ("video", SECOND_URL),
                                             ("text", "https://toki34.com/novel/123/456"),
                                             ("text_zip", "https://toki34.com/novel/123")), 1):
            job = app._new_job(url, str(self.videos), f"{number:032x}")
            job.update(kind=kind, status="completed")
            app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set([job["id"] for job in app.jobs])
        app._refresh_details(force=True)
        app._dirty = True
        self.assertTrue(app._save_jobs(force=True))
        original = self.state_path.read_bytes()
        original_urls = [job["url"] for job in app.jobs]
        with patch.object(app.root, "clipboard_clear") as clear, patch.object(app.root, "clipboard_append") as copied:
            app.job_menu.invoke(7)
            clear.assert_called_once()
            copied.assert_called_once_with("\n".join((
                PAGE_URL.replace("tvmon1.com", "tvwiki54.net"), SECOND_URL.replace("tvmon1.com", "tvwiki54.net"),
                "https://toki92.com/novel/123/456", "https://toki92.com/novel/123")))
        self.assertEqual([job["url"] for job in app.jobs], original_urls)
        self.assertEqual(self.state_path.read_bytes(), original)
        app._apply_domains(["https://tvwiki55.net", "https://tvwiki54.net"], 0)
        app.tree.selection_set(app.jobs[0]["id"])
        with patch.object(app.root, "clipboard_clear"), patch.object(app.root, "clipboard_append") as copied:
            app.copy_job_urls()
            copied.assert_called_once_with(PAGE_URL.replace("tvmon1.com", "tvwiki55.net"))
        self.assertEqual([job["url"] for job in app.jobs], original_urls)

    def test_unchanged_queue_rows_are_not_rewritten_and_filter_order_remain_correct(self):
        app = self.create_app()
        for number in range(60):
            job = app._new_job(PAGE_URL, str(self.videos), f"{number+1:032x}")
            job.update(title=f"영상 {number+1}", status="paused" if number % 2 else "completed", total=4, done=2)
            app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(app.jobs[0]["id"])
        app.root.update_idletasks()
        with patch.object(app.tree, "item", wraps=app.tree.item) as item, \
                patch.object(app.tree, "move", wraps=app.tree.move) as move:
            app._refresh_table()
            app._refresh_table()
            item.assert_not_called()
            move.assert_not_called()
            app.jobs[1].update(done=3, bytes=1024)
            app._refresh_table()
            self.assertEqual(item.call_count, 1)
            self.assertEqual(item.call_args.args, (app.jobs[1]["id"],))
            move.assert_not_called()
        self.assertEqual(app.tree.set(app.jobs[1]["id"], "progress"), "75.0%")
        app.filter.set("일시정지")
        app._filter_changed()
        expected = tuple(job["id"] for job in app.jobs if job["status"] == "paused")
        self.assertEqual(app.tree.get_children(), expected)
        self.assertEqual(app.tree.selection(), expected[:1])
        app.jobs.reverse()
        app._refresh_table()
        self.assertEqual(app.tree.get_children(), tuple(reversed(expected)))
        removed = app.jobs.pop()
        app.tree.delete(removed["id"])
        app._refresh_table()
        self.assertNotIn(removed["id"], app._table_rows)
        app.filter.set("전체")
        app._filter_changed()
        self.assertEqual(app.tree.get_children(), tuple(job["id"] for job in app.jobs))

    def test_worker_and_segment_events_keep_data_without_refreshing_unchanged_queue_rows(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        job = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        job.update(status="downloading", run_id=1, total=4)
        app.jobs.append(job)
        app.active_id = job["id"]
        app._refresh_table()
        app.tree.selection_set(job["id"])
        app.root.update()
        self.cancel_timers(app)
        workers = [{"slot": 0, "index": 0, "state": "receiving", "host": "media.example", "bytes": 188}]
        try:
            with patch.object(app, "_refresh_table", wraps=app._refresh_table) as refresh:
                app.events.put((job["id"], 1, "workers", workers))
                app.events.put((job["id"], 1, "segment", {"index": 0, "state": "complete", "source": "http"}))
                app.events.put((job["id"], 1, "file", str(self.videos / "영상.ts")))
                app.poll()
                refresh.assert_not_called()
                self.assertEqual(job["workers"], workers)
                self.assertEqual(job["completed_indices"], [0])
                self.assertEqual(job["http_completed_indices"], [0])
                app.events.put((job["id"], 1, "progress", {"done": 1, "total": 4, "bytes": 188, "eta": 3}))
                app.poll()
                refresh.assert_called_once()
                self.assertEqual(app.tree.set(job["id"], "progress"), "25.0%")
                self.assertEqual(app.tree.set(job["id"], "size"), "188.0 B")
        finally:
            app.active_id = None
        self.assertEqual(self.callback_errors, [])

    def test_hidden_detail_widgets_refresh_current_data_when_reopened(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        job = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        job.update(status="downloading", run_id=1, total=4)
        app.jobs.append(job)
        app.active_id = job["id"]
        app._refresh_table()
        app.tree.selection_set(job["id"])
        app.root.update()
        app._refresh_details(force=True)
        self.cancel_timers(app)
        try:
            with patch.object(app, "_refresh_transfer_details", wraps=app._refresh_transfer_details) as transfer, \
                    patch.object(app, "_draw_map", wraps=app._draw_map) as draw:
                app.events.put((job["id"], 1, "transfer", {"speed": 1048576, "p2p_bytes": 1048576, "p2p_peers": 1,
                    "p2p_peer_details": [{"id": "peer", "address": "192.0.2.1", "country": "검증용", "state": "ready"}]}))
                app.poll()
                app.poll()
                transfer.assert_not_called()
                draw.assert_not_called()
                self.assertEqual(job["p2p_bytes"], 1048576)
                self.assertEqual(app.speed.get(), "1.0 MB/s")
                app.show_details()
                self.assertTrue(transfer.called)
                self.assertTrue(draw.called)
                self.assertEqual(app.source_tree.item("p2p", "values")[1], "1.0 MB")
                self.assertEqual(app.peer_connections_tree.item("peer", "values")[1], "192.0.2.1")
        finally:
            app.active_id = None
        self.assertEqual(self.callback_errors, [])

    def test_queue_title_reuses_measurements_until_text_or_available_width_changes(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        job = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        job.update(title="긴 영상 제목 " * 15, status="completed")
        app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(job["id"])
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.root.geometry("960x850")
        app.root.update()
        app._refresh_details(force=True)
        narrow = app.queue_title.cget("text")
        self.assertTrue(narrow.endswith("…"))
        with patch.object(downloader.tkfont, "Font", wraps=downloader.tkfont.Font) as font:
            app._fit_queue_title()
            app._fit_queue_title()
            font.assert_not_called()
        app.root.geometry("1400x900")
        app.root.update()
        wide = app.queue_title.cget("text")
        self.assertGreater(len(wide), len(narrow))
        job["title"] = "짧은 제목"
        app._refresh_details(force=True)
        self.assertEqual(app.queue_title.cget("text"), "짧은 제목")
        app.root.withdraw()

    def test_job_menu_targets_clicked_row_and_preserves_multi_selection(self):
        app = self.create_app()
        for number, status in enumerate(("completed", "paused", "queued"), 1):
            job = app._new_job(PAGE_URL if number == 1 else SECOND_URL, str(self.videos), f"{number:032x}")
            job.update(title=f"영상 {number}", status=status)
            app.jobs.append(job)
        target = self.videos / "영상 1.ts"
        self.videos.mkdir()
        target.write_bytes(packet(0))
        app.jobs[0]["file"] = str(target)
        app._refresh_table()
        first, second, third = [job["id"] for job in app.jobs]
        app.tree.selection_set(second)
        with patch.object(app.tree, "identify_row", return_value=first), \
                patch.object(app.job_menu, "tk_popup") as popup:
            app._show_job_menu(Mock(num=3, y=40, x_root=100, y_root=200))
            popup.assert_called_once_with(100, 200)
        self.assertEqual(app.tree.selection(), (first,))
        self.assertEqual(app.tree.focus(), first)
        self.assertEqual(app.job_menu.entrycget(0, "state"), "normal")
        self.assertEqual(app.job_menu.entrycget(2, "state"), "disabled")
        self.assertEqual(app.job_menu.entrycget(3, "state"), "disabled")
        with patch.object(downloader.os, "startfile") as opened:
            app.job_menu.invoke(0)
            opened.assert_called_once_with(str(target.resolve()))
        app.tree.selection_set((first, second))
        with patch.object(app.tree, "identify_row", return_value=second), patch.object(app.job_menu, "tk_popup"):
            app._show_job_menu(Mock(num=3, y=70, x_root=100, y_root=230))
        self.assertEqual(set(app.tree.selection()), {first, second})
        self.assertEqual(app.tree.focus(), second)
        for index in (0, 5, 6):
            self.assertEqual(app.job_menu.entrycget(index, "state"), "disabled")
        self.assertEqual(app.job_menu.entrycget(2, "state"), "normal")
        with patch.object(app.root, "clipboard_clear") as clear, patch.object(app.root, "clipboard_append") as copied:
            app.job_menu.invoke(7)
            clear.assert_called_once()
            copied.assert_called_once_with(PAGE_URL + "\n" + SECOND_URL)
        with patch.object(app.tree, "identify_row", return_value=""), patch.object(app.job_menu, "tk_popup") as popup:
            app._show_job_menu(Mock(num=3, y=800, x_root=100, y_root=900))
            popup.assert_not_called()
        self.assertEqual(set(app.tree.selection()), {first, second})
        with patch.object(app.tree, "identify_row", return_value=third), patch.object(app.job_menu, "tk_popup"):
            app._show_job_menu(Mock(num=3, y=100, x_root=100, y_root=260))
        self.assertEqual(app.tree.selection(), (third,))
        self.assertEqual(app.job_menu.entrycget(3, "state"), "normal")
        self.assertEqual(self.callback_errors, [])

    def test_job_menu_pauses_resumes_and_keeps_completed_file_on_removal(self):
        app = self.create_app()
        entered = self.hold_after_first_cached_segment()
        job = self.add(app)
        self.pump(app, lambda: entered.is_set() and job["done"] == 1)
        app._update_actions()
        self.assertEqual(app.job_menu.entrycget(0, "state"), "disabled")
        self.assertEqual(app.job_menu.entrycget(3, "state"), "normal")
        self.assertEqual(app.job_menu.entrycget(9, "state"), "disabled")
        app.job_menu.invoke(3)
        self.pump(app, lambda: app.active_id is None and job["status"] == "paused")
        self.assertEqual(app.job_menu.entrycget(2, "state"), "normal")
        self.assertEqual(app.job_menu.entrycget(3, "state"), "disabled")
        self.segment_handler = None
        app.job_menu.invoke(2)
        self.pump(app, lambda: app.active_id is None and job["status"] == "completed")
        self.assertEqual(self.requests.count((FIRST_EPISODE, 0)), 1)
        target = Path(job["file"])
        expected = b"".join(packet(index) for index in range(3))
        self.assertEqual(target.read_bytes(), expected)
        with patch.object(downloader.os, "startfile") as opened:
            app.job_menu.invoke(6)
            opened.assert_called_once_with(str(self.videos.resolve()))
        with patch.object(downloader.messagebox, "askyesno", return_value=False):
            app.job_menu.invoke(9)
        self.assertEqual(app.jobs, [job])
        with patch.object(downloader.messagebox, "askyesno", return_value=True):
            app.job_menu.invoke(9)
        self.assertFalse(app.jobs)
        self.assertEqual(target.read_bytes(), expected)

    def test_completed_selection_keeps_summary_when_another_job_merges(self):
        app = self.create_app()
        completed = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        completed.update(title="완료한 영상", status="completed", total=3, done=3, bytes=7 * 1024**3)
        running = app._new_job(SECOND_URL, str(self.videos), "2" * 32)
        running.update(title="진행 중 영상", status="downloading", run_id=1, total=3, done=3, speed=9999, eta=10)
        app.jobs.extend((completed, running))
        app.active_id = running["id"]
        app._refresh_table()
        app.tree.selection_set(completed["id"])
        app.root.update()
        app.events.put((running["id"], 1, "status", "받은 영상 조각을 순서대로 합치고 있습니다..."))
        app.poll()
        self.assertEqual(app.selection_status.get(), "완료")
        self.assertEqual(app.video_title.get(), "완료한 영상")
        self.assertEqual(app.queue_size["text"], "7.0 GB")
        self.assertEqual(app.status.get(), "")
        self.assertFalse(app.status_label.winfo_manager())
        self.assertEqual(app.tree.set(running["id"], "status"), "병합 중")
        self.assertEqual(app.tree.set(running["id"], "speed"), "—")
        self.assertEqual(app.tree.set(running["id"], "eta"), "—")
        app.events.put((running["id"], 1, "error", "파일 저장 실패"))
        app.poll()
        self.assertEqual(app.status.get(), "진행 중 영상 · 파일 저장 실패")
        self.assertTrue(app.status_label.winfo_manager())
        app.active_id = None

    def test_job_menu_keyboard_follows_visible_selection_without_footer_menu_button(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        for number in range(30):
            job = app._new_job(PAGE_URL, str(self.videos), f"{number + 1:032x}")
            job.update(title=f"영상 {number + 1}", status="paused")
            app.jobs.append(job)
        app._refresh_table()
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.tree.selection_set(app.jobs[0]["id"])
        app.root.update()
        self.assertFalse(hasattr(app, "queue_menu_button"))
        self.assertTrue(app.tree.bind("<Shift-F10>"))
        self.assertTrue(app.tree.bind("<App>"))
        with patch.object(app.job_menu, "tk_popup") as popup:
            app._show_job_menu()
            self.assertEqual(popup.call_count, 1)
            app._show_job_menu(Mock(num="??"))
            self.assertEqual(popup.call_count, 2)
        app.tree.yview_moveto(1)
        app.root.update()
        self.assertFalse(app.tree.bbox(app.jobs[0]["id"]))
        with patch.object(app.job_menu, "tk_popup") as popup:
            app._show_job_menu()
            popup.assert_called_once()
        app.root.update()
        self.assertTrue(app.tree.bbox(app.jobs[0]["id"]))
        app.filter_buttons["오류"].invoke()
        app.root.update()
        self.assertTrue(app.detail_button.instate(["disabled"]))
        with patch.object(app.job_menu, "tk_popup") as popup:
            app._show_job_menu()
            popup.assert_not_called()
        self.assertEqual(self.callback_errors, [])
        app.root.withdraw()

    def test_job_menu_keeps_posted_items_stable_while_polling_and_refreshes_after_close(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        job.update(status="downloading", run_id=1, total=3, done=1)
        app.jobs.append(job)
        app.active_id = job["id"]
        app._refresh_table()
        app.tree.selection_set(job["id"])
        self.cancel_timers(app)

        def posted_menu(*_):
            self.assertEqual(app.job_menu.entrycget(3, "state"), "normal")
            with patch.object(app.job_menu, "entryconfigure") as configure, \
                    patch.object(app.job_menu, "add_command") as add, \
                    patch.object(app.job_menu, "delete") as delete:
                app.events.put((job["id"], 1, "status", "받은 영상 조각을 순서대로 합치고 있습니다..."))
                for _ in range(5):
                    app.poll()
                self.assertEqual(job["status"], "merging")
                self.assertEqual(app.selection_status.get(), "병합 중")
                app._show_job_menu(Mock(num=3, y=40, x_root=100, y_root=200))
                self.assertEqual(popup.call_count, 1)
                job.update(status="completed", file=str(self.videos / "완료.ts"), done=3)
                app.active_id = None
                app._refresh_details(force=True)
                configure.assert_not_called()
                add.assert_not_called()
                delete.assert_not_called()

        with patch.object(app.tree, "identify_row", return_value=job["id"]), \
                patch.object(app.job_menu, "tk_popup", side_effect=posted_menu) as popup:
            app._show_job_menu(Mock(num=3, y=40, x_root=100, y_root=200))
        self.assertFalse(app._job_menu_open)
        self.assertEqual(app.job_menu.entrycget(0, "state"), "normal")
        self.assertEqual(app.job_menu.entrycget(3, "state"), "disabled")
        self.assertEqual(self.callback_errors, [])

    def test_details_reopen_on_progress_tab_and_explicit_server_navigation_still_works(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), "1" * 32)
        app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(job["id"])
        app._refresh_details(force=True)
        for tab in app.detail_tabs.tabs()[1:]:
            with self.subTest(previous_tab=tab):
                app.show_details(tab)
                self.assertEqual(app.detail_tabs.select(), tab)
                app.detail_window.withdraw()
                app.detail_button.invoke()
                self.assertEqual(app.detail_tabs.index(app.detail_tabs.select()), 0)
                app.detail_tabs.select(tab)
                app.detail_window.withdraw()
                app.job_menu.invoke(5)
                self.assertEqual(app.detail_tabs.index(app.detail_tabs.select()), 0)
        app.server_info_button.invoke()
        self.assertNotEqual(app.detail_tabs.index(app.detail_tabs.select()), 0)
        self.assertEqual(app.detail_tabs.select(), str(app.server_tab))
        self.assertEqual(self.callback_errors, [])

    def test_layout_keeps_controls_and_all_eight_workers_visible_at_supported_sizes(self):
        with patch.object(downloader, "WORKERS", 8):
            app = self.create_app()
            app.auto_domain.set(False)
            app.auto_text_domain.set(False)
            for number in range(25):
                job = app._new_job(PAGE_URL, str(self.videos), f"{number + 1:032x}")
                job["title"] = f"목록 검증 {number + 1}화"
                app.jobs.append(job)
            app._refresh_table()
            app.tree.selection_set(app.jobs[0]["id"])
            app.root.attributes("-alpha", 0.0)
            app.root.attributes("-toolwindow", True)
            app.root.deiconify()
            try:
                for width, height in ((1200, 939), (960, 850)):
                    with self.subTest(size=(width, height)):
                        app.root.geometry(f"{width}x{height}")
                        app.root.update()
                        self.assertFalse(app.detail_window.winfo_ismapped())
                        self.assertGreaterEqual(sum(bool(app.tree.bbox(row)) for row in app.tree.get_children()), 11)
                        self.assertGreater(app.tree.winfo_height(), app.root.winfo_height() * .47)
                        for widget in (app.url_entry, app.add_button, app.auto_retry_check, app.list_search, app.tree, app.detail_button):
                            self.assertGreater(widget.winfo_width(), 1)
                            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), app.root.winfo_rootx() + width)
                            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), app.root.winfo_rooty() + height)
                        queue_height = app.tree.winfo_height()
                        app.detail_button.invoke()
                        app.root.update()
                        self.assertTrue(app.detail_window.winfo_ismapped())
                        self.assertEqual(app.fastest_server_check.winfo_toplevel(), app.detail_window)
                        self.assertFalse(app.fastest_server_check.winfo_ismapped())
                        self.assertEqual(app.detail_window.winfo_width(), min(1540, app.root.winfo_screenwidth() - 80))
                        self.assertEqual(app.detail_window.winfo_height(), min(660, app.root.winfo_screenheight() - 80))
                        app.server_info_button.invoke()
                        app.root.update()
                        self.assertTrue(app.fastest_server_check.winfo_ismapped())
                        self.assertGreaterEqual(app.fastest_server_check.winfo_width(), app.fastest_server_check.winfo_reqwidth())
                        app.detail_tabs.select(0)
                        app.root.update()
                        self.assertEqual(app.tree.winfo_height(), queue_height)
                        for slot in range(8):
                            row = app.worker_tree.bbox(str(slot))
                            self.assertTrue(row, f"Worker {slot + 1} is clipped")
                            self.assertLessEqual(row[1] + row[3], app.worker_tree.winfo_height())
                        app.detail_window.withdraw()
                        app.root.update()
                        self.assertEqual(self.callback_errors, [])
            finally:
                app.root.withdraw()

    def test_queue_scrollbars_follow_overflow_filter_and_resize(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        bars = {str(widget["orient"]): widget for widget in app.tree.master.winfo_children()
                if isinstance(widget, downloader.ttk.Scrollbar)}

        def check(vertical, horizontal):
            app.root.update()
            self.assertEqual(bool(bars["vertical"].winfo_ismapped()), vertical, app.tree.yview())
            self.assertEqual(bool(bars["horizontal"].winfo_ismapped()), horizontal, app.tree.xview())
            self.assertEqual(self.callback_errors, [])

        for width in (1200, 960):
            app.root.geometry(f"{width}x850")
            check(False, False)
        for number in range(40):
            job = app._new_job(PAGE_URL, str(self.videos), f"{number + 1:032x}")
            job["title"] = "단일 결과" if number == 0 else f"다른 작업 {number}"
            app.jobs.append(job)
        app._refresh_table()
        check(True, False)
        app.tree.yview_moveto(1)
        check(True, False)
        app.list_query.set("단일 결과")
        check(False, False)
        app.list_query.set("검색 결과 없음")
        check(False, False)
        app.list_query.set("")
        check(True, False)
        app.tree.column("title", minwidth=1200, width=1200)
        check(True, True)
        app.tree.xview_moveto(1)
        app.tree.column("title", minwidth=220, width=420)
        for width in (1200, 960, 1200):
            app.root.geometry(f"{width}x850")
            check(True, False)
        app.tree.selection_set([job["id"] for job in app.jobs[1:]])
        with patch.object(downloader.messagebox, "askyesno", return_value=True):
            app.remove_jobs()
        check(False, False)
        self.assertEqual(app.tree.get_children(), (app.jobs[0]["id"],))

    def test_eta_header_and_values_fit_with_scrollbars_at_display_scales(self):
        for scaling in (4 / 3, 5 / 3, 2):
            with self.subTest(scaling=scaling):
                self.state_path = self.storage / f"scale-{scaling}" / "jobs.json"
                app = self.create_app(scaling=scaling)
                app.auto_domain.set(False)
                app.auto_text_domain.set(False)
                app.root.attributes("-alpha", 0.0)
                app.root.attributes("-toolwindow", True)
                app.jobs.clear()
                for number in range(50):
                    job = app._new_job(PAGE_URL, str(self.videos), f"{number + 1:032x}")
                    job["title"] = "한 항목" if number == 0 else f"다른 작업 {number}"
                    app.jobs.append(job)
                app._refresh_table()
                app.root.deiconify()
                style = downloader.ttk.Style(app.root)
                heading = downloader.tkfont.Font(app.root, font=style.lookup("Queue.Treeview.Heading", "font"))
                body = downloader.tkfont.Font(app.root, font=style.lookup("Queue.Treeview", "font"))
                vertical = next(widget for widget in app.tree.master.winfo_children()
                                if isinstance(widget, downloader.ttk.Scrollbar) and str(widget["orient"]) == "vertical")
                for width, height in ((960, 850), (1200, 900), (960, 850)):
                    app.root.geometry(f"{width}x{height}")
                    for query in ("한 항목", ""):
                        app.list_query.set(query)
                        app.root.update()
                        self.assertEqual(bool(vertical.winfo_ismapped()), not bool(query))
                        self.assertEqual(app.tree.xview(), (0.0, 1.0))
                        x, _, cell_width, _ = app.tree.bbox(app.jobs[0]["id"], "eta")
                        self.assertGreaterEqual(cell_width, heading.measure("남은 시간") + 14)
                        self.assertGreaterEqual(cell_width, body.measure("999:59:59") + 16)
                        self.assertLessEqual(x + cell_width, app.tree.winfo_width() - 8)
                self.assertEqual(self.callback_errors, [])
                app.root.withdraw()

    def test_domain_scrollbars_follow_each_list_independently(self):
        app = self.create_app()
        app.auto_domain.set(False)
        app.auto_text_domain.set(False)
        app.root.attributes("-alpha", 0.0)
        app.root.attributes("-toolwindow", True)
        app.root.deiconify()
        app.root.geometry("1200x900")
        app.main_tabs.select(app.domains_tab)
        for site in ("A", "B"):
            app.site_buttons[site].invoke()
            app._toggle_domain_section(site, "history", opened=True)
            frame = app.domain_sections[site]["history"]
            tree = next(widget for widget in frame.winfo_children() if isinstance(widget, downloader.ttk.Treeview))
            bars = {str(widget["orient"]): widget for widget in frame.winfo_children()
                    if isinstance(widget, downloader.ttk.Scrollbar)}
            app.root.update()
            self.assertFalse(bars["vertical"].winfo_ismapped())
            self.assertFalse(bars["horizontal"].winfo_ismapped())
            for index in range(10):
                tree.insert("", "end", values=("확인", f"https://example{index}.com", "성공", "확인 완료"))
            app.root.update()
            self.assertTrue(bars["vertical"].winfo_ismapped())
            app.root.geometry("960x850")
            app.root.update()
            self.assertTrue(bars["horizontal"].winfo_ismapped())
            tree.delete(*tree.get_children())
            app.root.geometry("1200x900")
            app.root.update()
            self.assertFalse(bars["vertical"].winfo_ismapped())
            self.assertFalse(bars["horizontal"].winfo_ismapped())
        self.assertEqual(self.callback_errors, [])

    def test_queue_filter_buttons_and_filename_search_preserve_jobs(self):
        app = self.create_app()
        for number, (title, status) in enumerate((("삼국지 1화", "queued"), ("삼국지 2화", "completed"), ("다른 작품", "error")), 1):
            job = app._new_job(PAGE_URL, str(self.videos), f"{number:032x}")
            job.update(title=title, status=status)
            app.jobs.append(job)
        app._refresh_table()
        app.filter_buttons["대기"].invoke()
        self.assertEqual(app.tree.get_children(), (app.jobs[0]["id"],))
        app.filter_buttons["전체"].invoke()
        app.list_query.set("삼국지")
        self.assertEqual(len(app.tree.get_children()), 2)
        app.filter_buttons["완료"].invoke()
        self.assertEqual(app.tree.get_children(), (app.jobs[1]["id"],))
        app.list_query.set("없는 제목")
        self.assertEqual(app.tree.get_children(), ())
        self.assertEqual(app.empty_label["text"], "조건에 맞는 작업이 없습니다.")
        self.assertEqual(len(app.jobs), 3)

    def test_domain_connection_badge_requires_success_and_authentication_opens_help(self):
        app = self.create_app()
        self.assertEqual(app.site_badges["B"].get(), "확인 전")
        self.assertEqual(app.site_nav_badges["B"]["text"], "● 확인 전")
        app._set_connection_state("A", "checking")
        self.assertEqual(app.site_nav_badges["A"]["text"], "● 확인 중")
        app._set_connection_state("A", "success")
        self.assertEqual(app.site_nav_badges["A"]["text"], "● 정상 연결")
        self.assertEqual(str(app.site_nav_badges["A"]["foreground"]), "#16713D")
        app.text_domain_check_events.put(("verification", ("https://toki92.com/", "검증이 필요합니다.")))
        app._poll_text_domain_check()
        self.assertEqual(app.site_badges["B"].get(), "인증 필요")
        self.assertEqual(app.site_nav_badges["B"]["text"], "● 인증 필요")
        self.assertEqual(str(app.site_nav_badges["B"]["foreground"]), "#B42318")
        self.assertTrue(app.domain_sections["B"]["help"].winfo_manager())
        self.assertFalse(app.text_domain_browser_button.instate(["disabled"]))
        with patch.object(app, "_save_jobs", return_value=False):
            app.text_domain_check_events.put(("success", ("https://toki92.com", None, "안내 주소")))
            app._poll_text_domain_check()
        self.assertEqual(app.site_badges["B"].get(), "확인 필요")
        self.assertEqual(app.site_nav_badges["B"]["text"], "● 확인 필요")
        self.assertEqual(str(app.site_nav_badges["B"]["background"]), "#FEECEB")
        app.site_buttons["B"].invoke()
        app._sync_domain_navigation()
        self.assertEqual(str(app.site_cards["B"]["highlightbackground"]), "#245EB8")
        self.assertEqual(str(app.site_nav_badges["B"]["foreground"]), "#B42318")
        self.assertEqual(str(app.site_nav_badges["A"]["foreground"]), "#16713D")
        self.assertTrue(app.domain_sections["B"]["history"].winfo_manager())
        app.text_domain_check_events.put(("success", ("https://toki92.com", None, "안내 주소")))
        app._poll_text_domain_check()
        self.assertEqual(app.site_badges["B"].get(), "정상 연결")
        self.assertEqual(app.site_nav_badges["B"]["text"], "● 정상 연결")
        self.assertEqual(str(app.site_nav_badges["B"]["foreground"]), "#16713D")
        self.assertEqual(str(app.site_badge_labels["B"]["background"]), "#E5F5EB")
        self.assertEqual(app.site_nav_badges["A"]["text"], "● 정상 연결")
        self.assertEqual([step.get() for step in app.site_steps["B"]], ["성공", "성공"])


    def test_slow_server_status_and_rescue_are_visible_and_not_persisted(self):
        app = self.create_app()
        job = app._new_job(PAGE_URL, str(self.videos), 'f' * 32)
        host = 'c9.slow.example'
        job.update(total=3, status='downloading', server_count=1, servers={host: 3}, run_id=1,
                   workers=[dict(slot=0, index=0, host=host, source='http', state='receiving',
                                 bytes=32, http_bytes=32, rescue_host='c9.fast.example')])
        app.jobs.append(job)
        app.active_id = job['id']
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app.show_details()
        app.root.update_idletasks()
        app.events.put((job['id'], 1, 'transfer', dict(speed=0, server_health={host: dict(
            state='slow', cooldown=25, reason='수신 정체', samples=1, speed=1024, active=1,
            switches=1, slow_count=1)})))
        app.poll()
        values = app.server_tree.item(host, 'values')
        self.assertEqual(values, (host, '3', '느림 · 회피 25초', '1.0 KB/s', '1'))
        self.assertIn('slow', app.server_tree.item(host, 'tags'))
        self.assertIn('느린 서버 1개', app.server_status.get())
        self.assertIn('서버 자동 선택', app.server_status.get())
        app.fastest_server_check.invoke()
        self.assertIn('가장 빠른 서버 집중', app.server_status.get())
        self.assertEqual(app.worker_tree.set('0', 'state'), '대체 병행')
        self.assertIn('slow', app.worker_tree.item('0', 'tags'))
        app.worker_tree.selection_set('0')
        app._refresh_worker_host()
        self.assertIn('c9.fast.example', app.worker_host.get())
        app._dirty = True
        app._save_jobs(force=True)
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertNotIn('server_health', saved['jobs'][0])
        app.active_id = None

    def test_recovered_server_and_job_selection_clear_slow_highlight(self):
        app = self.create_app()
        host = 'c9.slow.example'
        job = app._new_job(PAGE_URL, str(self.videos), 'e' * 32)
        job.update(server_count=1, servers={host: 2}, server_health={host: dict(
            state='healthy', samples=2, speed=2048, active=0, slow_count=1)})
        app.jobs.append(job)
        app._refresh_table()
        app.tree.selection_set(job['id'])
        app._refresh_details(force=True)
        self.assertEqual(app.server_tree.set(host, 'state'), '정상 · 회복')
        self.assertFalse(app.server_tree.item(host, 'tags'))
        app.tree.selection_remove(job['id'])
        app._refresh_details(force=True)
        self.assertEqual(app.server_tree.get_children(), ())
        self.assertEqual(app.server_status.get(), '')


if __name__ == "__main__":
    unittest.main()

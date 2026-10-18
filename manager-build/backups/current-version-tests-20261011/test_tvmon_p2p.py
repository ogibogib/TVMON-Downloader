"""P2P buffering, deadlines and protocol checks without a browser or network."""
import base64
import json
import threading
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import tvmon_p2p as p2p
from tvmon_http import TransferError

MANIFEST = "https://every9.poorcdn.com/v/e/" + "a" * 32 + "/c.html?token=test"
SEGMENTS = [(MANIFEST.split("c.html")[0] + f"index{i:05d}.gif", None, bytes(16), 2.002) for i in range(3)]


class P2PReceiverTests(unittest.TestCase):
    def receiver(self, completed=()):
        with patch.object(p2p.threading, "Thread") as thread:
            thread.return_value.is_alive.return_value = False
            value = p2p.P2PVideoReceiver(MANIFEST, SEGMENTS, completed, cancel=threading.Event(),
                                        load_script=Mock(), on_bytes=Mock(), on_status=Mock())
        self.addCleanup(value.close)
        return value

    def chunk(self, data, *, index=0, offset=0, total=None, received=None):
        return {"bytes": len(data) if received is None else received, "peers": 2,
                "chunk": {"index": index, "offset": offset, "total": len(data) if total is None else total,
                          "data": base64.b64encode(data).decode()}}

    def test_only_known_single_quality_content_joins_swarm(self):
        self.assertEqual(p2p.video_swarm_id(MANIFEST, SEGMENTS), "tvw-v5-e-" + "a"*32)
        for url in (MANIFEST.replace("https:", "http:"), MANIFEST.replace("poorcdn.com", "example.com"),
                    MANIFEST.replace("/e/", "/e/other/"), MANIFEST.replace("c.html", "master.m3u8")):
            self.assertIsNone(p2p.video_swarm_id(url, SEGMENTS))
        self.assertIsNone(p2p.video_swarm_id(MANIFEST, [("https://media.example/other.gif", None, bytes(16), 2)]))

    def test_tracker_status_distinguishes_no_peers_errors_and_peer_connection_wait(self):
        receiver = self.receiver()
        self.assertEqual(receiver.stats()['p2p_status'], 'initializing')
        receiver._ready = True
        trackers = [{'url':url, 'state':'announced', 'error':'', 'remote_peers':0} for url in p2p.P2P_TRACKERS]
        message = {'bytes':0, 'peers':0, 'trackers':trackers}
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'no_peers')
        trackers[0].update(state='error', error='connection_failed', remote_peers=None)
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'tracker_partial')
        for item in trackers:
            item.update(state='error', error='connection_failed', remote_peers=None)
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'tracker_error')
        for item in trackers:
            item.update(state='announced', error='', remote_peers=0)
        trackers[0]['remote_peers'] = 2
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'peer_wait')
        message['peers'] = 1
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'connected')
        message['peers'] = 0
        trackers[0].update(state='connected', remote_peers=None)
        receiver._accept(message)
        self.assertEqual(receiver.stats()['p2p_status'], 'searching')

    def test_invalid_optional_tracker_data_cannot_discard_video_or_leak_urls(self):
        receiver = self.receiver()
        receiver._on_event = Mock()
        trackers = [{'url':url, 'state':'announced', 'error':'', 'remote_peers':0} for url in p2p.P2P_TRACKERS]
        trackers[0]['signed_url'] = MANIFEST
        receiver._accept_trackers(trackers)
        self.assertNotIn('token=test', str(receiver.stats()['p2p_trackers']))
        self.assertNotIn('token=test', str(receiver._on_event.call_args))
        previous = receiver.stats()['p2p_trackers']
        for invalid in (None, {}, [{}]*3, [dict(item, remote_peers=True) for item in trackers],
                        [dict(item, error=MANIFEST) for item in trackers],
                        [{k:v for k,v in item.items() if k != 'remote_peers'} for item in trackers],
                        [trackers[0]]*3):
            message = self.chunk(b'valid')
            message['trackers'] = invalid
            receiver._accept(message)
            self.assertEqual(receiver.stats()['p2p_trackers'], previous)
        self.assertEqual(receiver.get(0), b'valid')

    def test_worker_checks_player_origin_before_executing_engine_and_cleans_up(self):
        receiver = self.receiver()
        receiver._load_script.return_value = 'engine-script'
        browser = Mock()
        browser._call.side_effect = lambda method, params, *args, **kwargs: (
            {'targetId':'target'} if method == 'Target.createTarget'
            else {'sessionId':'session'} if method == 'Target.attachToTarget' else {})
        expressions = []
        def evaluate(expression):
            expressions.append(expression)
            if expression.startswith('({origin:'):
                return {'origin':'https://player.bunny-frame.online', 'ready':'interactive'}
            if expression.startswith('window.tvmonP2P.pull('):
                receiver._stop.set()
                return {'bytes':0, 'peers':0}
            return True
        with patch.object(p2p, 'TextSiteBrowser', return_value=browser), patch.object(receiver, '_evaluate', side_effect=evaluate):
            receiver._run()
        self.assertIsNone(receiver.error)
        navigation = next(call for call in browser._call.call_args_list if call.args[0] == 'Page.navigate')
        self.assertEqual(navigation.args[1]['url'], p2p.P2P_PAGE_URL)
        self.assertEqual(expressions[1], 'engine-script')
        browser.close.assert_called_once()

    def test_worker_rejects_wrong_player_origin_without_starting_engine(self):
        receiver = self.receiver()
        receiver._load_script.return_value = 'engine-script'
        browser = Mock()
        browser._call.side_effect = lambda method, *args, **kwargs: (
            {'targetId':'target'} if method == 'Target.createTarget'
            else {'sessionId':'session'} if method == 'Target.attachToTarget' else {})
        with patch.object(p2p, 'TextSiteBrowser', return_value=browser), \
                patch.object(receiver, '_evaluate', return_value={'origin':'null','ready':'complete'}) as evaluate, \
                patch.object(p2p.time, 'monotonic', side_effect=[0, 16]):
            receiver._run()
        self.assertIn('재생기 페이지', receiver.error)
        self.assertEqual(evaluate.call_count, 1)
        self.assertTrue(receiver._failed)
        browser.close.assert_called_once()

    def test_connected_peer_addresses_are_validated_and_country_queries_are_deduplicated(self):
        receiver = self.receiver()
        details = [{'id':'61', 'address':'8.8.8.8', 'state':'ready'},
                   {'id':'62', 'address':'2001:4860:4860:0:0:0:0:8888', 'state':'ready'},
                   {'id':'63', 'address':'192.168.1.2', 'state':'ready'},
                   {'id':'64', 'address':'private-name.local', 'state':'ready'},
                   {'id':'65', 'address':'', 'state':'checking'}]
        receiver._accept_peers(details + [details[0], {'id':'invalid', 'address':'8.8.4.4', 'state':'ready'}])
        self.assertEqual(receiver._country_pending, ['8.8.8.8', '2001:4860:4860::8888'])
        values = receiver.stats()['p2p_peer_details']
        self.assertEqual(len(values), 5)
        self.assertEqual(values[2]['country'], '사설·예약 주소')
        self.assertEqual((values[3]['address'], values[3]['state']), ('', 'unavailable'))
        self.assertEqual(values[4]['state'], 'checking')
        receiver._accept_peers(details)
        self.assertEqual(len(receiver._country_pending), 2)
        receiver._accept_peers([])
        self.assertEqual(receiver.stats()['p2p_peer_details'], [])
        receiver._accept_peers(details)
        self.assertEqual(len(receiver._country_pending), 2, 'Reconnect must reuse pending/cached lookups')

    def test_optional_peer_details_cannot_discard_a_valid_video_body(self):
        for invalid in (None, 'not a list', [{}], [{'id':'61','address':{},'state':'ready'}], [{}]*101):
            receiver = self.receiver()
            message = self.chunk(b'valid')
            message['peerDetails'] = invalid
            receiver._accept(message)
            self.assertEqual(receiver.get(0), b'valid')

    def test_country_response_must_match_requested_ip_and_has_a_deadline(self):
        client = Mock()
        client.get.return_value = (b'{"ip":"8.8.8.8","country":"US"}', 'https://api.country.is/8.8.8.8')
        self.assertIn('US', p2p._lookup_country('8.8.8.8', client, threading.Event()))
        self.assertEqual(client.get.call_args.args[3], 4096)
        client.reset_mock()
        self.assertEqual(p2p._lookup_country('127.0.0.1', client, threading.Event()), '사설·예약 주소')
        client.get.assert_not_called()
        for value in ({'ip':'8.8.4.4','country':'US'}, {'ip':'8.8.8.8','country':'US\n'},
                      {'ip':'8.8.8.8','country':None}):
            client.get.return_value = (json.dumps(value).encode(), 'https://api.country.is/8.8.8.8')
            with self.assertRaises(ValueError):
                p2p._lookup_country('8.8.8.8', client, threading.Event())
        client.get.side_effect = lambda *args, **kwargs: kwargs['on_wait']()
        with patch.object(p2p.time, 'monotonic', side_effect=[0, 5]), self.assertRaises(TimeoutError):
            p2p._lookup_country('8.8.8.8', client, threading.Event())

    def test_slow_country_lookup_does_not_block_peer_data_and_closes_cleanly(self):
        receiver = self.receiver()
        entered, release = threading.Event(), threading.Event()
        detail = {'id':'61', 'address':'8.8.8.8', 'state':'ready'}
        receiver._accept_peers([detail])
        client = Mock()

        def lookup(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Lookup was not released')
            return '미국 (US)'

        with patch.object(p2p, 'WinHttpClient', return_value=client), patch.object(p2p, '_lookup_country', side_effect=lookup):
            thread = threading.Thread(target=receiver._resolve_countries)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                message = self.chunk(b'video')
                message['peerDetails'] = [detail]
                receiver._accept(message)
                self.assertEqual(receiver.get(0), b'video')
                self.assertEqual(receiver.stats()['p2p_peer_details'][0]['country'], '조회 중')
            finally:
                receiver._stop.set()
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(receiver._countries['8.8.8.8'], '미국 (US)')
        client.close.assert_called_once()

    def test_bounded_messages_reassemble_before_becoming_available(self):
        receiver = self.receiver()
        first = b"x"*(1024*1024)
        receiver._accept(self.chunk(first, total=len(first)+7))
        self.assertEqual(receiver._buffered, 0)
        receiver._accept(self.chunk(b"ending!", offset=len(first), total=len(first)+7, received=len(first)+7))
        self.assertEqual(receiver.get(0), first+b"ending!")
        self.assertEqual(receiver._buffered, 0)
        self.assertIn(0, receiver._finished)
        self.assertEqual(sum(call.args[0] for call in receiver._on_bytes.call_args_list), len(first)+7)
        receiver._on_status.assert_called_once()

    def test_resume_skips_complete_parts_but_late_peer_data_can_replace_slow_http(self):
        receiver = self.receiver(completed=(1,))
        receiver._accept(self.chunk(b"old", index=1))
        self.assertEqual(receiver._buffered, 0)
        receiver._peers = 0
        self.assertIsNone(receiver.get(0))
        receiver._accept(self.chunk(b"late", index=0, received=7))
        self.assertEqual(receiver.get(0, wait=False), b"late")
        self.assertNotIn(0, receiver._remaining)
        receiver.finish(2)
        self.assertNotIn(2, receiver._remaining)

    def test_peer_discovery_and_unavailable_segments_never_hold_an_http_worker(self):
        receiver = self.receiver()
        with patch.object(receiver._condition, 'wait') as wait:
            self.assertIsNone(receiver.get(0))
            receiver._peers = 3
            self.assertIsNone(receiver.get(0))
            wait.assert_not_called()

    def test_only_a_currently_arriving_peer_segment_gets_a_bounded_wait(self):
        receiver = self.receiver()
        receiver._peers = 2
        receiver._active.add(0)
        started = time.monotonic()
        with patch.object(p2p, 'PEER_WAIT_SECONDS', .025):
            self.assertIsNone(receiver.get(0))
        self.assertGreaterEqual(time.monotonic()-started, .02)
        self.assertLess(time.monotonic()-started, .5)

    def test_failure_keeps_complete_buffer_but_new_requests_fall_back_immediately(self):
        receiver = self.receiver()
        receiver._accept(self.chunk(b"valid-body"))
        receiver._failed = True
        self.assertEqual(receiver.get(0), b"valid-body")
        self.assertIsNone(receiver.get(1))

    def test_cancellation_returns_immediately_and_releases_partial_body(self):
        receiver = self.receiver()
        receiver._accept(self.chunk(b"part", total=8))
        receiver._cancel.set()
        self.assertIsNone(receiver.get(0))
        self.assertIsNone(receiver._assembling)

    def test_malformed_or_out_of_order_chunks_are_rejected(self):
        values = [self.chunk(b"x", index=-1), self.chunk(b"x", total=p2p.MAX_BUFFER+1),
                  self.chunk(b"x", offset=1, total=2), self.chunk(b"xx", total=1),
                  {"bytes": -1, "peers": 0}, {"bytes": 0, "peers": 101},
                  {"bytes": 1, "peers": 0, "chunk": {"index":0,"offset":0,"total":1,"data":"?"}}]
        for value in values:
            with self.subTest(value=str(value)[:100]):
                receiver = self.receiver()
                with self.assertRaises((TransferError, ValueError)):
                    receiver._accept(value)

    def test_python_receive_buffer_is_bounded(self):
        receiver = self.receiver()
        receiver._buffered = p2p.MAX_BUFFER
        with self.assertRaises(TransferError):
            receiver._accept(self.chunk(b"x"))

    def test_map_status_includes_peer_prefetch_and_buffered_parts_until_taken(self):
        receiver = self.receiver(completed=(1,))
        value = self.chunk(b"part", total=8)
        value['active'] = [1, 2]
        receiver._accept(value)
        self.assertEqual(receiver.stats()['p2p_active_indices'], [0, 2])
        self.assertEqual(receiver.stats()['p2p_parts'], [
            {'index':0, 'state':'transferring', 'bytes':4, 'total':8},
            {'index':2, 'state':'receiving', 'bytes':0}])
        receiver._accept(self.chunk(b"done", offset=4, total=8, received=8))
        self.assertEqual(receiver.stats()['p2p_active_indices'], [0])
        self.assertEqual(receiver.stats()['p2p_parts'], [{'index':0, 'state':'ready', 'bytes':8}])
        self.assertEqual(receiver.get(0), b'partdone')
        self.assertEqual(receiver.stats()['p2p_active_indices'], [])
        self.assertEqual(receiver.stats()['p2p_parts'], [])

    def test_map_status_rejects_invalid_peer_segment_numbers(self):
        receiver = self.receiver()
        for active in ([True], [-1], [3], '0'):
            with self.subTest(active=active), self.assertRaises(TransferError):
                receiver._accept({'bytes': 0, 'peers': 1, 'active': active, 'chunk': None})

    def test_script_failure_wakes_http_fallback(self):
        receiver = self.receiver()
        receiver._load_script.side_effect = TransferError("offline")
        receiver._run()
        self.assertTrue(receiver._failed)
        self.assertIsNone(receiver.get(0))
        receiver._on_status.assert_called_once()

    def test_http_claim_waits_for_ack_and_completion_wakes_receiver(self):
        receiver = self.receiver()
        receiver._ready = True
        returned = threading.Event()
        thread = threading.Thread(target=lambda: (receiver.claim_http(0), returned.set()))
        thread.start()
        try:
            self.assertTrue(receiver._wake.wait(1))
            self.assertFalse(returned.is_set(), "HTTP must wait for ownership acknowledgement")
            with receiver._condition:
                self.assertEqual(receiver._http, {0})
                receiver._http_acknowledged.add(0)
                receiver._condition.notify_all()
            self.assertTrue(returned.wait(1))
            receiver._wake.clear()
            receiver.release_http(0)
            self.assertTrue(receiver._wake.is_set())
            self.assertNotIn(0, receiver._http)
            self.assertIn(0, receiver._remaining, 'Releasing HTTP must allow peer rescue without marking completion')
            receiver._wake.clear()
            receiver.finish(0)
            self.assertTrue(receiver._wake.is_set())
            self.assertNotIn(0, receiver._http)
            self.assertNotIn(0, receiver._http_acknowledged)
        finally:
            receiver._stop.set()
            with receiver._condition:
                receiver._condition.notify_all()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_http_claim_before_startup_is_nonblocking_and_failure_or_cancel_releases_wait(self):
        receiver = self.receiver()
        receiver.claim_http(0)
        self.assertEqual(receiver._http, {0})
        self.assertTrue(receiver._wake.is_set())
        for cancelled in (False, True):
            receiver = self.receiver()
            receiver._ready = True
            returned = threading.Event()
            thread = threading.Thread(target=lambda: (receiver.claim_http(1), returned.set()))
            thread.start()
            try:
                self.assertTrue(receiver._wake.wait(1))
                with receiver._condition:
                    if cancelled:
                        receiver._cancel.set()
                    else:
                        receiver._failed = True
                    receiver._condition.notify_all()
                self.assertTrue(returned.wait(1))
            finally:
                receiver._stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_profile_cleanup_retries_windows_file_lock_without_failing_download(self):
        receiver = self.receiver()
        temporary = Mock(name=str(Path(tempfile.gettempdir()) / 'tvmon-p2p-test'))
        temporary.name = str(Path(tempfile.gettempdir()) / 'tvmon-p2p-test')
        temporary.cleanup.side_effect = [NotADirectoryError('mapped file is still closing'), None]
        with patch.object(p2p.time, 'sleep'):
            receiver._cleanup_profile(temporary)
        self.assertEqual(temporary.cleanup.call_count, 2)
        self.assertIsNone(receiver.cleanup_error)
        self.assertIsNone(receiver.error)
        receiver._on_status.assert_not_called()

    def test_profile_cleanup_does_not_remove_paths_outside_its_temp_directory(self):
        receiver = self.receiver()
        temporary = Mock()
        temporary.name = str(Path(tempfile.gettempdir()).parent / 'tvmon-p2p-test')
        receiver._cleanup_profile(temporary)
        temporary.cleanup.assert_not_called()
        self.assertIsNotNone(receiver.cleanup_error)

    def diagnostic_sample(self):
        return {'state':'ready', 'anchor':0, 'requested_anchor':0, 'anchor_registered':True, 'anchor_failures':0,
                'queue_processing_pending':False, 'engine_peers':2, 'idle_peers':1, 'engine_p2p_running':0,
                'p2p_limit':5, 'queue_length':1, 'pending_js_parts':0, 'cache_used_bytes':61*1024*1024,
                'cache_limit_bytes':64*1024*1024, 'cache_total_bytes':62*1024*1024, 'cache_released_bytes':0, 'pending_js_bytes':0,
                'budget_bytes':p2p.MAX_BUFFER, 'cache_free_percent':4.6875, 'effective_p2p_window':0,
                'last_queue_age_seconds':.5, 'pieces':[{'index':1, 'queued':False, 'high_demand':False,
                'in_p2p_window':False, 'cached':False, 'loaded_peers':1, 'loading_peers':0,
                'idle_loaded_peers':1, 'request_state':'none', 'reason':'outside_effective_window'}]}

    def test_diagnostics_explain_inventory_queue_and_storage_without_signed_urls(self):
        receiver = self.receiver()
        receiver._on_event = Mock()
        sample = self.diagnostic_sample()
        sample['signed_url'] = MANIFEST
        sample['pieces'][0]['runtimeId'] = MANIFEST
        message = self.chunk(b'valid')
        message['diagnostics'] = sample
        receiver._accept(message)
        event = receiver._on_event.call_args
        self.assertEqual(event.args, ('p2p_diagnostics',))
        self.assertEqual(event.kwargs['anchor_segment'], 1)
        self.assertEqual(event.kwargs['pieces'][0]['segment'], 2)
        self.assertEqual(event.kwargs['pieces'][0]['loaded_peers'], 1)
        self.assertEqual(event.kwargs['pieces'][0]['reason'], 'outside_effective_window')
        self.assertEqual(event.kwargs['cache_free_percent'], 4.6875)
        self.assertNotIn('token=test', str(event))
        self.assertEqual(receiver.get(0), b'valid')

    def test_invalid_optional_diagnostics_do_not_discard_valid_video(self):
        broken = [[], {'state':'unknown'}]
        sample = self.diagnostic_sample()
        sample['cache_free_percent'] = float('nan')
        broken.append(sample)
        sample = self.diagnostic_sample()
        sample['pieces'][0]['reason'] = ['not a reason']
        broken.append(sample)
        sample = self.diagnostic_sample()
        sample['effective_p2p_window'] = 10**400
        broken.append(sample)
        for sample in broken:
            with self.subTest(sample=str(sample)[:120]):
                receiver = self.receiver()
                receiver._on_event = Mock()
                message = self.chunk(b'valid')
                message['diagnostics'] = sample
                receiver._accept(message)
                self.assertEqual(receiver.get(0), b'valid')
                receiver._on_event.assert_called_once_with('p2p_diagnostics', state='invalid')

    def test_released_cache_counter_can_exceed_live_buffer_limits(self):
        receiver = self.receiver()
        receiver._on_event = Mock()
        sample = self.diagnostic_sample()
        sample['cache_released_bytes'] = 3 * 1024 * 1024 * 1024
        message = self.chunk(b'valid')
        message['diagnostics'] = sample
        receiver._accept(message)
        self.assertEqual(receiver._on_event.call_args.kwargs['cache_released_bytes'], 3 * 1024**3)
        self.assertEqual(receiver.get(0), b'valid')

    def test_invalid_released_cache_counter_does_not_discard_video(self):
        for value in (-1, True, 2**53, float('nan')):
            with self.subTest(value=value):
                receiver = self.receiver()
                receiver._on_event = Mock()
                sample = self.diagnostic_sample()
                sample['cache_released_bytes'] = value
                message = self.chunk(b'valid')
                message['diagnostics'] = sample
                receiver._accept(message)
                receiver._on_event.assert_called_once_with('p2p_diagnostics', state='invalid')
                self.assertEqual(receiver.get(0), b'valid')

    def test_unavailable_engine_inspection_is_explicit_and_cannot_leak_error_urls(self):
        for state in ('initializing', 'unsupported', 'error'):
            with self.subTest(state=state):
                receiver = self.receiver()
                receiver._on_event = Mock()
                message = self.chunk(b'valid')
                message['diagnostics'] = {'state':state, 'error':MANIFEST}
                receiver._accept(message)
                receiver._on_event.assert_called_once_with('p2p_diagnostics', state=state)
                self.assertEqual(receiver.get(0), b'valid')


if __name__ == "__main__":
    unittest.main()

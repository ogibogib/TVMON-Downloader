"""Receive site-compatible P2P segments in an isolated installed-Chrome worker."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

from tvmon_browser import TextSiteBrowser
from tvmon_http import TransferError, WinHttpClient


P2P_SCRIPT_URL = "https://player.bunny-frame.online/player/assets/p2p-media-loader-hlsjs.iife.min.js"
P2P_PAGE_URL = "https://player.bunny-frame.online/player/"
P2P_TRACKERS = ("wss://t.peertrk.com", "wss://tracker.webtorrent.dev", "wss://tracker.openwebtorrent.com")
MAX_BUFFER = 32 * 1024 * 1024
PEER_WAIT_SECONDS = 2


def _lookup_country(address, client, cancel):
    """Look up only a public numeric IP, with a small response and deadline."""
    address = str(ipaddress.ip_address(address))
    if not ipaddress.ip_address(address).is_global or '%' in address:
        return "사설·예약 주소"
    deadline = time.monotonic() + 4

    def check_deadline(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError()

    body, _ = client.get("https://api.country.is/" + address, {"Accept": "application/json"},
                         cancel, 4096, check_deadline, on_wait=check_deadline)
    value = json.loads(body)
    if (str(ipaddress.ip_address(value.get("ip", ""))) != address
            or not isinstance(value.get("country"), str) or not re.fullmatch(r"[A-Z]{2}", value["country"])):
        raise ValueError("Invalid country response")
    code = value["country"]
    try:
        # Use Windows' country names in the user's display language.
        lookup = ctypes.WinDLL("kernel32", use_last_error=True).GetGeoInfoEx
        lookup.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int]
        lookup.restype = ctypes.c_int
        name = ctypes.create_unicode_buffer(128)
        if lookup(code, 8, name, len(name)):
            return f"{name.value} ({code})"
    except (AttributeError, OSError):
        pass
    return code


def video_swarm_id(manifest_url, segments):
    """Only opt into the site's known, single-quality VOD identity scheme."""
    parsed = urlsplit(manifest_url)
    match = re.fullmatch(r"/v/([a-z]+)/([0-9a-f]{32})/c\.html", parsed.path)
    if (parsed.scheme != "https" or not re.fullmatch(r"every[0-9]+\.poorcdn\.com", parsed.hostname or "")
            or not match or not segments):
        return None
    prefix = f"/v/{match[1]}/{match[2]}/"
    if any(not urlsplit(item[0]).path.startswith(prefix) for item in segments):
        return None
    return f"tvw-v5-{match[1]}-{match[2]}"


_WORKER_JS = r"""function(config) {
  const LIMIT = 32 * 1024 * 1024;
  const trackers = new Map(config.trackers.map(url => [url,
    {url, state: 'connecting', error: '', remote_peers: null}]));
  const sockets = new Map(), NativeWebSocket = window.WebSocket;
  // Observe only the engine's configured trackers; leave their traffic intact.
  window.WebSocket = new Proxy(NativeWebSocket, {construct(target, args, newTarget) {
    const socket = Reflect.construct(target, args, newTarget);
    const url = new URL(args[0]).href.replace(/\/$/, ''), tracker = trackers.get(url);
    if (!tracker) return socket;
    sockets.set(url, socket);
    tracker.state = 'connecting';
    tracker.remote_peers = null;
    const update = (state, error = '') => {
      if (sockets.get(url) === socket) Object.assign(tracker, {state, error});
    };
    socket.addEventListener('open', () => update('connected'));
    socket.addEventListener('error', () => update('error', 'connection_failed'));
    socket.addEventListener('close', () => {
      if (sockets.get(url) === socket && tracker.state !== 'error') update('error', 'disconnected');
    });
    socket.addEventListener('message', event => {
      if (sockets.get(url) !== socket || typeof event.data !== 'string' || event.data.length > 131072) return;
      try {
        const value = JSON.parse(event.data);
        if (value['failure reason']) { update('error', 'announce_rejected'); return; }
        if (value.action !== 'announce' || value.info_hash !== core.getStreams()[0]?.infoHash) return;
        update('announced');
        if (Number.isSafeInteger(value.complete) && value.complete >= 0
            && Number.isSafeInteger(value.incomplete) && value.incomplete >= 0) {
          tracker.remote_peers = Math.max(0, value.complete + value.incomplete - 1);
        }
        if (value.offer || value.answer) tracker.remote_peers = Math.max(1, tracker.remote_peers || 0);
      } catch (_) { /* Optional tracker inspection must not interrupt the engine. */ }
    });
    return socket;
  }});
  // Associate the engine's public data channels with their WebRTC connections.
  // Wrappers preserve the native return value and never await inspection work.
  const connections = new WeakMap(), watched = new WeakSet(), peerInfo = new WeakMap();
  const rtc = window.RTCPeerConnection?.prototype;
  if (typeof rtc?.createDataChannel === 'function' && typeof rtc?.setRemoteDescription === 'function') {
    const createChannel = rtc.createDataChannel, setRemote = rtc.setRemoteDescription;
    rtc.createDataChannel = function(...args) {
      const channel = createChannel.apply(this, args);
      connections.set(channel, this);
      return channel;
    };
    rtc.setRemoteDescription = function(...args) {
      if (!watched.has(this)) {
        watched.add(this);
        this.addEventListener('datachannel', event => connections.set(event.channel, this));
      }
      return setRemote.apply(this, args);
    };
  }
  const done = new Set(config.completed);
  let http = new Set(config.http || []);
  const byUrl = new Map(config.segments.map((s, i) => [s.url, i]));
  const pending = new Map();
  const active = new Set();
  const events = [], attempts = new Map();
  const stats = {bytes: 0, peers: 0};
  let droppedEvents = 0;
  function record(kind, index, detail) {
    if (events.length >= 128) { droppedEvents++; return; }
    const now = performance.now();
    events.push({kind, index, bytes: detail.bytesLength || 0,
      seconds: kind === 'start' ? 0 : (now - (attempts.get(index) ?? now)) / 1000,
      error: String(detail.error?.type || detail.error?.name || '').slice(0, 80),
      peer: String(detail.peerId || '').slice(0, 96)});
  }
  let buffered = 0, cursor = 0, lastAnchor = -1, anchorFailures = 0, registered = new Set();
  let releasedCacheBytes = 0;
  let position = 0;
  const segments = config.segments.map((s, i) => {
    const start = position; position += s.duration;
    return {runtimeId: s.url, url: s.url, externalId: i, startTime: start, endTime: position};
  });
  const core = new p2pml.hlsjs.Core({
    swarmId: config.swarmId,
    announceTrackers: config.trackers,
    simultaneousHttpDownloads: 0, simultaneousP2PDownloads: 5,
    highDemandTimeWindow: 15, httpDownloadTimeWindow: 0, p2pDownloadTimeWindow: position,
    p2pNotReceivingBytesTimeoutMs: 2500, p2pMaxPeers: 15,
    segmentMemoryStorageLimit: 64, isP2PUploadDisabled: true,
    validateP2PSegment: (url, range, data) => {
      const index = byUrl.get(url);
      if (index === undefined || done.has(index) || http.has(index) || !registered.has(index)) return false;
      if (!data || !data.byteLength || data.byteLength > LIMIT) return false;
      if (pending.has(index)) return true;
      if (buffered + data.byteLength > LIMIT) return false;
      pending.set(index, {bytes: new Uint8Array(data).slice(), offset: 0});
      buffered += data.byteLength;
      return true;
    }
  });
  // The site's playback queue stops at an unavailable segment and preempts later
  // requests for playback deadlines. File downloads need all registered pieces.
  const createLoader = core.createNewHybridLoader;
  if (typeof createLoader !== 'function') throw new Error('Unsupported P2P download queue');
  core.createNewHybridLoader = function(segment) {
    const loader = createLoader.call(this, segment);
    if (typeof loader.generateQueue !== 'function'
        || typeof loader.loadThroughP2P !== 'function'
        || typeof loader.processQueue !== 'function'
        || typeof loader.p2pLoaders?.currentLoader?.isSegmentLoadingOrLoadedBySomeone !== 'function') {
      throw new Error('Unsupported P2P download queue');
    }
    const loadThroughP2P = loader.loadThroughP2P;
    loader.loadThroughP2P = function(segment) {
      // The engine can also start its playback anchor outside generateQueue.
      if (!http.has(segment.externalId) && !done.has(segment.externalId)) {
        return loadThroughP2P.call(this, segment);
      }
    };
    loader.generateQueue = function() {
      const queue = [], queueSegmentIds = new Set();
      let maxPossibleLength = 0, alreadyLoadedCount = 0;
      const free = this.getAvailableStorageCapacityPercent();
      const seconds = free <= 5 ? 0 : free <= 10 ? this.config.httpDownloadTimeWindow : this.config.p2pDownloadTimeWindow;
      for (const segment of this.lastRequestedSegment.stream.segments.values()) {
        const index = segment.externalId;
        if (!registered.has(index) || done.has(index) || http.has(index)) continue;
        maxPossibleLength++;
        if (this.segmentStorage.hasSegment(segment.stream.swarmId, segment.stream.streamSwarmId, index)
            || this.requests.get(segment)?.status === 'succeed') {
          alreadyLoadedCount++;
          continue;
        }
        const inWindow = !(this.playback.position + seconds * this.playback.rate < segment.startTime
                           || this.playback.position > segment.endTime);
        queue.push({segment, statuses: {isHighDemand: false, isHttpDownloadable: false,
          isP2PDownloadable: inWindow && this.p2pLoaders.currentLoader.isSegmentLoadingOrLoadedBySomeone(segment)}});
        // Keep in-flight requests even if a peer's next announcement omits them.
        queueSegmentIds.add(segment.runtimeId);
      }
      return {queue, queueSegmentIds, maxPossibleLength, alreadyLoadedCount,
        queueDownloadRatio: maxPossibleLength ? alreadyLoadedCount / maxPossibleLength : 0};
    };
    return loader;
  };
  core.addEventListener('onChunkDownloaded', (bytes, source) => { if (source === 'p2p') stats.bytes += bytes; });
  core.addEventListener('onPeerConnect', () => stats.peers++);
  core.addEventListener('onPeerClose', () => stats.peers = Math.max(0, stats.peers - 1));
  core.addEventListener('onTrackerError', detail => {
    const tracker = trackers.get(String(detail.trackerUrl).replace(/\/$/, ''));
    if (tracker) Object.assign(tracker, {state: 'error', error: 'connection_failed'});
  });
  core.addEventListener('onSegmentStart', detail => {
    const index = byUrl.get(detail.segment?.url);
    if (detail.downloadSource === 'p2p' && index !== undefined && !done.has(index)) {
      active.add(index);
      attempts.set(index, performance.now());
      record('start', index, detail);
    }
  });
  for (const event of ['onSegmentLoaded', 'onSegmentError', 'onSegmentAbort']) {
    core.addEventListener(event, detail => {
      const index = byUrl.get(detail.segment?.url);
      active.delete(index);
      if (detail.downloadSource === 'p2p' && index !== undefined) {
        record(event === 'onSegmentLoaded' ? 'loaded' : event === 'onSegmentError' ? 'error' : 'abort', index, detail);
        attempts.delete(index);
      }
    });
  }
  core.setManifestResponseUrl(config.manifestUrl);
  core.addStreamIfNoneExists({runtimeId: config.manifestUrl, type: 'main', properties: {bitrate: 0}});
  if (!core.getStreams().length) throw new Error('P2P stream registration failed');
  core.setIsLive(false);
  function refresh() {
    while (done.has(cursor)) cursor++;
    const next = new Set();
    for (let i = cursor; i < segments.length && next.size < 32; i++) {
      if (!done.has(i)) next.add(i);
    }
    const remove = [...registered].filter(i => !next.has(i)).map(i => segments[i].url);
    const add = [...next].filter(i => !registered.has(i)).map(i => segments[i]);
    core.updateStream(config.manifestUrl, add, remove);
    registered = next;
    if (cursor !== lastAnchor && cursor < segments.length) {
      lastAnchor = cursor;
      core.updatePlayback(segments[cursor].startTime, 1);
      const anchor = cursor;
      core.loadSegment(segments[anchor].url, {onSuccess: () => {}, onError: () => {}}).catch(error => {
        anchorFailures++;
        record('anchor_error', anchor, {error: {type: String(error?.message || '').startsWith('Not found segment with id:')
          ? 'unregistered_segment' : 'engine_request_error'}});
      });
    }
  }
  refresh();
  function releaseCompletedCache() {
    const loader = core.mainStreamLoader;
    if (!loader) return;
    const storage = loader.segmentStorage;
    if (!(storage.cache instanceof Map) || typeof storage.decreaseStorageUsage !== 'function'
        || typeof storage.sendUpdatesToAffectedStreams !== 'function') {
      throw new Error('Unsupported P2P cache release');
    }
    const streamId = loader.lastRequestedSegment.stream.streamSwarmId;
    let released = 0;
    for (const [key, entry] of storage.cache) {
      if (entry.streamSwarmId !== streamId || !done.has(entry.segmentId)) continue;
      released += entry.data.byteLength;
      storage.cache.delete(key);
    }
    if (released) {
      storage.decreaseStorageUsage(released);
      releasedCacheBytes += released;
      storage.sendUpdatesToAffectedStreams(new Set([streamId]));
    }
  }
  let lastDiagnostics = -Infinity;
  function connectedPeers() {
    try {
    const manager = core.mainStreamLoader?.p2pLoaders?.currentLoader;
    if (typeof manager?.peers !== 'function') return [];
    return [...manager.peers()].slice(0, 100).map(peer => {
      let info = peerInfo.get(peer);
      if (!info) {
        info = {address: '', state: 'checking', updated: -Infinity, pending: false};
        peerInfo.set(peer, info);
      }
      if (!info.pending && performance.now() - info.updated >= 2000) {
        info.pending = true;
        info.updated = performance.now();
        const connection = connections.get(peer.channel);
        Promise.resolve().then(() => connection?.getStats()).then(report => {
          let pair;
          if (report) {
            for (const item of report.values()) {
              if (item.type === 'transport' && item.selectedCandidatePairId) {
                pair = report.get(item.selectedCandidatePairId); break;
              }
            }
            if (!pair) pair = [...report.values()].find(item =>
              item.type === 'candidate-pair' && item.state === 'succeeded' && item.nominated);
          }
          const candidate = pair && report.get(pair.remoteCandidateId);
          info.address = typeof candidate?.address === 'string' ? candidate.address.slice(0, 128) : '';
          info.state = info.address ? 'ready' : 'unavailable';
        }).catch(() => { info.address = ''; info.state = 'unavailable'; })
          .finally(() => { info.pending = false; });
      }
      return {id: [...String(peer.id)].map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join('').slice(0, 192),
        address: info.address, state: info.state};
    });
    } catch (_) { return []; }
  }
  function diagnose(budget) {
    const loader = core.mainStreamLoader;
    if (!loader) return {state: 'initializing'};
    // Read-only inspection of the site's engine; missing internals must not stop receiving.
    try {
      const manager = loader.p2pLoaders?.currentLoader;
      if (!manager || typeof manager.peers !== 'function' || typeof loader.generateQueue !== 'function'
          || typeof loader.segmentStorage?.getUsage !== 'function') return {state: 'unsupported'};
      const peers = [...manager.peers()];
      const queue = loader.generateQueue();
      const queued = new Set(queue.queue.map(item => item.segment.externalId));
      const usage = loader.segmentStorage.getUsage();
      const free = loader.getAvailableStorageCapacityPercent();
      const settings = loader.config, playback = loader.playback;
      const p2pWindow = free <= 5 ? 0 : free <= 10 ? settings.httpDownloadTimeWindow : settings.p2pDownloadTimeWindow;
      const httpWindow = free <= 5 ? 0 : settings.httpDownloadTimeWindow;
      const inWindow = (segment, seconds) => !(playback.position + seconds * playback.rate < segment.startTime
                                               || playback.position > segment.endTime);
      const anchorRegistered = loader.lastRequestedSegment.stream.segments.has(loader.lastRequestedSegment.runtimeId);
      const pieces = [...registered].map(index => {
        const segment = loader.lastRequestedSegment.stream.segments.get(segments[index].url);
        let loaded = 0, loading = 0, idleLoaded = 0;
        if (segment) for (const peer of peers) {
          const status = peer.getSegmentStatus(segment);
          if (status === 'loaded') { loaded++; if (!peer.downloadingSegment) idleLoaded++; }
          else if (status === 'http-loading') loading++;
        }
        const request = segment ? loader.requests.get(segment) : undefined;
        const highDemand = false; // The download queue has no playback deadline.
        const inP2PWindow = !!segment && inWindow(segment, p2pWindow);
        const cached = !!segment && loader.segmentStorage.hasSegment(segment.stream.swarmId, segment.stream.streamSwarmId, segment.externalId);
        let reason;
        if (!segment) reason = 'not_registered';
        else if (pending.has(index)) reason = 'awaiting_transfer';
        else if (http.has(index)) reason = 'http_owned';
        else if (request?.status === 'loading') reason = 'receiving';
        else if (cached) reason = 'engine_cached';
        else if (!peers.length) reason = 'no_peers';
        else if (!highDemand && !inP2PWindow && !inWindow(segment, httpWindow)) reason = 'outside_effective_window';
        else if (!highDemand && !loaded && !loading) reason = 'not_advertised';
        else if (!queued.has(index)) reason = anchorRegistered ? 'behind_queue_boundary' : 'anchor_not_registered';
        else if (!loaded) reason = loading ? 'peer_still_loading' : 'not_advertised';
        else if (!idleLoaded) reason = 'peer_busy';
        else if (loader.requests.executingP2PCount >= settings.simultaneousP2PDownloads) reason = 'concurrency_limit';
        else reason = 'eligible_not_requested';
        return {index, queued: queued.has(index), high_demand: highDemand, in_p2p_window: inP2PWindow, cached,
          loaded_peers: loaded, loading_peers: loading, idle_loaded_peers: idleLoaded,
          request_state: request?.status || 'none', reason};
      });
      return {state: 'ready', anchor: loader.lastRequestedSegment.externalId, requested_anchor: lastAnchor,
        anchor_registered: anchorRegistered, anchor_failures: anchorFailures,
        queue_processing_pending: !!loader.isProcessQueueMicrotaskCreated,
        engine_peers: peers.length, idle_peers: peers.filter(peer => !peer.downloadingSegment).length,
        engine_p2p_running: loader.requests.executingP2PCount, p2p_limit: settings.simultaneousP2PDownloads,
        cache_used_bytes: Math.round(usage.usedCapacity * 1024 * 1024),
        cache_limit_bytes: Math.round(usage.totalCapacity * 1024 * 1024),
        cache_total_bytes: Math.round(loader.segmentStorage.currentStorageUsage * 1024 * 1024),
        cache_released_bytes: releasedCacheBytes,
        cache_free_percent: free, effective_p2p_window: p2pWindow, queue_length: queue.queue.length,
        last_queue_age_seconds: loader.lastQueueProcessingTimeStamp === undefined ? null :
          (performance.now() - loader.lastQueueProcessingTimeStamp) / 1000,
        pending_js_parts: pending.size, pending_js_bytes: buffered, budget_bytes: budget, pieces};
    } catch (_) { return {state: 'error'}; }
  }
  window.tvmonP2P = {
    pull: (finished, budget, httpIndices = []) => {
      // Sample before acknowledging completions: refresh schedules engine microtasks that have not run yet.
      let diagnostics;
      if (config.diagnostics && performance.now() - lastDiagnostics >= 2000) {
        diagnostics = diagnose(budget);
        lastDiagnostics = performance.now();
      }
      for (const index of finished) {
        done.add(index);
        active.delete(index);
        const value = pending.get(index);
        if (value) { buffered -= value.bytes.length; pending.delete(index); }
      }
      // Also collect results cached after an earlier completion acknowledgement.
      releaseCompletedCache();
      if (finished.length) refresh();
      const changed = http.size !== httpIndices.length || httpIndices.some(index => !http.has(index));
      http = new Set(httpIndices);
      // Acknowledge HTTP ownership only after the engine has cancelled competing
      // requests. Its queue also excludes these pieces on future peer announcements.
      if (changed && core.mainStreamLoader) {
        const loader = core.mainStreamLoader;
        for (const index of http) {
          const segment = loader.lastRequestedSegment.stream.segments.get(segments[index].url);
          const request = segment && loader.requests.get(segment);
          if (request?.status === 'loading' && request.downloadSource === 'p2p') {
            request.cancel();
            loader.requests.remove(request);
          }
        }
        loader.processQueue();
      }
      const result = {...stats, active: [...new Set([...active, ...pending.keys()])], window: [...registered],
        events: events.splice(0), droppedEvents, chunk: null, peerDetails: connectedPeers(),
        trackers: [...trackers.values()].map(item => ({...item}))};
      if (diagnostics) result.diagnostics = diagnostics;
      const entry = pending.entries().next().value;
      if (entry && entry[1].bytes.length <= budget) {
        const [index, value] = entry;
        const end = Math.min(value.offset + 1024 * 1024, value.bytes.length);
        let text = '';
        for (let i = value.offset; i < end; i += 32768) {
          text += String.fromCharCode(...value.bytes.subarray(i, Math.min(i + 32768, end)));
        }
        result.chunk = {index, offset: value.offset, total: value.bytes.length, data: btoa(text)};
        value.offset = end;
        if (end === value.bytes.length) { buffered -= value.bytes.length; pending.delete(index); }
      }
      return result;
    },
    close: () => { core.destroy(); pending.clear(); window.WebSocket = NativeWebSocket; }
  };
  return true;
}"""


class P2PVideoReceiver:
    """One browser thread prefetches; existing download workers take complete bodies."""

    def __init__(self, manifest_url, segments, completed, *, cancel, load_script, on_bytes, on_status, on_event=None):
        self._config = {"manifestUrl": manifest_url, "swarmId": video_swarm_id(manifest_url, segments),
                        "segments": [{"url": item[0], "duration": item[3]} for item in segments],
                        "completed": sorted(completed), "diagnostics": on_event is not None,
                        "trackers": list(P2P_TRACKERS)}
        if not self._config["swarmId"]:
            raise ValueError("Unsupported P2P video identity")
        self._cancel = cancel
        self._load_script = load_script
        self._on_bytes = on_bytes
        self._on_status = on_status
        self._on_event = on_event
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._condition = threading.Condition()
        self._remaining = set(range(len(segments))) - set(completed)
        self._finished = set()
        self._http = set()
        self._http_acknowledged = set()
        self._ready = False
        self._bodies = {}
        self._buffered = 0
        self._assembling = None
        self._bytes = 0
        self._peers = 0
        self._peer_details = []
        self._trackers = []
        self._countries = {}
        self._country_pending = []
        self._active = set()
        self._window = []
        self._dropped_events = 0
        self._connected = False
        self._failed = False
        self.error = None
        self.cleanup_error = None
        self._browser = None
        self._country_thread = threading.Thread(target=self._resolve_countries, name="tvmon-peer-countries", daemon=True)
        self._country_thread.start()
        self._thread = threading.Thread(target=self._run, name="tvmon-p2p", daemon=True)
        self._thread.start()

    def _discard(self, index):
        body = self._bodies.pop(index, None)
        if body is not None:
            self._buffered -= len(body)
        self._remaining.discard(index)
        self._finished.add(index)
        self._http.discard(index)
        self._http_acknowledged.discard(index)
        if self._assembling and self._assembling[0] == index:
            self._assembling = None
        self._condition.notify_all()
        self._wake.set()

    def get(self, index, *, wait=True):
        """Wait only for a segment already arriving; peer discovery never blocks HTTP."""
        with self._condition:
            arriving = index in self._active or (self._assembling and self._assembling[0] == index)
            deadline = time.monotonic() + (PEER_WAIT_SECONDS if wait and arriving else 0)
            while True:
                if self._cancel.is_set() or self._stop.is_set():
                    self._discard(index)
                    return None
                body = self._bodies.get(index)
                if body is not None:
                    self._discard(index)
                    return body
                remaining = deadline - time.monotonic()
                if self._failed or index not in self._remaining or remaining <= 0:
                    return None
                self._condition.wait(min(.1, remaining))

    def finish(self, index):
        with self._condition:
            self._discard(index)

    def claim_http(self, index):
        """Exclude HTTP work from the peer queue before starting its request."""
        with self._condition:
            if index not in self._remaining or self._failed:
                return
            self._http.add(index)
            self._wake.set()
            # Before startup, the initial JS configuration receives these claims.
            # Once ready, a pull must apply the claim before HTTP can proceed.
            while (self._ready and index not in self._http_acknowledged
                   and not self._failed and not self._stop.is_set() and not self._cancel.is_set()):
                self._condition.wait(.1)

    def release_http(self, index):
        """Let peers rescue a slow HTTP request; the first verified body wins."""
        with self._condition:
            if index in self._http:
                self._http.remove(index)
                self._http_acknowledged.discard(index)
                self._wake.set()

    def stats(self):
        with self._condition:
            active = self._active | self._bodies.keys()
            parts = {index: {"index": index, "state": "receiving", "bytes": 0} for index in active & self._remaining}
            for index, body in self._bodies.items():
                if index in self._remaining:
                    parts[index] = {"index": index, "state": "ready", "bytes": len(body)}
            if self._assembling:
                index, total, body = self._assembling
                active.add(index)
                if index in self._remaining:
                    parts[index] = {"index": index, "state": "transferring", "bytes": len(body), "total": total}
            if self._failed:
                status = "unavailable"
            elif self._peers:
                status = "connected"
            elif not self._ready:
                status = "initializing"
            elif any(item["error"] for item in self._trackers):
                status = "tracker_error" if all(item["error"] for item in self._trackers) else "tracker_partial"
            elif self._trackers and all(item["state"] == "announced" for item in self._trackers):
                status = ("no_peers" if all(item["remote_peers"] == 0 for item in self._trackers)
                          else "peer_wait" if any(item["remote_peers"] for item in self._trackers) else "searching")
            else:
                status = "searching"
            return {"p2p_bytes": self._bytes, "p2p_peers": self._peers,
                    "p2p_peer_details": [{**item, "country": self._countries.get(item["address"], "조회 불가")}
                                         for item in self._peer_details],
                    "p2p_status": status, "p2p_trackers": [dict(item) for item in self._trackers],
                    "p2p_active_indices": sorted(active & self._remaining),
                    "p2p_window": list(self._window), "p2p_buffered_bytes": self._buffered,
                    "p2p_dropped_log_events": self._dropped_events,
                    "p2p_parts": [parts[index] for index in sorted(parts)]}

    def _resolve_countries(self):
        client = None
        try:
            while not self._stop.is_set() and not self._cancel.is_set():
                with self._condition:
                    if not self._country_pending:
                        self._condition.wait(.2)
                        continue
                    address = self._country_pending.pop(0)
                try:
                    if client is None:
                        client = WinHttpClient("TVMON peer country lookup")
                    country = _lookup_country(address, client, self._stop)
                except Exception:
                    country = "조회 불가"
                with self._condition:
                    if address in self._countries:
                        self._countries[address] = country
                # Stay below the lookup service's per-IP request rate limit.
                self._stop.wait(.15)
        finally:
            if client is not None:
                client.close()

    def _accept_peers(self, value):
        # Optional inspection must never turn a valid download into an error.
        details = []
        seen = set()
        if isinstance(value, list) and len(value) <= 100:
            for item in value:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not re.fullmatch(r"[0-9a-f]{2,192}", item["id"])
                        or item.get("state") not in ("checking", "ready", "unavailable")):
                    continue
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                address = item.get("address")
                try:
                    if not isinstance(address, str) or len(address) > 128 or '%' in address:
                        raise ValueError()
                    address = str(ipaddress.ip_address(address))
                except ValueError:
                    address = ""
                details.append({"id": item["id"], "address": address,
                                "state": "ready" if address else "checking" if item["state"] == "checking" else "unavailable"})
        with self._condition:
            self._peer_details = details
            for item in details:
                address = item["address"]
                if not address or address in self._countries:
                    continue
                if len(self._countries) >= 512:
                    self._countries.pop(next(iter(self._countries)))
                if not ipaddress.ip_address(address).is_global:
                    self._countries[address] = "사설·예약 주소"
                elif len(self._country_pending) < 100:
                    self._countries[address] = "조회 중"
                    self._country_pending.append(address)
            self._condition.notify_all()

    def _evaluate(self, expression):
        response = self._browser._call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, self._stop, timeout=8)
        if response.get("exceptionDetails"):
            detail = response["exceptionDetails"].get("exception", {}).get("description", "")
            raise TransferError("P2P 수신 작업자를 실행하지 못했습니다. " + str(detail)[:400])
        return response.get("result", {}).get("value")

    def _accept_trackers(self, value):
        if (not isinstance(value, list) or len(value) != len(P2P_TRACKERS)
                or any(not isinstance(item, dict) or item.get("url") not in P2P_TRACKERS
                       or item.get("state") not in ("connecting", "connected", "announced", "error")
                       or item.get("error") not in ("", "connection_failed", "disconnected", "announce_rejected")
                       or "remote_peers" not in item or (item.get("remote_peers") is not None
                           and (type(item["remote_peers"]) is not int or not 0 <= item["remote_peers"] <= 10000000))
                       for item in value)
                or {item["url"] for item in value} != set(P2P_TRACKERS)):
            return
        trackers = [{key: item[key] for key in ("url", "state", "error", "remote_peers")} for item in value]
        with self._condition:
            changed = trackers != self._trackers
            self._trackers = trackers
        if changed and self._on_event is not None:
            self._on_event("p2p_trackers", trackers=trackers)

    def _accept(self, value):
        if not isinstance(value, dict):
            raise TransferError("P2P 작업자 응답이 올바르지 않습니다.")
        received, peers = value.get("bytes"), value.get("peers")
        if type(received) is not int or received < self._bytes or type(peers) is not int or not 0 <= peers <= 100:
            raise TransferError("P2P 수신 통계가 올바르지 않습니다.")
        delta = received - self._bytes
        active = value.get("active", [])
        window = value.get("window", [])
        events, dropped = value.get("events", []), value.get("droppedEvents", 0)
        if (not isinstance(active, list) or len(active) > len(self._config["segments"])
                or not isinstance(window, list) or len(window) > 32
                or any(type(index) is not int or not 0 <= index < len(self._config["segments"]) for index in active + window)):
            raise TransferError("P2P 수신 조각 번호가 올바르지 않습니다.")
        if (not isinstance(events, list) or len(events) > 128 or type(dropped) is not int or dropped < 0
                or any(not isinstance(item, dict) or item.get("kind") not in ("start", "loaded", "error", "abort", "anchor_error")
                       or type(item.get("index")) is not int or not 0 <= item["index"] < len(self._config["segments"])
                       or type(item.get("bytes")) is not int or not 0 <= item["bytes"] <= MAX_BUFFER
                       or type(item.get("seconds")) not in (int, float) or not math.isfinite(item["seconds"]) or item["seconds"] < 0
                       or not isinstance(item.get("error"), str) or len(item["error"]) > 80
                       or not isinstance(item.get("peer"), str) or len(item["peer"]) > 96 for item in events)):
            raise TransferError("P2P 조각 진단 기록이 올바르지 않습니다.")
        with self._condition:
            self._bytes, self._peers = received, peers
            self._active = set(active)
            self._window = window
            self._dropped_events = dropped
            first = peers > 0 and not self._connected
            self._connected |= peers > 0
            chunk = value.get("chunk")
            if chunk is not None:
                index, offset, total = (chunk.get(key) for key in ("index", "offset", "total"))
                if (type(index) is not int or not 0 <= index < len(self._config["segments"])
                        or type(offset) is not int or type(total) is not int or not 0 <= offset < total <= MAX_BUFFER
                        or not isinstance(chunk.get("data"), str) or len(chunk["data"]) > 1400000):
                    raise TransferError("P2P 영상 조각의 크기 또는 번호가 올바르지 않습니다.")
                block = base64.b64decode(chunk["data"], validate=True)
                if not block or len(block) > 1024 * 1024 or offset + len(block) > total:
                    raise TransferError("P2P 영상 조각을 모두 받지 못했습니다.")
                if index in self._remaining:
                    if offset == 0:
                        if index not in self._bodies and self._buffered + total > MAX_BUFFER:
                            raise TransferError("P2P 수신 대기 데이터가 허용 크기를 초과했습니다.")
                        self._assembling = (index, total, bytearray())
                    if (self._assembling is None or self._assembling[:2] != (index, total)
                            or len(self._assembling[2]) != offset):
                        raise TransferError("P2P 영상 조각의 수신 순서가 올바르지 않습니다.")
                    self._assembling[2].extend(block)
                    if offset + len(block) == total:
                        body = bytes(self._assembling[2])
                        self._assembling = None
                        if index not in self._bodies:
                            self._bodies[index] = body
                            self._buffered += total
                elif self._assembling and self._assembling[0] == index:
                    self._assembling = None
            self._condition.notify_all()
        if delta:
            self._on_bytes(delta)
        self._accept_peers(value.get("peerDetails", []))
        self._accept_trackers(value.get("trackers"))
        if self._on_event is not None:
            for item in events:
                self._on_event("p2p_" + item["kind"], segment=item["index"] + 1, bytes=item["bytes"],
                               seconds=item["seconds"], error=item["error"], peer=item["peer"])
            if "diagnostics" in value:
                self._report_diagnostics(value["diagnostics"])
        if first:
            self._on_status("P2P 피어에 연결했습니다. P2P와 HTTP로 영상 조각을 받습니다.")

    def _report_diagnostics(self, value):
        # Whitelist fields: engine objects may contain signed URLs, so never serialize them directly.
        if not isinstance(value, dict) or value.get("state") not in ("initializing", "unsupported", "error", "ready"):
            self._on_event("p2p_diagnostics", state="invalid")
            return
        if value["state"] != "ready":
            self._on_event("p2p_diagnostics", state=value["state"])
            return
        counts = ("engine_peers", "idle_peers", "engine_p2p_running", "p2p_limit", "queue_length", "pending_js_parts")
        sizes = ("cache_used_bytes", "cache_limit_bytes", "cache_total_bytes", "pending_js_bytes", "budget_bytes")
        numbers = ("cache_free_percent", "effective_p2p_window", "last_queue_age_seconds")
        reasons = {"not_registered", "awaiting_transfer", "receiving", "engine_cached", "no_peers",
                   "outside_effective_window", "not_advertised", "behind_queue_boundary", "anchor_not_registered", "peer_still_loading",
                   "peer_busy", "concurrency_limit", "eligible_not_requested", "http_owned"}
        pieces = value.get("pieces")
        valid = (type(value.get("anchor")) is int and 0 <= value["anchor"] < len(self._config["segments"])
                 and type(value.get("requested_anchor")) is int and 0 <= value["requested_anchor"] < len(self._config["segments"])
                 and type(value.get("anchor_failures")) is int and 0 <= value["anchor_failures"] <= len(self._config["segments"])
                 and type(value.get("anchor_registered")) is bool and type(value.get("queue_processing_pending")) is bool
                 and all(type(value.get(key)) is int and 0 <= value[key] <= 100 for key in counts)
                 and all(type(value.get(key)) is int and 0 <= value[key] <= 1024 * 1024 * 1024 for key in sizes)
                 and type(value.get("cache_released_bytes")) is int and 0 <= value["cache_released_bytes"] < 2**53
                 and all((key == "last_queue_age_seconds" and value.get(key) is None)
                         or (type(value.get(key)) in (int, float) and -1e9 <= value[key] <= 1e9
                             and math.isfinite(value[key])) for key in numbers)
                 and isinstance(pieces, list) and len(pieces) <= 32)
        if valid:
            valid = all(isinstance(item, dict) and type(item.get("index")) is int
                        and 0 <= item["index"] < len(self._config["segments"])
                        and all(type(item.get(key)) is bool for key in ("queued", "high_demand", "in_p2p_window", "cached"))
                        and all(type(item.get(key)) is int and 0 <= item[key] <= 100
                                for key in ("loaded_peers", "loading_peers", "idle_loaded_peers"))
                        and item.get("request_state") in ("none", "not-started", "loading", "succeed", "failed", "aborted")
                        and isinstance(item.get("reason"), str) and item["reason"] in reasons for item in pieces)
        if not valid:
            self._on_event("p2p_diagnostics", state="invalid")
            return
        fields = {key: value[key] for key in (*counts, *sizes, *numbers)}
        fields["cache_released_bytes"] = value["cache_released_bytes"]
        fields["anchor_segment"] = value["anchor"] + 1
        fields["requested_anchor_segment"] = value["requested_anchor"] + 1
        fields.update({key: value[key] for key in ("anchor_registered", "anchor_failures", "queue_processing_pending")})
        fields["pieces"] = [{"segment": item["index"] + 1,
                            **{key: item[key] for key in ("queued", "high_demand", "in_p2p_window", "cached",
                               "loaded_peers", "loading_peers", "idle_loaded_peers", "request_state", "reason")}}
                           for item in pieces]
        self._on_event("p2p_diagnostics", state="ready", **fields)

    def _run(self):
        temporary = None
        try:
            script = self._load_script(self._stop)
            if self._on_event is not None:
                self._on_event("p2p_engine", sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
                               diagnostics_version=3, sample_phase="before_finish_ack", queue_policy="download_available",
                               cache_policy="release_completed", ownership_policy="http_claim_ack")
            if self._stop.is_set() or self._cancel.is_set():
                return
            temporary = tempfile.TemporaryDirectory(prefix="tvmon-p2p-")
            profile = Path(temporary.name).resolve()
            if profile.parent != Path(tempfile.gettempdir()).resolve():
                raise TransferError("P2P 임시 폴더의 위치가 올바르지 않습니다.")
            self._browser = TextSiteBrowser(profile)
            try:
                browser = self._browser
                browser._start(self._stop)
                browser.target = browser._call("Target.createTarget", {"url": "about:blank", "hidden": True, "background": True}, self._stop)["targetId"]
                browser.session = browser._call("Target.attachToTarget", {"targetId": browser.target, "flatten": True}, self._stop)["sessionId"]
                browser._call("Page.enable", {}, self._stop)
                navigation = browser._call("Page.navigate", {"url": P2P_PAGE_URL}, self._stop, timeout=15)
                if navigation.get("errorText"):
                    raise TransferError("P2P 재생기 페이지에 접속하지 못했습니다.")
                deadline = time.monotonic() + 15
                while True:
                    page = self._evaluate("({origin: location.origin, ready: document.readyState})")
                    if page.get("origin") == "https://player.bunny-frame.online" and page.get("ready") in ("interactive", "complete"):
                        break
                    if time.monotonic() >= deadline:
                        raise TransferError("P2P 재생기 페이지를 준비하지 못했습니다.")
                    if self._stop.wait(.1) or self._cancel.is_set():
                        return
                self._evaluate(script)
                with self._condition:
                    self._evaluate(f"({_WORKER_JS})({json.dumps({**self._config, 'http': sorted(self._http)})})")
                    self._http_acknowledged = set(self._http)
                    self._ready = True
                while not self._stop.is_set() and not self._cancel.is_set():
                    with self._condition:
                        self._wake.clear()
                        if not self._remaining:
                            break
                        finished = sorted(self._finished)
                        self._finished.clear()
                        budget = MAX_BUFFER - self._buffered
                        http = sorted(self._http)
                    value = self._evaluate(f"window.tvmonP2P.pull({json.dumps(finished)}, {budget}, {json.dumps(http)})")
                    self._accept(value)
                    with self._condition:
                        self._http_acknowledged = set(http) & self._http
                        self._condition.notify_all()
                    self._wake.wait(.02 if value.get("chunk") else .2)
            finally:
                self._browser.close()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            if not self._stop.is_set() and not self._cancel.is_set():
                self._on_status("P2P 연결을 사용할 수 없어 HTTP 다운로드로 계속합니다.")
        finally:
            with self._condition:
                self._failed = True
                self._peers = 0
                self._peer_details = []
                self._active.clear()
                self._condition.notify_all()
            if temporary is not None:
                self._cleanup_profile(temporary)

    def _cleanup_profile(self, temporary):
        # Chrome's crash-reporting child can briefly retain a mapped file after exit.
        for attempt in range(5):
            profile = Path(temporary.name).resolve()
            if profile.parent != Path(tempfile.gettempdir()).resolve() or not profile.name.startswith("tvmon-p2p-"):
                self.cleanup_error = "P2P 임시 폴더의 위치가 올바르지 않습니다."
                return
            try:
                temporary.cleanup()
                return
            except OSError as error:
                if attempt == 4:
                    self.cleanup_error = str(error)
                else:
                    time.sleep(.2 * 2 ** attempt)

    def close(self):
        self._stop.set()
        self._wake.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=15)
        self._country_thread.join(timeout=5)
        if self._thread.is_alive() and self._browser:
            self._browser.close()
            self._thread.join(timeout=3)

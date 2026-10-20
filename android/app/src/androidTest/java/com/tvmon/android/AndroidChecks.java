package com.tvmon.android;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.media.MediaExtractor;
import android.os.Bundle;
import android.util.AtomicFile;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/** Dependency-free instrumentation checks, including actual HTTP and MediaStore. */
public final class AndroidChecks extends Instrumentation {
    private Bundle args;
    private int passed;
    @Override public void onCreate(Bundle arguments) { args = arguments; start(); }
    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            if ("true".equals(args.getString("flow"))) flow();
            else if ("true".equals(args.getString("live"))) live();
            else if ("true".equals(args.getString("scheduler"))) scheduler();
            else if ("true".equals(args.getString("ui"))) ui();
            else if ("true".equals(args.getString("usability"))) usability();
            else if ("true".equals(args.getString("quality"))) quality();
            else if ("true".equals(args.getString("thumbnails"))) thumbnails();
            else if ("true".equals(args.getString("deletion"))) deletion();
            else if ("true".equals(args.getString("deletionLive"))) deletionLive();
            else if ("true".equals(args.getString("library"))) library();
            else if ("true".equals(args.getString("queueControls"))) queueControls();
            else if ("true".equals(args.getString("queueControlsLive"))) queueControlsLive();
            else if ("true".equals(args.getString("guide"))) guideLive();
            else if ("true".equals(args.getString("settings"))) settings();
            else if ("true".equals(args.getString("native"))) nativeTransport();
            else if ("true".equals(args.getString("ech"))) echLive();
            else { formats(); catalogue(); guide(); transport(); nativeTransport(); resume(); scheduler(); }
            result.putString("stream", "\nPASS: " + passed + " checks\n"); finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("stream", "\nFAIL after " + passed + " checks: " + android.util.Log.getStackTraceString(error)); finish(Activity.RESULT_CANCELED, result);
        }
    }
    private void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
        passed++; Bundle status = new Bundle(); status.putString("stream", "PASS " + message + "\n"); sendStatus(0, status);
    }
    private interface Operation { void run() throws Exception; }
    private void rejects(Operation operation, String message) throws Exception {
        boolean rejected = false; try { operation.run(); } catch (Exception expected) { rejected = true; } check(rejected, message);
    }
    private static byte[] bytes(String value) { return value.getBytes(StandardCharsets.UTF_8); }

    private void formats() throws Exception {
        check(SiteClient.origin("tvwiki52.net/").equals("https://tvwiki52.net"), "domain normalization");
        rejects(() -> SiteClient.origin("https://user@example.com"), "reject domain credentials");
        rejects(() -> SiteClient.origin("http://example.com"), "reject HTTP domain");
        rejects(() -> SiteClient.origin("https://example.com/path"), "reject domain path");
        rejects(() -> SiteClient.checked("file:///etc/passwd"), "reject non-network media URL");
        List<VideoFormat.Segment> playlist = VideoFormat.playlist(bytes("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:12\n#EXT-X-KEY:METHOD=AES-128,URI=\"/key\"\n#EXTINF:4,\na.ts\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:5,\nb.ts\n#EXT-X-ENDLIST\n"), "https://cdn.example/v/c.html");
        check(playlist.size() == 2 && playlist.get(0).iv()[15] == 12 && playlist.get(0).keyUrl().equals("https://cdn.example/key") && playlist.get(1).keyUrl() == null, "HLS sequence IV, relative URL, key rotation");
        rejects(() -> VideoFormat.playlist(bytes("#EXTM3U\n#EXTINF:4,\na.ts\n"), "https://cdn.example/"), "reject live playlist");
        rejects(() -> VideoFormat.playlist(bytes("#EXTM3U\n#EXT-X-MAP:URI=\"a.mp4\"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST"), "https://cdn.example/"), "reject unsupported fMP4 playlist");
        rejects(() -> VideoFormat.playlist(bytes("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST"), "https://cdn.example/"), "reject unsupported encryption");
        rejects(() -> VideoFormat.playlist(bytes("#EXTM3U\n#EXTINF:NaN,\na.ts\n#EXT-X-ENDLIST"), "https://cdn.example/"), "reject invalid duration");
        byte[] key = new byte[16]; for (int i = 0; i < 16; i++) key[i] = (byte) i;
        check(Arrays.equals(key, VideoFormat.decodeKey(key)), "raw AES key");
        byte[] fixture; try (InputStream input = getContext().getAssets().open("level7.json")) { fixture = input.readAllBytes(); }
        check(Arrays.equals(key, VideoFormat.decodeKey(fixture)), "desktop Level7 fixture matches Android key decoder");
        rejects(() -> VideoFormat.decodeKey(bytes("{}")), "reject malformed key response");
        JSONObject malformed = new JSONObject(new String(fixture, StandardCharsets.UTF_8));
        JSONArray layers = malformed.getJSONArray("layers"); layers.getJSONObject(0).getJSONArray("perm").put(0, -1);
        rejects(() -> VideoFormat.decodeKey(bytes(malformed.toString())), "reject malformed key permutation");
        byte[] original = ts(3); Cipher cipher = Cipher.getInstance("AES/CBC/PKCS5Padding"); byte[] iv = new byte[16];
        cipher.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv)); byte[] encrypted = cipher.doFinal(original);
        check(Arrays.equals(original, VideoFormat.decrypt(encrypted, key, iv)), "AES-CBC PKCS7 decrypt");
        rejects(() -> VideoFormat.validateTs(bytes("<html>error</html>")), "reject error page as video");
        original[188] = 0; rejects(() -> VideoFormat.validateTs(original), "validate every TS packet");
    }

    private void catalogue() throws Exception {
        SiteClient client = new SiteClient(SiteClient.domains(SiteClient.DEFAULT_DOMAINS));
        SiteClient.Search result = client.parseSearch(bytes("{\"list\":[{\"wr_subject\":\"삼국지 &amp; 영웅\",\"href\":\"/world/10180\",\"board_name\":\"해외드라마\",\"wr_6\":\"2010-01-01\"}],\"pages\":\"<a href='?page=2'>다음</a>\"}"), SiteClient.CANONICAL + "/bbs/ajax.searchList.php", 1);
        check(result.more() && result.items().size() == 1 && result.items().get(0).title().equals("삼국지 & 영웅"), "search JSON, entities and pagination");
        String work = SiteClient.CANONICAL + "/world/10180";
        String html = "<div id='epListScroll'><a href='" + work + "/999'>short list</a></div><div id='other_list'><ul>" + row(work, 2) + row(work, 1) + "</ul><a class='pg_page' href='?page=2&amp;s=1'>2</a></div>";
        CatalogPage page = new CatalogPage(client, work, work, html);
        check(page.complete && page.items.size() == 2 && page.pages.contains(2) && page.items.get(0).title().equals("삼국지 2화"), "full episode list excludes player overlay and parses pagination");
        check(page.items.get(1).url().equals(work + "/1") && page.items.get(0).thumbnail().equals("https://images.example/2.png"), "episode links and lazy thumbnails");
        rejects(() -> new CatalogPage(client, work, work, html.replace(work + "/2", SiteClient.CANONICAL + "/world/999/2")), "reject episode from another work");
        rejects(() -> client.canonical("https://unregistered.example/world/1/2", work, true), "reject unregistered episode host");
        rejects(() -> client.canonical("https://tvmon1.com/novel/1", work, true), "reject novel/content link as video episode");
    }
    private void guide() throws Exception {
        String link = "<a href='https://tvwiki54.net'>바로가기</a>";
        check(SiteClient.guideDomain("<dt>외부 연결 주소</dt><dd>tvwiki54.net</dd>" + link, SiteClient.GUIDE).equals("https://tvwiki54.net"), "current guide external-address label");
        check(SiteClient.guideDomain("<dt> 현재 안내 주소 </dt><dd><strong>tvwiki54.net</strong></dd>" + link, SiteClient.GUIDE).equals("https://tvwiki54.net"), "previous guide label and nested markup");
        rejects(() -> SiteClient.guideDomain("<dt>외부 연결 주소</dt><dd>tvwiki54.net</dd><a href='https://other.example'>바로가기</a>", SiteClient.GUIDE), "reject guide address/link mismatch");
        rejects(() -> SiteClient.guideDomain("<dt>외부 연결 주소</dt><dd>tvwiki54.net</dd><dt>현재 안내 주소</dt><dd>tvwiki55.net</dd>" + link, SiteClient.GUIDE), "reject conflicting current addresses");
        rejects(() -> SiteClient.guideDomain("<a href='https://advert.example'>광고</a>", SiteClient.GUIDE), "do not treat arbitrary external links as current address");
        check(SiteClient.guideUrl(" guide.example/current?site=video ").equals("https://guide.example/current?site=video"), "editable guide supports HTTPS page paths");
        rejects(() -> SiteClient.guideUrl("http://guide.example"), "reject insecure guide URL");
        rejects(() -> SiteClient.guideUrl("https://user@guide.example"), "reject guide credentials");
    }
    private void guideLive() throws Exception {
        Context context = getTargetContext(); SiteClient.Stop stop = new SiteClient.Stop();
        String guide = context.getSharedPreferences("settings", 0).getString("guide", SiteClient.GUIDE);
        SiteClient.Response response = SiteClient.read(guide, guide, 2 * 1024 * 1024, stop);
        check(!SiteClient.guideDomain(new String(response.body(), StandardCharsets.UTF_8), response.url()).isEmpty(), "actual guide page address and matching link");
        String domain = new SiteClient(SiteClient.domains(SiteClient.DEFAULT_DOMAINS)).currentDomain(guide, stop);
        check(!domain.isEmpty(), "discovered address passes actual homepage verification: " + domain);
    }
    private static String row(String work, int n) {
        return "<li class='searchText'><a href='" + work + "/" + n + "'><img src='/lazy.svg' data-src='https://images.example/" + n + ".png'></a><div class='date'>10.10.2026</div><a class='title' href='" + work + "/" + n + "'><div> 삼국지 " + n + "화 </div></a></li>";
    }

    private void transport() throws Exception {
        try (LocalServer server = new LocalServer()) {
            check(SiteClient.read(server.url("/0.ts"), server.url("/"), 4096, new SiteClient.Stop()).body().length == 376, "real Android HTTP response read");
            java.util.concurrent.atomic.AtomicLong received = new java.util.concurrent.atomic.AtomicLong();
            SiteClient.read(server.url("/1.ts"), server.url("/"), 4096, new SiteClient.Stop(), received::addAndGet);
            check(received.get() == 376, "speed telemetry counts actual HTTP bytes");
            rejects(() -> SiteClient.read(server.url("/0.ts"), server.url("/"), 64, new SiteClient.Stop()), "enforce response size limit");
            rejects(() -> SiteClient.read(server.url("/redirect"), server.url("/"), 4096, new SiteClient.Stop()), "reject redirect to non-HTTPS external URL");
            SiteClient.Stop stop = new SiteClient.Stop(); stop.cancel();
            rejects(() -> SiteClient.read(server.url("/0.ts"), server.url("/"), 4096, stop), "cancel before network request");
        }
    }

    private void nativeTransport() throws Exception {
        String features = NativeHttp.features();
        check(features.contains(" ECH") && features.contains(" HTTPSRR") && features.contains(" HTTP2"), "native library includes real ECH, HTTPS DNS records and HTTP/2");
        android.util.Log.i("TVMON", features);
        try (LocalServer server = new LocalServer()) {
            check(NativeHttp.read(server.url("/0.ts"), server.url("/"), 4096, new SiteClient.Stop()).body().length == 376, "JNI native HTTP returns exact response bytes");
            rejects(() -> NativeHttp.read(server.url("/0.ts"), server.url("/"), 64, new SiteClient.Stop()), "native response size limit");
            rejects(() -> NativeHttp.read(server.url("/redirect"), server.url("/"), 4096, new SiteClient.Stop()), "native redirects cannot downgrade to external HTTP");
            rejects(() -> NativeHttp.read(server.url("/credentials"), server.url("/"), 4096, new SiteClient.Stop()), "native redirects reject embedded credentials");
            try { NativeHttp.read(server.url("/unavailable"), server.url("/"), 4096, new SiteClient.Stop()); throw new AssertionError("HTTP 503 accepted"); }
            catch (java.io.IOException expected) { check(expected.getMessage().contains("HTTP 503"), "native preserves HTTP status errors"); }
            try { NativeHttp.read(server.url("/challenge"), server.url("/"), 4096, new SiteClient.Stop()); throw new AssertionError("challenge accepted"); }
            catch (java.io.IOException expected) { check(expected.getMessage().contains("브라우저 인증"), "native distinguishes browser authentication challenge"); }
            SiteClient.Stop cancelled = new SiteClient.Stop(); cancelled.cancel(); int before = server.count("/0.ts");
            rejects(() -> NativeHttp.read(server.url("/0.ts"), server.url("/"), 4096, cancelled), "native cancels before any network request");
            check(server.count("/0.ts") == before, "cancelled native request never reaches server");
            SiteClient.Stop stop = new SiteClient.Stop(); java.util.concurrent.atomic.AtomicReference<Throwable> stopped = new java.util.concurrent.atomic.AtomicReference<>();
            Thread worker = new Thread(() -> { try { NativeHttp.read(server.url("/stall"), server.url("/"), 4096, stop); } catch (Throwable error) { stopped.set(error); } });
            worker.start(); long deadline = android.os.SystemClock.elapsedRealtime() + 5000;
            while (server.count("/stall") == 0 && worker.isAlive() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(20);
            check(server.count("/stall") > 0, "native cancellation test reaches stalled response");
            stop.cancel(); worker.join(2000);
            check(!worker.isAlive() && stopped.get() instanceof java.io.InterruptedIOException, "native multi polling cancels stalled transfer within two seconds");
        }
        try (ServerSocket closed = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            int port = closed.getLocalPort(); closed.close();
            SiteClient client = new SiteClient(Collections.singletonList("https://127.0.0.1:" + port));
            try { client.fetch(SiteClient.CANONICAL + "/", SiteClient.CANONICAL + "/", 4096, new SiteClient.Stop()); throw new AssertionError("closed connection accepted"); }
            catch (java.io.IOException expected) { check(expected.getMessage().contains("기본 연결과 ECH/DoH 재접속이 모두 실패"), "site connection failure invokes the ECH/DoH fallback"); }
        }
    }

    private void echLive() throws Exception {
        SiteClient.Stop stop = new SiteClient.Stop(); String traceUrl = "https://www.cloudflare.com/cdn-cgi/trace";
        String ech = new String(NativeHttp.read("https://tls-ech.dev/", "https://tls-ech.dev/", 65536, stop).body(), StandardCharsets.UTF_8);
        check(ech.contains("You are using ECH"), "live test server confirms an actually encrypted ClientHello");
        String trace = new String(NativeHttp.read(traceUrl, "https://www.cloudflare.com/", 65536, stop).body(), StandardCharsets.UTF_8);
        check(trace.contains("http=http/2"), "live native transport negotiates HTTP/2");
        String html = new String(NativeHttp.read(SiteClient.GUIDE, SiteClient.GUIDE, 2 * 1024 * 1024, stop).body(), StandardCharsets.UTF_8);
        String domain = SiteClient.guideDomain(html, SiteClient.GUIDE);
        check(!domain.isEmpty(), "native HTTPS reads guide using Android trust roots and DoH");
        SiteClient client = new SiteClient(Collections.singletonList(domain));
        check(domain.equals(client.currentDomain(stop)), "guide discovers and verifies actual site through normal connection or ECH retry");
        try { NativeHttp.read("https://self-signed.badssl.com/", "https://self-signed.badssl.com/", 65536, stop); throw new AssertionError("untrusted certificate accepted"); }
        catch (java.io.IOException expected) { check(expected.getMessage().contains("SSL peer certificate"), "native TLS still rejects an untrusted certificate"); }
    }

    private void resume() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        String previousWorks = context.getSharedPreferences("settings", 0).getString("recentWorks", null);
        String url = SiteClient.CANONICAL + "/ent/1/" + System.currentTimeMillis();
        store.add(Collections.singletonList(new SiteClient.Item("TVMON instrumentation " + System.currentTimeMillis(), url, "", "")));
        DownloadStore.Job job = null; for (DownloadStore.Job value : store.snapshot()) if (value.url.equals(url)) job = value;
        if (job == null) throw new AssertionError("test job missing");
        DownloadStore.Job current = job;
        try (LocalServer server = new LocalServer()) {
            List<VideoFormat.Segment> segments = new ArrayList<>(); ByteArrayOutputStream expected = new ByteArrayOutputStream();
            for (int i = 0; i < 12; i++) { segments.add(new VideoFormat.Segment(server.url("/" + i + ".ts"), null, new byte[16], 4)); expected.write(ts(i)); }
            DownloadEngine.Source source = new DownloadEngine.Source(segments, new HashMap<>());
            store.update(current.id, value -> value.state = DownloadStore.RUNNING);
            SiteClient.Stop pause = new SiteClient.Stop(); boolean interrupted = false;
            try { DownloadEngine.transfer(context, store, current, source, pause, update -> { if (update.done >= 2) pause.cancel(); }); }
            catch (Exception expectedPause) { interrupted = true; }
            check(interrupted && store.find(current.id).output.isEmpty(), "pause retains parts without publishing incomplete video");
            JSONObject saved = new JSONObject(new String(new AtomicFile(new File(store.parts(current.id), "state.json")).readFully(), StandardCharsets.UTF_8));
            List<String> indexes = new ArrayList<>(); java.util.Iterator<String> keys = saved.getJSONObject("parts").keys(); while (keys.hasNext()) indexes.add(keys.next());
            check(indexes.size() >= 2, "durable resume checkpoint");
            DownloadStore.Job paused = store.find(current.id);
            check(paused.durationSeconds == 48 && paused.speed() == 0, "sum HLS playback duration and clear speed after stop");
            int savedCount = 0; boolean receiving = false; for (byte state : paused.partStates) { if (state == 2) savedCount++; if (state == 1) receiving = true; }
            check(!receiving, "no receiving tile remains after stop");
            check(savedCount == indexes.size() && savedCount == paused.done, "segment map agrees with durable out-of-order checkpoint");
            JSONObject previous = paused.json(); previous.remove("durationSeconds");
            check(DownloadStore.Job.from(previous).durationSeconds == 0, "older queue records remain readable");
            String good = indexes.get(0), corrupt = indexes.get(1);
            int goodRequests = server.count("/" + good + ".ts"), corruptRequests = server.count("/" + corrupt + ".ts");
            File corruptFile = new File(store.parts(current.id), String.format(java.util.Locale.ROOT, "%06d.ts", Integer.parseInt(corrupt)));
            byte[] tampered = Files.readAllBytes(corruptFile.toPath()); tampered[10] ^= 1; Files.write(corruptFile.toPath(), tampered);
            DownloadEngine.transfer(context, store, current, source, new SiteClient.Stop(), update -> {});
            DownloadStore.Job done = store.find(current.id);
            check(DownloadStore.DONE.equals(done.state) && done.done == 12, "resume downloads remaining parts to completion");
            check(DownloadStore.Job.from(done.json()).durationSeconds == 48, "playback duration survives queue serialization");
            check(server.count("/" + good + ".ts") == goodRequests, "reuse verified cached segment without HTTP request");
            check(server.count("/" + corrupt + ".ts") > corruptRequests, "re-download tampered cached segment");
            try (InputStream input = context.getContentResolver().openInputStream(android.net.Uri.parse(done.output))) {
                check(input != null && Arrays.equals(expected.toByteArray(), input.readAllBytes()), "MediaStore output preserves exact segment order and bytes");
            }
            check(!store.parts(current.id).exists(), "remove temporary segments after successful publish");
        } finally {
            DownloadStore.Job finalJob = store.find(current.id);
            if (finalJob != null && !finalJob.output.isEmpty()) context.getContentResolver().delete(android.net.Uri.parse(finalJob.output), null, null);
            store.remove(current.id);
            restoreWorks(previousWorks);
        }
    }

    private void live() throws Exception {
        Context context = getTargetContext(); SiteClient client = new SiteClient(SiteClient.domains(context.getSharedPreferences("settings", 0).getString("domains", SiteClient.DEFAULT_DOMAINS)));
        SiteClient.Stop stop = new SiteClient.Stop();
        SiteClient.Search search = client.search(args.getString("query", "런닝맨"), 1, stop);
        check(!search.items().isEmpty(), "live site title search");
        List<SiteClient.Item> episodes = client.episodes(search.items().get(0).url(), stop);
        check(!episodes.isEmpty(), "live full episode list: " + episodes.size());
        SiteClient.Video video = client.video(episodes.get(episodes.size() - 1).url(), stop);
        DownloadEngine.Source source = DownloadEngine.loadSource(client, video.manifest(), stop);
        check(!source.segments().isEmpty(), "live playlist and AES key: " + source.segments().size() + " segments");
        File sample = new File(context.getCacheDir(), "live-video-sample.ts");
        try (java.io.FileOutputStream output = new java.io.FileOutputStream(sample)) {
            for (int i = 0; i < Math.min(3, source.segments().size()); i++) {
                VideoFormat.Segment segment = source.segments().get(i); byte[] data = SiteClient.read(segment.url(), SiteClient.PLAYER, SiteClient.MEDIA_LIMIT, stop).body();
                if (segment.keyUrl() != null) data = VideoFormat.decrypt(data, source.keys().get(segment.keyUrl()), segment.iv());
                VideoFormat.validateTs(data); output.write(data);
            }
        }
        check(sample.length() > 0, "live encrypted segments downloaded, decrypted and validated");
        MediaExtractor extractor = new MediaExtractor();
        try { extractor.setDataSource(sample.getAbsolutePath()); check(extractor.getTrackCount() > 0, "Android MediaExtractor recognizes saved video tracks"); }
        finally { extractor.release(); sample.delete(); }
    }
    private void scheduler() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        String url = "https://unregistered.invalid/ent/1/" + System.currentTimeMillis();
        store.add(Collections.singletonList(new SiteClient.Item("Scheduler check", url, "", "")));
        DownloadStore.Job job = null; for (DownloadStore.Job value : store.snapshot()) if (value.url.equals(url)) job = value;
        if (job == null) throw new AssertionError("scheduler job missing");
        Activity activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
        try {
            java.util.concurrent.atomic.AtomicReference<Exception> error = new java.util.concurrent.atomic.AtomicReference<>();
            java.util.concurrent.atomic.AtomicReference<android.app.job.JobInfo> scheduled = new java.util.concurrent.atomic.AtomicReference<>();
            runOnMainSync(() -> {
                try {
                    TransferScheduler.start(activity);
                    if (android.os.Build.VERSION.SDK_INT >= 34) scheduled.set(context.getSystemService(android.app.job.JobScheduler.class).getPendingJob(TransferScheduler.JOB));
                } catch (Exception e) { error.set(e); }
            });
            if (error.get() != null) throw error.get();
            if (android.os.Build.VERSION.SDK_INT >= 34) {
                check(scheduled.get() != null, "user-initiated job is registered");
                check(!scheduled.get().getRequiredNetwork().hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_NOT_VPN),
                        "download job allows Internet through VPN or ad blocker");
            }
            long deadline = android.os.SystemClock.elapsedRealtime() + 15000;
            while (!DownloadStore.FAILED.equals(store.find(job.id).state) && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(DownloadStore.FAILED.equals(store.find(job.id).state), "native background scheduler runs queue and records failure");
            while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(!DownloadEngine.running(), "background runner releases queue after completion");
        } finally {
            context.getSystemService(android.app.job.JobScheduler.class).cancel(TransferScheduler.JOB);
            DownloadEngine.cancel();
            long deadline = android.os.SystemClock.elapsedRealtime() + 15000;
            while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            runOnMainSync(activity::finish); store.remove(job.id);
        }
    }
    private static <T extends android.view.View> T find(android.view.View view, Class<T> type, String label) {
        if (type.isInstance(view) && (label == null || view instanceof android.widget.TextView && label.equals(((android.widget.TextView) view).getText().toString()))) return type.cast(view);
        if (view instanceof android.view.ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) { T result = find(group.getChildAt(i), type, label); if (result != null) return result; }
        return null;
    }
    private void flow() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        String previousWorks = context.getSharedPreferences("settings", 0).getString("recentWorks", null);
        try (android.os.ParcelFileDescriptor ignored = getUiAutomation().executeShellCommand("pm grant com.tvmon.android android.permission.POST_NOTIFICATIONS")) { }
        Activity activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
        android.view.View root = activity.getWindow().getDecorView();
        SetHolder holder = new SetHolder();
        try {
            runOnMainSync(() -> { find(root, android.widget.EditText.class, null).setText("런닝맨"); find(root, android.widget.Button.class, "검색").performClick(); });
            long deadline = android.os.SystemClock.elapsedRealtime() + 180000;
            while (android.os.SystemClock.elapsedRealtime() < deadline) {
                java.util.concurrent.atomic.AtomicBoolean ready = new java.util.concurrent.atomic.AtomicBoolean();
                runOnMainSync(() -> { android.widget.ListView list = find(root, android.widget.ListView.class, null); ready.set(list != null && list.getAdapter().getCount() > list.getHeaderViewsCount() + list.getFooterViewsCount()); });
                if (ready.get()) break; Thread.sleep(250);
            }
            runOnMainSync(() -> { android.widget.ListView list = find(root, android.widget.ListView.class, null); if (list.getAdapter().getCount() <= list.getHeaderViewsCount() + list.getFooterViewsCount()) throw new AssertionError("search UI has no results"); int first = list.getHeaderViewsCount(); list.performItemClick(null, first, first); });
            check(true, "UI title search and open work");
            while (android.os.SystemClock.elapsedRealtime() < deadline) {
                java.util.concurrent.atomic.AtomicBoolean ready = new java.util.concurrent.atomic.AtomicBoolean();
                runOnMainSync(() -> { android.widget.ListView list = find(root, android.widget.ListView.class, null); ready.set(find(root, android.widget.Button.class, "다운로드") != null && list != null && list.getAdapter().getCount() > list.getHeaderViewsCount() + list.getFooterViewsCount()); });
                if (ready.get()) break; Thread.sleep(250);
            }
            runOnMainSync(() -> {
                android.widget.ListView list = find(root, android.widget.ListView.class, null); int last = list.getAdapter().getCount() - list.getFooterViewsCount() - 1;
                if (last < list.getHeaderViewsCount()) throw new AssertionError("episode UI has no rows"); list.performItemClick(null, last, last);
                check(find(root, android.widget.Button.class, "다운로드").isEnabled(), "UI episode selection enables download");
                find(root, android.widget.Button.class, "다운로드").performClick();
            });
            for (DownloadStore.Job job : store.snapshot()) if (!DownloadStore.DONE.equals(job.state)) holder.id = job.id;
            if (holder.id == null) throw new AssertionError("UI did not enqueue episode");
            deadline = android.os.SystemClock.elapsedRealtime() + 90000;
            while (android.os.SystemClock.elapsedRealtime() < deadline) {
                DownloadStore.Job job = store.find(holder.id);
                if (DownloadStore.FAILED.equals(job.state)) throw new AssertionError(job.message);
                if (job.done >= 2) break; Thread.sleep(100);
            }
            check(store.find(holder.id).done >= 2, "UI download button starts real background HTTP transfer");
            check(store.find(holder.id).durationSeconds > 0 && store.find(holder.id).speed() > 0, "live download supplies actual playback duration and network speed");
            DownloadStore.Job downloaded = store.find(holder.id);
            check(!downloaded.contentUrl.isEmpty() && store.recentWorks().stream().anyMatch(item -> item.url().equals(downloaded.contentUrl)), "actual HTTP download remembers exact work for future direct return");
            runOnMainSync(() -> { android.widget.ListView list = find(root, android.widget.ListView.class, null); for (int i = 0; i < list.getCount(); i++) if (list.getAdapter().getItem(i) instanceof DownloadStore.Job entry && entry.id.equals(holder.id)) { list.performItemClick(null, i, i); break; } });
            Thread.sleep(800); android.app.Dialog detail = detailsDialog(activity); if (detail == null) throw new AssertionError("live detail did not open"); capture("details-live.png");
            runOnMainSync(() -> find(detail.getWindow().getDecorView(), android.widget.Button.class, "일시정지").performClick());
            deadline = android.os.SystemClock.elapsedRealtime() + 20000;
            while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(!DownloadEngine.running() && DownloadStore.PAUSED.equals(store.find(holder.id).state), "UI pause stops native background transfer and preserves job");
            android.graphics.Bitmap screenshot = getUiAutomation().takeScreenshot();
            try (java.io.FileOutputStream output = new java.io.FileOutputStream(new File(context.getFilesDir(), "queue-verified.png"))) { screenshot.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, output); }
        } finally {
            store.pauseAll(); DownloadEngine.cancel(); context.getSystemService(android.app.job.JobScheduler.class).cancel(TransferScheduler.JOB);
            long deadline = android.os.SystemClock.elapsedRealtime() + 20000; while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            if (holder.id != null) store.remove(holder.id);
            restoreWorks(previousWorks);
            runOnMainSync(activity::finish);
        }
    }
    private static final class SetHolder { String id; }
    private android.widget.TextView contains(android.view.View view, String value) {
        if (view instanceof android.widget.TextView text && text.getText().toString().contains(value)) return text;
        if (view instanceof android.view.ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) { android.widget.TextView result = contains(group.getChildAt(i), value); if (result != null) return result; }
        return null;
    }
    private void waitForText(android.view.View root, String value) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 45000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            java.util.concurrent.atomic.AtomicBoolean found = new java.util.concurrent.atomic.AtomicBoolean(); runOnMainSync(() -> found.set(contains(root, value) != null));
            if (found.get()) return; Thread.sleep(150);
        }
        throw new AssertionError("UI text did not appear: " + value);
    }
    private void settings() throws Exception {
        Context context = getTargetContext(); android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
        String previousGuide = prefs.getString("guide", null), previousDomains = prefs.getString("domains", null), custom = SiteClient.GUIDE + "?android-check=1";
        Activity activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
        try {
            android.view.View root = activity.getWindow().getDecorView();
            runOnMainSync(() -> { find(root, android.widget.Button.class, "설정").performClick(); find(root, android.widget.EditText.class, previousGuide == null ? SiteClient.GUIDE : previousGuide).setText(custom); find(root, android.widget.Button.class, "주소 설정 저장").performClick(); });
            check(prefs.getString("guide", "").equals(custom), "edited guide URL is persisted");
            Activity first = activity; runOnMainSync(first::finish);
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)); android.view.View reopened = activity.getWindow().getDecorView();
            runOnMainSync(() -> { find(reopened, android.widget.Button.class, "설정").performClick(); check(find(reopened, android.widget.EditText.class, custom) != null, "edited guide URL survives activity restart"); find(reopened, android.widget.Button.class, "안내 페이지에서 현재 주소 확인").performClick(); });
            waitForText(reopened, "안내 주소 https://"); check(true, "guide button reads editable URL and reports discovered address or precise connection error");
            runOnMainSync(() -> find(reopened, android.widget.Button.class, "사이트 접속 확인").performClick()); waitForText(reopened, "홈페이지 확인 ");
            runOnMainSync(() -> check(contains(reopened, "연결됨 · ") != null, "connection check shows verified host and success state"));
            Thread.sleep(400); capture("settings-ui.png");
            runOnMainSync(() -> { find(reopened, android.widget.EditText.class, null).setText("https://unregistered.invalid"); find(reopened, android.widget.Button.class, "주소 설정 저장").performClick(); });
            waitForText(reopened, "사이트 연결 실패"); check(true, "unreachable site produces explicit failure badge");
        } finally {
            Activity close = activity; runOnMainSync(close::finish); android.content.SharedPreferences.Editor edit = prefs.edit();
            if (previousGuide == null) edit.remove("guide"); else edit.putString("guide", previousGuide); if (previousDomains == null) edit.remove("domains"); else edit.putString("domains", previousDomains); edit.commit();
        }
    }
    private android.app.Dialog detailsDialog(Activity activity) throws Exception {
        java.lang.reflect.Field field = MainActivity.class.getDeclaredField("detailsDialog"); field.setAccessible(true); return (android.app.Dialog) field.get(activity);
    }
    private void restoreWorks(String saved) { android.content.SharedPreferences.Editor edit = getTargetContext().getSharedPreferences("settings", 0).edit(); if (saved == null) edit.remove("recentWorks"); else edit.putString("recentWorks", saved); edit.commit(); }
    private void thumbnails() throws Exception {
        android.content.SharedPreferences prefs = getTargetContext().getSharedPreferences("settings", 0);
        String previous = prefs.getString("recentWorks", null); DownloadStore store = DownloadStore.get(getTargetContext());
        try {
            prefs.edit().putString("recentWorks", "[]").commit();
            DownloadStore.Job work = new DownloadStore.Job(); work.url = SiteClient.CANONICAL + "/world/10180/1"; work.contentUrl = SiteClient.CANONICAL + "/world/10180"; work.title = "삼국지 1화"; work.contentTitle = "삼국지"; work.bytes = 376; work.state = DownloadStore.DONE;
            store.rememberWork(work);
            DownloadStore.Job other = new DownloadStore.Job(); other.url = SiteClient.CANONICAL + "/world/10181/2"; other.contentUrl = SiteClient.CANONICAL + "/world/10181"; other.title = "삼국지 2화"; other.contentTitle = "삼국지"; other.thumbnail = "https://images.example/other.jpg"; other.bytes = 376; other.state = DownloadStore.DONE;
            store.rememberWork(other);
            String metadata = store.recentWorks().get(1).meta();
            store.rememberThumbnail(new SiteClient.Item("삼국지", work.contentUrl, "", "https://images.example/work.jpg"));
            List<SiteClient.Item> saved = store.recentWorks();
            check(saved.get(1).thumbnail().equals("https://images.example/work.jpg") && saved.get(0).thumbnail().equals(other.thumbnail), "thumbnail backfill matches exact work URL even for identical titles");
            check(saved.get(0).url().equals(other.contentUrl) && saved.get(1).meta().equals(metadata), "thumbnail backfill preserves recent order episode and completion");
            check(new JSONArray(prefs.getString("recentWorks", "[]")).getJSONObject(1).getString("thumbnail").equals("https://images.example/work.jpg"), "backfilled thumbnail is persisted in existing work record");
            String unchanged = prefs.getString("recentWorks", "[]");
            store.rememberThumbnail(new SiteClient.Item("삼국지", SiteClient.CANONICAL + "/world/10182", "", "https://images.example/wrong.jpg"));
            store.rememberThumbnail(new SiteClient.Item("삼국지", work.contentUrl, "", ""));
            check(unchanged.equals(prefs.getString("recentWorks", "[]")), "unknown works and empty images never replace or add recent records");
            store.rememberWork(work);
            check(store.recentWorks().get(0).thumbnail().equals("https://images.example/work.jpg"), "later downloads without poster metadata retain the saved thumbnail");
            work.thumbnail = "https://images.example/new.jpg"; store.rememberWork(work);
            check(store.recentWorks().get(0).thumbnail().equals(work.thumbnail), "new downloads retain their current search-result thumbnail");
        } finally { restoreWorks(previous); }
    }
    private void capture(String name) throws Exception {
        waitForIdleSync(); Thread.sleep(250);
        try (java.io.FileOutputStream output = new java.io.FileOutputStream(new File(getTargetContext().getFilesDir(), name))) { getUiAutomation().takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG, 100, output); }
    }
    private void swipeRow(Activity activity, String key, float horizontal, float vertical, boolean cancel) throws Exception {
        int[] bounds = new int[4];
        onUi(() -> {
            android.widget.ListView list = find(activity.getWindow().getDecorView(), android.widget.ListView.class, null);
            for (int i = 0; i < list.getCount(); i++) {
                Object item = list.getItemAtPosition(i); String id = item instanceof DownloadStore.Job job ? job.id : item instanceof SiteClient.Item work ? work.url() : "";
                if (!key.equals(id)) continue;
                android.view.View card = list.getChildAt(i - list.getFirstVisiblePosition()); if (card == null) throw new AssertionError("Swipe card is off screen");
                int[] location = new int[2]; card.getLocationOnScreen(location); bounds[0] = location[0]; bounds[1] = location[1]; bounds[2] = card.getWidth(); bounds[3] = card.getHeight(); return;
            }
            throw new AssertionError("Swipe card missing: " + key);
        });
        float x = bounds[0] + bounds[2] * (horizontal > 0 ? 0.2f : 0.65f), y = bounds[1] + bounds[3] * 0.65f;
        long down = android.os.SystemClock.uptimeMillis();
        sendPointerSync(android.view.MotionEvent.obtain(down, down, android.view.MotionEvent.ACTION_DOWN, x, y, 0));
        for (int i = 1; i <= 8; i++) { Thread.sleep(20); sendPointerSync(android.view.MotionEvent.obtain(down, android.os.SystemClock.uptimeMillis(), android.view.MotionEvent.ACTION_MOVE, x + bounds[2] * horizontal * i / 8, y + bounds[3] * vertical * i / 8, 0)); }
        sendPointerSync(android.view.MotionEvent.obtain(down, android.os.SystemClock.uptimeMillis(), cancel ? android.view.MotionEvent.ACTION_CANCEL : android.view.MotionEvent.ACTION_UP, x + bounds[2] * horizontal, y + bounds[3] * vertical, 0));
        waitForIdleSync(); Thread.sleep(150);
    }
    private void waitRemoved(Activity activity, DownloadStore store, String id) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 25000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            java.util.concurrent.atomic.AtomicBoolean busy = new java.util.concurrent.atomic.AtomicBoolean(); onUi(() -> busy.set((boolean) field(activity, "removingJobs")));
            if (!busy.get() && (id == null ? store.snapshot().isEmpty() : store.find(id) == null)) { waitForIdleSync(); return; }
            Thread.sleep(50);
        }
        throw new AssertionError("Removal did not finish");
    }
    private void deletion() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        // Recover only this check's fixtures if the dedicated emulator was interrupted.
        for (DownloadStore.Job job : store.snapshot()) if (job.title.startsWith("삭제 점검 작품 ")) { if (!job.output.isEmpty()) context.getContentResolver().delete(android.net.Uri.parse(job.output), null, null); store.update(job.id, current -> current.state = DownloadStore.PAUSED); store.remove(job.id); }
        check(store.snapshot().isEmpty(), "destructive UI checks require an empty dedicated device queue");
        String previous = context.getSharedPreferences("settings", 0).getString("recentWorks", null), base = SiteClient.CANONICAL + "/world/" + System.currentTimeMillis();
        Activity activity = null; android.net.Uri output = null;
        try {
            context.getSharedPreferences("settings", 0).edit().putString("recentWorks", "[]").commit();
            List<SiteClient.Item> items = Arrays.asList(new SiteClient.Item("삭제 점검 작품 1화", base + "/1", "", ""), new SiteClient.Item("삭제 점검 작품 2화", base + "/2", "", ""), new SiteClient.Item("삭제 점검 작품 3화", base + "/3", "", ""));
            store.add(items, new SiteClient.Item("삭제 점검 작품", base, "", "https://images.example/poster.jpg"));
            String done = store.snapshot().get(0).id, paused = store.snapshot().get(1).id, active = store.snapshot().get(2).id;
            android.content.ContentValues values = new android.content.ContentValues(); values.put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, "tvmon-delete-test-" + System.currentTimeMillis() + ".ts"); values.put(android.provider.MediaStore.MediaColumns.MIME_TYPE, "video/mp2t"); values.put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, "Download/TVMON"); output = context.getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values); if (output == null) throw new AssertionError("test output missing");
            try (java.io.OutputStream stream = context.getContentResolver().openOutputStream(output)) { stream.write(ts(4)); }
            String uri = output.toString(); store.update(done, job -> { job.state = DownloadStore.DONE; job.bytes = 376; job.output = uri; }); store.update(paused, job -> { job.state = DownloadStore.PAUSED; job.bytes = 376; }); store.update(active, job -> job.state = DownloadStore.PAUSED);
            File parts = store.parts(paused); parts.mkdirs(); Files.write(new File(parts, "0.ts").toPath(), ts(5)); store.rememberWork(store.find(done));
            DownloadStore.Job other = store.find(done); other.contentUrl = base + "1"; other.url = other.contentUrl + "/1"; store.rememberWork(other);
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity; android.view.View root = shown.getWindow().getDecorView(); waitForIdleSync();
            swipeRow(shown, other.contentUrl, -0.1f, 0, false); check(store.recentWorks().size() == 2, "short horizontal drags do not delete recent works");
            swipeRow(shown, other.contentUrl, -0.5f, 0, true); check(store.recentWorks().size() == 2, "canceled swipes restore the card without deleting it");
            swipeRow(shown, other.contentUrl, -0.5f, 0, false); check(store.recentWorks().size() == 1 && store.recentWorks().get(0).url().equals(base), "left swipe deletes only the exact recent work");
            check(store.snapshot().size() == 3 && "".equals(field(shown, "contentUrl")), "recent swipe neither opens episodes nor removes queue records");
            swipeRow(shown, base, 0.5f, 0, false); check(store.recentWorks().isEmpty(), "right swipe removes the last recent work");
            check(context.getSharedPreferences("settings", 0).getString("recentWorks", "").equals("[]"), "deleting the final recent work persists an empty history");
            store.rememberWork(store.find(done)); String recent = context.getSharedPreferences("settings", 0).getString("recentWorks", "");
            onUi(() -> tab(shown, 1)); waitForIdleSync();
            swipeRow(shown, paused, -0.05f, -0.5f, false); check(store.find(paused) != null, "vertical scrolling does not remove queue records"); onUi(() -> tab(shown, 1)); waitForIdleSync();
            swipeRow(shown, done, -0.5f, 0, false); waitRemoved(shown, store, done); check(store.find(done) == null, "completed card swipe removes the record without a confirmation dialog");
            try (InputStream stream = context.getContentResolver().openInputStream(output)) { check(Arrays.equals(ts(4), stream.readAllBytes()), "completed swipe preserves the saved public video"); }
            check(new File(parts, "0.ts").exists(), "completed deletion preserves unrelated unfinished parts");
            swipeRow(shown, paused, -0.5f, 0, false); accessibilityClick("취소"); check(store.find(paused) != null && new File(parts, "0.ts").exists(), "canceling unfinished deletion preserves record and received parts");
            swipeRow(shown, paused, -0.5f, 0, false); accessibilityClick("목록에서 삭제"); waitRemoved(shown, store, paused); check(!parts.exists(), "confirmed unfinished deletion removes only its temporary parts");
            store.update(active, job -> job.state = DownloadStore.RUNNING); onUi(() -> tab(shown, 1)); waitForIdleSync();
            swipeRow(shown, active, -0.5f, 0, false); capture("swipe-delete-running-confirm.png");
            check(getUiAutomation().getRootInActiveWindow().findAccessibilityNodeInfosByText("다운로드를 중단하고 목록에서 제거할까요?").size() > 0, "running card asks explicitly to stop downloading and remove the record"); accessibilityClick("취소");
            check(DownloadStore.RUNNING.equals(store.find(active).state), "canceling active deletion does not stop or alter the job");
            onUi(() -> find(root, android.widget.Button.class, "전체 삭제").performClick()); waitForIdleSync(); accessibilityClick("취소"); check(store.find(active) != null, "canceling clear-all leaves the queue unchanged");
            onUi(() -> find(root, android.widget.Button.class, "전체 삭제").performClick()); waitForIdleSync(); capture("swipe-delete-clear-confirm.png"); accessibilityClick("전체 삭제"); waitRemoved(shown, store, null);
            check(store.snapshot().isEmpty() && !TransferScheduler.removing(), "clear-all removes the active record and releases deletion state");
            onUi(() -> check(((android.widget.Button) field(shown, "queueClear")).getVisibility() == android.view.View.GONE, "empty queue hides the clear-all action"));
            check(recent.equals(context.getSharedPreferences("settings", 0).getString("recentWorks", "")), "clearing the queue preserves recent works including their thumbnails");
            try (InputStream stream = context.getContentResolver().openInputStream(output)) { check(Arrays.equals(ts(4), stream.readAllBytes()), "clear-all preserves completed public video bytes"); }
            capture("swipe-delete-empty.png");
        } finally {
            if (activity != null) { Activity close = activity; onUi(close::finish); }
            for (DownloadStore.Job job : store.snapshot()) if (job.url.startsWith(base + "/")) { store.update(job.id, current -> current.state = DownloadStore.PAUSED); store.remove(job.id); }
            if (output != null) context.getContentResolver().delete(output, null, null); restoreWorks(previous);
        }
    }
    private void deletionLive() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context); check(store.snapshot().isEmpty(), "live deletion requires an empty dedicated device queue");
        String previous = context.getSharedPreferences("settings", 0).getString("recentWorks", null); Activity activity = null;
        try {
            SiteClient client = new SiteClient(SiteClient.domains(context.getSharedPreferences("settings", 0).getString("domains", SiteClient.DEFAULT_DOMAINS)));
            SiteClient.Item work = client.search("닥터X", 1, new SiteClient.Stop()).items().get(0); List<SiteClient.Item> episodes = client.episodes(work.url(), new SiteClient.Stop()); store.add(episodes.subList(0, 2), work);
            String first = store.snapshot().get(0).id, second = store.snapshot().get(1).id; File firstParts = store.parts(first), secondParts = store.parts(second);
            try (android.os.ParcelFileDescriptor ignored = getUiAutomation().executeShellCommand("pm grant com.tvmon.android android.permission.POST_NOTIFICATIONS")) { }
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity;
            onUi(() -> { tab(shown, 1); find(shown.getWindow().getDecorView(), android.widget.Button.class, "전체 시작").performClick(); });
            long deadline = android.os.SystemClock.elapsedRealtime() + 90000; while (store.find(first).done < 2 && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(store.find(first).done >= 2 && DownloadEngine.running(), "real background download receives parts before swipe deletion"); waitForIdleSync(); swipeRow(shown, first, -0.5f, 0, false); accessibilityClick("중단하고 제거"); waitRemoved(shown, store, first);
            check(!firstParts.exists(), "active removal waits for transfer workers and removes their parts");
            deadline = android.os.SystemClock.elapsedRealtime() + 90000; while (store.find(second).done < 2 && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(store.find(second).done >= 2 && DownloadEngine.running(), "remaining waiting download starts automatically after active deletion");
            onUi(() -> find(shown.getWindow().getDecorView(), android.widget.Button.class, "전체 삭제").performClick()); waitForIdleSync(); capture("swipe-delete-live-clear-confirm.png"); accessibilityClick("전체 삭제"); waitRemoved(shown, store, null); Thread.sleep(800);
            check(!DownloadEngine.running() && !firstParts.exists() && !secondParts.exists(), "live clear-all stops downloading without recreating deleted parts");
            check(!store.recentWorks().isEmpty(), "live queue deletion retains the downloaded work history");
        } finally {
            store.pauseAll(); DownloadEngine.cancel(); context.getSystemService(android.app.job.JobScheduler.class).cancel(TransferScheduler.JOB);
            long deadline = android.os.SystemClock.elapsedRealtime() + 20000; while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            for (DownloadStore.Job job : store.snapshot()) store.remove(job.id);
            if (activity != null) { Activity close = activity; onUi(close::finish); } restoreWorks(previous);
        }
    }
    private android.view.View queueCard(Activity activity, String id) {
        return queueCard(activity, id, null);
    }
    private android.view.View queueCard(Activity activity, String id, android.view.View recycled) {
        android.widget.ListView list = find(activity.getWindow().getDecorView(), android.widget.ListView.class, null);
        for (int i = 0; i < list.getCount(); i++) if (list.getItemAtPosition(i) instanceof DownloadStore.Job job && id.equals(job.id)) return list.getAdapter().getView(i, recycled, list);
        throw new AssertionError("Queue card missing: " + id);
    }
    private void waitIndividualPause(Activity activity) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 25000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            java.util.concurrent.atomic.AtomicBoolean busy = new java.util.concurrent.atomic.AtomicBoolean(); onUi(() -> busy.set((boolean) field(activity, "pausingJob")));
            if (!busy.get() && !TransferScheduler.busy()) { waitForIdleSync(); return; } Thread.sleep(50);
        }
        throw new AssertionError("Individual pause did not finish");
    }
    private void queueControls() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context); check(store.snapshot().isEmpty(), "queue controls checks require an empty dedicated device queue");
        String base = SiteClient.CANONICAL + "/world/" + System.currentTimeMillis(); Activity activity = null;
        try {
            store.add(Arrays.asList(new SiteClient.Item("아주 긴 영상 제목과 큰 용량을 표시하는 다운로드 대기열 점검 작품 123화", base + "/1", "", ""), new SiteClient.Item("대기열 점검 작품 2화", base + "/2", "", "")));
            String first = store.snapshot().get(0).id, second = store.snapshot().get(1).id;
            store.update(first, job -> { job.state = DownloadStore.RUNNING; job.done = 123; job.total = 1000; job.bytes = 302200000L; job.durationSeconds = 3661; job.partStates = new byte[1000]; Arrays.fill(job.partStates, 0, 123, (byte) 2); });
            store.update(second, job -> { job.state = DownloadStore.DONE; job.bytes = 2500000000L; job.durationSeconds = 3600; });
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity;
            onUi(() -> tab(shown, 1)); waitForIdleSync();
            final int[] runningHeight = {0}, pausedHeight = {0}, actionBounds = new int[4]; final android.view.View[] recycled = new android.view.View[1];
            onUi(() -> {
                check(!((android.view.View) field(shown, "queueActions")).isShown(), "one running job plus completed jobs hides global controls");
                android.view.View card = queueCard(shown, first); Object holder = card.getTag(); android.widget.Button button = (android.widget.Button) field(holder, "action");
                int width = (int) (280 * context.getResources().getDisplayMetrics().density); card.measure(android.view.View.MeasureSpec.makeMeasureSpec(width, android.view.View.MeasureSpec.EXACTLY), android.view.View.MeasureSpec.makeMeasureSpec(0, android.view.View.MeasureSpec.UNSPECIFIED)); card.layout(0, 0, width, card.getMeasuredHeight()); runningHeight[0] = card.getMeasuredHeight();
                recycled[0] = card; actionBounds[0] = button.getLeft(); actionBounds[1] = button.getTop(); actionBounds[2] = button.getWidth(); actionBounds[3] = button.getHeight();
                check("일시정지".contentEquals(button.getText()) && button.getParent() == field(holder, "top") && button.getTop() == 0 && button.getRight() == ((android.view.View) field(holder, "top")).getWidth(), "individual pause stays at the top right and never uses an all-pause label");
                android.widget.TextView size = (android.widget.TextView) field(holder, "size"), title = (android.widget.TextView) field(holder, "title"), metrics = (android.widget.TextView) field(holder, "metrics");
                android.view.View transfer = (android.view.View) field(holder, "transfer"); check(size.getParent() == metrics.getParent() && transfer.getWidth() == card.getWidth() - card.getPaddingLeft() - card.getPaddingRight() && size.getRight() <= metrics.getLeft() && metrics.getRight() == transfer.getWidth() && (metrics.getGravity() & android.view.Gravity.END) == android.view.Gravity.END && size.getText().toString().contains(" / ") && !size.getText().toString().contains("약"), "capacity stays left without the approximate prefix and speed/ETA align right without overlap");
                check(title.getMaxLines() == 2 && button.getHeight() >= 48 * context.getResources().getDisplayMetrics().density && button.getLineCount() == 1, "long title is bounded and pause keeps a 48dp single-line target");
                check(((android.widget.TextView) field(holder, "meta")).getLineCount() == 1, "playback duration uses the full width when needed without wrapping beside the button");
                check(!metrics.getText().toString().contains("남음"), "zero speed never produces a fabricated remaining time");
                check(((android.widget.ProgressBar) field(holder, "progress")).getProgress() == 123 && contains(card, "12%") != null, "progress and percentage reflect the same saved segment count");
                button.performClick();
            }); waitIndividualPause(shown);
            DownloadStore.Job paused = store.find(first); check(DownloadStore.PAUSED.equals(paused.state) && paused.done == 123 && paused.bytes == 302200000L && paused.partStates[122] == 2, "individual pause preserves saved progress bytes and segment states");
            check(DownloadStore.DONE.equals(store.find(second).state), "individual pause leaves completed records untouched");
            onUi(() -> {
                android.view.View card = queueCard(shown, first, recycled[0]); Object holder = card.getTag(); int width = (int) (280 * context.getResources().getDisplayMetrics().density); card.measure(android.view.View.MeasureSpec.makeMeasureSpec(width, android.view.View.MeasureSpec.EXACTLY), android.view.View.MeasureSpec.makeMeasureSpec(0, android.view.View.MeasureSpec.UNSPECIFIED)); card.layout(0, 0, width, card.getMeasuredHeight()); pausedHeight[0] = card.getMeasuredHeight(); android.widget.Button action = (android.widget.Button) field(holder, "action");
                check(card == recycled[0] && action.getLeft() == actionBounds[0] && action.getTop() == actionBounds[1] && action.getWidth() == actionBounds[2] && action.getHeight() == actionBounds[3], "recycled paused cards keep exactly the same control position and size");
                check(card.getMeasuredHeight() <= runningHeight[0] && ((android.view.View) field(holder, "metrics")).getVisibility() == android.view.View.GONE && ((android.view.View) field(holder, "metrics")).getParent() == field(holder, "transfer"), "paused cards hide speed and ETA without leaving a separate metrics row");
                check(find(card, android.widget.Button.class, "재개") != null && !((android.view.View) field(shown, "queueActions")).isShown(), "single paused job offers individual resume only");
                tab(shown, 2); tab(shown, 1);
            }); waitForIdleSync(); capture("queue-controls-single-paused.png");
            store.resume(first); onUi(() -> tab(shown, 1)); waitForIdleSync();
            onUi(() -> {
                android.view.View card = queueCard(shown, first, recycled[0]); Object holder = card.getTag(); int width = (int) (280 * context.getResources().getDisplayMetrics().density); card.measure(android.view.View.MeasureSpec.makeMeasureSpec(width, android.view.View.MeasureSpec.EXACTLY), android.view.View.MeasureSpec.makeMeasureSpec(0, android.view.View.MeasureSpec.UNSPECIFIED)); card.layout(0, 0, width, card.getMeasuredHeight()); android.widget.Button action = (android.widget.Button) field(holder, "action");
                check("시작".contentEquals(action.getText()) && action.getLeft() == actionBounds[0] && action.getTop() == actionBounds[1] && action.getWidth() == actionBounds[2] && action.getHeight() == actionBounds[3], "waiting start retains the running and paused control bounds");
                check("대기 중".contentEquals(((android.widget.TextView) field(holder, "meta")).getText()) && ((android.view.View) field(holder, "state")).getVisibility() == android.view.View.GONE && ((android.view.View) field(holder, "progress")).getVisibility() == android.view.View.GONE && ((android.view.View) field(holder, "size")).getVisibility() == android.view.View.GONE && ((android.view.View) field(holder, "metrics")).getVisibility() == android.view.View.GONE, "waiting cards show only title status and start without bytes or a progress bar");
                check(card.getMeasuredHeight() < pausedHeight[0], "waiting cards are more compact than paused and running cards");
            }); capture("queue-top-actions-waiting.png"); store.pause(first); onUi(() -> tab(shown, 1)); waitForIdleSync();
            check(store.find(first).done == 123 && DownloadStore.PAUSED.equals(store.find(first).state), "tab navigation preserves individual pause and progress");
            store.update(second, job -> job.state = DownloadStore.PAUSED);
            onUi(() -> tab(shown, 1)); waitForIdleSync();
            onUi(() -> check(!((android.view.View) field(shown, "queuePause")).isShown() && ((android.view.View) field(shown, "queueStart")).isShown(), "two paused jobs expose only global resume"));
            store.update(first, job -> job.state = DownloadStore.RUNNING); onUi(() -> tab(shown, 1)); waitForIdleSync(); Thread.sleep(250);
            onUi(() -> {
                check(((android.view.View) field(shown, "queuePause")).isShown() && ((android.view.View) field(shown, "queueStart")).isShown(), "mixed running and paused jobs expose both global actions");
                android.widget.Button pause = (android.widget.Button) field(shown, "queuePause"), start = (android.widget.Button) field(shown, "queueStart");
                check(pause.getLineCount() == 1 && start.getLineCount() == 1 && pause.getWidth() - pause.getPaddingLeft() - pause.getPaddingRight() >= pause.getPaint().measureText(pause.getText().toString()), "global controls preserve complete single-line labels at the current window and font size (width=" + pause.getWidth() + ", text=" + pause.getPaint().measureText(pause.getText().toString()) + ", lines=" + pause.getLineCount() + "/" + start.getLineCount() + ")");
                android.widget.ListView list = find(shown.getWindow().getDecorView(), android.widget.ListView.class, null); check(list.getHeight() > 0, "global controls leave a scrollable queue viewport"); list.setSelection(list.getHeaderViewsCount());
            }); waitForIdleSync(); capture("queue-controls-mixed.png");
            store.update(second, job -> job.state = DownloadStore.RUNNING); onUi(() -> tab(shown, 1)); waitForIdleSync();
            onUi(() -> check(((android.view.View) field(shown, "queuePause")).isShown() && !((android.view.View) field(shown, "queueStart")).isShown(), "multiple running records expose global pause only"));
            store.update(second, job -> job.state = DownloadStore.WAITING); store.pause(second);
            check(DownloadStore.RUNNING.equals(store.find(first).state) && DownloadStore.PAUSED.equals(store.find(second).state), "pausing a waiting job leaves the running job unchanged");
            store.resume(second); check(DownloadStore.WAITING.equals(store.find(second).state) && DownloadStore.RUNNING.equals(store.find(first).state), "individual resume queues only its selected job");
            store.update(second, job -> job.state = DownloadStore.DONE); store.update(first, job -> { long now = android.os.SystemClock.elapsedRealtime(); job.transferStarted = now - 1000; int slot = (int) (now / 1000 % 5); job.speedSeconds[slot] = now / 1000; job.speedBytes[slot] = 4500000; });
            onUi(() -> { tab(shown, 1); android.widget.ListView list = find(shown.getWindow().getDecorView(), android.widget.ListView.class, null); list.setSelection(list.getHeaderViewsCount()); }); waitForIdleSync(); capture("queue-controls-single-running.png");
            onUi(() -> { android.view.View card = queueCard(shown, first); android.widget.TextView metrics = (android.widget.TextView) field(card.getTag(), "metrics"); check(metrics.getText().toString().contains("남음") && metrics.getText().toString().contains("/s") && !metrics.getText().toString().contains("\n") && !metrics.getText().toString().contains("약") && metrics.getMaxLines() == 1 && Math.abs(metrics.getTextScaleX() - 0.95f) < 0.001f, "running card omits the approximate prefix and slightly narrows the right-hand single-line text"); });
            store.update(first, job -> { job.state = DownloadStore.DONE; job.done = job.total; }); onUi(() -> tab(shown, 1)); waitForIdleSync();
            onUi(() -> {
                android.view.View card = queueCard(shown, first); Object holder = card.getTag();
                check(find(card, android.widget.Button.class, "재생") != null && ((android.view.View) field(holder, "size")).getVisibility() == android.view.View.GONE && ((android.view.View) field(holder, "metrics")).getVisibility() == android.view.View.GONE, "completion switches to a compact playable card without transfer metrics");
                check(!((android.view.View) field(shown, "queueActions")).isShown(), "all completed records hide the global control area");
            }); capture("queue-controls-complete.png");
            store.add(Arrays.asList(new SiteClient.Item("대기 중인 작품 3화", base + "/3", "", ""), new SiteClient.Item("제목이 아주 긴 대기 중인 작품도 버튼과 겹치지 않는지 확인하는 4화", base + "/4", "", ""), new SiteClient.Item("대기 중인 작품 5화", base + "/5", "", ""))); onUi(() -> tab(shown, 1)); waitForIdleSync(); capture("queue-top-actions-waiting-list.png");
        } finally {
            if (activity != null) { Activity close = activity; onUi(close::finish); }
            for (DownloadStore.Job job : store.snapshot()) if (job.url.startsWith(base + "/")) { store.update(job.id, current -> current.state = DownloadStore.PAUSED); store.remove(job.id); }
        }
    }
    private void waitReceived(DownloadStore store, String id, int minimum) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 90000;
        while (store.find(id).done < minimum && android.os.SystemClock.elapsedRealtime() < deadline) { if (DownloadStore.FAILED.equals(store.find(id).state)) throw new AssertionError(store.find(id).message); Thread.sleep(100); }
        check(store.find(id).done >= minimum && DownloadEngine.running(), "real transfer received at least " + minimum + " saved segments");
    }
    private void queueControlsLive() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context); check(store.snapshot().isEmpty(), "live controls checks require an empty dedicated device queue");
        String previous = context.getSharedPreferences("settings", 0).getString("recentWorks", null); List<String> owned = new ArrayList<>(); Activity activity = null;
        try {
            SiteClient client = new SiteClient(SiteClient.domains(context.getSharedPreferences("settings", 0).getString("domains", SiteClient.DEFAULT_DOMAINS)));
            SiteClient.Item work = client.search("닥터X", 1, new SiteClient.Stop()).items().get(0); store.add(client.episodes(work.url(), new SiteClient.Stop()).subList(0, 2), work); for (DownloadStore.Job job : store.snapshot()) owned.add(job.id);
            String first = owned.get(0), second = owned.get(1);
            try (android.os.ParcelFileDescriptor ignored = getUiAutomation().executeShellCommand("pm grant com.tvmon.android android.permission.POST_NOTIFICATIONS")) { }
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity;
            onUi(() -> { tab(shown, 1); ((android.widget.Button) field(shown, "queueStart")).performClick(); }); waitReceived(store, first, 3); capture("queue-controls-live-running.png");
            onUi(() -> find(queueCard(shown, first), android.widget.Button.class, "일시정지").performClick()); waitIndividualPause(shown);
            DownloadStore.Job checkpoint = store.find(first); check(DownloadStore.PAUSED.equals(checkpoint.state) && checkpoint.done >= 3 && new File(store.parts(first), "state.json").isFile(), "individual pause retains the real checkpoint and saved parts");
            waitReceived(store, second, 3); check(DownloadStore.PAUSED.equals(store.find(first).state) && store.find(first).done == checkpoint.done, "other waiting episode continues while the selected episode stays paused"); capture("queue-controls-live-mixed.png");
            onUi(() -> find(queueCard(shown, first), android.widget.Button.class, "재개").performClick());
            check(DownloadStore.WAITING.equals(store.find(first).state) && DownloadStore.RUNNING.equals(store.find(second).state), "individual resume preserves the existing sequential queue policy");
            onUi(() -> { tab(shown, 2); tab(shown, 1); }); int beforeBackground = store.find(second).done;
            try (android.os.ParcelFileDescriptor ignored = getUiAutomation().executeShellCommand("input keyevent KEYCODE_HOME")) { } Thread.sleep(1800);
            try (android.os.ParcelFileDescriptor command = getUiAutomation().executeShellCommand("am start -n com.tvmon.android/.MainActivity"); InputStream stream = new android.os.ParcelFileDescriptor.AutoCloseInputStream(command)) { stream.readAllBytes(); } Activity returned = shown; onUi(() -> tab(returned, 1)); waitForIdleSync();
            check(store.find(second).done >= beforeBackground && DownloadEngine.running(), "background and tab return keep the active transfer and saved progress");
            onUi(() -> ((android.widget.Button) field(returned, "queuePause")).performClick());
            long deadline = android.os.SystemClock.elapsedRealtime() + 25000; while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            check(!DownloadEngine.running() && DownloadStore.PAUSED.equals(store.find(first).state) && DownloadStore.PAUSED.equals(store.find(second).state), "global pause still pauses running and waiting episodes together");
            onUi(() -> tab(returned, 1)); waitForIdleSync(); capture("queue-controls-live-all-paused.png");
            onUi(() -> ((android.widget.Button) field(returned, "queueStart")).performClick()); waitReceived(store, first, checkpoint.done + 2);
            check(store.find(first).bytes >= checkpoint.bytes && new File(store.parts(first), "state.json").isFile(), "global resume continues the saved episode without resetting received bytes");
            onUi(() -> find(queueCard(returned, first), android.widget.Button.class, "일시정지").performClick()); waitIndividualPause(returned); waitReceived(store, second, beforeBackground + 2);
            check(DownloadStore.PAUSED.equals(store.find(first).state), "repeated individual pause still leaves the other episode running");
        } finally {
            for (String id : owned) store.pause(id); DownloadEngine.cancel(); context.getSystemService(android.app.job.JobScheduler.class).cancel(TransferScheduler.JOB); context.stopService(new android.content.Intent(context, TransferService.class));
            long deadline = android.os.SystemClock.elapsedRealtime() + 25000; while (DownloadEngine.running() && android.os.SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            if (activity != null) { Activity close = activity; onUi(close::finish); }
            for (String id : owned) store.remove(id); restoreWorks(previous);
        }
    }
    private void tapView(android.view.View view, float xFraction, float yFraction) throws Exception {
        final float[] point = new float[2]; onUi(() -> { int[] location = new int[2]; view.getLocationOnScreen(location); point[0] = location[0] + view.getWidth() * xFraction; point[1] = location[1] + view.getHeight() * yFraction; });
        long now = android.os.SystemClock.uptimeMillis(); sendPointerSync(android.view.MotionEvent.obtain(now, now, android.view.MotionEvent.ACTION_DOWN, point[0], point[1], 0)); sendPointerSync(android.view.MotionEvent.obtain(now, now + 50, android.view.MotionEvent.ACTION_UP, point[0], point[1], 0)); waitForIdleSync();
    }
    private void waitLibrary(Activity activity) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 8000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) { java.util.concurrent.atomic.AtomicBoolean ready = new java.util.concurrent.atomic.AtomicBoolean(); onUi(() -> ready.set(((android.widget.Button) field(activity, "libraryRefresh")).isEnabled())); if (ready.get()) { waitForIdleSync(); Thread.sleep(200); return; } Thread.sleep(50); }
        throw new AssertionError("Library load did not finish");
    }
    private android.view.View savedCard(Activity activity, String name) {
        android.widget.ListView list = find(activity.getWindow().getDecorView(), android.widget.ListView.class, null);
        for (int i = 0; i < list.getChildCount(); i++) { android.view.View card = list.getChildAt(i); if (card.getTag() != null && name.equals(field(field(card.getTag(), "video"), "name"))) return card; }
        throw new AssertionError("Visible saved file missing: " + name);
    }
    private void library() throws Exception {
        Context context = getTargetContext(); List<android.net.Uri> outputs = new ArrayList<>(); Activity activity = null; ActivityMonitor monitor = null;
        DownloadStore store = DownloadStore.get(context); String queueId = null;
        String prefix = "보관함 점검 아주 긴 영상 제목과 아이콘 배치를 확인하는 작품 tvmon-library-check-" + System.currentTimeMillis(), history = context.getSharedPreferences("settings", 0).getString("recentWorks", null);
        byte[] firstBytes = new byte[188 * 6000]; Arrays.fill(firstBytes, (byte) 6); for (int i = 0; i < firstBytes.length; i += 188) firstBytes[i] = 0x47;
        try (android.database.Cursor cursor = context.getContentResolver().query(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, new String[]{"_id"}, "relative_path=? AND is_pending=0 AND _size>0", new String[]{"Download/TVMON/"}, null)) { check(cursor != null && cursor.getCount() == 0, "library deletion and empty-state checks require empty test storage"); }
        try {
            for (int i = 0; i < 5; i++) {
                android.content.ContentValues values = new android.content.ContentValues(); values.put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, prefix + "-" + (i == 4 ? 3 : i) + (i == 4 ? ".mp4" : ".ts")); values.put(android.provider.MediaStore.MediaColumns.MIME_TYPE, i == 4 ? "video/mp4" : "video/mp2t"); values.put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, i == 2 ? "Download" : "Download/TVMON"); values.put(android.provider.MediaStore.MediaColumns.IS_PENDING, i == 1 ? 1 : 0);
                android.net.Uri uri = context.getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values); if (uri == null) throw new AssertionError("test file missing"); outputs.add(uri); try (java.io.OutputStream stream = context.getContentResolver().openOutputStream(uri)) { stream.write(i == 0 ? firstBytes : ts(6)); }
            }
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity; android.view.View root = shown.getWindow().getDecorView();
            onUi(() -> find(root, android.widget.Button.class, "보관함").performClick());
            waitLibrary(shown);
            final android.view.View[] card = new android.view.View[1]; final android.content.Intent[] launched = new android.content.Intent[1]; AtomicInteger plays = new AtomicInteger();
            onUi(() -> {
                check(((android.widget.Button[]) field(shown, "navigation")).length == 4 && find(root, android.widget.Button.class, "보관함").isSelected(), "library appears as the fourth destination with correct selected state");
                android.widget.ListView list = find(root, android.widget.ListView.class, null); int position = -1; boolean pending = false, outside = false;
                for (int i = 0; i < list.getCount(); i++) { Object item = list.getItemAtPosition(i); if (item == null) continue; String name = (String) field(item, "name"); if (name.equals(prefix + "-0.ts")) position = i; if (name.equals(prefix + "-1.ts")) pending = true; if (name.equals(prefix + "-2.ts")) outside = true; }
                check(position >= 0, "library lists the actual saved file without a queue record"); check(!pending && !outside, "library excludes pending files and files outside Download/TVMON");
                card[0] = list.getAdapter().getView(position, null, list); check(list.getAdapter().getView(position, card[0], list) == card[0], "library scrolling reuses its file card");
                Object holder = card[0].getTag(); check((prefix + "-0").contentEquals(((android.widget.TextView) field(holder, "title")).getText()), "default title omits ts without renaming the file");
                String date = android.text.format.DateFormat.format("yyyy.MM.dd", new java.util.Date((long) field(field(holder, "video"), "added") * 1000)).toString(); check(((android.widget.TextView) field(holder, "meta")).getText().toString().endsWith(date), "saved date uses actual file metadata with a four-digit year");
                boolean distinct = false; for (int i = 0; i < list.getCount(); i++) if (list.getItemAtPosition(i) != null && (prefix + "-3.ts").equals(field(list.getItemAtPosition(i), "name"))) { android.view.View duplicate = list.getAdapter().getView(i, null, list); distinct = (prefix + "-3.ts").contentEquals(((android.widget.TextView) field(duplicate.getTag(), "title")).getText()); }
                check(distinct, "same title with different extensions keeps the extension for distinction");
                boolean formatShown = false; for (int i = 0; i < list.getCount(); i++) if (list.getItemAtPosition(i) != null && (prefix + "-3.ts").equals(field(list.getItemAtPosition(i), "name"))) { android.view.View duplicate = list.getAdapter().getView(i, null, list); formatShown = ((android.widget.TextView) field(duplicate.getTag(), "meta")).getText().toString().startsWith("TS · "); } check(formatShown, "extension stays visible in metadata even when the matching long title is ellipsized");
                check(find(card[0], android.widget.Button.class, "공유") == null && field(holder, "play") instanceof android.widget.ImageButton && field(holder, "more") instanceof android.widget.ImageButton, "library uses play and more icons without a share or inline delete button");
                list.setSelection(position);
            });
            waitForIdleSync(); Thread.sleep(250); onUi(() -> card[0] = savedCard(shown, prefix + "-0.ts"));
            monitor = new ActivityMonitor() { @Override public ActivityResult onStartActivity(android.content.Intent intent) { plays.incrementAndGet(); launched[0] = new android.content.Intent(intent); return new ActivityResult(Activity.RESULT_CANCELED, null); } }; addMonitor(monitor);
            tapView((android.view.View) field(card[0].getTag(), "title"), .3f, .5f); check(plays.get() == 1, "touching the actual title starts playback once");
            tapView((android.view.View) field(card[0].getTag(), "meta"), .3f, .5f); check(plays.get() == 2, "touching capacity and date starts playback once");
            tapView(card[0], .02f, .9f); check(plays.get() == 3, "touching blank card padding starts playback once");
            tapView((android.view.View) field(card[0].getTag(), "play"), .5f, .5f); check(plays.get() == 4, "touching the play icon starts playback once");
            check(android.content.Intent.ACTION_VIEW.equals(launched[0].getAction()) && outputs.get(0).equals(launched[0].getData()) && (launched[0].getFlags() & android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0, "library playback retains the saved content URI and read grant");
            onUi(() -> {
                Object holder = card[0].getTag(); android.view.View play = (android.view.View) field(holder, "play"), more = (android.view.View) field(holder, "more"); float density = context.getResources().getDisplayMetrics().density;
                check(play.getWidth() >= 48 * density && play.getHeight() >= 48 * density && more.getWidth() >= 48 * density && more.getHeight() >= 48 * density, "long file names leave both icon touch targets at least 48dp");
                check(card[0].getBackground() instanceof android.graphics.drawable.RippleDrawable && more.getContentDescription().toString().endsWith(", 더보기"), "card ripple and labeled more button expose their actions accessibly");
                check(((android.widget.TextView) field(holder, "title")).getMaxLines() == 2, "long file names are limited to two ellipsized lines");
            });
            capture("library-file-list.png");
            tapView((android.view.View) field(card[0].getTag(), "more"), .5f, .5f);
            android.app.Dialog menu = (android.app.Dialog) field(shown, "libraryMenuDialog"); check(menu != null && menu.isShowing() && plays.get() == 4, "more touch opens only the menu and never propagates playback"); capture("library-playback-menu.png");
            onUi(() -> find(menu.getWindow().getDecorView(), android.widget.Button.class, "영상 재생").performClick()); check(plays.get() == 5 && !menu.isShowing() && outputs.get(0).equals(launched[0].getData()), "menu playback closes the sheet and opens the same file");
            String queueUrl = SiteClient.CANONICAL + "/world/" + System.currentTimeMillis() + "/1"; store.add(Collections.singletonList(new SiteClient.Item(prefix, queueUrl, "", ""))); queueId = store.snapshot().stream().filter(job -> queueUrl.equals(job.url)).findFirst().orElseThrow().id;
            String uri = outputs.get(0).toString(); store.update(queueId, job -> { job.state = DownloadStore.DONE; job.output = uri; job.bytes = firstBytes.length; });
            onUi(() -> { ((android.view.View) field(card[0].getTag(), "more")).performClick(); android.app.Dialog actions = (android.app.Dialog) field(shown, "libraryMenuDialog"); find(actions.getWindow().getDecorView(), android.widget.Button.class, "삭제").performClick(); check(!actions.isShowing(), "delete closes the menu before displaying confirmation"); }); waitForIdleSync(); capture("library-file-delete-confirm.png");
            check(getUiAutomation().getRootInActiveWindow().findAccessibilityNodeInfosByText(prefix + "-0.ts").size() > 0, "file deletion confirmation names the exact video"); accessibilityClick("취소");
            try (InputStream stream = context.getContentResolver().openInputStream(outputs.get(0))) { check(Arrays.equals(firstBytes, stream.readAllBytes()) && store.find(queueId) != null, "canceling file deletion preserves actual bytes and completed record"); }
            onUi(() -> check(((android.widget.TextView) field(shown, "libraryStatus")).getText().toString().startsWith("영상 3개"), "canceling deletion preserves the library count"));
            onUi(() -> { ((android.view.View) field(card[0].getTag(), "more")).performClick(); android.app.Dialog actions = (android.app.Dialog) field(shown, "libraryMenuDialog"); find(actions.getWindow().getDecorView(), android.widget.Button.class, "삭제").performClick(); }); waitForIdleSync(); accessibilityClick("파일 삭제");
            long removed = android.os.SystemClock.elapsedRealtime() + 5000;
            while (android.os.SystemClock.elapsedRealtime() < removed) { java.util.concurrent.atomic.AtomicBoolean missing = new java.util.concurrent.atomic.AtomicBoolean(); onUi(() -> { @SuppressWarnings("unchecked") List<Object> saved = (List<Object>) field(shown, "savedVideos"); missing.set(saved.stream().noneMatch(item -> (prefix + "-0.ts").equals(field(item, "name")))); }); if (missing.get()) break; Thread.sleep(50); }
            try (android.database.Cursor cursor = context.getContentResolver().query(outputs.get(0), new String[]{android.provider.MediaStore.MediaColumns._ID}, null, null, null)) { check(cursor == null || !cursor.moveToFirst(), "confirming file deletion removes the actual MediaStore video"); }
            check(store.find(queueId) == null, "file deletion removes only the completed record referring to that file");
            check(java.util.Objects.equals(history, context.getSharedPreferences("settings", 0).getString("recentWorks", null)), "file deletion keeps recent work history unchanged");
            waitLibrary(shown); onUi(() -> check("영상 2개 · 0.0 MB".contentEquals(((android.widget.TextView) field(shown, "libraryStatus")).getText()), "successful deletion refreshes both the file count and aggregate size"));
            onUi(() -> { tab(shown, 1); tab(shown, 3); }); waitLibrary(shown); onUi(() -> check(((java.util.List<?>) field(shown, "savedVideos")).size() == 2, "returning to the library reloads the remaining files"));
            onUi(() -> { android.widget.ListView list = find(root, android.widget.ListView.class, null); for (int i = 0; i < list.getCount(); i++) if (list.getItemAtPosition(i) != null && (prefix + "-3.ts").equals(field(list.getItemAtPosition(i), "name"))) { android.view.View stale = list.getAdapter().getView(i, null, list); ((android.view.View) field(stale.getTag(), "more")).performClick(); android.app.Dialog actions = (android.app.Dialog) field(shown, "libraryMenuDialog"); find(actions.getWindow().getDecorView(), android.widget.Button.class, "삭제").performClick(); break; } });
            context.getContentResolver().delete(outputs.get(3), null, null); accessibilityClick("파일 삭제");
            long failureDeadline = android.os.SystemClock.elapsedRealtime() + 5000; boolean failureShown = false; while (android.os.SystemClock.elapsedRealtime() < failureDeadline) { android.view.accessibility.AccessibilityNodeInfo window = getUiAutomation().getRootInActiveWindow(); if (window != null && !window.findAccessibilityNodeInfosByText("영상 파일을 삭제하지 못했습니다.").isEmpty()) { failureShown = true; break; } Thread.sleep(50); } check(failureShown, "a failed file deletion reports failure instead of claiming success"); accessibilityClick("확인");
            context.getContentResolver().delete(outputs.get(4), null, null);
            onUi(() -> find(root, android.widget.Button.class, "새로고침").performClick()); waitLibrary(shown);
            onUi(() -> { @SuppressWarnings("unchecked") List<Object> saved = (List<Object>) field(shown, "savedVideos"); check(saved.stream().noneMatch(item -> (prefix + "-3.ts").equals(field(item, "name"))), "refresh removes files deleted from phone storage"); });
            onUi(() -> check(((android.view.View) field(shown, "libraryEmpty")).isShown() && "영상 0개 · 0.0 MB".contentEquals(((android.widget.TextView) field(shown, "libraryStatus")).getText()), "empty library explains where downloaded videos appear and shows zero count")); capture("library-playback-empty.png");
        } finally {
            if (monitor != null) removeMonitor(monitor); if (activity != null) { Activity close = activity; onUi(close::finish); }
            if (queueId != null) { store.update(queueId, job -> job.state = DownloadStore.PAUSED); store.remove(queueId); }
            for (android.net.Uri uri : outputs) context.getContentResolver().delete(uri, null, null);
        }
    }
    private void ui() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        String previousWorks = context.getSharedPreferences("settings", 0).getString("recentWorks", null);
        String url = SiteClient.CANONICAL + "/ent/2/" + System.currentTimeMillis(); store.add(Collections.singletonList(new SiteClient.Item("나를 충전해줘 1화", url, "", "")));
        DownloadStore.Job fixture = null; for (DownloadStore.Job job : store.snapshot()) if (job.url.equals(url)) fixture = job;
        if (fixture == null) throw new AssertionError("detail fixture missing"); String id = fixture.id;
        store.update(id, job -> {
            job.state = DownloadStore.RUNNING; job.total = 1857; job.done = 1170; job.bytes = 1800L * 1024 * 1024; job.durationSeconds = 3743;
            job.partStates = new byte[job.total]; java.util.Arrays.fill(job.partStates, 0, 1170, (byte) 2); java.util.Arrays.fill(job.partStates, 1170, 1174, (byte) 1);
            long now = android.os.SystemClock.elapsedRealtime(); job.transferStarted = now - 1000; int slot = (int) (now / 1000 % 5); job.speedSeconds[slot] = now / 1000; job.speedBytes[slot] = (long) (8.4 * 1024 * 1024);
        });
        Activity activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
        try {
            android.view.View root = activity.getWindow().getDecorView();
            onUi(() -> {
                find(root, android.widget.Button.class, "대기열").performClick(); android.widget.ListView list = find(root, android.widget.ListView.class, null);
                for (int i = 0; i < list.getCount(); i++) if (list.getAdapter().getItem(i) instanceof DownloadStore.Job entry && entry.id.equals(id)) { list.performItemClick(null, i, i); break; }
            });
            waitForIdleSync(); android.app.Dialog dialog = detailsDialog(activity); if (dialog == null) throw new AssertionError("detail did not open"); android.view.View sheet = dialog.getWindow().getDecorView();
            onUi(() -> {
                check(find(sheet, android.widget.TextView.class, "다운로드 상세") != null, "queue tap opens download bottom sheet");
                check(find(sheet, android.widget.TextView.class, "재생시간 01:02:23") != null, "playback duration appears below episode title");
                check(find(sheet, android.widget.TextView.class, "63%") != null, "detail displays actual completed fraction");
                check(find(sheet, android.widget.Button.class, "진행 중인 작업 4개") != null, "detail identifies four active HTTP workers");
            });
            Thread.sleep(800);
            onUi(() -> find(sheet, android.widget.Button.class, "메뉴").performClick()); Thread.sleep(250); sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_BACK); Thread.sleep(500);
            onUi(() -> { android.widget.ScrollView scroll = find(sheet, android.widget.ScrollView.class, null); check(scroll.getScrollY() == 0, "closing action menu preserves detail scroll position"); });
            capture("details-ui.png");
            onUi(() -> { find(sheet, android.widget.Button.class, "진행 중인 작업 4개").performClick(); android.widget.TextView workers = find(sheet, android.widget.TextView.class, "작업 1 · 1171번 조각 수신 중\n작업 2 · 1172번 조각 수신 중\n작업 3 · 1173번 조각 수신 중\n작업 4 · 1174번 조각 수신 중"); check(workers != null && workers.getVisibility() == android.view.View.VISIBLE, "worker disclosure expands actual segment identifiers"); find(sheet, android.widget.Button.class, "일시정지").performClick(); });
            Thread.sleep(600); onUi(() -> check(find(sheet, android.widget.Button.class, "이어받기 / 시작") != null, "detail action updates after pause"));
            check(DownloadStore.PAUSED.equals(store.find(id).state), "detail pause preserves queued segments");
            onUi(() -> find(sheet, android.widget.ImageButton.class, null).performClick()); check(!dialog.isShowing(), "visible close control dismisses sheet");
        } finally { onUi(activity::finish); waitForIdleSync(); Thread.sleep(300); store.update(id, job -> job.state = DownloadStore.PAUSED); store.remove(id); }
        Activity historyActivity = null;
        try {
            DownloadStore.Job work = new DownloadStore.Job(); work.url = SiteClient.CANONICAL + "/world/10180/1"; work.contentUrl = SiteClient.CANONICAL + "/world/10180"; work.title = "삼국지 1화"; work.contentTitle = "삼국지"; work.bytes = 376; work.state = DownloadStore.DONE;
            store.rememberWork(work); store.rememberWork(work);
            DownloadStore.Job other = new DownloadStore.Job(); other.url = SiteClient.CANONICAL + "/world/10181/2"; other.contentUrl = SiteClient.CANONICAL + "/world/10181"; other.title = "삼국지 2화"; other.contentTitle = "삼국지"; other.bytes = 376; other.state = DownloadStore.DONE;
            store.rememberWork(other);
            int matching = 0; for (SiteClient.Item item : store.recentWorks()) if (item.url().equals(work.contentUrl)) matching++;
            check(matching == 1 && store.recentWorks().get(0).url().equals(other.contentUrl), "downloaded works deduplicate by exact work URL and retain same-title works separately");
            check(store.recentWorks().stream().anyMatch(item -> item.url().equals(work.contentUrl) && item.meta().contains("1화") && item.meta().contains("완료")), "downloaded work remembers its last episode and completion");
            historyActivity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); android.view.View root = historyActivity.getWindow().getDecorView(); Activity shown = historyActivity;
            onUi(() -> {
                android.widget.ListView list = find(root, android.widget.ListView.class, null); int target = -1;
                for (int i = 0; i < list.getCount(); i++) if (list.getAdapter().getItem(i) instanceof SiteClient.Item entry && entry.url().equals(work.contentUrl)) target = i;
                check(target >= 0, "persisted downloaded works appear in search tab after activity restart"); list.performItemClick(null, target, target);
            });
            waitForIdleSync(); waitForText(root, "‹ 최근 받은 작품");
            onUi(() -> {
                try { java.lang.reflect.Field content = MainActivity.class.getDeclaredField("contentUrl"); content.setAccessible(true); check(work.contentUrl.equals(content.get(shown)) && find(root, android.widget.Button.class, "‹ 최근 받은 작품") != null, "recent work opens its exact episode page without keyword search"); }
                catch (ReflectiveOperationException error) { throw new AssertionError(error); }
            });
        } finally { if (historyActivity != null) { Activity close = historyActivity; onUi(close::finish); } restoreWorks(previousWorks); }
    }
    private void usability() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context);
        android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
        String previousWorks = prefs.getString("recentWorks", null), savedGuide = prefs.getString("guide", SiteClient.GUIDE);
        String base = SiteClient.CANONICAL + "/drama/" + System.currentTimeMillis();
        List<SiteClient.Item> items = Arrays.asList(new SiteClient.Item("점검 작품 1화", base + "/1", "", ""), new SiteClient.Item("점검 작품 2화", base + "/2", "", ""));
        List<String> ids = new ArrayList<>(); Activity activity = null;
        try {
            store.add(items, new SiteClient.Item("점검 작품", base, "", ""));
            for (DownloadStore.Job job : store.snapshot()) if (job.url.startsWith(base + "/")) ids.add(job.id);
            store.update(ids.get(0), job -> { job.state = DownloadStore.DONE; job.bytes = 376; job.total = job.done = 1; job.durationSeconds = 10; });
            store.update(ids.get(1), job -> { job.state = DownloadStore.RUNNING; job.total = 2; job.durationSeconds = 20; });
            store.rememberWork(store.find(ids.get(0)));
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK));
            Activity shown = activity; android.view.View root = activity.getWindow().getDecorView();
            onUi(() -> {
                check(find(root, android.widget.Button.class, "최근 받은 작품").getVisibility() == android.view.View.GONE, "recent list hides its redundant navigation button");
                check(((android.widget.TextView) field(shown, "searchStatus")).getAccessibilityLiveRegion() == android.view.View.ACCESSIBILITY_LIVE_REGION_POLITE, "search results use polite accessibility updates");
                find(root, android.widget.Button.class, "대기열").performClick();
                check(find(root, android.widget.Button.class, "대기열").isSelected() && !find(root, android.widget.Button.class, "검색").isSelected(), "navigation exposes the selected tab");
                android.widget.ListView queue = find(root, android.widget.ListView.class, null); int active = -1, done = -1;
                for (int i = 0; i < queue.getCount(); i++) if (queue.getItemAtPosition(i) instanceof DownloadStore.Job job) { if (job.id.equals(ids.get(0))) done = i; if (job.id.equals(ids.get(1))) active = i; }
                check(active >= 0 && active < done, "active transfer appears before completed jobs without changing stored order");
                queue.setSelection(active);
            });
            waitForIdleSync();
            onUi(() -> {
                android.widget.ListView queue = find(root, android.widget.ListView.class, null); int position = -1;
                check(queue.getHeight() > 0, "queue retains a scrollable viewport");
                for (int i = 0; i < queue.getCount(); i++) if (queue.getItemAtPosition(i) instanceof DownloadStore.Job job && job.id.equals(ids.get(1))) position = i;
                queue.performItemClick(null, position, position);
            });
            waitForIdleSync(); android.app.Dialog dialog = detailsDialog(activity); android.view.View sheet = dialog.getWindow().getDecorView();
            onUi(() -> {
                android.widget.Button menu = find(sheet, android.widget.Button.class, "메뉴");
                check(menu.getWidth() - menu.getPaddingLeft() - menu.getPaddingRight() >= menu.getPaint().measureText("메뉴"), "menu has enough measured width for its complete label at current font scale");
                android.widget.Button workers = find(sheet, android.widget.Button.class, "진행 중인 작업 0개");
                if (android.os.Build.VERSION.SDK_INT >= 30) { check("접힘".contentEquals(workers.getStateDescription()), "worker list exposes its collapsed state"); workers.performClick(); check("펼쳐짐".contentEquals(workers.getStateDescription()), "worker list exposes its expanded state"); }
                find(sheet, android.widget.ImageButton.class, null).performClick();
                try {
                    java.lang.reflect.Field url = MainActivity.class.getDeclaredField("contentUrl"); url.setAccessible(true); url.set(shown, base);
                    java.lang.reflect.Field title = MainActivity.class.getDeclaredField("contentTitle"); title.setAccessible(true); title.set(shown, "점검 작품");
                    java.lang.reflect.Field field = MainActivity.class.getDeclaredField("episodes"); field.setAccessible(true);
                    @SuppressWarnings("unchecked") List<SiteClient.Item> episodes = (List<SiteClient.Item>) field.get(shown); episodes.clear(); episodes.addAll(items);
                    java.lang.reflect.Method tab = MainActivity.class.getDeclaredMethod("showTab", int.class); tab.setAccessible(true); tab.invoke(shown, 0);
                } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
                android.widget.ListView list = find(root, android.widget.ListView.class, null); int first = list.getHeaderViewsCount();
                android.view.View completed = list.getAdapter().getView(first, null, list), running = list.getAdapter().getView(first + 1, null, list);
                check(contains(completed, "다운로드 완료") != null && contains(running, "다운로드 중") != null, "episode rows distinguish completed and running downloads by exact URL");
                list.performItemClick(null, first + 1, first + 1);
                android.view.accessibility.AccessibilityNodeInfo node = running.createAccessibilityNodeInfo();
                check(node.isCheckable() && node.isChecked(), "episode row exposes checkbox selection to accessibility services"); node.recycle();
                find(root, android.widget.Button.class, "설정").performClick();
                android.widget.EditText domain = find(root, android.widget.EditText.class, null), guide = find(root, android.widget.EditText.class, savedGuide);
                check(find(root, android.widget.TextView.class, "접속 도메인").getLabelFor() == domain.getId() && find(root, android.widget.TextView.class, "안내 페이지 주소").getLabelFor() == guide.getId(), "settings labels are associated with their editable fields");
                guide.setText(savedGuide + "?draft-check=1"); domain.setText("https://draft.invalid");
                find(root, android.widget.Button.class, "대기열").performClick(); find(root, android.widget.Button.class, "설정").performClick();
                check(find(root, android.widget.EditText.class, savedGuide + "?draft-check=1") != null && find(root, android.widget.EditText.class, "https://draft.invalid") != null, "both unsaved addresses survive tab navigation");
                check(contains(root, "저장된 접속 도메인").getAccessibilityLiveRegion() == android.view.View.ACCESSIBILITY_LIVE_REGION_POLITE, "connection results use polite accessibility updates");
            });
            ActivityMonitor monitor = addMonitor(MainActivity.class.getName(), null, false);
            onUi(shown::recreate); Activity recreated = waitForMonitorWithTimeout(monitor, 8000); removeMonitor(monitor);
            if (recreated == null) throw new AssertionError("recreated activity was not observed"); activity = recreated; waitForIdleSync(); android.view.View restored = recreated.getWindow().getDecorView();
            onUi(() -> check(find(restored, android.widget.EditText.class, savedGuide + "?draft-check=1") != null && find(restored, android.widget.EditText.class, "https://draft.invalid") != null, "both unsaved addresses survive activity recreation"));
            check(savedGuide.equals(prefs.getString("guide", SiteClient.GUIDE)), "draft changes do not modify saved connection settings");
        } finally {
            if (activity != null) { Activity close = activity; onUi(close::finish); waitForIdleSync(); }
            for (String id : ids) { store.update(id, job -> job.state = DownloadStore.PAUSED); store.remove(id); }
            restoreWorks(previousWorks);
        }
    }
    private void onUi(Runnable action) {
        java.util.concurrent.atomic.AtomicReference<Throwable> failure = new java.util.concurrent.atomic.AtomicReference<>();
        runOnMainSync(() -> { try { action.run(); } catch (Throwable error) { failure.set(error); } });
        if (failure.get() != null) throw new AssertionError("UI check failed", failure.get());
    }
    private static Object field(Object target, String name) {
        try { java.lang.reflect.Field f = target.getClass().getDeclaredField(name); f.setAccessible(true); return f.get(target); } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private static void setField(Object target, String name, Object value) {
        try { java.lang.reflect.Field f = target.getClass().getDeclaredField(name); f.setAccessible(true); f.set(target, value); } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private static void tab(Activity activity, int destination) {
        try { java.lang.reflect.Method method = MainActivity.class.getDeclaredMethod("showTab", int.class); method.setAccessible(true); method.invoke(activity, destination); } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private void accessibilityClick(String label) {
        long deadline = android.os.SystemClock.elapsedRealtime() + 5000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            android.view.accessibility.AccessibilityNodeInfo root = getUiAutomation().getRootInActiveWindow();
            if (root != null) for (android.view.accessibility.AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText(label)) if (label.contentEquals(node.getText()) && node.isClickable() && node.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_CLICK)) return;
            try { Thread.sleep(100); } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new AssertionError(error); }
        }
        try { capture("delete-action-failure.png"); } catch (Exception ignored) { }
        throw new AssertionError("Accessible action not found: " + label);
    }
    private void quality() throws Exception {
        Context context = getTargetContext(); DownloadStore store = DownloadStore.get(context); android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
        String previousWorks = prefs.getString("recentWorks", null), guide = prefs.getString("guide", SiteClient.GUIDE), base = SiteClient.CANONICAL + "/drama/" + System.currentTimeMillis();
        List<SiteClient.Item> items = Arrays.asList(new SiteClient.Item("품질 점검 작품 1화", base + "/1", "", ""), new SiteClient.Item("아주 긴 작품 이름이 여러 줄에 걸쳐 표시되는 품질 점검 작품 2화", base + "/2", "", ""));
        Activity activity = null; android.net.Uri output = null; ActivityMonitor intentMonitor = null; final android.content.Intent[] launched = new android.content.Intent[1];
        try {
            store.add(items); List<DownloadStore.Job> fixtures = new ArrayList<>(); for (DownloadStore.Job job : store.snapshot()) if (job.url.startsWith(base + "/")) fixtures.add(job);
            String id = fixtures.get(0).id, other = fixtures.get(1).id;
            android.content.ContentValues values = new android.content.ContentValues(); values.put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, "tvmon-ui-quality-" + System.currentTimeMillis() + ".ts"); values.put(android.provider.MediaStore.MediaColumns.MIME_TYPE, "video/mp2t"); values.put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, "Download/TVMON");
            output = context.getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values); if (output == null) throw new AssertionError("test output missing");
            try (java.io.OutputStream stream = context.getContentResolver().openOutputStream(output)) { stream.write(ts(3)); }
            String uri = output.toString();
            for (String owned : Arrays.asList(id, other)) store.update(owned, job -> { job.state = DownloadStore.DONE; job.durationSeconds = 3743; job.total = job.done = 1857; job.bytes = 1800L * 1024 * 1024; job.output = uri; job.partStates = new byte[1857]; Arrays.fill(job.partStates, (byte) 2); });
            activity = startActivitySync(new android.content.Intent(context, MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)); Activity shown = activity; android.view.View root = shown.getWindow().getDecorView();
            onUi(() -> {
                find(root, android.widget.Button.class, "검색").performClick(); check(find(root, android.widget.EditText.class, null).getError() != null, "empty search has an inline error");
                setField(shown, "showingRecent", false); setField(shown, "loading", true); tab(shown, 0);
            }); waitForIdleSync();
            onUi(() -> { check(((android.widget.ProgressBar) field(shown, "searchLoading")).isShown() && !((android.widget.Button) field(shown, "searchButton")).isEnabled(), "search loading shows progress and prevents duplicate requests"); setField(shown, "loading", false); tab(shown, 0); }); waitForIdleSync();
            onUi(() -> {
                check(contains(root, "검색 결과가 없습니다") != null, "empty results explain how to recover");
                @SuppressWarnings("unchecked") List<SiteClient.Item> results = (List<SiteClient.Item>) field(shown, "results"); results.add(new SiteClient.Item(items.get(1).title(), base, "드라마", "")); tab(shown, 0);
            }); waitForIdleSync();
            onUi(() -> {
                android.widget.ListView list = find(root, android.widget.ListView.class, null); android.view.View card = list.getAdapter().getView(list.getHeaderViewsCount(), null, list); Object holder = card.getTag();
                check(((android.widget.TextView) field(holder, "title")).getMaxLines() == 2 && ((android.widget.ImageView) field(holder, "cover")).getDrawable() != null, "long work titles are bounded and missing posters have a placeholder");
                android.view.View rebound = list.getAdapter().getView(list.getHeaderViewsCount(), card, list); check(rebound == card, "search scrolling reuses existing card views");
                android.widget.EditText input = (android.widget.EditText) field(shown, "searchInput"); check(input.getHeight() == ((android.widget.Button) field(shown, "searchButton")).getHeight(), "search field and button remain aligned at current font scale");
                setField(shown, "contentUrl", base); setField(shown, "contentTitle", "품질 점검 작품"); @SuppressWarnings("unchecked") List<SiteClient.Item> episodes = (List<SiteClient.Item>) field(shown, "episodes"); episodes.clear(); episodes.addAll(items); tab(shown, 0);
            }); waitForIdleSync();
            onUi(() -> {
                android.widget.Button add = (android.widget.Button) field(shown, "addButton"), download = (android.widget.Button) field(shown, "downloadButton");
                check(!add.isEnabled() && !download.isEnabled(), "episode actions are disabled with no selection");
                android.graphics.Bitmap disabled = android.graphics.Bitmap.createBitmap(4, 4, android.graphics.Bitmap.Config.ARGB_8888); download.getBackground().setBounds(0, 0, 4, 4); download.getBackground().draw(new android.graphics.Canvas(disabled)); int disabledColor = disabled.getPixel(2, 2);
                android.widget.ListView list = find(root, android.widget.ListView.class, null); int first = list.getHeaderViewsCount(); android.view.View card = list.getAdapter().getView(first, null, list);
                check(contains(card, "저장할 회차를 선택하세요") == null && contains(card, "다운로드 완료") != null, "episode cards remove repeated help and preserve completed state"); list.performItemClick(null, first, first);
                check(add.isEnabled() && download.isEnabled(), "previously completed episodes remain selectable for another download");
                android.graphics.Bitmap enabled = android.graphics.Bitmap.createBitmap(4, 4, android.graphics.Bitmap.Config.ARGB_8888); download.getBackground().setBounds(0, 0, 4, 4); download.getBackground().draw(new android.graphics.Canvas(enabled)); check(disabledColor != enabled.getPixel(2, 2), "disabled buttons have a different background as well as text"); download.getBackground().setBounds(0, 0, download.getWidth(), download.getHeight());
                android.widget.Button all = (android.widget.Button) field(shown, "selectAllButton"); all.performClick(); check("선택 해제".contentEquals(all.getText()) && contains(root, "2개 선택") != null, "select-all label and selection count agree"); all.performClick(); check("전체 선택".contentEquals(all.getText()) && !download.isEnabled(), "clear-all restores disabled actions");
                list.performItemClick(null, first + 1, first + 1); tab(shown, 2); tab(shown, 0); check(((java.util.Set<?>) field(shown, "selected")).size() == 1, "episode selection survives switching tabs"); tab(shown, 1);
            }); waitForIdleSync(); capture("quality-queue-complete.png");
            onUi(() -> {
                check(((android.view.View) field(shown, "queueActions")).getVisibility() == android.view.View.GONE, "all-complete queue hides unnecessary global controls");
                android.widget.ListView list = find(root, android.widget.ListView.class, null); android.widget.TextView summary = (android.widget.TextView) field(shown, "queueSummary");
                android.graphics.Rect bounds = new android.graphics.Rect(); check(summary.getGlobalVisibleRect(bounds) && bounds.height() == summary.getHeight(), "queue summary is fully visible at the top of the scrolling list");
                int position = -1; for (int i = 0; i < list.getCount(); i++) if (list.getItemAtPosition(i) instanceof DownloadStore.Job job && job.id.equals(id)) position = i;
                android.view.View card = list.getAdapter().getView(position, null, list); Object holder = card.getTag(); check(((android.view.View) field(holder, "progress")).getVisibility() == android.view.View.GONE && find(card, android.widget.Button.class, "재생") != null && contains(card, "조각") == null, "complete cards prioritize playback and omit progress and segment noise");
                check(list.getAdapter().getView(position, card, list) == card, "queue updates reuse card views"); list.performItemClick(null, position, position);
            }); waitForIdleSync(); Thread.sleep(250); android.app.Dialog dialog = detailsDialog(shown); android.view.View sheet = dialog.getWindow().getDecorView(); capture("quality-details-complete.png");
            onUi(() -> {
                check(!((android.view.View) field(shown, "detailsMetrics")).isShown() && !((android.view.View) field(shown, "detailsWorkersButton")).isShown(), "complete details hide transfer metrics and idle workers");
                check(!((android.view.View) field(shown, "detailsParts")).isShown() && find(sheet, android.widget.Button.class, "영상 열기").isShown(), "completed segment map starts collapsed while playback stays visible");
                ((android.widget.Button) field(shown, "detailsPartsButton")).performClick(); check(((android.view.View) field(shown, "detailsParts")).isShown() && contains(sheet, "1,857 / 1,857") != null, "segment disclosure retains actual counts and the large map");
                ((android.widget.Button) field(shown, "detailsMenuButton")).performClick(); check(detailsDialogUnchecked(shown) == dialog && ((android.view.View) field(shown, "detailsMenu")).isShown(), "job menu uses the existing sheet without stacking another dialog");
                check(find(sheet, android.widget.Button.class, "공유") != null && find(sheet, android.widget.Button.class, "영상 주소 복사") != null && find(sheet, android.widget.Button.class, "목록에서 삭제") != null, "job menu retains share copy and delete actions");
            }); waitForIdleSync(); capture("quality-job-menu.png");
            onUi(() -> { android.graphics.Rect bounds = new android.graphics.Rect(); android.widget.Button delete = find(sheet, android.widget.Button.class, "목록에서 삭제"); check(delete.getGlobalVisibleRect(bounds) && bounds.height() >= (int) (48 * context.getResources().getDisplayMetrics().density), "last menu action keeps a full 48dp touch target above system navigation"); });
            sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_BACK); waitForIdleSync();
            onUi(() -> check(dialog.isShowing() && !(boolean) field(shown, "detailsMenuShowing"), "system back returns from menu to download detail"));
            intentMonitor = new ActivityMonitor() { @Override public ActivityResult onStartActivity(android.content.Intent intent) { launched[0] = new android.content.Intent(intent); return new ActivityResult(Activity.RESULT_CANCELED, null); } }; addMonitor(intentMonitor);
            onUi(() -> find(sheet, android.widget.Button.class, "영상 열기").performClick());
            check(launched[0] != null && android.content.Intent.ACTION_VIEW.equals(launched[0].getAction()) && uri.equals(launched[0].getDataString()) && (launched[0].getFlags() & android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0, "playback sends the saved video URI with read permission");
            onUi(() -> { ((android.widget.Button) field(shown, "detailsMenuButton")).performClick(); find(sheet, android.widget.Button.class, "공유").performClick(); });
            android.content.Intent share = launched[0].getParcelableExtra(android.content.Intent.EXTRA_INTENT); check(android.content.Intent.ACTION_CHOOSER.equals(launched[0].getAction()) && share != null && uri.equals(share.getParcelableExtra(android.content.Intent.EXTRA_STREAM).toString()) && share.getClipData() != null, "sharing retains video stream and ClipData permission grant");
            onUi(() -> { ((android.widget.Button) field(shown, "detailsMenuButton")).performClick(); find(sheet, android.widget.Button.class, "목록에서 삭제").performClick(); }); waitForIdleSync();
            accessibilityClick("취소"); waitForIdleSync(); check(store.find(id) != null, "canceling delete keeps the completed record");
            onUi(() -> { ((android.widget.Button) field(shown, "detailsMenuButton")).performClick(); find(sheet, android.widget.Button.class, "목록에서 삭제").performClick(); }); waitForIdleSync(); accessibilityClick("목록에서 삭제"); waitForIdleSync();
            check(store.find(id) == null, "confirmed deletion removes only the selected record"); try (InputStream stream = context.getContentResolver().openInputStream(output)) { check(Arrays.equals(ts(3), stream.readAllBytes()), "deleting a completed record preserves its public video bytes"); }
            removeMonitor(intentMonitor); intentMonitor = null;
            for (String state : Arrays.asList(DownloadStore.WAITING, DownloadStore.RUNNING, DownloadStore.PAUSED, DownloadStore.FAILED)) {
                store.update(other, job -> { job.state = state; job.done = 100; job.message = DownloadStore.FAILED.equals(state) ? "연결이 끊어졌습니다. 다시 시도하세요." : ""; }); onUi(() -> tab(shown, 1)); waitForIdleSync();
                onUi(() -> {
                    android.widget.Button pause = (android.widget.Button) field(shown, "queuePause"), start = (android.widget.Button) field(shown, "queueStart");
                    boolean active = DownloadStore.RUNNING.equals(state), waiting = DownloadStore.WAITING.equals(state);
                    check(!pause.isShown() && !start.isShown(), "single controllable job hides global controls in " + state);
                    android.widget.ListView list = find(root, android.widget.ListView.class, null); android.view.View card = list.getAdapter().getView(list.getHeaderViewsCount(), null, list);
                    String action = active ? "일시정지" : waiting ? "시작" : DownloadStore.PAUSED.equals(state) ? "재개" : "재시도";
                    check(find(card, android.widget.Button.class, action) != null, "job recovery action reflects " + state);
                }); capture("quality-queue-" + state + ".png");
            }
            store.resume(other); check(DownloadStore.WAITING.equals(store.find(other).state), "resume preserves the existing waiting-state transition");
            store.add(Collections.singletonList(new SiteClient.Item("품질 점검 작품 3화", base + "/3", "", ""))); String pausedId = store.snapshot().stream().filter(job -> job.url.equals(base + "/3")).findFirst().orElseThrow().id;
            store.update(other, job -> job.state = DownloadStore.RUNNING); store.update(pausedId, job -> job.state = DownloadStore.PAUSED); onUi(() -> tab(shown, 1)); waitForIdleSync();
            onUi(() -> { android.widget.Button resume = (android.widget.Button) field(shown, "queueStart"); check(((android.view.View) field(shown, "queuePause")).isShown() && resume.isShown() && resume.isEnabled() && "전체 재개".contentEquals(resume.getText()), "mixed running and paused jobs retain both global pause and resume"); });
            onUi(() -> { tab(shown, 2); android.widget.EditText input = find(root, android.widget.EditText.class, guide); input.setText("http://invalid.example"); find(root, android.widget.Button.class, "주소 설정 저장").performClick(); check(input.getError() != null && input.hasFocus(), "invalid guide address is explained at the focused field"); });
            check(guide.equals(prefs.getString("guide", SiteClient.GUIDE)), "invalid UI edits never overwrite saved settings");
        } finally {
            if (intentMonitor != null) removeMonitor(intentMonitor);
            if (activity != null) { Activity close = activity; onUi(close::finish); waitForIdleSync(); }
            for (DownloadStore.Job job : store.snapshot()) if (job.url.startsWith(base + "/")) { store.update(job.id, owned -> owned.state = DownloadStore.PAUSED); store.remove(job.id); }
            if (output != null) context.getContentResolver().delete(output, null, null); restoreWorks(previousWorks);
        }
    }
    private android.app.Dialog detailsDialogUnchecked(Activity activity) { try { return detailsDialog(activity); } catch (Exception error) { throw new AssertionError(error); } }
    private static byte[] ts(int value) { byte[] data = new byte[376]; Arrays.fill(data, (byte) value); data[0] = data[188] = 0x47; return data; }

    private static final class LocalServer implements AutoCloseable {
        final ServerSocket server = new ServerSocket(0, 16, java.net.InetAddress.getByName("127.0.0.1"));
        final Map<String, AtomicInteger> requests = new ConcurrentHashMap<>();
        volatile boolean closed;
        LocalServer() throws Exception {
            Thread thread = new Thread(() -> {
                while (!closed) {
                    try { Socket socket = server.accept(); Thread request = new Thread(() -> respond(socket)); request.setDaemon(true); request.start(); }
                    catch (Exception e) { if (!closed) throw new RuntimeException(e); }
                }
            }); thread.setDaemon(true); thread.start();
        }
        String url(String path) { return "http://127.0.0.1:" + server.getLocalPort() + path; }
        int count(String path) { AtomicInteger count = requests.get(path); return count == null ? 0 : count.get(); }
        void respond(Socket socket) {
            try (socket) {
                socket.setSoTimeout(5000); BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.US_ASCII));
                String first = reader.readLine(); if (first == null) return; String path = first.split(" ")[1];
                requests.computeIfAbsent(path, key -> new AtomicInteger()).incrementAndGet();
                String line; while ((line = reader.readLine()) != null && !line.isEmpty()) { }
                if (path.equals("/redirect")) { socket.getOutputStream().write(bytes("HTTP/1.1 302 Found\r\nLocation: http://example.com/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")); return; }
                if (path.equals("/credentials")) { socket.getOutputStream().write(bytes("HTTP/1.1 302 Found\r\nLocation: https://user:pass@example.com/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")); return; }
                if (path.equals("/unavailable")) { socket.getOutputStream().write(bytes("HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")); return; }
                if (path.equals("/challenge")) { socket.getOutputStream().write(bytes("HTTP/1.1 403 Forbidden\r\ncf-mitigated: challenge\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")); return; }
                if (path.equals("/stall")) { socket.getOutputStream().write(bytes("HTTP/1.1 200 OK\r\nContent-Length: 376\r\nConnection: close\r\n\r\n")); socket.getOutputStream().flush(); Thread.sleep(5000); return; }
                int index = Integer.parseInt(path.substring(1, path.indexOf('.'))); Thread.sleep(index % 3 * 20L);
                byte[] body = ts(index); socket.getOutputStream().write(bytes("HTTP/1.1 200 OK\r\nContent-Length: " + body.length + "\r\nConnection: close\r\n\r\n")); socket.getOutputStream().write(body);
            } catch (Exception ignored) { }
        }
        @Override public void close() throws Exception { closed = true; server.close(); }
    }
}

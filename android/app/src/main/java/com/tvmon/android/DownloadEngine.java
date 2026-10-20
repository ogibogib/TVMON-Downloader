package com.tvmon.android;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.AtomicFile;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CompletionService;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

final class DownloadEngine {
    private static SiteClient.Stop activeStop;
    private static boolean running;
    static synchronized boolean running() { return running; }
    static synchronized void cancel() { if (activeStop != null) activeStop.cancel(); }
    record Source(List<VideoFormat.Segment> segments, Map<String, byte[]> keys) {}
    record Part(int index, int size, String hash) {}

    static void runQueue(Context context, SiteClient.Stop stop, Consumer<DownloadStore.Job> progress) {
        synchronized (DownloadEngine.class) { if (running) return; running = true; activeStop = stop; }
        DownloadStore store = DownloadStore.get(context);
        try {
            SiteClient client = new SiteClient(SiteClient.domains(context.getSharedPreferences("settings", 0).getString("domains", SiteClient.DEFAULT_DOMAINS)));
            while (!stop.stopped()) {
                DownloadStore.Job job = store.next();
                if (job == null) break;
                try {
                    stop.check();
                    store.update(job.id, current -> { if (DownloadStore.WAITING.equals(current.state)) { current.state = DownloadStore.RUNNING; current.message = "영상 정보를 확인하고 있습니다…"; } });
                    if (!DownloadStore.RUNNING.equals(store.find(job.id).state)) continue;
                    progress.accept(store.find(job.id));
                    SiteClient.Video video = client.video(job.url, stop);
                    store.update(job.id, current -> { if (!video.title().isEmpty()) current.title = video.title(); });
                    Source source = loadSource(client, video.manifest(), stop);
                    transfer(context, store, job, source, stop, progress);
                } catch (Exception error) {
                    try {
                        boolean stopped = stop.stopped() || error instanceof InterruptedIOException || Thread.currentThread().isInterrupted();
                        store.update(job.id, current -> {
                            if (DownloadStore.RUNNING.equals(current.state)) {
                                current.state = stopped ? DownloadStore.WAITING : DownloadStore.FAILED;
                                current.message = stopped ? "연결이 중단되었습니다. 받은 조각은 보관됩니다." : message(error);
                            }
                        });
                        progress.accept(store.find(job.id));
                    } catch (Exception persistenceError) { android.util.Log.e("TVMON", "작업 상태 저장 실패", persistenceError); }
                    if (stop.stopped()) break;
                }
            }
        } catch (Exception error) { android.util.Log.e("TVMON", "다운로드 시작 실패", error); }
        finally { synchronized (DownloadEngine.class) { activeStop = null; running = false; } }
    }

    static String message(Throwable error) {
        while (error.getCause() != null && (error instanceof java.util.concurrent.ExecutionException)) error = error.getCause();
        return error.getMessage() == null ? "다운로드 중 오류가 발생했습니다. 이어받기를 다시 시도해 주세요." : error.getMessage();
    }

    static Source loadSource(SiteClient client, String manifest, SiteClient.Stop stop) throws Exception {
        URI original = URI.create(manifest); List<String> candidates = new ArrayList<>(); candidates.add(manifest);
        if (original.getHost().matches("every[0-9]+\\.poorcdn\\.com") && original.getPath().matches("/v/[a-z]+/[0-9a-f]{32}/c\\.html")) {
            for (int i = 1; i <= 9; i++) if (!original.getHost().equals("every" + i + ".poorcdn.com"))
                candidates.add(new URI("https", "every" + i + ".poorcdn.com", original.getPath(), original.getQuery(), null).toString());
        }
        Exception first = null;
        for (String candidate : candidates) {
            stop.check();
            try {
                SiteClient.Response response = client.fetch(candidate, SiteClient.PLAYER, 4 * 1024 * 1024, stop);
                List<VideoFormat.Segment> segments = VideoFormat.playlist(response.body(), response.url());
                boolean alternate = !candidate.equals(manifest);
                if (alternate) {
                    URI resolved = URI.create(response.url()); String prefix = original.getPath().substring(0, original.getPath().lastIndexOf('/') + 1);
                    if (!resolved.getHost().equals(URI.create(candidate).getHost()) || !resolved.getPath().equals(original.getPath())) throw new IOException("대체 영상 서버의 주소가 일치하지 않습니다.");
                    for (VideoFormat.Segment part : segments) if (!URI.create(part.url()).getPath().startsWith(prefix)) throw new IOException("대체 서버의 영상 식별자가 일치하지 않습니다.");
                }
                Map<String, byte[]> keys = new HashMap<>();
                for (VideoFormat.Segment part : segments) if (part.keyUrl() != null && !keys.containsKey(part.keyUrl())) {
                    String keyUrl = part.keyUrl();
                    if (keyUrl.contains("/v/key7?") && !keyUrl.matches(".*[?&]mode=.*")) keyUrl += "&mode=obfuscated";
                    keys.put(part.keyUrl(), VideoFormat.decodeKey(client.fetch(keyUrl, SiteClient.PLAYER, 1024 * 1024, stop).body()));
                }
                Source source = new Source(segments, keys);
                if (alternate) for (int index : new int[]{0, segments.size() / 2, segments.size() - 1}) receive(segments.get(index), keys, stop);
                return source;
            } catch (Exception error) { stop.check(); if (first == null) first = error; }
        }
        throw new IOException("다운로드 가능한 영상 서버를 찾지 못했습니다. " + message(first), first);
    }

    private static byte[] receive(VideoFormat.Segment part, Map<String, byte[]> keys, SiteClient.Stop stop) throws Exception {
        return receive(part, keys, stop, null);
    }
    private static byte[] receive(VideoFormat.Segment part, Map<String, byte[]> keys, SiteClient.Stop stop, java.util.function.LongConsumer received) throws Exception {
        byte[] data = SiteClient.read(part.url(), SiteClient.PLAYER, SiteClient.MEDIA_LIMIT, stop, received).body();
        if (part.keyUrl() != null) data = VideoFormat.decrypt(data, keys.get(part.keyUrl()), part.iv());
        VideoFormat.validateTs(data); return data;
    }
    private static File partFile(File folder, int index) { return new File(folder, String.format(Locale.ROOT, "%06d.ts", index)); }

    static void transfer(Context context, DownloadStore store, DownloadStore.Job job, Source source,
                         SiteClient.Stop stop, Consumer<DownloadStore.Job> progress) throws Exception {
        File folder = store.parts(job.id);
        if (!folder.isDirectory() && !folder.mkdirs()) throw new IOException("조각 저장 폴더를 만들지 못했습니다.");
        AtomicFile checkpoint = new AtomicFile(new File(folder, "state.json"));
        String identity = VideoFormat.identity(source.segments());
        JSONObject state = new JSONObject().put("identity", identity).put("parts", new JSONObject());
        if (checkpoint.getBaseFile().exists()) {
            try { state = new JSONObject(new String(checkpoint.readFully(), StandardCharsets.UTF_8)); }
            catch (Exception error) { throw new IOException("이어받기 기록이 손상되었습니다. 작업을 삭제하고 다시 추가해 주세요.", error); }
            if (!identity.equals(state.optString("identity"))) throw new IOException("서버의 영상 구성이 변경되었습니다. 기존 작업을 삭제한 뒤 다시 추가해 주세요.");
        }
        JSONObject parts = state.getJSONObject("parts"); List<Integer> missing = new ArrayList<>();
        int completed = 0; long bytes = 0;
        byte[] partStates = new byte[source.segments().size()];
        double duration = 0; for (VideoFormat.Segment segment : source.segments()) duration += segment.duration();
        for (int index = 0; index < source.segments().size(); index++) {
            stop.check(); File file = partFile(folder, index); JSONObject record = parts.optJSONObject(String.valueOf(index));
            boolean valid = false;
            if (record != null && file.isFile() && file.length() > 0 && file.length() <= SiteClient.MEDIA_LIMIT && file.length() == record.optLong("size", -1)) {
                byte[] data = Files.readAllBytes(file.toPath());
                try { VideoFormat.validateTs(data); valid = VideoFormat.sha256(data).equals(record.optString("sha256")); } catch (IOException ignored) { }
            }
            if (valid) { completed++; bytes += file.length(); partStates[index] = 2; }
            else { parts.remove(String.valueOf(index)); missing.add(index); }
        }
        DownloadStore.write(checkpoint, state.toString().getBytes(StandardCharsets.UTF_8));
        int initialDone = completed; long initialBytes = bytes;
        double durationSeconds = duration;
        store.update(job.id, current -> {
            current.done = initialDone; current.total = source.segments().size(); current.bytes = initialBytes;
            current.durationSeconds = durationSeconds; current.partStates = partStates;
            current.transferStarted = android.os.SystemClock.elapsedRealtime(); current.speedBytes = new long[5]; current.speedSeconds = new long[5];
            current.message = initialDone > 0 ? "검증한 조각부터 이어받고 있습니다…" : "HTTP 다운로드 중…";
        });
        progress.accept(store.find(job.id));
        if (initialDone > 0) store.rememberWork(store.find(job.id));
        ExecutorService pool = Executors.newFixedThreadPool(4);
        CompletionService<Part> finished = new ExecutorCompletionService<>(pool);
        List<Future<Part>> futures = new ArrayList<>();
        int submitted = 0, received = 0;
        try {
            while (received < missing.size()) {
                stop.check();
                while (submitted < missing.size() && submitted - received < 4) {
                    int index = missing.get(submitted++);
                    futures.add(finished.submit(() -> {
                        Exception last = null;
                        for (int attempt = 0; attempt < 3; attempt++) {
                            stop.check();
                            store.receiving(job.id, index, 1, 0);
                            try {
                                byte[] data = receive(source.segments().get(index), source.keys(), stop, count -> store.receiving(job.id, index, 1, count));
                                stop.check(); DownloadStore.write(new AtomicFile(partFile(folder, index)), data);
                                return new Part(index, data.length, VideoFormat.sha256(data));
                            } catch (Exception e) { stop.check(); store.receiving(job.id, index, 3, 0); last = e; if (attempt < 2) Thread.sleep(500L * (attempt + 1)); }
                        }
                        throw last;
                    }));
                }
                Future<Part> future = finished.poll(250, TimeUnit.MILLISECONDS);
                if (future == null) continue;
                Part part = future.get(); received++;
                parts.put(String.valueOf(part.index()), new JSONObject().put("size", part.size()).put("sha256", part.hash()));
                DownloadStore.write(checkpoint, state.toString().getBytes(StandardCharsets.UTF_8));
                completed++; bytes += part.size();
                int done = completed; long savedBytes = bytes;
                store.update(job.id, current -> { current.done = done; current.bytes = savedBytes; current.partStates[part.index()] = 2; current.message = "HTTP 다운로드 중…"; });
                if (received == 1) store.rememberWork(store.find(job.id));
                progress.accept(store.find(job.id));
            }
        } finally {
            for (Future<Part> future : futures) if (!future.isDone()) future.cancel(true);
            pool.shutdownNow();
            try { if (!pool.awaitTermination(15, TimeUnit.SECONDS)) throw new IOException("다운로드 작업이 종료되지 않았습니다. 잠시 후 다시 시도해 주세요."); }
            finally { store.clearReceiving(job.id); }
        }
        stop.check();
        store.update(job.id, current -> current.message = "조각을 순서대로 병합하고 있습니다…"); progress.accept(store.find(job.id));
        Uri output = publish(context, store, store.find(job.id), folder, source.segments().size(), stop);
        store.update(job.id, current -> { current.output = output.toString(); current.state = DownloadStore.DONE; current.message = "다운로드/TVMON에 저장했습니다."; });
        store.rememberWork(store.find(job.id));
        progress.accept(store.find(job.id));
        try { DownloadStore.clearParts(folder); } catch (IOException e) { android.util.Log.w("TVMON", "완료한 임시 조각 정리 실패", e); }
    }

    private static Uri publish(Context context, DownloadStore store, DownloadStore.Job job, File folder, int count, SiteClient.Stop stop) throws Exception {
        if (!job.output.isEmpty()) {
            Uri previous = Uri.parse(job.output);
            try (Cursor cursor = context.getContentResolver().query(previous, new String[]{MediaStore.Downloads.IS_PENDING, MediaStore.Downloads.SIZE}, null, null, null)) {
                if (cursor != null && cursor.moveToFirst()) {
                    if (cursor.getInt(0) == 0 && cursor.getLong(1) == job.bytes) return previous;
                    if (cursor.getInt(0) == 1) context.getContentResolver().delete(previous, null, null);
                }
            }
        }
        String name = job.title.replaceAll("[<>:\"/\\\\|?*\\x00-\\x1f]", "_").trim();
        if (name.length() > 100) name = name.substring(0, 100);
        if (name.isEmpty()) name = "TVMON 영상";
        ContentValues values = new ContentValues(); values.put(MediaStore.Downloads.DISPLAY_NAME, name + ".ts");
        values.put(MediaStore.Downloads.MIME_TYPE, "video/mp2t"); values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/TVMON");
        values.put(MediaStore.Downloads.IS_PENDING, 1);
        Uri output = context.getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (output == null) throw new IOException("다운로드 폴더에 파일을 만들지 못했습니다.");
        try {
            store.update(job.id, current -> current.output = output.toString());
            try (OutputStream target = context.getContentResolver().openOutputStream(output, "w")) {
                if (target == null) throw new IOException("출력 파일을 열지 못했습니다.");
                byte[] buffer = new byte[65536];
                for (int i = 0; i < count; i++) {
                    stop.check();
                    try (FileInputStream input = new FileInputStream(partFile(folder, i))) {
                        int size; while ((size = input.read(buffer)) != -1) { stop.check(); target.write(buffer, 0, size); }
                    }
                }
                target.flush();
            }
            stop.check();
            ContentValues complete = new ContentValues(); complete.put(MediaStore.Downloads.IS_PENDING, 0);
            if (context.getContentResolver().update(output, complete, null, null) != 1) throw new IOException("완료한 영상 파일을 공개하지 못했습니다.");
            return output;
        } catch (Exception error) {
            context.getContentResolver().delete(output, null, null);
            store.update(job.id, current -> current.output = "");
            throw error;
        }
    }
}

package com.tvmon.android;

import android.content.Context;
import android.content.Intent;
import android.util.AtomicFile;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.function.Consumer;

final class DownloadStore {
    static final String CHANGED = "com.tvmon.android.QUEUE_CHANGED";
    static final String WAITING = "waiting", RUNNING = "running", PAUSED = "paused", DONE = "done", FAILED = "failed";
    private static DownloadStore instance;
    private final Context context;
    private final AtomicFile file;
    private final List<Job> jobs = new ArrayList<>();

    static final class Job {
        String id, url, title, state = WAITING, message = "대기 중", output = "", contentTitle = "", contentUrl = "", thumbnail = "";
        int done, total;
        long bytes;
        double durationSeconds;
        byte[] partStates;
        long transferStarted;
        long[] speedSeconds = new long[5], speedBytes = new long[5];
        JSONObject json() {
            JSONObject value = new JSONObject();
            try {
                value.put("id", id).put("url", url).put("title", title).put("state", state).put("message", message)
                        .put("output", output).put("done", done).put("total", total).put("bytes", bytes).put("durationSeconds", durationSeconds);
                value.put("contentTitle", contentTitle).put("contentUrl", contentUrl).put("thumbnail", thumbnail);
            } catch (Exception e) { throw new IllegalStateException(e); }
            return value;
        }
        static Job from(JSONObject value) throws Exception {
            Job job = new Job(); job.id = value.getString("id");
            if (!UUID.fromString(job.id).toString().equals(job.id)) throw new IOException("잘못된 작업 식별자입니다.");
            job.url = value.getString("url"); job.title = value.getString("title"); job.state = value.getString("state");
            job.message = value.optString("message"); job.output = value.optString("output");
            job.done = value.optInt("done"); job.total = value.optInt("total"); job.bytes = value.optLong("bytes");
            job.durationSeconds = value.optDouble("durationSeconds", 0);
            job.contentTitle = value.optString("contentTitle"); job.contentUrl = value.optString("contentUrl"); job.thumbnail = value.optString("thumbnail");
            return job;
        }
        Job copy() {
            try {
                Job copy = from(json()); copy.partStates = partStates == null ? null : partStates.clone();
                copy.transferStarted = transferStarted; copy.speedSeconds = speedSeconds.clone(); copy.speedBytes = speedBytes.clone(); return copy;
            } catch (Exception e) { throw new IllegalStateException(e); }
        }
        double speed() {
            if (!RUNNING.equals(state) || transferStarted == 0) return 0;
            long now = android.os.SystemClock.elapsedRealtime(), second = now / 1000, received = 0;
            for (int i = 0; i < speedBytes.length; i++) if (second - speedSeconds[i] >= 0 && second - speedSeconds[i] < 5) received += speedBytes[i];
            return received / Math.max(1, Math.min(5, (now - transferStarted) / 1000.0));
        }
    }

    private DownloadStore(Context context) {
        this.context = context.getApplicationContext(); file = new AtomicFile(new File(this.context.getFilesDir(), "queue.json"));
        if (file.getBaseFile().exists() || new File(file.getBaseFile() + ".bak").exists()) {
            try {
                JSONArray saved = new JSONArray(new String(file.readFully(), StandardCharsets.UTF_8));
                for (int i = 0; i < saved.length(); i++) {
                    Job job = Job.from(saved.getJSONObject(i));
                    if (RUNNING.equals(job.state)) { job.state = PAUSED; job.message = "앱이 중단되었습니다. 이어받기를 눌러 주세요."; }
                    jobs.add(job);
                }
            } catch (Exception e) { throw new IllegalStateException("저장된 작업 목록을 읽지 못했습니다. 기존 queue.json은 유지됩니다.", e); }
        }
        if (!context.getSharedPreferences("settings", 0).contains("recentWorks")) for (Job job : jobs) if (job.bytes > 0) rememberWork(job);
    }
    static synchronized DownloadStore get(Context context) {
        if (instance == null) instance = new DownloadStore(context);
        return instance;
    }
    synchronized List<Job> snapshot() { List<Job> result = new ArrayList<>(); for (Job job : jobs) result.add(job.copy()); return result; }
    synchronized Job find(String id) { for (Job job : jobs) if (job.id.equals(id)) return job.copy(); return null; }
    synchronized Job next() { for (Job job : jobs) if (WAITING.equals(job.state)) return job.copy(); return null; }
    synchronized int add(List<SiteClient.Item> items) throws IOException {
        return add(items, null);
    }
    synchronized int add(List<SiteClient.Item> items, SiteClient.Item content) throws IOException {
        int count = 0;
        for (SiteClient.Item item : items) {
            boolean exists = false;
            for (Job job : jobs) if (job.url.equals(item.url()) && !DONE.equals(job.state)) { exists = true; break; }
            if (exists) continue;
            Job job = new Job(); job.id = UUID.randomUUID().toString(); job.url = item.url(); job.title = item.title();
            if (content != null && item.url().startsWith(content.url() + "/")) { job.contentTitle = content.title(); job.contentUrl = content.url(); job.thumbnail = content.thumbnail(); }
            jobs.add(job); count++;
        }
        save(); return count;
    }
    synchronized List<SiteClient.Item> recentWorks() {
        List<SiteClient.Item> result = new ArrayList<>();
        try {
            JSONArray saved = new JSONArray(context.getSharedPreferences("settings", 0).getString("recentWorks", "[]"));
            for (int i = 0; i < saved.length(); i++) {
                JSONObject work = saved.getJSONObject(i);
                result.add(new SiteClient.Item(work.getString("title"), work.getString("url"), "마지막 다운로드 · " + work.getString("episode") + (DONE.equals(work.optString("state")) ? " · 완료" : ""), work.optString("thumbnail")));
            }
        } catch (Exception error) { android.util.Log.w("TVMON", "최근 다운로드 작품 기록 읽기 실패", error); }
        return result;
    }
    synchronized void rememberWork(Job job) {
        if (job == null || job.bytes == 0) return;
        try {
            String url = job.contentUrl.isEmpty() ? job.url.substring(0, job.url.lastIndexOf('/')) : job.contentUrl;
            String title = job.contentTitle.isEmpty() ? job.title.replaceFirst("\\s+[0-9]+\\s*(?:화|회)(?:\\s.*)?$", "") : job.contentTitle;
            String episode = job.title.startsWith(title) ? job.title.substring(title.length()).trim() : job.title;
            if (episode.isEmpty()) episode = job.title;
            android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
            JSONArray previous = new JSONArray(prefs.getString("recentWorks", "[]")), saved = new JSONArray();
            String thumbnail = job.thumbnail;
            if (thumbnail.isEmpty()) for (int i = 0; i < previous.length(); i++) { JSONObject work = previous.getJSONObject(i); if (url.equals(work.optString("url"))) { thumbnail = work.optString("thumbnail"); break; } }
            saved.put(new JSONObject().put("title", title).put("url", url).put("thumbnail", thumbnail).put("episode", episode).put("state", job.state));
            for (int i = 0; i < previous.length() && saved.length() < 10; i++) { JSONObject work = previous.getJSONObject(i); if (!url.equals(work.optString("url"))) saved.put(work); }
            if (!prefs.edit().putString("recentWorks", saved.toString()).commit()) android.util.Log.e("TVMON", "최근 다운로드 작품 기록 저장 실패");
        } catch (Exception error) { android.util.Log.w("TVMON", "최근 다운로드 작품 기록 저장 실패", error); }
    }
    synchronized void rememberThumbnail(SiteClient.Item item) {
        if (item.thumbnail().isEmpty()) return;
        try {
            android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
            JSONArray saved = new JSONArray(prefs.getString("recentWorks", "[]"));
            for (int i = 0; i < saved.length(); i++) {
                JSONObject work = saved.getJSONObject(i);
                if (item.url().equals(work.optString("url")) && work.optString("thumbnail").isEmpty()) {
                    work.put("thumbnail", item.thumbnail());
                    if (!prefs.edit().putString("recentWorks", saved.toString()).commit()) android.util.Log.e("TVMON", "최근 작품 썸네일 저장 실패");
                    return;
                }
            }
        } catch (Exception error) { android.util.Log.w("TVMON", "최근 작품 썸네일 저장 실패", error); }
    }
    synchronized void removeRecentWork(String url) throws IOException {
        try {
            android.content.SharedPreferences prefs = context.getSharedPreferences("settings", 0);
            JSONArray previous = new JSONArray(prefs.getString("recentWorks", "[]")), saved = new JSONArray();
            for (int i = 0; i < previous.length(); i++) { JSONObject work = previous.getJSONObject(i); if (!url.equals(work.getString("url"))) saved.put(work); }
            if (!prefs.edit().putString("recentWorks", saved.toString()).commit()) throw new IOException("최근 작품 기록을 삭제하지 못했습니다.");
        } catch (org.json.JSONException error) { throw new IOException("최근 작품 기록을 읽지 못했습니다.", error); }
    }
    synchronized void update(String id, Consumer<Job> action) throws IOException { for (Job job : jobs) if (job.id.equals(id)) { action.accept(job); save(); return; } }
    synchronized void receiving(String id, int index, int state, long received) {
        for (Job job : jobs) if (job.id.equals(id) && job.partStates != null && index < job.partStates.length) {
            job.partStates[index] = (byte) state;
            if (received > 0) {
                long second = android.os.SystemClock.elapsedRealtime() / 1000; int slot = (int) (second % 5);
                if (job.speedSeconds[slot] != second) { job.speedSeconds[slot] = second; job.speedBytes[slot] = 0; }
                job.speedBytes[slot] += received;
            }
            return;
        }
    }
    synchronized void clearReceiving(String id) {
        for (Job job : jobs) if (job.id.equals(id) && job.partStates != null) {
            for (int i = 0; i < job.partStates.length; i++) if (job.partStates[i] == 1) job.partStates[i] = 0;
            job.transferStarted = 0;
        }
    }
    void restorePartStates(String id) throws Exception {
        Job job = find(id);
        if (job == null || job.partStates != null || job.total == 0) return;
        byte[] states = new byte[job.total];
        if (DONE.equals(job.state)) java.util.Arrays.fill(states, (byte) 2);
        else {
            AtomicFile checkpoint = new AtomicFile(new File(parts(id), "state.json"));
            if (!checkpoint.getBaseFile().exists()) return;
            JSONObject parts = new JSONObject(new String(checkpoint.readFully(), StandardCharsets.UTF_8)).getJSONObject("parts");
            for (int i = 0; i < states.length; i++) if (parts.has(String.valueOf(i))) states[i] = 2;
        }
        synchronized (this) { for (Job current : jobs) if (current.id.equals(id) && current.partStates == null) current.partStates = states; }
    }
    synchronized boolean pause(String id) throws IOException {
        for (Job job : jobs) if (job.id.equals(id) && (WAITING.equals(job.state) || RUNNING.equals(job.state))) {
            boolean active = RUNNING.equals(job.state); job.state = PAUSED; job.message = "일시정지됨 · 받은 조각은 보관됩니다."; save(); return active;
        }
        return false;
    }
    synchronized void pauseAll() throws IOException {
        for (Job job : jobs) if (WAITING.equals(job.state) || RUNNING.equals(job.state)) { job.state = PAUSED; job.message = "일시정지됨 · 받은 조각은 보관됩니다."; }
        save();
    }
    synchronized void resume(String id) throws IOException {
        for (Job job : jobs) if ((id == null || job.id.equals(id)) && (PAUSED.equals(job.state) || FAILED.equals(job.state))) { job.state = WAITING; job.message = "이어받기 대기 중"; }
        save();
    }
    synchronized void remove(String id) throws IOException {
        Job job = find(id);
        if (job == null) return;
        if (RUNNING.equals(job.state)) throw new IOException("다운로드를 일시정지한 뒤 삭제해 주세요.");
        jobs.removeIf(item -> item.id.equals(id)); save();
        clearParts(parts(id));
        // Completed public videos belong to the user and are deliberately retained.
    }
    File parts(String id) throws IOException {
        if (!UUID.fromString(id).toString().equals(id)) throw new IOException("잘못된 작업 식별자입니다.");
        File root = new File(context.getFilesDir(), "parts"), folder = new File(root, id);
        if (!folder.getCanonicalFile().getParentFile().equals(root.getCanonicalFile())) throw new IOException("잘못된 조각 저장 경로입니다.");
        return folder;
    }
    static void clearParts(File folder) throws IOException {
        if (!folder.exists()) return;
        File[] files = folder.listFiles();
        if (files == null) throw new IOException("임시 조각 폴더를 읽지 못했습니다.");
        for (File file : files) {
            if (!file.getCanonicalFile().getParentFile().equals(folder.getCanonicalFile()) || file.isDirectory()) throw new IOException("임시 조각 폴더에 예상하지 못한 경로가 있습니다.");
            if (!file.delete()) throw new IOException("임시 조각을 삭제하지 못했습니다.");
        }
        if (!folder.delete()) throw new IOException("임시 조각 폴더를 삭제하지 못했습니다.");
    }
    private void save() throws IOException {
        JSONArray values = new JSONArray(); for (Job job : jobs) values.put(job.json());
        write(file, values.toString().getBytes(StandardCharsets.UTF_8));
        context.sendBroadcast(new Intent(CHANGED).setPackage(context.getPackageName()));
    }
    static void write(AtomicFile target, byte[] bytes) throws IOException {
        FileOutputStream output = null;
        try { output = target.startWrite(); output.write(bytes); target.finishWrite(output); }
        catch (IOException e) { if (output != null) target.failWrite(output); throw e; }
    }
}

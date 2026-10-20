package com.tvmon.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import java.io.IOException;
import java.util.List;

final class TransferScheduler {
    static final String CHANNEL = "downloads";
    static final int NOTIFICATION = 10, JOB = 100;
    private static volatile boolean removing;
    private static volatile boolean pausing;
    static boolean removing() { return removing; }
    static boolean busy() { return removing || pausing; }

    static void channel(Context context) {
        context.getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(CHANNEL, "다운로드 진행 및 완료", NotificationManager.IMPORTANCE_LOW));
    }
    static void start(Context context) throws IOException {
        if (busy()) throw new IOException("다운로드 상태를 변경하고 있습니다. 잠시 후 다시 시도해 주세요.");
        channel(context);
        if (DownloadEngine.running()) return;
        if (Build.VERSION.SDK_INT >= 34) {
            JobInfo job = new JobInfo.Builder(JOB, new ComponentName(context, TransferJob.class))
                    .setUserInitiated(true)
                    .setRequiredNetwork(new NetworkRequest.Builder()
                            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                            // The default NOT_VPN requirement blocks VPN/ad-blocker networks.
                            .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN).build())
                    .build();
            if (context.getSystemService(JobScheduler.class).schedule(job) != JobScheduler.RESULT_SUCCESS) throw new IOException("다운로드를 시작하지 못했습니다. 앱을 연 상태에서 다시 시도해 주세요.");
        } else context.startForegroundService(new Intent(context, TransferService.class));
    }
    // The engine runs one episode at a time. Pause only this record, then let other waiting episodes continue.
    static synchronized boolean pause(Context context, String id) throws Exception {
        pausing = true;
        try {
            DownloadStore store = DownloadStore.get(context);
            if (!store.pause(id)) return false;
            JobScheduler scheduler = context.getSystemService(JobScheduler.class);
            boolean restart = DownloadEngine.running() || scheduler.getPendingJob(JOB) != null;
            scheduler.cancel(JOB); context.stopService(new Intent(context, TransferService.class)); DownloadEngine.cancel();
            long deadline = android.os.SystemClock.elapsedRealtime() + 20000;
            while (DownloadEngine.running()) {
                if (android.os.SystemClock.elapsedRealtime() >= deadline) throw new IOException("일시정지를 마무리하고 있습니다. 잠시 후 다시 시도해 주세요.");
                Thread.sleep(50);
            }
            context.getSystemService(NotificationManager.class).cancel(NOTIFICATION);
            return restart && store.next() != null;
        } finally { pausing = false; }
    }
    // Called off the main thread. Never delete part files while transfer workers can still write them.
    static synchronized boolean remove(Context context, List<String> ids) throws Exception {
        removing = true;
        DownloadStore store = DownloadStore.get(context);
        boolean restart = false;
        try {
            boolean pending = false;
            for (String id : ids) { DownloadStore.Job job = store.find(id); if (job != null && !DownloadStore.DONE.equals(job.state)) pending = true; }
            if (pending) {
                JobScheduler scheduler = context.getSystemService(JobScheduler.class);
                restart = DownloadEngine.running() || scheduler.getPendingJob(JOB) != null;
                for (String id : ids) store.update(id, job -> { if (DownloadStore.RUNNING.equals(job.state) || DownloadStore.WAITING.equals(job.state)) job.state = DownloadStore.PAUSED; });
                scheduler.cancel(JOB); context.stopService(new Intent(context, TransferService.class)); DownloadEngine.cancel();
            }
            // A paused job can still be finishing cancellation; wait before cleaning any selected parts.
            long deadline = android.os.SystemClock.elapsedRealtime() + 20000;
            while (pending && DownloadEngine.running()) {
                if (android.os.SystemClock.elapsedRealtime() >= deadline) throw new IOException("다운로드 중단을 기다리고 있습니다. 잠시 후 다시 제거해 주세요.");
                Thread.sleep(50);
            }
            for (String id : ids) store.remove(id);
            if (pending || store.snapshot().isEmpty()) context.getSystemService(NotificationManager.class).cancel(NOTIFICATION);
            return restart && store.next() != null;
        } finally { removing = false; }
    }
    static Notification notification(Context context, DownloadStore.Job job) {
        channel(context);
        PendingIntent open = PendingIntent.getActivity(context, 0, new Intent(context, MainActivity.class), PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent pause = PendingIntent.getBroadcast(context, 1, new Intent(context, PauseReceiver.class), PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        boolean active = job == null || DownloadStore.RUNNING.equals(job.state);
        Notification.Builder builder = new Notification.Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_download).setContentTitle(job == null ? "TVMON 다운로드 준비 중" : job.title)
                .setContentText(job == null ? "대기열을 확인하고 있습니다…" : job.message)
                .setContentIntent(open).setOnlyAlertOnce(true).setOngoing(active);
        if (active) builder.addAction(new Notification.Action.Builder(null, "전체 일시정지", pause).build());
        if (active && job != null && job.total > 0) builder.setProgress(job.total, job.done, false);
        if (!active) builder.setSmallIcon(DownloadStore.DONE.equals(job.state) ? android.R.drawable.stat_sys_download_done : android.R.drawable.stat_notify_error).setAutoCancel(true);
        return builder.build();
    }

    public static final class PauseReceiver extends BroadcastReceiver {
        @Override public void onReceive(Context context, Intent intent) {
            PendingResult pending = goAsync();
            new Thread(() -> {
                try {
                    DownloadStore.get(context).pauseAll(); DownloadEngine.cancel();
                    context.getSystemService(JobScheduler.class).cancel(JOB);
                    context.stopService(new Intent(context, TransferService.class));
                    context.getSystemService(NotificationManager.class).cancel(NOTIFICATION);
                } catch (Exception e) { android.util.Log.e("TVMON", "일시정지 실패", e); }
                finally { pending.finish(); }
            }, "tvmon-pause").start();
        }
    }
}

package com.tvmon.android;

import android.app.job.JobParameters;
import android.app.job.JobService;
import android.os.Handler;
import android.os.Looper;

@android.annotation.TargetApi(34)
public final class TransferJob extends JobService {
    private SiteClient.Stop stop;
    private Thread worker;
    private long lastNotice;
    private volatile boolean stopped;

    @Override public boolean onStartJob(JobParameters params) {
        stopped = false; stop = new SiteClient.Stop();
        setNotification(params, TransferScheduler.NOTIFICATION, TransferScheduler.notification(this, null), JOB_END_NOTIFICATION_POLICY_DETACH);
        worker = new Thread(() -> {
            DownloadEngine.runQueue(this, stop, job -> {
                long now = android.os.SystemClock.elapsedRealtime();
                if (job != null && (now - lastNotice >= 1000 || !DownloadStore.RUNNING.equals(job.state))) {
                    lastNotice = now;
                    new Handler(Looper.getMainLooper()).post(() -> {
                        if (!stopped) setNotification(params, TransferScheduler.NOTIFICATION, TransferScheduler.notification(this, job), JOB_END_NOTIFICATION_POLICY_DETACH);
                    });
                }
            });
            new Handler(Looper.getMainLooper()).post(() -> { if (!stopped) jobFinished(params, DownloadStore.get(this).next() != null); });
        }, "tvmon-download");
        worker.start(); return true;
    }
    @Override public boolean onStopJob(JobParameters params) {
        stopped = true; if (stop != null) stop.cancel(); if (worker != null) worker.interrupt();
        return true;
    }
}

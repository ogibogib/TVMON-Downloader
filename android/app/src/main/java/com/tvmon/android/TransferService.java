package com.tvmon.android;

import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.os.PowerManager;

/** Foreground transfer for Android 10–13; Android 14+ uses TransferJob. */
public final class TransferService extends Service {
    private SiteClient.Stop stop;
    private Thread worker;
    private PowerManager.WakeLock wake;
    private long lastNotice;

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(TransferScheduler.NOTIFICATION, TransferScheduler.notification(this, null));
        if (worker != null && worker.isAlive()) return START_NOT_STICKY;
        stop = new SiteClient.Stop();
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TVMON:Download"); wake.acquire(6 * 60 * 60 * 1000L);
        worker = new Thread(() -> {
            try {
                DownloadEngine.runQueue(this, stop, job -> {
                    long now = android.os.SystemClock.elapsedRealtime();
                    if (job != null && (now - lastNotice >= 1000 || !DownloadStore.RUNNING.equals(job.state))) {
                        lastNotice = now; getSystemService(NotificationManager.class).notify(TransferScheduler.NOTIFICATION, TransferScheduler.notification(this, job));
                    }
                });
            } finally {
                if (wake.isHeld()) wake.release();
                stopForeground(STOP_FOREGROUND_DETACH); stopSelf();
            }
        }, "tvmon-download"); worker.start(); return START_NOT_STICKY;
    }
    @Override public void onDestroy() {
        if (stop != null) stop.cancel(); if (worker != null) worker.interrupt();
        if (wake != null && wake.isHeld()) wake.release(); super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}

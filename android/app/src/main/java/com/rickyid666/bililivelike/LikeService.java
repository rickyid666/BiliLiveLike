package com.rickyid666.bililivelike;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * 前台服务: 点赞任务运行期间保活 (息屏 / 切后台 / 锁屏时不至于被系统掐断网络)。
 * 同时持有一把 PARTIAL_WAKE_LOCK, 让 CPU 在息屏时保持工作。
 */
public class LikeService extends Service {

    private static final String CH_ID = "bll_like";
    private static final int NOTI_ID = 1001;
    private static final long WAKE_TIMEOUT_MS = 3 * 60 * 60 * 1000L;   // 最长 3 小时

    private static LikeService instance;

    private PowerManager.WakeLock wake;
    private String text = "正在点赞中…";

    public static void start(Context ctx, String text) {
        Intent i = new Intent(ctx, LikeService.class);
        i.putExtra("text", text);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ctx.startForegroundService(i);
        } else {
            ctx.startService(i);
        }
    }

    public static void stop(Context ctx) {
        ctx.stopService(new Intent(ctx, LikeService.class));
    }

    public static void update(String text) {
        LikeService s = instance;
        if (s != null && text != null) {
            s.text = text;
            s.notifyNow();
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        createChannel();
        startForeground(NOTI_ID, buildNotification());

        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bll:like");
                wake.setReferenceCounted(false);
                wake.acquire(WAKE_TIMEOUT_MS);
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && intent.getStringExtra("text") != null) {
            text = intent.getStringExtra("text");
        }
        notifyNow();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        try {
            if (wake != null && wake.isHeld()) {
                wake.release();
            }
        } catch (Exception ignored) {
        }
        instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null && nm.getNotificationChannel(CH_ID) == null) {
                NotificationChannel ch = new NotificationChannel(
                        CH_ID, "点赞任务", NotificationManager.IMPORTANCE_LOW);
                ch.setShowBadge(false);
                ch.enableVibration(false);
                nm.createNotificationChannel(ch);
            }
        }
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CH_ID)
                : new Notification.Builder(this);
        b.setContentTitle("BiliLiveLike 正在点赞")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_stat_like)
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(pi);
        return b.build();
    }

    private void notifyNow() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.notify(NOTI_ID, buildNotification());
            }
        } catch (Exception ignored) {
        }
    }
}

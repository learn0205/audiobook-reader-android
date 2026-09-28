package org.audiobookreader.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;

/**
 * 通知栏媒体控制 + MediaSession（耳机线控 / 蓝牙按键）。
 *
 * 全部用 framework 类（无 androidx 依赖）。通知动作按钮用 PendingIntent.getBroadcast
 * 发到**动态注册**的接收器（不必改 Manifest）；接收器再经 MediaActionListener
 * （Python 实现）把动作交回引擎。API 兼容范围 minapi 24：
 *  - NotificationChannel 仅 26+，低版本走旧构造器；
 *  - registerReceiver 在 33+ 必须带 RECEIVER_NOT_EXPORTED；
 *  - Notification.MediaStyle 24+ 公开，失败则退普通通知，不影响功能。
 *
 * ⚠️ MediaSession 在部分 OEM ROM 上构造即抛 NullPointerException（本机即如此，
 * 自检屏显示「媒体控制 未挂 JVM exception: NullPointerException」）。这种情况
 * 不再让异常吞掉整条通知——降级为「无 MediaSession 的纯通知」，书名/控制按钮
 * 照常工作；MediaStyle 媒体卡片仅在 session 可用时挂上。
 */
public class MediaControls {
    public static final String ACTION_PREV = "org.audiobookreader.android.MEDIA_PREV";
    public static final String ACTION_PLAYPAUSE = "org.audiobookreader.android.MEDIA_PLAYPAUSE";
    public static final String ACTION_NEXT = "org.audiobookreader.android.MEDIA_NEXT";
    private static final String CHANNEL_ID = "playback_controls";
    private static final int NOTIF_ID = 424242;

    private static MediaSession sSession;
    private static BroadcastReceiver sReceiver;
    private static MediaActionListener sListener;
    private static boolean sPlaying = false;
    private static String sTitle = "";

    public static void setup(Context ctx, MediaActionListener listener) {
        sListener = listener;
        // ---- MediaSession：部分 ROM 构造即抛 NPE，单独 try，失败则降级纯通知 ----
        if (sSession == null) {
            try {
                sSession = new MediaSession(ctx, "AudioBookReader");
                sSession.setCallback(new MediaSession.Callback() {
                    @Override public void onPlay() { fire("playpause"); }
                    @Override public void onPause() { fire("playpause"); }
                    @Override public void onSkipToNext() { fire("next"); }
                    @Override public void onSkipToPrevious() { fire("prev"); }
                });
                sSession.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
                        | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
                sSession.setActive(true);
            } catch (Exception ignore) {
                sSession = null;   // MediaSession 不可用：后续走纯通知降级路径
            }
        }
        // ---- 动作接收器：无论有没有 session 都要注册（通知栏按钮/线控仍可用） ----
        if (sReceiver == null) {
            sReceiver = new BroadcastReceiver() {
                @Override public void onReceive(Context c, Intent i) {
                    String a = i.getAction();
                    if (ACTION_NEXT.equals(a)) fire("next");
                    else if (ACTION_PREV.equals(a)) fire("prev");
                    else if (ACTION_PLAYPAUSE.equals(a)) fire("playpause");
                }
            };
            IntentFilter f = new IntentFilter();
            f.addAction(ACTION_PREV); f.addAction(ACTION_PLAYPAUSE); f.addAction(ACTION_NEXT);
            if (Build.VERSION.SDK_INT >= 33) {
                ctx.registerReceiver(sReceiver, f, Context.RECEIVER_NOT_EXPORTED);
            } else {
                ctx.registerReceiver(sReceiver, f);
            }
        }
    }

    /** 更新播放态 + 通知（playing / 标题）。playing=true 显示「暂停」按钮。 */
    public static void update(Context ctx, boolean playing, String title) {
        sPlaying = playing;
        if (title != null && !title.isEmpty()) sTitle = title;

        // 媒体元数据：让锁屏 / 通知栏的媒体卡片显示书名，而不是泛化的占位文案
        // （不设置时部分机型/锁屏会显示成 "playback" 一类的通用标签）。
        if (sSession != null) {
            try {
                sSession.setPlaybackState(new PlaybackState.Builder()
                        .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                                | PlaybackState.ACTION_PLAY_PAUSE
                                | PlaybackState.ACTION_SKIP_TO_NEXT
                                | PlaybackState.ACTION_SKIP_TO_PREVIOUS)
                        .setState(playing ? PlaybackState.STATE_PLAYING
                                : PlaybackState.STATE_PAUSED, 0,
                                playing ? 1.0f : 0.0f)
                        .build());
                MediaMetadata.Builder mb = new MediaMetadata.Builder();
                mb.putString(MediaMetadata.METADATA_KEY_TITLE,
                        sTitle.isEmpty() ? "有声书朗读" : sTitle);
                mb.putString(MediaMetadata.METADATA_KEY_ARTIST, "有声书朗读");
                sSession.setMetadata(mb.build());
            } catch (Exception ignore) { }
        }

        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "朗读控制",
                    NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(ch);
        }
        try {
            Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(ctx, CHANNEL_ID)
                    : new Notification.Builder(ctx);
            b.setSmallIcon(android.R.drawable.ic_media_play)
             .setContentTitle(sTitle.isEmpty() ? "有声书朗读" : sTitle)
             .setContentText(playing ? "正在朗读" : "已暂停")
             .setOngoing(playing)
             .setOnlyAlertOnce(true)
             .setContentIntent(PendingIntent.getActivity(ctx, 0,
                     new Intent(ctx, ctx.getClass()),
                     PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE))
             .addAction(android.R.drawable.ic_media_previous, "上一章",
                     pi(ctx, ACTION_PREV, 1))
             .addAction(playing ? android.R.drawable.ic_media_pause
                                : android.R.drawable.ic_media_play,
                     playing ? "暂停" : "播放", pi(ctx, ACTION_PLAYPAUSE, 2))
             .addAction(android.R.drawable.ic_media_next, "下一章",
                     pi(ctx, ACTION_NEXT, 3));
            if (Build.VERSION.SDK_INT >= 24 && sSession != null) {
                try {
                    b.setStyle(new Notification.MediaStyle()
                            .setMediaSession(sSession.getSessionToken()));
                } catch (Exception ignore) { }
            }
            nm.notify(NOTIF_ID, b.build());
        } catch (Exception ignore) { }
    }

    public static void teardown(Context ctx) {
        if (sReceiver != null) {
            try { ctx.unregisterReceiver(sReceiver); } catch (Exception ignore) { }
            sReceiver = null;
        }
        if (sSession != null) {
            try { sSession.setActive(false); sSession.release(); } catch (Exception ignore) { }
            sSession = null;
        }
        try {
            NotificationManager nm = (NotificationManager)
                    ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(NOTIF_ID);
        } catch (Exception ignore) { }
    }

    private static PendingIntent pi(Context ctx, String action, int rc) {
        Intent it = new Intent(action).setPackage(ctx.getPackageName());
        return PendingIntent.getBroadcast(ctx, rc, it,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static void fire(String action) {
        if (sListener != null) sListener.onAction(action);
    }
}

package org.audiobookreader.android;

import android.os.Handler;
import android.os.Looper;
import android.speech.tts.UtteranceProgressListener;

/**
 * 挂到 TextToSpeech.setOnUtteranceProgressListener 上的真监听器
 * （UtteranceProgressListener 的真子类，随 APK 编译进 classes.dex）。
 *
 * TTS 回调从 binder 线程送达，这里统一 post 到主线程再转发，
 * 保证 Python 侧的引擎状态推进与 poll_advance / speak 调用同线程。
 */
public class TTSProgressListener extends UtteranceProgressListener {
    private final TTSProgressCallback cb;
    private final Handler main = new Handler(Looper.getMainLooper());

    public TTSProgressListener(TTSProgressCallback cb) {
        this.cb = cb;
    }

    @Override
    public void onStart(final String utteranceId) {
        if (cb == null) return;
        main.post(new Runnable() { public void run() { cb.onStart(utteranceId); } });
    }

    @Override
    public void onDone(final String utteranceId) {
        if (cb == null) return;
        main.post(new Runnable() { public void run() { cb.onDone(utteranceId); } });
    }

    @Override
    public void onError(String utteranceId) {
        onError(utteranceId, -1);
    }

    @Override
    public void onError(final String utteranceId, final int errorCode) {
        if (cb == null) return;
        main.post(new Runnable() { public void run() { cb.onError(utteranceId, errorCode); } });
    }
}

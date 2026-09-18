package org.audiobookreader.android;

/**
 * 朗读进度回调接口 —— 专供 pyjnius 实现用。
 *
 * pyjnius 的 PythonJavaClass 底层是 java.lang.reflect.Proxy，只能实现
 * **接口**；而 TextToSpeech.setOnUtteranceProgressListener 需要的是抽象类
 * UtteranceProgressListener 的子类（Proxy 传抽象类直接抛
 * IllegalArgumentException: ... is not an interface）。
 * 所以真正挂到 TextToSpeech 上的是本包内的 TTSProgressListener（Java 抽象类
 * 的真子类），它把事件转发给 Python 实现的本接口。
 */
public interface TTSProgressCallback {
    void onStart(String utteranceId);
    void onDone(String utteranceId);
    void onError(String utteranceId, int errorCode);
}

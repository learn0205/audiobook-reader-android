package org.audiobookreader.android;

/**
 * 通知栏/耳机媒体按键的动作回调接口 —— 专供 pyjnius 实现
 * （PythonJavaClass 只能实现接口，见 TTSProgressCallback 的说明）。
 */
public interface MediaActionListener {
    /** action: "playpause" / "next" / "prev" */
    void onAction(String action);
}

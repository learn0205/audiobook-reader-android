package org.audiobookreader.android;

/**
 * 通知栏/耳机媒体按键的动作回调接口 —— 专供 pyjnius 实现
 * （PythonJavaClass 只能实现接口，见 TTSProgressCallback 的说明）。
 */
public interface MediaActionListener {
    /**
     * action: "play" / "pause" / "next" / "prev"（推荐）；
     * 兼容旧版： "playpause"（单键切换，新代码不再发送）。
     *
     * ⚠️ 「播放」与「暂停」必须是**两个**独立动作：外部命令经常重复到达
     * （蓝牙重连自动续播、锁屏/ROM 重发媒体键），若统一用「切换」语义，
     * 重复的播放请求会把「正在播」反成「暂停」——表现就是念着念着自己停下。
     */
    void onAction(String action);
}

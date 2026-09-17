# -*- coding: utf-8 -*-
"""edge_tts_client.py —— 纯标准库实现的微软 Edge 在线 TTS 客户端。

为什么不用官方 edge-tts 库？
  它依赖 aiohttp + websockets，而 aiohttp 在 python-for-android 下编译经常失败，
  一旦失败会把**整个 App 的构建**拖垮。这里只用 Python 标准库
  （socket / ssl / struct / uuid / json / urllib / hashlib / zlib）手写
  WebSocket 握手与 Edge 的语音合成协议，p4a 可直接打包，且本机与手机上行为一致。

协议要点（与官方 edge-tts 同源，参考 rany2/edge-tts）：
  · 音色列表：GET https://speech.platform.bing.com/.../voices/list?trustedclienttoken=...
  · 合成：wss://speech.platform.bing.com/.../readaloud/edge/v1?...&Sec-MS-GEC=...
          先发 speech.config（JSON），再发 ssml（XML），
          服务端回 binary 帧（Path:audio 后跟 mp3 字节），最后回 turn.end。
  · 关键鉴权：URL 上必须带 Sec-MS-GEC（每 5 分钟轮换的 SHA256 令牌），否则 401。

本模块在桌面环境也能 import / 直接跑测试，不依赖安卓。
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.request
import uuid
import xml.sax.saxutils as su
import zlib

# ---- 端点与常量（必须与官方一致，否则 401 / 列表不全）----
_TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
_WS_HOST = "speech.platform.bing.com"
_WS_PORT = 443
_WS_PATH = "/consumer/speech/synthesize/readaloud/edge/v1"
_VOICES_URL = (
    "https://speech.platform.bing.com/consumer/speech/synthesize/"
    "readaloud/voices/list?trustedclienttoken=" + _TRUSTED_CLIENT_TOKEN
)
# 24kbps 单声道 mp3：体积小、流式友好，听书足够清晰
_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
_CHROMIUM_FULL = "143.0.3650.75"
_SEC_MS_GEC_VERSION = "1-" + _CHROMIUM_FULL
_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROMIUM_FULL.split('.')[0]}.0.0.0 "
    f"Safari/537.36 Edg/{_CHROMIUM_FULL.split('.')[0]}.0.0.0"
)
_ORIGIN = "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold"

WIN_EPOCH = 11644473600          # 1601-01-01 起的秒数
S_TO_NS = 1e9


def _generate_sec_ms_gec():
    """生成 Sec-MS-GEC 令牌：当前时间（Windows 文件时间，向下取整 5 分钟）拼接 token 后 SHA256。

    与官方 edge-tts 的 DRM.generate_sec_ms_gec() 完全等价（忽略时钟偏差）。
    """
    ticks = time.time() + WIN_EPOCH
    ticks -= ticks % 300                       # 向下取整到最近 5 分钟
    ticks *= S_TO_NS / 100                     # 转成 100 纳秒间隔
    s = f"{ticks:.0f}{_TRUSTED_CLIENT_TOKEN}"
    return hashlib.sha256(s.encode("ascii")).hexdigest().upper()


def _muid():
    return uuid.uuid4().hex.upper()


def _make_ssl_context():
    """构造一个「能找到 CA 证书」的 SSLContext。

    ⚠️ 安卓上 Python 的 ssl 默认**一个 CA 都找不到**（certifi / OpenSSL 的默认
    证书路径在 p4a 打包后都不存在），于是所有 HTTPS 请求都会报：
        [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
    表现就是 Edge TTS 完全不可用（音色列表、合成都失败）。

    这里显式加载安卓系统 CA 目录 `/system/etc/security/cacerts`——它是 OpenSSL
    的 capath 格式（文件名即 subject hash），`load_verify_locations(capath=...)`
    可以直接吃。再兜底试几个常见路径；万一还是拿不到 CA，才关闭校验以保证可用。
    """
    ctx = ssl.create_default_context()

    def _has_ca():
        try:
            return bool(ctx.get_ca_certs())
        except Exception:
            return False

    if not _has_ca():
        # ① 先把安卓 CA 目录里的 PEM **拼成一个 bundle** 直接加载。
        #    这样不依赖 OpenSSL 的 capath 命名规则（各版本 subject hash 算法不一致，
        #    用 capath 可能静默加载不到任何证书）。
        for d in ("/system/etc/security/cacerts",
                  "/data/misc/user/0/cacerts-added"):
            pems = _read_pem_bundle(d)
            if pems:
                try:
                    ctx.load_verify_locations(cadata=pems)
                except Exception:
                    pass
                if _has_ca():
                    break

    if not _has_ca():
        # ② 再试标准 capath（桌面 / 部分 ROM 可用）
        for capath in ("/etc/ssl/certs", "/etc/pki/tls/certs",
                       "/system/etc/security/cacerts"):
            try:
                ctx.load_verify_locations(capath=capath)
            except Exception:
                continue
            if _has_ca():
                break

    if not _has_ca():
        # ③ 最后兜底：拿不到任何 CA 就只能关校验，否则功能彻底不可用
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _read_pem_bundle(directory):
    """把目录下所有 PEM 证书拼成一个字符串（找不到就返回空串）。"""
    try:
        names = sorted(os.listdir(directory))
    except Exception:
        return ""
    chunks = []
    for name in names:
        try:
            with open(os.path.join(directory, name), "rb") as f:
                data = f.read()
        except Exception:
            continue
        if b"BEGIN CERTIFICATE" in data:
            chunks.append(data.decode("ascii", "ignore"))
    return "\n".join(chunks)


def list_voices(retries=3):
    """返回可用音色（原始 dict 列表，含 ShortName / Gender / Locale / FriendlyName）。"""
    req = urllib.request.Request(_VOICES_URL, headers={
        "Authority": _WS_HOST,
        "User-Agent": _EDGE_UA,
        "Accept": "*/*",
        "Sec-CH-UA": ('" Not;A Brand";v="99", "Microsoft Edge";v="143", '
                      '"Chromium";v="143"'),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = _make_ssl_context()          # 安卓上必须显式带上 CA，否则证书校验失败
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Edge TTS 获取音色列表失败（已重试 {retries} 次）: {last_err}")


def _js_date():
    return (time.strftime("%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime())
            + " (Coordinated Universal Time)")


def _ws_handshake(ssock, conn_id):
    """完成 WebSocket 握手，返回 (use_deflate: bool)。

    use_deflate=True 仅当服务端在 101 响应里回显了 sec-websocket-extensions:
    permessage-deflate。**只有此时**客户端才能压缩出站帧、并对下行帧做 deflate
    解压；否则（绝大多数情况服务端不协商）必须明文收发，否则服务端会直接断开连接。
    """
    gec = _generate_sec_ms_gec()
    path = (f"{_WS_PATH}?TrustedClientToken={_TRUSTED_CLIENT_TOKEN}"
            f"&ConnectionId={conn_id}"
            f"&Sec-MS-GEC={gec}"
            f"&Sec-MS-GEC-Version={_SEC_MS_GEC_VERSION}")
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {_WS_HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: {_EDGE_UA}\r\n"
        f"Origin: {_ORIGIN}\r\n"
        "Pragma: no-cache\r\n"
        "Cache-Control: no-cache\r\n"
        "Accept: */*\r\n"
        "Accept-Encoding: gzip, deflate, br, zstd\r\n"
        "Accept-Language: en-US,en;q=0.9\r\n"
        "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
        f"Cookie: muid={_muid()};\r\n"
        "\r\n"
    )
    ssock.sendall(req.encode("ascii"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = ssock.recv(4096)
        if not chunk:
            raise ConnectionError("Edge TTS 握手无响应")
        resp += chunk
    header_blob, _, _ = resp.partition(b"\r\n\r\n")
    header_text = header_blob.decode("ascii", "ignore")
    status_line = header_text.split("\r\n", 1)[0]
    if "101" not in status_line:
        raise RuntimeError("Edge TTS WebSocket 握手失败: " + status_line)
    low = header_text.lower()
    use_deflate = ("sec-websocket-extensions" in low
                   and "permessage-deflate" in low)
    return use_deflate


# permessage-deflate：原始 deflate 流，解压时需补回 spec 移除的 4 字节尾部
_DEFLATE_TAIL = b"\x00\x00\xff\xff"


def _inflate(data):
    """解压单个 permessage-deflate 帧（非分片消息）。"""
    d = zlib.decompressobj(-zlib.MAX_WBITS)
    return d.decompress(data + _DEFLATE_TAIL)


def _send_frame(ssock, payload, opcode=1, comp=None):
    """客户端发出的帧必须加掩码（mask）。opcode: 1=text, 2=binary。

    comp 为 zlib.compressobj(-MAX_WBITS) 时，按 permessage-deflate 压缩出站帧
    （置 RSV1 位），与 aiohttp compress=15 的行为一致——服务端期望收到压缩帧，
    否则会直接断开。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    rsv1 = 0
    if comp is not None:
        # 压缩 + sync flush，并剥掉末尾 4 字节的 00 00 ff ff 标记
        blob = comp.compress(payload) + comp.flush(zlib.Z_SYNC_FLUSH)
        if len(blob) > 4 and blob[-4:] == _DEFLATE_TAIL:
            blob = blob[:-4]
        payload = blob
        rsv1 = 0x40
    length = len(payload)
    mask = os.urandom(4)                      # 掩码密钥（必须随帧发出！）
    header = bytearray()
    header.append(0x80 | rsv1 | opcode)       # FIN=1 (+RSV1)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    # 帧结构：header(2) + mask(4) + masked_payload。掩码密钥必须紧跟长度之后发出，
    # 否则服务端会把负载的前 4 字节当成掩码密钥、解掩码错位 → 直接断开连接。
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    ssock.sendall(bytes(header) + mask + masked)


def _read_frame(ssock, decomp):
    """读一个 WebSocket 帧，返回 (fin, opcode, payload)。遇到 close 帧返回 (True, 8, b'')。

    fin 表示是否为消息的最后一帧（用于跨帧分片重组）。
    支持 permessage-deflate：下行帧若置 RSV1 位（0x40）则用 decomp 解压。
    decomp 是整条连接共用的 zlib.decompressobj(-MAX_WBITS)（服务器采用
    context takeover，必须在消息间保留解压状态）。
    """
    hdr = _recv_exact(ssock, 2)
    b0, b1 = hdr[0], hdr[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    rsv1 = bool(b0 & 0x40)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(ssock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(ssock, 8))[0]
    if masked:
        mask = _recv_exact(ssock, 4)          # 服务端→客户端一般不掩码，有则记录
    payload = _recv_exact(ssock, length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if rsv1 and decomp is not None:
        # 仅当服务端确实协商了 deflate 才解压；否则保留原始（明文）payload
        payload = decomp.decompress(payload + _DEFLATE_TAIL)
    return fin, opcode, payload


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Edge TTS 连接被提前关闭")
        buf += chunk
    return buf


def _full_voice_name(voice):
    """把 ShortName（zh-CN-YunxiNeural）转成 Edge SSML 要求的完整名。

    Edge 服务端只认完整名，例如
    ``Microsoft Server Speech Text to Speech Voice (zh-CN, YunxiNeural)``，
    传 ShortName 会被直接拒绝（连接被关）。注意完整名里**只保留本地名部分**
    （YunxiNeural），不能把 locale 再拼回去，否则会变成
    ``(zh-CN, zh-CN-YunxiNeural)`` 这种非法名字被服务端断开。
    """
    if voice.startswith("Microsoft Server Speech Text to Speech Voice ("):
        return voice
    parts = voice.split("-")
    if len(parts) >= 3:
        locale = f"{parts[0]}-{parts[1]}"
        name = "-".join(parts[2:])      # 仅取 locale 之后的名字部分
    else:
        locale = parts[0]
        name = "-".join(parts[1:]) if len(parts) > 1 else parts[0]
    return f"Microsoft Server Speech Text to Speech Voice ({locale}, {name})"


def _build_messages(voice, text, rate, pitch, volume):
    # 注意：下面所有头字段的冒号后**没有空格**，且 SSML 的 X-Timestamp 末尾带 Z，
    # 这些细节 Edge 服务端都会严格校验，照抄官方 edge-tts 的格式（已逐字节核对）。
    # 关键点：sentenceBoundaryEnabled 官方用 "true"；prosody 属性顺序为
    # pitch 在前 rate 在后；音色名必须是完整名。
    full_voice = _full_voice_name(voice)
    ts = _js_date()
    config = (
        "X-Timestamp:{ts}\r\n"
        "Content-Type:application/json; charset=utf-8\r\n"
        "Path:speech.config\r\n"
        "\r\n"
        '{{"context":{{"synthesis":{{"audio":{{"metadataoptions":{{'
        '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"}},'
        '"outputFormat":"{fmt}"}}}}}}}}\r\n'
    ).format(ts=ts, fmt=_OUTPUT_FORMAT)

    ssml = (
        "X-RequestId:{rid}\r\n"
        "Content-Type:application/ssml+xml\r\n"
        "X-Timestamp:{ts}Z\r\n"          # 官方确有这个尾巴（微软 bug，照抄）
        "Path:ssml\r\n"
        "\r\n"
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        "xml:lang='en-US'>"
        "<voice name='{voice}'><prosody pitch='{pitch}' rate='{rate}' "
        "volume='{volume}'>{text}</prosody></voice></speak>"
    ).format(rid=str(uuid.uuid4()), ts=ts, voice=full_voice,
             rate=rate, pitch=pitch, volume=volume, text=text)
    return config, ssml


def synthesize(text, voice, rate="+0%", pitch="+0Hz", volume="+0%",
               timeout=40, retries=2, debug=False):
    """合成一句话，返回 mp3 字节（bytes）。失败抛异常（含重试）。"""
    safe_text = su.escape(text)
    config, ssml = _build_messages(voice, safe_text, rate, pitch, volume)

    last_err = None
    for attempt in range(retries + 1):
        try:
            ctx = _make_ssl_context()      # 安卓上必须显式带上 CA（见 _make_ssl_context）
            sock = socket.create_connection((_WS_HOST, _WS_PORT), timeout=timeout)
            ssock = ctx.wrap_socket(sock, server_hostname=_WS_HOST)
            try:
                use_deflate = _ws_handshake(ssock, str(uuid.uuid4()))
                # 仅当服务端确实协商了 permessage-deflate 才启用压缩 / 解压。
                # 否则明文收发——之前“连接被提前关闭”的根因正是：服务端未协商
                # deflate，客户端却压缩了出站帧（带 RSV1 位），服务端视为协议违规直接断开。
                comp = zlib.compressobj(wbits=-zlib.MAX_WBITS) if use_deflate else None
                _send_frame(ssock, config, opcode=1, comp=comp)
                _send_frame(ssock, ssml, opcode=1, comp=comp)
                # 解压器整条连接共用（context takeover）。未协商时为 None。
                decomp = zlib.decompressobj(-zlib.MAX_WBITS) if use_deflate else None
                audio = bytearray()
                turn_ended = False
                # 跨帧分片重组：一条消息可能由多帧组成（首帧 opcode=2/FIN=0，
                # 其余 opcode=0 续帧，末帧 FIN=1）。必须整条消息拼齐后再剥离
                # "Path:audio...\\r\\n\\r\\n" 头，否则会把头字节混进 mp3 导致文件损坏。
                msg_op = None
                msg_buf = bytearray()
                while not turn_ended:
                    fin, opcode, payload = _read_frame(ssock, decomp)
                    if opcode == 8:               # close
                        break
                    if opcode == 9:               # ping -> 必须回 pong
                        _send_frame(ssock, payload, opcode=10, comp=None)
                        if debug:
                            print(f"[debug] ping -> pong ({len(payload)}B)")
                        continue
                    if opcode == 10:              # pong
                        if debug:
                            print("[debug] pong")
                        continue
                    if opcode == 0:               # 续帧：拼到当前消息
                        msg_buf += payload
                    else:                         # 新消息首帧
                        msg_op = opcode
                        msg_buf = bytearray(payload)
                    if not fin:                   # 还有后续分片，继续拼
                        continue
                    # —— 一条完整消息已拼齐，按类型处理 ——
                    if msg_op == 2:               # binary = audio
                        # Edge 下行音频帧结构（与官方 edge-tts 一致）：
                        #   前 2 字节 = header_length（大端），
                        #   随后 header_length 字节 = 头部（X-RequestId/Path:audio…），
                        #   再后面才是 mp3 字节。
                        # 不要去找 "Path:audio" 或 "\\r\\n\\r\\n"——那是头部的内部内容，
                        # 必须按显式的长度前缀精确切分，否则会把头字节混进 mp3。
                        if len(msg_buf) < 2:
                            pass
                        else:
                            header_length = int.from_bytes(msg_buf[:2], "big")
                            if 2 + header_length <= len(msg_buf):
                                audio += msg_buf[2 + header_length:]
                            else:
                                # 长度异常时退回按首行空白行兜底
                                sep = msg_buf.find(b"\r\n\r\n")
                                if sep != -1:
                                    audio += msg_buf[sep + 4:]
                        if debug:
                            print(f"[debug] audio 累计 {len(audio)}B")
                    elif msg_op == 1:             # text = 状态 / 结束标记
                        if debug:
                            print(f"[debug] text: {msg_buf[:80]!r}")
                        if b"turn.end" in msg_buf:
                            turn_ended = True
                    msg_op = None
                    msg_buf = bytearray()
                if debug:
                    print(f"[debug] 结束，音频共 {len(audio)}B"
                          f"（deflate={use_deflate}）")
                return bytes(audio)
            finally:
                try:
                    ssock.close()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Edge TTS 合成失败（已重试 {retries} 次）: {last_err}")


def synthesize_to_file(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
    data = synthesize(text, voice, rate, pitch, volume)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


# 便于在子线程里跑（合成是阻塞 IO，避免卡 UI 主线程）
def _run_in_thread(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t

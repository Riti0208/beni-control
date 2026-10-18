"""UCP（Mondo Robotics Beni の UDP トランスポート）のパケット読み書き。

フォーマットは APK 逆解析の結果に基づく。詳細と根拠は `docs/robot-protocol.md` §2 / §3 を参照。
ここでは【確定】/【高確】の値だけを使い、推定に留まる部分はコメントに明記する。

自作の PC クライアントからも、モック機体（`mock_robot.py`）からも使う想定。
標準ライブラリだけで動く。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

# --- ハンドシェイク ---------------------------------------------------------
# クライアント → 機体は ASCII 8B "ucp:sync"、機体 → クライアントは ASCII 7B "ucp:ack"。
SYNC = b"ucp:sync"
ACK = b"ucp:ack"

# --- ヘッダ ----------------------------------------------------------------
UCP_VERSION = 1
UCP_MAGIC = 5
HEADER_BYTE0 = (UCP_MAGIC << 4) | UCP_VERSION  # 0x51
HEADER_LEN = 12
MTU = 1460
MIN_DATAGRAM_LEN = 13  # §2.4「最小データグラム 13B」

# +1 のフラグ（§2.2）
FLAG_BAK = 0x01  # ack 要求
FLAG_FRAGMENT = 0x02  # 断片化継続中（最後の断片ではクリアされる）
FLAG_TYPE_MASK = 0x0C  # パケット種別 0〜3（値の意味は未確定）
FLAG_HB = 0x10  # 末尾に ACK ブロックを同梱するパケット

# --- 制御語（HB のペイロード）----------------------------------------------
HB_LEN = 39  # 12B ヘッダ + 16B 制御語 + 11B ACK ブロック
ACK_BLOCK_LEN = 11  # {0x05, u16, u32, u32}
ACK_BLOCK_MAGIC = 0x05


class UcpError(ValueError):
    """ヘッダとして解釈できないデータ。"""


@dataclass(frozen=True)
class Header:
    """12 バイトの UCP ヘッダ。"""

    flags: int
    una: int  # +2: 自分が次に期待する sn（累積 ack）
    sn: int  # +4
    length: int  # +6: ヘッダ 12B を含む総長
    timestamp_us: int  # +8

    @property
    def is_hb(self) -> bool:
        return bool(self.flags & FLAG_HB)

    @property
    def bak_requested(self) -> bool:
        return bool(self.flags & FLAG_BAK)

    @property
    def fragmented(self) -> bool:
        return bool(self.flags & FLAG_FRAGMENT)

    @property
    def type_code(self) -> int:
        return (self.flags & FLAG_TYPE_MASK) >> 2

    def describe(self) -> str:
        return (
            f"sn={self.sn} una={self.una} len={self.length} flags=0x{self.flags:02x}"
            f"({'HB' if self.is_hb else f'type{self.type_code}'}"
            f"{',BAK' if self.bak_requested else ''}"
            f"{',FRAG' if self.fragmented else ''})"
        )


@dataclass
class Packet:
    """1 つの UCP パケット（ヘッダ + ペイロード）。"""

    offset: int  # データグラム内の開始位置
    header: Header
    payload: bytes

    @property
    def raw(self) -> bytes:
        return pack_header(
            self.header.flags,
            self.header.una,
            self.header.sn,
            self.header.length,
            self.header.timestamp_us,
        ) + self.payload


def pack_header(
    flags: int,
    una: int,
    sn: int,
    length: int,
    timestamp_us: int | None = None,
) -> bytes:
    """12 バイトのヘッダを組み立てる（リトルエンディアン）。

    `timestamp_us` を省略すると現在時刻（エポック µs）を使う。アプリは
    `DateTime._getCurrentMicros`（＝エポック µs）を u32 に切り詰めて入れている。
    """
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)
    return struct.pack(
        "<BBHHHI",
        HEADER_BYTE0,
        flags & 0xFF,
        una & 0xFFFF,
        sn & 0xFFFF,
        length & 0xFFFF,
        timestamp_us & 0xFFFFFFFF,
    )


def parse_header(buf: bytes) -> Header:
    """先頭 12 バイトをヘッダとして読む。壊れていれば `UcpError`。"""
    if len(buf) < HEADER_LEN:
        raise UcpError(f"short header: {len(buf)} < {HEADER_LEN}")
    byte0, flags, una, sn, length, ts = struct.unpack_from("<BBHHHI", buf, 0)
    if byte0 != HEADER_BYTE0:
        version = byte0 & 0x0F
        magic = byte0 >> 4
        raise UcpError(f"bad byte0: 0x{byte0:02x} (version={version}, magic={magic})")
    if length < HEADER_LEN:
        raise UcpError(f"bad length: {length} < {HEADER_LEN}")
    return Header(flags=flags, una=una, sn=sn, length=length, timestamp_us=ts)


def split_packets(buf: bytes) -> tuple[list[Packet], bytes]:
    """1 データグラムに連結された複数の UCP パケットを切り分ける。

    戻り値は `(パケットのリスト, 解釈できなかった残り)`. 残りが出るのは
    断片が途中で切れている場合など。
    """
    packets: list[Packet] = []
    off = 0
    while off + HEADER_LEN <= len(buf):
        try:
            header = parse_header(buf[off:])
        except UcpError:
            break
        end = off + header.length
        if end > len(buf):
            break  # 断片が欠けている
        packets.append(
            Packet(offset=off, header=header, payload=buf[off + HEADER_LEN : end])
        )
        off = end
    return packets, buf[off:]


# --- 末尾 ACK ブロック（HB の末尾 11 バイト）--------------------------------
# レイアウトは `ucp_init_tx` の送信準備（`strb #5, [末尾+0x1d]` のあと u16/u32/u32）から【確定】。
# 中身の意味（una + 64bit SACK ビットマップ）は【推定】。
@dataclass(frozen=True)
class AckBlock:
    una: int
    bitmap_lo: int
    bitmap_hi: int

    @property
    def bitmap64(self) -> int:
        return (self.bitmap_hi << 32) | self.bitmap_lo

    @property
    def bitmap_bits(self) -> list[int]:
        return [i for i in range(64) if self.bitmap64 >> i & 1]

    def describe(self) -> str:
        bits = ",".join(str(b) for b in self.bitmap_bits) or "-"
        return f"una={self.una} bitmap=0x{self.bitmap64:016x} bits[{bits}]"


def parse_ack_block(buf: bytes) -> AckBlock | None:
    """末尾 11 バイトが ACK ブロックなら読む。

    `buf` はパケット全体でもペイロードでもよい（末尾を見るだけ）。判定条件はアプリと
    同じ「`len > 0x17` かつ `buf[len-11] == 0x05`」。
    """
    if len(buf) <= 0x17:
        return None
    tail = buf[-ACK_BLOCK_LEN:]
    if tail[0] != ACK_BLOCK_MAGIC:
        return None
    una = struct.unpack_from("<H", tail, 1)[0]
    bitmap_lo = struct.unpack_from("<I", tail, 3)[0]
    bitmap_hi = struct.unpack_from("<I", tail, 7)[0]
    return AckBlock(una=una, bitmap_lo=bitmap_lo, bitmap_hi=bitmap_hi)


def pack_ack_block(una: int, bitmap: int = 0) -> bytes:
    """11 バイトの ACK ブロックを組み立てる。"""
    return (
        bytes([ACK_BLOCK_MAGIC])
        + struct.pack("<H", una & 0xFFFF)
        + struct.pack("<II", bitmap & 0xFFFFFFFF, (bitmap >> 32) & 0xFFFFFFFF)
    )


def pack_hb(ctrl16: bytes, una: int, sn: int, bitmap: int = 0, flags: int = FLAG_HB) -> bytes:
    """HB（制御語）パケット = 12B ヘッダ + 16B 制御語 + 11B ACK ブロック = 39B。

    アプリが作る `+1` は 0x12（bit1 を立てたまま）で、ワイヤに出る前に bit1 が
    落ちて 0x10 になる（§2.2）。ここでは既定で「ワイヤ上の値」= 0x10 を使う。
    """
    if len(ctrl16) != 16:
        raise UcpError(f"control word must be 16 bytes, got {len(ctrl16)}")
    block = pack_ack_block(una=una, bitmap=bitmap)
    total = HEADER_LEN + 16 + len(block)
    return pack_header(flags, una, sn, total) + ctrl16 + block


# --- 制御語のビット配置（§3.3, アプリの `unpackControl` に名前が残っている）---
CONTROL_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("channel0", 0, 11),
    ("channel1", 11, 11),
    ("channel2", 22, 11),
    ("channel3", 33, 6),
    ("roll", 39, 6),
    ("height", 45, 5),
    ("jump_cmd", 50, 1),
    ("flip_cmd", 51, 3),
    ("max_speed", 54, 2),
    ("nav_flag", 56, 2),
    ("ispause", 58, 1),
    ("islock", 59, 1),
    ("min_turn_mode", 60, 1),
    ("third_person_mode", 61, 1),
    ("jump_mode", 62, 1),
    ("no_liveview", 63, 1),
)

# 11bit（map2000）のアナログ 3ch と 6bit（map62）の 2ch。それ以外は生の小さい整数/フラグ。
CONTROL_ANALOG: dict[str, int] = {
    "channel0": 2000,
    "channel1": 2000,
    "channel2": 2000,
    "channel3": 62,
    "roll": 62,
}

# 軸の意味（§3.3。`device_motion_capability.dart` の setter 名から確定）
CONTROL_MEANING: dict[str, str] = {
    "channel0": "前後",
    "channel1": "旋回",
    "channel2": "雲台yaw",
    "channel3": "雲台pitch",
    "roll": "ロール",
    "height": "車高",
}

CONTROL_MASK: dict[str, int] = {name: (1 << width) - 1 for name, _off, width in CONTROL_FIELDS}


def map2000(v: float) -> int:
    """-1.0..1.0 → 0..2000（中央 1000）。§3.4【確定】"""
    v = max(-1.0, min(1.0, float(v)))
    return max(0, min(2000, int(round(v * 1000 + 1000))))


def map62(v: float) -> int:
    """-1.0..1.0 → 0..62（中央 31）。§3.4【確定】"""
    v = max(-1.0, min(1.0, float(v)))
    return max(0, min(62, int(round(v * 31 + 31))))


def unmap2000(raw: int) -> float:
    """0..2000 → -1.0..1.0（逆変換。厳密な逆ではないので表示用）"""
    return (raw - 1000) / 1000.0


def unmap62(raw: int) -> float:
    return (raw - 31) / 31.0


def encode_control_word(**fields: int) -> bytes:
    """制御語 16 バイト（有効なのは先頭 8 バイト）を組み立てる。

    `channel0=1.0` のように `*_norm` なしの名前で、`channel*`/`roll` は
    -1.0..1.0 の正規化値、それ以外は生の整数（`height` は 0..31 など）で渡す。
    """
    word = 0
    for name, off, width in CONTROL_FIELDS:
        raw = fields.pop(name, 0)
        if name in CONTROL_ANALOG:
            raw = map2000(raw) if width == 11 else map62(raw)
        word |= (int(raw) & CONTROL_MASK[name]) << off
    if fields:
        raise UcpError(f"unknown control fields: {sorted(fields)}")
    return word.to_bytes(8, "little") + b"\x00" * 8


def decode_control_word(data: bytes) -> dict[str, int]:
    """制御語（8 or 16 バイト。先頭 8 バイトを使う）を名前付きの生値に戻す。"""
    if len(data) < 8:
        raise UcpError(f"control word too short: {len(data)}")
    word = int.from_bytes(data[:8], "little")
    out: dict[str, int] = {}
    for name, off, width in CONTROL_FIELDS:
        out[name] = (word >> off) & CONTROL_MASK[name]
    return out


def format_control_word(raw: dict[str, int]) -> str:
    """制御語を 1〜2 行の人間向け文字列にする（モックのログ用）。"""
    analog = []
    for name in ("channel0", "channel1", "channel2", "channel3", "roll"):
        v = raw[name]
        norm = unmap2000(v) if name in ("channel0", "channel1", "channel2") else unmap62(v)
        analog.append(f"{name}={v}({norm:+.3f} {CONTROL_MEANING[name]})")
    analog.append(f"height={raw['height']}({CONTROL_MEANING['height']})")
    flags = " ".join(
        f"{name}={raw[name]}"
        for name in (
            "jump_cmd",
            "flip_cmd",
            "max_speed",
            "nav_flag",
            "ispause",
            "islock",
            "min_turn_mode",
            "third_person_mode",
            "jump_mode",
            "no_liveview",
        )
    )
    return " ".join(analog) + "\n    " + flags


# --- Zenoh リンクフレーム（§4）---------------------------------------------
# 0xA4/0xA5/0xA6 = 5B ヘッダ（magic + u32 BE シーケンス）、0xA7 = 3B ヘッダ（magic + u16 BE）
ZENOH_MAGICS: dict[int, tuple[str, int]] = {
    0xA4: ("5B", 4),
    0xA5: ("5B", 4),
    0xA6: ("5B", 4),
    0xA7: ("3B", 2),
}


def zenoh_frame_info(payload: bytes) -> tuple[int, int | None, bytes] | None:
    """ペイロードが Zenoh リンクフレームなら `(magic, seq, body)` を返す。"""
    if not payload or payload[0] not in ZENOH_MAGICS:
        return None
    _form, seq_len = ZENOH_MAGICS[payload[0]]
    if len(payload) < 1 + seq_len:
        return payload[0], None, b""
    seq = int.from_bytes(payload[1 : 1 + seq_len], "big")
    return payload[0], seq, payload[1 + seq_len :]


def extract_strings(buf: bytes, min_len: int = 4) -> list[str]:
    """バイナリから印字可能な ASCII の並びを拾う（keyexpr 探索用）。"""
    out: list[str] = []
    cur = bytearray()
    for b in buf:
        if 0x20 <= b < 0x7F:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode("ascii"))
            cur.clear()
    if len(cur) >= min_len:
        out.append(cur.decode("ascii"))
    return out

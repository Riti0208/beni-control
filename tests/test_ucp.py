"""UCP コーデックとモック機体の単体テスト。

値の根拠は `docs/robot-protocol.md` §2 / §3（APK 逆解析の結果）。
"""

import pytest

from beni_control import ucp
from beni_control.mock_robot import MockRobot


# --------------------------------------------------------------------------
# ヘッダ
# --------------------------------------------------------------------------
def test_header_round_trip():
    raw = ucp.pack_header(flags=0x10, una=7, sn=6, length=39, timestamp_us=123456)
    assert raw[0] == 0x51
    h = ucp.parse_header(raw)
    assert (h.flags, h.una, h.sn, h.length, h.timestamp_us) == (0x10, 7, 6, 39, 123456)
    assert h.is_hb and not h.bak_requested and not h.fragmented


def test_parse_header_rejects_garbage():
    with pytest.raises(ucp.UcpError):
        ucp.parse_header(b"\x00" * 12)
    with pytest.raises(ucp.UcpError):
        ucp.parse_header(b"\x51" * 11)  # 短い


def test_split_packets_single_hb():
    hb = ucp.pack_hb(ucp.encode_control_word(channel0=1.0), una=0, sn=5)
    assert len(hb) == ucp.HB_LEN == 39
    packets, trailing = ucp.split_packets(hb)
    assert trailing == b""
    assert len(packets) == 1
    h = packets[0].header
    assert (h.sn, h.length, h.flags) == (5, 39, 0x10)
    assert len(packets[0].payload) == 16 + ucp.ACK_BLOCK_LEN == 27


def test_split_packets_multiple_concatenated():
    a = ucp.pack_hb(ucp.encode_control_word(), una=0, sn=1)
    b = ucp.pack_header(0x00, una=1, sn=0, length=13) + b"\x00"
    packets, trailing = ucp.split_packets(a + b)
    assert trailing == b""
    assert [p.header.sn for p in packets] == [1, 0]
    assert [p.header.length for p in packets] == [39, 13]


def test_split_packets_leaves_trailing_fragment():
    hb = ucp.pack_hb(ucp.encode_control_word(), una=0, sn=1)
    packets, trailing = ucp.split_packets(hb[:20])
    assert packets == []
    assert trailing == hb[:20]


# --------------------------------------------------------------------------
# 末尾 ACK ブロック（11B）
# --------------------------------------------------------------------------
def test_ack_block_round_trip():
    hb = ucp.pack_hb(ucp.encode_control_word(), una=3, sn=9, bitmap=0b1010)
    ack = ucp.parse_ack_block(hb)
    assert ack is not None
    assert ack.una == 3
    assert ack.bitmap64 == 0b1010
    assert ack.bitmap_bits == [1, 3]
    # パケット全体でもペイロードでも同じ結果（末尾を見ているだけ）
    assert ucp.parse_ack_block(hb[12:]) == ack


def test_ack_block_absent_for_small_payload():
    assert ucp.parse_ack_block(b"\x00" * 16) is None
    assert ucp.parse_ack_block(b"\x00" * 24) is None  # 0x05 が無い


def test_ack_block_is_the_last_11_bytes_before_nothing():
    """HB の構造: 12B ヘッダ + 16B 制御語 + 11B ACK ブロック（12B ヘッダ付きの入れ子は無い）。"""
    hb = ucp.pack_hb(ucp.encode_control_word(), una=0, sn=0)
    assert len(hb) == 39
    assert hb[0] == 0x51                      # UCP ヘッダ
    assert hb[12:28] == ucp.encode_control_word()  # 制御語 16B
    assert hb[28] == ucp.ACK_BLOCK_MAGIC == 0x05   # ACK ブロックの magic
    assert len(hb[28:]) == ucp.ACK_BLOCK_LEN == 11
    block = ucp.pack_ack_block(7, 0x1)
    assert len(block) == 11 and block[0] == 0x05
    assert ucp.parse_ack_block(b"\x00" * 23 + block) == ucp.AckBlock(7, 1, 0)


# --------------------------------------------------------------------------
# 制御語
# --------------------------------------------------------------------------
def test_control_field_layout_is_contiguous_and_64_bits():
    off = 0
    for name, start, width in ucp.CONTROL_FIELDS:
        assert start == off, name
        off += width
    assert off == 64


def test_map2000_and_map62():
    assert ucp.map2000(0.0) == 1000
    assert ucp.map2000(1.0) == 2000
    assert ucp.map2000(-1.0) == 0
    assert ucp.map2000(2.0) == 2000   # クランプ
    assert ucp.map2000(-2.0) == 0
    assert ucp.map62(0.0) == 31
    assert ucp.map62(1.0) == 62
    assert ucp.map62(-1.0) == 0


def test_encode_control_word_defaults_to_neutral():
    raw = ucp.decode_control_word(ucp.encode_control_word())
    assert raw["channel0"] == raw["channel1"] == raw["channel2"] == 1000
    assert raw["channel3"] == raw["roll"] == 31
    assert raw["height"] == 0
    assert all(raw[n] == 0 for n in ("jump_cmd", "flip_cmd", "max_speed", "nav_flag",
                                     "ispause", "islock", "min_turn_mode",
                                     "third_person_mode", "jump_mode", "no_liveview"))


def test_encode_control_word_round_trip():
    word = ucp.encode_control_word(
        channel0=0.5, channel1=-0.25, channel2=1.0, channel3=-1.0, roll=0.5,
        height=31, jump_cmd=1, flip_cmd=4, max_speed=2, nav_flag=1,
        ispause=1, islock=1, min_turn_mode=1, third_person_mode=1,
        jump_mode=1, no_liveview=1,
    )
    raw = ucp.decode_control_word(word)
    assert raw["channel0"] == ucp.map2000(0.5)
    assert raw["channel1"] == ucp.map2000(-0.25)
    assert raw["channel2"] == 2000
    assert raw["channel3"] == 0
    assert raw["roll"] == ucp.map62(0.5)
    assert raw["height"] == 31
    assert raw["flip_cmd"] == 4 and raw["max_speed"] == 2 and raw["no_liveview"] == 1


def test_encode_control_word_rejects_unknown_field():
    with pytest.raises(ucp.UcpError):
        ucp.encode_control_word(channel9=1.0)


def test_decode_control_word_accepts_16_bytes():
    word = ucp.encode_control_word(channel0=1.0)
    assert len(word) == 16
    assert ucp.decode_control_word(word) == ucp.decode_control_word(word[:8])


def test_known_bit_values_from_reverse_engineering():
    """ビット位置の固定（§3.3 の表そのもの）。"""
    word = (
        (2000 << 0) | (2000 << 11) | (2000 << 22) | (62 << 33) | (62 << 39)
        | (31 << 45) | (1 << 50) | (4 << 51) | (2 << 54) | (2 << 56)
        | (1 << 58) | (1 << 59) | (1 << 60) | (1 << 61) | (1 << 62) | (1 << 63)
    ).to_bytes(8, "little")
    raw = ucp.decode_control_word(word)
    assert raw["channel0"] == 2000 and raw["channel1"] == 2000 and raw["channel2"] == 2000
    assert raw["channel3"] == 62 and raw["roll"] == 62
    assert raw["height"] == 31
    assert raw["jump_cmd"] == 1 and raw["flip_cmd"] == 4
    assert raw["max_speed"] == 2 and raw["nav_flag"] == 2
    assert raw["ispause"] == 1 and raw["no_liveview"] == 1


def test_format_control_word_has_axis_names():
    text = ucp.format_control_word(ucp.decode_control_word(ucp.encode_control_word(channel0=1.0)))
    assert "前後" in text and "旋回" in text and "雲台yaw" in text
    assert "+1.000" in text


# --------------------------------------------------------------------------
# Zenoh
# --------------------------------------------------------------------------
def test_zenoh_frame_info():
    assert ucp.zenoh_frame_info(b"\xa4\x00\x00\x00\x2ahello") == (0xA4, 42, b"hello")
    assert ucp.zenoh_frame_info(b"\xa7\x00\x2ahello") == (0xA7, 42, b"hello")
    assert ucp.zenoh_frame_info(b"\x01\x02") is None
    assert ucp.zenoh_frame_info(b"\xa4") == (0xA4, None, b"")  # シーケンスが欠けている


def test_extract_strings():
    assert ucp.extract_strings(b"\x01\x02device/cmd/common/ping_req\x00ab") == [
        "device/cmd/common/ping_req"
    ]


# --------------------------------------------------------------------------
# モック機体
# --------------------------------------------------------------------------
def test_mock_handshake_replies_ack_and_bootstrap():
    robot = MockRobot(reply_mode="ack", bootstrap_packets=1)
    replies = robot.handle_datagram(ucp.SYNC, ("192.168.7.2", 50000))
    assert replies[0] == ucp.ACK
    assert len(replies) == 2
    h = ucp.parse_header(replies[1])
    assert (h.sn, h.una, h.length) == (0, 0, ucp.MIN_DATAGRAM_LEN)
    assert robot.evidence.sync_count == 1


def test_mock_records_control_word_and_ack_block():
    robot = MockRobot(reply_mode="ack")
    robot.handle_datagram(ucp.SYNC, ("1.2.3.4", 1234))
    hb = ucp.pack_hb(ucp.encode_control_word(channel0=1.0), una=0, sn=7, bitmap=0b100)
    robot.handle_datagram(hb, ("1.2.3.4", 1234))
    ev = robot.evidence
    assert ev.type_hb == 1
    assert ev.flag_hist[0x10] == 1
    assert ev.ctrl_first is not None and ev.ctrl_first["channel0"] == 2000
    assert ev.ctrl_changes == 0
    assert ev.ack_samples == [(0, 0b100)]
    # sn=7 は期待値 0 の飛び越しなので、累積 ack は 0 のまま SACK ビットマップで報告する
    reply = robot.handle_datagram(hb, ("1.2.3.4", 1234))[0]
    assert ucp.parse_header(reply).una == 0
    assert robot.sack_bitmap(0) & (1 << 7)


def test_mock_una_advances_in_order():
    robot = MockRobot(reply_mode="ack", bootstrap_packets=0)
    robot.handle_datagram(ucp.SYNC, ("1.2.3.4", 1))
    for sn in (0, 1):
        reply = robot.handle_datagram(
            ucp.pack_hb(ucp.encode_control_word(), una=0, sn=sn), ("1.2.3.4", 1)
        )[0]
        assert ucp.parse_header(reply).una == sn + 1
    assert robot.rx_next == 2


def test_mock_reply_mode_none_sends_nothing():
    robot = MockRobot(reply_mode="none")
    assert robot.handle_datagram(ucp.SYNC, ("1.2.3.4", 1)) == []
    assert robot.handle_datagram(ucp.pack_hb(ucp.encode_control_word(), 0, 0), ("1.2.3.4", 1)) == []
    assert robot.evidence.type_hb == 1


def test_mock_gap_sn_is_skipped():
    robot = MockRobot(reply_mode="ack", gap_sns=(1,), bootstrap_packets=0)
    robot.handle_datagram(ucp.SYNC, ("1.2.3.4", 1))
    probe = ucp.pack_header(0x00, 0, 0x10, 13) + b"\x00"
    first = robot.handle_datagram(probe, ("1.2.3.4", 1))
    second = robot.handle_datagram(probe, ("1.2.3.4", 1))
    assert len(first) == 1 and ucp.parse_header(first[0]).sn == 0
    assert second == []  # sn=1 は意図的に落とした
    assert robot.evidence.skipped_sns == [1]


def test_mock_non_ucp_datagram_is_counted():
    robot = MockRobot(reply_mode="ack")
    assert robot.handle_datagram(b"junkjunk", ("1.2.3.4", 1)) == []
    assert robot.evidence.non_ucp_datagrams == 1


def test_sack_candidates_prefers_relative_when_una_advances():
    """una が進む状況では `relative` だけが合致する（#8 の自動判定の中身）。"""
    from beni_control.mock_robot import Evidence

    ev = Evidence()
    # una=5 で、未 cum-ack は [5, 7] → relative なら bit0 と bit2
    ev.ack_samples = [(5, 0b101)]
    ev.ack_pending = [[5, 7]]
    scores = {name: (score, total) for name, score, total in ev.sack_candidates()}
    assert scores["relative"] == (1, 1)
    assert scores["absolute"] == (0, 1)
    assert scores["absolute+1"] == (0, 1)


def test_mock_report_marks_flag_values():
    robot = MockRobot(reply_mode="ack")
    robot.handle_datagram(ucp.SYNC, ("1.2.3.4", 1))
    robot.handle_datagram(ucp.pack_hb(ucp.encode_control_word(), una=0, sn=1), ("1.2.3.4", 1))
    report = robot.evidence.report()
    assert "#7" in report and "#8" in report and "#9" in report and "#10" in report
    assert "0x10" in report
    assert "前後" in report


def test_end_to_end_over_udp():
    """モックと実ソケットで 1 往復（ハンドシェイク → HB → 返信）。"""
    import socket
    import threading

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    robot = MockRobot(reply_mode="ack")
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            for reply in robot.handle_datagram(data, addr):
                sock.sendto(reply, addr)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    try:
        client.sendto(ucp.SYNC, ("127.0.0.1", port))
        replies = [client.recvfrom(4096)[0] for _ in range(2)]
        assert replies[0] == ucp.ACK
        assert ucp.parse_header(replies[1]).length == ucp.MIN_DATAGRAM_LEN
        client.sendto(
            ucp.pack_hb(ucp.encode_control_word(channel0=1.0), una=0, sn=0),
            ("127.0.0.1", port),
        )
        reply = client.recvfrom(4096)[0]
        assert ucp.parse_header(reply).una == 1
    finally:
        client.close()
        stop.set()
        thread.join(timeout=2)
        sock.close()
    assert robot.evidence.type_hb == 1
    assert robot.evidence.ctrl_first is not None
    assert robot.evidence.ctrl_first["channel0"] == 2000

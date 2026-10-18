"""モック機体（実機なしで UCP を観察するための道具）。

`docs/robot-protocol.md` §8 の未確定項目のうち、**機体が無くても埋まるもの**を
観察するために、PC 側で「機体のふり」をする UDP サーバ。

できること:

1. `ucp:sync` に `ucp:ack` を返してセッションを成立させる（アプリは接続したと認識する）
2. アプリから来る全データグラムを UCP として解釈し、制御語（HB）はビット名付きで表示
3. アプリの ACK ブロック（una + ビットマップ）を記録し、こちらが送った sn と突き合わせる
4. Zenoh リンクフレームを検出し、マジック・シーケンス・中から拾える文字列を表示
5. 終了時に「#7〜#10 の答え合わせ」レポートを出す

実機が要る部分（未認証セッションで走るか、実際に前進するか等）は埋まらない。
Zenoh のセッションは機体側の応答が無いと確立しないので、上位層の put
（keyexpr の完全形）は取れない可能性が高い。その旨はレポートにも出す。

使い方::

    uv run beni-mock-robot --port 9007            # 別端末でアプリを接続させる
    uv run beni-mock-robot --reply-mode hb --gap-at 20,40 --keepalive 0.02

自作クライアントを書くときの参考実装でもある（`ucp.py` を使っている）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import select
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

try:  # `python src/beni_control/mock_robot.py` でも動くように
    from . import ucp
except ImportError:  # pragma: no cover
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from beni_control import ucp  # type: ignore[no-redef]

# 既定の送信間隔など
DEFAULT_KEEPALIVE = 1.0
SUMMARY_EVERY = 5.0


def _ts() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------
# 観察結果の集計
# --------------------------------------------------------------------------
@dataclass
class Evidence:
    """#7〜#10 を埋めるための観測結果。"""

    sync_count: int = 0
    datagrams: int = 0
    packets: int = 0
    trailing_bytes: int = 0
    other_clients: int = 0

    flag_hist: Counter[int] = field(default_factory=Counter)  # +1 の値（#7）
    length_hist: Counter[int] = field(default_factory=Counter)
    type_hb: int = 0

    # ACK ブロック（#8）: (アプリの una, 64bit ビットマップ)
    ack_samples: list[tuple[int, int]] = field(default_factory=list)
    # サンプル時刻に「こちらが送った sn のうち una 以降のもの」
    ack_pending: list[list[int]] = field(default_factory=list)

    zenoh_magics: Counter[int] = field(default_factory=Counter)  # #9
    zenoh_seqs: list[tuple[int, int]] = field(default_factory=list)
    zenoh_strings: Counter[str] = field(default_factory=Counter)  # #10

    unknown_first_bytes: Counter[int] = field(default_factory=Counter)
    non_ucp_datagrams: int = 0

    # 制御語
    ctrl_samples: int = 0
    ctrl_changes: int = 0
    ctrl_first: dict[str, int] | None = None
    ctrl_last: dict[str, int] | None = None
    ctrl_moved: dict[str, int] | None = None  # 最初と違った最後のサンプル

    # こちらから送ったもの
    sent_sns: list[int] = field(default_factory=list)
    skipped_sns: list[int] = field(default_factory=list)

    def note_control(self, raw: dict[str, int]) -> bool:
        """制御語を記録し、変化があったかを返す。"""
        self.ctrl_samples += 1
        if self.ctrl_first is None:
            self.ctrl_first = dict(raw)
            self.ctrl_last = dict(raw)
            return True
        if raw != self.ctrl_last:
            self.ctrl_changes += 1
            self.ctrl_last = dict(raw)
            self.ctrl_moved = dict(raw)
            return True
        return False

    # -- #8 の当てはめ ---------------------------------------------------
    def sack_candidates(self) -> list[tuple[str, int, int]]:
        """ビットマップの読み方を候補ごとに採点する（合致数, サンプル数）。

        * `absolute`: bit i = sn i が届いた
        * `relative`: bit i = sn (una + i) が届いた
        * `absolute+1`: bit i = sn (i + 1) が届いた
        """
        scores = {"absolute": 0, "relative": 0, "absolute+1": 0}
        total = 0
        for (una, bitmap), pending in zip(self.ack_samples, self.ack_pending):
            if not pending:
                continue
            total += 1
            rel = 0
            abs_ = 0
            abs1 = 0
            for sn in pending:
                if 0 <= sn < 64:
                    abs_ |= 1 << sn
                if 1 <= sn < 64:
                    abs1 |= 1 << (sn - 1)
                d = sn - una
                if 0 <= d < 64:
                    rel |= 1 << d
            scores["absolute"] += int(abs_ == bitmap)
            scores["relative"] += int(rel == bitmap)
            scores["absolute+1"] += int(abs1 == bitmap)
        return [(name, score, total) for name, score in scores.items()]

    def report(self, *, hb_only: bool = False) -> str:
        out: list[str] = []
        w = out.append
        w("=" * 72)
        w("モック機体 観察レポート（docs/robot-protocol.md §8 の答え合わせ）")
        w("=" * 72)
        w(f"受信データグラム: {self.datagrams}  うち UCP として解釈: {self.packets}"
          f"  非 UCP: {self.non_ucp_datagrams}  余りバイト: {self.trailing_bytes}")
        w(f"ハンドシェイク (ucp:sync): {self.sync_count} 回")
        if self.other_clients:
            w(f"※ 別の送信元からのパケットを {self.other_clients} 件無視した")

        # #7
        w("")
        w("--- #7  ワイヤ上の +1 フラグ（生成時の 0x12 が実際どうなっているか）")
        if self.flag_hist:
            for f, n in self.flag_hist.most_common():
                hb = "HB(bit4)" if f & ucp.FLAG_HB else "-"
                frag = "FRAG(bit1)" if f & ucp.FLAG_FRAGMENT else "-"
                bak = "BAK(bit0)" if f & ucp.FLAG_BAK else "-"
                w(f"    0x{f:02x}  {n:6d} 個  {hb} {frag} {bak}")
            w("    → 上の値が答え。0x10 が出ていれば『bit1 はワイヤで落ちる』が確定。")
        else:
            w("    （HB を観測していない）")
        if self.length_hist:
            top = ", ".join(f"{ln}B×{n}" for ln, n in self.length_hist.most_common(6))
            w(f"    パケット長の内訳: {top}")

        # #8
        w("")
        w("--- #8  ACK ブロック末尾の u16/u32 の意味")
        if self.ack_samples:
            w(f"    サンプル数: {len(self.ack_samples)}")
            for i, ((una, bitmap), pending) in enumerate(
                list(zip(self.ack_samples, self.ack_pending))[:5]
            ):
                bits = [b for b in range(64) if bitmap >> b & 1]
                w(f"    [{i}] una={una} bitmap=0x{bitmap:016x} bits={bits}")
                w(f"         その時点で未 cum-ack だった送信 sn: {pending[:12]}"
                  f"{' …' if len(pending) > 12 else ''}")
            w("    読み方の当てはめ（合致サンプル数 / 判定できたサンプル数）:")
            scores = self.sack_candidates()
            for name, score, total in scores:
                mark = "★" if score == total and total else " "
                w(f"      {mark} {name:12s} {score}/{total}")
            winners = [name for name, score, total in scores if total and score == total]
            if len(winners) > 1:
                w("    ※ ★ が複数: una が進んでいないと `bit i = sn(una+i)` と "
                  "`bit i = sn(i+1)` が同じになる。una が進む状況か "
                  "`--gap-at` で sn を飛ばすと区別できる。")
            elif winners:
                w(f"    → 最有力: {winners[0]}")
            else:
                w("    → 情報不足（`--gap-at` で sn を意図的に飛ばすと判定しやすい）")
        else:
            w("    （ACK ブロックを観測していない）")
        if self.sent_sns:
            w(f"    こちらが送った sn: {self.sent_sns[:8]} … 計 {len(self.sent_sns)} 個"
              f"（意図的な抜け: {self.skipped_sns or 'なし'}）")

        # #9 / #10
        w("")
        w("--- #9  Zenoh リンクフレームの形")
        if self.zenoh_magics:
            for magic, n in self.zenoh_magics.most_common():
                form = ucp.ZENOH_MAGICS.get(magic, ("?", 0))[0]
                w(f"    0x{magic:02x} ({form} ヘッダ) {n} 個")
            if self.zenoh_seqs:
                head = ", ".join(f"0x{m:02x}:{s}" for m, s in self.zenoh_seqs[:10])
                w(f"    最初のマジック/シーケンス: {head}")
        else:
            w("    （Zenoh フレームを観測していない）")
            w("    ※ アプリは UCP のハンドシェイク後に Zenoh を張りにいく。"
              "ここに出ないなら UCP 層で止まっている")

        w("")
        w("--- #10  keyexpr の手掛かり（ペイロード中の文字列）")
        if self.zenoh_strings:
            for s, n in self.zenoh_strings.most_common(20):
                w(f"    {n:4d}×  {s!r}")
            w("    → `/cmd/...` `/status/...` `/device/...` が出ていれば keyexpr の完全形が確認できる")
        else:
            w("    （文字列は拾えず。Zenoh の宣言メッセージは機体の応答が要る）")

        # 制御語
        w("")
        w("--- 制御語（HB のペイロード）")
        if self.ctrl_first is not None:
            w(f"    サンプル {self.ctrl_samples} 個 / 変化 {self.ctrl_changes} 回")
            w("    最初のサンプル:")
            w("    " + ucp.format_control_word(self.ctrl_first).replace("\n", "\n    "))
            if self.ctrl_moved is not None:
                w("    変化した最後のサンプル:")
                w("    " + ucp.format_control_word(self.ctrl_moved).replace("\n", "\n    "))
            else:
                w("    （サンプル間で変化なし。中立のままか、入力が一定だっただけの可能性がある）")
        else:
            w("    （制御語を観測していない）")
        w("")
        w("=" * 72)
        return "\n".join(out)


# --------------------------------------------------------------------------
# モック機体
# --------------------------------------------------------------------------
class MockRobot:
    """UCP のハンドシェイクに応答しつつ、来たものを全部記録する。"""

    def __init__(
        self,
        *,
        reply_mode: str = "ack",
        reply_flags: int = 0x00,
        reply_len: int = ucp.MIN_DATAGRAM_LEN,
        gap_sns: tuple[int, ...] = (),
        bootstrap_packets: int = 1,
        keepalive: float = DEFAULT_KEEPALIVE,
        hex_limit: int = 96,
        verbose: bool = False,
    ) -> None:
        self.reply_mode = reply_mode  # none / ack / hb
        self.reply_flags = reply_flags
        self.reply_len = max(ucp.HEADER_LEN, reply_len)
        self.skip_sns = set(gap_sns)
        self.bootstrap_packets = bootstrap_packets
        self.keepalive = keepalive
        self.hex_limit = hex_limit
        self.verbose = verbose

        self.evidence = Evidence()
        self.client: tuple[str, int] | None = None
        self.tx_sn = 0
        self.rx_next = 0  # 自分が次に期待するアプリ側の sn（= 送る +2 の値）
        self.rx_seen: set[int] = set()  # 順不同で先に届いたアプリ側 sn（SACK ビットマップの元）
        self.handshaken = False

    # -- ログ -------------------------------------------------------------
    def log(self, text: str = "") -> None:
        print(text, flush=True)

    def _hex(self, buf: bytes) -> str:
        if len(buf) <= self.hex_limit:
            return buf.hex()
        return buf[: self.hex_limit].hex() + f"…(+{len(buf) - self.hex_limit}B)"

    # -- 受信 -------------------------------------------------------------
    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> list[bytes]:
        """1 データグラムを処理し、返信すべきデータグラムを返す。"""
        ev = self.evidence
        ev.datagrams += 1

        # 1) ハンドシェイク
        if data == ucp.SYNC:
            ev.sync_count += 1
            if self.client != addr:
                self.log(f"{_ts()} <- {addr[0]}:{addr[1]}  ucp:sync  (接続確立)")
            self.client = addr
            self.handshaken = True
            self.tx_sn = 0
            if self.reply_mode == "none":
                return []
            replies = [ucp.ACK]
            for _ in range(self.bootstrap_packets):
                pkt = self._next_reply(una=0)
                if pkt:
                    replies.append(pkt)
            return replies

        # 2) 送信元の切り替わり注意
        if self.client is None:
            self.client = addr
        elif self.client != addr:
            ev.other_clients += 1
            if ev.other_clients == 1:
                self.log(f"{_ts()} ※ {addr[0]}:{addr[1]} からも受信（{self.client} 以外）。"
                         "以降も処理するが sn 管理は 1 クライアント前提")
            self.client = addr

        # 3) UCP として解釈
        packets, trailing = ucp.split_packets(data)
        if not packets:
            ev.non_ucp_datagrams += 1
            ev.unknown_first_bytes[data[0] if data else -1] += 1
            self.log(f"{_ts()} <- {addr[0]}:{addr[1]}  {len(data)}B  "
                     f"[UCP でない/切れている] {self._hex(data)}")
            return []
        if trailing:
            ev.trailing_bytes += len(trailing)

        replies: list[bytes] = []
        for pkt in packets:
            reply = self._handle_packet(pkt, addr)
            if reply:
                replies.append(reply)
        return replies

    def _handle_packet(self, pkt: ucp.Packet, addr: tuple[str, int]) -> bytes | None:
        ev = self.evidence
        h = pkt.header
        ev.packets += 1
        ev.flag_hist[h.flags] += 1
        ev.length_hist[h.length] += 1
        if h.is_hb:
            ev.type_hb += 1

        # アプリの制御語（HB）
        raw = None
        if h.is_hb and len(pkt.payload) >= 8:
            try:
                raw = ucp.decode_control_word(pkt.payload[:8])
            except ucp.UcpError:
                raw = None
        ack = ucp.parse_ack_block(pkt.raw)
        zenoh = ucp.zenoh_frame_info(pkt.payload)

        # ログ: 制御語は変化したときだけ本文を出す（20ms で来るので）
        if raw is not None:
            changed = ev.note_control(raw)
            if changed:
                self.log(f"{_ts()} <- {addr[0]}:{addr[1]}  HB {h.describe()}")
                self.log("    " + ucp.format_control_word(raw).replace("\n", "\n    "))
        elif zenoh is not None:
            magic, seq, body = zenoh
            ev.zenoh_magics[magic] += 1
            if seq is not None:
                ev.zenoh_seqs.append((magic, seq))
            for s in ucp.extract_strings(body):
                ev.zenoh_strings[s] += 1
            self.log(f"{_ts()} <- {addr[0]}:{addr[1]}  zenoh magic=0x{magic:02x} "
                     f"seq={seq} {h.describe()}")
            if self.verbose or ev.zenoh_magics[magic] <= 2:
                self.log(f"    body: {self._hex(body)}")
            strings = ucp.extract_strings(body)
            if strings:
                self.log(f"    strings: {strings[:8]}")
        else:
            if self.verbose or h.length > ucp.HEADER_LEN:
                self.log(f"{_ts()} <- {addr[0]}:{addr[1]}  {h.describe()} "
                         f"payload={self._hex(pkt.payload)}")

        if ack is not None:
            ev.ack_samples.append((ack.una, ack.bitmap64))
            pending = [sn for sn in ev.sent_sns if sn >= ack.una]
            ev.ack_pending.append(pending)
            if self.verbose:
                self.log(f"    ack block: {ack.describe()}")

        # 返信
        self.note_received(h.sn)
        if self.reply_mode == "none":
            return None
        return self._next_reply(una=self.rx_next)

    def note_received(self, sn: int) -> None:
        """アプリ側の sn を受け取り、累積 ack を進める（順不同はビットマップで持つ）。"""
        self.rx_seen.add(sn)
        guard = 0
        while self.rx_next in self.rx_seen and guard < 0x10000:
            self.rx_seen.discard(self.rx_next)
            self.rx_next = (self.rx_next + 1) & 0xFFFF
            guard += 1

    def sack_bitmap(self, una: int) -> int:
        """`una` 以降で受信済みのアプリ側 sn をビットマップにする（自分から見た SACK）。"""
        bitmap = 0
        for sn in self.rx_seen:
            d = (sn - una) & 0xFFFF
            if d < 64:
                bitmap |= 1 << d
        return bitmap

    # -- 送信 -------------------------------------------------------------
    def _next_reply(self, *, una: int) -> bytes | None:
        """次の自パケットを作る。`skip_sns` に入っている sn は『送らない』。"""
        ev = self.evidence
        sn = self.tx_sn
        self.tx_sn = (self.tx_sn + 1) & 0xFFFF
        if sn in self.skip_sns:
            ev.skipped_sns.append(sn)
            return None
        ev.sent_sns.append(sn)
        if self.reply_mode == "hb":
            # 機体側の HB（制御語は中立）。ACK ブロックは「アプリ側 sn の受信状況」
            return ucp.pack_hb(
                ucp.encode_control_word(),
                una=una,
                sn=sn,
                bitmap=self.sack_bitmap(una),
                flags=ucp.FLAG_HB,
            )
        total = self.reply_len
        return ucp.pack_header(self.reply_flags, una, sn, total) + b"\x00" * (
            total - ucp.HEADER_LEN
        )

    def keepalive_packet(self) -> bytes | None:
        if not self.handshaken or self.reply_mode == "none":
            return None
        return self._next_reply(una=self.rx_next)


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------
def _parse_gap(text: str) -> tuple[int, ...]:
    if not text:
        return ()
    out = []
    for part in text.split(","):
        part = part.strip()
        if part:
            out.append(int(part, 0))
    return tuple(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="beni-mock-robot",
        description="Beni のモック機体: UCP ハンドシェイクに応答し、来たパケットを全部記録する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  beni-mock-robot --port 9007\n"
            "  beni-mock-robot --reply-mode hb --keepalive 0.02 --gap-at 20,40 -v\n"
            "  beni-mock-robot --duration 60 > cap.txt\n"
        ),
    )
    p.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス (既定 0.0.0.0)")
    p.add_argument("--port", type=int, default=9007, help="待ち受けポート (既定 9007)")
    p.add_argument(
        "--reply-mode",
        choices=("none", "ack", "hb"),
        default="ack",
        help="返信の種類: none=受け取るだけ / ack=ヘッダのみ / hb=機体側 HB (既定 ack)",
    )
    p.add_argument("--reply-flags", type=lambda s: int(s, 0), default=0x00,
                   help="ack モードの +1 フラグ (既定 0x00)")
    p.add_argument("--reply-len", type=int, default=ucp.MIN_DATAGRAM_LEN,
                   help=f"ack モードのデータグラム長 (既定 {ucp.MIN_DATAGRAM_LEN})")
    p.add_argument("--gap-at", type=_parse_gap, default=(),
                   help="この sn を意図的に送らない (カンマ区切り)。ACK ブロックのビット対応の切り分け用")
    p.add_argument("--bootstrap-packets", type=int, default=1,
                   help="ucp:sync 直後に送る UCP パケット数 (既定 1)")
    p.add_argument("--keepalive", type=float, default=DEFAULT_KEEPALIVE,
                   help=f"無通信時に送る間隔 [秒]、0 で無効 (既定 {DEFAULT_KEEPALIVE})")
    p.add_argument("--summary-every", type=float, default=SUMMARY_EVERY,
                   help=f"経過サマリの間隔 [秒] (既定 {SUMMARY_EVERY})")
    p.add_argument("--duration", type=float, default=0.0,
                   help="この秒数で自動終了 (既定 0 = Ctrl-C まで)")
    p.add_argument("--hex-limit", type=int, default=96, help="1 パケットの hex 表示上限バイト数")
    p.add_argument("-v", "--verbose", action="store_true", help="全パケット・ACK ブロックも表示")
    p.add_argument("--report-out", default="", help="レポートをこのファイルにも書く")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    robot = MockRobot(
        reply_mode=args.reply_mode,
        reply_flags=args.reply_flags,
        reply_len=args.reply_len,
        gap_sns=args.gap_at,
        bootstrap_packets=args.bootstrap_packets,
        keepalive=args.keepalive,
        hex_limit=args.hex_limit,
        verbose=args.verbose,
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.setblocking(False)

    robot.log(f"モック機体: udp {args.host}:{args.port} で待ち受け中")
    robot.log(f"  返信モード={args.reply_mode}  keepalive={args.keepalive}s  "
              f"sn の抜け={list(args.gap_at) or 'なし'}")
    if args.port != 9007:
        robot.log("  ※ 実機の既定ポートは 9007。アプリに合わせるなら --port 9007")
    robot.log("  アプリの接続先をこの PC (192.168.7.1) にすること。docs/mock-robot.md 参照")
    robot.log("  Ctrl-C で終了（終了時に観察レポートを出す）")

    started = time.monotonic()
    next_summary = started + args.summary_every
    next_keepalive = started + args.keepalive if args.keepalive else None
    interrupted = False

    try:
        while True:
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                break
            timeout = 0.1
            if next_summary:
                timeout = min(timeout, max(0.0, next_summary - now))
            r, _, _ = select.select([sock], [], [], timeout)
            if r:
                data, addr = sock.recvfrom(65535)
                for reply in robot.handle_datagram(data, addr):
                    sock.sendto(reply, addr)
            now = time.monotonic()
            if next_keepalive is not None and now >= next_keepalive and robot.client:
                pkt = robot.keepalive_packet()
                if pkt:
                    sock.sendto(pkt, robot.client)
                next_keepalive = now + args.keepalive
            if next_summary and now >= next_summary:
                ev = robot.evidence
                hb = ev.type_hb
                robot.log(
                    f"{_ts()} … HB {hb} 個 / パケット {ev.packets} 個 "
                    f"({ev.datagrams / max(0.001, now - started):.1f} データグラム/s) "
                    f"制御語サンプル {ev.ctrl_samples} 変化 {ev.ctrl_changes}"
                )
                next_summary = now + args.summary_every
    except KeyboardInterrupt:
        interrupted = True
        robot.log("")
        robot.log("Ctrl-C を受信。まとめます…")
    finally:
        sock.close()
        report = robot.evidence.report()
        print(report)
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
            print(f"(レポートを {args.report_out} に書いた)")
    return 0 if not interrupted else 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

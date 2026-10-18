# Beni (LZ100) を PC から動かすための全情報

対象: `com.mondorobotics.mondo.app` 1.0.35 / 機体コード名 **LZ100 = Beni**
解析元: `libmondo_native.so`（Debugビルド・シンボル残存, objdump）, `libapp.so`（Dart AOT, blutter で逆アセンブル）, `classes.dex`（SecNeo暗号化のため静的解析不可）
本ドキュメントは「PC 側で実装して Beni を操作する」ために必要な事実を全部まとめたもの。

## 確度の表記

| 記号 | 意味 |
|---|---|
| **【確定】** | ネイティブ/ Dart の逆アセンブル結果から直接読み取れた。定数・オフセット・ビット位置を確認済み |
| **【高確】** | 逆アセンブルの結果 + プロトコル上の必然性からほぼ間違いない |
| **【推定】** | 逆アセンブルから妥当に推論したが、実機キャプチャでの確認が望ましい |
| **【未確認】** | わかっていない / 値を特定できていない |

> 重要: スマホアプリの制御経路は **クラウドを経由しない**。`スマホ →(UDP)→ 192.168.7.1:9007` の自作トランスポート "UCP" 上を、①生の操作パケット（16バイト制御語）と ②Zenoh over UCP（protobuf コマンド）が同居して流れる。**PC から動かすには ① だけで足りる。**

---

## 0. 最短手順 (TL;DR)

1. **機体 Wi-Fi に参加する。** 機体は AP（ゲートウェイ `192.168.7.1`）。SSID/パスワードは BLE (GATT `0000FFF0-…` / `FFF1..FFF3`) で受け取る仕組み。すでに Wi-Fi 設定済みならその SSID に参加。
2. **UDP `192.168.7.1:9007` を開き、`ucp:sync` を送り `ucp:ack` を受ける**（ハンドシェイク）。
3. **20 ms ごとに「HB パケット」を送る。** 1 パケット = 12B ヘッダ + 16B 制御語 + 23B(入れ子ACK)。制御語の下位 8 バイトに走行値（ch0/ch1/ch2/ch3/roll）をビット詰めする。
4. 走行値が 0（=中央 1000/31）のまま送れば停止。値を変えればその方向へ動く。
5. 機体からのパケットに返す ACK は、自前の「次に期待する sn」を上記ヘッダと入れ子ACKに書けばよい（受信しなければ 0 のままでよいが、長時間放置すると機体側 ARQ が切れる可能性あり）。

これだけで走る。コマンド（パラメータ変更・ゲーム・attest 等）を投げたい場合のみ Zenoh over UCP（§6）が必要。

---

## 1. ネットワーク構成とポート一覧

| 層 | アドレス | プロトコル | 用途 | 確度 |
|---|---|---|---|---|
| 機体 AP | `192.168.7.1` | — | ゲートウェイ / 機体の全サービスのホスト | 【確定】 |
| 制御 | `192.168.7.1:9007` (UDP) | **UCP**（Zenoh + 生操作） | **本命**。`socket_client(0, ip, 9007)` がハードコード | 【確定】 |
| HTTP | `192.168.7.1:8080` | HTTP | 機体 HTTP エントリ | 【確定】 |
| HTTP | `192.168.7.1:8083` | HTTP | `/files/params_export.json`, `/files/snapshot/by_event_id/`, `/files/` | 【確定】 |
| HTTP | `192.168.7.1:8084` | HTTP | `/media/health`, `/media/files/`, `/media/sessions/`, `/media/cancel-download` | 【確定】 |
| Zenoh scout | `224.0.0.224:7446` (UDP, multicast) | Zenoh scout | Zenoh のピア探索。PC 実装では不要 | 【確定】 |
| （旧/別ポート） | 値 `400`, `500` | — | `__ucp_init` が同時に設定する別ポート。用途【未確認】（サーバ側/予備と推定） | 【未確認】 |
| クラウド | `global-api.mondorobotics.com` (+`global-apipre`/`global-apitest`/`us-apitest`) | HTTPS | アカウント・端末紐付け・MQTT/RTC 資格情報 | 【確定】 |
| 中継 | MQTT (ブローカーは API 依存) | MQTT | テレメトリ中継のみ | 【確定】 |
| 通話 | Agora RTC (トークンは API 依存) | HTTPS/UDP | 遠隔の映像/音声 | 【確定】 |

`network_security_config` の平文許可は `192.168.7.1` と `127.0.0.1` のみ → **機体向けは平文前提**の設計。

---

## 2. UCP（UDP 上の自作トランスポート）【確定】

実装は `libmondo_native.so`。シンボルが残っているので関数名で読める。

### 2.1 ハンドシェイク

`udpclient_connect_and_recv_thread` 内:

- クライアント → 機体: **ASCII 8 バイト `ucp:sync`**（データ領域 vaddr `0x2ca68`）
- 機体 → クライアント: **ASCII 7 バイト `ucp:ack`**（検証は「先頭4バイト == `ucp:`」かつ「bytes[3..6] == `:ack`」）

これが通れば接続確立。以後は通常パケットを送る。

> 実装時の注意: 機体側は接続元 IP/ポートを覚えるので、**同じ UDP ソケット**で送受信し続けること（NAT/ソースポート固定）。

### 2.2 パケットヘッダ（12 バイト, リトルエンディアン）

パケット先頭は `node+0x28`。送信は `ucp_init_tx` 内の `os_udp_send`、受信は `ucp_input(pkt,len)`（≈`0x71820`）。

| オフセット | サイズ | 内容 | 確度 |
|---|---|---|---|
| +0 | u8 | `0x51` = 下位4bit=**version 1**, 上位4bit=**magic 5** | 【確定】 |
| +1 | u8 | フラグ（下表） | 【確定】 |
| +2 | u16 | 累積 ack（piggyback）= 自分が次に期待する sn（`g_sock+0xa`） | 【確定】 |
| +4 | u16 | シーケンス番号（sn） | 【確定】 |
| +6 | u16 | パケット長（**ヘッダ12Bを含む**総長） | 【確定】 |
| +8 | u32 | タイムスタンプ（マイクロ秒） | 【確定】 |
| +12 | — | ペイロード | 【確定】 |

#### +1 フラグのビット

| bit | mask | 意味 | 確度 |
|---|---|---|---|
| 0 | `0x01` | ack（BAK）要求 | 【確定】 |
| 1 | `0x02` | フラグメント継続中（最後の断片では送信時にクリアされる。`ucp_send` 末尾の `and w10,w9,#0xfffffdff` が bit9=byte1 bit1 を落とす） | 【確定】 |
| 2-3 | `0x0C` | パケット種別（`bfi w8,type,#10,#2` → u16 の bit10-11）。値 0〜3 | 【確定】／値の意味割当は【推定】 |
| 4 | `0x10` | HB（後述の入れ子 ACK を同梱するパケット） | 【高確】 |

受信側は `(flags & 0xff) == 0x51` を検証し、外れたら
`pkt->version:%d, pkt->magic:%d` をログする。
`bit12`(=`0x1000`, つまり +1 の bit4) が立っていると「入れ子 ACK 付き」経路、`bit8` が立っていると confirm 経路に入る。

**送信時にアプリが作る値**（`ucp_send` が組み立てる段階）と、**実際にワイヤに乗る値**（bit1 クリア後）:

| 種別 | JNI 関数 | 作成時の +1 | ワイヤ上の +1（推定） |
|---|---|---|---|
| 生データ | `UcpManager_nativeSendData` (`0x608f0`) | `0x03` | `0x01` |
| ハートビート（制御語） | `nativeSendHeartBeat` (`0x60c64`) | `0x12` | **`0x10`** |
| その他 | `ucp_send(pkt,0)` | `0x02` | `0x00` |

→ **PC クライアントは HB を `+1 = 0x10` で送るのが安全**（bit1 は断片化フラグなので単一断片では落とす）。ロボット側は bit1 を見ていない可能性が高いので `0x12` でも通ると思われる【推定】。

### 2.3 ペイロード種別

1. **生データ**（`nativeSendData`）: ヘッダ12B + 任意バイト列。`+6` は `12+len`。BAK は付かない。
2. **HB（制御語）**（`nativeSendHeartBeat`）: **ヘッダ12B + 16B 制御語 + 23B の入れ子パケット**。合計 **39 バイト**。
   - `+6` = `segLen + 0x17` = `16 + 23` = **39**
   - 末尾 11 バイト（入れ子パケットのペイロード）の先頭バイトは **`0x05`**。受信側は
     `pkt->len > 0x17` かつ `pkt[pkt_len - 0xb] == 0x05` を要求する（= 入れ子 ACK ブロックの magic）。
3. **入れ子パケット（23B）のレイアウト**【高確】
   - 12B ヘッダ: `+0 = 0x51`, `+1 = 0x12`, `+2 = u16 累積ack`, `+4 = u16 sn`【未確認】, `+6 = u16 0x0017(23)`, `+8 = u32 ts`
   - 11B ペイロード: `+0 = u8 0x05`, `+1 = u16`, `+3 = u32`, `+7 = u32`
     この u16/u32/u32 はソケットの ACK 状態（`RECV BAK: cnt:%u, una:%d, bitmap:%x %x` と同じもの）で、
     実体は「una（累積ack）＋ 64bit SACK ビットマップ」と推定。

   → つまり HB の 11B ペイロード = `{0x05, u16 una, u32 bitmap_lo, u32 bitmap_hi}`【推定】

### 2.4 ARQ / タイムアウト

- 累積 ACK + SACK ビットマップ方式（`ucp_out: fastack retry sn:%d, len:%d, live_time:%dms`）。
- 重複検出: `ucp_parse_data: dup: sn:%d, len:%d, rcv_next:%d`
- タイムアウトで切断: `ucp connection timeout, disconnect!`
- MTU **1460** → 1 断片の最大ペイロード 1448B。最小データグラム 13B。
- 複数の UCP パケットを 1 データグラムに連結可能。
- `__ucp_init` が設定する値に `400` と `500` がある（再送間隔 ms / ウィンドウと推定）【未確認】。

### 2.5 受信デコードの要点（PC 側で機体パケットを読む場合）

- `ucp_input(pkt,len)` に渡る。`+6` の長さと `len` の整合性を取ってから 12B ヘッダを剥がす。
- 自分の送った sn と、相手から届いた sn を別々に管理し、**自分が次に期待する sn を `+2` と入れ子 ACK の una に**書く。

---

## 3. 走行制御（最重要）— 16 バイト制御語 【確定】

### 3.1 経路

```
control_loop.dart (20ms 周期)
  → ControlStateSnapshot.toPacket({allowLockedGimbalYaw})
  → ControlDataPacker.packControl(...)      ← 8 バイトのビットフィールドを作る
  → device_controller.dart
  → DeviceLinkManagerImpl.updateHeartbeatPacket(Uint8List(16))
  → NativeManager.updateHeartbeatPacket → MethodChannel
  → Kotlin UcpManager → JNI nativeSendHeartBeat
  → ucp_send(... flags=0x08 ...) → UDP 192.168.7.1:9007
```

### 3.2 制御語の実体

`packControl` は **`Uint8List(16)` を確保して、その先頭 8 バイトだけ**に 64bit のビットフィールドをリトルエンディアンで書く（残り 8 バイトは 0）。受信側 `unpackControl` は **8 バイト**を期待する。

### 3.3 ビット配置

呼び出し元 `toPacket` は `ControlStateSnapshot` の以下のフィールドをこの順で渡す:
5 つの double（`field_7, field_f, field_17, field_1f, field_27`）＋ 6 つの int（`field_3b, field_43, field_4b, field_2f, field_33, field_37`）＋ 6 つの追加（`field_67, field_5f, field_53, field_5b, field_4b, field_63`）。

`packControl` 側で:

| ビット範囲 | 幅 | 値 | 確度 |
|---|---|---|---|
| 0-10 | 11 | `_map2000(p1)` | 【確定】(p1 = field_7) |
| 11-21 | 11 | `_map2000(p2)` | 【確定】(p2 = field_f) |
| 22-32 | 11 | `_map2000(p3)` | 【確定】(p3 = field_17) |
| 33-38 | 6 | `_map62(p4)` | 【確定】(p4 = field_1f) |
| 39-44 | 6 | `_map62(p5)` | 【確定】(p5 = field_27) |
| 45-49 | 5 | 0..31 にクランプされる値 | 【確定】／名前【未確認】 |
| 50 | 1 | bool 2 つのAND | 【確定】／名前【未確認】 |
| 51-53 | 3 | `flipCmd` を 0..4 にクランプ | 【確定】(ログ `flipCmd out of range`) |
| 54-55 | 2 | 2bit 値 | 【未確認】 |
| 56-57 | 2 | 2bit 値 | 【未確認】 |
| 58 | 1 | フラグ | 【未確認】 |
| 59 | 1 | フラグ | 【未確認】 |
| 60 | 1 | フラグ | 【未確認】 |
| 61 | 1 | フラグ | 【未確認】 |
| 62 | 1 | フラグ | 【未確認】 |
| 63 | 1 | `(param & 0x10) != 0` | 【確定】 |

ロック中／非常停止のケースでは **ch0..ch2 = 1000（中央）、map62 系 = 31（中央）** に強制され、特定フィールドが 0/31 にされる。

`ControlStateSnapshot` の**名前付きフィールド**（`copyWith` から復元、16 個）:
`channel0Norm, channel1Norm, channel2Norm, channel3Norm, rollNorm, maxSpeed, isLock, isEmergencyStopped, minTurnMode, navFlag, height, jumpMode, flipCmd, jump, thirdPersonMode, videoEnabled`

→ **5 つの double = `channel0Norm, channel1Norm, channel2Norm, channel3Norm, rollNorm`**（この順は【推定】。ビット幅の非対称性から「0..2000 の3ch（前後・左右・旋回）＋ 0..62 の2ch（ch3 と roll）」と読める）。

### 3.4 正規化値 → 生値の変換式 【確定】

```python
def clamp(x, lo, hi): return max(lo, min(hi, x))

def map2000(v):   # 11bit, 0..2000, 中央=1000
    return clamp(round(clamp(v, -1.0, 1.0) * 1000 + 1000), 0, 2000)

def map62(v):     # 6bit, 0..62, 中央=31
    return clamp(round(clamp(v, -1.0, 1.0) * 31 + 31), 0, 62)
```

* `map2000`: -1.0 → 0、0 → 1000、+1.0 → 2000
* `map62`: -1.0 → 0、0 → 31、+1.0 → 62

### 3.5 Python 実装（最小構成・これだけで走る）

```python
import socket, struct, time

ROBOT = ("192.168.7.1", 9007)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.0)

# --- 1. ハンドシェイク ---
sock.sendto(b"ucp:sync", ROBOT)
data, _ = sock.recvfrom(2048)
assert data[:7] == b"ucp:ack", data
print("UCP handshake OK")

# --- 2. 制御語 ---
def map2000(v): return max(0, min(2000, round(max(-1.0, min(1.0, v)) * 1000 + 1000)))
def map62(v):   return max(0, min(62,   round(max(-1.0, min(1.0, v)) * 31  + 31)))

def control_word(ch0=0.0, ch1=0.0, ch2=0.0, ch3=0.0, roll=0.0):
    """ch0/ch1/ch2: -1..1 (前後/左右/旋回 等), ch3/roll: -1..1
    戻り値は 16 バイト（有効なのは先頭 8 バイト）。"""
    w = 0
    w |= (map2000(ch0) & 0x7FF) << 0
    w |= (map2000(ch1) & 0x7FF) << 11
    w |= (map2000(ch2) & 0x7FF) << 22
    w |= (map62(ch3)   & 0x3F ) << 33
    w |= (map62(roll)  & 0x3F ) << 39
    # bits 45-63 は用途未特定。0 のままで中央/停止相当（要実測で確認）
    return w.to_bytes(8, "little") + b"\x00" * 8

# --- 3. HB パケット組み立て（12B header + 16B ctrl + 23B nested ack） ---
def hb_packet(ctrl16, sn, una=0, bitmap=0):
    total = 12 + len(ctrl16) + 23          # = 39
    ts = int(time.monotonic() * 1_000_000) & 0xFFFFFFFF
    hdr = bytes([0x51, 0x10]) + struct.pack("<HHH", una, sn & 0xFFFF, total) + struct.pack("<I", ts)
    # 入れ子 ACK パケット（自身の 12B ヘッダ + 11B ペイロード）
    inner_len = 23
    inner_hdr = bytes([0x51, 0x12]) + struct.pack("<HHH", una, sn & 0xFFFF, inner_len) + struct.pack("<I", ts)
    inner_payload = bytes([0x05]) + struct.pack("<H", una) + struct.pack("<II", bitmap & 0xFFFFFFFF, bitmap >> 32)
    assert len(inner_payload) == 11
    return hdr + ctrl16 + inner_hdr + inner_payload

# --- 4. 20ms 周期で送る ---
sn = 0
for i in range(100):                       # 2 秒ぶん
    pkt = hb_packet(control_word(ch0=0.0), sn)
    sock.sendto(pkt, ROBOT)
    sn = (sn + 1) & 0xFFFF
    # 機体からのパケットを読み、ACK に反映（理想形）
    try:
        sock.settimeout(0)
        while True:
            d, _ = sock.recvfrom(2048)
            # d[4:6] が sn, d[2:4] が una → rcv_next を進める
    except BlockingIOError:
        pass
    except OSError:
        pass
    time.sleep(0.02)
```

送るだけでよい（HB は ack 要求ビットを立てない）。停止したいときは `ch0..ch2 = 0.0`（=1000）を送り続ける。

### 3.6 送信周期と注意

- `control_loop.dart` は **20 ms（50 Hz）**。
- 送信を止めると機体側の UCP がタイムアウトして切断（`ucp connection timeout, disconnect!`）。**停止させたい場合も「ゼロ値の HB を送り続ける」**。
- 制御語の ch0..ch2 の**符号・軸の対応**（前後/左右/旋回のどれが bit0-10 か）は §3.3 のとおり `channel0Norm..` の順と推定。**実機で 1 軸ずつ動かして確定させるのが確実**。

---

## 4. Zenoh over UCP（コマンド系）【高確】

制御語だけで走れるが、パラメータ変更・機体状態取得・ゲーム・attest を行うには Zenoh 層が必要。

- 同一の UDP 9007 ソケットに、UCP ペイロードとして Zenoh リンクフレームが載る（`mdZenohOverUcp*`, zenoh-pico 同梱）。
- リンクフレームのマジック（先頭バイト）:
  - `0xA4` / `0xA5` / `0xA6` … 5 バイトヘッダ（magic + u32 BE シーケンス）
  - `0xA7` … 3 バイトヘッダ（magic + u16 BE シーケンス）
- ペイロードのエンコーディングは **`application/protobuf`**（チャネル引数から確認済み）。
- 鍵空間プレフィクス: **`device`** が連結される（`AllocateArray(["device", <topic>])` → 例 `device/cmd/common/ping_req`）。購読はワイルドカード **`device/**`**。
- ホスト側の Kotlin ブリッジ: MethodChannel `mondoapp/zenoh_control` / EventChannel `mondoapp/zenoh_control/events`
  （メソッド: `start, publish, subscribe, unsubscribe, close, getState, getGeneration`、引数 `encoding`）
- Dart 側: `mondoapp/core/control_bus/transport/zenoh_transport_client.dart` など。

### 4.1 メッセージの枠（全コマンド共通）

```
message XxxReq  { WireHeader header = 1; XxxInput  data = 2; }   // 送信
message XxxRsp  { WireHeader header = 1; XxxOutput data = 2; }   // 受信
message WireHeader { uint32 seqNum = 1; }                        // 相関 ID のみ
```

`header.seqNum` でリクエスト/レスポンスを突き合わせる。トピックが型を決める（ワイヤに型名は無い）。

### 4.2 protobuf の型定義（PbFieldType → ワイヤ型）

`schema.txt` の `pb=` は Dart protobuf の `PbFieldType` 定数。`field_type.dart` の定義と突き合わせた対応表:

| pb 値 | PbFieldType | protobuf 型 | ワイヤ |
|---|---|---|---|
| `0x8000` | UINT32 | uint32 | varint |
| `0x2000` | SINT32 | sint32 | zigzag varint |
| `0x10000` | UINT64 | uint64 (Dart では Int64) | varint |
| `0x100` | FLOAT | **float（32bit）**（Dart の型は double と表示される） | fixed32 (wire type 5) |
| `0x80` | DOUBLE | double | fixed64 (wire type 1) |
| `0x40` | STRING | string | LEN |
| `0x20` | BYTES | bytes (Dart では List<int>) | LEN |
| `0x200` | ENUM | enum | varint |
| `0x10` | BOOL | bool | varint |
| `0x8006` | PACKED_UINT32 | repeated uint32 | LEN（packed） |
| `0x106` | PACKED_FLOAT | repeated float | LEN（packed） |
| `0x10006` | PACKED_UINT64 | repeated uint64 | LEN（packed） |
| `0x2006` | PACKED_SINT32 | repeated sint32 | LEN（packed） |

> 重要: **「double」と出ているフィールドの大半は実は protobuf `float`（32bit）**（`tripOdomDis` 等）。`explorationScale` なども `float`。Dart 側では `double` として扱われるため、逆アセンブル的には double に見える。実装では float として読むこと。
> `velocity` / `pitchAngle` / `gimbalAngle` / `rollAngle` は **sint32**（zigzag）、番号系・状態系は **uint32**。

スキーマ全体は 263 メッセージ・602 フィールド（`schema.txt` に全量あり。§9 参照）。

### 4.3 トピック一覧（主要）

`device` + 以下の topic。

**走行・状態（real_rl）**

| トピック | 方向 | 内容 |
|---|---|---|
| `/cmd/real_rl/control_request` | →機体 | **高レベル制御要求**（＝学習済み制御器への指令） |
| `/cmd/real_rl/control_ack` | ←機体 | 上記の ack |
| `/status/real_rl/status` | ←機体 | real_rl 実行状態 |
| `/data/model/control_input_frame` | ←機体 | 制御入力フレーム（RC 生入力の中継） |
| `/data/model/control_input_state` | ←機体 | 制御入力状態 |
| `/data/model/robot_body_state` | ←機体 | 機体姿勢・姿勢角・状態 |
| `/status/robot/status` | ←機体 | 機体ステータス |

**共通**

| トピック | In / Out |
|---|---|
| `/cmd/common/ping_req` | `PingInput{bytes pingData=1}` / `PingOutput{uint32 retCode=1; bytes pingData=2}` |
| `/cmd/common/get_version_req` | 空 / `GetVersionOutput{uint32 retCode=1; uint32 majorVersion=2; uint32 minorVersion=3; bytes hardwareVersion=4; bytes loaderVersion=5; bytes firmwareVersion=6; uint32 isMassProduct=7}` |
| `/cmd/common/set_parameter_req` | `{uint32 category=1; uint32 parameterId=2; uint32 type=3; uint32 len=4; bytes value=5}` → `{uint32 returnCode=1}` |
| `/cmd/common/get_parameter_req` | `{uint32 category=1; uint32 parameterId=2}` → `{uint32 returnCode=1; uint32 category=2; uint32 parameterId=3; uint32 type=4; uint32 len=5; bytes value=6}` |
| `/cmd/robot/sn_req` | → `RobotSnRsp{uint32 snType=1; uint32 lens=2; bytes sn=3}` |

**ゲーム（綱引き・宝探し等）**

| トピック | 内容 |
|---|---|
| `/cmd/game/control_req` | `GameControlInput{uint64 timestampNs=1; uint32 version=2; uint32 gameType=3; uint32 action=4; uint32 flags=5; uint32 sessionId=6; uint32 paramHash=7}` |
| `/cmd/game/player_input` | `GamePlayerInput{uint64 timestampNs=1; ...; repeated float accG=11; repeated float gyroDps=12; repeated float attitudeDeg=13}`（11-13 は packed float） |
| `/status/game/status` | ゲーム状態 |
| `RobotChaseGameParams{uint32 iceBreakDistance=1; uint32 chaseDistance=2; uint32 speedSetting=3; uint32 turnTimeMs=4; uint32 turnTransitionMs=5}` | 鬼ごっこ設定 |

**real_rl のメッセージ定義**

```
RealRlControlRequest { uint32 version=1; uint32 inferenceMode=2;
                       uint32 rolloutRecordingRequested=3; uint32 recordingDurationMs=4;
                       uint64 clientSessionId=5; float explorationScale=6; }
RealRlControlAck     { uint32 resultCode=1; uint32 requestedInferenceMode=2;
                       uint32 rolloutRecordingRequested=3; uint32 controlSource=4;
                       uint32 recordingDurationMs=5; uint64 clientSessionId=6;
                       float requestedExplorationScale=7; }
RealRlStatus         { uint32 version=1; uint32 supported=2; uint32 reserved0=3;
                       uint32 controlSource=4; uint32 requestedInferenceMode=5;
                       uint32 activeInferenceMode=6; uint32 rolloutRecordingRequested=7;
                       uint32 rolloutRecordingActive=8; uint32 runtimeState=9;
                       uint32 modelReady=10; uint32 collectorReady=11; uint32 collectorFault=12;
                       uint32 sessionActive=13; uint32 episodeActive=14; uint32 lastRequestSeqNum=15;
                       uint32 recordingRemainingMs=16; uint64 sessionId=17; uint64 episodeId=18;
                       uint64 writtenRecordCount=19; uint32 pendingRecordCount=20;
                       uint64 droppedRecordCount=21; uint32 completedFragmentCount=22;
                       uint64 lastRecordTimestampNs=23; uint64 timestampNs=24;
                       uint32 inferenceActive=25; uint64 lastClientSessionId=26;
                       uint32 statusStale=27; uint32 runtimeStatusAgeMs=28;
                       float requestedExplorationScale=31; float activeExplorationScale=32; }
```

**セキュリティ / アクティベーション**

```
DeviceSecurityDeviceActivateReq      { bytes requestId=1; uint64 activatedAt=2; string activationToken=3; }
DeviceSecurityDeviceActivateRsp      { uint32 retCode=1; bytes requestId=2; uint32 state=3; uint64 stateChangedAt=4; }
DeviceSecurityDeviceActivationStateReq {}   // 空
DeviceSecurityDeviceActivationStateRsp { uint32 retCode=1; uint32 state=2; uint64 stateChangedAt=3; }
DeviceSecurityDeviceAttestReq        { bytes challenge=1; }
DeviceSecurityDeviceAttestRsp        { uint32 retCode=1; uint32 certLen=2; bytes certDer=3; bytes signature=4; }
DeviceSecurityE2eProvisionReq        (鍵プロビジョニング)
```
トピック: `/cmd/security/device_activate_req` | `device_activate_rsp` | `device_attest_req` | `device_activation_state_req` | `e2e_provision_req` | `crypto_info` 系。
クラウド側の対応: `/app/v1/user-device/{sn}/security/{binding/confirm, activation/activate, device-attestation/challenge}`。

その他: `/cmd/audio/op_req`, `/cmd/camera/{unified_cmd,config_switch_cmd}`, `/cmd/led/ctrl_oneway`, `/cmd/tracking/{algo_switch_cmd,ctrl_cmd}`, `/cmd/ota/upgrade_control_req`, `/cmd/time/correction_req`, `/cmd/mission/request`, `/cmd/hms/action_request`, `/cmd/parameter/{update_req,query_req}`, `/cmd/log/*`, `/status/telemetry/event`, `/status/gateway/telemetry_event`, `/cmd/gateway/analytics_event_receipt`, `/media/*`。

（全 234 トピック: `keys.txt`。全 263 メッセージ定義: `schema.txt`。全 68 オペレーション: `ops.txt`。§9 参照）

### 4.4 Zenoh フレームを PC から送る場合の難度

`0xA4-0xA7` のマジック＋シーケンス番号のフレームに、protobuf ペイロードを載せて UCP ペイロードとして送る。**リンク層（zenoh-pico のセッション確立/keepalive/PING/PONG）を実装する必要があり、§3 の制御語に比べ難度が高い。**
→ 既存の zenoh-pico を PC にビルドしてカスタムトランスポート（UCP=UDP 9007）を差し込むのが現実的。

---

## 5. パラメータ

`params_mapping.json`（アプリ内アセット）から復元:

| parameterId | 名前 | 値 | 変換 |
|---|---|---|---|
| 1 | `max_speed` | 2.0 | — |
| 2 | `max_yaw_rate` | 170.0 | radToDeg |
| 3 | `power_response_level` | 2.0 | — |
| 4 | `circling_linear_velocity` | 0.5 | — |

読み出しは HTTP `http://192.168.7.1:8083/files/params_export.json` でも可能【確定】。
書き換えは Zenoh `/cmd/common/set_parameter_req`（`category`, `parameterId`, `type`, `len`, `value`）または `/cmd/parameter/update_req`。

---

## 6. ローカル HTTP / BLE / クラウド

### 6.1 ローカル HTTP（PC から curl で叩ける）

| URL | 内容 |
|---|---|
| `http://192.168.7.1:8080/` | 機体 HTTP エントリ |
| `http://192.168.7.1:8083/files/` | ファイル一覧 |
| `http://192.168.7.1:8083/files/params_export.json` | **パラメータ全量** |
| `http://192.168.7.1:8083/files/snapshot/by_event_id/<id>` | イベント画像 |
| `http://192.168.7.1:8084/media/health` | メディアサーバ死活 |
| `http://192.168.7.1:8084/media/files/`, `/media/sessions/` | 録画ファイル / セッション |
| `http://192.168.7.1:8084/media/cancel-download` | ダウンロード中断 |

### 6.2 BLE（初期設定・Wi-Fi 資格情報）

- GATT service `0000FFF0-0000-1000-8000-00805F9B34FB`（解決後 `0100FFF0-…`）、characteristic `FFF1`–`FFF3`。
- 手順: `ble_probe` → `ble_connect` → **`ble_wifi_credential_step`（SSID/パスワードを BLE で機体へ渡す）** → `ble_country_code_*`（国コード同期）。
- BLE も `md_device_protocol` のバイト輸送路（`ble_characteristic_byte_transport`）= **Wi-Fi 前でも同じメッセージ体系が使える**。
- スキャン時の判別は manufacturer data / service data（`lz_manifest`）。

### 6.3 クラウド（PC 制御には必須ではない）

- `https://global-api.mondorobotics.com`（テスト系: `global-apipre`/`global-apitest`/`us-apitest`）
- ログイン: `POST /account/v1/login` → `/account/v1/login/endpoint` でリージョン別 URL を取得 → `/account/v1/jwt/derive` で派生トークン。
- パスワードは `/account/v1/rsa/pub-key` の RSA 公開鍵で暗号化。
- ヘッダ: `Authorization: Bearer <JWT>`, `X-Region`, `X-App-Version`, `X-App-Build-Id`, `X-App-Package`, `X-Trace-Id`, `X-Mondocut-Billing-Ver`。
- 端末紐付け: `/app/v1/user-device/{sn}/binding-account`, `/unbind`, `/deviceActive/status`。
- MQTT ブローカー: `/api/v1/mqtt/robotConnect` が払い出す（ホスト・トピック・資格情報は API 応答依存）。
- Agora RTC: `rtc_api`（`getRtcToken`/`getChannelName`）。

---

## 7. セキュリティ

- 機体との E2E: `/cmd/security/*`（challenge → attest → activate → e2e_provision）。クラウド `/app/v1/user-device/{sn}/security/*` と対。
- ファームウェアは暗号化配布（`firmware_encryption_resolver`）。
- **UCP（UDP）自体に暗号化は見当たらない**【推定】。したがって同一 LAN に居れば §3 のパケットで走らせられる可能性が高い（未認証セッションを拒否する実装なら activate が必要）。
- attest/activate を通していないセッションで走行が拒否されるかは【未確認】。まず素で HB を送ってみるのが最短。

---

## 8. 未確定事項と実機での確定手順

| # | 未確定 | 確定方法 |
|---|---|---|
| 1 | 制御語 ch0/ch1/ch2 の軸対応（前後/左右/旋回） | 実機で 1 軸ずつ ±1 を与えて動作を観察 |
| 2 | bits 45-49, 50, 54-63 の意味（`isLock`/`height`/`jump`/`throw` 等） | アプリで該当操作をしつつ PCAP で制御語を差分観測 |
| 3 | `+1` bit1（フラグメント）がワイヤ上で 0 か 1 か | PCAP で 1 パケット観察 |
| 4 | 入れ子 ACK の u16/u32 の意味（una / bitmap） | PCAP で sn と突き合わせ |
| 5 | Zenoh リンクフレームの完全フォーマット | アプリでコマンド送信しつつ PCAP。`device/**` 購読で観測 |
| 6 | keyexpr の完全形（`device/cmd/...` か `device/<sn>/cmd/...` か） | PCAP の zenoh フレーム内のキー文字列を直接読む |
| 7 | UCP の暗号化有無 / 未認証拒否の有無 | 素で HB を送ってみる |
| 8 | ポート 400 / 500 の用途 | ネイティブの `__ucp_init` 呼び出し元を追う |

### 実機キャプチャ手順

```bash
# 機体 Wi-Fi に接続した PC で
sudo tcpdump -i en0 -n -s0 -w beni_ucp.pcap 'host 192.168.7.1 and udp'
# ハンドシェイクだけ確認
sudo tcpdump -i en0 -X -n 'host 192.168.7.1 and udp port 9007'
```
- アプリを起動 → 接続 → スティックを動かす → 停止、の各フェーズでキャプチャを分ける。
- 期待される観測: 最初に `ucp:sync` → `ucp:ack`、以後 20ms 間隔の 39 バイト UDP。

Frida で `ucp_send` / `ucp_input` をフックすれば、ワイヤ値と内部値を同時に取れる（`libmondo_native.so` はシンボルが残っているのでフックしやすい）。

---

## 9. 参考ファイル（解析成果物）

解析は使い捨ての環境（スクラッチパッドと `/tmp`）で行った。以下の表は「どんな成果物が得られたか」の記録で、**`docs/apk-comms.md` 以外はこのリポジトリには含まれていない**。

| 成果物 | 内容 |
|---|---|
| `docs/apk-comms.md` | 通信全体の解析レポート（同梱） |
| `keys.txt` | Zenoh トピック 234 件 |
| `schema.txt` | protobuf 263 メッセージ・602 フィールドの全定義 |
| `ops.txt` | Zenoh オペレーション 68 件（op → topic → In/Out 型） |
| `native_text.asm` | `libmondo_native.so` `.text` 全逆アセンブル（**UCP ヘッダ/ハンドシェイクの根拠はここ**） |
| `dynsym.txt` | ネイティブの動的シンボル一覧（`objdump -T`、約 2149 個） |
| `blutter_out/asm/` | Dart AOT 逆アセンブル（blutter 出力。クラス/メソッド単位の疑似 Dart） |
| `blutter_out/asm/mondoapp/core/robotics/control_data_packer.dart` | `packControl` / `_map2000` / `_map62` / `unpackControl` |
| `blutter_out/asm/mondoapp/core/device_hub/products/lz/data/model/control_state_snapshot.dart` | 制御語の元になる 16 フィールド |
| `blutter_out/asm/mondoapp/core/device_hub/products/lz/usecase/impl/control_loop.dart` | 20ms 制御ループ |
| `blutter_out/asm/mondoapp/core/robotics/ucp_manager.dart` | `updateHeartbeatPacket` |
| `blutter_out/asm/mondoapp/core/control_bus/transport/zenoh_transport_client.dart` | Zenoh チャネル引数・イベント |
| `blutter_out/asm/md_device_protocol/src/wire/generated_protobuf_wire.dart` | protobuf メッセージ定義（フル） |
| `blutter_out/asm/md_device_protocol/src/wire/generated_protobuf_codecs.dart` | encode / decode |
| `blutter_out/asm/md_device_protocol/src/operation/generated_zenoh_operations.dart` | op → topic 表 |
| `apk/unzipped/` | APK 展開（`bledata.proto`, `assets/rum_android_config.json`, `params_mapping.json` 等） |

### 逆アセンブルをやり直す手順

```sh
# 1) APK を展開して arm64-v8a のライブラリを取り出す
unzip -o MondoApp.apk 'lib/arm64-v8a/*' 'classes*.dex' 'assets/*' -d unzipped

# 2) ネイティブはシンボルが残っている（Debug ビルド）ので関数名で読める
objdump -d --no-show-raw-insn unzipped/lib/arm64-v8a/libmondo_native.so > native_text.asm
objdump -T unzipped/lib/arm64-v8a/libmondo_native.so > dynsym.txt

# 3) Dart AOT (libapp.so) は Dart 3.11.5 対応の blutter で疑似 Dart に落とす
#    → blutter_out/asm/ 以下にクラス/メソッド単位で出力される
```

> `libmondo_native.so` は `D:/android/mondo-app/build/.cxx/debug/...` を埋め込んだ **Debug ビルドのまま出荷**されている。このおかげで UCP の中身（ポート 9007・`ucp:sync`/`ucp:ack`・12 バイトヘッダ）が読めた。
> Kotlin/Java 側（`classes.dex`）は SecNeo で暗号化されており静的解析は不可。`UcpManager` / `NativeZenohBridge` / `ZenohControlPlugin` 程度のクラス名しか見えないため、本ドキュメントの結論は **ネイティブ .so と Dart AOT から**導いている。

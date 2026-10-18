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
3. **20 ms ごとに「HB パケット」を送る。** 1 パケット = 12B ヘッダ + 16B 制御語 + 11B の ACK ブロック（合計 39B）。制御語の下位 8 バイトに走行値（ch0/ch1/ch2/ch3/roll）をビット詰めする。
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
| 4 | `0x10` | HB（末尾に ACK ブロックを同梱するパケット。§2.3） | 【高確】 |

受信側は `(flags & 0xff) == 0x51` を検証し、外れたら
`pkt->version:%d, pkt->magic:%d` をログする。
`bit12`(=`0x1000`, つまり +1 の bit4) が立っていると「ACK ブロック付き」経路、`bit8` が立っていると confirm 経路に入る。
（これらは送信キューの内部フラグ（`ucp_init_tx` が `item+0x28` から読む u16）。ワイヤの `+1` はこの上位バイトに対応する。）

**送信時にアプリが作る値**（`ucp_send` が組み立てる段階）と、**実際にワイヤに乗る値**（bit1 クリア後）:

| 種別 | JNI 関数 | 作成時の +1 | ワイヤ上の +1（推定） |
|---|---|---|---|
| 生データ | `UcpManager_nativeSendData` (`0x608f0`) | `0x03` | `0x01` |
| ハートビート（制御語） | `nativeSendHeartBeat` (`0x60c64`) | `0x12` | **`0x10`** |
| その他 | `ucp_send(pkt,0)` | `0x02` | `0x00` |

→ **PC クライアントは HB を `+1 = 0x10` で送るのが安全**（bit1 は断片化フラグなので単一断片では落とす）。ロボット側は bit1 を見ていない可能性が高いので `0x12` でも通ると思われる【推定】。

> アプリの HB がワイヤで実際にどの値になるか（#7）は [docs/mock-robot.md](mock-robot.md) のモック機体で観測できる（レポートの「#7 ワイヤ上の +1 フラグ」）。

### 2.3 ペイロード種別

1. **生データ**（`nativeSendData`）: ヘッダ12B + 任意バイト列。`+6` は `12+len`。BAK は付かない。
2. **HB（制御語）**（`nativeSendHeartBeat`）: **ヘッダ12B + 16B 制御語 + 11B の ACK ブロック**。合計 **39 バイト**。
   - `+6` = `segLen + 0x17` = `16 + 23` = **39**（`0x17` = ヘッダ 12B + ACK ブロック 11B のオーバーヘッド）
   - 末尾 11 バイトの先頭バイトは **`0x05`**。受信側は
     `pkt->len > 0x17` かつ `pkt[pkt_len - 0xb] == 0x05` を要求する（= ACK ブロックの magic）。
   - → **制御語の後ろに 12B ヘッダ付きの入れ子パケットは無い**（以前の記述は誤り。39 = 12 + 16 + 11 で閉じる）。
3. **末尾 ACK ブロック（11B）のレイアウト**【確定】— `ucp_init_tx` の送信準備で `strb #0x05, [末尾+0x1d]`、続けて
   `strh <u16>, [末尾+0x1e]`, `str <u32>, [末尾+0x20]`, `str <u32>, [末尾+0x24]` を書いている:
   - `+0 = u8 0x05`（magic）
   - `+1 = u16`, `+3 = u32`, `+7 = u32`

   この u16/u32/u32 はソケットの ACK 状態（ログ `RECV BAK: cnt:%u, una:%d, bitmap:%x %x` と同じ並び）で、
   実体は「una（累積ack）＋ 64bit SACK ビットマップ」と推定【推定】。

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

### 3.3 ビット配置【確定 — アプリ自身のデコード関数に名前が残っている】

`control_data_packer.dart` の **`unpackControl`**（8 バイト → `Map<String, dynamic>` に戻す関数。`ControlLoop._updateDebugInfo` から呼ばれるデバッグ用デコード）に、**フィールド名とビット幅がそのまま残っている**。これが正:

| ビット範囲 | 幅 | 名前 | 値（エンコード側） |
|---|---|---|---|
| 0-10 | 11 | `channel0` | `map2000` → 0..2000（中央 1000） |
| 11-21 | 11 | `channel1` | `map2000` |
| 22-32 | 11 | `channel2` | `map2000` |
| 33-38 | 6 | `channel3` | `map62` → 0..62（中央 31） |
| 39-44 | 6 | `roll` | `map62` |
| 45-49 | 5 | `height` | 0..31 にクランプ |
| 50 | 1 | `jump_cmd` | 0/1 |
| 51-53 | 3 | `flip_cmd` | 0..4 にクランプ（`flipCmd out of range: ..., clamped to 0..4` の警告ログあり） |
| 54-55 | 2 | `max_speed` | 0..2 |
| 56-57 | 2 | `nav_flag` | 0..2 |
| 58 | 1 | `ispause` | 0/1 |
| 59 | 1 | `islock` | 0/1 |
| 60 | 1 | `min_turn_mode` | 0/1 |
| 61 | 1 | `third_person_mode` | 0/1 |
| 62 | 1 | `jump_mode` | 0/1 |
| 63 | 1 | `no_liveview` | 0/1（エンコード側は `(param & 0x10) != 0`） |

`channel0`〜`channel2` が 11bit、`channel3`/`roll` が 6bit という非対称は、前者が `map2000`、後者が `map62` を通るため（エンコード側も `_map2000`×3 + `_map62`×2 の 5 呼び出しで一致）。

**物理的な意味【確定】** — `device_motion_capability.dart` の setter 名がそのまま軸名:

| ビット | 制御語名 | 意味 | アプリ内の setter |
|---|---|---|---|
| 0-10 | `channel0` | **前後（前進/後退）** | `setForwardNorm` → `setChannel0Norm` |
| 11-21 | `channel1` | **旋回（ヨー）** | `setYawNorm` → `setChannel1Norm` |
| 22-32 | `channel2` | **雲台 yaw（首振り）** | `setGimbalYaw` → `setChannel2Norm` |
| 33-38 | `channel3` | **雲台 pitch（チルト）** | `setGimbalTilt` → `setChannel3Norm` |
| 39-44 | `roll` | **機体ロール** | `setRoll` → `setRollNorm` |
| 45-49 | `height` | **車高** | `setHeight` / `adjustHeight` → `setHeightRaw` |
| 54-55 | `max_speed` | 速度域（0..2） | `DeviceMotionSettingsCapability` → `setMaxSpeed` |
| 56-57 | `nav_flag` | ナビ/追従のモード（0..2） | `setNavFlag`（`device_body_control_capability` / `nav_settings_session`） |

→ **横移動（strafe / vy）チャンネルは存在しない**。前後 + 旋回の 2 自由度（差動）が基本。`setMovement(前後, 旋回)` が channel0 と channel1 を同時に設定する。

ゲームパッド入力には `_onControllerChanged` 内で **expo カーブ**（`out = expo·v³ + (1-expo)·v`）と ±1 クランプが入り、`roll` は |v| ≦ 0.01 で自動解放（`releaseRoll`）される。PC 実装では expo は省いても動くが、挙動を合わせたいなら同式を使う。

`packControl` の入力（`ControlStateSnapshot` → `toPacket`）は、5 つの double（`channel0Norm..channel3Norm, rollNorm`）のうち最初の 3 つ → `map2000`、後の 2 つ → `map62`、残りは int/bool をクランプして詰める。【高確】

ロック中／非常停止のケースでは **ch0..ch2 = 1000（中央）、map62 系 = 31（中央）** に強制され、特定フィールドが 0/31 にされる。

`ControlStateSnapshot` の**名前付きフィールド**（`copyWith` から復元、16 個）:
`channel0Norm, channel1Norm, channel2Norm, channel3Norm, rollNorm, maxSpeed, isLock, isEmergencyStopped, minTurnMode, navFlag, height, jumpMode, flipCmd, jump, thirdPersonMode, videoEnabled`

ビット名との対応（【高確】。名前が一致するものは確定、それ以外は推定）:
`channel0Norm..3Norm, rollNorm` ↔ `channel0..3`, `roll`／`height`↔`height`／`jump`↔`jump_cmd`／`flipCmd`↔`flip_cmd`／`maxSpeed`↔`max_speed`／`navFlag`↔`nav_flag`／`isLock`↔`islock`／`minTurnMode`↔`min_turn_mode`／`thirdPersonMode`↔`third_person_mode`／`jumpMode`↔`jump_mode`／`isEmergencyStopped`↔`ispause`【推定】／`videoEnabled`↔`no_liveview`【推定・符号反転の可能性】

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

ここで入るのは**正規化値まで**。これが実際に何 m/s に相当するか（機体側の換算係数）は §5.1 と §8 #13 を参照。

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

def control_word(ch0=0.0, ch1=0.0, ch2=0.0, ch3=0.0, roll=0.0,
                 height=0, jump_cmd=0, flip_cmd=0, max_speed=0, nav_flag=0,
                 ispause=0, islock=0, min_turn_mode=0, third_person_mode=0,
                 jump_mode=0, no_liveview=0):
    """ch*: -1..1（§3.6 の軸対応は実機で確定）。height: 0..31。flip_cmd: 0..4。
    戻り値は 16 バイト（有効なのは先頭 8 バイト）。"""
    w = 0
    w |= (map2000(ch0) & 0x7FF) << 0        # channel0
    w |= (map2000(ch1) & 0x7FF) << 11       # channel1
    w |= (map2000(ch2) & 0x7FF) << 22       # channel2
    w |= (map62(ch3)   & 0x3F ) << 33       # channel3
    w |= (map62(roll)  & 0x3F ) << 39       # roll
    w |= (height       & 0x1F ) << 45       # height
    w |= (jump_cmd     & 0x1  ) << 50       # jump_cmd
    w |= (flip_cmd     & 0x7  ) << 51       # flip_cmd (0..4)
    w |= (max_speed    & 0x3  ) << 54       # max_speed
    w |= (nav_flag     & 0x3  ) << 56       # nav_flag
    w |= (ispause      & 0x1  ) << 58       # ispause
    w |= (islock       & 0x1  ) << 59       # islock
    w |= (min_turn_mode     & 0x1) << 60    # min_turn_mode
    w |= (third_person_mode & 0x1) << 61    # third_person_mode
    w |= (jump_mode         & 0x1) << 62    # jump_mode
    w |= (no_liveview       & 0x1) << 63    # no_liveview
    return w.to_bytes(8, "little") + b"\x00" * 8

# --- 3. HB パケット組み立て（12B header + 16B ctrl + 11B ACK ブロック = 39B） ---
def hb_packet(ctrl16, sn, una=0, bitmap=0):
    total = 12 + len(ctrl16) + 11          # = 39
    ts = int(time.monotonic() * 1_000_000) & 0xFFFFFFFF
    hdr = bytes([0x51, 0x10]) + struct.pack("<HHH", una, sn & 0xFFFF, total) + struct.pack("<I", ts)
    # 末尾 11B の ACK ブロック（12B ヘッダは付けない。§2.3）
    ack = bytes([0x05]) + struct.pack("<H", una) + struct.pack("<II", bitmap & 0xFFFFFFFF, bitmap >> 32)
    assert len(ack) == 11
    return hdr + ctrl16 + ack

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
- 制御語の軸対応は §3.3 で確定済み（channel0=前後, channel1=旋回, channel2=雲台yaw, channel3=雲台pitch, roll=ロール, height=車高）。**符号の向き**（+ が前進か、右旋回か）だけは実機で 1 回確認するのが確実（ゲームパッド JSON の `invert` フラグ依存）。

---

### 3.7 制御系統の整理 — 「誰が指令を出しているか」【確定】

Beni の操作は 3 系統ある。**付属の送信機（RC）は Android を経由しない。混同しやすいので注意。**

| # | 指令の出し手 | 経路 | スマホ / PC の関与 |
|---|---|---|---|
| ① | **付属の送信機（UWB RC）** | RC →(UWB 無線)→ 機体 **直結** | **無し**。アプリ/PC は観測のみ |
| ② | **スマホ側の入力**（画面の仮想スティック / スマホに繋いだゲームパッド） | アプリ →(UCP)→ 機体。**16B 制御語（§3.3）** | アプリが入力源。§3 はこれ |
| ③ | 機体の学習済み制御器 (real_rl) | アプリが `/cmd/real_rl/control_request` で要求 | 高レベル要求のみ |

#### ① 送信機（RC）— Android を通らない

根拠: 以下はすべて **機体 → アプリ**（"listen" オペレーション。アプリは購読するだけ）。

| トピック | 型 | 内容 |
|---|---|---|
| `/status/uwb/rc_status` | `UwbRcStatus{uint32 battery=1; sint32 rssi=2}` | 送信機の電池・電波強度 = **機体が RC と無線で直結している** |
| `/status/rc/joystick_raw` | `RcJoystickRaw{uint32 x=1; uint32 y=2}` | 送信機スティックの生値 |
| `/status/rc/fsm_state` | `RcFsmState{timestampMs, prevState, prevSub, keyId, eventType, nextState, nextSub}` | 送信機の状態機械・キーイベント |
| `/status/rc/joystick_calib_progress` | — | 較正の進捗 |

アプリが ① に**送る**のは設定系のみ: `/cmd/rc/joystick_calib_req`（較正）, `/cmd/rc/vibration_set_req`/`vibration_get_req`（バイブ）, `/cmd/rc/function_key_mode`（ファンクションキー割当）。
→ **スティックの指令そのものはスマホを通らない。** PC から使いたい場合は「機体が公開する `joystick_raw` を読んで自分の制御語に詰め直す」中継しかない（送信元にはなれない）。

#### ② スマホ側の入力 — Android がパススルーする経路

根拠: アプリは **`gamepads` プラグイン**（`package:gamepads_platform_interface`, `_AndroidMotionEventConverter`）でスマホに繋いだコントローラを読み、`DeviceExternalInputService`（`attachController` / `detachController` / `_onControllerChanged` / `_applyHoldButtons`）で処理し、`assets/config/gamepad/*.json` の表で軸/ボタンを**抽象アクション**に変換する。入力源は enum `ControlInputSource`（`touch` = 画面の仮想スティック, `gamepad` = 外部パッド）でタグ付けされる（`DeviceController.markInputSource`）。

ゲームパッド定義（APK 同梱 `assets/flutter_assets/assets/config/gamepad/`）:

| ファイル | 想定デバイス | racing モードの主な割当 |
|---|---|---|
| `xbox_android.json` | Xbox パッド (Android) | `AXIS_X`=yaw, `AXIS_LTRIGGER`(-1)/`AXIS_RTRIGGER`(+1)=throttle, `AXIS_Z`=gimbalYaw, `AXIS_RZ`=gimbalPitch(反転), `A`=jump, `B`=standUpFold, `X`=record, `Y`=markHighlight, `THUMBR`=horn, `SELECT`=intercomToggle, `START`=settings, HAT_Y=heightUp/Down |
| `xbox_ios.json` | Xbox パッド (iOS) | `leftStick-x`=yaw, トリガ=throttle, `rightStick-x`=gimbalYaw, `rightStick-y`=gimbalPitch(反転) |
| `dji.json` | DJI 送信機 | `AXIS_X`=yaw, `AXIS_Y`=throttle, `AXIS_RX`=gimbalYaw, `AXIS_RY`=gimbalPitch(反転), `AXIS_Z`=roll, `AXIS_RZ`=rightDial, `A`=jump, `B`=flipForward, `X`=shutter, `Y`=standUpFold(500ms ホールド), `R1`=horn, `L1`=rcSwitch |
| `mt12.json` | RadioMaster MT12 | `AXIS_X`=throttle, `AXIS_Y`=yaw(反転) |
| `steer_and_pedal.json` | ハンドル + ペダル | `AXIS_X`=yaw, `AXIS_Y`=throttle |

抽象アクション名（`mondoapp/core/services/gamepad/robot_action.dart`）:
`throttle, yaw, gimbalYaw, gimbalPitch, roll, rightDial, jump, flipForward, flipBackward, flipLeft, flipRight, standUpFold, horn, shutter, record, markHighlight, intercomToggle, settings, rcSwitch, heightUp, heightDown`
モードは `racing` / `camera` の 2 種。`rcSwitch`（L1/LB）は **対応するハンドラ本体を asm 上で特定できていない**（`RobotAction` の値として名前があるだけ。`libapp.strings` の出現も enum 名 1 件のみ）。下記のとおり「RC とアプリのどちらが有効か」を決めるゲートはスタブ化されているので、パッド側のモード切替（`racing` ↔ `camera`）と推定。【未確定】

外部入力サービスが特別扱いする操作（中国語ログから）: `手柄触发急停`（緊急停止）, `手柄触发恢复`（立ち上がり/復帰）, `手柄触发对讲切换`（インターコム切替）。

→ **PC から物理パッドで操作する場合は、② と同じ実装をすればよい**（PC でパッドを読み、axis → 制御語を上の表に従って詰める）。① の送信機を使いたい場合は PC は観測側に回る。

#### ① とアプリの接点はどこにあるか【確定】

アプリ側の RC 担当は `.../products/lz/adapter/remote_control/remote_control_service.dart`。購読した値をどこで使うかを追うと、**走行制御には一切使っていない**ことが確認できる。

| 受信 | 消費先 | 用途 |
|---|---|---|
| `/status/rc/joystick_raw` → `rawSticksStream` | `AccessoryManagementService.calibrationEventsFor`（`calibrationStatusStream` と merge して `RcCalibrationEvent`） | **送信機の較正 UI の表示のみ** |
| `/status/uwb/rc_status` → `stream` | `remote_control_side_panel.dart` 等 | 電池 / RSSI の表示 |
| `/status/rc/fsm_state` | `DeviceCameraCapability._onRcFsmState` | 下記 1 件のみ |

- **スティック値を 16B 制御語に詰め直すコードは存在しない**（`rawSticksStream` の利用箇所は較正サービス 1 箇所だけ）→ 「RC → Android → 機体」のパススルーは **無い**。
- `_onRcFsmState` の条件（逆アセンブル）: 3 つの前提フラグを通したうえで **`keyId == 132` かつ `eventType == 33`** のときだけ `DeviceHighlightEvent(timestampMs)` を発火（同一 `timestampMs` は重複排除）。他のキーは無視。
  → 送信機のボタンが「RC → 機体 → アプリ」経由でアプリ機能（録画のハイライト）を叩く**唯一の経路**。制御（走行）には効かない。【確定】
- `/cmd/rc/function_key_mode`（`{cameraBtnMode, gameBtnMode, updateFlag, reserved}`）は送信機のファンクションキーのモード（カメラ系 / ゲーム系）設定。ただし **アプリの Dart コードからは呼ばれていない**（生成オペレーション定義と、失敗ログ `遥控器功能键设置失败` があるだけ）。【高確】

#### アービトレーション — アプリは送信機に制御を譲らない【確定】

`control_loop.dart` には送信可否を決めるゲートがある。

```
enum _TickGate { disconnected, none, normal, override, remoteControl, seeding }
_currentTickGate(linkUp, remoteControlActive):
    linkUp == false      -> disconnected
    remoteControlActive  -> remoteControl   // 「送信機が操作中なので黙る」用
    ハンドオフ中         -> override
    seeding 中           -> seeding
    それ以外             -> normal
```

しかし **`remoteControl` はこのビルドでは到達しない**:

| 実装 | 実体 |
|---|---|
| `Transport::isRemoteControlActive`（transport.dart, 0xca7c2c） | `getIt<IRemoteControlState>()` を呼ぶが **戻り値は常に `false`**（スタブ） |
| `DeviceLinkManagerImpl::isRemoteControlActive`（0x8f400c） | 両分岐とも **`false` を返すだけ** |
| DI 実装（`core/extensions/i_remote_control_state.dart`） | `InactiveRemoteControlState`（空の const クラス = 常に非アクティブ） |
| 唯一の呼び出し元（`ControlLoop._tick`） | 2 番目の引数に **`false` を定数で渡している** |

→ **アプリ側に「RC が操作中なら黙る」ロジックは実装されていない**（スタブ）。制御権の裁定は **機体側だけで行われている**。
→ PC からも同じ: 20 ms で制御語を送り続け、採否は機体が決める。**PC 側に RC 検知の実装は不要**（真似すべきアプリ側ロジックが存在しない）。

`override` ゲートは離散動作用。`device_control_runtime_capability.dart` が `beginMovementHandoff` / `completeMovementHandoff` / `pushImmediateInputPacket` / `pushImmediateCenterPacket` / `setTickOverride` を公開し、`preset_action_manager.dart`（プリセット動作）と `device_body_control_capability.dart`（立ち上がり / 収納）が使う。20 ms ハートビートを一時的に上書きして**即時パケット**を送る仕組みで、PC で作り込む場合も同じ構造が要る。

なお `RealRlStatus.controlSource`（real_rl の制御元）は **アプリの Dart からは読まれていない**（表示 / 未使用）。「機体が今どの制御元を選んでいるか」を PC から知りたければ、実機でこのフィールドを観測するのが第一候補（値の意味は未確定）。

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

### 5.1 スティック値 → 実速度の換算【推定】

制御語に入るのは **-1.0〜1.0 の正規化値**（§3.4 で 0..2000 / 0..62 に変換）だけで、
**m/s や rad/s への換算は機体側（ファーム）にある**。アプリはその係数をパラメータとして持つ:

| 制御語のフィールド | ±1.0 が意味する実速度（推定） | 根拠 |
|---|---|---|
| `channel0`（前後） | ±`max_speed` = **±2.0 m/s** | パラメータ id 1 |
| `channel1`（旋回） | ±`max_yaw_rate` = **170 deg/s**（≒2.97 rad/s） | パラメータ id 2（`radToDeg` 付き＝保存値は度） |

- `power_response_level`(2.0) はゲームパッド入力のカーブ用、`circling_linear_velocity`(0.5) は
  周回動作用と推定。通常のスティック入力の換算には使わない【推定】。
- PC から見ると **1.0 が最大**で、それ以上は `map2000` が飽和する。**実際に何 m/s 出ているかは
  PC 側の制御語からは分からない**（機体のテレメトリを見るしかない）。→ §8 #13

**実機での較正手順（#13）**:

1. 広い場所で HB を **`channel0 = +1.0` 固定・20ms 周期**で 3〜5 秒送る（§3.5 のサンプルをそのまま使う。
   止めるときは 0 を送り続ける）。
2. 並行して Zenoh の `/status/robot/status`（`RobotStatusData`）を購読する。使えるフィールド:
   - `tripOdomDis`（double）… 累積走行距離。**単位は m と推定**（確実なのは「距離」であること）
   - `velocity`（sint32）… 瞬間速度。スケール（mm/s 等）が不明なので距離の方が確実
3. **`tripOdomDis` の差 ÷ 経過秒 = 実速度 [m/s]**。これが `channel0 = 1.0` に対応する速度。
   例: 3.0 秒で 4.5 m → 1.5 m/s（`max_speed` = 2.0 と一致しないなら、加速の遅れか係数違い）
4. 旋回は `/status/robot/status` に角度が無いので、`channel1 = +1.0` を送って**その場で 1 周に
   かかる秒数**を測る（360 ÷ 秒数 = deg/s）。`max_yaw_rate` = 170 deg/s と比較する。
5. 求めた係数を `beni_control` の `spec.py` に反映する（シム側の速度範囲と突き合わせる）。

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
| 1 | 制御語の**符号の向き**（+ = 前進 / 右旋回 か） | 実機で ch0/ch1 を ±1 にして動作を観察（軸の割当自体は §3.3 で確定済み） |
| 2 | `roll` の実挙動（±1 でどれだけ傾くか） | 実機で `roll` を ±1 にして姿勢を観察しつつ PCAP で制御語を差分観測 |
| 3 | `ispause` が `isEmergencyStopped` か、`no_liveview` が `videoEnabled` の反転か | `device_body_control_capability.dart` の該当 setter を追う / 実機で差分 |
| 4 | `rcSwitch`（パッド L1/LB）の実動作 | 実機で押して PCAP（制御語のどのビットが変わるか）+ 画面の変化 |
| 5 | `RealRlStatus.controlSource` の値の意味（現在アプリ未使用） | 実機で real_rl 実行中に `/status/**` を購読して値を観察 |
| 6 | `_onRcFsmState` の `keyId=132` / `eventType=33` が送信機のどのキーか | 送信機のボタンを 1 つずつ押して `/status/rc/fsm_state` をログ |
| 7 | `+1` bit1（フラグメント）がワイヤ上で 0 か 1 か | **モック機体**で観測（[docs/mock-robot.md](mock-robot.md)）。PCAP でも可 |
| 8 | ACK ブロック末尾の u16/u32 の意味（una / bitmap） | **モック機体**で `--gap-at` を使いビット対応を確定。PCAP で sn と突き合わせても可 |
| 9 | Zenoh リンクフレームの完全フォーマット | **モック機体**で magic / seq / 中身を観測（全種は機体の応答待ち）。PCAP でも可 |
| 10 | keyexpr の完全形（`device/cmd/...` か `device/<sn>/cmd/...` か） | **モック機体**の文字列抽出 / PCAP の zenoh フレーム内のキー文字列を直接読む |
| 11 | UCP の暗号化有無 / 未認証拒否の有無 | 素で HB を送ってみる（機体が必要） |
| 12 | ポート 400 / 500 の用途 | ネイティブの `__ucp_init` 呼び出し元を追う |
| 13 | **スティック値（正規化 -1..1）→ 実速度（m/s, rad/s）の換算係数** | §5.1 の較正手順（`/status/robot/status` の `tripOdomDis` で実測） |

### 実機なしで進められる項目（モック機体）

#7〜#10 は **機体が無くても**取れる。PC 側に「機体のふり」をするサーバを立てれば、アプリは実機と同様に接続して全トラフィックを PC に流す。

**実装済み**: [`src/beni_control/mock_robot.py`](../src/beni_control/mock_robot.py)（使い方は [docs/mock-robot.md](mock-robot.md)）

```sh
uv run beni-mock-robot                  # udp 0.0.0.0:9007
uv run beni-mock-robot -v --report-out cap.txt
uv run beni-mock-robot --reply-mode hb --gap-at 5,20   # #8 のビット対応を確定させる
```

やっていること: `ucp:sync` に `ucp:ack` を返してセッションを成立させ、以後の全データグラムを UCP として解釈して
制御語（HB）はビット名付きで表示、Zenoh フレームはマジック・シーケンス・中の文字列を表示。終了時に
#7〜#10 の答え合わせレポートを出す。コーデックは [`src/beni_control/ucp.py`](../src/beni_control/ucp.py)（PC クライアントでも使い回す）。

アプリに機体を `192.168.7.1` として認識させる必要があるため、PC を AP（またはテザリング）にして `192.168.7.1` を名乗るのが手軽。
手順は [docs/mock-robot.md](mock-robot.md) §2。

限界: **UCP より手前（BLE の発見・Wi-Fi 接続）でアプリが止まると `ucp:sync` すら来ない**（モックの出番が無い）。
UCP が繋がっても Zenoh のセッション確立は機体の応答が要るので、アプリの上位層は止まりうる（keyexpr が `put` に乗らない）。
実機でしか埋まらない項目（未認証セッションの受付・軸の符号・速度換算）はここでは埋まらない。詳細は [docs/mock-robot.md](mock-robot.md)。

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

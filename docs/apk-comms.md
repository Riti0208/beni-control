# Mondo App (Beni) APK 通信解析メモ

> **PC から Beni を操作するための実装情報は `robot-protocol.md` に全部書いた**（UCP ポート/ハンドシェイク/ヘッダ、制御語のビット配置と変換式、送信周期、protobuf 型対応表、Python サンプル、実機確認手順）。本ファイルは通信全体の俯瞰。

対象: `MondoApp.apk`（`com.mondorobotics.mondo.app` 1.0.35）
解析日: 2026-10-18
結論を一行で: **「クラウド(REST) ＋ ロボット直結(UDP上のZenoh + BLE + ローカルHTTP) ＋ 中継(MQTT) ＋ 通話SDK(Agora) ＋ 中国系解析SDK(Aliyun/Umeng)」の5〜6系統**。
ロボット制御の本体はクラウドではなく、**スマホ→ロボットのローカルUDP(Zenoh over UCP, protobuf)**。

---

## 1. APK の素性

| 項目 | 値 |
|---|---|
| パッケージ | `com.mondorobotics.mondo.app` (Activity は `com.mondorobotics.mondoapp.MainActivity`) |
| ラベル / 版 | Mondo / versionName 1.0.35 (versionCode 1000035) |
| SDK | minSdk 26, targetSdk 36 |
| サイズ | 231MB (arm64-v8a のみ) |
| 構成 | Flutter (`libapp.so` 22MB) + Kotlin/Java (`classes.dex` 23MB) + ネイティブSDK + Lynx (ByteDance製の動的UIエンジン) |
| 難読化 | **SecNeo (com.secneo.apkwrapper.AW/AP/CP)**。`assets/info.y` は anti-Frida / anti-Xposed / dex隠蔽の設定。`libDexHelper.so` 系 |
| 平文通信 | `android:usesCleartextTraffic="true"` だが network_security_config で **平文OKは 192.168.7.1 / 127.0.0.1 のみ** |
| 主要ネイティブ | `libapp.so`(Dart), `libmondo_native.so`(UCP/Zenoh/H.265/Opus), `libagora-rtc-sdk.so`, `libflutter.so`, `liblynx*.so`, `librum-native-core.so`, ffmpeg, MLKit barcode |
| デバッグ痕跡 | `libmondo_native.so` は `D:/android/mondo-app/build/.cxx/debug/...` (=Debugビルドのまま出荷) |

製品コード名: アセットに `assets/device/lz100/...` と `img_recording_device_beni_lite.png` → **LZ100 = Beni**, Lite 版あり。BLE の country code / manufacturer data も `lz_*` 系。

---

## 2. 通信の全体マップ

```
                    ┌──────────────── Mondo クラウド ────────────────┐
                    │ global-api.mondorobotics.com  (REST, JWT)      │
                    │   ├ /account/v1/*   /app/v1/*  /userprofile/v1 │
                    │   ├ /api/v1/*  (entitlements, mondocut, mqtt)  │
                    │ cn-cdn / us-cdn.mondorobotics.com (Lynxバンドル)│
                    │ mondocut.mondorobotics.com (AI編集)            │
                    └───▲──────────────▲───────────────▲────────────┘
        HTTPS(Dio)      │              │ MQTT(mqtt_client)│ HTTPS(Agora RTC token)
   ┌────────────────────┴──────────────┴───────────────┴──────────────────┐
   │                         Mondo App (スマホ)                            │
   │  署名付きJWT, X-Region / X-App-* / X-Trace-Id ヘッダ                 │
   └───┬───────────────┬──────────────┬──────────────┬──────────────┬─────┘
       │ BLE (GATT)    │ UDP          │ HTTP         │ Agora RTC    │ 解析SDK
       │ FFF0 svc      │ UCP/Zenoh    │ 192.168.7.1  │ (映像/音声)   │ Aliyun RUM
       ▼               ▼              ▼              ▼              ▼
   ロボット(Beni)  ロボット(Beni)  ロボット内蔵   Agora雲        QuickTracking
   初期設定/      制御・状態・    HTTPサーバ     (通話)         Umeng/SLS
   国コード同期    映像/音声       8080/8083/8084               Cloudflare
                                                               Turnstile
```

```
付属 UWB 送信機 (RC) ──(UWB 無線。スマホを経由しない)──▶ ロボット
   アプリは /status/rc/* を購読して表示・較正するだけ（走行制御には関与しない）
```

---

## 3. 経路ごとの詳細

### (A) スマホ ↔ クラウド REST  — 主経路

- クライアント: `dio` + `http`。ホストは環境で切替（`global-api` / `global-apipre` / `global-apitest` / `us-apitest`）。
  ログイン時に `/account/v1/login/endpoint` で**リージョン別エンドポイントを動的取得**し、`endpoint_cache_v2_*` / `dynamic_endpoints_*` にキャッシュする。
- 認可: `Authorization: Bearer <JWT>`。`/account/v1/jwt/derive` で**用途別に派生トークンを発行**（WebView 用・MondoCut 用 = `mondocut_ticket_service`, `token_bridge_module`）。refresh は `/account/v1/login/refresh`。
- パスワードは `/account/v1/rsa/pub-key` の公開鍵で暗号化（`rsa_crypto.dart`, pointycastle）。
- 独自ヘッダ: `X-Region`, `X-App-Version`, `X-App-Build-Id`, `X-App-Package`, `X-Trace-Id`, `X-Mondocut-Billing-Ver`。
- ローカルDB: `drift` + `sqlite3`（テレメトリ・ギャラリー・アウトボックス），トークンは `flutter_secure_storage`。

主要エンドポイント（全部で 200 弱、抜粋）:

| 分類 | パス |
|---|---|
| 認証 | `/account/v1/login`, `/login/refresh`, `/login/endpoint`, `/jwt/derive`, `/rsa/pub-key`, `/oauth/{google,apple,wx}/verify`, `/verify/{email,phone}/code/{send,verify}`, `/verify/check-account`, `/verify/pwd`, `/verify/set-password` |
| アカウント | `/account-manage/v1/{info,logout,delete,password/change/*,phone/change/*}` |
| 端末 | `/app/v1/user-device/list`, `/{sn}/detail`, `/{sn}/binding-account`, `/{sn}/unbind`, `/{sn}/updateName`, `/{sn}/factory-reset`, `/{sn}/deviceActive/status` |
| 端末セキュリティ | `/app/v1/user-device/{sn}/security/{binding/confirm,activation/activate,device-attestation/challenge}` |
| アクセサリ | `/app/v1/user-device/{sn}/accessory/activation/report` |
| ファイル/更新 | `/app/v1/files/{app-update/check, package/latest, package/list, getDownloadUrl, getUploadUrl, getBugTagConfig}`, `/app/v1/robot/user/files/list` |
| OTA | `/v2/upgrades/`, `/firmware/.` |
| フィードバック | `/app/v1/files/feedback/{fault,opinion}`, `/app/v1/feedback/survey/{query,confirm}`, `/api/v1/feedback/{question/template/get,answers/get,answers/submit}` |
| テレメトリ | `/status/telemetry/event`(UCP側), `/api/v1/mqtt/robotConnect`, `/api/v1/mqtt/robotDisconnect` |
| MondoCut (AI編集) | `/api/v1/mondocutapp/{user,user-test,workflow,tasks,footage/footage/upload/*}`, `/api/v1/mondocut/points/*` |
| 特典/ポイント | `/api/v1/entitlements{,/grants,/ledger,/activation-observations}` |
| ユーザー | `/userprofile/v1/profile`, `/userprofile/v1/robot-mileage/records` |
| 地域 | `/geo/v1/country` |
| その他 | `/platform/auth`, `/auth/region` |

- アップロード/ダウンロードは **Alibaba OSS**（`sts_api` で一時クレデンシャル→`oss_api` でマルチパート署名付きURL、`x-oss-process` ヘッダ使用）。動画素材アップロードも OSS 経由。
- WebView: `mondo_webview` が **トークンを JS に注入**して MondoCut 等の Web 画面を開く（`mondocut.mondorobotics.com`, `app.mondorobotics.com/wechat/`）。Figma の asset URL が残っているのは設計時の名残。

### (B) スマホ ↔ ロボット： Zenoh over UCP (UDP)  — **制御の本体**

- `libmondo_native.so` に **`mdZenohChannel*` / `mdZenohOverUcp*` と zenoh-pico 本体**（`z_subscriber_keyexpr` 等）を同梱。Zenoh を **自前の UDP トランスポート "UCP"** に載せている（`ucp_init_udp_client`, `ucp_send`, `udpserver`, heartbeat, `224.0.0.224:7446` は Zenoh の scout 用マルチキャスト）。
- Kotlin ブリッジ: `UcpManager.kt`, `NativeZenohBridge.kt`, `ZenohControlPlugin`（channel `mondoapp/zenoh_control`）。Dart 側は `md_device_protocol` + `device_protocol_core`。
- ペイロードは **protobuf**（`md_device_protocol/src/wire/generated_protobuf_wire.dart`）。`application/protobuf`。
- 映像/音声もネイティブで UCP 上を流れる: `native_rx_video_callback` / `native_rx_audio_callback`、**H.265 デコーダ + Opus**、`H265TextureRenderer`、`[VPL-TRACE]` ログ。
- 鍵空間は `/cmd/...` `/status/...` `/data/...` `/media/...`。ワイルドカード `device/**` が予約（= key は `device/<sn>/cmd/...` 形式と推定）。**検出した topic は 234 個**（下記内訳）。

| prefix | 数 | 例 |
|---|---|---|
| `/cmd/*` | 99 | `common/ping_req`, `real_rl/control_request`, `security/device_attest_req`, `ota/upgrade_control_req`, `tracking/ctrl_cmd`, `camera/unified_cmd`, `game/control_req`, `robot/sn_req` |
| `/data/*` | 69 | `model/robot_body_state`, `model/control_input_frame`, `model/body_action`, `model/device_telemetry`, `model/firmware_package_info` |
| `/media/*` | 36 | `files`, `health`, `sessions`, `models/media_item` |
| `/status/*` | 28 | `robot/status`, `real_rl/status`, `game/status`, `telemetry/event`, `gateway/telemetry_event`, `hms/event_broadcast`, `ota/upgrade_progress` |

**注目: 走行指令は `/cmd/real_rl/control_request`**（`RealRlControlRequest` → `/cmd/real_rl/control_ack`、`/status/real_rl/status` で状態）。"real (機体) + RL" = **機体側の学習済み制御器への高レベル指令**と読める。open-beni の「機体で policy を回してアプリは vx/wz を送るだけ」という構成と整合的。ゲーム（綱引き/宝探し/モーションセンス）も `/cmd/game/*` `/status/game/*` で同居。

**注意（別系統）**: この UCP 経路とは別に、**付属の UWB 送信機（RC）は機体と直結**しておりスマホを経由しない。アプリは `/status/rc/*`（`joystick_raw` / `fsm_state` / `joystick_calib_progress`）と `/status/uwb/rc_status` を**購読するだけ**で、送るのは較正・バイブ・ファンクションキー設定のみ。スティック値を制御語に詰め直すコードも無く、制御権を RC に譲る判定（`isRemoteControlActive`）もスタブで常に false。詳細は `docs/robot-protocol.md` §3.7。

その他、`/cmd/security/*`（activate / deactivate / attest / e2e_provision / crypto_info）と `/app/v1/.../device-attestation/challenge` を組み合わせた**アプリ・機体・クラウド間のattestationとE2E鍵プロビジョニング**、暗号化ファームウェア（`firmware_encryption_resolver`）がある。

### (C) スマホ ↔ ロボット： BLE (GATT) — 初期設定と予備トランスポート

- BLE UUID: service **`0000FFF0-...`**（+ 解決後 **`0100FFF0-...`**）、characteristic `FFF1/FFF2/FFF3`。`flutter_reactive_ble` + `RxAndroidBle`。
- 用途（ファイル名から）: `ble_probe_step` → `ble_connect_step` → **`ble_wifi_credential_step`（Wi-Fi の SSID/パスワードをBLEで渡す）** → `ble_country_code_*`（国コード同期）。
- `ble_characteristic_byte_transport` = **BLE も md_device_protocol のバイト輸送路**（Wi-Fi 前の初期設定に同じメッセージ体系を使う）。
- スキャンは manufacturer data / service data (`lz_manifest`) で Beni を判別。

### (D) スマホ ↔ ロボット： ローカル HTTP（テザリング/同一LAN）

機体 AP（ゲートウェイ 192.168.7.1）上の HTTP サーバ:

| ポート | 用途 |
|---|---|
| `:8080` | 機体の HTTP エントリ（`http://192.168.7.1:8080`） |
| `:8083` | ファイル系: `/files/params_export.json`, `/files/snapshot/by_event_id/`, `/files/` |
| `:8084` | メディアサーバ: `/media/health`, `/media/files/`, `/media/sessions/`, `/media/cancel-download` |
| `127.0.0.1:<動的>` | アプリ自身が立てる `local_video_server`（WebView へ動画を渡す） |

- **`DualNetworkManager` / `NetworkBoundHttp`（`bindProcessToNetwork` / `bindSocket`）**で、Wi-Fi(機体) とモバイル回線(クラウド) を**同時に使い分け**ている。クラウド通信は回線、機体通信は Wi-Fi にソケットを束縛。
- USB アクセサリモードも対応（`UsbManager.kt`, channel `mondo/usb`, `USB_ACCESSORY_ATTACHED`）= 有線での第4の経路。

### (E) MQTT — 中継系

- Dart の `mqtt_client`（`MqttServerClient`）があり、`domain/telemetry/services/telemetry_mqtt_{config,connection,reporter}.dart` が使用。
- 接続情報は REST `/api/v1/mqtt/robotConnect` / `robotDisconnect` から取得（= ブローカー/トピック/資格情報をクラウドが払い出す）。
- 用途: `TelemetryEventMqtt.fromEventTelemetry`, `buildTelemetryTopic`, `subscribeToAuthResponses` などから、**機体の解析イベント/テレメトリをクラウドへ中継**（および認証のやり取り）。ブローカーのホスト・ポートはハードコードされておらず API 応答依存。

### (F) Agora RTC — 映像/音声（通話SDK）

- `libagora-rtc-sdk.so` + `io.agora.rtc2`。Kotlin `AgoraRtcManager`、channel `mondo/agora_rtc`（`AGORA_RTC_CHANNEL`）。
- トークン/チャンネルは **REST `rtc_api`（`getRtcToken` / `getChannelName`）でクラウドから取得**して join。
- 役割は「インターネット越しのライブ映像/音声」（ペット見守り・遠隔）。同一LAN/機体直結時は (B) の UCP 上の H.265/Opus が使われる、という**二系統**。

### (G) 機体 → アプリ → クラウドのテレメトリ

- 機体は UCP で `/status/gateway/telemetry_event`（`GatewayTelemetryEvent`）や `/status/telemetry/event` を送り、アプリは `/cmd/gateway/analytics_event_receipt`（`accepted_delivery_ids`）で ack。
- アプリ内で sqlite（`telemetry_event_database`, `tracking_lost_event_database`）に溜め、`snapshot_upload_scheduler` / `snapshot_upload_client` / `TelemetryMqttReporter` で**後追いアップロード**。追尾ロスト時は機体の `/files/snapshot/by_event_id/` から画像を取得（`tracking_lost_event`, `gallery_video_database`）。
- 端末識別子・走行距離などは `/userprofile/v1/robot-mileage/records`, `board_telemetry` モデル経由。

### (H) サードパーティ（解析・その他）

| 送信先 | 内容 | 実体 |
|---|---|---|
| `proj-xtrace-...cn-shenzhen / us-west-1.log.aliyuncs.com` | アプリ性能・クラッシュ・ANR・H5 | **AlibabaCloudRUM / OTel**（`com/alibabacloud/rum`, `librum-native-core.so`, `assets/rum_android_config.json`, `mondoapp/rum_bootstrap`） |
| `quicktracking-qlc-ap-southeast-1.aliyuncs.com`, `quickaplus-he-api-cn-shanghai.aliyuncs.com`, SLS logstore `default-cms-1359540498859146-{cn-shenzhen,us-west-1}` | 操作イベント解析 | **Aliyun QuickTracking (aplus)**。appkey `ddx0a6q3yi@926a3dde2874e94691af0`(cn) / `@ee9a3edbe8923ae8c8c32`(us) |
| `ulogs.umeng.com`, `ouplog/plbslog.umeng.com` | 解析・エラー | Umeng SDK（classes.dex 内） |
| `o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js` | ログイン時のCAPTCHA | Aliyun AFTS（`AFTSO001`, `aliyun_captcha_sheet.dart`） |
| `challenges.cloudflare.com/turnstile/v0/api.js?render=explicit` | ログイン時のCAPTCHA（地域別） | **Cloudflare Turnstile**（`turnstile_captcha_sheet.dart`） |
| `open.weixin.qq.com`, `mp.weixin.qq.com`, `long.open.weixin.qq.com` | WeChat SDK（ログイン/共有） | fluwx / com.tencent.mm |
| `accounts.google.com/o/oauth2/revoke`, `www.googleapis.com/auth/games` | Google ログイン | google_sign_in |
| `feishu.cn` / `larksuite.com` | 不具合フィードバックの記録 | `feishu_bitable_reporter.dart`（Feishu Bitable へ投稿） |
| `www.figma.com/api/mcp/asset/...` | 残骸（設計時アセット参照） | 通信は行わない想定 |
| Firebase | **実質未使用**。`firebase-projectid` 等は MLKit barcode の依存として同梱（Analytics/Crashlytics/Messaging の登録は manifest に無し） | |
| MLKit barcode / Mobilenet} | QR・バーコード読み取り（モデルを `assets/mlkit_barcode_models/` に同梱 = オフライン動作） | |

プッシュ通知 SDK（FCM/Huawei/Xiaomi/Oppo/Vivo）は**入っていない** → 通知はポーリング/自前。

---

## 4. セキュリティ・認証まわり（通信に関係する部分）

- JWT(access/refresh) → **用途別に `jwt/derive` で派生**し、リクエスト毎に `Authorization` + `X-Region` を付与。
- パスワードは RSA 公開鍵で暗号化して送信。CAPTCHA は Aliyun AFTS と Cloudflare Turnstile を地域で使い分け（`regional_captcha_sheet`）。
- 端末紐付けは **attestation チャレンジ + activate + E2E provision**（`/cmd/security/*` を UCP 越し、クラウドは `/app/v1/user-device/{sn}/security/*` で裏取り）。ファームウェアは暗号化されて配布。
- 通信路は HTTPS／(機体内LANは平文HTTP)／UDP(UCP, Zenohレベルで暗号化は未確認)。

---

## 5. Beni を外から動かす観点のまとめ

- **クラウドを経由せずに走らせる道はある**: UCP/Zenoh はスマホ→機体の直結(UDP)。必要なのは (1) 機体の IP/ポート、(2) Zenoh key expression と protobuf スキーマ、(3) 認証（`/cmd/security/*` の activate/attest を通すか、既に紐付け済みセッションを使うか）。
- まず触るべきは **`/cmd/real_rl/control_request` と `/data/model/control_input_frame` / `/data/model/robot_body_state`**。ここが「vx/wz 相当を投げて機体状態を受ける」部分（open-beni の UDP JSON と役割が同じ）。
- 送受信は全て protobuf なので、**`md_device_protocol` のスキーマを復元するのが最優先**。今回の文字列ダンプからモデル名（`control_input_frame`, `robot_body_state`, `body_action`, `real_rl_*` など）は取れているので、`libmondo_native.so` の protobuf ディスクリプタ or `libapp.so` の Dart 定数からフィールドを掘るのが次の手。
- 参考になる近道: 既存の公開実装・ライブラリ（`zenoh-pico` は公開、`md_device_protocol` は非公開）。実機が来たら **同一 Wi-Fi に繋いで機体へ UDP を投げ、Zenoh scout(224.0.0.224:7446) を観測**するのが最短。
- 未確認/要実測:
  1. ~~Zenoh key の完全形~~ → **解決**: プレフィクスは `device` を連結（例 `device/cmd/common/ping_req`）。購読は `device/**`。区切り/SN 有無は実機で最終確認
  2. ~~UCP の本番ポート~~ → **解決**: **UDP 9007**（`socket_client(0, ip, 9007)` ハードコード）。ハンドシェイクは `ucp:sync` → `ucp:ack`。詳細は `robot-protocol.md`
  3. 平文 HTTP `http://nlb-*.cn-shenzhen.nlb.aliyuncsslb.com:18891`（2つ）の用途 — クラウドの中継/ゲートウェイ候補。Dart 側にしか無く、cleartext 制限と矛盾するので**ネイティブ/生ソケットで使われている可能性**が高い
  4. MQTT のブローカー・トピック実値（API 応答依存）
  5. UCP の暗号化有無（暗号化は見当たらず【推定】）

---

## 付録: 解析に使った成果物

解析は使い捨ての環境（スクラッチパッドと `/tmp`）で行った。以下は成果物の**名前と内容**の記録で、このリポジトリには含まれていない。

| ファイル | 中身 |
|---|---|
| `apk/libapp.strings` (1.4MB) | Flutter snapshot の全文字列（URL, APIパス, クラス名, protobufモデル名） |
| `apk/native.strings` | `libmondo_native.so` の文字列（UCP/Zenoh/topic, JNIシンボル） |
| `apk/keys.txt` (234行) | Zenoh topic 一覧 |
| `classes_strings.txt` | `classes.dex` の文字列（SDK 一覧・appkey 等） |
| `jadx_res/resources/AndroidManifest.xml` | デコード済み manifest（権限・コンポーネント） |
| `apk/unzipped/` | APK 展開（`bledata.proto`, `assets/rum_android_config.json` など） |

# beni-control

Mondo Robotics の Beni を PC から動かすためのコントローラー。実機の公式SDKはまだ無いので、今は
[open-beni](https://github.com/Mondo-Robotics/open-beni)（MIT）のロボットモデルと学習済み制御プログラムを
MuJoCo 上で動かしている。実機用の `beni-real` もあり、操作側（UDP JSON・teleop）はどちらにもそのまま使える。

実機の通信仕様（APK を逆解析して判明したもの）は [docs/robot-protocol.md](docs/robot-protocol.md) にまとめてある。通信全体の俯瞰は [docs/apk-comms.md](docs/apk-comms.md)。

実機が無くてもアプリと通信して未確定項目を潰せる「機体のふり」ツールがある: [docs/mock-robot.md](docs/mock-robot.md)（`uv run beni-mock-robot`）。

## セットアップ

```sh
./scripts/fetch_open_beni.sh   # モデルと policy.onnx を third_party/ に取得（コミット固定）
uv sync
```

## 動かす

```sh
uv run mjpython -m beni_control.sim_main   # ビューアー付き（macOS は mjpython が必要）
uv run beni-sim --headless                 # ウィンドウなし
uv run beni-teleop                         # 別ターミナルでキーボード操作
```

teleop のキー: `w/s` 前後速度、`a/d` 旋回、`space` 停止、`q/e` 首、`c` 首を自動に戻す、`z/x` 車高、`r` リセット、`g` 実機のロック解除/ロック、`Esc` 終了。

## 実機を動かす（未検証）

PC を機体の Wi-Fi に繋いでから、`beni-sim` の代わりに `beni-real` を起動する。teleop など操作側はそのまま使える。

```sh
uv run beni-real            # 機体 192.168.7.1:9007 に接続
uv run beni-teleop          # 別ターミナル。g でロック解除してから w/a/s/d
```

- **起動直後はロック状態**（アプリが機体の状態を読めなかったときと同じ「lock+pause」）。`{"cmd": "arm"}`（teleop の `g`）で初めて走る。
  `{"cmd": "disarm"}`、操作側から1秒指令が途絶えたとき、終了時（Ctrl-C / SIGTERM）には再びロックする。
- 機体からの応答が3秒途絶えるとロックして再接続する。
- 指令は 50Hz の 16 バイト制御語に変換して送る（`docs/robot-protocol.md` §3）。`vx` は ±2.0 m/s、`wz` は ±170°/s を
  スティック最大として割り算しているが、この換算・軸の符号・認証なしで受け付けるかは**実機で未確認**（§8 #1, #11, #13）。
  符号が逆なら `--invert-vx` / `--invert-wz`、換算は `--vx-full` / `--wz-full` で直す。
- 車高は機体の現在値を読めないので `--height-raw`（4〜31、既定 16）で固定。`height` 指令は実機では無視する。
  速度域は `--gear`（0〜2、既定 0 = 最低）。
- **最初は車輪を浮かせて**、ロック解除 → ごく小さい `vx` で向きを確認すること。

実機なしの通し確認はモック機体相手にできる:

```sh
uv run beni-mock-robot --host 127.0.0.1 --port 19007
uv run beni-real --robot 127.0.0.1 --robot-port 19007
uv run beni-teleop
```

## UDP インターフェイス（`udp://127.0.0.1:9870`）

JSON を送ると動く。指定しなかった項目は前の値を保つ。

```json
{"vx": 0.8, "wz": 0.5, "head_yaw": 0.3, "height": 0.11}
{"head_yaw": null}
{"cmd": "reset"}
{"cmd": "arm"}
{"cmd": "disarm"}
```

| 項目 | 範囲 | 意味 |
|---|---|---|
| `vx` | -2.0〜2.0 m/s | 前後速度 |
| `wz` | -5.0〜5.0 rad/s | 旋回速度（左が正） |
| `head_yaw` | ±4.16 rad / `null` | 首の角度。`null` で制御プログラム任せ |
| `height` | 0.10〜0.12 m | 車高 |
| `pitch` / `roll` | ±0.1 rad | 胴体の傾き |

- **0.5秒指令が来ないと `vx`/`wz` は0に戻る**。走らせ続けるなら20Hz程度で送り続ける。
- 送ってきた相手には20Hzでテレメトリ（IMU、関節角、シミュレーター上の位置・速度）を返す。`beni-real` はリンクの状態（armed / seeding / 送受信数）を返す。
- `reset` はシミュレーターだけ、`arm` / `disarm` は `beni-real` だけが使う。

```python
import json, socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _ in range(60):
    s.sendto(json.dumps({"vx": 0.5, "wz": 1.0}).encode(), ("127.0.0.1", 9870))
    time.sleep(0.05)
```

## 構成

```
操作側 (teleop / 自作プログラム)
   │ UDP JSON
command_server.py   指令の受信・ウォッチドッグ・テレメトリ送信
   │ Command
controller.py       観測値40次元を組み立て → policy (GRU-512, 100Hz) → 関節目標値
   │ JointTargets（脚・首は角度、車輪は速度）
backend.py          RobotBackend: SimBackend（MuJoCo）
```

実機は関節を直接動かせず、スティック相当の高レベル指令しか受け付けない（バランス制御は機体の中で動く）。
そのため実機では `controller.py` 以下を使わず、`Command` をそのまま制御語にする:

```
command_server.py → real_main.py（ロック/解除・変化率制限・換算）→ robot_link.py（UCP: ハンドシェイク・シード・50Hz HB）→ 機体
```

観測値・モーターモデル・指令遅延は open-beni の `web/game/physics.mjs` と学習設定から移植した（数値は `spec.py`）。

## 分かっている制限

- **首の指令**: 学習時の報酬にジンバル指令が使われていないので、制御プログラムは首の指令に従わない。
  `head_yaw` を指定すると、首の目標角度だけ制御プログラムの出力を上書きする。
- **ジャンプ未対応**: open-beni のジャンプ制御（`jump-controller.mjs`）はまだ移植していない。
- **シミュレーターの値は実機と合っている保証が無い**: モーターの値は open-beni の学習設定のもの。

## テスト

```sh
uv run pytest
```

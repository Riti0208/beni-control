# beni-control

Mondo Robotics の Beni を PC から動かすためのコントローラー。実機の公式SDKはまだ無いので、今は
[open-beni](https://github.com/Mondo-Robotics/open-beni)（MIT）のロボットモデルと学習済み制御プログラムを
MuJoCo 上で動かしている。実機が手に入ったら `RobotBackend` の実機版を書けば、制御部と操作側はそのまま使える。

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

teleop のキー: `w/s` 前後速度、`a/d` 旋回、`space` 停止、`q/e` 首、`c` 首を自動に戻す、`z/x` 車高、`r` リセット、`Esc` 終了。

## UDP インターフェイス（`udp://127.0.0.1:9870`）

JSON を送ると動く。指定しなかった項目は前の値を保つ。

```json
{"vx": 0.8, "wz": 0.5, "head_yaw": 0.3, "height": 0.11}
{"head_yaw": null}
{"cmd": "reset"}
```

| 項目 | 範囲 | 意味 |
|---|---|---|
| `vx` | -2.0〜2.0 m/s | 前後速度 |
| `wz` | -5.0〜5.0 rad/s | 旋回速度（左が正） |
| `head_yaw` | ±4.16 rad / `null` | 首の角度。`null` で制御プログラム任せ |
| `height` | 0.10〜0.12 m | 車高 |
| `pitch` / `roll` | ±0.1 rad | 胴体の傾き |

- **0.5秒指令が来ないと `vx`/`wz` は0に戻る**。走らせ続けるなら20Hz程度で送り続ける。
- 送ってきた相手には20Hzでテレメトリ（IMU、関節角、シミュレーター上の位置・速度）を返す。

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
backend.py          RobotBackend: 今は SimBackend（MuJoCo）。実機版をここに足す
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

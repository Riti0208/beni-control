# Beni 調査メモ

調べた日: 2026-10-18

## 製品
Mondo Robotics のカメラロボット。2026年7月にKickstarterで発表。開発チームは元DJIのメンバーと報じられている。

| 項目 | 内容 |
|---|---|
| 走り方 | 脚の先に車輪が付いた二輪。自分でバランスを取る。膝に受動バネ |
| 最高速度 | 約29 km/h（17.9 mph） |
| ジャンプ・段差 | 約25cm。階段を上れる。転んでも起き上がる |
| サイズ・重さ | 21.5 × 18 × 18 cm、1.75 kg |
| カメラ | 4K/30fps、3K/60fps、1080p/100fps |
| 障害物を見るカメラ | 視野150°が2つ |
| 保存先 | 本体32GB＋microSD |
| バッテリー | 交換式。1本約1.5時間、満充電まで約4.5時間 |
| 無線 | Wi-Fi 6、Bluetooth 5.4 |
| 操作 | スマホアプリ「Mondo App」（iOS / Android）、腕時計型のモーショントラッカー |
| 値段 | Kickstarter $549、一般販売 $799 の見込み |
| 出荷 | 2026年10月予定と報じられている |

- 追尾は本体の中だけで処理していて、ネット接続は不要。AI編集（MondoCut）だけクラウドを使う。
- 防水ではない。カメラ位置が低い。

## 開発者向けの状況
- **公式のSDK・APIは無い**。会社の回答は「Right now, we simply haven't reached any conclusions regarding developer tools, documentation, or access.」で、最初の出荷が終わってから検討するとのこと。
- 2026年5月、会社のアカウントが開発キットについて「actively thinking about」と回答。
- 「2027年第1四半期にAPI・開発キット」という話はHacker Newsのコメントでの伝聞で、元の発言は確認できていない。

## 連絡先・コミュニティ
- Discord: https://discord.com/invite/96MefHYE62
- Reddit: https://reddit.com/r/MondoRobotics
- サポート: support@mondorobotics.com
- チュートリアル: https://www.mondorobotics.com/pages/support/product-tutorials

## open-beni（公式の公開リポジトリ）
https://github.com/Mondo-Robotics/open-beni （MITライセンス）。`scripts/fetch_open_beni.sh` でコミット `4545b86` に固定して取得している。

中身は、MuJoCo用のモデル（`beni.xml`）、URDF、学習済みの制御プログラム（`.pt` / `.onnx` / `.mnn`）、学習コード（mjlab / MuJoCo Warp）、ブラウザのレースゲーム。実機との通信方法は書かれていない。

### 関節
| 関節 | 動かし方 | トルク上限 | 指令の倍率 | 初期角度 |
|---|---|---|---|---|
| L1 / R1（股関節） | 角度（kp=0.5, kd=0.02） | 1.375 Nm | 0.25 | 0.3 rad |
| L2 / R2（膝） | 角度（kp=0.5, kd=0.02）＋受動バネ | 3.6 Nm（バネ込み4.6） | 0.25 | 0.5 rad |
| LW / RW（車輪） | 速度（kd=0.004） | 0.6 Nm | 20.0 | ― |
| head_yaw | 角度（kp=3.0）、可動範囲 ±5.2 rad | 0.1 Nm | 0.5 | 0 |
| head_roll | モーターなし。ジンバルが機械的に追従 | ― | ― | ― |

膝のバネのトルク: `0.22·sin(θ−1) + 0.15·sin(2(θ−1))` [Nm]

### 制御プログラム
- 1層のGRU（隠れ状態512）。入力 `obs[1,40]` と `h_in[1,1,512]`、出力 `actions[1,7]` と `h_out`。
- 100Hz（物理計算2ms × 5回）。指令の遅れ4〜10msを想定して学習。
- 出力の並び: `L1, R1, L2, R2, LW, RW, head_yaw`
- 目標値への変換: 脚と首は「初期角度＋倍率×出力」、車輪は「20×出力」の速度。

入力40次元の内訳:

| 位置 | 中身 |
|---|---|
| 0:3 | 胴体IMUの角速度 |
| 3:6 | 重力方向（IMUの姿勢から計算） |
| 6:9 | 速度指令（前後, 0, 旋回） |
| 9:14 | 関節角度−初期角度（L1, R1, head_yaw, L2, R2） |
| 14:21 | 関節速度（L1, R1, head_yaw, L2, R2, LW, RW） |
| 21:28 | 前回の出力 |
| 28:31 | 姿勢指令（pitch, roll, 車高）。普段は (0, 0, 0.11) |
| 31:33 | ジンバル指令（yaw, 有効フラグ）。普段は (0, 1) |
| 33:36 | 頭のIMUの角速度 |
| 36:40 | ジャンプ指令。0ならジャンプしない |

学習時の指令の範囲: 前後 ±2.0 m/s、旋回 ±5.0 rad/s、pitch・roll ±0.1 rad、車高 0.10〜0.12 m。

## シミュレーターで分かったこと
- 速度の追従は良い（1.0 m/s → 1.02 m/s、2.0 rad/s → 1.99 rad/s）。
- **首の指令には従わない**。ジンバル指令を使う報酬が学習に無いため。`head_yaw` を指定すると、首の出力だけ上書きしている。
- ジャンプは未移植（open-beniの `web/game/jump-controller.mjs` にある）。
- モーターの値は学習設定のもので、実機と合っているかは分からない。

## 出典
- https://www.mondorobotics.com/
- https://github.com/Mondo-Robotics/open-beni
- https://news.ycombinator.com/item?id=49463993
- https://www.kickstarter.com/projects/mondorobotics/beni-all-terrain-camera-robot
- https://www.gizmodo.jp/article/mondo-robotics-beni-2607/
- https://newatlas.com/consumer-tech/mondo-beni-robot-action-cam/
- https://the-gadgeteer.com/2026/07/10/mondo-robotics-beni-all-terrain-camera-robot-kickstarter/

# wan-pulse

マイクから犬の鳴き声をリアルタイムに拾い、**最終的には感情を推論する**ことを目指すシステムです。

このリポジトリは、その第一歩となる **「動く骨組み」** を実装しています。

```
マイク(M-305)
  └─ 音声ストリーム取得      sounddevice で連続受信        ← 常時動く・軽い処理
       └─ エネルギーで音を検知  RMS が閾値超え＝「鳴った」    ← 常時動く・軽い処理
            └─ 区間を .wav に保存  前後マージン付き              ← 鳴った時だけ・重い処理
                 └─ モデルで推論   （今後実装）
                      └─ 結果を記録・通知（今後実装）
```

つまり今できることは:

- マイクから音声を**連続で録音**する
- **無音はスキップ**する（エネルギーゲート）
- **鳴った区間だけ**を前後マージン付きで `.wav` に保存する

推論・通知はまだ含まれていません（保存された `.wav` がその入力になります）。

---

## 構成

| ファイル | 役割 |
| --- | --- |
| `wan_pulse/config.py` | サンプルレート・閾値・マージン等の設定（既定値の定義） |
| `wan_pulse/configfile.py` | `wan-pulse.toml` の読み込み・優先順位のマージ・run ログ出力 |
| `wan_pulse/ring_buffer.py` | 直近の音声を保持するリングバッファ（前マージン用の先読み） |
| `wan_pulse/gate.py` | エネルギーゲート（無音→鳴った→無音 を区間として切り出す状態機械） |
| `wan_pulse/writer.py` | 区間を日付フォルダ配下の `.wav` に保存（ファイル名に peak dBFS）＋推論結果の JSON サイドカー |
| `wan_pulse/capture.py` | sounddevice ストリーム → ゲート → 保存 →（任意）推論 を繋ぐ実行ループ |
| `wan_pulse/classify.py` | YAMNet(TFLite) で「犬か／発声タイプ」を推論（任意・Mac/RPi 共通） |
| `wan_pulse/cli.py` | `init` / `devices` / `monitor` / `run` / `classify` の各コマンド |
| `tests/` | マイク不要のロジックテスト（合成波形で検証） |

音声処理（`gate.py` / `ring_buffer.py`）は **音声 I/O から完全に分離**してあります。
そのため、マイクや PortAudio が無い環境（CI や Mac での開発）でも、合成データで
テスト・開発を進められます。

---

## 動作環境

- **本番環境**: Raspberry Pi 4 系 + Ubuntu
- **開発環境**: MacBook Pro + macOS

どちらも Python 3.9 以上が必要です。

---

## セットアップ

### 共通（Python パッケージ）

仮想環境を作ってインストールします。

```bash
python3 -m venv .venv
source .venv/bin/activate

# どちらでも可
pip install -e .            # pyproject.toml を使う（wan-pulse コマンドが入る）
# または
pip install -r requirements.txt
```

`sounddevice` / `soundfile` は OS 側のオーディオライブラリ（PortAudio / libsndfile）に
依存します。OS ごとに以下を先に入れてください。

### macOS（開発環境）

[Homebrew](https://brew.sh/) で PortAudio を入れておくと `sounddevice` が確実に動きます。

```bash
brew install portaudio libsndfile
pip install -e .
```

> 初回実行時、macOS が**マイクの使用許可**を求めます。
> 「システム設定 → プライバシーとセキュリティ → マイク」で、実行元のターミナル
> （Terminal / iTerm / VS Code など）を許可してください。許可しないと無音しか録れません。

### Raspberry Pi 4 + Ubuntu（本番環境）

PortAudio と libsndfile を入れます。

```bash
sudo apt-get update
sudo apt-get install -y python3-venv libportaudio2 libsndfile1

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

M-305 のような USB マイクは挿すだけで認識されることがほとんどです。
認識されているかは次の「デバイス確認」で確かめられます。

---

## 使い方

インストール後は `wan-pulse` コマンド、または `python -m wan_pulse` で実行できます。

### 0. 設定ファイルを作る

チューニング値は毎回フラグを打ち直さなくても、**TOML 設定ファイル**で管理できます。

```bash
wan-pulse init        # ./wan-pulse.toml を生成（コメント付き）
```

生成される `wan-pulse.toml` を編集して保存すれば、次回 `run` から反映されます。
設定の優先順位は **組み込み既定値 ＜ wan-pulse.toml ＜ CLI フラグ**。
つまり普段はファイルで管理し、その場限りの上書きだけフラグで渡せます。

> 初期テンプレートは `threshold_db` を **低め（-60）** にしてあります。
> これは**「鳴いたのに保存されない」を防ぐため**で、まず広めに全部録ってから、
> 保存ファイルの `peak`（後述）を見て閾値を上げていく、という流れを想定しています。

### 1. デバイスを確認する

接続されているマイクの一覧と、デフォルト入力デバイスを表示します。

```bash
wan-pulse devices
```

M-305 の行に出ている**番号**または**名前の一部**を、`wan-pulse.toml` の `device`
（または `--device`）に指定できます。

```toml
# wan-pulse.toml
device = "M-305"
```

### 2. まず低い閾値で録ってみる（チューニングの起点）

`init` 直後はそのまま起動して構いません。閾値が低いので、ちょっとした物音まで
拾って保存されます。**保存し過ぎでも OK** ── 次のステップで絞り込みます。

```bash
wan-pulse run
```

音が閾値を超えると録音区間が始まり、`post_margin` 秒ぶん静かになると区間が閉じて、
次の名前で保存されます（**ファイル名にピーク音量 `peak…dBFS` が入る**のがポイント）。

```
recordings/2026-05-30/bark_20260530_071530_812_peak-11.3dBFS.wav
```

```
[wan-pulse] config: wan-pulse.toml
[wan-pulse] logging to logs/wan-pulse.log
[wan-pulse] mic: USB PnP Sound Device  (device=USB PnP Sound Device, max in ch 1, native 48000 Hz)
[wan-pulse] listening: 16000 Hz, 1 ch, block 30 ms, threshold -60.0 dBFS
[wan-pulse] saving segments under ./recordings/  (Ctrl+C to stop)
[wan-pulse] run settings -> runs/run_20260530_071500.toml
[wan-pulse] saved bark_20260530_071530_812_peak-11.3dBFS.wav  (1.74s, peak -11.3 dBFS)
```

起動時に、**実際にどのマイクから録っているか**（解決後のデバイス名）が出るので、
`device` 指定が意図通りか確認できます。その回で使った設定は
`runs/run_YYYYMMDD_HHMMSS.toml` として 1 本残ります（再現用。`recordings/` とは別ディレクトリ。
変更は `run_log_dir` / `--run-log-dir`）。

### ログ

`run` / `monitor` / `classify` の処理ログは、コンソールと **ログファイルの両方**に
出ます（既定 `logs/wan-pulse.log`、5MB × 5 世代でローテーション）。

```bash
wan-pulse run                          # logs/wan-pulse.log に追記
wan-pulse run --log-file /var/log/wan-pulse.log
wan-pulse run --log-file none          # ファイル出力を止める（コンソールのみ）
wan-pulse run --log-level DEBUG        # 詳細度
```

ファイルにはタイムスタンプ付きで残ります:

```
2026-05-30 07:15:30,812 INFO    [wan-pulse] saved bark_...wav  (1.74s, peak -11.3 dBFS)
2026-05-30 07:15:31,002 INFO    [wan-pulse] classified bark_...wav  -> Bark 0.82 [dog:Bark 0.82]
```

### 3. peak を見て閾値を決める

保存された `.wav` を聞きつつ、ファイル名の `peak…dBFS` を眺めます。

- **犬の鳴き声**のファイルの peak（例 −12 dB 前後）と、
- **拾いたくない生活音**のファイルの peak（例 −45 dB 前後）

の**間**に `threshold_db` を置けば、鳴き声だけが残ります（例: `-30`）。
リアルタイムに数値を見たい場合は `monitor` も使えます。

```bash
wan-pulse monitor
```

```
  -58.3 dBFS |#########                               |
  -12.1 dBFS |##################################      |  <== over threshold
```

決めた値を `wan-pulse.toml` に書いて、本運用へ。

```toml
# wan-pulse.toml
threshold_db = -30.0
```

> **このゲートは「音の大きさ（RMS）」だけで判定します**。犬特化のフィルタは入って
> おらず、ドア・TV・話し声などの大きい音も拾います。これは意図的で、軽いゲートで
> 広く拾い、「犬か／どんな感情か」の判別は後段のモデル推論に任せる設計です。

> **マイクの感度（ゲイン）は OS 側で調整**します（ラズパイ: `alsamixer` / `amixer`、
> macOS: システム設定 → サウンド → 入力）。ゲインを上げると同じ音でも dBFS が大きく
> なるため、**先に OS でゲインを固定 → その状態で閾値を決める**のが順序です。

#### 設定できる値（`wan-pulse.toml` のキー / 対応する CLI フラグ）

| キー / フラグ | 既定値 | 説明 |
| --- | --- | --- |
| `threshold_db` / `--threshold-db` | `-40.0` | 検知の閾値（dBFS）。小さいほど敏感（テンプレートは `-60`） |
| `pre_margin_sec` / `--pre-margin` | `0.5` | 検知の**前**に残す秒数（鳴き始めの切れ防止） |
| `post_margin_sec` / `--post-margin` | `0.8` | この秒数ぶん静かになったら区間を閉じる |
| `min_segment_sec` / `--min-segment` | `0.3` | これより短い区間はノイズとして破棄 |
| `max_segment_sec` / `--max-segment` | `15.0` | 1 区間の最大長（暴走防止） |
| `samplerate` / `--samplerate` | `16000` | サンプルレート(Hz)。M-305 が拒否する場合は `44100` / `48000` |
| `channels` / `--channels` | `1` | 入力チャンネル数 |
| `block_ms` / `--block-ms` | `30.0` | 1 ブロックの長さ(ms)。検知の時間分解能 |
| `device` / `--device` | 既定入力 | 入力デバイス（番号 or 名前の一部） |
| `output_dir` / `--output-dir` | `recordings` | 保存先ディレクトリ |

全オプションは `wan-pulse run --help`、別ファイル指定は `--config path/to.toml` です。

---

## 編集後の再実行・更新の反映

`pip install -e .`（editable インストール）で入れていれば、コードを編集しても
**入れ直しは不要**です。Python は起動時にコードを読むので、反映するには
**実行中のプロセスを止めて起動し直すだけ**です。

| 何を編集したか | やること |
| --- | --- |
| `wan_pulse/*.py`（ロジック） | `Ctrl+C` で停止 → もう一度 `wan-pulse run`。再インストール不要 |
| `wan-pulse.toml`（設定値） | 同上。設定は**起動時に読む**ので再起動で反映 |
| 依存を追加 / `pyproject.toml` の `dependencies` を変更 | `pip install -e .`（or `-r requirements.txt`）してから再実行 |
| `[project.scripts]`（`wan-pulse` コマンド定義）やバージョンを変更 | `pip install -e .` で入れ直し |

ポイントは2つ:

1. **走らせっぱなしだと反映されない** → 必ず止めて起動し直す。
2. **`-e`（editable）で入れること**。`pip install .`（`-e` なし）だと編集のたびに
   入れ直しが必要です。

> インストールせずに `python -m wan_pulse run` で実行する手もあります。常にカレントの
> ソースを直接使うので、編集 → 即再実行ができます（依存が揃っていれば）。

### 本番（ラズパイ）での再デプロイ

```bash
git pull origin <ブランチ>     # コード取得
pip install -e .               # ★依存や entry point が変わった時だけ
# 動いている run を止めて
wan-pulse run                  # 再起動
```

コードだけの変更なら `git pull` → 止めて `run` の 2 手で十分です。24 時間つけっぱなしに
するなら **systemd サービス化**がおすすめで、`sudo systemctl restart wan-pulse` の 1 コマンドで
再起動できます。設定一式は [`deploy/`](deploy/) にあります（`sudo ./deploy/install-systemd.sh`）。

---

## 推論（犬判定・発声タイプ）※任意

保存した区間に対して「犬か？ どんな発声か（吠え／唸り／クンクン…）」を推論する
ステージです。**YAMNet（AudioSet 521 クラス）を TFLite で** 動かします。

- YAMNet の入力は **16kHz・モノ・float32** ＝ capture の出力そのままで前処理不要。
- TFLite は **Mac でも RPi でも同一 API**。バックエンドだけ環境で differ するので、
  **同じモデル・同じコードで両方エッジ推論**できます（開発＝本番）。
- 推論は「鳴った時だけ」走る重い処理。依存は**任意インストール**で、入れなければ
  これまで通り録音のみで動きます。

> ⚠️ これは「感情」ではなく **AudioSet のクラス**を出します。`Dog/Bark/Howl/Growling/
> Whimper(dog)…` を拾って「犬らしさ＋発声タイプ」を返す、感情ステージの土台です。

### セットアップ

```bash
# 1) 推論用の依存（Mac/RPi 共通。RPi は tflite-runtime でも可）
pip install -e ".[infer]"

# 2) モデルとラベルを取得（./models/ に保存）
./scripts/download-yamnet.sh
```

> モデル URL が移動していた場合は、[Kaggle Models の YAMNet (TFLite)](https://www.kaggle.com/models/google/yamnet/tfLite)
> から `.tflite` を落として `models/yamnet.tflite` に置けば OK です。

### 使い方

**オンライン（録音と同時に推論）** ── `wan-pulse.toml` の `[classify]` で `enabled = true`、
または `--classify` フラグ:

```bash
wan-pulse run --classify
```

推論は**バックグラウンドのワーカースレッド**で走り、録音（保存）は推論を待たずに
即座に行われます（重い推論がリアルタイム経路を止めない設計）。保存と推論で行が分かれます:

```
[wan-pulse] classifying each segment (ai_edge_litert.interpreter)
[wan-pulse] saved bark_20260530_171500_999_peak-12.3dBFS.wav  (1.56s, peak -12.3 dBFS)
[wan-pulse] classified bark_20260530_171500_999_peak-12.3dBFS.wav  -> Bark 0.82 [dog:Bark 0.82]
```

`.wav` の隣に同名の `.json`（サイドカー）が出ます:

```json
{ "file": "bark_...wav", "top_label": "Bark", "top_score": 0.82,
  "is_dog": true, "dog_label": "Bark", "dog_score": 0.82,
  "top_k": [["Bark", 0.82], ["Dog", 0.41], ...], "backend": "..." }
```

**オフライン（保存済み wav を後から／マイク無しの Mac で試す）**:

```bash
wan-pulse classify recordings/2026-05-30/bark_*.wav
wan-pulse classify some.wav --write-sidecar   # JSON も書き出す
```

### `[classify]` の設定（`wan-pulse.toml`）

```toml
[classify]
enabled = true                              # run --classify と同等
model_path = "models/yamnet.tflite"
labels_path = "models/yamnet_class_map.csv"
dog_threshold = 0.3                          # 犬クラスのスコアがこれ以上で is_dog=true
top_k = 5                                     # サイドカーに残す上位ラベル数
```

---

## テスト

マイク不要で、合成波形を使ってエネルギーゲートとリングバッファを検証します
（macOS / CI どちらでも実行可）。

```bash
pip install -e ".[dev]"
pytest
```

---

## 今後の予定

- ✅ **犬判定／発声タイプの推論**（YAMNet・上記「推論」セクション）
- ⬜ **ラベル付けの回し**: 保存 wav とサイドカーをレビューして「犬/非犬・タイプ」を
  蓄積し、自前データを作る
- ⬜ **感情推論**: まずは発声タイプ → 粗い状態のルール対応、データが貯まったら自前学習
- ⬜ 推論結果の **記録・通知**（DB/Slack 等）

推論結果は `on_segment` で即時に得られるので、通知や記録はこのフックに足せます。

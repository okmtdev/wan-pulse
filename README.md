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
| `wan_pulse/config.py` | サンプルレート・閾値・マージン等の設定（チューニングはここ） |
| `wan_pulse/ring_buffer.py` | 直近の音声を保持するリングバッファ（前マージン用の先読み） |
| `wan_pulse/gate.py` | エネルギーゲート（無音→鳴った→無音 を区間として切り出す状態機械） |
| `wan_pulse/writer.py` | 区間を日付フォルダ配下の `.wav` に保存 |
| `wan_pulse/capture.py` | sounddevice ストリーム → ゲート → 保存 を繋ぐ実行ループ |
| `wan_pulse/cli.py` | `devices` / `monitor` / `run` の各コマンド |
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

### 1. デバイスを確認する

接続されているマイクの一覧と、デフォルト入力デバイスを表示します。

```bash
wan-pulse devices
```

M-305 の行に出ている**番号**または**名前の一部**を、後述の `--device` に指定できます。

```bash
wan-pulse run --device 2
wan-pulse run --device "M-305"
```

### 2. 閾値を合わせる（キャリブレーション）

部屋の暗騒音とマイクによって適切な閾値（`--threshold-db`, dBFS）は変わります。
`monitor` でリアルタイムの音量を見ながら調整してください。

```bash
wan-pulse monitor
```

```
  -58.3 dBFS |#########                               |
  -12.1 dBFS |##################################      |  <== over threshold
```

静かな状態の値と、手を叩く／犬が鳴いたときの値の**間**に閾値を置くのがコツです。
例: 暗騒音 −58 dB、鳴き声 −15 dB なら `--threshold-db -40` あたり。

### 3. 録音する（無音スキップ＋区間保存）

```bash
wan-pulse run --threshold-db -40
```

音が閾値を超えると録音区間が始まり、`--post-margin` 秒ぶん静かになると区間が閉じて、
`recordings/YYYY-MM-DD/bark_YYYYMMDD_HHMMSS_mmm.wav` として保存されます。
保存のたびにファイル名・長さ・ピーク音量が表示されます。`Ctrl+C` で停止します。

```
[wan-pulse] listening: 16000 Hz, 1 ch, block 30 ms, threshold -40.0 dBFS
[wan-pulse] saving segments under ./recordings/  (Ctrl+C to stop)
[wan-pulse] saved bark_20260530_071530_812.wav  (1.74s, peak -11.3 dBFS)
```

#### よく使うオプション

| オプション | 既定値 | 説明 |
| --- | --- | --- |
| `--threshold-db` | `-40.0` | 検知の閾値（dBFS）。小さいほど敏感 |
| `--pre-margin` | `0.5` | 検知の**前**に残す秒数（鳴き始めの切れ防止） |
| `--post-margin` | `0.8` | この秒数ぶん静かになったら区間を閉じる |
| `--min-segment` | `0.3` | これより短い区間はノイズとして破棄 |
| `--max-segment` | `15.0` | 1 区間の最大長（暴走防止） |
| `--samplerate` | `16000` | サンプルレート(Hz)。M-305 が拒否する場合は `44100` / `48000` を試す |
| `--device` | 既定入力 | 入力デバイス（番号 or 名前の一部） |
| `--output-dir` | `recordings` | 保存先ディレクトリ |

全オプションは `wan-pulse run --help` で確認できます。

---

## テスト

マイク不要で、合成波形を使ってエネルギーゲートとリングバッファを検証します
（macOS / CI どちらでも実行可）。

```bash
pip install -e ".[dev]"
pytest
```

---

## 今後の予定（このリポジトリの範囲外）

- 保存した `.wav` を入力に、犬かどうか／感情を推論する **モデル推論**
- 推論結果の **記録・通知**

骨組みは整っているので、`Capture(on_segment=...)` のコールバックに推論処理を
差し込めば、保存と同時に推論を走らせる形に拡張できます。

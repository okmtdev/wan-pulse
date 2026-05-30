# 常時稼働（systemd）

ラズパイ等で wan-pulse を 24 時間動かし、起動時に自動起動・落ちたら自動再起動
させるための systemd 設定です。

| ファイル | 用途 |
| --- | --- |
| `install-systemd.sh` | この環境（ユーザー / リポジトリ / venv）に合わせて unit を生成し、インストール・有効化する自動スクリプト（推奨） |
| `wan-pulse.service` | 手動で置きたい人向けの unit テンプレート（3か所を編集して使う） |

## 前提

1. リポジトリを clone 済み。
2. venv にインストール済み（推奨）:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
3. 設定を済ませる:
   ```bash
   .venv/bin/wan-pulse init     # wan-pulse.toml を生成
   # 必要なら threshold_db / device などを編集
   ```

## 自動インストール（推奨）

リポジトリのルートで:

```bash
sudo ./deploy/install-systemd.sh
```

スクリプトがやること:

- 実行ユーザー（sudo を打った本人）・リポジトリの場所・`wan-pulse` の実体を自動検出
- そのユーザーを `audio` グループに追加（マイクアクセスに必要）
- `/etc/systemd/system/wan-pulse.service` を生成
- `daemon-reload` → `enable --now` で起動＆自動起動を設定

## 運用コマンド

```bash
systemctl status wan-pulse
journalctl -u wan-pulse -f          # ログ追尾
sudo systemctl restart wan-pulse    # コードや wan-pulse.toml を変えたら
sudo systemctl stop wan-pulse
sudo systemctl disable wan-pulse    # 自動起動をやめる
```

## 手動で入れる場合

`wan-pulse.service` の `User` / `WorkingDirectory` / `ExecStart` を自分の環境に
書き換えて配置します。

```bash
sudo usermod -aG audio "$USER"      # マイクアクセス権
sudo cp deploy/wan-pulse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wan-pulse
```

## 注意（音声デバイスの見え方）

- **ヘッドレス（Ubuntu Server 等）で ALSA 直**: 上記のシステムサービス（`audio`
  グループ所属のユーザーで実行）でそのまま動きます。
- **デスクトップ環境で PipeWire/PulseAudio をユーザーセッションで使っている場合**:
  システムサービスからはユーザーの音声サーバーが見えないことがあります。その場合は
  ユーザーサービスにすると確実です:
  ```bash
  mkdir -p ~/.config/systemd/user
  # User= 行を削った unit を ~/.config/systemd/user/wan-pulse.service に置く
  systemctl --user enable --now wan-pulse
  loginctl enable-linger "$USER"   # ログアウト後も動かす
  ```

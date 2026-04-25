# py_youtube_summary_cdp

WSL 上の **Chrome / Chromium** を **Chrome DevTools Protocol (CDP)** 経由で制御し、YouTube 動画ページの **YouTube チャット上の Gemini** にプロンプトを送り、返答（要約・字幕指示など）を取得する Python スクリプト集です。任意で、結果を **Gmail SMTP** でメール送信できます。

リポジトリの推奨実行環境は **WSL2（Linux ディストリビューション）** です。以下は WSL 前提の手順です。

---

## 前提

- WSL2 と Ubuntu 等（Windows 上で Linux ディストリが動くこと）
- リポジトリを **Windows ドライブ（`/mnt/c/...`）上にクローンしてある** 場合も動作しますが、Chrome のプロファイルはパフォーマンスと CDP 安定性のため **WSL 側の ext4 領域**（例: `~/.local/share/...`）に自動で寄せる場合があります。ログに案内が出るので、そのときは [プロファイルの同期](#chrome-プロファイルと-mnt-上のリポ)を参照してください。

---

## 1. システムに入れるもの（WSL 内）

### Google Chrome または Chromium

スクリプトは `YOUTUBE_CDP_CHROME_BIN` 未指定時、WSL 内の Chrome / Chromium のパスを探します。例（Debian / Ubuntu 系）:

```bash
sudo apt update
sudo apt install -y google-chrome-stable
# または
# sudo apt install -y chromium-browser
```

見つからない場合は、環境変数 `YOUTUBE_CDP_CHROME_BIN` に実行ファイルのフルパスを指定します。  
Windows 側の `chrome.exe` を使う例は `run_youtube_summary.sh` 内（`USE_GOOGLE_CHROME=1` 等）のコメントを参照してください。

### Python 用 venv モジュール

システム Python で PEP 668 が有効な場合、venv なしの `pip install` は失敗しやすいです。`python3` のバージョンに合わせてパッケージ名を選びます。

```bash
python3 --version
# 例: Python 3.12 の場合
sudo apt install -y python3.12-venv
```

### （任意）ポート占有解除用

既定では CDP 用ポート（多くの場合 9222）のリスナーを起動前に外します。`fuser` か `lsof` があると確実です。

```bash
sudo apt install -y psmisc   # fuser
# または
sudo apt install -y lsof
```

外したくない場合は `YOUTUBE_CDP_NO_PORT_KILL=1` を設定します。

---

## 2. リポジトリを WSL から開く

Windows のパス上にある場合の例:

```bash
cd /mnt/c/Users/<あなたのユーザー>/py_youtube_summary_cdp
```

---

## 3. 仮想環境（venv_wsl）の作成

同じフォルダに **Windows 用** の `.venv` があると、WSL から `bin/python3` が使えないことがあります。このリポジトリでは **Linux 専用**として `venv_wsl/` を使います。

```bash
chmod +x ./scripts/bootstrap_venv_wsl.sh
./scripts/bootstrap_venv_wsl.sh
```

成功すると `venv_wsl/` が作られ、依存は `requirements.txt` から入ります。有効化する場合:

```bash
source venv_wsl/bin/activate
```

`./run_youtube_summary.sh` は `venv_wsl/bin/python3` があると **自動でそちら**を使います（activate 不要でも可）。

### Playwright

依存に `playwright` が含まれます。本ツールは主に **既に起動した Chrome に CDP で接続**するため、ブラウザのダウンロードは必須ではないことが多いです。初回や接続まわりで不具合が出る場合は、venv を有効化したうえで次を試してください。

```bash
playwright install
```

---

## 4. 設定ファイル `.env`（リポジトリ直下）

`youtube_cdp.py` 起動時にリポジトリ直下の `.env` を読みます（`python-dotenv` が入っていればそれを利用し、未導入時は簡易パーサで補完します）。

### メール送信を使う場合

Gmail のアプリパスワード等で SMTP する想定の例（値は例です。実際の秘密情報は共有しないでください）:

```env
GMAIL_USER=youraddress@gmail.com
GMAIL_APP_PASSWORD=xxxx
MAIL_TO=destination@example.com
```

送信先は `MAIL_TO` のほか `RESULT_EMAIL_TO` / `EMAIL_TO` / `GMAIL_TO` なども解釈します。

### メールのオン・オフ

- 送信先と Gmail 系変数が揃っていれば、**`--send-email` を付けなくても** 既定で送信を試みます。
- 送りたくない実行だけ: `--no-send-email` または `YOUTUBE_CDP_SEND_EMAIL=0`（`false` / `off` 等可）。

`.env` は `.gitignore` 対象にしてください（誤コミット防止）。

---

## 5. 実行

### 便利ラッパー（推奨）

```bash
chmod +x ./run_youtube_summary.sh
./run_youtube_summary.sh --url "https://www.youtube.com/watch?v=..." --summary "短く要約して"
```

環境変数で既定を変える例:

```bash
URL="https://..." PROMPT="章立てで要約" ./run_youtube_summary.sh
```

### 直接 `youtube_cdp.py` を呼ぶ

```bash
./venv_wsl/bin/python3 youtube_cdp.py gemini --use-repo-chrome-profile --url "..." --prompt "..."
```

ヘッド付き（ウィンドウ表示）:

```bash
./run_youtube_summary.sh --headed
# または
HEADLESS=0 ./run_youtube_summary.sh
```

ヘルプ:

```bash
./venv_wsl/bin/python3 youtube_cdp.py --help
./venv_wsl/bin/python3 youtube_cdp.py gemini --help
```

---

## ログの見方（`gemini`）と追加の診断ファイル

実行ログはだいたい次の順です。

1. **`--use-repo-chrome-profile`** … 実際の `--user-data-dir`（このパスに Cookie 等が入る）
2. **CDP 接続候補** … 試す `http://...:9222`
3. **未応答なら Chrome 自動起動** … `user-data-dir`・`headless`・**起動 argv**（引数の確認用）
4. **`page.goto` … (domcontentloaded)** … 動画 URL への遷移完了
5. **スクリーンショット** … リポ直下 `youtube_cdp_screenshot.png`（ビューポートのみ）
6. **`ページ: url=` / `ページ: title=`** … リダイレクト後の URL とタブタイトル（ログイン壁・bot 画面の目安）
7. 以降 **「質問する」等のボタン探索** … 見つからないと `TimeoutError`（Cloud Shell や未ログインだとここで落ちやすい）

**もっと状況を残したいとき**（HTML・フルページ画像・本文抜粋）:

- **失敗時**（入口ボタンが見つからず例外になったとき）に、自動で次をリポジトリ直下に保存します。  
  - `youtube_cdp_gemini_debug_full.png`（フルページ）  
  - `youtube_cdp_gemini_debug.html`（DOM。長い場合は切り詰め）  
  - `youtube_cdp_gemini_debug_meta.txt`（url / title / body 先頭の抜粋）
- **成功前から毎回ほしい**ときは、実行前に  
  `export YOUTUBE_CDP_GEMINI_DEBUG=1`  
  を付けてください（viewport スクショの直後にも同じ一式を保存）。

---

## Chrome プロファイルと `/mnt/` 上のリポ

WSL で Linux 版 Chrome を使い、リポジトリが **`/mnt/c/...`（drvfs）** 上にある場合、CDP（9222）が立ちにくいことがあります。その場合、ツールは **ext4 上**のユーザデータディレクトリを使うよう切り替えます。リポ内の `chrome_cdp_profile/` と揃えたいときは、リポジトリ同梱の `scripts/copy_cdp_profile_to_repo.sh` などの利用を検討してください（スクリプト内のコメントを参照）。

---

## トラブル時の目安

| 状況 | 確認 |
|------|------|
| CDP に繋がらない | 9222 が他プロセスに占有されていないか。`YOUTUBE_CDP_NO_PORT_KILL=1` で挙動比較。 |
| WSL から Windows の Chrome だけ使いたい | `YOUTUBE_CDP_CHROME_BIN` や `USE_GOOGLE_CHROME=1` など。ログの CDP 到達性メッセージを確認。 |
| pip / venv が作れない | `python3-venv` をバージョンに合わせて導入。`bootstrap_venv_wsl.sh` の注記を参照。 |
| メールが送れない | `.env` の `GMAIL_USER` / `GMAIL_APP_PASSWORD`、送信先キー。`--email-to` で上書き可能。 |
| 「質問する」でタイムアウト | ログの `ページ: title=` と `youtube_cdp_screenshot.png`、失敗時の `youtube_cdp_gemini_debug_*` を確認。未ログイン・英語 UI・bot 確認画面が多い。 |

---

## ライセンス・免貢事項

利用する各サービス（YouTube、Gemini、Gmail 等）の利用規約・ポリシーに従ってください。本 README の情報は現時点の実装に基づきます。挙動はコード変更で変わる可能性があります。

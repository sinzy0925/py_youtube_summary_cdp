#!/usr/bin/env bash
# YouTube 動画の Gemini 要約（youtube_cdp.py gemini）を既定引数で実行する
#
# 使い方:
#   ./run_youtube_summary.sh
#   ./run_youtube_summary.sh --url "https://..." --summary "短く要約して"
#   ./run_youtube_summary.sh --url "https://..." --prompt "章立てで要約して" --after-entry 2
#   URL=... PROMPT=... AFTER=2 ./run_youtube_summary.sh
#
# 既定（このスクリプト）:
#   - YOUTUBE_CDP_CHROME_BIN: /usr/bin/google-chrome-stable（存在するときのみ。未設定かつパスあり）
#   - ヘッドレス起動（--chromium-headless 相当）
# ウィンドウ表示: ./run_youtube_summary.sh --headed  または HEADLESS=0
# 既存の CDP(9222 等) を掴む Chrome があると python は新規起動をスキップする。
# ヘッドレス・ヘッドありどちらも、既定で CDP ポートのリスナーを先に外す（fuser / lsof）。
# 占有解除をスキップ: YOUTUBE_CDP_NO_PORT_KILL=1（旧: YOUTUBE_CDP_HEADED_NO_PORT_KILL）
# その他の引数はそのまま python に渡る（例: --cdp-wait-sec 120 --v）
# メール送信: .env に GMAIL_USER, GMAIL_APP_PASSWORD と送信先（MAIL_TO 等）があれば既定で送る（--send-email は不要）
# 送らない: --no-send-email または YOUTUBE_CDP_SEND_EMAIL=0
# Google Chrome 優先（WSL で Windows の chrome.exe も試す）: USE_GOOGLE_CHROME=1 ./run_youtube_summary.sh
# WSL で pip/venv: システム Python は PEP 668 のため、./scripts/bootstrap_venv_wsl.sh で venv_wsl/ を作る
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"
# WSL: 同じフォルダの .venv は Windows 用(Scripts)のことが多く、bin/python3 が無い。venv_wsl/ を使う
PY=python3
if [[ -x "$REPO_ROOT/venv_wsl/bin/python3" ]]; then
  PY="$REPO_ROOT/venv_wsl/bin/python3"
fi

#URL="${URL:-https://www.youtube.com/watch?v=K4khB34HDd8}"
URL="${URL:-https://www.youtube.com/watch?v=8W6Qn2hNrAM}"
PROMPT="${PROMPT:-要約せず、全ての字幕を出力して}"
AFTER="${AFTER:-2}"
CDP="${CDP:-http://127.0.0.1:9222}"
if [[ -z "${YOUTUBE_CDP_PREFER_GOOGLE_CHROME:-}" && "${USE_GOOGLE_CHROME:-0}" == "1" ]]; then
  export YOUTUBE_CDP_PREFER_GOOGLE_CHROME=1
fi

# 既定: ヘッドレス。--headed または HEADLESS=0 でウィンドウ表示（従来の HEADLESS=1 は既定がヘッドレスのため不要）
CH_HEAD=(--chromium-headless)
export YOUTUBE_CDP_CHROME_HEADLESS=1
if [[ "${HEADLESS:-}" == "0" ]]; then
  CH_HEAD=()
  export YOUTUBE_CDP_CHROME_HEADLESS=0
fi

# 既定 Chrome: Linux(WSL) の google-chrome-stable（未指定かつ存在するとき）
if [[ -z "${YOUTUBE_CDP_CHROME_BIN:-}" && -f /usr/bin/google-chrome-stable ]]; then
  export YOUTUBE_CDP_CHROME_BIN=/usr/bin/google-chrome-stable
fi

CHROME="${YOUTUBE_CDP_CHROME_BIN:-}"
if [[ -z "$CHROME" && "${USE_GOOGLE_CHROME:-0}" == "1" ]]; then
  for p in \
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"; do
    if [[ -f "$p" ]]; then
      CHROME="$p"
      break
    fi
  done
fi
if [[ -z "$CHROME" ]]; then
  if [[ "${USE_GOOGLE_CHROME:-0}" == "1" || "${YOUTUBE_CDP_PREFER_GOOGLE_CHROME:-0}" == "1" ]]; then
    CHROME=$(command -v google-chrome-stable || command -v google-chrome || true)
  fi
fi
if [[ -z "$CHROME" ]]; then
  CHROME=$(command -v chromium || command -v chromium-browser || command -v google-chrome-stable || command -v google-chrome || true)
fi
if [[ -z "$CHROME" ]]; then
  echo "Chromium / Google Chrome が見つかりません。YOUTUBE_CDP_CHROME_BIN にフルパスを設定するか、USE_GOOGLE_CHROME=1 で Windows 版のパスを探します。" >&2
  exit 1
fi
export YOUTUBE_CDP_CHROME_BIN="$CHROME"

EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      URL="$2"
      shift 2
      ;;
    --prompt)
      PROMPT="$2"
      shift 2
      ;;
    --summary)
      PROMPT="$2"
      shift 2
      ;;
    --after-entry)
      AFTER="$2"
      shift 2
      ;;
    --cdp)
      CDP="$2"
      shift 2
      ;;
    --headed)
      CH_HEAD=()
      export YOUTUBE_CDP_CHROME_HEADLESS=0
      shift
      ;;
    --chromium-headless)
      CH_HEAD=(--chromium-headless)
      export YOUTUBE_CDP_CHROME_HEADLESS=1
      shift
      ;;
    --send-email)
      export YOUTUBE_CDP_SEND_EMAIL=1
      shift
      ;;
    --email-to)
      export MAIL_TO="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,22p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      EXTRA+=("$1")
      shift
      ;;
  esac
done

# CDP 用 Chrome が前回のまま 9222 等を掴むと、python は新規起動せず前回のフラグのままになる。
# ヘッドレス・ヘッドありのどちらも、既定で当該ポートのリスナーを終了してから起動。
_NO_KILL_PORT="${YOUTUBE_CDP_NO_PORT_KILL:-${YOUTUBE_CDP_HEADED_NO_PORT_KILL:-0}}"
if [[ "$_NO_KILL_PORT" != "1" ]]; then
  _cdp_p=9222
  if [[ "$CDP" =~ :([0-9]+) ]]; then
    _cdp_p="${BASH_REMATCH[1]}"
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${_cdp_p}/tcp" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -t -iTCP:"${_cdp_p}" -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
  else
    echo "run_youtube_summary: fuser も lsof も見つかりません。前回の CDP(ポート ${_cdp_p})を手で終了するか、YOUTUBE_CDP_NO_PORT_KILL=1 で再試行してください。" >&2
  fi
  sleep 0.5
fi

exec env \
  YOUTUBE_CDP_CHROME_BIN="$YOUTUBE_CDP_CHROME_BIN" \
  YOUTUBE_CDP_CHROME_HEADLESS="${YOUTUBE_CDP_CHROME_HEADLESS}" \
  "$PY" "$REPO_ROOT/youtube_cdp.py" gemini \
  --use-repo-chrome-profile \
  "${CH_HEAD[@]}" \
  --cdp "$CDP" \
  --url "$URL" \
  --prompt "$PROMPT" \
  --after-entry "$AFTER" \
  "${EXTRA[@]}"

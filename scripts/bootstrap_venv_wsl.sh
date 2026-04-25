#!/usr/bin/env bash
# WSL / Linux: リポジトリ直下に venv_wsl/ を作り、requirements.txt を入れる。
# .venv が Windows 用 (Scripts/ のみ) のとき、WSL からは bin/python3 が使えないため
# このディレクトリに Linux 専用 venv を分ける（Windows 用 .venv は消さない）。
#
# 事前: Debian/Ubuntu では venv モジュールが入っていないと失敗する。
#   sudo apt update && sudo apt install -y python3.12-venv
# （python3 --version のマイナーに合わせる。例: python3.12-venv）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/venv_wsl}"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -U pip wheel
"$VENV_DIR/bin/pip" install -r "$REPO_ROOT/requirements.txt"
echo "OK: $VENV_DIR を作成し依存関係を入れました。有効化: source $VENV_DIR/bin/activate"
echo "run_youtube_summary.sh は venv_wsl があると自動でこの Python を使います。"

#!/usr/bin/env bash
# ext4 上の Chromium プロファイルを、リポジトリ直下の chrome_cdp_profile/ へ入れる (WSL 向け)
# 1) Chromium を完全終了
# 2) リポジトリ直下で: bash scripts/copy_cdp_profile_to_repo.sh
#    または: bash scripts/copy_cdp_profile_to_repo.sh  /path/to/chrome_cdp_profile_日時_xxx
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${REPO_ROOT}/chrome_cdp_profile"
SRC=""

if [[ -n "${1:-}" ]]; then
  if [[ "$1" == /* ]]; then
    SRC="$1"
  elif [[ "$1" == ~* ]]; then
    SRC="${1/#\~/$HOME}"
  else
    SRC="$REPO_ROOT/$1"
  fi
else
  base="${XDG_DATA_HOME:-"$HOME/.local/share"}/py_youtube_summary_cdp"
  SRC=$(ls -td "$base"/chrome_cdp_profile_* 2>/dev/null | head -1 || true)
  if [[ -z "$SRC" || ! -d "$SRC" ]]; then
    echo "コピー元が見つかりません: $base に chrome_cdp_profile_日付_* があるか" >&2
    echo "例: $0 $HOME/.local/share/py_youtube_summary_cdp/chrome_cdp_profile_20260423_224521_16906" >&2
    exit 1
  fi
fi

if [[ ! -d "$SRC" ]]; then
  echo "ディレクトリではありません: $SRC" >&2
  exit 1
fi
nfiles=$(find "$SRC" -type f 2>/dev/null | wc -l)
echo "  元(スクリプト想定): $SRC  (通常ファイル $nfiles 個)"
# snap の /snap/bin/chromium は --user-data-dir を付けても、実データが
# ~/.local/share/... に現れないことが多い。実体は ~/snap/chromium/common/chromium 等
if [[ "$nfiles" -lt 1 ]]; then
  for p in \
    "$HOME/snap/chromium/common/chromium" \
    "$HOME/snap/chromium/current/.config/chromium" \
    "$HOME/.var/app/org.chromium.Chromium/config/chromium" ; do
    if [[ -d "$p" ]]; then
      n2=$(find "$p" -type f 2>/dev/null | wc -l)
      if [[ "$n2" -ge 1 ]]; then
        echo "  → snap/Flatpak 側のプロファイルに切替: $p  (通常ファイル $n2 個)" >&2
        SRC="$p"
        nfiles=$n2
        break
      fi
    fi
  done
fi
if [[ "$nfiles" -lt 1 ]]; then
  echo "エラー: コピーできるファイルがどこにもありません。" >&2
  echo "  手動: find \"\$HOME/snap\" -name 'Local State' 2>/dev/null" >&2
  echo "  または: find \"\$HOME/.var\" -name 'Local State' 2>/dev/null  (Flatpak)" >&2
  exit 1
fi
echo "  実際のコピー元: $SRC  (通常ファイル $nfiles 個)"
echo "  先: $DEST"
read -r -p "Chromium / chrome を完全終了した? [y/N] " a || true
if [[ "${a:-}" != "y" && "${a:-}" != "Y" ]]; then
  echo "中断" >&2
  exit 1
fi
rm -rf "$DEST"
mkdir -p "$DEST"
if command -v rsync >/dev/null 2>&1; then
  rsync -aHh --no-perms --no-owner --no-group --info=stats2,progress2 "$SRC"/ "$DEST"/
else
  (cd "$SRC" && tar -cf - .) | (cd "$DEST" && tar -xf -)
fi
echo "--- 先 ($DEST) ---"
ls -la "$DEST" | head -25
du -sh "$DEST" 2>/dev/null || true
echo "完了。次: python3 youtube_cdp.py home --use-repo-chrome-profile ..."

#!/usr/bin/env python3
"""
WSL2 上の Chrome（CDP: --remote-debugging-port=9222）に接続し YouTube を操作する。
起動中でなければ、ローカル向け --cdp のみ Chrome を自動起動（既定プロファイルはリポ内 chrome_cdp_profile/）。

WSL2 内で google-chrome が Windows 上で動く場合: WSL 内 127.0.0.1:9222 には乗らない。Chrome に
--remote-debugging-address=0.0.0.0 を付け、172.16/12 上の仮想 NIC（eth0 推定 or ip route の via）へ
``http://<ホストIP>:9222`` で接続する。``default via 192.168.x.1`` だけに頼ると家のルータを指して
Connection refused になることがある。
Windows の Python から WSL2 の Chrome へ接続する場合: WSL の IP を使うか、WSL の localhost 転送が有効なら 127.0.0.1 でも可（環境による）。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)

DEFAULT_CDP = "http://127.0.0.1:9222"
# CDP 専用 Chrome プロファイル（リポジトリ直下・初回起動で自動作成。中身は .gitignore）
_REPO_DIR = Path(__file__).resolve().parent
REPO_CHROME_CDP_PROFILE = _REPO_DIR / "chrome_cdp_profile"
YOUTUBE = "https://www.youtube.com/"
# 依頼例の動画。--url で上書き可。
DEFAULT_GEMINI_VIDEO = "https://www.youtube.com/watch?v=O8PzL3S-TbU"
# gemini: 動画ページ表示直後の待機秒 → スクリーンショット
GEMINI_POST_GOTO_SCREENSHOT_SEC = 2.0
GEMINI_SCREENSHOT_FILENAME = "youtube_cdp_screenshot.png"
# 診断: 失敗時 & YOUTUBE_CDP_GEMINI_DEBUG=1 のときリポジトリ直下へ保存
GEMINI_DEBUG_HTML = "youtube_cdp_gemini_debug.html"
GEMINI_DEBUG_FULL_PNG = "youtube_cdp_gemini_debug_full.png"
GEMINI_DEBUG_META_TXT = "youtube_cdp_gemini_debug_meta.txt"
GEMINI_DEBUG_HTML_MAX_CHARS = 1_500_000
GEMINI_DEBUG_BODY_MAX_CHARS = 8_000
# 上記のセレクタで取れないときの accessible name 用（英語UI等・大小文字は re.IGNORECASE）
DEFAULT_GEMINI_NAME_PATTERN = r"(質問する|gemini|ask)"
# 動画下メニュー「質問する」（Gemini チャット入口）:
# <button-view-model class="... you-chat-entrypoint-button"><button aria-label="質問する" ...
SELECTOR_YOUTUBE_CHAT_ENTRYPOINT = (
    "ytd-menu-renderer button-view-model.you-chat-entrypoint-button button"
)
SELECTOR_YOUTUBE_CHAT_ENTRYPOINT_LOOSE = (
    "button-view-model.you-chat-entrypoint-button button"
)
# パネル内の「質問を入力」欄
DEFAULT_GEMINI_PROMPT = "要約して"
DEFAULT_AFTER_ENTRY_SEC = 2.0
# 要約して等を入れた直後、手動でスペースを入れると送信が有効になる挙向への対策
POST_PROMPT_SETTLE_SEC = 2.0
_NAME_QUESTION_PLACEHOLDER = re.compile("質問を入力")
# 動画下・フッター側の Gemini チャット（質問するクリック後に出る想定）
# 送信: form 内の button-view-model>button。touch-feedback 内の div は当てない。
SELECTOR_FOOTER_CHAT_ROOT = "#footer yt-chat-input-view-model"
# ... > div > form > button-view-model > button（子 div では触らない）
SELECTOR_FOOTER_CHAT_SEND_BUTTON = (
    f"{SELECTOR_FOOTER_CHAT_ROOT} form > button-view-model > button, "
    f"{SELECTOR_FOOTER_CHAT_ROOT} form button-view-model > button, "
    f"{SELECTOR_FOOTER_CHAT_ROOT} form button-view-model button"
)
# Gemini 応答: <you-chat-item-view-model> 内 <markdown-div class="ytwMarkdownDivHost" ...>（吹き出し単位で取得）
SELECTOR_YOUCHAT_ITEM = "you-chat-item-view-model"
SELECTOR_MARKDOWN_IN_CHAT_ITEM = (
    f"{SELECTOR_YOUCHAT_ITEM} markdown-div.ytwMarkdownDivHost"
)
# 上記 0 件のとき用（従来）
SELECTOR_MARKDOWN_REPLY_FALLBACK = "markdown-div.ytwMarkdownDivHost"
DEFAULT_RESPONSE_TIMEOUT_MS = 120_000


def _normalize_cdp_base(cdp_url: str) -> str:
    u = (cdp_url or DEFAULT_CDP).strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")


def _cdp_json_version_url(base: str) -> str:
    return f"{_normalize_cdp_base(base)}/json/version"


def _cdp_urllib_opener() -> urllib.request.OpenerDirector:
    """
    環境変数の http(s)_proxy により 127.0.0.1:9222 へのアクセスがプロキシ経由になり
    タイムアウトするのを防ぐ（urllib は localhost をプロキシから除外するとは限らない）。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdp_listening_info(cdp_url: str, open_timeout: float = 2.0) -> tuple[bool, str]:
    """
    GET {base}/json/version の成否と、ログ用の一行メッセージ。
    失敗時は接続拒否・タイムアウト・HTTP コード等を含める。
    """
    base = _normalize_cdp_base(cdp_url)
    url = _cdp_json_version_url(base)
    try:
        with _cdp_urllib_opener().open(url, timeout=open_timeout) as r:
            c = r.getcode()
            if 200 <= c < 500:
                return True, f"HTTP {c} OK"
            return False, f"HTTP {c} (json/version は 2xx〜4xx を想定)"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code} {e.reason!r} (url={url})"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, OSError):
            en = getattr(reason, "errno", None)
            return False, f"URLError (OSError): {reason!r} errno={en} (url={url})"
        return False, f"URLError: {e!r} reason={reason!r} (url={url})"
    except TimeoutError:
        return False, f"タイムアウト ({open_timeout}s, url={url})"
    except OSError as e:
        return False, f"OSError: {e!r} errno={getattr(e, 'errno', None)} (url={url})"
    except ValueError as e:
        return False, f"ValueError: {e!r} (url={url})"


def cdp_listening(cdp_url: str, open_timeout: float = 2.0) -> bool:
    """Chrome DevTools Protocol (GET /json/version) が応答するか。"""
    ok, _ = cdp_listening_info(cdp_url, open_timeout=open_timeout)
    return ok


def probe_all_cdp_candidates(
    cdp_url: str, per_try_timeout: float
) -> dict[str, tuple[bool, str]]:
    """各候補 base URL -> (成功, 詳細一行)。"""
    out: dict[str, tuple[bool, str]] = {}
    for cand in _cdp_url_candidates(cdp_url):
        b = _normalize_cdp_base(cand)
        out[b] = cdp_listening_info(b, open_timeout=per_try_timeout)
    return out


def first_cdp_url_that_listens(
    cdp_url: str, per_try_timeout: float = 1.0
) -> str | None:
    """候補のうち /json/version が返る base URL。だめなら None。"""
    for cand in _cdp_url_candidates(cdp_url):
        ok, _ = cdp_listening_info(cand, open_timeout=per_try_timeout)
        if ok:
            return _normalize_cdp_base(cand)
    return None


def _is_wsl() -> bool:
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8", errors="replace") as f:
            t = f.read().lower()
        return "microsoft" in t or "wsl" in t
    except OSError:
        return False


def _is_wsl_invoked_windows_chrome_exe(binary: str) -> bool:
    """WSL 上から起動する実行ファイルが Windows の chrome.exe か（--user-data-dir は Windows 形式が必要）。"""
    if sys.platform == "win32" or not _is_wsl():
        return False
    b = binary.replace("\\", "/").lower()
    return b.endswith("chrome.exe")


def _wsl_user_data_dir_for_chrome_binary(binary: str, user_data_dir_linux: str) -> str:
    """
    WSL から chrome.exe を起動するとき、--user-data-dir に Linux パスを渡すと無視され
    デフォルトプロファイルのままになり、--remote-debugging-port も効かないことがある。
    wslpath -w で C:\\... または \\\\wsl$\\... に変換する。
    """
    if not _is_wsl_invoked_windows_chrome_exe(binary):
        return user_data_dir_linux
    u = user_data_dir_linux
    if re.match(r"^[A-Za-z]:[\\/]", u):
        return u
    try:
        r = subprocess.run(
            ["wslpath", "-w", u],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            w = (r.stdout or "").strip().splitlines()[0].strip()
            if w:
                logger.info(
                    "WSL+chrome.exe: --user-data-dir を Windows 形式に変換: %s -> %s",
                    u,
                    w,
                )
                return w
        err = ((r.stderr or "") + (r.stdout or "")).strip()
        if err:
            logger.warning("WSL+chrome.exe: wslpath -w 非0 (%s): %s", r.returncode, err[:500])
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("WSL+chrome.exe: wslpath -w 失敗: %s", e)
    logger.warning(
        "WSL+chrome.exe: --user-data-dir を Windows 形式にできませんでした (%s)。"
        " そのまま渡します。stderr に「DevTools remote debugging requires a non-default…」と出る場合は"
        " パスを確認してください。",
        u,
    )
    return u


def _wsl_nameserver_ip() -> str | None:
    """/etc/resolv.conf の nameserver（フォールバック用。WSL2 では 10.255.255.254 等で CDP には届かないことも）。"""
    try:
        with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if line.lower().startswith("nameserver "):
                    return line.split()[1].strip()
    except OSError:
        return None
    return None


def _wsl_default_gateway_ip() -> str | None:
    """default 行の via（必ずしも Windows ホストではない。家庭用ルータ 192.168.x.1 のことも）。"""
    try:
        r = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        m = re.search(r"\bvia\s+(\S+)", (r.stdout or "")[:500])
        if not m:
            return None
        g = m.group(1).strip()
        if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", g):
            return g
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def _wsl_infer_windows_host_from_eth0() -> str | None:
    """
    典型 WSL2: eth0 が 172.16.0.0/12 内。Windows 側の仮想 NIC は同一サブネットの
    network+1（例: 172.30.0.0/20 なら 172.30.0.1）であることが多い。
    eth0 が 192.168 等のブリッジ構成のときは推定しない（ルータ .1 と誤るため）。
    """
    try:
        r = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", "eth0"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0:
            return None
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", r.stdout or "")
        if not m:
            return None
        addr_s, pfx = m.group(1), int(m.group(2))
        a, b, _, _ = (int(x) for x in addr_s.split("."))
        if not (a == 172 and 16 <= b <= 31):
            return None
        ifc = ipaddress.IPv4Interface(f"{addr_s}/{pfx}")
        gw = ifc.network.network_address + 1
        return str(gw)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _wsl_private_via_addresses_from_routes() -> list[str]:
    """ip route 全文から private な `via` を集める（default が家のルータだけでも 172.x の行があれば拾える）。"""
    try:
        r = subprocess.run(
            ["ip", "-4", "route", "show"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    found: set[str] = set()
    for m in re.finditer(r"\bvia\s+((\d{1,3}\.){3}\d{1,3})\b", r.stdout):
        s = m.group(1)
        try:
            if ipaddress.ip_address(s).is_private:
                found.add(s)
        except ValueError:
            pass
    return list(found)


def _wsl_host_ip_sort_key(ip: str) -> tuple[int, int]:
    """
    接続候補の優先度。WSL2 の Windows 仮想 NIC は 172.16/12 に現れることが多い。
    10.255.255.254（名前解決用等）は後ろに回す。家庭用 default の 192.168.x.1 は中〜後。
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return (90, 0)
    t = 50
    if a in ipaddress.ip_network("172.16.0.0/12", strict=False):
        t = 0
    elif a in ipaddress.ip_network("10.0.0.0/8", strict=False) and a != ipaddress.ip_address("10.255.255.254"):
        t = 10
    elif a in ipaddress.ip_network("192.168.0.0/16", strict=False):
        t = 20
    elif a == ipaddress.ip_address("10.255.255.254"):
        t = 40
    return (t, int(a))


def _wsl_windows_host_ips() -> list[str]:
    """
    同一マシン上の「Windows ホスト上の Chrome」に届きやすいプライベート IP（重複なし、順序あり）。
    127.0.0.1 は含めない（WSL 側の loopback なので Windows の CDP とは無関係）。

    注意: default `via 192.168.3.1` は**家のルータ**のことが多い。WSL2 の本物の相手は
    しばしば `ip route` 内の 172.16/12 系、または `eth0` から推定した 172.30.0.1 等。
    """
    seen: set[str] = set()
    out: list[str] = []

    def add_raw(ip: str | None) -> None:
        if not ip or not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip):
            return
        try:
            if not ipaddress.ip_address(ip).is_private:
                return
        except ValueError:
            return
        if ip in seen:
            return
        seen.add(ip)
        out.append(ip)

    add_raw((os.environ.get("WSL2_GATEWAY") or "").strip())
    add_raw((os.environ.get("YOUTUBE_CDP_WINDOWS_HOST_IP") or "").strip())
    add_raw(_wsl_infer_windows_host_from_eth0())

    for ip in sorted(
        _wsl_private_via_addresses_from_routes(),
        key=_wsl_host_ip_sort_key,
    ):
        add_raw(ip)
    # default が ルータ(192.168..1) だけ早めに出すと外れるので、上で拾えた after で十分。
    # resolv / default は最後方にフォールバック
    add_raw(_wsl_nameserver_ip())
    add_raw(_wsl_default_gateway_ip())
    return out


def _log_wsl_cdp_environment() -> None:
    """WSL / ルーティングまわりの診断（失敗時の切り分け用）。"""
    logger.info("環境: WSL と判定" if _is_wsl() else "環境: WSL ではないと判定")
    if not _is_wsl():
        return
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8", errors="replace") as f:
            logger.info("kernel osrelease: %s", f.read().strip()[:200])
    except OSError as e:
        logger.info("kernel osrelease 読めず: %s", e)
    gw = (os.environ.get("WSL2_GATEWAY") or "").strip()
    if gw:
        logger.info("環境変数 WSL2_GATEWAY=%s", gw)
    wh = (os.environ.get("YOUTUBE_CDP_WINDOWS_HOST_IP") or "").strip()
    if wh:
        logger.info("環境変数 YOUTUBE_CDP_WINDOWS_HOST_IP=%s (CDP 接続先の手動上書き)", wh)
    try:
        r = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        logger.info("ip route show default: %s", (r.stdout or "").strip() or "(空)")
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.info("ip route show default 失敗: %s", e)
    eth_infer = _wsl_infer_windows_host_from_eth0()
    if eth_infer:
        logger.info("eth0 から推定した Windows ホスト候補(172/16/12 時): %s", eth_infer)
    else:
        logger.info("eth0 から 172.16/12 前提の推定: 得られず（ブリッジ等は手動の --cdp を検討）")
    try:
        r2 = subprocess.run(
            ["ip", "-4", "route", "show"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if r2.returncode == 0 and (r2.stdout or "").strip():
            lines = (r2.stdout or "").strip().splitlines()[:32]
            logger.info("ip -4 route show (先頭32行):\n%s", "\n".join(lines))
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.info("ip -4 route show 取得失敗: %s", e)
    via_all = _wsl_private_via_addresses_from_routes()
    if via_all:
        logger.info("ルート上の private via 候補: %s", sorted(via_all, key=_wsl_host_ip_sort_key))
    hosts = _wsl_windows_host_ips()
    logger.info("WSL から試す Windows ホスト IP 候補（試行順）: %s", hosts or "(なし)")


def _name_regex_for_playwright(name_pattern: str) -> re.Pattern[str]:
    """
    ``get_by_role(..., name=<Regex>)`` に渡す正規表現。パターン先頭の ``(?i)`` は
    ブラウザ側セレクタ用エンジンで Invalid group になりやすいため、取り除き
    ``re.IGNORECASE`` を付与する。
    """
    p = (name_pattern or "").strip()
    if p.startswith("(?i)"):
        p = p[4:].lstrip()
    if not p:
        p = "."
    return re.compile(p, re.IGNORECASE | re.UNICODE)


def _read_text_file_tail(path: str, max_bytes: int = 6000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            sz = f.tell()
            f.seek(max(0, sz - max_bytes))
            raw = f.read()
        return raw.decode("utf-8", errors="replace")
    except OSError as e:
        return f"(読めません: {e})"


def _cdp_url_candidates(cdp_url: str) -> list[str]:
    """
    接続候補。WSL で `google-chrome` が **Windows 版**を立てると、WSL 内 127.0.0.1:9222 には
    Windows 上の CDP は乗らない。さらに Windows 側の Chromium は 127.0.0.1:9222 のみ bind しがちで、
    ルート上の「ホスト」IP からの接続も弾かれるため、起動に ``--remote-debugging-address=0.0.0.0`` が必要
   （_launch_chrome_cdp 側で付与）。

    フォールバックは eth0(172/16/12) からの推定・`ip -4 route` の各 via（172 系を優先）・
    resolv・default gateway。default が 192.168.x.1 の場合は家のルータのことがある点に注意。
    """
    b = _normalize_cdp_base(cdp_url)
    p = urlparse(b)
    port = p.port or 9222
    h = (p.hostname or "localhost").lower()
    out: list[str] = [b]
    if _is_wsl() and h in ("127.0.0.1", "localhost", "::1"):
        for wh in _wsl_windows_host_ips():
            alt = f"http://{wh}:{port}"
            if alt not in out:
                out.append(alt)
    return out


def _is_local_cdp_url(cdp_url: str) -> bool:
    p = urlparse(_normalize_cdp_base(cdp_url))
    h = (p.hostname or "localhost").lower()
    return h in ("127.0.0.1", "localhost", "::1") or p.hostname is None


def _prefer_google_chrome_over_chromium() -> bool:
    """YOUTUBE_CDP_PREFER_GOOGLE_CHROME=1 のとき PATH 探索で Google Chrome 系を先に。"""
    v = (os.environ.get("YOUTUBE_CDP_PREFER_GOOGLE_CHROME") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _find_chrome_executable() -> str | None:
    for name in ("YOUTUBE_CDP_CHROME_BIN", "CHROME_PATH", "GOOGLE_CHROME_SHIM"):
        v = (os.environ.get(name) or "").strip()
        if v and os.path.isfile(v):
            return v
    # WSL で「Google Chrome を使う」: Windows 版の実体（WSL から /mnt/c/...）または
    # google-chrome-stable（.deb）。YOUTUBE_CDP_PREFER_GOOGLE_CHROME=1 で有効。
    # 未指定時は従来どおり Chromium 先（/usr/bin/google-chrome がラッパーで user-data-dir が効かないことがあるため）
    if _is_wsl() and _prefer_google_chrome_over_chromium():
        for p in (
            r"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
            r"/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        ):
            if p and os.path.isfile(p):
                return p
        cands: tuple[str, ...] = (
            "google-chrome-stable",
            "google-chrome",
            "chromium",
            "chromium-browser",
        )
    elif _is_wsl():
        cands = (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        )
    else:
        cands = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
    for cand in cands:
        w = shutil.which(cand)
        if w:
            return w
    if sys.platform == "win32":
        w = shutil.which("chrome")
        if w and os.path.isfile(w):
            return w
        for path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"
            ),
        ):
            if path and os.path.isfile(path):
                return path
    return None


def _resolve_linux_chrome_elf(binary: str) -> str:
    """
    /usr/bin/google-chrome* は多くの環境でシェルラッパーであり、
    子の chrome へ --user-data-dir や --remote-debugging-* が飛ばず
    「DevTools remote debugging requires a non-default data directory」
    や file:// プロファイル表示だけになる。Google 公式 .deb なら
    /opt/google/chrome/chrome へ直す。無効化: YOUTUBE_CDP_CHROME_ELF=0
    """
    v = (os.environ.get("YOUTUBE_CDP_CHROME_ELF") or "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return binary
    if binary.strip() and _is_wsl_invoked_windows_chrome_exe(binary):
        return binary
    if sys.platform not in ("linux", "linux2", "linux3", "linux4"):
        return binary
    rp = os.path.normpath(os.path.realpath(os.path.expanduser(binary)))
    base = os.path.basename(rp).lower()
    # 既に実体 (Google .deb) に近い
    if base == "chrome" and "opt" + os.path.sep + "google" + os.path.sep + "chrome" in rp.replace("\\", "/").lower():
        return binary
    # 公式 Google Chrome: 実 ELF
    for elf in (
        "/opt/google/chrome/chrome",
        "/opt/google/chrome-beta/chrome",
        "/opt/google/chrome-unstable/chrome",
    ):
        e = os.path.normpath(elf)
        if os.path.isfile(e) and os.access(e, os.X_OK):
            if base.startswith("google-chrome") or rp.endswith("/usr/bin/google-chrome"):
                logger.info(
                    "Linux: google-chrome ラッパーを飛ばし実バイナリを使います: %s -> %s",
                    binary,
                    e,
                )
                return e
            break
    # ディストリ Chromium 名のラッパー（あれば同梱先）
    if base.startswith("chromium"):
        for elf in ("/usr/lib/chromium/chromium", "/usr/lib/chromium-browser/chromium-browser"):
            e = os.path.normpath(elf)
            if os.path.isfile(e) and os.access(e, os.X_OK):
                if os.path.normpath(rp) != e:
                    logger.info("Linux: Chromium ラッパーを飛ばし実バイナリを使います: %s -> %s", binary, e)
                return e
    return binary


def _wsl_nativelike_chrome_user_data_dir() -> str:
    """
    WSL 内 ext4 側の専用プロファイル。XDG 準拠。
    /mnt/c/...（drvfs）上だと Chrome が user-data-dir を正しく扱えず
    「DevTools remote debugging requires a non-default data directory」となり
    9222 が上がらないことがある。
    """
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    base = xdg or str(Path.home() / ".local" / "share")
    return str(Path(base) / "py_youtube_summary_cdp" / "chrome_cdp_profile")


def default_cdp_chrome_user_data_dir() -> str:
    """
    自動起動用の固定プロファイル置き場（未指定・未上書き時）。

    通常: リポジトリ直下 ``chrome_cdp_profile/`` 。
    WSL でリポジトリが ``/mnt/``（Windows ドライブマウント）上のとき: **既定は ext4 側**
    ``~/.local/share/py_youtube_summary_cdp/chrome_cdp_profile/`` 。
    上書き: 環境変数 YOUTUBE_CDP_USER_DATA_DIR または --chrome-user-data-dir 。
    """
    e = (os.environ.get("YOUTUBE_CDP_USER_DATA_DIR") or "").strip()
    if e:
        return os.path.expanduser(e)
    repo_profile = str(REPO_CHROME_CDP_PROFILE)
    if _is_wsl() and repo_profile.startswith("/mnt/"):
        nat = _wsl_nativelike_chrome_user_data_dir()
        logger.info(
            "WSL + /mnt/ 検出: --user-data-dir を Linux ネイティブ配下にします: %s\n"
            "  (/mnt/c/.../chrome_cdp_profile のままだと、Chrome が CDP 用の "
            "user-data-dir を受け付けず json/version に繋がらないことがあります。 "
            "リポ同じ場所に寄せる場合は YOUTUBE_CDP_USER_DATA_DIR を手動で指定)",
            nat,
        )
        return nat
    return repo_profile


def _wants_use_repo_chrome_profile(args: Namespace) -> bool:
    """リポジトリ直下 ``chrome_cdp_profile/`` を使う（フラグまたは環境変数）。"""
    if getattr(args, "use_repo_chrome_profile", False):
        return True
    v = (os.environ.get("YOUTUBE_CDP_USE_REPO_CHROME_PROFILE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _wants_chromium_headless(args: Namespace) -> bool:
    """GUI なし起動: --chromium-headless または YOUTUBE_CDP_CHROME_HEADLESS=1"""
    if getattr(args, "chromium_headless", False):
        return True
    v = (os.environ.get("YOUTUBE_CDP_CHROME_HEADLESS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def new_chrome_profile_user_data_dir(base: str) -> str:
    """
    ``base`` を名前の雛形に、空の新規 ``--user-data-dir`` パスを返す（親ディレクトリは既存想定）。

    例: ``.../py_youtube_summary_cdp/chrome_cdp_profile`` →
    ``.../py_youtube_summary_cdp/chrome_cdp_profile_20260423_143022_12345``
    """
    b = Path(os.path.expanduser(base))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return str(b.parent / f"{b.name}_{stamp}_{os.getpid()}")


def _launch_chrome_cdp(
    cdp_url: str,
    binary: str,
    user_data_dir: str,
    *,
    headless: bool = False,
) -> tuple[subprocess.Popen, str]:
    """
    Chrome を起動。戻り値 (Popen, stderr ログファイルパス)。stderr は常にファイルへ出し、失敗時に末尾を読む。

    起動行末の URL は付けない（about:blank のみ）。URL を重ねると二重タブになるほか、
    snap 版 Chromium 等で先頭が ``file://`` のプロファイル一覧になる例がある。
    遷移は接続後の Playwright ``page.goto`` に任せる。

    headless: True のとき ``--headless=new``（Chromium 112+ 相当。画面は出さない。一部サイトの検出に注意）
    """
    base = _normalize_cdp_base(cdp_url)
    p = urlparse(base)
    port = p.port or 9222
    expanded_ud = os.path.expanduser(user_data_dir)
    os.makedirs(expanded_ud, exist_ok=True)
    ud_for_cmd = _wsl_user_data_dir_for_chrome_binary(binary, expanded_ud)
    if _is_wsl() and expanded_ud.replace("\\", "/").lower().startswith("/mnt/"):
        if not _is_wsl_invoked_windows_chrome_exe(binary):
            logger.warning(
                "WSL: --user-data-dir が /mnt/ (drvfs) 上です。 stderr に "
                "「DevTools remote debugging requires a non-default data directory」が出る場合は "
                "~/.local/share/py_youtube_summary_cdp/chrome_cdp_profile/ 等の ext4 側へ戻してください。"
            )
    # プロファイルを固定。ここにないと、起動のたびに「別プロファイル」扱いになりやすい。
    # --user-data-dir は「=付き1引数」にする（二引数分割の解釈ブレを避ける）
    ud_arg = f"--user-data-dir={ud_for_cmd}"
    cmd: list[str] = [
        binary,
        ud_arg,
        f"--remote-debugging-port={port}",
        # 既定は 127.0.0.1 のみ。WSL から「Windows 上に立った」Chrome へ接続するには LAN に出す（ローカル開発向け）
        "--remote-debugging-address=0.0.0.0",
        # Playwright / curl 等の HTTP 接続用（Chromium 111+）
        "--remote-allow-origins=*",
    ]
    if _is_wsl() and sys.platform != "win32":
        # /dev/shm 不足でタブがおかしくなったり CDP が上がらないことがある（WSL/Docker 系）
        cmd.insert(2, "--disable-dev-shm-usage")
    if headless:
        # 旧版は --headless のみ。新ヘッドレスは new（ドキュメント: Chrome headless mode）
        cmd.append("--headless=new")
    if sys.platform != "win32":
        # WSL2 / Linux でよく使う。Windows 用 Chrome では通常不要。
        cmd.append("--no-sandbox")
    # 行末の URL（YouTube 等）を足さない: Playwright 側の 1 回だけ goto に統一（二重タブ・
    # snap 先頭タブの file:// プロファイル表示を避ける）
    cmd.append("about:blank")
    fd, err_path = tempfile.mkstemp(prefix="youtube_cdp_chrome_", suffix=".stderr.log")
    os.close(fd)
    err_f = open(err_path, "wb")
    child_env = os.environ.copy()
    _no = "127.0.0.1,localhost,::1"
    _ex = (child_env.get("NO_PROXY") or child_env.get("no_proxy") or "").strip()
    child_env["NO_PROXY"] = f"{_no},{_ex}" if _ex else _no
    child_env["no_proxy"] = child_env["NO_PROXY"]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": err_f,
        "stdin": subprocess.DEVNULL,
        "env": child_env,
    }
    if sys.platform == "win32":
        df = getattr(subprocess, "DETACHED_PROCESS", 0)
        npg = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if df or npg:
            kwargs["creationflags"] = df | npg
    else:
        kwargs["start_new_session"] = True
    http_p = (os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    https_p = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    if http_p or https_p:
        logger.info(
            "CDP 診断: 環境に HTTP(S)_PROXY あり（urllib の json/version はプロキシ無効で試行） http=%s https=%s",
            (http_p[:80] + "…") if len(http_p) > 80 else http_p or "(なし)",
            (https_p[:80] + "…") if len(https_p) > 80 else https_p or "(なし)",
        )
    logger.info(
        "CDP 用 Chrome: user-data-dir=%s headless=%s 起動直後 about:blank（遷移は Playwright）",
        ud_for_cmd,
        headless,
    )
    logger.info("CDP 用 Chrome: 実行ファイル=%s", binary)
    try:
        logger.info("CDP 用 Chrome: 起動 argv（shlex）: %s", shlex.join(cmd))
    except (TypeError, ValueError):
        logger.info("CDP 用 Chrome: 起動 argv: %r", cmd)
    logger.debug("CDP 用 Chrome: 起動行=%s", cmd)
    proc = subprocess.Popen(cmd, **kwargs)
    err_f.close()
    logger.info("CDP 用 Chrome: 起動 pid=%s stderrログ=%s", proc.pid, err_path)
    return proc, err_path


def ensure_cdp_chrome(
    cdp_url: str,
    max_wait_sec: float,
    user_data_dir: str,
    *,
    chromium_headless: bool = False,
) -> str:
    """
    CDP が生きていなければ（ローカルに限る）Chrome を起動し、/json/version が返るまで待つ。
    戻り値は実際に応答が取れた CDP ベース URL（WSL で Windows 版 Chrome 時は 127.0.0.1 では届かないためホスト IP になることあり）。

    user_data_dir: 毎回同じにすることで、別プロファイルで起動しにくくする。
    起動直後の URL は about:blank のみ。表示したい URL は接続後の action 側 page.goto。
    """
    base = _normalize_cdp_base(cdp_url)
    cands = _cdp_url_candidates(cdp_url)
    logger.info("CDP 接続候補: %s", cands)
    if _is_wsl():
        _log_wsl_cdp_environment()

    pre = probe_all_cdp_candidates(cdp_url, per_try_timeout=0.6)
    for b, (ok, line) in pre.items():
        logger.debug("起動前プローブ %s -> %s %s", b, "OK" if ok else "NG", line)
    up = first_cdp_url_that_listens(cdp_url, per_try_timeout=0.6)
    if up:
        logger.info("CDP は起動中です: %s", up)
        return up
    if not _is_local_cdp_url(cdp_url):
        for b, (ok, line) in pre.items():
            logger.error("CDP 失敗(リモート想定) %s: %s", b, line)
        raise RuntimeError(
            f"CDP ({base}) に接続できません。リモートの場合、先にそのホストで "
            f"Chrome を起動し --cdp URL を合わせてください。"
        )
    logger.info(
        "CDP 未応答のためローカルで Chrome を起動します: ポート %s, プロファイル %s",
        urlparse(base).port or 9222,
        user_data_dir,
    )
    for b, (ok, line) in pre.items():
        logger.info("起動前の到達性: %s -> %s", b, line if not ok else "OK /json/version")
    bin_path = _find_chrome_executable()
    if not bin_path:
        raise RuntimeError(
            "Google Chrome 実行ファイルが見つかりません。"
            "YOUTUBE_CDP_CHROME_BIN または CHROME_PATH にフルパスを設定するか、"
            "PATH に google-chrome / chromium 等を入れてください。"
        )
    bin_path = _resolve_linux_chrome_elf(bin_path)
    if _is_wsl() and bin_path:
        bn = os.path.basename(bin_path).lower()
        if bn.startswith("google-chrome") and "chromium" not in bn:
            logger.warning(
                "WSL: 採用した実行ファイルは %s です。Windows 版を起こす「ラッパー」だと、"
                "Linux 側の --user-data-dir が使われず、新規プロファイルでも常にログイン済み"
                "のように見えたり、CDP(9222)が WSL から届きません。対策: `sudo apt install "
                "chromium-browser` 等で Linux 版を入れ、"
                "YOUTUBE_CDP_CHROME_BIN=/usr/bin/chromium（または which chromium）を指定。",
                bin_path,
            )
    proc: subprocess.Popen | None = None
    chrome_err: str = ""
    try:
        proc, chrome_err = _launch_chrome_cdp(
            cdp_url, bin_path, user_data_dir, headless=chromium_headless
        )
    except OSError as e:
        raise RuntimeError(f"Chrome の起動に失敗しました: {e}") from e
    t0 = time.time()
    deadline = t0 + max_wait_sec
    last_log = 0.0
    parent_exited_noted = False
    while time.time() < deadline:
        up2 = first_cdp_url_that_listens(cdp_url, per_try_timeout=0.8)
        if up2:
            logger.info("CDP 応答を確認しました: %s", up2)
            return up2
        now = time.time()
        elapsed = now - t0
        if proc is not None and proc.poll() is not None and not parent_exited_noted and elapsed < 10.0:
            parent_exited_noted = True
            # WSL の /usr/bin/google-chrome は Windows の chrome.exe を起動し親 PID が即終了(0)することが多い
            logger.warning(
                "起動した Chrome の親プロセスはすでに終了しています: pid=%s returncode=%s。 "
                "WSL では /usr/bin/google-chrome がラッパーで、Windows 上の子が CDP を握る例があります。 "
                "CDP は上記の候補 IP:9222 を引き続き試します。 "
                "うまくいかなければ YOUTUBE_CDP_CHROME_BIN=/mnt/c/.../chrome.exe や "
                "同マシン上の native Chromium を指定する方法もあります。",
                proc.pid,
                proc.returncode,
            )
        if now - last_log >= 12.0:
            last_log = now
            probe = probe_all_cdp_candidates(cdp_url, per_try_timeout=0.6)
            lines = [f"  {u}: {'OK' if ok else detail}" for u, (ok, detail) in probe.items()]
            logger.info(
                "CDP 待機中: 経過 %.0fs / 残り約 %.0fs\n%s",
                elapsed,
                max(0.0, deadline - now),
                "\n".join(lines),
            )
            if proc is not None and proc.poll() is not None and parent_exited_noted:
                logger.debug("親の Popen プロセスは終了済み（上で説明）pid=%s", proc.pid)
        if logger.isEnabledFor(logging.DEBUG):
            probe_d = probe_all_cdp_candidates(cdp_url, per_try_timeout=0.5)
            for u, (ok, detail) in probe_d.items():
                logger.debug("待機ループ: %s -> %s %s", u, ok, detail)
        time.sleep(0.35)
    final = probe_all_cdp_candidates(cdp_url, per_try_timeout=1.0)
    for b, (ok, line) in final.items():
        logger.error("最終プローブ %s: %s %s", b, "OK" if ok else "NG", line)
    if proc is not None:
        rc = proc.poll()
        if rc is not None:
            logger.error(
                "親 Popen 上の Chrome は終了: pid=%s returncode=%s（WSL ラッパーなら Windows 上の子は残っている可能性）",
                proc.pid,
                rc,
            )
        else:
            logger.error("Chrome 親プロセスはまだ生きている: pid=%s (CDP だけ届いていない)", proc.pid)
    if chrome_err:
        tail = _read_text_file_tail(chrome_err, max_bytes=8000)
        logger.error("Chrome stderr ログ末尾 (ファイル=%s):\n%s", chrome_err, tail)
    cands_s = ", ".join(cands)
    raise RuntimeError(
        f"{max_wait_sec:.0f} 秒以内に次のいずれの json/version も応答しませんでした: {cands_s}\n"
        "ヒント: (1) `ip -4 route show` に 172.x 系の via が出る場合、default の 192.168.x.1 は家のルータのことが多く、"
        "WSL2 の Windows 仮想 NIC(例: 172.30.0.1)とは別。 "
        "ログの「WSL から試す…候補」に 172 系が含まれているか確認。 "
        "(2) 127.0.0.1:9222 は WSL 内のループバックのため、Windows 上の Chrome には届かない。 "
        "(3) `ip -4 addr show dev eth0` で 172.16/12 内なら、同サブの x.x.0.1 へ `curl` を試す。 "
        "(4) Windows ファイアウォールで 9222/TCP を許可。 "
        "(5) WSL の google-chrome がラッパーなら YOUTUBE_CDP_CHROME_BIN=/mnt/c/.../chrome.exe 等の明示。 "
        "(6) ホスト IP を手動で YOUTUBE_CDP_WINDOWS_HOST_IP=（Windows の IPv4; ipconfig 等）を設定。"
    )


def _click_locator_with_visible_or_force(
    el: Locator, click_timeout_ms: int, log_label: str
) -> None:
    """
    先に view 内へ。visible 待ち（動画・UI では aria 付きボタンが hidden のままのことがある）で
    失敗したら force クリックで試す。
    """
    el.scroll_into_view_if_needed(timeout=click_timeout_ms)
    try:
        el.wait_for(state="visible", timeout=click_timeout_ms)
        el.click(timeout=click_timeout_ms)
    except Exception as e:
        logger.warning(
            "%s: 表示待ち/通常クリックに失敗。force クリックを試します: %s",
            log_label,
            e,
        )
        el.scroll_into_view_if_needed(timeout=click_timeout_ms)
        el.click(timeout=click_timeout_ms, force=True)


def _gemini_debug_artifacts_enabled() -> bool:
    v = (os.environ.get("YOUTUBE_CDP_GEMINI_DEBUG") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _log_gemini_page_brief(page: Page) -> None:
    try:
        logger.info("ページ: url=%s", page.url)
    except Exception as e:
        logger.warning("ページ url 取得失敗: %s", e)
    try:
        logger.info("ページ: title=%r", (page.title() or "").strip())
    except Exception as e:
        logger.warning("ページ title 取得失敗: %s", e)


def _write_gemini_debug_artifacts(page: Page, reason: str) -> None:
    """HTML・フルページ画像・body 抜粋。未ログイン/bot/言語違いの切り分け用。"""
    base = _REPO_DIR
    try:
        p_png = base / GEMINI_DEBUG_FULL_PNG
        page.screenshot(path=str(p_png), full_page=True, timeout=180_000)
        logger.info("診断: フルページ画像 %s（%s）", p_png, reason)
    except Exception as e:
        logger.warning("診断: フルページ画像失敗: %s", e)
    try:
        html = page.content()
        if len(html) > GEMINI_DEBUG_HTML_MAX_CHARS:
            html = (
                html[: GEMINI_DEBUG_HTML_MAX_CHARS] + f"\n<!-- 省略 {len(html) - GEMINI_DEBUG_HTML_MAX_CHARS} 文字 -->\n"
            )
        (base / GEMINI_DEBUG_HTML).write_text(html, encoding="utf-8")
        logger.info("診断: HTML %s", base / GEMINI_DEBUG_HTML)
    except Exception as e:
        logger.warning("診断: HTML 保存失敗: %s", e)
    try:
        u = page.url
        t = (page.title() or "").strip()
        try:
            body = page.locator("body").inner_text(timeout=10_000)
        except Exception:
            body = ""
        if len(body) > GEMINI_DEBUG_BODY_MAX_CHARS:
            body = body[: GEMINI_DEBUG_BODY_MAX_CHARS] + "…\n(省略)"
        meta = f"reason={reason}\nurl={u}\ntitle={t}\n\n--- body 抜粋（先頭 {GEMINI_DEBUG_BODY_MAX_CHARS} 文字まで）---\n{body}\n"
        (base / GEMINI_DEBUG_META_TXT).write_text(meta, encoding="utf-8")
        logger.info("診断: 本文抜粋 %s", base / GEMINI_DEBUG_META_TXT)
    except Exception as e:
        logger.warning("診断: meta/本文保存失敗: %s", e)


def _click_youtube_chat_entrypoint(
    page: Page, name_pattern: str, click_timeout_ms: int
) -> None:
    for sel in (SELECTOR_YOUTUBE_CHAT_ENTRYPOINT, SELECTOR_YOUTUBE_CHAT_ENTRYPOINT_LOOSE):
        loc = page.locator(sel)
        if loc.count() > 0:
            _click_locator_with_visible_or_force(
                loc.first, click_timeout_ms, f"Gemini 入口 {sel!r}"
            )
            return
    by_label = page.get_by_role("button", name="質問する")
    if by_label.count() > 0:
        _click_locator_with_visible_or_force(
            by_label.first, click_timeout_ms, "Gemini 入口 (name=質問する)"
        )
        return
    reg = _name_regex_for_playwright(name_pattern)
    target = page.get_by_role("button", name=reg)
    if target.count() == 0:
        target = page.get_by_label(reg)
    if target.count() == 0:
        target = page.get_by_text(reg, exact=False)
    _click_locator_with_visible_or_force(
        target.first, click_timeout_ms, f"Gemini 入口 (正規表現 {name_pattern!r})"
    )


def _click_and_fill_question_input(page: Page, text: str, timeout_ms: int) -> Locator:
    # 「質問する」までは別要素。ここは #footer 内の yt-chat-input-view-model 優先
    try:
        page.wait_for_selector(
            SELECTOR_FOOTER_CHAT_ROOT,
            state="visible",
            timeout=min(timeout_ms, 25_000),
        )
    except Exception:
        pass
    root = page.locator(SELECTOR_FOOTER_CHAT_ROOT)
    el: Locator
    if root.count() > 0:
        inner = root.locator(
            'textarea, input, [contenteditable="true"], [role="textbox"]'
        )
        if inner.count() > 0:
            el = inner.first
        else:
            el = _locate_question_input_fallback(page)
    else:
        el = _locate_question_input_fallback(page)
    el.wait_for(state="visible", timeout=timeout_ms)
    el.scroll_into_view_if_needed()
    el.click(timeout=timeout_ms)
    _type_chat_prompt_like_user(el, text)
    time.sleep(POST_PROMPT_SETTLE_SEC)
    el.press(" ")
    return el


def _locate_question_input_fallback(page: Page) -> Locator:
    loc = page.get_by_role("textbox", name=_NAME_QUESTION_PLACEHOLDER)
    if loc.count() == 0:
        loc = page.get_by_placeholder("質問を入力")
    if loc.count() == 0:
        loc = page.get_by_label("質問を入力")
    if loc.count() == 0:
        loc = page.locator(
            'textarea[placeholder="質問を入力"], input[placeholder="質問を入力"]'
        )
    if loc.count() == 0:
        loc = page.locator(
            'textarea[placeholder*="質問を入力"], input[placeholder*="質問を入力"]'
        )
    if loc.count() == 0:
        loc = page.locator(
            "[contenteditable='true'][aria-label*='質問']"
        )
    if loc.count() == 0:
        loc = page.get_by_text("質問を入力", exact=True)
    return loc.first


def _type_chat_prompt_like_user(el: Locator, text: str, per_char_ms: int = 35) -> None:
    """
    YouTube 等の制御付き input は fill() だけだと内部 state が更新されず送信が有効化されないことがある。
    実キー入力に近い press_sequentially を使う。
    """
    time.sleep(0.05)
    if sys.platform == "darwin":
        el.press("Meta+a")
    else:
        el.press("Control+a")
    el.press("Backspace")
    el.press_sequentially(text, delay=per_char_ms)


# aria / 可視テキストに依存せず: YouTube 送信アイコンのスタイル型（無効は ytSpecButtonShapeNextDisabled / aria-disabled / disabled）
SELECTOR_YOUTUBE_SEND_ICON = (
    "button.ytSpecButtonShapeNextIconButton"
    ":not(.ytSpecButtonShapeNextDisabled):not([aria-disabled='true']):not([disabled])"
)


def _submit_youtube_chat_prompt(
    page: Page, input_box: Locator, click_timeout_ms: int
) -> None:
    """
    ① #footer yt-chat-input-view-model 内の form>button-view-model>button（構造はユーザー提供の DOM 準拠。
       yt-touch-feedback の子 div ではなく button 本体をクリック）
    ② 同枠内の紙飛行機クラス ③ ページ全体のクラス ④ Enter
    """
    cap = min(click_timeout_ms / 1000.0, 15.0)
    deadline = time.time() + cap

    def _poll_send(loc: Locator) -> bool:
        n = loc.count()
        if n == 0:
            return False
        btn = loc.first
        if not btn.is_visible() or not btn.is_enabled():
            return False
        btn.scroll_into_view_if_needed()
        btn.click(timeout=5_000)
        return True

    footer_send = page.locator(SELECTOR_FOOTER_CHAT_SEND_BUTTON)
    while time.time() < deadline:
        if _poll_send(footer_send):
            return
        time.sleep(0.1)
    # ② フッタ内に限定してアイコンのみ（他 UI と取り違え防止）
    scoped_icon = page.locator(SELECTOR_FOOTER_CHAT_ROOT).locator(SELECTOR_YOUTUBE_SEND_ICON)
    t2 = time.time() + min(5.0, cap)
    while time.time() < t2:
        n = scoped_icon.count()
        if n > 0:
            btn = scoped_icon.nth(n - 1)
            if btn.is_visible() and btn.is_enabled():
                btn.scroll_into_view_if_needed()
                btn.click(timeout=5_000)
                return
        time.sleep(0.1)
    # ③ 従来: 全体からアイコンのみ
    t3 = time.time() + min(5.0, cap)
    by_class = page.locator(SELECTOR_YOUTUBE_SEND_ICON)
    while time.time() < t3:
        n = by_class.count()
        if n > 0:
            btn = by_class.nth(n - 1)
            if btn.is_enabled() and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click(timeout=5_000)
                return
        time.sleep(0.1)
    input_box.click(timeout=5_000)
    input_box.press("Enter")


def _inner_text_stripped(el: Locator) -> str:
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _wait_for_one_locator(
    page: Page,
    loc: Locator,
    count_before: int,
    timeout_ms: int,
    stream_settle_sec: float,
    poll_sec: float,
) -> str:
    """loc が指す markdown の最後の要素を待つ。count 送信前より増えて十分な文字が入ったら採用。"""
    deadline = time.time() + timeout_ms / 1000.0
    min_chars = 8

    while time.time() < deadline:
        n = loc.count()
        if n == 0:
            time.sleep(poll_sec)
            continue
        el = loc.nth(n - 1)
        try:
            el.scroll_into_view_if_needed()
        except Exception:
            pass
        text = _inner_text_stripped(el)
        # 送信前より markdown 件数が増えたあと（新しい吹き出し）の本文だけ採用。件数が増える前の
        # 「挨拶だけ」の安定テキストでは return しない。
        if n > count_before and len(text) >= min_chars:
            time.sleep(stream_settle_sec)
            return _inner_text_stripped(loc.nth(n - 1))
        time.sleep(poll_sec)

    n = loc.count()
    if n == 0:
        return ""
    if n > count_before:
        return _inner_text_stripped(loc.nth(n - 1))
    logger.warning(
        "markdown の件数が送信前 (before=%d) から増えず。新しい応答を待てませんでした (now=%d)。",
        count_before,
        n,
    )
    return ""


def _wait_for_markdown_result(
    page: Page,
    in_chat_count_before: int,
    global_md_count_before: int,
    timeout_ms: int,
    stream_settle_sec: float = 2.0,
    poll_sec: float = 0.4,
) -> str:
    """
    まず you-chat-item-view-model 内の markdown を待つ。ダメなら全ページの markdown-div。
    global_md_count_before は送信前の markdown-div 件数（フォールバック比較用）。
    """
    t1 = int(timeout_ms * 0.75) if timeout_ms > 8_000 else timeout_ms
    t2 = max(4_000, timeout_ms - t1) if timeout_ms > 8_000 else 0
    loc_item = page.locator(SELECTOR_MARKDOWN_IN_CHAT_ITEM)
    text = _wait_for_one_locator(
        page, loc_item, in_chat_count_before, t1, stream_settle_sec, poll_sec
    )
    if text:
        return text

    logger.warning(
        "you-chat-item 内の markdown を先に %d ms 待ちました。markdown-div 全体でフォールバック (残り %d ms)",
        t1,
        t2,
    )
    if t2 <= 0:
        t2 = timeout_ms
    return _wait_for_one_locator(
        page,
        page.locator(SELECTOR_MARKDOWN_REPLY_FALLBACK),
        global_md_count_before,
        t2,
        stream_settle_sec,
        poll_sec,
    )


def get_page(browser: Browser) -> Page:
    """接続直後: 既存コンテキストの先頭タブ、なければ新規ページ。"""
    if not browser.contexts:
        return browser.new_context().new_page()
    ctx: BrowserContext = browser.contexts[0]
    if ctx.pages:
        return ctx.pages[0]
    return ctx.new_page()


def _connect_over_cdp_logged(p: Playwright, cdp: str) -> Browser:
    logger.info("Playwright connect_over_cdp: %s", cdp)
    try:
        return p.chromium.connect_over_cdp(cdp)
    except Exception as e:
        logger.error(
            "connect_over_cdp 失敗: endpoint=%s 例外型=%s メッセージ=%s",
            cdp,
            type(e).__name__,
            e,
        )
        raise


def _page_goto_logged(page: Page, url: str) -> None:
    logger.info("page.goto: %s (domcontentloaded)", url)
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        logger.error("page.goto 失敗: url=%s 型=%s %s", url, type(e).__name__, e)
        raise


def action_goto(p: Playwright, cdp: str, url: str) -> None:
    browser = _connect_over_cdp_logged(p, cdp)
    page = get_page(browser)
    _page_goto_logged(page, url)
    # 既存 Chrome に接続しているため browser.close() は呼ばない


def action_search(
    p: Playwright, cdp: str, query: str, after_seconds: float
) -> None:
    """トップの検索欄に query を入れて Enter（ログインセッションをそのまま利用）。"""
    browser = _connect_over_cdp_logged(p, cdp)
    page = get_page(browser)
    _page_goto_logged(page, YOUTUBE)
    # 同意バナー等: 出たら消す（ロケールで文言が違うので複数候補）
    for name in ("Accept all", "すべて同意", "同意する"):
        btn = page.get_by_role("button", name=name)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            break
    search = page.locator('input[name="search_query"]')
    search.first.wait_for(state="visible", timeout=15000)
    search.first.click()
    search.first.fill(query)
    search.first.press("Enter")
    if after_seconds > 0:
        time.sleep(after_seconds)


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """
    最小限の .env パーサ（export 行・UTF-8 BOM 対応）。
    ``python-dotenv`` 非導入時や、行形式の違いのフォールバック用。
    """
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        k, _, rest = s.partition("=")
        k, rest = k.strip(), rest.strip()
        if "#" in rest and not (rest.startswith('"') or rest.startswith("'")):
            rest = rest.split("#", 1)[0].strip()
        v = rest.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _load_dotenv_if_available() -> None:
    """リポジトリ直下の .env を読む（GMAIL_*, MAIL_TO 等）。"""
    p = _REPO_DIR / ".env"
    if not p.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(p, override=False)
    except ImportError:
        logger.warning(
            "python-dotenv が未インストールです。pip install python-dotenv か、.env の手動読み取りに頼ります。",
        )
    # dotenv 無し・export 行差・一部キー抜けの補完: 空のキーだけ埋める
    for k, v in _parse_dotenv_file(p).items():
        if v and not (os.environ.get(k) or "").strip():
            os.environ[k] = v
    if logger.isEnabledFor(logging.DEBUG):
        sk = {x for x in os.environ if any(x.startswith(p) for p in ("MAIL", "GMAIL", "EMAIL", "SMTP_"))}
        logger.debug("環境(メール関連キー名のみ): %s", sorted(sk))


def _mail_to_from_environ() -> str:
    """送信先。よく使う .env キー名を列挙。"""
    for key in (
        "MAIL_TO",
        "RESULT_EMAIL_TO",
        "EMAIL_TO",
        "GMAIL_TO",
        "TO_EMAIL",
        "SMTP_TO",
        "SEND_TO",
    ):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return ""


def _youtube_title_from_page(page: Page) -> str:
    """動画タイトル。document.title から ' - YouTube' を除く。失敗時はプレースホルダ。"""
    try:
        t = (page.title() or "").strip()
        for suf in (" - YouTube", " - YouTube Studio"):
            if t.endswith(suf):
                t = t[: -len(suf)].strip()
        if t:
            return t
    except Exception:
        pass
    return "（タイトル不明）"


def _maybe_send_gemini_result_email(
    page: Page, video_url: str, answer_text: str, to_email: str
) -> bool:
    """
    一時ディレクトリに summary.txt / video_info.json を書き、send_result_email で送る。
    認証は .env または環境の GMAIL_USER, GMAIL_APP_PASSWORD。
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False
    d = tempfile.mkdtemp(prefix="yt_gemini_mail_")
    try:
        sp = os.path.join(d, "summary.txt")
        with open(sp, "w", encoding="utf-8") as f:
            f.write(answer_text or "")
        with open(os.path.join(d, "video_info.json"), "w", encoding="utf-8") as f:
            json.dump({"title": _youtube_title_from_page(page)}, f, ensure_ascii=False, indent=2)
        from send_result_email import send_result_email

        return bool(send_result_email(d, to_email, video_url))
    except Exception as e:
        logger.error("メール送信中にエラー: %s", e, exc_info=logger.isEnabledFor(logging.DEBUG))
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def action_home(p: Playwright, cdp: str, after_seconds: float) -> None:
    browser = _connect_over_cdp_logged(p, cdp)
    page = get_page(browser)
    _page_goto_logged(page, YOUTUBE)
    if after_seconds > 0:
        time.sleep(after_seconds)


def action_gemini(
    p: Playwright,
    cdp: str,
    url: str,
    name_pattern: str,
    settle_seconds: float,
    click_timeout_ms: int,
    after_entry_seconds: float,
    prompt: str,
    response_timeout_ms: int,
    after_seconds: float,
    *,
    send_email: bool = False,
    email_to: str | None = None,
) -> bool:
    """
    指定 URL を開き、チャット入口「質問する」→ 入力 → 送信 → markdown-div の応答をログ出力。
    send_email が True のとき要約を send_result_email 経由で送信（GMAIL_USER / GMAIL_APP_PASSWORD 要）。
    送信先は email_to、なければ環境の MAIL_TO 等（_mail_to_from_environ）。
    戻り値: 成功 True。メール送信を試みたが失敗した場合 False。
    """
    browser = _connect_over_cdp_logged(p, cdp)
    page = get_page(browser)
    _page_goto_logged(page, url)
    time.sleep(GEMINI_POST_GOTO_SCREENSHOT_SEC)
    shot = _REPO_DIR / GEMINI_SCREENSHOT_FILENAME
    try:
        page.screenshot(path=str(shot), full_page=False)
        logger.info("スクリーンショット: %s", shot)
    except Exception as e:
        logger.warning("スクリーンショット失敗: %s", e)
    _log_gemini_page_brief(page)
    if _gemini_debug_artifacts_enabled():
        _write_gemini_debug_artifacts(page, "YOUTUBE_CDP_GEMINI_DEBUG=1")
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    try:
        _click_youtube_chat_entrypoint(page, name_pattern, click_timeout_ms)
    except Exception as e:
        _write_gemini_debug_artifacts(page, f"Gemini 入口失敗: {e!r}")
        logger.error(
            "Gemini チャット入口（「質問する」等）に到達できませんでした。"
            " 未ログイン・ボット確認画面・UI 言語・リージョンでボタン文言が違う場合があります。"
            " 診断ファイル（%s / %s / %s）を確認するか、"
            " 事前に YOUTUBE_CDP_GEMINI_DEBUG=1 で常に同ファイルを出せます。",
            GEMINI_DEBUG_FULL_PNG,
            GEMINI_DEBUG_HTML,
            GEMINI_DEBUG_META_TXT,
        )
        raise
    if after_entry_seconds > 0:
        time.sleep(after_entry_seconds)
    in_chat_md_before = page.locator(SELECTOR_MARKDOWN_IN_CHAT_ITEM).count()
    global_md_before = page.locator(SELECTOR_MARKDOWN_REPLY_FALLBACK).count()
    input_box = _click_and_fill_question_input(page, prompt, click_timeout_ms)
    _submit_youtube_chat_prompt(page, input_box, click_timeout_ms)
    answer = _wait_for_markdown_result(
        page,
        in_chat_md_before,
        global_md_before,
        response_timeout_ms,
    )
    if answer:
        logger.info("Gemini 応答 (you-chat-item > markdown-div):\n%s", answer)
    else:
        logger.info("Gemini 応答: (空、または取得タイムアウト)")

    if send_email:
        to = (email_to or "").strip() or _mail_to_from_environ()
        if not to:
            logger.warning(
                "メール送信スキップ: 送信先が空。--email-to または .env / 環境変数の "
                "MAIL_TO, EMAIL_TO, GMAIL_TO, RESULT_EMAIL_TO 等を設定してください。"
            )
        else:
            if not _maybe_send_gemini_result_email(page, url, answer or "", to):
                return False

    if after_seconds > 0:
        time.sleep(after_seconds)
    return True


def _cdp_parent_parser() -> argparse.ArgumentParser:
    """ルートとサブの両方で `--cdp` を受け取る（parents 共有）。"""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--cdp",
        default=DEFAULT_CDP,
        help=f"CDP エンドポイント（デフォルト: {DEFAULT_CDP}）",
    )
    p.add_argument(
        "--no-auto-chrome",
        action="store_true",
        help="起動中チェック＆未起動時の Chrome 自動起動をしない（従来どおり手動起動必須）",
    )
    p.add_argument(
        "--cdp-wait-sec",
        type=float,
        default=60.0,
        help="自動起動したあと、CDP が応答するまで待つ最長秒数",
    )
    p.add_argument(
        "--chrome-user-data-dir",
        default=None,
        metavar="DIR",
        help=(
            "CDP 用 Chrome の --user-data-dir 。未指定は YOUTUBE_CDP_USER_DATA_DIR 、"
            "WSL+リポが /mnt/c のときは ~/.local/share/...（drvfs 上は CDP 失敗しやすい）"
        ),
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="詳細ログ（CDP 待機の毎回プローブ、Chrome 起動行の全引数等）",
    )
    p.add_argument(
        "--new-chrome-profile",
        action="store_true",
        help=(
            "都度 新しい user-data-dir を採用（基準パス名に日時+PID のサフィックスで隣接作成）。"
            "前回のログインは引き継がれません。"
            " --use-repo-chrome-profile と併用ならリポ内に chrome_cdp_profile_日時_PID ができる。"
        ),
    )
    p.add_argument(
        "--use-repo-chrome-profile",
        action="store_true",
        help=(
            "このリポジトリ直下の chrome_cdp_profile/ を常に --user-data-dir にする（同じディレクトリを使い回し）。"
            "YOUTUBE_CDP_USER_DATA_DIR や --chrome-user-data-dir より優先。"
            "追跡するなら .gitignore から chrome_cdp_profile/ を外す（プライベートリポ・Cookie に注意）。"
            "環境変数 YOUTUBE_CDP_USE_REPO_CHROME_PROFILE=1 でも同じ。"
        ),
    )
    p.add_argument(
        "--chromium-headless",
        action="store_true",
        help=(
            "自動起動する Chromium をヘッドレス (--headless=new)。画面は出ない。"
            "環境変数 YOUTUBE_CDP_CHROME_HEADLESS=1 でも有効。手動起動の Chrome には影響しない。"
        ),
    )
    return p


def _build_parser() -> argparse.ArgumentParser:
    cdp_p = _cdp_parent_parser()
    ap = argparse.ArgumentParser(
        description="Playwright で CDP 接続先の YouTube を操作",
        parents=[cdp_p],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s_home = sub.add_parser("home", help="YouTube トップを開く", parents=[cdp_p])
    s_home.add_argument(
        "--wait",
        type=float,
        default=5.0,
        help="表示後に待つ秒数（0 で即終了）",
    )

    s_goto = sub.add_parser("goto", help="任意 URL を開く", parents=[cdp_p])
    s_goto.add_argument("url", help="https://...")

    s_search = sub.add_parser("search", help="YouTube トップで検索", parents=[cdp_p])
    s_search.add_argument("query", help="検索語")
    s_search.add_argument(
        "--wait",
        type=float,
        default=8.0,
        help="検索後に待つ秒数（0 で即終了）",
    )

    s_gem = sub.add_parser(
        "gemini",
        help="「質問する」→「要約して」等を入力→「送信」（you-chat-entrypoint-button）",
        parents=[cdp_p],
    )
    s_gem.add_argument(
        "--url",
        default=DEFAULT_GEMINI_VIDEO,
        help=f"開く YouTube 動画 URL（デフォルト: {DEFAULT_GEMINI_VIDEO}）",
    )
    s_gem.add_argument(
        "--name-regex",
        default=DEFAULT_GEMINI_NAME_PATTERN,
        help="入口セレクタで見つからないとき、accessible name 等に当てる正規表現（大小は無視。先頭の (?i) は付けないでください）",
    )
    s_gem.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="ページ表示後、クリック前に待つ秒数（UI 描画用）",
    )
    s_gem.add_argument(
        "--click-timeout",
        type=int,
        default=20_000,
        help="各要素が表示・操作可能になるまでの待ちミリ秒",
    )
    s_gem.add_argument(
        "--after-entry",
        type=float,
        default=DEFAULT_AFTER_ENTRY_SEC,
        help="「質問する」クリック後、入力欄操作までの待ち秒数",
    )
    s_gem.add_argument(
        "--prompt",
        "--summary",
        default=DEFAULT_GEMINI_PROMPT,
        help="「質問を入力」欄に入れる文字。--summary は --prompt と同じ（デフォルト: 要約して）",
    )
    s_gem.add_argument(
        "--response-timeout",
        type=int,
        default=DEFAULT_RESPONSE_TIMEOUT_MS,
        help="送信後、markdown-div 応答を待つ最大ミリ秒（デフォルト: 120000）",
    )
    s_gem.add_argument(
        "--wait",
        type=float,
        default=3.0,
        help="応答ログ出力後、終了前に待つ秒数（0 で即終了）",
    )
    s_gem.add_argument(
        "--send-email",
        action="store_true",
        help="メール送信を明示有効化（省略時も、送信先が .env 等にあれば既定で送信）。",
    )
    s_gem.add_argument(
        "--no-send-email",
        action="store_true",
        help="送信先があってもメールしない（YOUTUBE_CDP_SEND_EMAIL=0 と同様）。",
    )
    s_gem.add_argument(
        "--email-to",
        default=None,
        metavar="ADDR",
        help="送信先メール。未指定は MAIL_TO / RESULT_EMAIL_TO 等（.env 可）",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_available()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    args: Namespace
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1

    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)
        for _h in logging.getLogger().handlers:
            _h.setLevel(logging.DEBUG)

    cdp_effective = _normalize_cdp_base(args.cdp)
    if not getattr(args, "no_auto_chrome", False):
        ud = getattr(args, "chrome_user_data_dir", None)
        ud_str = (ud and str(ud).strip()) or ""
        env_ud = (os.environ.get("YOUTUBE_CDP_USER_DATA_DIR") or "").strip()

        if _wants_use_repo_chrome_profile(args):
            if ud_str:
                logger.warning(
                    "--use-repo-chrome-profile のため --chrome-user-data-dir は無視します (%s)",
                    ud_str,
                )
            if env_ud:
                logger.warning(
                    "--use-repo-chrome-profile のため YOUTUBE_CDP_USER_DATA_DIR は無視します (%s)",
                    env_ud,
                )
            chrome_profile_dir = str(REPO_CHROME_CDP_PROFILE)
            logger.info(
                "リポジトリ内プロファイル (--use-repo-chrome-profile): %s",
                chrome_profile_dir,
            )
            if _is_wsl() and chrome_profile_dir.replace("\\", "/").startswith("/mnt/"):
                bin_guess = _find_chrome_executable()
                if bin_guess and _is_wsl_invoked_windows_chrome_exe(bin_guess):
                    logger.warning(
                        "WSL でリポが /mnt/ 上です。起動先が Windows 版 chrome.exe のときは "
                        "user-data-dir を wslpath で C:\\ 形式に渡します。CDP が届かないときは "
                        "YOUTUBE_CDP_WINDOWS_HOST_IP を参照してください。"
                    )
                else:
                    # Linux の google-chrome / Chromium は、drvfs 上の --user-data-dir だと
                    # 実質デフォルト扱いになり「DevTools remote debugging requires a non-default…」で
                    # 9222 が上がらないことが多い。ext4 側に寄せる。
                    nat = _wsl_nativelike_chrome_user_data_dir()
                    logger.warning(
                        "WSL + Linux 系ブラウザ: drvfs 上の chrome_cdp_profile では CDP(9222)が立ちにくいため、"
                        "実際の --user-data-dir を ext4 に切り替えます: %s",
                        nat,
                    )
                    logger.info(
                        "  リポ内 chrome_cdp_profile/ との同期が必要なら scripts/copy_cdp_profile_to_repo.sh 等を使ってください。"
                    )
                    chrome_profile_dir = nat
        elif not ud_str:
            chrome_profile_dir = default_cdp_chrome_user_data_dir()
        else:
            chrome_profile_dir = os.path.expanduser(ud_str)

        if getattr(args, "new_chrome_profile", False):
            chrome_profile_dir = new_chrome_profile_user_data_dir(chrome_profile_dir)
            logger.info("新規 Chrome プロファイル (--new-chrome-profile): %s", chrome_profile_dir)
        try:
            cdp_effective = ensure_cdp_chrome(
                args.cdp,
                max_wait_sec=float(getattr(args, "cdp_wait_sec", 60.0)),
                user_data_dir=chrome_profile_dir,
                chromium_headless=_wants_chromium_headless(args),
            )
        except RuntimeError as e:
            logger.error("%s", e)
            return 1
    else:
        if getattr(args, "new_chrome_profile", False):
            logger.warning(
                "--new-chrome-profile は --no-auto-chrome では Chrome を起動しないため "
                "無視されます。手動起動の --user-data-dir に、似た形式のパスを使ってください。"
            )
        if _wants_use_repo_chrome_profile(args):
            logger.warning(
                "--use-repo-chrome-profile は手動起動時は効きません。同じ保存先で起動する例: %s",
                str(REPO_CHROME_CDP_PROFILE),
            )
        probe_na = probe_all_cdp_candidates(args.cdp, per_try_timeout=1.0)
        for b, (ok, line) in probe_na.items():
            logger.info(
                "CDP (--no-auto-chrome) 到達性: %s -> %s",
                b,
                "OK" if ok else line,
            )
        up = first_cdp_url_that_listens(args.cdp, per_try_timeout=1.0)
        if up:
            cdp_effective = up
            logger.info("CDP 接続先: %s", cdp_effective)
        else:
            logger.warning(
                "CDP (--no-auto-chrome) はどの候補も /json/version に失敗。指定のまま接続を試みます: %s",
                cdp_effective,
            )

    with sync_playwright() as p:
        if args.cmd == "home":
            action_home(p, cdp_effective, args.wait)
        elif args.cmd == "goto":
            action_goto(p, cdp_effective, args.url)
        elif args.cmd == "search":
            action_search(p, cdp_effective, args.query, args.wait)
        elif args.cmd == "gemini":
            _arg_to = getattr(args, "email_to", None)
            _eto = (str(_arg_to).strip() if _arg_to else "") or _mail_to_from_environ() or None
            _flag = (os.environ.get("YOUTUBE_CDP_SEND_EMAIL") or "").strip().lower()
            _env_on = _flag in ("1", "true", "yes", "on")
            _env_off = _flag in ("0", "false", "no", "off")
            _no_send = bool(getattr(args, "no_send_email", False))
            if _no_send or _env_off:
                _send = False
            elif bool(getattr(args, "send_email", False)) or _env_on:
                _send = True
            else:
                # 送信先が分かるときだけ既定で送る（--send-email 省略可）
                _send = bool(_eto)
            if not action_gemini(
                p,
                cdp_effective,
                args.url,
                args.name_regex,
                args.settle,
                args.click_timeout,
                args.after_entry,
                args.prompt,
                args.response_timeout,
                args.wait,
                send_email=_send,
                email_to=_eto,
            ):
                return 1
        else:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

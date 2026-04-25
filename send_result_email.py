#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文字起こし完了後、結果ファイルを Gmail SMTP で送信する。"""

import html
import os
import sys
import json
import glob
import logging
import smtplib
from typing import Optional
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

PYTHON_NAME = os.path.basename(__file__)
logger = logging.getLogger(__name__)

# 要約 API が失敗したとき summary.txt に書き、メール本文【要約】にも載せる
SUMMARY_UNAVAILABLE_BODY = os.getenv(
    "SUMMARY_UNAVAILABLE_BODY",
    "【要約の自動生成は行えませんでした】\n"
    "（API のレート制限・一時エラー・キー不足などの可能性があります。）\n"
    "全文は添付の transcript.txt をご確認ください。",
).strip()


def write_summary_unavailable_placeholder(archive_dir: str) -> None:
    """要約に失敗した旨を summary.txt に書く。メール送信時に本文・添付に反映される。"""
    path = os.path.join(archive_dir, "summary.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(SUMMARY_UNAVAILABLE_BODY + "\n")
        logger.info("要約失敗用の summary.txt を書きました: %s : (%s)", path, PYTHON_NAME)
    except Exception as e:
        logger.warning("summary.txt（要約失敗プレースホルダ）の書き込みに失敗: %s : (%s)", e, PYTHON_NAME)


def _read_summary_for_body(summary_path: str) -> str:
    """summary.txt を UTF-8 で読み、本文用の文字列を返す。"""
    if not summary_path or not os.path.isfile(summary_path):
        return ""
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("summary.txt の読み込みに失敗しました: %s : (%s)", e, PYTHON_NAME)
        return ""


def _apply_body_length_limit(text: str) -> str:
    """環境変数 MAIL_BODY_SUMMARY_MAX_CHARS が正の整数なら本文を切り詰げる。"""
    raw = os.getenv("MAIL_BODY_SUMMARY_MAX_CHARS", "").strip()
    if not raw or not raw.isdigit():
        return text
    max_chars = int(raw)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + "\n\n（※ 長いため本文は省略しました。全文は添付の summary.txt をご覧ください。）"
    )


def _summary_markdown_to_html_fragment(md_text: str) -> str:
    """
    summary.txt（Markdown）を HTML 断片に変換。Gmail は本文を HTML で送れば体裁を再現できる。
    markdown 未インストール時はプレーンをエスケープして <pre> にする。
    """
    if not (md_text or "").strip():
        return ""
    try:
        import markdown as md_lib
    except ImportError:
        return (
            '<pre style="white-space:pre-wrap;font-family:inherit;font-size:14px;">'
            f"{html.escape(md_text)}"
            "</pre>"
        )
    return md_lib.markdown(md_text, extensions=["extra", "nl2br"])


def _build_html_body(title: str, video_url: str, summary_text: str) -> str:
    """メール本文（HTML）。タイトル・URL はエスケープ、要約は Markdown→HTML。"""
    safe_title = html.escape(title or "")
    safe_url = html.escape(video_url or "", quote=True)
    summary_html = _summary_markdown_to_html_fragment(summary_text)
    body_style = (
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "line-height:1.55;color:#202124;font-size:14px;max-width:720px;"
    )
    parts = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"</head><body style=\"{html.escape(body_style, quote=True)}\">",
        '<h2 style="font-size:16px;margin:0 0 12px;">Youtube文字起こし</h2>',
        f'<p style="margin:8px 0;"><strong>タイトル：</strong>{safe_title}</p>',
        '<p style="margin:8px 0;"><strong>URL：</strong>',
        f'<a href="{safe_url}">{html.escape(video_url or "")}</a></p>',
    ]
    if summary_html:
        parts.append('<h3 style="font-size:15px;margin:16px 0 8px;">【要約】</h3>')
        parts.append(
            '<div style="margin:8px 0 16px;padding:12px 14px;border:1px solid #e0e0e0;'
            'border-radius:8px;background:#fafafa;">'
        )
        parts.append(summary_html)
        parts.append("</div>")
    parts.append('<hr style="border:none;border-top:1px solid #dadce0;margin:16px 0;">')
    parts.append(
        "<p style=\"margin:0;\">その他の成果物（全文・字幕など）は添付ファイルをご確認ください。</p>"
    )
    parts.append("</body></html>")
    return "".join(parts)


def _find_subtitle_path(archive_dir: str) -> Optional[str]:
    """archive_dir 内の subtitle_ja.vtt または最初の subtitle_*.vtt を返す。"""
    ja = os.path.join(archive_dir, "subtitle_ja.vtt")
    if os.path.isfile(ja):
        return ja
    for path in sorted(glob.glob(os.path.join(archive_dir, "subtitle_*.vtt"))):
        return path
    return None


def send_result_email(
    archive_dir: str,
    to_email: str,
    video_url: str,
    *,
    from_email: Optional[str] = None,
    gmail_password: Optional[str] = None,
) -> bool:
    """
    archive_dir 内の summary.txt, video_info.json, transcript.txt, subtitle_*.vtt を添付して送信する。
    summary.txt の内容は本文【要約】に含める。Gmail 表示用に HTML（Markdown 変換）も送る（multipart/alternative）。
    本文が長すぎる場合は MAIL_BODY_SUMMARY_MAX_CHARS（文字数）で切り詰げ可能。
    from_email / gmail_password が None の場合は環境変数 GMAIL_USER, GMAIL_APP_PASSWORD を使用。
    """
    from_email = from_email or os.getenv("GMAIL_USER", "").strip()
    gmail_password = gmail_password or os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if not from_email or not gmail_password:
        msg = f"警告: GMAIL_USER または GMAIL_APP_PASSWORD が未設定です。メール送信をスキップします。 : ({PYTHON_NAME})"
        print(msg)
        logger.warning(msg)
        return False
    if not to_email or not to_email.strip():
        msg = f"警告: 送信先メールが空です。メール送信をスキップします。 : ({PYTHON_NAME})"
        print(msg)
        logger.warning(msg)
        return False

    video_info_path = os.path.join(archive_dir, "video_info.json")
    summary_path = os.path.join(archive_dir, "summary.txt")
    transcript_path = os.path.join(archive_dir, "transcript.txt")
    subtitle_path = _find_subtitle_path(archive_dir)

    title = "（タイトル不明）"
    if os.path.isfile(video_info_path):
        try:
            with open(video_info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
                title = (info.get("title") or title).strip()
        except Exception:
            pass

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email.strip()
    msg["Subject"] = f"[Youtube文字起こし]{title[:80]}"

    summary_text = _read_summary_for_body(summary_path)
    summary_text = _apply_body_length_limit(summary_text)

    lines = [
        "Youtube文字起こし",
        f"タイトル：{title}",
        f"URL：{video_url}",
        "",
    ]
    if summary_text:
        lines.extend(["【要約】", summary_text, ""])
    lines.extend(
        [
            "---",
            "その他の成果物（全文・字幕など）は添付ファイルをご確認ください。",
        ]
    )
    body_plain = "\n".join(lines)
    body_html = _build_html_body(title, video_url, summary_text)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_plain, "plain", "utf-8"))
    alt.attach(MIMEText(body_html, "html", "utf-8"))
    msg.attach(alt)

    attachment_count = 0
    for label, path in [
        ("summary.txt", summary_path),
        ("video_info.json", video_info_path),
        ("transcript.txt", transcript_path),
        ("subtitle_ja.vtt", subtitle_path),
    ]:
        if path and os.path.isfile(path):
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
            msg.attach(part)
            attachment_count += 1

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, gmail_password)
            server.sendmail(from_email, [to_email.strip()], msg.as_string())
        subj = msg.get("Subject", "")
        ok_msg = (
            f"✓ メール送信に成功しました: To={to_email.strip()}, From={from_email}, "
            f"Subject={subj!r}, 添付={attachment_count}件, archive_dir={archive_dir} : ({PYTHON_NAME})"
        )
        print(ok_msg)
        logger.info(ok_msg)
        return True
    except Exception as e:
        err = f"✗ メール送信に失敗しました: {e} : ({PYTHON_NAME})"
        print(err, file=sys.stderr)
        logger.error(err)
        return False

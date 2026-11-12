"""Sends the edition PDF to your phone.   python3 -m news.deliver [path-to-pdf]

Email (simplest; any number of recipients): GMAIL_USER + GMAIL_APP_PASSWORD (the Google app password) + EMAIL_TO (comma-separated addresses).
    One message with the PDF attached, sent through Gmail; recipients are blind-copied so they do not see each other's addresses.
Telegram (simple, free):   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
WhatsApp Cloud API (Meta): WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID + WHATSAPP_TO (digits with country code, e.g. 919812345678)
    optional WHATSAPP_TEMPLATE (+ WHATSAPP_TEMPLATE_LANG, default en): a Meta-approved template with a DOCUMENT header and one body variable
    (the date). Without a template, a plain document message is sent, which WhatsApp only delivers if you messaged that number in the last 24 hours.
Nothing is sent when the edition has no stories. Tokens are never printed."""
from __future__ import annotations

import json
import smtplib
import sys
from email.message import EmailMessage
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from core.secrets import load_secrets, redact

ROOT = Path(__file__).resolve().parent.parent


def _multipart(fields: dict, file_field: str, filename: str, data: bytes, mime: str) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _call(url: str, body: bytes, headers: dict, opener: Callable = urllib.request.urlopen, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def send_telegram(secrets: dict, pdf: Optional[Path], caption: str, opener: Callable = urllib.request.urlopen) -> str:
    tok, chat = secrets["TELEGRAM_BOT_TOKEN"], secrets["TELEGRAM_CHAT_ID"]
    if pdf is None:
        _call(f"https://api.telegram.org/bot{tok}/sendMessage", json.dumps({"chat_id": chat, "text": caption}).encode(), {"content-type": "application/json"}, opener)
        return "Telegram: message sent"
    body, ctype = _multipart({"chat_id": chat, "caption": caption[:1000]}, "document", pdf.name, pdf.read_bytes(), "application/pdf")
    _call(f"https://api.telegram.org/bot{tok}/sendDocument", body, {"content-type": ctype}, opener)
    return "Telegram: PDF sent"


def send_email(secrets: dict, pdf: Optional[Path], subject: str, text: str, smtp_factory: Callable = smtplib.SMTP_SSL, html: Optional[Path] = None) -> str:
    user, pw = secrets["GMAIL_USER"], secrets["GMAIL_APP_PASSWORD"]
    rcpt = [a.strip() for a in secrets["EMAIL_TO"].replace(";", ",").split(",") if "@" in a]
    if not rcpt:
        raise RuntimeError("EMAIL_TO has no valid address")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, user, subject          # To is yourself; the real recipients go in Bcc
    msg.set_content(text)
    if pdf is not None:
        msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    if html is not None:
        msg.add_attachment(html.read_bytes(), maintype="text", subtype="html", filename=html.name)    # the full page: expands and folds, opens in any browser
    with smtp_factory("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg, from_addr=user, to_addrs=rcpt)
    what = "notice" if pdf is None else "PDF + HTML" if html is not None else "PDF"
    return f"Email: {what} sent to {len(rcpt)} recipient(s)"


def send_whatsapp(secrets: dict, pdf: Path, caption: str, date_text: str, opener: Callable = urllib.request.urlopen) -> str:
    ver = secrets.get("WHATSAPP_API_VERSION", "v23.0")
    pid, tok, to = secrets["WHATSAPP_PHONE_NUMBER_ID"], secrets["WHATSAPP_TOKEN"], secrets["WHATSAPP_TO"].lstrip("+")
    base = f"https://graph.facebook.com/{ver}/{pid}"
    auth = {"Authorization": f"Bearer {tok}"}
    body, ctype = _multipart({"messaging_product": "whatsapp", "type": "application/pdf"}, "file", pdf.name, pdf.read_bytes(), "application/pdf")
    media = _call(f"{base}/media", body, {**auth, "content-type": ctype}, opener)["id"]
    if secrets.get("WHATSAPP_TEMPLATE"):
        msg = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
            "name": secrets["WHATSAPP_TEMPLATE"], "language": {"code": secrets.get("WHATSAPP_TEMPLATE_LANG", "en")},
            "components": [{"type": "header", "parameters": [{"type": "document", "document": {"id": media, "filename": pdf.name}}]},
                           {"type": "body", "parameters": [{"type": "text", "text": date_text}]}]}}
    else:
        msg = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": {"id": media, "filename": pdf.name, "caption": caption[:900]}}
    _call(f"{base}/messages", json.dumps(msg).encode(), {**auth, "content-type": "application/json"}, opener)
    return "WhatsApp: PDF sent" + (" (template)" if secrets.get("WHATSAPP_TEMPLATE") else " (plain document: needs an open 24-hour window)")


def deliver(secrets: dict, pdf: Path, stories: int, date_text: str, opener: Callable = urllib.request.urlopen,
            smtp_factory: Callable = smtplib.SMTP_SSL, headlines: Optional[list] = None) -> list[str]:
    log: list[str] = []
    has_tg = bool(secrets.get("TELEGRAM_BOT_TOKEN") and secrets.get("TELEGRAM_CHAT_ID"))
    has_wa = all(secrets.get(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TO"))
    has_mail = all(secrets.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "EMAIL_TO"))
    if not (has_tg or has_wa or has_mail):
        return ["No delivery channel configured (Email, Telegram or WhatsApp secrets missing); nothing sent."]
    if stories == 0:
        if has_mail:
            try:
                log.append(send_email(secrets, None, f"The Morning Edition {date_text}: could not be built",
                                      "No stories were read this morning, so no PDF was made. Check the run log on GitHub.", smtp_factory))
            except Exception as e:  # noqa: BLE001
                log.append("Email notice failed: " + redact(str(e), secrets))
        if has_tg:
            try:
                log.append(send_telegram(secrets, None, f"The Morning Edition for {date_text} could not be built (no stories were read). No PDF was sent. Check the run log.", opener))
            except Exception as e:  # noqa: BLE001
                log.append("Telegram notice failed: " + redact(str(e), secrets))
        return log + ["Edition has no stories: PDF not sent."]
    caption = f"The Morning Edition, {date_text}"
    top = "\n".join(f"- {h}" for h in (headlines or [])[:5])
    html = pdf.with_suffix(".html")
    html = html if html.exists() else None
    files = "the PDF (for reading on a phone) and the HTML page (open it in a browser to expand and fold sections)" if html else "the PDF"
    body = f"Your Morning Edition for {date_text} is attached ({stories} stories): {files}.\n\nIn this edition:\n{top}\n\nInformation only, not advice." if top else \
        f"Your Morning Edition for {date_text} is attached ({stories} stories): {files}. Information only, not advice."
    for name, ok, fn in (("Email", has_mail, lambda: send_email(secrets, pdf, f"The Morning Edition, {date_text}", body, smtp_factory, html)),
                         ("Telegram", has_tg, lambda: send_telegram(secrets, pdf, caption, opener)),
                         ("WhatsApp", has_wa, lambda: send_whatsapp(secrets, pdf, caption, date_text, opener))):
        if ok:
            try:
                log.append(fn())
            except Exception as e:  # noqa: BLE001
                log.append(f"{name} FAILED: " + redact(str(e), secrets))
    return log


def main() -> int:
    from datetime import datetime, timedelta, timezone
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted((ROOT / "reports").glob("news_*.pdf"))[-1]
    latest = json.loads((ROOT / "data" / "news" / "latest.json").read_text())
    stories = len(latest.get("stories", []))
    heads = [s.get("headline") or s.get("title") for s in latest.get("stories", [])[:5] if isinstance(s, dict)]
    date_text = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")
    log = deliver(load_secrets(), pdf, stories, date_text, headlines=[h for h in heads if h])
    print("\n".join(log))
    return 1 if any("FAILED" in x for x in log) else 0


if __name__ == "__main__":
    raise SystemExit(main())

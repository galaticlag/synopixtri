"""Reminder when folders are still waiting for a real name (ntfy, webhook or e-mail)."""

from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import time
import urllib.request
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Callable

from . import folders

log = logging.getLogger("synopixtri")
Sender = Callable[[SimpleNamespace, str, str, list], None]


def _post(url: str, data: bytes, content_type: str) -> None:
    request = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - URL set by the owner
        response.read(1024)


def send_ntfy(cfg: SimpleNamespace, title: str, body: str, items: list) -> None:
    _post(cfg.notify_url, body.encode(), "text/plain; charset=utf-8")


def send_webhook(cfg: SimpleNamespace, title: str, body: str, items: list) -> None:
    payload = {"title": title, "text": body, "folders": items}
    _post(cfg.notify_url, json.dumps(payload).encode(), "application/json")


def send_email(cfg: SimpleNamespace, title: str, body: str, items: list) -> None:
    if not (cfg.smtp_host and cfg.smtp_to):
        raise ValueError("smtp_host and smtp_to are required")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, cfg.smtp_from or cfg.smtp_user or "synopixtri", cfg.smtp_to
    msg.set_content(body)
    port = int(cfg.smtp_port)
    smtp = smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=20) if port == 465 else smtplib.SMTP(cfg.smtp_host, port, timeout=20)
    with smtp:
        if port != 465:
            smtp.starttls()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)


SENDERS: dict[str, Sender] = {"ntfy": send_ntfy, "webhook": send_webhook, "email": send_email}


def pending(conn: sqlite3.Connection, cfg: SimpleNamespace, now: float) -> list[dict]:
    """Folders with the provisional label for at least ``notify_after_days``."""
    out = []
    for item in folders.to_name(conn, cfg.placeholder_label, cfg.event_template):
        row = conn.execute(
            "SELECT j.started_at FROM folder f JOIN job j ON j.id=f.created_job_id WHERE f.path=?",
            (item["path"],),
        ).fetchone()
        if row is None or now - row["started_at"] < cfg.notify_after_days * 86400:
            continue
        out.append({**item, "name": item["path"].replace("\\", "/").rsplit("/", 1)[-1]})
    return out


def message(items: list[dict]) -> tuple[str, str]:
    title = "SynoPixtri: dossiers à nommer"
    lines = [f"{len(items)} dossier(s) attendent un nom. Renomme-les dans File Station :"]
    lines += [f"- {i['name']}" for i in items[:20]]
    if len(items) > 20:
        lines.append(f"... et {len(items) - 20} autres")
    return title, "\n".join(lines)


def check(conn: sqlite3.Connection, cfg: SimpleNamespace, now: float | None = None, *, force: bool = False) -> dict:
    """Send the reminder if due. Never raises: a failing channel is reported, not fatal."""
    now = time.time() if now is None else now
    if cfg.notify_kind == "none":
        return {"sent": False, "reason": "disabled"}
    items = pending(conn, cfg, now)
    if not items and not force:
        return {"sent": False, "reason": "nothing_to_name"}
    last = conn.execute("SELECT value FROM kv WHERE key='notify_last'").fetchone()
    if not force and last is not None and now - float(last["value"]) < cfg.notify_every_days * 86400:
        return {"sent": False, "reason": "too_soon"}
    title, body = message(items) if items else ("SynoPixtri: test", "Notification de test, tout fonctionne.")
    try:
        SENDERS[cfg.notify_kind](cfg, title, body, items)
    except Exception as exc:  # noqa: BLE001 - network, SMTP, DNS...
        log.warning("notification failed: %s", exc)
        return {"sent": False, "reason": "error", "error": f"{type(exc).__name__}: {exc}"}
    if items:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES('notify_last', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(now),),
        )
        conn.commit()
    return {"sent": True, "count": len(items)}

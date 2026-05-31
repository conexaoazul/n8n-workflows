#!/usr/bin/env python3
"""
OTP Service — validação de WhatsApp para o Lead Gate.

Providers: disabled | evolution | cloud
Degradação graciosa: se envio falhar, retorna fallback=True (nunca bloqueia lead).

Config (env):
  LEADGATE_WA_PROVIDER  = disabled|evolution|cloud  (default: disabled)
  LEADGATE_WA_ENABLED   = true|false                 (default: false)
  LEADGATE_WA_INSTANCE   = Evolution instance name
  LEADGATE_WA_BRAND     = nome exibido na mensagem     (default: Conexão Azul)
  EVOLUTION_BASE_URL    = Evolution API base URL
  EVOLUTION_API_KEY     = Evolution API key
  WHATSAPP_CLOUD_TOKEN  = Meta Graph API token
  WHATSAPP_PHONE_NUMBER_ID = Meta phone_number_id
  WHATSAPP_TEMPLATE_NAME  = template name (optional — uses text if empty)
  WHATSAPP_TEMPLATE_LANG   = template language (default: pt_BR)
"""

import json
import os
import re
import sqlite3
import secrets
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

DATA_DIR = Path(os.environ.get("LEAD_DATA_DIR", "/data/n8n-workflows/leads"))
DB_PATH = DATA_DIR / "otp.db"
TTL_SECONDS = 600
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = 45

_lock = threading.Lock()


def _wa_provider() -> str:
    provider = os.environ.get("LEADGATE_WA_PROVIDER", "disabled").strip().lower()
    return provider if provider in ("disabled", "evolution", "cloud") else "disabled"


def _wa_enabled() -> bool:
    return os.environ.get("LEADGATE_WA_ENABLED", "false").strip().lower() == "true"


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if not digits.startswith("55") and 10 <= len(digits) <= 11:
        digits = "55" + digits
    return digits


def _init_db() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS otp (
               phone TEXT PRIMARY KEY, code TEXT NOT NULL,
               created_at REAL NOT NULL, attempts INTEGER DEFAULT 0,
               last_sent REAL NOT NULL, verified INTEGER DEFAULT 0)"""
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

_init_db()


def _send_whatsapp_evolution(phone: str, code: str) -> bool:
    base = os.environ.get("EVOLUTION_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("EVOLUTION_API_KEY", "").strip()
    instance = os.environ.get("LEADGATE_WA_INSTANCE", "").strip()
    brand = os.environ.get("LEADGATE_WA_BRAND", "Conexão Azul")
    if not (base and key and instance):
        return False
    text = (f"*{brand}* — código de verificação: *{code}*\n\n"
            f"Use este código para liberar o acesso ao catálogo de automações. "
            f"Ele expira em 10 minutos. Se você não solicitou, ignore esta mensagem.")
    body = json.dumps({"number": phone, "text": text}).encode("utf-8")
    url = f"{base}/message/sendText/{instance}"
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json", "apikey": key})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def _resolve_template(lang: str) -> tuple:
    """Resolve template name and language based on user language.
    Returns (template_name, wa_lang_code).
    AUTHENTICATION templates are auto-approved by Meta."""
    # Map our lang codes to WhatsApp template language codes
    lang_map = {
        "pt-BR": ("ca_autenticacao_acesso_v1", "pt_BR"),
        "pt_BR": ("ca_autenticacao_acesso_v1", "pt_BR"),
        "en-US": ("ca_autenticacao_acesso_v1", "en_US"),
        "en_US": ("ca_autenticacao_acesso_v1", "en_US"),
        "es":    ("ca_autenticacao_acesso_v1", "es"),
        "es-ES": ("ca_autenticacao_acesso_v1", "es"),
    }
    # Allow env override for template name
    env_template = os.environ.get("WHATSAPP_TEMPLATE_NAME", "").strip()
    env_lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "").strip()
    if env_template:
        return env_template, env_lang or "pt_BR"
    return lang_map.get(lang, ("ca_autenticacao_acesso_v1", "pt_BR"))


def _send_whatsapp_cloud(phone: str, code: str, lang: str = "pt_BR") -> bool:
    token = os.environ.get("WHATSAPP_CLOUD_TOKEN", "").strip()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()

    if not (token and phone_number_id):
        return False

    template_name, wa_lang = _resolve_template(lang)
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"

    payload = {
        "messaging_product": "whatsapp", "to": phone, "type": "template",
        "template": {
            "name": template_name, "language": {"code": wa_lang},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {"type": "button", "sub_type": "url", "index": 0,
                 "parameters": [{"type": "text", "text": code}]}
            ]
        }
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def _send_whatsapp(phone: str, code: str) -> bool:
    provider = _wa_provider()
    if provider == "cloud":
        return _send_whatsapp_cloud(phone, code)
    if provider == "evolution":
        return _send_whatsapp_evolution(phone, code)
    return False


def send_code(raw_phone: str) -> dict:
    phone = normalize_phone(raw_phone)
    if not phone or len(phone) < 10:
        return {"ok": False, "error": "WhatsApp inválido"}

    if not _wa_enabled():
        return {"ok": True, "fallback": True, "sent": False, "reason": "otp_disabled"}

    now = time.time()
    code = f"{secrets.randbelow(900000) + 100000}"

    with _lock:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            row = conn.execute("SELECT last_sent FROM otp WHERE phone=?", (phone,)).fetchone()
            if row and (now - row[0]) < RESEND_COOLDOWN:
                conn.close()
                return {"ok": False, "cooldown": int(RESEND_COOLDOWN - (now - row[0]))}
            conn.execute(
                """INSERT OR REPLACE INTO otp(phone,code,created_at,attempts,last_sent,verified)
                   VALUES(?,?,?,?,?,0)""", (phone, code, now, 0, now))
            conn.commit()
            conn.close()
        except Exception:
            return {"ok": True, "fallback": True, "sent": False, "reason": "store_error"}

    sent = _send_whatsapp(phone, code)
    if not sent:
        return {"ok": True, "fallback": True, "sent": False, "reason": "send_failed"}
    return {"ok": True, "fallback": False, "sent": True}


def verify_code(raw_phone: str, code: str) -> dict:
    phone = normalize_phone(raw_phone)
    code = re.sub(r"\D", "", code or "")
    if not _wa_enabled():
        return {"ok": True, "verified": True, "fallback": True}
    if not phone or not code:
        return {"ok": False, "verified": False, "error": "Dados incompletos"}

    now = time.time()
    with _lock:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            row = conn.execute("SELECT code,created_at,attempts FROM otp WHERE phone=?", (phone,)).fetchone()
            if not row:
                conn.close()
                return {"ok": False, "verified": False, "error": "Código não encontrado"}
            db_code, created_at, attempts = row
            if now - created_at > TTL_SECONDS:
                conn.close()
                return {"ok": False, "verified": False, "error": "Código expirado"}
            if attempts >= MAX_ATTEMPTS:
                conn.close()
                return {"ok": False, "verified": False, "error": "Muitas tentativas"}
            if db_code != code:
                conn.execute("UPDATE otp SET attempts=attempts+1 WHERE phone=?", (phone,))
                conn.commit()
                conn.close()
                return {"ok": False, "verified": False, "error": "Código incorreto"}
            conn.execute("UPDATE otp SET verified=1 WHERE phone=?", (phone,))
            conn.commit()
            conn.close()
            return {"ok": True, "verified": True}
        except Exception:
            return {"ok": True, "verified": True, "fallback": True}
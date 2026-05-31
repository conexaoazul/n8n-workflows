#!/usr/bin/env python3
"""
Lead Store — armazenamento resiliente de leads para o Lead Gate da Conexão Azul.

Princípios:
- Append-only JSONL como fonte de verdade (nunca perde lead, mesmo se SQLite falhar).
- SQLite como índice para consulta/dedup.
- Nunca lança exceção que quebre a captura do lead (best-effort no índice).
- Persistência em /app/data (bind mount no host para sobreviver a redeploys).
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DATA_DIR = Path(os.environ.get("LEAD_DATA_DIR", "/app/data"))
JSONL_PATH = DATA_DIR / "leads.jsonl"
DB_PATH = DATA_DIR / "leads.db"

_lock = threading.Lock()

# Campos comerciais aceitos (whitelist — evita lixo no store)
ALLOWED_FIELDS = {
    "nome", "email", "whatsapp", "empresa", "segmento", "interesse",
    "lgpd_consent", "flow", "desafio", "ferramenta_atual", "urgencia",
    "orcamento", "area_uso", "quer_reuniao", "melhor_horario",
    "volume_clientes", "modelo", "country", "language", "currency_preference",
    "source_url", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "referrer",
}

DEFAULTS = {
    "country": "BR",
    "language": "pt-BR",
    "currency_preference": "BRL",
    "flow": "catalog",
}


def _ensure_dirs() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _init_db() -> None:
    _ensure_dirs()
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                email TEXT,
                whatsapp TEXT,
                nome TEXT,
                empresa TEXT,
                segmento TEXT,
                interesse TEXT,
                flow TEXT,
                country TEXT,
                language TEXT,
                currency_preference TEXT,
                utm_source TEXT,
                utm_campaign TEXT,
                synced_odoo INTEGER DEFAULT 0,
                synced_n8n INTEGER DEFAULT 0,
                raw TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_whatsapp ON leads(whatsapp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_sync ON leads(synced_odoo, synced_n8n)")
        conn.commit()
        conn.close()
    except Exception:
        pass


_init_db()


def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for k, v in payload.items():
        if k in ALLOWED_FIELDS and v is not None:
            if isinstance(v, str):
                v = v.strip()[:500]
            clean[k] = v
    for k, default in DEFAULTS.items():
        clean.setdefault(k, default)
    return clean


def save_lead(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Salva o lead. Retorna o registro com id e created_at. Nunca lança."""
    record = _clean(payload)
    record["id"] = uuid.uuid4().hex
    record["created_at"] = datetime.now(timezone.utc).isoformat()

    line = json.dumps(record, ensure_ascii=False)

    with _lock:
        # 1) JSONL append — fonte de verdade
        try:
            _ensure_dirs()
            with open(JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

        # 2) SQLite índice — best effort
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.execute(
                """
                INSERT OR REPLACE INTO leads
                (id, created_at, email, whatsapp, nome, empresa, segmento, interesse,
                 flow, country, language, currency_preference, utm_source, utm_campaign, raw)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"], record["created_at"], record.get("email"),
                    record.get("whatsapp"), record.get("nome"), record.get("empresa"),
                    record.get("segmento"), record.get("interesse"), record.get("flow"),
                    record.get("country"), record.get("language"),
                    record.get("currency_preference"), record.get("utm_source"),
                    record.get("utm_campaign"), line,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    return record


def stats() -> Dict[str, Any]:
    """Estatísticas do store. Best effort."""
    out = {"total": 0, "by_flow": {}, "pending_odoo": 0, "pending_n8n": 0}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()
        out["total"] = cur.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        for flow, n in cur.execute("SELECT flow, COUNT(*) FROM leads GROUP BY flow"):
            out["by_flow"][flow or "unknown"] = n
        out["pending_odoo"] = cur.execute("SELECT COUNT(*) FROM leads WHERE synced_odoo=0").fetchone()[0]
        out["pending_n8n"] = cur.execute("SELECT COUNT(*) FROM leads WHERE synced_n8n=0").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return out

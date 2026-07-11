#!/usr/bin/env python3
"""
Chatwoot Lead Sync — cria contato + nota privada no Chatwoot account 64.

Best-effort: nunca lança exceção que quebre o lead gate.
Se Chatwoot estiver indisponível, retorna {ok: False, reason: "..."}.
"""

import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone


def _clean_phone(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits and not digits.startswith("55") and 10 <= len(digits) <= 11:
        digits = "55" + digits
    return f"+{digits}" if digits else ""


def _phone_digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _extract_contacts(search_data) -> list:
    if isinstance(search_data, list):
        return search_data
    if not isinstance(search_data, dict):
        return []
    payload = search_data.get("payload", search_data)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        contacts = payload.get("contacts", [])
        return contacts if isinstance(contacts, list) else []
    return []


def _contact_id(contact: dict):
    if not isinstance(contact, dict):
        return None
    return contact.get("id") or (contact.get("contact") or {}).get("id")


def _contact_matches(contact: dict, email: str, phone: str) -> bool:
    if not isinstance(contact, dict):
        return False
    contact_email = (contact.get("email") or "").strip().lower()
    contact_phone = _phone_digits(contact.get("phone_number") or contact.get("phone") or "")
    target_phone = _phone_digits(phone)
    return bool((email and contact_email == email) or (target_phone and contact_phone == target_phone))


def _search_contact(base: str, account_id: str, token: str, query: str, email: str, phone: str):
    if not query:
        return None
    search_url = f"{base}/api/v1/accounts/{account_id}/contacts/search?q={urllib.request.quote(query)}"
    search_resp = _cw_request(search_url, "GET", token)
    if search_resp.get("status") != 200:
        return None
    items = _extract_contacts(search_resp.get("data"))
    for item in items:
        if _contact_matches(item, email, phone):
            return _contact_id(item)
    return _contact_id(items[0]) if items and isinstance(items[0], dict) else None


def _flow_label(flow: str) -> str:
    labels = {
        "catalog_access": "📥 Catálogo Gratuito",
        "trial_3_days": "🧪 Teste 3 Dias",
        "implementation_7_days": "🚀 Sprint 7 Dias",
        "white_label_enterprise": "🤝 Parceria/White-Label",
    }
    return labels.get(flow, f"📌 {flow}")


def _cw_request(url: str, method: str, token: str, body: dict = None) -> dict:
    """Helper para requests ao Chatwoot. Retorna {status, data} ou {error}."""
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ConexaoAzulLeadGate/1.0",
        "api_access_token": token,
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode("utf-8") or "{}")
            return {"status": resp.status, "data": resp_data}
    except urllib.error.HTTPError as e:
        body_resp = ""
        try:
            body_resp = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        return {"status": e.code, "error": "http_error", "body": body_resp}
    except Exception as exc:
        return {"status": 0, "error": "exception", "detail": str(exc)[:200]}


def sync_lead_to_chatwoot(payload: dict) -> dict:
    """
    Cria contato no Chatwoot e envia nota privada com dados do lead.

    Retorna: {ok, contact_id, conversation_id, note_id, reason?}
    """
    base = os.environ.get("CHATWOOT_BASE_URL", "").rstrip("/")
    account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "64")
    token = os.environ.get("CHATWOOT_API_TOKEN", "")
    inbox_id = os.environ.get("CHATWOOT_BLUECONNECT_INBOX_ID", "351")

    if not (base and account_id and token):
        return {"ok": False, "reason": "missing_env"}

    email = (payload.get("email") or "").strip().lower()
    whatsapp = _clean_phone(payload.get("whatsapp") or "")
    name = payload.get("name") or payload.get("company") or email or whatsapp or "Lead n8n Workflows"
    flow = payload.get("conversion_flow") or "catalog_access"
    label = _flow_label(flow)

    # 1) Criar contato
    contact_body = {
        "name": name,
        "email": email or None,
        "phone_number": whatsapp or None,
        "identifier": f"leadgate:{email or whatsapp}",
        "additional_attributes": {
            "source": "n8n-workflows",
            "company": payload.get("company"),
            "segment": payload.get("segment"),
            "interest": payload.get("interest"),
            "conversion_flow": flow,
            "country": payload.get("country", "BR"),
            "language": payload.get("language", "pt-BR"),
            "currency_preference": payload.get("currency_preference", "BRL"),
            "utm_source": payload.get("utm_source"),
            "utm_medium": payload.get("utm_medium"),
            "utm_campaign": payload.get("utm_campaign"),
            "source_url": payload.get("source_url"),
            "whatsapp_verified": payload.get("whatsapp_verified", False),
            "otp_fallback": payload.get("otp_fallback", True),
        }
    }

    # Reaproveitar contato existente primeiro evita 422 por telefone duplicado e reduz POSTs na API.
    contact_mode = None
    phone_digits = _phone_digits(whatsapp)
    for mode, query in (
        ("existing_phone", phone_digits),
        ("existing_phone_e164", whatsapp),
        ("existing_email", email),
    ):
        contact_id = _search_contact(base, account_id, token, query, email, whatsapp)
        if contact_id:
            contact_mode = mode
            break

    contact_url = f"{base}/api/v1/accounts/{account_id}/contacts"
    contact_resp = {"status": 0}
    if not contact_id:
        contact_resp = _cw_request(contact_url, "POST", token, contact_body)
        contact_data = contact_resp.get("data") or contact_resp
        contact_mode = "created"

        # Extrair contact_id (pode vir em payload.contact.id ou direto)
        if isinstance(contact_data, dict):
            contact_id = (contact_data.get("payload", {}) or {}).get("contact", {}).get("id")
            if not contact_id:
                contact_id = contact_data.get("id")

    # Se contato já existir (409 ou 422), tentar buscar por telefone e email.
    if not contact_id and contact_resp.get("status") in (409, 422):
        search_attempts = [
            ("existing_phone", phone_digits),
            ("existing_phone_e164", whatsapp),
            ("existing_email", email),
            ("existing_name", name),
        ]
        for mode, query in search_attempts:
            contact_id = _search_contact(base, account_id, token, query, email, whatsapp)
            if contact_id:
                contact_mode = mode
                break

    # Se ainda não temos contact_id, retornar o que temos
    if not contact_id:
        reason = f"contact_status_{contact_resp.get('status', 0)}"
        if contact_resp.get("status") == 403:
            reason = "chatwoot_unavailable"
        return {
            "ok": False,
            "contact_id": None,
            "reason": reason,
        }

    # Atualiza atributos do contato existente, mas nunca quebra a captura por falha no Chatwoot.
    if contact_mode != "created":
        update_body = dict(contact_body)
        update_body.pop("identifier", None)
        update_url = f"{base}/api/v1/accounts/{account_id}/contacts/{contact_id}"
        _cw_request(update_url, "PUT", token, update_body)

    # 2) Criar ou encontrar conversa no inbox BlueConnect
    conversation_id = None
    conv_url = f"{base}/api/v1/accounts/{account_id}/conversations"
    conv_body = {
        "contact_id": contact_id,
        "inbox_id": int(inbox_id),
    }
    conv_resp = _cw_request(conv_url, "POST", token, conv_body)
    if conv_resp.get("status") in (200, 201):
        conv_data = conv_resp.get("data") or {}
        conversation_id = conv_data.get("id")

    # 3) Enviar nota privada com dados do lead
    note_id = None
    if conversation_id:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        utm_block = ""
        for utm_key in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
            val = payload.get(utm_key)
            if val:
                utm_block += f"  • {utm_key}: {val}\n"

        note_content = (
            f"🔔 **Lead Gate — {label}**\n\n"
            f"📋 **Dados do Lead:**\n"
            f"  • Nome: {name}\n"
            f"  • Email: {email or 'n/a'}\n"
            f"  • WhatsApp: {whatsapp or 'n/a'}\n"
            f"  • Empresa: {payload.get('company') or 'n/a'}\n"
            f"  • Segmento: {payload.get('segment') or 'n/a'}\n"
            f"  • Interesse: {payload.get('interest') or 'n/a'}\n"
            f"  • Flow: {flow}\n"
            f"  • País: {payload.get('country', 'BR')} | Idioma: {payload.get('language', 'pt-BR')} | Moeda: {payload.get('currency_preference', 'BRL')}\n"
        )

        if payload.get("challenge"):
            note_content += f"\n🎯 **Desafio:** {payload['challenge']}\n"
        if payload.get("whatsapp_verified"):
            note_content += f"\n✅ WhatsApp verificado via OTP\n"
        else:
            note_content += f"\n⚠️ WhatsApp não verificado (fallback)\n"

        if utm_block:
            note_content += f"\n📊 **UTMs:**\n{utm_block}"

        if payload.get("source_url"):
            note_content += f"\n🔗 URL origem: {payload['source_url']}\n"

        note_content += f"\n⏰ Capturado em: {now}"

        note_url = f"{base}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
        note_body = {
            "content": note_content,
            "message_type": "outgoing",
            "private": True,
        }
        note_resp = _cw_request(note_url, "POST", token, note_body)
        if note_resp.get("status") in (200, 201):
            note_data = note_resp.get("data") or {}
            note_id = note_data.get("id")

    return {
        "ok": True,
        "contact_id": contact_id,
        "conversation_id": conversation_id,
        "note_id": note_id,
        "mode": contact_mode,
    }

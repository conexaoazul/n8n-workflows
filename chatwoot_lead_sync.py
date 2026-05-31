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
    headers = {"Content-Type": "application/json", "api_access_token": token}
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

    contact_url = f"{base}/api/v1/accounts/{account_id}/contacts"
    contact_resp = _cw_request(contact_url, "POST", token, contact_body)
    contact_data = contact_resp.get("data") or contact_resp

    # Extrair contact_id (pode vir em payload.contact.id ou direto)
    contact_id = None
    if isinstance(contact_data, dict):
        contact_id = (contact_data.get("payload", {}) or {}).get("contact", {}).get("id")
        if not contact_id:
            contact_id = contact_data.get("id")

    # Se contato já existir (409 ou 422), tentar buscar pelo identifier
    if not contact_id and contact_resp.get("status") in (409, 422):
        # Search by email first, then phone, then name
        search_query = email or whatsapp or name
        search_url = f"{base}/api/v1/accounts/{account_id}/contacts/search?q={urllib.request.quote(search_query)}"
        search_resp = _cw_request(search_url, "GET", token)
        if search_resp.get("status") == 200:
            search_data = search_resp.get("data")
            items = []
            # Chatwoot API returns payload as list or dict depending on version
            if isinstance(search_data, list):
                items = search_data
            elif isinstance(search_data, dict):
                search_payload = search_data.get("payload", search_data)
                if isinstance(search_payload, list):
                    items = search_payload
                elif isinstance(search_payload, dict):
                    items = search_payload.get("contacts", [])
            if items and isinstance(items[0], dict):
                contact_id = items[0].get("id")

        # If search by email found nothing, try by phone
        if not contact_id and whatsapp:
            phone_digits = "".join(ch for ch in whatsapp if ch.isdigit())
            search_url2 = f"{base}/api/v1/accounts/{account_id}/contacts/search?q={urllib.request.quote(phone_digits)}"
            search_resp2 = _cw_request(search_url2, "GET", token)
            if search_resp2.get("status") == 200:
                search_data2 = search_resp2.get("data")
                items2 = []
                if isinstance(search_data2, list):
                    items2 = search_data2
                elif isinstance(search_data2, dict):
                    search_payload2 = search_data2.get("payload", search_data2)
                    if isinstance(search_payload2, list):
                        items2 = search_payload2
                    elif isinstance(search_payload2, dict):
                        items2 = search_payload2.get("contacts", [])
                if items2 and isinstance(items2[0], dict):
                    contact_id = items2[0].get("id")

    # Se ainda não temos contact_id, retornar o que temos
    if not contact_id:
        return {
            "ok": contact_resp.get("status") in (200, 201),
            "contact_id": None,
            "reason": f"contact_status_{contact_resp.get('status', 0)}",
        }

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
    }
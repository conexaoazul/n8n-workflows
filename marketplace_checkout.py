#!/usr/bin/env python3
"""CA Marketplace checkout integration for Odoo sale orders."""

from __future__ import annotations

import importlib.util
import base64
import hashlib
import hmac
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

from workflow_db import WorkflowDatabase


ODOO_360_TOOL_PATHS = (
    Path("/docker/openclaw/.openclaw/workspace/scripts/odoo_360_tool.py"),
    Path("/docker/openclaw/workspace/scripts/odoo_360_tool.py"),
    Path("/docker/openclaw/.openclaw/workspace/dhy/workspace/scripts/odoo_360_tool.py"),
    Path("/docker/openclaw/.openclaw/dhy/workspace/scripts/odoo_360_tool.py"),
)
DEFAULT_FULFILLMENT_WEBHOOK = "ca-marketplace-fulfillment"
DOWNLOAD_TOKEN_SCOPE = "download"
DOWNLOAD_TOKEN_TTL_SECONDS = 10 * 60
LOGGER = logging.getLogger(__name__)


class CheckoutCustomer(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    email: str = ""
    phone: Optional[str] = Field(default=None, max_length=40)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value and ("@" not in value or len(value) > 254):
            raise ValueError("Email inválido")
        return value


class CheckoutRequest(BaseModel):
    customer: CheckoutCustomer
    plan: Optional[str] = None


def load_odoo_360_tool() -> Any:
    """Load the existing Odoo 360 JSON-RPC tool without copying its client code."""
    explicit_path = os.environ.get("ODOO_360_TOOL_PATH")
    candidates = [Path(explicit_path)] if explicit_path else list(ODOO_360_TOOL_PATHS)
    for path in candidates:
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location("odoo_360_tool", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["odoo_360_tool"] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "execute"):
            raise RuntimeError(f"odoo_360_tool at {path} does not expose execute()")

        # Normalize the environment names required by the marketplace contract
        # onto the existing tool variables. Values are never hardcoded here.
        odoo_url = os.environ.get("ODOO_URL") or os.environ.get("ODOO_JSONRPC_URL")
        if odoo_url:
            module.URL_INTERNAL = odoo_url if odoo_url.endswith("/jsonrpc") else f"{odoo_url.rstrip('/')}/jsonrpc"
        if os.environ.get("ODOO_DB"):
            module.DB = os.environ["ODOO_DB"]
        if os.environ.get("ODOO_PASSWORD"):
            module.PWD = os.environ["ODOO_PASSWORD"]
        if os.environ.get("ODOO_UID"):
            module.USER_UID = int(os.environ["ODOO_UID"])
        return module
    raise RuntimeError("odoo_360_tool.py not found. Set ODOO_360_TOOL_PATH or install the OpenClaw script.")


def odoo_execute(tool: Any, model: str, method: str, args: Optional[list] = None, kwargs: Optional[dict] = None) -> Any:
    payload = {
        "payload": {
            "model": model,
            "m": method,
            "args": args or [],
            "kwargs": kwargs or {},
            "safe": False,
        }
    }
    result = tool.execute(payload)
    if result.get("status") != "success":
        raise RuntimeError(f"Odoo {model}.{method} failed: {result}")
    return result.get("result")


def get_product_id(db: WorkflowDatabase, slug: str) -> Optional[int]:
    import sqlite3

    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_id FROM asset_product_map WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return int(row["product_id"]) if row else None


def _normalize_email(email: Optional[str]) -> Optional[str]:
    value = (email or "").strip().lower()
    return value if "@" in value else None


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D+", "", phone or "")
    return digits if len(digits) >= 8 else None


def _search_partner_id(tool: Any, domain: list) -> Optional[int]:
    result = odoo_execute(tool, "res.partner", "search_read", [domain], {"fields": ["id"], "limit": 1})
    if result:
        return int(result[0]["id"])
    return None


def find_existing_partner(tool: Any, customer: CheckoutCustomer) -> Optional[int]:
    email = _normalize_email(customer.email)
    phone = _normalize_phone(customer.phone)
    if not email and not phone:
        return None

    email_partner = _search_partner_id(tool, [("email", "=", email)]) if email else None
    phone_partner = None
    if phone:
        phone_domain = ["|", ("phone", "=", phone), ("mobile", "=", phone)]
        phone_partner = _search_partner_id(tool, phone_domain)

    if email_partner and phone_partner and email_partner != phone_partner:
        raise ValueError("Identity conflict: email and phone match different partners")
    return email_partner or phone_partner


def create_partner(tool: Any, customer: CheckoutCustomer) -> int:
    email = _normalize_email(customer.email)
    phone = _normalize_phone(customer.phone)
    if not email and not phone:
        raise ValueError("Checkout requires a valid email or phone")

    vals = {
        "name": customer.name,
    }
    if email:
        vals["email"] = email
    if phone:
        vals["phone"] = phone
    return int(odoo_execute(tool, "res.partner", "create", [vals]))


def _find_or_create_partner(tool: Any, customer: CheckoutCustomer) -> int:
    partner_id = find_existing_partner(tool, customer)
    if partner_id is not None:
        return partner_id
    # Re-search immediately before create to reduce duplicate partners under concurrent checkout.
    partner_id = find_existing_partner(tool, customer)
    if partner_id is not None:
        return partner_id
    return create_partner(tool, customer)


def _parse_odoo_datetime(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    from datetime import datetime, timezone

    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _subscription_grace_seconds() -> int:
    hours = float(os.environ.get("MARKETPLACE_SUBSCRIPTION_GRACE_HOURS", "24"))
    return int(hours * 3600)


def _order_is_inside_subscription_grace(order: Dict[str, Any]) -> bool:
    order_time = _parse_odoo_datetime(order.get("date_order") or order.get("write_date"))
    if order_time is None:
        return False
    return time.time() - order_time <= _subscription_grace_seconds()


def check_entitlement(tool: Any, partner_id: int, slug: str) -> Dict[str, Any]:
    subscription_domain = [
        ("customer_id", "=", partner_id),
        ("sale_order_id.order_line.product_id.default_code", "in", ["BLUE-APPS", "BLUE-AUTOMATE"]),
    ]
    subscription = odoo_execute(
        tool,
        "asaas.subscription",
        "search_read",
        [subscription_domain],
        {"fields": ["id", "is_active"], "limit": 1},
    )
    if subscription and subscription[0].get("is_active") is True:
        return {"entitled": True, "scope": "all", "via": "subscription"}
    if subscription:
        return {"entitled": False}

    all_access_order_domain = [
        ("partner_id", "=", partner_id),
        ("state", "in", ["sale", "done"]),
        ("order_line.product_id.default_code", "in", ["BLUE-APPS", "BLUE-AUTOMATE"]),
        ("invoice_ids.payment_state", "in", ["paid", "in_payment"]),
    ]
    all_access_order = odoo_execute(
        tool,
        "sale.order",
        "search_read",
        [all_access_order_domain],
        {"fields": ["id", "date_order", "write_date"], "limit": 1, "order": "date_order desc"},
    )
    if all_access_order and _order_is_inside_subscription_grace(all_access_order[0]):
        return {"entitled": True, "scope": "all", "via": "order_grace"}

    # DECISION-007: single workflow entitlement is bound to the slug persisted
    # in sale.order.line.name because workflows now map to shared tier SKUs.
    single_domain = [
        ("partner_id", "=", partner_id),
        ("state", "in", ["sale", "done"]),
        ("invoice_ids.payment_state", "in", ["paid", "in_payment"]),
        ("order_line.name", "ilike", f"[{slug}]"),
    ]
    single = odoo_execute(
        tool,
        "sale.order",
        "search_read",
        [single_domain],
        {"fields": ["id"], "limit": 1},
    )
    if single:
        return {"entitled": True, "scope": "single"}
    return {"entitled": False}


def sign_download_token(slug: str, partner_id: int, scope: str, now: Optional[int] = None) -> str:
    secret = os.environ.get("MARKETPLACE_DOWNLOAD_SECRET")
    if not secret:
        raise RuntimeError("MARKETPLACE_DOWNLOAD_SECRET is required for paid downloads")
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + DOWNLOAD_TOKEN_TTL_SECONDS
    nonce = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    payload = f"{slug}|{partner_id}|{scope}|{issued_at}|{expires_at}|{nonce}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )


def validate_download_token(token: Optional[str], slug: str, scope: str = DOWNLOAD_TOKEN_SCOPE) -> bool:
    secret = os.environ.get("MARKETPLACE_DOWNLOAD_SECRET")
    if not secret:
        LOGGER.error("MARKETPLACE_DOWNLOAD_SECRET missing; paid download denied")
        return False
    if not isinstance(token, str) or "." not in token:
        return False
    payload_b64, signature_b64 = token.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
        signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    except (ValueError, UnicodeDecodeError):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return False
    parts = payload.split("|")
    if len(parts) != 6:
        return False
    token_slug, partner_id, token_scope, issued_at, expires_at, nonce = parts
    if not partner_id or not issued_at.isdigit() or not expires_at.isdigit() or not nonce:
        return False
    return token_slug == slug and token_scope == scope and int(expires_at) >= int(time.time())


def create_sale_order(tool: Any, partner_id: int, product_id: int, asset: Dict[str, Any], plan: Optional[str]) -> int:
    product = odoo_execute(
        tool,
        "product.product",
        "read",
        [[product_id]],
        {"fields": ["id", "list_price", "default_code"]},
    )
    if not product:
        raise ValueError("Missing Odoo product for marketplace asset")
    catalog_price = round(int(asset.get("price_cents") or 0) / 100, 2)
    odoo_price = round(float(product[0].get("list_price") or 0), 2)
    if odoo_price != catalog_price:
        LOGGER.warning("Marketplace price mismatch for %s: catalog=%s odoo=%s", asset["slug"], catalog_price, odoo_price)
        raise ValueError("Preço em atualização. Tente novamente em instantes.")

    order_vals = {
        "partner_id": partner_id,
        "origin": f"CA Marketplace: {asset['slug']}",
        "note": f"Asset: {asset['name']} ({asset['asset_type']})" + (f"\nPlano: {plan}" if plan else ""),
    }
    order_id = odoo_execute(tool, "sale.order", "create", [order_vals])
    line_vals = {
        "order_id": order_id,
        "product_id": product_id,
        "product_uom_qty": 1,
        "name": f"Workflow: {asset['name']} [{asset['slug']}]",
    }
    odoo_execute(tool, "sale.order.line", "create", [line_vals])
    return int(order_id)


def build_payment_url(order_id: int) -> str:
    base = os.environ.get("ASAAS_PAYMENT_BASE_URL") or os.environ.get("ODOO_URL", "").rstrip("/")
    if not base:
        return ""
    if "asaas" in base:
        return f"{base.rstrip('/')}/{order_id}"
    return f"{base}/shop/payment?order_id={order_id}"


def checkout_asset(slug: str, request: CheckoutRequest, db_path: Optional[str] = None) -> Dict[str, Any]:
    db = WorkflowDatabase(db_path)
    asset = db.get_asset(slug)
    if not asset:
        raise ValueError(f"Asset not found: {slug}")

    tool = load_odoo_360_tool()
    partner_id = _find_or_create_partner(tool, request.customer)

    entitlement = check_entitlement(tool, partner_id, slug)
    if entitlement.get("entitled"):
        return {
            **entitlement,
            "download_url": f"/api/assets/{slug}/download?token={sign_download_token(slug, partner_id, DOWNLOAD_TOKEN_SCOPE)}",
        }

    product_id = get_product_id(db, slug)
    if product_id is None:
        raise ValueError(f"Missing Odoo product_id for asset slug: {slug}")

    order_id = create_sale_order(tool, partner_id, product_id, asset, request.plan)
    payment_url = build_payment_url(order_id)
    fulfillment_webhook = os.environ.get("FULFILLMENT_WEBHOOK_URL", DEFAULT_FULFILLMENT_WEBHOOK)
    return {
        "entitled": False,
        "order_id": order_id,
        "payment_url": payment_url,
        "fulfillment_webhook": fulfillment_webhook,
    }


__all__ = [
    "CheckoutCustomer",
    "CheckoutRequest",
    "check_entitlement",
    "checkout_asset",
    "sign_download_token",
    "validate_download_token",
]

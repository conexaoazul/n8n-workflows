#!/usr/bin/env python3
"""CA Marketplace checkout integration for Odoo sale orders."""

from __future__ import annotations

import importlib.util
import os
import sys
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


class CheckoutCustomer(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    email: str
    phone: Optional[str] = Field(default=None, max_length=40)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not value or "@" not in value or len(value) > 254:
            raise ValueError("Email inválido")
        return value.strip().lower()


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


def find_existing_partner(tool: Any, customer: CheckoutCustomer) -> Optional[int]:
    domain = ["|", ("email", "=", customer.email), ("phone", "=", customer.phone or "")]
    result = odoo_execute(tool, "res.partner", "search_read", [domain], {"fields": ["id"], "limit": 1})
    if result:
        return int(result[0]["id"])
    return None


def create_partner(tool: Any, customer: CheckoutCustomer) -> int:
    vals = {
        "name": customer.name,
        "email": customer.email,
    }
    if customer.phone:
        vals["phone"] = customer.phone
    return int(odoo_execute(tool, "res.partner", "create", [vals]))


def check_entitlement(tool: Any, partner_id: int, slug: str) -> Dict[str, Any]:
    subscription_domain = [
        ("customer_id", "=", partner_id),
        ("is_active", "=", True),
        ("sale_order_id.order_line.product_id.default_code", "in", ["BLUE-APPS", "BLUE-AUTOMATE"]),
    ]
    active_subscription = odoo_execute(
        tool,
        "asaas.subscription",
        "search_read",
        [subscription_domain],
        {"fields": ["id"], "limit": 1},
    )
    if active_subscription:
        return {"entitled": True, "scope": "all", "via": "subscription"}

    all_access_order_domain = [
        ("partner_id", "=", partner_id),
        ("state", "in", ["sale", "done"]),
        ("order_line.product_id.default_code", "in", ["BLUE-APPS", "BLUE-AUTOMATE"]),
        ("payment_state", "in", ["paid", "done"]),
    ]
    all_access_order = odoo_execute(
        tool,
        "sale.order",
        "search_read",
        [all_access_order_domain],
        {"fields": ["id"], "limit": 1},
    )
    if all_access_order:
        return {"entitled": True, "scope": "all", "via": "order"}

    single_domain = [
        ("partner_id", "=", partner_id),
        ("state", "in", ["sale", "done"]),
        ("payment_state", "in", ["paid", "done"]),
        ("order_line.product_id.default_code", "=", f"WF-{slug}"),
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


def create_sale_order(tool: Any, partner_id: int, product_id: int, asset: Dict[str, Any], plan: Optional[str]) -> int:
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
    }
    if asset.get("price_cents") is not None:
        line_vals["price_unit"] = round(int(asset.get("price_cents") or 0) / 100, 2)
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
    partner_id = find_existing_partner(tool, request.customer)
    if partner_id is None:
        partner_id = create_partner(tool, request.customer)

    entitlement = check_entitlement(tool, partner_id, slug)
    if entitlement.get("entitled"):
        return {
            **entitlement,
            "download_url": f"/api/assets/{slug}/download",
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


__all__ = ["CheckoutCustomer", "CheckoutRequest", "check_entitlement", "checkout_asset"]

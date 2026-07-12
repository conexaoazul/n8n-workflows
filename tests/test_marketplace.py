import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import api_server
import marketplace_checkout
import populate_assets
from marketplace_checkout import CheckoutCustomer, CheckoutRequest
from workflow_db import WorkflowDatabase


class FakeOdooTool:
    def __init__(self, partners=None, phones=None, entitlements=None, products=None):
        self.partners = partners or {}
        self.phones = phones or {}
        self.entitlements = entitlements or {}
        self.products = products if products is not None else {321: {"id": 321, "list_price": 19.0, "default_code": "WF-MEDIUM"}}
        self.calls = []
        self.next_partner_id = 100
        self.next_order_id = 1234
        self.next_line_id = 4321

    def execute(self, payload):
        call = payload["payload"]
        self.calls.append(call)
        model = call["model"]
        method = call["m"]
        args = call["args"]

        if model == "res.partner" and method == "search_read":
            domain = args[0]
            email = next((item[2] for item in domain if isinstance(item, tuple) and item[:2] == ("email", "=")), None)
            phone = next((item[2] for item in domain if isinstance(item, tuple) and item[0] in ("phone", "mobile")), None)
            partner_id = self.partners.get(email) if email else self.phones.get(phone)
            return {"status": "success", "result": [{"id": partner_id}] if partner_id else []}

        if model == "res.partner" and method == "create":
            partner_id = self.next_partner_id
            self.next_partner_id += 1
            if args[0].get("email"):
                self.partners[args[0]["email"]] = partner_id
            if args[0].get("phone"):
                self.phones[args[0]["phone"]] = partner_id
            return {"status": "success", "result": partner_id}

        if model == "asaas.subscription" and method == "search_read":
            domain = args[0]
            partner_id = next((item[2] for item in domain if isinstance(item, tuple) and item[:2] == ("customer_id", "=")), None)
            if "subscription" not in self.entitlements.get(partner_id, {}):
                return {"status": "success", "result": []}
            return {"status": "success", "result": [{"id": 8001, "is_active": self.entitlements[partner_id]["subscription"]}]}

        if model == "sale.order" and method == "search_read":
            domain = args[0]
            partner_id = next((item[2] for item in domain if isinstance(item, tuple) and item[:2] == ("partner_id", "=")), None)
            default_code_filter = next(
                (item for item in domain if isinstance(item, tuple) and item[0] == "order_line.product_id.default_code"),
                None,
            )
            line_name_filter = next((item for item in domain if isinstance(item, tuple) and item[0] == "order_line.name"), None)
            if default_code_filter and default_code_filter[1] == "in":
                order = self.entitlements.get(partner_id, {}).get("all_order")
                return {"status": "success", "result": [order] if order else []}
            if line_name_filter and line_name_filter[1] == "ilike":
                marker = line_name_filter[2]
                has_single = marker in self.entitlements.get(partner_id, {}).get("single", set())
                return {"status": "success", "result": [{"id": 9002}] if has_single else []}
            return {"status": "success", "result": []}

        if model == "product.product" and method == "read":
            product_id = args[0][0]
            product = self.products.get(product_id)
            return {"status": "success", "result": [product] if product else []}

        if model == "sale.order" and method == "create":
            order_id = self.next_order_id
            self.next_order_id += 1
            return {"status": "success", "result": order_id}

        if model == "sale.order.line" and method == "create":
            line_id = self.next_line_id
            self.next_line_id += 1
            return {"status": "success", "result": line_id}

        return {"status": "error", "data": f"unexpected {model}.{method}"}


def create_checkout_db(tmp_path, slug="workflow-x", product_id=321, price_cents=1900):
    db_path = tmp_path / "marketplace.db"
    db = WorkflowDatabase(str(db_path))
    db.upsert_asset({
        "slug": slug,
        "name": "Workflow X",
        "asset_type": "workflow",
        "category": "CRM/Vendas",
        "price_cents": price_cents,
        "model": "one-shot",
    })
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asset_product_map(slug, product_id) VALUES (?, ?)", (slug, product_id))
    conn.commit()
    conn.close()
    return db_path


def checkout_request(email="cliente@example.com"):
    return CheckoutRequest(
        customer=CheckoutCustomer(name="Cliente Teste", email=email, phone="+5511999999999"),
        plan="one-shot",
    )


def phone_only_request(phone="+55 11 99999-9999"):
    return CheckoutRequest(
        customer=CheckoutCustomer(name="Cliente Teste", email="", phone=phone),
        plan="one-shot",
    )


def test_search_assets(tmp_path):
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "ca-nfse-validator",
        "name": "Validador NFS-e",
        "asset_type": "skill",
        "category": "Financeiro/Fiscal",
        "summary": "Valida NFS-e remotamente",
        "description": "Skill read-only para validar emissão fiscal.",
        "price_cents": 0,
        "model": "free",
        "tags": ["nfse", "odoo"],
        "complements": [],
    })

    results, total = db.search_assets(type="skill", category="Financeiro/Fiscal", q="nfse")

    assert total == 1
    assert results[0]["slug"] == "ca-nfse-validator"
    assert results[0]["tags"] == ["nfse", "odoo"]


def test_populate(tmp_path, monkeypatch):
    module_root = tmp_path / "BlueApps19"
    module = module_root / "blue_payment_asaas_nfse"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text(
        "{'name': 'Asaas NFS-e', 'version': '19.0.1.0.0', 'summary': 'Emissao NFS-e Asaas', 'license': 'OPL-1', 'category': 'Accounting'}",
        encoding="utf-8",
    )

    skills_root = tmp_path / "skills"
    skill = skills_root / "ca-nfse-validator"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Validador NFS-e\nValida notas.", encoding="utf-8")

    workflows_root = tmp_path / "workflows"
    workflows_root.mkdir()
    (workflows_root / "001_Odoo_Webhook.json").write_text(
        json.dumps({"name": "Odoo Webhook", "nodes": [{"type": "n8n-nodes-base.webhook"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(populate_assets, "DEFAULT_MODULE_ROOT", module_root)
    monkeypatch.setattr(populate_assets, "DEFAULT_SKILLS_ROOT", skills_root)
    monkeypatch.setattr(populate_assets, "DEFAULT_WORKFLOWS_ROOT", workflows_root)
    monkeypatch.setattr(populate_assets, "parse_pricing", lambda: {
        "blue-payment-asaas-nfse": {"model": "one-shot", "price_cents": 250000},
        "ca-nfse-validator": {"model": "free", "price_cents": 0},
    })

    counts = populate_assets.populate(str(tmp_path / "marketplace.db"))
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))

    assert counts["module"] == 1
    assert counts["skill"] == 1
    assert counts["workflow"] == 1
    assert db.get_asset("blue-payment-asaas-nfse")["price_cents"] == 250000
    assert db.get_asset("workflow-001-odoo-webhook")["price_cents"] == 1900


def test_complements(tmp_path):
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "ca-nfse-validator",
        "name": "Validador NFS-e",
        "asset_type": "skill",
        "category": "Financeiro/Fiscal",
        "complements": ["blue-payment-asaas-nfse", "suporte-nfse"],
    })
    db.upsert_asset({
        "slug": "blue-payment-asaas-nfse",
        "name": "Asaas NFS-e",
        "asset_type": "module",
        "category": "Financeiro/Fiscal",
    })
    db.upsert_asset({
        "slug": "suporte-nfse",
        "name": "Suporte NFS-e",
        "asset_type": "agent",
        "category": "Financeiro/Fiscal",
    })

    complements = db.list_complements("ca-nfse-validator")

    assert [item["slug"] for item in complements] == ["blue-payment-asaas-nfse", "suporte-nfse"]


def test_checkout(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path, "blue-payment-asaas-nfse", price_cents=250000)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, products={321: {"id": 321, "list_price": 2500.0}})

    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    monkeypatch.setenv("ODOO_URL", "https://odoo.example.com")
    result = marketplace_checkout.checkout_asset(
        "blue-payment-asaas-nfse",
        checkout_request(),
        str(db_path),
    )

    assert result["entitled"] is False
    assert result["order_id"] == 1234
    assert result["payment_url"] == "https://odoo.example.com/shop/payment?order_id=1234"
    assert any(call["model"] == "sale.order.line" and call["args"][0]["product_id"] == 321 for call in tool.calls)


def test_active_subscription_grants_all(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"subscription": True, "all": True}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result["entitled"] is True
    assert result["scope"] == "all"
    assert result["via"] == "subscription"
    assert result["download_url"].startswith("/api/assets/workflow-x/download?token=")
    assert any(call["model"] == "asaas.subscription" and call["m"] == "search_read" for call in tool.calls)
    assert not any(call["model"] == "sale.order" and call["m"] == "search_read" for call in tool.calls)
    assert not any(call["model"] == "sale.order" and call["m"] == "create" for call in tool.calls)


def test_recent_paid_order_without_subscription_grants_provisional(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    recent = datetime.now(timezone.utc).isoformat()
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"all_order": {"id": 9001, "date_order": recent}}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result["entitled"] is True
    assert result["scope"] == "all"
    assert result["via"] == "order_grace"
    assert result["download_url"].startswith("/api/assets/workflow-x/download?token=")
    assert any(call["model"] == "asaas.subscription" and call["m"] == "search_read" for call in tool.calls)
    assert any(call["model"] == "sale.order" and call["m"] == "search_read" for call in tool.calls)
    assert not any(call["model"] == "sale.order" and call["m"] == "create" for call in tool.calls)


def test_inactive_subscription_denies_even_with_historical_paid_order(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"subscription": False, "all_order": {"id": 9001, "date_order": old}}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result["entitled"] is False
    assert result["order_id"] == 1234
    all_order_calls = [
        call for call in tool.calls
        if call["model"] == "sale.order" and call["m"] == "search_read"
        and any(isinstance(item, tuple) and item[0] == "order_line.product_id.default_code" for item in call["args"][0])
    ]
    assert all_order_calls == []


def test_old_paid_order_without_subscription_denies(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"all_order": {"id": 9001, "date_order": old}}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result["entitled"] is False
    assert result["order_id"] == 1234


def test_previous_buyer_single_scope_only_for_matching_workflow(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path, "workflow-x")
    db = WorkflowDatabase(str(db_path))
    db.upsert_asset({
        "slug": "workflow-y",
        "name": "Workflow Y",
        "asset_type": "workflow",
        "category": "CRM/Vendas",
        "price_cents": 1900,
        "model": "one-shot",
    })
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asset_product_map(slug, product_id) VALUES (?, ?)", ("workflow-y", 654))
    conn.commit()
    conn.close()

    tool = FakeOdooTool(
        partners={"cliente@example.com": 99},
        entitlements={99: {"single": {"[workflow-x]"}}},
        products={
            321: {"id": 321, "list_price": 19.0, "default_code": "WF-MEDIUM"},
            654: {"id": 654, "list_price": 19.0, "default_code": "WF-MEDIUM"},
        },
    )
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")

    entitled = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))
    not_entitled = marketplace_checkout.checkout_asset("workflow-y", checkout_request(), str(db_path))

    assert entitled["entitled"] is True
    assert entitled["scope"] == "single"
    assert entitled["download_url"].startswith("/api/assets/workflow-x/download?token=")
    assert not_entitled["entitled"] is False
    assert not_entitled["order_id"] == 1234


def test_order_line_does_not_override_price_unit(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path, price_cents=2700)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, products={321: {"id": 321, "list_price": 27.0}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    line_call = next(call for call in tool.calls if call["model"] == "sale.order.line")
    assert result["entitled"] is False
    assert "price_unit" not in line_call["args"][0]
    assert line_call["args"][0]["name"] == "Workflow: Workflow X [workflow-x]"


def test_missing_partner_is_created_before_sale_order(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool()
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request("novo@example.com"), str(db_path))

    assert result["entitled"] is False
    assert any(call["model"] == "res.partner" and call["m"] == "create" for call in tool.calls)
    sale_order_call = next(call for call in tool.calls if call["model"] == "sale.order" and call["m"] == "create")
    assert sale_order_call["args"][0]["partner_id"] == 100


def test_empty_phone_never_in_domain(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    request = CheckoutRequest(customer=CheckoutCustomer(name="Cliente Teste", email="cliente@example.com", phone=""), plan="one-shot")

    marketplace_checkout.checkout_asset("workflow-x", request, str(db_path))

    domains = [call["args"][0] for call in tool.calls if call["model"] == "res.partner" and call["m"] == "search_read"]
    assert not any(isinstance(item, tuple) and item[0] in ("phone", "mobile") and item[2] == "" for domain in domains for item in domain)


def test_empty_email_never_in_domain(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(phones={"5511999999999": 99})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    marketplace_checkout.checkout_asset("workflow-x", phone_only_request(), str(db_path))

    domains = [call["args"][0] for call in tool.calls if call["model"] == "res.partner" and call["m"] == "search_read"]
    assert not any(isinstance(item, tuple) and item[0] == "email" and item[2] == "" for domain in domains for item in domain)


def test_email_match(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    marketplace_checkout.checkout_asset("workflow-x", checkout_request("CLIENTE@example.com"), str(db_path))

    sale_order_call = next(call for call in tool.calls if call["model"] == "sale.order" and call["m"] == "create")
    assert sale_order_call["args"][0]["partner_id"] == 99


def test_phone_match(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(phones={"5511999999999": 88})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    marketplace_checkout.checkout_asset("workflow-x", phone_only_request(), str(db_path))

    sale_order_call = next(call for call in tool.calls if call["model"] == "sale.order" and call["m"] == "create")
    assert sale_order_call["args"][0]["partner_id"] == 88


def test_email_phone_conflict_rejected(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, phones={"5511999999999": 88})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    with pytest.raises(ValueError, match="Identity conflict"):
        marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))


def test_missing_identity_rejected(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool()
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    request = CheckoutRequest(customer=CheckoutCustomer(name="Cliente Teste", email="", phone="123"), plan="one-shot")

    with pytest.raises(ValueError, match="valid email or phone"):
        marketplace_checkout.checkout_asset("workflow-x", request, str(db_path))


def test_partner_create_rechecks_before_create(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool()
    original_execute = tool.execute
    searches = {"count": 0}

    def execute(payload):
        call = payload["payload"]
        if call["model"] == "res.partner" and call["m"] == "search_read":
            searches["count"] += 1
            if searches["count"] == 2:
                return {"status": "success", "result": [{"id": 77}]}
        return original_execute(payload)

    tool.execute = execute
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert searches["count"] == 2
    assert not any(call["model"] == "res.partner" and call["m"] == "create" for call in tool.calls)
    sale_order_call = next(call for call in tool.calls if call["model"] == "sale.order" and call["m"] == "create")
    assert sale_order_call["args"][0]["partner_id"] == 77


def test_product_is_required(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, products={})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    with pytest.raises(ValueError, match="Missing Odoo product"):
        marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))
    assert not any(call["model"] == "sale.order" and call["m"] == "create" for call in tool.calls)


def test_catalog_product_price_mismatch_rejected(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path, price_cents=1900)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, products={321: {"id": 321, "list_price": 27.0}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    with pytest.raises(ValueError, match="Preço em atualização"):
        marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))
    assert not any(call["model"] == "sale.order" and call["m"] == "create" for call in tool.calls)


def test_free_download_public(tmp_path, monkeypatch):
    asset_path = tmp_path / "free.json"
    asset_path.write_text("{}", encoding="utf-8")
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "free-workflow",
        "name": "Free Workflow",
        "asset_type": "workflow",
        "path": str(asset_path),
        "price_cents": 0,
        "model": "free",
    })
    monkeypatch.setattr(api_server, "db", db)

    response = asyncio.run(api_server.download_asset("free-workflow"))

    assert str(response.path) == str(asset_path)


def test_paid_direct_download_rejected(tmp_path, monkeypatch):
    asset_path = tmp_path / "paid.json"
    asset_path.write_text("{}", encoding="utf-8")
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "paid-workflow",
        "name": "Paid Workflow",
        "asset_type": "workflow",
        "path": str(asset_path),
        "price_cents": 1900,
        "model": "one-shot",
    })
    monkeypatch.setattr(api_server, "db", db)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")

    with pytest.raises(api_server.HTTPException) as exc:
        asyncio.run(api_server.download_asset("paid-workflow", token=None))
    assert exc.value.status_code == 403


def test_paid_valid_token_allowed(tmp_path, monkeypatch):
    asset_path = tmp_path / "paid.json"
    asset_path.write_text("{}", encoding="utf-8")
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "paid-workflow",
        "name": "Paid Workflow",
        "asset_type": "workflow",
        "path": str(asset_path),
        "price_cents": 1900,
        "model": "one-shot",
    })
    monkeypatch.setattr(api_server, "db", db)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")
    token = marketplace_checkout.sign_download_token("paid-workflow", 99, marketplace_checkout.DOWNLOAD_TOKEN_SCOPE)

    response = asyncio.run(api_server.download_asset("paid-workflow", token=token))

    assert str(response.path) == str(asset_path)


def test_token_wrong_slug_rejected(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")
    token = marketplace_checkout.sign_download_token("workflow-a", 99, marketplace_checkout.DOWNLOAD_TOKEN_SCOPE)

    assert marketplace_checkout.validate_download_token(token, "workflow-b") is False


def test_token_expired_rejected(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")
    token = marketplace_checkout.sign_download_token("workflow-a", 99, marketplace_checkout.DOWNLOAD_TOKEN_SCOPE, now=int(time.time()) - 700)

    assert marketplace_checkout.validate_download_token(token, "workflow-a") is False


def test_token_tampered_rejected(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")
    token = marketplace_checkout.sign_download_token("workflow-a", 99, marketplace_checkout.DOWNLOAD_TOKEN_SCOPE)

    assert marketplace_checkout.validate_download_token(token[:-1] + "A", "workflow-a") is False


def test_tier_products_created_once(tmp_path):
    db_path = tmp_path / "marketplace.db"
    db = WorkflowDatabase(str(db_path))
    for slug, price_cents in (("workflow-simple", 900), ("workflow-medium", 1900), ("workflow-advanced", 2700)):
        db.upsert_asset({
            "slug": slug,
            "name": slug,
            "asset_type": "workflow",
            "category": "CRM/Vendas",
            "price_cents": price_cents,
            "model": "one-shot",
        })
    products = {}
    calls = []

    class ProductTool:
        @staticmethod
        def execute(payload):
            call = payload["payload"]
            calls.append(call)
            if call["model"] == "product.product" and call["m"] == "search_read":
                tmpl = call["args"][0][0][2]
                return {"status": "success", "result": [{"id": int(tmpl) + 1000}]}
            if call["model"] == "product.template" and call["m"] == "search_read":
                code = call["args"][0][0][2]
                return {"status": "success", "result": [{"id": products[code]}] if code in products else []}
            if call["model"] == "product.template" and call["m"] == "create":
                vals = call["args"][0]
                products[vals["default_code"]] = 500 + len(products)
                return {"status": "success", "result": products[vals["default_code"]]}
            return {"status": "error", "data": "unexpected"}

    first = populate_assets.ensure_workflow_tier_products(ProductTool, db)
    second = populate_assets.ensure_workflow_tier_products(ProductTool, db)

    create_calls = [call for call in calls if call["model"] == "product.template" and call["m"] == "create"]
    assert first == {"checked": 3, "created": 3, "mapped": 3}
    assert second == {"checked": 3, "created": 0, "mapped": 3}
    assert [call["args"][0]["default_code"] for call in create_calls] == ["WF-SIMPLE", "WF-MEDIUM", "WF-ADVANCED"]


def test_no_per_workflow_product_created(tmp_path):
    db = WorkflowDatabase(str(tmp_path / "marketplace.db"))
    db.upsert_asset({
        "slug": "workflow-x",
        "name": "Workflow X",
        "asset_type": "workflow",
        "category": "CRM/Vendas",
        "price_cents": 1900,
        "model": "one-shot",
    })
    created_codes = []

    class ProductTool:
        @staticmethod
        def execute(payload):
            call = payload["payload"]
            if call["model"] == "product.product" and call["m"] == "search_read":
                tmpl = call["args"][0][0][2]
                return {"status": "success", "result": [{"id": int(tmpl) + 1000}]}
            if call["model"] == "product.template" and call["m"] == "search_read":
                return {"status": "success", "result": []}
            if call["model"] == "product.template" and call["m"] == "create":
                created_codes.append(call["args"][0]["default_code"])
                return {"status": "success", "result": len(created_codes)}
            return {"status": "error", "data": "unexpected"}

    populate_assets.ensure_workflow_tier_products(ProductTool, db)

    assert "WF-workflow-x" not in created_codes
    assert created_codes == ["WF-SIMPLE", "WF-MEDIUM", "WF-ADVANCED"]


def test_single_entitlement_by_slug_still_works(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"single": {"[workflow-x]"}}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)
    monkeypatch.setenv("MARKETPLACE_DOWNLOAD_SECRET", "test-secret")

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result["entitled"] is True
    single_call = next(
        call for call in tool.calls
        if call["model"] == "sale.order" and call["m"] == "search_read"
        and any(isinstance(item, tuple) and item[0] == "order_line.name" for item in call["args"][0])
    )
    assert ("order_line.name", "ilike", "[workflow-x]") in single_call["args"][0]

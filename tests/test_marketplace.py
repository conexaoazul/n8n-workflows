import json
import sqlite3
from pathlib import Path

import marketplace_checkout
import populate_assets
from marketplace_checkout import CheckoutCustomer, CheckoutRequest
from workflow_db import WorkflowDatabase


class FakeOdooTool:
    def __init__(self, partners=None, entitlements=None):
        self.partners = partners or {}
        self.entitlements = entitlements or {}
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
            partner_id = self.partners.get(email)
            return {"status": "success", "result": [{"id": partner_id}] if partner_id else []}

        if model == "res.partner" and method == "create":
            partner_id = self.next_partner_id
            self.next_partner_id += 1
            self.partners[args[0]["email"]] = partner_id
            return {"status": "success", "result": partner_id}

        if model == "sale.order" and method == "search_read":
            domain = args[0]
            partner_id = next((item[2] for item in domain if isinstance(item, tuple) and item[:2] == ("partner_id", "=")), None)
            default_code_filter = next(
                (item for item in domain if isinstance(item, tuple) and item[0] == "order_line.product_id.default_code"),
                None,
            )
            if default_code_filter and default_code_filter[1] == "in":
                has_all = bool(self.entitlements.get(partner_id, {}).get("all"))
                return {"status": "success", "result": [{"id": 9001}] if has_all else []}
            if default_code_filter and default_code_filter[1] == "=":
                code = default_code_filter[2]
                has_single = code in self.entitlements.get(partner_id, {}).get("single", set())
                return {"status": "success", "result": [{"id": 9002}] if has_single else []}
            return {"status": "success", "result": []}

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
    tool = FakeOdooTool(partners={"cliente@example.com": 99})

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


def test_subscriber_all_access_returns_download_without_sale_order(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"all": True}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    assert result == {"entitled": True, "scope": "all", "download_url": "/api/assets/workflow-x/download"}
    assert not any(call["model"] == "sale.order" and call["m"] == "create" for call in tool.calls)


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

    tool = FakeOdooTool(partners={"cliente@example.com": 99}, entitlements={99: {"single": {"WF-workflow-x"}}})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    entitled = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))
    not_entitled = marketplace_checkout.checkout_asset("workflow-y", checkout_request(), str(db_path))

    assert entitled == {"entitled": True, "scope": "single", "download_url": "/api/assets/workflow-x/download"}
    assert not_entitled["entitled"] is False
    assert not_entitled["order_id"] == 1234


def test_non_subscriber_creates_sale_order_with_asset_price(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path, price_cents=2700)
    tool = FakeOdooTool(partners={"cliente@example.com": 99})
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request(), str(db_path))

    line_call = next(call for call in tool.calls if call["model"] == "sale.order.line")
    assert result["entitled"] is False
    assert line_call["args"][0]["price_unit"] == 27


def test_missing_partner_is_created_before_sale_order(tmp_path, monkeypatch):
    db_path = create_checkout_db(tmp_path)
    tool = FakeOdooTool()
    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: tool)

    result = marketplace_checkout.checkout_asset("workflow-x", checkout_request("novo@example.com"), str(db_path))

    assert result["entitled"] is False
    assert any(call["model"] == "res.partner" and call["m"] == "create" for call in tool.calls)
    sale_order_call = next(call for call in tool.calls if call["model"] == "sale.order" and call["m"] == "create")
    assert sale_order_call["args"][0]["partner_id"] == 100


def test_ensure_workflow_products_is_idempotent(tmp_path):
    db_path = tmp_path / "marketplace.db"
    db = WorkflowDatabase(str(db_path))
    db.upsert_asset({
        "slug": "workflow-x",
        "name": "Workflow X",
        "asset_type": "workflow",
        "category": "CRM/Vendas",
        "price_cents": 1900,
        "model": "one-shot",
    })
    products = {}
    calls = []

    class ProductTool:
        @staticmethod
        def execute(payload):
            call = payload["payload"]
            calls.append(call)
            if call["model"] == "product.template" and call["m"] == "search_read":
                code = call["args"][0][0][2]
                return {"status": "success", "result": [{"id": products[code]}] if code in products else []}
            if call["model"] == "product.template" and call["m"] == "create":
                vals = call["args"][0]
                products[vals["default_code"]] = 501
                return {"status": "success", "result": 501}
            return {"status": "error", "data": "unexpected"}

    first = populate_assets.ensure_workflow_products(ProductTool, db)
    second = populate_assets.ensure_workflow_products(ProductTool, db)

    create_calls = [call for call in calls if call["model"] == "product.template" and call["m"] == "create"]
    assert first == {"checked": 1, "created": 1, "mapped": 1}
    assert second == {"checked": 1, "created": 0, "mapped": 1}
    assert len(create_calls) == 1

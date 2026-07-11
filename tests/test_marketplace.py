import json
import sqlite3
from pathlib import Path

import marketplace_checkout
import populate_assets
from marketplace_checkout import CheckoutCustomer, CheckoutRequest
from workflow_db import WorkflowDatabase


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
    db_path = tmp_path / "marketplace.db"
    db = WorkflowDatabase(str(db_path))
    db.upsert_asset({
        "slug": "blue-payment-asaas-nfse",
        "name": "Asaas NFS-e",
        "asset_type": "module",
        "category": "Financeiro/Fiscal",
        "price_cents": 250000,
        "model": "one-shot",
    })
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO asset_product_map(slug, product_id) VALUES (?, ?)", ("blue-payment-asaas-nfse", 321))
    conn.commit()
    conn.close()

    calls = []

    class FakeTool:
        @staticmethod
        def execute(payload):
            call = payload["payload"]
            calls.append(call)
            if call["model"] == "res.partner":
                return {"status": "success", "result": [{"id": 99}]}
            if call["model"] == "sale.order":
                return {"status": "success", "result": 1234}
            if call["model"] == "sale.order.line":
                return {"status": "success", "result": 4321}
            return {"status": "error", "data": "unexpected"}

    monkeypatch.setattr(marketplace_checkout, "load_odoo_360_tool", lambda: FakeTool)
    monkeypatch.setenv("ODOO_URL", "https://odoo.example.com")
    result = marketplace_checkout.checkout_asset(
        "blue-payment-asaas-nfse",
        CheckoutRequest(
            customer=CheckoutCustomer(name="Cliente Teste", email="cliente@example.com", phone="+5511999999999"),
            plan="one-shot",
        ),
        str(db_path),
    )

    assert result["order_id"] == 1234
    assert result["payment_url"] == "https://odoo.example.com/shop/payment?order_id=1234"
    assert any(call["model"] == "sale.order.line" and call["args"][0]["product_id"] == 321 for call in calls)

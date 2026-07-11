#!/usr/bin/env python3
"""Populate CA Marketplace assets from local modules, skills and workflows."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from workflow_db import WorkflowDatabase


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_MODULE_ROOT = Path("/docker/azul/19/mod/BlueApps19")
DEFAULT_SKILLS_ROOT = Path("/docker/openclaw/.openclaw/skills-for-clients/skills")
DEFAULT_PRICING_FILE = Path("/docker/openclaw/ASSET-PRICING.md")
DEFAULT_WORKFLOWS_ROOT = REPO_ROOT / "workflows"

CATEGORIES = {
    "financeiro": "Financeiro/Fiscal",
    "fiscal": "Financeiro/Fiscal",
    "nfse": "Financeiro/Fiscal",
    "nfs-e": "Financeiro/Fiscal",
    "nfe": "Financeiro/Fiscal",
    "cobranca": "Financeiro/Fiscal",
    "cobrança": "Financeiro/Fiscal",
    "asaas": "Financeiro/Fiscal",
    "payment": "Financeiro/Fiscal",
    "crm": "CRM/Vendas",
    "sales": "CRM/Vendas",
    "sale": "CRM/Vendas",
    "lead": "CRM/Vendas",
    "proposta": "CRM/Vendas",
    "chatwoot": "Comunicação",
    "whatsapp": "Comunicação",
    "email": "Comunicação",
    "otp": "Comunicação",
    "evolution": "Comunicação",
    "docker": "Infra/DevOps",
    "devops": "Infra/DevOps",
    "health": "Infra/DevOps",
    "ha": "Infra/DevOps",
    "s3": "Infra/DevOps",
    "ixc": "Provedor ISP",
    "provedor": "Provedor ISP",
    "assertiva": "Consultas/Data",
    "lemiti": "Consultas/Data",
    "credit": "Consultas/Data",
    "query": "Consultas/Data",
    "consulta": "Consultas/Data",
    "data": "Consultas/Data",
    "agent": "Agentes",
    "paperclip": "Agentes",
    "routine": "Agentes",
    "openclaw": "Agentes",
}

WORKFLOW_PRIORITY_TERMS = (
    "leadgate",
    "asaas",
    "chatwoot",
    "odoo",
    "whatsapp",
    "lead",
    "crm",
    "sales",
    "webhook",
    "http",
    "email",
)

DEFAULT_COMPLEMENTS = {
    "ca-nfse-validator": ["blue-payment-asaas-nfse", "suporte-nfse"],
    "blue-payment-asaas-nfse": ["ca-nfse-validator", "setup-express", "suporte-nfse"],
    "blue-payment-asaas": ["blue-payment-asaas-nfse", "suporte-nfse"],
    "ca-odoo-cobranca": ["conexaoazul-cobranca-email", "blue-carta-cobranca", "setup-express"],
    "blue-carta-cobranca": ["ca-odoo-cobranca", "suporte-nfse"],
    "conexaoazul-cobranca-email": ["blue-carta-cobranca", "ca-odoo-cobranca"],
    "ca-odoo-ha": ["operacao-gerenciada", "suporte-nfse"],
    "paperclip-create-agent": ["operacao-gerenciada", "ca-odoo-ha"],
}

PLAN_ASSETS = [
    {
        "slug": "setup-express",
        "name": "Setup Express",
        "asset_type": "agent",
        "category": "Agentes",
        "repo": "dhy",
        "path": "PLANS.md",
        "summary": "Call de 1h, .env, primeira NFS-e e audit inicial.",
        "description": "Plano one-shot para ativar o asset comprado com configuração assistida.",
        "price_cents": 60000,
        "currency": "BRL",
        "license": None,
        "model": "one-shot",
        "tags": ["setup", "onboarding", "nfs-e"],
        "popularity": 80,
        "complements": ["suporte-nfse"],
    },
    {
        "slug": "suporte-nfse",
        "name": "Suporte NFS-e",
        "asset_type": "agent",
        "category": "Financeiro/Fiscal",
        "repo": "dhy",
        "path": "PLANS.md",
        "summary": "SLA 48h, health-check e updates mensais.",
        "description": "Plano recorrente de suporte para operação fiscal e cobrança.",
        "price_cents": 29000,
        "currency": "BRL",
        "license": None,
        "model": "subscription",
        "tags": ["suporte", "nfse", "recorrente"],
        "popularity": 70,
        "complements": ["operacao-gerenciada"],
    },
    {
        "slug": "operacao-gerenciada",
        "name": "Operação Gerenciada",
        "asset_type": "agent",
        "category": "Agentes",
        "repo": "dhy",
        "path": "PLANS.md",
        "summary": "Emissão, reconciliação, KPI e rotina mensal de operação.",
        "description": "Plano recorrente para clientes que querem operação assistida ponta a ponta.",
        "price_cents": 89000,
        "currency": "BRL",
        "license": None,
        "model": "subscription",
        "tags": ["operacao", "recorrente", "paperclip"],
        "popularity": 65,
        "complements": ["ca-odoo-ha"],
    },
]


def slugify(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def map_category(*parts: str) -> str:
    haystack = " ".join(part or "" for part in parts).lower()
    normalized = haystack.replace("_", "-")
    for token, category in CATEGORIES.items():
        if token in normalized:
            return category
    return "CRM/Vendas"


def parse_ticket_to_cents(ticket: str) -> int:
    if not ticket or ticket.strip() in {"—", "-"}:
        return 0
    text = ticket.lower()
    match = re.search(r"r\$\s*([\d.,]+)", text)
    if not match:
        return 0
    value = match.group(1).replace(".", "").replace(",", ".")
    try:
        return int(round(float(value) * 100))
    except ValueError:
        return 0


def parse_pricing(pricing_file: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    pricing_file = pricing_file or DEFAULT_PRICING_FILE
    pricing: Dict[str, Dict[str, Any]] = {}
    if not pricing_file.exists():
        return pricing
    for line in pricing_file.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        slug_match = re.search(r"`([^`]+)`", cells[0])
        if not slug_match:
            continue
        slug = slug_match.group(1).strip()
        model = "one-shot"
        ticket_cell = cells[-1]
        for cell in cells[2:-1]:
            clean = re.sub(r"[*`]", "", cell).strip().lower()
            if any(item in clean for item in ("free", "one-shot", "subscription", "credits")):
                model = clean.split()[0]
                if model == "free":
                    model = "free"
                break
        pricing[slug] = {
            "model": model,
            "price_cents": parse_ticket_to_cents(ticket_cell),
        }
    return pricing


def parse_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        manifest = ast.literal_eval(content)
    except (SyntaxError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def build_module_assets(pricing: Dict[str, Dict[str, Any]], module_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    module_root = module_root or DEFAULT_MODULE_ROOT
    assets: List[Dict[str, Any]] = []
    if not module_root.exists():
        return assets
    for manifest_path in sorted(module_root.glob("*/__manifest__.py")):
        module_name = manifest_path.parent.name
        manifest = parse_manifest(manifest_path)
        if not manifest:
            continue
        slug = slugify(module_name)
        price = pricing.get(slug, {})
        summary = manifest.get("summary") or manifest.get("description") or manifest.get("name") or module_name
        description = manifest.get("description") or summary
        license_name = manifest.get("license")
        model = price.get("model") or ("free" if license_name in {"LGPL-3", "AGPL-3"} else "one-shot")
        assets.append({
            "slug": slug,
            "name": manifest.get("name") or module_name,
            "asset_type": "module",
            "category": map_category(module_name, manifest.get("category", ""), summary, description),
            "repo": "BlueApps19",
            "path": str(manifest_path.parent),
            "summary": str(summary).strip()[:500],
            "description": str(description).strip(),
            "price_cents": price.get("price_cents", 0),
            "currency": "BRL",
            "license": license_name,
            "model": model,
            "tags": [module_name, manifest.get("category", ""), license_name],
            "popularity": 50,
            "complements": DEFAULT_COMPLEMENTS.get(slug, []),
        })
    return assets


def first_markdown_summary(content: str) -> str:
    for line in content.splitlines():
        clean = line.strip().strip("#").strip()
        if clean:
            return clean[:500]
    return ""


def build_skill_assets(pricing: Dict[str, Dict[str, Any]], skills_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    skills_root = skills_root or DEFAULT_SKILLS_ROOT
    assets: List[Dict[str, Any]] = []
    if not skills_root.exists():
        return assets
    for skill_file in sorted(skills_root.glob("*/SKILL.md")):
        slug = slugify(skill_file.parent.name)
        content = skill_file.read_text(encoding="utf-8", errors="replace")
        first_heading = first_markdown_summary(content)
        price = pricing.get(slug, {})
        assets.append({
            "slug": slug,
            "name": first_heading or slug.replace("-", " ").title(),
            "asset_type": "skill",
            "category": map_category(slug, content[:1000]),
            "repo": "odoo-claude-skills",
            "path": str(skill_file),
            "summary": first_heading,
            "description": content[:4000],
            "price_cents": price.get("price_cents", 0),
            "currency": "BRL",
            "license": "LGPL-3",
            "model": price.get("model", "free"),
            "tags": ["claude-code", "skill", slug],
            "popularity": 60,
            "complements": DEFAULT_COMPLEMENTS.get(slug, []),
        })
    return assets


def workflow_score(path: Path) -> int:
    text = str(path).lower()
    return sum((len(WORKFLOW_PRIORITY_TERMS) - idx) * 10 for idx, term in enumerate(WORKFLOW_PRIORITY_TERMS) if term in text)


def describe_workflow(path: Path) -> Dict[str, Any]:
    name = path.stem
    description = ""
    tags: List[str] = ["n8n", "workflow"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data.get("name") or name
        nodes = data.get("nodes") or []
        node_types = [node.get("type", "").split(".")[-1] for node in nodes if isinstance(node, dict)]
        tags.extend(sorted({item for item in node_types if item})[:8])
        description = f"Workflow n8n com {len(nodes)} nós: {', '.join(tags[2:6])}."
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        description = "Workflow n8n selecionado para automação comercial."
    return {"name": name, "description": description, "tags": tags}


def build_workflow_assets(workflows_root: Optional[Path] = None, limit: int = 25) -> List[Dict[str, Any]]:
    workflows_root = workflows_root or DEFAULT_WORKFLOWS_ROOT
    if not workflows_root.exists():
        return []
    candidates = sorted(workflows_root.rglob("*.json"), key=lambda p: (-workflow_score(p), str(p)))[:limit]
    assets: List[Dict[str, Any]] = []
    for path in candidates:
        meta = describe_workflow(path)
        slug = f"workflow-{slugify(path.stem)}"
        try:
            stored_path = str(path.relative_to(REPO_ROOT))
        except ValueError:
            stored_path = str(path)
        assets.append({
            "slug": slug,
            "name": meta["name"],
            "asset_type": "workflow",
            "category": map_category(str(path), meta["description"]),
            "repo": "n8n-workflows",
            "path": stored_path,
            "summary": meta["description"],
            "description": meta["description"],
            "price_cents": 0,
            "currency": "BRL",
            "license": "free",
            "model": "free",
            "tags": meta["tags"],
            "popularity": workflow_score(path),
            "complements": ["setup-express"] if workflow_score(path) else [],
        })
    return assets


def build_agent_assets() -> List[Dict[str, Any]]:
    assets = [
        {
            "slug": "paperclip-create-agent",
            "name": "Paperclip Create Agent",
            "asset_type": "agent",
            "category": "Agentes",
            "repo": "paperclip",
            "path": "/docker/paperclip/skills/paperclip-create-agent/SKILL.md",
            "summary": "Criação governada de agentes Paperclip para rotinas operacionais.",
            "description": "Asset recorrente para contratar e configurar agentes com governança Paperclip.",
            "price_cents": 89000,
            "currency": "BRL",
            "license": None,
            "model": "subscription",
            "tags": ["paperclip", "create_agent", "agent"],
            "popularity": 75,
            "complements": DEFAULT_COMPLEMENTS["paperclip-create-agent"],
        },
        {
            "slug": "routine-templates",
            "name": "Routine Templates",
            "asset_type": "agent",
            "category": "Agentes",
            "repo": "paperclip",
            "path": "/docker/openclaw/.openclaw/arsenal/skills/paperclip/references/routines.md",
            "summary": "Templates de rotinas recorrentes para cadência, saúde e pós-venda.",
            "description": "Pacote de templates para ativar rotinas recorrentes no Paperclip/OpenClaw.",
            "price_cents": 29000,
            "currency": "BRL",
            "license": None,
            "model": "subscription",
            "tags": ["routine", "paperclip", "post-sale"],
            "popularity": 55,
            "complements": ["paperclip-create-agent", "operacao-gerenciada"],
        },
    ]
    return [*assets, *PLAN_ASSETS]


def populate(db_path: Optional[str] = None) -> Dict[str, int]:
    db = WorkflowDatabase(db_path)
    pricing = parse_pricing()
    sources = {
        "module": build_module_assets(pricing),
        "skill": build_skill_assets(pricing),
        "workflow": build_workflow_assets(),
        "agent": build_agent_assets(),
    }
    counts: Dict[str, int] = {}
    for asset_type, assets in sources.items():
        counts[asset_type] = 0
        for asset in assets:
            db.upsert_asset(asset)
            counts[asset_type] += 1
    counts["total"] = sum(counts.values())
    return counts


def main() -> None:
    counts = populate(os.environ.get("WORKFLOW_DB_PATH"))
    print(json.dumps({"ok": True, "counts": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

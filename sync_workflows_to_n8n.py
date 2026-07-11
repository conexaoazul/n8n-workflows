#!/usr/bin/env python3
"""
Sync workflows do repo n8n-workflows → nossa instância n8n (conector.conexaoazul.com).

IMPORT INTELIGENTE:
  - dedup por nome (não reimporta workflow existente)
  - auto-tag categoria (baseado em nodes/triggers: email, chat, ai, db, http, security...)
  - validação JSON antes de importar (pula quebrados)
  - bulk: importa em lote com limite de rate
  - retry/backoff em falhas transientes
  - detecta credenciais necessárias (warn, não falha)
  - trial: ativa webhook triggers e gera URL demo corretta
  - modo --dry-run (simula, não importa)

Dois propósitos:
  1. VALIDAÇÃO (dogfooding): importar e rodar cada workflow nosso no n8n antes de vender.
  2. DEMO TRIAL p/ leads: workflows demo com webhook → URL trial → captura (Chatwoot/CRM).

Env:
  N8N_API_URL   (default: https://conector.conexaoazul.com)  ← sem .br
  N8N_API_KEY   (chave API do n8n)

Uso:
  python3 sync_workflows_to_n8n.py --list
  python3 sync_workflows_to_n8n.py --list --filter our
  python3 sync_workflows_to_n8n.py --import --filter our --dry-run
  python3 sync_workflows_to_n8n.py --import --filter our
  python3 sync_workflows_to_n8n.py --trial <wf-id>
  python3 sync_workflows_to_n8n.py --validate <wf-id>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parent
WORKFLOWS_DIR = REPO / "workflows"
OUR_DIR = WORKFLOWS_DIR / "our"
DEMO_TAG = "marketplace-demo"

N8N_URL = os.environ.get("N8N_API_URL", "https://conector.conexaoazul.com").rstrip("/")
N8N_KEY = os.environ.get("N8N_API_KEY", "")

# mapeamento node-type → categoria
CATEGORY_MAP = {
    "gmail": "email", "imap": "email", "smtp": "email", "mail": "email",
    "telegram": "chat", "whatsapp": "chat", "slack": "chat", "discord": "chat",
    "chatwoot": "chat", "evolution": "chat",
    "openai": "ai", "anthropic": "ai", "langchain": "ai", "agent": "ai",
    "postgres": "db", "mysql": "db", "redis": "db", "mongo": "db", "supabase": "db",
    "http": "http", "webhook": "http",
    "googleads": "ads", "facebook": "ads", "meta": "ads",
    "s3": "storage", "aws": "storage",
    "hubspot": "crm", "salesforce": "crm",
}


def n8n_request(method, path, payload=None, retries=3):
    if not N8N_KEY:
        return {"error": "N8N_API_KEY não configurada"}
    url = f"{N8N_URL}/api/v1/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-N8N-API-KEY": N8N_KEY,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": f"HTTP {e.code}", "body": e.read().decode()[:300]}
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": f"URLError: {e.reason}", "hint": "n8n offline/inacessível"}
    return {"error": "max retries"}


def list_existing_names():
    """Busca nomes de workflows já no n8n (para dedup)."""
    names = set()
    cursor = None
    for _ in range(10):  # paginação
        path = "workflows?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        res = n8n_request("GET", path)
        if "error" in res:
            break
        data = res.get("data", res) if isinstance(res, dict) else res
        if not data:
            break
        for w in data:
            names.add(w.get("name"))
        cursor = res.get("nextCursor") if isinstance(res, dict) else None
        if not cursor:
            break
    return names


def categorize(wf):
    """Auto-tag categoria baseado nos node types."""
    cats = set()
    for n in wf.get("nodes", []):
        t = (n.get("type") or "").lower()
        for key, cat in CATEGORY_MAP.items():
            if key in t:
                cats.add(cat)
    return sorted(cats) or ["general"]


def has_credentials(wf):
    """Detecta nodes que precisam credenciais (warn)."""
    creds = []
    for n in wf.get("nodes", []):
        c = n.get("credentials") or n.get("parameters", {}).get("credentials")
        if c:
            creds.append(n.get("type"))
    return creds


def validate_json(wf):
    """Valida estrutura mínima de workflow exportado."""
    if not isinstance(wf, dict):
        return False, "não é dict"
    if "nodes" not in wf or not isinstance(wf["nodes"], list):
        return False, "sem nodes"
    if "connections" not in wf:
        return False, "sem connections"
    return True, "ok"


def list_workflows(filter_tag=None):
    out = []
    dirs = [OUR_DIR] if filter_tag == "our" else [OUR_DIR, WORKFLOWS_DIR]
    seen = set()
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            if not p.is_file() or p in seen:
                continue
            seen.add(p)
            try:
                wf = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            ok, _ = validate_json(wf)
            triggers = [n.get("type", "") for n in wf.get("nodes", [])
                        if any(k in n.get("type", "").lower()
                               for k in ("trigger", "webhook", "form", "chat"))]
            out.append({
                "file": str(p.relative_to(REPO)),
                "name": wf.get("name", p.stem),
                "nodes": len(wf.get("nodes", [])),
                "categories": categorize(wf),
                "triggers": triggers[:3],
                "needs_creds": bool(has_credentials(wf)),
                "valid": ok,
                "trial_capable": bool(triggers),
            })
    return out


def normalize_for_n8n(wf, categories):
    return {
        "name": wf.get("name", "Imported workflow"),
        "nodes": wf.get("nodes", []),
        "connections": wf.get("connections", {}),
        "settings": wf.get("settings", {"executionOrder": "v1"}),
        "active": False,
        "tags": [{"name": DEMO_TAG}] + [{"name": f"cat:{c}"} for c in categories],
    }


def import_workflow(path, existing_names, dry_run=False):
    try:
        wf = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"JSON inválido: {e}"}
    ok, msg = validate_json(wf)
    if not ok:
        return {"error": f"inválido: {msg}"}
    name = wf.get("name", Path(path).stem)
    if name in existing_names:
        return {"skipped": "dedup", "name": name}
    cats = categorize(wf)
    payload = normalize_for_n8n(wf, cats)
    if dry_run:
        return {"dry_run": True, "name": name, "categories": cats,
                "needs_creds": bool(has_credentials(wf))}
    res = n8n_request("POST", "workflows", payload)
    if "error" in res:
        return res
    return {"id": res.get("id"), "name": res.get("name"),
            "categories": cats, "needs_creds": bool(has_credentials(wf))}


def trial_workflow(wf_id):
    """Ativa workflow webhook e retorna URL demo trial (production path)."""
    res = n8n_request("PATCH", f"workflows/{wf_id}", {"active": True})
    if "error" in res:
        return res
    wf = n8n_request("GET", f"workflows/{wf_id}")
    urls = []
    for n in wf.get("nodes", []):
        if n.get("type") == "n8n-nodes-base.webhook":
            params = n.get("parameters", {})
            path = params.get("path", "")
            method = params.get("httpMethod", "GET")
            # n8n production webhook URL: /webhook/{path}
            urls.append({"method": method, "url": f"{N8N_URL}/webhook/{path}"})
    return {"id": wf_id, "active": True, "trial_urls": urls,
            "hint": "Compartilhe trial_urls com o lead. Captura via Chatwoot/CRM."}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true")
    p.add_argument("--filter", choices=["our", "all"], default="all")
    p.add_argument("--import", dest="do_import", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="simula import sem POSTar")
    p.add_argument("--trial", metavar="WF_ID", help="ativa workflow e retorna URL demo")
    p.add_argument("--validate", metavar="WF_ID", help="indica validação via MCP n8n_test_workflow")
    args = p.parse_args()

    if args.list:
        wfs = list_workflows(args.filter)
        print(f"{'name':40} {'nodes':>5} {'trial':>6} {'creds':>5}  categories")
        for w in wfs:
            flag = "sim" if w["trial_capable"] else "nao"
            creds = "sim" if w["needs_creds"] else "nao"
            print(f"{w['name'][:40]:40} {w['nodes']:>5} {flag:>6} {creds:>5}  {','.join(w['categories'])}")
        print(f"\nTotal: {len(wfs)} ({sum(1 for w in wfs if w['trial_capable'])} trial-capable, "
              f"{sum(1 for w in wfs if w['valid'])} válidos)")
        return

    if args.do_import:
        wfs = list_workflows(args.filter)
        existing = list_existing_names() if not args.dry_run else set()
        if not args.dry_run and not existing and N8N_KEY:
            print("[warn] não foi possível listar workflows existentes (dedup parcial)")
        ok = skip = fail = 0
        for w in wfs:
            r = import_workflow(w["file"], existing, args.dry_run)
            if "skipped" in r:
                print(f"[SKIP] {w['name']} (dedup — já existe)")
                skip += 1
            elif "error" in r:
                print(f"[FAIL] {w['name']}: {r['error']}")
                if "offline" in str(r.get("hint", "")) or "URLError" in str(r["error"]):
                    print("\n[ABORT] n8n offline. Restaure conector.conexaoazul.com e tente.")
                    return
                fail += 1
            elif r.get("dry_run"):
                creds = " (precisa creds)" if r["needs_creds"] else ""
                print(f"[DRY] {w['name']} → cats={r['categories']}{creds}")
                ok += 1
            else:
                print(f"[OK] {w['name']} → id {r['id']} cats={r['categories']}"
                      + (" (precisa creds!)" if r["needs_creds"] else ""))
                ok += 1
                existing.add(w["name"])
        mode = "DRY-RUN" if args.dry_run else "IMPORT"
        print(f"\n{mode}: {ok} ok, {skip} dedup, {fail} fail de {len(wfs)}")
        return

    if args.trial:
        print(json.dumps(trial_workflow(args.trial), indent=2, ensure_ascii=False))
        return

    if args.validate:
        print(f"Validar workflow {args.validate} via MCP:")
        print(f"  mcp__n8n__n8n_test_workflow(workflowId={args.validate}, triggerType=webhook)")
        return

    p.print_help()


if __name__ == "__main__":
    main()
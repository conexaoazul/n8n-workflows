#!/usr/bin/env python3
"""
Sync workflows do repo n8n-workflows → nossa instância n8n (conector.conexaoazul.com.br).

Dois propósitos:
  1. VALIDAÇÃO (dogfooding): importar e rodar cada workflow nosso no n8n antes de vender.
  2. DEMO TRIAL p/ leads: workflows demo com webhook trigger → URL pública trial →
     lead testa de verdade → captura (Chatwoot/CRM) → upgrade p/ plano pago.

Modos:
  --list            lista workflows candidatos (nossos + selecionados da coleção)
  --import          importa no n8n (INATIVO por padrão, tag marketplace-demo)
  --trial <slug>    ativa workflow webhook e retorna URL demo trial
  --validate <id>   roda n8n_test_workflow no workflow importado

Env:
  N8N_API_URL   (default: https://conector.conexaoazul.com.br)
  N8N_API_KEY   (chave API do n8n)

Credenciais via ENV — nunca hardcodear. n8n offline? o script avisa e aborta.

Uso:
  python3 sync_workflows_to_n8n.py --list
  python3 sync_workflows_to_n8n.py --import --filter our
  python3 sync_workflows_to_n8n.py --trial ca-marketplace-fulfillment
"""
import argparse
import json
import os
import sys
from pathlib import Path
import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parent
WORKFLOWS_DIR = REPO / "workflows"
OUR_DIR = WORKFLOWS_DIR / "our"
DEMO_TAG = "marketplace-demo"

N8N_URL = os.environ.get("N8N_API_URL", "https://conector.conexaoazul.com").rstrip("/")
N8N_KEY = os.environ.get("N8N_API_KEY", "")


def n8n_request(method, path, payload=None):
    if not N8N_KEY:
        return {"error": "N8N_API_KEY não configurada"}
    url = f"{N8N_URL}/api/v1/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-N8N-API-KEY": N8N_KEY,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode()[:500]}
    except urllib.error.URLError as e:
        return {"error": f"URLError: {e.reason}", "hint": "n8n offline/inacessível"}


def list_workflows(filter_tag=None):
    """Lista workflows JSON do repo."""
    out = []
    dirs = [OUR_DIR] if filter_tag == "our" else [OUR_DIR, WORKFLOWS_DIR]
    seen = set()
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            try:
                wf = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            triggers = [n.get("type", "") for n in wf.get("nodes", [])
                        if "trigger" in n.get("type", "").lower()
                        or "webhook" in n.get("type", "").lower()
                        or "form" in n.get("type", "").lower()
                        or "chat" in n.get("type", "").lower()]
            out.append({
                "file": str(p.relative_to(REPO)),
                "name": wf.get("name", p.stem),
                "nodes": len(wf.get("nodes", [])),
                "triggers": triggers[:3],
                "trial_capable": bool(triggers),
            })
    return out


def normalize_for_n8n(wf):
    """Converte workflow exportado p/ formato n8n create (name+nodes+connections+settings)."""
    return {
        "name": wf.get("name", "Imported workflow"),
        "nodes": wf.get("nodes", []),
        "connections": wf.get("connections", {}),
        "settings": wf.get("settings", {"executionOrder": "v1"}),
        "active": False,  # INATIVO por padrão
        "tags": [{"name": DEMO_TAG}],
    }


def import_workflow(path):
    wf = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = normalize_for_n8n(wf)
    res = n8n_request("POST", "workflows", payload)
    if "error" in res:
        return res
    return {
        "id": res.get("id"),
        "name": res.get("name"),
        "active": res.get("active"),
        "tag": DEMO_TAG,
        "imported_from": path,
    }


def trial_workflow(wf_id):
    """Ativa workflow webhook e retorna URL demo trial."""
    # ativa
    res = n8n_request("PATCH", f"workflows/{wf_id}", {"active": True})
    if "error" in res:
        return res
    # busca webhook URL
    wf = n8n_request("GET", f"workflows/{wf_id}")
    webhook_url = None
    for n in wf.get("nodes", []):
        if n.get("type") == "n8n-nodes-base.webhook":
            path = n.get("parameters", {}).get("path", "")
            webhook_url = f"{N8N_URL}/webhook/{path}"
            break
    return {
        "id": wf_id,
        "active": True,
        "trial_url": webhook_url,
        "hint": "Compartilhe a trial_url com o lead. Captura via Chatwoot/CRM.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true")
    p.add_argument("--filter", choices=["our", "all"], default="all")
    p.add_argument("--import", dest="do_import", action="store_true")
    p.add_argument("--trial", metavar="WF_ID", help="ativa workflow e retorna URL demo")
    p.add_argument("--validate", metavar="WF_ID", help="roda n8n_test_workflow")
    args = p.parse_args()

    if args.list:
        wfs = list_workflows(args.filter)
        print(f"{'name':40} {'nodes':>5} {'trial':>6}  triggers")
        for w in wfs:
            print(f"{w['name'][:40]:40} {w['nodes']:>5} {'sim' if w['trial_capable'] else 'nao':>6}  {','.join(w['triggers'])}")
        print(f"\nTotal: {len(wfs)} workflows ({sum(1 for w in wfs if w['trial_capable'])} trial-capable)")
        return

    if args.do_import:
        wfs = list_workflows(args.filter)
        ok = 0
        for w in wfs:
            r = import_workflow(w["file"])
            if "error" in r:
                print(f"[FAIL] {w['name']}: {r['error']}")
                if "offline" in str(r.get("hint", "")) or "URLError" in str(r["error"]):
                    print("\n[ABORT] n8n offline. Restaure conector.conexaoazul.com.br e tente novamente.")
                    return
            else:
                print(f"[OK] {w['name']} → id {r['id']} (inativo, tag {DEMO_TAG})")
                ok += 1
        print(f"\nImportados: {ok}/{len(wfs)}")
        return

    if args.trial:
        print(json.dumps(trial_workflow(args.trial), indent=2, ensure_ascii=False))
        return

    if args.validate:
        # via MCP n8n_test_workflow — aqui só delega指示ão
        print(f"Para validar o workflow {args.validate}, use o MCP n8n_test_workflow:")
        print(f"  mcp__n8n__n8n_test_workflow(workflowId={args.validate}, triggerType=webhook)")
        return

    p.print_help()


if __name__ == "__main__":
    main()
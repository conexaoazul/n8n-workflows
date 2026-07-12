#!/usr/bin/env python3
"""
FastAPI Server for N8N Workflow Documentation
High-performance API with sub-100ms response times.
"""

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, field_validator
from typing import Optional, List, Dict, Any
import json
import os
import re
import asyncio
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
import uvicorn

from workflow_db import WorkflowDatabase
from marketplace_checkout import CheckoutRequest, checkout_asset, validate_download_token

try:
    import lead_store
except Exception:
    lead_store = None

try:
    import otp_service
except Exception:
    otp_service = None

try:
    import chatwoot_lead_sync
except Exception:
    chatwoot_lead_sync = None

# Initialize FastAPI app
app = FastAPI(
    title="N8N Workflow Documentation API",
    description="Fast API for browsing and searching workflow documentation",
    version="2.0.0"
)

# Add middleware for performance
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
db = WorkflowDatabase()

# Startup function to verify database
@app.on_event("startup")
async def startup_event():
    """Verify database connectivity on startup."""
    try:
        stats = db.get_stats()
        if stats['total'] == 0:
            print("⚠️  Warning: No workflows found in database. Run indexing first.")
        else:
            print(f"✅ Database connected: {stats['total']} workflows indexed")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        raise

# Response models
class WorkflowSummary(BaseModel):
    id: Optional[int] = None
    filename: str
    name: str
    active: bool
    description: str = ""
    trigger_type: str = "Manual"
    complexity: str = "low"
    node_count: int = 0
    integrations: List[str] = []
    tags: List[str] = []
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    class Config:
        # Allow conversion of int to bool for active field
        validate_assignment = True
        
    @field_validator('active', mode='before')
    @classmethod
    def convert_active(cls, v):
        if isinstance(v, int):
            return bool(v)
        return v
    

class SearchResponse(BaseModel):
    workflows: List[WorkflowSummary]
    total: int
    page: int
    per_page: int
    pages: int
    query: str
    filters: Dict[str, Any]

class StatsResponse(BaseModel):
    total: int
    active: int
    inactive: int
    triggers: Dict[str, int]
    complexity: Dict[str, int]
    total_nodes: int
    unique_integrations: int
    last_indexed: str


class AssetSummary(BaseModel):
    id: Optional[int] = None
    slug: str
    name: str
    asset_type: str
    category: Optional[str] = None
    repo: Optional[str] = None
    path: Optional[str] = None
    summary: Optional[str] = None
    price_cents: int = 0
    currency: str = "BRL"
    license: Optional[str] = None
    model: Optional[str] = None
    tags: List[str] = []
    popularity: int = 0
    complements: List[str] = []
    visible: bool = True
    updated_at: Optional[str] = None


class AssetSearchResponse(BaseModel):
    assets: List[AssetSummary]
    total: int
    query: str
    filters: Dict[str, Any]


# ============================================================================
# LEAD GATE — Growth Engine Conexão Azul
# Captura email + WhatsApp + dados comerciais. Persistência resiliente.
# 4 flows: catalog_access | trial_3_days | implementation_7_days | white_label_enterprise
# ============================================================================

class LeadGateRequest(BaseModel):
    name: Optional[str] = None
    email: str
    whatsapp: str
    company: Optional[str] = None
    segment: Optional[str] = None
    challenge: Optional[str] = None
    current_tool: Optional[str] = None
    urgency: Optional[str] = None
    interest: Optional[str] = "catalog"
    conversion_flow: Optional[str] = "catalog_access"
    budget: Optional[str] = None
    use_area: Optional[str] = None
    wants_meeting: Optional[bool] = None
    best_time: Optional[str] = None
    clients_volume: Optional[str] = None
    model: Optional[str] = None
    country: Optional[str] = "BR"
    language: Optional[str] = "pt-BR"
    currency_preference: Optional[str] = "BRL"
    consent: bool
    whatsapp_verified: Optional[bool] = False
    verification_method: Optional[str] = None
    otp_fallback: Optional[bool] = False
    lead_stage: Optional[str] = "captured"
    source_url: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None
    utm_term: Optional[str] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if not v or "@" not in v or len(v) > 254:
            raise ValueError("Email inválido")
        return v.strip().lower()

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v):
        if not v or len(v.strip()) < 8 or len(v) > 30:
            raise ValueError("WhatsApp inválido")
        return v.strip()

    @field_validator("consent")
    @classmethod
    def validate_consent(cls, v):
        if v is not True:
            raise ValueError("Consentimento obrigatório")
        return v


@app.post("/api/leads/automation")
async def capture_automation_lead(lead: LeadGateRequest):
    """Captura lead do catálogo de automações. Nunca perde o lead."""
    lead_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    payload = lead.model_dump()

    # Persistência resiliente via lead_store (JSONL + SQLite). Fallback inline.
    saved = False
    if lead_store is not None:
        try:
            store_payload = dict(payload)
            store_payload["flow"] = payload.get("conversion_flow") or "catalog_access"
            store_payload["lgpd_consent"] = payload.get("consent")
            store_payload["whatsapp"] = payload.get("whatsapp")
            store_payload["nome"] = payload.get("name")
            store_payload["empresa"] = payload.get("company")
            store_payload["segmento"] = payload.get("segment")
            store_payload["interesse"] = payload.get("interest")
            store_payload["desafio"] = payload.get("challenge")
            store_payload["ferramenta_atual"] = payload.get("current_tool")
            store_payload["urgencia"] = payload.get("urgency")
            store_payload["orcamento"] = payload.get("budget")
            rec = lead_store.save_lead(store_payload)
            lead_id = rec.get("id", lead_id)
            saved = True
        except Exception:
            saved = False

    if not saved:
        # Fallback inline — append direto no JSONL
        try:
            leads_dir = Path(os.environ.get("LEAD_DATA_DIR", "/data/n8n-workflows/leads"))
            leads_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "id": lead_id,
                "created_at": now,
                "status": "pending_sync",
                "source": "n8n-workflows",
                "payload": payload,
            }
            with (leads_dir / "automation-leads.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # Log mínimo — nunca o payload completo (LGPD)
    print(f"[leadgate] novo lead flow={payload.get('conversion_flow')} "
          f"segment={payload.get('segment')} country={payload.get('country')}")

    # Chatwoot sync — best effort, nunca bloqueia
    chatwoot_result = {"ok": False, "reason": "unavailable"}
    if chatwoot_lead_sync is not None:
        try:
            chatwoot_result = chatwoot_lead_sync.sync_lead_to_chatwoot(payload)
        except Exception:
            chatwoot_result = {"ok": False, "reason": "exception"}

    return {
        "ok": True,
        "lead_id": lead_id,
        "status": "pending_sync",
        "access_url": "https://n8n-workflows.conexaoazul.com",
        "message": "Acesso liberado",
        "chatwoot": {
            "ok": bool(chatwoot_result.get("ok")),
            "contact_id": chatwoot_result.get("contact_id"),
            "conversation_id": chatwoot_result.get("conversation_id"),
            "note_id": chatwoot_result.get("note_id"),
            "mode": chatwoot_result.get("mode"),
        },
    }


@app.get("/api/leads/stats")
async def leads_stats():
    """Estatísticas internas do lead store."""
    if lead_store is None:
        return {"ok": False, "reason": "lead_store unavailable"}
    return {"ok": True, "stats": lead_store.stats()}


class LeadOtpSendRequest(BaseModel):
    whatsapp: str

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v):
        if not v or len(v.strip()) < 8:
            raise ValueError("WhatsApp inválido")
        return v.strip()


class LeadOtpVerifyRequest(BaseModel):
    whatsapp: str
    code: str

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v):
        if not v or len(v.strip()) < 8:
            raise ValueError("WhatsApp inválido")
        return v.strip()

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        digits = re.sub(r"\D", "", v or "")
        if len(digits) != 6:
            raise ValueError("Código deve ter 6 dígitos")
        return digits


@app.post("/api/leads/otp/send")
async def send_lead_otp(req: LeadOtpSendRequest):
    """Envia código OTP via WhatsApp. Retorna fallback se indisponível."""
    if otp_service is None:
        return {"ok": True, "sent": False, "fallback": True,
                "message": "Validação liberada em modo fallback"}
    result = otp_service.send_code(req.whatsapp)
    if result.get("fallback"):
        return {"ok": True, "sent": False, "fallback": True,
                "reason": result.get("reason"),
                "message": "Validação liberada em modo fallback"}
    return {"ok": bool(result.get("ok")), "sent": bool(result.get("sent")),
            "fallback": False, "cooldown": result.get("cooldown"),
            "message": "Código enviado pelo WhatsApp" if result.get("sent") else "Não foi possível enviar o código"}


@app.post("/api/leads/otp/verify")
async def verify_lead_otp(req: LeadOtpVerifyRequest):
    """Verifica código OTP. Retorna verified=True em fallback."""
    if otp_service is None:
        return {"ok": True, "verified": True, "fallback": True}
    return otp_service.verify_code(req.whatsapp, req.code)


@app.get("/")
async def root():
    """Serve the main documentation page."""
    static_dir = Path("static")
    index_file = static_dir / "index.html"
    if not index_file.exists():
        return HTMLResponse("""
        <html><body>
        <h1>Setup Required</h1>
        <p>Static files not found. Please ensure the static directory exists with index.html</p>
        <p>Current directory: """ + str(Path.cwd()) + """</p>
        </body></html>
        """)
    return FileResponse(str(index_file))

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "message": "N8N Workflow API is running"}

@app.get("/api/stats", response_model=StatsResponse)
async def get_stats():
    """Get workflow database statistics."""
    try:
        stats = db.get_stats()
        return StatsResponse(**stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching stats: {str(e)}")

@app.get("/api/workflows", response_model=SearchResponse)
async def search_workflows(
    q: str = Query("", description="Search query"),
    trigger: str = Query("all", description="Filter by trigger type"),
    complexity: str = Query("all", description="Filter by complexity"),
    active_only: bool = Query(False, description="Show only active workflows"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page")
):
    """Search and filter workflows with pagination."""
    try:
        offset = (page - 1) * per_page
        
        workflows, total = db.search_workflows(
            query=q,
            trigger_filter=trigger,
            complexity_filter=complexity,
            active_only=active_only,
            limit=per_page,
            offset=offset
        )
        
        # Convert to Pydantic models with error handling
        workflow_summaries = []
        for workflow in workflows:
            try:
                # Remove extra fields that aren't in the model
                clean_workflow = {
                    'id': workflow.get('id'),
                    'filename': workflow.get('filename', ''),
                    'name': workflow.get('name', ''),
                    'active': workflow.get('active', False),
                    'description': workflow.get('description', ''),
                    'trigger_type': workflow.get('trigger_type', 'Manual'),
                    'complexity': workflow.get('complexity', 'low'),
                    'node_count': workflow.get('node_count', 0),
                    'integrations': workflow.get('integrations', []),
                    'tags': workflow.get('tags', []),
                    'created_at': workflow.get('created_at'),
                    'updated_at': workflow.get('updated_at')
                }
                workflow_summaries.append(WorkflowSummary(**clean_workflow))
            except Exception as e:
                print(f"Error converting workflow {workflow.get('filename', 'unknown')}: {e}")
                # Continue with other workflows instead of failing completely
                continue
        
        pages = (total + per_page - 1) // per_page  # Ceiling division
        
        return SearchResponse(
            workflows=workflow_summaries,
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
            query=q,
            filters={
                "trigger": trigger,
                "complexity": complexity,
                "active_only": active_only
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching workflows: {str(e)}")

@app.get("/api/workflows/{filename}")
async def get_workflow_detail(filename: str):
    """Get detailed workflow information including raw JSON."""
    try:
        # Get workflow metadata from database
        workflows, _ = db.search_workflows(f'filename:"{filename}"', limit=1)
        if not workflows:
            raise HTTPException(status_code=404, detail="Workflow not found in database")
        
        workflow_meta = workflows[0]
        
        # file_path = Path(__file__).parent / "workflows" / workflow_meta.name / filename
        # print(f"当前工作目录: {workflow_meta}")
        # Load raw JSON from file
        workflows_path = Path('workflows')
        json_files = list(workflows_path.rglob("*.json"))
        file_path = [f for f in json_files if f.name == filename][0]
        if not file_path.exists():
            print(f"Warning: File {file_path} not found on filesystem but exists in database")
            raise HTTPException(status_code=404, detail=f"Workflow file '{filename}' not found on filesystem")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_json = json.load(f)
        
        return {
            "metadata": workflow_meta,
            "raw_json": raw_json
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading workflow: {str(e)}")

@app.get("/api/workflows/{filename}/download")
async def download_workflow(filename: str):
    """Download workflow JSON file."""
    try:
        workflows_path = Path('workflows')
        json_files = list(workflows_path.rglob("*.json"))
        file_path = [f for f in json_files if f.name == filename][0]
        if not os.path.exists(file_path):
            print(f"Warning: Download requested for missing file: {file_path}")
            raise HTTPException(status_code=404, detail=f"Workflow file '{filename}' not found on filesystem")
        
        return FileResponse(
            file_path,
            media_type="application/json",
            filename=filename
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Workflow file '{filename}' not found")
    except Exception as e:
        print(f"Error downloading workflow {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading workflow: {str(e)}")

@app.get("/api/workflows/{filename}/diagram")
async def get_workflow_diagram(filename: str):
    """Get Mermaid diagram code for workflow visualization."""
    try:
        workflows_path = Path('workflows')
        json_files = list(workflows_path.rglob("*.json"))
        file_path = [f for f in json_files if f.name == filename][0]
        print(f'{file_path}')
        if not file_path.exists():
            print(f"Warning: Diagram requested for missing file: {file_path}")
            raise HTTPException(status_code=404, detail=f"Workflow file '{filename}' not found on filesystem")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        nodes = data.get('nodes', [])
        connections = data.get('connections', {})
        
        # Generate Mermaid diagram
        diagram = generate_mermaid_diagram(nodes, connections)
        
        return {"diagram": diagram}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Workflow file '{filename}' not found")
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON in {filename}: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON in workflow file: {str(e)}")
    except Exception as e:
        print(f"Error generating diagram for {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error generating diagram: {str(e)}")

def generate_mermaid_diagram(nodes: List[Dict], connections: Dict) -> str:
    """Generate Mermaid.js flowchart code from workflow nodes and connections."""
    if not nodes:
        return "graph TD\n  EmptyWorkflow[No nodes found in workflow]"
    
    # Create mapping for node names to ensure valid mermaid IDs
    mermaid_ids = {}
    for i, node in enumerate(nodes):
        node_id = f"node{i}"
        node_name = node.get('name', f'Node {i}')
        mermaid_ids[node_name] = node_id
    
    # Start building the mermaid diagram
    mermaid_code = ["graph TD"]
    
    # Add nodes with styling
    for node in nodes:
        node_name = node.get('name', 'Unnamed')
        node_id = mermaid_ids[node_name]
        node_type = node.get('type', '').replace('n8n-nodes-base.', '')
        
        # Determine node style based on type
        style = ""
        if any(x in node_type.lower() for x in ['trigger', 'webhook', 'cron']):
            style = "fill:#b3e0ff,stroke:#0066cc"  # Blue for triggers
        elif any(x in node_type.lower() for x in ['if', 'switch']):
            style = "fill:#ffffb3,stroke:#e6e600"  # Yellow for conditional nodes
        elif any(x in node_type.lower() for x in ['function', 'code']):
            style = "fill:#d9b3ff,stroke:#6600cc"  # Purple for code nodes
        elif 'error' in node_type.lower():
            style = "fill:#ffb3b3,stroke:#cc0000"  # Red for error handlers
        else:
            style = "fill:#d9d9d9,stroke:#666666"  # Gray for other nodes
        
        # Add node with label (escaping special characters)
        clean_name = node_name.replace('"', "'")
        clean_type = node_type.replace('"', "'")
        label = f"{clean_name}<br>({clean_type})"
        mermaid_code.append(f"  {node_id}[\"{label}\"]")
        mermaid_code.append(f"  style {node_id} {style}")
    
    # Add connections between nodes
    for source_name, source_connections in connections.items():
        if source_name not in mermaid_ids:
            continue
        
        if isinstance(source_connections, dict) and 'main' in source_connections:
            main_connections = source_connections['main']
            
            for i, output_connections in enumerate(main_connections):
                if not isinstance(output_connections, list):
                    continue
                    
                for connection in output_connections:
                    if not isinstance(connection, dict) or 'node' not in connection:
                        continue
                        
                    target_name = connection['node']
                    if target_name not in mermaid_ids:
                        continue
                        
                    # Add arrow with output index if multiple outputs
                    label = f" -->|{i}| " if len(main_connections) > 1 else " --> "
                    mermaid_code.append(f"  {mermaid_ids[source_name]}{label}{mermaid_ids[target_name]}")
    
    # Format the final mermaid diagram code
    return "\n".join(mermaid_code)

@app.post("/api/reindex")
async def reindex_workflows(background_tasks: BackgroundTasks, force: bool = False):
    """Trigger workflow reindexing in the background."""
    def run_indexing():
        db.index_all_workflows(force_reindex=force)
    
    background_tasks.add_task(run_indexing)
    return {"message": "Reindexing started in background"}

@app.get("/api/integrations")
async def get_integrations():
    """Get list of all unique integrations."""
    try:
        stats = db.get_stats()
        # For now, return basic info. Could be enhanced to return detailed integration stats
        return {"integrations": [], "count": stats['unique_integrations']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching integrations: {str(e)}")

@app.get("/api/categories")
async def get_categories():
    """Get available workflow categories for filtering."""
    try:
        # Try to load from the generated unique categories file
        categories_file = Path("context/unique_categories.json")
        if categories_file.exists():
            with open(categories_file, 'r', encoding='utf-8') as f:
                categories = json.load(f)
            return {"categories": categories}
        else:
            # Fallback: extract categories from search_categories.json
            search_categories_file = Path("context/search_categories.json")
            if search_categories_file.exists():
                with open(search_categories_file, 'r', encoding='utf-8') as f:
                    search_data = json.load(f)
                
                unique_categories = set()
                for item in search_data:
                    if item.get('category'):
                        unique_categories.add(item['category'])
                    else:
                        unique_categories.add('Uncategorized')
                
                categories = sorted(list(unique_categories))
                return {"categories": categories}
            else:
                # Last resort: return basic categories
                return {"categories": ["Uncategorized"]}
                
    except Exception as e:
        print(f"Error loading categories: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching categories: {str(e)}")

@app.get("/api/category-mappings")
async def get_category_mappings():
    """Get filename to category mappings for client-side filtering."""
    try:
        search_categories_file = Path("context/search_categories.json")
        if not search_categories_file.exists():
            return {"mappings": {}}
        
        with open(search_categories_file, 'r', encoding='utf-8') as f:
            search_data = json.load(f)
        
        # Convert to a simple filename -> category mapping
        mappings = {}
        for item in search_data:
            filename = item.get('filename')
            category = item.get('category') or 'Uncategorized'
            if filename:
                mappings[filename] = category
        
        return {"mappings": mappings}
        
    except Exception as e:
        print(f"Error loading category mappings: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching category mappings: {str(e)}")

@app.get("/api/workflows/category/{category}", response_model=SearchResponse)
async def search_workflows_by_category(
    category: str,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page")
):
    """Search workflows by service category (messaging, database, ai_ml, etc.)."""
    try:
        offset = (page - 1) * per_page
        
        workflows, total = db.search_by_category(
            category=category,
            limit=per_page,
            offset=offset
        )
        
        # Convert to Pydantic models with error handling
        workflow_summaries = []
        for workflow in workflows:
            try:
                clean_workflow = {
                    'id': workflow.get('id'),
                    'filename': workflow.get('filename', ''),
                    'name': workflow.get('name', ''),
                    'active': workflow.get('active', False),
                    'description': workflow.get('description', ''),
                    'trigger_type': workflow.get('trigger_type', 'Manual'),
                    'complexity': workflow.get('complexity', 'low'),
                    'node_count': workflow.get('node_count', 0),
                    'integrations': workflow.get('integrations', []),
                    'tags': workflow.get('tags', []),
                    'created_at': workflow.get('created_at'),
                    'updated_at': workflow.get('updated_at')
                }
                workflow_summaries.append(WorkflowSummary(**clean_workflow))
            except Exception as e:
                print(f"Error converting workflow {workflow.get('filename', 'unknown')}: {e}")
                continue
        
        pages = (total + per_page - 1) // per_page
        
        return SearchResponse(
            workflows=workflow_summaries,
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
            query=f"category:{category}",
            filters={"category": category}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching by category: {str(e)}")


def load_asset_source(asset: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort README/manifest/raw metadata for the asset detail endpoint."""
    source_path = Path(asset.get("path") or "")
    details: Dict[str, Any] = {}
    try:
        if asset.get("asset_type") == "module" and source_path.is_dir():
            manifest = source_path / "__manifest__.py"
            readme = next((source_path / name for name in ("README.md", "readme.md") if (source_path / name).exists()), None)
            if manifest.exists():
                details["manifest"] = manifest.read_text(encoding="utf-8", errors="replace")[:12000]
            if readme:
                details["readme"] = readme.read_text(encoding="utf-8", errors="replace")[:12000]
        elif asset.get("asset_type") == "skill" and source_path.exists():
            details["readme"] = source_path.read_text(encoding="utf-8", errors="replace")[:12000]
        elif asset.get("asset_type") == "workflow":
            workflow_path = Path(asset.get("path") or "")
            if not workflow_path.is_absolute():
                workflow_path = Path.cwd() / workflow_path
            if workflow_path.exists():
                details["workflow"] = json.loads(workflow_path.read_text(encoding="utf-8"))
        elif asset.get("asset_type") == "agent" and source_path.exists():
            details["readme"] = source_path.read_text(encoding="utf-8", errors="replace")[:12000]
    except Exception as exc:
        details["source_error"] = str(exc)
    return details


def zip_asset_path(asset: Dict[str, Any]) -> Path:
    source_path = Path(asset.get("path") or "")
    if asset.get("asset_type") == "workflow":
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Workflow source not found")
        return source_path

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Asset source not found")

    temp_dir = Path(tempfile.gettempdir()) / "ca-marketplace-downloads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    zip_path = temp_dir / f"{asset['slug']}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        if source_path.is_file():
            archive.write(source_path, arcname=source_path.name)
        else:
            for item in source_path.rglob("*"):
                if item.is_file():
                    archive.write(item, arcname=str(Path(source_path.name) / item.relative_to(source_path)))
    return zip_path


@app.get("/api/assets", response_model=AssetSearchResponse)
async def search_assets(
    type: Optional[str] = Query(None, description="Asset type: workflow|skill|module|agent"),
    category: Optional[str] = Query(None, description="Marketplace category"),
    q: str = Query("", description="Full-text search query"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Search CA Marketplace assets across skills, Odoo modules, workflows and agents."""
    try:
        assets, total = db.search_assets(type=type, category=category, q=q, limit=limit, offset=offset)
        return AssetSearchResponse(
            assets=[AssetSummary(**asset) for asset in assets],
            total=total,
            query=q,
            filters={"type": type, "category": category},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching assets: {str(e)}")


@app.get("/api/assets/{slug}")
async def get_asset_detail(slug: str):
    """Return asset metadata plus source README/manifest/workflow payload when available."""
    asset = db.get_asset(slug)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"asset": asset, "source": load_asset_source(asset)}


def is_free_asset(asset: Dict[str, Any]) -> bool:
    return int(asset.get("price_cents") or 0) == 0 and asset.get("model") == "free"


@app.get("/api/assets/{slug}/download")
async def download_asset(slug: str, token: Optional[str] = Query(None)):
    """Download the asset source as JSON or ZIP, depending on asset type."""
    asset = db.get_asset(slug)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not is_free_asset(asset) and not validate_download_token(token, slug):
        raise HTTPException(status_code=403, detail="Download token required")
    file_path = zip_asset_path(asset)
    if asset.get("asset_type") == "workflow":
        return FileResponse(file_path, media_type="application/json", filename=file_path.name)
    return FileResponse(file_path, media_type="application/zip", filename=file_path.name)


@app.get("/api/assets/{slug}/complements")
async def get_asset_complements(slug: str):
    """Return configured cross-sell complements for an asset."""
    if not db.get_asset(slug):
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"asset_slug": slug, "complements": db.list_complements(slug)}


@app.post("/api/assets/{slug}/checkout")
async def checkout_marketplace_asset(slug: str, request: CheckoutRequest):
    """Create an Odoo sale.order for a mapped marketplace asset."""
    try:
        return checkout_asset(slug, request, db.db_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Checkout failed: {str(exc)}")


@app.get("/marketplace")
async def marketplace_page():
    """Serve a lightweight CA Marketplace browser."""
    assets, _ = db.search_assets(limit=200)
    tabs = [
        ("skill", "Skills"),
        ("module", "Módulos Odoo"),
        ("workflow", "Workflows n8n"),
        ("agent", "Agents"),
    ]
    grouped = {asset_type: [asset for asset in assets if asset.get("asset_type") == asset_type] for asset_type, _ in tabs}

    def card(asset: Dict[str, Any]) -> str:
        price = "Grátis" if int(asset.get("price_cents") or 0) == 0 else f"R$ {int(asset['price_cents']) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        tags = " ".join(f"<span>{tag}</span>" for tag in (asset.get("tags") or [])[:3] if tag)
        complements = ", ".join(asset.get("complements") or [])
        return f"""
        <article class="asset-card">
          <div class="asset-meta">{asset.get('category') or 'Sem categoria'} · {asset.get('model') or 'one-shot'}</div>
          <h3><a href="/api/assets/{asset['slug']}">{asset['name']}</a></h3>
          <p>{asset.get('summary') or ''}</p>
          <div class="tags">{tags}</div>
          <div class="asset-footer">
            <strong>{price}</strong>
            <a class="button" href="/api/assets/{asset['slug']}/download">Download</a>
          </div>
          <small>Complementa com: {complements or 'setup-express'}</small>
        </article>
        """

    sections = "\n".join(
        f"""
        <section id="{asset_type}">
          <h2>{label}</h2>
          <div class="grid">{''.join(card(asset) for asset in grouped[asset_type]) or '<p>Nenhum asset populado.</p>'}</div>
        </section>
        """
        for asset_type, label in tabs
    )
    html = f"""
    <!doctype html>
    <html lang="pt-BR">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>CA Marketplace</title>
      <style>
        body {{ margin:0; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f7f8fa; color:#18202a; }}
        header {{ padding:32px 5vw 20px; background:#ffffff; border-bottom:1px solid #dde2ea; }}
        h1 {{ margin:0 0 8px; font-size:32px; }}
        nav {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:20px; }}
        nav a, .button {{ color:#0b3d91; text-decoration:none; border:1px solid #9db7df; border-radius:6px; padding:8px 12px; background:#fff; }}
        main {{ padding:24px 5vw 48px; }}
        section {{ margin-bottom:36px; }}
        h2 {{ font-size:22px; margin:0 0 14px; }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:14px; }}
        .asset-card {{ background:#fff; border:1px solid #dde2ea; border-radius:8px; padding:16px; min-height:230px; display:flex; flex-direction:column; gap:10px; }}
        .asset-card h3 {{ margin:0; font-size:18px; }}
        .asset-card h3 a {{ color:#18202a; text-decoration:none; }}
        .asset-card p {{ margin:0; color:#465466; line-height:1.45; }}
        .asset-meta, small {{ color:#6b7788; font-size:12px; }}
        .tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:auto; }}
        .tags span {{ border-radius:999px; background:#eef2f6; color:#39485a; padding:4px 8px; font-size:12px; }}
        .asset-footer {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
      </style>
    </head>
    <body>
      <header>
        <h1>CA Marketplace</h1>
        <p>Skills Claude Code, módulos Odoo, workflows n8n e agents operacionais.</p>
        <nav>{''.join(f'<a href="#{asset_type}">{label}</a>' for asset_type, label in tabs)}</nav>
      </header>
      <main>{sections}</main>
    </body>
    </html>
    """
    return HTMLResponse(html)

# Custom exception handler for better error responses
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"}
    )

# Mount static files AFTER all routes are defined
static_dir = Path("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")
    print(f"✅ Static files mounted from {static_dir.absolute()}")
else:
    print(f"❌ Warning: Static directory not found at {static_dir.absolute()}")

def create_static_directory():
    """Create static directory if it doesn't exist."""
    static_dir = Path("static")
    static_dir.mkdir(exist_ok=True)
    return static_dir

def run_server(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Run the FastAPI server."""
    # Ensure static directory exists
    create_static_directory()
    
    # Debug: Check database connectivity
    try:
        stats = db.get_stats()
        print(f"✅ Database connected: {stats['total']} workflows found")
        if stats['total'] == 0:
            print("🔄 Database is empty. Indexing workflows...")
            db.index_all_workflows()
            stats = db.get_stats()
    except Exception as e:
        print(f"❌ Database error: {e}")
        print("🔄 Attempting to create and index database...")
        try:
            db.index_all_workflows()
            stats = db.get_stats()
            print(f"✅ Database created: {stats['total']} workflows indexed")
        except Exception as e2:
            print(f"❌ Failed to create database: {e2}")
            stats = {'total': 0}
    
    # Debug: Check static files
    static_path = Path("static")
    if static_path.exists():
        files = list(static_path.glob("*"))
        print(f"✅ Static files found: {[f.name for f in files]}")
    else:
        print(f"❌ Static directory not found at: {static_path.absolute()}")
    
    print(f"🚀 Starting N8N Workflow Documentation API")
    print(f"📊 Database contains {stats['total']} workflows")
    print(f"🌐 Server will be available at: http://{host}:{port}")
    print(f"📁 Static files at: http://{host}:{port}/static/")
    
    uvicorn.run(
        "api_server:app",
        host=host,
        port=port,
        reload=reload,
        access_log=True,  # Enable access logs for debugging
        log_level="info"
    )

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='N8N Workflow Documentation API Server')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind to')
    parser.add_argument('--reload', action='store_true', help='Enable auto-reload for development')
    
    args = parser.parse_args()
    
    run_server(host=args.host, port=args.port, reload=args.reload)

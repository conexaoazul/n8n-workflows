#!/usr/bin/env bash
# sync_vendors.sh — sync incremental dos submodules vendors/ + re-popula assets.
#
# Atualiza todos os submodules (vendors/workflows + vendors/skills) para o
# último commit dos forks conexaoazul/*, e re-popula a tabela assets do
# marketplace com os novos workflows/skills encontrados.
#
# Uso:
#   bash sync_vendors.sh            # sync + re-populate
#   bash sync_vendors.sh --no-commit # sync + re-populate sem commitar
#   bash sync_vendors.sh --status    # só mostra status dos submodules
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-sync}"
COMMIT=true
[[ "${1:-}" == "--no-commit" ]] && COMMIT=false

if [[ "$MODE" == "--status" ]]; then
  echo "=== submodule status ==="
  git submodule status
  exit 0
fi

echo "=== 1. Sync submodules (último commit dos forks) ==="
git submodule update --remote --merge 2>&1 | tail -20 || git submodule update --init --recursive

echo ""
echo "=== 2. Re-populate assets (inclui vendors/) ==="
WORKFLOW_DB_PATH="${WORKFLOW_DB_PATH:-workflows.db}" python3 populate_assets.py 2>&1 | tail -5 || echo "[warn] populate_assets falhou (dependências?)"

if [[ "$COMMIT" == "true" ]]; then
  echo ""
  echo "=== 3. Commit incremental ==="
  git config user.name "github-actions[bot]" 2>/dev/null || true
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com" 2>/dev/null || true
  git add .gitmodules vendors/ workflows.db 2>/dev/null || true
  if git diff --staged --quiet; then
    echo "Sem mudanças nos vendors/assets."
  else
    git commit -m "chore(sync-vendors): update submodules + re-populate assets

Sync incremental automático dos vendors (forks conexaoazul/*) +
re-população da tabela assets do marketplace.

Co-Authored-By: Diego Santos <diego@conexaoazul.com>" 2>&1 | tail -3
    git push 2>&1 | tail -2 || echo "[warn] push falhou (rodar local? defina remote)"
  fi
fi

echo ""
echo "=== 4. Resumo ==="
git submodule status | wc -l | xargs echo "submodules:"
echo "assets DB: ${WORKFLOW_DB_PATH:-workflows.db}"
echo "Done."
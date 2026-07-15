#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WIPE_MINIBOOK=false

if [[ ${1:-} == "--wipe-minibook" ]]; then
  WIPE_MINIBOOK=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--wipe-minibook]" >&2
  exit 2
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

disable_worker_crons() {
  local provisioner="$ROOT/scripts/provision-worker.ps1"
  if [[ -f "$provisioner" ]]; then
    pwsh -NoProfile -File "$provisioner" -All -Disable
  fi
}

stop_workers() {
  if [[ ! -f "$ROOT/docker-compose.yml" ]]; then
    return
  fi
  local services
  services=$(docker compose -f "$ROOT/docker-compose.yml" config --services | grep -E '^worker' || true)
  if [[ -n "$services" ]]; then
    # Service names originate from Compose, not user input.
    docker compose -f "$ROOT/docker-compose.yml" stop $services
  fi
}

archive_run() {
  local timestamp archive
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  archive="$ROOT/runs/$timestamp"
  mkdir -p "$archive"

  if [[ -f "$ROOT/docker-compose.yml" ]] && \
     docker compose -f "$ROOT/docker-compose.yml" config --services | grep -qx mariadb; then
    docker compose -f "$ROOT/docker-compose.yml" exec -T mariadb sh -lc \
      'mariadb-dump -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE"' \
      > "$archive/ledger.sql"
  elif [[ -f "$ROOT/blockchain.json" ]]; then
    cp "$ROOT/blockchain.json" "$archive/blockchain.json"
  fi

  if [[ -d "$ROOT/workspaces" ]]; then
    mv "$ROOT/workspaces" "$archive/workspaces"
  fi
  mkdir -p "$ROOT/workspaces"
}

delete_n8n_workflows() {
  local base_url=${N8N_URL:-http://localhost:15678}
  if [[ -z ${N8N_API_KEY:-} ]]; then
    echo "N8N_API_KEY is unset; refusing to claim workflows were deleted." >&2
    return 1
  fi
  local response
  response=$(curl -fsS -H "X-N8N-API-KEY: $N8N_API_KEY" "$base_url/api/v1/workflows?limit=250")
  while IFS= read -r workflow_id; do
    [[ -z "$workflow_id" ]] && continue
    curl -fsS -X DELETE -H "X-N8N-API-KEY: $N8N_API_KEY" \
      "$base_url/api/v1/workflows/$workflow_id" >/dev/null
  done < <(python -c 'import json,sys; print("\n".join(str(x["id"]) for x in json.load(sys.stdin).get("data", [])))' <<< "$response")
}

wipe_mailpit() {
  local base_url=${MAILPIT_URL:-http://localhost:8025}
  curl -fsS -X DELETE "$base_url/api/v1/messages" >/dev/null
}

wipe_minibook_if_requested() {
  [[ "$WIPE_MINIBOOK" == true ]] || return
  local database="$ROOT/minibook/minibook.db"
  if [[ "$database" != "$ROOT/minibook/minibook.db" ]]; then
    echo "Unsafe Minibook path" >&2
    exit 1
  fi
  rm -f -- "$database"
}

disable_worker_crons
stop_workers
archive_run
delete_n8n_workflows
wipe_mailpit
wipe_minibook_if_requested

echo "Reset complete. Previous run archived under $ROOT/runs/."

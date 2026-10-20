#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python || ! -d frontend/node_modules ]]; then
  echo '먼저 bash scripts/setup.sh를 실행하세요.' >&2
  exit 1
fi
if [[ "${1:-}" == '--mock' ]]; then
  export LORE_PROVIDER=mock
  export LORE_DB_PATH="${LORE_DB_PATH:-runtime/mock.db}"
  echo '명시적 모의 시연입니다. 실제 AI 응답이나 품질 평가가 아닙니다.'
elif [[ -n "${1:-}" ]]; then
  echo '사용법: bash scripts/dev.sh [--mock]' >&2
  exit 1
fi
# Only check ports. Never kill an unrelated process or delete a database.
.venv/bin/python - <<'PY'
import socket
for port in (8000, 5173):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'{port} 포트가 사용 중입니다. 기존 서버를 확인하세요.')
PY
backend_pid=''
frontend_pid=''
cleanup() {
  trap - EXIT INT TERM
  [[ -z "$frontend_pid" ]] || kill "$frontend_pid" 2>/dev/null || true
  [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
backend_pid=$!
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort &
frontend_pid=$!
echo '설정공방: http://127.0.0.1:5173 (종료: Ctrl+C)'
while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do sleep 1; done
echo '서버 하나가 종료되었습니다. 위 오류를 확인하세요.' >&2
exit 1

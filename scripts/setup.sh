#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${LORE_PYTHON:-python3.11}"
command -v "$python_bin" >/dev/null || { echo 'Python 3.11+이 필요합니다. LORE_PYTHON에 실행 경로를 지정하세요.' >&2; exit 1; }
"$python_bin" -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]' --no-deps
npm --prefix frontend ci
echo '설치 완료. 실제 API는 .env.example을 .env로 복사하고 기존 키를 로컬에서 설정하세요.'
echo '실행: bash scripts/dev.sh | 모의 시연: bash scripts/dev.sh --mock'

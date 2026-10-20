#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
if [[ -e .git ]] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
  git diff --cached --check
else
  echo 'Git 메타데이터가 없는 배포본입니다. 코드·타입·빌드 검사는 완료했으며 Git diff 검사는 생략합니다.'
fi

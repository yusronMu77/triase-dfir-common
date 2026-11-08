#!/bin/sh
# Uji lokal satu layanan terhadap salinan kerja common dan contracts (tanpa uv.lock, tanpa tag git).
# Dijalankan di kontainer py devenv dari folder layanan:  sh ../common/template/localtest.sh
# CI dan image tetap memakai uv.lock dengan tag git; skrip ini hanya untuk mengembangkan common dan layanan bersamaan.
set -eu
venv="/tmp/astria-localtest-$(basename "$PWD")"
uv venv -q --allow-existing "$venv"
VIRTUAL_ENV="$venv" uv pip install -q -e ../contracts -e "../common[otel]" -e . --no-sources "pytest==9.2.0" "anyio>=4.11" "ruff==0.17.4"
"$venv/bin/ruff" format --check . && "$venv/bin/ruff" check . && "$venv/bin/pytest" -q -p no:cacheprovider "$@"

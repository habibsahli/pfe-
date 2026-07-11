#!/usr/bin/env bash
set -euo pipefail

echo "Starting Phoenix standalone..."
docker run -d \
  --name phoenix \
  -p 6006:6006 \
  -v phoenix-data:/phoenix \
  arizephoenix/phoenix:latest || true

echo "Phoenix UI : http://localhost:6006"

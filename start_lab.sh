#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

export LAB_PORT=8080
export LAB_ADMIN_PASSWORD="admin123"

echo "Starting Projects Laboratory Server on port $LAB_PORT..."
python3 server.py
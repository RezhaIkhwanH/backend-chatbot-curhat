#!/bin/sh
set -e
# Mengaktifkan virtual environment
. /app/.venv/bin/activate
# Menjalankan aplikasi
exec python main.py
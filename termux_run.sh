#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Termux Run Script for PredictX Backend
# ============================================================================

set -e

cd "$(dirname "$0")"

# Activate virtual environment
source .venv/bin/activate

# Ensure data directory exists
mkdir -p data

echo "=========================================="
echo "  Starting PredictX Backend..."
echo "=========================================="
echo ""
echo "  API Docs: http://127.0.0.1:8002/docs"
echo "  Health:   http://127.0.0.1:8002/health"
echo ""
echo "  Press Ctrl+C to stop"
echo "=========================================="
echo ""

# Run the backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8002

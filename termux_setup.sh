#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Termux Setup Script for PredictX Backend
# Run this ONCE to install all dependencies
# ============================================================================

set -e

echo "=========================================="
echo "  PredictX Backend - Termux Setup"
echo "=========================================="
echo ""

# 1. Update packages
echo "[1/5] Updating Termux packages..."
pkg update -y && pkg upgrade -y

# 2. Install system dependencies
echo "[2/5] Installing system dependencies..."
pkg install -y \
    python \
    python-pip \
    libjpeg-turbo \
    libpng \
    clang \
    libopenblas \
    libffi \
    openssl \
    sqlite \
    git

# 3. Upgrade pip
echo "[3/5] Upgrading pip..."
pip install --upgrade pip setuptools wheel

# 4. Create virtual environment
echo "[4/5] Creating Python virtual environment..."
cd "$(dirname "$0")"
python -m venv .venv

# 5. Install Python dependencies
echo "[5/5] Installing Python dependencies..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "To run the backend:"
echo "  source termux_run.sh"
echo ""
echo "Or manually:"
echo "  source .venv/bin/activate"
echo "  uvicorn app.main:app --reload --host 0.0.0.0 --port 8002"
echo ""

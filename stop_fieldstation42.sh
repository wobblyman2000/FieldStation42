#!/usr/bin/env bash
# ==============================================================================
# FieldStation42 Master Shutdown Script
# ==============================================================================

echo "🛑 Stopping FieldStation42 Suite..."

pkill -f "field_player.py" 2>/dev/null
pkill -f "FieldStation42 GUI/server.py" 2>/dev/null
pkill -f "fs42/osd/main.py" 2>/dev/null

echo "✅ FieldStation42 stopped."

#!/usr/bin/env bash
# ==============================================================================
# FieldStation42 Master Auto-Start & Launch Script
# ==============================================================================

FS42_ENGINE_DIR="/home/dave/FieldStation42"
FS42_GUI_DIR="/home/dave/cpp_projects/FieldStation42 GUI"
VENV_PYTHON="$FS42_ENGINE_DIR/env/bin/python3"

echo "================================================="
echo "📺 Starting FieldStation42 Suite..."
echo "================================================="

# 1. Terminate any previous dangling instances
pkill -f "field_player.py" 2>/dev/null
pkill -f "FieldStation42 GUI/server.py" 2>/dev/null
pkill -f "fs42/osd/main.py" 2>/dev/null

sleep 1

# 2. Start FieldStation42 GUI Server (Port 4240)
echo "🚀 Starting GUI Server on http://localhost:4240..."
cd "$FS42_GUI_DIR" || exit 1
"$VENV_PYTHON" server.py > /tmp/fs42_gui_server.log 2>&1 &

# Wait up to 10s for GUI server to be responsive
for i in {1..10}; do
    if curl -s http://localhost:4240/api/channels > /dev/null 2>&1; then
        echo "✅ GUI Server ready on http://localhost:4240"
        break
    fi
    sleep 1
done

# 3. Start FieldStation42 Playback Engine (field_player.py)
echo "📺 Starting FieldStation42 Playback Engine..."
cd "$FS42_ENGINE_DIR" || exit 1
nohup "$VENV_PYTHON" field_player.py > /tmp/fs42_field_player.log 2>&1 &

# 4. Start TV OSD Overlay (fs42/osd/main.py)
echo "✨ Starting TV On-Screen Display Overlay..."
nohup "$VENV_PYTHON" fs42/osd/main.py > /tmp/fs42_osd.log 2>&1 &

# 5. Open Web Interface in Default Browser
if [ "$1" != "--no-browser" ] && command -v xdg-open > /dev/null 2>&1; then
    echo "🌐 Opening http://localhost:4240 in web browser..."
    xdg-open "http://localhost:4240" >/dev/null 2>&1 &
fi

echo "================================================="
echo "🎉 FieldStation42 is fully operational!"
echo "================================================="

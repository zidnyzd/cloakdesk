#!/bin/bash
# CloakDesk Standalone Desktop App Launcher
set -e

# Pastikan PATH menyertakan lokasi biner lokal pengguna pada sesi GUI desktop
export PATH="$HOME/.local/bin:$HOME/.local/share/lerd/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export DISPLAY="${DISPLAY:-:0}"

# Cari direktori root CloakDesk yang valid
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CANDIDATE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$CANDIDATE_DIR/ui/electron-main.cjs" ] && [ -d "$CANDIDATE_DIR/antidetect" ]; then
    PROJECT_DIR="$CANDIDATE_DIR"
elif [ -f "$HOME/cloakdesk/ui/electron-main.cjs" ]; then
    PROJECT_DIR="$HOME/cloakdesk"
elif [ -f "/opt/cloakdesk/ui/electron-main.cjs" ]; then
    PROJECT_DIR="/opt/cloakdesk"
else
    PROJECT_DIR="$CANDIDATE_DIR"
fi

PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ] && [ -x "$HOME/cloakdesk/venv/bin/python" ]; then
    PYTHON_BIN="$HOME/cloakdesk/venv/bin/python"
fi

# 0. Jika dipanggil dengan argumen subperintah CLI (misal: cloakdesk sync, cloakdesk list)
if [ "$#" -gt 0 ]; then
    case "$1" in
        -*)
            ;;
        *)
            PY="/usr/bin/python3"
            [ -x "$PYTHON_BIN" ] && PY="$PYTHON_BIN"
            export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
            exec "$PY" -m antidetect "$@"
            ;;
    esac
fi

# 1. Pastikan backend CloakDesk aktif di port 53200
if ! curl -s -m 1 http://127.0.0.1:53200/health >/dev/null 2>&1; then
    PY="/usr/bin/python3"
    [ -x "$PYTHON_BIN" ] && PY="$PYTHON_BIN"
    export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
    nohup "$PY" -m antidetect serve >/dev/null 2>&1 &
    for _ in $(seq 1 25); do
        if curl -s -m 1 http://127.0.0.1:53200/health >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
fi

# 2. Prioritaskan biner ELF native Electron langsung (tanpa ketergantungan shebang node)
ELECTRON_BIN=""
if [ -x "$PROJECT_DIR/ui/node_modules/electron/dist/electron" ]; then
    ELECTRON_BIN="$PROJECT_DIR/ui/node_modules/electron/dist/electron"
elif [ -x "$HOME/cloakdesk/ui/node_modules/electron/dist/electron" ]; then
    ELECTRON_BIN="$HOME/cloakdesk/ui/node_modules/electron/dist/electron"
elif [ -x "/opt/cloakdesk/ui/node_modules/electron/dist/electron" ]; then
    ELECTRON_BIN="/opt/cloakdesk/ui/node_modules/electron/dist/electron"
elif [ -x "$PROJECT_DIR/ui/node_modules/.bin/electron" ]; then
    ELECTRON_BIN="$PROJECT_DIR/ui/node_modules/.bin/electron"
elif [ -x "$HOME/cloakdesk/ui/node_modules/.bin/electron" ]; then
    ELECTRON_BIN="$HOME/cloakdesk/ui/node_modules/.bin/electron"
elif command -v electron >/dev/null 2>&1; then
    ELECTRON_BIN="$(command -v electron)"
fi

MAIN_CJS="$PROJECT_DIR/ui/electron-main.cjs"
if [ ! -f "$MAIN_CJS" ] && [ -f "$HOME/cloakdesk/ui/electron-main.cjs" ]; then
    MAIN_CJS="$HOME/cloakdesk/ui/electron-main.cjs"
fi

export CHROME_DESKTOP="CloakDesk.desktop"
if [ -z "$BAMF_DESKTOP_FILE_HINT" ]; then
    if [ -f "/usr/share/applications/cloakdesk.desktop" ]; then
        export BAMF_DESKTOP_FILE_HINT="/usr/share/applications/cloakdesk.desktop"
    elif [ -f "$HOME/.local/share/applications/cloakdesk.desktop" ]; then
        export BAMF_DESKTOP_FILE_HINT="$HOME/.local/share/applications/cloakdesk.desktop"
    fi
fi

if [ -n "$ELECTRON_BIN" ] && [ -f "$MAIN_CJS" ]; then
    exec "$ELECTRON_BIN" "$MAIN_CJS" --class=CloakDesk --name=CloakDesk "$@"
fi

# Fallback ke browser jika Electron belum terpasang
URL="http://127.0.0.1:53200/#/profiles"
exec xdg-open "$URL"

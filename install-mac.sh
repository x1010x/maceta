#!/usr/bin/env bash
#
# Instalador de Maceta para macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/x1010x/maceta/main/install-mac.sh | bash
#
# Variables que puedes ajustar:
#   MACETA_DIR     dónde dejar el código          (por defecto ~/maceta)
#   MACETA_PYTHON  usar este Python y saltar conda (si ya tienes entorno)
#   MACETA_APP_DIR dónde crear Maceta.app          (por defecto ~/Applications)
#
# Deja el código en ~/maceta, un entorno conda con todas las dependencias, y
# un Maceta.app en ~/Applications que puedes abrir como cualquier programa.
# Si vuelves a lanzarlo, actualiza en vez de duplicar.

set -euo pipefail

MACETA_DIR="${MACETA_DIR:-$HOME/maceta}"
ENV_NAME="${MACETA_ENV:-maceta}"
APP_DIR="${MACETA_APP_DIR:-$HOME/Applications}"
REPO="${MACETA_REPO:-https://github.com/x1010x/maceta.git}"
DRY="${MACETA_DRY_RUN:-0}"

bold=$'\033[1m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; off=$'\033[0m'
step() { printf "\n%s▸ %s%s\n" "$bold" "$1" "$off"; }
ok()   { printf "  %s✓%s %s\n" "$green" "$off" "$1"; }
warn() { printf "  %s!%s %s\n" "$yellow" "$off" "$1"; }
die()  { printf "\n%s✗ %s%s\n" "$red" "$1" "$off" >&2; exit 1; }
run()  { if [ "$DRY" = "1" ]; then printf "  [dry-run] %s\n" "$*"; else "$@"; fi; }

# Con `curl … | bash` la entrada estándar es el propio script, así que
# para preguntar hay que hablar con el terminal directamente.
ask_yes_no() {
    local pregunta="$1" respuesta
    if [ -n "${MACETA_FFMPEG:-}" ]; then
        [ "$MACETA_FFMPEG" = "1" ] && return 0 || return 1
    fi
    if [ ! -r /dev/tty ]; then
        warn "Sin terminal interactivo: asumo que SÍ"
        return 0
    fi
    printf "  %s [S/n] " "$pregunta" > /dev/tty
    read -r respuesta < /dev/tty || respuesta=""
    case "$respuesta" in
        [nN]*) return 1 ;;
        *)     return 0 ;;
    esac
}

find_ffmpeg() {
    # Primero junto al Python que va a usar Maceta: es donde lo deja
    # conda y donde el programa lo busca, aunque no esté en el PATH.
    if [ -n "${PY:-}" ] && [ -x "$(dirname "$PY")/ffmpeg" ]; then
        echo "$(dirname "$PY")/ffmpeg"; return 0
    fi
    command -v ffmpeg 2>/dev/null && return 0
    return 1
}

[ "$(uname -s)" = "Darwin" ] || die "Esto es el instalador de macOS. En Linux usa el README."
ARCH="$(uname -m)"   # arm64 en Apple Silicon, x86_64 en Intel

printf "%s\n" "${bold}Maceta — instalador para macOS (${ARCH})${off}"

# ---------- 1. Herramientas de línea de comandos (git) ----------
step "Comprobando git"
if ! command -v git >/dev/null 2>&1; then
    warn "git no está. macOS lo instala con las Command Line Tools."
    run xcode-select --install || true
    die "Acepta la ventana que acaba de salir, espera a que termine, y vuelve a lanzar esto."
fi
ok "git $(git --version | awk '{print $3}')"

# ---------- 2. conda ----------
# Atajo: si ya tienes un Python con las dependencias puestas, pásalo y nos
# saltamos conda entero.  MACETA_PYTHON=/ruta/a/python ./install-mac.sh
if [ -n "${MACETA_PYTHON:-}" ]; then
    step "Usando el Python que me has indicado"
    [ -x "$MACETA_PYTHON" ] || die "$MACETA_PYTHON no existe o no es ejecutable"
    PY="$MACETA_PYTHON"
    ok "$("$PY" -V 2>&1) — me salto conda"
else

step "Buscando una instalación de conda"
CONDA_ROOT=""
for cand in "${CONDA_PREFIX_1:-}" "${CONDA_PREFIX:-}" "$HOME/miniforge3" \
            "$HOME/miniconda3" "$HOME/anaconda3" "/opt/homebrew/Caskroom/miniforge/base" \
            "/opt/homebrew/anaconda3" "/opt/miniconda3"; do
    [ -n "$cand" ] && [ -x "$cand/bin/conda" ] && { CONDA_ROOT="$cand"; break; }
done
if [ -z "$CONDA_ROOT" ] && command -v conda >/dev/null 2>&1; then
    CONDA_ROOT="$(dirname "$(dirname "$(command -v conda)")")"
fi

if [ -z "$CONDA_ROOT" ]; then
    warn "No hay conda. Instalando Miniforge (~400 MB) en ~/miniforge3"
    INSTALLER="$(mktemp -d)/miniforge.sh"
    URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-${ARCH}.sh"
    run curl -fsSL "$URL" -o "$INSTALLER"
    run bash "$INSTALLER" -b -p "$HOME/miniforge3"
    CONDA_ROOT="$HOME/miniforge3"
    ok "Miniforge instalado"
else
    ok "conda encontrado en $CONDA_ROOT"
fi
CONDA="$CONDA_ROOT/bin/conda"

# ---------- 3. Entorno con las dependencias ----------
step "Preparando el entorno '$ENV_NAME' (esto tarda unos minutos)"
if [ -x "$CONDA_ROOT/envs/$ENV_NAME/bin/python" ]; then
    ok "El entorno ya existe, actualizo dependencias"
else
    run "$CONDA" create -n "$ENV_NAME" python=3.11 -y
fi
run "$CONDA" install -n "$ENV_NAME" -c conda-forge -y \
    librosa mutagen matplotlib requests ffmpeg
PY="$CONDA_ROOT/envs/$ENV_NAME/bin/python"
[ "$DRY" = "1" ] || [ -x "$PY" ] || die "El entorno no se creó bien. Mira los mensajes de conda de arriba."
ok "Dependencias listas"
fi

# ---------- 4. El código ----------
step "Descargando Maceta en $MACETA_DIR"
if [ -d "$MACETA_DIR/.git" ]; then
    run git -C "$MACETA_DIR" pull --ff-only
    ok "Actualizado"
else
    run git clone "$REPO" "$MACETA_DIR"
    ok "Clonado"
fi

# ---------- 5. Maceta.app ----------
step "Creando Maceta.app en $APP_DIR"
APP="$APP_DIR/Maceta.app"
run mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

if [ "$DRY" != "1" ]; then
    # Icono: logo.png → .icns con las resoluciones que pide macOS
    if [ -f "$MACETA_DIR/logo.png" ]; then
        ICONSET="$(mktemp -d)/maceta.iconset"
        mkdir -p "$ICONSET"
        for s in 16 32 64 128 256 512; do
            sips -z $s $s "$MACETA_DIR/logo.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1
            sips -z $((s*2)) $((s*2)) "$MACETA_DIR/logo.png" \
                 --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
        done
        iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/maceta.icns" 2>/dev/null || true
    fi

    cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Maceta</string>
    <key>CFBundleDisplayName</key>     <string>Maceta</string>
    <key>CFBundleIdentifier</key>      <string>com.x1010x.maceta</string>
    <key>CFBundleExecutable</key>      <string>Maceta</string>
    <key>CFBundleIconFile</key>        <string>maceta.icns</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleShortVersionString</key> <string>1.0</string>
    <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

    cat > "$APP/Contents/MacOS/Maceta" <<LAUNCHER
#!/bin/bash
# Lanzador generado por install-mac.sh
mkdir -p "\$HOME/Library/Logs"
exec >> "\$HOME/Library/Logs/Maceta-launcher.log" 2>&1
echo "--- \$(date) lanzando Maceta ---"
exec "$PY" "$MACETA_DIR/maceta.py"
LAUNCHER
    chmod +x "$APP/Contents/MacOS/Maceta"
    touch "$APP"   # que el Finder refresque el icono
fi
ok "Maceta.app creado"

# ---------- ffmpeg ----------
step "Comprobando ffmpeg"
FFMPEG="$(find_ffmpeg || true)"
if [ -n "$FFMPEG" ]; then
    ok "ffmpeg en $FFMPEG"
    case "$FFMPEG" in
      */envs/*/bin/ffmpeg)
        printf "    Está dentro del entorno de Maceta, así que 'which ffmpeg'\n"
        printf "    no lo encuentra en una terminal normal. Es correcto:\n"
        printf "    Maceta lo busca junto a su propio Python.\n" ;;
    esac
else
    warn "No hay ffmpeg. Sin él no se pueden analizar MP3, ni convertir a"
    warn "320, ni usar el reproductor."
    if ask_yes_no "¿Lo instalo ahora?"; then
        if [ -n "${CONDA:-}" ] && [ -x "${CONDA:-}" ]; then
            run "$CONDA" install -n "$ENV_NAME" -c conda-forge -y ffmpeg
        elif command -v brew >/dev/null 2>&1; then
            run brew install ffmpeg
        else
            die "No tengo ni conda ni Homebrew para instalarlo. Instala
    Homebrew desde https://brew.sh y luego: brew install ffmpeg"
        fi
        FFMPEG="$(find_ffmpeg || true)"
        [ -n "$FFMPEG" ] && ok "ffmpeg en $FFMPEG" \
            || warn "Sigue sin aparecer; revisa los mensajes de arriba."
    else
        warn "Seguimos sin ffmpeg. Puedes instalarlo luego con:"
        warn "  brew install ffmpeg"
    fi
fi

# ---------- Final ----------
VER="$(grep -m1 '^VERSION' "$MACETA_DIR/maceta.py" 2>/dev/null | cut -d'"' -f2 || echo "?")"
printf "\n%s✓ Maceta %s instalada%s\n\n" "$green" "$VER" "$off"
printf "  Ábrela desde %s, o con:\n" "$APP_DIR"
printf "      open '%s'\n\n" "$APP"
printf "  ffmpeg: %s\n" "${FFMPEG:-NO INSTALADO}"
printf "  Para actualizar en el futuro, vuelve a lanzar este script.\n"
printf "  Si quieres la parte de Soulseek, sigue la sección 'Soulseek' del README.\n\n"

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
# $2 (opcional) = nombre de variable de entorno para responder sin
# preguntar, p. ej. MACETA_FFMPEG=0. MACETA_YES=1 responde que sí a todo.
ask_yes_no() {
    local pregunta="$1" var="${2:-}" respuesta valor
    if [ -n "$var" ]; then
        valor="$(eval printf %s "\"\${$var:-}\"")"
        if [ -n "$valor" ]; then
            [ "$valor" = "1" ] && return 0 || return 1
        fi
    fi
    if [ -n "${MACETA_YES:-}" ]; then
        return 0
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
step "Maceta"
ver_de() { grep -m1 "^VERSION" "$1/maceta.py" 2>/dev/null | cut -d\" -f2; }
if [ -d "$MACETA_DIR/.git" ]; then
    ANTES="$(ver_de "$MACETA_DIR")"
    run git -C "$MACETA_DIR" pull --ff-only
    AHORA="$(ver_de "$MACETA_DIR")"
    if [ "$ANTES" = "$AHORA" ]; then
        ok "Maceta $AHORA ya instalada y al día"
    else
        ok "Maceta actualizada: $ANTES → $AHORA"
    fi
elif [ -e "$MACETA_DIR" ]; then
    die "$MACETA_DIR existe y no es un clon de git. Muévelo o usa
    MACETA_DIR=/otra/ruta"
else
    run git clone "$REPO" "$MACETA_DIR"
    ok "Maceta $(ver_de "$MACETA_DIR") instalada"
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
    if ask_yes_no "¿Lo instalo ahora?" MACETA_FFMPEG; then
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

# ---------- fpcalc (Chromaprint, para identificar canciones) ----------
step "fpcalc (huella acústica, identifica canciones de verdad)"
FPCALC_DEST="$(dirname "$PY")/fpcalc"
if [ -x "$FPCALC_DEST" ] || command -v fpcalc >/dev/null 2>&1; then
    ok "fpcalc ya disponible"
elif [ "$DRY" = "1" ]; then
    ok "[dry-run] descargaría fpcalc"
else
    case "$ARCH" in arm64) FA="arm64" ;; *) FA="x86_64" ;; esac
    URL="https://github.com/acoustid/chromaprint/releases/download/v1.6.2/chromaprint-fpcalc-1.6.2-macos-${FA}.tar.gz"
    TMPF="$(mktemp -d)"
    if curl -fsSL "$URL" -o "$TMPF/fp.tgz" && tar xzf "$TMPF/fp.tgz" -C "$TMPF"; then
        BIN="$(find "$TMPF" -name fpcalc -type f | head -1)"
        if [ -n "$BIN" ]; then
            cp "$BIN" "$FPCALC_DEST"
            xattr -dr com.apple.quarantine "$FPCALC_DEST" 2>/dev/null || true
            chmod +x "$FPCALC_DEST"
            ok "fpcalc instalado"
        else
            warn "No encontré fpcalc en el paquete; Maceta usará el método antiguo."
        fi
    else
        warn "No pude bajar fpcalc; Maceta usará el método antiguo (funciona igual, menos preciso)."
    fi
fi

# ---------- slskd (opcional) ----------
SLSKD_DIR="${SLSKD_DIR:-$HOME/slskd}"
# IMPORTANTE: en macOS slskd busca su config en ~/Library/Application
# Support/slskd/, no en ~/.local/share (que es lo de Linux). Si se
# escribe en el sitio equivocado, slskd arranca con el yml de ejemplo y
# las credenciales salen inválidas.
SLSKD_YML="$HOME/Library/Application Support/slskd/slskd.yml"

find_slskd() {
    for c in "$SLSKD_DIR/slskd" "$HOME/slskd/slskd" "/usr/local/bin/slskd"; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    command -v slskd 2>/dev/null && return 0
    return 1
}

# "v0.26.2" y "0.26.2.0 (0.26.2.0+e472ecd7)" son la misma versión: se
# comparan solo los tres primeros números.
norm_ver() {
    printf %s "$1" | sed -e "s/^v//" -e "s/[^0-9.].*$//" | cut -d. -f1-3
}

slskd_latest() {
    curl -fsSL "https://api.github.com/repos/slskd/slskd/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1
}

step "Soulseek (slskd) — opcional"
SLSKD="$(find_slskd || true)"
ULTIMA="$(slskd_latest || true)"
if [ -n "$SLSKD" ]; then
    INSTALADA="$("$SLSKD" --version 2>/dev/null | head -1 | tr -d "\r")"
    ok "slskd ya instalado en $SLSKD${INSTALADA:+ (v$(norm_ver "$INSTALADA"))}"
    if [ -n "$ULTIMA" ] && [ -n "$INSTALADA" ] && \
       [ "$(norm_ver "$ULTIMA")" != "$(norm_ver "$INSTALADA")" ]; then
        warn "Hay una versión nueva: $ULTIMA"
        if ask_yes_no "¿Actualizo slskd?" MACETA_SLSKD; then SLSKD=""; fi
    fi
fi

if [ -z "$SLSKD" ]; then
    printf "  slskd es lo que conecta Maceta con Soulseek para buscar y\n"
    printf "  descargar. El análisis de tu música funciona sin él.\n"
    if ask_yes_no "¿Instalo slskd${ULTIMA:+ $ULTIMA}?" MACETA_SLSKD; then
        case "$ARCH" in
            arm64) SL_ARCH="osx-arm64" ;;
            *)     SL_ARCH="osx-x64" ;;
        esac
        URL="https://github.com/slskd/slskd/releases/latest/download/slskd-${ULTIMA#v}-${SL_ARCH}.zip"
        TMPZ="$(mktemp -d)/slskd.zip"
        run mkdir -p "$SLSKD_DIR"
        if run curl -fsSL "$URL" -o "$TMPZ"; then
            run unzip -oq "$TMPZ" -d "$SLSKD_DIR"
            run chmod +x "$SLSKD_DIR/slskd"
            # Sin esto, macOS lo bloquea por venir de internet
            run xattr -dr com.apple.quarantine "$SLSKD_DIR" 2>/dev/null || true
            SLSKD="$SLSKD_DIR/slskd"
            ok "slskd instalado en $SLSKD"
        else
            warn "No pude descargar $URL"
            warn "Bájalo a mano de https://github.com/slskd/slskd/releases"
        fi
    else
        warn "Sin slskd: la pestaña Soulseek no podrá buscar ni descargar."
    fi
fi

# ---------- configuración de slskd + enlace con Maceta ----------

# ¿Podemos hablar con un terminal? Con `curl | bash` la entrada estándar
# es el script, y en CI o cron no hay terminal ninguno.
have_tty() { [ -r /dev/tty ] && (printf "" > /dev/tty) 2>/dev/null; }

pedir() {   # pedir <mensaje> <variable_entorno> [oculto]
    local mensaje="$1" var="$2" oculto="${3:-}" valor
    valor="$(eval printf %s "\"\${$var:-}\"")"
    if [ -n "$valor" ]; then printf %s "$valor"; return 0; fi
    have_tty || return 1
    printf "  %s" "$mensaje" > /dev/tty
    if [ -n "$oculto" ]; then
        read -rs valor < /dev/tty; printf "\n" > /dev/tty
    else
        read -r valor < /dev/tty
    fi
    printf %s "$valor"
}

yml_campo() { sed -n "s/^[[:space:]]*$1:[[:space:]]*//p" "$SLSKD_YML" 2>/dev/null \
              | head -1 | tr -d "\"' "; }

escribir_credenciales() {   # usuario, contraseña -> reescribe solo esas dos líneas
    "$PY" - "$SLSKD_YML" "$1" "$2" <<'PYYML'
import re, shutil, sys
ruta, usuario, clave = sys.argv[1], sys.argv[2], sys.argv[3]
shutil.copy2(ruta, ruta + ".bak")
texto = open(ruta, encoding="utf-8").read()
for campo, valor in (("username", usuario), ("password", clave)):
    patron = re.compile(r"^(\s*%s:).*$" % campo, re.M)
    if patron.search(texto):
        texto = patron.sub(lambda m: "%s %s" % (m.group(1), valor), texto, count=1)
    else:
        texto = re.sub(r"^(soulseek:\s*)$",
                       lambda m: "%s\n  %s: %s" % (m.group(1), campo, valor),
                       texto, count=1, flags=re.M)
open(ruta, "w", encoding="utf-8").write(texto)
print("  \u2713 Credenciales actualizadas (copia previa en %s.bak)" % ruta)
PYYML
}

crear_config_slskd() {
    local usuario clave_sk compartida
    usuario="$(pedir "Usuario de Soulseek: " MACETA_SL_USER)" || return 1
    clave_sk="$(pedir "Contraseña (no se muestra): " MACETA_SL_PASS oculto)" || return 1
    # Carpeta propia, nunca la de descargas: esa la vacía el botón
    # "Borrar TODO" de Maceta y tiene ficheros a medio bajar.
    COMPARTIDA_DEF="$HOME/maceta-compartido"
    compartida="$(pedir "Carpeta a compartir [$COMPARTIDA_DEF]: " MACETA_SL_SHARE)" || true
    compartida="${compartida:-$COMPARTIDA_DEF}"
    mkdir -p "$compartida" "$(dirname "$SLSKD_YML")"
    if [ ! -f "$compartida/LEEME.txt" ]; then
        cat > "$compartida/LEEME.txt" <<LEEME
Esta es la carpeta que compartes en Soulseek.

Copia aquí (o enlaza con un alias) la música que quieras ofrecer a los
demás. Compartir importa: muchos usuarios rechazan o dejan en cola
indefinida a quien no comparte nada, y entonces tus descargas no
arrancan.

Una carpeta vacía cuenta casi como no compartir. Mete algo.
LEEME
    fi
    CLAVE="$(openssl rand -hex 24)"
    cat > "$SLSKD_YML" <<YML
soulseek:
  username: $usuario
  password: $clave_sk
web:
  authentication:
    api_keys:
      maceta:
        key: $CLAVE
        cidr: 127.0.0.1/32,::1/128
directories:
  downloads: $MACETA_DIR/downloads
  incomplete: $MACETA_DIR/downloads/.incomplete
shares:
  directories:
    - $compartida
YML
    mkdir -p "$MACETA_DIR/downloads"
    chmod 600 "$SLSKD_YML"
    ok "slskd.yml creado en $SLSKD_YML"
    local n
    n=$(find "$compartida" -type f ! -name "LEEME.txt" 2>/dev/null | wc -l | tr -d " ")
    printf "    Compartes: %s (%s ficheros)\n" "$compartida" "$n"
    if [ "$n" = "0" ]; then
        warn "Esa carpeta está vacía, que para los demás es casi como no"
        warn "compartir. Copia ahí algo de música: muchos peers dejan en"
        warn "cola indefinida a quien no comparte."
    fi
}

if [ -n "$SLSKD" ] && [ "$DRY" != "1" ]; then
    if [ -f "$SLSKD_YML" ]; then
        CLAVE="$(yml_campo key)"
        USUARIO_ACTUAL="$(yml_campo username)"
        ok "Config de slskd ya existente${USUARIO_ACTUAL:+ (usuario: $USUARIO_ACTUAL)}"
        [ -n "$CLAVE" ] || warn "No tiene api key; añádela a mano al slskd.yml"
        if ask_yes_no "¿Cambiar el usuario o la contraseña de Soulseek?" \
                      MACETA_SL_CHANGE; then
            NUEVO_USER="$(pedir "Usuario [$USUARIO_ACTUAL]: " MACETA_SL_USER)" || NUEVO_USER=""
            NUEVA_PASS="$(pedir "Contraseña nueva (no se muestra): " MACETA_SL_PASS oculto)" || NUEVA_PASS=""
            if [ -n "$NUEVA_PASS" ]; then
                escribir_credenciales "${NUEVO_USER:-$USUARIO_ACTUAL}" "$NUEVA_PASS"
                warn "Reinicia slskd para que coja las credenciales nuevas."
            else
                warn "Sin contraseña nueva: no toco el slskd.yml."
            fi
        fi
    elif ask_yes_no "¿Creo la configuración de slskd (usuario, contraseña y carpeta compartida)?" MACETA_SLSKD_CONF; then
        if ! crear_config_slskd; then
            warn "Sin terminal para preguntarte: me salto la configuración."
            warn "Relánzalo en un terminal, o pasa MACETA_SL_USER y MACETA_SL_PASS."
        fi
    fi

    if [ -n "${CLAVE:-}" ]; then
        "$PY" - "$CLAVE" "$SLSKD" <<PYCFG
import json, os, sys
clave, binario = sys.argv[1], sys.argv[2]
p = os.path.expanduser("~/.maceta_config.json")
cfg = {}
if os.path.exists(p):
    try:
        cfg = json.load(open(p))
    except Exception:
        cfg = {}
cfg.setdefault("slskd_url", "http://localhost:5030")
cfg["slskd_api_key"] = clave
cfg["slskd_binary"] = binario
json.dump(cfg, open(p, "w"), indent=2, ensure_ascii=False)
print("  \u2713 Maceta configurada para hablar con slskd")
PYCFG
    fi
fi

# ---------- Final ----------
VER="$(grep -m1 '^VERSION' "$MACETA_DIR/maceta.py" 2>/dev/null | cut -d'"' -f2 || echo "?")"
printf "\n%s✓ Maceta %s instalada%s\n\n" "$green" "$VER" "$off"
printf "  Ábrela desde %s, o con:\n" "$APP_DIR"
printf "      open '%s'\n\n" "$APP"
printf "  ffmpeg: %s\n" "${FFMPEG:-NO INSTALADO}"
printf "  slskd : %s\n" "${SLSKD:-no instalado (opcional)}"

if [ -n "${SLSKD:-}" ]; then
    printf "\n%s── Lo que tiene que haber en Maceta ──%s\n" "$bold" "$off"
    printf "  Preferencias → ⚙ Opciones… → pestaña «slskd»\n\n"
    printf "    URL de slskd : %s\n" "http://localhost:5030"
    printf "    API key      : %s\n" "${CLAVE:-(mírala en $SLSKD_YML)}"
    printf "    Binario slskd: %s\n" "$SLSKD"
    printf "\n  Y pulsa «Probar conexión»: debe responder «Conectado».\n"
    if [ -n "${CLAVE:-}" ] && [ "$DRY" != "1" ]; then
        printf "  %s(ya te lo he dejado puesto; esto es solo para comprobarlo)%s\n" \
               "$green" "$off"
    fi
    USUARIO_FIN="$(yml_campo username 2>/dev/null || true)"
    COMPARTIDA_FIN="$(sed -n "s/^[[:space:]]*-[[:space:]]*\(\/.*\)/\1/p" \
                      "$SLSKD_YML" 2>/dev/null | head -1)"
    [ -n "$USUARIO_FIN" ] && printf "\n  Tu usuario de Soulseek: %s\n" "$USUARIO_FIN"
    [ -n "$COMPARTIDA_FIN" ] && printf "  Compartes: %s\n" "$COMPARTIDA_FIN"
    printf "\n  Arranca slskd con:  %s\n" "$SLSKD"
    printf "  (o deja que lo arranque Maceta: ya sabe dónde está)\n"
fi
printf "  Para actualizar en el futuro, vuelve a lanzar este script.\n"
printf "  Si quieres la parte de Soulseek, sigue la sección 'Soulseek' del README.\n\n"

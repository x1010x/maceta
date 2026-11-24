"""Maceta - Analizador de calidad de audio.

Recorre una carpeta, lee metadatos, detecta BPM y estima la frecuencia de corte
del espectro para descubrir si un archivo declarado como 320 kbps en realidad
viene de una fuente de menor calidad recodificada.

Copyright (C) 2026 Maceta contributors

Este programa es software libre: puedes redistribuirlo y/o modificarlo bajo
los términos de la Licencia Pública General GNU publicada por la Free
Software Foundation, en su versión 3 o (a tu elección) cualquier versión
posterior. Se distribuye SIN NINGUNA GARANTÍA. Consulta el fichero LICENSE
o <https://www.gnu.org/licenses/> para más detalles.
"""

import csv
import difflib
import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

import numpy as np
import librosa
import requests
from mutagen import File as MutagenFile

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg,
    NavigationToolbar2Tk,
)


VERSION = "0.4.55"
DEFAULT_UPDATE_REPO = "x1010x/maceta"
DEFAULT_UPDATE_BRANCH = "main"

# ---------- Logging ----------
# Un fichero rotativo en ~/.maceta.log (2 MB × 3). Todo lo que Maceta hace
# se escribe ahí para poder diagnosticar cuelgues sin acceso a la app.
LOG_PATH = Path.home() / ".maceta.log"


def _setup_logging():
    log = logging.getLogger("maceta")
    if log.handlers:
        return log
    log.setLevel(
        os.environ.get("MACETA_LOG", "INFO").upper()
    )
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    if os.environ.get("MACETA_LOG_STDERR"):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


log = _setup_logging()
log.info("=== Maceta %s arrancando (pid=%s) ===", VERSION, os.getpid())

SUPPORTED_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}
CONFIG_PATH = Path.home() / ".maceta_config.json"
MACETA_ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = MACETA_ROOT / "downloads"
ARCHIVE_DIR = MACETA_ROOT / "archivo"
LOSSLESS_EXTS = {"flac", "wav"}
GOOD_MP3_MIN_BR = 320       # target de calidad "ideal" para la biblioteca
OPPORTUNISTIC_FLOOR = 192   # cuando no forzamos 320, mínimo aceptable
MAX_FILE_SIZE_MB = 150      # cortar downloads de 2 GB para una canción normal
FAKE_CUTOFF_FLOOR_HZ = 16000  # por debajo de este corte, el transcode canta
CUTOFF_THRESHOLD_DB = -60.0   # energía mínima sobre el pico para find_cutoff
ANALYSIS_MAX_SECONDS = 300.0  # cuánto audio se carga por canción
AUDIO_MATCH_THRESHOLD = 0.85  # songs_match: a partir de aquí, misma canción

# Corte típico del filtro low-pass de LAME (MP3) por bitrate, en Hz.
# Se usa como referencia para juzgar si el corte real cuadra con el declarado.
LAME_CUTOFFS = {
    320: 20500,
    256: 20000,
    224: 19500,
    192: 19000,
    160: 17500,
    128: 16000,
    112: 15500,
    96:  15000,
    64:  11000,
}


def get_metadata(path: str) -> dict:
    info = {
        "bitrate_kbps": None,
        "sample_rate": None,
        "duration_s": None,
        "channels": None,
        "codec": Path(path).suffix.lstrip(".").upper(),
    }
    try:
        mf = MutagenFile(path)
        if mf is not None and getattr(mf, "info", None) is not None:
            br = getattr(mf.info, "bitrate", 0) or 0
            info["bitrate_kbps"] = int(round(br / 1000)) if br else None
            info["sample_rate"] = getattr(mf.info, "sample_rate", None)
            info["duration_s"] = getattr(mf.info, "length", None)
            info["channels"] = getattr(mf.info, "channels", None)
    except Exception:
        pass
    return info


def compute_spectrum(y: np.ndarray, sr: int, n_fft: int = 8192):
    """Promedio de PSD por Welch simple (ventana Hann, solape 50%)."""
    hop = n_fft // 2
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    n_frames = 1 + max(0, (len(y) - n_fft) // hop)
    window = np.hanning(n_fft)
    psd = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    for i in range(n_frames):
        seg = y[i * hop : i * hop + n_fft] * window
        spec = np.fft.rfft(seg)
        psd += (spec.real ** 2 + spec.imag ** 2)
    if n_frames > 0:
        psd /= n_frames
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    psd_db = 10.0 * np.log10(psd + 1e-20)
    return freqs, psd_db


def find_cutoff(freqs: np.ndarray, psd_db: np.ndarray,
                threshold_db: float = None) -> float:
    """Devuelve la frecuencia más alta cuya energía supera pico + threshold_db."""
    if threshold_db is None:
        threshold_db = CUTOFF_THRESHOLD_DB
    peak = float(np.max(psd_db))
    mask = psd_db > (peak + threshold_db)
    if not np.any(mask):
        return 0.0
    return float(freqs[int(np.max(np.where(mask)))])


def judge(bitrate_kbps, cutoff_hz, codec):
    codec = (codec or "").upper()
    if codec in ("FLAC", "WAV"):
        return "LOSSLESS", ""
    if not cutoff_hz:
        return "?", "Sin corte espectral"
    # El veredicto lo decide SOLO el espectro: el bitrate declarado se
    # ignora. Un 192 kbps con corte a 19 kHz suena bien de verdad, y un
    # 320 kbps con corte a 15 kHz es un transcode por mucho que declare.
    #
    # Por debajo de ~16000 Hz (corte típico de un MP3 128 kbps) el
    # brickwall de un transcode real es inconfundible. Por encima, el
    # umbral de detección (find_cutoff a -60 dB) empieza a confundir
    # rolloffs naturales del máster con un corte de encoder — ahí
    # genera muchos falsos positivos, así que ya no lo marcamos FAKE.
    if cutoff_hz >= FAKE_CUTOFF_FLOOR_HZ:
        below = (f" — declara {bitrate_kbps} kbps"
                 if bitrate_kbps and bitrate_kbps < GOOD_MP3_MIN_BR else "")
        return "OK", f"Corte {int(cutoff_hz)} Hz{below}"
    suggested = None
    for br in sorted(LAME_CUTOFFS.keys()):
        if cutoff_hz <= LAME_CUTOFFS[br] + 500:
            suggested = br
            break
    hint = f" (~{suggested} kbps)" if suggested else ""
    declared = f", declara {bitrate_kbps}" if bitrate_kbps else ""
    return "FAKE", f"Corte {int(cutoff_hz)} Hz — calidad real{hint}{declared}"


META_FIELDS = ("codec", "bitrate_kbps", "sample_rate", "duration_s",
               "channels")


def scan_row(path: str, use_index: bool = True) -> dict:
    """Fila con solo metadatos, sin decodificar el audio.

    Leer los tags con mutagen cuesta ~5 ms por fichero: con 11.000
    canciones es un minuto de espera en cada arranque. Si el índice ya
    tiene los metadatos de esta canción (y el fichero no ha cambiado) se
    reutilizan — identificarla por hash cuesta 1 ms."""
    base = {
        "path": str(path), "name": Path(path).name,
        "bpm": None, "cutoff_hz": None, "verdict": "", "detail": "",
        "freqs": None, "psd_db": None, "sr_used": None,
        "analyzed": False, "slsk_status": "",
    }
    if use_index:
        sub_reason = index_substituted(path)
        if sub_reason:
            base["slsk_status"] = f"✓ sustituido ({sub_reason})"
        meta = index_meta(path)
        if meta:
            base.update({k: meta.get(k) for k in META_FIELDS})
            return base
    meta = get_metadata(path)
    base.update({k: meta[k] for k in META_FIELDS})
    index_put_meta(path, base)
    return base


def analyze_row(row: dict, max_seconds: float = None) -> dict:
    """Rellena BPM, corte y veredicto.

    Decodifica hasta `max_seconds` del centro del track: suficiente para BPM
    estable y evita colgarse en mixes/podcasts largos.
    """
    if max_seconds is None:
        max_seconds = ANALYSIS_MAX_SECONDS
    path = row["path"]
    try:
        dur = row.get("duration_s") or 0
        if dur > max_seconds:
            offset = max(0.0, (dur - max_seconds) / 2)
            y, sr = librosa.load(
                path, sr=None, mono=True,
                offset=offset, duration=max_seconds,
            )
        else:
            y, sr = librosa.load(path, sr=None, mono=True)
        if y.size == 0:
            row["verdict"] = "ERROR"
            row["detail"] = "Audio vacío"
            row["analyzed"] = True
            return row
        try:
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr, start_bpm=120.0)
            tempo_val = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
            row["bpm"] = round(tempo_val, 1)
        except Exception:
            pass
        freqs, psd_db = compute_spectrum(y, sr)
        cutoff = find_cutoff(freqs, psd_db)
        row["cutoff_hz"] = int(round(cutoff))
        row["freqs"], row["psd_db"], row["sr_used"] = freqs, psd_db, sr
        verdict, detail = judge(row["bitrate_kbps"], cutoff, row["codec"])
        row["verdict"] = verdict
        row["detail"] = detail
    except Exception as e:
        row["verdict"] = "ERROR"
        row["detail"] = str(e)[:160]
    row["analyzed"] = True
    return row


# ---------- Config persistente ----------

class Tooltip:
    """Globo de ayuda al pasar el ratón, como los de macOS.

    Lo usan los botones que solo llevan icono: el texto desaparece de la
    barra pero no la información."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text):
        self.text = text

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        try:
            self._tip.tk.call("::tk::unsupported::MacWindowStyle", "style",
                              self._tip._w, "help", "noActivates")
        except Exception:
            pass
        lbl = tk.Label(self._tip, text=self.text, justify="left",
                       background="#2b2b2b", foreground="#f0f0f0",
                       relief="solid", borderwidth=0, padx=8, pady=4,
                       font=("TkDefaultFont", 11))
        lbl.pack()
        self._tip.update_idletasks()
        ancho = self._tip.winfo_width()
        self._tip.wm_geometry(f"+{max(0, x - ancho // 2)}+{y}")

    def _hide(self, _e=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


def icon_button(parent, icon, tooltip, command, width=3, **pack_kw):
    """Botón solo-icono con su globo de ayuda."""
    b = ttk.Button(parent, text=icon, command=command, width=width)
    b.pack(**pack_kw)
    b._tip = Tooltip(b, tooltip)
    return b


def make_scrollable_tree(parent, **tree_kwargs):
    """Crea un Treeview con scrollbars horizontal + vertical dentro de un
    contenedor. Devuelve (container_frame, treeview)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, **tree_kwargs)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


def sort_treeview_by_column(tree, col, descending=False):
    """Ordena un Treeview por el valor de una columna. Numérico si puede."""
    def try_num(v):
        try:
            # aguantar cosas como "3.5 MB", "320 kbps", "20033 Hz"
            return float(v.split()[0].replace(",", "."))
        except (ValueError, IndexError, AttributeError):
            return None
    rows = []
    for iid in tree.get_children(""):
        if col == "#0":
            v = tree.item(iid, "text")
        else:
            v = tree.set(iid, col)
        num = try_num(v)
        rows.append(((0, num) if num is not None else (1, (v or "").lower()), iid))
    rows.sort(reverse=descending)
    for i, (_, iid) in enumerate(rows):
        tree.move(iid, "", i)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# Estado de UI que se persiste entre sesiones. Se guarda dentro de la
# misma config (sección `user_state`) para no ensuciar la raíz.
USER_STATE_KEY = "user_state"
USER_STATE_DEFAULTS = {
    "last_folder":               "",
    "ask_quality":                False,
    "substitute":                 False,
    "auto_flac_to_mp3":           False,
    "force_320":                  False,
    "active_bucket":              "todas",
    "filter_pending":             False,
    "auto_retry":                 False,
    "auto_retry_interval_min":    5,   # minutos entre reintentos
    "auto_retry_max_attempts":    10,  # 0 = infinito
    # --- Filtros de búsqueda en Soulseek (0 = sin límite) ---
    "search_min_mb":              0.0,
    "search_max_mb":              150.0,
    "search_min_sec":             0,
    "search_max_sec":             0,
    "search_only_available":      False,
    "search_max_queue":           0,
    "search_formats":             "mp3,flac,wav,m4a",
    # --- Query y presentación de resultados ---
    "query_mode":                 "clean",  # raw | clean | tags
    "results_sort":               "score",  # score|br|size|speed|queue
    "results_show_rejected":      True,
    "results_limit":              300,
    # {clave_tabla: {columna: ancho}} — anchos que el usuario deja a mano
    "column_widths":              {},
    # --- Disposición de la ventana, para dejarla como la dejaste ---
    "window_geometry":            "",      # "1080x600+45+38"
    "active_tab":                 0,
    "sash":                       {},      # {panel: [posiciones]}
    "recent_folders":             [],
    "restore_last_folder":        True,
    "use_index":                  True,
    # --- Peers que nos tumban las descargas ---
    "banned_peers":               [],     # usuarios que ya no se usan
    "peer_strikes":               {},     # {usuario: [timestamps]}
    "ban_strikes":                3,      # cancelaciones para banear
    "ban_window_min":             60,     # ventana en minutos
    "smart_search":               True,   # reintenta con menos palabras
    "show_all_downloads":         False,  # incluir búsquedas sin descarga
    "font_sizes":                 {},     # {tabla: tamaño de letra}
    "compact_cols":               True,   # Formato+Bitrate+SR en una celda
    "font_family":                "",     # "" = la del sistema
    "ui_font_size":               0,      # 0 = la del sistema
    # --- Umbrales que antes vivían cableados en el código ---
    "target_bitrate":             320,     # GOOD_MP3_MIN_BR
    "opportunistic_floor":        192,     # OPPORTUNISTIC_FLOOR
    "max_file_size_mb":           150,     # MAX_FILE_SIZE_MB
    "fake_cutoff_floor_hz":       16000,
    "cutoff_threshold_db":        -60.0,
    "analysis_max_seconds":       300,
    "exact_match_ratio":          0.95,
    "plausible_name_ratio":       0.70,
    "audio_match_threshold":      0.85,
    "max_auto_requery":           3,
    "auto_substitute":            True,
}

QUERY_MODES = [
    ("raw",   "Nombre de fichero tal cual"),
    ("clean", "Nombre de fichero sin símbolos"),
    ("tags",  "Artista + título (tags ID3)"),
]

RESULT_SORTS = [
    ("score", "Puntuación (calidad + disponibilidad)"),
    ("br",    "Bitrate"),
    ("size",  "Tamaño"),
    ("speed", "Velocidad del peer"),
    ("queue", "Cola más corta"),
]

KNOWN_FORMATS = ["mp3", "flac", "wav", "m4a"]


def load_user_state() -> dict:
    cfg = load_config()
    stored = cfg.get(USER_STATE_KEY) or {}
    return {k: stored.get(k, default) for k, default in USER_STATE_DEFAULTS.items()}


def save_user_state(state: dict) -> None:
    cfg = load_config()
    cfg[USER_STATE_KEY] = {**(cfg.get(USER_STATE_KEY) or {}), **state}
    save_config(cfg)


# ---------- Auto-update ----------

_VERSION_RE = re.compile(r'^VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', re.M)


def _parse_version(text: str) -> str:
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else ""


def _version_tuple(v: str) -> tuple:
    """Convierte '0.4.10' → (0, 4, 10). Componentes no numéricos pasan a 0."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_update(token: str,
                     repo: str = DEFAULT_UPDATE_REPO,
                     branch: str = DEFAULT_UPDATE_BRANCH,
                     timeout: float = 15.0) -> dict:
    """Consulta la última maceta.py del repo. Devuelve dict con:
        remote_version: str, current_version: str, is_newer: bool,
        content: str (solo si hay update).

    Levanta excepciones si el token es inválido o hay problema de red.
    """
    url = f"https://api.github.com/repos/{repo}/contents/maceta.py?ref={branch}"
    r = requests.get(url, timeout=timeout, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    r.raise_for_status()
    content = r.text
    remote_v = _parse_version(content)
    if not remote_v:
        raise RuntimeError("No pude parsear VERSION del maceta.py remoto")
    is_newer = _version_tuple(remote_v) > _version_tuple(VERSION)
    return {
        "remote_version": remote_v,
        "current_version": VERSION,
        "is_newer": is_newer,
        "content": content if is_newer else "",
    }


def apply_update(content: str, target: Path = None) -> Path:
    """Aplica una actualización: valida sintaxis, backupea el actual a
    archivo/ y sobreescribe con escritura atómica. Devuelve el path al
    backup para poder deshacer.

    NO reinicia. El caller decide cuándo hacer os.execv.
    """
    import ast as _ast
    _ast.parse(content)  # peta antes de tocar nada si el remoto está roto

    if target is None:
        target = Path(__file__).resolve()
    target = Path(target).resolve()

    # Backup a archivo/maceta_<version>_<timestamp>.py
    backup_dir = ARCHIVE_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"maceta_{VERSION}_{int(time.time())}.py"
    shutil.copy2(target, backup)

    # Escritura atómica: tmp en el mismo directorio → os.replace
    tmp = target.with_suffix(".py.new")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return backup


def restart_self():
    """Re-lanza el proceso actual con el mismo intérprete y argumentos.
    No retorna."""
    os.execv(sys.executable, [sys.executable] + sys.argv)


_CHANGELOG_HEADING_RE = re.compile(r"^## (\S+)", re.M)


def read_changelog_entries(path: Path, since_version: str = "",
                           up_to_version: str = "") -> str:
    """Devuelve las entradas del CHANGELOG.md entre `since_version`
    (exclusive) y `up_to_version` (inclusive). Si `since_version` está
    vacío o no encaja, devuelve la entrada de `up_to_version` sola.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    # Partir en secciones por `## <version>`
    parts = []
    matches = list(_CHANGELOG_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        ver = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts.append((ver, text[start:end].rstrip()))
    if not parts:
        return ""

    since_t = _version_tuple(since_version) if since_version else None
    up_t = _version_tuple(up_to_version) if up_to_version else None

    picked = []
    for ver, block in parts:
        vt = _version_tuple(ver)
        if up_t is not None and vt > up_t:
            continue
        if since_t is not None and vt <= since_t:
            continue
        picked.append(block)
    if not picked and up_to_version:
        for ver, block in parts:
            if ver == up_to_version:
                return block
    return "\n\n".join(picked)


# ---------- Auto-launch de helpers (slskd, SoulseekQt) ----------

SLSKD_LOG = Path.home() / ".maceta-slskd.log"


def _spawn_detached(cmd, cwd=None, log_path=None):
    """Arranca un proceso desligado; sobrevive a la salida de Maceta.

    Con `log_path` su salida se guarda en ese fichero en vez de tirarse:
    cuando slskd no arranca, su primera línea de error es justo lo que
    hace falta para saber por qué."""
    salida = subprocess.DEVNULL
    if log_path:
        try:
            salida = open(log_path, "wb")
        except OSError:
            salida = subprocess.DEVNULL
    kw = dict(
        stdout=salida,
        stderr=subprocess.STDOUT if salida is not subprocess.DEVNULL
        else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if cwd:
        kw["cwd"] = cwd
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
        return True
    except Exception:
        return False


# ---------- Sustitución y archivado ----------

# Maceta NUNCA borra de verdad: todo lo que "elimina" va a la papelera
# del sistema (recuperable). Si send2trash no está, se mueve a una
# papelera propia dentro de la carpeta de Maceta, nunca a /dev/null.
MACETA_TRASH = MACETA_ROOT / "papelera"


def to_trash(path) -> bool:
    """Manda un fichero o carpeta a la papelera. Devuelve True si lo
    consiguió. No usa os.remove/unlink jamás: lo peor que pasa es que se
    quede donde está, nunca que se pierda."""
    p = Path(path)
    if not p.exists():
        return True
    try:
        from send2trash import send2trash as _s2t
        _s2t(str(p))
        return True
    except Exception as e:
        log.info("send2trash no disponible (%s); uso papelera propia", e)
    # Red de seguridad: mover a ~/maceta/papelera conservando el nombre.
    try:
        MACETA_TRASH.mkdir(parents=True, exist_ok=True)
        destino = MACETA_TRASH / p.name
        if destino.exists():
            destino = MACETA_TRASH / (
                p.stem + "." + str(int(time.time())) + p.suffix)
        shutil.move(str(p), str(destino))
        return True
    except Exception as e:
        log.warning("no se pudo enviar a la papelera %s: %s", p, e)
        return False


def archive_path_for(original_path: str) -> Path:
    """Ruta espejo dentro de ARCHIVE_DIR conservando la ruta absoluta original.

    Ejemplo: /Users/x/Music/A/song.mp3 -> ARCHIVE_DIR/Users/x/Music/A/song.mp3
    Así 'restaurar' es tan simple como copiar de vuelta.
    """
    p = Path(original_path).resolve()
    stripped = str(p).lstrip("/")
    if sys.platform == "win32":
        # Windows: C:\a\b -> archivo\C\a\b
        drive, tail = os.path.splitdrive(str(p))
        stripped = drive.rstrip(":") + tail.lstrip("\\").lstrip("/")
    return ARCHIVE_DIR / stripped


_TS_RE = re.compile(r"\.\d{10}(\.[a-z0-9]+)$", re.I)


def _norm_stem(name: str) -> str:
    stem = Path(name).stem.lower()
    stem = re.sub(r"\.\d{10}$", "", stem)
    stem = re.sub(r"[^a-z0-9]+", " ", stem)
    return " ".join(w for w in stem.split() if len(w) > 2)


def _good_quality(path: str) -> bool:
    """Lossless, o MP3/M4A a 320 kbps o más."""
    ext = Path(path).suffix.lower()
    if ext in (".flac", ".wav", ".aiff"):
        return True
    try:
        mf = MutagenFile(path)
        br = getattr(getattr(mf, "info", None), "bitrate", 0) or 0
        return br / 1000.0 >= GOOD_MP3_MIN_BR - 1
    except Exception:
        return False


def plan_clean_archive() -> dict:
    """Qué haría una limpieza de archivo/: devuelve las que volverían a
    su sitio y por qué se saltan las demás. No toca nada.

    Restaura solo las de buena calidad (320+/lossless) que no estén ya
    en la biblioteca; deja copias con timestamp, baja calidad y las que
    ya tienen su sitio ocupado."""
    if not ARCHIVE_DIR.exists():
        return {"restore": [], "dup": 0, "mala": 0, "ts": 0, "ocupado": 0}
    archivados = [p for p in ARCHIVE_DIR.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
    if not archivados:
        return {"restore": [], "dup": 0, "mala": 0, "ts": 0, "ocupado": 0}

    def destino(p):
        rel = p.relative_to(ARCHIVE_DIR)
        return Path("/" + str(rel))

    destinos = [destino(p) for p in archivados]
    raiz = Path(os.path.commonpath([str(d) for d in destinos]))
    presentes = set()
    for p in raiz.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            presentes.add(_norm_stem(p.name))

    restore, dup, mala, ts, ocupado = [], 0, 0, 0, 0
    for p in archivados:
        if _TS_RE.search(p.name):
            ts += 1; continue
        if _norm_stem(p.name) in presentes:
            dup += 1; continue
        if not _good_quality(str(p)):
            mala += 1; continue
        d = destino(p)
        if d.exists():
            ocupado += 1; continue
        restore.append((p, d))
        presentes.add(_norm_stem(p.name))
    return {"restore": restore, "dup": dup, "mala": mala,
            "ts": ts, "ocupado": ocupado, "raiz": str(raiz)}


def run_clean_archive(plan: dict) -> int:
    """Mueve de verdad las del plan. Devuelve cuántas restauró."""
    n = 0
    for src, dest in plan.get("restore", []):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            n += 1
        except Exception as e:
            log.warning("restaurar %s falló: %s", src.name, e)
    return n


def substitute_file(original_path: str, downloaded_path: str) -> Path:
    """Mueve el original a archivo/ y coloca el descargado en su sitio
    usando el mismo stem del original + la extensión del descargado.

    Devuelve la ruta final del sustituto.
    """
    orig = Path(original_path)
    dl = Path(downloaded_path)
    if not orig.exists():
        raise FileNotFoundError(f"Original no existe: {orig}")
    if not dl.exists():
        raise FileNotFoundError(f"Descarga no existe: {dl}")

    arch = archive_path_for(str(orig))
    arch.parent.mkdir(parents=True, exist_ok=True)
    if arch.exists():
        # Ya hay archivo previo — añade sufijo con timestamp para no perder nada
        arch = arch.with_name(
            arch.stem + "." + str(int(time.time())) + arch.suffix
        )
    shutil.move(str(orig), str(arch))

    target = orig.with_suffix(dl.suffix)
    if target.exists():
        target = target.with_name(target.stem + ".new" + target.suffix)
    shutil.move(str(dl), str(target))
    return target


DOWNLOAD_LINKS_PATH = DOWNLOADS_DIR / ".maceta_links.json"


HASH_CHUNK = 128 * 1024       # cabecera y cola que entran en el md5
_hash_cache = {}              # (ruta, tamaño, mtime) -> md5


def audio_hash(path: str) -> str:
    """md5 del contenido: tamaño + primeros y últimos 128 KB.

    Identifica la canción **esté donde esté**, así que mover o renombrar
    un fichero no pierde su análisis. No se hashea entero a propósito:
    con 4500 canciones serían decenas de GB de lectura por escaneo, y
    con el tamaño más los dos extremos la probabilidad de confundir dos
    canciones distintas es despreciable."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    clave = (str(path), st.st_size, round(st.st_mtime, 3))
    hit = _hash_cache.get(clave)
    if hit:
        return hit
    h = hashlib.md5()
    h.update(str(st.st_size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(HASH_CHUNK))
            if st.st_size > 2 * HASH_CHUNK:
                f.seek(-HASH_CHUNK, os.SEEK_END)
                h.update(f.read(HASH_CHUNK))
    except OSError:
        return ""
    digest = h.hexdigest()
    if len(_hash_cache) > 20000:
        _hash_cache.clear()
    _hash_cache[clave] = digest
    return digest


INDEX_PATH = Path.home() / ".maceta_index.json"
_index_cache = None
_index_lock = threading.Lock()
_index_dirty = False


PATHS_KEY = "__paths__"       # {ruta: hash} para no rehashear lo conocido


def _paths_map() -> dict:
    idx = load_index()
    m = idx.get(PATHS_KEY)
    if not isinstance(m, dict):
        m = {}
        idx[PATHS_KEY] = m
    return m


def index_entry(path: str, want_hash: bool = True) -> tuple:
    """(hash, entrada) de esta canción.

    Primero por ruta: si la ruta es conocida y el fichero no ha cambiado
    de tamaño ni fecha, nos ahorramos leer 256 KB para hashear — con
    11.000 canciones eso es la diferencia entre 16 s y 2 s de arranque.
    Si la ruta no está (fichero movido o nuevo) se recurre al hash, que
    es lo que permite reconocerla aunque la hayan cambiado de sitio."""
    idx = load_index()
    size, mtime = _file_fingerprint(path)
    digest = _paths_map().get(str(path))
    if digest:
        e = idx.get(digest)
        if e and e.get("size") == size and e.get("mtime") == mtime:
            return digest, e
    if not want_hash:
        return "", {}
    digest = audio_hash(path)
    if not digest:
        return "", {}
    e = idx.get(digest)
    if e and e.get("size") == size and e.get("mtime") == mtime:
        _paths_map()[str(path)] = digest
        return digest, e
    return digest, {}


def load_index() -> dict:
    """Índice de análisis ya hechos: {ruta: medidas}. Se lee una vez."""
    global _index_cache
    if _index_cache is None:
        try:
            _index_cache = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            _index_cache = {}
    return _index_cache


def save_index(force: bool = False) -> None:
    """Vuelca el índice a disco. Con 4000 canciones esto es ~1 MB, así
    que se escribe al terminar una tanda, no por cada fichero."""
    global _index_dirty
    with _index_lock:
        if not _index_dirty and not force:
            return
        idx = _index_cache or {}
        try:
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx), encoding="utf-8")
            tmp.replace(INDEX_PATH)
            _index_dirty = False
        except Exception as e:
            log.warning("no se pudo guardar el índice: %s", e)


def _file_fingerprint(path: str) -> tuple:
    """Tamaño y mtime: si cambia cualquiera, el análisis guardado no vale."""
    try:
        st = os.stat(path)
        return st.st_size, round(st.st_mtime, 3)
    except OSError:
        return (0, 0.0)


def _params_ok(e: dict) -> bool:
    return (e.get("cut_db") == CUTOFF_THRESHOLD_DB
            and e.get("secs") == ANALYSIS_MAX_SECONDS)


def index_get(path: str) -> dict:
    """Medidas guardadas para esta canción, o {} si no hay o no valen.

    La clave es el hash del contenido, así que encuentra el análisis
    aunque el fichero se haya movido o renombrado. Las entradas viejas
    (indexadas por ruta) se migran al vuelo la primera vez que se tocan,
    para no perder lo ya analizado al actualizar."""
    idx = load_index()
    digest, e = index_entry(path)
    if e:
        return e if _params_ok(e) else {}

    vieja = idx.get(str(path))
    if not vieja:
        return {}
    size, mtime = _file_fingerprint(path)
    if vieja.get("size") != size or vieja.get("mtime") != mtime:
        return {}
    if digest:                      # migrar de ruta a hash
        global _index_dirty
        with _index_lock:
            vieja = dict(vieja)
            vieja["path"] = str(path)
            idx[digest] = vieja
            idx.pop(str(path), None)
            _index_dirty = True
    return vieja if _params_ok(vieja) else {}


def index_put(row: dict) -> None:
    """Guarda lo caro del análisis. El veredicto NO se guarda: se
    recalcula con judge() al cargar, así cambiar los umbrales en
    Opciones reclasifica la biblioteca sin volver a analizar nada."""
    global _index_dirty
    path = row.get("path")
    if not path or row.get("verdict") == "ERROR":
        return
    size, mtime = _file_fingerprint(path)
    digest, _e = index_entry(path)
    if not digest:
        return
    with _index_lock:
        _paths_map()[str(path)] = digest
        idx = load_index()
        idx.pop(str(path), None)        # por si venía de la época de rutas
        e = idx.get(digest) or {}
        # Se conserva `meta`: los metadatos y el análisis viven en la
        # misma entrada y guardar uno no debe tirar el otro.
        e.update({
            "size": size, "mtime": mtime, "path": str(path),
            "bpm": row.get("bpm"), "cutoff_hz": row.get("cutoff_hz"),
            "cut_db": CUTOFF_THRESHOLD_DB, "secs": ANALYSIS_MAX_SECONDS,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        if not e.get("meta"):
            e["meta"] = {k: row.get(k) for k in META_FIELDS}
        idx[digest] = e
        _index_dirty = True


def index_meta(path: str) -> dict:
    """Metadatos guardados de esta canción, o {} si no valen.

    No mira los parámetros de análisis: el codec y la duración no
    dependen de con qué umbrales se analizó."""
    _digest, e = index_entry(path)
    meta = (e or {}).get("meta") or {}
    return meta if meta.get("codec") else {}


def index_put_meta(path: str, row: dict) -> None:
    """Guarda los metadatos (no el análisis) de una canción recién leída."""
    global _index_dirty
    digest, e = index_entry(path)
    if not digest:
        return
    size, mtime = _file_fingerprint(path)
    with _index_lock:
        idx = load_index()
        if not e:
            e = {"size": size, "mtime": mtime}
            idx[digest] = e
        e["meta"] = {k: row.get(k) for k in META_FIELDS}
        e["path"] = str(path)
        _paths_map()[str(path)] = digest
        _index_dirty = True


def index_mark_substituted(path: str, reason: str) -> None:
    """Deja constancia en el índice de que esta canción salió de una
    sustitución, para que siga apareciendo en «Sustituidas» aunque se
    reinicie Maceta. El `slsk_status` vive solo en memoria; esto es su
    copia persistente, por hash de contenido."""
    global _index_dirty
    digest, e = index_entry(path)
    if not digest:
        return
    size, mtime = _file_fingerprint(path)
    with _index_lock:
        idx = load_index()
        if not e:
            e = {"size": size, "mtime": mtime, "path": str(path)}
            idx[digest] = e
        e["sub"] = reason or "sustituido"
        e["path"] = str(path)
        _paths_map()[str(path)] = digest
        _index_dirty = True


def index_substituted(path: str) -> str:
    """Motivo guardado de sustitución, o "" si esta canción no viene de
    una. Se valida por hash, así que solo cuenta si el fichero es el que
    se puso al sustituir."""
    _digest, e = index_entry(path)
    return (e or {}).get("sub", "") or ""


def hydrate_from_index(row: dict) -> bool:
    """Rellena la fila desde el índice. True si se ha podido."""
    e = index_get(row.get("path", ""))
    if not e:
        return False
    row["bpm"] = e.get("bpm")
    row["cutoff_hz"] = e.get("cutoff_hz")
    row["verdict"], row["detail"] = judge(
        row.get("bitrate_kbps"), row.get("cutoff_hz"), row.get("codec"))
    row["analyzed"] = True
    row["from_index"] = True
    return True


def clear_index() -> int:
    """Vacía el índice. Devuelve cuántas entradas había."""
    global _index_cache, _index_dirty
    n = len(load_index())
    with _index_lock:
        _index_cache = {}
        _index_dirty = True
    save_index(force=True)
    return n


def load_download_links() -> dict:
    """Devuelve el registro `basename_lowercase → link_info`.

    link_info = {"original_path": str, "original_name": str,
                 "original_folder": str, "search_query": str,
                 "linked_at": iso8601}
    """
    if not DOWNLOAD_LINKS_PATH.exists():
        return {}
    try:
        return json.loads(DOWNLOAD_LINKS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_download_links(links: dict) -> None:
    """Guarda el registro. Crea DOWNLOADS_DIR si no existe."""
    try:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_LINKS_PATH.write_text(
            json.dumps(links, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def add_download_link(remote_filename: str, original_path: str,
                      search_query: str = "") -> None:
    """Registra que `remote_filename` (basename tras la barra) vendrá a
    reemplazar `original_path`. Persiste inmediatamente."""
    base = (remote_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not base:
        return
    orig = Path(original_path)
    links = load_download_links()
    links[base.lower()] = {
        "original_path":   str(orig),
        "original_name":   orig.name,
        "original_folder": str(orig.parent),
        "search_query":    search_query,
        "linked_at":       time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_download_links(links)


def get_download_link(local_filename: str) -> dict:
    """Devuelve el link_info para un fichero local (busca por basename
    case-insensitive)."""
    base = Path(local_filename).name.lower()
    return load_download_links().get(base) or {}


def remove_download_link(local_filename: str) -> None:
    base = Path(local_filename).name.lower()
    links = load_download_links()
    if base in links:
        links.pop(base, None)
        save_download_links(links)


def index_stats() -> tuple:
    """(entradas, cuántas siguen siendo del formato viejo por ruta)."""
    idx = load_index()
    viejas = sum(1 for k in idx if k != PATHS_KEY and os.sep in k)
    return len(idx) - (1 if PATHS_KEY in idx else 0), viejas


def prune_download_links() -> None:
    """Quita del índice los links cuyo fichero descargado ya no existe."""
    if not DOWNLOADS_DIR.exists():
        return
    have = {p.name.lower() for p in DOWNLOADS_DIR.rglob("*") if p.is_file()}
    links = load_download_links()
    alive = {k: v for k, v in links.items() if k in have}
    if len(alive) != len(links):
        save_download_links(alive)


_FPCALC = None
_fp_cache = {}      # (ruta, size, mtime) -> lista de uint32
# Umbral de similitud de Chromaprint: misma grabación ~0.98, distintas
# ~0.5. 0.90 deja un margen enorme y descarta falsos positivos (el
# problema que dejaba duplicados con el cromagrama en temas que suenan
# parecido, como el psytrance).
FP_MATCH_THRESHOLD = 0.90


def _find_fpcalc():
    """fpcalc: primero junto al Python de Maceta (donde lo pone conda o
    el instalador), luego en el PATH. None si no está."""
    global _FPCALC
    if _FPCALC is not None:
        return _FPCALC or None
    cand = Path(sys.executable).parent / "fpcalc"
    if cand.exists():
        _FPCALC = str(cand)
    else:
        _FPCALC = shutil.which("fpcalc") or ""
    return _FPCALC or None


def audio_fingerprint(path: str, length: int = 90):
    """Huella acústica de Chromaprint: lista de enteros de 32 bits.

    Identifica la grabación aunque cambie el bitrate o el recorte. Se
    cachea por fichero. [] si fpcalc no está o el audio no se lee."""
    ff = _find_fpcalc()
    if not ff:
        return []
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (str(path), st.st_size, round(st.st_mtime, 3))
    hit = _fp_cache.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            [ff, "-raw", "-length", str(length), path],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    fp = []
    for ln in out.splitlines():
        if ln.startswith("FINGERPRINT="):
            fp = [int(x) for x in ln[12:].split(",") if x]
            break
    if len(_fp_cache) > 5000:
        _fp_cache.clear()
    _fp_cache[key] = fp
    return fp


def fingerprint_similarity(fp1, fp2, max_offset: int = 60) -> float:
    """Fracción de bits que coinciden en la mejor alineación (prueba
    varios desfases para tolerar recortes distintos). 1 = idéntico,
    ~0.5 = azar."""
    if not fp1 or not fp2:
        return 0.0
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        a0, b0 = (off, 0) if off >= 0 else (0, -off)
        n = min(len(fp1) - a0, len(fp2) - b0)
        if n < 10:
            continue
        same = 0
        for i in range(n):
            x = (fp1[a0 + i] ^ fp2[b0 + i]) & 0xFFFFFFFF
            same += 32 - bin(x).count("1")
        sc = same / (n * 32)
        if sc > best:
            best = sc
    return best


def songs_match_fingerprint(path_a: str, path_b: str):
    """Compara por huella de Chromaprint. (match, sim, motivo) o None si
    fpcalc no está disponible (para que songs_match caiga al cromagrama)."""
    if not _find_fpcalc():
        return None
    fp_a = audio_fingerprint(path_a)
    fp_b = audio_fingerprint(path_b)
    if not fp_a or not fp_b:
        return None
    sim = fingerprint_similarity(fp_a, fp_b)
    return (sim >= FP_MATCH_THRESHOLD, sim, f"huella {sim:.2f}")


def songs_match(path_a: str, path_b: str,
                sec: float = 30.0, threshold: float = None):
    """Compara dos ficheros de audio y decide si son la misma canción.

    Usa chroma_stft (representa contenido harmónico y es robusto al bitrate).
    Devuelve (match: bool, sim: float, reason: str).
    """
    if threshold is None:
        threshold = AUDIO_MATCH_THRESHOLD
    # Primero la huella acústica (Chromaprint): es lo fiable. Solo si
    # fpcalc no está se cae al cromagrama de abajo.
    por_huella = songs_match_fingerprint(path_a, path_b)
    if por_huella is not None:
        return por_huella
    try:
        meta_a = get_metadata(path_a)
        meta_b = get_metadata(path_b)
        dur_a = meta_a.get("duration_s") or 0
        dur_b = meta_b.get("duration_s") or 0
        if dur_a and dur_b:
            tolerance = max(3.0, 0.08 * max(dur_a, dur_b))
            if abs(dur_a - dur_b) > tolerance:
                return False, 0.0, (
                    f"duración distinta ({dur_a:.0f}s vs {dur_b:.0f}s)"
                )

        def load_middle(path, dur):
            if dur > sec:
                offset = max(0.0, (dur - sec) / 2)
                return librosa.load(path, sr=22050, mono=True,
                                    offset=offset, duration=sec)
            return librosa.load(path, sr=22050, mono=True, duration=sec)

        y_a, sr_a = load_middle(path_a, dur_a)
        y_b, sr_b = load_middle(path_b, dur_b)
        if y_a.size == 0 or y_b.size == 0:
            return False, 0.0, "audio vacío"
        ca = librosa.feature.chroma_stft(y=y_a, sr=sr_a).mean(axis=1)
        cb = librosa.feature.chroma_stft(y=y_b, sr=sr_b).mean(axis=1)
        na = np.linalg.norm(ca)
        nb = np.linalg.norm(cb)
        if na == 0 or nb == 0:
            return False, 0.0, "chroma nulo"
        sim = float(np.dot(ca, cb) / (na * nb))
        return sim >= threshold, sim, f"sim {sim:.2f}"
    except Exception as e:
        return False, 0.0, f"error: {e}"


def verify_download_quality(path: str,
                            min_bitrate: int = GOOD_MP3_MIN_BR) -> tuple:
    """Comprueba que un fichero descargado cumple el mínimo de calidad
    pedido. Devuelve (ok, reason).

    - Lossless (FLAC/WAV) siempre pasa.
    - MP3 con bitrate declarado < min_bitrate → rechazado.
    - Si `min_bitrate` ≥ 320 (modo estricto), además ejecutamos análisis
      espectral para pillar fakes que anuncian 320 pero no lo son.
      Con min_bitrate < 320 confiamos en el bitrate declarado (política
      oportunista: "cualquier cosa mejor").
    """
    meta = get_metadata(path)
    codec = (meta.get("codec") or "").upper()
    if codec in ("FLAC", "WAV"):
        return True, f"{codec} lossless"
    br = meta.get("bitrate_kbps") or 0
    if br < min_bitrate:
        return False, f"declara {br} kbps (< {min_bitrate})"
    if min_bitrate < GOOD_MP3_MIN_BR:
        return True, f"{br} kbps"
    try:
        row = scan_row(path)
        row = analyze_row(row)
    except Exception as e:
        return False, f"error analizando: {e}"
    if row["verdict"] == "FAKE":
        return False, row["detail"]
    if row["verdict"] == "ERROR":
        return False, "no decodificable"
    return True, f"corte {row['cutoff_hz']} Hz"


SLSKD_STARTUP_TIMEOUT_S = 15.0
SLSKD_RECONNECT_TIMEOUT_S = 10.0
SLSKD_STARTUP_POLL_S = 0.5


def slskd_yml_path() -> Path:
    """Dónde vive el slskd.yml. En macOS slskd usa Application Support;
    en Linux, ~/.local/share. Devuelve el que exista, o el preferido."""
    home = Path.home()
    cands = [
        home / "Library" / "Application Support" / "slskd" / "slskd.yml",
        home / ".local" / "share" / "slskd" / "slskd.yml",
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[0] if sys.platform == "darwin" else cands[1]


def slskd_download_dir() -> str:
    """Carpeta donde slskd descarga, leída de su yml. '' si no está fijada
    (entonces slskd usa su carpeta por defecto, no la de Maceta)."""
    yml = slskd_yml_path()
    try:
        txt = yml.read_text(encoding="utf-8")
    except OSError:
        return ""
    dentro = False
    for ln in txt.splitlines():
        if re.match(r"^directories:\s*$", ln):
            dentro = True
            continue
        if dentro:
            m = re.match(r"^\s+downloads:\s*(.+?)\s*$", ln)
            if m:
                return m.group(1).strip().strip("\"'")
            if re.match(r"^\S", ln):
                break
    return ""


def slskd_set_download_dir(carpeta: str) -> bool:
    """Fija en el yml de slskd que descargue en `carpeta` (y los parciales
    en su subcarpeta .incomplete). Crea las carpetas. No borra nada: hace
    copia .bak del yml antes de tocarlo."""
    yml = slskd_yml_path()
    try:
        txt = yml.read_text(encoding="utf-8") if yml.exists() else ""
    except OSError:
        return False
    try:
        Path(carpeta).mkdir(parents=True, exist_ok=True)
        (Path(carpeta) / ".incomplete").mkdir(exist_ok=True)
    except OSError:
        pass
    # quitar cualquier bloque directories: activo
    txt = re.sub(r"(?m)^directories:\n(?:[ \t]+.*\n)*", "", txt)
    bloque = (f"directories:\n  downloads: {carpeta}\n"
              f"  incomplete: {carpeta}/.incomplete\n")
    nuevo = txt.rstrip() + "\n" + bloque
    try:
        yml.parent.mkdir(parents=True, exist_ok=True)
        if yml.exists():
            shutil.copy2(str(yml), str(yml) + ".bak")
        yml.write_text(nuevo, encoding="utf-8")
        return True
    except OSError as e:
        log.warning("no se pudo escribir %s: %s", yml, e)
        return False


def _slskd_status(url: str, key: str):
    """None si slskd no responde; si no, el JSON de /api/v0/application."""
    try:
        r = requests.get(
            url.rstrip("/") + "/api/v0/application",
            headers={"X-API-Key": key}, timeout=1.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _slskd_alive(url: str, key: str) -> bool:
    return _slskd_status(url, key) is not None


def _slskd_logged_in(url: str, key: str) -> bool:
    status = _slskd_status(url, key)
    return bool(status and status.get("server", {}).get("isLoggedIn"))


def _slskd_reconnect(url: str, key: str) -> None:
    """PUT /api/v0/server — el mismo botón 'Reconnect' de la web UI de
    slskd. slskd no reintenta solo si el servidor de Soulseek se cae
    (visto en vivo: se quedó 'Disconnected' hasta reiniciar el proceso
    a mano), así que lo disparamos nosotros al arrancar Maceta."""
    try:
        requests.put(
            url.rstrip("/") + "/api/v0/server",
            headers={"X-API-Key": key}, timeout=5.0,
        )
    except Exception:
        pass


def _wait_for(check, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        if check():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(SLSKD_STARTUP_POLL_S)


def _slskd_error_hint() -> str:
    """Las líneas con pinta de error de lo último que escupió slskd."""
    try:
        texto = SLSKD_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (f"\n\nNo hay salida en {SLSKD_LOG}. Pruébalo a mano en un "
                "terminal para ver el error.")
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if not lineas:
        return (f"\n\nslskd no escribió nada en {SLSKD_LOG}. Pruébalo a "
                "mano en un terminal.")
    interesantes = [l for l in lineas
                    if any(p in l.lower() for p in
                           ("error", "exception", "fatal", "invalid",
                            "denied", "refused", "in use", "unable",
                            "cannot", "failed", "required"))]
    muestra = (interesantes or lineas)[-4:]
    return ("\n\nLo que dijo slskd:\n  "
            + "\n  ".join(l[:150] for l in muestra)
            + f"\n\n(completo en {SLSKD_LOG})")


def maybe_launch_helpers() -> tuple:
    """Lee la config y arranca slskd/SoulseekQt si están configurados
    y no vivos aún. slskd se arranca primero y se espera a que responda
    antes de lanzar SoulseekQt. Devuelve (mensajes para status bar, hubo_fallo)."""
    cfg = load_config()
    msgs = []
    failed = False

    slskd_bin = (cfg.get("slskd_binary") or "").strip()
    url = (cfg.get("slskd_url") or "").strip()
    key = (cfg.get("slskd_api_key") or "").strip()
    if slskd_bin and Path(slskd_bin).exists() and url and key:
        if _slskd_alive(url, key):
            if _slskd_logged_in(url, key):
                msgs.append("slskd ya estaba en marcha")
            else:
                _slskd_reconnect(url, key)
                if _wait_for(lambda: _slskd_logged_in(url, key),
                             SLSKD_RECONNECT_TIMEOUT_S):
                    msgs.append("slskd estaba desconectado de Soulseek — reconectado")
                else:
                    msgs.append("slskd corriendo pero no conecta a Soulseek "
                               "(revisa tu red)")
                    failed = True
        elif _spawn_detached([slskd_bin], cwd=str(Path(slskd_bin).parent),
                             log_path=SLSKD_LOG):
            if _wait_for(lambda: _slskd_alive(url, key), SLSKD_STARTUP_TIMEOUT_S):
                if _wait_for(lambda: _slskd_logged_in(url, key),
                             SLSKD_RECONNECT_TIMEOUT_S):
                    msgs.append("slskd arrancado")
                else:
                    msgs.append("slskd arrancado pero no conecta a Soulseek "
                               "(revisa tu red)")
                    failed = True
            else:
                msgs.append("slskd no respondió tras arrancar."
                            + _slskd_error_hint())
                failed = True
        else:
            msgs.append("slskd no se pudo arrancar" + _slskd_error_hint())
            failed = True

    sqt = (cfg.get("soulseekqt_binary") or "").strip()
    if sqt and Path(sqt).exists():
        if sys.platform == "darwin" and sqt.endswith(".app"):
            # -g: no traer al foreground · -j: arrancar oculto
            _spawn_detached(["open", "-g", "-j", sqt])
        elif sys.platform == "win32":
            # Windows: pedir ventana minimizada via STARTUPINFO
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
                subprocess.Popen(
                    [sqt],
                    startupinfo=si,
                    creationflags=0x00000008,  # DETACHED_PROCESS
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except Exception:
                _spawn_detached([sqt])
        else:
            _spawn_detached([sqt])
        msgs.append("SoulseekQt lanzado (oculto)")

    return msgs, failed


# ---------- Cliente slskd ----------

SEARCH_WAIT_SECONDS = 60.0
SEARCH_POLL_INTERVAL = 2.0
SEARCH_MIN_RESPONSES = 5      # aceptamos parciales tras alcanzar esto
# slskd encola las búsquedas salientes: mientras estén en `Queued` aún no
# han salido a la red. Ese tiempo tiene su propio presupuesto para que no
# se coma el de la búsqueda real.
SEARCH_QUEUE_WAIT_SECONDS = 180.0
HTTP_RETRIES = 2               # nº total de intentos (1 = sin retry)
HTTP_RETRY_BACKOFF_S = 0.5


class SlskdClient:
    """Wrapper mínimo sobre la REST API de slskd (v0) con retry."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method, path, **kwargs):
        """HTTP con retry ante ConnectionError / Timeout transitorios."""
        url = f"{self.base}{path}"
        kwargs.setdefault("timeout", self.timeout)
        last = None
        for attempt in range(HTTP_RETRIES):
            try:
                r = self.session.request(method, url, **kwargs)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if attempt + 1 < HTTP_RETRIES:
                    time.sleep(HTTP_RETRY_BACKOFF_S * (attempt + 1))
        raise last

    def ping(self) -> dict:
        r = self._request("GET", "/api/v0/application", timeout=5)
        return r.json()

    def start_search(self, query: str,
                     search_timeout_ms: int = 30000) -> dict:
        """`search_timeout_ms` = tiempo que slskd espera peers antes de
        cerrar la búsqueda con 'Completed, TimedOut'. Default de slskd
        es 15000 (15 s). Con 30 s cazamos peers más lentos."""
        payload = {
            "searchText": query,
            "filterResponses": True,
            "fileLimit": 5000,
            "minimumResponseFileCount": 1,
            "searchTimeout": search_timeout_ms,
        }
        try:
            return self._request("POST", "/api/v0/searches",
                                 json=payload).json()
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) != 409:
                raise
            # slskd devuelve 409 si ya hay una búsqueda viva con el mismo
            # texto. En vez de fallar, nos enganchamos a esa: pasa cuando
            # dos canciones generan la misma query o con el rebusque auto.
            existing = self.find_active_search(query)
            if existing:
                log.info("409 en '%s': reutilizo la búsqueda %s",
                         query, existing.get("id"))
                return existing
            raise

    def find_active_search(self, query: str) -> dict:
        """Búsqueda sin terminar con ese mismo texto, o {}."""
        want = (query or "").strip().lower()
        try:
            searches = self.list_searches()
        except Exception:
            return {}
        for s in searches or []:
            if (s.get("searchText") or "").strip().lower() != want:
                continue
            if "Completed" not in str(s.get("state", "")):
                return s
        return {}

    def get_search(self, search_id: str) -> dict:
        r = self._request("GET", f"/api/v0/searches/{search_id}")
        return r.json()

    def get_responses(self, search_id: str) -> list:
        r = self._request(
            "GET", f"/api/v0/searches/{search_id}/responses")
        return r.json() or []

    def wait_for_search(
        self, search_id: str,
        max_wait: float = SEARCH_WAIT_SECONDS,
        poll_interval: float = SEARCH_POLL_INTERVAL,
        min_responses: int = SEARCH_MIN_RESPONSES,
        max_queue_wait: float = SEARCH_QUEUE_WAIT_SECONDS,
    ) -> dict:
        """Espera a que la búsqueda tenga suficiente material o complete.

        slskd encola las búsquedas salientes. Mientras el estado sea
        `Queued` la búsqueda todavía no ha salido a la red, así que ese
        tiempo se descuenta de `max_queue_wait` y no de `max_wait` — si
        no, nos rendíamos antes de que la búsqueda hubiese empezado.

        Acepta parciales: en cuanto haya ≥ `min_responses` respuestas
        tras al menos 15 s, cerramos la búsqueda y devolvemos. El cierre
        no es opcional — slskd solo sirve las respuestas de búsquedas
        completadas, así que devolver una InProgress equivalía a decir
        "nadie respondió" teniendo 200 peers esperando.
        """
        queue_deadline = time.time() + max_queue_wait
        deadline = early_deadline = None
        last = {}
        while True:
            try:
                info = self.get_search(search_id)
            except Exception:
                info = last
            last = info or {}
            state = str(last.get("state", ""))
            now = time.time()

            if "Queued" in state and "Completed" not in state:
                if now >= queue_deadline:
                    log.warning("search %s sigue en cola tras %.0fs",
                                search_id, max_queue_wait)
                    return last
                time.sleep(poll_interval)
                continue

            if deadline is None:  # primera vuelta ya fuera de la cola
                deadline = now + max_wait
                early_deadline = now + 15.0
            if "Completed" in state:
                return last
            rc = last.get("responseCount", 0) or 0
            if rc >= min_responses and now >= early_deadline:
                log.info("search %s: cierro con %d respuestas", search_id, rc)
                return self.close_for_results(search_id, last)
            if now >= deadline:
                log.info("search %s: agotada la espera con %d respuestas, "
                         "cierro para poder leerlas", search_id, rc)
                return self.close_for_results(search_id, last)
            time.sleep(poll_interval)

    def enqueue_download(self, username: str, files: list) -> None:
        self._request(
            "POST",
            f"/api/v0/transfers/downloads/"
            f"{urllib.parse.quote(username)}",
            json=files,
        )

    def list_searches(self) -> list:
        return self._request("GET", "/api/v0/searches").json() or []

    def stop_search(self, search_id: str) -> None:
        """Borra la búsqueda de slskd. Las respuestas se van con ella."""
        self._request("DELETE", f"/api/v0/searches/{search_id}")

    def cancel_search(self, search_id: str) -> None:
        """Cierra la búsqueda SIN borrarla: pasa a 'Completed, Cancelled'
        conservando todo lo recogido. Es la diferencia con `stop_search`,
        y es imprescindible: slskd **no sirve ninguna respuesta mientras
        la búsqueda está InProgress** — medido contra slskd 0.26, con 97
        respuestas contadas `/responses` devolvía 0, y 2 s después de este
        PUT devolvía 169."""
        self._request("PUT", f"/api/v0/searches/{search_id}")

    def close_for_results(self, search_id: str, last: dict = None,
                          timeout: float = 6.0) -> dict:
        """Cierra la búsqueda y espera a que slskd la marque completada,
        que es cuando empieza a servir las respuestas."""
        try:
            self.cancel_search(search_id)
        except Exception as e:
            log.warning("cancel_search %s falló: %s", search_id, e)
            return last or {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                info = self.get_search(search_id)
            except Exception:
                break
            if "Completed" in str(info.get("state", "")):
                return info
            last = info or last
        return last or {}

    # ---------- Mensajes privados ----------

    def list_conversations(self) -> list:
        return self._request("GET", "/api/v0/conversations").json() or []

    def get_conversation(self, username: str) -> dict:
        return self._request(
            "GET",
            f"/api/v0/conversations/{urllib.parse.quote(username)}",
        ).json() or {}

    def send_message(self, username: str, text: str) -> None:
        self._request(
            "POST",
            f"/api/v0/conversations/{urllib.parse.quote(username)}",
            json=text,
        )

    def acknowledge_conversation(self, username: str) -> None:
        self._request(
            "PUT",
            f"/api/v0/conversations/{urllib.parse.quote(username)}",
        )

    def list_downloads(self) -> list:
        return self._request(
            "GET", "/api/v0/transfers/downloads").json() or []

    def server_state(self) -> dict:
        """Estado de la conexión con la red Soulseek (de /application)."""
        try:
            app = self._request("GET", "/api/v0/application",
                                timeout=5).json() or {}
            return app.get("server") or {}
        except Exception:
            return {}

    def options(self) -> dict:
        """La configuración efectiva de slskd (directorios, shares…)."""
        try:
            return self._request("GET", "/api/v0/options", timeout=5).json() or {}
        except Exception:
            return {}

    def shares(self) -> list:
        """Carpetas que slskd comparte. Vacío = no compartes nada."""
        try:
            d = self._request("GET", "/api/v0/shares", timeout=5).json()
        except Exception:
            return []
        if isinstance(d, dict):
            return d.get("local") or d.get("directories") or []
        return d or []

    def cancel_download(self, username: str, transfer_id: str,
                        remove: bool = False) -> None:
        qs = "?remove=true" if remove else ""
        self._request(
            "DELETE",
            f"/api/v0/transfers/downloads/"
            f"{urllib.parse.quote(username)}/{transfer_id}{qs}",
        )


# ---------- Construcción de query y selección de mejor match ----------

_LEADING_NUM_RE = re.compile(r"^\d{1,4}[\s\-_.·]+")
_BRACKET_RE = re.compile(r"[\[\]\(\)\{\}]")
_QUALITY_JUNK_RE = re.compile(
    r"(\bwww\.\S+|"
    r"\b(320|256|192|128|96|kbps|hq|hd|hifi|lossless|"
    r"flac|mp3|aac|m4a|ogg|opus|wav)\b)",
    re.IGNORECASE,
)
# Palabras que distinguen una versión concreta: casi nunca están en los tags
# pero sí en el filename, y son las que hacen útil buscar por el nombre.
VERSION_WORDS = {
    "remix", "mix", "dub", "live", "edit", "version", "acoustic",
    "instrumental", "radio", "extended", "remaster", "remastered",
    "original", "club", "vocal", "feat", "ft", "bootleg", "rework",
    "unplugged", "demo", "cover",
}


def _clean_keep_brackets(s: str) -> str:
    """Quita símbolos (guiones, paréntesis, corchetes), número de pista y
    etiquetas de calidad, pero conserva las palabras de dentro de los
    paréntesis: ahí suele estar el nombre del remix o de la versión."""
    if not s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_NUM_RE.sub("", s)
    s = _BRACKET_RE.sub(" ", s)
    s = _QUALITY_JUNK_RE.sub(" ", s)
    s = re.sub(r"[_\-·–—]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def query_tokens(text: str) -> list:
    """Palabras únicas y buscables de un texto, en orden de aparición."""
    out, seen = [], set()
    for w in _clean_keep_brackets(text).split():
        w = w.strip(",.;:!?\"“”«»'`")
        k = w.lower()
        if not w or k in seen:
            continue
        seen.add(k)
        out.append(w)
    return out


EXACT_MATCH_RATIO = 0.95
# Por debajo de esto el nombre ya no es "la misma canción con adornos"
# sino otra cosa: un directo, otro corte del disco, un cover.
PLAUSIBLE_NAME_RATIO = 0.70


def name_similarity(query: str, filename: str) -> float:
    """Parecido 0..1 entre una query y el nombre de un fichero.

    Ignora carpeta, extensión, número de pista, puntuación, etiquetas de
    calidad y mayúsculas: lo que queda son las palabras de la canción.
    """
    stem = Path((filename or "").replace("\\", "/")).stem
    a = " ".join(query_tokens(query)).lower()
    b = " ".join(query_tokens(stem)).lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def exact_responses(responses: list, query: str,
                    ratio: float = EXACT_MATCH_RATIO) -> list:
    """Copia de `responses` conservando solo los ficheros cuyo nombre es
    practicamente idéntico a la query. Lista vacía si no hay ninguno."""
    out = []
    for resp in responses or []:
        files = [f for f in (resp.get("files") or [])
                 if name_similarity(query, f.get("filename")) >= ratio]
        if files:
            out.append({**resp, "files": files})
    return out


def apply_tunables(state: dict = None) -> None:
    """Vuelca los ajustes del usuario sobre las constantes del módulo.

    Las funciones puras (judge, choose_best, songs_match…) siguen leyendo
    globals, así que cambiar una opción tiene efecto inmediato sin tocar
    ninguna firma ni pasar config por diez sitios."""
    global GOOD_MP3_MIN_BR, OPPORTUNISTIC_FLOOR, MAX_FILE_SIZE_MB
    global FAKE_CUTOFF_FLOOR_HZ, CUTOFF_THRESHOLD_DB, ANALYSIS_MAX_SECONDS
    global EXACT_MATCH_RATIO, PLAUSIBLE_NAME_RATIO, AUDIO_MATCH_THRESHOLD
    st = load_user_state() if state is None else state

    def _n(key, cast, lo, hi):
        try:
            v = cast(st.get(key, USER_STATE_DEFAULTS[key]))
        except (TypeError, ValueError):
            v = cast(USER_STATE_DEFAULTS[key])
        return max(lo, min(hi, v))

    GOOD_MP3_MIN_BR      = _n("target_bitrate",       int,   64, 320)
    OPPORTUNISTIC_FLOOR  = _n("opportunistic_floor",  int,   32, 320)
    MAX_FILE_SIZE_MB     = _n("max_file_size_mb",     int,    1, 2000)
    FAKE_CUTOFF_FLOOR_HZ = _n("fake_cutoff_floor_hz", int, 8000, 22050)
    CUTOFF_THRESHOLD_DB  = _n("cutoff_threshold_db",  float, -120.0, -10.0)
    ANALYSIS_MAX_SECONDS = _n("analysis_max_seconds", float,  5.0, 3600.0)
    EXACT_MATCH_RATIO    = _n("exact_match_ratio",    float,  0.5, 1.0)
    PLAUSIBLE_NAME_RATIO = _n("plausible_name_ratio", float,  0.0, 1.0)
    AUDIO_MATCH_THRESHOLD = _n("audio_match_threshold", float, 0.3, 1.0)


# Palabras que no distinguen una canción de otra y solo sirven para que
# ningún peer encuentre nada.
QUERY_NOISE = {
    "the", "a", "an", "of", "and", "feat", "ft", "featuring", "with",
    "de", "la", "el", "los", "las", "y", "del", "un", "una",
    "remastered", "remaster", "version", "original", "official", "audio",
    "video", "hd", "hq", "stereo", "mono", "edit", "mix", "lp", "ep",
    "album", "single", "track", "bonus", "live",
}


def query_fallbacks(query: str, max_variants: int = 2) -> list:
    """Variantes progresivamente más cortas de una query, para cuando la
    exacta no devuelve nada.

    Un peer con la canción puede tenerla nombrada de otra forma: sobran
    las palabras de relleno, el año o la mitad del título. Se devuelven
    como mucho `max_variants`, siempre con dos palabras o más — con una
    sola palabra Soulseek devuelve ruido."""
    toks = query_tokens(query)
    if len(toks) < 3:
        return []
    out = []

    def push(words):
        cand = " ".join(words).strip()
        low = cand.lower()
        if (len(words) >= 2 and low != " ".join(toks).lower()
                and low not in [o.lower() for o in out]):
            out.append(cand)

    # 1. sin palabras de relleno ni años sueltos
    core = [t for t in toks
            if t.lower() not in QUERY_NOISE and not re.fullmatch(r"\d{4}", t)]
    push(core)
    # 2. las primeras palabras: el artista y el principio del título
    base = core or toks
    if len(base) > 4:
        push(base[:4])
    # 3. las tres más largas, que suelen ser las distintivas
    if len(base) > 3:
        longest = sorted(base, key=len, reverse=True)[:3]
        push([t for t in base if t in longest])
    return out[:max_variants]


def _tags_of(row):
    """Devuelve (artist, title, album) leídos vía mutagen, o strings vacíos."""
    try:
        mf = MutagenFile(row["path"], easy=True)
        if mf and mf.tags:
            artist = (mf.tags.get("artist") or [""])[0].strip()
            title = (mf.tags.get("title") or [""])[0].strip()
            album = (mf.tags.get("album") or [""])[0].strip()
            return artist, title, album
    except Exception:
        pass
    return "", "", ""


def build_query(row: dict, mode: str = "raw") -> str:
    """Query principal según el modo elegido en Preferencias.

    - raw:   nombre del fichero sin extensión, tal cual (guiones incluidos).
    - clean: lo mismo sin símbolos, nº de pista ni etiquetas de calidad.
    - tags:  artista + título leídos del ID3, con el filename de reserva.
    """
    stem = re.sub(r"\s+", " ", Path(row["path"]).stem).strip()
    if mode == "clean":
        return _clean_keep_brackets(stem) or stem
    if mode == "tags":
        artist, title, _ = _tags_of(row)
        if artist and title:
            return _clean_keep_brackets(f"{artist} {title}") or stem
        if title:
            return _clean_keep_brackets(title) or stem
    return stem


# Caracteres prohibidos en filenames por Windows/macOS/Linux.
_FS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Reemplaza caracteres prohibidos por '_' y normaliza espacios."""
    s = _FS_FORBIDDEN.sub("_", name or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s or "sin_nombre"


def parse_song_filename(stem: str) -> dict:
    """Extrae {artist, title} de un stem de fichero.

    Estrategia:
      1. Quita prefijo de número de pista (`01 - `, `007. `, `04_`).
      2. Quita `[...]` `(...)` y `{...}` finales (Remaster, feat, año).
      3. Quita basura habitual: `www.xxx`, `320`, `kbps`, `hq`, `flac`, etc.
      4. `_` → espacios, colapsa espacios.
      5. Divide por ` - `, `–`, `—`. Con 2 partes → artist / title;
         con 3+ → primera es artist, resto → title.
    """
    s = stem or ""
    # 1. Track number prefijo
    s = re.sub(r"^\s*\d{1,4}\s*[\-._·:]\s*", "", s)
    # 2. Bloques al final (repite: pueden anidarse)
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\s*[\[\(\{][^\]\)\}]*[\]\)\}]\s*$", "", s)
    # 3. Basura de bitrate/formato al final (repite: 'foo 320 kbps' → 'foo')
    junk_tail = re.compile(
        r"\s*(?:www\.\S+|320|256|192|128|96|kbps|hq|hd|hifi|"
        r"lossless|flac|mp3|aac|m4a|ogg|opus|wav)\s*$",
        re.IGNORECASE,
    )
    prev = None
    while prev != s:
        prev = s
        s = junk_tail.sub("", s)
    # 4. Underscores + espacios
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # 5. Split por separadores de artista/título
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]
    if len(parts) >= 2:
        return {"artist": parts[0], "title": " - ".join(parts[1:])}
    if len(parts) == 1:
        return {"artist": "", "title": parts[0]}
    return {"artist": "", "title": ""}


def suggest_filename(artist: str, title: str, ext: str) -> str:
    """Compone `Artist - Title.ext`. Si falta artist, sólo `Title.ext`."""
    ext = (ext or "").lstrip(".")
    parts = [p.strip() for p in (artist, title) if p and p.strip()]
    stem = " - ".join(parts) or "Unknown"
    return sanitize_filename(stem) + (f".{ext}" if ext else "")


def read_song_tags(path: str) -> dict:
    """Lee tags ID3/vorbis vía mutagen. Devuelve dict con claves comunes."""
    out = {"title": "", "artist": "", "album": "",
           "tracknumber": "", "date": "", "genre": ""}
    try:
        mf = MutagenFile(path, easy=True)
    except Exception:
        return out
    if mf is None or mf.tags is None:
        return out
    for k in out:
        v = mf.tags.get(k)
        if v:
            out[k] = v[0] if isinstance(v, list) else str(v)
    return out


def write_song_tags(path: str, tags: dict) -> None:
    """Escribe tags con mutagen easy. Crea el tag container si falta."""
    mf = MutagenFile(path, easy=True)
    if mf is None:
        raise RuntimeError("mutagen no reconoce el formato")
    if mf.tags is None:
        try:
            mf.add_tags()
        except Exception:
            pass
    for k, v in tags.items():
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            # Borrar la clave si viene vacía
            try:
                del mf.tags[k]
            except Exception:
                pass
            continue
        try:
            mf.tags[k] = v
        except Exception:
            pass
    mf.save()


def _file_ext(f: dict) -> str:
    ext = (f.get("extension") or "").lower().lstrip(".")
    if not ext:
        name = f.get("filename") or ""
        if "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
    return ext


def rejection_reason(f: dict, min_bitrate: int = GOOD_MP3_MIN_BR,
                     filters: dict = None) -> str:
    """Motivo por el que un fichero de Soulseek no vale, o "" si vale.

    Devolver el motivo (en vez de un bool) permite enseñar los descartados
    atenuados y entender por qué una búsqueda da pocos resultados."""
    filters = filters or {}
    size = f.get("size") or 0
    mb = size / 1024 / 1024
    max_mb = filters.get("max_mb", MAX_FILE_SIZE_MB) or 0
    min_mb = filters.get("min_mb", 0) or 0
    if max_mb and mb > max_mb:
        return f"muy grande ({mb:.0f} MB)"
    if min_mb and mb < min_mb:
        return f"muy pequeño ({mb:.1f} MB)"

    length = f.get("length") or 0
    min_sec = filters.get("min_sec", 0) or 0
    max_sec = filters.get("max_sec", 0) or 0
    if length:
        if min_sec and length < min_sec:
            return f"muy corto ({int(length)}s)"
        if max_sec and length > max_sec:
            return f"muy largo ({int(length)}s)"

    ext = _file_ext(f)
    allowed = filters.get("formats")
    if allowed and ext not in allowed:
        return f"formato {ext or '?'}"
    if ext in LOSSLESS_EXTS:
        return ""
    if ext != "mp3":
        return f"formato {ext or '?'}"

    br = f.get("bitRate") or 0
    if br >= min_bitrate:
        return ""
    if size and length:
        kbps = (size * 8) / length / 1000.0
        if kbps >= min_bitrate - 20:  # 20 kbps de tolerancia
            return ""
        return f"~{int(kbps)} kbps"
    return f"{br} kbps" if br else "bitrate desconocido"


def is_acceptable(f: dict, min_bitrate: int = GOOD_MP3_MIN_BR,
                  filters: dict = None) -> bool:
    return not rejection_reason(f, min_bitrate, filters)


def peer_rejection_reason(resp: dict, filters: dict = None) -> str:
    """Motivo por el que un peer no sirve, o "" si sirve."""
    filters = filters or {}
    banned = filters.get("banned")
    if banned and (resp.get("username") or "") in banned:
        return "usuario baneado"
    if filters.get("only_available") and not _is_available_peer(resp):
        return "sin slot libre"
    max_q = filters.get("max_queue", 0) or 0
    if max_q and (resp.get("queueLength") or 0) > max_q:
        return f"cola {resp.get('queueLength')}"
    return ""


def score(response: dict, f: dict, query: str) -> float:
    ext = _file_ext(f)
    if ext == "flac":
        quality = 100
    elif ext == "wav":
        quality = 90
    elif ext == "mp3":
        quality = min(80, int((f.get("bitRate") or 0) / 4))
    else:
        quality = 0
    slot = 20 if response.get("hasFreeUploadSlot") else 0
    queue_pen = min(30, response.get("queueLength", 0))
    speed = min(15, int((response.get("uploadSpeed") or 0) / 100_000))
    stem = Path((f.get("filename") or "").replace("\\", "/")).stem
    similarity = difflib.SequenceMatcher(
        None, query.lower(), stem.lower()
    ).ratio() * 40
    return quality + slot + speed + similarity - queue_pen


def name_tier(query: str, filename: str,
              exact: float = None, plausible: float = None) -> int:
    """2 = se llama igual que la query, 1 = plausible (misma canción con
    adornos), 0 = otra cosa. Es el primer criterio de `choose_best`."""
    exact = EXACT_MATCH_RATIO if exact is None else exact
    plausible = PLAUSIBLE_NAME_RATIO if plausible is None else plausible
    sim = name_similarity(query, filename)
    if sim >= exact:
        return 2
    return 1 if sim >= plausible else 0


def _quality_rank(resp: dict, f: dict, query: str) -> tuple:
    """Codec > tamaño > score. Lo que decide una vez el nombre empata."""
    ext = _file_ext(f)
    codec_rank = 3 if ext == "flac" else (2 if ext == "wav" else 1)
    size_bucket = (f.get("size") or 0) // (100 * 1024)
    return (codec_rank, size_bucket, score(resp, f, query))


def _is_available_peer(resp: dict) -> bool:
    """Peer con slot libre o sin cola — el que puede empezar YA."""
    if resp.get("hasFreeUploadSlot"):
        return True
    return (resp.get("queueLength") or 0) == 0


def choose_best(responses: list, query: str,
                min_bitrate: int = GOOD_MP3_MIN_BR,
                filters: dict = None):
    """Elige el mejor resultado.

    Regla dura: **primero se buscan peers disponibles** (slot libre o
    cola=0). Sólo si no hay ninguno disponible se cae al resto.

    Dentro de cada grupo:
      1. parecido del nombre con la query (exacto > plausible > otra cosa)
      2. codec class: FLAC > WAV > MP3
      3. tamaño en bytes (grande = calidad real; un '320 kbps' de 3 MB miente)
      4. score compuesto (velocidad, similitud de nombre, cola corta…)

    El nombre va primero a propósito: un fichero que no se llama como lo
    que buscas es otra canción — un directo, otro corte — y daba igual
    cuánto pesara. Antes el tamaño mandaba y se bajaban directos de 52 MB
    en vez del tema de estudio de 46 MB que sí se llamaba igual.
    """
    available, waiting = [], []
    for resp in responses:
        if peer_rejection_reason(resp, filters):
            continue
        for f in resp.get("files", []) or []:
            if not is_acceptable(f, min_bitrate=min_bitrate, filters=filters):
                continue
            (available if _is_available_peer(resp) else waiting).append(
                (resp, f)
            )

    def rank(rf):
        resp, f = rf
        return (name_tier(query, f.get("filename")),
                *_quality_rank(resp, f, query))

    pool = available if available else waiting
    if not pool:
        return None
    pool.sort(key=rank, reverse=True)
    return pool[0]


# ---------- Estado / bucket / display de la columna Calidad ----------
#
# Cada row de biblioteca cae en uno de estos "buckets" según su análisis y
# el estado de sus operaciones de Soulseek. Los chips arriba de la tabla
# usan esta agrupación.

BUCKETS = ("todas", "a_revisar", "fakes", "errores", "en_proceso",
           "listas", "sustituidas", "sin_analizar")

# (key, etiqueta base sin contador) — orden de aparición en la fila
CHIP_DEFS = [
    ("todas",        "📋 Todas"),
    ("a_revisar",    "⚠ Revisar"),
    ("fakes",        "❌ FAKE"),
    ("errores",      "⛔ Error"),
    ("en_proceso",   "⏳ En proceso"),
    ("listas",       "✅ Listas"),
    ("sustituidas",  "⬇ Sustituidas"),
    ("sin_analizar", "❓ Sin analizar"),
]


def _fmt_dur_long(seconds: float) -> str:
    """7320 -> '2 h 2 min'. Para totales de biblioteca, con días."""
    s = int(seconds or 0)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d} días {h} h {m} min"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def _fmt_size(num_bytes: float) -> str:
    mb = (num_bytes or 0) / 1024 / 1024
    return f"{mb/1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def compute_library_stats(rows: list) -> list:
    """Todas las estadísticas de una biblioteca, agrupadas por secciones.

    Devuelve [(título, [(etiqueta, valor, es_friki), ...]), ...]. Es
    función pura sobre las filas ya cargadas (solo toca disco para el
    tamaño de cada fichero), así que se puede llamar desde un hilo."""
    total = len(rows)
    if not total:
        return [("Sin datos", [("No hay canciones cargadas", "—", False)])]

    def pct(n):
        return f"{n} ({100 * n / total:.0f}%)"

    size_by_row, total_bytes = {}, 0
    for r in rows:
        try:
            b = os.path.getsize(r["path"])
        except OSError:
            b = 0
        size_by_row[r["path"]] = b
        total_bytes += b
    dur_total = sum(r.get("duration_s") or 0 for r in rows)
    analizadas = [r for r in rows if r.get("analyzed")]
    buckets = {}
    for r in rows:
        b = row_bucket(r)
        buckets[b] = buckets.get(b, 0) + 1
    carpetas = {str(Path(r["path"]).parent) for r in rows}

    secciones = [("📚 Resumen", [
        ("Canciones", str(total), False),
        ("Carpetas", str(len(carpetas)), False),
        ("Espacio ocupado", _fmt_size(total_bytes), False),
        ("Duración total", _fmt_dur_long(dur_total), False),
        ("A una canción al día, te dura",
         f"{total / 365:.1f} años" if total >= 365
         else f"{total} días", True),
        ("Tamaño medio por canción",
         _fmt_size(total_bytes / total), False),
        ("Analizadas", pct(len(analizadas)), False),
    ])]

    # ---------- calidad ----------
    cal = []
    for clave, etiqueta in (("listas", "✅ Listas"), ("fakes", "❌ FAKE"),
                            ("a_revisar", "⚠ A revisar"),
                            ("errores", "⛔ Con error"),
                            ("sustituidas", "⬇ Sustituidas"),
                            ("sin_analizar", "❓ Sin analizar")):
        cal.append((etiqueta, pct(buckets.get(clave, 0)), False))
    fakes = [r for r in rows if r.get("verdict") == "FAKE"]
    if fakes:
        peso_fake = sum(size_by_row.get(r["path"], 0) for r in fakes)
        cal.append(("Espacio malgastado en fakes", _fmt_size(peso_fake), True))
    lossless = [r for r in rows
                if (r.get("codec") or "").upper() in ("FLAC", "WAV")]
    if lossless:
        peso_ll = sum(size_by_row.get(r["path"], 0) for r in lossless)
        cal.append(("Lossless", pct(len(lossless)), False))
        cal.append(("Si pasaras el lossless a MP3 320 ahorrarías",
                    _fmt_size(max(0, peso_ll - sum(
                        (r.get("duration_s") or 0) * 40000
                        for r in lossless))), True))
    secciones.append(("🎯 Calidad", cal))

    # ---------- formatos y bitrates ----------
    codecs = {}
    for r in rows:
        c = (r.get("codec") or "?").upper()
        codecs[c] = codecs.get(c, 0) + 1
    fmt = [(c, pct(n), False)
           for c, n in sorted(codecs.items(), key=lambda kv: -kv[1])]
    brs = [r.get("bitrate_kbps") or 0 for r in rows if r.get("bitrate_kbps")]
    if brs:
        fmt.append(("Bitrate medio declarado",
                    f"{sum(brs) / len(brs):.0f} kbps", False))
        tramos = {"320+": 0, "256-319": 0, "192-255": 0, "<192": 0}
        for b in brs:
            k = ("320+" if b >= 320 else "256-319" if b >= 256
                 else "192-255" if b >= 192 else "<192")
            tramos[k] += 1
        for k, n in tramos.items():
            fmt.append((f"Declaran {k} kbps", pct(n), False))
    srs = {}
    for r in rows:
        s = r.get("sample_rate")
        if s:
            srs[s] = srs.get(s, 0) + 1
    for s, n in sorted(srs.items(), key=lambda kv: -kv[1])[:4]:
        fmt.append((f"{s} Hz", pct(n), False))
    secciones.append(("🎛 Formatos", fmt))

    # ---------- espectro ----------
    cortes = [r["cutoff_hz"] for r in rows if r.get("cutoff_hz")]
    if cortes:
        esp = [("Corte medio", f"{sum(cortes) / len(cortes):.0f} Hz", False)]
        tramos = {"≥ 19 kHz (excelente)": 0, "16–19 kHz (bien)": 0,
                  "14–16 kHz (sospechoso)": 0, "< 14 kHz (malo)": 0}
        for c in cortes:
            k = ("≥ 19 kHz (excelente)" if c >= 19000
                 else "16–19 kHz (bien)" if c >= 16000
                 else "14–16 kHz (sospechoso)" if c >= 14000
                 else "< 14 kHz (malo)")
            tramos[k] += 1
        for k, n in tramos.items():
            esp.append((k, f"{n} ({100 * n / len(cortes):.0f}% de analizadas)",
                        False))
        peor = min((r for r in rows if r.get("cutoff_hz")),
                   key=lambda r: r["cutoff_hz"])
        esp.append(("El fake más descarado",
                    f"{peor['name'][:44]} — {int(peor['cutoff_hz'])} Hz "
                    f"declarando {peor.get('bitrate_kbps') or '?'} kbps", True))
        secciones.append(("📈 Espectro", esp))

    # ---------- ritmo y duración ----------
    bpms = [r["bpm"] for r in rows if r.get("bpm")]
    ritmo = []
    if bpms:
        ritmo.append(("BPM medio", f"{sum(bpms) / len(bpms):.0f}", False))
        lenta = min((r for r in rows if r.get("bpm")), key=lambda r: r["bpm"])
        rapida = max((r for r in rows if r.get("bpm")), key=lambda r: r["bpm"])
        ritmo.append(("La más lenta",
                      f"{lenta['name'][:44]} — {lenta['bpm']:.0f} BPM", True))
        ritmo.append(("La más rápida",
                      f"{rapida['name'][:44]} — {rapida['bpm']:.0f} BPM", True))
        lentas = sum(1 for b in bpms if b < 90)
        medias = sum(1 for b in bpms if 90 <= b < 140)
        ritmo.append(("Lentas (< 90 BPM)", str(lentas), False))
        ritmo.append(("Medias (90–140)", str(medias), False))
        ritmo.append(("Rápidas (> 140)", str(len(bpms) - lentas - medias),
                      False))
    con_dur = [r for r in rows if r.get("duration_s")]
    if con_dur:
        larga = max(con_dur, key=lambda r: r["duration_s"])
        corta = min(con_dur, key=lambda r: r["duration_s"])
        ritmo.append(("Duración media",
                      _fmt_dur_long(dur_total / len(con_dur)), False))
        ritmo.append(("La más larga",
                      f"{larga['name'][:44]} — "
                      f"{_fmt_dur_long(larga['duration_s'])}", True))
        ritmo.append(("La más corta",
                      f"{corta['name'][:44]} — "
                      f"{_fmt_dur_long(corta['duration_s'])}", True))
    if ritmo:
        secciones.append(("🥁 Ritmo y duración", ritmo))

    # ---------- carpetas ----------
    por_carpeta = {}
    for r in rows:
        d = str(Path(r["path"]).parent)
        e = por_carpeta.setdefault(d, [0, 0])
        e[0] += 1
        e[1] += r.get("verdict") == "FAKE"
    top = sorted(por_carpeta.items(), key=lambda kv: -kv[1][0])[:5]
    carp = [(f"📁 {Path(d).name or d}", f"{n} temas, {f} fakes", False)
            for d, (n, f) in top]
    peores = [(d, v) for d, v in por_carpeta.items() if v[0] >= 5]
    peores.sort(key=lambda kv: -(kv[1][1] / kv[1][0]))
    if peores:
        d, (n, f) = peores[0]
        carp.append(("La carpeta con más fakes (de 5 temas o más)",
                     f"{Path(d).name} — {f} de {n} ({100 * f / n:.0f}%)",
                     True))
    secciones.append(("📂 Carpetas", carp))

    # ---------- si tu colección fuera física ----------
    # Duraciones típicas de cada soporte, en minutos.
    CD_MIN = 74        # CD-Audio estándar
    CINTA_MIN = 90     # cassette C-90 (45 por cara)
    VINILO_MIN = 44    # LP de 12", ~22 por cara
    MINIDISC_MIN = 74
    dur_min = dur_total / 60.0
    import math
    fis = [
        ("💿 CDs (74 min)", "%d discos" % math.ceil(dur_min / CD_MIN), True),
        ("📼 Cintas (C-90)", "%d cassettes" % math.ceil(dur_min / CINTA_MIN), True),
        ("🟤 Vinilos (LP)", "%d elepés" % math.ceil(dur_min / VINILO_MIN), True),
        ("💾 MiniDiscs", "%d" % math.ceil(dur_min / MINIDISC_MIN), True),
    ]
    # Altura de la pila de CDs (cada caja ~10 mm) y de vinilos (~3 mm el disco)
    n_cd = math.ceil(dur_min / CD_MIN)
    fis.append(("📏 Torre de CDs en cajas", "%.1f m de alto" % (n_cd * 0.01), True))
    # Peso aproximado de la colección en CDs (~100 g con caja)
    fis.append(("⚖ Pesarían en CDs", "%.1f kg" % (n_cd * 0.1), True))
    # Cuántos CDs por categoría (carpeta de primer nivel bajo la raíz).
    # La raíz es el ancestro común de todas las canciones.
    try:
        base = Path(os.path.commonpath([r["path"] for r in rows]))
    except Exception:
        base = None
    cat_dur = {}
    for r in rows:
        p = Path(r["path"])
        cat = "(raíz)"
        if base is not None:
            try:
                rel = p.relative_to(base)
                cat = rel.parts[0] if len(rel.parts) > 1 else "(raíz)"
            except ValueError:
                cat = "(fuera)"
        cat_dur[cat] = cat_dur.get(cat, 0) + (r.get("duration_s") or 0)
    top_cat = sorted(cat_dur.items(), key=lambda kv: -kv[1])[:6]
    for cat, d in top_cat:
        cds = math.ceil(d / 60.0 / CD_MIN)
        fis.append(("   💿 %s" % cat[:34],
                    "%d CDs (%s)" % (cds, _fmt_dur_long(d)), False))
    secciones.append(("💿 Si fuera física", fis))

    # ---------- artistas y frikadas ----------
    artistas, titulos = {}, {}
    for r in rows:
        info = parse_song_filename(Path(r["name"]).stem)
        a = (info.get("artist") or "").strip()
        t = (info.get("title") or "").strip().lower()
        if a:
            e = artistas.setdefault(a, [0, 0])
            e[0] += 1
            e[1] += r.get("verdict") == "FAKE"
        if a and t:
            titulos.setdefault(f"{a.lower()} - {t}", []).append(r["name"])
    frik = []
    top_a = sorted(artistas.items(), key=lambda kv: -kv[1][0])[:5]
    for a, (n, f) in top_a:
        frik.append((f"🎤 {a[:38]}", f"{n} temas" + (f", {f} fakes" if f else ""),
                     False))
    con_fakes = [(a, v) for a, v in artistas.items() if v[0] >= 3]
    con_fakes.sort(key=lambda kv: -(kv[1][1] / kv[1][0]))
    if con_fakes and con_fakes[0][1][1]:
        a, (n, f) = con_fakes[0]
        frik.append(("El artista peor servido", f"{a} — {f} de {n} fakes",
                     True))
    dups = {k: v for k, v in titulos.items() if len(v) > 1}
    frik.append(("Posibles duplicados", str(len(dups)), True))
    if dups:
        k = max(dups, key=lambda k: len(dups[k]))
        frik.append(("El más repetido",
                     f"{k[:40]} — {len(dups[k])} copias", True))
    nombre_largo = max(rows, key=lambda r: len(r["name"]))
    frik.append(("El nombre de fichero más largo",
                 f"{len(nombre_largo['name'])} caracteres", True))
    letras = {}
    for r in rows:
        c = (r["name"][:1] or "?").upper()
        letras[c] = letras.get(c, 0) + 1
    if letras:
        c, n = max(letras.items(), key=lambda kv: kv[1])
        frik.append(("Letra inicial más común", f"{c} ({n} canciones)", True))
    pendientes = [r for r in rows if not r.get("analyzed")]
    if pendientes:
        frik.append(("Música sin analizar",
                     _fmt_dur_long(sum(r.get("duration_s") or 0
                                       for r in pendientes)), True))
    secciones.append(("🏆 Artistas y frikadas", frik))
    return secciones


def row_bucket(row: dict) -> str:
    """Clasifica una fila en uno de: sin_analizar / en_proceso /
    a_revisar / fakes / errores / sustituidas / listas. 'todas' no la
    devuelve nadie — solo la usa el filtro para deshabilitarse a sí mismo.

    Las tres que piden atención van separadas a propósito: 'a_revisar' es
    una descarga que no cuadra con el original, 'fakes' es calidad real
    por debajo de lo declarado, y 'errores' es un fallo técnico al
    analizar. Son tres trabajos distintos."""
    # El estado de Soulseek se mira ANTES que `analyzed`, igual que en
    # display_verdict: una canción recién sustituida queda sin analizar
    # (el fichero cambió), pero su sitio es "sustituidas", no
    # "sin_analizar". Si no, el chip y la columna Calidad se contradicen.
    s = (row.get("slsk_status") or "").lower()
    if any(k in s for k in ("buscando", "verificando",
                             "descarg", "convirtiendo")):
        return "en_proceso"
    if s.startswith("↓") or "queued" in s or "cola" in s:
        return "en_proceso"
    if s.startswith("✓"):
        return "sustituidas"
    # Una descarga que no cuadra con el original manda sobre el veredicto.
    if "no coincide" in s:
        return "a_revisar"
    if not row.get("analyzed"):
        return "sin_analizar"
    # OK nativa (nada que hacer, siempre estuvo bien)
    v = row.get("verdict") or ""
    if v in ("OK", "LOSSLESS"):
        return "listas"
    if v == "FAKE":
        return "fakes"
    if v == "ERROR":
        return "errores"
    return "listas"


def tech_badge(row: dict) -> str:
    """Formato + bitrate + sample rate en una sola celda estrecha.

    `◆ FLAC` lossless · `● 320` un MP3 · el sample rate solo se escribe
    cuando NO es 44.1 kHz, que es el caso normal y no aporta nada. Tres
    columnas de 230 px pasan a una de 85."""
    codec = (row.get("codec") or "?").upper()
    br = row.get("bitrate_kbps")
    sr = row.get("sample_rate")
    if codec in ("FLAC", "WAV"):
        base = f"◆ {codec}"
    elif codec == "MP3":
        base = f"● {br}" if br else "● MP3"
    else:
        base = f"● {codec}"
    if sr and sr != 44100:
        base += f"·{sr // 1000}k"
    return base


def display_verdict(row: dict) -> str:
    """Texto enriquecido para la columna 'Calidad'. Prioriza el resultado
    de acciones (sustituido/convertido/descargando) sobre el veredicto
    original — el veredicto ya está reflejado en el detalle."""
    s = row.get("slsk_status") or ""
    if s.startswith("✓ sustituido"):
        return "⬇ Sustituida"
    if s.startswith("✓ MP3 320"):
        return "⬇ MP3 320"
    if s.startswith("✓"):
        return "✅ " + s.lstrip("✓ ").strip()
    if s.startswith("↓"):
        return "⏳ En cola"
    if "descarg" in s.lower() or "convirtiendo" in s.lower():
        return "⏳ " + s.split(":", 1)[-1].strip()
    if "buscando" in s.lower():
        return "🔎 Buscando"
    if "verificando" in s.lower():
        return "🔎 Verificando"
    if "no coincide" in s.lower():
        return "⚠ Revisar match"
    v = row.get("verdict") or ""
    if v == "LOSSLESS":
        return "🎵 LOSSLESS"
    if v == "OK":
        br = row.get("bitrate_kbps")
        return f"✅ {br} OK" if br else "✅ OK"
    if v == "FAKE":
        return "❌ FAKE"
    if v == "ERROR":
        return "❌ Error"
    if not row.get("analyzed"):
        return "❓ sin analizar"
    return v or "—"


VERDICT_COLORS = {
    "OK":         "#0a7f2e",
    "LOSSLESS":   "#0a5aa8",
    "FAKE":       "#b0261f",
    "ERROR":      "#666666",
    "?":          "#666666",
    "PENDING":    "#333333",
}


# Todos los colores tuneables. Key legible → (etiqueta UI, default hex).
COLOR_SPEC = [
    ("banner_bg",        "Disclaimer — fondo",        "#fff3b0"),
    ("banner_fg",        "Disclaimer — texto",        "#5c3d00"),
    ("tree_bg",          "Tabla — fondo",             "#ffffff"),
    ("tree_fg",          "Tabla — texto",             "#000000"),
    ("tree_selected_bg", "Tabla — fila seleccionada", "#3874d4"),
    ("tree_selected_fg", "Tabla — texto seleccionado","#ffffff"),
    ("verdict_OK",         "Veredicto OK",         "#0a7f2e"),
    ("verdict_LOSSLESS",   "Veredicto LOSSLESS",   "#0a5aa8"),
    ("verdict_FAKE",       "Veredicto FAKE",       "#b0261f"),
    ("verdict_ERROR",      "Veredicto ERROR",      "#666666"),
    ("verdict_PENDING",    "Fila sin analizar",    "#333333"),
    ("now_playing_fg",     "Now playing (texto)",  "#333333"),
    ("status_fg",          "Barra de estado (texto)", "#000000"),
]


def load_colors() -> dict:
    cfg = load_config()
    stored = cfg.get("colors") or {}
    return {k: stored.get(k, default) for k, _label, default in COLOR_SPEC}


def save_colors(colors: dict) -> None:
    cfg = load_config()
    cfg["colors"] = colors
    save_config(cfg)


class AudioPlayer:
    """Reproductor con control de posición para barras de scrubbing.

    Decodifica el fichero con ffmpeg a PCM (soporta cualquier formato que
    Maceta ya analiza) y lo reproduce con sounddevice, que permite
    empezar desde cualquier punto — eso es lo que da el seek. Si
    sounddevice no está, cae a `afplay` (nativo de macOS, sin barra).

    Un solo AudioPlayer suena a la vez: play() de otro instancia para el
    anterior, para no solapar audios ni pelear por la tarjeta.
    """

    _activo = None   # el que está sonando ahora mismo, sea quien sea

    def __init__(self, ffmpeg=None):
        self.ffmpeg = ffmpeg
        self.data = None          # numpy (N, canales) float32
        self.sr = 44100
        self.path = None
        self._pos0 = 0            # frame donde empezó el tramo actual
        self._t0 = None           # time.monotonic al arrancar el tramo
        self._paused_frame = 0
        self._afplay = None       # subproceso de afplay (fallback)

    # ---- backend ----
    @staticmethod
    def _sd():
        try:
            import sounddevice as sd
            return sd
        except Exception:
            return None

    def disponible_seek(self) -> bool:
        return self._sd() is not None

    # ---- carga ----
    def load(self, path) -> bool:
        if self.path == str(path) and self.data is not None:
            return True
        self.stop()
        self.path = str(path)
        self.data = None
        sd = self._sd()
        ff = self.ffmpeg
        if sd is None or not ff:
            return False      # sin seek: se usará afplay al reproducir
        try:
            import numpy as np
            out = subprocess.run(
                [ff, "-v", "quiet", "-i", self.path,
                 "-f", "f32le", "-ac", "2", "-ar", str(self.sr), "-"],
                capture_output=True)
            arr = np.frombuffer(out.stdout, dtype=np.float32)
            if arr.size == 0:
                self.data = None
                return False
            self.data = arr.reshape(-1, 2)
            return True
        except Exception as e:
            log.warning("AudioPlayer.load: %s", e)
            self.data = None
            return False

    @property
    def duration(self) -> float:
        if self.data is not None:
            return len(self.data) / self.sr
        return 0.0

    # ---- control ----
    def play(self, from_sec=None):
        sd = self._sd()
        if AudioPlayer._activo is not None and AudioPlayer._activo is not self:
            AudioPlayer._activo.stop()
        if self.data is not None and sd is not None:
            if from_sec is None:
                from_sec = self._paused_frame / self.sr
            frame = max(0, min(len(self.data) - 1, int(from_sec * self.sr)))
            try:
                sd.stop()
                sd.play(self.data[frame:], self.sr)
            except Exception as e:
                log.warning("AudioPlayer.play: %s", e)
                return
            self._pos0 = frame
            self._t0 = time.monotonic()
            AudioPlayer._activo = self
            return
        # ---- fallback afplay (sin seek) ----
        self._stop_afplay()
        try:
            self._afplay = subprocess.Popen(
                ["afplay", self.path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._t0 = time.monotonic()
            self._pos0 = 0
            AudioPlayer._activo = self
        except Exception as e:
            log.warning("afplay: %s", e)

    def pause(self):
        self._paused_frame = int(self.position * self.sr)
        self._detener_salida()

    def stop(self):
        self._paused_frame = 0
        self._detener_salida()

    def seek(self, sec):
        sonaba = self.is_playing
        self._paused_frame = max(0, int(sec * self.sr))
        if sonaba:
            self.play(sec)

    def _detener_salida(self):
        sd = self._sd()
        if sd is not None:
            try: sd.stop()
            except Exception: pass
        self._stop_afplay()
        self._t0 = None
        if AudioPlayer._activo is self:
            AudioPlayer._activo = None

    def _stop_afplay(self):
        if self._afplay:
            try:
                self._afplay.terminate()
                self._afplay.wait(timeout=1)
            except Exception:
                try: self._afplay.kill()
                except Exception: pass
            self._afplay = None

    @property
    def is_playing(self) -> bool:
        if self._afplay is not None:
            return self._afplay.poll() is None
        return (AudioPlayer._activo is self and self._t0 is not None
                and self.position < self.duration)

    @property
    def position(self) -> float:
        if self._t0 is None:
            return self._paused_frame / self.sr
        trans = time.monotonic() - self._t0
        if self.data is not None:
            return min(self.duration, self._pos0 / self.sr + trans)
        return trans


class MacetaApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Maceta – Analizador de audio")
        self.geometry("1080x600")
        self.minsize(880, 420)
        self.rows_by_iid: dict[str, dict] = {}
        self.folder = tk.StringVar(value="")
        self.status = tk.StringVar(value="Selecciona una carpeta para empezar")
        self.progress = tk.DoubleVar(value=0)
        self._user_state = load_user_state()
        us = self._user_state
        self.ask_quality_var = tk.BooleanVar(value=us["ask_quality"])
        self.substitute_var = tk.BooleanVar(value=us["substitute"])
        self.auto_flac_to_mp3_var = tk.BooleanVar(value=us["auto_flac_to_mp3"])
        self.force_320_var = tk.BooleanVar(value=us["force_320"])
        self.auto_retry_var = tk.BooleanVar(value=us["auto_retry"])
        self.auto_retry_interval_min = tk.IntVar(
            value=us["auto_retry_interval_min"])
        self.auto_retry_max_attempts = tk.IntVar(
            value=us["auto_retry_max_attempts"])
        self.search_min_mb = tk.DoubleVar(value=us["search_min_mb"])
        self.search_max_mb = tk.DoubleVar(value=us["search_max_mb"])
        self.search_min_sec = tk.IntVar(value=us["search_min_sec"])
        self.search_max_sec = tk.IntVar(value=us["search_max_sec"])
        self.search_only_available = tk.BooleanVar(
            value=us["search_only_available"])
        self.search_max_queue = tk.IntVar(value=us["search_max_queue"])
        stored_fmts = {s.strip().lower()
                       for s in (us["search_formats"] or "").split(",")
                       if s.strip()}
        self.search_format_vars = {
            ext: tk.BooleanVar(value=ext in stored_fmts)
            for ext in KNOWN_FORMATS
        }
        self.query_mode_var = tk.StringVar(value=us["query_mode"])
        self.results_sort_var = tk.StringVar(value=us["results_sort"])
        self.results_show_rejected = tk.BooleanVar(
            value=us["results_show_rejected"])
        self.results_limit = tk.IntVar(value=us["results_limit"])
        self.target_bitrate = tk.IntVar(value=us["target_bitrate"])
        self.opportunistic_floor = tk.IntVar(value=us["opportunistic_floor"])
        self.max_file_size_mb = tk.IntVar(value=us["max_file_size_mb"])
        self.fake_cutoff_floor_hz = tk.IntVar(value=us["fake_cutoff_floor_hz"])
        self.cutoff_threshold_db = tk.DoubleVar(value=us["cutoff_threshold_db"])
        self.analysis_max_seconds = tk.IntVar(value=us["analysis_max_seconds"])
        self.exact_match_ratio = tk.DoubleVar(value=us["exact_match_ratio"])
        self.plausible_name_ratio = tk.DoubleVar(
            value=us["plausible_name_ratio"])
        self.audio_match_threshold = tk.DoubleVar(
            value=us["audio_match_threshold"])
        self.max_auto_requery_var = tk.IntVar(value=us["max_auto_requery"])
        self.auto_substitute_var = tk.BooleanVar(value=us["auto_substitute"])
        self.restore_last_folder_var = tk.BooleanVar(
            value=us["restore_last_folder"])
        self.use_index_var = tk.BooleanVar(value=us["use_index"])
        self.banned_peers = list(us.get("banned_peers") or [])
        self.peer_strikes = dict(us.get("peer_strikes") or {})
        self.ban_strikes_var = tk.IntVar(value=us["ban_strikes"])
        self.ban_window_min_var = tk.IntVar(value=us["ban_window_min"])
        self.smart_search_var = tk.BooleanVar(value=us["smart_search"])
        self.show_all_dl_var = tk.BooleanVar(value=us["show_all_downloads"])
        self.compact_cols_var = tk.BooleanVar(value=us["compact_cols"])
        self._ui_font_orig = {}
        self.font_family_var = tk.StringVar(value=us.get("font_family") or "")
        self.ui_font_size_var = tk.IntVar(value=us.get("ui_font_size") or 0)
        self.font_sizes = dict(us.get("font_sizes") or {})
        self._font_keys = {}
        # Descargas que hemos cancelado nosotros: no cuentan como falta
        # del peer. Clave (usuario, fichero_lower), igual que el resto.
        self._self_cancelled: set = set()
        apply_tunables(us)
        # iid → {"next_at": epoch_s, "attempts": int}
        self._retry_queue: dict = {}
        # query → intentos automáticos gastados en rebuscarla
        self._requery_attempts: dict = {}
        # canciones que agotaron los reintentos y toca elegir a mano
        self._manual_needed: set = set()
        self._manual_prompt_job = None
        # (username, filename_reported_by_slskd_lower) -> original iid
        # Persistente durante toda la sesión — usado también para retry.
        self.pending_dl_links: dict[tuple, str] = {}
        # Descargas ya reconciliadas (para no re-disparar sustitución/status).
        self.finalized_dl_keys: set = set()
        # rows con búsqueda o descarga activa: iid -> "buscando" | "descargando"
        self.active_by_iid: dict[str, str] = {}
        # Numeración amigable #N para relacionar búsqueda ↔ descarga
        self._num_counter = 0
        self._num_lock = threading.Lock()
        self._search_num: dict[str, int] = {}  # sid -> #N
        self._dl_num: dict[tuple, int] = {}    # (user, filename_lc) -> #N
        self._search_link_iid: dict[str, str] = {}  # sid -> orig iid
        self._scan_gen = 0
        self._results_current_sid = None
        self._fetching_sid = None
        self._player_proc = None
        self._player_path = None
        self._player = AudioPlayer(self._find_ffmpeg())
        self.now_playing_var = tk.StringVar(value="—")
        self.colors = load_colors()
        self._themed_widgets: dict = {}  # role -> widget o [widgets]
        self._set_window_icon()
        self._build_ui()
        self._apply_colors()
        self._wire_state_persistence()
        self._col_save_job = None
        self._restore_column_widths()
        self._layout_job = None
        self._snapshot_ui_fonts()
        self._apply_ui_font()
        self.after(300, self._restore_layout)
        self.after(600, self._show_whats_new_if_updated)
        self.after(900, self._restore_last_folder)
        self.after(1500, self._startup_update_check)
        # NOTA: en v0.4.3 se retiró el poller que insertaba los ficheros
        # de DOWNLOADS_DIR en Biblioteca. Los descargados solo se ven en
        # Actividad → Descargas mientras están en curso y en el diálogo
        # "🔗 Sustituir con descarga…" para reemplazar un original.
        self._start_activity_poller()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        """Logo como icono de ventana. Si no está el png al lado del
        script, Tk se queda con el icono por defecto y no pasa nada."""
        png = MACETA_ROOT / "logo.png"
        if not png.exists():
            return
        try:
            self._icon_img = tk.PhotoImage(file=str(png))
            self.iconphoto(True, self._icon_img)
        except Exception as e:
            log.debug("no se pudo poner el icono: %s", e)

    def _build_ui(self):
        # Disclaimer permanente
        banner = tk.Frame(self, padx=8, pady=6)
        banner.pack(fill=tk.X, side=tk.TOP)
        banner_label = tk.Label(
            banner,
            text=(
                "⚠  SIEMPRE TEN UN BACKUP DE TU MÚSICA. Este programa está en "
                "desarrollo, puede tener bugs — no es buena idea correrlo "
                "encima de tu música sin copia previa."
            ),
            font=("TkDefaultFont", 10, "bold"),
            wraplength=1000, justify="left",
        )
        banner_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Se puede cerrar, pero vuelve en cada arranque: el aviso importa
        # y no se guarda ningún "no volver a mostrar".
        banner_close = tk.Label(banner, text="✕", cursor="pointinghand",
                                font=("TkDefaultFont", 12, "bold"))
        banner_close.pack(side=tk.RIGHT, padx=(8, 2))
        banner_close.bind("<Button-1>", lambda _e: banner.pack_forget())
        self._themed_widgets["banner"] = [banner, banner_label, banner_close]

        # Barra de estado abajo — siempre visible
        statusbar = ttk.Frame(self, padding=(8, 6), relief="sunken", borderwidth=1)
        statusbar.pack(fill=tk.X, side=tk.BOTTOM)
        self.progress_bar = ttk.Progressbar(
            statusbar, variable=self.progress, maximum=100, length=220
        )
        self.progress_bar.pack(side=tk.LEFT)
        self._status_label = ttk.Label(
            statusbar, textvariable=self.status, anchor=tk.W
        )
        self._status_label.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        # Notebook con Biblioteca + Actividad
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        biblio = ttk.Frame(self.notebook)
        activity = ttk.Frame(self.notebook)
        downloads = ttk.Frame(self.notebook)
        messages = ttk.Frame(self.notebook)
        self.notebook.add(biblio, text="Biblioteca")
        self.notebook.add(activity, text="Actividad Soulseek")
        self.notebook.add(downloads, text="Descargas")
        self.notebook.add(messages, text="💬 Mensajes")
        stats_tab = ttk.Frame(self.notebook)
        self.notebook.add(stats_tab, text="📊 Estadísticas")

        self._build_stats_tab(stats_tab)
        self._build_biblio_tab(biblio)
        self._build_activity_tab(activity)
        self._build_downloads_tab(downloads)
        self._build_messages_tab(messages)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_menubar()
        self._bind_shortcuts()
        self._bind_context_menu(self.tree, self._build_biblio_context)
        self._bind_context_menu(self.search_tree, self._build_search_context)
        self._bind_context_menu(self.dl_tree, self._build_dl_context)
        self._bind_context_menu(self.results_tree, self._build_results_context)

    # ---------- Pestaña de mensajes ----------

    def _build_messages_tab(self, parent):
        self._msg_user = None
        self._msg_convs = {}       # iid -> username
        self._msg_loaded_for = None

        top = ttk.Frame(parent, padding=(8, 6, 8, 4))
        top.pack(fill=tk.X)
        ttk.Button(top, text="↻ Actualizar",
                   command=self.on_refresh_conversations).pack(side=tk.LEFT)
        ttk.Label(top, text="   Escribir a:").pack(side=tk.LEFT)
        self.msg_new_user = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.msg_new_user, width=22)
        e.pack(side=tk.LEFT, padx=4)
        e.bind("<Return>", lambda _e: self.on_open_conversation_for(
            self.msg_new_user.get().strip()))
        ttk.Button(top, text="Abrir chat",
                   command=lambda: self.on_open_conversation_for(
                       self.msg_new_user.get().strip())).pack(side=tk.LEFT)
        self.msg_status = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.msg_status,
                  foreground="#777").pack(side=tk.LEFT, padx=12)

        panes = self._msg_panes = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(left, text="Conversaciones").pack(anchor="w")
        c_wrap, self.msg_tree = make_scrollable_tree(
            left, columns=("unread",), show="tree headings",
            selectmode="browse",
        )
        c_wrap.pack(fill=tk.BOTH, expand=True)
        self.msg_tree.heading("#0", text="Usuario")
        self.msg_tree.column("#0", width=180, anchor=tk.W)
        self.msg_tree.heading("unread", text="Sin leer")
        self.msg_tree.column("unread", width=70, anchor=tk.W)
        self.msg_tree.tag_configure("unread", foreground="#0a5aa8")
        self.msg_tree.bind("<<TreeviewSelect>>", self._on_conversation_select)

        right = ttk.Frame(panes)
        panes.add(right, weight=3)
        self.msg_title = tk.StringVar(value="Selecciona una conversación")
        ttk.Label(right, textvariable=self.msg_title,
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self.msg_text = tk.Text(right, wrap=tk.WORD, height=18,
                                state=tk.DISABLED)
        tsb = ttk.Scrollbar(right, orient="vertical",
                            command=self.msg_text.yview)
        self.msg_text.configure(yscrollcommand=tsb.set)
        self.msg_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)
        self.msg_text.tag_configure("in", foreground="#0a5aa8")
        self.msg_text.tag_configure("out", foreground="#0a7f2e")
        self.msg_text.tag_configure("meta", foreground="#999")

        send = ttk.Frame(right)
        send.pack(fill=tk.X, pady=(4, 0))
        self.msg_entry = tk.Text(send, height=3, wrap=tk.WORD)
        self.msg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.msg_entry.bind("<Return>", self._on_msg_return)
        ttk.Button(send, text="Enviar", command=self.on_send_message
                   ).pack(side=tk.LEFT, padx=6)

    def _on_msg_return(self, event):
        if event.state & 0x0001:      # Shift+Enter = salto de línea
            return None
        self.on_send_message()
        return "break"

    def on_refresh_conversations(self):
        client = self._slsk_client()
        if not client:
            self.msg_status.set("slskd no configurado")
            return
        self.msg_status.set("cargando…")

        def worker():
            try:
                convs = client.list_conversations()
            except Exception as e:
                log.warning("list_conversations failed: %s", e)
                self.after(0, self.msg_status.set, f"error: {e}")
                return
            self.after(0, self._populate_conversations, convs)
        threading.Thread(target=worker, daemon=True,
                         name="conversations").start()

    def _populate_conversations(self, convs):
        keep = self._msg_user
        self.msg_tree.delete(*self.msg_tree.get_children())
        self._msg_convs.clear()
        convs = sorted(convs, key=lambda c: (
            not c.get("hasUnAcknowledgedMessages"),
            (c.get("username") or "").lower()))
        for c in convs:
            user = c.get("username") or "?"
            n = c.get("unAcknowledgedMessageCount") or 0
            iid = self.msg_tree.insert(
                "", tk.END, text=user, values=(n or "",),
                tags=("unread",) if n else (),
            )
            self._msg_convs[iid] = user
            if user == keep:
                self.msg_tree.selection_set(iid)
        unread = sum(1 for c in convs if c.get("hasUnAcknowledgedMessages"))
        self.msg_status.set(f"{len(convs)} conversaciones · {unread} sin leer")

    def _on_conversation_select(self, _event=None):
        sel = self.msg_tree.selection()
        if not sel:
            return
        user = self._msg_convs.get(sel[0])
        if user and user != self._msg_loaded_for:
            self._load_conversation(user)

    def on_open_conversation_for(self, username):
        """Abre (o crea) el chat con un usuario y enfoca la pestaña."""
        username = (username or "").strip()
        if not username:
            return
        self.notebook.select(3)
        for iid, u in self._msg_convs.items():
            if u == username:
                self.msg_tree.selection_set(iid)
                self.msg_tree.see(iid)
                break
        self._load_conversation(username)

    def _load_conversation(self, username):
        self._msg_user = username
        self._msg_loaded_for = username
        self.msg_title.set(f"💬 {username}")
        self._render_messages([], loading=True)
        client = self._slsk_client()
        if not client:
            return

        def worker():
            try:
                conv = client.get_conversation(username)
            except Exception as e:
                log.warning("get_conversation(%s) failed: %s", username, e)
                self.after(0, self._render_messages, [], False, str(e))
                return
            msgs = conv.get("messages") or []
            if conv.get("hasUnAcknowledgedMessages"):
                try:
                    client.acknowledge_conversation(username)
                except Exception as e:
                    log.warning("acknowledge(%s) failed: %s", username, e)
            self.after(0, self._render_messages, msgs)
        threading.Thread(target=worker, daemon=True, name="conv").start()

    def _render_messages(self, msgs, loading=False, error=None):
        self.msg_text.configure(state=tk.NORMAL)
        self.msg_text.delete("1.0", tk.END)
        if loading:
            self.msg_text.insert(tk.END, "cargando…\n", "meta")
        elif error:
            self.msg_text.insert(tk.END, f"No se pudo cargar: {error}\n",
                                 "meta")
        elif not msgs:
            self.msg_text.insert(
                tk.END, "Sin mensajes todavía. Escribe abajo para empezar.\n",
                "meta")
        for m in msgs:
            out = (m.get("direction") or "").lower() == "out"
            who = "tú" if out else (m.get("username") or "?")
            ts = (m.get("timestamp") or "").replace("T", " ")[:16]
            self.msg_text.insert(tk.END, f"[{ts}] {who}: ", "meta")
            self.msg_text.insert(tk.END, f"{m.get('message', '')}\n",
                                 "out" if out else "in")
        self.msg_text.configure(state=tk.DISABLED)
        self.msg_text.see(tk.END)

    def on_send_message(self):
        user = self._msg_user
        text = self.msg_entry.get("1.0", tk.END).strip()
        if not user:
            messagebox.showinfo("Maceta", "Elige primero una conversación.")
            return
        if not text:
            return
        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        self.msg_entry.delete("1.0", tk.END)
        self.msg_status.set(f"enviando a {user}…")

        def worker():
            try:
                client.send_message(user, text)
            except Exception as e:
                log.warning("send_message(%s) failed: %s", user, e)
                self.after(0, self.msg_status.set, f"no se pudo enviar: {e}")
                self.after(0, messagebox.showerror, "Maceta",
                           f"No se pudo enviar el mensaje a {user}:\n{e}")
                return
            log.info("mensaje enviado a %s (%d chars)", user, len(text))
            self.after(0, self.msg_status.set, f"enviado a {user}")
            self.after(0, self._load_conversation, user)
        threading.Thread(target=worker, daemon=True, name="send-msg").start()

    def _build_menubar(self):
        mb = tk.Menu(self)
        self.configure(menu=mb)

        m_file = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Archivo", menu=m_file)
        m_file.add_command(label="Seleccionar carpeta…",
                           accelerator="⌘O", command=self.on_pick)
        self._recent_menu = tk.Menu(m_file, tearoff=0)
        m_file.add_cascade(label="Carpetas recientes", menu=self._recent_menu)
        self._rebuild_recent_menu()
        m_file.add_command(label="Exportar CSV…",
                           accelerator="⌘S", command=self.on_export)
        m_file.add_separator()
        m_file.add_command(label="Salir", accelerator="⌘Q",
                           command=self._on_close)

        m_view = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Ver", menu=m_view)
        m_view.add_command(label="Ver espectro",
                           accelerator="⌘E", command=self.on_show_spectrum)
        m_view.add_separator()
        m_view.add_command(label="▶ Reproducir",
                           accelerator="Space", command=self.on_play_selected)
        m_view.add_command(label="⏹ Parar",
                           command=self.on_stop_playback)
        m_view.add_separator()
        m_view.add_command(label="Refrescar Actividad",
                           command=self._activity_tick)

        m_ana = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Análisis", menu=m_ana)
        m_ana.add_command(label="Analizar selección",
                          accelerator="⌘R", command=self.on_analyze_selected)
        m_ana.add_command(label="Analizar todas",
                          accelerator="⇧⌘R", command=self.on_analyze_all)
        m_ana.add_command(label="🔄 Re-analizar (ignora el índice)",
                          command=self.on_reanalyze)
        m_ana.add_separator()
        m_ana.add_command(label="Convertir a MP3 320",
                          command=self.on_convert_mp3)

        m_sk = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Soulseek", menu=m_sk)
        m_sk.add_command(label="Bajar con Soulseek…",
                         accelerator="⌘D", command=self.on_slsk_search)
        m_sk.add_command(label="Bajar todas FAKE",
                         command=self.on_slsk_fakes)
        m_sk.add_separator()
        m_sk.add_command(label="Buscar por texto…",
                         accelerator="⌘F",
                         command=lambda: (self.notebook.select(1),
                                          self.search_entry.focus_set()))
        m_sk.add_separator()
        m_sk.add_command(label="Cancelar y borrar todo",
                         command=self.on_clear_all_activity)
        m_sk.add_command(label="Limpiar búsquedas completadas",
                         command=self.on_clear_searches)
        m_sk.add_command(label="Limpiar descargas completadas",
                         command=self.on_clear_downloads)
        m_sk.add_separator()
        m_sk.add_command(label="🩺 Diagnóstico de Soulseek…",
                         command=self.on_soulseek_diag)
        m_sk.add_command(label="🚫 Usuarios baneados…",
                         command=self.on_banned_peers)
        m_sk.add_command(label="⚙ Config Soulseek…",
                         command=self.on_slsk_config)

        m_bib = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Biblioteca", menu=m_bib)
        m_bib.add_command(label="🔗 Sustituir con descarga…",
                          command=self.on_manual_substitute)
        m_bib.add_command(label="Borrar (a archivo/)",
                          accelerator="⌫", command=self.on_delete_selected)
        m_bib.add_separator()
        m_bib.add_command(label="Vaciar lista",
                          command=self.on_clear_library)

        m_pref = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Preferencias", menu=m_pref)
        m_pref.add_checkbutton(label="Preguntar calidad",
                               variable=self.ask_quality_var,
                               command=self._update_pref_indicator)
        m_pref.add_checkbutton(label="Sustituir original al descargar",
                               variable=self.substitute_var,
                               command=self._update_pref_indicator)
        m_pref.add_checkbutton(label="Autoconvertir FLAC→MP3 320",
                               variable=self.auto_flac_to_mp3_var,
                               command=self._update_pref_indicator)
        m_pref.add_checkbutton(label="Forzar 320 (rechaza < 320)",
                               variable=self.force_320_var,
                               command=self._update_pref_indicator)
        m_pref.add_separator()
        m_pref.add_command(label="⚙ Opciones…", accelerator="⌘,",
                           command=self.on_options)
        m_pref.add_command(label="🎨 Colores…", command=self.on_colors_config)

        m_help = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Ayuda", menu=m_help)
        m_help.add_command(label="📖 Manual de Maceta…",
                           command=self.on_manual)
        m_help.add_separator()
        m_help.add_command(label="Comprobar actualizaciones…",
                           command=self.on_check_updates)
        m_help.add_command(label="Configurar actualizaciones…",
                           command=self.on_update_config)
        m_help.add_separator()
        m_help.add_command(label="📜 Ver changelog…",
                           command=self.on_show_changelog)
        m_help.add_command(label="📋 Abrir log…",
                           command=self.on_open_log)
        m_help.add_command(label="Acerca de Maceta…", command=self._on_about)

    def _bind_shortcuts(self):
        for seq, cb in [
            ("<Command-o>", self.on_pick),
            ("<Command-s>", self.on_export),
            ("<Command-e>", self.on_show_spectrum),
            ("<Command-r>", self.on_analyze_selected),
            ("<Command-Shift-R>", self.on_analyze_all),
            ("<Command-d>", self.on_slsk_search),
            ("<Command-f>", lambda _e=None: (
                self.notebook.select(1), self.search_entry.focus_set())),
            ("<Command-comma>", self.on_options),
        ]:
            self.bind_all(seq, lambda _e, c=cb: c())
        # Space y Backspace en la tabla de Biblioteca
        self.tree.bind("<space>", lambda _e: (self.on_play_selected(), "break"))
        self.tree.bind("<BackSpace>",
                       lambda _e: (self.on_delete_selected(), "break"))

    def _bind_context_menu(self, tree, builder):
        """Bindea click derecho (y two-finger-tap trackpad) para popupear
        un menú construido por `builder(iid) -> tk.Menu`."""
        def _popup(event):
            iid = tree.identify_row(event.y)
            if iid and iid not in tree.selection():
                tree.selection_set(iid)
            menu = builder(iid)
            if menu is None:
                return
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        # Botón derecho ratón + control-click en macOS + trackpad
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            tree.bind(seq, _popup)

    def _build_biblio_context(self, iid):
        row = self.rows_by_iid.get(iid) if iid else None
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="▶ Reproducir", command=self.on_play_selected,
                      state=("normal" if row else "disabled"))
        m.add_command(label="Ver espectro", command=self.on_show_spectrum,
                      state=("normal" if row else "disabled"))
        m.add_separator()
        n_sel = len(self.tree.selection())
        analyze_label = ("Analizar seleccionadas"
                         if n_sel > 1 else "Analizar")
        m.add_command(label=analyze_label,
                      command=self.on_analyze_selected,
                      state=("normal" if row else "disabled"))
        m.add_command(label="🔄 Re-analizar (ignora el índice)",
                      command=self.on_reanalyze,
                      state=("normal" if row else "disabled"))
        m.add_command(label="Bajar con Soulseek…",
                      command=self.on_slsk_search,
                      state=("normal" if row else "disabled"))
        m.add_command(label="🔗 Sustituir con descarga…",
                      command=self.on_manual_substitute,
                      state=("normal" if row and n_sel == 1 else "disabled"))
        is_lossless = row and (row.get("codec") or "").upper() in {"FLAC", "WAV"}
        m.add_command(label="Convertir a MP3 320",
                      command=self.on_convert_mp3,
                      state=("normal" if is_lossless else "disabled"))
        m.add_separator()
        m.add_command(label="🏷 Renombrar inteligente / editar tags…",
                      command=self.on_smart_rename,
                      state=("normal" if row and n_sel == 1 else "disabled"))
        m.add_command(label="Copiar ruta",
                      command=self._copy_selected_path,
                      state=("normal" if row else "disabled"))
        m.add_command(label="Mostrar en Finder",
                      command=self._reveal_in_finder,
                      state=("normal" if row else "disabled"))
        m.add_separator()
        if row and iid in self._retry_queue:
            m.add_command(
                label="🛑 Detener rebúsqueda auto",
                command=lambda: (self._retry_queue.pop(iid, None),
                                 self._set_slsk_status(iid, "auto-retry off")),
            )
        m.add_command(label="🗑 Borrar (a archivo/)",
                      command=self.on_delete_selected,
                      state=("normal" if row else "disabled"))
        return m

    def _build_search_context(self, iid):
        m = tk.Menu(self, tearoff=0)
        has = bool(iid)
        m.add_command(label="Elegir resultado…",
                      command=self.on_search_pick,
                      state=("normal" if has else "disabled"))
        m.add_command(label="Rebuscar (editar query)…",
                      command=self.on_research,
                      state=("normal" if has else "disabled"))
        m.add_command(label="🔍 Encontrar en Biblioteca",
                      command=self._find_from_search,
                      state=("normal" if has else "disabled"))
        m.add_separator()
        m.add_command(label="Cancelar",
                      command=self.on_cancel_search,
                      state=("normal" if has else "disabled"))
        return m

    def find_in_library(self, nombre, sid=None, user=None, remote=None):
        """Lleva a Biblioteca y marca la canción que corresponde.

        Busca en este orden: el link que se guardó al encolar la
        descarga, la búsqueda enlazada a la fila, y por último el
        parecido de nombre. Quita los filtros que la esconderían."""
        iid = None
        if user and remote:
            iid = self.pending_dl_links.get((user, (remote or "").lower()))
        if not iid and nombre:
            link = get_download_link(Path(nombre).name)
            if link.get("original_path"):
                iid = self._iid_for_path(link["original_path"])
        if not iid and sid:
            iid = self._search_link_iid.get(sid)
        if iid and iid not in self.rows_by_iid:
            iid = None
        if not iid and nombre:
            stem = Path(nombre).stem
            mejor, mejor_sim = None, 0.0
            for cand, row in self.rows_by_iid.items():
                sim = name_similarity(stem, row.get("name") or "")
                if sim > mejor_sim:
                    mejor, mejor_sim = cand, sim
            if mejor_sim >= 0.6:
                iid = mejor
        if not iid:
            # Caso típico: la canción existe pero la biblioteca cargada
            # apunta a otra carpeta. Si el link sabe dónde está, se
            # ofrece abrirla en vez de dejar al usuario tirado.
            destino = ""
            if nombre:
                link = get_download_link(Path(nombre).name)
                destino = (link.get("original_folder")
                           or str(Path(link["original_path"]).parent)
                           if link.get("original_path") else "")
            if destino and os.path.isdir(destino):
                if messagebox.askyesno(
                    "Maceta",
                    f"«{Path(nombre).stem[:48]}» no está en la biblioteca "
                    f"cargada, pero sé de dónde salió:\n\n{destino}\n\n"
                    "¿Abro esa carpeta?",
                ):
                    self.open_folder(destino)
                    self.after(1200, lambda: self.find_in_library(nombre))
                return
            messagebox.showinfo(
                "Maceta",
                f"No encuentro «{Path(nombre or '').stem[:50]}» en la "
                "biblioteca cargada.\n\nPuede que esté en otra carpeta: "
                "ábrela y vuelve a intentarlo.")
            return
        self._focus_library_row(iid)

    def _focus_library_row(self, iid):
        """Quita filtros, salta a Biblioteca y deja la fila marcada."""
        self.folder_filter = ""
        # El árbol tiene que acompañar: si se queda marcando una
        # subcarpeta mientras la lista enseña todo, el siguiente evento
        # del árbol vuelve a filtrar y se pierde la selección.
        try:
            raices = self.folder_tree.get_children("")
            if raices:
                self.folder_tree.selection_set(raices[0])
                self.folder_tree.see(raices[0])
        except Exception:
            pass
        self.folder_filter = ""
        self.active_bucket.set("todas")
        self.filter_var.set("")
        self.filter_pending_sub_var.set(False)
        self._apply_filter()
        self.notebook.select(0)
        self._pin_row(iid)
        # Un análisis en marcha repinta el filtro cada pocos ms y puede
        # llevarse la vista; se reafirma la selección un instante después.
        self.after(180, self._pin_row, iid)
        self.after(600, self._pin_row, iid)

    def _pin_row(self, iid):
        if iid not in self.rows_by_iid or not self.tree.exists(iid):
            return
        try:
            if iid in getattr(self, "_hidden_iids", set()):
                self.tree.reattach(iid, "", tk.END)
                self._hidden_iids.discard(iid)
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        except Exception:
            return
        row = self.rows_by_iid.get(iid) or {}
        carpeta = Path(row.get("path", "")).parent.name
        self.status.set(f"📍 {row.get('name', '')}  ·  en 📂 {carpeta}")

    def _find_from_downloads(self):
        sel = self.dl_tree.selection()
        if not sel:
            return
        tags = self.dl_tree.item(sel[0], "tags")
        user = tags[0] if tags and tags[0] != "virtual" else None
        nombre = self.dl_tree.set(sel[0], "file")
        self.find_in_library(nombre, user=user, remote=nombre)

    def _find_from_search(self):
        sel = self.search_tree.selection()
        if not sel:
            return
        tags = self.search_tree.item(sel[0], "tags")
        self.find_in_library(self.search_tree.set(sel[0], "query"),
                             sid=tags[0] if tags else None)

    def _find_from_results(self):
        sel = self.results_tree.selection()
        if not sel:
            return
        ref = self._results_items.get(sel[0])
        nombre = ""
        if ref:
            nombre = (ref[1].get("filename") or "").replace("\\", "/")
            nombre = nombre.rsplit("/", 1)[-1]
        self.find_in_library(nombre,
                             sid=getattr(self, "_results_current_sid", None))

    def _find_from_downloads_tab(self):
        sel = self.dl_files_tree.selection()
        if not sel:
            return
        p = self._dl_files_paths.get(sel[0])
        if p:
            self.find_in_library(p.name)

    def _build_dl_context(self, iid):
        m = tk.Menu(self, tearoff=0)
        has = bool(iid)
        m.add_command(label="Rebuscar (nueva query)…",
                      command=self.on_retry_download,
                      state=("normal" if has else "disabled"))
        user = ""
        if has:
            tags = self.dl_tree.item(iid, "tags")
            user = tags[0] if tags else ""
        m.add_command(
            label=f"💬 Escribir a {user}…" if user else "💬 Escribir al usuario…",
            command=lambda: self.on_open_conversation_for(user),
            state=("normal" if user else "disabled"))
        m.add_command(label="🔍 Encontrar en Biblioteca",
                      command=self._find_from_downloads,
                      state=("normal" if has else "disabled"))
        m.add_separator()
        m.add_command(
            label="Cancelar",
            command=lambda: self.on_cancel_download(remove=False),
            state=("normal" if has else "disabled"))
        m.add_command(
            label="Cancelar y borrar",
            command=lambda: self.on_cancel_download(remove=True),
            state=("normal" if has else "disabled"))
        return m

    def _build_results_context(self, iid):
        m = tk.Menu(self, tearoff=0)
        user = self.results_tree.set(iid, "user") if iid else ""
        m.add_command(label="⬇ Descargar seleccionados",
                      command=self._download_selected_results,
                      state=("normal" if iid else "disabled"))
        m.add_command(label="🔍 Encontrar en Biblioteca",
                      command=self._find_from_results,
                      state=("normal" if iid else "disabled"))
        m.add_separator()
        m.add_command(
            label=f"💬 Escribir a {user}…" if user else "💬 Escribir al usuario…",
            command=lambda: self.on_open_conversation_for(user),
            state=("normal" if user else "disabled"))
        return m

    def _copy_selected_path(self):
        sel = self.tree.selection()
        if not sel: return
        row = self.rows_by_iid.get(sel[0])
        if not row: return
        self.clipboard_clear()
        self.clipboard_append(row["path"])
        self.status.set(f"Ruta copiada: {row['path']}")

    def _reveal_in_finder(self):
        sel = self.tree.selection()
        if not sel: return
        row = self.rows_by_iid.get(sel[0])
        if not row: return
        p = row["path"]
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", p])
        else:
            subprocess.Popen(["xdg-open", str(Path(p).parent)])

    def _on_about(self):
        messagebox.showinfo(
            "Acerca de Maceta",
            f"Maceta v{VERSION} — analizador de calidad de audio\n\n"
            "Detecta MP3 fakes, extrae BPM y se integra con Soulseek/slskd\n"
            "para bajar versiones mejores.\n\n"
            "En desarrollo. Haz siempre backup de tu música.",
        )

    # ---------- Auto-update ----------

    def on_update_config(self):
        OptionsDialog(self, tab="Actualizaciones")

    def _show_whats_new_if_updated(self):
        """Al arrancar, si la versión cambió desde la última vez, muestra
        el bloque del CHANGELOG con lo nuevo. Persiste la versión vista."""
        cfg = load_config()
        last = cfg.get("last_seen_version", "")
        if last == VERSION:
            return
        entries = read_changelog_entries(
            MACETA_ROOT / "CHANGELOG.md",
            since_version=last,
            up_to_version=VERSION,
        )
        if entries:
            WhatsNewDialog(self, VERSION, entries)
        cfg["last_seen_version"] = VERSION
        save_config(cfg)

    def on_show_changelog(self):
        entries = read_changelog_entries(
            MACETA_ROOT / "CHANGELOG.md",
            up_to_version=VERSION,
        )
        WhatsNewDialog(self, VERSION, entries or "No hay CHANGELOG.md")

    def on_open_log(self):
        if not LOG_PATH.exists():
            messagebox.showinfo("Maceta", f"Aún no hay log en {LOG_PATH}")
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(LOG_PATH)])
        elif sys.platform == "win32":
            subprocess.Popen(["notepad", str(LOG_PATH)])
        else:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])

    def on_check_updates(self):
        cfg = load_config()
        token = (cfg.get("update_token") or "").strip()
        if not token:
            messagebox.showwarning(
                "Maceta",
                "Configura primero el token de actualizaciones\n"
                "(menú Ayuda → Configurar actualizaciones…).",
            )
            return
        self.status.set("Comprobando actualizaciones…")
        threading.Thread(
            target=self._check_updates_worker, args=(token, cfg, True),
            daemon=True,
        ).start()

    def _startup_update_check(self):
        cfg = load_config()
        if not cfg.get("auto_check_on_startup", True):
            return
        token = (cfg.get("update_token") or "").strip()
        if not token:
            return
        threading.Thread(
            target=self._check_updates_worker,
            args=(token, cfg, False),
            daemon=True,
        ).start()

    def _check_updates_worker(self, token, cfg, verbose):
        repo = cfg.get("update_repo") or DEFAULT_UPDATE_REPO
        branch = cfg.get("update_branch") or DEFAULT_UPDATE_BRANCH
        try:
            info = check_for_update(token, repo, branch)
        except Exception as e:
            if verbose:
                self.after(0, messagebox.showerror,
                           "Maceta", f"No se pudo comprobar:\n{e}")
            self.after(0, self.status.set, f"Update: error ({e})")
            return
        if not info["is_newer"]:
            msg = f"Estás en la última versión (v{info['current_version']})"
            self.after(0, self.status.set, msg)
            if verbose:
                self.after(0, messagebox.showinfo, "Maceta", msg)
            return
        self.after(0, self._prompt_update,
                   info["remote_version"], info["content"])

    def _prompt_update(self, remote_version, content):
        self.status.set(f"⬆ Nueva versión v{remote_version} disponible")
        if not messagebox.askyesno(
            "Maceta — actualización disponible",
            f"Hay una nueva versión: v{remote_version}\n"
            f"Tú tienes: v{VERSION}\n\n"
            "¿Bajarla e instalarla?\n"
            "(se hará backup del actual en 'archivo/' y Maceta se reiniciará)",
        ):
            return
        try:
            backup = apply_update(content)
        except Exception as e:
            messagebox.showerror(
                "Maceta",
                f"No se pudo aplicar la actualización:\n{e}\n\n"
                "No se ha tocado nada.",
            )
            return
        messagebox.showinfo(
            "Maceta",
            f"Actualizado a v{remote_version}.\n"
            f"Backup: {backup}\n\n"
            "Maceta se reiniciará.",
        )
        try:
            self.on_stop_playback()
        except Exception:
            pass
        self.destroy()
        restart_self()

    def _update_pref_indicator(self):
        parts = []
        if self.substitute_var.get(): parts.append("sustituir")
        if self.auto_flac_to_mp3_var.get(): parts.append("autoconv")
        if self.ask_quality_var.get(): parts.append("picker")
        if self.force_320_var.get(): parts.append("forzar 320")
        text = f"[{' · '.join(parts)}]" if parts else ""
        if hasattr(self, "pref_indicator_var"):
            self.pref_indicator_var.set(text)
        # Persistir el estado (se llama en cada toggle)
        self._save_current_state()

    def _wire_state_persistence(self):
        """Al modificar cualquier var persistente, guardar en disco."""
        # Chip activo + toggle "solo pdtes sustituir" → guardar
        self.active_bucket.trace_add(
            "write", lambda *_: self._save_current_state())
        self.filter_pending_sub_var.trace_add(
            "write", lambda *_: self._save_current_state())
        # Las 4 preferencias ya llaman a _update_pref_indicator que guarda.
        self._update_pref_indicator()

    def _save_current_state(self):
        save_user_state({
            "last_folder":              self.folder.get(),
            "ask_quality":              bool(self.ask_quality_var.get()),
            "substitute":               bool(self.substitute_var.get()),
            "auto_flac_to_mp3":         bool(self.auto_flac_to_mp3_var.get()),
            "force_320":                bool(self.force_320_var.get()),
            "active_bucket":            self.active_bucket.get(),
            "filter_pending":           bool(self.filter_pending_sub_var.get()),
            "auto_retry":               bool(self.auto_retry_var.get()),
            "auto_retry_interval_min":  int(self.auto_retry_interval_min.get() or 5),
            "auto_retry_max_attempts":  int(self.auto_retry_max_attempts.get() or 0),
            "search_min_mb":            self._num(self.search_min_mb, 0.0),
            "search_max_mb":            self._num(self.search_max_mb, 150.0),
            "search_min_sec":           int(self._num(self.search_min_sec, 0)),
            "search_max_sec":           int(self._num(self.search_max_sec, 0)),
            "search_only_available":    bool(self.search_only_available.get()),
            "search_max_queue":         int(self._num(self.search_max_queue, 0)),
            "search_formats":           ",".join(
                e for e, v in self.search_format_vars.items() if v.get()),
            "query_mode":               self.query_mode_var.get() or "clean",
            "results_sort":             self.results_sort_var.get() or "score",
            "results_show_rejected":    bool(self.results_show_rejected.get()),
            "results_limit":            int(self._num(self.results_limit, 300)),
            "target_bitrate":           int(self._num(self.target_bitrate, 320)),
            "opportunistic_floor":      int(self._num(self.opportunistic_floor, 192)),
            "max_file_size_mb":         int(self._num(self.max_file_size_mb, 150)),
            "fake_cutoff_floor_hz":     int(self._num(self.fake_cutoff_floor_hz, 16000)),
            "cutoff_threshold_db":      float(self._num(self.cutoff_threshold_db, -60.0)),
            "analysis_max_seconds":     int(self._num(self.analysis_max_seconds, 300)),
            "exact_match_ratio":        float(self._num(self.exact_match_ratio, 0.95)),
            "plausible_name_ratio":     float(self._num(self.plausible_name_ratio, 0.70)),
            "audio_match_threshold":    float(self._num(self.audio_match_threshold, 0.85)),
            "max_auto_requery":         int(self._num(self.max_auto_requery_var, 3)),
            "auto_substitute":          bool(self.auto_substitute_var.get()),
            "restore_last_folder":      bool(self.restore_last_folder_var.get()),
            "use_index":                bool(self.use_index_var.get()),
            "banned_peers":             list(self.banned_peers),
            "peer_strikes":             self.peer_strikes,
            "ban_strikes":              int(self._num(self.ban_strikes_var, 3)),
            "ban_window_min":           int(self._num(self.ban_window_min_var, 60)),
            "smart_search":             bool(self.smart_search_var.get()),
            "show_all_downloads":       bool(self.show_all_dl_var.get()),
            "font_family":              self.font_family_var.get() or "",
            "ui_font_size":             int(self._num(self.ui_font_size_var, 0)),
        })

    @staticmethod
    def _num(var, default):
        """Lee un Spinbox que el usuario puede haber dejado vacío o a medias."""
        try:
            return var.get()
        except Exception:
            return default

    def _search_filters(self) -> dict:
        """Filtros activos, listos para rejection_reason/peer_rejection_reason."""
        return {
            "min_mb":         self._num(self.search_min_mb, 0.0),
            "max_mb":         self._num(self.search_max_mb, 150.0),
            "min_sec":        int(self._num(self.search_min_sec, 0)),
            "max_sec":        int(self._num(self.search_max_sec, 0)),
            "only_available": bool(self.search_only_available.get()),
            "max_queue":      int(self._num(self.search_max_queue, 0)),
            "formats":        {e for e, v in self.search_format_vars.items()
                               if v.get()},
            "banned":         set(self.banned_peers),
        }

    def _restore_last_folder(self):
        """Al arrancar, si había una carpeta escaneada, la vuelve a
        escanear silenciosamente para restaurar el contexto de trabajo."""
        us = self._user_state
        last = us.get("last_folder") or ""
        # Restaurar chip y toggle antes de escanear
        if us.get("active_bucket") in BUCKETS:
            self.active_bucket.set(us["active_bucket"])
        if "filter_pending" in us:
            self.filter_pending_sub_var.set(bool(us["filter_pending"]))
        if last and os.path.isdir(last) and self.restore_last_folder_var.get():
            self.folder.set(last)
            self.status.set(f"Restaurando carpeta previa: {last}")
            threading.Thread(
                target=self._scan_folder, args=(last,), daemon=True
            ).start()

    def _min_bitrate_for(self, row=None):
        """Devuelve el mínimo bitrate aceptable según preferencias.

        - Forzar 320 ON → 320.
        - Off → OPPORTUNISTIC_FLOOR (192), o algo por encima del actual
          si hay row de referencia (queremos mejorar, no repetir).
        """
        if self.force_320_var.get():
            return GOOD_MP3_MIN_BR
        floor = OPPORTUNISTIC_FLOOR
        if row and row.get("bitrate_kbps"):
            floor = max(floor, min(GOOD_MP3_MIN_BR,
                                    row["bitrate_kbps"] + 32))
        return floor

    def _build_biblio_tab(self, parent):
        # El selector de carpeta vive en el panel izquierdo, no en una
        # barra a lo ancho: así la tabla gana una fila entera de alto.
        self._biblio_panes = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        self._biblio_panes.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(self._biblio_panes)
        self._biblio_panes.add(left, weight=1)
        right = ttk.Frame(self._biblio_panes)
        self._biblio_panes.add(right, weight=4)
        self._build_folder_explorer(left)
        parent = right

        # Chips de estado (buckets). Cada chip filtra a un subconjunto.
        self._chips_frame = ttk.Frame(parent, padding=(8, 4, 8, 0))
        self._chips_frame.pack(fill=tk.X)
        self.active_bucket = tk.StringVar(value="todas")
        self.chip_labels = {}
        for i, (key, label) in enumerate(CHIP_DEFS):
            btn = ttk.Radiobutton(
                self._chips_frame, text=label, value=key,
                variable=self.active_bucket,
                command=self._apply_filter,
            )
            btn.grid(row=0, column=i, sticky="w", padx=(0, 10))
            self.chip_labels[key] = btn
        self._chips_cols = len(CHIP_DEFS)
        self._chips_frame.bind("<Configure>", self._reflow_chips)

        # Filtro de biblioteca (solo oculta filas, no borra nada)
        filt = ttk.Frame(parent, padding=(8, 0, 8, 4))
        filt.pack(fill=tk.X)
        ttk.Label(filt, text="🔎 Filtrar (usa * y ?):").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(filt, textvariable=self.filter_var, width=28)
        filter_entry.pack(side=tk.LEFT, padx=6)
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        self.filter_effective_var = tk.StringVar(value="(sin filtro)")
        ttk.Label(filt, textvariable=self.filter_effective_var,
                  foreground="#666").pack(side=tk.LEFT, padx=4)
        ttk.Button(filt, text="Limpiar",
                   command=lambda: self.filter_var.set("")).pack(side=tk.LEFT)
        self.filter_pending_sub_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filt, text="Solo pdtes. sustituir",
            variable=self.filter_pending_sub_var,
            command=self._apply_filter,
        ).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(filt, text="Compacto",
                        variable=self.compact_cols_var,
                        command=self._apply_compact_cols
                        ).pack(side=tk.RIGHT, padx=(0, 6))
        self._font_controls(filt, lambda: self.tree, "biblio")
        self.pref_indicator_var = tk.StringVar(value="")
        ttk.Label(filt, textvariable=self.pref_indicator_var,
                  foreground="#888").pack(side=tk.RIGHT, padx=8)

        cols = ("tec", "codec", "bitrate", "sr", "dur", "bpm", "cutoff",
                "verdict", "detail", "slsk")
        headers = {
            "tec":     ("Audio",     85),
            "codec":   ("Formato",   70),
            "bitrate": ("Bitrate",   85),
            "sr":      ("SR",        75),
            "dur":     ("Duración",  75),
            "bpm":     ("BPM",       60),
            "cutoff":  ("Corte",     85),
            "verdict": ("Calidad",   110),
            "detail":  ("Detalle",   260),
            "slsk":    ("Soulseek",  200),
        }
        mid, self.tree = make_scrollable_tree(
            parent, columns=cols, show="tree headings", selectmode="extended"
        )
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.tree.heading("#0", text="Archivo")
        self.tree.column("#0", width=260, minwidth=180,
                         anchor=tk.W, stretch=False)
        for c in cols:
            title, w = headers[c]
            self.tree.heading(c, text=title,
                              command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=w, minwidth=max(40, w // 2),
                             anchor=tk.W, stretch=False)
        for v, color in VERDICT_COLORS.items():
            self.tree.tag_configure(v, foreground=color)
        self._apply_compact_cols()
        self.tree.bind("<Double-1>", self.on_show_spectrum)
        self.bind_all("<Control-a>", self._select_all_tree)
        self.bind_all("<Command-a>", self._select_all_tree)

        # Miniplayer + acciones rápidas de batch
        player = ttk.Frame(parent, padding=(8, 6, 8, 0))
        player.pack(fill=tk.X)
        icon_button(player, "▶", "Reproducir la seleccionada",
                    self.on_play_selected, side=tk.LEFT)
        icon_button(player, "⏹", "Parar", self.on_stop_playback,
                    side=tk.LEFT, padx=6)
        ttk.Separator(player, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        self.analyze_btn_var = tk.StringVar(value="Analizar todas")
        self._analyze_btn = icon_button(
            player, "🔎", "Analizar todas", self.on_analyze_all,
            side=tk.LEFT)
        icon_button(player, "🔄", "Re-analizar ignorando el índice",
                    self.on_reanalyze, side=tk.LEFT, padx=6)
        self.fakes_btn_var = tk.StringVar(value="Buscar fakes")
        self._fakes_btn = icon_button(
            player, "⬇", "Buscar en Soulseek las FAKE",
            self.on_slsk_fakes, side=tk.LEFT, padx=6)
        ttk.Separator(player, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        icon_button(player, "🗑", "Mover a archivo/ (no borra de verdad)",
                    self.on_delete_selected, side=tk.LEFT)
        # Lo que suena se dice en la barra de estado: el cuadro de aquí
        # ocupaba media barra para repetir lo mismo.

        # Preferencias globales — también accesibles desde el menú
        # "Preferencias". Las tenemos duplicadas aquí porque son
        # decisiones que afectan al flujo de descarga y conviene verlas
        # sin abrir un menú.
        opts = ttk.Frame(parent, padding=(8, 4, 8, 0))
        opts.pack(fill=tk.X)
        ttk.Label(opts, text="Opciones:",
                  foreground="#666").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Checkbutton(opts, text="Preguntar calidad",
                        variable=self.ask_quality_var,
                        command=self._update_pref_indicator
                        ).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Sustituir original",
                        variable=self.substitute_var,
                        command=self._update_pref_indicator
                        ).pack(side=tk.LEFT, padx=12)
        ttk.Checkbutton(opts, text="Autoconv FLAC→MP3 320",
                        variable=self.auto_flac_to_mp3_var,
                        command=self._update_pref_indicator
                        ).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Forzar 320",
                        variable=self.force_320_var,
                        command=self._update_pref_indicator
                        ).pack(side=tk.LEFT, padx=12)


    # ---------- Pestaña de estadísticas ----------

    def _build_stats_tab(self, parent):
        top = ttk.Frame(parent, padding=(10, 10, 10, 4))
        top.pack(fill=tk.X)
        ttk.Button(top, text="📊 Calcular estadísticas",
                   command=self.on_compute_stats).pack(side=tk.LEFT)
        self._font_controls(top, lambda: self.stats_tree, "stats")
        self.stats_scope_var = tk.StringVar(value="todo")
        ttk.Radiobutton(top, text="Toda la biblioteca", value="todo",
                        variable=self.stats_scope_var).pack(side=tk.LEFT,
                                                            padx=(14, 4))
        ttk.Radiobutton(top, text="Solo la carpeta seleccionada",
                        value="carpeta",
                        variable=self.stats_scope_var).pack(side=tk.LEFT)
        self.stats_status_var = tk.StringVar(
            value="Sin calcular todavía. Pulsa el botón: se calcula en "
                  "segundo plano y puedes seguir trabajando.")
        ttk.Label(parent, textvariable=self.stats_status_var,
                  foreground="#888", padding=(10, 0, 10, 6)).pack(fill=tk.X)

        wrap, self.stats_tree = make_scrollable_tree(
            parent, columns=("valor",), show="tree headings",
            selectmode="browse")
        wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.stats_tree.heading("#0", text="Métrica")
        self.stats_tree.column("#0", width=420, minwidth=220, anchor=tk.W)
        self.stats_tree.heading("valor", text="Valor")
        self.stats_tree.column("valor", width=320, minwidth=120, anchor=tk.W)
        self.stats_tree.tag_configure("section",
                                      font=("TkDefaultFont", 12, "bold"))
        self.stats_tree.tag_configure("fun", foreground="#7b4fd1")
        self._stats_running = False

    def on_compute_stats(self):
        if self._stats_running:
            messagebox.showinfo("Maceta", "Ya se está calculando.")
            return
        if not self.rows_by_iid:
            messagebox.showinfo("Maceta", "Primero selecciona una carpeta.")
            return
        solo_carpeta = (self.stats_scope_var.get() == "carpeta"
                        and self.folder_filter)
        rows = [dict(r) for r in self.rows_by_iid.values()
                if not solo_carpeta or self._in_folder_filter(r)]
        ambito = (Path(self.folder_filter).name if solo_carpeta
                  else (Path(self.folder.get()).name or "biblioteca"))
        self._stats_running = True
        self.stats_status_var.set(
            f"Calculando sobre {len(rows)} canciones de «{ambito}»… "
            "tarda un poco; puedes seguir usando Maceta y volver luego.")
        threading.Thread(target=self._stats_worker, args=(rows, ambito),
                         daemon=True, name="stats").start()

    def _stats_worker(self, rows, ambito):
        t0 = time.time()
        try:
            secciones = compute_library_stats(rows)
        except Exception as e:
            log.exception("estadísticas")
            self.after(0, self._stats_failed, str(e))
            return
        self.after(0, self._stats_done, secciones, ambito, time.time() - t0)

    def _stats_failed(self, msg):
        self._stats_running = False
        self.stats_status_var.set(f"No se pudieron calcular: {msg}")

    def _stats_done(self, secciones, ambito, elapsed):
        self._stats_running = False
        self.stats_tree.delete(*self.stats_tree.get_children())
        for titulo, filas in secciones:
            parent = self.stats_tree.insert(
                "", tk.END, text=titulo, values=("",), open=True,
                tags=("section",))
            for etiqueta, valor, friki in filas:
                self.stats_tree.insert(
                    parent, tk.END, text="   " + etiqueta,
                    values=(valor,), tags=("fun",) if friki else ())
        self.stats_status_var.set(
            f"«{ambito}» · calculado en {elapsed:.1f} s · "
            + time.strftime("%H:%M:%S"))

    # ---------- Tamaño de letra por tabla ----------

    FONT_MIN, FONT_MAX = 8, 22

    def _font_controls(self, parent, tree_getter, key):
        """Dos iconitos A− / A+ que cambian la letra solo de esa tabla."""
        box = ttk.Frame(parent)
        box.pack(side=tk.RIGHT)
        menos = ttk.Button(box, text="A−", width=3,
                           command=lambda: self._bump_font(tree_getter, key, -1))
        menos.pack(side=tk.LEFT)
        menos._tip = Tooltip(menos, "Letra más pequeña en esta tabla")
        mas = ttk.Button(box, text="A+", width=3,
                         command=lambda: self._bump_font(tree_getter, key, +1))
        mas.pack(side=tk.LEFT, padx=(2, 0))
        mas._tip = Tooltip(mas, "Letra más grande en esta tabla")
        self._font_keys[key] = tree_getter
        return box

    UI_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                "TkHeadingFont", "TkTooltipFont", "TkIconFont")

    def available_font_families(self) -> list:
        """Tipografías instaladas, sin las internas que empiezan por "."."""
        from tkinter import font as tkfont
        fams = sorted({f for f in tkfont.families() if not f.startswith(".")},
                      key=str.lower)
        return ["(la del sistema)"] + fams

    def _apply_ui_font(self):
        """Cambia la tipografía (y el tamaño base) de toda la interfaz.

        Toca las fuentes con nombre de Tk, así que afecta a menús,
        botones y etiquetas de una vez. Las tablas se repintan luego con
        `_restore_font_sizes`, que lee de aquí la familia."""
        from tkinter import font as tkfont
        fam = (self.font_family_var.get() or "").strip()
        if fam in ("", "(la del sistema)"):
            fam = None
        try:
            size = int(self._num(self.ui_font_size_var, 0))
        except Exception:
            size = 0
        for nombre in self.UI_FONTS:
            try:
                f = tkfont.nametofont(nombre)
            except Exception:
                continue
            if not hasattr(self, "_ui_font_base"):
                pass
            cambios = {}
            if fam:
                cambios["family"] = fam
            elif self._ui_font_orig.get(nombre):
                cambios["family"] = self._ui_font_orig[nombre][0]
            if size:
                cambios["size"] = max(self.FONT_MIN, min(self.FONT_MAX, size))
            elif self._ui_font_orig.get(nombre):
                cambios["size"] = self._ui_font_orig[nombre][1]
            if cambios:
                try:
                    f.configure(**cambios)
                except Exception as e:
                    log.warning("no se pudo aplicar la fuente %s: %s",
                                nombre, e)
        self._restore_font_sizes()

    def _snapshot_ui_fonts(self):
        """Guarda familia y tamaño originales para poder volver."""
        from tkinter import font as tkfont
        self._ui_font_orig = {}
        for nombre in self.UI_FONTS:
            try:
                f = tkfont.nametofont(nombre)
                self._ui_font_orig[nombre] = (f.cget("family"),
                                              int(f.cget("size")))
            except Exception:
                pass

    def _base_font_size(self) -> int:
        from tkinter import font as tkfont
        try:
            return int(tkfont.nametofont("TkDefaultFont").cget("size")) or 12
        except Exception:
            return 12

    def _apply_font_size(self, tree, key, size):
        from tkinter import font as tkfont
        try:
            fam = tkfont.nametofont("TkDefaultFont").cget("family")
        except Exception:
            fam = "TkDefaultFont"
        style_name = f"F{key}.Treeview"
        st = ttk.Style(self)
        st.configure(style_name, font=(fam, size),
                     rowheight=int(size * 2.0) + 2)
        st.configure(f"{style_name}.Heading", font=(fam, max(8, size - 1)))
        try:
            tree.configure(style=style_name)
        except Exception:
            pass

    def _bump_font(self, tree_getter, key, delta):
        tree = tree_getter() if callable(tree_getter) else tree_getter
        if tree is None:
            return
        antes = self.font_sizes.get(key, self._base_font_size())
        size = max(self.FONT_MIN, min(self.FONT_MAX, antes + delta))
        if size == antes:
            return
        self.font_sizes[key] = size
        self._apply_font_size(tree, key, size)
        self._scale_columns(tree, size / antes)
        save_user_state({"font_sizes": self.font_sizes})
        if self._col_save_job:
            try:
                self.after_cancel(self._col_save_job)
            except Exception:
                pass
        self._col_save_job = self.after(600, self._save_column_widths)

    def _scale_columns(self, tree, ratio):
        """Las columnas acompañan al tamaño de letra.

        Se escala lo que haya, no se recalcula desde cero: si el usuario
        ha estrechado una columna a mano, sigue siendo la estrecha."""
        try:
            cols = ("#0",) + tuple(tree.cget("columns") or ())
        except Exception:
            return
        for c in cols:
            try:
                w = int(tree.column(c, "width"))
            except Exception:
                continue
            nuevo = max(30, int(round(w * ratio)))
            try:
                tree.column(c, width=nuevo,
                            minwidth=min(int(tree.column(c, "minwidth")),
                                         nuevo))
            except Exception:
                pass

    def _restore_font_sizes(self):
        for key, getter in self._font_keys.items():
            size = self.font_sizes.get(key)
            if not size:
                continue
            tree = getter() if callable(getter) else getter
            if tree is not None:
                self._apply_font_size(tree, key, size)

    # ---------- Explorador de carpetas ----------

    def _build_folder_explorer(self, parent):
        sel = ttk.Frame(parent, padding=(8, 8, 4, 2))
        sel.pack(fill=tk.X)
        b = ttk.Button(sel, text="📂", command=self.on_pick, width=4)
        b.pack(side=tk.LEFT)
        b._tip = Tooltip(b, "Elegir la carpeta de tu música")
        ttk.Label(parent, textvariable=self.folder, foreground="#888",
                  wraplength=230, justify="left",
                  padding=(8, 0, 4, 4)).pack(fill=tk.X)

        head = ttk.Frame(parent, padding=(8, 0, 4, 2))
        head.pack(fill=tk.X)
        ttk.Label(head, text="Carpetas").pack(side=tk.LEFT)
        self._font_controls(head, lambda: self.folder_tree, "folders")
        cols = ("n", "fake", "pend")
        wrap, self.folder_tree = make_scrollable_tree(
            parent, columns=cols, show="tree headings", selectmode="browse")
        wrap.pack(fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 4))
        self.folder_tree.heading("#0", text="Carpeta")
        self.folder_tree.column("#0", width=210, minwidth=120, anchor=tk.W,
                                stretch=True)
        for c, (t, w) in [("n", ("Temas", 60)), ("fake", ("FAKE", 60)),
                          ("pend", ("Pdtes.", 60))]:
            self.folder_tree.heading(c, text=t)
            self.folder_tree.column(c, width=w, minwidth=40, anchor=tk.E,
                                    stretch=False)
        self.folder_tree.tag_configure("hasfake", foreground="#d04a3f")
        self.folder_tree.tag_configure("clean", foreground="#1a7f37")
        self.folder_tree.bind("<<TreeviewSelect>>",
                              lambda _e: self._on_folder_selected())
        self._folder_paths = {}      # iid -> ruta
        self.folder_filter = ""

        btns = ttk.Frame(parent, padding=(8, 0, 0, 6))
        btns.pack(fill=tk.X)
        icon_button(btns, "🔄", "Re-escanear esta carpeta: busca lo nuevo "
                    "y quita lo borrado", self.on_rescan_folder, side=tk.LEFT)
        icon_button(btns, "📚", "Volver a toda la biblioteca",
                    self._clear_folder_filter, side=tk.LEFT, padx=4)
        self._bind_context_menu(self.folder_tree, self._build_folder_context)

    def _build_folder_context(self, iid):
        path = self._folder_paths.get(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="🔄 Re-escanear esta carpeta",
                      command=self.on_rescan_folder,
                      state=("normal" if path else "disabled"))
        m.add_command(label="🔎 Analizar lo pendiente de aquí",
                      command=self._analyze_current_folder,
                      state=("normal" if path else "disabled"))
        m.add_separator()
        m.add_command(label="Mostrar en Finder",
                      command=lambda: self._reveal_path(path),
                      state=("normal" if path else "disabled"))
        return m

    def _reveal_path(self, path):
        if not path:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _folder_stats(self) -> dict:
        """{carpeta: [total, fakes, pendientes]} con los descendientes
        sumados hacia arriba. Sale de las filas ya cargadas, así que no
        toca disco."""
        root = self.folder.get()
        stats = {}
        for row in self.rows_by_iid.values():
            p = Path(row.get("path") or "")
            bucket = row_bucket(row)
            d = p.parent
            while True:
                s = stats.setdefault(str(d), [0, 0, 0])
                s[0] += 1
                s[1] += bucket == "fakes"
                s[2] += bucket == "sin_analizar"
                if str(d) == root or d == d.parent:
                    break
                d = d.parent
        return stats

    def _refresh_folder_counts(self):
        """Actualiza solo los números del árbol, sin rehacerlo.

        Durante un análisis largo esto se llama cada segundo: rehacer el
        árbol entero perdería lo desplegado y la selección, y con 4500
        filas no sale gratis."""
        if not getattr(self, "_folder_paths", None):
            return
        stats = self._folder_stats()
        for iid, path in list(self._folder_paths.items()):
            if not self.folder_tree.exists(iid):
                continue
            n, fake, pend = stats.get(path, [0, 0, 0])
            vals = (n, fake or "", pend or "")
            if self.folder_tree.item(iid, "values") != vals:
                tag = "hasfake" if fake else ("clean" if n and not pend else "")
                self.folder_tree.item(iid, values=vals,
                                      tags=(tag,) if tag else ())

    def _rebuild_folder_tree(self):
        """Repinta el árbol conservando lo desplegado y la selección."""
        if not hasattr(self, "folder_tree"):
            return
        opened = {self._folder_paths[i]
                  for i in self._folder_paths
                  if self.folder_tree.exists(i)
                  and self.folder_tree.item(i, "open")}
        selected = self.folder_filter
        self.folder_tree.delete(*self.folder_tree.get_children())
        self._folder_paths.clear()
        root = self.folder.get()
        if not root:
            return
        stats = self._folder_stats()
        hijos = {}
        for d in stats:
            if d == root:
                continue
            hijos.setdefault(str(Path(d).parent), []).append(d)

        def insert(parent_iid, path, label):
            n, fake, pend = stats.get(path, [0, 0, 0])
            tag = "hasfake" if fake else ("clean" if n and not pend else "")
            iid = self.folder_tree.insert(
                parent_iid, tk.END, text=label,
                values=(n, fake or "", pend or ""),
                tags=(tag,) if tag else (), open=path in opened)
            self._folder_paths[iid] = path
            for sub_path in sorted(hijos.get(path, []), key=str.lower):
                insert(iid, sub_path, Path(sub_path).name)
            return iid

        root_iid = insert("", root, f"📚 {Path(root).name or root}")
        self.folder_tree.item(root_iid, open=True)
        for iid, path in self._folder_paths.items():
            if path == selected:
                self.folder_tree.selection_set(iid)
                self.folder_tree.see(iid)
                break

    def _on_folder_selected(self):
        sel = self.folder_tree.selection()
        path = self._folder_paths.get(sel[0]) if sel else ""
        root = self.folder.get()
        self.folder_filter = "" if path == root else (path or "")
        self._apply_filter()

    def _clear_folder_filter(self):
        self.folder_filter = ""
        if self.folder_tree.get_children():
            self.folder_tree.selection_set(
                self.folder_tree.get_children()[0])
        self._apply_filter()

    def _analyze_current_folder(self):
        iids = [i for i, r in self.rows_by_iid.items()
                if self._in_folder_filter(r) and not r.get("analyzed")]
        if not iids:
            messagebox.showinfo("Maceta",
                                "No queda nada por analizar en esa carpeta.")
            return
        self._start_analysis(iids)

    def _in_folder_filter(self, row) -> bool:
        if not self.folder_filter:
            return True
        p = str(Path(row.get("path") or ""))
        return p.startswith(self.folder_filter + os.sep)

    def on_rescan_folder(self):
        """Vuelve a mirar el disco: las carpetas cambian, y hay que poder
        recoger lo nuevo sin perder el análisis de lo que ya estaba."""
        target = self.folder_filter or self.folder.get()
        if not target or not os.path.isdir(target):
            messagebox.showinfo("Maceta", "Primero selecciona una carpeta.")
            return
        self.status.set(f"Re-escaneando {Path(target).name}…")
        threading.Thread(target=self._rescan_worker, args=(target,),
                         daemon=True, name="rescan-folder").start()

    def _rescan_worker(self, target):
        en_disco = {
            str(p) for p in Path(target).rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        }
        conocidos = {str(Path(r["path"])): iid
                     for iid, r in list(self.rows_by_iid.items())
                     if str(Path(r["path"])).startswith(str(Path(target)))}
        nuevos = sorted(en_disco - set(conocidos))
        perdidos = [iid for p, iid in conocidos.items() if p not in en_disco]
        filas = []
        for p in nuevos:
            row = scan_row(p)
            if self.use_index_var.get():
                hydrate_from_index(row)
            filas.append(row)
        self.after(0, self._rescan_apply, filas, perdidos, target)

    def _rescan_apply(self, filas, perdidos, target):
        for iid in perdidos:
            self.rows_by_iid.pop(iid, None)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        for row in filas:
            iid = self.tree.insert(
                "", tk.END, text=row["name"],
                values=self._row_values(row),
                tags=(row["verdict"] or "PENDING",))
            self.rows_by_iid[iid] = row
        partes = []
        if filas:
            partes.append(f"{len(filas)} nueva(s)")
        if perdidos:
            partes.append(f"{len(perdidos)} ya no está(n)")
        self.status.set(
            f"{Path(target).name}: " +
            (" · ".join(partes) if partes else "sin cambios"))
        self._rebuild_folder_tree()
        self._apply_filter()

    def _build_activity_tab(self, parent):
        # ── Cabecera: caja de búsqueda por texto libre + botón cleanup ──
        search = ttk.Frame(parent, padding=(8, 6, 8, 0))
        search.pack(fill=tk.X)
        ttk.Label(search, text="🔎 Buscar y descargar:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.search_entry.bind("<Return>", lambda _e: self.on_search_query())
        icon_button(search, "⬇", "Buscar y descargar de Soulseek",
                    self.on_search_query, side=tk.LEFT)
        icon_button(search, "🧨", "Cancelar y borrar TODAS las búsquedas y "
                    "descargas", self.on_clear_all_activity,
                    side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            search, text="⟳ Rebuscar auto",
            variable=self.auto_retry_var,
            command=self._save_current_state,
        ).pack(side=tk.LEFT, padx=(12, 0))

        # ── Resumen de lo que está pasando, al estilo de los chips de
        # Biblioteca: de un vistazo, cuántas buscan, cuántas encontraron
        # y cuántas descargas van bien.
        stats = ttk.Frame(parent, padding=(8, 4, 8, 0))
        stats.pack(fill=tk.X)
        self.act_search_stats = tk.StringVar(value="🔎 sin búsquedas")
        self.act_dl_stats = tk.StringVar(value="📥 sin descargas")
        ttk.Label(stats, textvariable=self.act_search_stats).pack(side=tk.LEFT)
        ttk.Separator(stats, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Label(stats, textvariable=self.act_dl_stats).pack(side=tk.LEFT)

        # ── PanedWindow vertical: (arriba) Búsquedas | Resultados,
        # (abajo) Descargas — el usuario puede arrastrar los divisores.
        main = self._activity_panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        top_paned = self._activity_top_panes = ttk.PanedWindow(
            main, orient=tk.HORIZONTAL)
        main.add(top_paned, weight=1)

        # --- Búsquedas (izquierda) ---
        s_frame = ttk.LabelFrame(top_paned, text="Búsquedas", padding=6)
        top_paned.add(s_frame, weight=1)
        s_head = ttk.Frame(s_frame)
        s_head.pack(fill=tk.X)
        self._font_controls(s_head, lambda: self.search_tree, "searches")
        s_cols = ("num", "query", "state", "responses", "files")
        s_wrap, self.search_tree = make_scrollable_tree(
            s_frame, columns=s_cols, show="headings",
            selectmode="extended", height=6,
        )
        s_wrap.pack(fill=tk.BOTH, expand=True)
        # Rojo: nadie respondió. Ámbar: respondieron pero tus filtros
        # dejan fuera todo lo que ofrecen.
        self.search_tree.tag_configure("nores", foreground="#d04a3f")
        self.search_tree.tag_configure("filtered", foreground="#c9890a")
        self.search_tree.tag_configure("done", foreground="#1a7f37")
        # Completada, con resultados aceptables, pero aún no has bajado
        # ninguna: hay cosa que elegir aquí. Cian para que no se confunda
        # con las que siguen en curso (negro).
        self.search_tree.tag_configure("pendiente", foreground="#2aa6c0")
        self._search_usable = {}
        # #N que ya han terminado de bajar. Se acumula y no se vacía:
        # limpiar la lista de descargas de slskd no debe perder el verde.
        self._completed_nums: set = set()
        for c, (t, w) in [
                ("num",       ("#",        40)),
                ("query",     ("Query",    280)),
                ("state",     ("Estado",   150)),
                ("responses", ("Resps.",   70)),
                ("files",     ("Archivos", 70))]:
            self.search_tree.heading(c, text=t)
            self.search_tree.column(c, width=w, minwidth=max(40, w // 2),
                                    anchor=tk.W, stretch=False)
        s_btns = ttk.Frame(s_frame)
        s_btns.pack(fill=tk.X, pady=(4, 0))
        icon_button(s_btns, "✕", "Cancelar la búsqueda seleccionada",
                    self.on_cancel_search, side=tk.LEFT)
        icon_button(s_btns, "✎", "Rebuscar editando la query",
                    self.on_research, side=tk.LEFT, padx=4)
        icon_button(s_btns, "🧹", "Quitar las búsquedas completadas",
                    self.on_clear_searches, side=tk.LEFT, padx=4)
        icon_button(s_btns, "↻", "Refrescar", self._activity_tick,
                    side=tk.LEFT)
        self.search_tree.bind("<<TreeviewSelect>>",
                              lambda _e: (self._on_search_selected(),
                                          self._sync_dl_from_search()))
        self.search_tree.bind("<Double-1>",
                              lambda _e: self._on_search_selected())

        # --- Resultados (derecha) ---
        r_frame = ttk.LabelFrame(top_paned, text="Resultados", padding=6)
        top_paned.add(r_frame, weight=2)
        r_head = ttk.Frame(r_frame)
        r_head.pack(fill=tk.X)
        self._font_controls(r_head, lambda: self.results_tree, "results")
        # Orden: # · Nombre · Cola · Slot · MB · (resto)
        r_cols = ("num", "name", "queue", "slot", "size",
                  "ext", "br", "user", "folder", "speed")
        r_wrap, self.results_tree = make_scrollable_tree(
            r_frame, columns=r_cols, show="headings",
            selectmode="extended", height=6,
        )
        r_wrap.pack(fill=tk.BOTH, expand=True)
        # Descartados: grises y en cursiva, para que se lean como
        # desactivados pero sigan siendo seleccionables a mano.
        self.results_tree.tag_configure(
            "rejected", foreground="#9a9a9a",
            font=("TkDefaultFont", 11, "italic"))
        self._results_items = {}
        self._results_sort = {}

        def _r_sorter(col):
            def cb():
                desc = self._results_sort.get(col, False)
                sort_treeview_by_column(self.results_tree, col,
                                        descending=desc)
                self._results_sort[col] = not desc
            return cb

        for c, (t, w) in [
                ("num",    ("#",       40)),
                ("name",   ("Fichero", 300)),
                ("queue",  ("Cola",    55)),
                ("slot",   ("Slot",    50)),
                ("size",   ("MB",      65)),
                ("ext",    ("Ext",     55)),
                ("br",     ("kbps",    60)),
                ("user",   ("Usuario", 130)),
                ("folder", ("Carpeta", 180)),
                ("speed",  ("Vel",     70))]:
            self.results_tree.heading(c, text=t, command=_r_sorter(c))
            self.results_tree.column(c, width=w, minwidth=max(40, w // 2),
                                    anchor=tk.W, stretch=False)
        r_btns = ttk.Frame(r_frame)
        r_btns.pack(fill=tk.X, pady=(4, 0))
        icon_button(r_btns, "⬇", "Descargar los resultados seleccionados",
                    self._download_selected_results, side=tk.LEFT)
        icon_button(r_btns, "↻", "Recargar los resultados",
                    self._on_search_selected, side=tk.LEFT, padx=4)
        self.results_tree.bind("<Double-1>",
                               lambda _e: self._download_selected_results())

        # --- Descargas (abajo, ocupa el segundo pane vertical) ---
        d_frame = ttk.LabelFrame(main, text="Descargas", padding=6)
        main.add(d_frame, weight=1)
        d_head = ttk.Frame(d_frame)
        d_head.pack(fill=tk.X)
        self._font_controls(d_head, lambda: self.dl_tree, "activity_dl")
        d_cols = ("num", "file", "user", "state", "queue",
                  "progress", "size")
        d_wrap, self.dl_tree = make_scrollable_tree(
            d_frame, columns=d_cols, show="headings",
            selectmode="extended", height=6,
        )
        d_wrap.pack(fill=tk.BOTH, expand=True)
        # Las filas "fantasma" (búsquedas que no bajaron nada) en gris.
        self.dl_tree.tag_configure("virtual", foreground="#9a9a9a")
        for c, (t, w) in [
                ("num",      ("#",       40)),
                ("file",     ("Fichero", 320)),
                ("user",     ("Usuario", 140)),
                ("state",    ("Estado",  180)),
                ("queue",    ("Cola",    50)),
                ("progress", ("%",       60)),
                ("size",     ("Tamaño",  90))]:
            self.dl_tree.heading(c, text=t)
            self.dl_tree.column(c, width=w, minwidth=max(40, w // 2),
                                anchor=tk.W, stretch=False)
        self.dl_tree.bind("<<TreeviewSelect>>",
                          lambda _e: self._sync_search_from_dl())
        d_btns = ttk.Frame(d_frame)
        d_btns.pack(fill=tk.X, pady=(4, 0))
        icon_button(d_btns, "✕", "Cancelar la descarga seleccionada",
                    lambda: self.on_cancel_download(remove=False),
                    side=tk.LEFT)
        icon_button(d_btns, "🗑", "Cancelar y quitarla de la lista",
                    lambda: self.on_cancel_download(remove=True),
                    side=tk.LEFT, padx=4)
        icon_button(d_btns, "✎", "Rebuscar esta con otra query",
                    self.on_retry_download, side=tk.LEFT, padx=4)
        icon_button(d_btns, "⟳", "Rebuscar TODAS las caídas "
                    "(canceladas y erroreadas)", self.on_requery_failed,
                    side=tk.LEFT, padx=4)
        icon_button(d_btns, "✅", "Quitar las completadas con éxito",
                    self.on_clear_downloads, side=tk.LEFT, padx=4)
        icon_button(d_btns, "🧹", "Vaciar la lista: quita todo lo terminado",
                    self.on_clear_dl_list, side=tk.LEFT, padx=4)
        ttk.Checkbutton(d_btns, text="Mostrar todas",
                        variable=self.show_all_dl_var,
                        command=self._on_show_all_dl).pack(side=tk.LEFT,
                                                           padx=(10, 0))
        icon_button(d_btns, "↻", "Refrescar", self._activity_tick,
                    side=tk.LEFT)

    def _build_downloads_tab(self, parent):
        """Pestaña con los ficheros que ya viven en downloads/.
        Independiente de Biblioteca — aquí ves solo lo que has bajado."""
        top = ttk.Frame(parent, padding=(8, 6, 8, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"📂 {DOWNLOADS_DIR}",
                  foreground="#555").pack(side=tk.LEFT)
        ttk.Button(top, text="↻ Refrescar",
                   command=self._refresh_downloads_tab).pack(side=tk.RIGHT,
                                                             padx=6)
        self._font_controls(top, lambda: self.dl_files_tree, "downloads")

        cols = ("ext", "size", "duration", "bitrate")
        wrap, self.dl_files_tree = make_scrollable_tree(
            parent, columns=cols, show="tree headings",
            selectmode="extended",
        )
        wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        self._dl_files_paths = {}   # iid -> Path
        self._dl_files_sort = {}

        def _sorter(col):
            def cb():
                desc = self._dl_files_sort.get(col, False)
                sort_treeview_by_column(self.dl_files_tree, col,
                                        descending=desc)
                self._dl_files_sort[col] = not desc
            return cb

        self.dl_files_tree.heading("#0", text="Fichero", command=_sorter("#0"))
        self.dl_files_tree.column("#0", width=460, minwidth=200,
                                  anchor=tk.W, stretch=False)
        for c, (t, w) in [("ext", ("Ext", 60)),
                          ("size", ("MB", 80)),
                          ("duration", ("Duración", 90)),
                          ("bitrate", ("kbps", 80))]:
            self.dl_files_tree.heading(c, text=t, command=_sorter(c))
            self.dl_files_tree.column(c, width=w, minwidth=max(40, w // 2),
                                      anchor=tk.W, stretch=False)
        self.dl_files_tree.bind("<Double-1>",
                                lambda _e: self._play_downloads_selected())

        # Fila 1: playback + acciones habituales (compactas)
        r1 = ttk.Frame(parent, padding=(8, 0, 8, 2))
        r1.pack(fill=tk.X)
        icon_button(r1, "▶", "Reproducir", self._play_downloads_selected,
                    side=tk.LEFT)
        icon_button(r1, "⏹", "Parar", self.on_stop_playback,
                    side=tk.LEFT, padx=(4, 8))
        ttk.Separator(r1, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        icon_button(r1, "🪄", "Sustituir el original por esta descarga "
                    "(automático si se sabe cuál es)",
                    self._substitute_from_downloads_tab, side=tk.LEFT)
        icon_button(r1, "📁", "Mostrar en el Finder",
                    self._reveal_downloads_selected, side=tk.LEFT, padx=6)

        # Fila 2: acciones destructivas separadas visualmente
        r2 = ttk.Frame(parent, padding=(8, 2, 8, 8))
        r2.pack(fill=tk.X)
        icon_button(r2, "🗑", "Borrar del disco las seleccionadas",
                    self._delete_downloads_selected, side=tk.LEFT)
        icon_button(r2, "💣", "Borrar del disco TODA la carpeta de descargas",
                    self._delete_all_downloads, side=tk.LEFT, padx=6)
        icon_button(r2, "🧹", "Limpiar los ficheros a medio descargar",
                    self._clean_incomplete, side=tk.LEFT, padx=6)

        self._bind_context_menu(self.dl_files_tree,
                                self._build_downloads_context)

    def _refresh_downloads_tab(self):
        """Refresca la pestaña Descargas. Escanea disco + lee metadata en
        un hilo — clickar la pestaña ya no bloquea Tk."""
        # Limpieza inmediata en Tk
        self.dl_files_tree.delete(*self.dl_files_tree.get_children())
        self._dl_files_paths.clear()
        if not DOWNLOADS_DIR.exists():
            return
        # Evita solapes si el usuario click-click en la pestaña
        if getattr(self, "_refreshing_downloads", False):
            return
        self._refreshing_downloads = True
        log.info("refresh_downloads_tab: iniciando scan de %s", DOWNLOADS_DIR)
        threading.Thread(
            target=self._downloads_scan_worker,
            daemon=True,
        ).start()

    def _downloads_scan_worker(self):
        t0 = time.time()
        rows = []
        try:
            for p in sorted(DOWNLOADS_DIR.rglob("*")):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                try:
                    meta = get_metadata(str(p))
                    size_mb = p.stat().st_size / 1024 / 1024
                except Exception:
                    continue
                dur = meta.get("duration_s") or 0
                dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
                br = meta.get("bitrate_kbps")
                rows.append((
                    p,
                    str(p.relative_to(DOWNLOADS_DIR)),
                    (p.suffix.lstrip(".").upper(),
                     f"{size_mb:.1f}", dur_str,
                     f"{br}" if br else "?"),
                ))
        except Exception as e:
            log.exception("downloads scan failed")
        log.info("downloads scan: %d files, %.2fs",
                 len(rows), time.time() - t0)
        self.after(0, self._downloads_populate, rows)

    def _downloads_populate(self, rows):
        # Inserta en Tk en lotes pequeños con `after` para no bloquear
        # (5000 inserts secuenciales bloquean, en lotes de 50 el UI
        # sigue respondiendo).
        BATCH = 50
        def insert_batch(idx):
            end = min(idx + BATCH, len(rows))
            for i in range(idx, end):
                p, name, vals = rows[i]
                iid = self.dl_files_tree.insert(
                    "", tk.END, text=name, values=vals)
                self._dl_files_paths[iid] = p
            if end < len(rows):
                self.after(1, insert_batch, end)
            else:
                self._refreshing_downloads = False
        if rows:
            insert_batch(0)
        else:
            self._refreshing_downloads = False

    def _play_downloads_selected(self):
        sel = self.dl_files_tree.selection()
        if not sel:
            return
        p = self._dl_files_paths.get(sel[0])
        if p:
            self.play_path(str(p), p.name)

    def _delete_downloads_selected(self):
        sel = list(self.dl_files_tree.selection())
        if not sel:
            return
        paths = [self._dl_files_paths[i] for i in sel
                 if i in self._dl_files_paths]
        if not messagebox.askyesno(
            "Maceta",
            f"Enviar a la papelera {len(paths)} fichero(s) de downloads/?\n\n"
            "Van a la papelera del sistema: recuperables desde ahí.",
        ):
            return
        fallos = 0
        for p in paths:
            if not to_trash(p):
                fallos += 1
        if fallos:
            self.status.set(f"{fallos} no se pudieron enviar a la papelera")
        self._refresh_downloads_tab()

    def _clean_incomplete(self):
        """Borra el contenido de downloads/.incomplete/ (ficheros parciales
        que dejó slskd al fallar/cancelar descargas)."""
        inc = DOWNLOADS_DIR / ".incomplete"
        if not inc.exists():
            messagebox.showinfo(
                "Maceta",
                f"No hay carpeta {inc} — nada que limpiar.",
            )
            return
        # Contar tamaño total para dar contexto
        total_bytes = 0
        total_files = 0
        for p in inc.rglob("*"):
            if p.is_file():
                try:
                    total_bytes += p.stat().st_size
                    total_files += 1
                except Exception:
                    pass
        if total_files == 0:
            messagebox.showinfo(
                "Maceta", f"{inc} ya está vacía.")
            return
        size_mb = total_bytes / 1024 / 1024
        if not messagebox.askyesno(
            "Maceta",
            f"Borrar {total_files} fichero(s) parciales ({size_mb:.1f} MB) "
            f"de\n{inc}\n\n"
            "Descargas en curso podrían fallar. Recomendado: primero "
            "cancela cualquier cosa en Actividad → Descargas.",
        ):
            return
        errors = 0
        for entry in inc.iterdir():
            if not to_trash(entry):
                errors += 1
        if errors:
            self.status.set(
                f"Limpiado con {errors} error(es) — algunos ficheros pueden "
                "estar en uso por slskd")
        else:
            self.status.set(
                f"Limpiado: {total_files} fichero(s) ({size_mb:.1f} MB)")

    def _reveal_downloads_selected(self):
        sel = self.dl_files_tree.selection()
        if not sel:
            return
        p = self._dl_files_paths.get(sel[0])
        if not p:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])

    def _delete_all_downloads(self):
        """Vacía downloads/ borrando del disco de verdad. A diferencia de
        una sustitución, esto NO pasa por archivo/ — el usuario lo quiere
        definitivo. Incluye los parciales de .incomplete/."""
        if not DOWNLOADS_DIR.exists():
            messagebox.showinfo("Maceta", f"{DOWNLOADS_DIR} no existe.")
            return
        files, total = [], 0
        for p in DOWNLOADS_DIR.rglob("*"):
            if not p.is_file() or p.name == DOWNLOAD_LINKS_PATH.name:
                continue
            files.append(p)
            try:
                total += p.stat().st_size
            except Exception:
                pass
        if not files:
            messagebox.showinfo("Maceta", f"{DOWNLOADS_DIR} ya está vacía.")
            return
        size_mb = total / 1024 / 1024
        if not messagebox.askyesno(
            "Maceta",
            f"Borrar del disco TODO el contenido de downloads/?\n\n"
            f"{len(files)} fichero(s) · {size_mb:.1f} MB\n{DOWNLOADS_DIR}\n\n"
            "Incluye los parciales de .incomplete/. No pasan por archivo/: "
            "esto NO se puede recuperar.",
        ):
            return
        self.status.set(f"Borrando {len(files)} fichero(s)…")
        log.warning("BORRAR TODO confirmado: %d ficheros (%.1f MB) en %s",
                    len(files), size_mb, DOWNLOADS_DIR)

        def worker():
            errors = 0
            for p in files:
                if not to_trash(p):
                    errors += 1
            # Carpetas vacías que queden: a la papelera también, sin
            # forzar (si algo quedó dentro, se respeta).
            dirs = [d for d in DOWNLOADS_DIR.rglob("*") if d.is_dir()]
            for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
            prune_download_links()
            msg = (f"Borrados {len(files) - errors} fichero(s) "
                   f"({size_mb:.1f} MB)")
            if errors:
                msg += f" · {errors} en uso o protegido(s)"
            log.warning("BORRAR TODO hecho: %d borrados, %d con error",
                        len(files) - errors, errors)
            self.after(0, self.status.set, msg)
            self.after(0, self._refresh_downloads_tab)
        threading.Thread(target=worker, daemon=True,
                         name="delete-all-downloads").start()

    def _substitute_from_downloads_tab(self):
        """Sustituye los originales de las descargas seleccionadas sin
        preguntar cuando se puede saber a quién reemplazan: primero por el
        link que se guardó al bajarlas, y si no hay link comparando el
        audio contra Biblioteca. Solo abre diálogo cuando no hay forma."""
        sel = list(self.dl_files_tree.selection())
        if not sel:
            messagebox.showinfo("Maceta",
                                "Selecciona una descarga primero.")
            return
        paths = [self._dl_files_paths[i] for i in sel
                 if i in self._dl_files_paths]
        if not paths:
            return
        if not self.auto_substitute_var.get():
            for p in paths:
                SubstituteTargetDialog(self, p)
            return
        self.status.set(f"Identificando {len(paths)} descarga(s)…")
        threading.Thread(target=self._auto_sub_worker, args=(paths,),
                         daemon=True, name="auto-substitute").start()

    def _iid_for_path(self, path):
        """iid de la fila de Biblioteca que vive en esa ruta, o None."""
        want = str(Path(path or ""))
        if not want:
            return None
        for iid, row in list(self.rows_by_iid.items()):
            if str(Path(row.get("path") or "")) == want:
                return iid
        return None

    def _auto_sub_worker(self, paths):
        done = asked = failed = 0
        for p in paths:
            link = get_download_link(p.name)
            orig = (link.get("original_path") or "").strip()
            if orig and Path(orig).exists():
                if self._apply_substitution(orig, p, "link"):
                    done += 1
                else:
                    failed += 1
                continue
            cand = self._best_audio_match(p)
            if cand is not None:
                if self._apply_substitution(cand[0], p, cand[1]):
                    done += 1
                else:
                    failed += 1
                continue
            asked += 1
            self.after(0, lambda pp=p: SubstituteTargetDialog(self, pp))
        parts = [f"{done} sustituida(s) sola(s)"]
        if asked:
            parts.append(f"{asked} sin identificar — elige a mano")
        if failed:
            parts.append(f"{failed} con error")
        self.after(0, self.status.set, " · ".join(parts))

    def _best_audio_match(self, downloaded):
        """Qué canción de Biblioteca reemplaza esta descarga. Preselecciona
        por parecido de nombre (comparar audio es caro) y lo confirma con
        songs_match. Devuelve (original_path, motivo) o None."""
        ranked = []
        for _iid, row in list(self.rows_by_iid.items()):
            sim = name_similarity(downloaded.stem, row.get("name") or "")
            if sim >= 0.5:
                ranked.append((sim, row))
        ranked.sort(key=lambda t: -t[0])
        for _sim, row in ranked[:10]:
            path = row.get("path") or ""
            if not path or not Path(path).exists():
                continue
            match, _s, reason = songs_match(path, str(downloaded))
            if match:
                return path, reason
        return None

    def _apply_substitution(self, original_path, downloaded, reason):
        """Mueve el original a archivo/, pone la descarga en su sitio y
        refresca la fila de Biblioteca si está cargada."""
        iid = self._iid_for_path(original_path)
        row = self.rows_by_iid.get(iid) if iid else None
        if row is not None:
            self.after(0, self.do_manual_substitute, iid, row,
                       str(downloaded), reason)
            return True
        try:
            new_path = substitute_file(original_path, str(downloaded))
        except Exception as e:
            log.warning("auto-substitute %s falló: %s", downloaded.name, e)
            self.after(0, self.status.set,
                       f"No se pudo sustituir {downloaded.name}: {e}")
            return False
        index_mark_substituted(str(new_path), reason)
        remove_download_link(downloaded.name)
        self.after(0, self.status.set,
                   f"✓ {new_path.name} sustituido ({reason})")
        self.after(0, self._refresh_downloads_tab)
        return True

    def _build_downloads_context(self, iid):
        m = tk.Menu(self, tearoff=0)
        has = bool(iid)
        m.add_command(label="▶ Reproducir",
                      command=self._play_downloads_selected,
                      state=("normal" if has else "disabled"))
        m.add_command(label="🪄 Sustituir original (auto)",
                      command=self._substitute_from_downloads_tab,
                      state=("normal" if has else "disabled"))
        m.add_command(label="🔍 Encontrar en Biblioteca",
                      command=self._find_from_downloads_tab,
                      state=("normal" if has else "disabled"))
        m.add_separator()
        m.add_command(label="Mostrar en Finder",
                      command=self._reveal_downloads_selected,
                      state=("normal" if has else "disabled"))
        m.add_command(label="🗑 Borrar del disco",
                      command=self._delete_downloads_selected,
                      state=("normal" if has else "disabled"))
        return m

    def _on_tab_changed(self, _event=None):
        try:
            tab_text = self.notebook.tab(self.notebook.select(), "text")
        except Exception:
            return
        log.debug("tab change → %s", tab_text)
        if tab_text == "Descargas":
            self._refresh_downloads_tab()

    def _apply_filter(self):
        """Filtra combinando bucket + wildcard + toggle 'pdtes. sustituir'.

        Optimización: sólo hace `reattach`/`detach` para filas cuya
        visibilidad **ha cambiado** desde el último apply. Con 3000 filas
        que no cambian, hace 0 llamadas al tree.
        """
        t0 = time.time()
        anchor = (self.tree.selection() or [None])[0]
        yview0 = self.tree.yview()[0]
        raw = self.filter_var.get().strip()
        if not raw:
            pattern = ""
            self.filter_effective_var.set("(sin filtro)")
        elif "*" in raw or "?" in raw:
            pattern = raw
            self.filter_effective_var.set(f"→ {pattern}")
        else:
            pattern = "*" + "*".join(raw.split()) + "*"
            self.filter_effective_var.set(f"→ {pattern}")

        pattern_lc = pattern.lower() if pattern else ""
        pending_only = self.filter_pending_sub_var.get()
        bucket = self.active_bucket.get()
        counts = {b: 0 for b in BUCKETS}
        visible = 0
        prev_hidden = getattr(self, "_hidden_iids", set())
        now_hidden = set()

        for iid, row in self.rows_by_iid.items():
            # Fuera de la carpeta elegida no cuenta ni para los chips:
            # así los contadores son las estadísticas de esa carpeta.
            if not self._in_folder_filter(row):
                now_hidden.add(iid)
                if iid not in prev_hidden:
                    try:
                        self.tree.detach(iid)
                    except Exception:
                        pass
                continue
            b = row_bucket(row)
            counts[b] += 1
            counts["todas"] += 1

            match = (not pattern_lc) or fnmatch.fnmatch(
                row["name"].lower(), pattern_lc)
            if pending_only:
                slsk = (row.get("slsk_status") or "").lower()
                match = match and "no coincide" in slsk
            if bucket != "todas":
                match = match and b == bucket

            if match:
                visible += 1
                if iid in prev_hidden:
                    try:
                        self.tree.reattach(iid, "", tk.END)
                    except Exception:
                        pass
            else:
                now_hidden.add(iid)
                if iid not in prev_hidden:
                    try:
                        self.tree.detach(iid)
                    except Exception:
                        pass
        self._hidden_iids = now_hidden

        total = counts["todas"]
        for key, base in CHIP_DEFS:
            btn = self.chip_labels.get(key)
            if btn is None:
                continue
            n = counts[key]
            if key == "todas" or not total:
                btn.configure(text=f"{base} ({n})")
            else:
                btn.configure(text=f"{base} ({n} · {100 * n / total:.0f}%)")

        if self.folder_filter:
            self.status.set(
                f"📂 {Path(self.folder_filter).name}: {total} temas · "
                f"{visible} visibles con los filtros actuales")
        elif pattern or pending_only or bucket != "todas":
            self.status.set(
                f"Filtro: {visible}/{len(self.rows_by_iid)} filas visibles"
            )
        self._update_scope_labels(visible)
        self._restore_view(anchor, yview0)
        elapsed = time.time() - t0
        if elapsed > 0.1:
            log.info("apply_filter: %d rows en %.2fs", len(self.rows_by_iid),
                     elapsed)

    def _reflow_chips(self, event=None):
        """Con los contadores puestos, los chips piden más ancho del que
        tiene la ventana por defecto. Si no caben en una fila, se parten
        en dos; si caben, vuelven a una."""
        btns = [self.chip_labels[k] for k, _ in CHIP_DEFS
                if k in self.chip_labels]
        if not btns:
            return
        avail = event.width if event is not None \
            else self._chips_frame.winfo_width()
        needed = sum(b.winfo_reqwidth() + 10 for b in btns)
        cols = len(btns) if needed <= avail else (len(btns) + 1) // 2
        if cols == self._chips_cols:
            return
        self._chips_cols = cols
        for i, b in enumerate(btns):
            b.grid(row=i // cols, column=i % cols, sticky="w", padx=(0, 10))

    def _update_scope_labels(self, visible):
        """Que el botón diga a cuántas va a afectar, no 'todas' a secas."""
        if not hasattr(self, "analyze_btn_var"):
            return
        if self.folder_filter or self.active_bucket.get() != "todas" \
                or self.filter_var.get().strip():
            analizar = f"Analizar estas {visible} ({self._scope_label()})"
            fakes = f"Buscar en Soulseek las FAKE de {self._scope_label()}"
        else:
            analizar = f"Analizar todas ({visible})"
            fakes = "Buscar en Soulseek todas las FAKE"
        self.analyze_btn_var.set(analizar)
        self.fakes_btn_var.set(fakes)
        for btn, texto in ((getattr(self, "_analyze_btn", None), analizar),
                           (getattr(self, "_fakes_btn", None), fakes)):
            if btn is not None and getattr(btn, "_tip", None):
                btn._tip.set_text(texto)

    def _restore_view(self, anchor, yview0):
        """Deja la vista de Biblioteca donde estaba: pegada a la fila
        marcada si sigue visible, o en la misma fracción de scroll si no
        hay marca. Sin esto, filtrar o analizar arrastraba la tabla."""
        hidden = getattr(self, "_hidden_iids", set())
        try:
            if anchor and anchor not in hidden and self.tree.exists(anchor):
                self.tree.see(anchor)
            else:
                self.tree.yview_moveto(yview0)
        except Exception:
            pass

    def _apply_colors(self):
        c = self.colors
        # Banner
        for w in self._themed_widgets.get("banner", []):
            try: w.configure(bg=c["banner_bg"])
            except Exception: pass
            try: w.configure(fg=c["banner_fg"])
            except Exception: pass
        # Tree principal
        style = ttk.Style(self)
        try:
            style.configure("Treeview",
                            background=c["tree_bg"],
                            foreground=c["tree_fg"],
                            fieldbackground=c["tree_bg"])
            style.map("Treeview",
                      background=[("selected", c["tree_selected_bg"])],
                      foreground=[("selected", c["tree_selected_fg"])])
        except Exception:
            pass
        # Tags de veredicto en el tree principal
        for verdict, key in [("OK", "verdict_OK"),
                             ("LOSSLESS", "verdict_LOSSLESS"),
                             ("FAKE", "verdict_FAKE"),
                             ("ERROR", "verdict_ERROR"),
                             ("PENDING", "verdict_PENDING"),
                             ("?", "verdict_ERROR")]:
            try: self.tree.tag_configure(verdict, foreground=c[key])
            except Exception: pass
        # Now playing + status

        try: self._status_label.configure(foreground=c["status_fg"])
        except Exception: pass

    def on_colors_config(self):
        ColorEditorDialog(self)

    def on_banned_peers(self):
        BannedPeersDialog(self)

    def on_soulseek_diag(self):
        SoulseekDiagDialog(self)

    def on_options(self, tab=None):
        OptionsDialog(self, tab=tab)

    def on_manual(self):
        ManualDialog(self)

    def _select_all_tree(self, _event=None):
        if self.focus_get() is self.tree:
            self.tree.selection_set(self.tree.get_children(""))
            return "break"

    def _apply_compact_cols(self):
        """Compacto: una celda `● 320` en vez de Formato+Bitrate+SR."""
        compacto = bool(self.compact_cols_var.get())
        cols = list(self.tree.cget("columns"))
        if compacto:
            visibles = [c for c in cols if c not in ("codec", "bitrate", "sr")]
        else:
            visibles = [c for c in cols if c != "tec"]
        try:
            self.tree.configure(displaycolumns=visibles)
        except Exception:
            pass
        save_user_state({"compact_cols": compacto})

    def _sort_by(self, col):
        rows = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]
        def keyfn(t):
            v = t[0]
            try:
                return (0, float(v.split()[0]))
            except (ValueError, IndexError):
                return (1, v.lower())
        rows.sort(key=keyfn)
        for i, (_, iid) in enumerate(rows):
            self.tree.move(iid, "", i)

    def on_pick(self):
        d = filedialog.askdirectory(
            title="Elige la carpeta con las canciones",
            initialdir=self._last_browse_dir())
        if not d:
            return
        self.open_folder(d)

    def _last_browse_dir(self) -> str:
        """Dónde abrir el diálogo: la carpeta en uso, si no la última
        reciente que siga existiendo, y si no el home."""
        for cand in [self.folder.get()] + list(
                self._user_state.get("recent_folders") or []):
            if cand and os.path.isdir(cand):
                return cand
        return str(Path.home())

    def _scan_folder(self, folder, gen=None):
        t0 = time.time()
        if gen is None:
            self._scan_gen = getattr(self, "_scan_gen", 0) + 1
            gen = self._scan_gen
        log.info("scan_folder start: %s", folder)
        files = [
            p for p in sorted(Path(folder).rglob("*"))
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        ]
        if not files:
            self.after(0, lambda: messagebox.showinfo(
                "Maceta", "No hay audio compatible en esa carpeta."))
            self.after(0, self.status.set, "Sin archivos compatibles")
            return
        total = len(files)
        # Trabajo pesado (mutagen) en este hilo. Insertamos en Tk por
        # lotes para no ahogar la queue de after().
        BATCH = 50
        batch = []
        cached = 0
        use_index = self.use_index_var.get()
        for i, p in enumerate(files, 1):
            row = scan_row(str(p))
            if use_index and hydrate_from_index(row):
                cached += 1
            batch.append(row)
            if gen != getattr(self, "_scan_gen", gen):
                log.info("scan_folder %s cancelado: hay otro más reciente",
                         folder)
                return
            if len(batch) >= BATCH or i == total:
                self.after(0, self._insert_rows_batch, list(batch), i, total,
                           gen)
                batch.clear()
        log.info("scan_folder done: %d files in %.2fs (%d desde el índice)",
                 total, time.time() - t0, cached)
        save_index()
        if gen == getattr(self, "_scan_gen", gen):
            self.after(0, self._scan_done, total, cached)

    def _insert_rows_batch(self, rows, done, total, gen=None):
        if gen is not None and gen != getattr(self, "_scan_gen", gen):
            return
        for row in rows:
            tag = row["verdict"] if row["verdict"] else "PENDING"
            iid = self.tree.insert(
                "", tk.END, text=row["name"],
                values=self._row_values(row), tags=(tag,),
            )
            self.rows_by_iid[iid] = row
        self.progress.set(done / total * 100)
        self.status.set(f"Listando {done}/{total}")
        # Sólo un refresh de filtro al final de cada batch
        self._schedule_filter_refresh()
        self._schedule_folder_refresh()

    def _scan_done(self, n, cached=0):
        self.progress.set(0)
        self.status.set(
            f"{n} archivos listados" + (f" · {cached} ya analizados (índice)" if cached else "") + " — selecciona y pulsa Analizar "
            "(Cmd/Ctrl+A para todas)"
        )

    def _row_values(self, row):
        dash = "—"
        return (
            tech_badge(row),
            row["codec"],
            f"{row['bitrate_kbps']} kbps" if row["bitrate_kbps"] else "?",
            f"{row['sample_rate']} Hz" if row["sample_rate"] else "?",
            self._fmt_dur(row["duration_s"]),
            f"{row['bpm']:.1f}" if row["bpm"] else dash,
            f"{row['cutoff_hz']} Hz" if row["cutoff_hz"] else dash,
            display_verdict(row),
            row["detail"],
            row.get("slsk_status") or dash,
        )

    def _insert_row(self, row):
        tag = row["verdict"] if row["verdict"] else "PENDING"
        iid = self.tree.insert(
            "", tk.END, text=row["name"], values=self._row_values(row), tags=(tag,)
        )
        self.rows_by_iid[iid] = row
        self._schedule_filter_refresh()

    def _schedule_filter_refresh(self):
        """Debounced refresh de chips + filtro. Coalesce hasta ~250 ms."""
        if getattr(self, "_filter_refresh_id", None):
            return
        self._filter_refresh_id = self.after(250, self._do_filter_refresh)

    def _do_filter_refresh(self):
        self._filter_refresh_id = None
        try:
            self._apply_filter()
        except Exception:
            pass

    def on_analyze_selected(self):
        iids = list(self.tree.selection())
        if not iids:
            messagebox.showinfo(
                "Maceta",
                "Selecciona una o varias canciones (Cmd/Ctrl+click, Shift+click, "
                "o Cmd/Ctrl+A para todas)."
            )
            return
        self._start_analysis(iids)

    def _scope_iids(self) -> list:
        """Las filas sobre las que actúan los botones de abajo.

        Es **lo que estás viendo**: si hay una carpeta elegida en el
        explorador (o un chip, o un filtro de texto), los botones operan
        solo sobre eso. Actuar sobre las 11.000 de la biblioteca cuando
        tienes abierta una carpeta de 3 no es lo que nadie espera."""
        oculta = getattr(self, "_hidden_iids", set())
        return [i for i in self.rows_by_iid if i not in oculta]

    def _scope_label(self) -> str:
        if self.folder_filter:
            return f"📂 {Path(self.folder_filter).name}"
        bucket = self.active_bucket.get()
        if bucket != "todas":
            return dict(CHIP_DEFS).get(bucket, bucket)
        if self.filter_var.get().strip():
            return "el filtro actual"
        return "toda la biblioteca"

    def on_analyze_all(self):
        iids = self._scope_iids()
        if not iids:
            messagebox.showinfo("Maceta", "Primero selecciona una carpeta.")
            return
        self.status.set(f"Analizando {self._scope_label()}…")
        self._start_analysis(iids)

    def on_reanalyze(self):
        """Re-analiza ignorando el índice. Sobre la selección si hay, y
        si no sobre toda la lista previa confirmación."""
        iids = list(self.tree.selection())
        if not iids:
            iids = self._scope_iids()
            if not iids:
                messagebox.showinfo("Maceta", "Primero selecciona una carpeta.")
                return
            if not messagebox.askyesno(
                "Maceta",
                f"No hay nada seleccionado.\n\n¿Re-analizar las {len(iids)} "
                f"canciones de {self._scope_label()} desde cero?\n\n"
                "Se ignora el índice: volverá a decodificar cada fichero.",
            ):
                return
        self._start_analysis(iids, force=True)

    def _start_analysis(self, iids, force=False):
        """Analiza las filas pedidas. Sin `force`, las que ya tienen
        análisis válido (en memoria o en el índice) se saltan: con una
        biblioteca grande volver a escanearla entera eran minutos."""
        pending, skipped = [], 0
        for iid in iids:
            row = self.rows_by_iid.get(iid)
            if row is None:
                continue
            if force:
                row.pop("from_index", None)
                pending.append((iid, row))
                continue
            if row.get("analyzed"):
                skipped += 1
                continue
            if self.use_index_var.get() and hydrate_from_index(row):
                skipped += 1
                self._update_row_quiet(iid, row)
                continue
            pending.append((iid, row))

        if not pending:
            self.status.set(
                f"Nada que analizar: {skipped} ya analizada(s). "
                "Usa 🔄 Re-analizar para forzar.")
            return
        self.progress.set(0)
        extra = f" ({skipped} ya analizadas)" if skipped else ""
        self.status.set(f"Analizando {len(pending)} archivos{extra}…")
        threading.Thread(
            target=self._run_analysis, args=(pending,), daemon=True
        ).start()

    def _update_row_quiet(self, iid, row):
        """Repinta una fila sin tocar progreso ni estado."""
        if iid not in self.rows_by_iid or not self.tree.exists(iid):
            return
        self.tree.item(iid, values=self._row_values(row),
                       tags=(row["verdict"] or "PENDING",))

    def _run_analysis(self, items):
        total = len(items)
        max_workers = min(4, max(1, (os.cpu_count() or 2) - 1))

        def worker(iid, row):
            self.after(0, self._mark_analyzing, iid, row)
            return iid, analyze_row(row)

        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(worker, iid, row): iid for iid, row in items}
            for fut in as_completed(futures):
                iid = futures[fut]
                try:
                    _, row = fut.result()
                except Exception as e:
                    row = self.rows_by_iid[iid]
                    row["verdict"] = "ERROR"
                    row["detail"] = str(e)[:160]
                    row["analyzed"] = True
                self.rows_by_iid[iid] = row
                if row.get("analyzed") and row.get("verdict") != "ERROR":
                    index_put(row)
                done += 1
                self.after(0, self._update_row, iid, row, done, total)
        save_index()
        self.after(0, self._analysis_done)

    def _mark_analyzing(self, iid, row):
        if iid not in self.rows_by_iid or not self.tree.exists(iid):
            return
        preview = dict(row)
        preview["verdict"] = "analizando…"
        self.tree.item(iid, values=self._row_values(preview), tags=("PENDING",))
        self.status.set(f"Analizando: {row['name']}")

    def _update_row(self, iid, row, done, total):
        if iid not in self.rows_by_iid or not self.tree.exists(iid):
            self.progress.set(done / total * 100)
            return
        tag = row["verdict"] if row["verdict"] else "PENDING"
        self.tree.item(iid, values=self._row_values(row), tags=(tag,))
        self.progress.set(done / total * 100)
        self.status.set(
            f"Analizadas {done}/{total} — última: {row['name']} → {row['verdict']}"
        )
        self._schedule_filter_refresh()
        # Los contadores del explorador van bajando a la vez que avanza
        # el análisis, en vez de quedarse congelados hasta el final.
        self._schedule_folder_refresh(rebuild=False)

    @staticmethod
    def _fmt_dur(s):
        if not s:
            return "?"
        m, sec = divmod(int(s), 60)
        return f"{m}:{sec:02d}"

    def _schedule_folder_refresh(self, rebuild=True):
        """Repintar el árbol por cada fila analizada sería absurdo."""
        if getattr(self, "_folder_job", None):
            try:
                self.after_cancel(self._folder_job)
            except Exception:
                pass
        cb = self._rebuild_folder_tree if rebuild else self._refresh_folder_counts
        self._folder_job = self.after(700, cb)

    def _analysis_done(self):
        analyzed = [r for r in self.rows_by_iid.values() if r.get("analyzed")]
        total = len(self.rows_by_iid)
        n = len(analyzed)
        fakes = sum(1 for r in analyzed if r["verdict"] == "FAKE")
        pct = f" ({100 * n / total:.0f}%)" if total else ""
        fake_pct = f" ({100 * fakes / n:.0f}% de las analizadas)" if n else ""
        self.status.set(
            f"Analizadas {n}/{total}{pct} · {fakes} fake{fake_pct}"
        )
        self.progress.set(100)
        self._rebuild_folder_tree()

    # ---------- Poller de actividad Soulseek ----------

    def _start_activity_poller(self):
        self._activity_search_iids = {}  # search_id -> tree iid en search_tree
        self._activity_dl_iids = {}      # (username, dl_id) -> tree iid en dl_tree
        self._activity_search_vals = {}  # iid -> last rendered tuple
        self._activity_dl_vals = {}
        self.after(2000, self._activity_tick)
        self.after(15000, self._auto_retry_tick)

    # ---------- Auto-retry ----------

    # Estados de slskd que cuentan como descarga caída
    _FAILED_STATES = ("Cancelled", "Errored", "TimedOut", "Rejected")
    _RETRY_TRIGGERS = ("sin resultados", "calidad baja",
                       "sin match", "error", "errored")
    _RETRY_STOP = ("↓", "✓", "buscando", "verificando",
                   "descarg", "en cola", "convirtiendo")

    def _maybe_schedule_retry(self, iid, text):
        """Si el texto indica fallo y auto_retry está on, agenda un
        reintento tras el intervalo configurado. Si indica progreso/éxito,
        retira la entrada. Respeta el límite de intentos (0 = infinito)."""
        if not self.auto_retry_var.get():
            return
        low = (text or "").lower()
        if any(k in low for k in self._RETRY_STOP):
            self._retry_queue.pop(iid, None)
            return
        if any(k in low for k in self._RETRY_TRIGGERS):
            entry = self._retry_queue.setdefault(
                iid, {"attempts": 0, "next_at": 0.0})
            entry["attempts"] += 1
            interval_min = max(1, int(self.auto_retry_interval_min.get() or 5))
            max_attempts = max(0, int(self.auto_retry_max_attempts.get() or 0))
            row = self.rows_by_iid.get(iid)
            if max_attempts and entry["attempts"] > max_attempts:
                self._retry_queue.pop(iid, None)
                if row:
                    row["slsk_status"] = (
                        f"✗ agotado tras {max_attempts} reintentos"
                    )
                    if self.tree.exists(iid):
                        self.tree.item(
                            iid, values=self._row_values(row),
                            tags=(row["verdict"] or "PENDING",))
                self._flag_manual_needed(iid)
                return
            entry["next_at"] = time.time() + interval_min * 60
            if row:
                row["slsk_status"] = (
                    f"⟳ reprogramada en {interval_min}m "
                    f"(intento {entry['attempts']}"
                    f"{'/'+str(max_attempts) if max_attempts else ''})"
                )
                if self.tree.exists(iid):
                    self.tree.item(
                        iid, values=self._row_values(row),
                        tags=(row["verdict"] or "PENDING",))

    def _flag_manual_needed(self, iid):
        """Agotados los reintentos automáticos de una canción. Acumula y
        pregunta una sola vez por tanda, no una vez por canción."""
        self._manual_needed.add(iid)
        if self._manual_prompt_job is None:
            self._manual_prompt_job = self.after(3000,
                                                 self._ask_manual_needed)

    def _ask_manual_needed(self):
        self._manual_prompt_job = None
        iids = [i for i in self._manual_needed if i in self.rows_by_iid]
        self._manual_needed.clear()
        if not iids:
            return
        names = sorted(self.rows_by_iid[i]["name"] for i in iids)
        shown = "\n".join(f"· {n}" for n in names[:10])
        if len(names) > 10:
            shown += f"\n… y {len(names) - 10} más"
        if not messagebox.askyesno(
            "Maceta",
            f"{len(names)} canción(es) no han bajado tras agotar los "
            f"reintentos automáticos:\n\n{shown}\n\n"
            "¿Quieres elegir la versión a mano ahora?",
        ):
            return
        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        pairs = [(i, self.rows_by_iid[i]) for i in iids]
        threading.Thread(target=self._manual_pick_worker,
                         args=(client, pairs), daemon=True,
                         name="manual-pick").start()

    def _manual_pick_worker(self, client, pairs):
        """Una pasada abriendo el picker por cada canción pendiente."""
        for iid, row in pairs:
            if iid not in self.rows_by_iid:
                continue
            query = build_query(row, self.query_mode_var.get())
            _ok, text = self._slsk_search_download(
                client, query, allow_picker=True, force_picker=True,
                link_iid=iid)
            self.after(0, self._set_slsk_status, iid, text)

    def _auto_retry_tick(self):
        try:
            if self.auto_retry_var.get() and self._retry_queue:
                now = time.time()
                due = [iid for iid, e in list(self._retry_queue.items())
                       if e.get("next_at", 0) <= now
                       and iid in self.rows_by_iid
                       and iid not in self.active_by_iid]
                if due:
                    client = self._slsk_client()
                    if client is not None:
                        candidates = []
                        for iid in due:
                            row = self.rows_by_iid.get(iid)
                            if row:
                                candidates.append((iid, row))
                                self._retry_queue.pop(iid, None)
                        if candidates:
                            self._launch_slsk_batch(client, candidates)
        finally:
            self.after(30_000, self._auto_retry_tick)

    def _activity_tick(self):
        client = self._slsk_client()
        if client is None:
            self.after(4000, self._activity_tick)
            return
        threading.Thread(
            target=self._activity_fetch, args=(client,), daemon=True
        ).start()

    def _activity_fetch(self, client):
        t0 = time.time()
        try:
            searches = client.list_searches()
        except Exception as e:
            log.warning("list_searches failed: %s", e)
            searches = []
        try:
            downloads = client.list_downloads()
        except Exception as e:
            log.warning("list_downloads failed: %s", e)
            downloads = []
        # Pre-compute lookups en disco EN ESTE HILO (no en Tk).
        # rglob() sobre downloads/ puede ser caro si hay muchos ficheros.
        local_lookup = {}
        if DOWNLOADS_DIR.exists():
            for user_group in downloads:
                username = user_group.get("username", "")
                for directory in (user_group.get("directories") or []):
                    for f in (directory.get("files") or []):
                        state = str(f.get("state") or "")
                        if "Completed" not in state:
                            continue
                        fn = f.get("filename") or ""
                        key = (username, fn.lower())
                        if key in self.pending_dl_links:
                            local_lookup[key] = self._find_local_download(fn)
        log.debug("activity_fetch: %d searches, %d dl-groups, took %.2fs",
                  len(searches), len(downloads), time.time() - t0)
        self.after(0, self._activity_render, searches, downloads)
        self.after(0, self._activity_reconcile, downloads, local_lookup)
        self.after(4000, self._activity_tick)

    def _activity_render(self, searches, downloads):
        for ug in downloads:
            user = ug.get("username", "")
            for d in (ug.get("directories") or []):
                for f in (d.get("files") or []):
                    if "Succeeded" not in str(f.get("state", "")):
                        continue
                    n = self._dl_num.get(
                        (user, (f.get("filename") or "").lower()))
                    if n:
                        self._completed_nums.add(n)
        current_sids = set()
        for s in searches:
            sid = s.get("id")
            if not sid:
                continue
            current_sids.add(sid)
            n = self._search_num.get(sid, "")
            vals = (
                f"#{n}" if n else "",
                s.get("searchText", "?"),
                str(s.get("state", "")),
                str(s.get("responseCount", 0)),
                str(s.get("fileCount", 0)),
            )
            state_s = str(s.get("state", ""))
            rc = s.get("responseCount") or 0
            usable = self._search_usable.get(sid)
            completada = "Completed" in state_s
            tag2 = ""
            if n and n in self._completed_nums:
                tag2 = "done"          # ya bajada: manda sobre lo demás
            elif completada and rc == 0:
                tag2 = "nores"
            elif usable == 0 and (s.get("fileCount") or 0):
                tag2 = "filtered"
            elif completada and rc:
                # terminó, tiene respuestas y no has bajado nada: a elegir.
                tag2 = "pendiente"
            tags = (sid, tag2) if tag2 else (sid,)
            iid = self._activity_search_iids.get(sid)
            if iid and self.search_tree.exists(iid):
                if self._activity_search_vals.get(iid) != vals + (tag2,):
                    self.search_tree.item(iid, values=vals, tags=tags)
                    self._activity_search_vals[iid] = vals + (tag2,)
            else:
                iid = self.search_tree.insert(
                    "", tk.END, values=vals, tags=tags
                )
                self._activity_search_iids[sid] = iid
                self._activity_search_vals[iid] = vals + (tag2,)
        for sid in list(self._activity_search_iids.keys()):
            if sid not in current_sids:
                iid = self._activity_search_iids.pop(sid)
                self._activity_search_vals.pop(iid, None)
                if self.search_tree.exists(iid):
                    self.search_tree.delete(iid)

        current_keys = set()
        for user_group in downloads:
            username = user_group.get("username", "?")
            for directory in (user_group.get("directories") or []):
                for f in (directory.get("files") or []):
                    dl_id = f.get("id") or f.get("filename")
                    key = (username, dl_id)
                    current_keys.add(key)
                    fn = (f.get("filename") or "").replace("\\", "/")
                    name = fn.rsplit("/", 1)[-1]
                    pct = f.get("percentComplete") or 0
                    try:
                        pct = float(pct)
                    except Exception:
                        pct = 0.0
                    size = f.get("size") or 0
                    size_mb = f"{size/1024/1024:.1f} MB" if size else "?"
                    remote_fn_lc = (f.get("filename") or "").lower()
                    n = self._dl_num.get((username, remote_fn_lc), "")
                    place = f.get("placeInQueue")
                    place_str = "" if place in (None, 0) else str(place)
                    vals = (f"#{n}" if n else "", name, username,
                            str(f.get("state", "")),
                            place_str,
                            f"{pct:.0f}%", size_mb)
                    iid = self._activity_dl_iids.get(key)
                    if iid and self.dl_tree.exists(iid):
                        if self._activity_dl_vals.get(iid) != vals:
                            self.dl_tree.item(iid, values=vals)
                            self._activity_dl_vals[iid] = vals
                    else:
                        iid = self.dl_tree.insert(
                            "", tk.END, values=vals,
                            tags=(username, str(dl_id)),
                        )
                        self._activity_dl_iids[key] = iid
                        self._activity_dl_vals[iid] = vals
        if self.show_all_dl_var.get():
            current_keys |= self._render_missing_downloads(searches)
        for key in list(self._activity_dl_iids.keys()):
            if key not in current_keys:
                iid = self._activity_dl_iids.pop(key)
                self._activity_dl_vals.pop(iid, None)
                if self.dl_tree.exists(iid):
                    self.dl_tree.delete(iid)
        self._update_activity_stats(searches, downloads)

    def _render_missing_downloads(self, searches) -> set:
        """Añade una fila por cada búsqueda que no llegó a descargar nada,
        diciendo por qué. Sin esto, una búsqueda sin resultados
        simplemente no aparecía y parecía que se había perdido."""
        con_descarga = {
            n for n in (self._dl_num.get(k) for k in self._dl_num) if n}
        for iid in self.dl_tree.get_children(""):
            num = self._extract_num(self.dl_tree.set(iid, "num"))
            if num:
                con_descarga.add(num)
        keys = set()
        for s in searches:
            sid = s.get("id")
            n = self._search_num.get(sid)
            if not n or n in con_descarga:
                continue
            state = str(s.get("state", ""))
            rc = s.get("responseCount") or 0
            fc = s.get("fileCount") or 0
            if "Completed" not in state:
                motivo = "buscando…"
            elif rc == 0:
                motivo = "— sin resultados"
            elif self._search_usable.get(sid) == 0 and fc:
                motivo = f"— {fc} ficheros, ninguno pasa el filtro"
            else:
                motivo = "— sin descarga"
            key = ("virtual", n)
            keys.add(key)
            vals = (f"#{n}", s.get("searchText", "?"), "—", motivo,
                    "", "", "")
            iid = self._activity_dl_iids.get(key)
            if iid and self.dl_tree.exists(iid):
                if self._activity_dl_vals.get(iid) != vals:
                    self.dl_tree.item(iid, values=vals)
                    self._activity_dl_vals[iid] = vals
            else:
                iid = self.dl_tree.insert("", tk.END, values=vals,
                                          tags=("virtual",))
                self._activity_dl_iids[key] = iid
                self._activity_dl_vals[iid] = vals
        return keys

    def _update_activity_stats(self, searches, downloads):
        """Resumen de la pestaña, al estilo de los chips de Biblioteca."""
        total_s = len(searches)
        buscando = sum(1 for s in searches
                       if "Completed" not in str(s.get("state", "")))
        con_resp = sum(1 for s in searches if (s.get("responseCount") or 0))
        sin_nada = sum(1 for s in searches
                       if "Completed" in str(s.get("state", ""))
                       and not (s.get("responseCount") or 0))
        filtradas = sum(1 for s in searches
                        if self._search_usable.get(s.get("id")) == 0
                        and (s.get("fileCount") or 0))

        def pct(n, t):
            return f" ({100 * n / t:.0f}%)" if t else ""

        if total_s:
            self.act_search_stats.set(
                f"🔎 {total_s} búsquedas · ⏳ {buscando} en curso · "
                f"✅ {con_resp} con respuesta{pct(con_resp, total_s)} · "
                f"🚫 {sin_nada} sin nada · ⚠ {filtradas} filtradas")
        else:
            self.act_search_stats.set("🔎 sin búsquedas")

        bajando = encolados = hechas = fallidas = total_d = 0
        for ug in downloads:
            for d in (ug.get("directories") or []):
                for f in (d.get("files") or []):
                    st = str(f.get("state", ""))
                    total_d += 1
                    if "InProgress" in st:
                        bajando += 1
                    elif "Queued" in st or "Requested" in st:
                        encolados += 1
                    elif "Succeeded" in st:
                        hechas += 1
                    elif "Completed" in st:
                        fallidas += 1
        if total_d:
            self.act_dl_stats.set(
                f"📥 {total_d} descargas · ⬇ {bajando} bajando · "
                f"⏸ {encolados} en cola · ✅ {hechas}{pct(hechas, total_d)} · "
                f"✗ {fallidas} caídas")
        else:
            self.act_dl_stats.set("📥 sin descargas")

    def _activity_reconcile(self, downloads, local_lookup=None):
        """Cuando una descarga completa, actualiza el estado en Biblioteca
        y (si toca) sustituye el original.

        `local_lookup`: dict (username, filename_lc) -> Path o None,
        precalculado en el hilo del fetch para evitar `rglob()` en Tk.
        """
        local_lookup = local_lookup or {}
        for user_group in downloads:
            username = user_group.get("username", "")
            for directory in (user_group.get("directories") or []):
                for f in (directory.get("files") or []):
                    state = str(f.get("state") or "")
                    fn_remote = (f.get("filename") or "")
                    key = (username, fn_remote.lower())
                    orig_iid = self.pending_dl_links.get(key)
                    if not orig_iid:
                        continue
                    if key in self.finalized_dl_keys:
                        continue
                    if "Errored" in state or "Cancelled" in state:
                        self._set_slsk_status(orig_iid, f"✗ {state}")
                        self._release_active(orig_iid)
                        self.finalized_dl_keys.add(key)
                        if ("Cancelled" in state or "Rejected" in state) \
                                and key not in self._self_cancelled:
                            self._record_peer_cancel(username)
                    elif "Completed, Succeeded" in state or state == "Completed":
                        local = local_lookup.get(key)
                        if local is None:
                            self._set_slsk_status(orig_iid, "esperando fichero…")
                            continue
                        row = self.rows_by_iid.get(orig_iid)
                        self.finalized_dl_keys.add(key)
                        is_flac = local.suffix.lower() == ".flac"
                        if self.auto_flac_to_mp3_var.get() and is_flac:
                            self._set_slsk_status(orig_iid, "convirtiendo FLAC→MP3…")
                            threading.Thread(
                                target=self._auto_convert_and_maybe_sub,
                                args=(orig_iid, row, str(local)),
                                daemon=True,
                            ).start()
                        elif row and self.substitute_var.get():
                            self._set_slsk_status(orig_iid, "verificando…")
                            threading.Thread(
                                target=self._verify_and_substitute,
                                args=(orig_iid, row, str(local)),
                                daemon=True,
                            ).start()
                        else:
                            self._set_slsk_status(orig_iid, "✓ descargado")
                            self._release_active(orig_iid)

    def _auto_convert_and_maybe_sub(self, orig_iid, row, flac_path):
        """Convierte FLAC descargado a MP3 320, borra el FLAC, y si
        'Sustituir' está activo, continúa con la sustitución del original."""
        ff = self._find_ffmpeg()
        if not ff:
            self.after(0, self._set_slsk_status, orig_iid,
                       "ffmpeg no encontrado")
            self.after(0, self._release_active, orig_iid)
            return
        flac = Path(flac_path)
        mp3 = flac.with_suffix(".mp3")
        if mp3.exists():
            mp3 = flac.parent / (flac.stem + ".converted.mp3")
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(flac),
               "-codec:a", "libmp3lame", "-b:a", "320k",
               "-f", "mp3", str(mp3)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
            err = (proc.stderr or "").strip().splitlines()
            reason = err[-1] if err else f"exit {proc.returncode}"
            self.after(0, self._set_slsk_status, orig_iid,
                       f"error convert: {reason[:100]}")
            self.after(0, self._release_active, orig_iid)
            return
        # El FLAC ya convertido va a la papelera, no se borra.
        try:
            to_trash(flac)
        except Exception:
            pass
        if row and self.substitute_var.get():
            self.after(0, self._set_slsk_status, orig_iid, "verificando…")
            self._verify_and_substitute(orig_iid, row, str(mp3))
        else:
            self.after(0, self._set_slsk_status, orig_iid,
                       "✓ MP3 320 (convertido)")
            self.after(0, self._release_active, orig_iid)

    def _verify_and_substitute(self, orig_iid, row, local_path):
        match, sim, reason = songs_match(row["path"], local_path)
        if not match:
            self.after(0, self._set_slsk_status, orig_iid,
                       f"✗ no coincide ({reason})")
            self.after(0, self._release_active, orig_iid)
            return
        q_ok, q_reason = verify_download_quality(
            local_path, min_bitrate=self._min_bitrate_for(row))
        if not q_ok:
            self.after(0, self._set_slsk_status, orig_iid,
                       f"✗ calidad baja ({q_reason})")
            self.after(0, self._release_active, orig_iid)
            return
        try:
            new_path = substitute_file(row["path"], local_path)
        except Exception as e:
            self.after(0, self._set_slsk_status, orig_iid, f"error sust.: {e}")
            self.after(0, self._release_active, orig_iid)
            return

        def _update():
            row["path"] = str(new_path)
            row["name"] = new_path.name
            fresh = scan_row(str(new_path))
            for k in ("codec", "bitrate_kbps", "sample_rate",
                      "duration_s", "channels"):
                row[k] = fresh[k]
            row["analyzed"] = False
            row["verdict"] = ""
            row["bpm"] = None
            row["cutoff_hz"] = None
            row["detail"] = ""
            row["slsk_status"] = f"✓ sustituido ({reason})"
            index_mark_substituted(str(new_path), reason)
            self.tree.item(orig_iid, text=row["name"],
                           values=self._row_values(row), tags=("PENDING",))
            self._release_active(orig_iid)
            try:
                self._refresh_downloads_tab()
            except Exception:
                pass

        self.after(0, _update)

    def _download_basename(self, remote_filename):
        return (remote_filename or "").replace("\\", "/").rsplit("/", 1)[-1]

    def _find_local_download(self, remote_filename):
        """Busca en DOWNLOADS_DIR un archivo cuyo basename coincida con el
        último segmento del filename remoto (slskd usa \\ o /)."""
        base = (remote_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not base:
            return None
        for p in DOWNLOADS_DIR.rglob(base):
            if p.is_file():
                return p
        return None

    def _record_peer_cancel(self, username: str):
        """Un peer nos ha tumbado una descarga que no cancelamos nosotros.
        A la N-ésima dentro de la ventana, deja de usarse."""
        if not username or username in self.banned_peers:
            return
        limit = max(1, int(self._num(self.ban_strikes_var, 3)))
        window = max(1, int(self._num(self.ban_window_min_var, 60))) * 60
        now = time.time()
        hits = [t for t in self.peer_strikes.get(username, [])
                if now - t < window]
        hits.append(now)
        self.peer_strikes[username] = hits
        log.info("peer %s: %d cancelación(es) en %d min",
                 username, len(hits), window // 60)
        if len(hits) >= limit:
            self.banned_peers.append(username)
            self.peer_strikes.pop(username, None)
            log.warning("peer %s baneado tras %d cancelaciones",
                        username, len(hits))
            self.status.set(
                f"🚫 {username} baneado: {len(hits)} descargas canceladas "
                f"en {window // 60} min")
        self._save_current_state()

    def _mark_self_cancelled(self, username: str, filename: str):
        """Apunta que la cancelación es nuestra, para no culpar al peer."""
        if username and filename:
            self._self_cancelled.add((username, (filename or "").lower()))

    def _release_active(self, iid):
        self.active_by_iid.pop(iid, None)

    def _run_bg(self, name, target, *args, on_status=None):
        """Ejecuta `target(*args)` en un hilo con logging.
        `on_status`: mensaje de estado tras terminar (usa `.format(res=...)`
        si `target` devuelve algo)."""
        def _wrapped():
            t0 = time.time()
            log.info("bg %s start", name)
            try:
                res = target(*args)
            except Exception as e:
                log.exception("bg %s failed", name)
                self.after(0, self.status.set, f"{name}: error — {e}")
                return
            log.info("bg %s done in %.2fs", name, time.time() - t0)
            if on_status:
                try:
                    msg = on_status.format(res=res)
                except Exception:
                    msg = on_status
                self.after(0, self.status.set, msg)
            self.after(0, self._activity_tick)
        threading.Thread(target=_wrapped, daemon=True,
                         name=f"bg-{name}").start()

    def on_cancel_search(self):
        client = self._slsk_client()
        if not client:
            return
        sids = []
        for iid in self.search_tree.selection():
            tags = self.search_tree.item(iid, "tags")
            if tags:
                sids.append(tags[0])
        if not sids:
            return

        def worker():
            for sid in sids:
                try:
                    client.stop_search(sid)
                except Exception as e:
                    log.warning("stop_search %s failed: %s", sid, e)
            return len(sids)
        self.status.set(f"Cancelando {len(sids)} búsqueda(s)…")
        self._run_bg("cancel_search", worker,
                     on_status=f"{len(sids)} búsqueda(s) canceladas")

    def on_cancel_download(self, remove=False):
        client = self._slsk_client()
        if not client:
            return
        pairs = []
        for iid in self.dl_tree.selection():
            tags = self.dl_tree.item(iid, "tags")
            if len(tags) >= 2 and tags[0] != "virtual":
                pairs.append((tags[0], tags[1]))
                self._mark_self_cancelled(tags[0],
                                          self.dl_tree.set(iid, "file"))
        if not pairs:
            return

        def worker():
            for user, dl_id in pairs:
                try:
                    client.cancel_download(user, dl_id, remove=remove)
                except Exception as e:
                    log.warning("cancel_download %s/%s failed: %s",
                                user, dl_id, e)
            return len(pairs)
        self.status.set(f"Cancelando {len(pairs)} descarga(s)…")
        self._run_bg("cancel_download", worker,
                     on_status=f"{len(pairs)} descarga(s) canceladas")

    def on_clear_searches(self):
        """Elimina de slskd las búsquedas terminadas o canceladas."""
        client = self._slsk_client()
        if not client:
            return

        def worker():
            try:
                searches = client.list_searches()
            except Exception as e:
                raise
            removed = 0
            for s in searches:
                state = str(s.get("state") or "")
                if "InProgress" in state:
                    continue
                sid = s.get("id")
                if not sid:
                    continue
                try:
                    client.stop_search(sid)
                    removed += 1
                except Exception:
                    pass
            return removed
        self.status.set("Limpiando búsquedas…")
        self._run_bg("clear_searches", worker,
                     on_status="{res} búsquedas limpiadas")

    def on_clear_downloads(self):
        """Borra de slskd solo las descargas terminadas con éxito
        (Completed, Succeeded). Deja intactas las erroreadas/canceladas
        por si quieres reintentarlas."""
        client = self._slsk_client()
        if not client:
            return

        def worker():
            downloads = client.list_downloads()
            removed = 0
            for user_group in downloads:
                username = user_group.get("username", "")
                for directory in (user_group.get("directories") or []):
                    for f in (directory.get("files") or []):
                        state = str(f.get("state") or "")
                        if "Completed, Succeeded" not in state:
                            continue
                        dl_id = f.get("id")
                        if not dl_id:
                            continue
                        try:
                            client.cancel_download(username, dl_id, remove=True)
                            removed += 1
                        except Exception:
                            pass
            return removed
        self.status.set("Limpiando descargas completadas…")
        self._run_bg("clear_downloads", worker,
                     on_status="{res} descargas limpiadas")

    def on_clear_all_activity(self):
        """Limpia TODO."""
        if not messagebox.askyesno(
            "Maceta",
            "Cancelar y borrar TODAS las búsquedas y descargas activas?\n\n"
            "Se perderán las descargas en curso también.",
        ):
            return
        client = self._slsk_client()
        if not client:
            return

        def worker():
            s_removed = d_removed = 0
            try:
                for s in client.list_searches():
                    sid = s.get("id")
                    if sid:
                        try:
                            client.stop_search(sid); s_removed += 1
                        except Exception: pass
            except Exception: pass
            try:
                for user_group in client.list_downloads():
                    username = user_group.get("username", "")
                    for directory in (user_group.get("directories") or []):
                        for f in (directory.get("files") or []):
                            dl_id = f.get("id")
                            if not dl_id:
                                continue
                            try:
                                client.cancel_download(username, dl_id, remove=True)
                                d_removed += 1
                            except Exception: pass
            except Exception: pass
            return (s_removed, d_removed)
        self.status.set("Limpiando todo…")
        self._run_bg("clear_all", worker,
                     on_status="Limpiado todo: {res[0]} búsq., {res[1]} desc.")

    def _on_show_all_dl(self):
        """Al apagarlo hay que barrer las filas fantasma a mano: el
        render solo limpia lo que él mismo dejó de generar."""
        self._save_current_state()
        if not self.show_all_dl_var.get():
            for key in [k for k in self._activity_dl_iids
                        if isinstance(k, tuple) and k[0] == "virtual"]:
                iid = self._activity_dl_iids.pop(key)
                self._activity_dl_vals.pop(iid, None)
                if self.dl_tree.exists(iid):
                    self.dl_tree.delete(iid)
        self._activity_tick()

    def on_clear_dl_list(self):
        """Vacía la lista de descargas de slskd: quita todas las entradas
        terminadas (completadas, canceladas, erroreadas) y deja las que
        siguen bajando. No toca ningún fichero del disco."""
        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        if not messagebox.askyesno(
            "Maceta",
            "Vaciar la lista de descargas?\n\n"
            "Quita todas las entradas terminadas — completadas, canceladas "
            "y erroreadas. Las que siguen bajando se quedan.\n\n"
            "No borra ningún fichero del disco.",
        ):
            return

        def worker():
            removed = 0
            for user_group in client.list_downloads():
                username = user_group.get("username", "")
                for directory in (user_group.get("directories") or []):
                    for f in (directory.get("files") or []):
                        if "Completed" not in str(f.get("state") or ""):
                            continue
                        dl_id = f.get("id")
                        if not dl_id:
                            continue
                        try:
                            client.cancel_download(username, dl_id,
                                                   remove=True)
                            removed += 1
                        except Exception:
                            pass
            return removed
        self.status.set("Vaciando lista de descargas…")
        self._run_bg("clear_dl_list", worker,
                     on_status="Lista vaciada: {res} entrada(s)")

    def on_requery_failed(self):
        """Relanza todas las descargas caídas (canceladas o erroreadas)
        sin preguntar: recupera la query con la que se pidió cada fichero,
        borra la entrada muerta de slskd y vuelve a buscar sola. Cuando una
        query ya agotó el máximo de intentos configurado, abre el picker
        en vez de insistir."""
        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        self.status.set("Buscando descargas caídas…")
        threading.Thread(target=self._requery_failed_worker, args=(client,),
                         daemon=True, name="requery-failed").start()

    def _requery_failed_worker(self, client):
        try:
            downloads = client.list_downloads()
        except Exception as e:
            self.after(0, self.status.set, f"No se pudo leer slskd: {e}")
            return
        failed = []
        for user_group in downloads:
            username = user_group.get("username", "")
            for directory in (user_group.get("directories") or []):
                for f in (directory.get("files") or []):
                    state = str(f.get("state") or "")
                    if any(k in state for k in self._FAILED_STATES):
                        failed.append((username, f))
        if not failed:
            self.after(0, self.status.set, "No hay descargas caídas")
            return
        relaunched = asked = 0
        max_tries = max(1, int(self._num(self.max_auto_requery_var, 3)))
        for username, f in failed:
            fn = f.get("filename") or ""
            base = self._download_basename(fn)
            link = get_download_link(base)
            query = (link.get("search_query") or "").strip()
            if not query:
                query = " ".join(query_tokens(Path(base).stem))
            if not query:
                continue
            key = (username, fn.lower())
            link_iid = self.pending_dl_links.pop(key, None)
            self.finalized_dl_keys.discard(key)
            self._dl_num.pop(key, None)
            if link_iid is None and link.get("original_path"):
                link_iid = self._iid_for_path(link["original_path"])
            if link_iid:
                self.after(0, self._release_active, link_iid)
                self.after(0, self._set_slsk_status, link_iid, "rebuscando…")
            attempts = self._requery_attempts.get(query, 0) + 1
            self._requery_attempts[query] = attempts
            force = attempts >= max_tries
            try:
                self._mark_self_cancelled(username, fn)
                client.cancel_download(username, f.get("id"), remove=True)
            except Exception as e:
                log.warning("cancel en rebúsqueda falló: %s", e)
            ok, text = self._slsk_search_download(
                client, query, allow_picker=force, force_picker=force,
                link_iid=link_iid)
            if link_iid:
                self.after(0, self._set_slsk_status, link_iid, text)
            if ok:
                relaunched += 1
                self._requery_attempts.pop(query, None)
            elif force:
                asked += 1
            self.after(0, self.status.set,
                       f"Rebuscando caídas… {relaunched}/{len(failed)}")
        msg = f"Caídas rebuscadas: {relaunched}/{len(failed)} relanzada(s)"
        if asked:
            msg += f" · {asked} sin suerte tras {max_tries} intentos"
        self.after(0, self.status.set, msg)
        self.after(0, self._activity_tick)

    def on_retry_download(self):
        """Coge la descarga seleccionada, la cancela+borra, y lanza una
        nueva búsqueda con query editable. Si estaba linkeada a un row
        de Biblioteca, mantiene el link para poder sustituir."""
        sel = self.dl_tree.selection()
        if not sel:
            messagebox.showinfo("Maceta",
                                "Selecciona una descarga para reintentar.")
            return
        iid = sel[0]
        tags = self.dl_tree.item(iid, "tags")
        if len(tags) < 2:
            return
        user, dl_id = tags[0], tags[1]
        fname = self.dl_tree.set(iid, "file")
        # Query inicial: nombre del fichero sin extensión, limpiado
        stem = Path(fname).stem
        initial = re.sub(r"\s+", " ", stem).strip() or stem

        # Buscar el link original si existe
        link_iid = None
        for (u, remote_fn), oiid in list(self.pending_dl_links.items()):
            if u == user and remote_fn.endswith(fname.lower()):
                link_iid = oiid
                # Este key ya no debe procesarse más
                self.finalized_dl_keys.add((u, remote_fn))
                self.pending_dl_links.pop((u, remote_fn), None)
                break

        ref_row = self.rows_by_iid.get(link_iid) if link_iid else None
        src_path = (ref_row or {}).get("path") or fname
        new_query = ResearchDialog(self, initial, path=src_path).result
        if not new_query:
            return

        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        if link_iid:
            self._release_active(link_iid)
            self._set_slsk_status(link_iid, "rebuscando…")
        self.status.set(f"Rebuscando: {new_query}")

        def worker():
            try:
                client.cancel_download(user, dl_id, remove=True)
            except Exception as e:
                log.warning("cancel_download during retry failed: %s", e)
            self._run_search_query(client, new_query, link_iid=link_iid)
        threading.Thread(target=worker, daemon=True,
                         name="retry-download").start()

    # (compat: código antiguo llamaba a on_search_pick para abrir modal.
    # Ahora todo se hace inline en el panel Resultados.)
    def on_search_pick(self):
        self._on_search_selected()

    def _extract_num(self, val):
        """'#12' → 12; '' → None. Robusto a espacios."""
        s = (val or "").strip().lstrip("#")
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    def _iid_for_num(self, tree, num):
        """Devuelve el iid con esa columna 'num' = #N. Lineal pero
        limitado a filas visibles y evita ping HTTP."""
        wanted = f"#{num}"
        for iid in tree.get_children():
            if tree.set(iid, "num") == wanted:
                return iid
        return None

    def _release_syncing(self):
        self._syncing_activity = False

    def _sync_dl_from_search(self):
        if getattr(self, "_syncing_activity", False):
            return
        sel = self.search_tree.selection()
        if not sel:
            return
        n = self._extract_num(self.search_tree.set(sel[0], "num"))
        if n is None:
            return
        target = self._iid_for_num(self.dl_tree, n)
        if not target:
            return
        # Si ya está seleccionada, no toques nada (evita disparar
        # el <<TreeviewSelect>> del dl_tree y por tanto el eco).
        if target in self.dl_tree.selection():
            return
        self._syncing_activity = True
        self.dl_tree.selection_set(target)
        self.dl_tree.see(target)
        # Mantén la flag hasta después de que Tk procese el evento async
        # que dispara `selection_set`. 150 ms es más que suficiente.
        self.after(150, self._release_syncing)

    def _sync_search_from_dl(self):
        if getattr(self, "_syncing_activity", False):
            return
        sel = self.dl_tree.selection()
        if not sel:
            return
        n = self._extract_num(self.dl_tree.set(sel[0], "num"))
        if n is None:
            return
        target = self._iid_for_num(self.search_tree, n)
        if not target:
            return
        if target in self.search_tree.selection():
            return
        self._syncing_activity = True
        self.search_tree.selection_set(target)
        self.search_tree.see(target)
        self.after(150, self._release_syncing)

    def _on_search_selected(self):
        """Al seleccionar una búsqueda en Actividad, pobla el panel de
        Resultados. La lectura de responses se hace en un hilo — el Tk
        loop no se bloquea aunque slskd tarde en responder."""
        sel = self.search_tree.selection()
        if not sel:
            # Limpia si no hay selección
            for iid in self.results_tree.get_children():
                self.results_tree.delete(iid)
            self._results_items.clear()
            self._results_current_sid = None
            return
        iid = sel[0]
        tags = self.search_tree.item(iid, "tags")
        if not tags:
            return
        sid = tags[0]
        log.info("on_search_selected sid=%s current=%s fetching=%s",
                 sid, self._results_current_sid,
                 getattr(self, "_fetching_sid", None))
        if self._results_current_sid == sid:
            log.info("skip: already showing %s", sid)
            return
        if getattr(self, "_fetching_sid", None) == sid:
            log.info("skip: already fetching %s", sid)
            return
        self._fetching_sid = sid
        # Limpieza al pasar a un sid nuevo
        for iid_r in self.results_tree.get_children():
            self.results_tree.delete(iid_r)
        self._results_items.clear()
        self._results_current_sid = None

        sel = self.search_tree.selection()  # re-check (defensivo)
        if not sel:
            self._fetching_sid = None
            return
        iid = sel[0]
        tags = self.search_tree.item(iid, "tags")
        if not tags:
            return
        sid = tags[0]
        query = self.search_tree.set(iid, "query")
        client = self._slsk_client()  # sin ping — el ping era otro HTTP síncrono
        if client is None:
            return
        self.status.set(f"Cargando resultados de #{self._search_num.get(sid, '?')}…")
        threading.Thread(
            target=self._fetch_results_worker,
            args=(client, sid, query),
            daemon=True,
        ).start()

    def _fetch_results_worker(self, client, sid, query):
        t0 = time.time()
        log.info("fetch_results sid=%s", sid)
        try:
            responses = client.get_responses(sid)
        except Exception as e:
            log.warning("fetch_results failed for sid=%s: %s", sid, e)
            self.after(0, self.status.set, f"No se pudo leer respuestas: {e}")
            def _clear():
                if getattr(self, "_fetching_sid", None) == sid:
                    self._fetching_sid = None
            self.after(0, _clear)
            return
        log.info("fetch_results sid=%s took=%.2fs responses=%d",
                 sid, time.time() - t0, len(responses) if responses else 0)
        # Cómputo pesado (filtrado + score) también fuera del hilo Tk
        link_iid = self._search_link_iid.get(sid)
        ref_row = self.rows_by_iid.get(link_iid) if link_iid else None
        min_br = self._min_bitrate_for(ref_row)

        filters = self._search_filters()
        show_rejected = bool(self.results_show_rejected.get())
        limit = int(self._num(self.results_limit, 300)) or 300
        sort_by = self.results_sort_var.get() or "score"

        rows, rejected = [], []
        for resp in responses or []:
            peer_bad = peer_rejection_reason(resp, filters)
            for f in (resp.get("files") or []):
                why = peer_bad or rejection_reason(f, min_br, filters)
                (rejected if why else rows).append((resp, f, why))
        # Si no pasa ni uno, se enseñan igualmente los descartados con su
        # motivo: un panel vacío con 13 ficheros encontrados no dice nada
        # y no te deja bajarlos a mano aunque quieras.
        n_ok = len(rows)
        if show_rejected or not rows:
            rows = rows + rejected

        sorters = {
            "score": lambda t: -score(t[0], t[1], query),
            "br":    lambda t: -(t[1].get("bitRate") or 0),
            "size":  lambda t: -(t[1].get("size") or 0),
            "speed": lambda t: -(t[0].get("uploadSpeed") or 0),
            "queue": lambda t: (t[0].get("queueLength") or 0),
        }
        key = sorters.get(sort_by, sorters["score"])
        # Los descartados siempre al final, ordenados entre ellos igual.
        rows.sort(key=lambda t: (bool(t[2]), key(t)))
        rows = rows[:limit]
        reasons = {}
        for _r, _f, why in rejected:
            if why:
                tag = re.sub(r"\s*\([^)]*\)", "", why).strip()
                tag = re.sub(r"^~?\d+ kbps$", "bitrate bajo", tag)
                reasons[tag] = reasons.get(tag, 0) + 1

        # Orden: num, name, queue, slot, size, ext, br, user, folder, speed
        num = self._search_num.get(sid, "")
        num_str = f"#{num}" if num else ""
        items = []
        for resp, f, why in rows:
            ext = _file_ext(f) or "?"
            br = f.get("bitRate") or ""
            size_mb = f.get("size", 0) / 1024 / 1024
            fn = (f.get("filename") or "").replace("\\", "/")
            name = fn.rsplit("/", 1)[-1]
            folder = fn.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in fn else ""
            vals = (
                num_str,
                f"— {name}  ({why})" if why else name,
                str(resp.get("queueLength", 0)),
                "✓" if resp.get("hasFreeUploadSlot") else "—",
                f"{size_mb:.1f}",
                ext, str(br),
                resp.get("username", "?"),
                folder,
                f"{(resp.get('uploadSpeed') or 0)/1024:.0f}KB",
            )
            items.append((vals, (resp, f), bool(why)))

        self._search_usable[sid] = n_ok
        self.after(0, self._populate_results, sid, items, n_ok, reasons)

    def _populate_results(self, sid, items, n_ok=None, reasons=None):
        log.info("populate_results sid=%s items=%d current=%s",
                 sid, len(items), self._results_current_sid)
        if getattr(self, "_fetching_sid", None) == sid:
            self._fetching_sid = None
        if self._results_current_sid not in (None, sid):
            log.info("populate skip: current_sid=%s != %s",
                     self._results_current_sid, sid)
            return
        sel = self.search_tree.selection()
        if sel and self.search_tree.item(sel[0], "tags") and \
                self.search_tree.item(sel[0], "tags")[0] != sid:
            log.info("populate skip: selection moved to another search")
            return
        shown_ok = 0
        for vals, ref, rejected in items:
            item_iid = self.results_tree.insert(
                "", tk.END, values=vals,
                tags=("rejected",) if rejected else (),
            )
            self._results_items[item_iid] = ref
            shown_ok += not rejected
        self._results_current_sid = sid
        n = self._search_num.get(sid, "?")
        n_ok = shown_ok if n_ok is None else n_ok
        n_bad = len(items) - shown_ok
        if n_ok:
            extra = f" · {n_bad} en gris (no pasan el filtro)" if n_bad else ""
            plural = "válido" if n_ok == 1 else "válidos"
            self.status.set(f"Resultados de #{n}: {n_ok} {plural}{extra}")
        elif items:
            top = ", ".join(f"{v}× {k}" for k, v in sorted(
                (reasons or {}).items(), key=lambda kv: -kv[1])[:3])
            self.status.set(
                f"#{n}: ninguno de los {len(items)} pasa el filtro"
                + (f" ({top})" if top else "")
                + " — se muestran en gris, puedes bajarlos igual")
        else:
            self.status.set(f"#{n}: la búsqueda no devolvió ningún fichero")

    def _download_selected_results(self):
        """Encola descarga de los resultados seleccionados en un hilo
        aparte para no bloquear Tk."""
        sel = list(self.results_tree.selection())
        if not sel:
            messagebox.showinfo(
                "Maceta",
                "Selecciona uno o varios resultados (Cmd/Shift+click).",
            )
            return
        client = self._slsk_client()
        if client is None:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        bad = [i for i in sel
               if "rejected" in (self.results_tree.item(i, "tags") or ())]
        if bad:
            motivos = []
            for i in bad[:5]:
                name = self.results_tree.set(i, "name")
                motivos.append("· " + name.lstrip("— ").strip())
            resto = f"\n… y {len(bad) - 5} más" if len(bad) > 5 else ""
            if not messagebox.askyesno(
                "Maceta",
                f"{len(bad)} de los seleccionados no pasan tus filtros:\n\n"
                + "\n".join(motivos) + resto
                + "\n\n¿Bajarlos igualmente?",
            ):
                sel = [i for i in sel if i not in bad]
                if not sel:
                    return

        sid = getattr(self, "_results_current_sid", None)
        n = self._search_num.get(sid) if sid else None
        link_iid = self._search_link_iid.get(sid) if sid else None
        picks = [self._results_items[i] for i in sel if i in self._results_items]

        self.status.set(f"Encolando {len(picks)} descarga(s)…")

        def worker():
            if link_iid and n is not None:
                self._cancel_downloads_for_num(client, n)
            first = True
            queued = 0
            for resp, f in picks:
                payload = [{"filename": f["filename"], "size": f.get("size", 0)}]
                try:
                    client.enqueue_download(resp["username"], payload)
                except Exception as e:
                    log.warning("enqueue_download failed: %s", e)
                    continue
                key = (resp["username"], (f["filename"] or "").lower())
                if n is not None:
                    self._dl_num[key] = n
                if first and link_iid:
                    self.pending_dl_links[key] = link_iid
                    self.active_by_iid[link_iid] = "descargando"
                    self.finalized_dl_keys.discard(key)
                    self.after(0, self._set_slsk_status, link_iid,
                               f"↓ (elegido) de {resp['username']}")
                    # Persistir link para poder sustituir aunque después
                    # el usuario vacíe la lista.
                    row = self.rows_by_iid.get(link_iid) or {}
                    if row.get("path"):
                        try:
                            add_download_link(f["filename"], row["path"],
                                              search_query="picker")
                        except Exception as e:
                            log.warning("add_download_link failed: %s", e)
                first = False
                queued += 1
            return queued
        self._run_bg("enqueue_downloads", worker,
                     on_status="{res} descarga(s) encoladas")

    def _cancel_downloads_for_num(self, client, num):
        """Cancela+borra en slskd cualquier descarga cuyo #N coincide."""
        try:
            downloads = client.list_downloads()
        except Exception:
            return
        for user_group in downloads:
            username = user_group.get("username", "")
            for directory in (user_group.get("directories") or []):
                for f in (directory.get("files") or []):
                    remote_fn_lc = (f.get("filename") or "").lower()
                    if self._dl_num.get((username, remote_fn_lc)) == num:
                        try:
                            self._mark_self_cancelled(username, remote_fn_lc)
                            client.cancel_download(
                                username, f.get("id"), remove=True
                            )
                            self.finalized_dl_keys.add(
                                (username, remote_fn_lc)
                            )
                        except Exception:
                            pass

    def on_research(self):
        """Coge la búsqueda seleccionada, pregunta un nuevo query, cancela la
        vieja y lanza una nueva."""
        sel = self.search_tree.selection()
        if not sel:
            messagebox.showinfo("Maceta", "Selecciona una búsqueda para editar.")
            return
        iid = sel[0]
        old_query = self.search_tree.set(iid, "query")
        tags = self.search_tree.item(iid, "tags")
        old_sid = tags[0] if tags else None

        ref_iid = self._search_link_iid.get(old_sid) if old_sid else None
        ref_row = self.rows_by_iid.get(ref_iid) if ref_iid else None
        new_query = ResearchDialog(
            self, old_query, path=(ref_row or {}).get("path")).result
        if not new_query:
            return

        client = self._slsk_client()
        if not client:
            messagebox.showwarning("Maceta", "slskd no configurado.")
            return
        self.status.set(f"Rebuscando: {new_query}")

        def worker():
            if old_sid:
                try:
                    client.stop_search(old_sid)
                except Exception as e:
                    log.warning("stop_search during research failed: %s", e)
            self._run_search_query(client, new_query)
        threading.Thread(target=worker, daemon=True,
                         name="research").start()

    # ---------- Conversión a MP3 320 (solo si origen es mejor) ----------

    def _can_convert_to_mp3_320(self, row):
        codec = (row.get("codec") or "").upper()
        return codec in {"FLAC", "WAV"}

    @staticmethod
    def _find_ffmpeg():
        p = shutil.which("ffmpeg")
        if p:
            return p
        py_dir = Path(sys.executable).parent
        for cand in (py_dir / "ffmpeg", py_dir / "ffmpeg.exe"):
            if cand.exists():
                return str(cand)
        return None

    def on_convert_mp3(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("Maceta",
                                "Selecciona una o varias canciones a convertir.")
            return
        # Filtrar por elegibilidad
        eligible = [(i, self.rows_by_iid[i]) for i in sel
                    if self._can_convert_to_mp3_320(self.rows_by_iid[i])]
        skipped = len(sel) - len(eligible)
        if not eligible:
            messagebox.showinfo(
                "Maceta",
                "Ninguna de las canciones seleccionadas es lossless "
                "(FLAC/WAV). Convertir un MP3 a MP3 320 no mejora nada.",
            )
            return
        if skipped:
            if not messagebox.askyesno(
                "Maceta",
                f"{skipped} canción(es) no son lossless y se omitirán.\n"
                f"Convertir {len(eligible)} a MP3 320?",
            ):
                return
        ff = self._find_ffmpeg()
        if not ff:
            messagebox.showerror(
                "Maceta",
                "ffmpeg no está instalado. Instálalo (Mac: 'conda install "
                "-c conda-forge ffmpeg' o brew install ffmpeg).",
            )
            return
        self._ffmpeg_path = ff
        threading.Thread(
            target=self._run_convert_batch, args=(eligible,), daemon=True
        ).start()

    def _run_convert_batch(self, items):
        total = len(items)
        for i, (iid, row) in enumerate(items, 1):
            self.after(0, self.status.set,
                       f"Convirtiendo {i}/{total}: {row['name']}")
            self.after(0, self._set_slsk_status, iid, "convirtiendo…")
            try:
                orig = Path(row["path"])
                # Nombre temporal cuya extensión real ES .mp3 (para que
                # Path.suffix devuelva ".mp3" y substitute_file lo use bien).
                tmp_out = orig.parent / (orig.stem + ".maceta_tmp.mp3")
                cmd = [
                    self._ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig),
                    "-codec:a", "libmp3lame", "-b:a", "320k",
                    "-f", "mp3",
                    str(tmp_out),
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, check=False
                )
                if proc.returncode != 0:
                    stderr = (proc.stderr or "").strip().splitlines()
                    reason = stderr[-1] if stderr else f"exit {proc.returncode}"
                    print(f"[convert] {orig}: {reason}", file=sys.stderr)
                    raise RuntimeError(reason[:160])
                if not tmp_out.exists() or tmp_out.stat().st_size == 0:
                    raise RuntimeError("ffmpeg no generó salida")
                # Sustituir: original -> archivo/, tmp_out -> orig.stem.mp3
                new_path = substitute_file(str(orig), str(tmp_out))
                row["path"] = str(new_path)
                row["name"] = new_path.name
                fresh = scan_row(str(new_path))
                for k in ("codec","bitrate_kbps","sample_rate","duration_s","channels"):
                    row[k] = fresh[k]
                row["analyzed"] = False
                row["verdict"] = ""
                row["bpm"] = None
                row["cutoff_hz"] = None
                row["detail"] = ""
                row["slsk_status"] = "✓ MP3 320"
                self.after(0, lambda i=iid, r=row: self.tree.item(
                    i, text=r["name"], values=self._row_values(r), tags=("PENDING",)))
            except Exception as e:
                self.after(0, self._set_slsk_status, iid, f"error: {e}")
            self.after(0, self.progress.set, i / total * 100)
        self.after(0, self._convert_done, total)

    def _convert_done(self, total):
        self.status.set(f"Conversión terminada — {total} procesadas")

    # ---------- Miniplayer (ffplay) ----------

    @staticmethod
    def _find_ffplay():
        p = shutil.which("ffplay")
        if p:
            return p
        py_dir = Path(sys.executable).parent
        for cand in (py_dir / "ffplay", py_dir / "ffplay.exe"):
            if cand.exists():
                return str(cand)
        return None

    def play_path(self, path, label=""):
        """Reproduce cualquier ruta (usado también desde diálogos)."""
        if not Path(path).exists():
            messagebox.showerror("Maceta", f"El fichero no existe:\n{path}")
            return
        self.on_stop_playback()
        if not self._find_ffmpeg() and not shutil.which("afplay"):
            messagebox.showerror(
                "Maceta", "No hay forma de reproducir: falta ffmpeg.")
            return
        self._player.load(path)
        self._player.play(0)
        self._player_path = path
        self.now_playing_var.set(f"▶ {label or Path(path).name}")
        self.status.set(f"▶ {label or Path(path).name}")
        self.after(500, self._player_poll)

    def on_play_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("Maceta", "Selecciona una canción para reproducir.")
            return
        row = self.rows_by_iid.get(sel[0])
        if not row:
            return
        self.play_path(row["path"], row["name"])

    def on_stop_playback(self):
        try:
            self._player.stop()
        except Exception:
            pass
        self._player_proc = None
        self._player_path = None
        self.now_playing_var.set("—")

    def _player_poll(self):
        if self._player_path and not self._player.is_playing:
            self._player_path = None
            self.now_playing_var.set("—")
            return
        if self._player_path:
            self.after(500, self._player_poll)

    def _on_close(self):
        self.on_stop_playback()
        self._save_column_widths()
        self._save_layout()
        save_index()
        self.destroy()

    # ---------- Disposición de la ventana ----------

    def _panes(self) -> dict:
        """Los PanedWindow cuyos divisores merece la pena recordar."""
        return {k: w for k, w in (
            ("biblioteca", getattr(self, "_biblio_panes", None)),
            ("mensajes", getattr(self, "_msg_panes", None)),
            ("actividad", getattr(self, "_activity_panes", None)),
            ("actividad_top", getattr(self, "_activity_top_panes", None)),
        ) if w is not None}

    def _save_layout(self):
        """Tamaño y sitio de la ventana, pestaña abierta y divisores."""
        try:
            geo = self.geometry()
        except Exception:
            return
        # Se parte de lo ya guardado: un panel cuya pestaña no está a la
        # vista no se puede medir, y machacarlo con basura lo colapsaría
        # en el próximo arranque.
        sash = dict(self._user_state.get("sash") or {})
        for name, pane in self._panes().items():
            measured = self._measure_sashes(pane)
            if measured:
                sash[name] = measured
        try:
            tab = self.notebook.index(self.notebook.select())
        except Exception:
            tab = 0
        save_user_state({"window_geometry": geo, "active_tab": int(tab),
                         "sash": sash})

    def _on_tab_layout(self, _event=None):
        """Los divisores de una pestaña solo se pueden colocar cuando se
        muestra: al cambiar de pestaña se aplican los suyos."""
        self.after(120, self._apply_saved_sashes)
        self._schedule_layout_save()

    def _apply_saved_sashes(self):
        saved = self._user_state.get("sash") or {}
        self.update_idletasks()
        for name, pane in self._panes().items():
            horiz = str(pane.cget("orient")).startswith("horiz")
            span = pane.winfo_width() if horiz else pane.winfo_height()
            if span < 3 * self.MIN_PANE_PX:
                continue
            for i, pos in enumerate(saved.get(name) or []):
                pos = int(pos)
                if pos < self.MIN_PANE_PX or pos > span - self.MIN_PANE_PX:
                    continue
                try:
                    pane.sashpos(i, pos)
                except Exception:
                    pass

    MIN_PANE_PX = 60   # por debajo de esto un panel queda inservible

    def _measure_sashes(self, pane):
        """Posiciones de los divisores, o None si el panel todavía no
        está dibujado o el valor dejaría un lado aplastado.

        Un ttk.PanedWindow de una pestaña que nunca se ha mostrado
        devuelve posiciones sin sentido (0, o un par de píxeles), y
        guardarlas es lo que dejaba Búsquedas y Resultados invisibles."""
        horiz = str(pane.cget("orient")).startswith("horiz")
        span = pane.winfo_width() if horiz else pane.winfo_height()
        if span < 3 * self.MIN_PANE_PX:
            return None
        pos, i = [], 0
        while True:
            try:
                pos.append(int(pane.sashpos(i)))
            except Exception:
                break
            i += 1
        if not pos:
            return None
        for p in pos:
            if p < self.MIN_PANE_PX or p > span - self.MIN_PANE_PX:
                return None
        return pos

    def _schedule_layout_save(self, _event=None):
        """<Configure> dispara en cada píxel de un arrastre: agrupamos."""
        if getattr(self, "_layout_job", None):
            try:
                self.after_cancel(self._layout_job)
            except Exception:
                pass
        self._layout_job = self.after(1200, self._save_layout)

    def _restore_layout(self):
        """Devuelve la ventana a donde estaba. Si la geometría guardada
        cae fuera de las pantallas actuales (un monitor que ya no está),
        se ignora la posición y se queda el tamaño."""
        us = self._user_state
        geo = us.get("window_geometry") or ""
        m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geo)
        if m:
            w, h, x, y = (int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), int(m.group(4)))
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = max(700, min(w, sw)), max(400, min(h, sh))
            if -50 <= x <= sw - 100 and 0 <= y <= sh - 100:
                self.geometry(f"{w}x{h}+{x}+{y}")
            else:
                self.geometry(f"{w}x{h}")
        try:
            tab = int(us.get("active_tab") or 0)
            if 0 <= tab < self.notebook.index("end"):
                self.notebook.select(tab)
        except Exception:
            pass
        saved = us.get("sash") or {}
        self.update_idletasks()
        for name, pane in self._panes().items():
            horiz = str(pane.cget("orient")).startswith("horiz")
            span = pane.winfo_width() if horiz else pane.winfo_height()
            if span < 3 * self.MIN_PANE_PX:
                continue   # pestaña no visible: se aplicará cuando se abra
            for i, pos in enumerate(saved.get(name) or []):
                pos = int(pos)
                # Nunca restaurar algo que deje un lado aplastado.
                if pos < self.MIN_PANE_PX or pos > span - self.MIN_PANE_PX:
                    continue
                try:
                    pane.sashpos(i, pos)
                except Exception:
                    pass
        self.bind("<Configure>", self._schedule_layout_save, add="+")
        self.notebook.bind("<<NotebookTabChanged>>",
                           self._on_tab_layout, add="+")

    # ---------- Carpetas recientes ----------

    def _push_recent_folder(self, folder):
        recent = [f for f in (self._user_state.get("recent_folders") or [])
                  if f != folder]
        recent.insert(0, folder)
        recent = recent[:8]
        self._user_state["recent_folders"] = recent
        save_user_state({"recent_folders": recent})
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        menu = getattr(self, "_recent_menu", None)
        if menu is None:
            return
        menu.delete(0, "end")
        recent = [f for f in (self._user_state.get("recent_folders") or [])
                  if os.path.isdir(f)]
        if not recent:
            menu.add_command(label="(ninguna todavía)", state="disabled")
            return
        for f in recent:
            menu.add_command(label=f.replace(str(Path.home()), "~"),
                             command=lambda d=f: self.open_folder(d))
        menu.add_separator()
        menu.add_command(label="Olvidar la lista",
                         command=self._clear_recent_folders)

    def _clear_recent_folders(self):
        self._user_state["recent_folders"] = []
        save_user_state({"recent_folders": []})
        self._rebuild_recent_menu()

    def open_folder(self, d):
        """Escanea una carpeta y la apunta en las recientes.

        Cada escaneo lleva número: si eliges otra carpeta mientras uno
        está en marcha, el viejo sigue vivo en su hilo pero sus lotes se
        descartan. Sin esto, dos escaneos insertaban en la misma tabla y
        salían filas duplicadas."""
        self.folder_filter = ""
        self.folder.set(d)
        self._push_recent_folder(d)
        self._save_current_state()
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        self.tree.delete(*self.tree.get_children())
        self.rows_by_iid.clear()
        self.progress.set(0)
        self.status.set(f"Leyendo metadatos de {d}…")
        threading.Thread(target=self._scan_folder, args=(d, self._scan_gen),
                         daemon=True).start()

    # ---------- Anchos de columna persistentes ----------

    def _tracked_trees(self) -> dict:
        """Tablas cuyos anchos de columna se recuerdan entre sesiones."""
        names = ("tree", "search_tree", "results_tree",
                 "dl_tree", "dl_files_tree")
        return {n: t for n in names
                if (t := getattr(self, n, None)) is not None}

    def _restore_column_widths(self):
        saved = (self._user_state.get("column_widths") or {})
        for key, tree in self._tracked_trees().items():
            for col, w in (saved.get(key) or {}).items():
                try:
                    tree.column(col, width=int(w))
                except Exception:
                    pass
            tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")

    def _on_tree_release(self, event):
        """Guarda solo si se soltó el ratón sobre un separador de cabecera,
        que es lo único que cambia el ancho de una columna."""
        try:
            if event.widget.identify_region(event.x, event.y) != "separator":
                return
        except Exception:
            return
        if self._col_save_job:
            try:
                self.after_cancel(self._col_save_job)
            except Exception:
                pass
        self._col_save_job = self.after(600, self._save_column_widths)

    def _save_column_widths(self):
        self._col_save_job = None
        out = {}
        for key, tree in self._tracked_trees().items():
            widths = {}
            for col in ("#0", *tree["columns"]):
                try:
                    widths[col] = int(tree.column(col, "width"))
                except Exception:
                    pass
            if widths:
                out[key] = widths
        if out:
            self._user_state["column_widths"] = out
            save_user_state({"column_widths": out})

    # ---------- Borrar / Vaciar lista ----------

    def on_delete_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("Maceta", "Selecciona filas para borrar.")
            return
        n = len(sel)
        if not messagebox.askyesno(
            "Maceta",
            f"Mover {n} archivo(s) a la carpeta 'archivo/' (recuperable)?\n\n"
            "Se conserva la ruta original dentro de archivo/ por si "
            "necesitas restaurar.",
        ):
            return
        moved = 0
        skipped = 0
        for iid in sel:
            row = self.rows_by_iid.get(iid)
            if not row:
                continue
            orig = Path(row["path"])
            if not orig.exists():
                # Fichero ya no existe: solo lo quito de la lista
                self.tree.delete(iid)
                self.rows_by_iid.pop(iid, None)
                skipped += 1
                continue
            try:
                arch = archive_path_for(str(orig))
                arch.parent.mkdir(parents=True, exist_ok=True)
                if arch.exists():
                    arch = arch.with_name(
                        arch.stem + "." + str(int(time.time())) + arch.suffix
                    )
                shutil.move(str(orig), str(arch))
                self.tree.delete(iid)
                self.rows_by_iid.pop(iid, None)
                self._retry_queue.pop(iid, None)
                moved += 1
            except Exception as e:
                self.status.set(f"error borrando {orig.name}: {e}")
        self.status.set(
            f"Borrado: {moved} movidos a archivo/, {skipped} ya no estaban"
        )

    def on_smart_rename(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        if len(sel) > 1:
            messagebox.showinfo(
                "Maceta",
                "Selecciona una única canción para renombrar/editar tags.")
            return
        iid = sel[0]
        row = self.rows_by_iid.get(iid)
        if not row:
            return
        SmartRenameDialog(self, iid, row)

    def apply_rename_and_tags(self, iid, row, new_name, tags,
                              write_tags=True, do_rename=True):
        """Aplica edición de tags + renombrado. Actualiza la fila."""
        errors = []
        path = Path(row["path"])
        target_path = path
        # 1. Escribir tags si toca
        if write_tags:
            try:
                write_song_tags(str(path), tags)
            except Exception as e:
                errors.append(f"tags: {e}")
        # 2. Renombrar si el nombre cambia
        if do_rename and new_name and new_name != path.name:
            candidate = path.with_name(sanitize_filename(new_name))
            if candidate.exists() and candidate != path:
                errors.append(f"ya existe {candidate.name}")
            else:
                try:
                    path.rename(candidate)
                    target_path = candidate
                except Exception as e:
                    errors.append(f"rename: {e}")
        # 3. Refrescar la fila
        row["path"] = str(target_path)
        row["name"] = target_path.name
        # Metadatos frescos
        fresh = scan_row(str(target_path))
        for k in ("codec", "bitrate_kbps", "sample_rate",
                  "duration_s", "channels"):
            row[k] = fresh[k]
        if self.tree.exists(iid):
            self.tree.item(iid, text=row["name"],
                           values=self._row_values(row))
        if errors:
            self.status.set("Renombrado con errores: " + "; ".join(errors))
        else:
            self.status.set(f"Renombrado a {target_path.name}")

    def on_manual_substitute(self):
        """Permite asignar manualmente un archivo descargado a la fila
        seleccionada, saltándose la verificación de similitud."""
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("Maceta",
                                "Selecciona la canción original a sustituir.")
            return
        if len(sel) > 1:
            messagebox.showinfo("Maceta",
                                "Solo una fila a la vez para esta operación.")
            return
        iid = sel[0]
        row = self.rows_by_iid.get(iid)
        if not row:
            return
        ManualSubstituteDialog(self, iid, row)

    def do_manual_substitute(self, orig_iid, row, downloaded_path,
                             reason="manual"):
        """Sustituye el original por la descarga ya elegida. `reason` es de
        dónde salió la decisión: 'manual', 'link' o la similitud de audio."""
        try:
            new_path = substitute_file(row["path"], downloaded_path)
        except Exception as e:
            messagebox.showerror("Maceta", f"No se pudo sustituir: {e}")
            return
        row["path"] = str(new_path)
        row["name"] = new_path.name
        fresh = scan_row(str(new_path))
        for k in ("codec", "bitrate_kbps", "sample_rate",
                  "duration_s", "channels"):
            row[k] = fresh[k]
        row["analyzed"] = False
        row["verdict"] = ""
        row["bpm"] = None
        row["cutoff_hz"] = None
        row["detail"] = ""
        row["slsk_status"] = f"✓ sustituido ({reason})"
        index_mark_substituted(str(new_path), reason)
        remove_download_link(Path(downloaded_path).name)
        if orig_iid in self.rows_by_iid and self.tree.exists(orig_iid):
            self.tree.item(orig_iid, text=row["name"],
                           values=self._row_values(row), tags=("PENDING",))
        self._schedule_filter_refresh()
        self._schedule_folder_refresh(rebuild=False)
        self.status.set(f"Sustituido ({reason}): {new_path.name}")
        # Refrescar la pestaña Descargas (el fichero ya no está allí)
        try:
            self._refresh_downloads_tab()
        except Exception:
            pass

    def on_clear_library(self):
        if not self.rows_by_iid:
            return
        if not messagebox.askyesno(
            "Maceta",
            f"Vaciar la lista ({len(self.rows_by_iid)} entradas)?\n\n"
            "No borra ningún archivo. Solo limpia la pantalla.",
        ):
            return
        self.tree.delete(*self.tree.get_children())
        self.rows_by_iid.clear()
        self.active_by_iid.clear()
        self.pending_dl_links.clear()
        self._retry_queue.clear()
        self._requery_attempts.clear()
        self._manual_needed.clear()
        self.progress.set(0)
        self.folder.set("")
        self.status.set("Lista vaciada")

    def on_show_spectrum(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Maceta", "Selecciona una canción primero.")
            return
        iid = sel[0]
        row = self.rows_by_iid.get(iid)
        if not row:
            return
        if not row.get("analyzed"):
            if messagebox.askyesno(
                "Maceta",
                f"'{row['name']}' aún no está analizada.\n\n¿Analizarla ahora?",
            ):
                self._start_analysis([iid])
            return
        if row["freqs"] is None:
            messagebox.showinfo(
                "Maceta",
                f"No hay datos de espectro para '{row['name']}'\n"
                f"(estado: {row['verdict']} — {row['detail']})",
            )
            return
        SpectrumWindow(self, row)

    def on_export(self):
        if not self.rows_by_iid:
            messagebox.showinfo("Maceta", "Aún no hay resultados.")
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")]
        )
        if not p:
            return
        cols = ["name", "path", "codec", "bitrate_kbps", "sample_rate",
                "duration_s", "channels", "bpm", "cutoff_hz", "verdict", "detail"]
        try:
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, quoting=csv.QUOTE_ALL, doublequote=True)
                w.writerow(cols)
                for row in self.rows_by_iid.values():
                    w.writerow([
                        "" if row.get(c) is None else row.get(c)
                        for c in cols
                    ])
            self.status.set(f"CSV guardado: {p}")
        except Exception as e:
            messagebox.showerror("Maceta", f"No se pudo escribir el CSV: {e}")

    # ---------- Soulseek ----------

    def _slsk_client(self):
        cfg = load_config()
        url = (cfg.get("slskd_url") or "").strip()
        key = (cfg.get("slskd_api_key") or "").strip()
        if not url or not key:
            return None
        return SlskdClient(url, key)

    def on_slsk_config(self):
        OptionsDialog(self, tab="slskd")

    def on_slsk_search(self):
        client = self._connected_client()
        if not client:
            return

        sel_iids = list(self.tree.selection())
        custom_query = None
        if sel_iids:
            candidates = [(i, self.rows_by_iid[i]) for i in sel_iids]
            # Con una sola selección, permitir editar la query antes de buscar
            if len(candidates) == 1:
                _, row = candidates[0]
                initial = (build_query(row, self.query_mode_var.get())
                           or Path(row["path"]).stem)
                edited = ResearchDialog(self, initial,
                                        path=row["path"]).result
                if not edited:
                    return
                custom_query = edited
        else:
            candidates = [
                (i, r) for i, r in self.rows_by_iid.items()
                if r.get("analyzed") and r["verdict"] not in {"OK", "LOSSLESS"}
            ]
            if not candidates:
                messagebox.showinfo(
                    "Maceta",
                    "No hay filas para descargar. Analiza primero, "
                    "o selecciona canciones manualmente.",
                )
                return
        self._launch_slsk_batch(client, candidates, custom_query=custom_query)

    def on_slsk_fakes(self):
        client = self._connected_client()
        if not client:
            return
        ambito = set(self._scope_iids())
        candidates = [(i, r) for i, r in self.rows_by_iid.items()
                      if i in ambito and r.get("analyzed")
                      and r["verdict"] == "FAKE"]
        if not candidates:
            messagebox.showinfo(
                "Maceta",
                f"No hay canciones FAKE en {self._scope_label()}.")
            return
        if not messagebox.askyesno(
            "Maceta",
            f"Buscar en Soulseek {len(candidates)} canción(es) FAKE de "
            f"{self._scope_label()}?",
        ):
            return
        self._launch_slsk_batch(client, candidates)

    def _connected_client(self):
        """Devuelve un SlskdClient si está configurado. Ya NO pinguea —
        la ping era una llamada HTTP síncrona en el hilo Tk que bloqueaba
        la UI cuando slskd tardaba en responder. Los errores reales de
        conexión salen ahora del worker que llame `enqueue_download` u
        otro método, y se marshalan a la UI via `after(0, ...)`."""
        client = self._slsk_client()
        if client is None:
            messagebox.showwarning(
                "Maceta", "Configura primero slskd (⚙ Config Soulseek…)."
            )
            return None
        return client

    def _launch_slsk_batch(self, client, candidates, custom_query=None):
        candidates = [(i, r) for i, r in candidates
                      if i not in self.active_by_iid]
        if not candidates:
            messagebox.showinfo("Maceta",
                                "Esas canciones ya están buscando/descargando.")
            return
        self.status.set(f"Buscando en Soulseek: {len(candidates)} canciones…")
        threading.Thread(
            target=self._run_slsk_batch,
            args=(client, candidates),
            kwargs={"custom_query": custom_query},
            daemon=True,
        ).start()

    def _run_slsk_batch(self, client, candidates, custom_query=None):
        total = len(candidates)
        allow_picker = total == 1

        # Marcar todos como "en cola" antes de arrancar workers
        for iid, row in candidates:
            self.active_by_iid[iid] = "buscando"
        self.after(0, self.status.set,
                   f"Buscando en Soulseek ({total} en paralelo)…")

        def worker(iid, row):
            query = (custom_query
                     or build_query(row, self.query_mode_var.get())
                     or row["name"])
            self.after(0, self._set_slsk_status, iid, f"buscando: {query}")
            t0 = time.time()
            log.info("search start iid=%s query=%r", iid, query)
            try:
                ok, text = self._slsk_search_download(
                    client, query, allow_picker=allow_picker, link_iid=iid,
                )
            except Exception as e:
                log.exception("search worker failed for %r", query)
                ok, text = False, f"error: {str(e)[:60]}"
            log.info("search done iid=%s ok=%s took=%.2fs → %s",
                     iid, ok, time.time() - t0, text)
            return iid, ok, text

        # Búsquedas secuenciales — el paralelismo saturaba slskd y hacía
        # que el hilo principal esperase en llamadas HTTP encadenadas.
        done = 0
        for iid, row in candidates:
            try:
                _, ok, text = worker(iid, row)
            except Exception as e:
                ok, text = False, f"error: {str(e)[:60]}"
            if ok:
                self.after(0, self._set_slsk_status, iid, text)
            else:
                self.after(0, self._set_slsk_status, iid, f"✗ {text}")
                self._release_active(iid)
            done += 1
            self.after(0, self.progress.set, done / total * 100)
        self.after(0, self._slsk_batch_done, total)

    def _acceptable_count(self, responses, min_br):
        """Cuántos ficheros pasarían el filtro — lo que el picker
        mostraría de verdad. Sirve para no abrir un selector vacío."""
        filters = self._search_filters()
        n = 0
        for resp in responses or []:
            if peer_rejection_reason(resp, filters):
                continue
            for f in (resp.get("files") or []):
                if is_acceptable(f, min_bitrate=min_br, filters=filters):
                    n += 1
        return n

    def _slsk_search_download(self, client, query, allow_picker=False,
                              link_iid=None, force_picker=False):
        """Busca en Soulseek, encola descarga.
        - allow_picker: si True y ask_quality está activo, muestra picker.
        - force_picker: muestra el picker siempre, ignorando ask_quality.
          Lo usan los caminos de "ya lo he intentado solo y no hay forma".
        - link_iid: iid del row original para tracking + sustitución.
        Devuelve (ok, text)."""
        if not query:
            return False, "sin query"

        attempts = [query]
        if self.smart_search_var.get():
            attempts += query_fallbacks(query)

        responses, sid, used_query, variant = [], None, query, 0
        for i, q in enumerate(attempts):
            info = client.start_search(q)
            sid = info.get("id")
            if not sid:
                return False, "no search id"
            # Asignar #N a esta búsqueda (thread-safe)
            with self._num_lock:
                self._num_counter += 1
                n = self._num_counter
                self._search_num[sid] = n
                if link_iid:
                    self._search_link_iid[sid] = link_iid
            if i:
                self.after(0, self._set_slsk_status, link_iid,
                           f"probando «{q}»…") if link_iid else None
                log.info("sin respuestas, pruebo variante %d: '%s'", i, q)
            # Sin max_wait a mano: 25 s era menos que el searchTimeout de
            # 30 s que le pedimos a slskd, así que nos rendíamos antes de
            # que la búsqueda pudiera cerrarse sola.
            client.wait_for_search(sid)
            responses = client.get_responses(sid)
            if responses:
                used_query, variant = q, i
                break
        query = used_query
        if sid:
            self._search_usable[sid] = sum(
                1 for r in (responses or [])
                if not peer_rejection_reason(r, self._search_filters())
                for f in (r.get("files") or [])
                if not rejection_reason(f, self._min_bitrate_for(
                    self.rows_by_iid.get(link_iid) if link_iid else None),
                    self._search_filters()))
        pick = None
        ref_row = self.rows_by_iid.get(link_iid) if link_iid else None
        min_br = self._min_bitrate_for(ref_row)
        auto_note = ""
        want_picker = force_picker or (allow_picker
                                       and self.ask_quality_var.get())
        if want_picker and not force_picker:
            # Si algún resultado se llama igual que la query no hay nada que
            # preguntar: se baja el mejor de esos y no se abre el picker.
            pick = choose_best(exact_responses(responses, query), query,
                               min_bitrate=min_br,
                               filters=self._search_filters())
            if pick is not None:
                want_picker = False
                auto_note = " · exacta"
        if want_picker and self._acceptable_count(responses, min_br) == 0:
            # El selector saldría vacío: no hay ningún fichero que pase el
            # filtro. Mejor decirlo que abrir un diálogo sin nada.
            n_files = sum(len(r.get("files") or []) for r in responses or [])
            if not responses:
                return False, "nadie respondió"
            return False, (f"{n_files} ficheros, ninguno pasa el filtro "
                           f"(min {min_br} kbps) — nada que elegir")
        if want_picker:
            box = {}
            def _open():
                dlg = QualityPickerDialog(self, query, responses,
                                          min_bitrate=min_br,
                                          filters=self._search_filters())
                box["r"] = dlg.result
            self.after(0, _open)
            # Espera activa hasta que se cierre el diálogo
            while "r" not in box:
                time.sleep(0.1)
            pick = box["r"]
            if pick is None:
                return False, "saltado por usuario"
        elif pick is None:
            pick = choose_best(responses, query, min_bitrate=min_br,
                               filters=self._search_filters())
        if not pick:
            n_files = sum(len(r.get("files") or []) for r in responses or [])
            if not responses:
                if len(attempts) > 1:
                    return False, (f"nadie respondió "
                                   f"({len(attempts)} variantes probadas)")
                return False, "nadie respondió (query demasiado específica?)"
            return False, (f"{n_files} ficheros, ninguno pasa el filtro "
                           f"(min {min_br} kbps)")
        resp, f = pick
        # Si el fichero ya vive en downloads/ (basename), no duplicamos.
        existing = self._find_local_download(f["filename"])
        if existing is not None:
            if link_iid:
                key = (resp["username"], (f["filename"] or "").lower())
                self.pending_dl_links[key] = link_iid
                self.active_by_iid[link_iid] = "descargando"
                self.finalized_dl_keys.discard(key)
            return True, f"ya en descargas: {existing.name}"
        payload = [{"filename": f["filename"], "size": f.get("size", 0)}]
        client.enqueue_download(resp["username"], payload)
        key = (resp["username"], (f["filename"] or "").lower())
        self._dl_num[key] = n
        if link_iid:
            self.pending_dl_links[key] = link_iid
            self.active_by_iid[link_iid] = "descargando"
            # Persistir el link para poder sustituir aunque la biblio
            # ya no tenga la fila cargada.
            row = self.rows_by_iid.get(link_iid) or {}
            if row.get("path"):
                try:
                    add_download_link(f["filename"], row["path"], query)
                except Exception as e:
                    log.warning("add_download_link failed: %s", e)
        ext = _file_ext(f)
        br = f.get("bitRate")
        tag = "FLAC" if ext == "flac" else (f"{br}kbps" if br else ext.upper())
        if variant:
            auto_note += f" · variante {variant}"
        return True, f"↓ {tag} de {resp['username']}{auto_note}"

    def _slsk_one(self, client, iid, row):
        query = build_query(row, self.query_mode_var.get())
        _, text = self._slsk_search_download(
            client, query, allow_picker=False, link_iid=iid
        )
        self.after(0, self._set_slsk_status, iid, text)

    def _set_slsk_status(self, iid, text):
        row = self.rows_by_iid.get(iid)
        if not row or not self.tree.exists(iid):
            return
        row["slsk_status"] = text
        tag = row["verdict"] if row["verdict"] else "PENDING"
        self.tree.item(iid, values=self._row_values(row), tags=(tag,))
        self._schedule_filter_refresh()
        self._maybe_schedule_retry(iid, text)

    def _slsk_batch_done(self, total):
        queued = sum(1 for r in self.rows_by_iid.values()
                     if (r.get("slsk_status") or "").startswith("↓"))
        self.status.set(
            f"Soulseek listo — {queued}/{total} en cola de descarga en slskd"
        )
        self.progress.set(100)

    # ---------- Buscar por texto y encolar descarga ----------

    def on_search_query(self):
        query = self.search_var.get().strip()
        if not query:
            self.search_entry.focus_set()
            return
        client = self._slsk_client()
        if client is None:
            messagebox.showwarning(
                "Maceta", "Configura primero slskd (⚙ Config Soulseek…)."
            )
            return
        # Sin ping — el worker reporta el error si slskd no responde.
        self.status.set(f"Buscando: {query}…")
        log.info("on_search_query: %s", query)
        threading.Thread(
            target=self._run_search_query,
            args=(client, query),
            daemon=True,
        ).start()

    def _run_search_query(self, client, query, link_iid=None):
        try:
            ok, text = self._slsk_search_download(
                client, query, allow_picker=True, link_iid=link_iid,
            )
        except Exception as e:
            ok, text = False, f"error: {e}"
        self.after(0, self.status.set, f"'{query}' → {text}")
        if ok and link_iid is None:
            self.after(0, self.search_var.set, "")

MACETA_MANUAL = [
    ("¿Qué es Maceta?", [
        "Maceta mira tu biblioteca de música y encuentra los MP3 falsos: "
        "los que dicen «320 kbps» pero en realidad vienen de un 128 "
        "recodificado y suenan peor. Luego te ayuda a bajar una versión "
        "buena por Soulseek y a ponerla en su sitio.",
        "",
        "Regla de oro: Maceta NUNCA borra nada. Al sustituir, el original "
        "va a la carpeta archivo/. Al «borrar», va a la papelera del "
        "sistema. Todo es recuperable.",
    ]),
    ("Las pestañas de arriba", [
        "• Biblioteca — tu música: analizar, ver la calidad, buscar fakes.",
        "• Actividad Soulseek — las búsquedas y descargas en curso.",
        "• Descargas — los ficheros que has bajado, para colocarlos.",
        "• Mensajes — el chat privado de Soulseek.",
        "• Estadísticas — números de tu colección, con cosas curiosas.",
    ]),
    ("Biblioteca: el explorador de carpetas (izquierda)", [
        "A la izquierda está el árbol de tus carpetas, con cuántos temas, "
        "fakes y pendientes tiene cada una.",
        "• Al seleccionar una carpeta, TODO trabaja sobre ella: los chips "
        "de arriba pasan a ser las estadísticas de esa carpeta, y los "
        "botones de abajo actúan solo sobre lo que ves.",
        "• «📚» o «Toda la biblioteca» vuelve a verlo todo.",
        "• «🔄 Re-escanear» vuelve a mirar el disco: recoge lo nuevo y "
        "quita lo que ya no está, sin perder el análisis de lo demás.",
    ]),
    ("Biblioteca: los chips de estado", [
        "Arriba, cada chip filtra y cuenta un grupo:",
        "• ⚠ Revisar — descargas cuyo audio no cuadra con el original.",
        "• ❌ FAKE — calidad real por debajo de la que declaran.",
        "• ⛔ Error — fallo técnico al analizar.",
        "• ⏳ En proceso — con búsqueda o descarga en marcha.",
        "• ✅ Listas — analizadas y correctas.",
        "• ⬇ Sustituidas — ya reemplazadas por una versión mejor.",
        "• ❓ Sin analizar — pendientes de analizar.",
        "",
        "El filtro de texto de al lado oculta filas por nombre (usa * y ?).",
    ]),
    ("Biblioteca: analizar y los botones", [
        "• «🔎 Analizar» — mira el espectro de cada canción y decide si es "
        "FAKE. Lo hace una sola vez: lo ya analizado se recuerda (por "
        "huella del fichero), así que mover o renombrar no obliga a "
        "repetir.",
        "• «🔄 Re-analizar» — fuerza, ignorando lo recordado.",
        "• «⬇ Buscar fakes» — busca en Soulseek reemplazo de las FAKE que "
        "estás viendo (de la carpeta o chip activo).",
        "• «▶ / ⏹» — reproduce la canción seleccionada.",
        "• «🗑 Borrar (a archivo/)» — mueve a archivo/, NO borra.",
        "• «Compacto» junta Formato+Bitrate+SR en una celda; A−/A+ cambian "
        "el tamaño de letra de esa tabla.",
        "• Doble clic en una fila abre su espectro.",
    ]),
    ("Cómo decide si algo es FAKE", [
        "Mira el corte del espectro: por encima de ~16 kHz hay información "
        "real; por debajo, es un transcode. Un «320» con corte a 15 kHz es "
        "un 128 disfrazado. El bitrate declarado da igual: manda el audio.",
        "Doble clic en una canción muestra su espectro con el corte "
        "marcado, para verlo tú mismo.",
    ]),
    ("Actividad Soulseek: buscar y descargar", [
        "Arriba escribes y «Bajar de Soulseek». Abajo, tres paneles:",
        "• Búsquedas — tus consultas, con un color por estado: verde = ya "
        "bajada, cian = tiene resultados sin bajar (elige), rojo = nadie "
        "respondió, ámbar = respondieron pero no pasan tus filtros.",
        "• Resultados — los ficheros de la búsqueda elegida. Los que no "
        "pasan el filtro salen en gris, pero puedes bajarlos igual.",
        "• Descargas — lo que se está bajando, con su estado.",
        "",
        "Arriba del todo hay un resumen con cuántas búsquedas hay, cuántas "
        "con respuesta y cuántas descargas van bien.",
    ]),
    ("Actividad: botones (solo iconos)", [
        "Pasa el ratón por encima y cada icono dice qué hace. Los clave:",
        "• ⟳ Rebuscar canceladas — relanza solas todas las descargas que "
        "fallaron, con la query con que se pidieron.",
        "• 🧹 Vaciar lista — quita de slskd lo terminado (no toca ficheros).",
        "• 🔍 Encontrar en Biblioteca (botón derecho) — salta a la canción "
        "de tu biblioteca a la que corresponde esa búsqueda o descarga.",
    ]),
    ("Descargas: colocar lo bajado", [
        "La pestaña Descargas lista lo que hay en tu carpeta de descargas.",
        "• «🪄 Sustituir (auto)» — pone la descarga en el sitio del original "
        "que le toca. Si Maceta la bajó, sabe cuál es; si no, lo busca por "
        "parecido de audio. El original sustituido va a archivo/.",
        "• «🔍 Encontrar en Biblioteca» (botón derecho) — localiza la "
        "canción original.",
    ]),
    ("Sustituir con seguridad", [
        "Cuando sustituyes, se abre «Comparar»: ves el espectro de la vieja "
        "y la nueva lado a lado, y cada una tiene un reproductor con barra "
        "para escuchar el mismo trozo en ambas antes de decidir.",
        "El original siempre se guarda en archivo/.",
    ]),
    ("La carpeta archivo/ y limpiarla", [
        "Cada sustitución deja el original en archivo/ (recuperable).",
        "En Preferencias → Opciones → Descargas → «🧹 Limpiar archivo…» "
        "devuelves a la biblioteca los originales que merezca la pena: "
        "buenos (320+/lossless) y no duplicados. Las copias y lo de baja "
        "calidad se quedan. Nada se borra.",
    ]),
    ("Estadísticas", [
        "La pestaña 📊 calcula (en segundo plano) todo sobre tu colección: "
        "calidad, formatos, espectro, ritmo, ranking de carpetas y "
        "artistas, y cosas curiosas: el fake más descarado, duplicados, y "
        "cuántos CDs, cintas o vinilos ocuparía si fuera física.",
    ]),
    ("Opciones (Preferencias → ⚙ Opciones…)", [
        "Un panel con TODO lo configurable, por pestañas: umbral de calidad, "
        "el Hz a partir del cual algo es FAKE, filtros de búsqueda, "
        "sustitución automática, rebúsqueda, usuarios de Soulseek baneados, "
        "slskd, actualizaciones, tipografía y colores. Cada ajuste lleva su "
        "explicación al lado.",
    ]),
    ("Menú Archivo / Ver / Análisis", [
        "ARCHIVO:",
        "• Seleccionar carpeta… (⌘O) — elige la carpeta de música.",
        "• Carpetas recientes — vuelve a una de las últimas que abriste.",
        "• Exportar CSV… (⌘S) — guarda la tabla en un CSV.",
        "• Salir (⌘Q).",
        "VER:",
        "• Ver espectro (⌘E) — gráfico del espectro de la canción.",
        "• ▶ Reproducir / ⏹ Parar.",
        "• Refrescar Actividad — relee búsquedas y descargas de slskd.",
        "ANÁLISIS:",
        "• Analizar selección (⌘R) / Analizar todas (⇧⌘R).",
        "• 🔄 Re-analizar — ignora lo ya recordado y vuelve a medir.",
        "• Convertir a MP3 320 — pasa a MP3 320 los lossless seleccionados.",
    ]),
    ("Menú Soulseek / Biblioteca", [
        "SOULSEEK:",
        "• Bajar con Soulseek… (⌘D) — busca reemplazo de la seleccionada.",
        "• Bajar todas FAKE — busca reemplazo de todas las marcadas FAKE.",
        "• Buscar por texto… (⌘F) — salta a la caja de búsqueda libre.",
        "• Cancelar y borrar todo — limpia búsquedas y descargas de slskd.",
        "• Limpiar búsquedas / descargas completadas.",
        "• 🚫 Usuarios baneados… — lista de peers que ya no se usan "
        "(se añaden solos los que te cancelan varias veces; editable).",
        "• ⚙ Config Soulseek… — abre las opciones de slskd.",
        "BIBLIOTECA:",
        "• 🔗 Sustituir con descarga… — elige a mano qué descarga reemplaza "
        "a la canción seleccionada.",
        "• Borrar (a archivo/) (⌫) — mueve a archivo/, no borra.",
        "• Vaciar lista — limpia la vista (no toca ficheros).",
    ]),
    ("Menú Preferencias / Ayuda", [
        "PREFERENCIAS:",
        "• Las cuatro casillas rápidas: Preguntar calidad, Sustituir "
        "original, Autoconvertir FLAC→MP3 320, Forzar 320.",
        "• ⚙ Opciones… (⌘,) — el panel completo de ajustes.",
        "• 🎨 Colores… — cambia los colores de la interfaz.",
        "AYUDA:",
        "• 📖 Manual de Maceta — esta ventana.",
        "• Comprobar / Configurar actualizaciones.",
        "• 📜 Ver changelog — las novedades de cada versión.",
        "• 📋 Abrir log — el registro para diagnosticar problemas.",
        "• Acerca de Maceta.",
    ]),
    ("Clic derecho: menús contextuales", [
        "Sobre una canción de Biblioteca (botón derecho) aparecen acciones "
        "que no están en los botones:",
        "• ▶ Reproducir · Ver espectro · Analizar.",
        "• Bajar con Soulseek… · 🔗 Sustituir con descarga…",
        "• Convertir a MP3 320 (si es lossless).",
        "• 🏷 Renombrar inteligente / editar tags… — ordena el nombre del "
        "fichero a partir de artista y título, o edita los tags.",
        "• Copiar ruta · Mostrar en Finder · Borrar (a archivo/).",
        "",
        "En Actividad y Descargas, el botón derecho ofrece «🔍 Encontrar "
        "en Biblioteca» y «💬 Escribir al usuario» (abre el chat con el "
        "peer que tiene el fichero).",
    ]),
    ("Atajos", [
        "• ⌘O — Seleccionar carpeta       • ⌘F — Buscar por texto",
        "• ⌘R — Analizar selección        • ⇧⌘R — Analizar todas",
        "• ⌘E — Ver espectro              • ⌘D — Bajar con Soulseek",
        "• ⌘, — Opciones                  • Espacio — Reproducir",
    ]),
    ("Si algo va mal", [
        "• Ayuda → «📋 Abrir log…» muestra el registro de lo que pasa.",
        "• Las descargas de Soulseek necesitan slskd corriendo; Maceta lo "
        "arranca sola si le diste su ruta en Opciones → slskd.",
        "• Recuerda: nada se borra. Lo sustituido está en archivo/, lo "
        "«borrado» en la papelera.",
    ]),
]


class ManualDialog(tk.Toplevel):
    """Manual de uso de Maceta, legible y con fondo oscuro."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Manual de Maceta")
        self.geometry("720x620")
        self.transient(master)

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text="📖 Manual de Maceta",
                  font=("TkDefaultFont", 15, "bold")).pack(anchor="w")
        ttk.Label(top, text="Todo lo que hace y cómo, sección a sección.",
                  foreground="#9a9a9a").pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self, padding=(12, 4))
        body.pack(fill=tk.BOTH, expand=True)
        txt = tk.Text(body, wrap="word", padx=10, pady=8,
                      background="#141414", foreground="#ededed",
                      insertbackground="#ededed", relief="flat",
                      highlightthickness=0, font=("TkDefaultFont", 12))
        vsb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.tag_configure("h", font=("TkDefaultFont", 13, "bold"),
                          foreground="#8ab4f8", spacing1=12, spacing3=4)
        for titulo, parrafos in MACETA_MANUAL:
            txt.insert(tk.END, titulo + "\n", "h")
            for p in parrafos:
                txt.insert(tk.END, (p or "") + "\n")
        txt.configure(state="disabled")

        btns = ttk.Frame(self, padding=(12, 4, 12, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Entendido",
                   command=self.destroy).pack(side=tk.RIGHT)


class WhatsNewDialog(tk.Toplevel):
    """Popup con las novedades entre la versión anterior y la actual."""

    def __init__(self, master, version, markdown_text):
        super().__init__(master)
        self.title(f"Novedades — v{version}")
        self.geometry("620x520")
        self.transient(master)

        top = ttk.Frame(self, padding=(12, 12, 12, 0))
        top.pack(fill=tk.X)
        ttk.Label(
            top,
            text=f"✨ Novedades en Maceta v{version}",
            font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            top,
            text="Esto es lo que ha cambiado desde tu versión anterior:",
            foreground="#9a9a9a",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self, padding=(12, 8))
        body.pack(fill=tk.BOTH, expand=True)
        text_frame = ttk.Frame(body)
        text_frame.pack(fill=tk.BOTH, expand=True)
        # Sin foreground explícito, en modo oscuro macOS pintaba las
        # letras en blanco sobre el fondo claro: ilegible.
        txt = tk.Text(text_frame, wrap="word", padx=8, pady=8,
                      background="#141414", foreground="#ededed",
                      insertbackground="#ededed",
                      selectbackground="#3874d4", selectforeground="#ffffff",
                      highlightthickness=0, relief="flat",
                      font=("TkDefaultFont", 11))
        vsb = ttk.Scrollbar(text_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.tag_configure("h2", font=("TkDefaultFont", 12, "bold"),
                          foreground="#8ab4f8", spacing1=8, spacing3=4)
        for line in markdown_text.splitlines():
            if line.startswith("## "):
                txt.insert(tk.END, line[3:] + "\n", "h2")
            else:
                txt.insert(tk.END, line + "\n")
        txt.configure(state="disabled")

        btns = ttk.Frame(self, padding=(12, 4, 12, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Vamos allá",
                   command=self.destroy).pack(side=tk.RIGHT)


class CompareDialog(tk.Toplevel):
    """Comparación side-by-side de original vs candidata antes de sustituir.

    - Metadata de ambos (formato, bitrate, duración, corte espectral, verdict).
    - Espectros dibujados con matplotlib (calculados en segundo plano).
    - Botones ▶/⏹ por lado para escuchar cada uno.
    - "✓ Sustituir" llama a `on_confirm()` (con la sustitución real).
    """

    def __init__(self, app, orig_path, cand_path, on_confirm,
                 orig_label="Original", cand_label="Candidata"):
        super().__init__(app)
        self.app = app
        self.orig_path = str(orig_path)
        self.cand_path = str(cand_path)
        self.on_confirm = on_confirm
        self.title(f"Comparar — {Path(orig_path).name} ↔ {Path(cand_path).name}")
        self.geometry("1180x680")
        self.transient(app)

        ttk.Label(
            self,
            text=("Escucha y compara los espectros antes de decidir. "
                  "El original se moverá a archivo/ solo si pulsas Sustituir."),
            foreground="#555", padding=(10, 8),
        ).pack(fill=tk.X)

        body = ttk.Frame(self, padding=(8, 0, 8, 4))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1, uniform="pane")
        body.columnconfigure(1, weight=1, uniform="pane")
        body.rowconfigure(0, weight=1)

        self._players = {}
        self._panels = {}
        self._panels["orig"] = self._make_side_panel(
            body, 0, orig_label, self.orig_path, accent="#a33",
            side_key="orig")
        self._panels["cand"] = self._make_side_panel(
            body, 1, cand_label, self.cand_path, accent="#2a6",
            side_key="cand")

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="Cancelar",
                   command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(btns, text="✓ Sustituir",
                   command=self._confirm).pack(side=tk.RIGHT, padx=6)

        # Lanzar cálculos en paralelo
        for side, path in (("orig", self.orig_path),
                           ("cand", self.cand_path)):
            threading.Thread(
                target=self._compute_spectrum_thread,
                args=(side, path), daemon=True,
            ).start()

    def _make_side_panel(self, parent, col, label, path, accent,
                         side_key="x"):
        frame = ttk.LabelFrame(parent, text=label, padding=8)
        frame.grid(row=0, column=col, sticky="nsew", padx=6)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        # Nombre + metadata
        name_lbl = tk.Label(
            frame, text=Path(path).name, anchor="w",
            font=("TkDefaultFont", 12, "bold"), fg=accent,
        )
        name_lbl.grid(row=0, column=0, sticky="ew")
        meta = get_metadata(path)
        size_mb = 0
        try:
            size_mb = Path(path).stat().st_size / 1024 / 1024
        except Exception:
            pass
        dur = meta.get("duration_s") or 0
        dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
        info_txt = (
            f"{meta.get('codec') or '?'} · "
            f"{meta.get('bitrate_kbps') or '?'} kbps · "
            f"{meta.get('sample_rate') or '?'} Hz · "
            f"{dur_str} · {size_mb:.1f} MB"
        )
        info_lbl = ttk.Label(frame, text=info_txt, foreground="#333")
        info_lbl.grid(row=1, column=0, sticky="w", pady=(2, 6))

        # Placeholder para el gráfico
        fig = Figure(figsize=(5.2, 3.8), dpi=90)
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "Analizando…", ha="center", va="center",
                fontsize=12, color="#888", transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=frame)
        canvas.get_tk_widget().grid(row=2, column=0, sticky="nsew")
        canvas.draw()

        # Reproductor con barra para moverse y comparar el mismo punto
        # en las dos versiones.
        player = ttk.Frame(frame)
        player.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        pl = AudioPlayer(self.app._find_ffmpeg())
        play_var = tk.StringVar(value="▶")
        pos_var = tk.DoubleVar(value=0.0)
        time_var = tk.StringVar(value="0:00 / 0:00")

        def _toggle():
            if pl.is_playing:
                pl.pause(); play_var.set("▶")
            else:
                if pl.data is None and not pl.load(path):
                    # sin seek: afplay desde el principio
                    pl.play(0)
                else:
                    pl.play()
                play_var.set("⏸")
                self._tick_player(side_key)

        def _seek(v):
            if pl.duration:
                pl.seek(float(v) / 1000.0 * pl.duration)

        btn = ttk.Button(player, textvariable=play_var, width=3,
                         command=_toggle)
        btn.pack(side=tk.LEFT)
        slider = ttk.Scale(player, from_=0, to=1000, variable=pos_var,
                           command=lambda v: None)
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        # arrastrar la barra = saltar a ese punto
        slider.bind("<ButtonRelease-1>",
                    lambda e: _seek(pos_var.get()))
        slider.bind("<B1-Motion>", lambda e: setattr(self, "_dragging_"+side_key, True))
        ttk.Label(player, textvariable=time_var,
                  foreground="#888").pack(side=tk.LEFT)
        self._players[side_key] = {
            "pl": pl, "play_var": play_var, "pos_var": pos_var,
            "time_var": time_var, "slider": slider}

        # Verdict local (lo rellena el hilo)
        verdict_var = tk.StringVar(value="…")
        verdict_lbl = ttk.Label(frame, textvariable=verdict_var,
                                foreground="#555")
        verdict_lbl.grid(row=4, column=0, sticky="w", pady=(4, 0))

        return {"figure": fig, "ax": ax, "canvas": canvas,
                "meta": meta, "verdict_var": verdict_var,
                "info_lbl": info_lbl}

    def _fmt_t(self, sec):
        sec = int(sec or 0)
        return f"{sec // 60}:{sec % 60:02d}"

    def _tick_player(self, side_key):
        d = self._players.get(side_key)
        if not d:
            return
        pl = d["pl"]
        dur = pl.duration or 0
        pos = pl.position
        if dur and not getattr(self, "_dragging_" + side_key, False):
            d["pos_var"].set(pos / dur * 1000.0)
        d["time_var"].set(f"{self._fmt_t(pos)} / {self._fmt_t(dur)}")
        if not pl.is_playing:
            d["play_var"].set("▶")
            if dur and pos >= dur - 0.3:
                d["pos_var"].set(0.0)
            return
        setattr(self, "_dragging_" + side_key, False)
        self.after(200, self._tick_player, side_key)

    def _compute_spectrum_thread(self, side, path):
        try:
            dur = 0
            try:
                dur = get_metadata(path).get("duration_s") or 0
            except Exception:
                pass
            load_seconds = 60.0
            if dur > load_seconds:
                offset = max(0.0, (dur - load_seconds) / 2)
                y, sr = librosa.load(path, sr=None, mono=True,
                                     offset=offset, duration=load_seconds)
            else:
                y, sr = librosa.load(path, sr=None, mono=True)
            freqs, psd_db = compute_spectrum(y, sr)
            cutoff = find_cutoff(freqs, psd_db)
            self.after(0, self._render_spectrum, side, freqs, psd_db,
                       sr, cutoff)
        except Exception as e:
            self.after(0, self._render_error, side, str(e))

    def _render_spectrum(self, side, freqs, psd_db, sr, cutoff):
        panel = self._panels.get(side)
        if not panel or not self.winfo_exists():
            return
        ax = panel["ax"]
        ax.clear()
        peak = float(np.max(psd_db))
        ax.plot(freqs / 1000.0, psd_db - peak, linewidth=0.8)
        nyq = min(22.05, (sr or 44100) / 2000.0)
        ax.set_xlim(0, nyq)
        ax.set_ylim(-100, 5)
        ax.set_xlabel("kHz", fontsize=9)
        ax.set_ylabel("dB rel.", fontsize=9)
        ax.grid(True, alpha=0.3)
        for f_khz, label in [(16, "128"), (19, "192"), (20, "256/320")]:
            ax.axvline(f_khz, color="gray", linestyle="--", alpha=0.5)
            ax.text(f_khz, 3, label, rotation=90, va="top", ha="right",
                    fontsize=7, alpha=0.6)
        if cutoff:
            ax.axvline(cutoff / 1000.0, color="red", linewidth=1.2)
            ax.text(cutoff / 1000.0, -3,
                    f"corte {int(cutoff)} Hz",
                    color="red", rotation=90, va="top", ha="right",
                    fontsize=8)
        panel["figure"].tight_layout()
        panel["canvas"].draw_idle()

        meta = panel["meta"]
        verdict, detail = judge(meta.get("bitrate_kbps"), cutoff,
                                meta.get("codec"))
        panel["verdict_var"].set(f"{verdict} · {detail}")

    def _render_error(self, side, err):
        panel = self._panels.get(side)
        if not panel or not self.winfo_exists():
            return
        ax = panel["ax"]
        ax.clear()
        ax.text(0.5, 0.5, f"Error:\n{err[:80]}", ha="center", va="center",
                fontsize=10, color="#a33", transform=ax.transAxes,
                wrap=True)
        ax.set_xticks([]); ax.set_yticks([])
        panel["canvas"].draw_idle()
        panel["verdict_var"].set(f"error: {err[:60]}")

    def _cancel(self):
        for d in getattr(self, "_players", {}).values():
            try: d["pl"].stop()
            except Exception: pass
        try:
            self.app.on_stop_playback()
        except Exception:
            pass
        self.destroy()

    def _confirm(self):
        try:
            self.app.on_stop_playback()
        except Exception:
            pass
        try:
            self.on_confirm()
        finally:
            self.destroy()


class SmartRenameDialog(tk.Toplevel):
    """Rename inteligente + editor de tags ID3/vorbis en una ventana.

    - Auto-detección desde el filename actual (limpia números, brackets,
      basura de bitrate).
    - Muestra los tags actuales editables (Title, Artist, Album, Track#,
      Año, Genre).
    - Previsualiza el nuevo nombre de fichero.
    - Botones para aplicar solo tags, solo renombrar, o los dos.
    """

    def __init__(self, app, iid, row):
        super().__init__(app)
        self.app = app
        self.iid = iid
        self.row = row
        self.path = Path(row["path"])
        self.title(f"Renombrar / tags — {self.path.name}")
        self.geometry("640x460")
        self.transient(app)
        self.grab_set()

        current_tags = read_song_tags(str(self.path))

        # Header
        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Archivo actual:",
                  foreground="#555").pack(anchor="w")
        ttk.Label(header, text=self.path.name,
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

        # Grid con tags editables
        body = ttk.Frame(self, padding=(12, 4))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self.vars = {}
        rows = [
            ("title",       "Título"),
            ("artist",      "Artista"),
            ("album",       "Álbum"),
            ("tracknumber", "Nº pista"),
            ("date",        "Año"),
            ("genre",       "Género"),
        ]
        for i, (key, label) in enumerate(rows):
            ttk.Label(body, text=label + ":").grid(
                row=i, column=0, sticky="w", padx=(0, 8), pady=3)
            var = tk.StringVar(value=current_tags.get(key, ""))
            self.vars[key] = var
            ttk.Entry(body, textvariable=var).grid(
                row=i, column=1, sticky="ew", pady=3)
            var.trace_add("write", lambda *_: self._refresh_preview())

        # Auto-detect button
        ttk.Button(body, text="🪄 Auto-detectar desde nombre",
                   command=self._auto_detect).grid(
                       row=len(rows), column=0, columnspan=2,
                       sticky="w", pady=(8, 0))

        # Preview del nuevo filename
        prev_frame = ttk.LabelFrame(self, text="Vista previa",
                                    padding=(12, 8))
        prev_frame.pack(fill=tk.X, padx=12, pady=(6, 4))
        ttk.Label(prev_frame, text="Nuevo nombre de fichero:").pack(anchor="w")
        self.new_name_var = tk.StringVar()
        self.new_name_entry = ttk.Entry(
            prev_frame, textvariable=self.new_name_var)
        self.new_name_entry.pack(fill=tk.X, pady=(4, 0))

        # Toggles + botones
        ctl = ttk.Frame(self, padding=(12, 4))
        ctl.pack(fill=tk.X)
        self.apply_tags_var = tk.BooleanVar(value=True)
        self.rename_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Actualizar tags ID3",
                        variable=self.apply_tags_var).pack(side=tk.LEFT)
        ttk.Checkbutton(ctl, text="Renombrar fichero",
                        variable=self.rename_var).pack(side=tk.LEFT, padx=12)

        btns = ttk.Frame(self, padding=(12, 8, 12, 12))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="Cancelar",
                   command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="✓ Aplicar",
                   command=self._apply).pack(side=tk.RIGHT, padx=6)

        # Refresh inicial
        self._refresh_preview()

    def _current_tags(self):
        return {k: v.get() for k, v in self.vars.items()}

    def _refresh_preview(self):
        tags = self._current_tags()
        proposed = suggest_filename(
            tags.get("artist", ""), tags.get("title", ""),
            self.path.suffix,
        )
        # Solo actualizamos el entry si está vacío o coincide con el
        # último auto-preview — así no pisamos ediciones manuales.
        cur = self.new_name_var.get().strip()
        if not cur or cur == getattr(self, "_last_preview", None):
            self.new_name_var.set(proposed)
        self._last_preview = proposed

    def _auto_detect(self):
        guess = parse_song_filename(self.path.stem)
        if guess.get("artist"):
            self.vars["artist"].set(guess["artist"])
        if guess.get("title"):
            self.vars["title"].set(guess["title"])
        # Fuerza el refresh del nombre propuesto
        self._last_preview = self.new_name_var.get().strip()
        self._refresh_preview()

    def _apply(self):
        new_name = self.new_name_var.get().strip()
        tags = self._current_tags()
        self.app.apply_rename_and_tags(
            self.iid, self.row, new_name, tags,
            write_tags=self.apply_tags_var.get(),
            do_rename=self.rename_var.get(),
        )
        self.destroy()


class SubstituteTargetDialog(tk.Toplevel):
    """Al revés que ManualSubstituteDialog: viene desde la pestaña
    Descargas con el fichero descargado ya elegido, y pides qué row de
    Biblioteca reemplazar."""

    def __init__(self, app, downloaded_path):
        super().__init__(app)
        self.app = app
        self.downloaded = downloaded_path
        self.title(f"Sustituir con — {downloaded_path.name}")
        self.geometry("820x520")
        self.transient(app)
        self.grab_set()

        ttk.Label(self, text=f"Fichero descargado: {downloaded_path.name}",
                  padding=8).pack(fill=tk.X)
        ttk.Label(self, text="Elige la canción original que reemplaza:",
                  padding=(8, 0)).pack(fill=tk.X)
        self._hint_var = tk.StringVar()
        ttk.Label(self, textvariable=self._hint_var, padding=(8, 2),
                  foreground="#1a7f37").pack(fill=tk.X)

        frame = ttk.Frame(self, padding=(8, 4))
        frame.pack(fill=tk.BOTH, expand=True)
        cols = ("match", "format", "bitrate", "verdict")
        self.tree = ttk.Treeview(
            frame, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Archivo")
        self.tree.column("#0", width=400, anchor=tk.W)
        for c, (t, w) in [("match", ("Parecido", 85)),
                          ("format", ("Formato", 70)),
                          ("bitrate", ("Bitrate", 90)),
                          ("verdict", ("Calidad", 130))]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._iid_to_row = {}
        link = get_download_link(downloaded_path.name)
        linked = (str(Path(link["original_path"]))
                  if link.get("original_path") else "")
        ranked = [(name_similarity(downloaded_path.stem, row.get("name") or ""),
                   iid, row)
                  for iid, row in app.rows_by_iid.items()]
        ranked.sort(key=lambda t: -t[0])
        self.tree.tag_configure("linked", foreground="#1a7f37")
        self.tree.tag_configure("match", foreground="#1a7f37")
        best = linked_iid = None
        for sim, iid, row in ranked:
            is_linked = bool(linked) and str(
                Path(row.get("path") or "")) == linked
            tags = (("linked",) if is_linked
                    else ("match",) if sim >= 0.75 else ())
            item_iid = self.tree.insert(
                "", tk.END, text=row["name"],
                values=(
                    "⬇ enlazada" if is_linked else f"{sim * 100:.0f}%",
                    row.get("codec") or "?",
                    f"{row['bitrate_kbps']} kbps"
                        if row.get("bitrate_kbps") else "?",
                    display_verdict(row),
                ),
                tags=tags,
            )
            self._iid_to_row[item_iid] = (iid, row)
            if is_linked:
                linked_iid = item_iid
            if best is None and sim >= 0.75:
                best = item_iid
        target = linked_iid or best
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        if linked_iid:
            self._hint_var.set(
                "Marcada la canción que esta descarga venía a reemplazar "
                "(link guardado al bajarla).")
        elif best:
            self._hint_var.set(
                "Preseleccionada la del nombre más parecido — compruébala "
                "antes de confirmar.")
        else:
            self._hint_var.set(
                "Ninguna se parece lo bastante; la lista va ordenada por "
                "parecido de nombre.")

        self.tree.bind("<Double-1>", lambda _e: self._confirm())
        btns = ttk.Frame(self, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="✓ Sustituir",
                   command=self._confirm).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cancelar",
                   command=self.destroy).pack(side=tk.LEFT, padx=6)

    def _confirm(self):
        sel = self.tree.selection()
        if not sel:
            return
        orig_iid, row = self._iid_to_row[sel[0]]

        def do_sub():
            self.app.do_manual_substitute(
                orig_iid, row, str(self.downloaded))
        CompareDialog(
            self.app,
            orig_path=row["path"],
            cand_path=str(self.downloaded),
            on_confirm=do_sub,
            orig_label="Original (Biblioteca)",
            cand_label="Candidata (descarga)",
        )
        self.destroy()


class ManualSubstituteDialog(tk.Toplevel):
    """Elige a mano qué descarga sustituye al original, con preview de ambas."""

    def __init__(self, app, orig_iid, row):
        super().__init__(app)
        self.app = app
        self.orig_iid = orig_iid
        self.row = row
        self.title(f"Sustituir manualmente — {row['name']}")
        self.geometry("820x520")
        self.transient(app)

        # Original
        orig_frame = ttk.LabelFrame(self, text="Canción original", padding=8)
        orig_frame.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(orig_frame, text=row["path"], anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(orig_frame, text="▶",
                   command=lambda: app.play_path(row["path"], row["name"])
                   ).pack(side=tk.LEFT, padx=4)
        ttk.Button(orig_frame, text="⏹",
                   command=app.on_stop_playback).pack(side=tk.LEFT)

        # Candidatos
        cand_frame = ttk.LabelFrame(
            self, text=f"Descargas disponibles en {DOWNLOADS_DIR}", padding=8
        )
        cand_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        wrap = ttk.Frame(cand_frame)
        wrap.pack(fill=tk.BOTH, expand=True)
        cols = ("match", "ext", "size", "duration")
        self.tree = ttk.Treeview(
            wrap, columns=cols, show="tree headings", selectmode="browse",
        )
        self.tree.tag_configure("linked", foreground="#1a7f37")
        self.tree.tag_configure("match", foreground="#1a7f37")
        self._sort_state = {}
        def _make_sorter(col):
            def _cb():
                desc = self._sort_state.get(col, False)
                sort_treeview_by_column(self.tree, col, descending=desc)
                self._sort_state[col] = not desc
            return _cb
        self.tree.heading("#0", text="Fichero", command=_make_sorter("#0"))
        self.tree.column("#0", width=420, anchor=tk.W)
        for c, (t, w) in [("match", ("Parecido", 85)),
                          ("ext", ("Ext", 60)),
                          ("size", ("MB", 80)),
                          ("duration", ("Duración", 90))]:
            self.tree.heading(c, text=t, command=_make_sorter(c))
            self.tree.column(c, width=w, anchor=tk.W)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._paths = {}  # iid -> Path
        # Primero las que se parecen al original: con 40 descargas en la
        # carpeta, buscar a ojo la que toca era lo pesado de este diálogo.
        orig_stem = Path(row["name"]).stem
        orig_path = str(Path(row["path"]))
        cands = []
        for p in DOWNLOADS_DIR.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS:
                continue
            link = get_download_link(p.name)
            linked = bool(link.get("original_path")) and \
                str(Path(link["original_path"])) == orig_path
            cands.append((linked, name_similarity(orig_stem, p.name), p))
        cands.sort(key=lambda t: (-t[0], -t[1], str(t[2]).lower()))

        best = linked_iid = None
        for linked, sim, p in cands:
            meta = get_metadata(str(p))
            size_mb = p.stat().st_size / 1024 / 1024
            dur = meta.get("duration_s") or 0
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            tags = ("linked",) if linked else (
                ("match",) if sim >= 0.75 else ())
            iid = self.tree.insert(
                "", tk.END, text=str(p.relative_to(DOWNLOADS_DIR)),
                values=("⬇ enlazada" if linked else f"{sim * 100:.0f}%",
                        p.suffix.lstrip(".").upper(),
                        f"{size_mb:.1f}", dur_str),
                tags=tags,
            )
            self._paths[iid] = p
            if linked and linked_iid is None:
                linked_iid = iid
            if best is None and sim >= 0.75:
                best = iid
        target = linked_iid or best
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="▶ Escuchar seleccionada",
                   command=self._preview).pack(side=tk.LEFT)
        ttk.Button(btns, text="⏹ Parar",
                   command=app.on_stop_playback).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cerrar",
                   command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="✓ Usar esta y sustituir",
                   command=self._confirm).pack(side=tk.RIGHT, padx=6)

        self.tree.bind("<Double-1>", lambda _e: self._preview())

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._paths.get(sel[0])

    def _preview(self):
        p = self._selected_path()
        if not p:
            return
        self.app.play_path(str(p), p.name)

    def _confirm(self):
        p = self._selected_path()
        if not p:
            messagebox.showinfo("Maceta", "Selecciona un archivo primero.")
            return
        self.app.on_stop_playback()

        orig_iid = self.orig_iid
        row = self.row
        picked = str(p)

        def do_sub():
            self.app.do_manual_substitute(orig_iid, row, picked)

        CompareDialog(
            self.app,
            orig_path=row["path"],
            cand_path=picked,
            on_confirm=do_sub,
            orig_label="Original (Biblioteca)",
            cand_label="Candidata (descarga)",
        )
        self.destroy()


# ---------- Panel único de opciones ----------
#
# Cada campo dice de dónde sale su valor: "app" es una tk var que ya vive
# en MacetaApp (se edita en vivo, el resto de la UI se entera al momento)
# y "cfg" es una clave del fichero de configuración (se escribe al Guardar).

OPTION_TABS = [
    ("Análisis", [
     {"src": "app", "key": "target_bitrate", "kind": "int",
      "min": 64, "max": 320, "step": 32,
      "label": "Target de calidad (kbps)",
      "hint": "Lo que Maceta considera una canción buena. Por debajo de "
              "esto intenta mejorarla."},
     {"src": "app", "key": "opportunistic_floor", "kind": "int",
      "min": 32, "max": 320, "step": 32,
      "label": "Mínimo aceptable (kbps)",
      "hint": "Suelo cuando no fuerzas 320: no se baja nada por debajo."},
     {"src": "app", "key": "fake_cutoff_floor_hz", "kind": "int",
      "min": 8000, "max": 22050, "step": 500,
      "label": "Corte para marcar FAKE (Hz)",
      "hint": "Por debajo de este corte espectral la canción se marca "
              "FAKE. 16000 Hz es el corte típico de un MP3 128."},
     {"src": "app", "key": "cutoff_threshold_db", "kind": "float",
      "min": -120.0, "max": -10.0, "step": 5.0,
      "label": "Sensibilidad del corte (dB)",
      "hint": "Energía mínima respecto al pico para considerar que ahí "
              "todavía hay señal. Más negativo = detecta cortes más altos."},
     {"src": "app", "key": "analysis_max_seconds", "kind": "int",
      "min": 5, "max": 3600, "step": 30,
      "label": "Audio analizado por canción (s)",
      "hint": "Cuánto se decodifica de cada fichero. Menos es más rápido "
              "y menos fiable para el BPM."},
     {"src": "app", "key": "max_file_size_mb", "kind": "int",
      "min": 1, "max": 2000, "step": 10,
      "label": "Tamaño máximo de descarga (MB)",
      "hint": "Descarta discos enteros colados como una sola canción."},
    ]),
    ("General", [
     {"src": "app", "key": "restore_last_folder_var", "kind": "bool",
      "label": "Reabrir la última carpeta al arrancar",
      "hint": "Vuelve a escanear donde lo dejaste. Apágalo si prefieres "
              "empezar siempre con la lista vacía."},
     {"src": "app", "key": "use_index_var", "kind": "bool",
      "label": "Recordar análisis ya hechos",
      "hint": "Guarda BPM y corte de cada fichero en ~/.maceta_index.json "
              "y los reutiliza en vez de volver a decodificar. Se invalida "
              "solo si el fichero cambia de tamaño o fecha."},
     {"kind": "action", "label": "Índice", "button": "Vaciar índice",
      "cmd": "clear_index",
      "hint": "Olvida todos los análisis guardados. La próxima vez habrá "
              "que analizar desde cero."},
     {"kind": "action", "label": "Ventana", "button": "Olvidar posición",
      "cmd": "reset_layout",
      "hint": "Descarta el tamaño, el sitio y los divisores guardados."},
    ]),
    ("Búsqueda", [
     {"src": "app", "key": "query_mode_var", "kind": "choice",
      "choices": ["raw", "clean", "tags"],
      "label": "Cómo se construye la query",
      "hint": "raw = nombre del fichero tal cual · clean = limpiando "
              "números y etiquetas · tags = artista y título del ID3."},
     {"src": "app", "key": "ask_quality_var", "kind": "bool",
      "label": "Preguntar calidad antes de bajar",
      "hint": "Abre el selector manual. Si un resultado se llama igual "
              "que la query se baja solo igualmente."},
     {"src": "app", "key": "force_320_var", "kind": "bool",
      "label": "Forzar 320 (rechaza menos)",
      "hint": "Ignora cualquier MP3 por debajo del target."},
     {"src": "app", "key": "exact_match_ratio", "kind": "float",
      "min": 0.5, "max": 1.0, "step": 0.01,
      "label": "Umbral de nombre exacto",
      "hint": "Parecido a partir del cual un resultado cuenta como la "
              "misma canción y se baja sin preguntar."},
     {"src": "app", "key": "plausible_name_ratio", "kind": "float",
      "min": 0.0, "max": 1.0, "step": 0.05,
      "label": "Umbral de nombre plausible",
      "hint": "Por debajo de esto se considera otra cosa (un directo, un "
              "cover) y pierde contra cualquiera que sí se llame igual, "
              "por grande que sea el fichero."},
     {"src": "app", "key": "search_min_mb", "kind": "float",
      "min": 0.0, "max": 2000.0, "step": 1.0,
      "label": "Tamaño mínimo (MB)", "hint": "0 = sin límite."},
     {"src": "app", "key": "search_max_mb", "kind": "float",
      "min": 0.0, "max": 2000.0, "step": 10.0,
      "label": "Tamaño máximo (MB)", "hint": "0 = sin límite."},
     {"src": "app", "key": "search_min_sec", "kind": "int",
      "min": 0, "max": 7200, "step": 30,
      "label": "Duración mínima (s)", "hint": "Filtra intros y jingles."},
     {"src": "app", "key": "search_max_sec", "kind": "int",
      "min": 0, "max": 7200, "step": 60,
      "label": "Duración máxima (s)", "hint": "0 = sin límite."},
     {"src": "app", "key": "search_only_available", "kind": "bool",
      "label": "Solo peers con slot libre",
      "hint": "Descarta a quien te pondría en cola."},
     {"src": "app", "key": "search_max_queue", "kind": "int",
      "min": 0, "max": 500, "step": 5,
      "label": "Cola máxima aceptada", "hint": "0 = sin límite."},
     {"src": "app", "key": "smart_search_var", "kind": "bool",
      "label": "Reintentar con menos palabras si no hay respuestas",
      "hint": "Si la búsqueda exacta no devuelve nada, prueba variantes "
              "más cortas: quita relleno y se queda con lo distintivo."},
     {"src": "app", "key": "ban_strikes_var", "kind": "int",
      "min": 1, "max": 20, "step": 1,
      "label": "Cancelaciones para banear a un peer",
      "hint": "Si un usuario nos tumba tantas descargas dentro de la "
              "ventana, deja de usarse al elegir resultados."},
     {"src": "app", "key": "ban_window_min_var", "kind": "int",
      "min": 5, "max": 1440, "step": 15,
      "label": "Ventana del baneo (minutos)",
      "hint": "Las cancelaciones más viejas que esto se olvidan."},
     {"kind": "action", "label": "Baneados", "button": "🚫 Editar lista…",
      "cmd": "banned",
      "hint": "Ver, quitar o añadir usuarios a mano."},
     {"src": "app", "key": "search_format_vars", "kind": "formats",
      "label": "Formatos aceptados", "hint": ""},
     {"src": "app", "key": "results_limit", "kind": "int",
      "min": 10, "max": 2000, "step": 50,
      "label": "Resultados mostrados por búsqueda",
      "hint": "Subirlo mucho ralentiza la tabla de Resultados."},
     {"src": "app", "key": "results_show_rejected", "kind": "bool",
      "label": "Mostrar también los descartados",
      "hint": "Los pinta en gris en vez de ocultarlos."},
    ]),
    ("Descargas", [
     {"src": "app", "key": "substitute_var", "kind": "bool",
      "label": "Sustituir el original al descargar",
      "hint": "El original se mueve a archivo/, nunca se borra."},
     {"src": "app", "key": "auto_flac_to_mp3_var", "kind": "bool",
      "label": "Autoconvertir FLAC → MP3 320",
      "hint": "Convierte con ffmpeg y borra el FLAC descargado."},
     {"src": "app", "key": "auto_substitute_var", "kind": "bool",
      "label": "Sustituir automático desde Descargas",
      "hint": "Usa el link guardado al bajar y, si no hay, compara el "
              "audio. Apagado, el botón pregunta siempre."},
     {"src": "app", "key": "audio_match_threshold", "kind": "float",
      "min": 0.3, "max": 1.0, "step": 0.01,
      "label": "Parecido de audio para sustituir",
      "hint": "Similitud de cromagrama mínima para dar dos ficheros por "
              "la misma canción. Más alto = más estricto."},
     {"kind": "action", "label": "Carpeta archivo/",
      "button": "🧹 Limpiar archivo…",
      "cmd": "clean_archive",
      "hint": "Devuelve a la biblioteca los originales archivados que "
              "sean buenos (320+/lossless) y no estén ya duplicados. "
              "Deja las copias repetidas y las de baja calidad."},
    ]),
    ("Reintentos", [
     {"src": "app", "key": "auto_retry_var", "kind": "bool",
      "label": "Rebuscar automáticamente lo que falla",
      "hint": "Reprograma las búsquedas sin resultado."},
     {"src": "app", "key": "auto_retry_interval_min", "kind": "int",
      "min": 1, "max": 600, "step": 5,
      "label": "Minutos entre reintentos", "hint": ""},
     {"src": "app", "key": "auto_retry_max_attempts", "kind": "int",
      "min": 0, "max": 100, "step": 1,
      "label": "Reintentos máximos", "hint": "0 = sin límite."},
     {"src": "app", "key": "max_auto_requery_var", "kind": "int",
      "min": 1, "max": 20, "step": 1,
      "label": "Rebúsquedas antes de preguntar",
      "hint": "Cuando una query llega a este número de intentos sin "
              "bajar nada, Maceta deja de insistir y te abre el selector."},
    ]),
    ("slskd", [
     {"src": "cfg", "key": "slskd_url", "kind": "str",
      "default": "http://localhost:5030",
      "label": "URL de slskd", "hint": ""},
     {"src": "cfg", "key": "slskd_api_key", "kind": "secret",
      "default": "", "label": "API key", "hint": ""},
     {"src": "cfg", "key": "slskd_binary", "kind": "path", "default": "",
      "label": "Binario slskd",
      "hint": "Opcional: si se indica, Maceta lo arranca al abrir."},
     {"src": "cfg", "key": "soulseekqt_binary", "kind": "path", "default": "",
      "label": "SoulseekQt (.app)", "hint": "Opcional, igual que arriba."},
     {"kind": "action", "label": "Conexión", "button": "Probar conexión",
      "cmd": "test_slskd", "hint": ""},
    ]),
    ("Actualizaciones", [
     {"src": "cfg", "key": "update_repo", "kind": "str",
      "default": DEFAULT_UPDATE_REPO, "label": "Repo", "hint": ""},
     {"src": "cfg", "key": "update_branch", "kind": "str",
      "default": DEFAULT_UPDATE_BRANCH, "label": "Rama", "hint": ""},
     {"src": "cfg", "key": "update_token", "kind": "secret", "default": "",
      "label": "GitHub token",
      "hint": "Personal access token con permiso de lectura del repo."},
     {"src": "cfg", "key": "auto_check_on_startup", "kind": "bool",
      "default": True, "label": "Comprobar al arrancar Maceta", "hint": ""},
     {"kind": "action", "label": "Versión", "button": "Comprobar ahora",
      "cmd": "test_update", "hint": ""},
    ]),
    ("Apariencia", [
     {"src": "app", "key": "font_family_var", "kind": "fontfamily",
      "label": "Tipografía de la interfaz",
      "hint": "Se aplica a menús, botones y tablas. «(la del sistema)» "
              "devuelve la de macOS."},
     {"src": "app", "key": "ui_font_size_var", "kind": "int",
      "min": 0, "max": 22, "step": 1,
      "label": "Tamaño base de la interfaz",
      "hint": "0 = el del sistema. Cada tabla mantiene además su propio "
              "A− / A+."},
     {"kind": "action", "label": "Tipografía", "button": "Aplicar ahora",
      "cmd": "apply_font",
      "hint": "Los cambios de tipografía se ven al pulsar aquí o al "
              "guardar."},
     {"kind": "action", "label": "Colores", "button": "🎨 Editar colores…",
      "cmd": "colors",
      "hint": "Fondo, texto y color de cada veredicto."},
     {"kind": "action", "label": "Columnas", "button": "Resetear anchos",
      "cmd": "reset_columns",
      "hint": "Vuelve a los anchos por defecto de todas las tablas."},
    ]),
]


class OptionsDialog(tk.Toplevel):
    """Todo lo configurable del programa en un sitio.

    Los campos "app" se editan directamente sobre las tk vars vivas, así
    que el resto de la UI (menús, chips) refleja el cambio al momento;
    Cancelar restaura el valor que tenían al abrir. Los campos "cfg"
    van al fichero de configuración al pulsar Guardar.
    """

    def __init__(self, app, tab=None):
        super().__init__(app)
        self.app = app
        self.title("Opciones de Maceta")
        self.geometry("980x640")
        self.transient(app)
        cfg = load_config()
        self._cfg_vars = {}
        self._snapshot = {}
        self._result_var = tk.StringVar(value="")
        self._result_labels = []

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        for title, fields in OPTION_TABS:
            frame = ttk.Frame(nb, padding=12)
            nb.add(frame, text=title)
            self._build_fields(frame, fields, cfg)
            if tab and title.lower().startswith(tab.lower()):
                nb.select(frame)

        ttk.Label(self, textvariable=self._result_var,
                  foreground="#1a7f37", padding=(12, 0)).pack(fill=tk.X)
        ttk.Label(
            self, padding=(12, 2), foreground="#777",
            text="Los umbrales de análisis se aplican a los próximos "
                 "análisis: para recalcular veredictos ya hechos, vuelve "
                 "a analizar la biblioteca.",
        ).pack(fill=tk.X)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Guardar",
                   command=self._on_save).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cancelar",
                   command=self._on_cancel).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Restaurar valores por defecto",
                   command=self._on_defaults).pack(side=tk.RIGHT)

    # ---------- construcción ----------

    def _build_fields(self, frame, fields, cfg):
        for r, f in enumerate(fields):
            ttk.Label(frame, text=f["label"]).grid(
                row=r, column=0, sticky="w", padx=(0, 12), pady=5)
            widget = self._widget_for(frame, f, cfg)
            widget.grid(row=r, column=1, sticky="w", pady=5)
            if f.get("hint"):
                ttk.Label(frame, text=f["hint"], foreground="#777",
                          wraplength=430, justify="left").grid(
                    row=r, column=2, sticky="w", padx=(16, 0))
        frame.columnconfigure(2, weight=1)

    def _var_for(self, f, cfg):
        if f["src"] == "app":
            var = getattr(self.app, f["key"])
            if f["kind"] == "formats":
                self._snapshot[f["key"]] = {k: v.get()
                                            for k, v in var.items()}
            else:
                self._snapshot[f["key"]] = var.get()
            return var
        raw = cfg.get(f["key"], f.get("default", ""))
        var = (tk.BooleanVar(value=bool(raw)) if f["kind"] == "bool"
               else tk.StringVar(value="" if raw is None else str(raw)))
        self._cfg_vars[f["key"]] = var
        return var

    def _widget_for(self, frame, f, cfg):
        kind = f["kind"]
        if kind == "action":
            box = ttk.Frame(frame)
            ttk.Button(box, text=f["button"],
                       command=getattr(self, f"_do_{f['cmd']}")
                       ).pack(side=tk.LEFT)
            # El resultado va aquí mismo: antes solo aparecía en una
            # etiqueta al fondo del diálogo y no se veía.
            lbl = ttk.Label(box, textvariable=self._result_var,
                            foreground="#1a7f37", wraplength=330,
                            justify="left")
            lbl.pack(side=tk.LEFT, padx=10)
            self._result_labels.append(lbl)
            return box
        var = self._var_for(f, cfg)
        if kind == "bool":
            return ttk.Checkbutton(frame, variable=var)
        if kind in ("int", "float"):
            return ttk.Spinbox(
                frame, textvariable=var, width=12,
                from_=f.get("min", 0), to=f.get("max", 10_000),
                increment=f.get("step", 1))
        if kind == "choice":
            return ttk.Combobox(frame, textvariable=var, width=14,
                                state="readonly", values=f["choices"])
        if kind == "fontfamily":
            return ttk.Combobox(frame, textvariable=var, width=28,
                                state="readonly",
                                values=self.app.available_font_families())
        if kind == "secret":
            return ttk.Entry(frame, textvariable=var, show="•", width=46)
        if kind == "path":
            box = ttk.Frame(frame)
            ttk.Entry(box, textvariable=var, width=40).pack(side=tk.LEFT)
            ttk.Button(box, text="…", width=3,
                       command=lambda v=var: self._pick_path(v)).pack(
                side=tk.LEFT, padx=4)
            return box
        if kind == "formats":
            box = ttk.Frame(frame)
            for ext, v in var.items():
                ttk.Checkbutton(box, text=ext, variable=v).pack(side=tk.LEFT)
            return box
        return ttk.Entry(frame, textvariable=var, width=46)

    def _pick_path(self, var):
        p = filedialog.askopenfilename(title="Elige el binario")
        if p:
            var.set(p)

    # ---------- acciones ----------

    def _resultado(self, texto, ok=True):
        self._result_var.set(texto)
        for lbl in self._result_labels:
            try:
                lbl.configure(foreground="#1a7f37" if ok else "#d04a3f")
            except Exception:
                pass

    def _do_test_slskd(self):
        url = self._cfg_vars["slskd_url"].get().strip()
        key = self._cfg_vars["slskd_api_key"].get().strip()
        if not url:
            self._resultado("Falta la URL de slskd", ok=False)
            return
        # Sin esto la ventana se queda muda (y congelada) mientras dura
        # la petición: hasta 5 s si slskd no contesta.
        self._resultado(f"Probando {url}…")
        self.update_idletasks()

        def worker():
            try:
                info = SlskdClient(url, key).ping()
                ver = info.get("version", "?")
                self.after(0, self._resultado,
                           f"✓ Conectado — slskd {ver}", True)
            except Exception as e:
                msg = str(e)
                if "Connection refused" in msg or "Max retries" in msg:
                    pista = (f"✗ Nadie responde en {url}. ¿Has arrancado "
                             "slskd? Pruébalo en un terminal.")
                elif "401" in msg or "403" in msg:
                    pista = ("✗ slskd responde pero rechaza la API key. "
                             "Comprueba que es la misma que en slskd.yml.")
                else:
                    pista = f"✗ {msg[:160]}"
                self.after(0, self._resultado, pista, False)
        threading.Thread(target=worker, daemon=True,
                         name="test-slskd").start()

    def _do_test_update(self):
        self._resultado("Comprobando…")
        self.update_idletasks()
        try:
            info = check_for_update(
                self._cfg_vars["update_token"].get().strip(),
                self._cfg_vars["update_repo"].get().strip(),
                self._cfg_vars["update_branch"].get().strip())
            self._resultado(
                f"✓ Local v{VERSION} · remoto v{info['remote_version']} "
                f"({'hay actualización' if info['is_newer'] else 'al día'})")
        except Exception as e:
            self._resultado(f"✗ {str(e)[:160]}", False)

    def _do_banned(self):
        BannedPeersDialog(self.app)

    def _do_apply_font(self):
        self.app._apply_ui_font()
        self._result_var.set("Tipografía aplicada")

    def _do_colors(self):
        ColorEditorDialog(self.app)

    def _do_clean_archive(self):
        self._resultado("Analizando archivo/…")
        self.update_idletasks()
        plan = plan_clean_archive()
        restore = plan["restore"]
        if not restore:
            self._resultado(
                "Nada que restaurar: %d duplicadas, %d de baja calidad, "
                "%d copias." % (plan["dup"], plan["mala"], plan["ts"]),
                ok=False)
            return
        if not messagebox.askyesno(
            "Maceta",
            f"Devolver a la biblioteca {len(restore)} canción(es) "
            f"archivadas?\n\n"
            f"Se restauran solo las buenas (320+/lossless) y no "
            f"duplicadas.\n\n"
            f"Se quedan en archivo/:\n"
            f"  · {plan['dup']} ya están en la biblioteca\n"
            f"  · {plan['mala']} por debajo de 320\n"
            f"  · {plan['ts']} copias repetidas\n\n"
            "Nada se borra.",
        ):
            return
        n = run_clean_archive(plan)
        self._resultado(f"✓ {n} canción(es) restauradas a su sitio. "
                        "Reinicia o re-escanea para verlas.")
        self.app.status.set(f"archivo/ limpiado: {n} restauradas")

    def _do_clear_index(self):
        n = clear_index()
        self._result_var.set(f"Índice vaciado: {n} análisis olvidados")

    def _do_reset_layout(self):
        save_user_state({"window_geometry": "", "sash": {}, "active_tab": 0})
        self._result_var.set(
            "Posición olvidada — se nota al reiniciar Maceta")

    def _do_reset_columns(self):
        save_user_state({"column_widths": {}})
        self._result_var.set(
            "Anchos reseteados — se ven al reiniciar Maceta")

    # ---------- guardar / cancelar ----------

    def _on_save(self):
        cfg = load_config()
        for key, var in self._cfg_vars.items():
            v = var.get()
            cfg[key] = v if isinstance(v, bool) else str(v).strip()
        save_config(cfg)
        self.app._save_current_state()
        self.app._apply_ui_font()
        apply_tunables()
        self.app._update_pref_indicator()
        self.app.status.set("Opciones guardadas")
        self.destroy()

    def _on_cancel(self):
        for key, old in self._snapshot.items():
            var = getattr(self.app, key)
            if isinstance(old, dict):
                for ext, val in old.items():
                    var[ext].set(val)
            else:
                var.set(old)
        self.destroy()

    def _on_defaults(self):
        if not messagebox.askyesno(
            "Maceta",
            "¿Restaurar todas las opciones a sus valores por defecto?",
        ):
            return
        for _title, fields in OPTION_TABS:
            for f in fields:
                if f.get("kind") == "action":
                    continue
                if f["src"] == "app":
                    var = getattr(self.app, f["key"])
                    if f["kind"] == "formats":
                        defaults = {
                            s.strip() for s in
                            USER_STATE_DEFAULTS["search_formats"].split(",")}
                        for ext, v in var.items():
                            v.set(ext in defaults)
                    else:
                        state_key = f["key"].removesuffix("_var")
                        if state_key in USER_STATE_DEFAULTS:
                            var.set(USER_STATE_DEFAULTS[state_key])
                else:
                    self._cfg_vars[f["key"]].set(f.get("default", ""))
        self._result_var.set("Valores por defecto puestos — pulsa Guardar")


COLOR_ESSENTIALS = [
    ("tree_bg",          "Fondo tabla"),
    ("tree_fg",          "Texto tabla"),
    ("tree_selected_bg", "Selección tabla"),
    ("banner_bg",        "Fondo aviso amarillo"),
    ("verdict_OK",       "Texto OK"),
    ("verdict_FAKE",     "Texto FAKE"),
    ("verdict_LOSSLESS", "Texto LOSSLESS"),
]


class SoulseekDiagDialog(tk.Toplevel):
    """Revisa que Soulseek esté bien y arregla lo que pueda.

    Comprueba lo que nos ha dado problemas una y otra vez: que slskd
    responda, que esté conectado a la red, que descargue DONDE Maceta
    mira, y que compartas algo (si no, las descargas se quedan en cola)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Diagnóstico de Soulseek")
        self.geometry("680x520")
        self.transient(app)

        top = ttk.Frame(self, padding=(14, 12, 14, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text="🩺 Diagnóstico de Soulseek",
                  font=("TkDefaultFont", 15, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="↻ Revisar de nuevo",
                   command=self._run).pack(side=tk.RIGHT)

        self.rows_frame = ttk.Frame(self, padding=(14, 4))
        self.rows_frame.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar(value="Revisando…")
        ttk.Label(self, textvariable=self.status_var, foreground="#888",
                  padding=(14, 0, 14, 10)).pack(fill=tk.X)
        self._run()

    def _run(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.status_var.set("Revisando…")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        cfg = load_config()
        url = (cfg.get("slskd_url") or "").strip()
        key = (cfg.get("slskd_api_key") or "").strip()
        binario = (cfg.get("slskd_binary") or "").strip()
        checks = []

        # 1. configurado
        if url and key:
            checks.append(("ok", "slskd configurado en Maceta",
                           f"{url}", None, None))
        else:
            checks.append(("bad", "slskd sin configurar",
                           "Falta URL o API key.",
                           "Abrir Opciones", "opciones"))
            self.after(0, self._render, checks)
            return

        client = SlskdClient(url, key)

        # 2. responde
        try:
            info = client.ping()
            ver = info.get("version", {})
            ver = ver.get("current") if isinstance(ver, dict) else ver
            checks.append(("ok", "slskd responde", f"versión {ver}", None, None))
        except Exception as e:
            arranca = ("Arrancar slskd", "arrancar") if binario else (
                None, None)
            checks.append(("bad", "slskd no responde",
                           f"Nadie contesta en {url}. ¿Está arrancado?",
                           arranca[0], arranca[1]))
            self.after(0, self._render, checks)
            return

        # 3. conectado a Soulseek
        srv = client.server_state()
        if srv.get("isLoggedIn"):
            checks.append(("ok", "Conectado a Soulseek",
                           f"estado: {srv.get('state')}", None, None))
        else:
            checks.append(("bad", "No conectado a Soulseek",
                           "Revisa usuario/contraseña en el slskd.yml.",
                           None, None))

        # 4. descarga donde Maceta mira
        dl = slskd_download_dir()
        quiere = str(DOWNLOADS_DIR)
        if dl and Path(dl).resolve() == Path(quiere).resolve():
            checks.append(("ok", "Descarga donde Maceta mira", dl, None, None))
        elif not dl:
            checks.append(("warn", "slskd descarga en su carpeta por defecto",
                           f"Maceta mira en {quiere}, pero slskd no tiene "
                           "fijado dónde descargar: lo que bajes no "
                           "aparecerá en Descargas.",
                           "Arreglar", "fix_dir"))
        else:
            checks.append(("warn", "slskd descarga en otro sitio",
                           f"slskd → {dl}\nMaceta mira → {quiere}",
                           "Arreglar", "fix_dir"))

        # 5. comparte algo
        shares = client.shares()
        if shares:
            carpetas = [sh.get("localPath") or sh.get("raw") or ""
                        for sh in shares if isinstance(sh, dict)]
            ficheros = 0
            for c in carpetas:
                try:
                    ficheros += sum(1 for _ in Path(c).rglob("*")
                                    if _.is_file())
                except Exception:
                    pass
            if ficheros > 0:
                checks.append(("ok", f"Compartes {ficheros} ficheros",
                               ", ".join(Path(c).name for c in carpetas),
                               None, None))
            else:
                checks.append(("warn", "Compartes una carpeta, pero vacía",
                               "Muchos peers rechazan a quien no comparte "
                               "nada y tus descargas se quedan en cola. "
                               "Copia algo de música a la carpeta "
                               "compartida.",
                               "Abrir carpeta", "abrir_share"))
                self._share_dir = carpetas[0] if carpetas else ""
        else:
            checks.append(("bad", "No compartes nada",
                           "Es la causa más común de que las descargas no "
                           "arranquen. Comparte una carpeta en el "
                           "slskd.yml.", None, None))

        # 6. mensajes sin leer
        try:
            convs = client.list_conversations()
            sin_leer = sum(1 for c in convs
                           if c.get("unAcknowledgedMessageCount", 0)
                           or c.get("hasUnAcknowledgedMessages"))
            if sin_leer:
                checks.append(("warn", f"{sin_leer} conversaciones sin leer",
                               "Suelen ser avisos de peers (a menudo: "
                               "'comparte algo'). Míralas en la pestaña "
                               "Mensajes.", None, None))
        except Exception:
            pass

        self.after(0, self._render, checks)

    def _render(self, checks):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        icon = {"ok": ("✓", "#1a7f37"), "warn": ("⚠", "#c9890a"),
                "bad": ("✗", "#d04a3f")}
        malos = sum(1 for c in checks if c[0] != "ok")
        for estado, titulo, detalle, boton, accion in checks:
            sym, col = icon[estado]
            fila = ttk.Frame(self.rows_frame, padding=(0, 5))
            fila.pack(fill=tk.X)
            tk.Label(fila, text=sym, fg=col, font=("TkDefaultFont", 14, "bold"),
                     width=2).pack(side=tk.LEFT, anchor="n")
            col_txt = ttk.Frame(fila)
            col_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Label(col_txt, text=titulo, anchor="w",
                     font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
            if detalle:
                tk.Label(col_txt, text=detalle, anchor="w", justify="left",
                         fg="#999", wraplength=460).pack(anchor="w")
            if boton and accion:
                ttk.Button(fila, text=boton,
                           command=lambda a=accion: self._do(a)
                           ).pack(side=tk.RIGHT, anchor="n")
        self.status_var.set(
            "Todo correcto ✓" if not malos
            else f"{malos} cosa(s) por revisar")

    def _do(self, accion):
        if accion == "opciones":
            self.app.on_options(tab="slskd")
        elif accion == "arrancar":
            self.status_var.set("Arrancando slskd…")
            msgs, failed = maybe_launch_helpers()
            self.status_var.set(" · ".join(msgs) or "hecho")
            self.after(1500, self._run)
        elif accion == "fix_dir":
            if not messagebox.askyesno(
                "Maceta",
                f"Fijar que slskd descargue en:\n{DOWNLOADS_DIR}\n\n"
                "Se edita su configuración (con copia de seguridad) y se "
                "reinicia slskd. Nada se borra."):
                return
            self.status_var.set("Arreglando y reiniciando slskd…")
            threading.Thread(target=self._fix_dir_worker, daemon=True).start()
        elif accion == "abrir_share":
            d = getattr(self, "_share_dir", "")
            if d and sys.platform == "darwin":
                subprocess.Popen(["open", d])

    def _fix_dir_worker(self):
        ok = slskd_set_download_dir(str(DOWNLOADS_DIR))
        if ok:
            cfg = load_config()
            binario = (cfg.get("slskd_binary") or "").strip()
            if binario and Path(binario).exists():
                try:
                    subprocess.run(["pkill", "-9", "-f",
                                    Path(binario).name], check=False)
                except Exception:
                    pass
                time.sleep(3)
                _spawn_detached([binario], cwd=str(Path(binario).parent),
                                log_path=SLSKD_LOG)
                time.sleep(6)
        self.after(0, lambda: self.status_var.set(
            "Arreglado — slskd descarga ahora donde Maceta mira"
            if ok else "No se pudo editar el slskd.yml"))
        self.after(1500, self._run)


class BannedPeersDialog(tk.Toplevel):
    """Lista de usuarios de Soulseek que Maceta ya no usa, editable.

    Se llena sola cuando un peer nos cancela varias descargas seguidas,
    pero la última palabra es del usuario: aquí se quita o se añade a
    mano quien haga falta."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Usuarios baneados")
        self.geometry("520x460")
        self.transient(app)

        ttk.Label(
            self, padding=(12, 10, 12, 4), justify="left", wraplength=480,
            text="Estos usuarios no se eligen al buscar. Se añaden solos "
                 "cuando cancelan varias descargas seguidas, y puedes "
                 "quitarlos o añadir otros a mano.",
        ).pack(fill=tk.X)

        box = ttk.Frame(self, padding=(12, 0))
        box.pack(fill=tk.BOTH, expand=True)
        self.listbox = tk.Listbox(box, selectmode=tk.EXTENDED)
        vsb = ttk.Scrollbar(box, orient="vertical",
                            command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        add = ttk.Frame(self, padding=(12, 8))
        add.pack(fill=tk.X)
        self.new_var = tk.StringVar()
        entry = ttk.Entry(add, textvariable=self.new_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._add())
        ttk.Button(add, text="Añadir", command=self._add).pack(
            side=tk.LEFT, padx=6)

        btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Quitar seleccionados",
                   command=self._remove).pack(side=tk.LEFT)
        ttk.Button(btns, text="Vaciar lista",
                   command=self._clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cerrar",
                   command=self.destroy).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, padding=(12, 0, 12, 8),
                  foreground="#777").pack(fill=tk.X)
        self._refresh()

    def _refresh(self):
        self.listbox.delete(0, tk.END)
        for u in sorted(self.app.banned_peers, key=str.lower):
            self.listbox.insert(tk.END, u)
        pend = {u: len(v) for u, v in (self.app.peer_strikes or {}).items()
                if v}
        extra = ""
        if pend:
            top = sorted(pend.items(), key=lambda kv: -kv[1])[:3]
            extra = " · con avisos: " + ", ".join(
                f"{u} ({n})" for u, n in top)
        self.status_var.set(f"{len(self.app.banned_peers)} baneado(s){extra}")

    def _add(self):
        u = self.new_var.get().strip()
        if u and u not in self.app.banned_peers:
            self.app.banned_peers.append(u)
            self.app._save_current_state()
        self.new_var.set("")
        self._refresh()

    def _remove(self):
        for i in reversed(self.listbox.curselection()):
            u = self.listbox.get(i)
            if u in self.app.banned_peers:
                self.app.banned_peers.remove(u)
            self.app.peer_strikes.pop(u, None)
        self.app._save_current_state()
        self._refresh()

    def _clear(self):
        if not self.app.banned_peers:
            return
        if not messagebox.askyesno(
            "Maceta",
            f"¿Quitar los {len(self.app.banned_peers)} usuarios de la "
            "lista?\n\nVolverán a usarse en las búsquedas.",
        ):
            return
        self.app.banned_peers.clear()
        self.app.peer_strikes.clear()
        self.app._save_current_state()
        self._refresh()


class ColorEditorDialog(tk.Toplevel):
    """Editor sencillo: cambia un color con un click, se aplica y guarda al
    instante. No hay 'Guardar' ni 'Cerrar' — cambio en vivo."""

    def __init__(self, master):
        super().__init__(master)
        self.master_app = master
        self.title("🎨 Colores")
        self.geometry("380x330")
        self.transient(master)

        ttk.Label(
            self,
            text="Click en cualquier cuadro para cambiar el color.\n"
                 "El cambio se aplica y guarda al instante.",
            foreground="#555", padding=(12, 10),
        ).pack(fill=tk.X)

        body = ttk.Frame(self, padding=(12, 4))
        body.pack(fill=tk.BOTH, expand=True)
        self._swatches = {}
        for i, (key, label) in enumerate(COLOR_ESSENTIALS):
            ttk.Label(body, text=label, width=24, anchor="w").grid(
                row=i, column=0, sticky="w", pady=4)
            sw = tk.Label(body, text="   ", width=6,
                          bg=master.colors.get(key, "#ffffff"),
                          relief="raised", borderwidth=1, cursor="hand2")
            sw.grid(row=i, column=1, padx=6, sticky="w")
            sw.bind("<Button-1>",
                    lambda _e, k=key: self._pick(k))
            self._swatches[key] = sw

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="Restaurar defaults",
                   command=self._reset).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cerrar",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _pick(self, key):
        try:
            _rgb, hex_ = colorchooser.askcolor(
                self.master_app.colors.get(key, "#ffffff"),
                parent=self, title=key,
            )
        except Exception as e:
            messagebox.showerror("Maceta", f"Color picker error: {e}")
            return
        if not hex_:
            return
        self.master_app.colors[key] = hex_
        self._swatches[key].configure(bg=hex_)
        self.master_app._apply_colors()
        save_colors(self.master_app.colors)
        self.master_app.status.set(f"Color {key} → {hex_}")

    def _reset(self):
        defaults = {k: d for k, _l, d in COLOR_SPEC}
        self.master_app.colors = defaults
        for key, sw in self._swatches.items():
            sw.configure(bg=defaults[key])
        self.master_app._apply_colors()
        save_colors(defaults)
        self.master_app.status.set("Colores restaurados a defaults")


class SpectrumWindow(tk.Toplevel):
    def __init__(self, master, row):
        super().__init__(master)
        self.title(f"Espectro – {row['name']}")
        self.geometry("820x540")
        fig = Figure(figsize=(8, 5), dpi=100)
        ax = fig.add_subplot(111)
        freqs = row["freqs"]
        psd_db = row["psd_db"]
        peak = float(np.max(psd_db))
        ax.plot(freqs / 1000.0, psd_db - peak, linewidth=0.8)
        nyquist_khz = (row["sr_used"] or 44100) / 2000.0
        ax.set_xlim(0, min(22.05, nyquist_khz))
        ax.set_ylim(-100, 5)
        ax.set_xlabel("Frecuencia (kHz)")
        ax.set_ylabel("Nivel relativo al pico (dB)")
        ax.grid(True, alpha=0.3)
        for f_khz, label in [(16, "≈ 128 kbps"),
                             (19, "≈ 192 kbps"),
                             (20, "≈ 256/320 kbps")]:
            ax.axvline(f_khz, color="gray", linestyle="--", alpha=0.5)
            ax.text(f_khz, 3, label, rotation=90, va="top", ha="right",
                    fontsize=8, alpha=0.75)
        if row["cutoff_hz"]:
            c = row["cutoff_hz"] / 1000.0
            ax.axvline(c, color="red", linewidth=1.2)
            ax.text(c, -3, f"corte {row['cutoff_hz']} Hz", color="red",
                    rotation=90, va="top", ha="right", fontsize=9)
        title_bits = [row["name"]]
        if row["bitrate_kbps"]:
            title_bits.append(f"{row['bitrate_kbps']} kbps declarado")
        title_bits.append(f"veredicto: {row['verdict']}")
        ax.set_title(" — ".join(title_bits), fontsize=10)
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(canvas, self)
        toolbar.update()


class ResearchDialog(tk.Toplevel):
    """Diálogo para editar la query de una búsqueda y relanzarla.

    Descompone filename y tags en palabras marcables para construir la query
    a golpe de click; el campo de texto sigue siendo editable a mano y ambos
    se mantienen sincronizados."""

    def __init__(self, master, initial_query, path=None):
        super().__init__(master)
        self.title("Rebuscar en Soulseek")
        self.resizable(True, False)
        self.transient(master)
        self.grab_set()
        self.result = None
        self._syncing = False

        groups = self._collect_groups(initial_query, path)
        self.all_tokens = [w for _, words in groups for w in words]
        self._order = {self._norm(w): i for i, w in enumerate(self.all_tokens)}

        ttk.Label(self, text="Nuevo texto de búsqueda:",
                  padding=(10, 10, 10, 4)).pack(anchor="w")
        self.var = tk.StringVar(value=initial_query)
        entry = ttk.Entry(self, textvariable=self.var, width=60)
        entry.pack(fill=tk.X, padx=10)
        entry.select_range(0, tk.END)
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self.on_ok())

        ttk.Label(self, text="Palabras disponibles (marca las que quieras "
                             "incluir):", padding=(10, 10, 10, 2)
                  ).pack(anchor="w")
        self.chips = {}
        chips_box = ttk.Frame(self, padding=(10, 0))
        chips_box.pack(fill=tk.X)
        for label, words in groups:
            self._build_group(chips_box, label, words)

        self.tip = ttk.Label(self, padding=(10, 8), foreground="#888",
                             wraplength=560, justify=tk.LEFT)
        self.tip.pack(anchor="w", fill=tk.X)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Buscar", command=self.on_ok).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cancelar",
                   command=self.destroy).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Limpiar",
                   command=lambda: self._set_query("")).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Todo", command=self._select_all
                   ).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btns, text="Quitar  -  ( )", command=self._strip_symbols
                   ).pack(side=tk.RIGHT)

        self.var.trace_add("write", lambda *_a: self._on_text_change())
        self._sync_chips()
        self._update_tip()
        self.wait_window(self)

    @staticmethod
    def _collect_groups(initial_query, path):
        """[(etiqueta, [palabras])] sin repetir palabras entre grupos."""
        raw = []
        if path:
            artist, title, album = _tags_of({"path": path})
            raw += [("Artista", artist), ("Título", title),
                    ("Álbum", album), ("Fichero", Path(path).stem)]
        raw.append(("Búsqueda", initial_query))
        groups, seen = [], set()
        for label, text in raw:
            words = [w for w in query_tokens(text) if w.lower() not in seen]
            if not words:
                continue
            seen.update(w.lower() for w in words)
            groups.append((label, words))
        return groups

    def _build_group(self, parent, label, words):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=1)
        ttk.Label(row, text=f"{label}:", width=9).pack(side=tk.LEFT,
                                                       anchor="nw")
        wrap = ttk.Frame(row)
        wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)
        line, used = ttk.Frame(wrap), 0
        line.pack(fill=tk.X, anchor="w")
        for w in words:
            if used and used + len(w) > 52:
                line, used = ttk.Frame(wrap), 0
                line.pack(fill=tk.X, anchor="w")
            var = tk.BooleanVar(value=False)
            self.chips[w] = var
            ttk.Checkbutton(
                line, text=w, variable=var,
                command=lambda word=w, v=var: self._apply_chip(word, v.get()),
            ).pack(side=tk.LEFT, padx=(0, 6))
            used += len(w) + 3

    @staticmethod
    def _norm(word):
        """Clave de comparación: el texto conserva guiones y paréntesis,
        los chips no, así que ambos se comparan normalizados."""
        return word.strip("-()[]{}_,.;:!?\"“”«»'`").lower()

    def _set_query(self, text):
        self.var.set(text)

    def _strip_symbols(self):
        self._set_query(_clean_keep_brackets(self.var.get()))

    def _on_text_change(self):
        if self._syncing:
            return
        self._sync_chips()
        self._update_tip()

    def _sync_chips(self):
        """Marca cada chip según esté o no su palabra en el texto actual."""
        present = {self._norm(w) for w in self.var.get().split()}
        self._syncing = True
        try:
            for word, var in self.chips.items():
                var.set(self._norm(word) in present)
        finally:
            self._syncing = False

    def _apply_chip(self, word, on):
        if self._syncing:
            return
        words = self.var.get().split()
        key = self._norm(word)
        if on:
            if key in {self._norm(w) for w in words}:
                return
            idx = self._order.get(key, len(self.all_tokens))
            pos = len(words)
            for i, w in enumerate(words):
                if self._order.get(self._norm(w), -1) > idx:
                    pos = i
                    break
            words.insert(pos, word)
        else:
            words = [w for w in words if self._norm(w) != key]
        self._syncing = True
        try:
            self.var.set(" ".join(words))
        finally:
            self._syncing = False
        self._update_tip()

    def _select_all(self):
        self._set_query(" ".join(self.all_tokens))

    def _update_tip(self):
        words = self.var.get().split()
        n = len(words)
        if n == 0:
            msg = "Marca al menos una palabra para poder buscar."
        elif n > 7:
            msg = (f"{n} palabras: Soulseek exige que estén TODAS en el "
                   "nombre del fichero remoto. Si no sale nada, quita "
                   "sello, año o palabras raras.")
        else:
            present = {self._norm(w) for w in words}
            missing = [w for w in self.all_tokens
                       if self._norm(w) in VERSION_WORDS
                       and self._norm(w) not in present]
            if missing:
                msg = ("Versión detectada en el nombre: "
                       f"{' '.join(missing)} — márcala si buscas justo esa "
                       "mezcla; déjala fuera para ver todas.")
            elif n <= 2:
                msg = ("Pocas palabras: saldrán muchos resultados. Añade "
                       "artista o título para afinar.")
            else:
                msg = ("Menos palabras = más resultados. Más palabras = "
                       "más exacto.")
        self.tip.configure(text=msg)

    def on_ok(self):
        q = self.var.get().strip()
        if q:
            self.result = q
        self.destroy()


class QualityPickerDialog(tk.Toplevel):
    """Ventana modal para elegir manualmente qué resultado de Soulseek
    descargar. Devuelve (response, file) o None si cancela."""

    def __init__(self, master, query, responses,
                 min_bitrate: int = GOOD_MP3_MIN_BR, filters: dict = None):
        super().__init__(master)
        self.title(f"Elegir versión — {query}")
        self.geometry("900x480")
        self.transient(master)
        self.grab_set()
        self.result = None

        ttk.Label(self, text=f"Resultados para: {query}",
                  padding=8).pack(fill=tk.X)
        frame = ttk.Frame(self, padding=(8, 0))
        frame.pack(fill=tk.BOTH, expand=True)

        cols = ("ext", "br", "size", "user", "slot", "queue", "speed")
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings",
                                 selectmode="browse")
        self._sort_state = {}
        def _sorter(col):
            def cb():
                desc = self._sort_state.get(col, False)
                sort_treeview_by_column(self.tree, col, descending=desc)
                self._sort_state[col] = not desc
            return cb
        self.tree.heading("#0", text="Fichero", command=_sorter("#0"))
        self.tree.column("#0", width=380, anchor=tk.W)
        for c, (t, w) in [("ext", ("Ext", 55)), ("br", ("kbps", 60)),
                          ("size", ("MB", 65)), ("user", ("Usuario", 130)),
                          ("slot", ("Slot", 50)), ("queue", ("Cola", 55)),
                          ("speed", ("Vel", 70))]:
            self.tree.heading(c, text=t, command=_sorter(c))
            self.tree.column(c, width=w, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._items = {}  # iid -> (response, file)
        self.tree.tag_configure("exacta", foreground="#1a7f37")
        self.tree.tag_configure("otra", foreground="#999999")
        rows = []
        for resp in responses:
            if peer_rejection_reason(resp, filters):
                continue
            for f in (resp.get("files") or []):
                if not is_acceptable(f, min_bitrate=min_bitrate,
                                     filters=filters):
                    continue
                tier = name_tier(query, f.get("filename"))
                rows.append((tier, score(resp, f, query), resp, f))
        # Mismo orden que usa la elección automática: primero los que se
        # llaman como la query, y dentro de eso el score.
        rows.sort(key=lambda t: (-t[0], -t[1]))
        for tier, _s, resp, f in rows[:200]:
            ext = _file_ext(f) or "?"
            br = f.get("bitRate") or ""
            size_mb = f.get("size", 0) / 1024 / 1024
            fn = (f.get("filename") or "").replace("\\", "/")
            name = fn.rsplit("/", 1)[-1]
            vals = (
                ext, str(br), f"{size_mb:.1f}",
                resp.get("username", "?"),
                "✓" if resp.get("hasFreeUploadSlot") else "—",
                str(resp.get("queueLength", 0)),
                f"{(resp.get('uploadSpeed') or 0)/1024:.0f}KB",
            )
            tags = ("exacta",) if tier == 2 else (
                () if tier == 1 else ("otra",))
            iid = self.tree.insert("", tk.END, text=name, values=vals,
                                   tags=tags)
            self._items[iid] = (resp, f)
        first = (self.tree.get_children("") or [None])[0]
        if first:
            self.tree.selection_set(first)
            self.tree.focus(first)

        self.tree.bind("<Double-1>", lambda _e: self.on_ok())
        btns = ttk.Frame(self, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Descargar", command=self.on_ok).pack(side=tk.LEFT)
        ttk.Button(btns, text="Saltar", command=self.destroy).pack(side=tk.LEFT, padx=6)

        self.wait_visibility()
        self.focus_set()
        self.wait_window(self)

    def on_ok(self):
        sel = self.tree.selection()
        if not sel:
            return
        self.result = self._items.get(sel[0])
        self.destroy()


def main():
    helper_msgs, helper_failed = maybe_launch_helpers()
    app = MacetaApp()
    if helper_msgs:
        app.status.set("Autoinicio: " + " · ".join(helper_msgs))
    if helper_failed:
        messagebox.showwarning(
            "Maceta",
            "Hubo un problema arrancando slskd automáticamente:\n\n"
            + "\n".join(helper_msgs)
            + "\n\nRevisa Configuración Soulseek (slskd) o arráncalo a mano.",
        )
    app.mainloop()


if __name__ == "__main__":
    main()

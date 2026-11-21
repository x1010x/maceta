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


VERSION = "0.4.34"
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


def scan_row(path: str) -> dict:
    """Fila con solo metadatos, sin decodificar el audio."""
    meta = get_metadata(path)
    return {
        "path": str(path),
        "name": Path(path).name,
        "codec": meta["codec"],
        "bitrate_kbps": meta["bitrate_kbps"],
        "sample_rate": meta["sample_rate"],
        "duration_s": meta["duration_s"],
        "channels": meta["channels"],
        "bpm": None,
        "cutoff_hz": None,
        "verdict": "",
        "detail": "",
        "freqs": None,
        "psd_db": None,
        "sr_used": None,
        "analyzed": False,
        "slsk_status": "",
    }


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
    "results_show_rejected":      False,
    "results_limit":              300,
    # {clave_tabla: {columna: ancho}} — anchos que el usuario deja a mano
    "column_widths":              {},
    # --- Disposición de la ventana, para dejarla como la dejaste ---
    "window_geometry":            "",      # "1080x600+45+38"
    "active_tab":                 0,
    "sash":                       {},      # {panel: [posiciones]}
    "recent_folders":             [],
    "restore_last_folder":        True,
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

def _spawn_detached(cmd, cwd=None):
    """Arranca un proceso desligado; sobrevive a la salida de Maceta."""
    kw = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


def prune_download_links() -> None:
    """Quita del índice los links cuyo fichero descargado ya no existe."""
    if not DOWNLOADS_DIR.exists():
        return
    have = {p.name.lower() for p in DOWNLOADS_DIR.rglob("*") if p.is_file()}
    links = load_download_links()
    alive = {k: v for k, v in links.items() if k in have}
    if len(alive) != len(links):
        save_download_links(alive)


def songs_match(path_a: str, path_b: str,
                sec: float = 30.0, threshold: float = None):
    """Compara dos ficheros de audio y decide si son la misma canción.

    Usa chroma_stft (representa contenido harmónico y es robusto al bitrate).
    Devuelve (match: bool, sim: float, reason: str).
    """
    if threshold is None:
        threshold = AUDIO_MATCH_THRESHOLD
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
        elif _spawn_detached([slskd_bin], cwd=str(Path(slskd_bin).parent)):
            if _wait_for(lambda: _slskd_alive(url, key), SLSKD_STARTUP_TIMEOUT_S):
                if _wait_for(lambda: _slskd_logged_in(url, key),
                             SLSKD_RECONNECT_TIMEOUT_S):
                    msgs.append("slskd arrancado")
                else:
                    msgs.append("slskd arrancado pero no conecta a Soulseek "
                               "(revisa tu red)")
                    failed = True
            else:
                msgs.append("slskd no respondió tras arrancar (revisa config/logs)")
                failed = True
        else:
            msgs.append("slskd no se pudo arrancar")
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
        r = self._request(
            "POST", "/api/v0/searches",
            json={
                "searchText": query,
                "filterResponses": True,
                "fileLimit": 5000,
                "minimumResponseFileCount": 1,
                "searchTimeout": search_timeout_ms,
            },
        )
        return r.json()

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


def row_bucket(row: dict) -> str:
    """Clasifica una fila en uno de: sin_analizar / en_proceso /
    a_revisar / fakes / errores / sustituidas / listas. 'todas' no la
    devuelve nadie — solo la usa el filtro para deshabilitarse a sí mismo.

    Las tres que piden atención van separadas a propósito: 'a_revisar' es
    una descarga que no cuadra con el original, 'fakes' es calidad real
    por debajo de lo declarado, y 'errores' es un fallo técnico al
    analizar. Son tres trabajos distintos."""
    if not row.get("analyzed"):
        return "sin_analizar"
    s = (row.get("slsk_status") or "").lower()
    # Movimientos de Soulseek en curso
    if any(k in s for k in ("buscando", "verificando",
                             "descarg", "convirtiendo")):
        return "en_proceso"
    if s.startswith("↓") or "queued" in s or "cola" in s:
        return "en_proceso"
    # Ya sustituida / convertida por nosotros
    if s.startswith("✓"):
        return "sustituidas"
    # Una descarga que no cuadra con el original manda sobre el
    # veredicto: es trabajo pendiente aunque la canción sea OK. Así el
    # chip coincide con lo que pinta display_verdict.
    if "no coincide" in s:
        return "a_revisar"
    # OK nativa (nada que hacer, siempre estuvo bien)
    v = row.get("verdict") or ""
    if v in ("OK", "LOSSLESS"):
        return "listas"
    if v == "FAKE":
        return "fakes"
    if v == "ERROR":
        return "errores"
    return "listas"


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
        self._player_proc = None
        self._player_path = None
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
        banner_label.pack(fill=tk.X)
        self._themed_widgets["banner"] = [banner, banner_label]

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
        m.add_separator()
        m.add_command(label="Cancelar",
                      command=self.on_cancel_search,
                      state=("normal" if has else "disabled"))
        return m

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
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Seleccionar carpeta…", command=self.on_pick).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.folder, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )

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
        self.pref_indicator_var = tk.StringVar(value="")
        ttk.Label(filt, textvariable=self.pref_indicator_var,
                  foreground="#888").pack(side=tk.RIGHT)

        cols = ("codec", "bitrate", "sr", "dur", "bpm", "cutoff",
                "verdict", "detail", "slsk")
        headers = {
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
        self.tree.bind("<Double-1>", self.on_show_spectrum)
        self.bind_all("<Control-a>", self._select_all_tree)
        self.bind_all("<Command-a>", self._select_all_tree)

        # Miniplayer + acciones rápidas de batch
        player = ttk.Frame(parent, padding=(8, 6, 8, 0))
        player.pack(fill=tk.X)
        ttk.Button(player, text="▶ Reproducir",
                   command=self.on_play_selected).pack(side=tk.LEFT)
        ttk.Button(player, text="⏹ Parar",
                   command=self.on_stop_playback).pack(side=tk.LEFT, padx=6)
        ttk.Separator(player, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(player, text="🔎 Analizar todas",
                   command=self.on_analyze_all).pack(side=tk.LEFT)
        ttk.Button(player, text="⬇ Buscar fakes",
                   command=self.on_slsk_fakes).pack(side=tk.LEFT, padx=6)
        ttk.Separator(player, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(player, text="🗑 Borrar (a archivo/)",
                   command=self.on_delete_selected).pack(side=tk.LEFT)
        self._now_playing_label = ttk.Label(
            player, textvariable=self.now_playing_var, anchor=tk.W
        )
        self._now_playing_label.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

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


    def _build_activity_tab(self, parent):
        # ── Cabecera: caja de búsqueda por texto libre + botón cleanup ──
        search = ttk.Frame(parent, padding=(8, 6, 8, 0))
        search.pack(fill=tk.X)
        ttk.Label(search, text="🔎 Buscar y descargar:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.search_entry.bind("<Return>", lambda _e: self.on_search_query())
        ttk.Button(search, text="Bajar de Soulseek",
                   command=self.on_search_query).pack(side=tk.LEFT)
        ttk.Button(search, text="🗑 Limpiar todo",
                   command=self.on_clear_all_activity).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            search, text="⟳ Rebuscar auto",
            variable=self.auto_retry_var,
            command=self._save_current_state,
        ).pack(side=tk.LEFT, padx=(12, 0))

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
        s_cols = ("num", "query", "state", "responses", "files")
        s_wrap, self.search_tree = make_scrollable_tree(
            s_frame, columns=s_cols, show="headings",
            selectmode="extended", height=6,
        )
        s_wrap.pack(fill=tk.BOTH, expand=True)
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
        ttk.Button(s_btns, text="Cancelar",
                   command=self.on_cancel_search).pack(side=tk.LEFT)
        ttk.Button(s_btns, text="Rebuscar…",
                   command=self.on_research).pack(side=tk.LEFT, padx=4)
        ttk.Button(s_btns, text="Limpiar",
                   command=self.on_clear_searches).pack(side=tk.LEFT, padx=4)
        ttk.Button(s_btns, text="↻",
                   command=self._activity_tick, width=3).pack(side=tk.LEFT)
        self.search_tree.bind("<<TreeviewSelect>>",
                              lambda _e: (self._on_search_selected(),
                                          self._sync_dl_from_search()))
        self.search_tree.bind("<Double-1>",
                              lambda _e: self._on_search_selected())

        # --- Resultados (derecha) ---
        r_frame = ttk.LabelFrame(top_paned, text="Resultados", padding=6)
        top_paned.add(r_frame, weight=2)
        # Orden: # · Nombre · Cola · Slot · MB · (resto)
        r_cols = ("num", "name", "queue", "slot", "size",
                  "ext", "br", "user", "folder", "speed")
        r_wrap, self.results_tree = make_scrollable_tree(
            r_frame, columns=r_cols, show="headings",
            selectmode="extended", height=6,
        )
        r_wrap.pack(fill=tk.BOTH, expand=True)
        self.results_tree.tag_configure("rejected", foreground="#999999")
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
        ttk.Button(r_btns, text="⬇ Descargar seleccionados",
                   command=self._download_selected_results).pack(side=tk.LEFT)
        ttk.Button(r_btns, text="↻",
                   command=self._on_search_selected, width=3).pack(side=tk.LEFT, padx=4)
        self.results_tree.bind("<Double-1>",
                               lambda _e: self._download_selected_results())

        # --- Descargas (abajo, ocupa el segundo pane vertical) ---
        d_frame = ttk.LabelFrame(main, text="Descargas", padding=6)
        main.add(d_frame, weight=1)
        d_cols = ("num", "file", "user", "state", "queue",
                  "progress", "size")
        d_wrap, self.dl_tree = make_scrollable_tree(
            d_frame, columns=d_cols, show="headings",
            selectmode="extended", height=6,
        )
        d_wrap.pack(fill=tk.BOTH, expand=True)
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
        ttk.Button(d_btns, text="Cancelar",
                   command=lambda: self.on_cancel_download(remove=False)).pack(side=tk.LEFT)
        ttk.Button(d_btns, text="Cancelar y borrar",
                   command=lambda: self.on_cancel_download(remove=True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(d_btns, text="Rebuscar…",
                   command=self.on_retry_download).pack(side=tk.LEFT, padx=4)
        ttk.Button(d_btns, text="⟳ Rebuscar canceladas",
                   command=self.on_requery_failed).pack(side=tk.LEFT, padx=4)
        ttk.Button(d_btns, text="Limpiar",
                   command=self.on_clear_downloads).pack(side=tk.LEFT, padx=4)
        ttk.Button(d_btns, text="🧹 Vaciar lista",
                   command=self.on_clear_dl_list).pack(side=tk.LEFT, padx=4)
        ttk.Button(d_btns, text="↻",
                   command=self._activity_tick, width=3).pack(side=tk.LEFT)

    def _build_downloads_tab(self, parent):
        """Pestaña con los ficheros que ya viven en downloads/.
        Independiente de Biblioteca — aquí ves solo lo que has bajado."""
        top = ttk.Frame(parent, padding=(8, 6, 8, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"📂 {DOWNLOADS_DIR}",
                  foreground="#555").pack(side=tk.LEFT)
        ttk.Button(top, text="↻ Refrescar",
                   command=self._refresh_downloads_tab).pack(side=tk.RIGHT)

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
        ttk.Button(r1, text="▶", command=self._play_downloads_selected,
                   width=3).pack(side=tk.LEFT)
        ttk.Button(r1, text="⏹", command=self.on_stop_playback,
                   width=3).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Separator(r1, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        ttk.Button(r1, text="🪄 Sustituir (auto)",
                   command=self._substitute_from_downloads_tab
                   ).pack(side=tk.LEFT)
        ttk.Button(r1, text="Mostrar en Finder",
                   command=self._reveal_downloads_selected
                   ).pack(side=tk.LEFT, padx=6)

        # Fila 2: acciones destructivas separadas visualmente
        r2 = ttk.Frame(parent, padding=(8, 2, 8, 8))
        r2.pack(fill=tk.X)
        ttk.Button(r2, text="🗑 Borrar del disco",
                   command=self._delete_downloads_selected).pack(side=tk.LEFT)
        ttk.Button(r2, text="🗑 Borrar TODO",
                   command=self._delete_all_downloads).pack(side=tk.LEFT, padx=6)
        ttk.Button(r2, text="🧹 Limpiar no completados",
                   command=self._clean_incomplete
                   ).pack(side=tk.LEFT, padx=6)

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
            f"Borrar del disco {len(paths)} fichero(s) en downloads/?\n\n"
            "Esta acción NO es recuperable.",
        ):
            return
        for p in paths:
            try:
                p.unlink()
            except Exception as e:
                self.status.set(f"Error borrando {p.name}: {e}")
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
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except Exception:
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
                try:
                    p.unlink()
                except Exception:
                    errors += 1
            dirs = [d for d in DOWNLOADS_DIR.rglob("*") if d.is_dir()]
            for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
                try:
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

        if pattern or pending_only or bucket != "todas":
            self.status.set(
                f"Filtro: {visible}/{len(self.rows_by_iid)} filas visibles"
            )
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
        try: self._now_playing_label.configure(foreground=c["now_playing_fg"])
        except Exception: pass
        try: self._status_label.configure(foreground=c["status_fg"])
        except Exception: pass

    def on_colors_config(self):
        ColorEditorDialog(self)

    def on_options(self, tab=None):
        OptionsDialog(self, tab=tab)

    def _select_all_tree(self, _event=None):
        if self.focus_get() is self.tree:
            self.tree.selection_set(self.tree.get_children(""))
            return "break"

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
        d = filedialog.askdirectory(title="Elige la carpeta con las canciones")
        if not d:
            return
        self.open_folder(d)

    def _scan_folder(self, folder):
        t0 = time.time()
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
        for i, p in enumerate(files, 1):
            batch.append(scan_row(str(p)))
            if len(batch) >= BATCH or i == total:
                self.after(0, self._insert_rows_batch, list(batch), i, total)
                batch.clear()
        log.info("scan_folder done: %d files in %.2fs",
                 total, time.time() - t0)
        self.after(0, self._scan_done, total)

    def _insert_rows_batch(self, rows, done, total):
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

    def _scan_done(self, n):
        self.progress.set(0)
        self.status.set(
            f"{n} archivos listados — selecciona y pulsa Analizar "
            "(Cmd/Ctrl+A para todas)"
        )

    def _row_values(self, row):
        dash = "—"
        return (
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

    def on_analyze_all(self):
        iids = list(self.rows_by_iid.keys())
        if not iids:
            messagebox.showinfo("Maceta", "Primero selecciona una carpeta.")
            return
        self._start_analysis(iids)

    def _start_analysis(self, iids):
        items = [(iid, self.rows_by_iid[iid]) for iid in iids]
        self.progress.set(0)
        self.status.set(f"Analizando {len(items)} archivos…")
        threading.Thread(
            target=self._run_analysis, args=(items,), daemon=True
        ).start()

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
                done += 1
                self.after(0, self._update_row, iid, row, done, total)
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

    @staticmethod
    def _fmt_dur(s):
        if not s:
            return "?"
        m, sec = divmod(int(s), 60)
        return f"{m}:{sec:02d}"

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
            iid = self._activity_search_iids.get(sid)
            if iid and self.search_tree.exists(iid):
                if self._activity_search_vals.get(iid) != vals:
                    self.search_tree.item(iid, values=vals)
                    self._activity_search_vals[iid] = vals
            else:
                iid = self.search_tree.insert(
                    "", tk.END, values=vals, tags=(sid,)
                )
                self._activity_search_iids[sid] = iid
                self._activity_search_vals[iid] = vals
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
        for key in list(self._activity_dl_iids.keys()):
            if key not in current_keys:
                iid = self._activity_dl_iids.pop(key)
                self._activity_dl_vals.pop(iid, None)
                if self.dl_tree.exists(iid):
                    self.dl_tree.delete(iid)

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
        # Borrar el FLAC de forma permanente (no a archivo/)
        try:
            flac.unlink()
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
            if len(tags) >= 2:
                pairs.append((tags[0], tags[1]))
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

        rows = []
        for resp in responses or []:
            peer_bad = peer_rejection_reason(resp, filters)
            for f in (resp.get("files") or []):
                why = peer_bad or rejection_reason(f, min_br, filters)
                if why and not show_rejected:
                    continue
                rows.append((resp, f, why))

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

        self.after(0, self._populate_results, sid, items)

    def _populate_results(self, sid, items):
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
        n_ok = 0
        for vals, ref, rejected in items:
            item_iid = self.results_tree.insert(
                "", tk.END, values=vals,
                tags=("rejected",) if rejected else (),
            )
            self._results_items[item_iid] = ref
            n_ok += not rejected
        self._results_current_sid = sid
        n = self._search_num.get(sid, "?")
        if items:
            extra = (f" (+{len(items) - n_ok} descartados)"
                     if len(items) > n_ok else "")
            self.status.set(f"Resultados de #{n}: {n_ok}{extra}")
        else:
            self.status.set(
                f"#{n}: 0 resultados aceptables "
                f"(sube min bitrate en Preferencias si buscas menos estricto)"
            )

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
        ffplay = self._find_ffplay()
        if not ffplay:
            messagebox.showerror(
                "Maceta",
                "ffplay no encontrado. Viene con ffmpeg — instala ffmpeg.",
            )
            return
        try:
            self._player_proc = subprocess.Popen(
                [ffplay, "-autoexit", "-nodisp", "-loglevel", "quiet", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            self._player_path = path
            self.now_playing_var.set(f"▶ {label or Path(path).name}")
            self.after(500, self._player_poll)
        except Exception as e:
            messagebox.showerror("Maceta", f"No se pudo reproducir: {e}")

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
        if self._player_proc:
            try:
                self._player_proc.terminate()
                try:
                    self._player_proc.wait(timeout=1)
                except Exception:
                    self._player_proc.kill()
            except Exception:
                pass
        self._player_proc = None
        self._player_path = None
        self.now_playing_var.set("—")

    def _player_poll(self):
        if self._player_proc and self._player_proc.poll() is not None:
            # Terminó solo (fin de canción)
            self._player_proc = None
            self._player_path = None
            self.now_playing_var.set("—")
            return
        if self._player_proc:
            self.after(500, self._player_poll)

    def _on_close(self):
        self.on_stop_playback()
        self._save_column_widths()
        self._save_layout()
        self.destroy()

    # ---------- Disposición de la ventana ----------

    def _panes(self) -> dict:
        """Los PanedWindow cuyos divisores merece la pena recordar."""
        return {k: w for k, w in (
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
        sash = {}
        for name, pane in self._panes().items():
            pos = []
            i = 0
            while True:
                try:
                    pos.append(pane.sashpos(i))
                except Exception:
                    break
                i += 1
            # Un panel de una pestaña que todavía no se ha mostrado
            # devuelve 0: guardarlo colapsaría el divisor al reabrir.
            if any(p > 0 for p in pos):
                sash[name] = pos
        try:
            tab = self.notebook.index(self.notebook.select())
        except Exception:
            tab = 0
        save_user_state({"window_geometry": geo, "active_tab": int(tab),
                         "sash": sash})

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
        for name, pane in self._panes().items():
            for i, pos in enumerate(saved.get(name) or []):
                if int(pos) <= 0:
                    continue
                try:
                    pane.sashpos(i, int(pos))
                except Exception:
                    pass
        self.bind("<Configure>", self._schedule_layout_save, add="+")
        self.notebook.bind("<<NotebookTabChanged>>",
                           self._schedule_layout_save, add="+")

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
        """Escanea una carpeta y la apunta en las recientes."""
        self.folder.set(d)
        self._push_recent_folder(d)
        self._save_current_state()
        self.tree.delete(*self.tree.get_children())
        self.rows_by_iid.clear()
        self.progress.set(0)
        self.status.set(f"Leyendo metadatos de {d}…")
        threading.Thread(target=self._scan_folder, args=(d,),
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
        remove_download_link(Path(downloaded_path).name)
        if orig_iid in self.rows_by_iid and self.tree.exists(orig_iid):
            self.tree.item(orig_iid, text=row["name"],
                           values=self._row_values(row), tags=("PENDING",))
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
        candidates = [(i, r) for i, r in self.rows_by_iid.items()
                      if r.get("analyzed") and r["verdict"] == "FAKE"]
        if not candidates:
            messagebox.showinfo("Maceta", "No hay canciones marcadas como FAKE.")
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
        info = client.start_search(query)
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
        # Sin max_wait a mano: 25 s era menos que el searchTimeout de 30 s
        # que le pedimos a slskd, así que nos rendíamos antes de que la
        # búsqueda pudiera cerrarse sola.
        client.wait_for_search(sid)
        responses = client.get_responses(sid)
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
            foreground="#555",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self, padding=(12, 8))
        body.pack(fill=tk.BOTH, expand=True)
        text_frame = ttk.Frame(body)
        text_frame.pack(fill=tk.BOTH, expand=True)
        txt = tk.Text(text_frame, wrap="word", padx=8, pady=8,
                      background="#fafafa", relief="flat",
                      font=("TkDefaultFont", 11))
        vsb = ttk.Scrollbar(text_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.tag_configure("h2", font=("TkDefaultFont", 12, "bold"),
                          spacing1=8, spacing3=4)
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

        self._panels = {}
        self._panels["orig"] = self._make_side_panel(
            body, 0, orig_label, self.orig_path, accent="#a33")
        self._panels["cand"] = self._make_side_panel(
            body, 1, cand_label, self.cand_path, accent="#2a6")

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

    def _make_side_panel(self, parent, col, label, path, accent):
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

        # Player
        player = ttk.Frame(frame)
        player.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(player, text="▶ Reproducir",
                   command=lambda: self.app.play_path(path, Path(path).name)
                   ).pack(side=tk.LEFT)
        ttk.Button(player, text="⏹ Parar",
                   command=self.app.on_stop_playback
                   ).pack(side=tk.LEFT, padx=6)

        # Verdict local (lo rellena el hilo)
        verdict_var = tk.StringVar(value="…")
        verdict_lbl = ttk.Label(frame, textvariable=verdict_var,
                                foreground="#555")
        verdict_lbl.grid(row=4, column=0, sticky="w", pady=(4, 0))

        return {"figure": fig, "ax": ax, "canvas": canvas,
                "meta": meta, "verdict_var": verdict_var,
                "info_lbl": info_lbl}

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
        cols = ("ext", "size", "duration")
        self.tree = ttk.Treeview(
            wrap, columns=cols, show="tree headings", selectmode="browse",
        )
        self._sort_state = {}
        def _make_sorter(col):
            def _cb():
                desc = self._sort_state.get(col, False)
                sort_treeview_by_column(self.tree, col, descending=desc)
                self._sort_state[col] = not desc
            return _cb
        self.tree.heading("#0", text="Fichero", command=_make_sorter("#0"))
        self.tree.column("#0", width=460, anchor=tk.W)
        for c, (t, w) in [("ext", ("Ext", 60)),
                          ("size", ("MB", 80)),
                          ("duration", ("Duración", 90))]:
            self.tree.heading(c, text=t, command=_make_sorter(c))
            self.tree.column(c, width=w, anchor=tk.W)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._paths = {}  # iid -> Path
        for p in sorted(DOWNLOADS_DIR.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in SUPPORTED_EXTS:
                continue
            meta = get_metadata(str(p))
            size_mb = p.stat().st_size / 1024 / 1024
            dur = meta.get("duration_s") or 0
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            iid = self.tree.insert(
                "", tk.END, text=str(p.relative_to(DOWNLOADS_DIR)),
                values=(p.suffix.lstrip(".").upper(),
                        f"{size_mb:.1f}", dur_str),
            )
            self._paths[iid] = p

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
                       command=getattr(self, f"_do_{f['cmd']}")).pack()
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

    def _do_test_slskd(self):
        url = self._cfg_vars["slskd_url"].get().strip()
        key = self._cfg_vars["slskd_api_key"].get().strip()
        try:
            info = SlskdClient(url, key).ping()
            self._result_var.set(
                f"✓ Conectado — slskd {info.get('version', '?')}")
        except Exception as e:
            self._result_var.set(f"✗ {e}")

    def _do_test_update(self):
        try:
            info = check_for_update(
                self._cfg_vars["update_token"].get().strip(),
                self._cfg_vars["update_repo"].get().strip(),
                self._cfg_vars["update_branch"].get().strip())
            self._result_var.set(
                f"✓ Local v{VERSION} · remoto v{info['remote_version']} "
                f"({'hay actualización' if info['is_newer'] else 'al día'})")
        except Exception as e:
            self._result_var.set(f"✗ {e}")

    def _do_colors(self):
        ColorEditorDialog(self.app)

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

<img src="logo.png" alt="Maceta" width="110" align="right">

# Maceta

**Tu biblioteca de música está llena de mentiras. Maceta las encuentra, baja la
versión buena y la pone en su sitio.**

![Python](https://img.shields.io/badge/python-3.11+-3776ab)
![Licencia](https://img.shields.io/badge/licencia-GPL--3.0-green)
![Plataformas](https://img.shields.io/badge/macOS%20·%20Windows%20·%20Linux-lightgrey)
![Tests](https://img.shields.io/badge/tests-91%20passing-brightgreen)

Ese MP3 que pone "320 kbps" y suena a lata lleva años engañándote: alguien lo
recodificó desde un 128 y el reproductor se cree la etiqueta. Maceta no.
Mira el **espectro real** del audio, te dice cuáles mienten, busca el
reemplazo en Soulseek, comprueba por **huella sonora** que es la misma canción
y sustituye el fichero — archivando el original, nunca borrándolo.

---

## Lo que hace

🔬 **Detecta transcodes de verdad.** No se fía del bitrate declarado: calcula la
densidad espectral y busca el corte del filtro. Un "320" con brickwall a
15 kHz es un 128 disfrazado, y Maceta lo canta con el bitrate real estimado.

📈 **Te enseña el espectro.** Doble click en cualquier canción y sale la
gráfica con el corte marcado y líneas guía a 16/19/20 kHz. Lo ves tú mismo,
no te lo tienes que creer.

🥁 **Saca el BPM real** del audio, no de los tags.

🔎 **Busca y baja el reemplazo solo.** Conectado a Soulseek vía slskd:
construye la query, filtra por formato, tamaño, duración y cola del peer, y
elige el mejor resultado. Y elige **por nombre primero** — buscando
"Black Pumas - Colors" no te cuela un directo de 52 MB solo por ser más grande.

🧬 **Verifica antes de tocar nada.** Compara 30 s del centro de ambos ficheros
con cromagramas: si la descarga no es la misma canción, no sustituye. Se acabó
acabar con un remix en vez del original.

🗄️ **Nunca borra tu música.** El original sustituido se archiva en
`archivo/` con su ruta completa. Si te arrepientes, lo copias de vuelta.

🎯 **Trabaja mientras miras.** Rebusca sola lo que ha fallado, reintenta las
descargas canceladas, convierte FLAC a MP3 320 si se lo pides, y solo te
pregunta cuando de verdad se ha quedado sin opciones.

📊 **Te dice dónde estás.** Chips con porcentajes: cuántas están listas,
cuántas son fake, cuántas ya has sustituido. Una biblioteca de 4000 canciones
deja de ser un agujero negro.

💬 **Chat de Soulseek integrado**, porque a veces hay que pedirle el fichero
al que lo tiene.

⚙️ **40 ajustes** en un panel con explicación al lado de cada uno: desde el Hz
exacto a partir del cual algo es FAKE hasta cuántas veces reintenta antes de
preguntarte.

## Un paseo de 30 segundos

1. **Seleccionar carpeta** → Maceta lista tus canciones con formato, bitrate y
   duración.
2. **Analizar todas** → aparecen BPM, corte espectral y veredicto. El chip
   `❌ FAKE` te dice cuántas mienten.
3. **Bajar todas FAKE** → busca cada una en Soulseek y encola la mejor versión.
4. Cada descarga que completa se verifica por audio y **sustituye sola** al
   original, que se va a `archivo/`.
5. Lo que no haya salido se queda en `⚠ Revisar` para que lo mires a mano.

## ⚠️ Antes de nada

**Haz copia de seguridad de tu música.** El programa opera sobre tus ficheros:
mueve, sustituye y (si se lo pides) borra. Está en desarrollo y tiene bugs.
No lo estrenes sobre una biblioteca sin copia.

## Instalación

Maceta necesita tres cosas, en cualquier sistema:

| | Para qué |
| --- | --- |
| **Python 3.11+ con Tkinter** | el programa y su interfaz |
| **ffmpeg** (trae `ffplay`) | decodificar MP3, convertir a 320 y el reproductor |
| **slskd** *(opcional)* | solo si quieres la parte de Soulseek |

Elige tu sistema:

<details>
<summary><b>🍎 macOS</b> (Intel y Apple Silicon)</summary>

**En un comando**, si no quieres saber nada de entornos ni dependencias:

```bash
curl -fsSL https://raw.githubusercontent.com/x1010x/maceta/main/install-mac.sh | bash
```

El instalador hace todo y **se puede volver a lanzar cuando quieras**:

- Instala Miniforge si no lo tienes y crea el entorno con las dependencias.
- Clona Maceta en `~/maceta`, o la **actualiza** si ya está
  (te dice la versión: *"Maceta 0.4.46 ya instalada y al día"*).
- Deja un **Maceta.app** en `~/Applications` con el logo de icono.
- Comprueba **ffmpeg** y, si falta, pregunta si lo instala.
- Pregunta si quieres **slskd** (la parte de Soulseek, opcional): lo baja
  de sus releases, lo desbloquea del Gatekeeper, y si no tienes
  configuración te crea el `slskd.yml` con tu usuario, una API key nueva
  y una carpeta compartida — y deja Maceta ya apuntando a él. Si slskd ya
  está, mira si hay versión nueva y ofrece actualizarlo.

Variables para no tener que contestar: `MACETA_PYTHON=/ruta/a/python`
(salta conda), `MACETA_FFMPEG=0/1`, `MACETA_SLSKD=0/1`, `MACETA_YES=1`
(sí a todo), `MACETA_DIR` y `MACETA_APP_DIR`.

<br>

O **a mano**. La vía más cómoda es **Miniforge**, que trae Python con Tkinter ya dentro y
te instala `librosa` sin pelearte con la compilación:

1. Descarga el instalador de
   [Miniforge](https://github.com/conda-forge/miniforge#download) —
   `Miniforge3-MacOSX-arm64` si tu Mac es M1/M2/M3/M4, `x86_64` si es Intel.
2. Abre el Terminal y ejecuta:

```bash
bash ~/Downloads/Miniforge3-MacOSX-arm64.sh     # ajusta el nombre al que bajaste
conda create -n maceta python=3.11 -y
conda activate maceta
conda install -c conda-forge -y librosa mutagen matplotlib requests ffmpeg

git clone https://github.com/x1010x/maceta.git
cd maceta
python maceta.py
```

**Con Homebrew**, si ya lo usas y prefieres no instalar conda
([brew.sh](https://brew.sh)):

```bash
brew install python@3.11 python-tk@3.11 ffmpeg git
git clone https://github.com/x1010x/maceta.git && cd maceta
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python maceta.py
```

⚠️ Con Homebrew hace falta `python-tk` aparte: el Python de brew viene **sin**
Tkinter y Maceta no arrancaría.

</details>

<details>
<summary><b>🪟 Windows 10 / 11</b></summary>

1. Instala **Python 3.11+** desde
   [python.org/downloads](https://www.python.org/downloads/windows/) —
   marca **"Add python.exe to PATH"** en la primera pantalla. El instalador
   oficial ya incluye Tkinter.
   *(Alternativa: [Miniforge para Windows](https://github.com/conda-forge/miniforge#download).)*
2. Instala **ffmpeg** y **git**. Con winget, desde PowerShell:

```powershell
winget install Gyan.FFmpeg
winget install Git.Git
```

   *(O baja ffmpeg a mano de [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)
   y añade su carpeta `bin` al PATH.)*
3. **Cierra y reabre PowerShell** para que coja el PATH nuevo, y luego:

```powershell
git clone https://github.com/x1010x/maceta.git
cd maceta
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python maceta.py
```

Si PowerShell se queja al activar el entorno, ejecuta una vez:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

</details>

<details>
<summary><b>🐧 Linux</b></summary>

Instala Python con Tkinter, ffmpeg y git con el gestor de tu distro:

```bash
# Debian / Ubuntu / Mint
sudo apt update && sudo apt install -y python3 python3-tk python3-venv python3-pip ffmpeg git

# Fedora   (ffmpeg necesita RPM Fusion: https://rpmfusion.org/Configuration)
sudo dnf install -y python3 python3-tkinter python3-pip ffmpeg git

# Arch / Manjaro
sudo pacman -S --needed python tk python-pip ffmpeg git
```

Y después, igual en todas:

```bash
git clone https://github.com/x1010x/maceta.git
cd maceta
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python maceta.py
```

</details>

### Si algo falla

| Síntoma | Qué pasa |
| --- | --- |
| `ModuleNotFoundError: No module named '_tkinter'` | tu Python no trae Tkinter: instala `python-tk` (brew), `python3-tk` (apt) o `python3-tkinter` (dnf), o usa Miniforge |
| `ffplay no encontrado` al darle a ▶ | falta ffmpeg en el PATH |
| El primer análisis tarda una barbaridad | es `numba` compilando la primera vez; a partir de ahí va rápido |
| `pip install librosa` falla compilando | usa la vía conda, que trae binarios ya hechos |

## Cómo detecta los fakes

Analiza hasta 5 minutos del centro del track (evita colgarse en mixes largos),
calcula la densidad espectral de potencia (Welch, ventana Hann, FFT de 8192) y
busca la frecuencia más alta con energía por encima de *pico − 60 dB*. Ese es
el **corte**.

El veredicto lo decide **solo el corte**, no el bitrate declarado:

| Corte detectado | Veredicto |
| --- | --- |
| ≥ 16 000 Hz | `OK` — hay información arriba, el fichero es honesto |
| < 16 000 Hz | `FAKE` — brickwall de transcode, con estimación del bitrate real |

El suelo de 16 kHz es el corte típico de un MP3 a 128 kbps: por debajo de ahí
el recorte es inconfundible. Por encima, el detector empieza a confundir
música genuinamente apagada con un transcode, y marcarla daba demasiados
falsos positivos. Para estimar el bitrate real de un FAKE se compara el corte
con la tabla de filtros low-pass de LAME (320→20 500 Hz, 256→20 000,
192→19 000, 128→16 000, 96→15 000).

Un MP3 a 192 kbps honesto sale `OK`, con el detalle avisando de que declara
menos del target por si lo quieres mejorar igualmente. Todos los umbrales son
configurables.

## Soulseek

La parte de descarga es **opcional**: el análisis, el espectro y el BPM
funcionan sin tocar nada de esto. Si la quieres, hace falta **slskd**
corriendo en tu máquina — los clientes clásicos (SoulseekQt, Nicotine+) no
exponen una API con la que automatizar.

**1. Instala slskd** desde [sus releases](https://github.com/slskd/slskd/releases):

| Sistema | Fichero | Nota |
| --- | --- | --- |
| macOS Apple Silicon | `slskd-*-osx-arm64.zip` | al abrirlo la primera vez, macOS lo bloquea por no estar firmado: botón derecho → *Abrir*, o `xattr -d com.apple.quarantine slskd` |
| macOS Intel | `slskd-*-osx-x64.zip` | igual |
| Windows | `slskd-*-win-x64.zip` | SmartScreen avisará: *Más información → Ejecutar de todas formas* |
| Linux | `slskd-*-linux-x64.zip` | `chmod +x slskd` antes de arrancarlo · hay `linux-arm64` para Raspberry Pi |
| Cualquiera | [Docker](https://hub.docker.com/r/slskd/slskd) | `docker run -p 5030:5030 slskd/slskd` |

**2. Arráncalo una vez** para que genere su fichero de configuración, y páralo.

**3. Edita `slskd.yml`** (lo crea en `~/.local/share/slskd/` en macOS y Linux,
y en `%localappdata%\slskd\` en Windows):

```yaml
soulseek:
  username: tu_usuario_de_soulseek
  password: tu_contraseña
web:
  authentication:
    api_keys:
      maceta:
        key: pon-aqui-una-cadena-larga-de-40-o-mas-caracteres
```

**4. Vuelve a arrancar slskd** y, en Maceta, ve a
**Preferencias → ⚙ Opciones… → slskd**: pega la URL (`http://localhost:5030`)
y esa misma API key, pulsa **Probar conexión** y guarda.

Si le indicas la ruta del binario en esa pestaña, Maceta arranca slskd por ti
al abrirse.

> 💡 Comparte alguna carpeta en tu `slskd.yml` (sección `shares`). Muchos peers
> rechazan o dejan en cola indefinida a quien no comparte nada, y notarás que
> las descargas no arrancan.

## Qué hace cada pestaña

### Biblioteca

Escanea una carpeta recursivamente (mp3, flac, m4a, aac, ogg, opus, wav, wma)
y lista cada canción con formato, bitrate, sample rate, duración, BPM, corte
y veredicto. Las filas se agrupan en *chips* que filtran la tabla:

| Chip | Qué contiene |
| --- | --- |
| ⚠ Revisar | descargas cuyo audio no coincide con el original |
| ❌ FAKE | calidad real por debajo de la declarada |
| ⛔ Error | fallo técnico al analizar (fichero corrupto, ffmpeg) |
| ⏳ En proceso | con búsqueda o descarga en marcha |
| ✅ Listas | analizadas y correctas |
| ⬇ Sustituidas | ya reemplazadas por una versión mejor |
| ❓ Sin analizar | pendientes |

Desde aquí se analiza (selección o todas), se busca en Soulseek, se convierte
lossless a MP3 320, se ve el espectro en una ventana con matplotlib, y hay un
reproductor mínimo que usa `ffplay`.

### Actividad Soulseek

Caja de búsqueda libre, tabla de **Búsquedas** y tabla de **Descargas**,
correlacionadas por un `#N` (la búsqueda #3 produce la descarga #3), más un
panel de **Resultados** donde eliges a mano qué fichero bajar. Botones para
cancelar, rebuscar editando la query, **rebuscar todas las canceladas** de
golpe, limpiar completadas y vaciar la lista.

### Descargas

Lo que vive en `downloads/`: reproducir, mostrar en Finder, borrar, y
**🪄 Sustituir (auto)**, que busca a qué canción de tu biblioteca reemplaza
cada descarga.

### 💬 Mensajes

Los mensajes privados de Soulseek: lista de conversaciones, hilo y caja de
envío. Útil para escribirle a quien tiene el fichero que quieres.

## Cómo elige qué fichero bajar

Entre todos los resultados aceptables, el orden es:

1. **Que el nombre se parezca a lo que buscaste.** Exacto (≥95%) gana a
   plausible (≥70%), y ambos ganan a cualquier otra cosa. Sin esto, buscando
   "Black Pumas - Colors" se bajaba un directo de 52 MB llamado
   "0114 - Colors Live from Brooklyn Paramount" solo por ser más grande.
2. **Peer disponible** (slot libre o cola vacía) antes que peer con cola.
3. **Formato**: FLAC > WAV > MP3.
4. **Tamaño**: dentro del mismo codec, el más grande — un "320 kbps" que pesa
   3 MB miente.
5. Velocidad de subida y longitud de la cola.

Si algún resultado se llama exactamente como la búsqueda, se baja sin
preguntar aunque tengas activado "preguntar calidad".

Se descartan de entrada: formatos no elegidos, MP3 por debajo del mínimo
configurado, ficheros fuera del rango de tamaño o duración, y peers con más
cola de la aceptada.

## Sustitución y archivado

Cuando Maceta reemplaza una canción, el original **no se borra**: se mueve a
`archivo/<ruta absoluta original>`, de donde puedes recuperarlo copiándolo de
vuelta. El sustituto conserva el directorio y el nombre del original, pero
adopta la extensión del descargado (un `Song.mp3` fake pasa a ser un
`Song.flac` real).

Para decidir que dos ficheros son la misma canción se comparan 30 s del centro
de cada uno con `librosa.feature.chroma_stft` y se mide la similitud coseno
(umbral 0.85, configurable), con una comprobación previa de duración. Si no
coinciden, no sustituye.

Desde la pestaña Descargas la sustitución es automática: si la descarga la
pidió Maceta, usa el enlace que guardó al encolarla y sabe exactamente a qué
original reemplaza; si no, busca por parecido de nombre y lo confirma con el
audio. Solo pregunta cuando no hay forma de saberlo, y entonces la lista llega
ordenada por parecido y con la mejor candidata marcada.

## Opciones

**Preferencias → ⚙ Opciones…** (o `⌘,`) abre un panel con todos los ajustes
repartidos en siete pestañas: Análisis, Búsqueda, Descargas, Reintentos,
slskd, Actualizaciones y Apariencia. Cada campo lleva su explicación al lado.

Ahí se tocan, entre otras cosas, el target de calidad, el corte en Hz a partir
del cual algo es FAKE, la sensibilidad del detector, cuántos segundos se
analizan, los umbrales de parecido de nombre y de audio, los filtros de
búsqueda, y cuántas rebúsquedas automáticas se intentan antes de preguntarte.
Los cambios se aplican al guardar, sin reiniciar. Los umbrales de análisis
afectan a los análisis nuevos: para recalcular veredictos ya hechos hay que
volver a analizar.

Todo se guarda en `~/.maceta_config.json`. Nada vive en el código.

## Auto-actualización

Maceta puede actualizarse sola descargando la última `maceta.py` del repo.
En **Opciones → Actualizaciones** están el repo, la rama y un token opcional
(solo hace falta si apuntas a un repo privado o te topas con el límite de
peticiones anónimas de la API de GitHub).

Al arrancar comprueba en segundo plano si hay versión nueva y, si la hay,
pregunta. Si aceptas: copia de seguridad del `maceta.py` actual en `archivo/`,
validación de sintaxis del fichero descargado (no toca nada si el remoto está
roto), escritura atómica y reinicio.

## Tests

```bash
python3 -m pytest          # ~3 s, 91 tests
```

Cubren lógica pura, sin red ni ficheros del usuario: usan `tmp_path` y generan
WAVs sintéticos.

| Fichero | Cubre |
| --- | --- |
| `tests/test_analysis.py` | `get_metadata`, `compute_spectrum`, `find_cutoff`, `judge`, `analyze_row` de punta a punta |
| `tests/test_soulseek_logic.py` | `is_acceptable`, `choose_best`, construcción y limpieza de queries |
| `tests/test_substitute.py` | `archive_path_for`, `substitute_file`, `songs_match` |
| `tests/test_buckets.py` | clasificación de filas y texto de la columna Calidad |
| `tests/test_config.py` | persistencia de configuración y colores |
| `tests/test_update.py` | comparación de versiones y aplicación de updates |

## Estructura

```
maceta/
├── maceta.py           # todo el código
├── install-mac.sh      # instalador de una línea
├── logo.png
├── requirements.txt
├── tests/
├── CHANGELOG.md
├── README.md
├── LICENSE
├── downloads/          # (se crea sola) descargas de slskd
└── archivo/            # (se crea sola) originales archivados
```

## Licencia

GPL-3.0. Ver [LICENSE](LICENSE). Se distribuye sin ninguna garantía.

## Nota legal

Maceta es un cliente que habla con tu propia instancia de slskd; no aloja ni
distribuye música. Lo que descargues de Soulseek es cosa tuya y está sujeto a
la ley de copyright de tu país. Úsalo con material que tengas derecho a
obtener — por ejemplo, reemplazar por una copia decente un disco que ya
compraste.

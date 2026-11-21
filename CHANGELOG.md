# Changelog

Novedades por versión. Maceta te avisará automáticamente después de
actualizar con un popup mostrando lo que cambió desde tu versión anterior.

## 0.4.34

- **ARREGLADO: las búsquedas que no llegaban a 250 respuestas no bajaban
  nada.** slskd no sirve ni una respuesta mientras la búsqueda está
  `InProgress` — con 242 respuestas contadas, `/responses` devolvía 0 —
  y Maceta leía a los 15 segundos, siempre antes de tiempo. Solo
  funcionaban las búsquedas que tocaban el límite de 250 respuestas,
  porque ese límite cierra la búsqueda al instante. Ahora Maceta cierra
  la búsqueda ella misma (`PUT`, que conserva lo recogido) antes de leer,
  y además espera lo suficiente para que slskd pueda cerrarla sola: la
  espera eran 25 s cuando el propio slskd necesita 30.
- **La ventana se queda como la dejaste**: tamaño, posición, pestaña
  abierta y los divisores de los paneles se guardan y se restauran. Si
  la posición guardada cae fuera de las pantallas actuales (un monitor
  que ya no está), se conserva el tamaño y se centra.
- **Carpetas recientes** en el menú Archivo: las últimas 8 carpetas
  analizadas, un click para volver a cualquiera. Las que ya no existen
  se filtran solas.
- Dos opciones nuevas en el panel: *Reabrir la última carpeta al
  arrancar* y *Olvidar posición* de la ventana.
- **Instalador para macOS**: `install-mac.sh` monta Miniforge si hace
  falta, crea el entorno, clona el repo y deja un `Maceta.app` en
  Aplicaciones con el logo de icono.

## 0.4.33

- **Logo propio**: Maceta ya tiene icono de ventana. Si `logo.png` está
  junto a `maceta.py`, se usa automáticamente; si no está, Tk se queda
  con el icono por defecto y no pasa nada.
- Proyecto preparado para publicarse: licencia GPL-3.0, README nuevo y
  suite de tests al día (91, todos en verde).

## 0.4.32

- **Arreglada la elección automática**: se bajaba el fichero más grande
  aunque se llamara distinto. Buscando "BLACK PUMAS - Colors" elegía
  "0114 - Colors Live from Brooklyn Paramount.flac" (52 MB, un directo)
  en vez de "Black Pumas - Colors.flac" (46 MB). Ahora **el nombre manda**:
  primero se mira si el resultado se llama como lo que buscaste, y solo
  después entran formato, tamaño y velocidad. El selector manual también
  ordena así, pinta en verde los que se llaman igual y en gris los que
  no, y deja preseleccionado el primero.
- **Panel único de opciones** (Preferencias → ⚙ Opciones…, o ⌘,): 40
  ajustes repartidos en Análisis, Búsqueda, Descargas, Reintentos,
  slskd, Actualizaciones y Apariencia, cada uno con su explicación al
  lado. Incluye cosas que antes estaban cableadas en el código y no se
  podían tocar: target de calidad, mínimo aceptable, corte en Hz para
  marcar FAKE, sensibilidad del detector de corte, segundos de audio
  analizados, tamaño máximo de descarga, los dos umbrales de parecido
  de nombre, el parecido de audio para sustituir y cuántas rebúsquedas
  se intentan antes de preguntarte. Guardar aplica los cambios al
  momento, Cancelar deshace y hay botón de restaurar valores por
  defecto.
- Los diálogos sueltos de opciones de búsqueda, rebúsqueda automática,
  configuración de slskd y configuración de actualizaciones **se han
  fundido en ese panel**: los menús de siempre ahora lo abren en la
  pestaña que toca.

## 0.4.31

Peticiones recogidas de las pruebas en equipos reales:

- **Descargas → 🗑 Borrar TODO**: vacía `downloads/` de golpe, parciales
  de `.incomplete/` incluidos, y poda el índice de links. Pide
  confirmación diciendo cuántos ficheros y cuántos MB. Esto borra de
  verdad — no pasa por `archivo/`.
- **Actividad → 🧹 Vaciar lista**: quita de slskd todas las entradas
  terminadas (completadas, canceladas y erroreadas) y deja las que
  siguen bajando. No toca ningún fichero. El botón "Limpiar" de antes
  sigue limpiando solo las completadas.
- **Sustituir desde Descargas, ahora automático**: el botón pasa a ser
  🪄 Sustituir (auto). Si la descarga la bajó Maceta, usa el link que se
  guardó al pedirla y sustituye sin preguntar. Si no hay link, busca en
  Biblioteca por parecido de nombre y lo confirma comparando el audio.
  Solo abre diálogo cuando no hay forma de saber a quién reemplaza — y
  ese diálogo llega ya ordenado por parecido, con columna **Parecido**,
  la candidata marcada en verde y preseleccionada.
- **Búsqueda exacta = descarga directa**: si algún resultado se llama
  igual que lo que buscaste (≥95% de parecido ignorando carpeta,
  extensión, número de pista y puntuación), se baja el mejor de esos sin
  abrir el selector de calidad, aunque tengas "preguntar calidad"
  activado. El estado lo marca con "· exacta". Las versiones distintas
  (Live, Radio Edit, covers) no cuentan como exactas y siguen
  preguntando.
- **Actividad → ⟳ Rebuscar canceladas**: recorre todas las descargas
  caídas (canceladas, erroreadas, timeouts y rechazadas), recupera la
  query con la que se pidió cada fichero, borra la entrada muerta de
  slskd y vuelve a buscar sola. Tras 3 intentos sin suerte con la misma
  query deja de insistir y abre el selector para que elijas a mano.
- **Biblioteca ya no se mueve sola**: la tabla dejaba de estar donde la
  habías puesto porque el análisis arrastraba la vista a cada fila que
  empezaba. Ahora la vista se queda anclada a la fila marcada, y si no
  hay ninguna marcada conserva la posición del scroll al filtrar.
- Cuando el rebusque automático agota los reintentos de varias
  canciones, Maceta pregunta **una sola vez por tanda** si quieres
  elegirlas a mano, en vez de una ventana por canción.
- **"A revisar" se parte en tres listas**: el chip mezclaba cosas muy
  distintas. Ahora hay **⚠ Revisar** (descargas cuyo audio no cuadra con
  el original), **❌ FAKE** (calidad real por debajo de la declarada) y
  **⛔ Error** (fallo técnico al analizar). Cada una con su contador y su
  porcentaje. Si la ventana es estrecha los chips se reparten solos en
  dos filas en vez de salirse por la derecha.

## 0.4.20

- **Auto-selección más lista**: `choose_best` prioriza AHORA como
  regla dura los peers **con slot libre o cola=0**. Sólo si ninguno de
  los aceptables está disponible cae a los que tienen cola. Dentro de
  cada grupo el orden sigue siendo FLAC > WAV > MP3 → tamaño → score.
  Antes: un FLAC gigante con cola de 40 ganaba a un MP3 320 listo para
  empezar. Ahora empezamos ya.

## 0.4.19

- Nueva columna **Cola** en Descargas de Actividad Soulseek. Muestra
  el `placeInQueue` que reporta slskd — así ves si tu descarga es la
  siguiente o hay 12 delante.
- Cuando una búsqueda devuelve **0 resultados aceptables** (todos los
  peers están por debajo del min bitrate exigido), el status ahora lo
  dice explícitamente en vez de dejar la panel vacío en silencio.
- Logging fino en `_on_search_selected` y `_populate_results` — si
  hay algún salto raro entre búsquedas veremos exactamente qué guard
  se disparó en `~/.maceta.log`.

## 0.4.18

- **Fix crítico: bucle infinito en Actividad Soulseek**. Los logs
  reales mostraban **7500+ llamadas a `_fetch_results_worker` sobre el
  mismo sid en un segundo**, saturando Python y bloqueando la UI.
  Causa: el binding `<<TreeviewSelect>>` disparaba
  `_on_search_selected` + `_sync_dl_from_search`, y este segundo
  hacía `dl_tree.selection_set()` que a su vez disparaba el bind del
  dl_tree, que re-seleccionaba en search_tree, y vuelta a empezar.
  Fix: `_on_search_selected` es ahora idempotente (no repite fetch
  si ya está mostrando ese sid o hay uno en curso); los sync-guards
  mantienen la flag activa 150 ms para cubrir el evento async que
  dispara Tk tras `selection_set`; los syncs no hacen nada si la fila
  ya está seleccionada.
- Nueva referencia persistente **descarga → original**: cuando
  encolas una descarga para una fila de Biblioteca, se guarda un
  registro en `downloads/.maceta_links.json` con la ruta original,
  el nombre y la query. Sirve para que, aunque después vacíes la
  Biblioteca o cierres Maceta, siga siendo posible sustituir.

## 0.4.17

- **Actividad Soulseek: acciones que ya no cuelgan Tk**. Todas las
  operaciones que hacen HTTP a slskd (cancelar búsqueda/descarga,
  limpiar completadas, limpiar todo, encolar descargas del panel
  Resultados, rebuscar, retry download) pasan ahora por un hilo
  aparte. Antes se ejecutaban en el hilo Tk y con los retries de red
  podían bloquearlo 10-30 s.
- Fix bug menor: en `_populate_results` había una segunda `status.set`
  que referenciaba una variable inexistente y silenciosamente lanzaba
  `NameError` cada vez que se cargaban resultados.
- Todos los workers loggean start / done / duración (`~/.maceta.log`).

## 0.4.16

- **Las columnas ya no se autoencogen** al arrastrar una para
  agrandarla. Se aplica `stretch=False` + `minwidth` (mitad del ancho
  inicial) a todas las columnas de las 4 tablas: Biblioteca,
  Búsquedas, Resultados y Descargas de Actividad. Ampliar una columna
  ahora dispara el scroll horizontal si excede el ancho visible en vez
  de robar espacio a las demás.

## 0.4.15

- Panel Resultados: nuevo orden de columnas
  `# · Fichero · Cola · Slot · MB · Ext · kbps · Usuario · Carpeta · Vel`.
  El `#` es el mismo `#N` de la búsqueda de la que provienen los
  resultados — así ves de un vistazo a qué búsqueda pertenece cada
  resultado.

## 0.4.14

- **Cambiar de pestañas ya no bloquea la UI**:
  - Al entrar en **Descargas**, el escaneo del disco + lectura de
    metadata pasa a un hilo. Los ficheros se insertan en el tree en
    lotes de 50, dando aire a Tk entre lote y lote.
  - Al restaurar la última carpeta, `_scan_folder` inserta también en
    lotes (antes: 5000 tareas encoladas de una).
- **Filtro incremental**: `_apply_filter` sólo hace
  `reattach`/`detach` para filas cuya visibilidad **cambia**. Con 3000
  filas quietas → 0 llamadas al tree. Antes eran 3000 por refresh.
- **Búsquedas más generosas**: paso `searchTimeout=30000` (30 s) a
  slskd al arrancar cada búsqueda (default de slskd son 15 s). Da
  tiempo a peers lentos y reduce los `Completed, TimedOut` con 0
  resultados.
- Logs con timings de scan de descargas, scan de carpeta y filter
  (cuando pasa de 100 ms).
- **Panel Resultados reordenado**: columnas ahora en orden
  `Cola · Slot · MB · Fichero · Ext · kbps · Usuario · Carpeta · Vel`
  para que las tres decisiones clave (cuánto queue, si hay slot libre,
  tamaño) estén a la izquierda antes del nombre.

## 0.4.13

- **Nuevos logs**: Maceta escribe a `~/.maceta.log` (rota a 2 MB × 3
  ficheros). Nivel INFO por defecto, ajustable con `MACETA_LOG=DEBUG`.
  Nuevo menú `Ayuda → 📋 Abrir log…`.
- **Se eliminan los pings HTTP** en el hilo Tk al pulsar "Bajar con
  Soulseek" o "Bajar de Soulseek". Eran llamadas de red síncronas que
  con los retries de 0.4.10 podían congelar la UI 10-20 s. Ahora
  cualquier fallo de conexión aparece como error en el status/fila
  cuando el worker realmente lo intenta.
- **Disco fuera del hilo Tk**: el `rglob(downloads/)` que hacía
  `_activity_reconcile` cada 4 s ahora se ejecuta en el hilo del
  poller, no en Tk. En bibliotecas con muchas descargas eso era el
  origen de los "freezes" al buscar.
- Cada búsqueda, batch y fetch de resultados loggea inicio/fin y
  duración — sabemos exactamente en qué se atasca si algo va lento.

## 0.4.12

- **Actividad Soulseek ya no se cuelga**:
  - Al seleccionar una búsqueda, la lectura de resultados se hace en un
    hilo aparte. El Tk loop no se congela aunque slskd tarde 10 s en
    responder.
  - Al clickar en Resultados / cargar respuestas ya no hay ping HTTP
    "de calentamiento" bloqueante — se elimina la llamada redundante.
  - El poller de Actividad sólo re-pinta filas que han cambiado
    (diff contra el último snapshot). Evita repintar 100 filas iguales
    cada 4 s.
  - Sincronización #N búsqueda ↔ descarga simplificada.

## 0.4.11

- Nuevo **🏷 Renombrar inteligente / editar tags** en el menú
  contextual de Biblioteca (click derecho sobre una fila).
  - Muestra los tags ID3/vorbis actuales (Título, Artista, Álbum,
    Nº pista, Año, Género), editables.
  - Botón **🪄 Auto-detectar desde nombre**: extrae artista y título
    del nombre del fichero limpiando prefijos de pista (`01.`,
    `007 -`), bloques `[…]/(…)/{…}`, tokens de bitrate/formato y
    underscores.
  - Vista previa del **nuevo nombre de fichero** que se calcula
    automáticamente como `Artista - Título.ext`. Editable a mano.
  - Dos checkboxes: `Actualizar tags ID3` y `Renombrar fichero` —
    puedes hacer una, otra o ambas.
- Búsquedas de Soulseek vuelven a ser **secuenciales** (una a la vez)
  para no saturar slskd ni el hilo Tk. El paralelismo de 0.4.10 se
  revierte.

## 0.4.10

- **Búsquedas más fiables**:
  - Timeout de espera sube de 25 s a **60 s** y aceptamos parciales:
    con ≥5 respuestas tras 15 s ya damos por buena la búsqueda.
  - Todas las llamadas HTTP a slskd (start_search, get_responses,
    enqueue_download, list_*) reintentan una vez ante fallos de red
    transitorios con 0.5 s de backoff.
  - Batches (`Bajar todas FAKE`, `Bajar con Soulseek` con selección
    múltiple) corren **hasta 3 búsquedas en paralelo**.
- **⟳ Rebuscar auto** — nuevo checkbox en Actividad Soulseek. Cuando
  está activo, si una búsqueda falla ("sin resultados", "calidad baja"
  o error), se reprograma automáticamente. Configurable:
  - **Intervalo** entre reintentos (default 5 min).
  - **Máximo de reintentos** (default 10, `0` = infinito).
  - Configurar en `Preferencias → ⟳ Configurar rebúsqueda auto…`.
- Puedes detener el auto-retry para una canción concreta con click
  derecho → `🛑 Detener rebúsqueda auto`.
- **Duplicados en descargas**: al elegir el mejor resultado, si el
  fichero ya vive en `downloads/`, en vez de bajarlo otra vez marca
  `ya en descargas: …` y usa el existente.
- **Auto-limpiar Descargas**: cuando una sustitución completa, el
  fichero descargado deja de aparecer en la pestaña Descargas (ya está
  en su sitio final).
- **Editor de colores simplificado**: menos claves, click directo sobre
  el swatch → color picker → aplica y guarda al instante. Restaurar
  defaults con un botón.

## 0.4.9

- Al auto-arrancar SoulseekQt ya no salta a primer plano. En macOS se
  lanza con `open -g -j` (oculto), en Windows con `SW_SHOWMINNOACTIVE`
  (minimizado sin robar foco). Podrás activarlo desde su icono si lo
  necesitas, pero por defecto no molesta.

## 0.4.8

- **Comparador visual antes de sustituir**: al hacer "Sustituir" (desde
  Biblioteca o desde una descarga) ahora sale una ventana side-by-side
  con el original a la izquierda y la candidata a la derecha.
  - Metadata de cada uno (formato, kbps, SR, duración, MB).
  - **Espectros** de ambos calculados sobre 60 s del centro y dibujados
    con líneas guía a 16/19/20 kHz y marca en el corte detectado.
  - Botones **▶/⏹** independientes para reproducir cada lado.
  - Verdict local (`OK / FAKE / LOSSLESS`) según el corte medido.
- Solo cuando pulsas `✓ Sustituir` se mueve el original a `archivo/` y
  se pone la nueva en su sitio. Cancelar deja todo intacto.

## 0.4.7

- **Opciones visibles en Biblioteca**: los 4 checkboxes de preferencias
  (`Preguntar calidad`, `Sustituir original`, `Autoconv FLAC→MP3 320`,
  `Forzar 320`) ahora se ven encima del miniplayer sin abrir el menú.
- **Persistencia de estado**: Maceta recuerda entre sesiones tu última
  carpeta, los 4 toggles, el chip activo y el filtro "Solo pdtes.
  sustituir". Al arrancar restaura la carpeta y la vuelve a escanear
  sola. Todo en `~/.maceta_config.json` (sección `user_state`).
- Actividad: columna `#` movida al **primer sitio** en Búsquedas y
  Descargas.
- Al seleccionar una búsqueda se **auto-selecciona la descarga** con
  el mismo `#N` (y viceversa) — así ves de un vistazo qué búsqueda
  disparó qué descarga.
- **Resultados** ordenados por defecto por **MB descendente**: el
  fichero más grande sube arriba. Los "320 kbps" mentirosos de 3 MB caen
  al final. Puedes reordenar clicando cualquier cabecera.

## 0.4.6

- **Actividad Soulseek reorganizada**: Búsquedas y Resultados ahora están
  **lado a lado** en la parte de arriba (arrastra el divisor para
  redimensionar). Descargas queda debajo, ocupando el ancho completo.
- **Scrollbars horizontales** en todas las tablas para leer nombres
  largos sin cortar.
- **Descargas — botones repartidos en 2 filas** para que quepan todos
  sin salirse: playback + sustitución arriba, borrado + limpieza de
  incompletos abajo.
- Botones de Actividad con etiquetas más cortas (Cancelar / Rebuscar…
  / Limpiar / ↻) para caber cuando el pane está estrecho.

## 0.4.5

- Nuevo botón **🧹 Limpiar no completados** en la pestaña Descargas.
  Vacía `downloads/.incomplete/` (los trozos que deja slskd cuando una
  descarga falla o se cancela). Confirma antes con tamaño total y avisa
  si hay descargas activas.

## 0.4.4

- Nueva pestaña **Descargas**: lista independiente de todos los ficheros
  que hay en `downloads/`. Reproducir, sustituir, borrar, mostrar en
  Finder. Se refresca al abrir la pestaña.
- **Panel Resultados inline** en Actividad Soulseek: al clickar una
  búsqueda ves sus resultados abajo sin popup. Selección múltiple
  (Cmd/Shift+click) para bajarte varios ficheros de golpe (útil para
  álbumes). Nueva columna Carpeta.
- Nuevo botón **🔗 Sustituir original con esta…** en Descargas: elige el
  fichero descargado, luego a qué canción de Biblioteca reemplazar.
- **Popup de novedades** tras actualizar: al arrancar una versión nueva
  te sale un resumen con lo que cambió desde tu versión anterior (esta
  ventana).

## 0.4.3

- **Descargas fuera de Biblioteca**: los ficheros que bajas ya no
  aparecen como filas en Biblioteca. Solo se ven en Actividad → Descargas
  mientras están en curso, y en el diálogo "Sustituir con descarga…".
- Nueva preferencia **Forzar 320** (off por defecto). Con ella
  desactivada, aceptamos cualquier bitrate mejor que el actual, con un
  mínimo de 192 kbps. Activada, exige 320 estricto + análisis espectral.
- El objetivo de calidad y el filtro de resultados se ajustan según esa
  preferencia.

## 0.4.2

- **Política target 320**: MP3 declarado a menos de 320 kbps se marca
  automáticamente como FAKE. Antes se validaba contra su bitrate
  declarado, ahora contra el objetivo de 320.
- Nuevo chip **⬇ Sustituidas** en Biblioteca. Las canciones ya
  reemplazadas o convertidas por Maceta salen de "Listas" y aparecen
  aparte.
- **Verificación post-descarga**: tras el match por chroma, revalidamos
  que el fichero descargado cumple realmente el target. Si un peer
  anuncia 320 kbps pero es un fake, se rechaza (`✗ calidad baja`).

## 0.4.1

- **Chips de estado** en Biblioteca: `Todas · A revisar · En proceso ·
  Listas · Sin analizar`, con contadores en vivo y filtrado al click.
- Columna **Calidad** enriquecida con emoji y contexto:
  `✅ 320 OK`, `🎵 LOSSLESS`, `❌ FAKE`, `⬇ Sustituida`, `⏳ En cola`,
  `🔎 Buscando`, `⚠ Revisar match`, `❓ sin analizar`.

## 0.4.0

- **Auto-actualización**: Maceta ahora puede bajarse a sí misma la
  última versión desde el repo. Menú `Ayuda → Configurar
  actualizaciones…` para pegar el token. Auto-comprueba al arrancar.

## 0.3.x — Sesiones iniciales (retrospectivo)

Todo lo que Maceta tenía antes de introducir la constante `VERSION` y
el mecanismo de auto-update en 0.4.0. Reconstruido a partir de
`WORKLOG.md`.

### Análisis de audio
- Escaneo recursivo de carpeta (mp3, flac, m4a, aac, ogg, opus, wav, wma)
  con metadatos leídos vía `mutagen` (bitrate del frame real, no del tag).
- Análisis del audio con `librosa` cap a 5 min del centro del track
  para no colgarse en mixes de DJ.
- BPM con `librosa.beat.beat_track(start_bpm=120)` (prior arregla la
  mayoría de errores de octava en música bailable).
- Detección de fake 320 kbps: PSD por Welch (Hann + FFT 8192), corte a
  −60 dB del pico, comparativa con curvas típicas de LAME por bitrate.
- Veredictos con colores en la tabla: OK, LOSSLESS, SOSPECHOSO (retirado
  en 0.4.2), FAKE, ERROR, PENDING.
- Ventana de espectro por doble-click en fila (matplotlib TkAgg).

### UX de análisis
- Botones **Analizar selección** / **Analizar todas** con feedback
  por fila (`analizando…` antes de cada archivo), nombre en la barra de
  estado, contador `N/total`.
- Ordenación por click en cabecera, `Cmd/Ctrl+A` para seleccionar todas.

### Persistencia y export
- `~/.maceta_config.json` para URL/API key de slskd y rutas de
  binarios.
- Export a **CSV** con `QUOTE_ALL` + `doublequote` (RFC 4180): comillas
  dobles en nombres se escapan como `""`.

### Integración con Soulseek/slskd
- Cliente HTTP `SlskdClient` (start/stop search, wait, get responses,
  enqueue/list/cancel downloads).
- Filtro `is_acceptable`: FLAC/WAV o MP3 ≥ 320 kbps, cap 150 MB.
- Scoring y `choose_best`: prioriza codec (FLAC>WAV>MP3), luego tamaño
  en bytes, luego score compuesto (slot libre, cola corta, velocidad,
  similitud de nombre).
- Diálogo de config con URL, API key, rutas para auto-arrancar slskd
  + SoulseekQt al abrir Maceta si no están ya corriendo.

### UI a 2 pestañas
- **Notebook** con Biblioteca + Actividad Soulseek.
- Actividad: tabla de búsquedas y de descargas, columna `#N`
  correlacionando búsqueda ↔ descarga.
- Botones para cancelar, rebuscar (editar query), limpiar completadas,
  refrescar, y **🗑 Limpiar todo**.

### Sustitución y archivado
- Carpeta `archivo/` espejo de la ruta absoluta del original.
- `substitute_file`: mueve original a archivo/, coloca descarga en el
  sitio del original con la extensión real del descargado.
- Verificación de "misma canción" con `librosa.feature.chroma_stft`
  (30 s del centro) y similitud coseno, umbral 0.85, tolerancia ±8%
  de duración.
- **🔗 Sustituir con descarga…**: diálogo con original + lista de
  ficheros en `downloads/`, preview con el miniplayer, sustitución
  manual saltándose el chroma check.

### Otras acciones sobre biblioteca
- **Convertir a MP3 320** (sólo lossless FLAC/WAV) vía ffmpeg del env.
- **Autoconvertir FLAC → MP3 320**: checkbox. Tras la descarga
  transcodifica y borra el FLAC de forma permanente.
- **🗑 Borrar (a archivo/)**: mueve seleccionadas a archivo/
  (recuperable).
- **Vaciar lista**: limpia la pantalla sin tocar ficheros.

### Búsquedas
- Query cleaning fuerte: prefijos `01-`, `007-`, brackets, paréntesis,
  tokens de bitrate y de formato.
- Botones **Bajar todas FAKE** y **Bajar todas SOSPECHOSAS** (esta
  segunda retirada en 0.4.2).
- Con una única fila seleccionada, `Bajar con Soulseek` abre primero un
  editor de query.
- Rebuscar (editar query) desde la lista de descargas erroreadas, con
  soporte de link_iid para sustituir el original cuando complete.

### Miniplayer
- **▶ Reproducir** / **⏹ Parar** en Biblioteca usando `ffplay` del env.
  Etiqueta `▶ nombre_cancion.mp3` en la barra del player.

### Filtros y navegación
- Caja **🔎 Filtrar** en Biblioteca — oculta filas que no matchean
  por nombre o ruta. Después mejorado a wildcards y solo-nombre.
- Scrollbars en las tablas de búsquedas y descargas.

### Colores
- Editor inicial de colores con 14 claves tuneables (banner, tabla,
  selección, veredictos, now playing, status). Simplificado en 0.4.10.

### Disclaimer
- Banner amarillo permanente arriba con el aviso de backup.

### Fixes y ajustes de esa sesión
- Bug del `.mp3.tmp` (extensión inválida para ffmpeg) → nombre temp
  `<stem>.maceta_tmp.mp3` con `-f mp3` explícito.
- Callbacks del tree defensivos (chequean `iid in rows_by_iid`) para
  soportar `Vaciar lista` mientras hay análisis en curso.
- Reproductor: al cerrar Maceta corta el ffplay hijo limpiamente.
- Captura de stderr de ffmpeg para mostrar el error real en la fila.
- **SOSPECHOSO** eliminado como veredicto: los cortes en la franja
  intermedia pasan a OK (calidad "prácticamente equivalente").
- Filtro tokenizado por palabras (AND) y luego por wildcards visibles.
- Cabeceras clicables para ordenar en `QualityPickerDialog` y
  `ManualSubstituteDialog`.

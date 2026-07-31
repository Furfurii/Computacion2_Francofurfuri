# Monitor de Procesos y Threads

**Trabajo Práctico Nº 1 — Computación II — Universidad de Mendoza**
**Autor:** Franco Furfuri — 3º año, Ingeniería Informática

---

## 1. Descripción general

Es un monitor de procesos estilo `htop`, escrito en Python 3 puro (sin `psutil`), que lee
`/proc` directamente para exponer la anatomía interna de cada proceso del sistema: memoria,
file descriptors, threads (LWPs), señales pendientes/bloqueadas, scheduling y estadísticas
globales.

La arquitectura es **multiproceso**: un proceso por cada una de las 7 dimensiones de datos,
más un proceso de interfaz (TUI), todos leyendo y escribiendo sobre un snapshot compartido en
memoria.

### Cómo se usa

```
python3 main.py
# o, dentro de Docker:
docker compose up --build
```

| Tecla | Acción |
|---|---|
| `1`/`r` `2`/`m` `3`/`f` `4`/`t` `5`/`s` `6`/`p` `7`/`g` | Cambiar de vista (Resumen, Memoria, FDs, Threads, Señales, Scheduling, Sistema) |
| `↑` / `↓` | Navegar la lista de procesos |
| `Enter` | Pinear/despinear el proceso seleccionado |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario |
| `c` | Alternar orden (CPU % → RSS → PID) |
| `+` / `-` | Ajustar el intervalo de refresco de la vista activa |
| `h` / `?` | Mostrar/ocultar ayuda |
| `q` | Salir limpiamente |

En las vistas **Threads** y **FDs**, seleccionar un proceso (con `↑`/`↓`) expande su detalle
(cada LWP con su TID, o cada FD con su destino) justo debajo de la fila del proceso.

---

## 2. Arquitectura

```
                    ┌───────────────────────────────────────┐
                    │        Manager().dict() "snapshot"      │
                    │  (proceso servidor de multiprocessing)  │
                    │ ┌────────────────────────────────────┐ │
                    │ │ resumen | memoria | fds | threads   │ │
                    │ │ senales | scheduling | sistema      │ │
                    │ └────────────────────────────────────┘ │
                    └───▲────────────────────────────▲───────┘
                        │ escriben (1 asignación         │ lee cada
                        │  atómica por ciclo)             │ refresco
        ┌───────────────┼───────────────┬────────────────┴─────────┐
        │               │               │                          │
   ┌────▼────┐    ┌─────▼────┐   ┌──────▼─────┐            ┌───────▼───────┐
   │ Resumen │    │ Memoria  │   │    FDs     │   ...(x7)  │    Display     │
   │ Process │    │ Process  │   │  Process   │            │    (TUI)       │
   │  cada   │    │  cada    │   │   cada     │            │  rich + Live   │
   │Value(2s)│    │Value(3s) │   │ Value(5s)  │            └───────┬───────┘
   └─────────┘    └──────────┘   └────────────┘                    │
                                                                     │ teclado (/dev/tty)
        main.py (proceso padre)                                     │
        ├── self-pipe: SIGINT/TERM/HUP/USR1/USR2/WINCH               │
        ├── control = Manager().dict() → {'salir', 'filtros_*'} ─────┘
        └── intervalos = {vista: Value('d', seg)} × 7  (ajustables con +/-)
```

**Componentes:**

| Proceso | Archivo | Frecuencia default |
|---|---|---|
| Resumen | `analizadores/resumen.py` | 2 s |
| Memoria | `analizadores/memoria.py` | 3 s |
| FDs | `analizadores/fds.py` | 5 s |
| Threads | `analizadores/threads.py` | 2 s |
| Señales | `analizadores/senales.py` | 10 s |
| Scheduling | `analizadores/scheduling.py` | 10 s |
| Sistema | `analizadores/sistema.py` | 2 s |
| Display (TUI) | `display.py` | refresca a 4 fps, lee `snapshot` en cada frame |

`main.py` es el orquestador: carga `config.json`, crea el `Manager`, los 7 `Value` de
intervalo y lanza los 8 procesos (`multiprocessing.Process`). También instala los handlers de
señales (self-pipe, ver `senales.py`) y corre el loop que interpreta esas señales.

Cada analizador es un **proceso independiente**, no un thread: así, si uno se cuelga o muere
(por ejemplo con `kill -9 <pid>` a mano), el resto sigue funcionando — el display simplemente
deja de ver actualizarse esa vista puntual (el dato queda "congelado" con su último
`timestamp`), porque cada uno escribe su propia clave del `snapshot` sin depender de los demás.

---

## 3. Decisiones de diseño

### Por qué `Manager().dict()` para el snapshot y no `Value`/`Array`

El snapshot necesita guardar estructuras heterogéneas y de tamaño variable: diccionarios
anidados por PID, con distinta cantidad de threads o FDs en cada lectura. `Value` y `Array`
son para tipos fijos de tamaño conocido de antemano (`ctypes`) — no sirven para "un dict con
tantas claves como PIDs haya en este instante". `Manager().dict()` corre un proceso servidor
aparte que expone un proxy: cualquier proceso cliente puede leer/escribir claves arbitrarias
sin preocuparse por el tamaño. El costo es que cada acceso implica un viaje por socket/pipe al
servidor (más lento que memoria compartida cruda), pero para refrescos de 0.5–10 segundos ese
costo es insignificante frente a la simplicidad que da.

`Value` sí se usa, pero para lo opuesto: un escalar de tamaño fijo (el intervalo en segundos de
cada vista) que cambia con `+`/`-` desde el display y es leído constantemente por el
analizador correspondiente. Ahí un `Manager.dict()` sería overkill; un `Value('d', ...)` es
memoria compartida real (vía `mmap`), sin proceso servidor de por medio.

### Por qué self-pipe para señales

Los handlers de señal corren en un contexto "async-signal-safe": casi ninguna función normal
de Python (`print`, `open`, `json.dump`, incluso el propio GIL en ciertos casos) es segura de
llamar ahí, porque el proceso puede estar interrumpido a mitad de una estructura interna. La
solución (patrón self-pipe, `senales.py`) es que el handler *solo* haga `os.write(pipe, byte)`
— la operación mínima segura — y que el loop principal de `main.py`, corriendo en contexto
normal, lea ese pipe cada 0.5 s y ejecute la acción real (dump a JSON, reload de config,
print). Handler y loop nunca se bloquean entre sí porque el pipe está en modo no bloqueante
(`fcntl.O_NONBLOCK`).

### Cómo se evitan las race conditions

- **Snapshot:** cada analizador arma su `dict` completo en una variable local y recién al
  final hace `snapshot['vista'] = datos` — una única asignación atómica sobre el proxy del
  `Manager`. El display nunca ve un diccionario a medio construir; en el peor caso lee la
  versión anterior completa o la nueva completa, nunca una mezcla.
- **FDs/threads que mueren durante la lectura:** `leer_fds` y `leer_threads` (en `procfs.py`)
  hacen `os.listdir()` y después `os.readlink()`/`open()` por entrada; si el proceso murió o
  cerró ese FD entre medio, se captura `FileNotFoundError`/`PermissionError` puntual y se
  saltea esa entrada en vez de abortar la lectura completa.
- **CPU % con delta de jiffies:** cada analizador mantiene su propio historial
  `{pid: (jiffies, timestamp)}` *dentro de su propio proceso* (no compartido), y lo reemplaza
  entero en cada vuelta (`historial = historial_nuevo`) para no arrastrar PIDs reciclados con
  un valor viejo.
- **Teclado vs. redibujado:** `display.py` lee el teclado de forma no bloqueante
  (`select.select(..., 0.1)`) dentro del mismo loop que redibuja con `rich.Live`; no hay un
  thread de teclado separado compitiendo por el mismo estado, así que no hace falta lock ahí.

### Por qué esos intervalos default

Se calibraron según cuánto cambian los datos y cuán cara es leerlos:
- **Resumen/Threads/Sistema (2 s):** cambian rápido (CPU%, estados) y son baratos de leer
  (`stat`/`status` de cada PID).
- **Memoria (3 s):** un poco más cara porque además recorre `/proc/<pid>/maps` línea por línea
  para agrupar segmentos.
- **FDs (5 s) y Scheduling/Señales (10 s):** cambian poco de un momento a otro en un sistema
  normal (un proceso no abre/cierra FDs constantemente, ni cambia de política de scheduling),
  y leer FDs en particular implica un `readlink()` por descriptor — puede ser costoso en
  procesos con cientos de FDs abiertos.

Todos son ajustables en caliente con `+`/`-` (mínimos definidos en `INTERVALOS_MIN` en
`display.py`) sin reiniciar el monitor, comunicando el nuevo valor directamente al `Value`
compartido con el analizador correspondiente.

---

## 4. Conceptos del curso aplicados

- **Procesos vs. threads (Clase 3 y 10):** cada analizador es un `Process`, no un `Thread`,
  justamente por el GIL — threads de Python no ejecutan bytecode en paralelo en distintos
  cores, así que solo procesos separados escapan a esa limitación y aprovechan varios cores
  para leer `/proc` en paralelo. Además, procesos aislados significan que un bug en un
  analizador no tumba a los demás (memoria separada), mientras que un crash en un thread
  puede corromper el estado compartido de todo el proceso.
- **Threads como LWP (Clase 10):** en `procfs.leer_threads`, cada entrada de
  `/proc/<pid>/task/<tid>/` se lee con el mismo parser que un proceso normal, porque un thread
  en Linux *es* una tarea del kernel (modelo 1:1) que comparte memoria, FDs y PID con sus
  hermanos, pero tiene TID, stack, registros y señales pendientes propias. El TID del thread
  principal coincide con el PID del proceso.
- **fork/exec/wait, zombies (Clase 4):** el estado `Z` que aparece en `/proc/<pid>/stat` campo
  3 identifica un proceso terminado cuyo padre todavía no llamó a `wait()`; se cuenta
  agrupando por letra de estado en `analizador_sistema` y se ve también en las vistas por PID.
- **Pipes y file descriptors (Clase 5):** `procfs.leer_fds` usa `os.readlink()` (nunca `open()`)
  sobre `/proc/<pid>/fd/<n>` para saber a dónde apunta cada descriptor sin interferir con el
  recurso real — un socket detectado así nunca se llega a leer, solo se reporta que existe.
  El self-pipe de señales es, en sí mismo, un pipe anónimo (`os.pipe()`) usado para IPC
  mínimo entre un contexto de señal y el loop principal.
- **Señales, async-signal-safe (Clase 6):** ver la sección de self-pipe más arriba. Las 5
  señales obligatorias están implementadas en `senales.py` + el loop de `main.py`.
- **mmap y memoria compartida (Clase 7):** los `Value('d', ...)` de intervalo son memoria
  compartida real vía `mmap` bajo el capó de `multiprocessing`; se contrastan en el código con
  el `Manager().dict()`, que en cambio usa un proceso servidor + proxies (más flexible, más
  lento).
- **Multiprocessing y `Manager` (Clases 8 y 9):** todo el sistema de snapshot/agregación.
- **Memoria virtual de un proceso (Clase 3):** `procfs.leer_maps` clasifica cada línea de
  `/proc/<pid>/maps` en heap/stack/text/lib/anon según permisos y la etiqueta entre corchetes,
  y se contrastan conceptualmente heap (crecimiento dinámico, GC en Python) vs. stack
  (tamaño acotado, variables locales).

---

## 5. Limitaciones conocidas

- La vista **Sistema** no calcula el "Top 3 por CPU y por memoria" que sugiere el enunciado;
  solo muestra CPU global, memoria, load average, conteo de procesos por estado y uptime.
- No se suma la cantidad total de threads del sistema (sí se ve por proceso en la vista
  Threads/Resumen).
- `SIGWINCH` está capturado y le llega al loop principal, pero no dispara ninguna acción
  explícita — se confía en que `rich.Live` redibuje solo al cambiar el tamaño de la terminal.
- El primer refresco de CPU % después de arrancar (o después de que aparece un proceso nuevo)
  siempre muestra `0.0`, porque el cálculo necesita dos lecturas de jiffies para sacar un
  delta; recién en el segundo ciclo del analizador correspondiente el valor es representativo.
- Si el usuario que corre el monitor no es root, los procesos de otros usuarios (o de `root`)
  van a aparecer con FDs/maps vacíos o inaccesibles — es el comportamiento esperado de permisos
  de `/proc`, manejado con `PermissionError` capturado, no un bug.
- El detalle expandido de un proceso (threads/FDs/segmentos de memoria) solo se ve para **el
  proceso actualmente seleccionado**, no para todos a la vez — es una decisión de espacio en
  pantalla, no una limitación de los datos disponibles (están todos en el snapshot).

---

## 6. Cómo correr y testear

### Con Docker (recomendado)

```bash
docker compose up --build
```

El `docker-compose.yml` monta `/proc` del host como solo lectura (`/proc:/proc:ro`) y usa
`pid: host`, así el monitor ve los procesos reales de la máquina y no solo los del contenedor.
`tty: true` + `stdin_open: true` habilitan la TUI interactiva.

### Local (sin Docker)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

### Probar las señales

Con el monitor corriendo, desde otra terminal (`PID` es el que imprime `main.py` al arrancar):

```bash
kill -HUP  <PID>   # recarga config.json (intervalos y filtros default)
kill -USR1 <PID>   # dump del snapshot a dump_<timestamp>.json
kill -USR2 <PID>   # toggle modo verbose
kill -TERM <PID>   # shutdown limpio (igual que Ctrl+C)
```

### Verificación de Docker realizada en esta revisión

Se probó `docker build` sobre el `Dockerfile` (build OK, instala `rich` sin errores) y se
corrió el contenedor con las mismas flags que usa `docker-compose.yml`
(`--pid=host -v /proc:/proc:ro`): arrancó los 8 procesos, mostró datos reales del host en la
vista Resumen, y respondió correctamente a `SIGTERM` con shutdown limpio.

---

## 7. Decisiones sobre la TUI

Se eligió **`rich`** (`Live` + `Layout` + `Table`) por sobre `curses` porque da tablas con
estilos, colores y layout responsive con mucho menos código de bajo nivel (posicionamiento de
celdas, manejo de resize), a costa de menos control fino sobre el refresco — suficiente para
este TP, donde el foco es el contenido de los datos y no la performance del renderizado.

El layout tiene 3 franjas fijas (header con el estado del monitor, body con la vista activa,
footer con los atajos) y dentro del body, cada vista es una `Table` distinta armada en
`_render_vista`. Las vistas Threads, FDs y Memoria usan un patrón de "fila resumen + filas de
detalle": la fila normal por PID muestra los agregados, y si ese PID está seleccionado, se
insertan filas adicionales (estilo `dim`) con el desglose (cada thread, cada FD, o los campos
extendidos de memoria) — así no hay que elegir entre "ver todos los procesos" o "ver el detalle
de uno", conviven en la misma tabla.

El teclado se lee desde `/dev/tty` directamente (no `sys.stdin`), porque `multiprocessing`
reemplaza el stdin de los procesos hijos por `/dev/null`; abrir la terminal controladora a
mano es lo que permite que el proceso `display` (que sí es un hijo del padre) siga recibiendo
teclas.

---

## 8. Lo que aprendí


Lo que más me costó entender al principio fue por qué un handler de señal no puede simplemente
hacer lo que necesito (escribir un JSON, por ejemplo) ahí mismo. Una vez que entendí que el
proceso puede estar interrumpido literalmente en cualquier punto — incluso a mitad de un
`malloc()` interno o de un buffer de stdout — tuvo sentido por qué existen listas de funciones
"async-signal-safe" y por qué el patrón self-pipe es la solución estándar: reducir el handler a
la operación mínima posible y mover todo el trabajo real a un contexto normal.

Lo otro que terminé de entender con este TP es la diferencia real entre proceso y thread más
allá de la definición de manual: ver con mis propios ojos que un LWP en `/proc/<pid>/task/`
tiene *casi* la misma estructura que un proceso (su propio `stat`, su propio `status`) pero
comparte memoria y FDs con sus hermanos, hizo mucho más concreto por qué el GIL afecta a los
threads y no a los procesos, y por qué elegí procesos para los analizadores en lugar de
threads.

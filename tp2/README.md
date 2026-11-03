# TP2 — Plataforma de Tareas Distribuidas

**Computación II — Universidad de Mendoza — 2026**
**Autor:** Franco Furfuri (legajo 64180) — [@Pancho26](https://github.com/Pancho26)

Servicio HTTP al que se le encargan trabajos, que los ejecuta de forma
concurrente con un pool de workers asíncronos y permite consultar su estado,
cancelarlos y ver estadísticas. Todo corre en **un solo hilo con asyncio**; el
trabajo CPU-bound se deriva a un **pool de procesos**.

---

## Índice

1. [Descripción y uso](#1-descripción-y-uso)
2. [Arquitectura](#2-arquitectura)
3. [Decisiones de diseño](#3-decisiones-de-diseño)
4. [Mediciones](#4-mediciones)
5. [Conceptos del curso aplicados](#5-conceptos-del-curso-aplicados)
6. [Limitaciones conocidas](#6-limitaciones-conocidas)
7. [Lo que aprendí](#7-lo-que-aprendí)

---

## 1. Descripción y uso

### Levantar el sistema

```bash
git clone https://github.com/Pancho26/Computacion2_Francofurfuri.git
cd Computacion2_Francofurfuri/tp2
docker compose up --build
```

La API queda en **http://localhost:8000** (redirige a la documentación
interactiva Swagger en `/docs`). `Ctrl+C` o `docker compose down` disparan el
apagado ordenado.

### Configuración (variables de entorno)

| Variable | Default | Qué controla |
|----------|---------|--------------|
| `N_WORKERS` | `4` | Corrutinas consumidoras de la cola. Se puede pasar al levantar: `N_WORKERS=64 docker compose up -d` |
| `N_PROCESOS` | `0` (= núcleos) | Procesos del `ProcessPoolExecutor` para tareas CPU-bound |
| `TIMEOUT_DEFAULT` | `60` | Timeout (s) de una tarea que no especifica el suyo |
| `TIMEOUT_APAGADO` | `10` | Cuánto se espera a las tareas en curso al apagar |
| `DIR_ESTADO` | `/app/datos` | Dónde se vuelcan los `estado_<timestamp>.json` (montado en `./datos`) |

### Endpoints

| Método | Ruta | Qué hace | Respuestas |
|--------|------|----------|------------|
| `POST` | `/tareas` | Encola una tarea y responde **al instante** | `202` con `{id, estado}` · `422` si es inválida · `503` si el servidor se está apagando |
| `GET` | `/tareas?estado=` | Lista tareas, filtro opcional por estado | `200` |
| `GET` | `/tareas/{id}` | Estado, tiempos, resultado o error | `200` · `404` |
| `DELETE` | `/tareas/{id}` | Cancela una tarea **encolada o en ejecución** | `200` (ya cancelada) · `404` · `409` si ya terminó |
| `GET` | `/estadisticas` | Totales por estado, tiempo promedio (general y por tipo), workers ocupados, tamaño de la cola | `200` |
| `GET` | `/demo/lento?ms=` | Responde tras `ms` milisegundos. Servidor lento controlado para la medición 1 | `200` |

Estados: `pendiente → ejecutando → completada | fallida | cancelada`.

### Tipos de tarea

| Tipo | Naturaleza | Parámetros | Resultado |
|------|-----------|------------|-----------|
| `descargar` | I/O-bound | `urls: list[str]`, `secuencial: bool = false` | Por URL: status, bytes, segundos (o error). Total, ok, errores |
| `hashear` | CPU-bound | `texto`, `iteraciones`, `sin_executor: bool = false` | Hash SHA-256 iterado, modo, segundos |
| `esperar` | I/O simulado | `segundos` | `{durmio}` |

Todas aceptan además `timeout` (segundos). Los flags `secuencial` y
`sin_executor` existen **solo para las mediciones**: activan a propósito la
versión mala.

### Ejemplos con `curl`

```bash
# Crear tareas (devuelven 202 + id)
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"descargar","params":{"urls":["https://example.com","https://www.python.org"]}}'

curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"hashear","params":{"texto":"hola","iteraciones":3000000}}'

curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","params":{"segundos":30},"timeout":5}'

# Consultar
curl localhost:8000/tareas
curl 'localhost:8000/tareas?estado=ejecutando'
curl localhost:8000/tareas/<id>
curl localhost:8000/estadisticas

# Cancelar (funciona aunque esté a mitad de ejecución)
curl -X DELETE localhost:8000/tareas/<id>

# Ver que la API NO se congela durante un hash pesado
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"hashear","params":{"texto":"x","iteraciones":10000000}}'
curl -o /dev/null -w '%{time_total}s\n' localhost:8000/tareas     # ~ms

# ...y que sí se congela sin executor
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"hashear","params":{"texto":"x","iteraciones":10000000,"sin_executor":true}}'
curl -o /dev/null -w '%{time_total}s\n' localhost:8000/tareas     # ~6 s
```

### Correr sin Docker

```bash
cd tp2
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -m app.main
```

### Reproducir las mediciones

Con la API levantada, desde el host:

```bash
./venv/bin/python mediciones/medir_io.py         # medición 1
./venv/bin/python mediciones/medir_bloqueo.py    # medición 2
N_WORKERS=4  docker compose up -d --force-recreate && ./venv/bin/python mediciones/medir_techo.py
N_WORKERS=64 docker compose up -d --force-recreate && ./venv/bin/python mediciones/medir_techo.py
./venv/bin/python mediciones/medir_carga.py      # extra: 10.000 tareas
```

Los resultados crudos quedan en `mediciones/resultados/*.json`.

---

## 2. Arquitectura

### Componentes

```
                         ┌──────────────────────── proceso principal (1 hilo, 1 event loop) ───────────────────────┐
                         │                                                                                         │
  Cliente HTTP ── TCP ──>│  uvicorn ──> FastAPI (main.py)                                                          │
  (curl, Swagger)        │                │  valida con Pydantic (modelos.py)                                      │
                         │                v                                                                        │
                         │        ┌───────────────── Gestor (gestor.py) ─────────────────┐                         │
                         │        │                                                      │                         │
                         │        │  tareas: dict[id → Registro]   (estado en memoria)   │                         │
                         │        │                                                      │                         │
                         │        │  POST ──put_nowait(id)──> asyncio.Queue ──get()──┐   │                         │
                         │        │                                                  v   │                         │
                         │        │                         worker-0 … worker-N-1 (corrutinas)                     │
                         │        │                                     │ create_task(wait_for(tarea, timeout))    │
                         │        └─────────────────────────────────────┼────────────────┘                         │
                         │                                              v                                          │
                         │                    tareas.py:  descargar ── httpx.AsyncClient + gather ──────────────────┼──> Internet
                         │                                esperar   ── asyncio.sleep                               │
                         │                                hashear   ── run_in_executor (por bloques) ──┐           │
                         └──────────────────────────────────────────────────────────────────────────────┼───────────┘
                                                                                                        v
                                                                        ProcessPoolExecutor: proceso 1 … proceso K
                                                                        (cada uno con su intérprete y su GIL)
```

### Flujo de una tarea

```
 POST /tareas
    │  Pydantic valida (tipo desconocido / params inválidos → 422, nunca llega a la cola)
    v
 Gestor.encolar: crea Registro(estado=pendiente), cola.put_nowait(id)  ──> responde 202 (≈1 ms)
    │
    │  ... la tarea espera en la cola hasta que un worker esté libre ...
    v
 worker: id = await cola.get()
    │  ¿sigue pendiente? (si la cancelaron en cola, se descarta)
    v
 estado = ejecutando; reg._task = create_task(wait_for(EJECUTORES[tipo](params), timeout))
    │
    ├── termina bien ─────────────────────────> completada  (resultado)
    ├── lanza excepción ──────────────────────> fallida     ("TipoError: mensaje")
    ├── vence el timeout (TimeoutError) ──────> fallida     ("Timeout: superó Xs")
    ├── DELETE → reg._task.cancel() ──────────> cancelada   ("en ejecución")
    └── apagado → se cancela el worker ───────> cancelada   ("apagado del servidor")
    │
    v
 finally: cola.task_done(); el worker vuelve a esperar en cola.get()
```

### Estructura del código

```
tp2/
├── app/
│   ├── main.py      API FastAPI: endpoints, lifespan (arranque / apagado ordenado), config
│   ├── gestor.py    Cola, workers, registro de estado, cancelación, apagado, volcado JSON
│   ├── tareas.py    descargar / hashear / esperar
│   └── modelos.py   Modelos Pydantic: requests (unión discriminada por "tipo"), respuestas, estados
├── mediciones/      Scripts de las mediciones + resultados/*.json
├── datos/           Volcados estado_<timestamp>.json (volumen de Docker)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 3. Decisiones de diseño

### ¿Por qué `asyncio.Queue` y no `queue.Queue`?

`queue.Queue` está pensada para **threads**: su `get()` es bloqueante y duerme
el hilo con un lock del sistema operativo. Acá hay **un solo hilo**, el del event
loop: si un worker hiciera `queue.Queue.get()` con la cola vacía, congelaría el
loop entero (API, otros workers, timeouts) y nadie podría encolar nada. Sería
un deadlock.

`asyncio.Queue.get()` es una **corrutina**: si la cola está vacía, suspende
*solo a ese worker* (`await`) y le devuelve el control al loop, que sigue
atendiendo requests. Cuando un `POST` hace `put_nowait`, el loop despierta a un
worker en espera. Además, `asyncio.Queue` no usa locks: no los necesita porque
todo pasa en el mismo hilo (ver más abajo).

### ¿Por qué el pool de workers no necesita un `Lock`?

Los workers, los endpoints y el dict `tareas` viven en el mismo hilo. Una
corrutina **solo cede el control en un `await`**: entre dos `await` el código
corre de forma atómica respecto de las demás corrutinas. Las modificaciones de
estado (`reg.estado = ...`, `self._ocupados += 1`, `self.tareas[id] = reg`) no
tienen ningún `await` en el medio, así que nadie puede ver un estado a medias.
Con threads, el scheduler del SO puede interrumpir entre cualquier par de
instrucciones de bytecode, y por eso ahí sí hace falta el lock.

### ¿Cómo evito que una tarea CPU-bound congele la API?

`hashear` llama a `loop.run_in_executor(ProcessPoolExecutor, hash_bloque, ...)`.
El cálculo corre en **otro proceso** y la corrutina queda en `await` sobre un
future, así que el loop sigue libre. La medición 2 lo muestra: durante un hash
de 6,7 s la API respondió 284 veces con un p50 de 1,7 ms. Sin executor
respondió **una sola vez** y tardó 6,7 s.

**¿Por qué procesos y no threads (`asyncio.to_thread`)?** Por el **GIL**.
`hashlib` libera el GIL solo con datos de más de 2 KiB, y acá se hashean 32
bytes por vuelta. Un thread estaría compitiendo todo el tiempo con el event
loop por el intérprete: el loop no quedaría congelado, pero sí lento, y no
habría paralelismo real entre varios hashes. Cada proceso tiene su propio
intérprete y su propio GIL, así que hay paralelismo real en otro núcleo. El
costo es serializar con pickle los argumentos (32 bytes) y que `hash_bloque`
tenga que estar a nivel de módulo.

El pool se crea en el `lifespan` (no al importar el módulo) y el arranque está
protegido con `if __name__ == "__main__"`, para que funcione también con
`spawn` (macOS/Windows), que re-importa el módulo en cada hijo.

### ¿Cómo implementé la cancelación, y qué pasa con los recursos a medio usar?

Cada tarea corre en su **propia `asyncio.Task`** (hija del worker), guardada en
`reg._task`:

```python
reg._task = asyncio.create_task(asyncio.wait_for(ejecutor(params), reg.timeout))
resultado = await reg._task
```

- **Cancelar una tarea en cola:** `asyncio.Queue` no permite sacar un elemento
  del medio. La marco `cancelada`, y el worker que la saque la descarta sin
  ejecutarla.
- **Cancelar una tarea en ejecución:** `DELETE` llama a `reg._task.cancel()`.
  Eso inyecta un `CancelledError` en el próximo `await` de la tarea, que sube
  por la pila ejecutando cada `finally` y cada `__aexit__`. El `DELETE` espera
  (con un `asyncio.Event`) a que el worker termine de limpiar, así la respuesta
  ya dice `cancelada`.
- **Distinguir quién canceló:** si el cancelado es el worker (apagado), entonces
  `asyncio.current_task().cancelling() > 0`, así que marca la tarea y **re-lanza**
  para terminar. Si solo se canceló la hija (`DELETE`), el worker absorbe la
  excepción y sigue vivo.
- **Timeout:** `wait_for` cancela la corrutina interna con el mismo mecanismo
  (la misma limpieza) y lanza `TimeoutError`, y la tarea pasa a `fallida` con
  el motivo.

**Recursos a medio usar, tarea por tarea:**

| Tarea | Qué pasa al cancelar |
|-------|----------------------|
| `descargar` | `gather` cancela todas las descargas hijas y el `async with httpx.AsyncClient` cierra los sockets. Lo verifiqué con `ss`: **3 sockets ESTABLISHED antes del DELETE, 0 después**. |
| `esperar` | `asyncio.sleep` se cancela al instante, no hay nada que liberar. |
| `hashear` | El caso difícil: **un proceso que ya está calculando no se puede interrumpir desde asyncio**. Cancelar el future solo deja de esperarlo; el proceso seguiría quemando CPU. **Solución:** el hash se manda al pool **en bloques de 250.000 iteraciones (~0,15 s)**. Entre bloque y bloque hay un `await`, que es donde entra la cancelación. Como mucho se desperdicia un bloque. El resultado es idéntico porque SHA-256 iterado es una cadena (la salida de un bloque es la entrada del siguiente). Verificado: con el proceso hijo en estado `R` (corriendo), 5 de 5 muestras antes del DELETE y 0 de 5 después. |

### ¿Qué pasa si un worker lanza una excepción no capturada?

No puede pasar: `_ejecutar` envuelve la tarea en `try/except Exception`.
Cualquier error de la tarea la deja en `fallida` con
`"TipoDeError: mensaje"`, y el worker vuelve a `cola.get()`. Lo probé: una
tarea que lanza `NotImplementedError` quedó `fallida` y ese mismo worker
ejecutó la siguiente tarea sin problema.

Si no lo capturara, la excepción terminaría la `asyncio.Task` del worker **en
silencio**: asyncio solo loguea "Task exception was never retrieved" cuando la
task se recolecta. El pool perdería un consumidor por cada error, y después de
N errores la cola dejaría de procesarse sin que nadie se entere.

Detalles adicionales:
- `cola.task_done()` va en un `finally`, así la cuenta de la cola nunca se
  desbalancea.
- `CancelledError` hereda de `BaseException`, no de `Exception`: el
  `except Exception` **no** se traga las cancelaciones.
- Un error en **una** URL de `descargar` no hace fallar la tarea: queda
  registrado en la fila de esa URL y las demás siguen.

### Otras decisiones

- **`POST` responde `202 Accepted`:** semánticamente es "recibido, se procesa
  después". `put_nowait` sobre una cola sin límite nunca bloquea, así que el
  POST tarda ~1 ms aunque la tarea dure minutos.
- **httpx y no aiohttp:** ambos son asíncronos. Elegí httpx por su API casi
  idéntica a `requests` (más legible), los timeouts configurables por fase y
  porque su `AsyncClient` es un context manager que cierra las conexiones
  incluso al cancelar. `requests` está prohibido y además sería un bug: es
  bloqueante y congelaría el loop durante cada descarga.
- **Validación con una unión discriminada de Pydantic:** según `tipo`, FastAPI
  valida los parámetros que corresponden. Una tarea mal armada se rechaza con
  422 y nunca llega a un worker.
- **Señales:** uvicorn instala sus propios handlers de `SIGINT`/`SIGTERM` (con
  `loop.add_signal_handler`). Al recibir una, deja de aceptar conexiones,
  termina las requests en vuelo y sale del `yield` del `lifespan`, que es donde
  corre el apagado ordenado. No instalo handlers propios porque pisaría los de
  uvicorn y el servidor HTTP no se enteraría.
- **Apagado ordenado** (`Gestor.apagar`): (1) `aceptando = False`, así un POST
  recibe 503; (2) las tareas en cola pasan a `cancelada` con el motivo "no
  ejecutada"; (3) `asyncio.wait(en_curso, timeout=TIMEOUT_APAGADO)` espera sin
  cancelar; (4) cancela lo que no llegó, detiene los workers y el executor;
  (5) vuelca el estado. El volcado va en un `finally`, así se intenta aunque
  algo falle.
- **Volcado atómico:** escribe a `.json.tmp` y después hace `os.replace` (que
  es atómico), así nunca queda un `estado_*.json` a medio escribir. La
  escritura a disco y el `executor.shutdown(wait=True)` son bloqueantes, por
  eso van en `asyncio.to_thread`.
- **Docker:** el `CMD` está en formato *exec* para que Python sea el **PID 1** y
  reciba el `SIGTERM` de `docker compose down` (con formato shell le llegaría a
  `/bin/sh`, que no lo reenvía). `stop_grace_period: 20s` es mayor que
  `TIMEOUT_APAGADO`, porque si no Docker manda `SIGKILL` antes de que termine
  el volcado. El contenedor corre con el uid del usuario, así los JSON de
  `./datos` no quedan con dueño root.

---

## 4. Mediciones

**Entorno:** Intel Core i7-8750H (**6 núcleos físicos / 12 hilos**, 2,2 GHz
base, hasta 4,1 GHz turbo), Ubuntu 24.04, Docker 29.1, Python 3.12 en el
contenedor, 4 workers y 12 procesos salvo que se indique otra cosa. Los
scripts corren en el host contra la API dentro del contenedor.

### Medición 1: concurrencia I/O (20 descargas)

Misma tarea `descargar`, con `secuencial: true` (`for url in urls: await ...`)
contra el modo normal (`asyncio.gather`). Tres repeticiones de cada una, con
el orden alternado.

| Escenario | Secuencial | Concurrente | Speedup |
|-----------|-----------:|------------:|--------:|
| **Controlado**: 20 × `/demo/lento?ms=500` | **10,15 s** | **0,60 s** | **×16,9** |
| **Real**: 20 sitios de internet | 9,22 s | 1,17 s | ×7,9 |

Detalle por repetición (s):

| | Rep 1 | Rep 2 | Rep 3 |
|-|------:|------:|------:|
| Controlado secuencial | 10,128 | 10,147 | 10,162 |
| Controlado concurrente | 0,601 | 0,596 | 0,602 |
| Real secuencial | 9,775 | 9,220 | 8,657 |
| Real concurrente | 1,218 | 1,134 | 1,166 |

**Interpretación.** En el escenario controlado, el modo secuencial tarda
exactamente la **suma** (20 × 0,5 s = 10 s, más ~7 ms de overhead por
request). El concurrente tarda lo que **la más lenta** (0,5 s), más ~0,1 s de
abrir 20 conexiones TCP. Mientras una descarga espera la red, el loop avanza
las otras 19: el tiempo de espera de I/O se superpone. El speedup no llega a
×20 por ese costo fijo de conexión.

Con sitios reales el speedup baja a ×7,9 porque las latencias son
**desparejas**: el total concurrente queda dominado por el sitio más lento
(~1 s), mientras que la mayoría tarda entre 0,1 y 0,4 s. *Nota:* `gnu.org` y
`um.edu.ar` se quitaron de la lista porque desde mi red daban `ConnectTimeout`
casi siempre. Con `gnu.org` adentro, el modo concurrente tardaba **30 s
exactos** (el timeout del cliente), lo que ilustra lo mismo: con `gather`, el
total es el de la descarga más lenta.

### Medición 2: el event loop bloqueado

Durante un `hashear` de 10.000.000 iteraciones se hacen `GET /tareas` en
serie (cuando termina una request arranca la siguiente, con 20 ms de pausa) y
se registra la latencia de cada una.

| Escenario | Duración del hash | Respuestas durante el hash | p50 | p95 | Máx |
|-----------|------:|------:|------:|------:|------:|
| Referencia (sin carga) | — | — | 6,0 ms | 7,4 ms | 10,1 ms |
| **SIN executor** | 6,72 s | **1** | 6.721 ms | 6.721 ms | **6.721 ms** |
| **CON executor** | 6,64 s | **284** | 1,7 ms | 2,1 ms | **22,9 ms** |

**Interpretación.** Sin executor, `hash_iterado()` es un `for` de 10 millones
de vueltas **sin ningún `await`**: el event loop no recupera el control hasta
que termina. La primera request quedó esperando en el socket los 6,7 s
completos, y durante ese tiempo no se atendió nada más (ni la API, ni los otros
workers, ni los timeouts, que también dependen del loop). Con executor, la API
respondió 284 veces sin enterarse: el peor caso es **293 veces menor**. El
pico de 22,9 ms coincide con el arranque de los procesos del pool (el primer
`submit` hace `fork` de los hijos).

*¿Por qué la referencia (6 ms) es más lenta que "con executor" (1,7 ms)?*
Es por la gestión de energía de la notebook: en reposo la CPU baja a 800 MHz.
Con un núcleo al 100 % calculando el hash, el procesador sube la frecuencia de
todos los núcleos, y el event loop también corre más rápido. No es un efecto
de mi código, pero muestra que hay que medir con cuidado.

### Medición 3: techo del pool

Se mandan **K tareas de golpe** y se mide cuánto tarda el lote (desde el
primer `creada` hasta el último `finalizada`, con las marcas del servidor),
la espera promedio en cola y la ejecución promedio. Dos tipos de tarea y dos
configuraciones.

**`esperar 1s` (I/O simulado)**

| K | 4 workers: total | espera en cola | tareas/s | 64 workers: total | tareas/s |
|--:|------:|------:|------:|------:|------:|
| 1 | 1,00 s | 0,00 s | 1,00 | 1,00 s | 1,00 |
| 4 | 1,02 s | 0,00 s | 3,91 | 1,02 s | 3,90 |
| 8 | 2,01 s | 0,50 s | 3,97 | 1,02 s | 7,81 |
| 16 | 4,01 s | 1,49 s | 3,99 | 1,05 s | 15,19 |
| 32 | 8,02 s | 3,48 s | 3,99 | 1,05 s | 30,52 |
| 64 | 16,03 s | 7,45 s | 3,99 | 1,11 s | **57,52** |

**`hashear 1.000.000` (CPU, ~0,7 s cada una)**

| K | 4 workers: total | espera en cola | ejecución | tareas/s | 64 workers: total | ejecución | tareas/s |
|--:|------:|------:|------:|------:|------:|------:|------:|
| 1 | 0,70 s | 0,00 s | 0,70 s | 1,43 | 0,69 s | 0,69 s | 1,45 |
| 4 | 0,71 s | 0,00 s | 0,70 s | 5,61 | 0,75 s | 0,71 s | 5,31 |
| 8 | 1,43 s | 0,34 s | 0,70 s | 5,59 | 1,23 s | 1,07 s | 6,51 |
| 16 | 2,90 s | 1,06 s | 0,71 s | 5,52 | 2,10 s | 1,90 s | 7,63 |
| 32 | 5,97 s | 2,52 s | 0,74 s | 5,36 | 4,19 s | 3,77 s | 7,63 |
| 64 | 12,28 s | 5,59 s | 0,76 s | 5,21 | 8,72 s | 7,63 s | 7,34 |

**Interpretación: hay dos techos distintos.**

1. **Techo de workers** (con 4 workers, los dos tipos de tarea). Hasta K = 4
   el lote tarda lo mismo que una tarea; desde K = 8 el tiempo se **duplica
   con cada duplicación de K** y el throughput se clava en 4 tareas/s
   (`esperar`). La degradación aparece como **espera en cola**: cada tarea
   tarda lo mismo en ejecutarse (1,00 s), pero espera más para empezar. El
   límite es `N_WORKERS`.

2. **Techo de CPU** (`hashear` con 64 workers). Con workers de sobra no hay
   espera en cola (0,00 s), pero la **ejecución** de cada tarea pasa de 0,7 s
   a 7,6 s: las 64 tareas se reparten los núcleos. El throughput se planta en
   **~7,6 tareas/s**, que equivale a ~5,3 núcleos trabajando al 100 %
   (7,6 × 0,7 s). Aunque el pool tiene 12 procesos, la máquina tiene **6
   núcleos físicos**: los 12 "CPUs" son hilos de Hyper-Threading que comparten
   las unidades de cálculo, y para un bucle de SHA-256 el segundo hilo aporta
   poco. Además, con todos los núcleos cargados, el turbo baja de 4,1 GHz.

   Para `esperar`, en cambio, 64 workers dan **57 tareas/s** casi sin
   degradación: un worker esperando un `asyncio.sleep` no consume CPU, así que
   en I/O se pueden tener cientos de workers por casi nada de costo.

**Conclusión:** para I/O, el límite lo pone la cantidad de workers (y más
adelante la red o los file descriptors), y conviene subirlos. Para CPU, el
límite es la cantidad de **núcleos físicos**, y más workers no ayudan: solo
mueven la espera de la cola a la CPU.

### Extra: "si te mando 10.000 tareas de golpe, ¿qué se rompe primero?"

10.000 `esperar 60s` con 100 POST en paralelo, 4 workers:

| Qué | Resultado |
|-----|-----------|
| POSTs | 10.000 en 30,8 s → **325 POST/s**. p50 191 ms, p99 1,9 s (100 conexiones atendidas por un solo hilo) |
| Memoria del contenedor | de 44 MB a **60 MB**: ~1,5 KB por tarea. **No es lo primero que se rompe** |
| `GET /estadisticas` | 4,3 ms |
| `GET /tareas?estado=ejecutando` | 2,9 ms |
| `GET /tareas` (las 10.000) | **140 ms y 2,4 MB** |
| Vaciar la cola | 10.000 × 60 s / 4 workers ≈ **42 horas** |
| Apagado con 10.000 tareas | 10,9 s, con un volcado de 4,1 MB |

**Lo primero que se rompe es `GET /tareas`:** serializar todas las tareas es
O(n) y ocurre **dentro del event loop**, así que cada GET bloquea al resto
durante 140 ms, y crece linealmente con las tareas acumuladas (nunca se
borran). Lo segundo es la **falta de backpressure**: la cola no tiene límite,
así que la API acepta trabajo que va a tardar 42 horas en empezar sin avisarle
a nadie. La solución sería `asyncio.Queue(maxsize=...)` y responder `429`/`503`
cuando está llena, más paginación en `GET /tareas` y una expiración de las
tareas terminadas.

---

## 5. Conceptos del curso aplicados

Ordenado por clase. Los números siguen el repositorio de la cátedra
(`compu2_um_2026`).

| Clase | Tema | Dónde aparece en el TP |
|------:|------|------------------------|
| 01–02 | Docker | `Dockerfile`, `docker-compose.yml`, PID 1, `stop_grace_period` |
| 03–04 | Procesos, `fork`/`exec`/`wait` | Los hijos del `ProcessPoolExecutor` |
| 05 | Pipes | Comunicación con los procesos del pool |
| 06 | Señales | Apagado ordenado con `SIGTERM`/`SIGINT` |
| 08–09 | Multiprocessing | `ProcessPoolExecutor`, pickle, `fork` vs `spawn` |
| 10 | Threading | GIL: por qué procesos y no threads; `asyncio.to_thread` |
| 11 | Sincronización | Por qué el pool **no** necesita `Lock` |
| 12–13 | Redes, sockets TCP | Las conexiones de `descargar`, verificadas con `ss` |
| 14 | Servidores concurrentes | fork vs threads vs async |
| 17 | I/O multiplexing | El event loop por debajo es `epoll` |
| 18 | De `yield` a asyncio | Corrutinas, `await`, cancelación cooperativa |
| 19 | HTTP y FastAPI | La API completa |
| 20 | asyncio en red | `httpx.AsyncClient` + `gather` |
| 21 | asyncio avanzado | `asyncio.Queue`, cancelación, timeouts, `run_in_executor` |
| 22 | `concurrent.futures` | `ProcessPoolExecutor` y futures |

- **Docker (clases 01–02).** Todo levanta con `docker compose up --build`. El
  `CMD` en formato exec hace que Python sea el **PID 1** del contenedor y
  reciba directamente el `SIGTERM` de `docker compose down`.
  `stop_grace_period` le da tiempo al apagado ordenado antes del `SIGKILL`, y
  el volumen `./datos` hace que los volcados sobrevivan al contenedor.

- **Procesos y `fork` (clases 03–04).** En Linux el `ProcessPoolExecutor` crea
  sus procesos con `fork`. Dentro del contenedor se ven como hijos del PID 1
  (PID 17, 18, …), todos con la misma línea de comando porque son copias del
  padre. Cada uno tiene su propio espacio de memoria, que es lo que permite
  paralelismo real.

- **Pipes (clase 05).** El `ProcessPoolExecutor` se comunica con sus procesos
  por pipes: manda la función y los argumentos serializados con pickle y
  recibe el resultado por el mismo camino. Por eso `hash_bloque` tiene que
  estar a nivel de módulo (para que pickle la encuentre por nombre) y por eso
  conviene mandar poco dato: acá son 32 bytes por bloque.

- **Señales (clase 06).** `SIGTERM` y `SIGINT` disparan el apagado ordenado.
  A diferencia de `signal.signal()`, cuyo handler interrumpe al hilo principal
  en cualquier punto, `loop.add_signal_handler()` (el que usa uvicorn)
  convierte la señal en un callback del event loop, así el apagado corre como
  código asíncrono normal, con `await`. `SIGKILL` no se puede atrapar: si
  llega, no hay volcado.

- **Multiprocessing (clases 08–09).** `fork` en Linux vs `spawn` en
  macOS/Windows: con `spawn` cada hijo re-importa el módulo principal, por eso
  el arranque está protegido con `if __name__ == "__main__"`. El pool se crea
  en el `lifespan` y no al importar el módulo.

- **Threading y el GIL (clase 10).** Por el GIL, un thread que calcula SHA-256
  sobre 32 bytes compite con el event loop por el intérprete (`hashlib` solo
  suelta el GIL con datos de más de 2 KiB). Por eso `hashear` usa procesos y
  no `asyncio.to_thread`. Los threads sí se usan donde corresponden, para I/O
  bloqueante: la escritura del JSON y el `executor.shutdown(wait=True)` van en
  `asyncio.to_thread`.

- **Sincronización (clase 11): el pool no necesita `Lock`.** El pool de
  workers consume de una `asyncio.Queue` compartida (`Gestor._worker`). Es el
  mismo patrón productor-consumidor, pero con corrutinas en vez de threads:
  acá no hace falta lock porque el event loop no interrumpe entre `await`s.
  Las actualizaciones de estado (`reg.estado = ...`, `self._ocupados += 1`)
  no tienen ningún `await` en el medio, así que son atómicas respecto de las
  demás corrutinas.

- **Redes y sockets TCP (clases 12–13).** Cada URL de `descargar` es una
  conexión TCP que httpx abre y cierra. La limpieza al cancelar se verificó
  mirando las conexiones del proceso con `ss -tnp`: 3 sockets `ESTABLISHED`
  antes del `DELETE`, 0 después.

- **Servidores concurrentes (clase 14): fork vs threads vs async.** En esa
  clase hice `sever_fork.py` (un proceso por cliente) y `server_threads.py`
  (un thread por cliente). En `server_threads.py` el contador
  `clientes_activos` **necesita un `threading.Lock`**, porque varios threads
  lo modifican a la vez y "sin el lock hay race condition". Este TP es el
  tercer modelo: un solo hilo con un event loop para todas las conexiones. El
  contador equivalente (`self._ocupados`) no lleva lock, porque las
  corrutinas solo ceden el control en un `await`. Además cada tarea cuesta
  unos KB en vez de un proceso o un stack de thread: 10.000 tareas sumaron
  16 MB.

- **I/O multiplexing (clase 17).** El event loop de asyncio, que usan uvicorn
  y httpx, es por debajo **`epoll`** en Linux: la misma idea de
  `select`/`poll`, donde un solo hilo le pide al kernel "avisame qué
  descriptores están listos" y atiende solo a esos. Por eso `descargar` puede
  tener 20 sockets esperando respuesta en un solo hilo, y la medición 1 da
  ×16,9.

- **De `yield` a asyncio (clase 18).** Una corrutina es una función que se
  puede suspender en cada `await` y retomar después. `time.sleep()` dentro de
  una corrutina es un bug porque duerme el hilo entero; `asyncio.sleep()`
  suspende solo esa corrutina y le devuelve el control al loop. La
  cancelación también es cooperativa: solo puede ocurrir en un `await`, y por
  eso el hash se manda al pool en bloques (sección 3).

- **HTTP y FastAPI (clase 19).** Endpoints `async def`, validación con
  Pydantic (una unión discriminada por `tipo`), códigos HTTP con significado
  (202, 404, 409, 422, 503) y `lifespan` para atar los recursos (workers y
  executor) al ciclo de vida del servidor.

- **asyncio en red (clase 20).** `httpx.AsyncClient` con `asyncio.gather`. La
  diferencia entre `gather` y un `for` con `await` es exactamente la de la
  medición 1: el modo concurrente tarda lo que la descarga más lenta, y el
  secuencial tarda la suma.

- **asyncio avanzado (clase 21).** `asyncio.Queue`, `Task.cancel()`,
  `asyncio.wait_for` para los timeouts, `asyncio.wait` para el apagado,
  `run_in_executor`, `asyncio.to_thread` y `Task.cancelling()` (Python 3.11+)
  para distinguir si canceló el usuario o el apagado.

- **`concurrent.futures` (clase 22).** `ProcessPoolExecutor` pertenece a
  `concurrent.futures`. `run_in_executor` envuelve el `Future` de
  `concurrent.futures` en un future de asyncio, que se puede `await`-ear. Al
  apagar, `shutdown(cancel_futures=True)` descarta los trabajos que todavía
  no empezaron.

---

## 6. Limitaciones conocidas

- **Sin backpressure:** la cola no tiene límite. Con miles de tareas, la API
  acepta trabajo que va a tardar horas en empezar (ver el extra de la
  medición 3).
- **`GET /tareas` sin paginación:** con muchas tareas acumuladas, la
  serialización bloquea el loop (140 ms con 10.000 tareas). Las tareas
  terminadas nunca se borran de memoria.
- **El estado vive en un solo proceso:** no escala horizontalmente. Dos
  réplicas tendrían dos colas distintas.
- **El volcado no se recarga:** al arrancar solo se reporta en el log. Las
  tareas pendientes al apagar se pierden; no se re-encolan.
- **Si el proceso muere con `SIGKILL`** (o se corta la luz) no hay volcado: el
  estado solo se persiste en un apagado ordenado.
- **Cancelar un hash desperdicia hasta un bloque (~0,15 s)** de CPU en el
  proceso hijo. Si una tarea de CPU muy larga no se partiera en bloques, no se
  podría cancelar a mitad.
- **Las tareas CPU-bound compiten por los núcleos sin prioridad:** muchos
  hashes a la vez hacen que todos tarden más (medición 3). No hay un límite
  separado de tareas CPU concurrentes.
- **`sin_executor` y `secuencial` existen solo para las mediciones.** En
  producción no deberían estar expuestos, porque cualquiera podría congelar la
  API.
- **Sin autenticación ni rate limiting:** cualquiera puede crear o cancelar
  cualquier tarea.
- **Sin límite de tamaño de descarga:** `descargar` carga la respuesta entera
  en memoria.

---

## 7. Lo que aprendí


Lo que más me sorprendió fue la medición 2. Sabía de la teoría que una
corrutina sin `await` bloquea el event loop, pero ver que la API respondió
**una sola vez en 6,7 segundos** lo hizo concreto. Un solo `for` sin `await`
alcanza para congelar todo el servidor, y no hay ningún error ni warning que
lo avise: simplemente deja de responder. Me cambió la forma de leer código
asíncrono: ahora busco qué puede ejecutarse mucho tiempo entre dos `await`.

También entendí que la cancelación en asyncio es cooperativa, y lo que eso
implica en la práctica. Cancelar una descarga fue fácil, porque httpx y los
`async with` limpian solos. Pero un proceso que está calculando no se entera
de nada, y tuve que rediseñar el hash en bloques para que hubiera puntos donde
cortar. Lo mismo con el apagado: hay que pensar el orden (dejar de aceptar,
descartar la cola, esperar con límite, cancelar, volcar) y además que la señal
le llegue al proceso correcto dentro de Docker.

Por último, aprendí a medir en vez de suponer. Esperaba que con 12 procesos
los hashes escalaran a 12×, y la medición 3 mostró ~5,3×. Para entender por
qué tuve que mirar la CPU con `lscpu`: tiene 6 núcleos físicos con
Hyper-Threading. Y la referencia de la medición 2 salió más lenta que con
carga, por el ahorro de energía del procesador. Sin medir, las dos cosas las
hubiera respondido mal.

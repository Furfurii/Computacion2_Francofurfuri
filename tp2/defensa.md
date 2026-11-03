# Guía de defensa — TP2

Material de estudio para la corrección oral. La consigna avisa: **"Si no podés
explicar tu propio código, no aprueba."** Acá está todo lo que te pueden
preguntar, con la respuesta corta, la explicación y **dónde mostrarlo en el
código**.

---

## 0. El proyecto en 30 segundos

> "Es una API en FastAPI que recibe tareas por HTTP y las mete en una
> `asyncio.Queue`. Un pool de N corrutinas worker las consume y las ejecuta.
> Todo corre en **un solo hilo** con el event loop. Las tareas de I/O
> (`descargar`, `esperar`) se hacen concurrentes con `await`; la de CPU
> (`hashear`) se manda a un **ProcessPoolExecutor** para no congelar el loop.
> Cada tarea se puede cancelar aunque esté corriendo, tiene timeout, y al
> recibir SIGTERM el servidor se apaga ordenado y vuelca el estado a JSON."

**Los 3 números para recordar:**
- Medición 1: 20 descargas → **10,15 s secuencial vs 0,60 s concurrente (×17)**
- Medición 2: durante un hash de 6,7 s → **sin executor la API respondió 1 vez
  (6,7 s); con executor respondió 284 veces (p50 1,7 ms)**
- Medición 3: con 4 workers el techo es **4 tareas/s** (lo ponen los workers);
  con 64 workers el hash se planta en **~7,6 tareas/s** (lo ponen los **6
  núcleos físicos**)

---

## 1. Mapa del código

| Archivo | Qué tiene | Lo más importante |
|---------|-----------|-------------------|
| [app/main.py](app/main.py) | API FastAPI | `lifespan` (L35): arranca los workers, y después del `yield` hace el apagado ordenado. `crear_tarea` (L73) devuelve 202. `cancelar_tarea` (L94) devuelve 409 si ya terminó |
| [app/gestor.py](app/gestor.py) | El corazón del TP | `_worker` (L293), `_ejecutar` (L310), `cancelar` (L236), `apagar` (L138), `volcar_estado` (L202) |
| [app/tareas.py](app/tareas.py) | Las 3 tareas | `descargar` (L69) con `gather`, `hashear` (L121) con `run_in_executor` en bloques, `esperar` (L33) |
| [app/modelos.py](app/modelos.py) | Pydantic | Unión discriminada por `tipo`: valida los params según el tipo de tarea |
| [Dockerfile](Dockerfile) | Imagen | `CMD` en formato exec → Python es el PID 1 |
| [docker-compose.yml](docker-compose.yml) | Servicio | `stop_grace_period: 20s`, volumen `./datos`, variables de entorno |

### El recorrido de una tarea (para contarlo con el código abierto)

1. **`POST /tareas`** → Pydantic valida → `gestor.encolar()` crea un
   `Registro` con estado `pendiente`, lo guarda en el dict `tareas` y hace
   `cola.put_nowait(id)` → responde **202** en ~1 ms.
2. **`_worker`** está bloqueado (suspendido, no bloqueando el hilo) en
   `await self.cola.get()`. Cuando llega el id, se despierta.
3. Si la tarea ya no está `pendiente` (la cancelaron mientras esperaba) → `continue`.
4. **`_ejecutar`** pone `ejecutando` y crea la tarea hija:
   `reg._task = create_task(wait_for(EJECUTORES[tipo](params), timeout))`.
5. `await reg._task`, y según cómo termine:
   - OK → `completada`
   - `TimeoutError` → `fallida` ("Timeout: superó Xs")
   - `CancelledError` → `cancelada` (por el usuario o por apagado)
   - otra `Exception` → `fallida` ("TipoError: mensaje")
6. `finally: cola.task_done()` y el worker vuelve al paso 2.

---

## 2. Las 6 preguntas que avisa la consigna

### 2.1 "Mostrame dónde tu código podría bloquear el event loop."

**Respuesta honesta (es mejor reconocerlo que negarlo):**

| Dónde | Por qué bloquea | ¿Es grave? |
|-------|-----------------|------------|
| `hashear` con `sin_executor: true` ([tareas.py:131](app/tareas.py#L131)) | Llama `hash_iterado()` directo: un `for` sin `await` | **A propósito**, solo para la medición 2 |
| `GET /tareas` sin filtro ([main.py:84](app/main.py#L84)) | Serializa TODAS las tareas a JSON dentro del loop: O(n) | Con 10.000 tareas tarda 140 ms. Es lo primero que se rompe bajo carga |
| `_volcado()` ([gestor.py:191](app/gestor.py#L191)) | Arma el dict de todas las tareas en el loop | Solo al apagar. La *escritura* sí va a un thread |
| `estadisticas()` ([gestor.py:260](app/gestor.py#L260)) | Recorre todas las tareas | O(n), pero rápido (4 ms con 10.000) |
| Validación Pydantic / logging | Código síncrono | Microsegundos; despreciable |

Lo que **NO** bloquea y por qué:
- `hashear` normal → corre en otro proceso (`run_in_executor`).
- La escritura del JSON y `executor.shutdown(wait=True)` → `asyncio.to_thread`.
- `descargar` → httpx async, nunca `requests`.
- `esperar` → `asyncio.sleep`, nunca `time.sleep`.

### 2.2 "¿Qué pasa si cancelo una tarea justo cuando está a mitad de una descarga?"

**Corta:** se cancelan todas las descargas en curso y se cierran los sockets.
No queda nada colgado.

**Larga (con el código):**
1. `DELETE` → `gestor.cancelar()` ([gestor.py:236](app/gestor.py#L236)) →
   `reg._task.cancel()`.
2. `cancel()` no mata nada al instante: **programa** un `CancelledError` que
   se lanza en el `await` donde la tarea está parada (dentro de
   `cliente.get(url)`).
3. La excepción sube: `gather` ([tareas.py:87](app/tareas.py#L87)) la recibe
   y **cancela a todas sus hijas** (las otras descargas).
4. Sale del `async with httpx.AsyncClient(...)`, cuyo `__aexit__` **cierra
   todas las conexiones**.
5. Llega al worker en `_ejecutar` → `except CancelledError` → como el worker
   NO está siendo cancelado (`cancelling() == 0`), marca la tarea `cancelada`
   y **sigue vivo**.
6. `_finalizar` hace `reg.terminada.set()` → el `DELETE`, que estaba
   esperando ese `Event`, responde con estado `cancelada`.

**Prueba:** medí con `ss -tnp` → **3 sockets ESTABLISHED antes del DELETE, 0
después**.

### 2.3 "¿Por qué tu pool de workers no necesita un `Lock`?"

**Corta:** porque todo corre en un solo hilo, y una corrutina solo puede ser
interrumpida en un `await`.

**Larga:** con threads, el scheduler del SO puede sacarte la CPU **entre
cualquier par de instrucciones**: `self._ocupados += 1` son varias
instrucciones de bytecode (leer, sumar, escribir), y otro thread podría
meterse en el medio. Con asyncio el cambio de contexto es **cooperativo**:
solo pasa cuando la corrutina hace `await`. Mirá [gestor.py:296-308](app/gestor.py#L296):
entre `await self.cola.get()` y `await self._ejecutar(...)` no hay ningún
`await`, así que verificar el estado e incrementar `_ocupados` es atómico
respecto de las otras corrutinas.

**Usá tu propio código de la clase 14:** en
[server_threads.py](../clase_servidores_concurentes/server_threads.py) el
contador `clientes_activos` tiene un `threading.Lock` porque varios threads
lo modifican a la vez ("sin el lock hay race condition"). En el TP2,
`self._ocupados` hace lo mismo **sin lock**, porque las corrutinas no se
interrumpen entre `await`s.

**Ojo, trampa:** si en el medio de una actualización de estado hubiera un
`await`, ahí sí podría haber una condición de carrera (otra corrutina vería
el estado a medias). Por eso las transiciones de estado (`_finalizar`) son
síncronas.

### 2.4 "Si te mando 10.000 tareas de golpe, ¿qué se rompe primero?"

**Lo medí** ([mediciones/medir_carga.py](mediciones/medir_carga.py)):

- **Memoria: NO.** Pasa de 44 MB a 60 MB (~1,5 KB por tarea).
- **Los POST aguantan:** 325 POST/s, aunque el p99 sube a 1,9 s porque un
  solo hilo atiende 100 conexiones.
- **Lo primero que se rompe es `GET /tareas`:** 140 ms y 2,4 MB por request,
  y mientras serializa **bloquea el loop**. Crece lineal porque las tareas
  terminadas nunca se borran.
- **Lo segundo: no hay backpressure.** La cola es infinita, así que la API
  acepta todo aunque con 4 workers y tareas de 60 s tardaría **42 horas** en
  vaciarse.

**Cómo lo arreglaría:** `asyncio.Queue(maxsize=1000)` + responder `429 Too
Many Requests` cuando está llena; paginación en `GET /tareas`; borrar las
tareas terminadas después de un tiempo.

### 2.5 "¿Qué diferencia hay entre `asyncio.gather` y un `for` con `await` adentro?"

```python
# for + await: SECUENCIAL. Cada await espera que termine antes de seguir.
for url in urls:
    r = await cliente.get(url)        # total = SUMA de los tiempos

# gather: CONCURRENTE. Lanza todas y espera a que terminen todas.
await asyncio.gather(*(cliente.get(u) for u in urls))   # total = la MÁS LENTA
```

`gather` envuelve cada corrutina en una `Task` (las agenda en el loop
**ya**), y después espera a todas. Mientras una espera la red, el loop avanza
las otras.

**Mi medición 1 es exactamente esto** (el flag `secuencial` cambia uno por
el otro, [tareas.py:80-89](app/tareas.py#L80)): 20 × 500 ms → **10,15 s vs
0,60 s**.

Extras: `gather` devuelve los resultados **en el mismo orden** que los
argumentos; si una falla, por defecto la excepción se propaga (por eso
`_descargar_una` captura los errores de red adentro y devuelve una fila con
`error`); y si cancelás el `gather`, cancela a todas sus hijas.

### 2.6 "¿Por qué `time.sleep()` en una corrutina es un bug y `asyncio.sleep()` no?"

- `time.sleep(5)` es una **llamada al sistema que duerme el hilo**. Como hay
  un solo hilo, durante 5 s no corre nada: ni la API, ni los otros workers,
  ni los timeouts. Es exactamente lo que pasa en la medición 2 sin executor
  (el `for` del hash se comporta igual que un `time.sleep`).
- `await asyncio.sleep(5)` **suspende solo esta corrutina**: le dice al loop
  "despertame en 5 s" (registra un timer) y le devuelve el control. El loop
  sigue atendiendo todo lo demás.

**Demostración:** con 4 workers, 4 `esperar 2s` tardan 2 s en total, no 8.
Con `time.sleep` serían 8 s y la API no respondería mientras tanto.

---

## 3. Otras preguntas probables

**¿Por qué `asyncio.Queue` y no `queue.Queue`?**
`queue.Queue.get()` bloquea el **hilo** (usa un lock del SO). Con un solo
hilo, eso congela el loop y nadie puede hacer `put` → deadlock.
`asyncio.Queue.get()` es una corrutina que suspende solo al worker.

**¿Por qué `ProcessPoolExecutor` y no `ThreadPoolExecutor` / `asyncio.to_thread`?**
Por el **GIL**: en CPython solo un thread ejecuta bytecode Python a la vez.
`hashlib` libera el GIL solo con datos > 2 KiB, y acá hasheo 32 bytes. Con
threads el hash competiría con el event loop por el GIL: no habría
paralelismo y la API andaría lenta. Cada **proceso** tiene su propio
intérprete y su propio GIL → paralelismo real.

**¿Y cuándo sí usarías `to_thread`?** Para I/O bloqueante que libera el GIL:
escribir un archivo, una librería sin versión async. Lo uso para escribir el
JSON y para el `executor.shutdown(wait=True)`.

**¿Por qué el hash se manda en bloques?** ([tareas.py:141-158](app/tareas.py#L141))
Porque **un proceso que ya está calculando no se puede cancelar desde
asyncio**. `future.cancel()` solo deja de esperarlo: el proceso sigue
quemando CPU. Partiéndolo en bloques de 250.000 iteraciones (~0,15 s), entre
bloque y bloque hay un `await` donde entra la cancelación. El resultado es
igual porque SHA-256 iterado es una cadena: la salida de un bloque es la
entrada del siguiente. **Verificado:** el proceso hijo deja de estar en
estado R (corriendo) apenas se cancela, y el hash da igual que sin bloques
(`4738085c...`).

**¿Cómo distinguís un DELETE de un apagado si ambos lanzan `CancelledError`?**
([gestor.py:330-342](app/gestor.py#L330)) Con
`asyncio.current_task().cancelling()` (Python 3.11+). Si el **worker** está
siendo cancelado (apagado), devuelve > 0 → marco la tarea y **re-lanzo** para
que el worker termine. Si solo se canceló la **tarea hija** (DELETE), el
worker no está siendo cancelado → absorbo la excepción y el worker sigue.

**¿Por qué cada tarea corre en su propia Task y no directo en el worker?**
Para poder cancelar **la tarea** sin cancelar **al worker**. Si hiciera
`await ejecutor(params)` directo, para cancelarlo tendría que cancelar al
worker entero.

**¿Por qué `wait_for` para el timeout y `asyncio.wait` en el apagado?**
- `wait_for(coro, t)`: si vence, **cancela** la corrutina y lanza
  `TimeoutError`. Es lo que quiero para el timeout de una tarea.
- `asyncio.wait(tasks, timeout=t)`: si vence, **no cancela nada**, solo
  devuelve `(hechas, pendientes)`. En el apagado quiero decidir yo qué hacer
  con las pendientes.

**¿Por qué `CancelledError` no se atrapa en el `except Exception`?**
Porque desde Python 3.8 hereda de `BaseException`, no de `Exception`.
Justamente para que un `except Exception` genérico no se trague las
cancelaciones.

**¿Por qué `task_done()` está en un `finally`?** ([gestor.py:308](app/gestor.py#L308))
Para que se llame siempre, aunque la tarea falle o la cancelen. Si se
salteara, el contador interno de la cola quedaría desbalanceado y
`cola.join()` no volvería nunca.

**¿Para qué sirve el `asyncio.Event` del Registro?**
`cancel()` solo **pide** la cancelación; la limpieza ocurre después, cuando
la tarea llega a su próximo `await`. El `DELETE` espera ese `Event` (que se
setea en `_finalizar`) para responder con el estado ya actualizado a
`cancelada`, y no con `ejecutando`.

**¿Por qué el POST devuelve 202 y no 201 o 200?**
`202 Accepted` = "lo recibí, lo voy a procesar después". Es la semántica
exacta de una cola de trabajos. `201 Created` daría a entender que el
recurso ya está listo.

**¿Por qué los workers se crean en el `lifespan` y no al importar?**
`asyncio.create_task` necesita un event loop **corriendo**, y el loop lo crea
uvicorn recién al arrancar. Además así quedan atados al ciclo de vida: se
crean al arrancar y se destruyen al apagar.

**¿Dónde manejás SIGTERM/SIGINT?**
No instalo handlers propios: **uvicorn** ya instala los suyos con
`loop.add_signal_handler`. Al recibir la señal deja de aceptar conexiones,
termina las requests en vuelo y sale del `yield` del `lifespan`
([main.py:41](app/main.py#L41)), donde corre mi apagado. Si instalara los
míos, pisaría los de uvicorn y el servidor HTTP no se enteraría.

**¿Qué diferencia hay entre `signal.signal` y `loop.add_signal_handler`?**
`signal.signal` ejecuta el handler **interrumpiendo** el hilo principal en
cualquier punto, y ahí no podés hacer `await`. `add_signal_handler` hace que
el loop ejecute el callback como cualquier otro callback, en un punto
seguro. `add_signal_handler` es solo Unix; en Windows habría que usar
`signal.signal` con un flag.

**Contame el apagado ordenado paso a paso.** ([gestor.py:138](app/gestor.py#L138))
1. `aceptando = False` → un POST que llegue recibe **503**.
2. Las tareas **en cola** pasan a `cancelada` ("No ejecutada: apagado").
3. `asyncio.wait(en_curso, timeout=TIMEOUT_APAGADO)` → espera **sin cancelar**.
4. `detener()`: cancela los workers (y con ellos las tareas que no
   terminaron), espera con `gather`, cierra el executor (en `to_thread`).
5. `volcar_estado()` → `datos/estado_<timestamp>.json`, en un `finally`.

**Medido:** con 2 tareas cortas, 2 largas y 3 en cola y `TIMEOUT_APAGADO=3`
→ salió en **3,25 s**: 2 completadas, 2 canceladas por apagado, 3 no
ejecutadas.

**¿Por qué escribís a `.tmp` y después renombrás?**
`os.replace` es **atómico** en el mismo filesystem: o está el archivo viejo
o el nuevo completo, nunca uno a medias. Si el proceso muere escribiendo,
queda un `.tmp` roto pero ningún `estado_*.json` corrupto.

**¿Por qué el `CMD` del Dockerfile está en formato `["python", ...]`?**
Formato **exec**: Python es el **PID 1** y recibe el SIGTERM de
`docker compose down`. Con el formato shell (`CMD python -m app.main`) el PID
1 sería `/bin/sh`, que no reenvía las señales: Python nunca se enteraría y
Docker lo mataría con SIGKILL tras el grace period, **sin volcado**.

**¿Para qué `stop_grace_period: 20s`?**
Docker manda SIGTERM, espera 10 s por defecto y después manda **SIGKILL**
(que no se puede atrapar). Mi apagado espera hasta 10 s
(`TIMEOUT_APAGADO`) más el volcado, así que necesita más margen.

**¿Por qué httpx y no aiohttp?**
Los dos son async. httpx tiene una API casi igual a `requests` (más
legible), soporta HTTP/2, timeouts finos por fase, y el `AsyncClient` como
`async with` garantiza que se cierren las conexiones aunque se cancele.

**¿Fork o spawn? ¿Por qué el `if __name__ == "__main__"`?**
En Linux, `ProcessPoolExecutor` usa **fork** (copia el proceso). En
macOS/Windows usa **spawn**, que arranca un intérprete nuevo y **re-importa
el módulo principal**. Sin la guarda, cada hijo volvería a ejecutar
`uvicorn.run(...)` → bucle infinito de procesos. En Linux no pasa, así que
es un bug que solo aparece en otra plataforma.

**¿Por qué con 12 procesos el hash no escala 12×?**
Porque el i7-8750H tiene **6 núcleos físicos**; los 12 "CPUs" son
Hyper-Threading (2 hilos por núcleo, que comparten las unidades de cálculo).
Para un bucle de SHA-256 el segundo hilo aporta poco. Encima, con todos los
núcleos cargados el turbo baja de 4,1 GHz. Medí ~7,6 tareas/s × 0,7 s ≈
**5,3 núcleos efectivos**.

**¿Cuántos workers pondrías?**
Depende del tipo de tarea. Para **I/O**, muchos: un worker esperando no
consume CPU (con 64 workers, 64 `esperar` tardan 1,1 s). Para **CPU**, más
workers que núcleos no suma nada: solo pasa la espera de la cola a la CPU
(medición 3). Lo ideal sería un límite separado para las tareas CPU (por
ejemplo, un `asyncio.Semaphore(n_núcleos)`).

**¿Qué pasa si un worker lanza una excepción no capturada?**
En mi código no puede: `except Exception` en `_ejecutar`
([gestor.py:343](app/gestor.py#L343)) la convierte en `fallida` y el worker
sigue. Si no la capturara, la Task del worker moriría **en silencio** (solo
un "Task exception was never retrieved" en el log al recolectarla) y el pool
perdería un consumidor por cada error. **Lo probé:** una tarea con
`NotImplementedError` quedó `fallida` y el mismo worker ejecutó la
siguiente.

**¿Qué es exactamente un `await`?**
Es el punto donde una corrutina **puede ceder el control** al event loop. Si
lo que se espera no está listo (un socket sin datos, un timer que no venció,
un future de otro proceso), la corrutina se suspende y el loop corre otra
cosa. Cuando está listo, el loop la retoma en ese mismo punto.

**¿Qué es el event loop por debajo?**
Un bucle que en Linux usa **epoll** (de la familia de `select`/`poll`, que se
vio en la **clase 17, I/O multiplexing**): le
pide al kernel "avisame cuál de estos sockets está listo", ejecuta los
callbacks de los que están listos y los timers vencidos, y repite. Por eso
un solo hilo atiende miles de conexiones.

---

## 4. Demo para la corrección (5 minutos)

Preparar antes: `cd tp2 && docker compose up --build -d` y en otra terminal
`docker compose logs -f`.

```bash
crear() { ID=$(curl -s -X POST localhost:8000/tareas -H 'Content-Type: application/json' -d "$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo "ID=$ID"; }

# 1) Concurrencia I/O: 5 descargas de 0.5s → ~0.5s en total (no 2.5s)
crear '{"tipo":"descargar","params":{"urls":["http://localhost:8000/demo/lento?ms=500","http://localhost:8000/demo/lento?ms=500","http://localhost:8000/demo/lento?ms=500","http://localhost:8000/demo/lento?ms=500","http://localhost:8000/demo/lento?ms=500"]}}'
sleep 1; curl -s localhost:8000/tareas/$ID | python3 -c 'import sys,json;print(json.load(sys.stdin)["resultado"]["total_segundos"])'

# 2) Event loop: con vs sin executor
crear '{"tipo":"hashear","params":{"texto":"x","iteraciones":10000000}}'
curl -s -o /dev/null -w "CON executor: %{time_total}s\n" localhost:8000/tareas
sleep 7
crear '{"tipo":"hashear","params":{"texto":"x","iteraciones":10000000,"sin_executor":true}}'
curl -s -o /dev/null -w "SIN executor: %{time_total}s\n" localhost:8000/tareas

# 3) Cancelar en ejecución
crear '{"tipo":"esperar","params":{"segundos":60}}'; sleep 1
curl -s -X DELETE localhost:8000/tareas/$ID | python3 -m json.tool

# 4) Timeout
crear '{"tipo":"esperar","params":{"segundos":10},"timeout":2}'; sleep 3
curl -s localhost:8000/tareas/$ID | python3 -m json.tool

# 5) Estadísticas
curl -s localhost:8000/estadisticas | python3 -m json.tool

# 6) Apagado ordenado
crear '{"tipo":"esperar","params":{"segundos":60}}'
docker compose down          # ~10s, mirar los logs
ls datos/ && docker compose up -d && sleep 2 && docker compose logs | grep "Volcado previo"
```

O desde el navegador: **http://localhost:8000/docs**.

---

## 5. Qué clase respalda cada parte

| Si te preguntan por… | Citá | Ejemplo concreto |
|----------------------|------|------------------|
| Por qué no hay `Lock` | Clase 11 (sincronización) y clase 14 | El `threading.Lock` de tu `server_threads.py` |
| fork vs threads vs async | Clase 14 (servidores concurrentes) | `sever_fork.py` y `server_threads.py` |
| Cómo un hilo atiende muchos sockets | Clase 17 (I/O multiplexing) | epoll por debajo del event loop |
| Corrutinas y `time.sleep` | Clase 18 (de `yield` a asyncio) | `esperar` usa `asyncio.sleep` |
| Por qué procesos y no threads | Clase 10 (threading, GIL) y clases 08–09 (multiprocessing) | `hashlib` no suelta el GIL con 32 bytes |
| Cómo viajan los datos al pool | Clase 05 (pipes) y clase 22 (`concurrent.futures`) | pickle sobre pipes; por eso `hash_bloque` va a nivel de módulo |
| Apagado ordenado | Clase 06 (señales) y clases 01–02 (Docker) | PID 1, `CMD` en formato exec, `stop_grace_period` |
| La API | Clase 19 (FastAPI) | 202, 409, 422, 503, `lifespan` |
| `gather` vs `for` con `await` | Clase 20 (asyncio en red) | Medición 1 |
| Cancelación y timeouts | Clase 21 (asyncio avanzado) | `cancel()`, `wait_for`, `cancelling()` |

---

## 6. Glosario rápido

| Término | En una línea |
|---------|--------------|
| **Corrutina** | Función `async def`; se puede pausar en cada `await` y retomar después |
| **Event loop** | Bucle de un solo hilo que decide qué corrutina corre; usa epoll por debajo |
| **Task** | Corrutina agendada en el loop (`create_task`); corre "en segundo plano" y se puede cancelar |
| **Future** | Promesa de un resultado futuro; `run_in_executor` devuelve uno |
| **I/O-bound** | El tiempo se va esperando (red, disco). asyncio lo resuelve |
| **CPU-bound** | El tiempo se va calculando. asyncio NO lo resuelve: hace falta otro proceso |
| **GIL** | Lock de CPython: un solo thread ejecuta bytecode Python a la vez |
| **Concurrencia vs paralelismo** | Concurrencia = intercalar tareas (1 hilo, asyncio). Paralelismo = ejecutarlas a la vez en varios núcleos (procesos) |
| **Backpressure** | Rechazar o frenar trabajo nuevo cuando el sistema está saturado (mi sistema NO lo tiene) |
| **PID 1** | Primer proceso de un contenedor; recibe las señales de Docker |
| **SIGTERM / SIGINT / SIGKILL** | Pedido de terminar (`docker stop`) / Ctrl+C / matar sin aviso (no se puede atrapar) |

"""Implementación de los tipos de tarea.

Cada tarea es una corrutina `async def f(params: dict) -> resultado`.

| Tarea      | Naturaleza   | Cómo se resuelve                               |
|------------|--------------|------------------------------------------------|
| descargar  | I/O-bound    | httpx async + asyncio.gather (concurrencia)    |
| hashear    | CPU-bound    | ProcessPoolExecutor vía run_in_executor        |
| esperar    | I/O simulado | asyncio.sleep                                  |
"""
import asyncio
import hashlib
import logging
import time
from concurrent.futures import Executor

import httpx

log = logging.getLogger("tareas")

# Executor para tareas CPU-bound. Lo crea y lo cierra el Gestor (ciclo de vida
# atado al de la app); acá solo guardamos la referencia.
_executor: Executor | None = None


def configurar_executor(executor: Executor | None) -> None:
    global _executor
    _executor = executor


# --- esperar ------------------------------------------------------------------

async def esperar(params: dict) -> dict:
    segundos = params["segundos"]
    # asyncio.sleep y NO time.sleep: asyncio.sleep suspende esta corrutina y
    # le devuelve el control al event loop; time.sleep congela el hilo entero
    # (y con él a la API y a todos los demás workers).
    await asyncio.sleep(segundos)
    return {"durmio": segundos}


# --- descargar ----------------------------------------------------------------
# ¿Por qué httpx y no aiohttp? Los dos son asíncronos. httpx tiene una API casi
# idéntica a `requests` (más fácil de leer), soporta HTTP/2, timeouts finos, y
# su AsyncClient es un async context manager que cierra las conexiones al salir
# del bloque, incluso si la corrutina se cancela. `requests` NO sirve: es
# bloqueante y congelaría el event loop durante cada descarga.

async def _descargar_una(cliente: httpx.AsyncClient, url: str) -> dict:
    t0 = time.perf_counter()
    try:
        resp = await cliente.get(url)
        return {
            "url": url,
            "status": resp.status_code,
            "bytes": len(resp.content),
            "segundos": round(time.perf_counter() - t0, 4),
        }
    except httpx.HTTPError as e:
        # Un error en UNA url no tira abajo las demás: se reporta en su fila.
        return {
            "url": url,
            "status": None,
            "error": f"{type(e).__name__}: {e}",
            "segundos": round(time.perf_counter() - t0, 4),
        }


async def descargar(params: dict) -> dict:
    urls = params["urls"]
    t0 = time.perf_counter()
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=50),
    ) as cliente:
        # gather lanza TODAS las descargas a la vez y espera a que terminen:
        # el tiempo total ≈ la más lenta, no la suma.
        # Con un `for url in urls: await ...` serían secuenciales.
        # Si esta corrutina se cancela, gather cancela a todas sus hijas y el
        # `async with` cierra el cliente y sus sockets.
        if params.get("secuencial"):
            # Solo para la medición 1: cada await espera a que termine la
            # descarga anterior antes de empezar la siguiente.
            resultados = [await _descargar_una(cliente, u) for u in urls]
        else:
            resultados = await asyncio.gather(
                *(_descargar_una(cliente, u) for u in urls)
            )
    return {
        "modo": "secuencial" if params.get("secuencial") else "concurrente",
        "descargas": resultados,
        "total_segundos": round(time.perf_counter() - t0, 4),
        "ok": sum(1 for r in resultados if r.get("status") is not None),
        "errores": sum(1 for r in resultados if r.get("status") is None),
    }


# --- hashear ------------------------------------------------------------------

# Iteraciones por bloque (~0.15 s de CPU). Ver hashear() más abajo.
BLOQUE_HASH = 250_000


def hash_bloque(dato: bytes, iteraciones: int) -> bytes:
    """Aplica SHA-256 `iteraciones` veces. PURA, SÍNCRONA y CPU-bound.

    Tiene que estar a nivel de módulo para que el ProcessPoolExecutor pueda
    serializarla (pickle) y mandarla al proceso hijo.
    """
    for _ in range(iteraciones):
        dato = hashlib.sha256(dato).digest()
    return dato


def hash_iterado(texto: str, iteraciones: int) -> str:
    """Versión de un solo tirón (la usa el modo sin_executor)."""
    return hash_bloque(texto.encode(), iteraciones).hex()


async def hashear(params: dict) -> dict:
    texto = params["texto"]
    iteraciones = params["iteraciones"]
    t0 = time.perf_counter()

    if params.get("sin_executor"):
        # MAL A PROPÓSITO (solo para la medición 2): llamar la función
        # síncrona directo en la corrutina. No hay ningún `await` adentro, así
        # que el event loop queda congelado hasta que termine: la API deja de
        # responder y los demás workers se frenan.
        digest = hash_iterado(texto, iteraciones)
        modo = "event loop (bloqueante)"
    else:
        # BIEN: run_in_executor manda el trabajo a otro proceso y devuelve un
        # future que se puede `await`-ear. El event loop sigue atendiendo
        # requests mientras otro núcleo calcula.
        # ¿Procesos y no threads (asyncio.to_thread)? Por el GIL: un thread
        # haciendo SHA-256 sobre 32 bytes casi no suelta el GIL (hashlib solo
        # lo libera con datos > 2 KiB), así que competiría con el event loop
        # por el intérprete. Un proceso tiene su propio intérprete y su propio
        # GIL: paralelismo real en otro núcleo.
        #
        # CANCELACIÓN: un proceso que ya está calculando NO se puede
        # interrumpir desde asyncio (cancelar el future solo deja de esperarlo;
        # el proceso sigue quemando CPU). Por eso partimos el trabajo en
        # bloques: entre bloque y bloque hay un `await`, que es donde llega el
        # CancelledError (por DELETE o por timeout). Como mucho se desperdicia
        # UN bloque (~0.15 s) en vez del hash entero. El resultado es idéntico
        # porque SHA-256 iterado es una cadena: la salida de un bloque es la
        # entrada del siguiente.
        loop = asyncio.get_running_loop()
        dato = texto.encode()
        restantes = iteraciones
        while restantes > 0:
            n = min(BLOQUE_HASH, restantes)
            dato = await loop.run_in_executor(_executor, hash_bloque, dato, n)
            restantes -= n
        digest = dato.hex()
        modo = "process pool"

    return {
        "hash": digest,
        "iteraciones": iteraciones,
        "modo": modo,
        "segundos": round(time.perf_counter() - t0, 4),
    }

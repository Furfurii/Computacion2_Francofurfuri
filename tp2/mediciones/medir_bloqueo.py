"""Medición 2: el event loop bloqueado.

Mientras corre un `hashear` pesado, se mide la latencia de `GET /tareas`
haciendo requests en serie (una termina, arranca la siguiente).

Tres escenarios:
  - referencia: sin ninguna tarea corriendo
  - SIN executor: el hash corre dentro del event loop
  - CON executor: el hash corre en el ProcessPoolExecutor

Uso:  python mediciones/medir_bloqueo.py [--iteraciones 10000000]
"""
import argparse
import asyncio
import time

import httpx

from comun import API, FINALES, crear, guardar, obtener, resumen


async def muestrear(c, duracion=None, tid=None, pausa=0.02) -> list[float]:
    """Latencias (en segundos) de GET /tareas, en serie.

    Si se pasa `tid`, muestrea hasta que esa tarea termine; si no, durante
    `duracion` segundos.
    """
    lat = []
    fin = time.perf_counter() + (duracion or 0)
    while True:
        t0 = time.perf_counter()
        r = await c.get(f"{API}/tareas")
        r.raise_for_status()
        lat.append(time.perf_counter() - t0)
        if tid is not None:
            if (await obtener(c, tid))["estado"] in FINALES:
                return lat
        elif time.perf_counter() >= fin:
            return lat
        await asyncio.sleep(pausa)


def mostrar(nombre, lat, extra=""):
    r = resumen(lat)
    print(f"  {nombre:14s} n={r['n']:4d}  p50={r['p50'] * 1000:8.1f} ms  "
          f"p95={r['p95'] * 1000:8.1f} ms  max={r['max'] * 1000:8.1f} ms {extra}")
    return r


async def con_hash(c, iteraciones, sin_executor):
    tid = await crear(c, {"tipo": "hashear", "timeout": 600, "params": {
        "texto": "medicion", "iteraciones": iteraciones,
        "sin_executor": sin_executor}})
    lat = await muestrear(c, tid=tid)
    tarea = await obtener(c, tid)
    return lat, tarea["duracion"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iteraciones", type=int, default=10_000_000)
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=600) as c:
        print(f"Hash de {args.iteraciones:,} iteraciones. "
              "Latencia de GET /tareas:\n")
        # Calentamiento: abre la conexión keep-alive y saca a la CPU del
        # estado de bajo consumo, para que la referencia no salga inflada.
        await muestrear(c, duracion=1)
        ref = mostrar("referencia", await muestrear(c, duracion=3))

        lat_sin, dur_sin = await con_hash(c, args.iteraciones, True)
        sin = mostrar("SIN executor", lat_sin, f"(hash: {dur_sin:.2f}s)")

        lat_con, dur_con = await con_hash(c, args.iteraciones, False)
        con = mostrar("CON executor", lat_con, f"(hash: {dur_con:.2f}s)")

    print(f"\n  Peor latencia: SIN {sin['max'] * 1000:.0f} ms vs "
          f"CON {con['max'] * 1000:.1f} ms  ->  x{sin['max'] / con['max']:.0f}")
    print(f"  Respuestas durante el hash: SIN {sin['n']} vs CON {con['n']}")
    guardar("medicion2_bloqueo", {
        "iteraciones": args.iteraciones,
        "referencia": ref,
        "sin_executor": {**sin, "duracion_hash": dur_sin, "latencias": lat_sin},
        "con_executor": {**con, "duracion_hash": dur_con, "latencias": lat_con},
    })


if __name__ == "__main__":
    asyncio.run(main())

"""Medición 3: techo del pool.

Se mandan K tareas de golpe (K = 1, 2, 4, ... 64) y se mide cuánto tarda el
lote completo, cuánto esperó cada tarea en la cola y cuánto duró su ejecución.
Dos tipos de tarea, para ver los dos límites distintos:
  - esperar 1s   (I/O simulado: solo la limitan los workers)
  - hashear 1M   (CPU: la limitan los workers Y los núcleos)

Los tiempos salen de las marcas del servidor (creada/iniciada/finalizada),
así el polling del script no ensucia la medición.

Correr con distintas cantidades de workers:
    N_WORKERS=4  docker compose up -d && python mediciones/medir_techo.py
    N_WORKERS=64 docker compose up -d && python mediciones/medir_techo.py
"""
import argparse
import asyncio
import statistics
import time

import httpx

from comun import API, config, crear, guardar, ts

TAREAS = {
    "esperar": {"tipo": "esperar", "params": {"segundos": 1}},
    "hashear": {"tipo": "hashear",
                "params": {"texto": "techo", "iteraciones": 1_000_000}},
}


async def lote(c, cuerpo, k) -> dict:
    ids = await asyncio.gather(*(crear(c, {**cuerpo, "timeout": 600})
                                 for _ in range(k)))
    # Esperamos a que el pool se vacíe consultando /estadisticas (1 request
    # cada 50 ms, en vez de K requests de polling).
    while True:
        e = await config(c)
        if e["workers_ocupados"] == 0 and e["en_cola"] == 0:
            break
        await asyncio.sleep(0.05)
    r = await c.get(f"{API}/tareas")
    tareas = [t for t in r.json() if t["id"] in set(ids)]
    assert all(t["estado"] == "completada" for t in tareas), "alguna falló"
    total = (max(ts(t["finalizada"]) for t in tareas)
             - min(ts(t["creada"]) for t in tareas))
    espera = [ts(t["iniciada"]) - ts(t["creada"]) for t in tareas]
    dur = [t["duracion"] for t in tareas]
    return {"k": k, "total": total,
            "espera_cola_prom": statistics.mean(espera),
            "duracion_prom": statistics.mean(dur),
            "duracion_max": max(dur),
            "throughput": k / total}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", default="1,2,4,8,16,32,64")
    args = ap.parse_args()
    ks = [int(x) for x in args.ks.split(",")]

    async with httpx.AsyncClient(timeout=600,
                                 limits=httpx.Limits(max_connections=100)) as c:
        cfg = await config(c)
        w = cfg["workers_totales"]
        print(f"Servidor con {w} workers\n")
        datos = {"workers": w}
        for nombre, cuerpo in TAREAS.items():
            print(f"=== {nombre} ===")
            print(f"  {'K':>3} {'total':>8} {'espera cola':>12} "
                  f"{'ejecución':>10} {'tareas/s':>9}")
            filas = []
            for k in ks:
                f = await lote(c, cuerpo, k)
                filas.append(f)
                print(f"  {k:3d} {f['total']:7.2f}s {f['espera_cola_prom']:11.2f}s "
                      f"{f['duracion_prom']:9.2f}s {f['throughput']:9.2f}")
                await asyncio.sleep(0.5)
            datos[nombre] = filas
            print()
    guardar(f"medicion3_techo_w{w}", datos)


if __name__ == "__main__":
    t0 = time.perf_counter()
    asyncio.run(main())
    print(f"(medición completa en {time.perf_counter() - t0:.0f}s)")

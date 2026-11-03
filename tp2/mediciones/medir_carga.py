"""Extra: "si te mando 10.000 tareas de golpe, ¿qué se rompe primero?"

Manda N tareas `esperar 60s` (100 POST en paralelo) y mide: throughput y
latencia de los POST, cuánto tarda GET /tareas con N tareas en memoria y
el tamaño de la respuesta. La memoria del contenedor se mira aparte con
`docker stats`.

Uso:  python mediciones/medir_carga.py [--n 10000]
"""
import argparse
import asyncio
import time

import httpx

from comun import API, guardar, resumen


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--paralelo", type=int, default=100)
    args = ap.parse_args()

    async with httpx.AsyncClient(
        timeout=120, limits=httpx.Limits(max_connections=args.paralelo)
    ) as c:
        sem = asyncio.Semaphore(args.paralelo)
        lat = []

        async def post():
            async with sem:
                t = time.perf_counter()
                r = await c.post(f"{API}/tareas", json={
                    "tipo": "esperar", "params": {"segundos": 60}})
                lat.append(time.perf_counter() - t)
                r.raise_for_status()

        t0 = time.perf_counter()
        await asyncio.gather(*(post() for _ in range(args.n)))
        dt = time.perf_counter() - t0
        r = resumen(lat)
        print(f"{args.n} POST en {dt:.1f}s ({args.n / dt:.0f} POST/s) | "
              f"latencia p50={r['p50'] * 1000:.0f} ms max={r['max'] * 1000:.0f} ms")

        gets = {}
        for ruta in ["/estadisticas", "/tareas?estado=ejecutando", "/tareas"]:
            t = time.perf_counter()
            resp = await c.get(API + ruta)
            d = time.perf_counter() - t
            gets[ruta] = {"ms": d * 1000, "bytes": len(resp.content)}
            print(f"GET {ruta:28s} {d * 1000:8.1f} ms "
                  f"({len(resp.content) / 1e6:.2f} MB)")

    guardar("extra_carga", {"n": args.n, "segundos_posts": dt,
                            "post_por_segundo": args.n / dt,
                            "latencia_post": r, "gets": gets})


if __name__ == "__main__":
    asyncio.run(main())

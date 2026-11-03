"""Medición 1: concurrencia I/O.

20 descargas secuenciales (for + await) contra 20 concurrentes (gather),
usando la tarea `descargar` de la propia plataforma.

Dos escenarios:
  a) controlado: 20 requests a /demo/lento?ms=500 (latencia fija y conocida)
  b) real: 20 sitios de internet

Uso:  python mediciones/medir_io.py [--reps 3] [--ms 500]
"""
import argparse
import asyncio
import statistics

import httpx

from comun import API_INTERNA, crear, esperar_fin, guardar

SITIOS_REALES = [
    "https://example.com", "https://www.python.org", "https://docs.python.org/3/",
    "https://pypi.org", "https://github.com", "https://www.wikipedia.org",
    # (www.gnu.org y www.um.edu.ar se sacaron: desde esta red daban
    #  ConnectTimeout en casi todas las corridas)
    "https://www.debian.org", "https://www.kernel.org", "https://ubuntu.com",
    "https://www.mozilla.org", "https://fastapi.tiangolo.com",
    "https://www.python-httpx.org", "https://www.djangoproject.com",
    "https://www.postgresql.org", "https://www.sqlite.org", "https://nginx.org",
    "https://www.rust-lang.org", "https://go.dev", "https://nodejs.org",
    "https://www.apache.org",
]


async def correr(c, urls, secuencial: bool) -> dict:
    tid = await crear(c, {"tipo": "descargar", "timeout": 300,
                          "params": {"urls": urls, "secuencial": secuencial}})
    t = await esperar_fin(c, tid)
    if t["estado"] != "completada":
        raise RuntimeError(f"La tarea terminó {t['estado']}: {t['error']}")
    r = t["resultado"]
    return {
        "total": r["total_segundos"],
        "suma_individual": sum(d["segundos"] for d in r["descargas"]),
        "errores": r["errores"],
    }


async def escenario(c, nombre, urls, reps) -> dict:
    print(f"\n=== {nombre}: {len(urls)} URLs, {reps} repeticiones ===")
    res = {"secuencial": [], "concurrente": []}
    for i in range(reps):
        # Alternamos el orden para que ningún modo se beneficie siempre de
        # cachés/conexiones calientes del otro.
        orden = [True, False] if i % 2 == 0 else [False, True]
        for sec in orden:
            m = await correr(c, urls, sec)
            modo = "secuencial" if sec else "concurrente"
            res[modo].append(m)
            print(f"  rep {i + 1} {modo:11s}: {m['total']:7.3f}s "
                  f"(errores: {m['errores']})")
    s = statistics.mean(m["total"] for m in res["secuencial"])
    k = statistics.mean(m["total"] for m in res["concurrente"])
    print(f"  -> secuencial {s:.3f}s | concurrente {k:.3f}s | "
          f"speedup x{s / k:.1f}")
    return {"urls": urls, "corridas": res, "secuencial_prom": s,
            "concurrente_prom": k, "speedup": s / k}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--ms", type=int, default=500)
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=600) as c:
        controlado = [f"{API_INTERNA}/demo/lento?ms={args.ms}&i={i}"
                      for i in range(args.n)]
        a = await escenario(c, f"CONTROLADO ({args.ms} ms c/u)", controlado,
                            args.reps)
        b = await escenario(c, "SITIOS REALES", SITIOS_REALES[:args.n],
                            args.reps)
    guardar("medicion1_io", {"ms": args.ms, "n": args.n,
                             "controlado": a, "real": b})


if __name__ == "__main__":
    asyncio.run(main())

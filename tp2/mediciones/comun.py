"""Utilidades compartidas por los scripts de medición.

Se corren desde el host contra la API ya levantada:
    docker compose up -d
    python mediciones/medir_io.py
"""
import json
import os
import statistics
from datetime import datetime
from pathlib import Path

import httpx

API = os.getenv("API_URL", "http://localhost:8000")
# URL con la que el SERVIDOR se llama a sí mismo (dentro del contenedor,
# localhost es el propio contenedor).
API_INTERNA = os.getenv("API_INTERNA", "http://localhost:8000")
FINALES = {"completada", "fallida", "cancelada"}
RESULTADOS = Path(__file__).parent / "resultados"


async def crear(c: httpx.AsyncClient, cuerpo: dict) -> str:
    r = await c.post(f"{API}/tareas", json=cuerpo)
    r.raise_for_status()
    return r.json()["id"]


async def obtener(c: httpx.AsyncClient, tid: str) -> dict:
    r = await c.get(f"{API}/tareas/{tid}")
    r.raise_for_status()
    return r.json()


async def esperar_fin(c: httpx.AsyncClient, tid: str, intervalo=0.05) -> dict:
    import asyncio
    while True:
        t = await obtener(c, tid)
        if t["estado"] in FINALES:
            return t
        await asyncio.sleep(intervalo)


async def config(c: httpx.AsyncClient) -> dict:
    r = await c.get(f"{API}/estadisticas")
    return r.json()


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def percentil(valores: list[float], p: float) -> float:
    v = sorted(valores)
    k = (len(v) - 1) * p / 100
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


def resumen(valores: list[float]) -> dict:
    return {
        "n": len(valores),
        "min": min(valores),
        "p50": statistics.median(valores),
        "p95": percentil(valores, 95),
        "max": max(valores),
        "promedio": statistics.mean(valores),
    }


def guardar(nombre: str, datos: dict) -> Path:
    RESULTADOS.mkdir(exist_ok=True)
    datos = {"fecha": datetime.now().isoformat(timespec="seconds"),
             "api": API, **datos}
    destino = RESULTADOS / f"{nombre}.json"
    destino.write_text(json.dumps(datos, indent=2, ensure_ascii=False))
    print(f"\nResultados guardados en {destino}")
    return destino

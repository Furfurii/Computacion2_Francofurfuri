"""API HTTP de la plataforma de tareas distribuidas."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.gestor import Gestor, ServidorApagandose, reportar_volcado_previo
from app.modelos import (
    ESTADOS_FINALES, Estadisticas, Estado, TareaCrear, TareaCreada, TareaInfo,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

N_WORKERS = int(os.getenv("N_WORKERS", "4"))
TIMEOUT_DEFAULT = float(os.getenv("TIMEOUT_DEFAULT", "60"))
N_PROCESOS = int(os.getenv("N_PROCESOS", "0")) or None  # 0 = cpu_count()
TIMEOUT_APAGADO = float(os.getenv("TIMEOUT_APAGADO", "10"))
DIR_ESTADO = Path(os.getenv("DIR_ESTADO", "datos"))

log = logging.getLogger("api")

gestor = Gestor(n_workers=N_WORKERS, timeout_default=TIMEOUT_DEFAULT,
                n_procesos=N_PROCESOS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lo de antes del yield corre al arrancar, lo de después al apagar.
    # Los workers se crean acá (y no al importar el módulo) porque
    # create_task necesita un event loop corriendo: el de uvicorn.
    reportar_volcado_previo(DIR_ESTADO)
    await gestor.iniciar()
    yield
    # SEÑALES: uvicorn instala sus propios handlers de SIGINT y SIGTERM
    # (con loop.add_signal_handler en Unix). Al recibir una, deja de aceptar
    # conexiones nuevas, espera las requests en vuelo y recién ahí sale del
    # `yield`. No instalamos handlers propios porque pisaríamos los de uvicorn
    # y el servidor HTTP no se enteraría del apagado.
    log.info("Señal de apagado recibida: apagado ordenado")
    try:
        await gestor.apagar(limite=TIMEOUT_APAGADO)
    finally:
        # Aunque algo falle al apagar, el volcado se intenta igual.
        await gestor.volcar_estado(DIR_ESTADO)


app = FastAPI(title="Plataforma de Tareas Distribuidas", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def raiz():
    # Abrir http://localhost:8000 lleva directo a la documentación interactiva.
    return RedirectResponse(url="/docs")


def _buscar(tid: str):
    reg = gestor.obtener(tid)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tarea inexistente")
    return reg


@app.post("/tareas", response_model=TareaCreada,
          status_code=status.HTTP_202_ACCEPTED)
async def crear_tarea(tarea: TareaCrear):
    # 202 Accepted: se encola y se responde ya, sin esperar a que termine.
    try:
        reg = gestor.encolar(tarea.tipo, tarea.params.model_dump(), tarea.timeout)
    except ServidorApagandose:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "El servidor se está apagando")
    return TareaCreada(id=reg.id, estado=reg.estado)


@app.get("/tareas", response_model=list[TareaInfo])
async def listar_tareas(estado: Estado | None = None):
    return [r.a_info() for r in gestor.listar(estado)]


@app.get("/tareas/{tid}", response_model=TareaInfo)
async def obtener_tarea(tid: str):
    return _buscar(tid).a_info()


@app.delete("/tareas/{tid}", response_model=TareaInfo)
async def cancelar_tarea(tid: str):
    reg = _buscar(tid)
    if reg.estado in ESTADOS_FINALES:
        # Ya terminó (completada/fallida/cancelada): no hay nada que cancelar.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La tarea ya está en estado final: {reg.estado.value}",
        )
    return (await gestor.cancelar(tid)).a_info()


@app.get("/estadisticas", response_model=Estadisticas)
async def estadisticas():
    return gestor.estadisticas()


@app.get("/demo/lento", tags=["demo"])
async def demo_lento(ms: int = Query(500, ge=0, le=10_000)):
    """Responde después de `ms` milisegundos.

    Es un "servidor lento" controlado para la medición 1: las descargas
    contra internet varían mucho entre corridas; este endpoint tarda siempre
    lo mismo, así secuencial vs concurrente se compara limpio.
    """
    await asyncio.sleep(ms / 1000)
    return {"ms": ms}


if __name__ == "__main__":
    # Guarda necesaria: con ProcessPoolExecutor en macOS/Windows (spawn) el
    # módulo se re-importa en cada proceso hijo.
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""Gestor de tareas: cola + pool de workers + registro de estado.

Patrón productor-consumidor:
  - Productor: el endpoint POST /tareas, que hace `cola.put_nowait(id)`.
  - Consumidores: N corrutinas worker que hacen `await cola.get()`.

¿Por qué no hace falta un Lock? Todo corre en UN solo hilo (el del event
loop) y una corrutina solo cede el control en un `await`. Entre dos `await`
ninguna otra corrutina puede tocar `self.tareas` ni `self._ocupados`, así que
no hay condiciones de carrera. Con threads (clase de productor-consumidor con
queue.Queue) el scheduler del SO puede interrumpir en cualquier instrucción;
acá no.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import tareas as ejecutores
from app.modelos import ESTADOS_FINALES, Estadisticas, Estado, TareaInfo

log = logging.getLogger("gestor")

# Tipo de tarea -> corrutina que la ejecuta.
EJECUTORES = {
    "descargar": ejecutores.descargar,
    "hashear": ejecutores.hashear,
    "esperar": ejecutores.esperar,
}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class ServidorApagandose(Exception):
    """Se intentó encolar durante el apagado."""


def _escribir_json_atomico(destino: Path, datos: dict) -> None:
    """Escribe a un .tmp y después renombra.

    os.replace es atómico: si el proceso muere a mitad de la escritura queda
    un .tmp roto, nunca un estado_*.json corrupto.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False))
    os.replace(tmp, destino)


def reportar_volcado_previo(directorio: Path) -> Path | None:
    """Al arrancar: si hay un volcado previo, lo informa por log."""
    volcados = sorted(directorio.glob("estado_*.json"))
    if not volcados:
        log.info("No hay volcados previos en %s", directorio)
        return None
    ultimo = volcados[-1]
    try:
        datos = json.loads(ultimo.read_text())
        por_estado = {k: v for k, v in
                      datos["estadisticas"]["por_estado"].items() if v}
        log.info("Volcado previo encontrado: %s (del %s, %d tareas: %s). "
                 "Hay %d volcados en total.", ultimo.name, datos["volcado"],
                 len(datos["tareas"]), por_estado, len(volcados))
    except (OSError, ValueError, KeyError) as e:
        log.warning("Volcado previo %s ilegible: %r", ultimo.name, e)
    return ultimo


@dataclass
class Registro:
    """Estado interno de una tarea (lo que guardamos en memoria)."""
    id: str
    tipo: str
    params: dict[str, Any]
    timeout: float
    estado: Estado = Estado.PENDIENTE
    creada: datetime = field(default_factory=_ahora)
    iniciada: datetime | None = None
    finalizada: datetime | None = None
    duracion: float | None = None
    resultado: Any = None
    error: str | None = None
    # Internos (no se exponen por la API ni se vuelcan a JSON):
    # la asyncio.Task que ejecuta la tarea, para poder cancelarla en vivo...
    _task: asyncio.Task | None = field(default=None, repr=False)
    # ...y un Event que se dispara al llegar a un estado final, para que
    # DELETE pueda esperar a que la cancelación se complete de verdad.
    terminada: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def a_info(self) -> TareaInfo:
        return TareaInfo(
            id=self.id, tipo=self.tipo, params=self.params,
            estado=self.estado, timeout=self.timeout, creada=self.creada,
            iniciada=self.iniciada, finalizada=self.finalizada,
            duracion=self.duracion, resultado=self.resultado,
            error=self.error,
        )


class Gestor:
    def __init__(self, n_workers: int = 4, timeout_default: float = 60.0,
                 n_procesos: int | None = None):
        self.n_workers = n_workers
        self.n_procesos = n_procesos or os.cpu_count() or 2
        self._executor: ProcessPoolExecutor | None = None
        self.timeout_default = timeout_default
        # asyncio.Queue y no queue.Queue: su get() es una corrutina que
        # suspende al worker sin bloquear el hilo. queue.Queue.get() es
        # bloqueante y congelaría el event loop entero.
        self.cola: asyncio.Queue[str] = asyncio.Queue()
        self.tareas: dict[str, Registro] = {}
        self._workers: list[asyncio.Task] = []
        self._ocupados = 0
        self.aceptando = True

    # --- ciclo de vida --------------------------------------------------------

    async def iniciar(self) -> None:
        # Pool de procesos para lo CPU-bound (hashear).
        self._executor = ProcessPoolExecutor(max_workers=self.n_procesos)
        ejecutores.configurar_executor(self._executor)
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"worker-{i}")
            for i in range(self.n_workers)
        ]
        log.info("Pool iniciado con %d workers y %d procesos CPU",
                 self.n_workers, self.n_procesos)

    async def apagar(self, limite: float) -> None:
        """Apagado ordenado (lo dispara SIGINT/SIGTERM vía lifespan).

        1. Deja de aceptar tareas nuevas.
        2. Descarta las que estaban en cola (nunca empezaron).
        3. Espera hasta `limite` segundos a las que están en ejecución.
        4. Cancela las que no llegaron y detiene workers y executor.
        """
        self.aceptando = False
        log.info("Apagado: no se aceptan más tareas")

        pendientes = self.listar(Estado.PENDIENTE)
        for reg in pendientes:
            self._finalizar(reg, Estado.CANCELADA,
                            error="No ejecutada: apagado del servidor")
        if pendientes:
            log.info("Apagado: %d tareas en cola descartadas", len(pendientes))

        en_curso = [r._task for r in self.listar(Estado.EJECUTANDO) if r._task]
        if en_curso:
            log.info("Apagado: esperando hasta %.1fs a %d tareas en curso",
                     limite, len(en_curso))
            # asyncio.wait (no wait_for): con timeout NO cancela nada, solo
            # devuelve qué terminó y qué no. Eso lo decidimos nosotros abajo.
            hechas, sin_terminar = await asyncio.wait(en_curso, timeout=limite)
            log.info("Apagado: %d terminaron, %d se cancelan por el límite",
                     len(hechas), len(sin_terminar))

        await self.detener()

    async def detener(self) -> None:
        # Cancelar al worker cancela también la tarea hija que esté esperando
        # (await de una Task propaga la cancelación). El worker la marca como
        # "cancelada por apagado" y re-lanza.
        for w in self._workers:
            w.cancel()
        # gather espera a que cada worker procese su CancelledError.
        # return_exceptions=True: que un worker falle no corta la espera.
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if self._executor is not None:
            # shutdown(wait=True) es BLOQUEANTE (espera que los procesos
            # terminen su bloque actual): lo mandamos a un thread para no
            # congelar el loop. cancel_futures descarta lo que no arrancó.
            await asyncio.to_thread(
                self._executor.shutdown, wait=True, cancel_futures=True
            )
            ejecutores.configurar_executor(None)
            self._executor = None
        log.info("Pool detenido")

    # --- persistencia -----------------------------------------------------------

    def _volcado(self) -> dict:
        return {
            "volcado": _ahora().isoformat(),
            "config": {"n_workers": self.n_workers,
                       "n_procesos": self.n_procesos,
                       "timeout_default": self.timeout_default},
            "estadisticas": self.estadisticas().model_dump(mode="json"),
            "tareas": [r.a_info().model_dump(mode="json")
                       for r in self.tareas.values()],
        }

    async def volcar_estado(self, directorio: Path) -> Path:
        datos = self._volcado()   # se arma en el loop: nadie lo modifica a la vez
        ts = _ahora().strftime("%Y%m%dT%H%M%SZ")   # UTC, igual que adentro
        destino = directorio / f"estado_{ts}.json"
        # Escribir a disco es I/O bloqueante: a un thread.
        await asyncio.to_thread(_escribir_json_atomico, destino, datos)
        log.info("Estado volcado en %s (%d tareas)", destino, len(self.tareas))
        return destino

    # --- operaciones (llamadas desde la API) ----------------------------------

    def encolar(self, tipo: str, params: dict, timeout: float | None) -> Registro:
        if not self.aceptando:
            raise ServidorApagandose()
        reg = Registro(
            id=uuid.uuid4().hex, tipo=tipo, params=params,
            timeout=timeout or self.timeout_default,
        )
        self.tareas[reg.id] = reg
        # put_nowait: la cola no tiene límite, así que nunca bloquea y el
        # POST responde al instante.
        self.cola.put_nowait(reg.id)
        log.info("Encolada %s (%s)", reg.id, tipo)
        return reg

    def obtener(self, tid: str) -> Registro | None:
        return self.tareas.get(tid)

    def listar(self, estado: Estado | None = None) -> list[Registro]:
        regs = self.tareas.values()
        if estado is not None:
            return [r for r in regs if r.estado == estado]
        return list(regs)

    async def cancelar(self, tid: str) -> Registro | None:
        reg = self.tareas.get(tid)
        if reg is None or reg.estado in ESTADOS_FINALES:
            return reg
        if reg.estado == Estado.PENDIENTE:
            # Sigue dentro de la cola (asyncio.Queue no permite sacar un
            # elemento del medio). La marcamos y el worker que la saque la
            # descarta sin ejecutarla.
            self._finalizar(reg, Estado.CANCELADA,
                            error="Cancelada por el usuario (estaba en cola)")
        elif reg._task is not None:
            # EN EJECUCIÓN: cancel() inyecta un CancelledError en el próximo
            # `await` de la tarea. Eso recorre la pila hacia arriba ejecutando
            # cada `finally` / `async with` (el cliente httpx cierra sus
            # sockets, gather cancela a sus hijas, etc.).
            reg._task.cancel()
            # Esperamos a que el worker termine de limpiar y marque el estado,
            # así la respuesta del DELETE ya refleja "cancelada".
            try:
                await asyncio.wait_for(reg.terminada.wait(), timeout=5)
            except TimeoutError:
                log.error("La tarea %s no terminó de cancelarse en 5s", tid)
        return reg

    def estadisticas(self) -> Estadisticas:
        por_estado = {e: 0 for e in Estado}
        duraciones: dict[str, list[float]] = {}
        for r in self.tareas.values():
            por_estado[r.estado] += 1
            if r.estado == Estado.COMPLETADA and r.duracion is not None:
                duraciones.setdefault(r.tipo, []).append(r.duracion)
        todas = [d for ds in duraciones.values() for d in ds]
        return Estadisticas(
            total=len(self.tareas),
            por_estado=por_estado,
            tiempo_promedio=sum(todas) / len(todas) if todas else None,
            tiempo_promedio_por_tipo={
                t: sum(ds) / len(ds) for t, ds in duraciones.items()
            },
            workers_totales=self.n_workers,
            workers_ocupados=self._ocupados,
            en_cola=self.cola.qsize(),
        )

    # --- internos -------------------------------------------------------------

    def _finalizar(self, reg: Registro, estado: Estado, *,
                   resultado: Any = None, error: str | None = None) -> None:
        reg.estado = estado
        reg.resultado = resultado
        reg.error = error
        reg.finalizada = _ahora()
        if reg.iniciada is not None:
            reg.duracion = (reg.finalizada - reg.iniciada).total_seconds()
        reg._task = None
        reg.terminada.set()

    async def _worker(self, n: int) -> None:
        """Consume ids de la cola para siempre (hasta que lo cancelen)."""
        while True:
            tid = await self.cola.get()   # suspende sin bloquear el loop
            try:
                reg = self.tareas[tid]
                if reg.estado != Estado.PENDIENTE:
                    continue   # se canceló mientras esperaba en la cola
                self._ocupados += 1
                try:
                    await self._ejecutar(reg, n)
                finally:
                    self._ocupados -= 1
            finally:
                # Siempre, pase lo que pase: si no, cola.join() nunca vuelve.
                self.cola.task_done()

    async def _ejecutar(self, reg: Registro, n: int) -> None:
        reg.estado = Estado.EJECUTANDO
        reg.iniciada = _ahora()
        t0 = time.perf_counter()
        log.info("worker-%d ejecuta %s (%s)", n, reg.id, reg.tipo)

        # La tarea corre en su PROPIA asyncio.Task (hija del worker), para
        # poder cancelarla desde DELETE sin cancelar al worker.
        # wait_for le pone el límite de tiempo: al vencer, cancela la
        # corrutina interna (misma limpieza que un DELETE) y lanza TimeoutError.
        reg._task = asyncio.create_task(
            asyncio.wait_for(EJECUTORES[reg.tipo](reg.params), reg.timeout),
            name=f"tarea-{reg.id}",
        )
        try:
            resultado = await reg._task
        except TimeoutError:
            log.warning("Tarea %s superó el timeout (%ss)", reg.id, reg.timeout)
            self._finalizar(reg, Estado.FALLIDA,
                            error=f"Timeout: superó {reg.timeout}s")
        except asyncio.CancelledError:
            # ¿Quién canceló? Si el cancelado es el WORKER (apagado del
            # servidor), current_task().cancelling() > 0 y hay que propagar
            # la excepción para que el worker termine. Si solo se canceló la
            # tarea hija (DELETE), el worker la absorbe y sigue vivo.
            if asyncio.current_task().cancelling():
                reg._task.cancel()   # por las dudas: que la hija no quede huérfana
                self._finalizar(reg, Estado.CANCELADA,
                                error="Cancelada por apagado del servidor")
                raise
            log.info("Tarea %s cancelada por el usuario", reg.id)
            self._finalizar(reg, Estado.CANCELADA,
                            error="Cancelada por el usuario (en ejecución)")
        except Exception as e:
            # Cualquier excepción de la tarea se captura acá: la tarea pasa a
            # 'fallida' y el worker SIGUE VIVO para la próxima. Si dejáramos
            # escapar la excepción, el worker moriría en silencio y el pool
            # perdería un consumidor.
            # (CancelledError hereda de BaseException, no de Exception, así
            # que no se atrapa acá: la cancelación sigue funcionando.)
            log.warning("Tarea %s falló: %r", reg.id, e)
            self._finalizar(reg, Estado.FALLIDA, error=f"{type(e).__name__}: {e}")
        else:
            self._finalizar(reg, Estado.COMPLETADA, resultado=resultado)
        log.info("Tarea %s -> %s en %.3fs", reg.id, reg.estado.value,
                 time.perf_counter() - t0)

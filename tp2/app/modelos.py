"""Modelos de datos (Pydantic) de la API.

Pydantic valida el cuerpo de cada request: si un cliente manda un tipo de
tarea desconocido o parámetros inválidos, FastAPI responde 422 solo, sin que
la tarea llegue nunca a la cola.
"""
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class Estado(str, Enum):
    PENDIENTE = "pendiente"
    EJECUTANDO = "ejecutando"
    COMPLETADA = "completada"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"


# Estados de los que una tarea ya no sale.
ESTADOS_FINALES = {Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA}


# --- Parámetros de cada tipo de tarea ----------------------------------------

class ParamsDescargar(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    # Solo para la medición 1: descarga de a una (for + await) en vez de gather.
    secuencial: bool = False


class ParamsHashear(BaseModel):
    texto: str
    iteraciones: int = Field(gt=0, le=50_000_000)
    # Solo para la medición 2: corre el hash directo en el event loop.
    sin_executor: bool = False


class ParamsEsperar(BaseModel):
    segundos: float = Field(ge=0, le=3600)


# --- Request de creación ------------------------------------------------------
# Unión discriminada por "tipo": Pydantic elige el modelo según ese campo y
# valida los parámetros que le corresponden.

class _TareaBase(BaseModel):
    timeout: float | None = Field(
        default=None, gt=0,
        description="Segundos máximos de ejecución. None = default del servidor",
    )


class CrearDescargar(_TareaBase):
    tipo: Literal["descargar"]
    params: ParamsDescargar


class CrearHashear(_TareaBase):
    tipo: Literal["hashear"]
    params: ParamsHashear


class CrearEsperar(_TareaBase):
    tipo: Literal["esperar"]
    params: ParamsEsperar


TareaCrear = Annotated[
    Union[CrearDescargar, CrearHashear, CrearEsperar],
    Field(discriminator="tipo"),
]


# --- Respuestas ---------------------------------------------------------------

class TareaCreada(BaseModel):
    id: str
    estado: Estado


class TareaInfo(BaseModel):
    id: str
    tipo: str
    params: dict[str, Any]
    estado: Estado
    timeout: float
    creada: datetime
    iniciada: datetime | None = None
    finalizada: datetime | None = None
    duracion: float | None = None
    resultado: Any = None
    error: str | None = None


class Estadisticas(BaseModel):
    total: int
    por_estado: dict[Estado, int]
    tiempo_promedio: float | None
    tiempo_promedio_por_tipo: dict[str, float]
    workers_totales: int
    workers_ocupados: int
    en_cola: int

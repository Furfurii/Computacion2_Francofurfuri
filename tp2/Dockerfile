FROM python:3.12-slim

# PYTHONUNBUFFERED: los logs salen al instante en `docker compose logs`.
# PYTHONDONTWRITEBYTECODE: no ensuciar la imagen con .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Primero solo requirements: si el código cambia pero las dependencias no,
# Docker reusa esta capa cacheada y el build es instantáneo.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

EXPOSE 8000

# Formato EXEC (lista JSON), no shell: así python es el PID 1 del contenedor y
# recibe directamente el SIGTERM de `docker compose down`/`docker stop`.
# Con `CMD python -m app.main` la señal le llegaría a /bin/sh, que no la
# reenvía, y tras el grace period Docker mataría todo con SIGKILL (sin volcado).
CMD ["python", "-m", "app.main"]

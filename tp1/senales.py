"""
senales.py — Manejo de señales del monitor.

Implementa el patrón self-pipe para hacer que los handlers de señales
sean async-signal-safe. Los handlers solo escriben un byte en un pipe;
el loop principal lee del pipe y decide qué hacer.

Señales manejadas:
    SIGINT  / SIGTERM  → shutdown limpio
    SIGHUP             → recargar config.json
    SIGUSR1            → dump del snapshot a dump_<timestamp>.json
    SIGUSR2            → toggle modo verbose
    SIGWINCH           → repintar pantalla (terminal redimensionada)
"""

import os
import signal
import json
import time


# Pipe global para self-pipe pattern
_read_fd = None
_write_fd = None

# Flag global de modo verbose
_verbose = False


def instalar_handlers():
    """
    Crea el self-pipe y registra los handlers de señales.
    Debe llamarse UNA vez desde el proceso principal, antes de arrancar hijos.
    Devuelve el file descriptor de lectura para que el loop principal lo mire.
    """
    global _read_fd, _write_fd
    _read_fd, _write_fd = os.pipe()
    
    # Configurar el pipe como no-bloqueante
    import fcntl
    fcntl.fcntl(_write_fd, fcntl.F_SETFL, os.O_NONBLOCK)
    fcntl.fcntl(_read_fd,  fcntl.F_SETFL, os.O_NONBLOCK)
    
    # Registrar handlers. Cada uno escribe UN byte identificador en el pipe.
    signal.signal(signal.SIGINT,   lambda s, f: _escribir_pipe(b'T'))  # Terminate
    signal.signal(signal.SIGTERM,  lambda s, f: _escribir_pipe(b'T'))
    signal.signal(signal.SIGHUP,   lambda s, f: _escribir_pipe(b'R'))  # Reload
    signal.signal(signal.SIGUSR1,  lambda s, f: _escribir_pipe(b'D'))  # Dump
    signal.signal(signal.SIGUSR2,  lambda s, f: _escribir_pipe(b'V'))  # Verbose toggle
    signal.signal(signal.SIGWINCH, lambda s, f: _escribir_pipe(b'W'))  # Winch
    
    return _read_fd


def _escribir_pipe(byte):
    """Handler mínimo: solo escribe un byte identificador en el pipe."""
    try:
        os.write(_write_fd, byte)
    except (BlockingIOError, OSError):
        # Pipe lleno o cerrado: ignoramos (async-signal-safe)
        pass


def leer_evento():
    """
    Lee un byte del pipe (no bloqueante). Devuelve el byte leído o None.
    El loop principal llama esta función y actúa según qué byte llegó.
    """
    try:
        byte = os.read(_read_fd, 1)
        return byte if byte else None
    except (BlockingIOError, OSError):
        return None


def dump_snapshot(snapshot, ruta=None):
    """Dumpea el snapshot a un archivo JSON. Llamado por SIGUSR1."""
    if ruta is None:
        ruta = f"dump_{int(time.time())}.json"
    
    # Convertir el Manager.dict() a dict normal para serializar
    datos = {}
    for clave in snapshot.keys():
        try:
            datos[clave] = dict(snapshot[clave]) if hasattr(snapshot[clave], 'keys') else snapshot[clave]
        except Exception:
            datos[clave] = str(snapshot[clave])
    
    with open(ruta, 'w') as f:
        json.dump(datos, f, indent=2, default=str)
    
    return ruta


def cargar_config(ruta='config.json'):
    """Carga la configuración desde JSON. Llamado por SIGHUP o al arrancar."""
    try:
        with open(ruta, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def toggle_verbose():
    """Toggle del modo verbose."""
    global _verbose
    _verbose = not _verbose
    return _verbose


def is_verbose():
    return _verbose
"""
analizadores/fds.py — Analizador de File Descriptors por proceso.
Guarda en snapshot['fds'] un dict {pid: {total, por_tipo, timestamp}}.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_fds


def analizador_fds(snapshot, intervalo):
    while True:
        datos = {}
        for pid in listar_pids():
            fds = leer_fds(pid)
            if fds is None:
                continue  # sin permisos o proceso murió
            
            # Contar cuántos hay de cada tipo
            por_tipo = {}
            for fd in fds:
                tipo = fd['tipo']
                por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
            
            datos[pid] = {
                'total': len(fds),
                'por_tipo': por_tipo,
                'timestamp': time.time(),
            }
        
        try:
            snapshot['fds'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

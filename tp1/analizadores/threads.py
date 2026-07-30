"""
analizadores/threads.py — Analizador de threads (LWPs) por proceso.
Guarda en snapshot['threads'] un dict {pid: {cantidad, por_estado, timestamp}}.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_threads


def analizador_threads(snapshot, intervalo):
    while True:
        datos = {}
        for pid in listar_pids():
            threads = leer_threads(pid)
            if threads is None:
                continue
            
            # Contar por estado (R, S, D, T, Z, ...)
            por_estado = {}
            for t in threads:
                estado = t['estado']
                por_estado[estado] = por_estado.get(estado, 0) + 1
            
            datos[pid] = {
                'cantidad': len(threads),
                'por_estado': por_estado,
                'timestamp': time.time(),
            }
        
        try:
            snapshot['threads'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

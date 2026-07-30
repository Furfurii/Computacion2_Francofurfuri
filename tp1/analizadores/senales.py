"""
analizadores/senales.py — Analizador de señales por proceso.
Guarda en snapshot['senales'] las máscaras hex tal cual vienen de /proc/<pid>/status.
La decodificación a nombres (SIGINT, SIGTERM, ...) es responsabilidad de la vista.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_status


def analizador_senales(snapshot, intervalo):
    while True:
        datos = {}
        for pid in listar_pids():
            info = leer_status(pid)
            if info is None:
                continue
            
            datos[pid] = {
                'blk': info.get('SigBlk', '0'),   # bloqueadas
                'ign': info.get('SigIgn', '0'),   # ignoradas
                'cgt': info.get('SigCgt', '0'),   # con handler propio
                'pnd': info.get('SigPnd', '0'),   # pendientes al proceso
                'shd_pnd': info.get('ShdPnd', '0'),  # pendientes al grupo
                'timestamp': time.time(),
            }
        
        try:
            snapshot['senales'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

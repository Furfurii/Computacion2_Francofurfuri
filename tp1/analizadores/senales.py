"""
analizadores/senales.py — Analizador de señales por proceso.
Guarda en snapshot['senales'] las máscaras hex de /proc/<pid>/status y CPU acumulada.
La decodificación a nombres (SIGINT, SIGTERM, ...) es responsabilidad de la vista.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_status, leer_stat, calcular_cpu_pct


def analizador_senales(snapshot, intervalo):
    historial_cpu = {}

    while True:
        ahora = time.time()
        historial_nuevo = {}

        datos = {}
        for pid in listar_pids():
            info = leer_status(pid)
            info_stat = leer_stat(pid)
            if info is None or info_stat is None:
                continue

            jiffies = info_stat['utime'] + info_stat['stime']
            cpu_pct = calcular_cpu_pct(historial_cpu, historial_nuevo, pid, jiffies, ahora)

            datos[pid] = {
                'blk': info.get('SigBlk', '0'),   # bloqueadas
                'ign': info.get('SigIgn', '0'),   # ignoradas
                'cgt': info.get('SigCgt', '0'),   # con handler propio
                'pnd': info.get('SigPnd', '0'),   # pendientes al proceso
                'shd_pnd': info.get('ShdPnd', '0'),  # pendientes al grupo
                'cpu': round(cpu_pct, 1),
                'timestamp': ahora,
            }

        historial_cpu = historial_nuevo

        try:
            snapshot['senales'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

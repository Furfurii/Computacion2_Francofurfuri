"""
analizadores/fds.py — Analizador de File Descriptors por proceso.
Guarda en snapshot['fds'] un dict {pid: {total, por_tipo, fds, cpu, timestamp}},
donde 'fds' es la lista completa {fd, destino, tipo} de cada descriptor abierto.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_fds, leer_stat, calcular_cpu_pct


def analizador_fds(snapshot, intervalo):
    historial_cpu = {}

    while True:
        ahora = time.time()
        historial_nuevo = {}

        datos = {}
        for pid in listar_pids():
            fds = leer_fds(pid)
            info_stat = leer_stat(pid)
            if fds is None or info_stat is None:
                continue  # sin permisos o proceso murió

            # Contar cuántos hay de cada tipo
            por_tipo = {}
            for fd in fds:
                tipo = fd['tipo']
                por_tipo[tipo] = por_tipo.get(tipo, 0) + 1

            jiffies = info_stat['utime'] + info_stat['stime']
            cpu_pct = calcular_cpu_pct(historial_cpu, historial_nuevo, pid, jiffies, ahora)

            datos[pid] = {
                'total': len(fds),
                'por_tipo': por_tipo,
                # Detalle completo: número de FD, a dónde apunta y su tipo.
                'fds': fds,
                'cpu': round(cpu_pct, 1),
                'timestamp': ahora,
            }

        historial_cpu = historial_nuevo

        try:
            snapshot['fds'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

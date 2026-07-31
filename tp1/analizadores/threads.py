"""
analizadores/threads.py — Analizador de threads (LWPs) por proceso.
Guarda en snapshot['threads'] un dict {pid: {cantidad, por_estado, cpu, timestamp}}.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_threads, leer_stat, calcular_cpu_pct


def analizador_threads(snapshot, intervalo):
    historial_cpu_pid = {}
    historial_cpu_tid = {}

    while True:
        ahora = time.time()
        historial_nuevo_pid = {}
        historial_nuevo_tid = {}

        datos = {}
        for pid in listar_pids():
            threads = leer_threads(pid)
            info_stat = leer_stat(pid)
            if threads is None or info_stat is None:
                continue

            # Contar por estado (R, S, D, T, Z, ...)
            por_estado = {}
            for t in threads:
                estado = t['estado']
                por_estado[estado] = por_estado.get(estado, 0) + 1

            jiffies_proceso = info_stat['utime'] + info_stat['stime']
            cpu_pct_proceso = calcular_cpu_pct(
                historial_cpu_pid, historial_nuevo_pid, pid, jiffies_proceso, ahora)

            detalle_threads = []
            for t in threads:
                jiffies_thread = t['utime'] + t['stime']
                cpu_pct_thread = calcular_cpu_pct(
                    historial_cpu_tid, historial_nuevo_tid, t['tid'], jiffies_thread, ahora)
                detalle_threads.append({
                    'tid': t['tid'],
                    'nombre': t['nombre'],
                    'estado': t['estado'],
                    'cpu': round(cpu_pct_thread, 1),
                    'ctx_vol': t['ctx_vol'],
                    'ctx_invol': t['ctx_invol'],
                })

            datos[pid] = {
                'cantidad': len(threads),
                'por_estado': por_estado,
                # Detalle por LWP: TID, nombre, estado, CPU% y context switches propios.
                'threads': detalle_threads,
                'cpu': round(cpu_pct_proceso, 1),
                'timestamp': ahora,
            }

        historial_cpu_pid = historial_nuevo_pid
        historial_cpu_tid = historial_nuevo_tid

        try:
            snapshot['threads'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

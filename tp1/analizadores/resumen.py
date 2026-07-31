"""
analizadores/resumen.py — Analizador de la vista Resumen.

Lee /proc para todos los procesos vivos y guarda un dict con
{pid: {nombre, estado, ppid, uid, usuario, threads, cpu}} en snapshot['resumen'].
'cpu' es el % de CPU real, calculado como delta de jiffies entre esta
lectura y la anterior (no jiffies acumulados desde que arrancó el proceso).
"""

import sys
import os
import time

# Agregamos el directorio padre al path para que Python encuentre procfs.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_status, leer_stat, calcular_cpu_pct, resolver_usuario


def analizador_resumen(snapshot, intervalo):
    """
    Bucle infinito que actualiza snapshot['resumen'] cada 'intervalo.value' segundos.
    """
    historial_cpu = {}  # pid -> (jiffies, timestamp) de la lectura anterior

    while True:
        ahora = time.time()
        historial_nuevo = {}

        # 1. Recolectar datos de todos los procesos
        datos = {}
        for pid in listar_pids():
            info = leer_status(pid)
            info_stat = leer_stat(pid)
            if info is None or info_stat is None:
                continue  # proceso murió, salteamos

            jiffies = info_stat['utime'] + info_stat['stime']
            cpu_pct = calcular_cpu_pct(historial_cpu, historial_nuevo, pid, jiffies, ahora)

            # Uid: "<real> <efectivo> <guardado> <filesystem>" — usamos el real.
            uid = info.get('Uid', '0').split()[0]

            datos[pid] = {
                'nombre': info.get('Name', ''),
                'estado': info.get('State', ''),
                'ppid': int(info.get('PPid', 0)),
                'uid': uid,
                'usuario': resolver_usuario(uid),
                'threads': int(info.get('Threads', 1)),
                'cpu': round(cpu_pct, 1),
                'timestamp': ahora,
            }

        historial_cpu = historial_nuevo

        # 2. Escribir en el snapshot (una sola asignación)
        try:
            snapshot['resumen'] = datos
        except (BrokenPipeError, EOFError):
            break

        # 3. Dormir el intervalo actual
        time.sleep(intervalo.value)

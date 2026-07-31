"""
analizadores/memoria.py — Analizador de la vista Memoria.

Lee /proc para todos los procesos vivos y guarda info de memoria
{pid: {rss, vsize, swap, vmdata, vmstk, vmexe, vmlib, minflt, majflt,
       heap_bytes, stack_bytes, lib_bytes}} en snapshot['memoria'].
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_status, leer_stat, leer_maps, calcular_cpu_pct


def analizador_memoria(snapshot, intervalo):
    """
    Bucle infinito que actualiza snapshot['memoria'] cada 'intervalo.value' segundos.
    """
    historial_cpu = {}

    while True:
        ahora = time.time()
        historial_nuevo = {}

        datos = {}
        for pid in listar_pids():
            info_status = leer_status(pid)
            info_stat = leer_stat(pid)
            info_maps = leer_maps(pid)

            # Si alguno de los archivos falló (proceso murió o sin permisos),
            # saltamos este PID entero
            if info_status is None or info_stat is None:
                continue

            jiffies = info_stat['utime'] + info_stat['stime']
            cpu_pct = calcular_cpu_pct(historial_cpu, historial_nuevo, pid, jiffies, ahora)

            datos[pid] = {
                # De status (memoria virtual y real del proceso, en kB)
                'rss': info_status.get('VmRSS', '0 kB'),
                'vsize': info_status.get('VmSize', '0 kB'),
                'swap': info_status.get('VmSwap', '0 kB'),
                'hwm': info_status.get('VmHWM', '0 kB'),  # high water mark de RSS
                'vmdata': info_status.get('VmData', '0 kB'),  # heap + datos
                'vmstk': info_status.get('VmStk', '0 kB'),    # stack
                'vmexe': info_status.get('VmExe', '0 kB'),    # código propio
                'vmlib': info_status.get('VmLib', '0 kB'),    # bibliotecas mapeadas

                # De stat (page faults acumulados)
                'minflt': info_stat['minflt'],
                'majflt': info_stat['majflt'],
                'cpu': round(cpu_pct, 1),

                # De maps (segmentos agrupados, solo si tenemos permisos)
                'heap_segs': info_maps['heap']['segmentos'] if info_maps else 0,
                'heap_bytes': info_maps['heap']['bytes'] if info_maps else 0,
                'stack_segs': info_maps['stack']['segmentos'] if info_maps else 0,
                'stack_bytes': info_maps['stack']['bytes'] if info_maps else 0,
                'lib_segs': info_maps['lib']['segmentos'] if info_maps else 0,
                'lib_bytes': info_maps['lib']['bytes'] if info_maps else 0,

                'timestamp': ahora,
            }

        historial_cpu = historial_nuevo

        try:
            snapshot['memoria'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

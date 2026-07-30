"""
analizadores/resumen.py — Analizador de la vista Resumen.

Lee /proc para todos los procesos vivos y guarda un dict con
{pid: {nombre, estado, ppid, threads}} en snapshot['resumen'].
"""

import sys
import os
import time

# Agregamos el directorio padre al path para que Python encuentre procfs.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_status


def analizador_resumen(snapshot, intervalo):
    """
    Bucle infinito que actualiza snapshot['resumen'] cada 'intervalo.value' segundos.
    """
    while True:
        # 1. Recolectar datos de todos los procesos
        datos = {}
        for pid in listar_pids():
            info = leer_status(pid)
            if info is None:
                continue  # proceso murió, salteamos
            
            datos[pid] = {
                'nombre': info.get('Name', ''),
                'estado': info.get('State', ''),
                'ppid': int(info.get('PPid', 0)),
                'threads': int(info.get('Threads', 1)),
                'timestamp': time.time(),
            }
        
        # 2. Escribir en el snapshot (una sola asignación)
        try:
            snapshot['resumen'] = datos
        except (BrokenPipeError, EOFError):
            break
        
        # 3. Dormir el intervalo actual
        time.sleep(intervalo.value)

"""
analizadores/sistema.py — Analizador de métricas globales del sistema.
Guarda en snapshot['sistema'] un dict con CPU global, memoria, load, uptime.
Este analizador NO itera PIDs — trabaja con datos agregados del sistema.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_meminfo, leer_stat_global, leer_status


def analizador_sistema(snapshot, intervalo):
    while True:
        stat_global = leer_stat_global()
        meminfo = leer_meminfo()
        
        # Load average (viene de /proc/loadavg)
        try:
            with open('/proc/loadavg', 'r') as f:
                partes = f.read().split()
                load_1m = float(partes[0])
                load_5m = float(partes[1])
                load_15m = float(partes[2])
        except FileNotFoundError:
            load_1m = load_5m = load_15m = 0.0
        
        # Uptime del sistema (viene de /proc/uptime)
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seg = float(f.read().split()[0])
        except FileNotFoundError:
            uptime_seg = 0
        
        # Contar procesos por estado (recorriendo status de cada uno)
        total = 0
        por_estado = {}
        for pid in listar_pids():
            info = leer_status(pid)
            if info is None:
                continue
            total += 1
            estado_letra = info.get('State', '?')[0]  # primer carácter: R/S/D/T/Z
            por_estado[estado_letra] = por_estado.get(estado_letra, 0) + 1
        
        datos = {
            'cpu': stat_global['cpu'] if stat_global else {},
            'cpus_por_core': stat_global.get('cpus', []) if stat_global else [],
            'ctxt_total': stat_global.get('ctxt', 0) if stat_global else 0,
            'btime': stat_global.get('btime', 0) if stat_global else 0,
            'uptime_seg': uptime_seg,
            'load_avg': {'1m': load_1m, '5m': load_5m, '15m': load_15m},
            'memoria_kb': {
                'total': int(meminfo['MemTotal'].split()[0]) if meminfo else 0,
                'libre': int(meminfo['MemFree'].split()[0]) if meminfo else 0,
                'disponible': int(meminfo['MemAvailable'].split()[0]) if meminfo else 0,
                'swap_total': int(meminfo['SwapTotal'].split()[0]) if meminfo else 0,
                'swap_libre': int(meminfo['SwapFree'].split()[0]) if meminfo else 0,
                'cached': int(meminfo['Cached'].split()[0]) if meminfo else 0,
                'buffers': int(meminfo['Buffers'].split()[0]) if meminfo else 0,
            },
            'procesos': {
                'total': total,
                'por_estado': por_estado,
            },
            'timestamp': time.time(),
        }
        
        try:
            snapshot['sistema'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

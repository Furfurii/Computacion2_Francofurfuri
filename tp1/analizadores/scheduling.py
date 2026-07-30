"""
analizadores/scheduling.py — Analizador de scheduling por proceso.
Guarda en snapshot['scheduling'] {pid: {nice, priority, policy, ctx_switches, ...}}.
Combina datos de leer_stat (nice, priority, policy) y leer_status (ctx switches).
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procfs import listar_pids, leer_stat, leer_status

# Mapeo del número de policy al nombre legible
POLICY_NAMES = {
    0: 'SCHED_OTHER',
    1: 'SCHED_FIFO',
    2: 'SCHED_RR',
    3: 'SCHED_BATCH',
    5: 'SCHED_IDLE',
}


def analizador_scheduling(snapshot, intervalo):
    while True:
        datos = {}
        for pid in listar_pids():
            info_stat = leer_stat(pid)
            info_status = leer_status(pid)
            if info_stat is None or info_status is None:
                continue
            
            policy_num = info_stat.get('policy', 0)
            
            datos[pid] = {
                'nice': info_stat.get('nice', 0),
                'priority': info_stat.get('priority', 0),
                'policy': POLICY_NAMES.get(policy_num, f'DESCONOCIDO({policy_num})'),
                'rt_priority': info_stat.get('rt_priority', 0),
                'ctx_vol': int(info_status.get('voluntary_ctxt_switches', 0)),
                'ctx_invol': int(info_status.get('nonvoluntary_ctxt_switches', 0)),
                'cpu_affinity': info_status.get('Cpus_allowed_list', ''),
                'timestamp': time.time(),
            }
        
        try:
            snapshot['scheduling'] = datos
        except (BrokenPipeError, EOFError):
            break
        time.sleep(intervalo.value)

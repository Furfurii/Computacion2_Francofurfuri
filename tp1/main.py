"""
main.py — Orquestador principal del monitor de procesos.

Responsabilidades:
1. Cargar configuración desde config.json.
2. Crear el snapshot compartido (Manager.dict).
3. Crear los Value de intervalos ajustables.
4. Registrar handlers de señales con self-pipe.
5. Lanzar los 7 analizadores y el display como procesos separados.
6. Loop principal: escuchar señales y coordinar acciones.
7. Shutdown limpio al salir.
"""

import os
import sys
import time
from multiprocessing import Manager, Process, Value

from analizadores.resumen import analizador_resumen
from analizadores.memoria import analizador_memoria
from analizadores.fds import analizador_fds
from analizadores.threads import analizador_threads
from analizadores.senales import analizador_senales
from analizadores.scheduling import analizador_scheduling
from analizadores.sistema import analizador_sistema
from display import display_main
import senales


def main():
    # 1. Cargar configuración
    config = senales.cargar_config('config.json')
    intervalos_cfg = config.get('intervalos', {})
    filtros_cfg = config.get('filtros', {})

    # 2. Instalar handlers de señales (self-pipe)
    pipe_fd = senales.instalar_handlers()

    with Manager() as manager:
        # 3. Snapshot compartido
        snapshot = manager.dict()
        control = manager.dict()   # {'salir': bool}
        control['salir'] = False
        # Filtros default: el display los aplica al arrancar y cada vez que
        # 'filtros_version' cambia (lo dispara un reload por SIGHUP).
        control['filtro_nombre_default'] = filtros_cfg.get('nombre', '')
        control['filtro_usuario_default'] = filtros_cfg.get('usuario', '')
        control['filtros_version'] = 0
        
        # 4. Values de intervalos ajustables
        intervalos = {
            'resumen':    Value('d', intervalos_cfg.get('resumen', 2)),
            'memoria':    Value('d', intervalos_cfg.get('memoria', 3)),
            'fds':        Value('d', intervalos_cfg.get('fds', 5)),
            'threads':    Value('d', intervalos_cfg.get('threads', 2)),
            'senales':    Value('d', intervalos_cfg.get('senales', 10)),
            'scheduling': Value('d', intervalos_cfg.get('scheduling', 10)),
            'sistema':    Value('d', intervalos_cfg.get('sistema', 2)),
        }
        
        # 5. Definir procesos hijos
        procesos = [
            Process(target=analizador_resumen,    args=(snapshot, intervalos['resumen']),    name='resumen'),
            Process(target=analizador_memoria,    args=(snapshot, intervalos['memoria']),    name='memoria'),
            Process(target=analizador_fds,        args=(snapshot, intervalos['fds']),        name='fds'),
            Process(target=analizador_threads,    args=(snapshot, intervalos['threads']),    name='threads'),
            Process(target=analizador_senales,    args=(snapshot, intervalos['senales']),    name='senales'),
            Process(target=analizador_scheduling, args=(snapshot, intervalos['scheduling']), name='scheduling'),
            Process(target=analizador_sistema,    args=(snapshot, intervalos['sistema']),    name='sistema'),
            Process(target=display_main,          args=(snapshot, intervalos, control),      name='display'),
        ]
        
        # 6. Arrancar todos
        for p in procesos:
            p.start()
        
        print(f"[main] Monitor arrancado con {len(procesos)} procesos. PID padre: {os.getpid()}")
        print("[main] Ctrl+C para salir. SIGUSR1 para dump. SIGHUP para reload.")
        time.sleep(2)
        
        # 7. Loop principal: leer del self-pipe y actuar
        try:
            while not control.get('salir', False):
                time.sleep(0.5)
                
                # ¿Llegó alguna señal?
                evento = senales.leer_evento()
                if evento == b'T':   # SIGINT o SIGTERM
                    print("\n[main] Terminate solicitado por señal.")
                    control['salir'] = True
                    break
                elif evento == b'R': # SIGHUP → reload config
                    print("\n[main] Reload de config solicitado.")
                    nueva_cfg = senales.cargar_config('config.json')
                    for nombre, val in nueva_cfg.get('intervalos', {}).items():
                        if nombre in intervalos:
                            intervalos[nombre].value = val
                    nuevos_filtros = nueva_cfg.get('filtros', {})
                    control['filtro_nombre_default'] = nuevos_filtros.get('nombre', '')
                    control['filtro_usuario_default'] = nuevos_filtros.get('usuario', '')
                    control['filtros_version'] = control['filtros_version'] + 1
                elif evento == b'D': # SIGUSR1 → dump
                    ruta = senales.dump_snapshot(snapshot)
                    print(f"\n[main] Snapshot dumpeado en {ruta}")
                elif evento == b'V': # SIGUSR2 → toggle verbose
                    v = senales.toggle_verbose()
                    print(f"\n[main] Verbose mode = {v}")
                elif evento == b'W': # SIGWINCH → resize
                    pass  # Rich maneja el resize solo
        except KeyboardInterrupt:
            pass
        
        # 8. Shutdown limpio
        print("\n[main] Terminando procesos hijos...")
        control['salir'] = True
        for p in procesos:
            p.terminate()
        for p in procesos:
            p.join(timeout=2)
        for p in procesos:
            if p.is_alive():
                p.kill()
        for p in procesos:
            p.join(timeout=1)
        print("[main] Adiós.")


if __name__ == '__main__':
    main()

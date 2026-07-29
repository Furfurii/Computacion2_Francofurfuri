import os

def listar_pids():
    pids = []
    for entrada in os.listdir('/proc'):
        if entrada.isdigit():
            pids.append(int(entrada))
    return pids

def leer_stat(pid):
    """
    Lee /proc/<pid>/stat y devuelve un dict con los campos parseados.
    Si el proceso ya no existe, devuelve None.
    """
    try:
        with open(f'/proc/{pid}/stat', 'r') as f:
            linea = f.read()
    except FileNotFoundError:
        return None
    
    # Separar la línea en tres zonas usando el último ')'
    corte = linea.rfind(')')
    pid_str = linea[:corte].split()[0]           # zona 1: el PID
    nombre = linea[1 + linea.find('('):corte]    # zona 2: el nombre (entre paréntesis)
    resto = linea[corte + 2:].split()            # zona 3: los 50 campos restantes
    
    # Armar el dict con los campos que nos importan
    datos = {
        'pid': int(pid_str),
        'nombre': nombre,
        'estado': resto[0],                       # campo 3 en la doc oficial
        'ppid': int(resto[1]),                    # campo 4
        'pgid': int(resto[2]),                    # campo 5 (grupo de procesos)
        'sid': int(resto[3]),                     # campo 6 (sesión)
        'utime': int(resto[11]),                  # campo 14 (tiempo en user mode)
        'stime': int(resto[12]),                  # campo 15 (tiempo en kernel mode)
        'nice': int(resto[16]),                   # campo 19
        'num_threads': int(resto[17]),            # campo 20
        'rss_paginas': int(resto[21]),            # campo 24 (memoria residente en páginas)
    }
    return datos
def leer_meminfo():
    """
    Lee /proc/meminfo y devuelve un dict con los campos parseados.
    Los valores están en kB (kilobytes).
    Si por algún motivo el archivo no existe, devuelve None.
    """
    datos = {}
    try:
        with open('/proc/meminfo', 'r') as f:
            for linea in f:
                if ':' in linea:
                    campo, valor = linea.split(':', 1)
                    datos[campo] = valor.strip()
    except FileNotFoundError:
        return None
    return datos
def leer_fds(pid):
    """
    Lee /proc/<pid>/fd/ y devuelve una lista de dicts, uno por FD abierto.
    Cada dict tiene: {'fd': número, 'destino': dónde apunta, 'tipo': categoría}.
    Si el proceso no existe o no tenemos permisos, devuelve None.
    """
    fds = []
    try:
        # os.listdir sobre /proc/<pid>/fd/ nos da los nombres de los symlinks (0, 1, 2, ...)
        for entrada in os.listdir(f'/proc/{pid}/fd'):
            fd_path = f'/proc/{pid}/fd/{entrada}'
            try:
                # os.readlink NO abre el archivo destino: solo lee A DÓNDE APUNTA el symlink.
                # Esto es clave: si el FD apunta a un socket, no queremos leer del socket;
                # solo queremos reportar que existe.
                destino = os.readlink(fd_path)
            except (FileNotFoundError, PermissionError):
                # El FD puede cerrarse entre listdir() y readlink() (race condition, otra vez).
                continue
            
            # Categorizamos según cómo empieza el destino del symlink.
            # Los prefijos "socket:", "pipe:", "anon_inode:" son convenciones del kernel.
            if destino.startswith('socket:'):
                tipo = 'socket'
            elif destino.startswith('pipe:'):
                tipo = 'pipe'
            elif destino.startswith('anon_inode:'):
                tipo = 'anon'
            elif destino.startswith('/dev/pts/') or destino.startswith('/dev/tty'):
                tipo = 'tty'
            elif destino.startswith('/dev/'):
                tipo = 'device'
            else:
                tipo = 'file'
            
            fds.append({
                'fd': int(entrada),
                'destino': destino,
                'tipo': tipo,
            })
    except (FileNotFoundError, PermissionError):
        # El proceso ya no existe, o no tenemos permisos para leer sus FDs
        # (los de root, por ejemplo, si vos no sos root).
        return None
    
    
    
    
    
    
    
    
    # Ordenamos por número de FD para que la vista sea estable (0, 1, 2, 3...)
    fds.sort(key=lambda x: x['fd'])
    return fds

def leer_maps(pid):
    """
    Lee /proc/<pid>/maps y devuelve un dict con segmentos agrupados por categoría.
    Categorías: 'heap', 'stack', 'text' (código ejecutable),
                'lib' (bibliotecas compartidas), 'anon' (memoria anónima), 'otro'.
    Cada categoría tiene: cantidad de segmentos y bytes totales.
    Devuelve None si el proceso no existe.
    """
    # Inicializamos el dict con todas las categorías en 0.
    # Esto es importante: si un proceso no tiene stack (raro pero posible),
    # la clave sigue estando con valor 0. Los consumidores no tienen que
    # chequear "¿existe esta clave?" antes de usarla.
    resumen = {
        'heap':  {'segmentos': 0, 'bytes': 0},
        'stack': {'segmentos': 0, 'bytes': 0},
        'text':  {'segmentos': 0, 'bytes': 0},
        'lib':   {'segmentos': 0, 'bytes': 0},
        'anon':  {'segmentos': 0, 'bytes': 0},
        'otro':  {'segmentos': 0, 'bytes': 0},
    }
    
    try:
        with open(f'/proc/{pid}/maps', 'r') as f:
            for linea in f:
                # Cada línea tiene 5 o 6 campos separados por espacios.
                # El último campo (opcional) es el nombre del archivo o [etiqueta].
                # Ejemplo: "55c1a2d4e000-55c1a2d51000 r--p 00000000 08:02 12345 /usr/bin/python3.11"
                partes = linea.split(maxsplit=5)
                if len(partes) < 5:
                    continue  # línea rara, la salteamos
                
                # Campo 1: "55c1a2d4e000-55c1a2d51000" → rango. Lo partimos por "-".
                rango = partes[0]
                inicio_str, fin_str = rango.split('-')
                # Las direcciones están en hexadecimal (base 16). int(x, 16) las convierte.
                tamanio = int(fin_str, 16) - int(inicio_str, 16)
                
                # Campo 2: permisos, ej "r-xp". La 'x' significa ejecutable.
                permisos = partes[1]
                
                # Campo 6 (si existe): nombre del archivo o etiqueta [stack], [heap], etc.
                nombre = partes[5].strip() if len(partes) >= 6 else ''
                
                # Clasificación por regla de prioridad.
                # El orden importa: primero chequeamos las etiquetas especiales del kernel.
                if nombre == '[heap]':
                    categoria = 'heap'
                elif nombre == '[stack]':
                    categoria = 'stack'
                elif 'x' in permisos and nombre and not nombre.startswith('['):
                    # Segmento ejecutable con nombre de archivo → es código de un binario o .so
                    categoria = 'text' if not nombre.endswith('.so') and '.so.' not in nombre else 'lib'
                elif nombre.endswith('.so') or '.so.' in nombre:
                    # Biblioteca compartida (código o datos de una .so)
                    categoria = 'lib'
                elif nombre == '' or nombre.startswith('['):
                    # Sin nombre o etiqueta especial no reconocida → memoria anónima
                    categoria = 'anon'
                else:
                    categoria = 'otro'
                
                resumen[categoria]['segmentos'] += 1
                resumen[categoria]['bytes'] += tamanio
    except (FileNotFoundError, PermissionError):
        return None
    
    return resumen

def leer_threads(pid):
    """
    Lee /proc/<pid>/task/ y devuelve la lista de threads del proceso.
    Cada thread es un dict con: {'tid': número, 'estado': letra, 'nombre': ...}
    En Linux, un thread es un "Light-Weight Process" (LWP) y tiene su propia
    entrada en /proc/<pid>/task/<tid>/, con archivos casi idénticos a los del proceso.
    """
    threads = []
    try:
        # /proc/<pid>/task/ contiene una carpeta por cada thread del proceso.
        # El nombre de la carpeta es el TID (Thread ID).
        for tid_str in os.listdir(f'/proc/{pid}/task'):
            if not tid_str.isdigit():
                continue
            tid = int(tid_str)
            
            # /proc/<pid>/task/<tid>/stat tiene el mismo formato que /proc/<pid>/stat
            # ¡Esto es GENIAL! Podemos reutilizar la lógica de parseo.
            try:
                with open(f'/proc/{pid}/task/{tid}/stat', 'r') as f:
                    linea = f.read()
                with open(f'/proc/{pid}/task/{tid}/comm', 'r') as f:
                    nombre = f.read().strip()
            except (FileNotFoundError, PermissionError):
                continue  # thread murió mientras iterábamos
            
            # Extraemos el estado del thread (mismo truco que leer_stat)
            corte = linea.rfind(')')
            resto = linea[corte + 2:].split()
            
            threads.append({
                'tid': tid,
                'estado': resto[0],       # R/S/D/T/Z, igual que para procesos
                'nombre': nombre,          # nombre del thread (puede ser distinto al del proceso)
                'utime': int(resto[11]),
                'stime': int(resto[12]),
            })
    except (FileNotFoundError, PermissionError):
        return None
    
    threads.sort(key=lambda x: x['tid'])
    return threads


def leer_stat_global():
    """
    Lee /proc/stat y devuelve el snapshot de CPU global y contadores del sistema.
    A diferencia de /proc/<pid>/stat, este es el archivo del SISTEMA completo.
    Devuelve un dict con: 'cpu' (jiffies totales por modo), 'ctxt' (context switches),
    'btime' (timestamp de boot), 'processes' (total de procesos desde boot).
    """
    datos = {'cpus': []}
    try:
        with open('/proc/stat', 'r') as f:
            for linea in f:
                partes = linea.split()
                if not partes:
                    continue
                
                clave = partes[0]
                
                # La primera línea "cpu" es el agregado de todos los cores.
                # Las siguientes "cpu0", "cpu1", etc son por-core.
                if clave == 'cpu':
                    # Los campos son (en jiffies): user, nice, system, idle, iowait,
                    # irq, softirq, steal, guest, guest_nice
                    datos['cpu'] = {
                        'user':    int(partes[1]),
                        'nice':    int(partes[2]),
                        'system':  int(partes[3]),
                        'idle':    int(partes[4]),
                        'iowait':  int(partes[5]) if len(partes) > 5 else 0,
                        'irq':     int(partes[6]) if len(partes) > 6 else 0,
                        'softirq': int(partes[7]) if len(partes) > 7 else 0,
                    }
                elif clave.startswith('cpu') and clave[3:].isdigit():
                    # cpu0, cpu1, cpu2... uno por core
                    datos['cpus'].append({
                        'core':   int(clave[3:]),
                        'user':   int(partes[1]),
                        'system': int(partes[3]),
                        'idle':   int(partes[4]),
                    })
                elif clave == 'ctxt':
                    datos['ctxt'] = int(partes[1])  # total context switches
                elif clave == 'btime':
                    datos['btime'] = int(partes[1])  # boot time (unix timestamp)
                elif clave == 'processes':
                    datos['processes'] = int(partes[1])  # procesos creados desde boot
                elif clave == 'procs_running':
                    datos['procs_running'] = int(partes[1])
                elif clave == 'procs_blocked':
                    datos['procs_blocked'] = int(partes[1])
    except FileNotFoundError:
        return None
    
    return datos





if __name__ == '__main__':
    print(f"Procesos vivos: {len(listar_pids())}")
    
    # PID 1 (systemd) es de root — probamos que el manejo defensivo anda
    fds_1 = leer_fds(1)
    print(f"FDs del PID 1: {len(fds_1) if fds_1 else 'sin permiso o proceso muerto'}")
    
    maps_1 = leer_maps(1)
    print(f"Memoria heap del PID 1: {maps_1['heap'] if maps_1 else 'sin permiso o proceso muerto'}")
    
    threads_1 = leer_threads(1)
    print(f"Threads del PID 1: {len(threads_1) if threads_1 else 'sin permiso'}")
    
    # Datos globales del sistema (no requieren permisos)
    mem = leer_meminfo()
    print(f"MemTotal: {mem['MemTotal']}")
    
    cpu = leer_stat_global()
    print(f"CPU global (user jiffies): {cpu['cpu']['user']}")
    print(f"Procesos corriendo ahora: {cpu.get('procs_running', 'N/A')}")
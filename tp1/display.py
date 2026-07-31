"""
display.py — Interfaz de texto (TUI) del monitor.

Corre como un proceso separado. Lee el snapshot compartido y dibuja
la vista activa. Escucha el teclado para cambiar de vista, filtrar,
ordenar, ajustar intervalos, y salir.

Vistas:
    1/r  Resumen        4/t  Threads      7/g  Sistema
    2/m  Memoria        5/s  Señales
    3/f  FDs            6/p  Scheduling

Teclas:
    ↑↓         Navegar procesos            c   Toggle orden (CPU/RSS/PID)
    Enter      Pin proceso seleccionado    +/- Intervalo +/-1
    /          Filtro por nombre           q   Salir
    u          Filtro por usuario          h/? Ayuda
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape

from procfs import decodificar_mascara_senales


# Mapa de teclas a vistas
TECLA_A_VISTA = {
    '1': 'resumen', 'r': 'resumen',
    '2': 'memoria', 'm': 'memoria',
    '3': 'fds',     'f': 'fds',
    '4': 'threads', 't': 'threads',
    '5': 'senales', 's': 'senales',
    '6': 'scheduling', 'p': 'scheduling',
    '7': 'sistema', 'g': 'sistema',
}

ORDENES = ['cpu', 'rss', 'pid']
PASO_INTERVALO = 0.5
INTERVALOS_MIN = {
    'resumen': 0.5,
    'memoria': 1.0,
    'fds': 2.0,
    'threads': 0.5,
    'senales': 5.0,
    'scheduling': 5.0,
    'sistema': 1.0,
}


def display_main(snapshot, intervalos, control):
    """
    Función principal del proceso display.
    
    snapshot   → Manager.dict() con los datos.
    intervalos → dict de Value para poder ajustar intervalos con +/-.
    control    → Manager.dict() para señalizar salida al padre.
    """
    console = Console()
    error_console = Console(stderr=True)
    old_settings = None
    teclado = None
    teclado_fd = None
    
    # Estado local del display (no compartido con otros procesos)
    estado = {
        'vista': 'resumen',
        'filtro_nombre': '',
        'filtro_usuario': '',
        'orden': 'pid',
        'pin': None,
        'scroll': 0,
        'selected_pid': None,
        'pids_actuales': [],
        'input_mode': None,
        'input_buffer': '',
        'ayuda': False,
        '_filtros_version_vista': -1,  # fuerza aplicar los defaults al arrancar
    }
    _aplicar_filtros_default(estado, control)

    try:
        import select

        try:
            # multiprocessing reemplaza sys.stdin por /dev/null en los hijos.
            # Abrimos la terminal controladora para que el display pueda leer teclas.
            teclado = open('/dev/tty', 'r', buffering=1)
        except OSError:
            teclado = sys.stdin if sys.stdin.isatty() else None

        teclado_habilitado = teclado is not None and teclado.isatty()
        if teclado_habilitado:
            import termios, tty
            # Poner terminal en modo cbreak para leer teclas sin Enter
            teclado_fd = teclado.fileno()
            old_settings = termios.tcgetattr(teclado_fd)
            tty.setcbreak(teclado_fd)
        else:
            error_console.print(
                "[display] stdin no es una TTY: teclas deshabilitadas. "
                "La vista queda en modo solo lectura.",
                markup=False,
            )

        eof_repetido = 0
        with Live(_dibujar(snapshot, estado, intervalos, console), console=console,
                  refresh_per_second=4, screen=sys.stdout.isatty()) as live:
            while not control.get('salir', False):
                # ¿SIGHUP recargó config.json? Aplicamos los nuevos filtros default.
                _aplicar_filtros_default(estado, control)

                # Leer teclado no bloqueante (100ms de timeout)
                if teclado_habilitado and select.select([teclado], [], [], 0.1)[0]:
                    tecla = _leer_tecla(teclado)
                    if tecla is None:
                        # select dijo "hay datos" pero read() devolvió EOF: el fd
                        # está roto (típico de /dev/tty mal armado dentro de un
                        # contenedor). Sin este freno, select seguiría "listo" al
                        # instante en cada vuelta y el loop giraría sin pausa,
                        # parpadeando la pantalla sin poder leer teclas reales.
                        eof_repetido += 1
                        time.sleep(0.05)
                        if eof_repetido >= 10:
                            teclado_habilitado = False
                            error_console.print(
                                "[display] /dev/tty dejó de responder (EOF): "
                                "paso a modo solo lectura.",
                                markup=False,
                            )
                    else:
                        eof_repetido = 0
                        _procesar_tecla(tecla, estado, intervalos, control)
                elif not teclado_habilitado:
                    time.sleep(0.25)

                # Refrescar la pantalla
                live.update(_dibujar(snapshot, estado, intervalos, console))
    except Exception as e:
        control['display_error'] = repr(e)
        error_console.print(f"[display] error: {e!r}", markup=False)
    finally:
        if old_settings is not None and teclado_fd is not None:
            try:
                termios.tcsetattr(teclado_fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        if teclado is not None and teclado is not sys.stdin:
            try:
                teclado.close()
            except Exception:
                pass


def _aplicar_filtros_default(estado, control):
    """
    Si 'filtros_version' en control cambió (arranque, o reload por SIGHUP),
    pisa los filtros activos con los defaults de config.json.
    """
    version = control.get('filtros_version', 0)
    if version == estado.get('_filtros_version_vista'):
        return
    estado['_filtros_version_vista'] = version
    estado['filtro_nombre'] = control.get('filtro_nombre_default', '')
    estado['filtro_usuario'] = control.get('filtro_usuario_default', '')
    estado['scroll'] = 0
    estado['selected_pid'] = None


def _procesar_tecla(tecla, estado, intervalos, control):
    """Actualiza el estado local según la tecla presionada."""
    if estado.get('input_mode'):
        _procesar_input(tecla, estado)
    elif tecla in TECLA_A_VISTA:
        estado['vista'] = TECLA_A_VISTA[tecla]
        estado['scroll'] = 0
    elif tecla == 'q':
        control['salir'] = True
    elif tecla in ('h', '?'):
        estado['ayuda'] = not estado.get('ayuda', False)
    elif tecla == '/':
        estado['input_mode'] = 'nombre'
        estado['input_buffer'] = estado.get('filtro_nombre', '')
    elif tecla == 'u':
        estado['input_mode'] = 'usuario'
        estado['input_buffer'] = estado.get('filtro_usuario', '')
    elif tecla in ('UP', 'DOWN'):
        _mover_seleccion(tecla, estado)
    elif tecla == 'ENTER':
        pid = estado.get('selected_pid')
        if pid is not None:
            estado['pin'] = None if estado.get('pin') == pid else pid
    elif tecla == 'c':
        idx = ORDENES.index(estado['orden'])
        estado['orden'] = ORDENES[(idx + 1) % len(ORDENES)]
        estado['scroll'] = 0
    elif tecla == '+':
        vista = estado['vista']
        if vista in intervalos:
            intervalos[vista].value = min(intervalos[vista].value + PASO_INTERVALO, 60)
    elif tecla == '-':
        vista = estado['vista']
        if vista in intervalos:
            minimo = INTERVALOS_MIN.get(vista, 1.0)
            intervalos[vista].value = max(intervalos[vista].value - PASO_INTERVALO, minimo)


def _leer_tecla(teclado):
    """
    Lee una tecla; traduce flechas, Enter y Escape a nombres estables.
    Devuelve None si la lectura vino vacía (EOF: el fd ya no entrega datos,
    p.ej. /dev/tty roto dentro de un contenedor) para que el loop principal
    pueda distinguirlo de "se apretó una tecla real" y no haga spin.
    """
    import select

    tecla = teclado.read(1)
    if tecla == '':
        return None
    if tecla in ('\r', '\n'):
        return 'ENTER'
    if tecla in ('\x7f', '\b'):
        return 'BACKSPACE'
    if tecla != '\x1b':
        return tecla

    secuencia = tecla
    # 1s, no 20ms: medido en la práctica (ver debug_teclado.py), el resto de
    # la secuencia (el '[' y la letra) a veces tarda cientos de ms — incluso
    # segundos, en un caso puntual — en llegar, por hiccups del terminal o
    # del sistema. Con una ventana corta, un toque real de flecha se leía
    # como un ESC suelto (sin efecto) seguido de '[' y letra sueltos (también
    # sin efecto). Una tecla Escape real es rarísima en esta app, así que
    # perder hasta 1s en detectarla como tal es un costo aceptable a cambio
    # de no perder toques de flecha reales.
    while select.select([teclado], [], [], 1.0)[0]:
        secuencia += teclado.read(1)
        if len(secuencia) >= 3:
            break
    # CSI ('\x1b[A') es lo más común; algunas terminales en "application cursor
    # keys mode" mandan SS3 ('\x1bOA') en su lugar. Aceptamos ambas.
    if secuencia in ('\x1b[A', '\x1bOA'):
        return 'UP'
    if secuencia in ('\x1b[B', '\x1bOB'):
        return 'DOWN'
    return 'ESC'


def _procesar_input(tecla, estado):
    """Edita el filtro activo hasta Enter o Escape."""
    if tecla == 'ENTER':
        valor = estado.get('input_buffer', '').strip()
        if estado['input_mode'] == 'nombre':
            estado['filtro_nombre'] = valor
        elif estado['input_mode'] == 'usuario':
            estado['filtro_usuario'] = valor
        estado['input_mode'] = None
        estado['input_buffer'] = ''
        estado['scroll'] = 0
        estado['selected_pid'] = None
    elif tecla == 'ESC':
        estado['input_mode'] = None
        estado['input_buffer'] = ''
    elif tecla == 'BACKSPACE':
        estado['input_buffer'] = estado.get('input_buffer', '')[:-1]
    elif len(tecla) == 1 and tecla.isprintable():
        estado['input_buffer'] = estado.get('input_buffer', '') + tecla


def _mover_seleccion(tecla, estado):
    pids = estado.get('pids_actuales', [])
    if not pids:
        return
    seleccionado = estado.get('selected_pid')
    try:
        idx = pids.index(seleccionado)
    except ValueError:
        idx = 0
    if tecla == 'UP':
        idx = max(0, idx - 1)
    else:
        idx = min(len(pids) - 1, idx + 1)
    estado['selected_pid'] = pids[idx]


def _dibujar(snapshot, estado, intervalos, console):
    """Construye el Layout completo con header, tabla y footer."""
    layout = Layout()
    layout.split_column(
        Layout(name='header', size=3),
        Layout(name='body'),
        Layout(name='footer', size=3),
    )

    # Header (3) + footer (3) le sacan 6 líneas al alto real de la terminal,
    # y la tabla de rich agrega otras 4 propias (borde superior, encabezado,
    # separador de encabezado, borde inferior) que no son filas de datos.
    # Sin restar esas 4, el scroll interno calculaba filas "visibles" que en
    # realidad Rich recortaba en silencio al no entrar en la terminal real.
    max_filas = max(5, console.size.height - 6 - 4)

    vista = estado['vista']
    intervalo_actual = intervalos[vista].value if vista in intervalos else '?'
    filtros = _texto_filtros(estado)
    pin = estado.get('pin')
    pin_txt = f" │ Pin: [cyan]{pin}[/]" if pin is not None else ""
    
    layout['header'].update(Panel(
        f"[bold cyan]Monitor de Procesos[/] │ Vista: [yellow]{vista}[/] │ "
        f"Intervalo: [green]{_formatear_intervalo(intervalo_actual)}s[/] │ "
        f"Orden: [magenta]{estado['orden']}[/]{pin_txt}{filtros}",
        style='blue',
    ))
    
    if estado.get('ayuda'):
        tabla = _render_ayuda()
    else:
        tabla = _render_vista(vista, snapshot, estado, max_filas)
    layout['body'].update(tabla)
    
    layout['footer'].update(Panel(
        _texto_footer(estado),
        style='blue',
    ))
    
    return layout


def _render_vista(vista, snapshot, estado, max_filas):
    """Devuelve un renderable de Rich para la vista pedida."""
    if vista not in snapshot:
        return Panel(f"[yellow]Esperando datos de '{vista}'...[/]")

    datos = dict(snapshot[vista])

    if vista == 'sistema':
        estado['pids_actuales'] = []
        return _render_sistema(datos)

    # El resto son tablas por PID
    tabla = Table(expand=True)
    pids = _pids_para_mostrar(datos, estado, max_filas)
    
    if vista == 'resumen':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Nombre", style="green")
        tabla.add_column("Estado")
        tabla.add_column("Usuario")
        tabla.add_column("PPID", justify="right")
        tabla.add_column("Threads", justify="right")
        tabla.add_column("CPU%", justify="right")
        for pid in pids:
            info = datos[pid]
            tabla.add_row(_marca_pid(pid, estado), info['nombre'], info['estado'],
                          info.get('usuario', ''), str(info['ppid']), str(info['threads']),
                          f"{info.get('cpu', 0):.1f}", style=_estilo_fila(pid, estado))
    
    elif vista == 'memoria':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("RSS", justify="right")
        tabla.add_column("VSize", justify="right")
        tabla.add_column("Swap", justify="right")
        tabla.add_column("MinFlt", justify="right")
        tabla.add_column("MajFlt", justify="right")
        tabla.add_column("Detalle (HWM · VmData/Stk/Exe/Lib · Heap/Stack/Lib)")
        seleccionado = estado.get('selected_pid')
        for pid in pids:
            info = datos[pid]
            tabla.add_row(_marca_pid(pid, estado), info['rss'], info['vsize'], info['swap'],
                          str(info['minflt']), str(info['majflt']), "",
                          style=_estilo_fila(pid, estado))
            if pid == seleccionado:
                detalle = (
                    f"HWM:{info['hwm']} | "
                    f"Data:{info['vmdata']} Stk:{info['vmstk']} "
                    f"Exe:{info['vmexe']} Lib:{info['vmlib']} | "
                    f"Heap:{info['heap_segs']}segs/{info['heap_bytes'] // 1024}KB "
                    f"Stack:{info['stack_segs']}segs/{info['stack_bytes'] // 1024}KB "
                    f"Lib:{info['lib_segs']}segs/{info['lib_bytes'] // 1024}KB"
                )
                tabla.add_row("  └─", "", "", "", "", "", escape(detalle), style='dim')
    
    elif vista == 'fds':
        tabla.add_column("PID / FD", justify="right", style="cyan")
        tabla.add_column("Total / Destino")
        tabla.add_column("Por tipo / Tipo")
        seleccionado = estado.get('selected_pid')
        for pid in pids:
            info = datos[pid]
            tipos_str = ', '.join(f"{k}:{v}" for k, v in info['por_tipo'].items())
            tabla.add_row(_marca_pid(pid, estado), str(info['total']), tipos_str,
                          style=_estilo_fila(pid, estado))
            if pid == seleccionado:
                for fd in info.get('fds', []):
                    tabla.add_row(
                        f"  └─{fd['fd']}", escape(fd['destino']), fd['tipo'],
                        style='dim',
                    )
    
    elif vista == 'threads':
        tabla.add_column("PID / TID", justify="right", style="cyan")
        tabla.add_column("Nombre")
        tabla.add_column("Estado")
        tabla.add_column("CPU%", justify="right")
        tabla.add_column("Ctx Vol", justify="right")
        tabla.add_column("Ctx Invol", justify="right")
        seleccionado = estado.get('selected_pid')
        for pid in pids:
            info = datos[pid]
            estados_str = ', '.join(f"{k}:{v}" for k, v in info['por_estado'].items())
            tabla.add_row(_marca_pid(pid, estado), f"{info['cantidad']} threads", estados_str,
                          f"{info['cpu']:.1f}", "", "",
                          style=_estilo_fila(pid, estado))
            if pid == seleccionado:
                for t in info.get('threads', []):
                    tabla.add_row(
                        f"  └─{t['tid']}", t['nombre'], t['estado'],
                        f"{t['cpu']:.1f}", str(t['ctx_vol']), str(t['ctx_invol']),
                        style='dim',
                    )
    
    elif vista == 'senales':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Bloqueadas (SigBlk)")
        tabla.add_column("Ignoradas (SigIgn)")
        tabla.add_column("Con handler (SigCgt)")
        tabla.add_column("Pendientes (SigPnd)")
        for pid in pids:
            info = datos[pid]
            tabla.add_row(
                _marca_pid(pid, estado),
                _texto_senales(info['blk']),
                _texto_senales(info['ign']),
                _texto_senales(info['cgt']),
                _texto_senales(info['pnd']),
                style=_estilo_fila(pid, estado),
            )
    
    elif vista == 'scheduling':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Nice", justify="right")
        tabla.add_column("Priority", justify="right")
        tabla.add_column("RT Prio", justify="right")
        tabla.add_column("Policy")
        tabla.add_column("Affinity")
        tabla.add_column("Ctx Vol", justify="right")
        tabla.add_column("Ctx Invol", justify="right")
        for pid in pids:
            info = datos[pid]
            tabla.add_row(_marca_pid(pid, estado), str(info['nice']), str(info['priority']),
                          str(info['rt_priority']), info['policy'],
                          escape(info.get('cpu_affinity', '')),
                          str(info['ctx_vol']), str(info['ctx_invol']),
                          style=_estilo_fila(pid, estado))
    
    return tabla


def _texto_senales(mascara_hex):
    """Decodifica una máscara hex de señales a nombres separados por coma."""
    nombres = decodificar_mascara_senales(mascara_hex)
    return ', '.join(nombres) if nombres else '-'


def _render_sistema(datos):
    """Vista Sistema: no es una tabla por PID sino info global."""
    mem = datos.get('memoria_kb', {})
    cpu = datos.get('cpu', {})
    load = datos.get('load_avg', {})
    procs = datos.get('procesos', {})
    
    texto = Text()
    texto.append("═══ CPU global (jiffies acumulados) ═══\n", style="bold cyan")
    for campo, valor in cpu.items():
        texto.append(f"  {campo}: {valor}\n")
    
    texto.append(f"\n═══ Memoria ═══\n", style="bold cyan")
    texto.append(f"  Total:      {mem.get('total', 0):>12} kB\n")
    texto.append(f"  Libre:      {mem.get('libre', 0):>12} kB\n")
    texto.append(f"  Disponible: {mem.get('disponible', 0):>12} kB\n")
    texto.append(f"  Cached:     {mem.get('cached', 0):>12} kB\n")
    texto.append(f"  Swap total: {mem.get('swap_total', 0):>12} kB\n")
    texto.append(f"  Swap libre: {mem.get('swap_libre', 0):>12} kB\n")
    
    texto.append(f"\n═══ Load average ═══\n", style="bold cyan")
    texto.append(f"  1min:  {load.get('1m', 0):.2f}    ")
    texto.append(f"5min:  {load.get('5m', 0):.2f}    ")
    texto.append(f"15min: {load.get('15m', 0):.2f}\n")
    
    texto.append(f"\n═══ Procesos ═══\n", style="bold cyan")
    texto.append(f"  Total: {procs.get('total', 0)}\n")
    texto.append(f"  Por estado: {procs.get('por_estado', {})}\n")
    
    texto.append(f"\n═══ Uptime ═══\n", style="bold cyan")
    uptime = datos.get('uptime_seg', 0)
    horas = int(uptime // 3600)
    minutos = int((uptime % 3600) // 60)
    texto.append(f"  {horas}h {minutos}m\n")
    
    return Panel(texto, title="Sistema", border_style="cyan")


def _pids_para_mostrar(datos, estado, max_filas):
    pids = _ordenar_pids(datos, estado['orden'])
    pids = _aplicar_filtros(pids, datos, estado)
    estado['pids_actuales'] = pids

    if not pids:
        estado['selected_pid'] = None
        estado['scroll'] = 0
        return []

    seleccionado = estado.get('selected_pid')
    if seleccionado not in pids:
        seleccionado = pids[0]
        estado['selected_pid'] = seleccionado

    idx = pids.index(seleccionado)
    scroll = estado.get('scroll', 0)
    if idx < scroll:
        scroll = idx
    elif idx >= scroll + max_filas:
        scroll = idx - max_filas + 1

    max_scroll = max(0, len(pids) - max_filas)
    scroll = max(0, min(scroll, max_scroll))
    estado['scroll'] = scroll

    visibles = pids[scroll:scroll + max_filas]
    pin = estado.get('pin')
    if pin in pids:
        if pin in visibles:
            visibles.remove(pin)
        visibles.insert(0, pin)
        visibles = visibles[:max_filas]
    return visibles


def _aplicar_filtros(pids, datos, estado):
    filtro_nombre = estado.get('filtro_nombre', '').lower()
    filtro_usuario = estado.get('filtro_usuario', '').lower()
    filtrados = []

    for pid in pids:
        if filtro_nombre and filtro_nombre not in _nombre_pid(pid, datos).lower():
            continue
        if filtro_usuario and filtro_usuario not in _usuario_pid(pid).lower():
            continue
        filtrados.append(pid)

    return filtrados


def _nombre_pid(pid, datos):
    info = datos.get(pid, {})
    nombre = info.get('nombre', '')

    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').decode(errors='ignore').strip()
            if cmdline:
                return cmdline
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass

    if nombre:
        return nombre

    try:
        with open(f'/proc/{pid}/comm', 'r') as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ''


def _usuario_pid(pid):
    try:
        with open(f'/proc/{pid}/status', 'r') as f:
            for linea in f:
                if linea.startswith('Uid:'):
                    uid = int(linea.split()[1])
                    break
            else:
                return ''
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
        return ''

    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _marca_pid(pid, estado):
    """
    Prefijo visible para la fila seleccionada. No depende de que el estilo
    'reverse' se note bien en una terminal/tema en particular: cambia el
    contenido de la celda, no solo su color.
    """
    return f"▶{pid}" if pid == estado.get('selected_pid') else str(pid)


def _estilo_fila(pid, estado):
    seleccionado = pid == estado.get('selected_pid')
    pineado = pid == estado.get('pin')
    if seleccionado and pineado:
        return 'bold reverse magenta'
    if seleccionado:
        return 'reverse'
    if pineado:
        return 'bold magenta'
    return None


def _render_ayuda():
    texto = Text()
    texto.append("Vistas\n", style="bold cyan")
    texto.append("  1/r resumen  2/m memoria  3/f fds  4/t threads\n")
    texto.append("  5/s senales  6/p scheduling  7/g sistema\n\n")
    texto.append("Navegacion\n", style="bold cyan")
    texto.append("  ↑/↓ seleccionar proceso    Enter pinear/despinear\n")
    texto.append("  c alternar orden CPU/RSS/PID    +/- ajustar intervalo\n\n")
    texto.append("Filtros y salida\n", style="bold cyan")
    texto.append("  / filtrar por comando    u filtrar por usuario\n")
    texto.append("  h/? ocultar ayuda        q salir limpiamente\n")
    return Panel(texto, title="Ayuda", border_style="cyan")


def _texto_filtros(estado):
    partes = []
    if estado.get('filtro_nombre'):
        partes.append(f"cmd={escape(estado['filtro_nombre'])}")
    if estado.get('filtro_usuario'):
        partes.append(f"user={escape(estado['filtro_usuario'])}")
    return f" │ Filtro: [cyan]{' '.join(partes)}[/]" if partes else ""


def _texto_footer(estado):
    if estado.get('input_mode'):
        etiqueta = 'comando' if estado['input_mode'] == 'nombre' else 'usuario'
        valor = escape(estado.get('input_buffer', ''))
        return f"[dim]Filtro por {etiqueta}: {valor} │ Enter aplicar │ Esc cancelar[/]"
    return (
        "[dim]1-7 vista │ ↑↓ navegar │ Enter pin │ / cmd │ u usuario │ "
        "c orden │ +/- intervalo │ h/? ayuda │ q salir[/]"
    )


def _formatear_intervalo(valor):
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    if valor.is_integer():
        return str(int(valor))
    return f"{valor:.1f}"


def _ordenar_pids(datos, orden):
    """Devuelve los PIDs ordenados según el criterio elegido."""
    if orden == 'pid':
        return sorted(datos.keys())
    elif orden == 'cpu':
        return sorted(
            datos.keys(),
            key=lambda pid: (-_float_o_cero(datos[pid].get('cpu', 0)), pid),
        )
    elif orden == 'rss':
        def rss_int(pid):
            rss_str = datos[pid].get('rss', '0 kB')
            try:
                return int(rss_str.split()[0])
            except (ValueError, IndexError):
                return 0
        return sorted(datos.keys(), key=lambda pid: (-rss_int(pid), pid))
    else:
        return sorted(datos.keys())


def _float_o_cero(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0

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
    }
    
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

        with Live(_dibujar(snapshot, estado, intervalos), console=console,
                  refresh_per_second=4, screen=sys.stdout.isatty()) as live:
            while not control.get('salir', False):
                # Leer teclado no bloqueante (100ms de timeout)
                if teclado_habilitado and select.select([teclado], [], [], 0.1)[0]:
                    tecla = teclado.read(1)
                    _procesar_tecla(tecla, estado, intervalos, control)
                elif not teclado_habilitado:
                    time.sleep(0.25)
                
                # Refrescar la pantalla
                live.update(_dibujar(snapshot, estado, intervalos))
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


def _procesar_tecla(tecla, estado, intervalos, control):
    """Actualiza el estado local según la tecla presionada."""
    if tecla in TECLA_A_VISTA:
        estado['vista'] = TECLA_A_VISTA[tecla]
    elif tecla == 'q':
        control['salir'] = True
    elif tecla == 'c':
        idx = ORDENES.index(estado['orden'])
        estado['orden'] = ORDENES[(idx + 1) % len(ORDENES)]
    elif tecla == '+':
        vista = estado['vista']
        if vista in intervalos:
            intervalos[vista].value = min(intervalos[vista].value + 1, 60)
    elif tecla == '-':
        vista = estado['vista']
        if vista in intervalos:
            intervalos[vista].value = max(intervalos[vista].value - 1, 1)


def _dibujar(snapshot, estado, intervalos):
    """Construye el Layout completo con header, tabla y footer."""
    layout = Layout()
    layout.split_column(
        Layout(name='header', size=3),
        Layout(name='body'),
        Layout(name='footer', size=3),
    )
    
    vista = estado['vista']
    intervalo_actual = intervalos[vista].value if vista in intervalos else '?'
    
    layout['header'].update(Panel(
        f"[bold cyan]Monitor de Procesos[/] │ Vista: [yellow]{vista}[/] │ "
        f"Intervalo: [green]{intervalo_actual}s[/] │ Orden: [magenta]{estado['orden']}[/]",
        style='blue',
    ))
    
    # Renderizar la vista activa
    tabla = _render_vista(vista, snapshot, estado)
    layout['body'].update(tabla)
    
    layout['footer'].update(Panel(
        "[dim]1-7: cambiar vista │ +/-: intervalo │ c: orden │ q: salir[/]",
        style='blue',
    ))
    
    return layout


def _render_vista(vista, snapshot, estado):
    """Devuelve un renderable de Rich para la vista pedida."""
    if vista not in snapshot:
        return Panel(f"[yellow]Esperando datos de '{vista}'...[/]")
    
    datos = dict(snapshot[vista])
    
    if vista == 'sistema':
        return _render_sistema(datos)
    
    # El resto son tablas por PID
    tabla = Table(expand=True)
    
    if vista == 'resumen':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Nombre", style="green")
        tabla.add_column("Estado")
        tabla.add_column("PPID", justify="right")
        tabla.add_column("Threads", justify="right")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            tabla.add_row(str(pid), info['nombre'], info['estado'],
                          str(info['ppid']), str(info['threads']))
    
    elif vista == 'memoria':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("RSS", justify="right")
        tabla.add_column("VSize", justify="right")
        tabla.add_column("Swap", justify="right")
        tabla.add_column("MinFlt", justify="right")
        tabla.add_column("MajFlt", justify="right")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            tabla.add_row(str(pid), info['rss'], info['vsize'], info['swap'],
                          str(info['minflt']), str(info['majflt']))
    
    elif vista == 'fds':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Total", justify="right")
        tabla.add_column("Por tipo")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            tipos_str = ', '.join(f"{k}:{v}" for k, v in info['por_tipo'].items())
            tabla.add_row(str(pid), str(info['total']), tipos_str)
    
    elif vista == 'threads':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Cantidad", justify="right")
        tabla.add_column("Por estado")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            estados_str = ', '.join(f"{k}:{v}" for k, v in info['por_estado'].items())
            tabla.add_row(str(pid), str(info['cantidad']), estados_str)
    
    elif vista == 'senales':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("SigBlk (hex)")
        tabla.add_column("SigIgn (hex)")
        tabla.add_column("SigCgt (hex)")
        tabla.add_column("SigPnd (hex)")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            tabla.add_row(str(pid), info['blk'], info['ign'], info['cgt'], info['pnd'])
    
    elif vista == 'scheduling':
        tabla.add_column("PID", justify="right", style="cyan")
        tabla.add_column("Nice", justify="right")
        tabla.add_column("Priority", justify="right")
        tabla.add_column("Policy")
        tabla.add_column("Ctx Vol", justify="right")
        tabla.add_column("Ctx Invol", justify="right")
        for pid in _ordenar_pids(datos, estado['orden']):
            info = datos[pid]
            tabla.add_row(str(pid), str(info['nice']), str(info['priority']),
                          info['policy'], str(info['ctx_vol']), str(info['ctx_invol']))
    
    return tabla


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


def _ordenar_pids(datos, orden):
    """Devuelve los PIDs ordenados según el criterio elegido."""
    if orden == 'pid':
        return sorted(datos.keys())
    elif orden == 'rss':
        def rss_int(pid):
            rss_str = datos[pid].get('rss', '0 kB')
            try:
                return int(rss_str.split()[0])
            except (ValueError, IndexError):
                return 0
        return sorted(datos.keys(), key=rss_int, reverse=True)
    else:
        return sorted(datos.keys())

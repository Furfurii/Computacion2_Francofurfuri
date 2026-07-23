import os
import sys

def procesar_archivo(archivo):
    """El hijo procesa un archivo y reporta éxito/fallo via código de salida."""
    pid = os.fork()

    if pid == 0:
        try:
            with open(archivo) as f:
                lineas = len(f.readlines())
            print(f"{archivo}: {lineas} líneas")
            os._exit(0)  # Éxito
        except Exception as e:
            print(f"Error procesando {archivo}: {e}")
            os._exit(1)  # Fallo
    else:
        _, status = os.wait()
        return os.WEXITSTATUS(status) == 0

# Procesar varios archivos
archivos = ["/etc/passwd", "/etc/no_existe", "/etc/hosts"]
for archivo in archivos:
    if procesar_archivo(archivo):
        print(f"  ✓ {archivo} procesado correctamente")
    else:
        print(f"  ✗ {archivo} falló")
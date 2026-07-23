import threading
import time

def tarea(nombre, duracion):
    print (f"")
    print(f"[{nombre}] Iniciando...")
    time.sleep(duracion)
    print(f"[{nombre}] Terminado!")

# Crear threads
t1 = threading.Thread(target=tarea, args=("Thread-1", 2))
t2 = threading.Thread(target=tarea, args=("Thread-2", 1))

# Iniciar threads
t1.start()
t2.start()

print("[Main] Threads iniciados")

# Esperar a que terminen
t1.join()
t2.join()

print("[Main] Todos los threads terminaron")
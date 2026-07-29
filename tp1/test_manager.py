from multiprocessing import Manager, Process
import time

def escritor(snapshot, nombre):
    """Simula un analizador que escribe en el snapshot cada 1 segundo."""
    for i in range(5):
        snapshot[nombre] = {'valor': i, 'timestamp': time.time()}
        print(f"[{nombre}] escribió valor {i}")
        time.sleep(1)

def lector(snapshot):
    """Simula un display que lee el snapshot cada 0.5 segundos."""
    for _ in range(10):
        # Copiamos a un dict normal para imprimir prolijito
        estado = dict(snapshot)
        print(f"[DISPLAY] snapshot = {estado}")
        time.sleep(0.5)

if __name__ == '__main__':
    # Creamos el Manager y el snapshot compartido
    with Manager() as manager:
        snapshot = manager.dict()
        
        # Lanzamos 2 escritores + 1 lector
        procesos = [
            Process(target=escritor, args=(snapshot, 'resumen')),
            Process(target=escritor, args=(snapshot, 'memoria')),
            Process(target=lector, args=(snapshot,)),
        ]
        
        for p in procesos:
            p.start()
        for p in procesos:
            p.join()
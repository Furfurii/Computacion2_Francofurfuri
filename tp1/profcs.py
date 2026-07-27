import os

def listar_pids():
    pids = []
    for entrada in os.listdir('/proc'):
        if entrada.isdigit():
            pids.append(int(entrada))
    return pids

def leer_status(pid):
    datos = {}
    try:
        with open(f'/proc/{pid}/status', 'r') as f:
            for linea in f:
                if ':' in linea:
                    campo, valor = linea.split(':', 1)
                    datos[campo] = valor.strip()
    except FileNotFoundError:
        return None
    return datos

if __name__ == '__main__':
    info = None
    print(info['State'])
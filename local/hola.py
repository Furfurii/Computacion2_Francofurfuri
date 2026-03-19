#!/usr/bin/env python3
"""Script de prueba para Docker."""

import os
import sys
from datetime import datetime

print("="*50)
print("Ejecutando en Docker")
print("="*50)
print(f"Python: {sys.version}")
print(f"Sistema: {os.uname().sysname}")
print(f"Hostname: {os.uname().nodename}")
print(f"Fecha: {datetime.now().isoformat()}")
print(f"Usuario: {os.getenv('USER', 'desconocido')}")
print(f"Directorio actual: {os.getcwd()}")
print(f"Archivos aquí: {os.listdir('.')}")
print("="*50)
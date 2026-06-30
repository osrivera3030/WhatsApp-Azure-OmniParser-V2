"""Diagnóstico — ejecuta en el mismo venv que dashboard.py"""
import sys, pathlib, traceback

LOG = pathlib.Path(__file__).parent / "diag_output.txt"
lines = []

def p(msg):
    print(msg)
    lines.append(msg)

p("=== INICIO ===")
p("Python: " + sys.version)
p("CWD: " + str(pathlib.Path.cwd()))
p("__file__: " + str(pathlib.Path(__file__).resolve()))

# 1. tkinter
try:
    import tkinter as tk
    p("tkinter OK — version: " + str(tk.TkVersion))
except Exception:
    p("ERROR tkinter:\n" + traceback.format_exc())

# 2. config
try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from config import AZURE_CONNECTION_STRING, TABLAS, get_logger
    p("config OK — TABLAS: " + str(list(TABLAS.keys())))
except Exception:
    p("ERROR config:\n" + traceback.format_exc())

# 3. azure
try:
    from azure.data.tables import TableClient
    p("azure-data-tables OK")
except Exception:
    p("ERROR azure:\n" + traceback.format_exc())

# 4. ventana simple
try:
    root = tk.Tk()
    root.title("TEST")
    root.geometry("300x100")
    tk.Label(root, text="Si ves esto, tkinter funciona.\nCierra esta ventana.").pack(pady=20)
    p("Abriendo ventana de prueba...")
    LOG.write_text("\n".join(lines), encoding="utf-8")
    root.mainloop()
    p("Ventana cerrada OK")
except Exception:
    p("ERROR ventana:\n" + traceback.format_exc())

LOG.write_text("\n".join(lines), encoding="utf-8")
p("=== FIN === Log en: " + str(LOG))

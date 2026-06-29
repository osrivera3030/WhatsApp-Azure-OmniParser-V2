"""
dashboard.py - Panel de control unificado solo_nube.
"""
from __future__ import annotations
import logging, os, queue, subprocess, sys, threading, time
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from azure.data.tables import TableClient, UpdateMode

sys.path.append(str(Path(__file__).resolve().parent))
from config import AZURE_CONNECTION_STRING, TABLAS, get_logger

logger = get_logger(__name__)
ROOT_DIR = Path(__file__).resolve().parent

# Columnas: (key, encabezado, ancho, editable)
COLUMNAS = [
    ("ClienteID",          "ID Cliente",      100, False),
    ("phone",              "Telefono",        120, False),
    ("name",               "Nombre",          160, True),
    ("Respuesta_Recibida", "Respuesta",        90, True),
    ("Texto_Respuesta",    "Texto respuesta", 260, True),
    ("fecha_envio_msg",    "Fecha envio msg", 130, False),
    ("Fecha_Verificacion", "Fecha procesado", 150, False),
    ("mensaje_enviado",    "Mensaje enviado", 200, True),
]
EDITABLE_COLS = {c[0] for c in COLUMNAS if c[3]}
COL_KEYS = [c[0] for c in COLUMNAS]

# Pasos: (label, script, necesita_wtp, intervalo_min_default)
PASOS = [
    ("1 Limpieza datos",      "1-Data_cleaning/Data_cleaning.py",             False, 1440),
    ("2 Validar WhatsApp",    "2-Validation_wtp/validacion_st.py",            True,  1440),
    ("3 Enviar mensajes",     "3-Envio_mjs/envio_stg.py",                     True,   120),
    ("4 Capturar respuestas", "4-Guarda_mjs_recep/procesar_respuestas_v2.py", True,    60),
    ("5 Enviar respuesta",    "5-envio_mjs_repuesta/res_final.py",            True,   120),
    ("6 Archivar/resetear",   "6-Data_storege/respuesta_final.py",            False, 21600),
]

BG_MAIN  = "#1e1e2e"
BG_LEFT  = "#181825"
BG_CARD  = "#242436"
BG_CELL  = "#313244"
FG_MAIN  = "#cdd6f4"
FG_ACC   = "#cba6f7"
FG_GRN   = "#a6e3a1"
FG_RED   = "#f38ba8"
FG_YEL   = "#f9e2af"
FG_DIM   = "#6c7086"


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    def emit(self, record):
        self.q.put(self.format(record))


class AzureManager:
    def __init__(self):
        self.client = TableClient.from_connection_string(AZURE_CONNECTION_STRING, TABLAS["directorio"])
    def cargar(self):
        ents = list(self.client.list_entities())
        for i, e in enumerate(ents, 1):
            if not e.get("ClienteID"):
                e["ClienteID"] = "CLI-{:04d}".format(i)
        return ents
    def guardar(self, entity):
        self.client.update_entity(mode=UpdateMode.MERGE, entity=entity)


class CellEditor:
    def __init__(self, tree, on_commit):
        self.tree = tree; self.on_commit = on_commit
        self._entry = self._item = self._col = None
    def abrir(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell": return
        col_id = self.tree.identify_column(event.x)
        item   = self.tree.identify_row(event.y)
        if not item: return
        idx = int(col_id.replace("#","")) - 1
        if COLUMNAS[idx][0] not in EDITABLE_COLS: return
        bbox = self.tree.bbox(item, col_id)
        if not bbox: return
        x, y, w, h = bbox
        self.cerrar()
        self._item = item; self._col = col_id
        self._entry = tk.Entry(self.tree, font=("Segoe UI", 9), bg="#45475a", fg=FG_MAIN, relief="flat")
        self._entry.insert(0, self.tree.set(item, col_id))
        self._entry.select_range(0, tk.END)
        self._entry.place(x=x, y=y, width=w, height=h)
        self._entry.focus_set()
        self._entry.bind("<Return>",   self._ok)
        self._entry.bind("<Escape>",   lambda e: self.cerrar())
        self._entry.bind("<FocusOut>", self._ok)
    def _ok(self, _=None):
        if not self._entry: return
        val = self._entry.get()
        idx = int(self._col.replace("#","")) - 1
        self.tree.set(self._item, self._col, val)
        self.on_commit(self._item, COLUMNAS[idx][0], val)
        self.cerrar()
    def cerrar(self):
        if self._entry: self._entry.destroy(); self._entry = None


class Scheduler:
    def __init__(self, on_run, on_log):
        self.on_run = on_run; self.on_log = on_log
        self._run = False; self._t = None
        # interval ahora en segundos
        self.states = {i: {"active": False, "interval": PASOS[i][3]*60, "last": None} for i in range(len(PASOS))}
    def start(self):
        if self._run: return
        self._run = True
        self._t = threading.Thread(target=self._loop, daemon=True); self._t.start()
        self.on_log("Automatizacion iniciada")
    def stop(self):
        self._run = False; self.on_log("Automatizacion detenida")
    def mark_done(self, idx):
        self.states[idx]["last"] = datetime.now()
    def next_str(self, idx):
        s = self.states[idx]
        if not s["active"]: return "--"
        if s["last"] is None: return "ahora"
        diff = s["last"] + timedelta(seconds=s["interval"]) - datetime.now()
        sec = int(diff.total_seconds())
        if sec <= 0: return "ahora"
        d, r = divmod(sec, 86400); h, r2 = divmod(r, 3600); m, s2 = divmod(r2, 60)
        if d: return "{}d {}h".format(d, h)
        if h: return "{}h {}m".format(h, m)
        if m: return "{}m {}s".format(m, s2)
        return "{}s".format(s2)
    def _loop(self):
        while self._run:
            now = datetime.now()
            for i in range(len(PASOS)):
                s = self.states[i]
                if not s["active"]: continue
                if s["last"] is None or (now - s["last"]).total_seconds() >= s["interval"]:
                    self.on_log("Auto: {}".format(PASOS[i][0]))
                    self.mark_done(i)
                    self.on_run(PASOS[i][1], PASOS[i][2])
                    time.sleep(5)
            time.sleep(30)


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.lq = queue.Queue()
        self.az = AzureManager()
        self.ents = {}
        self._busy = False
        self._wtp  = False
        self.driver = None
        self.sched = Scheduler(self._run_paso, self._log)
        self._setup_win()
        self._build_ui()
        self._attach_log()
        self._load_table()
        self._poll_logs()
        self._poll_sched()

    def _setup_win(self):
        self.root.title("Panel de Control - solo_nube")
        self.root.geometry("1380x820")
        self.root.minsize(1000, 640)
        self.root.configure(bg=BG_MAIN)
        s = ttk.Style(); s.theme_use("clam")
        s.configure("Treeview", background="#2a2a3e", foreground=FG_MAIN,
                    fieldbackground="#2a2a3e", rowheight=26, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background=BG_CELL, foreground=FG_ACC,
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected","#45475a")])

    def _build_ui(self):
        m = tk.Frame(self.root, bg=BG_MAIN); m.pack(fill="both", expand=True, padx=10, pady=10)
        left = tk.Frame(m, bg=BG_LEFT, width=270); left.pack(side="left", fill="y", padx=(0,8)); left.pack_propagate(False)
        right = tk.Frame(m, bg=BG_MAIN); right.pack(side="left", fill="both", expand=True)
        self._left_panel(left)
        self._right_panel(right)

    # ─── PANEL IZQUIERDO ───────────────────────────────────────────────────
    def _left_panel(self, p):
        cv = tk.Canvas(p, bg=BG_LEFT, highlightthickness=0)
        sb = ttk.Scrollbar(p, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); cv.pack(side="left", fill="both", expand=True)
        f = tk.Frame(cv, bg=BG_LEFT); win = cv.create_window((0,0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(win, width=e.width))

        def lbl(txt, fg=FG_MAIN, font=("Segoe UI", 8), **kw):
            return tk.Label(f, text=txt, bg=BG_LEFT, fg=fg, font=font, **kw)

        # ── Titulo ──
        lbl("solo_nube", fg=FG_ACC, font=("Segoe UI", 12, "bold")).pack(pady=(14,1), padx=10, anchor="w")
        lbl("Panel de control de pipeline", fg=FG_DIM).pack(padx=10, anchor="w", pady=(0,8))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=4)

        # ── WhatsApp ──
        lbl("SESION WHATSAPP", fg=FG_ACC, font=("Segoe UI", 8, "bold")).pack(padx=10, anchor="w", pady=(6,2))
        lbl("Se abre una sola vez y permanece activa", fg=FG_DIM, font=("Segoe UI", 7)).pack(padx=10, anchor="w", pady=(0,4))
        self.btn_wtp = tk.Button(f, text="Abrir WhatsApp Web", bg=FG_GRN, fg="#1e1e2e",
                                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=6,
                                  command=self._open_wtp)
        self.btn_wtp.pack(fill="x", padx=10, pady=(0,4))
        self.lbl_wtp = lbl("Estado: no iniciado", fg=FG_RED)
        self.lbl_wtp.pack(padx=10, anchor="w", pady=(0,6))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=4)

        # ── Config envio ──
        lbl("CONFIGURACION DE ENVIO", fg=FG_ACC, font=("Segoe UI", 8, "bold")).pack(padx=10, anchor="w", pady=(6,4))

        row1 = tk.Frame(f, bg=BG_LEFT); row1.pack(fill="x", padx=10, pady=2)
        tk.Label(row1, text="Max mensajes:", bg=BG_LEFT, fg=FG_MAIN, font=("Segoe UI", 8)).pack(side="left")
        self.spin_cant = tk.Spinbox(row1, from_=1, to=500, width=5, bg=BG_CELL, fg=FG_MAIN,
                                     buttonbackground="#45475a", font=("Segoe UI", 8))
        self.spin_cant.delete(0,"end"); self.spin_cant.insert(0,"10")
        self.spin_cant.pack(side="left", padx=(4,0))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=8)

        # ── Pasos ──
        lbl("PASOS DEL PIPELINE", fg=FG_ACC, font=("Segoe UI", 8, "bold")).pack(padx=10, anchor="w", pady=(0,2))
        lbl("Ejecuta manualmente o activa Auto con intervalo", fg=FG_DIM, font=("Segoe UI", 7)).pack(padx=10, anchor="w", pady=(0,6))

        self.btns = []; self.chks = []; self.spins = []; self.lbls_next = []

        for i in range(len(PASOS)):
            label, script, wtp, interval_min = PASOS[i]
            card = tk.Frame(f, bg=BG_CARD, padx=6, pady=5); card.pack(fill="x", padx=8, pady=3)

            # Cabecera
            head = tk.Frame(card, bg=BG_CARD); head.pack(fill="x")
            badge = "[WTP]" if wtp else "[LOC]"
            bcol  = "#89b4fa" if wtp else "#a6e3a1"
            tk.Label(head, text=badge, bg=BG_CARD, fg=bcol, font=("Segoe UI", 7, "bold")).pack(side="left")
            tk.Label(head, text=" "+label, bg=BG_CARD, fg=FG_MAIN, font=("Segoe UI", 8, "bold")).pack(side="left")

            # Boton ejecutar
            btn = tk.Button(card, text="▶ Ejecutar ahora", bg=BG_CELL, fg=FG_MAIN,
                            font=("Segoe UI", 8), relief="flat", cursor="hand2", pady=3,
                            command=lambda s=script, w=wtp: self._run_paso(s, w))
            btn.pack(fill="x", pady=(4,2))
            self.btns.append(btn)

            # Fila auto: checkbox + d/h/m/s
            # Convertir intervalo default (minutos) a d/h/m/s
            total_s  = interval_min * 60
            d_def    = total_s // 86400
            h_def    = (total_s % 86400) // 3600
            m_def    = (total_s % 3600) // 60
            s_def    = total_s % 60

            var = tk.BooleanVar(value=False); self.chks.append(var)
            row0 = tk.Frame(card, bg=BG_CARD); row0.pack(fill="x", pady=(2,0))
            tk.Checkbutton(row0, text="Auto cada:", variable=var, bg=BG_CARD, fg=FG_GRN,
                           selectcolor=BG_CELL, activebackground=BG_CARD, font=("Segoe UI", 7),
                           command=lambda idx=i, v=var: self._toggle(idx, v)).pack(side="left")
            ln = tk.Label(row0, text="prox: --", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7))
            ln.pack(side="right"); self.lbls_next.append(ln)

            # Fila d/h/m/s
            row1 = tk.Frame(card, bg=BG_CARD); row1.pack(fill="x")
            def _mk_sp(parent, maxv, defv, idx=i):
                sp = tk.Spinbox(parent, from_=0, to=maxv, width=3, bg=BG_CELL, fg=FG_MAIN,
                                buttonbackground="#45475a", font=("Segoe UI", 7),
                                command=lambda ii=idx: self._upd_interval(ii))
                sp.delete(0,"end"); sp.insert(0, str(defv))
                sp.bind("<FocusOut>", lambda e, ii=idx: self._upd_interval(ii))
                return sp

            sp_d = _mk_sp(row1, 365, d_def)
            sp_d.pack(side="left")
            tk.Label(row1, text="d", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7)).pack(side="left", padx=(1,4))
            sp_h = _mk_sp(row1, 23, h_def)
            sp_h.pack(side="left")
            tk.Label(row1, text="h", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7)).pack(side="left", padx=(1,4))
            sp_m = _mk_sp(row1, 59, m_def)
            sp_m.pack(side="left")
            tk.Label(row1, text="m", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7)).pack(side="left", padx=(1,4))
            sp_s = _mk_sp(row1, 59, s_def)
            sp_s.pack(side="left")
            tk.Label(row1, text="s", bg=BG_CARD, fg=FG_DIM, font=("Segoe UI", 7)).pack(side="left", padx=(1,0))
            self.spins.append((sp_d, sp_h, sp_m, sp_s))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=8)

        # ── Boton Ejecutar Todo ──
        lbl("EJECUCION RAPIDA", fg=FG_ACC, font=("Segoe UI", 8, "bold")).pack(padx=10, anchor="w", pady=(0,4))
        lbl("Ejecuta todos los pasos activos en orden", fg=FG_DIM, font=("Segoe UI", 7)).pack(padx=10, anchor="w", pady=(0,4))

        self.btn_all = tk.Button(f, text="EJECUTAR TODO (pasos activos)",
                                  bg="#89b4fa", fg="#1e1e2e",
                                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=7,
                                  command=self._run_all)
        self.btn_all.pack(fill="x", padx=10, pady=(0,4))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=4)

        # ── Automatizacion ──
        lbl("AUTOMATIZACION", fg=FG_ACC, font=("Segoe UI", 8, "bold")).pack(padx=10, anchor="w", pady=(4,4))
        self.btn_auto = tk.Button(f, text="Iniciar automatizacion", bg="#585b70", fg=FG_MAIN,
                                   font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", pady=5,
                                   command=self._toggle_auto)
        self.btn_auto.pack(fill="x", padx=10, pady=(0,4))

        self.lbl_status = tk.Label(f, text="Listo", bg=BG_LEFT, fg=FG_GRN, font=("Segoe UI", 8, "bold"))
        self.lbl_status.pack(padx=10, anchor="w", pady=(0,12))

    # ─── PANEL DERECHO ─────────────────────────────────────────────────────
    def _right_panel(self, p):
        # Cabecera tabla
        top = tk.Frame(p, bg=BG_MAIN); top.pack(fill="x", pady=(0,6))
        tk.Label(top, text="Directorio WhatsApp", bg=BG_MAIN, fg=FG_ACC,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(top, text="Actualizar", bg=BG_CELL, fg=FG_MAIN,
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=self._load_table).pack(side="right", padx=4)
        tk.Button(top, text="Guardar cambios en Azure", bg="#89b4fa", fg="#1e1e2e",
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._save_changes).pack(side="right", padx=4)
        tk.Label(top, text="Doble clic en celda coloreada para editar",
                 bg=BG_MAIN, fg=FG_DIM, font=("Segoe UI", 8)).pack(side="right", padx=10)

        # Treeview
        ft = tk.Frame(p, bg=BG_MAIN); ft.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(ft, columns=COL_KEYS, show="headings", selectmode="browse")
        for key, head, width, editable in COLUMNAS:
            self.tree.heading(key, text=head + (" ✏" if editable else ""),
                              command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, minwidth=50, anchor="w")
        vsb = ttk.Scrollbar(ft, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(ft, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.editor = CellEditor(self.tree, self._on_edit)
        self.tree.bind("<Double-1>", self.editor.abrir)

        # Consola
        ttk.Separator(p, orient="horizontal").pack(fill="x", pady=4)
        tk.Label(p, text="Consola", bg=BG_MAIN, fg=FG_ACC, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        lf = tk.Frame(p, bg=BG_MAIN); lf.pack(fill="x")
        lsb = ttk.Scrollbar(lf, orient="vertical")
        self.txt_log = tk.Text(lf, height=8, bg="#11111b", fg=FG_GRN,
                                font=("Consolas", 8), state="disabled", relief="flat",
                                yscrollcommand=lsb.set)
        lsb.configure(command=self.txt_log.yview)
        lsb.pack(side="right", fill="y"); self.txt_log.pack(side="left", fill="x", expand=True)

    # ─── TABLA ─────────────────────────────────────────────────────────────
    def _load_table(self):
        self._log("Cargando datos desde Azure...")
        def w():
            try:
                ents = self.az.cargar()
                self.root.after(0, lambda: self._fill_table(ents))
            except Exception as e:
                self._log("Error: {}".format(e))
        threading.Thread(target=w, daemon=True).start()

    def _fill_table(self, ents):
        for i in self.tree.get_children(): self.tree.delete(i)
        self.ents.clear()
        for i, e in enumerate(ents):
            vals = tuple(e.get(c[0],"") for c in COLUMNAS)
            tag = "par" if i%2==0 else "impar"
            iid = self.tree.insert("","end", values=vals, tags=(tag,))
            self.ents[iid] = e
        self.tree.tag_configure("par",   background="#2a2a3e")
        self.tree.tag_configure("impar", background="#313244")
        self._log("{} contactos cargados.".format(len(ents)))

    def _on_edit(self, iid, col, val):
        if iid in self.ents: self.ents[iid][col] = val

    def _save_changes(self):
        def w():
            ok = 0
            for e in self.ents.values():
                try: self.az.guardar(e); ok += 1
                except Exception as exc: self._log("Error: {}".format(exc))
            self._log("{} registros guardados.".format(ok))
        threading.Thread(target=w, daemon=True).start()

    def _sort(self, key):
        items = [(self.tree.set(i, key), i) for i in self.tree.get_children()]
        items.sort(key=lambda x: x[0].lower())
        for n,(_, i) in enumerate(items): self.tree.move(i,"",n)

    # ─── WHATSAPP ──────────────────────────────────────────────────────────
    def _open_wtp(self):
        if self._wtp:
            messagebox.showinfo("WhatsApp", "El navegador ya esta abierto."); return
        self._log("Iniciando WhatsApp Web...")
        self.btn_wtp.configure(state="disabled", text="Iniciando...")
        def w():
            try:
                from selenium import webdriver
                from selenium.webdriver.edge.options import Options
                from selenium.webdriver.edge.service import Service
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                from config import PERFIL_WHATSAPP_DIR
                opts = Options()
                opts.add_argument("--start-maximized")
                opts.add_argument("--user-data-dir={}".format(PERFIL_WHATSAPP_DIR))
                opts.add_argument("--disable-blink-features=AutomationControlled")
                svc = Service(EdgeChromiumDriverManager().install())
                self.driver = webdriver.Edge(service=svc, options=opts)
                self.driver.set_page_load_timeout(30)
                self.driver.get("https://web.whatsapp.com")
                self._wtp = True
                self.root.after(0, self._wtp_ready)
                self._log("WhatsApp Web cargado. Escanea el QR si es necesario.")
            except Exception as exc:
                self._log("Error: {}".format(exc))
                self.root.after(0, lambda: self.btn_wtp.configure(
                    state="normal", text="Abrir WhatsApp Web"))
        threading.Thread(target=w, daemon=True).start()

    def _wtp_ready(self):
        self.btn_wtp.configure(bg=BG_CELL, fg=FG_DIM, text="WhatsApp activo", state="disabled")
        self.lbl_wtp.configure(text="Estado: sesion activa", fg=FG_GRN)

    # ─── PIPELINE ──────────────────────────────────────────────────────────
    def _run_paso(self, script, wtp):
        if self._busy:
            self._log("Proceso ocupado, espera..."); return
        if wtp and not self._wtp:
            self._log("Requiere WhatsApp activo. Aborta."); return
        self._busy = True
        self._set_status("Ejecutando...", FG_YEL)
        paso_idx = next((i for i in range(len(PASOS)) if PASOS[i][1]==script), None)
        sp = ROOT_DIR / script
        def w():
            try:
                self._log("▶ {}".format(script))
                env = dict(os.environ)
                env["CANTIDAD_ENVIOS"] = self.spin_cant.get()
                proc = subprocess.Popen([sys.executable, str(sp)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, env=env, cwd=str(ROOT_DIR))
                for line in proc.stdout:
                    if line.strip(): self._log(line.rstrip())
                proc.wait()
                self._log("Fin {} (codigo {})".format(script, proc.returncode))
                if paso_idx is not None: self.sched.mark_done(paso_idx)
            except Exception as exc:
                self._log("Error: {}".format(exc))
            finally:
                self._busy = False
                self.root.after(0, lambda: self._set_status("Listo", FG_GRN))
                self.root.after(500, self._load_table)
        threading.Thread(target=w, daemon=True).start()

    def _run_all(self):
        """Ejecuta en orden todos los pasos con checkbox Auto activo."""
        activos = [i for i in range(len(PASOS)) if self.chks[i].get()]
        if not activos:
            messagebox.showinfo("Ejecutar todo",
                "Activa el checkbox 'Auto cada' en los pasos que quieras ejecutar."); return
        def secuencia():
            for i in activos:
                label, script, wtp, _ = PASOS[i]
                self._log("--- Ejecutando paso {}: {}".format(i+1, label))
                if wtp and not self._wtp:
                    self._log("Omitido (WhatsApp no activo): {}".format(label)); continue
                if self._busy:
                    time.sleep(2)
                self._run_paso(script, wtp)
                # Esperar a que termine
                while self._busy: time.sleep(1)
                time.sleep(2)
            self._log("=== Secuencia completada ===")
        threading.Thread(target=secuencia, daemon=True).start()

    def _toggle(self, idx, var):
        self.sched.states[idx]["active"] = var.get()
        if var.get(): self.sched.states[idx]["last"] = None

    def _upd_interval(self, idx):
        try:
            sp_d, sp_h, sp_m, sp_s = self.spins[idx]
            total = (int(sp_d.get()) * 86400 + int(sp_h.get()) * 3600
                     + int(sp_m.get()) * 60   + int(sp_s.get()))
            self.sched.states[idx]["interval"] = max(1, total)
        except (ValueError, TypeError): pass

    def _toggle_auto(self):
        if self.sched._run:
            self.sched.stop()
            self.btn_auto.configure(text="Iniciar automatizacion", bg="#585b70")
        else:
            if not any(s["active"] for s in self.sched.states.values()):
                messagebox.showinfo("Auto", "Activa 'Auto cada' en al menos un paso."); return
            self.sched.start()
            self.btn_auto.configure(text="Detener automatizacion", bg=FG_RED)

    def _poll_sched(self):
        for i, lbl in enumerate(self.lbls_next):
            lbl.configure(text="prox: {}".format(self.sched.next_str(i)))
        self.root.after(1000, self._poll_sched)

    def _set_status(self, txt, col):
        self.lbl_status.configure(text=txt, fg=col)

    # ─── LOG ───────────────────────────────────────────────────────────────
    def _log(self, msg):
        self.lq.put("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg))

    def _poll_logs(self):
        try:
            while True:
                msg = self.lq.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", msg+"\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty: pass
        self.root.after(100, self._poll_logs)

    def _attach_log(self):
        h = QueueLogHandler(self.lq)
        logging.getLogger().addHandler(h)

    # ─── CIERRE ────────────────────────────────────────────────────────────
    def _on_close(self):
        self.sched.stop()
        if self._wtp and self.driver:
            try: self.driver.quit()
            except Exception: pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app  = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()

if __name__ == "__main__":
    main()

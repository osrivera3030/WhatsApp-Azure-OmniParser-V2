from __future__ import annotations
import os, queue, subprocess, sys, threading, time, traceback, pathlib
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

_LOG = pathlib.Path(__file__).resolve().parent / "dashboard_error.log"
def _ck(msg):
    with _LOG.open("a", encoding="utf-8") as fh:
        fh.write("[{}] {}\n".format(datetime.now().strftime("%H:%M:%S"), msg))
_LOG.write_text("", encoding="utf-8")
_ck("imports OK")

sys.path.append(str(Path(__file__).resolve().parent))
from config import AZURE_CONNECTION_STRING, TABLAS, get_logger
_ck("config OK")

logger   = get_logger(__name__)
ROOT_DIR = Path(__file__).resolve().parent

COLUMNAS = [
    ("ClienteID",          "ID",       75,  False),
    ("phone",              "Telefono", 115, False),
    ("name",               "Nombre",   155, False),
    ("Respuesta_Recibida", "Rta",       55, False),
    ("Texto_Respuesta",    "Respuesta",270, False),
    ("fecha_envio_msg",    "Enviado",  105, False),
    ("Fecha_Verificacion", "Procesado",125, False),
    ("mensaje_enviado",    "Mensaje",  190, False),
]
COL_KEYS = [c[0] for c in COLUMNAS]

PASOS = [
    ("1  Limpieza datos",        "1-Data_cleaning/Data_cleaning.py",             86400, False),
    ("2  Validar WhatsApp",      "2-Validation_wtp/validacion_st.py",            86400, True),
    ("3  Enviar mensajes",       "3-Envio_mjs/envio_stg.py",                      7200, True),
    ("4  Capturar respuestas",   "4-Guarda_mjs_recep/procesar_respuestas_v2.py",  3600, True),
    ("5  Enviar agradecimiento", "5-envio_mjs_repuesta/res_final.py",             7200, True),
    ("6  Archivar / resetear",   "6-Data_storege/respuesta_final.py",          777600, False),
]

C = dict(
    bg="#1e1e2e", panel="#181825", card="#242436", cell="#313244",
    fg="#cdd6f4", acc="#cba6f7",  grn="#a6e3a1",  red="#f38ba8",
    yel="#f9e2af", dim="#6c7086", blue="#89b4fa", teal="#94e2d5",
)


class AzureManager:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from azure.data.tables import TableClient
            self._client = TableClient.from_connection_string(
                AZURE_CONNECTION_STRING, TABLAS["directorio"])
        return self._client

    def cargar(self):
        ents = list(self._get_client().list_entities())
        for i, e in enumerate(ents, 1):
            if not e.get("ClienteID"):
                e["ClienteID"] = "CLI-{:04d}".format(i)
        return ents


class Scheduler:
    def __init__(self, on_run, on_log):
        self.on_run  = on_run
        self.on_log  = on_log
        self._activo = False
        self.estados = {
            i: {"activo": False, "intervalo": PASOS[i][2], "ultimo": None}
            for i in range(len(PASOS))
        }

    def iniciar(self):
        self._activo = True
        threading.Thread(target=self._loop, daemon=True).start()
        self.on_log("Automatizacion iniciada")

    def detener(self):
        self._activo = False
        self.on_log("Automatizacion detenida")

    def marcar(self, idx):
        self.estados[idx]["ultimo"] = datetime.now()

    def proximo(self, idx):
        e = self.estados[idx]
        if not e["activo"]:     return "--"
        if e["ultimo"] is None: return "ahora"
        delta = e["ultimo"] + timedelta(seconds=e["intervalo"]) - datetime.now()
        sec = int(delta.total_seconds())
        if sec <= 0: return "ahora"
        d, r = divmod(sec, 86400); h, r2 = divmod(r, 3600); m, s = divmod(r2, 60)
        if d: return "{}d {}h".format(d, h)
        if h: return "{}h {}m".format(h, m)
        if m: return "{}m {}s".format(m, s)
        return "{}s".format(s)

    def _loop(self):
        while self._activo:
            now = datetime.now()
            for i in range(len(PASOS)):
                e = self.estados[i]
                if not e["activo"]: continue
                ult = e["ultimo"]
                if ult is None or (now - ult).total_seconds() >= e["intervalo"]:
                    self.on_log("Auto: {}".format(PASOS[i][0]))
                    self.marcar(i)
                    self.on_run(i)
                    time.sleep(5)
            time.sleep(20)


class Dashboard:
    def __init__(self, root):
        _ck("Dashboard init")
        self.root  = root
        self.lq    = queue.Queue()
        self.az    = AzureManager()
        self.ents  = {}
        self._busy = False
        self.sched = Scheduler(self._lanzar_por_idx, self._log)
        self._spins_d = []; self._spins_h = []
        self._spins_m = []; self._spins_s = []
        self._chks      = []
        self._lbls_prox = []
        self._btns_paso = []
        _ck("_config_ventana")
        self._config_ventana()
        _ck("_build")
        self._build()
        _ck("_cargar_tabla")
        self._cargar_tabla()
        _ck("polls")
        self._poll_log()
        self._poll_sched()
        _ck("init completo")

    def _config_ventana(self):
        self.root.title("solo_nube")
        self.root.geometry("1440x840")
        self.root.configure(bg=C["bg"])
        s = ttk.Style(); s.theme_use("clam")
        s.configure("Treeview", background="#2a2a3e", foreground=C["fg"],
                    fieldbackground="#2a2a3e", rowheight=24, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background=C["cell"], foreground=C["acc"],
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", "#45475a")])

    def _build(self):
        m = tk.Frame(self.root, bg=C["bg"])
        m.pack(fill="both", expand=True, padx=10, pady=10)
        side = tk.Frame(m, bg=C["panel"], width=260)
        side.pack(side="left", fill="y", padx=(0, 8))
        side.pack_propagate(False)
        self._sidebar(side)
        right = tk.Frame(m, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)
        self._panel_der(right)

    def _sidebar(self, p):
        _ck("sidebar")
        cv = tk.Canvas(p, bg=C["panel"], highlightthickness=0)
        vsb = ttk.Scrollbar(p, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        f = tk.Frame(cv, bg=C["panel"])
        wid = cv.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>",  lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(wid, width=e.width))

        def L(txt, fg=None, fnt=("Segoe UI", 8)):
            return tk.Label(f, text=txt, bg=C["panel"], fg=fg or C["fg"], font=fnt)

        L("solo_nube", fg=C["acc"], fnt=("Segoe UI", 13, "bold")).pack(padx=12, pady=(14,1), anchor="w")
        L("Panel de control", fg=C["dim"]).pack(padx=12, anchor="w")
        ttk.Separator(f).pack(fill="x", padx=12, pady=8)

        L("CONFIGURACION", fg=C["acc"], fnt=("Segoe UI", 8, "bold")).pack(padx=12, anchor="w", pady=(0,4))
        cfg = tk.Frame(f, bg=C["card"], padx=8, pady=6)
        cfg.pack(fill="x", padx=8, pady=(0,4))

        r1 = tk.Frame(cfg, bg=C["card"]); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="Codigo de pais", bg=C["card"], fg=C["fg"], font=("Segoe UI", 8)).pack(side="left")
        self.var_pais = tk.StringVar(value="504")
        tk.Entry(r1, textvariable=self.var_pais, width=6,
                 bg=C["cell"], fg=C["teal"], insertbackground=C["teal"],
                 relief="flat", font=("Segoe UI", 9, "bold")).pack(side="right")

        r2 = tk.Frame(cfg, bg=C["card"]); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="Mensajes a enviar (paso 3)", bg=C["card"], fg=C["fg"],
                 font=("Segoe UI", 8)).pack(side="left")
        self.sp_cantidad = tk.Spinbox(r2, from_=1, to=9999, width=5,
                                      bg=C["cell"], fg=C["teal"],
                                      buttonbackground="#45475a",
                                      font=("Segoe UI", 9, "bold"))
        self.sp_cantidad.delete(0, "end"); self.sp_cantidad.insert(0, "20")
        self.sp_cantidad.pack(side="right")

        ttk.Separator(f).pack(fill="x", padx=12, pady=8)
        L("EJECUTAR AHORA", fg=C["acc"], fnt=("Segoe UI", 8, "bold")).pack(padx=12, anchor="w", pady=(0,4))

        for i, (label, script, seg_def, wtp) in enumerate(PASOS):
            _ck("paso {}".format(i))
            card = tk.Frame(f, bg=C["card"], padx=6, pady=5)
            card.pack(fill="x", padx=8, pady=3)

            rt = tk.Frame(card, bg=C["card"]); rt.pack(fill="x")
            tk.Label(rt, text="WTP" if wtp else "LOC",
                     bg=C["teal"] if wtp else C["dim"], fg="#1e1e2e",
                     font=("Segoe UI", 6, "bold"), padx=3, pady=1).pack(side="left", padx=(0,4))
            btn = tk.Button(rt, text="{}".format(label),
                            bg=C["cell"], fg=C["fg"], font=("Segoe UI", 8),
                            relief="flat", cursor="hand2", anchor="w", padx=4, pady=3,
                            command=lambda idx=i: self._lanzar_por_idx(idx))
            btn.pack(side="left", fill="x", expand=True)
            self._btns_paso.append(btn)

            rA = tk.Frame(card, bg=C["card"]); rA.pack(fill="x", pady=(4,0))
            var = tk.BooleanVar(value=False); self._chks.append(var)
            tk.Checkbutton(rA, text="Auto cada", variable=var,
                           bg=C["card"], fg=C["grn"], selectcolor=C["cell"],
                           activebackground=C["card"], font=("Segoe UI", 7),
                           command=lambda idx=i, v=var: self._toggle_auto(idx, v)
                           ).pack(side="left")
            lp = tk.Label(rA, text="prox: --", bg=C["card"], fg=C["dim"], font=("Segoe UI", 7))
            lp.pack(side="right"); self._lbls_prox.append(lp)

            d_d, r0 = divmod(seg_def, 86400)
            d_h, r1 = divmod(r0, 3600)
            d_m, d_s = divmod(r1, 60)
            rT = tk.Frame(card, bg=C["card"]); rT.pack(fill="x")

            def mk_sp(parent, maxv, defv, _i=i):
                sp = tk.Spinbox(parent, from_=0, to=maxv, width=3,
                                bg=C["cell"], fg=C["fg"],
                                buttonbackground="#45475a", font=("Segoe UI", 7),
                                command=lambda ii=_i: self._upd_intervalo(ii))
                sp.delete(0, "end"); sp.insert(0, str(defv))
                sp.bind("<FocusOut>", lambda e, ii=_i: self._upd_intervalo(ii))
                return sp

            def mk_l(parent, txt):
                return tk.Label(parent, text=txt, bg=C["card"], fg=C["dim"], font=("Segoe UI", 7))

            sd = mk_sp(rT, 365, d_d); sd.pack(side="left"); mk_l(rT,"d").pack(side="left",padx=(1,4))
            sh = mk_sp(rT, 23,  d_h); sh.pack(side="left"); mk_l(rT,"h").pack(side="left",padx=(1,4))
            sm = mk_sp(rT, 59,  d_m); sm.pack(side="left"); mk_l(rT,"m").pack(side="left",padx=(1,4))
            ss = mk_sp(rT, 59,  d_s); ss.pack(side="left"); mk_l(rT,"s").pack(side="left",padx=(1,0))
            self._spins_d.append(sd); self._spins_h.append(sh)
            self._spins_m.append(sm); self._spins_s.append(ss)

        ttk.Separator(f).pack(fill="x", padx=12, pady=8)
        L("AUTOMATIZACION", fg=C["acc"], fnt=("Segoe UI", 8, "bold")).pack(padx=12, anchor="w", pady=(0,4))
        L("Marca Auto cada en los pasos, luego inicia.",
          fg=C["dim"], fnt=("Segoe UI", 7)).pack(padx=12, anchor="w", pady=(0,6))

        self._btn_auto = tk.Button(f, text="Iniciar automatizacion",
                                   bg=C["blue"], fg="#1e1e2e",
                                   font=("Segoe UI", 9, "bold"),
                                   relief="flat", cursor="hand2", pady=7,
                                   command=self._toggle_automatizacion)
        self._btn_auto.pack(fill="x", padx=12, pady=(0,6))

        self._lbl_status = tk.Label(f, text="Listo", bg=C["panel"],
                                    fg=C["grn"], font=("Segoe UI", 9, "bold"))
        self._lbl_status.pack(padx=12, anchor="w", pady=(0,14))
        _ck("sidebar OK")

    def _panel_der(self, p):
        _ck("panel_der")
        top = tk.Frame(p, bg=C["bg"]); top.pack(fill="x", pady=(0,6))
        tk.Label(top, text="Directorio de contactos", bg=C["bg"],
                 fg=C["acc"], font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(top, text="Actualizar", bg=C["cell"], fg=C["fg"],
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=self._cargar_tabla).pack(side="right")
        self._lbl_count = tk.Label(top, text="", bg=C["bg"], fg=C["dim"], font=("Segoe UI", 8))
        self._lbl_count.pack(side="right", padx=10)

        ft = tk.Frame(p, bg=C["bg"]); ft.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(ft, columns=COL_KEYS, show="headings", selectmode="browse")
        for key, head, width, _ in COLUMNAS:
            self.tree.heading(key, text=head, command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, minwidth=30, anchor="w")
        vsb2 = ttk.Scrollbar(ft, orient="vertical",   command=self.tree.yview)
        hsb2 = ttk.Scrollbar(ft, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb2.set, xscrollcommand=hsb2.set)
        vsb2.pack(side="right", fill="y")
        hsb2.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)

        ttk.Separator(p).pack(fill="x", pady=4)
        hdr = tk.Frame(p, bg=C["bg"]); hdr.pack(fill="x")
        tk.Label(hdr, text="Consola", bg=C["bg"], fg=C["acc"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(hdr, text="Limpiar", bg=C["cell"], fg=C["dim"],
                  font=("Segoe UI", 7), relief="flat", cursor="hand2",
                  command=self._limpiar_log).pack(side="right")
        lf = tk.Frame(p, bg=C["bg"]); lf.pack(fill="x")
        lsb2 = ttk.Scrollbar(lf, orient="vertical")
        self.txt = tk.Text(lf, height=9, bg="#11111b", fg=C["grn"],
                           font=("Consolas", 8), state="disabled", relief="flat",
                           yscrollcommand=lsb2.set)
        lsb2.configure(command=self.txt.yview)
        lsb2.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="x", expand=True)
        _ck("panel_der OK")

    def _cargar_tabla(self):
        self._log("Actualizando tabla...")
        def w():
            try:
                ents = self.az.cargar()
                self.root.after(0, lambda: self._poblar(ents))
            except Exception as exc:
                self._log("Error Azure: {}".format(exc))
        threading.Thread(target=w, daemon=True).start()

    def _poblar(self, ents):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.ents.clear()
        si = no = 0
        for i, e in enumerate(ents):
            vals = tuple(str(e.get(c[0], "") or "") for c in COLUMNAS)
            rta  = str(e.get("Respuesta_Recibida", "")).upper()
            if rta == "SI":
                tag = "si"; si += 1
            elif rta == "NO":
                tag = "no"; no += 1
            else:
                tag = "par" if i % 2 == 0 else "impar"
            iid = self.tree.insert("", "end", values=vals, tags=(tag,))
            self.ents[iid] = e
        self.tree.tag_configure("si",    background="#1a3a22")
        self.tree.tag_configure("no",    background="#2a2a3e")
        self.tree.tag_configure("par",   background="#2a2a3e")
        self.tree.tag_configure("impar", background="#252535")
        total = len(ents)
        self._lbl_count.configure(
            text="{} contactos  SI:{}  NO:{}  Pendientes:{}".format(
                total, si, no, total - si - no))
        self._log("Tabla: {} registros".format(total))

    def _sort(self, key):
        rows = [(self.tree.set(i, key), i) for i in self.tree.get_children()]
        rows.sort(key=lambda x: x[0].lower())
        for n, (_, i) in enumerate(rows):
            self.tree.move(i, "", n)

    def _stdin_para(self, idx):
        cod  = self.var_pais.get().strip() or "504"
        cant = self.sp_cantidad.get().strip() or "20"
        if idx == 1: return "{}\n\n".format(cod)
        if idx == 2: return "{}\n{}\n".format(cant, cod)
        if idx == 3: return "\n"
        if idx == 4: return "{}\n\n".format(cod)
        return ""

    def _lanzar_por_idx(self, idx):
        label, script, _, _ = PASOS[idx]
        self._lanzar(script, idx, label)

    def _lanzar(self, script, idx=None, label=None):
        if self._busy:
            self._log("Proceso en curso."); return
        ruta = ROOT_DIR / script
        if not ruta.exists():
            self._log("No encontrado: {}".format(script)); return
        self._busy = True
        self._set_status("{}".format((label or script)[:30]), C["yel"])
        for b in self._btns_paso:
            b.configure(state="disabled")
        self.root.after(8000, self._auto_refresh)
        stdin_data = (self._stdin_para(idx) if idx is not None else "").encode()

        def w():
            try:
                self._log("Iniciando: {}".format(script))
                if stdin_data:
                    self._log("stdin: {}".format(repr(stdin_data.decode())))
                proc = subprocess.Popen(
                    [sys.executable, str(ruta)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=False,
                    cwd=str(ROOT_DIR),
                    env=dict(os.environ))
                if stdin_data:
                    proc.stdin.write(stdin_data)
                proc.stdin.close()
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self._log(line)
                proc.wait()
                rc = proc.returncode
                self._log("Fin {}  {}".format(script, "OK" if rc == 0 else "ERROR({})".format(rc)))
            except Exception as exc:
                self._log("Error: {}".format(exc))
            finally:
                self._busy = False
                self.root.after(0, lambda: self._set_status("Listo", C["grn"]))
                self.root.after(0, lambda: [b.configure(state="normal") for b in self._btns_paso])
                self.root.after(1000, self._cargar_tabla)

        threading.Thread(target=w, daemon=True).start()

    def _auto_refresh(self):
        if not self._busy:
            return
        self._cargar_tabla()
        self.root.after(8000, self._auto_refresh)

    def _toggle_auto(self, idx, var):
        self.sched.estados[idx]["activo"] = var.get()
        if var.get():
            self.sched.estados[idx]["ultimo"] = None

    def _upd_intervalo(self, idx):
        try:
            seg = (int(self._spins_d[idx].get()) * 86400
                 + int(self._spins_h[idx].get()) * 3600
                 + int(self._spins_m[idx].get()) * 60
                 + int(self._spins_s[idx].get()))
            self.sched.estados[idx]["intervalo"] = max(1, seg)
        except (ValueError, TypeError):
            pass

    def _toggle_automatizacion(self):
        if self.sched._activo:
            self.sched.detener()
            self._btn_auto.configure(text="Iniciar automatizacion", bg=C["blue"])
        else:
            if not any(e["activo"] for e in self.sched.estados.values()):
                messagebox.showinfo("Auto", "Marca Auto cada en al menos un paso.")
                return
            self.sched.iniciar()
            self._btn_auto.configure(text="Detener automatizacion", bg=C["red"])

    def _poll_sched(self):
        for i, lbl in enumerate(self._lbls_prox):
            lbl.configure(text="prox: {}".format(self.sched.proximo(i)))
        self.root.after(1000, self._poll_sched)

    def _log(self, msg):
        self.lq.put("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg))

    def _poll_log(self):
        try:
            while True:
                msg = self.lq.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _limpiar_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _set_status(self, txt, color):
        self._lbl_status.configure(text=txt, fg=color)

    def _on_close(self):
        self.sched.detener()
        self.root.destroy()


def main():
    _ck("main()")
    try:
        root = tk.Tk()
        _ck("Tk OK")
        app = Dashboard(root)
        _ck("Dashboard OK - mainloop")
        root.protocol("WM_DELETE_WINDOW", app._on_close)
        root.mainloop()
        _ck("mainloop fin")
    except Exception:
        msg = traceback.format_exc()
        _ck("EXCEPCION: " + msg)
        try:
            messagebox.showerror("Error dashboard", msg[:800])
        except Exception:
            pass


if __name__ == "__main__":
    _ck("__main__")
    main()

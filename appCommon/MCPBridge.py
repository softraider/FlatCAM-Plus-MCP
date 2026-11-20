"""
Local automation bridge for FlatCAM Plus.

Exposes a small JSON-over-HTTP API on 127.0.0.1 so an external MCP server (see mcp_server/) can drive the
running application through the same Tcl command layer used by the built-in Tcl Shell.

Security model:
  * Listens on the loopback interface only.
  * Every request must carry a random per-session token (header "X-Bridge-Token").
  * Port and token are written to a per-user discovery file that only the current user can read.
  * Disabled unless the FLATCAM_MCP_BRIDGE environment variable is set to "1".

Endpoints:
  GET  /status              -> app version, headless flag, busy state, object count
  GET  /objects             -> list of loaded objects (name, kind, bounds)
  POST /tcl {"script": str, "timeout": float}
                            -> runs Tcl in the GUI thread, waits for background jobs, returns result + messages
  POST /screenshot {"fit": bool}
                            -> PNG (base64) of the plot canvas
  POST /film {"films": [{gerber, kind, side, polarity}], "out": pdf, "outline", "drill", "name", "copies",
              "mark_dia"}   -> 1:1 A4 PDF photo films (see appCommon/PhotoFilm.py)
"""

import atexit
import base64
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyQt6 import QtCore, QtGui

BRIDGE_ENV = "FLATCAM_MCP_BRIDGE"
BRIDGE_PORT_ENV = "FLATCAM_MCP_BRIDGE_PORT"


def discovery_file_path():
    # Use the user's home folder rather than %APPDATA%: some MCP clients run servers with a redirected APPDATA,
    # and both sides must agree on this path.
    return os.path.join(os.path.expanduser("~"), ".flatcam_plus_mcp", "bridge.json")


class _Job:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload
        self.done = threading.Event()
        self.result = None
        self.error = None


class MCPBridge(QtCore.QObject):
    # carries a _Job from the HTTP thread to the Qt main thread
    job_posted = QtCore.pyqtSignal(object)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.thread = None
        self._capture_lock = threading.Lock()
        self._captures = []

        self.job_posted.connect(self._run_job, type=QtCore.Qt.ConnectionType.QueuedConnection)

        # collect status-bar / shell messages so they can be returned to the caller
        for sig in (self.app.inform, self.app.inform_shell):
            try:
                sig[str].connect(self._on_message)
                sig[str, bool].connect(lambda msg, _flag: self._on_message(msg))
            except Exception:
                pass

    # ------------------------------------------------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------------------------------------------------
    def start(self):
        port = int(os.environ.get(BRIDGE_PORT_ENV, "0") or 0)
        bridge = self

        class Handler(_Handler):
            owner = bridge

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="MCPBridge", daemon=True)
        self.thread.start()
        self._write_discovery()
        atexit.register(self.stop)
        self.app.log.info("MCP bridge listening on 127.0.0.1:%d" % self.server.server_address[1])

    def stop(self):
        try:
            path = discovery_file_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    if json.load(f).get("pid") == os.getpid():
                        os.remove(path)
        except Exception:
            pass
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                pass
            self.server = None

    def _write_discovery(self):
        path = discovery_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        info = {
            "port": self.server.server_address[1],
            "token": self.token,
            "pid": os.getpid(),
            "headless": self.is_headless(),
            "started": time.time(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, path)

    # ------------------------------------------------------------------------------------------------------------
    # helpers (any thread)
    # ------------------------------------------------------------------------------------------------------------
    def is_headless(self):
        return getattr(self.app, "cmd_line_headless", None) == 1

    def busy_count(self):
        pc = getattr(self.app, "proc_container", None)
        try:
            return len(pc.procs) if pc is not None else 0
        except Exception:
            return 0

    def _on_message(self, msg):
        with self._capture_lock:
            for buf in self._captures:
                buf.append(str(msg))

    def submit(self, kind, payload, timeout):
        job = _Job(kind, payload)
        self.job_posted.emit(job)
        if not job.done.wait(timeout):
            raise TimeoutError("FlatCAM did not respond within %.0f s" % timeout)
        if job.error is not None:
            raise job.error
        return job.result

    def run_tcl(self, script, timeout):
        buf = []
        with self._capture_lock:
            self._captures.append(buf)
        try:
            started = time.time()
            res = self.submit("tcl", {"script": script}, timeout)
            # many FlatCAM commands hand the heavy work to worker threads; wait for them to finish
            time.sleep(0.2)
            while self.busy_count() > 0 and time.time() - started < timeout:
                time.sleep(0.2)
            res["still_running"] = self.busy_count() > 0
            # show the results in the window (Tcl commands do not plot by themselves)
            if not self.is_headless() and not res["still_running"]:
                try:
                    self.submit("plot", {"fit": False}, 120)
                except Exception:
                    pass
            # let queued inform signals arrive
            time.sleep(0.1)
        finally:
            with self._capture_lock:
                self._captures.remove(buf)
        res["messages"] = buf
        return res

    # ------------------------------------------------------------------------------------------------------------
    # main-thread work
    # ------------------------------------------------------------------------------------------------------------
    @QtCore.pyqtSlot(object)
    def _run_job(self, job):
        try:
            if job.kind == "tcl":
                job.result = self._do_tcl(job.payload["script"])
            elif job.kind == "objects":
                job.result = self._do_objects()
            elif job.kind == "plot":
                self._do_plot(job.payload.get("fit", False))
                job.result = {"ok": True}
            elif job.kind == "screenshot":
                job.result = self._do_screenshot(job.payload.get("fit", False))
            else:
                raise ValueError("unknown job kind: %s" % job.kind)
        except Exception as e:
            job.error = e
        finally:
            job.done.set()

    def _do_tcl(self, script):
        shell = self.app.shell
        headless = self.is_headless()
        # file-open commands refuse to work on the start screen; open a project workspace automatically
        ui = getattr(self.app, "ui", None)
        if ui is not None and hasattr(ui, "has_active_project") and not ui.has_active_project():
            try:
                ui.activate_project_workspace()
            except Exception:
                pass
        if not headless:
            try:
                shell.append_output("[MCP] %s\n" % script.strip())
            except Exception:
                pass
        import tkinter as tk
        try:
            result = shell.exec_command_test(script, reraise=True, no_echo=headless)
            return {"ok": True, "result": "" if result in (None, "None") else str(result)}
        except tk.TclError:
            try:
                info = shell.tcl.eval("set errorInfo")
            except Exception:
                info = ""
            return {"ok": False, "result": "", "error": info}

    def _do_objects(self):
        out = []
        for obj in self.app.collection.get_list():
            item = {"name": obj.obj_options.get("name"), "kind": getattr(obj, "kind", None)}
            try:
                b = obj.bounds()
                item["bounds"] = [round(float(v), 4) for v in b]
            except Exception:
                item["bounds"] = None
            if item["kind"] == "excellon":
                try:
                    item["tools"] = {str(k): {"dia": v.get("tooldia"), "drills": len(v.get("drills", [])),
                                              "slots": len(v.get("slots", []))} for k, v in obj.tools.items()}
                except Exception:
                    pass
            out.append(item)
        return {"objects": out, "units": str(self.app.app_units if hasattr(self.app, "app_units")
                                              else self.app.options.get("units", ""))}

    def _do_plot(self, fit):
        # Tcl commands create objects with plot=False; redraw everything so the canvas matches the project
        try:
            self.app.plot_all(fit_view=False, muted=True, use_thread=False)
        except Exception as e:
            self.app.log.error("MCP bridge plot_all failed: %s" % str(e))
        if fit:
            try:
                self.app.on_zoom_fit()
            except Exception:
                pass
        QtCore.QCoreApplication.processEvents()

    def _do_screenshot(self, fit):
        self._do_plot(fit)
        canvas = self.app.plotcanvas
        image = None
        # vispy canvas: render offscreen (works in headless mode and for OpenGL widgets)
        if hasattr(canvas, "render"):
            try:
                arr = canvas.render()
                h, w = arr.shape[0], arr.shape[1]
                image = QtGui.QImage(bytes(arr.tobytes()), w, h, 4 * w, QtGui.QImage.Format.Format_RGBA8888).copy()
            except Exception:
                image = None
        if image is None:
            widget = getattr(canvas, "native", None) or getattr(canvas, "canvas", None)
            image = widget.grab().toImage()
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        buf.close()
        return {"png_base64": base64.b64encode(bytes(ba)).decode("ascii"),
                "width": image.width(), "height": image.height()}


class _Handler(BaseHTTPRequestHandler):
    owner: MCPBridge = None
    server_version = "FlatCAMPlusMCPBridge/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        tok = self.headers.get("X-Bridge-Token", "")
        if not secrets.compare_digest(tok, self.owner.token):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if not self._authorized():
            return
        try:
            if self.path == "/status":
                app = self.owner.app
                self._send(200, {"ok": True, "version": str(getattr(app, "version", "")),
                                 "headless": self.owner.is_headless(), "busy": self.owner.busy_count(),
                                 "objects": len(app.collection.get_list()), "pid": os.getpid()})
            elif self.path == "/objects":
                res = self.owner.submit("objects", {}, 30)
                res["ok"] = True
                self._send(200, res)
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            body = self._body()
            if self.path == "/tcl":
                timeout = float(body.get("timeout", 300))
                self._send(200, self.owner.run_tcl(str(body.get("script", "")), timeout))
            elif self.path == "/screenshot":
                res = self.owner.submit("screenshot", {"fit": bool(body.get("fit", False))}, 60)
                res["ok"] = True
                self._send(200, res)
            elif self.path == "/film":
                # file based and GUI independent: runs in this HTTP worker thread with FlatCAM's parsers
                from appCommon.PhotoFilm import export_films
                res = export_films(body.get("films", []), body["out"], body.get("outline"), body.get("drill"),
                                   body.get("name", ""), int(body.get("copies", 2)),
                                   float(body.get("mark_dia", 0.4)))
                res["ok"] = True
                self._send(200, res)
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


def start_bridge_if_enabled(app):
    """Start the bridge when FLATCAM_MCP_BRIDGE=1. Returns the bridge or None."""
    if os.environ.get(BRIDGE_ENV, "") != "1":
        return None
    try:
        bridge = MCPBridge(app)
        bridge.start()
        return bridge
    except Exception as e:
        try:
            app.log.error("MCP bridge failed to start: %s" % str(e))
        except Exception:
            pass
        return None

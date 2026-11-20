"""
FlatCAM Plus MCP - MCP server for FlatCAM Plus.

Talks to the MCP bridge built into FlatCAM Plus (appCommon/MCPBridge.py) over 127.0.0.1 and exposes
PCB CAM operations as MCP tools: open files, isolation, NCC, paint, follow, cutout, drilling, CNC jobs
(including laser preprocessors) and G-code export.

Deliberately NOT exposed: anything that moves a machine (serial/TCP streaming, jogging, probing).
Sending G-code to the machine stays a manual step in the FlatCAM Plus UI.

Run:  <repo>\\.venv\\Scripts\\python.exe <repo>\\mcp_server\\server.py
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.utilities.types import Image
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

FROZEN = getattr(sys, "frozen", False)
REPO_DIR = Path(__file__).resolve().parent.parent
VENV_PYW = REPO_DIR / ".venv" / "Scripts" / "pythonw.exe"
VENV_PY = REPO_DIR / ".venv" / "Scripts" / "python.exe"


def _flatcam_command() -> tuple[list[str], str]:
    """Command line and working dir used to start FlatCAM Plus.
    Order: FLATCAM_PLUS_EXE env var -> FlatCAMPlus.exe next to this server (installed build) ->
    source checkout (.venv) -> default install location."""
    exe = os.environ.get("FLATCAM_PLUS_EXE")
    if exe and Path(exe).exists():
        return [exe], str(Path(exe).parent)
    if FROZEN:
        sibling = Path(sys.executable).resolve().parent / "FlatCAMPlus.exe"
        if sibling.exists():
            return [str(sibling)], str(sibling.parent)
    if (REPO_DIR / "flatcam.py").exists():
        py = VENV_PYW if VENV_PYW.exists() else Path(sys.executable)
        return [str(py), str(REPO_DIR / "flatcam.py")], str(REPO_DIR)
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        cand = Path(base) / "FlatCAMPlus" / "FlatCAMPlus.exe"
        if base and cand.exists():
            return [str(cand)], str(cand.parent)
    raise RuntimeError("FlatCAM Plus not found. Set FLATCAM_PLUS_EXE to the path of FlatCAMPlus.exe.")

GERBER_EXT = {".gbr", ".ger", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp", ".gko", ".gm1",
              ".gml", ".gm", ".pho", ".art", ".cmp", ".sol", ".stc", ".sts", ".plc", ".pls", ".g1", ".g2", ".gp1"}
EXCELLON_EXT = {".drl", ".xln", ".exc", ".drd", ".txt", ".tap", ".ncd"}
GCODE_EXT = {".nc", ".gcode", ".ngc", ".cnc", ".g"}

mcp = FastMCP(
    "FlatCAM Plus MCP",
    instructions=(
        "Controls FlatCAM Plus (PCB CAM). Typical PCB flow: open_files -> isolate (copper) -> "
        "cncjob on the isolation geometry -> drill_cncjob on the Excellon -> cutout -> cncjob -> export_gcode. "
        "Laser work: choose a laser preprocessor (see list_preprocessors) and set las_power; z values are ignored "
        "by most laser preprocessors. UV solder-mask exposure: open the mask Gerber and run ncc on it (fills "
        "everything except the openings) or paint (fills the openings), then cncjob with a laser preprocessor. "
        "Units follow the FlatCAM preferences (see list_objects). Always check results with list_objects and "
        "screenshot. For photo-resist / UV-mask exposure films printed on transparency use export_films (works on "
        "Gerber files directly). This server never sends G-code to a machine."
    ),
)


# ----------------------------------------------------------------------------------------------------------------
# bridge client
# ----------------------------------------------------------------------------------------------------------------
def _discovery_candidates() -> list[Path]:
    """Where FlatCAM Plus may have written its bridge.json. The first entry is the current location
    (home folder); the others are used by builds up to v1.1.0-mcp.2 (%APPDATA%)."""
    home = Path(os.path.expanduser("~"))
    paths = [home / ".flatcam_plus_mcp" / "bridge.json",
             home / "AppData" / "Roaming" / "FlatCAMPlusMCP" / "bridge.json"]
    if os.environ.get("APPDATA"):
        paths.append(Path(os.environ["APPDATA"]) / "FlatCAMPlusMCP" / "bridge.json")
    unique = []
    for p in paths:
        if p not in unique:
            unique.append(p)
    return unique


def _discovery_path() -> Path:
    """Most recently written bridge.json, or the default location if none exists."""
    existing = [p for p in _discovery_candidates() if p.exists()]
    if not existing:
        return _discovery_candidates()[0]
    return max(existing, key=lambda p: p.stat().st_mtime)


def _bridge_info() -> Optional[dict]:
    p = _discovery_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _call(method: str, path: str, body: Optional[dict] = None, timeout: float = 30) -> dict:
    info = _bridge_info()
    if not info:
        raise RuntimeError("FlatCAM Plus bridge is not running. Call launch_flatcam first.")
    url = "http://127.0.0.1:%d%s" % (info["port"], path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Bridge-Token": info["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise RuntimeError("bridge HTTP %d" % e.code)
    except (urllib.error.URLError, ConnectionError) as e:
        raise RuntimeError("Cannot reach FlatCAM Plus bridge (%s). Is the app still open? "
                           "Call launch_flatcam." % e)


def _alive() -> Optional[dict]:
    try:
        st = _call("GET", "/status", timeout=3)
        return st if st.get("ok") else None
    except Exception:
        return None


def _q(value) -> str:
    """Quote a value as a single Tcl word."""
    if isinstance(value, float):
        # MCP clients send JSON numbers that arrive as floats (10000 -> 10000.0); several FlatCAM options are
        # parsed with int() and silently drop "10000.0", so write integral values without a fraction.
        s = str(int(value)) if value.is_integer() else format(value, ".10g")
    else:
        s = str(value)
    if re.fullmatch(r"[\w.\-+:/,]+", s):
        return s
    if "{" in s or "}" in s:
        return '"' + s.replace("\\", "/").replace('"', '\\"').replace("[", "\\[").replace("$", "\\$") + '"'
    return "{" + s + "}"


def _path(p: str) -> str:
    return _q(str(Path(p).expanduser().resolve()).replace("\\", "/"))


def _opts(**kw) -> str:
    parts = []
    for k, v in kw.items():
        if v is None or v is False:
            continue
        if v is True:
            parts.append("-%s" % k)
        else:
            parts.append("-%s %s" % (k, _q(v)))
    return " ".join(parts)


def _tcl(script: str, timeout: float = 600) -> str:
    res = _call("POST", "/tcl", {"script": script, "timeout": timeout}, timeout=timeout + 30)
    lines = []
    result = (res.get("result") or "").strip()
    msgs = []
    for m in res.get("messages", []):
        m = re.sub(r"<[^>]+>", "", m or "").strip()
        if m and not m.startswith("[log]") and not m.startswith("[selected]") and m not in msgs:
            msgs.append(m)
    failed = (not res.get("ok")) or result.lower() == "fail" or result.startswith("Operation failed") \
        or result.startswith("Object not found") or any(m.startswith("[ERROR") for m in msgs)
    if not res.get("ok"):
        lines.append("ERROR: %s" % (res.get("error") or "unknown error").strip())
    elif failed:
        lines.append("ERROR: command failed" + ("" if result.lower() == "fail" else " (%s)" % result))
    elif result:
        lines.append("Result: %s" % result)
    if msgs:
        lines.append("Messages:\n" + "\n".join("  " + m for m in msgs[-25:]))
    if failed and not msgs and result.lower() == "fail":
        lines.append("Hint: check the object name with list_objects and options with tcl_help.")
    if res.get("still_running"):
        lines.append("Note: background processing is still running; check list_objects again shortly.")
    if not lines:
        lines.append("OK")
    return "\n".join(lines)


def _cmd(command: str, name: str, **kw) -> str:
    return _tcl("%s %s %s" % (command, _q(name), _opts(**kw)))


# ----------------------------------------------------------------------------------------------------------------
# app lifecycle
# ----------------------------------------------------------------------------------------------------------------
def _flatcam_exe_running(exe_name: str) -> bool:
    if not exe_name.lower().startswith("flatcamplus") or sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe_name, "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return exe_name.lower() in out.lower()
    except Exception:
        return False


@mcp.tool()
def launch_flatcam(headless: bool = False, wait_seconds: int = 180) -> str:
    """Start FlatCAM Plus with the automation bridge enabled (if it is not already running).
    headless=True runs without a window (still controllable, screenshots still work)."""
    st = _alive()
    if st:
        return "Already running (pid %s, headless=%s, %s objects)." % (st["pid"], st["headless"], st["objects"])
    args, cwd = _flatcam_command()
    # FlatCAM Plus is single-instance: a second start only forwards its arguments to the open window and exits,
    # so an instance opened without the bridge must be closed first.
    if _flatcam_exe_running(Path(args[0]).name):
        return ("FlatCAM Plus is already open without the MCP bridge. Ask the user to save their work, close it, "
                "and start it from the 'FlatCAM Plus (MCP bridge)' shortcut (or let launch_flatcam start it).")
    env = dict(os.environ, FLATCAM_MCP_BRIDGE="1")
    args.append("--mcp=1")
    if headless:
        args.append("--headless=1")
    for p in _discovery_candidates():
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(args, cwd=cwd, env=env, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1)
        st = _alive()
        if st:
            return "FlatCAM Plus started (pid %s, headless=%s)." % (st["pid"], st["headless"])
    return "FlatCAM Plus did not report ready within %d s. It may still be starting; call status." % wait_seconds


@mcp.tool()
def status() -> str:
    """Report whether FlatCAM Plus is running with the bridge, and whether it is busy."""
    st = _alive()
    if not st:
        return "Not running (or bridge disabled). Use launch_flatcam."
    return json.dumps(st)


@mcp.tool()
def quit_flatcam() -> str:
    """Close FlatCAM Plus (unsaved work is lost; save_project first if needed)."""
    try:
        _call("POST", "/tcl", {"script": "quit_app", "timeout": 5}, timeout=10)
    except Exception:
        pass
    return "Quit requested."


# ----------------------------------------------------------------------------------------------------------------
# generic
# ----------------------------------------------------------------------------------------------------------------
@mcp.tool()
def run_tcl(script: str, timeout: int = 600) -> str:
    """Run raw FlatCAM Tcl commands (one per line). Use tcl_help to discover commands and options.
    Paths must use forward slashes."""
    return _tcl(script, timeout)


@mcp.tool()
def tcl_help(command: str = "") -> str:
    """Show FlatCAM Tcl help. Empty = list all commands; otherwise detailed help for one command."""
    return _tcl("help %s" % _q(command) if command else "help")


@mcp.tool()
def list_objects() -> str:
    """List loaded objects (name, kind gerber/excellon/geometry/cncjob, bounds xmin,ymin,xmax,ymax) and units."""
    res = _call("GET", "/objects", timeout=30)
    if not res.get("ok"):
        return "ERROR: %s" % res.get("error")
    res.pop("ok", None)
    return json.dumps(res, indent=1)


@mcp.tool()
def screenshot(fit: bool = True) -> Image:
    """Return a PNG of the FlatCAM plot canvas. fit=True zooms to fit all objects first."""
    res = _call("POST", "/screenshot", {"fit": fit}, timeout=90)
    if not res.get("ok"):
        raise RuntimeError(res.get("error"))
    import base64
    return Image(data=base64.b64decode(res["png_base64"]), format="png")


@mcp.tool()
def list_preprocessors() -> str:
    """List G-code preprocessors (e.g. GRBL_11, GRBL_laser, Marlin_laser_FAN_pin, default)."""
    return _tcl("list_pp")


@mcp.tool()
def delete_object(name: str) -> str:
    """Delete an object by name."""
    return _tcl("delete %s" % _q(name))


@mcp.tool()
def new_project() -> str:
    """Clear everything and start a new empty project."""
    return _tcl("new")


@mcp.tool()
def save_project(filename: str) -> str:
    """Save the current project (.FlatPrj)."""
    return _tcl("save_project %s" % _path(filename))


# ----------------------------------------------------------------------------------------------------------------
# input
# ----------------------------------------------------------------------------------------------------------------
@mcp.tool()
def open_files(paths: list[str], kind: str = "auto") -> str:
    """Open Gerber / Excellon / G-code / SVG / DXF files. kind: auto|gerber|excellon|gcode|svg|dxf.
    Object names default to the file name."""
    out = []
    for p in paths:
        ext = Path(p).suffix.lower()
        k = kind
        if k == "auto":
            if ext in GERBER_EXT:
                k = "gerber"
            elif ext in EXCELLON_EXT:
                k = "excellon"
            elif ext in GCODE_EXT:
                k = "gcode"
            elif ext == ".svg":
                k = "svg"
            elif ext == ".dxf":
                k = "dxf"
            else:
                out.append("%s: unknown extension, pass kind explicitly" % p)
                continue
        out.append("%s -> %s\n%s" % (Path(p).name, k, _tcl("open_%s %s" % (k, _path(p)))))
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------------------------
# geometry operations
# ----------------------------------------------------------------------------------------------------------------
@mcp.tool()
def isolate(name: str, dia: float, passes: int = 1, overlap: float = 10, combine: bool = True,
            iso_type: int = 2, outname: Optional[str] = None) -> str:
    """Isolation routing around copper of a Gerber object. dia = tool diameter, overlap in %.
    iso_type: 0=exterior, 1=interior, 2=full. Produces a Geometry object."""
    return _cmd("isolate", name, dia=dia, passes=passes, overlap=overlap,
                combine="True" if combine else "False", iso_type=iso_type, outname=outname)


@mcp.tool()
def ncc(name: str, tooldia: str, overlap: float = 40, margin: float = 1.0, method: str = "lines",
        connect: bool = True, contour: bool = True, offset: Optional[float] = None,
        rest: bool = False, outname: Optional[str] = None) -> str:
    """Non-Copper Clearing: fill everything inside the board area EXCEPT the Gerber's polygons.
    Use on copper to remove all unused copper, or on a solder-mask Gerber to expose (cure) the whole mask
    except the pad openings with a UV laser. tooldia can be a list '0.8,0.2'. method: standard|seed|lines|combo.
    overlap in %."""
    return _cmd("ncc", name, tooldia=tooldia, overlap=overlap, margin=margin, method=method,
                connect="1" if connect else "0", contour="1" if contour else "0", offset=offset,
                rest="1" if rest else None, all=True, outname=outname)


@mcp.tool()
def paint(name: str, tooldia: str, overlap: float = 40, method: str = "lines", offset: Optional[float] = None,
          connect: bool = True, contour: bool = True, outname: Optional[str] = None) -> str:
    """Fill the INSIDE of all polygons of a Gerber/Geometry (e.g. laser-ablate mask over pad openings,
    or fill-engrave). tooldia can be '0.1'. method: standard|seed|lines|combo. overlap in %."""
    return _cmd("paint", name, tooldia=tooldia, overlap=overlap, method=method, offset=offset,
                connect="1" if connect else "0", contour="1" if contour else "0", all=True, outname=outname)


@mcp.tool()
def follow(name: str, outname: Optional[str] = None) -> str:
    """Create a Geometry that follows the centerlines of Gerber traces (useful for pen plotting / thin laser)."""
    return _cmd("follow", name, outname=outname)


@mcp.tool()
def cutout(name: str, dia: float, margin: float = 0.1, gapsize: float = 1.0, gaps: str = "4",
           type: str = "rect", outname: Optional[str] = None) -> str:
    """Board outline cutout with holding tabs. gaps: lr|tb|2lr|2tb|4|8. type: rect|any."""
    return _cmd("cutout", name, dia=dia, margin=margin, gapsize=gapsize, gaps=gaps, type=type, outname=outname)


@mcp.tool()
def mill_drills(name: str, tooldia: float, milled_dias: str = "all", outname: Optional[str] = None) -> str:
    """Turn Excellon holes larger than the tool into milling geometry. milled_dias: 'all' or '1.0,1.2'."""
    return _cmd("milldrills", name, tooldia=tooldia, milled_dias=milled_dias, outname=outname)


@mcp.tool()
def mirror(name: str, axis: str = "Y", box: Optional[str] = None, origin: Optional[str] = None) -> str:
    """Mirror an object (for bottom layers / double-sided). axis X or Y; box = name of a reference object
    whose center is used, or origin = 'x,y' point the mirror axis passes through."""
    return _cmd("mirror", name, axis=axis, box=box, origin=origin)


@mcp.tool()
def offset(name: str, x: float, y: float) -> str:
    """Move an object by x, y."""
    return _cmd("offset", name, x=x, y=y)


# ----------------------------------------------------------------------------------------------------------------
# CNC jobs and export
# ----------------------------------------------------------------------------------------------------------------
@mcp.tool()
def cncjob(name: str, pp: str = "GRBL_11", dia: Optional[float] = None, z_cut: Optional[float] = None,
           z_move: Optional[float] = None, feedrate: Optional[float] = None, feedrate_z: Optional[float] = None,
           feedrate_rapid: Optional[float] = None, spindlespeed: Optional[float] = None,
           dpp: Optional[float] = None, las_power: Optional[float] = None, las_min_pwr: Optional[float] = None,
           dwelltime: Optional[float] = None, endz: Optional[float] = None, outname: Optional[str] = None) -> str:
    """Create a CNC job from a Geometry object. Milling: set z_cut (negative), z_move, feedrate, spindlespeed,
    optional dpp (depth per pass). Laser: use a laser pp (GRBL_laser, GRBL_laser_z, Marlin_laser_FAN_pin,
    Marlin_laser_Spindle_pin, default_laser) and las_power (S value), feedrate."""
    return _cmd("cncjob", name, pp=pp, dia=dia, z_cut=z_cut, z_move=z_move, feedrate=feedrate,
                feedrate_z=feedrate_z, feedrate_rapid=feedrate_rapid, spindlespeed=spindlespeed, dpp=dpp,
                las_power=las_power, las_min_pwr=las_min_pwr, dwelltime=dwelltime, endz=endz, outname=outname)


@mcp.tool()
def drill_cncjob(name: str, drillz: float, travelz: float = 2.0, feedrate_z: float = 100,
                 spindlespeed: Optional[float] = None, pp: str = "GRBL_11", drilled_dias: Optional[str] = None,
                 dpp: Optional[float] = None, endz: Optional[float] = None, toolchangez: Optional[float] = None,
                 outname: Optional[str] = None) -> str:
    """Create a drilling CNC job from an Excellon object. drillz negative. drilled_dias e.g. '0.8,1.0'
    (default all)."""
    return _cmd("drillcncjob", name, drillz=drillz, travelz=travelz, feedrate_z=feedrate_z,
                spindlespeed=spindlespeed, pp=pp, drilled_dias=drilled_dias, dpp=dpp, endz=endz,
                toolchangez=toolchangez, outname=outname)


@mcp.tool()
def export_gcode(name: str, filename: str, preamble: Optional[str] = None, postamble: Optional[str] = None) -> str:
    """Write a CNCJob object's G-code to a file."""
    r = _tcl("write_gcode %s %s %s" % (_q(name), _path(filename), _opts(preamble=preamble, postamble=postamble)))
    p = Path(filename).expanduser()
    if p.exists():
        r += "\nWritten %s (%d bytes)" % (p.resolve(), p.stat().st_size)
    return r


# ----------------------------------------------------------------------------------------------------------------
# photo films (toner/inkjet transparency for UV exposure)
# ----------------------------------------------------------------------------------------------------------------
def _films_local(payload: dict) -> dict:
    """Run the film generator in-process (source checkout only)."""
    if not (REPO_DIR / "appCommon" / "PhotoFilm.py").exists():
        raise RuntimeError("FlatCAM Plus with film support is not running. Call launch_flatcam first "
                           "(film export needs FlatCAM Plus v1.1.0-mcp.3 or newer).")
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    from appCommon.PhotoFilm import export_films as _export
    return _export(payload["films"], payload["out"], payload.get("outline"), payload.get("drill"),
                   payload.get("name", ""), payload.get("copies", 2), payload.get("mark_dia", 0.4))


@mcp.tool()
def export_films(films: list[dict], out_pdf: str, outline: Optional[str] = None, drill: Optional[str] = None,
                 name: str = "", copies: int = 2, mark_dia: float = 0.4) -> str:
    """Create printable photo films (A4 PDF, true 1:1 scale) for UV exposure of copper and solder mask,
    straight from Gerber/Excellon FILES (objects loaded in FlatCAM are not needed).

    films: list of {"gerber": path, "kind": "copper"|"mask", "side": "top"|"bottom", "polarity": "neg"|"pos"}.
      copper "neg" = negative film for negative-acting dry-film photoresist (copper clear);
      copper "pos" = positive film for positive photoresist (copper black);
      mask = UV-curable solder mask film (pad openings black). polarity is ignored for masks.
    Top layers are mirrored and bottom layers are not (printed side against the board, Gerbers in top view).
    outline: Edge_Cuts Gerber (board size and frame); drill: Excellon file, adds drill-centre marks of
    mark_dia mm on copper films. copies: copies of each film for stacking (density).
    The page has a 50 mm scale bar and corner registration marks; print at 100% / actual size."""
    payload = {"films": films, "out": str(Path(out_pdf).expanduser().resolve()), "outline": outline,
               "drill": drill, "name": name, "copies": copies, "mark_dia": mark_dia}
    res = None
    if _alive():
        res = _call("POST", "/film", payload, timeout=600)
        if not res.get("ok") and res.get("error") == "not found":
            res = None  # FlatCAM Plus build without film support
        elif not res.get("ok"):
            return "ERROR: %s" % res.get("error")
    if res is None:
        res = _films_local(payload)
    lines = ["Written %s" % res["pdf"], "Board %.2f x %.2f mm, %d copies per film:" % (
        res["board_mm"][0], res["board_mm"][1], res["copies"])]
    lines += ["  - %s%s" % (f["title"], " (mirrored)" if f["mirrored"] else "") for f in res["films"]]
    lines.append("Print at 100% / actual size and check the 50 mm scale bar.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

"""
End-to-end smoke test for FlatCAM Plus MCP.

Starts the MCP server (frozen FlatCAMPlusMCP.exe or `python server.py`) as an MCP client would, lets it launch
FlatCAM Plus with the bridge enabled, and runs a small PCB job:
open Gerber -> isolate -> cncjob -> export G-code (+ a laser job on NCC geometry) -> list objects -> quit.

Usage:
    python smoke_test.py <server command...> --gerber <file.gbr> --out <dir> [--headless] [--screenshot]

Exit code 0 on success, 1 on failure.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server", nargs="+", help="command that starts the MCP server")
    ap.add_argument("--gerber", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--screenshot", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(a.server, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    failures = []

    def watchdog():
        failures.append("timeout after %d s" % a.timeout)
        proc.kill()

    timer = threading.Timer(a.timeout, watchdog)
    timer.start()
    counter = [0]

    def rpc(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        if notify:
            return None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed the connection")
            r = json.loads(line)
            if r.get("id") == counter[0]:
                return r

    def tool(_name, **args):
        r = rpc("tools/call", {"name": _name, "arguments": args})
        if "error" in r:
            raise RuntimeError("%s: RPC error %s" % (_name, r["error"]))
        res = r["result"]
        content = res["content"][0]
        if content["type"] == "image":
            return content
        text = content["text"]
        print("--- %s %s\n%s" % (_name, json.dumps(args), text[:1500]), flush=True)
        if res.get("isError"):
            raise RuntimeError("%s failed: %s" % (_name, text[:500]))
        return text

    def check(cond, msg):
        if not cond:
            failures.append(msg)
            print("FAIL: " + msg, flush=True)

    try:
        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "smoke-test", "version": "1"}})
        check(init["result"]["serverInfo"]["name"] == "FlatCAM Plus MCP", "unexpected server name")
        rpc("notifications/initialized", notify=True)
        names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
        for required in ("launch_flatcam", "open_files", "isolate", "ncc", "cncjob", "export_gcode", "list_objects"):
            check(required in names, "missing tool %s" % required)

        t = tool("launch_flatcam", headless=a.headless, wait_seconds=300)
        check("started" in t or "Already running" in t, "FlatCAM Plus did not start")

        t = tool("open_files", paths=[str(Path(a.gerber).resolve())])
        check("Opened" in t and "ERROR" not in t, "open_files failed")

        t = tool("isolate", name=Path(a.gerber).name, dia=0.2, passes=2, outname="iso")
        check("ERROR" not in t, "isolate failed")

        t = tool("cncjob", name="iso", pp="GRBL_11", z_cut=-0.05, z_move=2, feedrate=200, spindlespeed=10000,
                 outname="iso_cnc")
        check("CNCjob created" in t and "ERROR" not in t, "milling cncjob failed")

        t = tool("ncc", name=Path(a.gerber).name, tooldia="0.15", overlap=30, outname="fill")
        check("ERROR" not in t, "ncc failed")
        t = tool("cncjob", name="fill", pp="GRBL_laser", feedrate=1500, las_power=300, outname="laser_cnc")
        check("CNCjob created" in t and "ERROR" not in t, "laser cncjob failed")

        for obj, fname, marker in (("iso_cnc", "iso.nc", "M03 S10000"), ("laser_cnc", "laser.nc", "M3 S300")):
            path = out / fname
            if path.exists():
                path.unlink()
            tool("export_gcode", name=obj, filename=str(path))
            check(path.exists() and path.stat().st_size > 1000, "%s not written" % fname)
            if path.exists():
                body = path.read_text(errors="replace")
                check(marker in body and "G1" in body.replace("G01", "G1"), "%s lacks expected G-code" % fname)

        objs = json.loads(tool("list_objects"))
        kinds = sorted(o["kind"] for o in objs["objects"])
        check(kinds.count("cncjob") == 2 and kinds.count("geometry") == 2 and "gerber" in kinds,
              "unexpected objects: %s" % kinds)

        if a.screenshot:
            img = tool("screenshot", fit=True)
            data = base64.b64decode(img["data"])
            (out / "screenshot.png").write_bytes(data)
            check(len(data) > 5000, "screenshot too small")

        t = tool("run_tcl", script="isolate no_such_object -dia 0.1")
        check(t.startswith("ERROR"), "errors are not reported")
    except Exception as e:
        failures.append("%s: %s" % (type(e).__name__, e))
    finally:
        try:
            tool("quit_flatcam")
        except Exception:
            pass
        timer.cancel()
        proc.kill()

    if failures:
        print("\nSMOKE TEST FAILED:\n  " + "\n  ".join(failures), flush=True)
        return 1
    print("\nSMOKE TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

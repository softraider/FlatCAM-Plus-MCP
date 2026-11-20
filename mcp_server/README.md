# FlatCAM Plus MCP

An MCP server that lets an AI assistant (Claude Code, Claude Desktop or any MCP client) drive FlatCAM Plus:
open Gerber/Excellon files, generate isolation, NCC, paint, follow and cutout geometry, create milling,
drilling and laser CNC jobs, and export G-code. Results can be checked with object lists and canvas screenshots.

## How it works

```
MCP client  <--stdio-->  mcp_server/server.py  <--HTTP 127.0.0.1 + token-->  FlatCAM Plus (appCommon/MCPBridge.py)
```

* `appCommon/MCPBridge.py` runs inside FlatCAM Plus. It executes Tcl commands in the GUI thread (the same
  command layer as the built-in Tcl Shell), waits for background jobs, redraws the canvas and returns
  the result and status messages.
* The bridge starts only when FlatCAM Plus is started with `--mcp=1` or with `FLATCAM_MCP_BRIDGE=1` set
  (`run_flatcam_mcp.bat` in a source checkout). It listens on
  127.0.0.1 only, on a random port, and every request needs a random per-session token. Port and token are
  stored in `%APPDATA%\FlatCAMPlusMCP\bridge.json`.
* The MCP server can also start FlatCAM Plus itself (`launch_flatcam`), with a window or headless.

## Safety

The server does **not** expose machine control (serial/TCP streaming, jogging, probing, SD jobs).
Generated G-code is only written to files; sending it to a machine stays a manual step.

## Setup with the Windows installer

The release installer puts `FlatCAMPlusMCP.exe` next to `FlatCAMPlus.exe` (no Python needed) and adds a
Start menu shortcut **FlatCAM Plus (MCP bridge)** (`FlatCAMPlus.exe --mcp=1`).

```powershell
claude mcp add --scope user flatcam-plus-mcp -- "C:\Program Files\FlatCAMPlus\FlatCAMPlusMCP.exe"
```

If the app is not running, the `launch_flatcam` tool starts it with the bridge enabled. Set the
`FLATCAM_PLUS_EXE` environment variable if FlatCAM Plus is installed somewhere else.

## Setup from source

```powershell
cd <repo>
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r mcp_server\requirements.txt
```

(GDAL from `requirements.txt` is not needed for this and can be skipped.)

Claude Code:

```powershell
claude mcp add --scope user flatcam-plus-mcp -- <repo>\.venv\Scripts\python.exe <repo>\mcp_server\server.py
```

Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "flatcam-plus-mcp": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": ["<repo>\\mcp_server\\server.py"]
    }
  }
}
```

## Tools

| Tool | Purpose |
| --- | --- |
| `launch_flatcam`, `status`, `quit_flatcam` | Start / check / close the app |
| `open_files` | Open Gerber, Excellon, G-code, SVG, DXF (type detected by extension) |
| `list_objects`, `screenshot` | Inspect loaded objects (kind, bounds, drill tools) and see the canvas |
| `isolate`, `ncc`, `paint`, `follow`, `cutout`, `mill_drills` | Geometry generation |
| `mirror`, `offset`, `delete_object` | Object transforms |
| `cncjob` | Geometry to CNC job (milling or laser, any preprocessor) |
| `drill_cncjob` | Excellon to drilling CNC job |
| `list_preprocessors`, `export_gcode` | Preprocessors and G-code export |
| `new_project`, `save_project` | Project handling |
| `run_tcl`, `tcl_help` | Any other FlatCAM Tcl command |

## Example workflows

* **Milled PCB:** `open_files` -> `isolate` (dia 0.2, passes 2) -> `cncjob` (pp GRBL_11, z_cut -0.05)
  -> `drill_cncjob` -> `cutout` -> `cncjob` -> `export_gcode`.
* **UV solder-mask exposure with a laser:** open the mask layer (`.GTS`/`.GBS`) -> `ncc` to fill everything
  except the openings (or `paint` to fill the openings) -> `cncjob` with `pp GRBL_laser`, `las_power`,
  `feedrate` -> `export_gcode`. Tune power, speed and overlap on test squares first.

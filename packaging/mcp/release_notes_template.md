## FlatCAM Plus {VERSION} + FlatCAM Plus MCP

Unofficial build of [FlatCAM Plus](https://github.com/thebestgoodguy/FlatCAM-Plus) {VERSION} with **FlatCAM Plus MCP**: an
[MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude Code, Claude Desktop, or any MCP client)
drive FlatCAM Plus for PCB CAM work.

### What you can ask the assistant to do

* Open Gerber / Excellon / G-code / SVG / DXF files
* Isolation routing, non-copper clearing (NCC), paint, follow, cutout with tabs, milling of large holes
* Milling, drilling and **laser** CNC jobs with any FlatCAM preprocessor (GRBL, Marlin, GRBL_laser, ...)
* UV solder-mask / photoresist exposure paths with a laser (NCC or paint on the mask layer)
* Export G-code, list objects and bounds, take canvas screenshots, run any FlatCAM Tcl command

### Safety

The MCP server has **no machine-control tools** (no serial/TCP streaming, jogging, probing). It only creates
files. Sending G-code to a machine stays a manual step in the FlatCAM Plus UI.

The bridge inside FlatCAM Plus is off by default. It is enabled only when the app is started with `--mcp=1`
(Start menu shortcut **FlatCAM Plus (MCP bridge)**, or automatically when the MCP server launches the app).
It listens on 127.0.0.1 only and requires a random per-session token.

### Install

1. Run `{INSTALLER}`. It installs into `C:\Program Files\FlatCAMPlus` and replaces an
   existing FlatCAM Plus installation. `FlatCAMPlusMCP.exe` is installed next to `FlatCAMPlus.exe`.
2. Register the MCP server:

   **Claude Code**
   ```powershell
   claude mcp add --scope user flatcam-plus-mcp -- "C:\Program Files\FlatCAMPlus\FlatCAMPlusMCP.exe"
   ```

   **Claude Desktop**: add to `%APPDATA%\Claude\claude_desktop_config.json`
   ```json
   {
     "mcpServers": {
       "flatcam-plus-mcp": { "command": "C:\\Program Files\\FlatCAMPlus\\FlatCAMPlusMCP.exe" }
     }
   }
   ```
3. Ask the assistant, for example: *"Open D:\pcb\board-F_Cu.gbr and board.drl, isolate with a 0.2 mm V-bit in
   2 passes, drill, cut out with 4 tabs, and export GRBL G-code."*

The installer is not code-signed, so Windows SmartScreen may show a warning.

### Source

Branch [`feature/flatcam-plus-mcp`](https://github.com/softraider/FlatCAM-Plus-MCP/tree/feature/flatcam-plus-mcp). Details in
[`mcp_server/README.md`](https://github.com/softraider/FlatCAM-Plus-MCP/blob/feature/flatcam-plus-mcp/mcp_server/README.md).
All credit for FlatCAM Plus goes to its authors; this release only adds the MCP bridge and server.

### Checksum

`{INSTALLER}` SHA-256: `{SHA256}`

### Upstream

Built from upstream [thebestgoodguy/FlatCAM-Plus@{UPSTREAM_SHORT}](https://github.com/thebestgoodguy/FlatCAM-Plus/commit/{UPSTREAM_SHA}) merged with the MCP branch at {MCP_SHORT}. The automated smoke test (open Gerber, isolation, milling and laser CNC jobs, G-code export) passed for this build.

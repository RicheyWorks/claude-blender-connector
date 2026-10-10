# Claude Blender Connector

Lets Claude work inside your open Blender session: read the scene, run Python, screenshot the viewport and render.

It has two parts:

- **Blender add-on** (`addon/claude_blender_bridge`) — runs a small server inside Blender on `127.0.0.1:9876`. Commands run on Blender's main thread.
- **Claude desktop extension** (`extension/`) — an MCP server with no dependencies, packaged as a `.mcpb`. It passes Claude's tool calls to the add-on.

## Install (Windows)

Close Blender, then paste this into PowerShell:

```powershell
irm https://raw.githubusercontent.com/RicheyWorks/claude-blender-connector/main/install.ps1 | iex
```

It downloads the latest release, installs and enables the add-on, reopens Blender, and opens the Claude extension installer. If Windows asks which app should open the `.mcpb` file, pick **Claude**, then click **Install**.

To check it worked, press **N** in Blender's 3D Viewport and open the **Claude** tab. It should say *Listening on 127.0.0.1:9876*.

### Manual install (any OS)

1. Download `claude_blender_bridge.zip` and `blender-connector.mcpb` from [Releases](../../releases/latest).
2. In Blender, go to **Edit → Preferences → Add-ons**, choose **Install from Disk…**, pick the zip, and tick the add-on to enable it.
3. Double-click the `.mcpb` file to install it in the Claude desktop app.

## Tools

| Tool | What it does |
|---|---|
| `blender_status` | Confirms Blender is open and reports its version and the open file |
| `get_scene_info` | Lists objects, the camera, render settings, the frame range, collections and materials |
| `get_object_info` | Gives the transform, dimensions, materials, modifiers and mesh/camera/light data for one object |
| `execute_python` | Runs Python in Blender with `bpy` loaded. Returns stdout and a `result` variable |
| `viewport_screenshot` | Captures the 3D Viewport offscreen, so it works even when Blender is behind other windows |
| `render_image` | Renders through the active camera, saves a PNG and returns a preview |

Try asking Claude something like: *"In Blender, build a low-poly forest at sunset and render it."*

## Security

`execute_python` runs any Python it is sent, with your user's permissions. The add-on only listens on `127.0.0.1`, so other machines can't reach it. Any program on your own computer can, though, while the bridge is running. To turn it off, click **Stop** in the Claude tab, or untick **Start with Blender** in the add-on preferences.

## Settings

- **Port** — set it in Blender's Claude tab and in the extension's **Configure** page in Claude. The two must match (default `9876`).
- **Start with Blender** — starts listening automatically when Blender opens.

## Development

The tests run the add-on inside Blender (the `bpy` module from PyPI) and drive it through the MCP server over stdio:

```bash
pip install bpy
python3 tests/host.py dist/claude_blender_bridge.zip 9911 45 &
node tests/client.js 9911
```

To build the release files:

```bash
(cd addon && zip -r ../dist/claude_blender_bridge.zip claude_blender_bridge -x '*__pycache__*')
npx @anthropic-ai/mcpb pack extension dist/blender-connector.mcpb
```

Supports Blender 3.6+ (tested on 4.3.1 and 5.2.2) and Node 18+ (the Claude desktop app ships with its own).

## License

MIT

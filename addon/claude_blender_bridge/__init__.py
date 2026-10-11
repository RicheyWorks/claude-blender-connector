"""Claude Blender Bridge.

Runs a small JSON-over-TCP server inside Blender, bound to 127.0.0.1 only.
The matching Claude desktop extension connects to it and sends commands.
Every command is executed on Blender's main thread through an app timer,
because bpy is not safe to call from worker threads.

Wire format: one JSON object per line.
  request : {"id": <any>, "type": "<command>", "params": {...}}
  response: {"id": <same>, "status": "ok", "result": ...}
            {"id": <same>, "status": "error", "message": "...", "traceback": "..."}
"""

bl_info = {
    "name": "Claude Blender Bridge",
    "author": "Richmond + Claude",
    "version": (1, 0, 2),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Claude",
    "description": "Lets Claude inspect the scene, run Python, take viewport screenshots and render",
    "category": "Interface",
}

import base64
import contextlib
import io
import json
import os
import queue
import socket
import tempfile
import threading
import traceback

import bpy
from bpy.app.handlers import persistent

DEFAULT_PORT = 9876
HOST = "127.0.0.1"
MAX_LINE = 64 * 1024 * 1024
MAIN_THREAD_TIMEOUT = 1800.0  # seconds a client waits for the main thread (renders are slow)

_jobs = queue.Queue()
_server = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _vec(v, nd=4):
    return [round(float(x), nd) for x in v]


def _png_b64(path, max_size):
    """Return (base64 png, width, height), downscaled so the long edge <= max_size."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            raise RuntimeError("image has no pixels: " + path)
        long_edge = max(w, h)
        if max_size and long_edge > max_size:
            s = max_size / float(long_edge)
            w, h = max(1, int(w * s)), max(1, int(h * s))
            img.scale(w, h)
        out = os.path.join(tempfile.gettempdir(), "claude_bridge_preview_%d.png" % os.getpid())
        img.file_format = "PNG"
        img.filepath_raw = out
        img.save()
        with open(out, "rb") as f:
            data = f.read()
        with contextlib.suppress(OSError):
            os.remove(out)
        return base64.b64encode(data).decode("ascii"), w, h
    finally:
        bpy.data.images.remove(img)


# --------------------------------------------------------------------------
# commands (all run on the main thread)
# --------------------------------------------------------------------------

def cmd_ping(_p):
    return {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "background": bool(bpy.app.background),
        "addon": ".".join(str(x) for x in bl_info["version"]),
    }


def cmd_get_scene_info(p):
    sc = bpy.context.scene
    limit = int(p.get("limit", 200))
    objs = []
    for i, ob in enumerate(sc.objects):
        if i >= limit:
            break
        objs.append({
            "name": ob.name,
            "type": ob.type,
            "location": _vec(ob.location),
            "visible": not ob.hide_viewport and not ob.hide_get() if ob.name in bpy.context.view_layer.objects else not ob.hide_viewport,
            "parent": ob.parent.name if ob.parent else None,
            "collections": [c.name for c in ob.users_collection],
        })
    active = bpy.context.view_layer.objects.active
    return {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "scene": sc.name,
        "object_count": len(sc.objects),
        "objects_listed": len(objs),
        "objects": objs,
        "active_object": active.name if active else None,
        "selected": [o.name for o in sc.objects if o.select_get()] if not bpy.app.background else [],
        "camera": sc.camera.name if sc.camera else None,
        "render": {
            "engine": sc.render.engine,
            "resolution": [sc.render.resolution_x, sc.render.resolution_y],
            "resolution_percentage": sc.render.resolution_percentage,
            "fps": sc.render.fps,
        },
        "frame": {"current": sc.frame_current, "start": sc.frame_start, "end": sc.frame_end},
        "collections": [c.name for c in bpy.data.collections],
        "materials": [m.name for m in bpy.data.materials][:200],
        "units": sc.unit_settings.system,
    }


def cmd_get_object_info(p):
    name = p.get("name")
    ob = bpy.data.objects.get(name) if name else None
    if ob is None:
        raise ValueError("no object named %r" % (name,))
    info = {
        "name": ob.name,
        "type": ob.type,
        "location": _vec(ob.location),
        "rotation_euler": _vec(ob.rotation_euler),
        "scale": _vec(ob.scale),
        "dimensions": _vec(ob.dimensions),
        "parent": ob.parent.name if ob.parent else None,
        "children": [c.name for c in ob.children],
        "collections": [c.name for c in ob.users_collection],
        "materials": [s.material.name if s.material else None for s in ob.material_slots],
        "modifiers": [{"name": m.name, "type": m.type} for m in ob.modifiers],
        "hide_viewport": ob.hide_viewport,
        "hide_render": ob.hide_render,
        "custom_properties": {k: repr(ob[k]) for k in ob.keys() if not k.startswith("_")},
    }
    if ob.type == "MESH":
        me = ob.data
        info["mesh"] = {"vertices": len(me.vertices), "edges": len(me.edges), "polygons": len(me.polygons)}
    elif ob.type == "CAMERA":
        info["camera"] = {"lens": ob.data.lens, "type": ob.data.type, "clip": [ob.data.clip_start, ob.data.clip_end]}
    elif ob.type == "LIGHT":
        info["light"] = {"type": ob.data.type, "energy": ob.data.energy, "color": _vec(ob.data.color)}
    elif ob.type == "CURVE":
        info["curve"] = {"splines": len(ob.data.splines), "bevel_depth": ob.data.bevel_depth}
    return info


def cmd_execute_code(p):
    code = p.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("'code' must be a non-empty string")
    ns = {"bpy": bpy, "__name__": "__claude__"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<claude>", "exec"), ns)
    res = ns.get("result")
    try:
        json.dumps(res)
    except (TypeError, ValueError):
        res = repr(res)
    out = buf.getvalue()
    if len(out) > 100000:
        out = out[:100000] + "\n... [stdout truncated]"
    return {"stdout": out, "result": res}


def _find_view3d():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return win, area, region
    return None, None, None


def cmd_viewport_screenshot(p):
    """Draw the 3D Viewport offscreen (OpenGL render of the view) at its on-screen size.

    screen.screenshot_area reads the front buffer, which comes back black on Windows
    when Blender is behind other windows, so the view is re-drawn offscreen instead.
    Scene output settings are restored afterwards.
    """
    if bpy.app.background:
        raise RuntimeError("Blender is running without a window; use render_image instead")
    win, area, region = _find_view3d()
    if area is None or region is None:
        raise RuntimeError("no 3D Viewport is open in Blender")
    r = bpy.context.scene.render
    saved = (r.filepath, r.image_settings.file_format, r.resolution_x, r.resolution_y, r.resolution_percentage)
    path = os.path.join(tempfile.gettempdir(), "claude_bridge_shot_%d.png" % os.getpid())
    try:
        r.filepath = path
        r.image_settings.file_format = "PNG"
        r.resolution_x, r.resolution_y, r.resolution_percentage = max(region.width, 16), max(region.height, 16), 100
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        r.filepath, r.image_settings.file_format, r.resolution_x, r.resolution_y, r.resolution_percentage = saved
    try:
        b64, w, h = _png_b64(path, int(p.get("max_size", 1200)))
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
    return {"image_png_base64": b64, "width": w, "height": h}


def cmd_render_image(p):
    sc = bpy.context.scene
    if sc.camera is None:
        raise RuntimeError("the scene has no active camera; add one and set scene.camera first")
    r = sc.render
    saved = (r.filepath, r.image_settings.file_format, r.resolution_percentage)
    out = p.get("output_path") or os.path.join(tempfile.gettempdir(), "claude_bridge_render.png")
    out = bpy.path.abspath(out)
    try:
        r.filepath = out
        r.image_settings.file_format = "PNG"
        if p.get("resolution_percentage"):
            r.resolution_percentage = int(p["resolution_percentage"])
        bpy.ops.render.render(write_still=True)
    finally:
        r.filepath, r.image_settings.file_format, r.resolution_percentage = saved
    if not os.path.exists(out):
        raise RuntimeError("render finished but no file was written at " + out)
    b64, w, h = _png_b64(out, int(p.get("max_size", 1200)))
    return {"image_png_base64": b64, "width": w, "height": h, "saved_to": out, "engine": r.engine}


COMMANDS = {
    "ping": cmd_ping,
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "execute_code": cmd_execute_code,
    "viewport_screenshot": cmd_viewport_screenshot,
    "render_image": cmd_render_image,
}


def _run(req):
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        fn = COMMANDS.get(req.get("type"))
        if fn is None:
            raise ValueError("unknown command %r; available: %s" % (req.get("type"), ", ".join(sorted(COMMANDS))))
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        return {"id": rid, "status": "ok", "result": fn(params)}
    except Exception as e:  # report every failure back to the client
        return {"id": rid, "status": "error", "message": "%s: %s" % (type(e).__name__, e), "traceback": traceback.format_exc()}


# --------------------------------------------------------------------------
# main-thread pump
# --------------------------------------------------------------------------

def drain(max_jobs=8):
    """Run queued jobs on the calling (main) thread. Returns how many ran."""
    n = 0
    while n < max_jobs:
        try:
            req, done, box = _jobs.get_nowait()
        except queue.Empty:
            break
        box.append(_run(req))
        done.set()
        n += 1
    return n


def _timer():
    drain()
    return 0.05


# --------------------------------------------------------------------------
# socket server (worker threads; never touches bpy)
# --------------------------------------------------------------------------

class BridgeServer:
    def __init__(self, port):
        self.port = port
        self.sock = None
        self.thread = None
        self.running = False
        self.clients = set()
        self.lock = threading.Lock()

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self.sock = s
        self.running = True
        self.thread = threading.Thread(target=self._accept, name="claude-bridge-accept", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        with contextlib.suppress(OSError):
            if self.sock:
                self.sock.close()
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                c.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)
        self.sock = None

    def _accept(self):
        while self.running:
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.clients.add(conn)
            threading.Thread(target=self._client, args=(conn,), name="claude-bridge-client", daemon=True).start()

    def _client(self, conn):
        buf = b""
        try:
            conn.settimeout(None)
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE:
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as e:
                        resp = {"id": None, "status": "error", "message": "invalid JSON: %s" % e}
                    else:
                        done, box = threading.Event(), []
                        _jobs.put((req, done, box))
                        if done.wait(MAIN_THREAD_TIMEOUT):
                            resp = box[0]
                        else:
                            resp = {"id": req.get("id") if isinstance(req, dict) else None, "status": "error",
                                    "message": "Blender's main thread did not answer in time (is it busy or showing a dialog?)"}
                    conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            with self.lock:
                self.clients.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()


def is_running():
    return _server is not None and _server.running


def start_server(port=None):
    global _server
    if is_running():
        return _server.port
    port = int(port or _pref_port())
    srv = BridgeServer(port)
    srv.start()
    _server = srv
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.1, persistent=True)
    return port


def stop_server():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def _prefs():
    ad = bpy.context.preferences.addons.get(__package__ or __name__)
    return ad.preferences if ad else None


def _pref_port():
    p = _prefs()
    return p.port if p else DEFAULT_PORT


class ClaudeBridgePrefs(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__

    port: bpy.props.IntProperty(name="Port", default=DEFAULT_PORT, min=1024, max=65535,
                                description="Local port the bridge listens on (must match the Claude extension setting)")
    autostart: bpy.props.BoolProperty(name="Start with Blender", default=True,
                                      description="Start listening automatically when Blender opens")

    def draw(self, _ctx):
        col = self.layout.column()
        col.prop(self, "port")
        col.prop(self, "autostart")


class CLAUDE_OT_bridge_start(bpy.types.Operator):
    bl_idname = "claude.bridge_start"
    bl_label = "Start Claude Bridge"
    bl_description = "Start listening for Claude on 127.0.0.1"

    def execute(self, _ctx):
        try:
            port = start_server()
        except OSError as e:
            self.report({"ERROR"}, "Could not listen on port %d: %s" % (_pref_port(), e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Claude bridge listening on %s:%d" % (HOST, port))
        return {"FINISHED"}


class CLAUDE_OT_bridge_stop(bpy.types.Operator):
    bl_idname = "claude.bridge_stop"
    bl_label = "Stop Claude Bridge"
    bl_description = "Stop listening for Claude"

    def execute(self, _ctx):
        stop_server()
        self.report({"INFO"}, "Claude bridge stopped")
        return {"FINISHED"}


class CLAUDE_PT_bridge(bpy.types.Panel):
    bl_label = "Claude Bridge"
    bl_idname = "CLAUDE_PT_bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, _ctx):
        col = self.layout.column()
        if is_running():
            col.label(text="Listening on %s:%d" % (HOST, _server.port), icon="CHECKMARK")
            col.operator("claude.bridge_stop", icon="PAUSE")
        else:
            col.label(text="Not running", icon="CANCEL")
            col.operator("claude.bridge_start", icon="PLAY")
        p = _prefs()
        if p:
            row = col.row()
            row.enabled = not is_running()
            row.prop(p, "port")
            col.prop(p, "autostart")


def _autostart():
    p = _prefs()
    if p is None or p.autostart:
        try:
            start_server()
        except OSError as e:
            print("[Claude Bridge] could not start on port %d: %s" % (_pref_port(), e))
    return None


@persistent
def _on_load(_dummy):
    # Loading a .blend (including the one Blender is launched with) drops
    # non-persistent timers, so re-check the pump and the autostart here.
    if is_running():
        if not bpy.app.timers.is_registered(_timer):
            bpy.app.timers.register(_timer, first_interval=0.1, persistent=True)
    else:
        _autostart()


_classes = (ClaudeBridgePrefs, CLAUDE_OT_bridge_start, CLAUDE_OT_bridge_stop, CLAUDE_PT_bridge)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_autostart, first_interval=0.5, persistent=True)


def unregister():
    stop_server()
    if bpy.app.timers.is_registered(_autostart):
        bpy.app.timers.unregister(_autostart)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)

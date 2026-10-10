# Test host: real Blender (bpy module), add-on installed from the zip, main-thread pump driven by hand.
import bpy, sys, time, importlib
zip_path, port, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
bpy.ops.wm.read_factory_settings()          # default cube/camera/light scene
bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
bpy.ops.preferences.addon_enable(module="claude_blender_bridge")
mod = importlib.import_module("claude_blender_bridge")
assert "claude_blender_bridge" in bpy.context.preferences.addons, "addon not enabled"
bpy.context.scene.render.engine = "CYCLES"; bpy.context.scene.cycles.samples = 8
bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = 640, 360
print("HOST listening on", mod.start_server(port), flush=True)
end = time.time() + secs
while time.time() < end:
    mod.drain(); time.sleep(0.02)
mod.stop_server(); bpy.ops.preferences.addon_disable(module="claude_blender_bridge")
print("HOST clean shutdown", flush=True)

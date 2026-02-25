bl_info = {
    "name": "Pseudo Rendering Farm",
    "description": "Spawns multiple background processes to render the current file.",
    "author": "Michael Klimenko",
    "version": (0, 8),
    "blender": (4, 2, 0),
    "location": "Properties > Render > Pseudo Rendering Farm",
    "category": "Render",
}

import bpy
import os
import shutil
import subprocess
import tempfile
import time


class Globals:
    active_render_processes = []
    is_rendering_active = False
    is_benchmarking = False
    early_exit_benchmark = False
    bench_status_msg = ""
    current_bench_instances = 1
    benchmark_frames = 1
    benchmark_results = {}
    bench_temp_dir = ""
    start_time = 0
    elapsed_time = 0
    seconds_per_frame = 0


def is_image_valid(filepath):
    """Checks if an image file is complete by looking for format-specific footers."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return False

    ext = os.path.splitext(filepath)[1].lower()

    try:
        with open(filepath, "rb") as f:
            f.seek(-10, 2)
            footer = f.read()

            if ext == ".png":
                return b"\xaeB`\x82" in footer
            elif ext in {".jpg", ".jpeg"}:
                return b"\xff\xd9" in footer
            elif ext == ".exr":
                return os.path.getsize(filepath) > 1000
    except Exception as e:
        print(f"Error checking {filepath}: {e}")
        return False

    return True


def cleanup_corrupted_frames():
    scene = bpy.context.scene
    output_path = bpy.path.abspath(scene.render.filepath)
    output_dir = os.path.dirname(output_path)

    if not os.path.exists(output_dir):
        return

    deleted_count = 0
    for filename in os.listdir(output_dir):
        file_path = os.path.join(output_dir, filename)
        if os.path.isfile(file_path):
            if not is_image_valid(file_path):
                try:
                    os.remove(file_path)
                    deleted_count += 1
                except Exception as e:
                    print(f"Failed to delete {filename}: {e}")

    return deleted_count


def cleanup_bench_dir():
    if Globals.bench_temp_dir and os.path.exists(Globals.bench_temp_dir):
        try:
            shutil.rmtree(Globals.bench_temp_dir)
        except:
            pass
        Globals.bench_temp_dir = ""


def check_render_status():
    for proc in Globals.active_render_processes[:]:
        if proc.poll() is not None:
            Globals.active_render_processes.remove(proc)

    if Globals.early_exit_benchmark or (
        Globals.is_benchmarking and not Globals.active_render_processes
    ):
        elapsed = time.time() - Globals.start_time
        throughput = Globals.benchmark_frames / max(elapsed, 0.001)

        Globals.benchmark_results[Globals.current_bench_instances] = throughput

        if (
            Globals.early_exit_benchmark
            or Globals.current_bench_instances >= 32
            or (
                Globals.current_bench_instances > 2
                and throughput < Globals.benchmark_results[1]
            )
        ):
            best_count = max(
                Globals.benchmark_results, key=Globals.benchmark_results.get
            )
            bpy.context.scene.pseudo_rendering_farm_instances = best_count

            Globals.is_benchmarking = False
            Globals.early_exit_benchmark = False
            Globals.bench_status_msg = f"Optimal found: {best_count}"
            cleanup_bench_dir()

            def draw_popup(self, context):
                self.layout.label(
                    text=f"Benchmark is complete, optimal number of instances is {best_count} with {1.0 / Globals.benchmark_results[best_count]:.1f} seconds per frame",
                    icon="CHECKMARK",
                )

            print(f"!!! Benchmarking stats for nerds !!!")
            print(Globals.benchmark_results)

            bpy.context.window_manager.popup_menu(
                draw_popup, title="Benchmark Complete", icon="RENDER_RESULT"
            )

            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()

            return None
        else:
            Globals.current_bench_instances += 1
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            launch_benchmark_iteration(bpy.context)

    if Globals.is_rendering_active and not Globals.active_render_processes:

        Globals.elapsed_time = time.time() - Globals.start_time
        scene = bpy.context.scene
        frames = scene.frame_end - scene.frame_start + 1
        Globals.seconds_per_frame = Globals.elapsed_time / frames
        Globals.is_rendering_active = False

        def draw_popup(self, context):
            self.layout.label(
                text=f"All instances finished in {Globals.elapsed_time:.1f} s at {Globals.seconds_per_frame:.1f} seconds per frame",
                icon="CHECKMARK",
            )

        bpy.context.window_manager.popup_menu(
            draw_popup, title="Pseudo Rendering Farm Complete", icon="RENDER_ANIMATION"
        )
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    return 1.0


"""
Rendering
"""


class RENDER_OT_pseudo_rendering_farm(bpy.types.Operator):
    """Launch multiple background render instances based on current scene settings"""

    bl_idname = "render.pseudo_rendering_farm"
    bl_label = "Launch Pseudo Rendering Farm"

    def execute(self, context):
        scene = context.scene
        rd = scene.render

        if rd.use_overwrite:
            self.report({"ERROR"}, "Validation Failed: 'Overwrite' must be UNCHECKED")
            return {"CANCELLED"}

        if not rd.use_placeholder:
            self.report({"ERROR"}, "Validation Failed: 'Placeholders' must be CHECKED")
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the scene")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()
        blender_exe = bpy.app.binary_path
        file_path = bpy.data.filepath
        num_instances = scene.pseudo_rendering_farm_instances

        Globals.active_render_processes.clear()
        Globals.start_time = time.time()
        Globals.is_rendering_active = True

        if not bpy.app.timers.is_registered(check_render_status):
            bpy.app.timers.register(check_render_status)

        for i in range(num_instances):
            try:
                Globals.active_render_processes.append(
                    subprocess.Popen([blender_exe, "-b", file_path, "-a"])
                )
            except Exception as e:
                self.report({"ERROR"}, f"Failed to launch instance {i}: {str(e)}")

        self.report({"INFO"}, f"Launched {num_instances} render instances.")
        return {"FINISHED"}


class RENDER_OT_cancel_pseudo_rendering_farm(bpy.types.Operator):
    """Stop all background render processes spawned by this plugin"""

    bl_idname = "render.cancel_pseudo_rendering_farm"
    bl_label = "Cancel All Renders"

    def execute(self, context):
        if not Globals.active_render_processes:
            self.report({"INFO"}, "No active processes found")
            return {"FINISHED"}

        count = 0
        for proc in Globals.active_render_processes:
            if proc.poll() is None:
                proc.terminate()
                count += 1
        Globals.early_exit_benchmark = True
        Globals.is_benchmarking = False

        Globals.active_render_processes.clear()

        time.sleep(0.2)
        cleared = cleanup_corrupted_frames()
        cleanup_bench_dir()

        for area in context.screen.areas:
            area.tag_redraw()

        if count != 0:
            self.report(
                {"WARNING"},
                f"Terminated {count} render processes. Removed {cleared} partial files",
            )
        return {"FINISHED"}


"""
Benchmarking
"""


def launch_benchmark_iteration(context):
    """Spawns processes for the current benchmark step."""
    Globals.start_time = time.time()
    exe = bpy.app.binary_path
    blend = bpy.data.filepath
    scene = bpy.context.scene
    Globals.benchmark_frames = (
        min(scene.frame_end, 50) // Globals.current_bench_instances
    )
    Globals.benchmark_frames *= (
        Globals.current_bench_instances
    )  # Ensure that the amount of frames is divisible

    Globals.bench_status_msg = f"Testing {Globals.current_bench_instances} instances on {Globals.benchmark_frames} frames"

    out_path = os.path.join(
        Globals.bench_temp_dir, f"inst_{Globals.current_bench_instances}", "frame_"
    )
    print(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for _ in range(Globals.current_bench_instances):
        cmd = [
            exe,
            "-b",
            blend,
            "-o",
            out_path,
            "-s",
            "1",
            "-e",
            str(Globals.benchmark_frames),
            "-a",
        ]
        Globals.active_render_processes.append(subprocess.Popen(cmd))

    if not bpy.app.timers.is_registered(check_render_status):
        bpy.app.timers.register(check_render_status)


class RENDER_OT_benchmarking(bpy.types.Operator):
    """Launch pseudo rendering farm benchmarking"""

    bl_idname = "render.benchmarking"
    bl_label = "Launch benchmark"

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save file before benchmarking.")
            return {"CANCELLED"}

        Globals.is_benchmarking = True
        Globals.current_bench_instances = 1
        Globals.benchmark_results = {}
        Globals.bench_temp_dir = tempfile.mkdtemp(prefix="blender_bench_")

        launch_benchmark_iteration(context)
        return {"FINISHED"}


"""
UI
"""


class RENDER_PT_pseudo_rendering_farm_panel(bpy.types.Panel):
    bl_label = "Pseudo Rendering Farm"
    bl_idname = "RENDER_PT_pseudo_rendering_farm"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        for proc in Globals.active_render_processes[:]:
            if proc.poll() is not None:
                Globals.active_render_processes.remove(proc)

        is_running = len(Globals.active_render_processes) > 0 or Globals.is_benchmarking
        col = layout.column(align=True)

        sub_col = col.column()
        sub_col.enabled = not is_running
        sub_col.prop(scene, "pseudo_rendering_farm_instances", text="Instances")
        row = col.row(align=True)

        launch_row = row.row(align=True)
        launch_row.enabled = not is_running
        launch_row.operator("render.pseudo_rendering_farm", icon="RENDER_ANIMATION")
        benchmark_row = row.row(align=True)
        benchmark_row.enabled = not is_running
        benchmark_row.operator("render.benchmarking", icon="SETTINGS")

        row = col.row(align=True)
        cancel_row = row.row(align=True)
        cancel_row.enabled = is_running
        cancel_row.operator(
            "render.cancel_pseudo_rendering_farm", icon="X", text="Stop"
        )

        if Globals.is_benchmarking:
            layout.label(text=Globals.bench_status_msg, icon="PLAY")
        elif Globals.is_rendering_active:
            layout.label(
                text=f"Rendering: {len(Globals.active_render_processes)} active",
                icon="URL",
            )
        else:
            if Globals.elapsed_time != 0:
                layout.label(
                    text=f"Ready. Spent {Globals.elapsed_time:.1f} seconds with {Globals.seconds_per_frame:.1f} seconds per frame",
                    icon="CHECKMARK",
                )
            else:
                layout.label(text=f"Ready", icon="CHECKMARK")


def register():
    bpy.utils.register_class(RENDER_OT_pseudo_rendering_farm)
    bpy.utils.register_class(RENDER_OT_cancel_pseudo_rendering_farm)
    bpy.utils.register_class(RENDER_OT_benchmarking)
    bpy.utils.register_class(RENDER_PT_pseudo_rendering_farm_panel)
    bpy.types.Scene.is_notifying = bpy.props.BoolProperty(default=False)
    bpy.types.Scene.pseudo_rendering_farm_instances = bpy.props.IntProperty(
        name="Instances", default=2, min=1, max=32
    )


def unregister():
    bpy.utils.unregister_class(RENDER_OT_pseudo_rendering_farm)
    bpy.utils.unregister_class(RENDER_OT_cancel_pseudo_rendering_farm)
    bpy.utils.unregister_class(RENDER_OT_benchmarking)
    bpy.utils.unregister_class(RENDER_PT_pseudo_rendering_farm_panel)
    del bpy.types.Scene.pseudo_rendering_farm_instances
    del bpy.types.Scene.is_notifying


if __name__ == "__main__":
    register()

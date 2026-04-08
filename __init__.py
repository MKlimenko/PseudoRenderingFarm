import bpy
import os
from pathlib import Path
import platform
import re
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
    declining_streak = 0
    peak_throughput = 0

    gpu_detection_active = False
    userpref_process = None
    userpref_path = ""

    dummy_scene_process = None

    gpu_discovery_processes = []

    gpu_devices = []
    gpu_devices_envs = []
    gpu_detected = False
    gpu_configured = False
    gpu_config_dir = ""


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
        print(f"PseudoRenderingFarm: Error checking {filepath}: {e}")
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
                    print(f"PseudoRenderingFarm: Failed to delete {filename}: {e}")

    return deleted_count


def cleanup_bench_dir():
    if Globals.bench_temp_dir and os.path.exists(Globals.bench_temp_dir):
        try:
            shutil.rmtree(Globals.bench_temp_dir)
        except:
            pass
        Globals.bench_temp_dir = ""


def terminate_all_processes():
    for proc in Globals.active_render_processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in Globals.active_render_processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    Globals.active_render_processes.clear()


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

        if throughput > Globals.peak_throughput:
            Globals.peak_throughput = throughput
            Globals.declining_streak = 0
        else:
            Globals.declining_streak += 1

        if (
            Globals.early_exit_benchmark
            or Globals.current_bench_instances >= 16
            or Globals.declining_streak >= 2 * max(len(Globals.gpu_devices), 1)
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

            print(f"PseudoRenderingFarm: !!! Benchmarking stats for nerds !!!")
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


def check_multi_gpu_status():
    setup_multi_gpu()
    if Globals.gpu_configured:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    return 1.0


def detect_gpus():
    if Globals.gpu_detected:
        return
    Globals.gpu_detected = True
    Globals.gpu_devices_envs = [os.environ.copy()]

    try:
        if "VULKAN" not in bpy.context.preferences.system.gpu_backend:
            print("PseudoRenderingFarm: Non-Vulkan backend, multi-GPU not available")
            return
        try:
            bpy.context.preferences.system.gpu_preferred_device = "___invalid___"
        except TypeError as e:
            Globals.gpu_devices = [
                d for d in re.findall(r"'([^']+)'", str(e)) if d != "AUTO"
            ]
    except Exception as e:
        print(f"PseudoRenderingFarm: GPU detection failed: {e}")


def setup_multi_gpu():
    if not Globals.userpref_path:
        if Globals.userpref_process is None:
            expr = "import bpy, os; print('USERPREF:' + os.path.join(bpy.utils.resource_path('USER'), 'config', 'userpref.blend'))"

            Globals.userpref_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return
        status = Globals.userpref_process.poll()
        if status is not None:
            stdout, stderr = Globals.userpref_process.communicate()

            for line in (stdout + stderr).splitlines():
                if line.startswith("USERPREF:"):
                    Globals.userpref_path = line[len("USERPREF:") :]
                    return

    if Globals.gpu_config_dir:
        Globals.gpu_config_dir = tempfile.mkdtemp(prefix="gpu_config_")
    scene_path = os.path.join(Globals.gpu_config_dir, "temp_scene.blend")

    if not os.path.isfile(scene_path):
        if Globals.dummy_scene_process is None:
            expr = f"import bpy; bpy.ops.wm.read_homefile(); bpy.ops.wm.save_as_mainfile(filepath=r'{scene_path}')"

            Globals.dummy_scene_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return
        status = Globals.dummy_scene_process.poll()
        if status is None:
            return

    if len(Globals.gpu_discovery_processes) == 0:
        Globals.gpu_devices_envs = []
        for i, gpu_name in enumerate(Globals.gpu_devices):
            gpu_dir = os.path.join(Globals.gpu_config_dir, f"gpu_{i}")
            os.makedirs(gpu_dir, exist_ok=True)
            shutil.copy2(Globals.userpref_path, os.path.join(gpu_dir, "userpref.blend"))

            env = os.environ.copy()
            env["BLENDER_USER_CONFIG"] = gpu_dir

            cmd = [
                bpy.app.binary_path,
                scene_path,
                "--python-expr",
                f"import bpy; bpy.context.preferences.system.gpu_preferred_device = '{gpu_name}'; bpy.ops.wm.save_userpref(); bpy.ops.wm.quit_blender()",
            ]
            Globals.gpu_discovery_processes.append(
                subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

            Globals.gpu_devices_envs.append(env)

        if not Globals.gpu_devices_envs:
            Globals.gpu_devices_envs = [os.environ.copy()]
            return

    if all(p.poll() is not None for p in Globals.gpu_discovery_processes):
        Globals.gpu_configured = True
    return


def cleanup_gpu_config():
    if Globals.gpu_config_dir and os.path.exists(Globals.gpu_config_dir):
        try:
            shutil.rmtree(Globals.gpu_config_dir)
        except Exception:
            pass
        Globals.gpu_config_dir = ""


def get_env_for_instance(index):
    if not Globals.gpu_devices_envs:
        Globals.gpu_devices_envs = [os.environ.copy()]
    return Globals.gpu_devices_envs[index % len(Globals.gpu_devices_envs)]


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
                    subprocess.Popen(
                        [
                            blender_exe,
                            "-b",
                            file_path,
                            "-a",
                        ],
                        env=get_env_for_instance(i),
                    )
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

        count = len([p for p in Globals.active_render_processes if p.poll() is None])
        if Globals.is_benchmarking:
            Globals.early_exit_benchmark = True
        Globals.is_benchmarking = False

        terminate_all_processes()
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
    frame_start = scene.frame_start
    frame_end = scene.frame_end
    available = frame_end - frame_start + 1
    Globals.benchmark_frames = min(48, available)

    Globals.bench_status_msg = f"Testing {Globals.current_bench_instances} instances on {Globals.benchmark_frames} frames"

    out_path = os.path.join(
        Globals.bench_temp_dir, f"inst_{Globals.current_bench_instances}", "frame_"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for i in range(Globals.current_bench_instances):
        cmd = [
            exe,
            "-b",
            blend,
            "-o",
            out_path,
            "-s",
            str(frame_start),
            "-e",
            str(frame_start + Globals.benchmark_frames - 1),
            "-a",
        ]

        Globals.active_render_processes.append(
            subprocess.Popen(cmd, env=get_env_for_instance(i))
        )

    if not bpy.app.timers.is_registered(check_render_status):
        bpy.app.timers.register(check_render_status)


class RENDER_OT_benchmarking(bpy.types.Operator):
    """Launch pseudo rendering farm benchmarking"""

    bl_idname = "render.benchmarking"
    bl_label = "Launch benchmark"

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
            self.report({"ERROR"}, "Save file before benchmarking.")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()

        Globals.is_benchmarking = True
        Globals.current_bench_instances = 1
        Globals.benchmark_results = {}
        Globals.declining_streak = 0
        Globals.peak_throughput = 0
        Globals.bench_temp_dir = tempfile.mkdtemp(prefix="blender_bench_")

        launch_benchmark_iteration(context)
        return {"FINISHED"}


"""
Multi-GPU setup
"""


class RENDER_OT_setup_multi_gpu(bpy.types.Operator):
    """Detect and setup multiple GPUs for parallel rendering"""

    bl_idname = "render.setup_multi_gpu"
    bl_label = "Setup multi-GPU"

    def execute(self, context):
        Globals.gpu_detection_active = True

        if not bpy.app.timers.is_registered(check_multi_gpu_status):
            bpy.app.timers.register(check_multi_gpu_status)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


"""
UI
"""


class RENDER_OT_open_folder(bpy.types.Operator):
    """Open the folder with rendered data"""

    bl_idname = "render.open_folder"
    bl_label = "Open folder"

    def sanitize(self, context):
        frame_path = Path(
            context.scene.render.frame_path(frame=context.scene.frame_current)
        )
        folder_path = frame_path.parent.absolute()
        return folder_path

    def execute(self, context):
        render_path = self.sanitize(context)

        if not os.path.exists(render_path):
            return

        current_os = platform.system()
        if current_os == "Windows":
            os.startfile(render_path)
        elif current_os == "Darwin":
            subprocess.run(["open", render_path])
        else:
            subprocess.run(["xdg-open", render_path])

        return {"FINISHED"}


class RENDER_PT_pseudo_rendering_farm_panel(bpy.types.Panel):
    bl_label = "Pseudo Rendering Farm"
    bl_idname = "RENDER_PT_pseudo_rendering_farm"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        is_running = (
            any(p.poll() is None for p in Globals.active_render_processes)
            or Globals.is_benchmarking
        )
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
        open_row = row.row(align=True)
        open_row.operator(
            "render.open_folder", icon="FILE_FOLDER", text="Open render folder"
        )

        if len(Globals.gpu_devices) > 1:
            if not Globals.gpu_configured:
                gpu_row = col.row(align=True)
                gpu_row.enabled = not is_running and not Globals.gpu_detection_active
                gpu_row.operator("render.setup_multi_gpu", icon="LIGHT")

        row = col.row(align=True)
        cancel_row = row.row(align=True)
        cancel_row.enabled = is_running
        cancel_row.operator(
            "render.cancel_pseudo_rendering_farm", icon="X", text="Stop"
        )

        if len(Globals.gpu_devices) > 1:
            if Globals.gpu_configured:
                layout.label(
                    text=f"Multi-GPU: {len(Globals.gpu_devices)} devices",
                    icon="PREFERENCES",
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


classes = [
    RENDER_OT_pseudo_rendering_farm,
    RENDER_OT_cancel_pseudo_rendering_farm,
    RENDER_OT_benchmarking,
    RENDER_OT_setup_multi_gpu,
    RENDER_OT_open_folder,
    RENDER_PT_pseudo_rendering_farm_panel,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pseudo_rendering_farm_instances = bpy.props.IntProperty(
        name="Instances", default=2, min=1, max=32
    )
    detect_gpus()


def unregister():
    cleanup_gpu_config()
    for c in classes:
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.pseudo_rendering_farm_instances


if __name__ == "__main__":
    register()

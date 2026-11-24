import bpy
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time


class Instance:
    """A spawned Blender process together with its log file."""

    def __init__(self, process, log_path):
        self.process = process
        self.log_path = log_path
        self.log_offset = 0
        self.frames_done = 0


class Globals:
    active_render_processes = []
    instances = []
    log_dir = ""
    output_dir = ""
    expected_frames = {}
    frame_step = 1
    total_frames = 0
    frames_done = 0
    error_msg = ""
    is_rendering_active = False
    is_benchmarking = False
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
    gpu_setup_error = ""


VALIDATED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".exr"}
SAVED_MARKER = b"saved: '"
SKIPPED_MARKER = b"skipping existing frame"
MAX_INSTANCES = 32
BENCHMARK_MAX_FRAMES = 48


def redraw_all_windows():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def is_image_valid(filepath):
    """Checks if an image file is complete by looking for format-specific footers."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 10:
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
    output_dir = Globals.output_dir

    if not os.path.isdir(output_dir):
        return 0

    deleted_count = 0
    for filename in os.listdir(output_dir):
        file_path = os.path.join(output_dir, filename)
        if not os.path.isfile(file_path):
            continue
        if os.path.splitext(filename)[1].lower() not in VALIDATED_EXTENSIONS:
            continue
        try:
            if os.path.getmtime(file_path) < Globals.start_time:
                continue
        except OSError:
            continue
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
        except OSError as e:
            print(f"PseudoRenderingFarm: Failed to remove benchmark dir: {e}")
        Globals.bench_temp_dir = ""


def cleanup_log_dir():
    if Globals.log_dir and os.path.exists(Globals.log_dir):
        try:
            shutil.rmtree(Globals.log_dir)
        except OSError as e:
            print(f"PseudoRenderingFarm: Failed to remove log dir: {e}")
    Globals.log_dir = ""


def reset_log_dir():
    cleanup_log_dir()
    Globals.log_dir = tempfile.mkdtemp(prefix="prf_logs_")


def start_run(total_frames):
    Globals.active_render_processes.clear()
    Globals.instances.clear()
    Globals.total_frames = total_frames
    Globals.frames_done = 0
    Globals.start_time = time.time()


def spawn_instance(cmd, index, log_name):
    log_path = os.path.join(Globals.log_dir, log_name)
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            cmd,
            env=get_env_for_instance(index),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    Globals.active_render_processes.append(process)
    Globals.instances.append(Instance(process, log_path))


def update_progress():
    """Counts the frames the instances reported in their logs.
    Returns True if the total has changed."""
    markers = [SAVED_MARKER]
    if is_system_balanced():
        markers.append(SKIPPED_MARKER)

    for instance in Globals.instances:
        try:
            with open(instance.log_path, "rb") as f:
                f.seek(instance.log_offset)
                data = f.read()
        except OSError:
            continue
        complete = data[: data.rfind(b"\n") + 1].lower()
        instance.log_offset += len(complete)
        instance.frames_done += sum(complete.count(m) for m in markers)

    done = sum(instance.frames_done for instance in Globals.instances)
    done = min(done, Globals.total_frames)
    changed = done != Globals.frames_done
    Globals.frames_done = done
    return changed


def count_failed_instances():
    return sum(1 for i in Globals.instances if i.process.poll() not in (None, 0))


def find_missing_frames():
    missing = []
    for frame, path in Globals.expected_frames.items():
        try:
            if os.path.getsize(path) == 0:
                missing.append(frame)
        except OSError:
            missing.append(frame)
    return missing


def format_frame_ranges(frames, step=1, max_ranges=4):
    """Formats sorted frame numbers compactly, e.g. '3-7, 12, 20-24'."""
    ranges = []
    for frame in frames:
        if ranges and frame == ranges[-1][1] + step:
            ranges[-1][1] = frame
        else:
            ranges.append([frame, frame])

    parts = [str(a) if a == b else f"{a}-{b}" for a, b in ranges[:max_ranges]]
    if len(ranges) > max_ranges:
        parts.append("...")
    return ", ".join(parts)


def get_failure_summary():
    """Describes what went wrong with the finished render, empty if nothing."""
    problems = []
    failed = count_failed_instances()
    if failed:
        problems.append(f"{failed} of {len(Globals.instances)} instances failed")

    missing = find_missing_frames()
    if missing:
        ranges = format_frame_ranges(missing, Globals.frame_step)
        problems.append(f"{len(missing)} frames missing ({ranges})")
    return ", ".join(problems)


def show_popup(title, text, icon, title_icon):
    if bpy.app.background:
        return

    def draw_popup(self, context):
        self.layout.label(text=text, icon=icon)

    bpy.context.window_manager.popup_menu(draw_popup, title=title, icon=title_icon)
    redraw_all_windows()


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


def finish_benchmark(failed):
    Globals.is_benchmarking = False
    Globals.bench_status_msg = ""
    cleanup_bench_dir()

    print("PseudoRenderingFarm: !!! Benchmarking stats for nerds !!!")
    print(Globals.benchmark_results)

    best_count = max(Globals.benchmark_results, key=Globals.benchmark_results.get)
    best_throughput = Globals.benchmark_results[best_count]
    if best_throughput <= 0:
        Globals.error_msg = "Benchmark failed: the instances could not render"
        show_popup("Benchmark Failed", Globals.error_msg, "ERROR", "ERROR")
        return

    bpy.context.scene.pseudo_rendering_farm_instances = best_count
    Globals.bench_status_msg = f"Optimal found: {best_count}"
    if failed:
        Globals.error_msg = (
            f"Benchmark stopped: rendering failed with "
            f"{Globals.current_bench_instances} instances"
        )

    show_popup(
        "Benchmark Complete",
        f"Benchmark is complete, optimal number of instances is {best_count} with {1.0 / best_throughput:.1f} seconds per frame",
        "CHECKMARK",
        "RENDER_RESULT",
    )


def check_render_status():
    for proc in Globals.active_render_processes[:]:
        if proc.poll() is not None:
            Globals.active_render_processes.remove(proc)

    if update_progress():
        redraw_all_windows()

    if Globals.is_benchmarking and not Globals.active_render_processes:
        failed = count_failed_instances() > 0
        elapsed = time.time() - Globals.start_time
        throughput = 0 if failed else Globals.benchmark_frames / max(elapsed, 0.001)

        Globals.benchmark_results[Globals.current_bench_instances] = throughput

        if throughput > Globals.peak_throughput:
            Globals.peak_throughput = throughput
            Globals.declining_streak = 0
        else:
            Globals.declining_streak += 1

        if (
            failed
            or Globals.current_bench_instances >= MAX_INSTANCES
            or Globals.declining_streak >= max(len(Globals.gpu_devices), 1)
        ):
            finish_benchmark(failed)
            redraw_all_windows()
            return None
        else:
            Globals.current_bench_instances += 1
            redraw_all_windows()
            launch_benchmark_iteration()

    if Globals.is_rendering_active and not Globals.active_render_processes:
        Globals.elapsed_time = time.time() - Globals.start_time
        Globals.seconds_per_frame = Globals.elapsed_time / max(Globals.total_frames, 1)
        Globals.is_rendering_active = False
        if count_failed_instances():
            cleanup_corrupted_frames()
        Globals.error_msg = get_failure_summary()

        if Globals.error_msg:
            show_popup(
                "Pseudo Rendering Farm Failed", Globals.error_msg, "ERROR", "ERROR"
            )
        else:
            show_popup(
                "Pseudo Rendering Farm Complete",
                f"All instances finished in {Globals.elapsed_time:.1f} s at {Globals.seconds_per_frame:.1f} seconds per frame",
                "CHECKMARK",
                "RENDER_ANIMATION",
            )
        redraw_all_windows()

        return None

    if not Globals.is_benchmarking and not Globals.active_render_processes:
        return None

    return 1.0


@bpy.app.handlers.persistent
def on_file_loaded(_):
    if Globals.is_benchmarking:
        Globals.is_benchmarking = False
        Globals.bench_status_msg = ""
        terminate_all_processes()
        cleanup_bench_dir()


def check_multi_gpu_status():
    setup_multi_gpu()
    if Globals.gpu_configured or Globals.gpu_setup_error:
        Globals.gpu_detection_active = False
        redraw_all_windows()
        return None

    return 1.0


def detect_gpus():
    if Globals.gpu_detected:
        return
    try:
        system_prefs = bpy.context.preferences.system
    except AttributeError:
        # bpy.context can be restricted while add-ons are registered during
        # startup; retry_detect_gpus will call again once it is available
        return
    Globals.gpu_detected = True
    Globals.gpu_devices_envs = [os.environ.copy()]

    try:
        if platform.system() != "Darwin" and "VULKAN" not in system_prefs.gpu_backend:
            print("PseudoRenderingFarm: Non-Vulkan backend, multi-GPU not available")
            return
        try:
            system_prefs.gpu_preferred_device = "___invalid___"
        except TypeError as e:
            Globals.gpu_devices = [
                d for d in re.findall(r"'([^']+)'", str(e)) if d != "AUTO"
            ]
    except Exception as e:
        print(f"PseudoRenderingFarm: GPU detection failed: {e}")


def retry_detect_gpus():
    detect_gpus()
    return None if Globals.gpu_detected else 0.5


def fail_gpu_setup(message):
    print(f"PseudoRenderingFarm: multi-GPU setup failed: {message}")
    Globals.gpu_setup_error = message
    Globals.userpref_process = None
    Globals.dummy_scene_process = None
    Globals.gpu_discovery_processes = []
    Globals.gpu_devices_envs = [os.environ.copy()]
    cleanup_gpu_config()


def setup_multi_gpu():
    if Globals.gpu_setup_error:
        return

    if not Globals.userpref_path:
        if Globals.userpref_process is None:
            expr = "import bpy, os; print('USERPREF:' + os.path.join(bpy.utils.resource_path('USER'), 'config', 'userpref.blend'))"

            Globals.userpref_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return
        if Globals.userpref_process.poll() is None:
            return
        stdout, _ = Globals.userpref_process.communicate()
        Globals.userpref_process = None

        for line in stdout.splitlines():
            if line.startswith("USERPREF:"):
                Globals.userpref_path = line[len("USERPREF:") :].strip()
                break

        if not Globals.userpref_path:
            fail_gpu_setup("Could not query the user preferences location")
        elif not os.path.isfile(Globals.userpref_path):
            missing_path = Globals.userpref_path
            Globals.userpref_path = ""
            fail_gpu_setup(f"No saved user preferences found at {missing_path}")
        return

    if not Globals.gpu_config_dir:
        Globals.gpu_config_dir = tempfile.mkdtemp(prefix="gpu_config_")
    scene_path = os.path.join(Globals.gpu_config_dir, "temp_scene.blend")

    if not os.path.isfile(scene_path):
        if Globals.dummy_scene_process is None:
            expr = f"import bpy; bpy.ops.wm.read_homefile(); bpy.ops.wm.save_as_mainfile(filepath={scene_path!r})"

            Globals.dummy_scene_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if Globals.dummy_scene_process.poll() is None:
            return
        Globals.dummy_scene_process = None
        if not os.path.isfile(scene_path):
            fail_gpu_setup("Failed to create the temporary configuration scene")
            return

    if not Globals.gpu_discovery_processes:
        processes = []
        envs = []
        for i, gpu_name in enumerate(Globals.gpu_devices):
            gpu_dir = os.path.join(Globals.gpu_config_dir, f"gpu_{i}")
            os.makedirs(gpu_dir, exist_ok=True)
            try:
                shutil.copy2(
                    Globals.userpref_path, os.path.join(gpu_dir, "userpref.blend")
                )
            except OSError as e:
                fail_gpu_setup(f"Failed to copy user preferences: {e}")
                return

            env = os.environ.copy()
            env["BLENDER_USER_CONFIG"] = gpu_dir

            expr = (
                f"import bpy; bpy.context.preferences.system.gpu_preferred_device = {gpu_name!r}; "
                "bpy.ops.wm.save_userpref(); bpy.ops.wm.quit_blender()"
            )
            cmd = [bpy.app.binary_path, scene_path, "--python-expr", expr]
            processes.append(
                subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            envs.append(env)

        Globals.gpu_discovery_processes = processes
        Globals.gpu_devices_envs = envs
        return

    if all(p.poll() is not None for p in Globals.gpu_discovery_processes):
        failed = [p for p in Globals.gpu_discovery_processes if p.returncode != 0]
        Globals.gpu_discovery_processes = []
        if failed:
            fail_gpu_setup(f"{len(failed)} GPU configuration process(es) failed")
            return
        Globals.gpu_configured = True


def cleanup_gpu_config():
    if Globals.gpu_config_dir and os.path.exists(Globals.gpu_config_dir):
        try:
            shutil.rmtree(Globals.gpu_config_dir)
        except OSError as e:
            print(f"PseudoRenderingFarm: Failed to remove GPU config dir: {e}")
        Globals.gpu_config_dir = ""


def get_env_for_instance(index):
    if not Globals.gpu_devices_envs:
        Globals.gpu_devices_envs = [os.environ.copy()]
    return Globals.gpu_devices_envs[index % len(Globals.gpu_devices_envs)]


def get_worker_subrange(start_idx, end_idx, num_workers, worker_id, step=1):
    if end_idx < start_idx or worker_id >= num_workers:
        return None
    total_frames = (end_idx - start_idx) // step + 1

    chunk_size = math.ceil(total_frames / num_workers)
    first_frame = worker_id * chunk_size
    if first_frame >= total_frames:
        return None
    last_frame = min(first_frame + chunk_size - 1, total_frames - 1)
    return (start_idx + first_frame * step, start_idx + last_frame * step)


def using_same_gpus():
    # "10de/2783/0" -> vendor_id/device_id/device_num
    if not Globals.gpu_devices:
        return True
    prefixes = {s.rsplit("/", 1)[0] for s in Globals.gpu_devices}

    return len(prefixes) <= 1


def is_system_balanced():
    return len(Globals.gpu_devices) <= 1 or using_same_gpus()


def is_scene_configured(extension_obj, scene_render):
    if scene_render.is_movie_format:
        extension_obj.report(
            {"ERROR"},
            "Video output is not supported, the instances cannot write to the same file. Render an image sequence (e.g. PNG) instead",
        )
        return False

    if is_system_balanced():
        return True

    if scene_render.use_overwrite:
        extension_obj.report(
            {"ERROR"},
            "Validation Failed: 'Overwrite' must be UNCHECKED for multi-GPU systems with different GPUs",
        )
        return False
    if not scene_render.use_placeholder:
        extension_obj.report(
            {"ERROR"},
            "Validation Failed: 'Placeholders' must be CHECKED for multi-GPU systems with different GPUs",
        )
        return False
    return True


# --- Rendering ---


class RENDER_OT_pseudo_rendering_farm(bpy.types.Operator):
    """Launch multiple background render instances based on current scene settings"""

    bl_idname = "render.pseudo_rendering_farm"
    bl_label = "Launch Pseudo Rendering Farm"

    def execute(self, context):
        scene = context.scene

        if not is_scene_configured(self, scene.render):
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the scene")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()
        blender_exe = bpy.app.binary_path
        blend_path = bpy.data.filepath
        num_instances = scene.pseudo_rendering_farm_instances

        frames = range(scene.frame_start, scene.frame_end + 1, scene.frame_step)
        render = scene.render
        Globals.output_dir = os.path.dirname(render.frame_path(frame=frames[0]))
        Globals.frame_step = scene.frame_step
        Globals.expected_frames = {}
        if not render.use_multiview:
            Globals.expected_frames = {f: render.frame_path(frame=f) for f in frames}

        Globals.error_msg = ""
        reset_log_dir()
        start_run(len(frames))
        Globals.is_rendering_active = True

        if not bpy.app.timers.is_registered(check_render_status):
            bpy.app.timers.register(check_render_status, persistent=True)

        launched = 0
        for i in range(num_instances):
            cmd = [blender_exe, "-b", blend_path, "-a"]
            try:
                if is_system_balanced():
                    subrange = get_worker_subrange(
                        scene.frame_start,
                        scene.frame_end,
                        num_instances,
                        i,
                        scene.frame_step,
                    )
                    if not subrange:
                        continue
                    subrange_start, subrange_end = subrange
                    cmd = (
                        cmd[:-1]
                        + ["-s", str(subrange_start), "-e", str(subrange_end)]
                        + cmd[-1:]
                    )
                spawn_instance(cmd, i, f"instance_{i}.log")
                launched += 1
            except Exception as e:
                self.report({"ERROR"}, f"Failed to launch instance {i}: {e}")

        if launched == 0:
            Globals.is_rendering_active = False
            self.report({"ERROR"}, "No render instances could be launched")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Launched {launched} render instances.")
        return {"FINISHED"}


class RENDER_OT_cancel_pseudo_rendering_farm(bpy.types.Operator):
    """Stop all background render processes spawned by this plugin"""

    bl_idname = "render.cancel_pseudo_rendering_farm"
    bl_label = "Cancel All Renders"

    def execute(self, context):
        if not Globals.active_render_processes and not Globals.is_benchmarking:
            self.report({"INFO"}, "No active processes found")
            return {"FINISHED"}

        count = len([p for p in Globals.active_render_processes if p.poll() is None])
        Globals.is_benchmarking = False
        Globals.is_rendering_active = False
        Globals.bench_status_msg = ""

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


# --- Benchmarking ---


def launch_benchmark_iteration():
    """Spawns processes for the current benchmark step."""
    blender_exe = bpy.app.binary_path
    blend_path = bpy.data.filepath
    scene = bpy.context.scene
    frame_start = scene.frame_start
    frame_end = scene.frame_end
    step = scene.frame_step
    available = (frame_end - frame_start) // step + 1
    Globals.benchmark_frames = min(BENCHMARK_MAX_FRAMES, available)
    start_run(Globals.benchmark_frames)
    bench_end = frame_start + (Globals.benchmark_frames - 1) * step

    Globals.bench_status_msg = f"Testing {Globals.current_bench_instances} instances on {Globals.benchmark_frames} frames"

    out_path = os.path.join(
        Globals.bench_temp_dir, f"inst_{Globals.current_bench_instances}", "frame_"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for i in range(Globals.current_bench_instances):
        if is_system_balanced():
            subrange = get_worker_subrange(
                frame_start,
                bench_end,
                Globals.current_bench_instances,
                i,
                step,
            )
            if not subrange:
                continue
        else:
            subrange = (frame_start, bench_end)
        subrange_start, subrange_end = subrange
        cmd = [
            blender_exe,
            "-b",
            blend_path,
            "-o",
            out_path,
            "-s",
            str(subrange_start),
            "-e",
            str(subrange_end),
            "-a",
        ]

        log_name = f"bench_{Globals.current_bench_instances}_instance_{i}.log"
        spawn_instance(cmd, i, log_name)

    if not bpy.app.timers.is_registered(check_render_status):
        bpy.app.timers.register(check_render_status, persistent=True)


class RENDER_OT_benchmarking(bpy.types.Operator):
    """Save the file and benchmark how many parallel instances render fastest"""

    bl_idname = "render.benchmarking"
    bl_label = "Launch benchmark"

    def execute(self, context):
        if not is_scene_configured(self, context.scene.render):
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
        Globals.output_dir = ""
        Globals.error_msg = ""
        reset_log_dir()

        launch_benchmark_iteration()
        return {"FINISHED"}


# --- Multi-GPU setup ---


class RENDER_OT_setup_multi_gpu(bpy.types.Operator):
    """Detect and setup multiple GPUs for parallel rendering"""

    bl_idname = "render.setup_multi_gpu"
    bl_label = "Setup multi-GPU"

    def execute(self, context):
        Globals.gpu_detection_active = True
        Globals.gpu_setup_error = ""

        if not bpy.app.timers.is_registered(check_multi_gpu_status):
            bpy.app.timers.register(check_multi_gpu_status)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# --- UI ---


def open_in_file_browser(path):
    if not path or not os.path.exists(path):
        return False

    current_os = platform.system()
    if current_os == "Windows":
        os.startfile(path)
    elif current_os == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)
    return True


class RENDER_OT_open_folder(bpy.types.Operator):
    """Open the folder with rendered data"""

    bl_idname = "render.open_folder"
    bl_label = "Open folder"

    def get_render_folder(self, context):
        frame_path = Path(
            context.scene.render.frame_path(frame=context.scene.frame_current)
        )
        folder_path = frame_path.parent.absolute()
        return folder_path

    def execute(self, context):
        if not open_in_file_browser(self.get_render_folder(context)):
            return {"CANCELLED"}
        return {"FINISHED"}


class RENDER_OT_open_farm_logs(bpy.types.Operator):
    """Open the folder with the logs of the render instances"""

    bl_idname = "render.open_farm_logs"
    bl_label = "Open logs"

    def execute(self, context):
        if not open_in_file_browser(Globals.log_dir):
            return {"CANCELLED"}
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

        if len(Globals.gpu_devices) > 1 and not Globals.gpu_configured:
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
            elif Globals.gpu_setup_error:
                layout.label(text=Globals.gpu_setup_error, icon="ERROR")

        progress = Globals.frames_done / max(Globals.total_frames, 1)
        if Globals.is_benchmarking:
            layout.progress(factor=progress, text=Globals.bench_status_msg)
        elif Globals.is_rendering_active:
            text = f"{Globals.frames_done} / {Globals.total_frames} frames, {len(Globals.active_render_processes)} active"
            failed = count_failed_instances()
            if failed:
                text += f", {failed} failed"
            layout.progress(factor=progress, text=text)
        elif Globals.error_msg:
            layout.label(text=Globals.error_msg, icon="ERROR")
            layout.operator("render.open_farm_logs", icon="TEXT")
        else:
            if Globals.elapsed_time != 0:
                layout.label(
                    text=f"Ready. Spent {Globals.elapsed_time:.1f} seconds with {Globals.seconds_per_frame:.1f} seconds per frame",
                    icon="CHECKMARK",
                )
            else:
                layout.label(text="Ready", icon="CHECKMARK")


classes = [
    RENDER_OT_pseudo_rendering_farm,
    RENDER_OT_cancel_pseudo_rendering_farm,
    RENDER_OT_benchmarking,
    RENDER_OT_setup_multi_gpu,
    RENDER_OT_open_folder,
    RENDER_OT_open_farm_logs,
    RENDER_PT_pseudo_rendering_farm_panel,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pseudo_rendering_farm_instances = bpy.props.IntProperty(
        name="Instances", default=2, min=1, max=MAX_INSTANCES
    )
    bpy.app.handlers.load_post.append(on_file_loaded)
    detect_gpus()
    if not Globals.gpu_detected and not bpy.app.background:
        bpy.app.timers.register(retry_detect_gpus, first_interval=0.5)


def unregister():
    Globals.is_rendering_active = False
    Globals.is_benchmarking = False
    terminate_all_processes()
    for timer in (check_render_status, check_multi_gpu_status, retry_detect_gpus):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    if on_file_loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_loaded)
    cleanup_bench_dir()
    cleanup_log_dir()
    cleanup_gpu_config()
    for c in classes:
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.pseudo_rendering_farm_instances


if __name__ == "__main__":
    register()

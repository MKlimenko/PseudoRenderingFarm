import glob
import os
import shutil
import subprocess
import time

import psutil
import pytest

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
EXTENSION_ZIP_PATTERN = "../pseudo_rendering_farm*.zip"
TEST_BLEND_FILE = "test_scene.blend"
FRAMES_TO_RENDER = 12


def count_spawned_renderers(pid):
    """Counts Blender processes spawned by the given process."""
    try:
        parent = psutil.Process(pid)
        return len(
            [
                child
                for child in parent.children(recursive=True)
                if "blender" in child.name().lower()
            ]
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


@pytest.fixture(scope="session", autouse=True)
def create_scene():
    cmd = [
        BLENDER_BIN,
        "-b",
        "--python-expr",
        (
            "import bpy; bpy.ops.wm.read_homefile(); "
            "render_settings = bpy.context.scene.render; "
            "render_settings.use_overwrite = False; "
            "render_settings.use_placeholder = True; "
            f"bpy.context.scene.frame_end = {FRAMES_TO_RENDER}; "
            "render_settings.filepath = '//out/'; "
            "bpy.ops.wm.save_as_mainfile(filepath='test_scene.blend')"
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert os.path.isfile(
        TEST_BLEND_FILE
    ), f"Failed to create test scene:\n{result.stdout}\n{result.stderr}"

    yield

    for leftover in (TEST_BLEND_FILE, TEST_BLEND_FILE + "1"):
        if os.path.isfile(leftover):
            os.remove(leftover)
    shutil.rmtree("out", ignore_errors=True)


def test_install_extension():
    zip_files = glob.glob(EXTENSION_ZIP_PATTERN)
    assert (
        len(zip_files) > 0
    ), f"No extension zip found matching {EXTENSION_ZIP_PATTERN}"

    cmd = [
        BLENDER_BIN,
        "--command",
        "extension",
        "install-file",
        zip_files[0],
        "-r",
        "user_default",
        "-e",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert (
        'STATUS Installed "pseudo_rendering_farm"' in result.stdout
        or 'STATUS Reinstalled "pseudo_rendering_farm"' in result.stdout
    )
    print("\nExtension installed successfully.")


def test_pseudo_rendering_farm():
    py_expr = (
        "import bpy,time,sys; "
        "G=sys.modules['bl_ext.user_default.pseudo_rendering_farm'].Globals; "
        "bpy.ops.render.pseudo_rendering_farm(); "
        "exec('while any(p.poll() is None for p in G.active_render_processes):\\n time.sleep(1)'); "
        "bpy.ops.wm.quit_blender()"
    )

    cmd = [
        BLENDER_BIN,
        "-b",
        TEST_BLEND_FILE,
        "-E",
        "BLENDER_EEVEE_NEXT",
        "--python-expr",
        py_expr,
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    max_seen = 0
    start_check = time.time()
    while (time.time() - start_check) < 60 and process.poll() is None:
        max_seen = max(max_seen, count_spawned_renderers(process.pid))
        if max_seen >= 2:
            break
        time.sleep(0.2)

    # communicate() drains the pipes: the spawned render instances inherit
    # them, and blocked pipes would deadlock the renders
    try:
        process.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        try:
            for child in psutil.Process(process.pid).children(recursive=True):
                child.kill()
        except psutil.NoSuchProcess:
            pass
        process.kill()
        process.communicate()
        raise

    assert max_seen >= 2, "Expected at least 2 parallel render instances"

    for i in range(1, FRAMES_TO_RENDER + 1):
        frame_file = os.path.join("out/", f"{i:04d}.png")
        assert os.path.exists(frame_file), f"Missing frame: {frame_file}"

    print(f"\nRendering run complete. {FRAMES_TO_RENDER} frames verified in out/")


def test_run_benchmark():
    py_expr = (
        "import bpy,time,sys; "
        "m=sys.modules['bl_ext.user_default.pseudo_rendering_farm']; "
        "bpy.ops.render.benchmarking(); "
        "exec('while m.Globals.is_benchmarking:\\n m.check_render_status()\\n time.sleep(1)'); "
        "bpy.ops.wm.quit_blender()"
    )

    cmd = [BLENDER_BIN, "-b", TEST_BLEND_FILE, "--python-expr", py_expr]

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1800, check=False
    )
    benchmark_result_str = "!!! Benchmarking stats for nerds !!!"
    assert benchmark_result_str in result.stdout
    for line in result.stdout.splitlines():
        if line.startswith("{1: ") or benchmark_result_str in line:
            print(line)
    print("\nBenchmark stats found in output.")


def test_cancel_render():
    """Cancelling must terminate the instances, reset the state and stop
    the status timer without reporting completion."""
    py_expr = (
        "import bpy,time,sys; "
        "m=sys.modules['bl_ext.user_default.pseudo_rendering_farm']; "
        "bpy.ops.render.pseudo_rendering_farm(); "
        "time.sleep(2); "
        "bpy.ops.render.cancel_pseudo_rendering_farm(); "
        "assert not m.Globals.is_rendering_active; "
        "assert not m.Globals.is_benchmarking; "
        "assert not m.Globals.active_render_processes; "
        "assert m.check_render_status() is None; "
        "print('CANCEL_OK'); "
        "bpy.ops.wm.quit_blender()"
    )

    cmd = [
        BLENDER_BIN,
        "-b",
        TEST_BLEND_FILE,
        "-E",
        "BLENDER_EEVEE_NEXT",
        "--python-expr",
        py_expr,
    ]

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300, check=False
    )
    assert (
        "CANCEL_OK" in result.stdout
    ), f"Cancel test failed:\n{result.stdout}\n{result.stderr}"
    print("\nCancel path verified.")


if __name__ == "__main__":
    test_install_extension()
    test_pseudo_rendering_farm()
    test_run_benchmark()
    test_cancel_render()

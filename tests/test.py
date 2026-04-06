import subprocess
import pytest
import time
import os
import glob
import psutil

BLENDER_BIN = "blender"
EXTENSION_ZIP_PATTERN = "../pseudo_rendering_farm*.zip"
TEST_BLEND_FILE = "test_scene.blend"


def count_blender_processes():
    count = 0
    for proc in psutil.process_iter(["name"]):
        try:
            if "blender" in proc.info["name"].lower():
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    print(f"Found {count} blender processes")
    return count


@pytest.fixture(scope="session", autouse=True)
def create_scene():
    cmd = [
        BLENDER_BIN,
        "-b",
        "--python-expr",
        f"import bpy; bpy.ops.wm.read_homefile(); render_settings = bpy.context.scene.render; "
        "render_settings.use_overwrite = False; render_settings.use_placeholder = True; "
        "render_settings.filepath = '//out/'; "
        "bpy.ops.wm.save_as_mainfile(filepath='test_scene.blend')",
    ]
    _ = subprocess.run(cmd, capture_output=True, text=True)


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

    result = subprocess.run(cmd, capture_output=True, text=True)
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
        "--python-expr",
        py_expr,
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    start_check = time.time()
    while (time.time() - start_check) < 30:
        blender_count = count_blender_processes()
        if blender_count >= 2:
            break
        time.sleep(1)

    stdout, stderr = process.communicate()

    print(stdout)
    print(stderr)

    retcode = process.wait()

    print(f"Retcode: {retcode}")

    for i in range(1, 251):
        frame_file = os.path.join("out/", f"{i:04d}.png")
        assert os.path.exists(frame_file), f"Missing frame: {frame_file}"

    print(f"\nExtension run complete. 250 frames verified in out/.")


def test_run_benchmark():
    py_expr = (
        "import bpy,time,sys; "
        "m=sys.modules['bl_ext.user_default.pseudo_rendering_farm']; "
        "bpy.ops.render.benchmarking(); "
        "exec('while m.Globals.is_benchmarking:\\n m.check_render_status()\\n time.sleep(1)'); "
        "bpy.ops.wm.quit_blender()"
    )

    cmd = [BLENDER_BIN, "-b", TEST_BLEND_FILE, "--python-expr", py_expr]

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    assert "!!! Benchmarking stats for nerds !!!" in result.stdout
    print("\nBenchmark stats found in output.")


if __name__ == "__main__":
    test_install_extension()
    test_pseudo_rendering_farm()
    test_run_benchmark()

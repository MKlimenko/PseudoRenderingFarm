import importlib.util
import os
import sys
import time
import types

import pytest


def _install_fake_bpy():
    bpy = types.ModuleType("bpy")

    class _Operator:
        pass

    class _Panel:
        pass

    bpy.types = types.SimpleNamespace(
        Operator=_Operator, Panel=_Panel, Scene=types.SimpleNamespace()
    )
    bpy.props = types.SimpleNamespace(IntProperty=lambda **kwargs: None)
    bpy.utils = types.SimpleNamespace(
        register_class=lambda c: None, unregister_class=lambda c: None
    )
    bpy.app = types.SimpleNamespace(
        background=True,
        binary_path="blender",
        timers=types.SimpleNamespace(
            is_registered=lambda f: False,
            register=lambda *args, **kwargs: None,
            unregister=lambda f: None,
        ),
    )
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace(filepath="")
    bpy.ops = types.SimpleNamespace()
    sys.modules["bpy"] = bpy
    return bpy


def _load_addon():
    _install_fake_bpy()
    path = os.path.join(os.path.dirname(__file__), "..", "__init__.py")
    spec = importlib.util.spec_from_file_location("pseudo_rendering_farm_unit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


addon = _load_addon()


@pytest.fixture(autouse=True)
def reset_globals():
    saved = {}
    for key, value in vars(addon.Globals).items():
        if key.startswith("__"):
            continue
        if isinstance(value, list):
            saved[key] = list(value)
        elif isinstance(value, dict):
            saved[key] = dict(value)
        else:
            saved[key] = value
    yield
    for key, value in saved.items():
        setattr(addon.Globals, key, value)


# --- get_worker_subrange ---


def collect_frames(start, end, num_workers, step=1):
    """Union of all worker subranges expanded along the frame-step grid."""
    frames = []
    for worker_id in range(num_workers):
        subrange = addon.get_worker_subrange(start, end, num_workers, worker_id, step)
        if subrange is None:
            continue
        sub_start, sub_end = subrange
        frames.extend(range(sub_start, sub_end + 1, step))
    return frames


def test_subranges_cover_range_exactly_once():
    assert collect_frames(1, 10, 4) == list(range(1, 11))


def test_subranges_with_more_workers_than_frames():
    frames = collect_frames(1, 3, 8)
    assert frames == [1, 2, 3]


def test_subrange_out_of_bounds_worker():
    assert addon.get_worker_subrange(1, 10, 4, 4) is None
    assert addon.get_worker_subrange(1, 10, 4, 99) is None


def test_subrange_empty_range():
    assert addon.get_worker_subrange(10, 5, 2, 0) is None


def test_subranges_align_to_frame_step():
    # Grid frames for start=1, end=10, step=3 are 1, 4, 7, 10
    assert addon.get_worker_subrange(1, 10, 2, 0, 3) == (1, 4)
    assert addon.get_worker_subrange(1, 10, 2, 1, 3) == (7, 10)
    assert collect_frames(1, 10, 2, 3) == [1, 4, 7, 10]


def test_subranges_exhaustive_coverage():
    for start in (0, 1, 17):
        for total in (1, 2, 5, 48, 100):
            for step in (1, 2, 3):
                end = start + (total - 1) * step
                expected = list(range(start, end + 1, step))
                for workers in (1, 2, 3, 7, 16):
                    assert (
                        collect_frames(start, end, workers, step) == expected
                    ), f"start={start} end={end} workers={workers} step={step}"


# --- is_image_valid ---

PNG_FOOTER = b"IEND\xaeB`\x82"
JPG_FOOTER = b"\xff\xd9"


def test_missing_and_empty_files_are_invalid(tmp_path):
    assert not addon.is_image_valid(str(tmp_path / "missing.png"))
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert not addon.is_image_valid(str(empty))
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(b"abc")
    assert not addon.is_image_valid(str(tiny))


def test_png_validation(tmp_path):
    complete = tmp_path / "complete.png"
    complete.write_bytes(b"x" * 100 + PNG_FOOTER)
    assert addon.is_image_valid(str(complete))

    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(b"x" * 100)
    assert not addon.is_image_valid(str(truncated))


def test_jpg_validation(tmp_path):
    complete = tmp_path / "complete.jpg"
    complete.write_bytes(b"x" * 100 + JPG_FOOTER)
    assert addon.is_image_valid(str(complete))

    truncated = tmp_path / "truncated.jpeg"
    truncated.write_bytes(b"x" * 100)
    assert not addon.is_image_valid(str(truncated))


def test_exr_validation(tmp_path):
    big = tmp_path / "big.exr"
    big.write_bytes(b"x" * 2000)
    assert addon.is_image_valid(str(big))

    small = tmp_path / "small.exr"
    small.write_bytes(b"x" * 100)
    assert not addon.is_image_valid(str(small))


def test_unknown_extensions_are_treated_as_valid(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_bytes(b"some unrelated file")
    assert addon.is_image_valid(str(other))


# --- cleanup_corrupted_frames ---


def test_cleanup_only_touches_recent_render_output(tmp_path):
    sys.modules["bpy"].context.scene = types.SimpleNamespace(
        render=types.SimpleNamespace(filepath=str(tmp_path / "frame_"))
    )
    addon.Globals.start_time = time.time() - 60

    corrupt_new = tmp_path / "0001.png"
    corrupt_new.write_bytes(b"garbage")

    corrupt_old = tmp_path / "0000.png"
    corrupt_old.write_bytes(b"garbage")
    old_time = time.time() - 3600
    os.utime(corrupt_old, (old_time, old_time))

    valid_new = tmp_path / "0002.png"
    valid_new.write_bytes(b"x" * 100 + PNG_FOOTER)

    unrelated = tmp_path / "notes.txt"
    unrelated.write_bytes(b"do not delete")

    deleted = addon.cleanup_corrupted_frames()

    assert deleted == 1
    assert not corrupt_new.exists()
    assert corrupt_old.exists(), "files predating the run must not be deleted"
    assert valid_new.exists()
    assert unrelated.exists(), "non-image files must not be deleted"


def test_cleanup_returns_zero_for_missing_directory(tmp_path):
    sys.modules["bpy"].context.scene = types.SimpleNamespace(
        render=types.SimpleNamespace(filepath=str(tmp_path / "nonexistent" / "frame_"))
    )
    assert addon.cleanup_corrupted_frames() == 0


# --- GPU helpers ---


def test_using_same_gpus():
    addon.Globals.gpu_devices = []
    assert addon.using_same_gpus()

    addon.Globals.gpu_devices = ["10de/2783/0", "10de/2783/1"]
    assert addon.using_same_gpus()

    addon.Globals.gpu_devices = ["10de/2783/0", "8086/1234/0"]
    assert not addon.using_same_gpus()


def test_is_system_balanced():
    addon.Globals.gpu_devices = ["10de/2783/0"]
    assert addon.is_system_balanced()

    addon.Globals.gpu_devices = ["10de/2783/0", "8086/1234/0"]
    assert not addon.is_system_balanced()


def test_get_env_for_instance_round_robin():
    env_a = {"GPU": "a"}
    env_b = {"GPU": "b"}
    addon.Globals.gpu_devices_envs = [env_a, env_b]
    assert addon.get_env_for_instance(0) is env_a
    assert addon.get_env_for_instance(1) is env_b
    assert addon.get_env_for_instance(2) is env_a


def test_get_env_for_instance_falls_back_to_current_env():
    addon.Globals.gpu_devices_envs = []
    env = addon.get_env_for_instance(0)
    assert env is not None


# --- render status timer ---


def test_check_render_status_stops_after_cancel():
    addon.Globals.active_render_processes = []
    addon.Globals.is_benchmarking = False
    addon.Globals.is_rendering_active = False
    assert addon.check_render_status() is None


# --- benchmark command construction ---


def _capture_benchmark_commands(tmp_path, monkeypatch, gpu_devices):
    fake_bpy = sys.modules["bpy"]
    fake_bpy.context.scene = types.SimpleNamespace(
        frame_start=1, frame_end=500, frame_step=1
    )
    fake_bpy.data.filepath = "scene.blend"
    addon.Globals.bench_temp_dir = str(tmp_path)
    addon.Globals.current_bench_instances = 2
    addon.Globals.gpu_devices = gpu_devices
    addon.Globals.gpu_devices_envs = [dict(os.environ)]
    addon.Globals.active_render_processes = []

    commands = []

    def fake_popen(cmd, env=None):
        commands.append(cmd)
        return types.SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(addon.subprocess, "Popen", fake_popen)
    addon.launch_benchmark_iteration()
    return commands


def test_benchmark_is_clamped_on_unbalanced_systems(tmp_path, monkeypatch):
    # Different GPUs: every instance claims frames via placeholders, but
    # must still be limited to the benchmark window
    commands = _capture_benchmark_commands(
        tmp_path, monkeypatch, ["10de/2783/0", "8086/1234/0"]
    )
    assert len(commands) == 2
    for cmd in commands:
        window = (cmd[cmd.index("-s") + 1], cmd[cmd.index("-e") + 1])
        assert window == ("1", "48"), "benchmark must not render the whole animation"


def test_benchmark_splits_window_on_balanced_systems(tmp_path, monkeypatch):
    commands = _capture_benchmark_commands(tmp_path, monkeypatch, [])
    assert len(commands) == 2
    windows = [(cmd[cmd.index("-s") + 1], cmd[cmd.index("-e") + 1]) for cmd in commands]
    assert windows == [("1", "24"), ("25", "48")]

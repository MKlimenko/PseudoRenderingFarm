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
        handlers=types.SimpleNamespace(persistent=lambda f: f, load_post=[]),
        timers=types.SimpleNamespace(
            is_registered=lambda f: False,
            register=lambda *args, **kwargs: None,
            unregister=lambda f: None,
        ),
    )
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.context = types.SimpleNamespace(
        window_manager=types.SimpleNamespace(windows=[])
    )
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
    addon.Globals.output_dir = str(tmp_path)
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
    addon.Globals.output_dir = str(tmp_path / "nonexistent")
    assert addon.cleanup_corrupted_frames() == 0


def test_cleanup_without_output_dir_is_a_noop():
    addon.Globals.output_dir = ""
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


def fake_process(returncode):
    """Stand-in for Popen: returncode None means still running."""
    return types.SimpleNamespace(poll=lambda: returncode, returncode=returncode)


def make_instance(tmp_path, name, log=b"", returncode=0):
    log_path = tmp_path / name
    log_path.write_bytes(log)
    return addon.Instance(fake_process(returncode), str(log_path))


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
    addon.Globals.log_dir = str(tmp_path)
    addon.Globals.current_bench_instances = 2
    addon.Globals.gpu_devices = gpu_devices
    addon.Globals.gpu_devices_envs = [dict(os.environ)]
    addon.Globals.active_render_processes = []

    commands = []

    def fake_popen(cmd, env=None, **kwargs):
        commands.append(cmd)
        return fake_process(None)

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


# --- progress ---

SAVED_LINE = b"00:03.073  render           | Saved: '/out/%04d.png'\n"
SKIPPED_LINE = (
    b'00:00.545  render           | Skipping existing frame "/out/%04d.png"\n'
)


def test_progress_counts_saved_frames_across_instances(tmp_path):
    noise = b"00:01.106  render           | Fra: 2 | Rendering 64 / 64 samples\n"
    addon.Globals.instances = [
        make_instance(tmp_path, "a.log", noise + SAVED_LINE % 1 + SAVED_LINE % 2),
        make_instance(tmp_path, "b.log", SAVED_LINE % 3 + noise),
    ]
    addon.Globals.total_frames = 10

    assert addon.update_progress()
    assert addon.Globals.frames_done == 3
    assert not addon.update_progress(), "nothing new was logged"


def test_progress_ignores_incomplete_lines(tmp_path):
    instance = make_instance(tmp_path, "a.log", SAVED_LINE % 1 + b"00:04  render | Sav")
    addon.Globals.instances = [instance]
    addon.Globals.total_frames = 10

    addon.update_progress()
    assert addon.Globals.frames_done == 1

    with open(instance.log_path, "ab") as f:
        f.write(b"ed: '/out/0002.png'\n")
    addon.update_progress()
    assert addon.Globals.frames_done == 2


def test_progress_counts_skipped_frames_only_without_placeholders(tmp_path):
    log = SAVED_LINE % 1 + SKIPPED_LINE % 2
    addon.Globals.total_frames = 10

    addon.Globals.gpu_devices = []
    addon.Globals.instances = [make_instance(tmp_path, "a.log", log)]
    addon.update_progress()
    assert addon.Globals.frames_done == 2

    addon.Globals.gpu_devices = ["10de/2783/0", "8086/1234/0"]
    addon.Globals.instances = [make_instance(tmp_path, "b.log", log)]
    addon.update_progress()
    assert addon.Globals.frames_done == 1


def test_progress_is_capped_at_total(tmp_path):
    log = b"".join(SAVED_LINE % i for i in range(1, 6))
    addon.Globals.instances = [make_instance(tmp_path, "a.log", log)]
    addon.Globals.total_frames = 3
    addon.update_progress()
    assert addon.Globals.frames_done == 3


# --- failure detection ---


def test_format_frame_ranges():
    assert addon.format_frame_ranges([]) == ""
    assert addon.format_frame_ranges([5]) == "5"
    assert addon.format_frame_ranges([3, 4, 5, 9, 12, 13]) == "3-5, 9, 12-13"
    assert addon.format_frame_ranges([1, 4, 7, 13], step=3) == "1-7, 13"
    assert addon.format_frame_ranges([1, 3, 5, 7, 9, 11]) == "1, 3, 5, 7, ..."


def _expect_frames(tmp_path, present, empty, absent):
    """Registers the expected frames, creating only the present/empty ones."""
    expected = {}
    for frame in sorted(present + empty + absent):
        path = tmp_path / f"{frame:04d}.png"
        if frame in present:
            path.write_bytes(b"x" * 100 + PNG_FOOTER)
        elif frame in empty:
            path.write_bytes(b"")
        expected[frame] = str(path)
    addon.Globals.expected_frames = expected


def test_find_missing_frames(tmp_path):
    _expect_frames(tmp_path, present=[1, 4], empty=[2], absent=[3])
    assert addon.find_missing_frames() == [2, 3]


def _finish_render(tmp_path, returncodes):
    addon.Globals.is_rendering_active = True
    addon.Globals.is_benchmarking = False
    addon.Globals.active_render_processes = []
    addon.Globals.output_dir = str(tmp_path)
    addon.Globals.total_frames = len(addon.Globals.expected_frames)
    addon.Globals.start_time = time.time() - 60
    addon.Globals.instances = [
        make_instance(tmp_path, f"{i}.log", returncode=code)
        for i, code in enumerate(returncodes)
    ]
    assert addon.check_render_status() is None
    assert not addon.Globals.is_rendering_active


def test_finished_render_without_problems_has_no_error(tmp_path):
    _expect_frames(tmp_path, present=[1, 2, 3], empty=[], absent=[])
    _finish_render(tmp_path, [0, 0])
    assert addon.Globals.error_msg == ""


def test_finished_render_reports_failed_instances_and_missing_frames(tmp_path):
    _expect_frames(tmp_path, present=[1, 2], empty=[3], absent=[4, 5, 6])
    _finish_render(tmp_path, [0, -9])
    assert addon.Globals.error_msg == (
        "1 of 2 instances failed, 4 frames missing (3-6)"
    )
    assert not (
        tmp_path / "0003.png"
    ).exists(), "placeholders of a dead instance must not block a resumed render"


def test_finished_render_reports_missing_frames_despite_clean_exit(tmp_path):
    _expect_frames(tmp_path, present=[1], empty=[], absent=[2])
    _finish_render(tmp_path, [0])
    assert addon.Globals.error_msg == "1 frames missing (2)"


def _finish_benchmark_iteration(tmp_path, instances_tested, results, returncode):
    scene = types.SimpleNamespace(pseudo_rendering_farm_instances=0)
    sys.modules["bpy"].context.scene = scene
    addon.Globals.is_benchmarking = True
    addon.Globals.is_rendering_active = False
    addon.Globals.active_render_processes = []
    addon.Globals.current_bench_instances = instances_tested
    addon.Globals.benchmark_results = dict(results)
    addon.Globals.peak_throughput = max(results.values(), default=0)
    addon.Globals.benchmark_frames = 48
    addon.Globals.start_time = time.time() - 1
    addon.Globals.instances = [make_instance(tmp_path, "a.log", returncode=returncode)]
    assert addon.check_render_status() is None
    assert not addon.Globals.is_benchmarking
    return scene


def test_benchmark_stops_when_an_instance_fails(tmp_path):
    scene = _finish_benchmark_iteration(tmp_path, 3, {1: 2.0, 2: 3.0}, returncode=1)
    assert addon.Globals.benchmark_results[3] == 0
    assert scene.pseudo_rendering_farm_instances == 2
    assert "3 instances" in addon.Globals.error_msg


def test_benchmark_fails_when_nothing_could_render(tmp_path):
    scene = _finish_benchmark_iteration(tmp_path, 1, {}, returncode=1)
    assert scene.pseudo_rendering_farm_instances == 0
    assert addon.Globals.error_msg.startswith("Benchmark failed")


# --- scene validation ---


class Reporter:
    def __init__(self):
        self.reports = []

    def report(self, level, message):
        self.reports.append((level, message))


def test_video_output_is_rejected():
    addon.Globals.gpu_devices = []
    reporter = Reporter()
    render = types.SimpleNamespace(is_movie_format=True)
    assert not addon.is_scene_configured(reporter, render)
    assert "Video output" in reporter.reports[0][1]


def test_image_sequence_is_accepted():
    addon.Globals.gpu_devices = []
    reporter = Reporter()
    render = types.SimpleNamespace(is_movie_format=False)
    assert addon.is_scene_configured(reporter, render)
    assert not reporter.reports


# --- file load ---


def test_loading_a_file_aborts_the_benchmark_but_not_the_render():
    addon.Globals.active_render_processes = []
    addon.Globals.is_benchmarking = True
    addon.Globals.is_rendering_active = False
    addon.on_file_loaded(None)
    assert not addon.Globals.is_benchmarking

    addon.Globals.is_rendering_active = True
    addon.on_file_loaded(None)
    assert addon.Globals.is_rendering_active

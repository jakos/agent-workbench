# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Tests for scripts/sandbox.py, the opt-in Podman/Docker command sandbox.

Run: uv run --script plugins/task-flow/tests/test_sandbox.py
     uv run --script plugins/task-flow/tests/test_sandbox.py --backend podman
     uv run --script plugins/task-flow/tests/test_sandbox.py --backend docker [--with-provisioning]

The ordinary invocation needs no container engine: configuration, argv, environment
and capture cases are pure, and the lifecycle cases drive a fake engine (a small
Python program written to a temporary directory, started as `python fake_engine.py
<state dir> ...`) that records every call and emulates `run`, `rm`, `info`, `image
inspect` and the volume commands. No daemon, image, network or model is used.

`--backend podman|docker` additionally runs the real-backend cases against that engine
and the approved local image (TASK_FLOW_SANDBOX_IMAGE overrides it; nothing is
pulled). They use only temporary fixture projects and a sentinel outside the mounted
worktree, and they fail — they are not skipped — when the engine or the image is
unavailable. `--with-provisioning` adds the locked-dependency provisioning fixture,
which needs network access for `uv lock` and the provisioning container.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import models
import sandbox

APPROVED_IMAGE = (
    "ghcr.io/astral-sh/uv:python3.14-bookworm-slim"
    "@sha256:e1869b1ddae289d6aa3c76649d0b663c11cb3215371146bf7dce95c821935fea"
)
DIGEST = "@sha256:" + "a" * 64
IMAGE = "example.invalid/uv:py3.14" + DIGEST
REAL_BACKEND: str | None = None
WITH_PROVISIONING = False

FAKE_ENGINE = r"""
import json, os, shutil, signal, subprocess, sys
from pathlib import Path

state = Path(sys.argv[1])
args = sys.argv[2:]
config = json.loads((state / "config.json").read_text(encoding="utf-8"))
volumes_path = state / "volumes.json"
volumes = json.loads(volumes_path.read_text(encoding="utf-8")) if volumes_path.exists() else {}

def save():
    volumes_path.write_text(json.dumps(volumes), encoding="utf-8")

with open(state / "calls.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "env": dict(os.environ)}) + "\n")

VALUE_OPTIONS = {"--name", "--label", "--network", "--pull", "--user", "--tmpfs",
    "--cap-drop", "--cap-add", "--security-opt", "--memory", "--memory-swap",
    "--pids-limit", "--workdir", "--volume", "--env", "--userns", "--format"}

def split_volume(spec):
    for suffix in (":ro", ":U", ":rw"):
        if spec.endswith(suffix):
            spec = spec[: -len(suffix)]
    source, _, dest = spec.rpartition(":/")
    return source, "/" + dest

def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

command = args[0] if args else ""
if command == "version":
    print(config.get("version", "5.8.3"))
    sys.exit(0)
if command == "info":
    if config.get("info_rc", 0):
        print("cannot connect to the runtime", file=sys.stderr)
        sys.exit(config["info_rc"])
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    if "Rootless" in fmt:
        print("true" if config.get("rootless") else "false")
    elif "SecurityOptions" in fmt:
        print(json.dumps(["name=rootless"] if config.get("rootless") else ["name=seccomp"]))
    else:
        print("{}")
    sys.exit(0)
if command == "image":
    sys.exit(0 if config.get("image_present", True) else 1)
if command == "volume":
    action, name = args[1], args[-1]
    if action == "inspect":
        sys.exit(0 if name in volumes else 1)
    if action == "create":
        volumes[name] = None
    elif action == "rm":
        volumes.pop(name, None)
    save()
    sys.exit(0)
if command == "rm":
    pid_file = state / "containers" / (args[-1] + ".pid")
    if pid_file.exists():
        kill_tree(int(pid_file.read_text()))
    sys.exit(0)
if command != "run":
    sys.exit(2)

index, options = 1, []
while index < len(args) and args[index].startswith("--"):
    option = args[index]
    if option in VALUE_OPTIONS:
        options.append((option, args[index + 1]))
        index += 2
    else:
        options.append((option, None))
        index += 1
image, argv = args[index], args[index + 1:]
name = next((v for o, v in options if o == "--name"), "anonymous")
if config.get("run_exit"):
    print("Error: the engine failed: " + config.get("run_message", "storage"), file=sys.stderr)
    sys.exit(config["run_exit"])
env, mounts = {}, {}
for option, value in options:
    if option == "--env":
        key, sep, val = value.partition("=")
        if sep:
            env[key] = val
        elif key in os.environ:
            env[key] = os.environ[key]
    elif option == "--volume":
        source, dest = split_volume(value)
        mounts[dest] = source
with open(state / "containers.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"name": name, "env": env, "argv": argv, "mounts": mounts}) + "\n")
venv = mounts.get("/venv")
if argv[:1] == ["cat"] and argv[1:] == ["/venv/.task-flow-ready"]:
    marker = volumes.get(venv)
    if marker:
        print(marker)
        sys.exit(0)
    sys.exit(1)
if argv[:1] == ["chown"]:
    sys.exit(0)
if argv[:2] == ["uv", "sync"]:
    sys.exit(config.get("sync_exit", 0))
if argv[:2] == ["python", "-c"] and "/venv/.task-flow-ready" in argv:
    volumes[venv] = argv[-1]
    save()
    sys.exit(0)
program = sys.executable if argv[0] in ("python", "python3") else shutil.which(argv[0])
if program is None:
    print("Error: executable file `%s` not found in $PATH: No such file or directory: "
          "OCI runtime attempted to invoke a command that was not found" % argv[0],
          file=sys.stderr)
    sys.exit(127)
run_env = dict(env)
for key in ("PATH", "SYSTEMROOT", "SystemRoot"):
    if key in os.environ:
        run_env[key] = os.environ[key]
kwargs = {"start_new_session": True} if os.name != "nt" else {}
proc = subprocess.Popen([program, *argv[1:]], cwd=mounts.get("/work"), env=run_env, **kwargs)
(state / "containers").mkdir(exist_ok=True)
(state / "containers" / (name + ".pid")).write_text(str(proc.pid))
sys.exit(proc.wait())
"""


class FakeEngine:
    """A fake podman/docker client: `python fake_engine.py <state> ...`."""

    def __init__(self, root: Path, backend: str = "podman", **config):
        self.state = root / "engine-state"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "config.json").write_text(json.dumps(config), encoding="utf-8")
        script = root / "fake_engine.py"
        script.write_text(FAKE_ENGINE, encoding="utf-8")
        self.engine = sandbox.Engine(
            backend, [sys.executable, str(script), str(self.state)], dict(os.environ)
        )

    def configure(self, **config) -> None:
        current = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        current.update(config)
        (self.state / "config.json").write_text(json.dumps(current), encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def containers(self) -> list[dict]:
        path = self.state / "containers.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def run_calls(self) -> list[list[str]]:
        return [c["args"] for c in self.calls() if c["args"][:1] == ["run"]]


def policy(**overrides) -> sandbox.Policy:
    table = {"backend": "podman", "image": IMAGE}
    table.update(overrides)
    return sandbox.parse(table, "test")


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sandbox test ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.work = self.root / "work tree"
        self.work.mkdir()


# ---------------------------------------------------------------------------- AC2


class ConfigTest(unittest.TestCase):
    def test_accepts_pinned_podman_and_docker_with_defaults(self) -> None:
        for backend in ("podman", "docker"):
            parsed = policy(backend=backend)
            self.assertTrue(parsed.enabled)
            self.assertEqual(parsed.image, IMAGE)
            self.assertEqual((parsed.memory, parsed.pids_limit), ("1g", 512))
            self.assertEqual(parsed.tmp_size, "1g")
            self.assertEqual(parsed.pass_env, ())
            self.assertFalse(parsed.provision)
        self.assertFalse(sandbox.parse({}, "x").enabled)
        self.assertFalse(sandbox.parse({"backend": "none"}, "x").enabled)
        self.assertEqual(
            sandbox.parse({"image": APPROVED_IMAGE}, "x").image, APPROVED_IMAGE
        )

    def assertRefused(self, table: dict, *needles: str) -> None:
        with self.assertRaises(sandbox.ConfigError) as ctx:
            sandbox.parse(table, "cfg.toml")
        self.assertIn("cfg.toml", str(ctx.exception))
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))

    def test_refuses_unsupported_backends_and_unknown_keys(self) -> None:
        self.assertRefused({"backend": "srt", "image": IMAGE}, "not implemented yet")
        self.assertRefused({"backend": "wsl", "image": IMAGE}, "not implemented yet")
        self.assertRefused({"backend": "lxc", "image": IMAGE}, "sandbox.backend")
        self.assertRefused(
            {"backend": "podman", "image": IMAGE, "network": "on"}, "unknown key"
        )
        self.assertRefused({"backend": "podman"}, "sandbox.image", "required")
        self.assertRefused([], "must be a table")

    def test_refuses_unpinned_images_and_invalid_limits(self) -> None:
        for image in (
            "ghcr.io/astral-sh/uv:python3.14-bookworm-slim",
            "uv:latest",
            "uv@sha256:abc",
            "UV" + DIGEST,
        ):
            self.assertRefused({"backend": "podman", "image": image}, "sandbox.image")
        for memory in ("0g", "1", "1x", 1, "-1g", "1.5g"):
            self.assertRefused(
                {"backend": "podman", "image": IMAGE, "memory": memory},
                "sandbox.memory",
            )
        self.assertRefused(
            {"backend": "podman", "image": IMAGE, "tmp_size": "0m"}, "sandbox.tmp_size"
        )
        for pids in (0, -1, True, 1.5, "512"):
            self.assertRefused(
                {"backend": "podman", "image": IMAGE, "pids_limit": pids},
                "sandbox.pids_limit",
            )
        self.assertRefused(
            {"backend": "podman", "image": IMAGE, "provision": "yes"},
            "sandbox.provision",
        )

    def test_refuses_unsafe_pass_env(self) -> None:
        for name in (
            "OPENAI_API_KEY",
            "MISTRAL_KEY",
            "GH_TOKEN",
            "DB_PASSWORD",
            "HTTP_PROXY",
            "no_proxy",
            "DOCKER_HOST",
            "CONTAINER_HOST",
            "SSH_AUTH_SOCK",
            "TASK_FLOW_PUBLISH_GH",
            "PATH",
            "HOME",
            "UV_INDEX_URL",
            "PYTHONPATH",
            "bad-name",
        ):
            self.assertRefused(
                {"backend": "podman", "image": IMAGE, "pass_env": [name]}, "pass_env"
            )
        self.assertRefused(
            {"backend": "podman", "image": IMAGE, "pass_env": "X"}, "list"
        )
        self.assertEqual(policy(pass_env=["TEST_MODE"]).pass_env, ("TEST_MODE",))


class ModelsIntegrationTest(unittest.TestCase):
    """models.sandbox_policy: repository-only, missing means disabled (AC1, AC2)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.repo = Path(tmp.name) / "repo"
        self.user_path = self.home / "models.toml"
        (self.repo / ".claude").mkdir(parents=True)
        self.home.mkdir()
        patcher = mock.patch.object(models, "user_file", lambda: self.user_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def project(self, text: str) -> None:
        (self.repo / ".claude" / "task-flow.models.toml").write_text(
            text, encoding="utf-8"
        )

    def test_missing_and_none_are_disabled(self) -> None:
        self.assertIs(models.sandbox_policy(self.repo), sandbox.DISABLED)
        self.project('[sandbox]\nbackend = "none"\n')
        self.assertFalse(models.sandbox_policy(self.repo).enabled)

    def test_enabled_project_policy_and_user_file_refused(self) -> None:
        self.project(f'[sandbox]\nbackend = "docker"\nimage = "{IMAGE}"\n')
        found = models.sandbox_policy(self.repo)
        self.assertEqual(found.backend, "docker")
        self.assertIn("task-flow.models.toml", found.source)
        self.user_path.write_text('[sandbox]\nbackend = "none"\n', encoding="utf-8")
        with self.assertRaises(models.ConfigError) as ctx:
            models.sandbox_policy(self.repo)
        self.assertIn("repository-only", str(ctx.exception))

    def test_pass_env_may_not_name_a_profile_key(self) -> None:
        self.project(
            '[profiles.p]\ndriver = "openai-loop"\nbase_url = "https://x.invalid/v1"\n'
            'model = "m"\napi_key_env = "VENDOR_ACCESS"\nbilling = "api"\n\n'
            f'[sandbox]\nbackend = "podman"\nimage = "{IMAGE}"\n'
            'pass_env = ["VENDOR_ACCESS"]\n'
        )
        for call in (models.sandbox_policy, models.load):
            with self.assertRaises(models.ConfigError) as ctx:
                call(self.repo)
            self.assertIn("api_key_env", str(ctx.exception))


# ---------------------------------------------------------------------------- capture


class CaptureTest(TempCase):
    def test_enabled_policy_is_captured_and_wins_over_later_config(self) -> None:
        task = self.root / ".work" / "t1"
        task.mkdir(parents=True)
        enabled = policy(memory="2g", pass_env=["TEST_MODE"])
        self.assertEqual(sandbox.capture(task, enabled), enabled)
        captured = task / sandbox.CAPTURE_NAME
        self.assertTrue(captured.is_file())
        # a later configuration (edited by a model, or removed) cannot disable it
        for later in (sandbox.DISABLED, policy(backend="docker")):
            again = sandbox.capture(task, later)
            self.assertEqual((again.backend, again.memory), ("podman", "2g"))
            self.assertEqual(again.pass_env, ("TEST_MODE",))

    def test_disabled_policy_writes_nothing_and_bad_capture_refuses(self) -> None:
        task = self.root / "task"
        task.mkdir()
        self.assertIs(sandbox.capture(task, sandbox.DISABLED), sandbox.DISABLED)
        self.assertEqual(list(task.iterdir()), [])
        (task / sandbox.CAPTURE_NAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(sandbox.ConfigError):
            sandbox.capture(task, sandbox.DISABLED)
        (task / sandbox.CAPTURE_NAME).write_text(
            json.dumps(
                {"schema": sandbox.CAPTURE_SCHEMA, "sandbox": {"backend": "none"}}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(sandbox.ConfigError):
            sandbox.capture(task, sandbox.DISABLED)


# ---------------------------------------------------------------------------- AC3


class ArgvTest(unittest.TestCase):
    def argv(self, backend: str = "podman", **kwargs) -> list[str]:
        values = {
            "name": "task-flow-sbx-1",
            "argv": ["python", "-m", "unittest"],
            "mount": r"C:\Users\a b\work",
            "git_mount": r"C:\Users\a b\work\.git",
            "mask_work": True,
            "venv_volume": "task-flow-venv-k",
            "forwarded": ["LANG", "TEST_MODE"],
            "user": "10001:10001",
        }
        values.update(kwargs)
        return sandbox.run_argv(
            policy(backend=backend, memory="768m", pids_limit=99), ["eng"], **values
        )

    def pair(self, argv: list[str], option: str) -> list[str]:
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == option]

    def test_confinement_flags_mounts_and_identity(self) -> None:
        argv = self.argv()
        self.assertEqual(argv[:3], ["eng", "run", "--rm"])
        self.assertEqual(self.pair(argv, "--network"), ["none"])
        self.assertEqual(self.pair(argv, "--pull"), ["never"])
        self.assertEqual(self.pair(argv, "--user"), ["10001:10001"])
        self.assertIn("--read-only", argv)
        self.assertEqual(self.pair(argv, "--cap-drop"), ["ALL"])
        self.assertEqual(self.pair(argv, "--security-opt"), ["no-new-privileges"])
        self.assertEqual(self.pair(argv, "--memory"), ["768m"])
        self.assertEqual(self.pair(argv, "--memory-swap"), ["768m"])
        self.assertEqual(self.pair(argv, "--pids-limit"), ["99"])
        self.assertEqual(self.pair(argv, "--workdir"), ["/work"])
        self.assertEqual(
            self.pair(argv, "--volume"),
            [
                r"C:\Users\a b\work:/work",
                r"C:\Users\a b\work\.git:/work/.git:ro",
                "task-flow-venv-k:/venv:ro",
            ],
        )
        tmpfs = self.pair(argv, "--tmpfs")
        self.assertTrue(tmpfs[0].startswith("/tmp:"))
        self.assertIn("size=1g", tmpfs[0])
        self.assertTrue(tmpfs[1].startswith("/work/.work:"))
        self.assertNotIn("--privileged", argv)
        for arg in argv:
            self.assertNotIn(":U", arg, "the project bind mount never gets :U")
            self.assertFalse(arg.endswith(".sock"), arg)
        self.assertEqual(argv[-4:], [IMAGE, "python", "-m", "unittest"])
        self.assertNotIn("sh", argv)

    def test_environment_by_name_and_proxy_handling(self) -> None:
        podman = self.argv()
        envs = self.pair(podman, "--env")
        self.assertIn("LANG", envs)
        self.assertIn("TEST_MODE", envs)
        self.assertIn("HOME=/tmp", envs)
        self.assertIn("UV_OFFLINE=1", envs)
        self.assertIn("VIRTUAL_ENV=/venv", envs)
        self.assertIn("--http-proxy=false", podman)
        docker = self.argv("docker", venv_volume=None)
        self.assertNotIn("--http-proxy=false", docker)
        envs = self.pair(docker, "--env")
        for name in sandbox.PROXY_NAMES:
            self.assertIn(f"{name}=", envs)
        self.assertNotIn("VIRTUAL_ENV=/venv", envs)
        self.assertNotIn("--userns", docker)
        self.assertEqual(self.pair(self.argv(keep_id=True), "--userns"), ["keep-id"])

    def test_windows_and_posix_mount_paths(self) -> None:
        self.assertEqual(
            sandbox.host_mount_path(Path(r"C:\Users\a b\w"), windows=True),
            r"C:\Users\a b\w",
        )
        self.assertEqual(
            sandbox.host_mount_path("C:/Users/x/w", windows=True), r"C:\Users\x\w"
        )
        for bad in (r"\\server\share\w", "/c/Users/x", "relative\\w"):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.host_mount_path(bad, windows=True)
        self.assertEqual(
            sandbox.host_mount_path("/home/u/w d", windows=False), "/home/u/w d"
        )
        for bad in ("/home/u/a:b", "rel/w", "/x\ny"):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.host_mount_path(bad, windows=False)

    def test_host_executables_and_git_are_refused(self) -> None:
        for program in (
            "git",
            "/usr/bin/git",
            "GIT.EXE",
            r"C:\Python\python.exe",
            "tools\\run.cmd",
            "npm.cmd",
            "C:/x/y",
            "build.ps1",
        ):
            self.assertIsNotNone(sandbox.program_problem(program), program)
        self.assertIn("host only", sandbox.program_problem("git"))
        for program in ("python", "uv", "./scripts/check.py", "/usr/bin/env", "pytest"):
            self.assertIsNone(sandbox.program_problem(program), program)

    def test_container_user_is_numeric_and_never_root(self) -> None:
        self.assertEqual(sandbox.container_user(windows=True), "10001:10001")
        self.assertEqual(
            sandbox.container_user(
                windows=False, getuid=lambda: 1000, getgid=lambda: 100
            ),
            "1000:100",
        )
        with self.assertRaises(sandbox.SandboxError):
            sandbox.container_user(windows=False, getuid=lambda: 0, getgid=lambda: 0)

    def test_workload_and_engine_environments(self) -> None:
        host = {
            "PATH": "/host/bin",
            "HOME": "/home/u",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUTF8": "1",
            "PYTHONPATH": "/host/lib",
            "UV_CACHE_DIR": "/host/cache",
            "OPENAI_API_KEY": "sk-secret",
            "HTTP_PROXY": "http://proxy:3128",
            "DOCKER_HOST": "unix:///run/docker.sock",
            "TEST_MODE": "fast",
            "TASK_FLOW_PUBLISH_GH": "ghp",
        }
        forwarded = sandbox.workload_env(
            host, policy(pass_env=["TEST_MODE", "ABSENT_X"]), host
        )
        self.assertEqual(
            forwarded,
            {
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONUTF8": "1",
                "TEST_MODE": "fast",
            },
        )
        engine_env = sandbox.engine_env(host)
        self.assertIn("PATH", engine_env)
        self.assertIn("DOCKER_HOST", engine_env)
        for name in (
            "OPENAI_API_KEY",
            "HTTP_PROXY",
            "TASK_FLOW_PUBLISH_GH",
            "TEST_MODE",
        ):
            self.assertNotIn(name, engine_env)


# ---------------------------------------------------------------------------- AC3, AC5, AC9


class FakeEngineRunTest(TempCase):
    def run_in(self, fake: FakeEngine, argv, *, forwarded=None, timeout=60, **kw):
        return sandbox.run(
            kw.pop("policy", policy()),
            argv,
            self.work,
            timeout=timeout,
            forwarded=forwarded or {},
            engine=fake.engine,
            **kw,
        )

    def test_runs_in_the_worktree_with_filtered_environment(self) -> None:
        fake = FakeEngine(self.root)
        fake.engine.env["SENTINEL_PROVIDER_TOKEN"] = "sentinel-secret"
        host = {
            "SENTINEL_PROVIDER_TOKEN": "sentinel-secret",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "TEST_MODE": "visible-value",
            "LANG": "C.UTF-8",
        }
        forwarded = sandbox.workload_env(host, policy(pass_env=["TEST_MODE"]), host)
        script = (
            "import json, os; open('out.txt', 'w').write('written');"
            "print(json.dumps({k: os.environ.get(k) for k in "
            "['SENTINEL_PROVIDER_TOKEN', 'HTTPS_PROXY', 'TEST_MODE', 'HOME']}))"
        )
        with mock.patch.dict(os.environ, host):
            result = self.run_in(
                fake,
                ["python", "-c", script],
                forwarded=forwarded,
                policy=policy(pass_env=["TEST_MODE"]),
            )
        self.assertIsNone(result.error, result.output)
        self.assertEqual(result.exit_code, 0)
        seen = json.loads(result.output.strip().splitlines()[-1])
        self.assertIsNone(seen["SENTINEL_PROVIDER_TOKEN"])
        self.assertIsNone(seen["HTTPS_PROXY"])
        self.assertEqual(seen["TEST_MODE"], "visible-value")
        self.assertEqual(seen["HOME"], "/tmp")
        self.assertEqual((self.work / "out.txt").read_text(), "written")
        run = fake.run_calls()[-1]
        self.assertNotIn("visible-value", " ".join(run), "values never travel on argv")
        self.assertIn(result.container, run)

    def test_backend_failure_never_falls_back_to_the_host(self) -> None:
        fake = FakeEngine(self.root, run_exit=125)
        result = self.run_in(fake, ["python", "-c", "open('ran.txt', 'w')"])
        self.assertIn("exit 125", result.error)
        self.assertFalse((self.work / "ran.txt").exists())
        self.assertEqual(len(fake.run_calls()), 1, "no retry, no host execution")

    def test_program_missing_in_image_is_an_error(self) -> None:
        fake = FakeEngine(self.root)
        result = self.run_in(fake, ["no-such-tool-sandbox-test"])
        self.assertEqual(result.exit_code, 127)
        self.assertIn("not available in the sandbox image", result.error)

    def test_pre_start_refusals(self) -> None:
        fake = FakeEngine(self.root)
        for argv in (["git", "status"], [r"C:\tools\x.exe"], []):
            with self.assertRaises(sandbox.SandboxError):
                self.run_in(fake, argv)
        self.assertEqual(fake.run_calls(), [])
        with self.assertRaises(sandbox.SandboxError) as ctx:
            sandbox.engine_for(policy(), which=lambda _name: None)
        self.assertIn("not found on PATH", str(ctx.exception))

    def test_git_dir_read_only_and_work_folder_masked(self) -> None:
        fake = FakeEngine(self.root)
        (self.work / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (self.work / ".work").mkdir()
        self.run_in(fake, ["python", "-c", "pass"])
        run = fake.run_calls()[-1]
        self.assertTrue(any(a.endswith(":/work/.git:ro") for a in run), run)
        self.assertTrue(any(a.startswith("/work/.work:") for a in run), run)

    def test_timeout_removes_only_its_container_and_keeps_output(self) -> None:
        fake = FakeEngine(self.root)
        beat = self.work / "beat.txt"
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            '\'import time\\nwhile True:\\n    open("beat.txt", "a").write("x")\\n'
            "    time.sleep(0.2)'])\n"
            "print('started', flush=True)\n"
            "time.sleep(120)\n"
        )
        started = time.monotonic()
        result = self.run_in(fake, ["python", "-c", script], timeout=4)
        self.assertLess(time.monotonic() - started, 60)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("started", result.output)
        removals = [c["args"] for c in fake.calls() if c["args"][:1] == ["rm"]]
        self.assertEqual(removals, [["rm", "--force", result.container]])
        time.sleep(1.0)
        size = beat.stat().st_size if beat.exists() else 0
        time.sleep(1.0)
        self.assertEqual(beat.stat().st_size if beat.exists() else 0, size)

    def test_interrupt_removes_the_container_and_propagates(self) -> None:
        fake = FakeEngine(self.root)
        removed = []
        fake.engine.remove_container = removed.append
        real = subprocess.Popen

        class Interrupted:
            def __init__(self, *args, **kwargs):
                self.proc = real(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                )
                self.pid = self.proc.pid

            def wait(self, timeout=None):
                raise KeyboardInterrupt

            def kill(self):
                self.proc.kill()

        with self.assertRaises(KeyboardInterrupt):
            self.run_in(fake, ["python", "-c", "pass"], popen=Interrupted)
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0].startswith(sandbox.NAME_PREFIX))

    def test_bounded_output(self) -> None:
        fake = FakeEngine(self.root)
        with mock.patch.object(sandbox, "MAX_OUTPUT_BYTES", 1000):
            result = self.run_in(fake, ["python", "-c", "print('y' * 5000)"])
        self.assertIn("bytes of output not kept", result.output)
        self.assertLess(len(result.output), 1200)


# ---------------------------------------------------------------------------- AC7


LOCK_OK = """
version = 1
requires-python = ">=3.14"

[manifest]
members = ["demo", "member"]

[[package]]
name = "demo"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "member"
version = "0.1.0"
source = { editable = "packages/member" }

[[package]]
name = "iniconfig"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/iniconfig-2.0.0-py3-none-any.whl" }]
"""


class ProvisionTest(TempCase):
    def project(self, lock: str = LOCK_OK) -> Path:
        project = self.root / "proj ect"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\n", encoding="utf-8"
        )
        (project / "uv.lock").write_text(lock, encoding="utf-8")
        return project

    def test_lock_problems(self) -> None:
        self.assertEqual(sandbox.lock_problems(LOCK_OK), [])
        bad = {
            'source = { registry = "https://pypi.org/simple" }\n': "source build",
            'source = { registry = "https://user:pw@idx.invalid/simple" }\nwheels = [{ url = "u" }]\n': "credentials",
            'source = { git = "https://github.com/x/y?rev=1#abc" }\n': "git source",
            'source = { path = "../lib" }\n': "path source",
            'source = { directory = "vendor/lib" }\n': "directory source",
            'source = { editable = "../outside" }\n': "editable source",
            'source = { url = "https://x.invalid/p.whl" }\n': "url source",
        }
        for source, needle in bad.items():
            lock = f'version = 1\n[[package]]\nname = "p"\nversion = "1"\n{source}'
            problems = sandbox.lock_problems(lock)
            self.assertEqual(len(problems), 1, source)
            self.assertIn(needle, problems[0])
        self.assertIn("not valid TOML", sandbox.lock_problems("[[")[0])

    def test_podman_provisioning_is_locked_read_only_and_marked_ready(self) -> None:
        fake = FakeEngine(self.root)
        project = self.project()
        enabled = policy(provision=True)
        fake.engine.env["SENTINEL_PROVIDER_TOKEN"] = "x"
        self.assertTrue(
            sandbox.provision(enabled, project, fake.engine).startswith("provisioned")
        )
        key = sandbox.dependency_key(enabled, (project / "uv.lock").read_bytes())
        venv, cache = sandbox.volume_names(key)
        sync = next(c for c in fake.run_calls() if "sync" in c)
        self.assertEqual(
            sync[-len(sandbox.PROVISION_ARGV) :], list(sandbox.PROVISION_ARGV)
        )
        for flag in (
            "--locked",
            "--no-install-project",
            "--no-install-workspace",
            "--no-build",
        ):
            self.assertIn(flag, sync)
        volumes = [sync[i + 1] for i, a in enumerate(sync) if a == "--volume"]
        self.assertEqual(
            volumes,
            [
                sandbox.host_mount_path(project) + ":/work:ro",
                f"{venv}:/venv:U",
                f"{cache}:/cache:U",
            ],
        )
        self.assertNotIn("--network", sync, "provisioning is the network-on phase")
        envs = [sync[i + 1] for i, a in enumerate(sync) if a == "--env"]
        self.assertTrue(all("=" in e for e in envs), "no host value is forwarded")
        self.assertIn("UV_PROJECT_ENVIRONMENT=/venv", envs)
        self.assertEqual(
            sandbox.provision(enabled, project, fake.engine)[:7], "already"
        )
        # execution then mounts the completed environment read-only, offline
        result = sandbox.run(
            enabled,
            ["python", "-c", "pass"],
            project,
            timeout=60,
            forwarded={},
            engine=fake.engine,
        )
        self.assertEqual(result.exit_code, 0, result.output)
        run = fake.run_calls()[-1]
        self.assertIn(f"{venv}:/venv:ro", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertNotIn("pull", [c["args"][0] for c in fake.calls()])

    def test_docker_initializes_ownership_without_U(self) -> None:
        fake = FakeEngine(self.root, backend="docker")
        fake.engine.backend = "docker"
        project = self.project()
        sandbox.provision(
            policy(backend="docker", provision=True), project, fake.engine
        )
        runs = fake.run_calls()
        chown = next(c for c in runs if "chown" in c)
        self.assertEqual(chown[chown.index("--user") + 1], "0:0")
        self.assertEqual(chown[chown.index("--cap-add") + 1], "CHOWN")
        self.assertEqual(chown[chown.index("--cap-drop") + 1], "ALL")
        for call in runs:
            self.assertFalse(any(a.endswith(":U") for a in call), call)

    def test_failed_or_stale_environment_is_never_reused(self) -> None:
        fake = FakeEngine(self.root, sync_exit=1)
        project = self.project()
        enabled = policy(provision=True)
        with self.assertRaises(sandbox.SandboxError) as ctx:
            sandbox.provision(enabled, project, fake.engine)
        self.assertIn("no reusable environment", str(ctx.exception))
        self.assertEqual(json.loads((fake.state / "volumes.json").read_text()), {})
        with self.assertRaises(sandbox.SandboxError) as ctx:
            sandbox.run(
                enabled,
                ["python"],
                project,
                timeout=10,
                forwarded={},
                engine=fake.engine,
            )
        self.assertIn("not provisioned", str(ctx.exception))
        fake.configure(sync_exit=0)
        sandbox.provision(enabled, project, fake.engine)
        # a changed lockfile changes the key: the old environment is not used
        (project / "uv.lock").write_text(LOCK_OK + "\n# changed\n", encoding="utf-8")
        fresh = sandbox.Engine("podman", fake.engine.argv, fake.engine.env)
        with self.assertRaises(sandbox.SandboxError):
            sandbox.run(
                enabled, ["python"], project, timeout=10, forwarded={}, engine=fresh
            )

    def test_refusals_before_anything_starts(self) -> None:
        fake = FakeEngine(self.root, image_present=False)
        project = self.project()
        with self.assertRaises(sandbox.SandboxError) as ctx:
            sandbox.provision(policy(provision=True), project, fake.engine)
        self.assertIn("never pulls", str(ctx.exception))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.provision(policy(provision=False), project, fake.engine)
        (project / "uv.lock").write_text(
            'version = 1\n[[package]]\nname = "p"\nversion = "1"\nsource = { git = "x" }\n',
            encoding="utf-8",
        )
        with self.assertRaises(sandbox.SandboxError) as ctx:
            sandbox.provision(policy(provision=True), project, fake.engine)
        self.assertIn("unsupported dependencies", str(ctx.exception))
        self.assertEqual(fake.run_calls(), [])
        self.assertNotIn("pull", [c["args"][0] for c in fake.calls()])


# ---------------------------------------------------------------------------- AC10 helpers


class StatusTest(TempCase):
    def test_status_rows(self) -> None:
        self.assertEqual(sandbox.status(sandbox.DISABLED, self.work)[0][1], "PASS")
        fake = FakeEngine(self.root)
        rows = sandbox.status(policy(), self.work, fake.engine)
        self.assertEqual([r[1] for r in rows], ["PASS", "PASS", "PASS"])
        self.assertIn("rootful", rows[1][2])
        fake.configure(rootless=True)
        rows = sandbox.status(
            policy(), self.work, FakeEngine(self.root, rootless=True).engine
        )
        self.assertIn("rootless", rows[1][2])
        rows = sandbox.status(
            policy(), self.work, FakeEngine(self.root, image_present=False).engine
        )
        self.assertEqual(rows[-1][:2], ("sandbox image", "FAIL"))
        rows = sandbox.status(
            policy(), self.work, FakeEngine(self.root, info_rc=1).engine
        )
        self.assertEqual(rows[-1][:2], ("sandbox runtime", "FAIL"))
        calls = [c["args"][0] for c in FakeEngine(self.root).calls()]
        self.assertNotIn("pull", calls)

    def test_probe_with_fake_engine(self) -> None:
        fake = FakeEngine(self.root)
        rows = sandbox.probe(policy(), fake.engine)
        # the fake runs on the host, so only the wiring is checked here; the real
        # isolation properties are asserted by the real-backend cases below
        self.assertEqual(rows[0][0], "non-root user")
        self.assertEqual(len(rows), 7)


# ---------------------------------------------------------------------------- AC8, AC9, AC11


class RealBackendTest(unittest.TestCase):
    """Real engine and approved local image; selected with --backend, never skipped then."""

    @classmethod
    def setUpClass(cls) -> None:
        if REAL_BACKEND is None:
            raise unittest.SkipTest("select with --backend podman|docker")
        image = os.environ.get("TASK_FLOW_SANDBOX_IMAGE", APPROVED_IMAGE)
        cls.policy = sandbox.parse(
            {
                "backend": REAL_BACKEND,
                "image": image,
                "memory": "512m",
                "pids_limit": 256,
                "pass_env": ["TASK_FLOW_PROBE_VALUE"],
            },
            "real-backend",
        )
        cls.engine = sandbox.engine_for(cls.policy)  # raises: a failure, not a skip
        if not cls.engine.image_present(image):
            raise AssertionError(
                f"{image} is not present locally; pull it explicitly before this suite"
            )
        version = cls.engine.call("version", "--format", "{{.Client.Version}}")
        print(
            f"\n[real backend] {REAL_BACKEND} {version.stdout.decode().strip()}, "
            f"rootless={cls.engine.rootless()}, image {image}",
            file=sys.stderr,
        )

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="task-flow sandbox real ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.work = self.root / "work tree"
        self.outside = self.root / "outside"
        self.work.mkdir()
        self.outside.mkdir()
        (self.work / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        (self.work / "data.txt").write_text("project data\n", encoding="utf-8")
        self.sentinel = self.outside / "sentinel.txt"
        self.sentinel.write_text("outside secret\n", encoding="utf-8")

    def run_python(self, script: str, *args: str, timeout: float = 120, forwarded=None):
        return sandbox.run(
            self.policy,
            ["python", "-c", script, *args],
            self.work,
            timeout=timeout,
            forwarded=forwarded or {},
            engine=self.engine,
        )

    def test_isolation_properties(self) -> None:
        host = {"TASK_FLOW_PROBE_VALUE": "visible", "SENTINEL_PROVIDER_TOKEN": "s3cret"}
        forwarded = sandbox.workload_env(host, self.policy, host)
        self.engine.env.update(host)
        self.addCleanup(self.engine.env.pop, "SENTINEL_PROVIDER_TOKEN", None)
        script = r"""
import json, os, socket, sys
out = {"uid": os.getuid(), "data": open("data.txt").read()}
open("written.txt", "w").write("from the container\n")
def attempt(path):
    try:
        open(path, "a").write("x")
        return True
    except OSError:
        return False
out["etc"] = attempt("/etc/task-flow-test")
out["git"] = attempt("/work/.git")
out["outside"] = attempt(sys.argv[1])
out["outside_exists"] = os.path.exists(sys.argv[1])
out["home_listing"] = os.listdir("/home") if os.path.isdir("/home") else []
out["env"] = {k: os.environ.get(k) for k in ("TASK_FLOW_PROBE_VALUE", "SENTINEL_PROVIDER_TOKEN")}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    out["tcp"] = True
except OSError as exc:
    out["tcp"] = repr(exc)
print(json.dumps(out))
"""
        started = time.monotonic()
        result = self.run_python(script, self.sentinel.as_posix(), forwarded=forwarded)
        duration = time.monotonic() - started
        self.assertIsNone(result.error, result.output)
        self.assertEqual(result.exit_code, 0, result.output)
        seen = json.loads(result.output.strip().splitlines()[-1])
        print(
            f"[real backend] isolation probe {duration:.1f} s: {seen}", file=sys.stderr
        )
        self.assertNotEqual(seen["uid"], 0)
        self.assertEqual(seen["data"], "project data\n")
        self.assertEqual(
            (self.work / "written.txt").read_text(), "from the container\n"
        )
        self.assertFalse(seen["etc"])
        self.assertFalse(seen["git"])
        self.assertEqual((self.work / ".git").read_text(), "gitdir: /nowhere\n")
        self.assertFalse(seen["outside"])
        self.assertFalse(seen["outside_exists"])
        self.assertEqual(self.sentinel.read_text(), "outside secret\n")
        self.assertEqual(seen["env"]["TASK_FLOW_PROBE_VALUE"], "visible")
        self.assertIsNone(seen["env"]["SENTINEL_PROVIDER_TOKEN"])
        self.assertIsNot(seen["tcp"], True)

    def test_timeout_removes_the_container(self) -> None:
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "print('started', flush=True)\n"
            "time.sleep(600)\n"
        )
        result = self.run_python(script, timeout=15)
        self.assertTrue(result.timed_out)
        self.assertIn("started", result.output)
        listed = self.engine.call(
            "ps",
            "--all",
            "--filter",
            f"name={result.container}",
            "--format",
            "{{.Names}}",
        )
        self.assertEqual(listed.stdout.decode().strip(), "")

    def test_missing_program_and_engine_failure_have_no_host_fallback(self) -> None:
        result = sandbox.run(
            self.policy,
            ["no-such-tool-xyz"],
            self.work,
            timeout=60,
            forwarded={},
            engine=self.engine,
        )
        self.assertIn(
            "not available in the sandbox image", result.error or result.output
        )
        missing = sandbox.parse(
            {**self.policy.table(), "image": "example.invalid/none" + DIGEST}, "missing"
        )
        result = sandbox.run(
            missing,
            ["python", "-c", "open('ran.txt', 'w')"],
            self.work,
            timeout=60,
            forwarded={},
            engine=self.engine,
        )
        self.assertIsNotNone(result.error)
        self.assertFalse((self.work / "ran.txt").exists())

    def test_provisioning_then_offline_run(self) -> None:
        if not WITH_PROVISIONING:
            self.skipTest("select with --with-provisioning (needs network for uv lock)")
        project = self.root / "provisioned project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nversion = "0.1.0"\nrequires-python = ">=3.14"\n'
            'dependencies = ["iniconfig==2.0.0"]\n',
            encoding="utf-8",
        )
        # lock with the image's own uv (network on), so the lockfile format matches the
        # uv that provisions it; this fixture step is the test's, not task-flow's
        user = sandbox.container_user()
        locked = self.engine.call(
            "run",
            "--rm",
            *sandbox.base_run_args(self.policy, user, keep_id=self.engine.keep_id()),
            "--workdir",
            "/work",
            "--volume",
            f"{sandbox.host_mount_path(project)}:/work",
            "--env",
            "HOME=/tmp",
            "--env",
            "UV_CACHE_DIR=/tmp/uv-cache",
            self.policy.image,
            "uv",
            "lock",
            timeout=600,
        )
        self.assertEqual(locked.returncode, 0, locked.stderr.decode(errors="replace"))
        enabled = sandbox.parse({**self.policy.table(), "provision": True}, "provision")
        key = sandbox.dependency_key(enabled, (project / "uv.lock").read_bytes())
        self.addCleanup(sandbox.remove_volumes, self.engine, key)
        print(
            "[real backend] " + sandbox.provision(enabled, project, self.engine),
            file=sys.stderr,
        )
        result = sandbox.run(
            enabled,
            ["python", "-c", "import iniconfig, socket; print(iniconfig.__name__)"],
            project,
            timeout=120,
            forwarded={},
            engine=self.engine,
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("iniconfig", result.output)


def main() -> None:
    global REAL_BACKEND, WITH_PROVISIONING
    argv = list(sys.argv)
    if "--backend" in argv:
        index = argv.index("--backend")
        REAL_BACKEND = argv[index + 1]
        if REAL_BACKEND not in ("podman", "docker"):
            raise SystemExit("--backend must be podman or docker")
        del argv[index : index + 2]
    if "--with-provisioning" in argv:
        WITH_PROVISIONING = True
        argv.remove("--with-provisioning")
    unittest.main(argv=argv)


if __name__ == "__main__":
    main()

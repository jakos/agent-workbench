# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Run model-influenced commands in a Podman or Docker container (opt-in per repository).

A git worktree separates files, not processes. With a `[sandbox]` table in the
repository's `.claude/task-flow.models.toml`, the commands that model-written code can
influence run in a container instead of on the host: the API-loop implementer's
`run_command` and `post_edit` commands (scripts/implement.py) and the script checker's
verification commands (scripts/gates.py). Model calls, credentials, git and the
runner's file tools stay on the host. Without the table, or with `backend = "none"`, nothing changes.

[sandbox] keys (only in the project file; the user file may not set them):

  backend      "none" (default) | "podman" | "docker"; "srt" and "wsl" are refused
               until a later task implements them
  image        required when enabled: a digest-pinned reference, name[:tag]@sha256:<64 hex>;
               it must already be local: task-flow never pulls (`--pull never`)
  memory       memory limit, "<n>k|m|g" (default "1g"); swap is limited to the same value
  pids_limit   process limit, integer >= 1 (default 512)
  tmp_size     size of the private /tmp tmpfs, "<n>k|m|g" (default "1g")
  pass_env     extra host environment-variable NAMES passed to the workload (default
               []); secret-looking names, proxy variables, container-runtime connection
               variables, publication credentials and the names the sandbox sets itself
               are refused
  provision    false (default): stdlib-only workloads, no dependency volume; true:
               commands get the read-only dependency environment that
               `sandbox.py provision` built from the worktree's uv.lock

Captured policy: the first runner that reads an enabled policy for a task writes it to
`.work/<id>/sandbox-policy.json` (outside the worktree and masked inside the
container), and every later command of the task uses that capture, so a model that
edits the configuration cannot turn isolation off. Delete the file to apply a changed
configuration to a running task.

A workload container (`run`): `--rm --pull never --network none --read-only` with a
private `/tmp` tmpfs, `--cap-drop ALL`, `--security-opt no-new-privileges`, memory, swap
and PID limits, a numeric non-root user (the host uid:gid on Linux and macOS, 10001:10001
on Windows; rootless Podman on Linux adds `--userns keep-id`), only the execution
worktree mounted read-write at /work, its `.git` re-mounted read-only, a repository
`.work` folder masked, and the dependency volume (when provisioned) read-only at /venv.
No home directory, git directory, socket or credential is mounted. Podman gets
`--http-proxy=false`; Docker gets empty proxy variables, so neither forwards host proxy
settings. Arguments are passed as a list, never through a shell; the program is
resolved inside the image. Windows host paths are passed in Windows form (never MSYS
form); UNC paths, relative paths and a colon in a POSIX path are refused, as are host
executables (a Windows drive or backslash path, `.exe`/`.cmd`/`.bat`/`.com`/`.ps1`) and
git, which runs on the host only.

Environment: the workload gets a fixed container environment (PATH, HOME=/tmp,
TMPDIR=/tmp, a private writable uv cache, UV_OFFLINE=1, UV_PYTHON_DOWNLOADS=never,
PYTHONDONTWRITEBYTECODE=1, and the /venv settings when provisioned) plus, by name, the
caller's allowlisted LANG, LC_*, TERM, CI and portable PYTHON* values and the declared
pass_env values. The values travel in the engine process's environment, never on its
command line; the engine process itself gets only the host settings it needs to reach
the runtime (PATH, home and temp folders, DOCKER_*/CONTAINER_HOST-style connection
variables).

Timeouts and interruption: the container has a unique name `task-flow-sbx-<hex>`; on a
timeout or an interrupt the runner runs `<engine> rm --force <name>` (which stops every
process in the container), kills the local engine client and keeps the output
collected so far. Only that named container is removed.

Failures never fall back to the host: a missing engine, a missing image, an
unprovisioned environment or an engine error (exit 125) is a SandboxError or a result
`error`; exit 126/127 with the runtime's "executable not found" message is reported as
a program missing from the image.

Provisioning (`provision`, explicit, network on): the project is mounted read-only, only
the dependency and cache volumes are writable, no host variable is passed, and the
fixed argv `uv sync --locked --no-install-project --no-install-workspace --no-build`
runs with UV_PROJECT_ENVIRONMENT=/venv. Lockfiles with git, path, direct URL,
credential-bearing or sdist-only packages are refused before anything starts. The
volumes are named from a key over the lockfile, the image and the provisioning argv;
a failed run removes them, and only a completed run writes the ready marker that
execution checks. Podman uses `:U` on those task-owned volumes only (never on the
project bind mount); Docker initializes their ownership with a separate root `chown`
container that has only CAP_CHOWN.

CLI:
  sandbox.py show      [--project <dir>]   the configured policy as JSON
  sandbox.py status    [--project <dir>]   engine, runtime health, rootless, image,
                                            provisioning; no pull, no network
  sandbox.py provision [--project <dir>]   build the dependency volume (network on)
  sandbox.py probe     [--project <dir>]   a disposable isolation probe (no pull)
Exit 0 on success, 1 when a status/probe check fails or provisioning fails, 2 for
invalid configuration or usage.

Stdlib only, started by `uv run --script` or imported as a sibling module, so it runs on
macOS, Linux and native Windows (no Git Bash).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import publish_credentials

BACKENDS = ("none", "podman", "docker")
REFUSED_BACKENDS = {
    "srt": "the srt backend is not implemented yet; use podman or docker",
    "wsl": "the wsl backend is not implemented yet; use podman or docker",
}
KEYS = {"backend", "image", "memory", "pids_limit", "tmp_size", "pass_env", "provision"}
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SIZE = re.compile(r"^[1-9][0-9]*[kmg]$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CAPTURE_NAME = "sandbox-policy.json"
CAPTURE_SCHEMA = "task-flow-sandbox/1"
WORK = "/work"
VENV = "/venv"
CACHE = "/cache"
WINDOWS_UID = "10001:10001"
LABEL = "task-flow.sandbox"
NAME_PREFIX = "task-flow-sbx-"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
ENGINE_CALL_TIMEOUT = 120
PROVISION_VERSION = "task-flow-provision/1"
PROVISION_ARGV = (
    "uv",
    "sync",
    "--locked",
    "--no-install-project",
    "--no-install-workspace",
    "--no-build",
)
READY_MARKER = f"{VENV}/.task-flow-ready"
SECRET_TOKENS = {
    "KEY",
    "KEYS",
    "TOKEN",
    "TOKENS",
    "SECRET",
    "SECRETS",
    "PASSWORD",
    "PASSWD",
    "PASS",
    "CREDENTIAL",
    "CREDENTIALS",
    "AUTH",
    "COOKIE",
    "SESSION",
}
PROXY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "FTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "ftp_proxy",
    "all_proxy",
    "no_proxy",
)
RUNTIME_PREFIXES = ("DOCKER_", "CONTAINER", "PODMAN_", "BUILDAH_", "SSH_", "XDG_")
RUNTIME_NAMES = {"DBUS_SESSION_BUS_ADDRESS", "KUBECONFIG"}
# Names the sandbox sets itself, or that only make sense on the host.
HOST_ONLY = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "VIRTUAL_ENV",
    "CODEX_HOME",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONEXECUTABLE",
    "PYTHONDONTWRITEBYTECODE",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
}
HOST_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
# Host settings the engine client itself may need to reach its runtime.
ENGINE_ENV_EXACT = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "LANG",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
}
ENGINE_ENV_PREFIXES = ("DOCKER_", "CONTAINER_", "CONTAINERS_")
NOT_FOUND = re.compile(
    r"executable file .{0,80}not found|OCI runtime .{0,200}(not found|no such file)"
    r"|exec: .{0,80}no such file or directory",
    re.IGNORECASE | re.DOTALL,
)


class ConfigError(Exception):
    """An invalid [sandbox] table or captured policy; the message names the source."""


class SandboxError(Exception):
    """The sandbox cannot run the command; never retried on the host."""


@dataclass(frozen=True)
class Policy:
    backend: str = "none"
    image: str | None = None
    memory: str = "1g"
    pids_limit: int = 512
    tmp_size: str = "1g"
    pass_env: tuple[str, ...] = ()
    provision: bool = False
    source: str = ""

    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    def table(self) -> dict:
        """The policy as a [sandbox]-shaped table (no source)."""
        table = {
            "backend": self.backend,
            "memory": self.memory,
            "pids_limit": self.pids_limit,
            "tmp_size": self.tmp_size,
            "pass_env": list(self.pass_env),
            "provision": self.provision,
        }
        if self.image is not None:
            table["image"] = self.image
        return table

    def describe(self) -> str:
        if not self.enabled:
            return "none (commands run on the host)"
        return (
            f"{self.backend}, image {self.image}, network none, memory {self.memory}, "
            f"pids {self.pids_limit}"
            + (", provisioned dependencies" if self.provision else "")
        )


DISABLED = Policy()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def unsafe_env_name(name: str) -> str | None:
    """Why `name` may not be passed into a workload, or None."""
    if not isinstance(name, str) or not ENV_NAME.match(name):
        return f"{name!r} is not an environment-variable name"
    upper = name.upper()
    if upper in {p.upper() for p in PROXY_NAMES}:
        return f"{name} is a proxy variable; workloads have no network"
    if publish_credentials.reserved(name):
        return f"{name} is a publication credential"
    if any(token in SECRET_TOKENS for token in upper.split("_")):
        return f"{name} looks like a secret (key, token, password or credential)"
    if upper.startswith(RUNTIME_PREFIXES) or upper in RUNTIME_NAMES:
        return f"{name} is a container-runtime or host connection setting"
    if upper in HOST_ONLY or upper.startswith("UV_"):
        return f"{name} is set by the sandbox itself or only valid on the host"
    return None


def parse(table: object, where: str) -> Policy:
    """Validate one [sandbox] table; raise ConfigError naming `where` and the key."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: sandbox: must be a table")
    for key in table:
        if key not in KEYS:
            raise ConfigError(
                f"{where}: sandbox.{key}: unknown key (accepted: {sorted(KEYS)})"
            )
    backend = table.get("backend", "none")
    if backend in REFUSED_BACKENDS:
        raise ConfigError(f"{where}: sandbox.backend: {REFUSED_BACKENDS[backend]}")
    if backend not in BACKENDS:
        raise ConfigError(
            f"{where}: sandbox.backend: must be one of {list(BACKENDS)}, got {backend!r}"
        )
    image = table.get("image")
    if image is not None and (not isinstance(image, str) or not IMAGE.match(image)):
        raise ConfigError(
            f"{where}: sandbox.image: must be pinned by digest "
            f"(name[:tag]@sha256:<64 hex>), got {image!r}"
        )
    if backend != "none" and image is None:
        raise ConfigError(
            f"{where}: sandbox.image: required when backend = {backend!r} "
            "(a digest-pinned image that is already local)"
        )
    for key in ("memory", "tmp_size"):
        value = table.get(key, getattr(DISABLED, key))
        if not isinstance(value, str) or not SIZE.match(value):
            raise ConfigError(
                f'{where}: sandbox.{key}: must be a positive size like "512m" or '
                f'"2g", got {value!r}'
            )
    pids = table.get("pids_limit", DISABLED.pids_limit)
    if not _is_int(pids) or pids < 1:
        raise ConfigError(
            f"{where}: sandbox.pids_limit: must be an integer >= 1, got {pids!r}"
        )
    pass_env = table.get("pass_env", [])
    if not isinstance(pass_env, list):
        raise ConfigError(f"{where}: sandbox.pass_env: must be a list of names")
    for name in pass_env:
        problem = unsafe_env_name(name)
        if problem is not None:
            raise ConfigError(f"{where}: sandbox.pass_env: {problem}")
    provision = table.get("provision", False)
    if not isinstance(provision, bool):
        raise ConfigError(
            f"{where}: sandbox.provision: must be true or false, got {provision!r}"
        )
    return Policy(
        backend=backend,
        image=image,
        memory=table.get("memory", DISABLED.memory),
        pids_limit=pids,
        tmp_size=table.get("tmp_size", DISABLED.tmp_size),
        pass_env=tuple(pass_env),
        provision=provision,
        source=where,
    )


# ---------------------------------------------------------------------------- capture


def read_capture(path: Path) -> Policy:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"{path}: cannot read the captured sandbox policy: {exc}")
    if not isinstance(data, dict) or data.get("schema") != CAPTURE_SCHEMA:
        raise ConfigError(f"{path}: not a {CAPTURE_SCHEMA} capture")
    policy = parse(data.get("sandbox"), str(path))
    if not policy.enabled:
        raise ConfigError(f"{path}: a capture always holds an enabled policy")
    return policy


def write_capture(task_dir: Path, policy: Policy) -> None:
    """Write the enabled policy into the task folder (atomic replace)."""
    if not policy.enabled:
        return
    path = Path(task_dir) / CAPTURE_NAME
    data = {
        "schema": CAPTURE_SCHEMA,
        "source": policy.source,
        "sandbox": policy.table(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def capture(task_dir: Path, policy: Policy) -> Policy:
    """The task's effective policy: an existing capture wins; an enabled policy is captured.

    A disabled configuration never removes a capture, so configuration edits made after
    the first enabled read cannot turn isolation off for the task.
    """
    path = Path(task_dir) / CAPTURE_NAME
    if path.is_file():
        return read_capture(path)
    if policy.enabled:
        write_capture(task_dir, policy)
    return policy


# ---------------------------------------------------------------------------- argv


def host_mount_path(path: Path, *, windows: bool | None = None) -> str:
    """A bind-mount source in the form the engine expects; refuses unsupported paths."""
    windows = os.name == "nt" if windows is None else windows
    text = str(path)
    if any(char in text for char in "\r\n\0"):
        raise SandboxError(f"unsupported host path for a bind mount: {text!r}")
    if windows:
        text = text.replace("/", "\\")
        if text.startswith("\\\\"):
            raise SandboxError(
                f"UNC and device paths cannot be mounted into the sandbox: {text}"
            )
        if not re.match(r"^[A-Za-z]:\\", text):
            raise SandboxError(f"not an absolute Windows drive path: {text}")
        return text
    if not text.startswith("/"):
        raise SandboxError(f"not an absolute path: {text}")
    if ":" in text:
        raise SandboxError(
            f"a colon in the path cannot be expressed in a volume mount: {text}"
        )
    return text


def join_host(base: str, name: str, *, windows: bool | None = None) -> str:
    windows = os.name == "nt" if windows is None else windows
    return base.rstrip("\\/") + ("\\" if windows else "/") + name


def is_git(program: str) -> bool:
    name = re.split(r"[\\/]", program)[-1].lower()
    return name in {"git", "git.exe"}


def program_problem(program: str) -> str | None:
    """Why `program` cannot run in the sandbox, or None (resolved inside the image)."""
    if is_git(program):
        return (
            "git runs on the host only; it is not available inside the sandbox and the "
            "runner stages changes itself"
        )
    if (
        re.match(r"^[A-Za-z]:", program)
        or "\\" in program
        or program.lower().endswith(HOST_EXECUTABLE_SUFFIXES)
    ):
        return (
            f"{program!r} is a host executable; inside the sandbox programs are "
            "resolved in the Linux image (use a program name, or a worktree-relative "
            "path with /)"
        )
    return None


def container_user(*, windows: bool | None = None, getuid=None, getgid=None) -> str:
    """The numeric non-root user: the host uid:gid on POSIX, 10001:10001 on Windows."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        return WINDOWS_UID
    uid = (getuid or os.getuid)()
    gid = (getgid or os.getgid)()
    if uid == 0:
        raise SandboxError(
            "refusing to run sandboxed commands as root: the container user is the "
            "host uid; run task-flow as a non-root user"
        )
    return f"{uid}:{gid}"


def fixed_env(policy: Policy, *, venv: bool) -> dict[str, str]:
    """The container environment the sandbox sets itself (never host values)."""
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_CACHE_DIR": "/tmp/uv-cache",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_LINK_MODE": "copy",
    }
    if venv:
        env.update(
            {
                "PATH": f"{VENV}/bin:" + env["PATH"],
                "VIRTUAL_ENV": VENV,
                "UV_PROJECT_ENVIRONMENT": VENV,
                "UV_NO_SYNC": "1",
            }
        )
    return env


def forwardable(name: str) -> bool:
    """True for an allowlisted host variable that is portable into the container."""
    upper = name.upper()
    if unsafe_env_name(name) is not None:
        return False
    return upper in {"LANG", "TERM", "CI"} or upper.startswith(("LC_", "PYTHON"))


def workload_env(
    allowlisted: dict, policy: Policy, host_env: dict | None = None
) -> dict[str, str]:
    """The host-sourced values forwarded by name: portable allowlisted ones + pass_env."""
    host_env = os.environ if host_env is None else host_env
    forwarded = {k: v for k, v in allowlisted.items() if forwardable(k)}
    for name in policy.pass_env:
        if host_env.get(name) is not None:
            forwarded[name] = host_env[name]
    return forwarded


def engine_env(host_env: dict | None = None) -> dict[str, str]:
    """The engine client's own environment: only what it needs to reach the runtime."""
    host_env = os.environ if host_env is None else host_env
    env = {}
    for name, value in host_env.items():
        upper = name.upper()
        if upper in ENGINE_ENV_EXACT or upper.startswith(ENGINE_ENV_PREFIXES):
            env[name] = value
    return publish_credentials.clean(env)


def base_run_args(policy: Policy, user: str, *, keep_id: bool) -> list[str]:
    """The confinement flags every container of this module shares."""
    args = [
        "--pull",
        "never",
        "--user",
        user,
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,exec,nosuid,nodev,size={policy.tmp_size},mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        policy.memory,
        "--memory-swap",
        policy.memory,
        "--pids-limit",
        str(policy.pids_limit),
    ]
    if keep_id:
        args += ["--userns", "keep-id"]
    if policy.backend == "podman":
        args.append("--http-proxy=false")
    else:
        for name in PROXY_NAMES:
            args += ["--env", f"{name}="]
    return args


def run_argv(
    policy: Policy,
    engine: list[str],
    *,
    name: str,
    argv: list[str],
    mount: str,
    git_mount: str | None,
    mask_work: bool,
    venv_volume: str | None,
    forwarded: list[str],
    user: str,
    keep_id: bool = False,
) -> list[str]:
    """The engine command line of one workload container (no shell anywhere)."""
    out = [
        *engine,
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        f"{LABEL}=workload",
        "--network",
        "none",
        *base_run_args(policy, user, keep_id=keep_id),
        "--workdir",
        WORK,
        "--volume",
        f"{mount}:{WORK}",
    ]
    if git_mount is not None:
        out += ["--volume", f"{git_mount}:{WORK}/.git:ro"]
    if mask_work:
        out += ["--tmpfs", f"{WORK}/.work:rw,noexec,nosuid,nodev,size=1m"]
    if venv_volume is not None:
        out += ["--volume", f"{venv_volume}:{VENV}:ro"]
    for key, value in sorted(fixed_env(policy, venv=venv_volume is not None).items()):
        out += ["--env", f"{key}={value}"]
    for key in sorted(forwarded):
        out += ["--env", key]
    out += [policy.image, *argv]
    return out


# ---------------------------------------------------------------------------- engine


@dataclass
class Engine:
    """The host-side container engine client (podman or docker)."""

    backend: str
    argv: list[str]
    env: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)

    def call(
        self, *args: str, timeout: float = ENGINE_CALL_TIMEOUT
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [*self.argv, *args],
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(
                f"{self.backend} {args[0] if args else ''} did not answer within "
                f"{timeout:g} s"
            ) from exc
        except OSError as exc:
            raise SandboxError(f"cannot start {self.backend}: {exc}") from exc

    def rootless(self) -> bool:
        if "rootless" not in self._cache:
            if self.backend == "podman":
                done = self.call("info", "--format", "{{.Host.Security.Rootless}}")
                self._cache["rootless"] = (
                    done.returncode == 0 and _text(done.stdout).strip() == "true"
                )
            else:
                done = self.call("info", "--format", "{{json .SecurityOptions}}")
                self._cache["rootless"] = done.returncode == 0 and "rootless" in _text(
                    done.stdout
                )
        return self._cache["rootless"]

    def keep_id(self) -> bool:
        """--userns keep-id: only rootless Podman on a Linux host needs it."""
        return (
            self.backend == "podman"
            and sys.platform.startswith("linux")
            and self.rootless()
        )

    def image_present(self, image: str) -> bool:
        done = self.call("image", "inspect", "--format", "{{.Id}}", image)
        return done.returncode == 0

    def volume_exists(self, volume: str) -> bool:
        return self.call("volume", "inspect", volume).returncode == 0

    def remove_container(self, name: str) -> None:
        try:
            self.call("rm", "--force", name, timeout=60)
        except SandboxError:
            pass


def _text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def engine_for(policy: Policy, host_env: dict | None = None, which=None) -> Engine:
    """The engine client for an enabled policy; SandboxError when it is not installed."""
    if not policy.enabled:
        raise SandboxError("the sandbox is disabled")
    found = (which or shutil.which)(policy.backend)
    if not found:
        raise SandboxError(
            f"{policy.backend} not found on PATH; install it, or set [sandbox] "
            'backend = "none" in .claude/task-flow.models.toml'
        )
    if found.lower().endswith((".cmd", ".bat")):
        raise SandboxError(f"{found} is a cmd shim; point PATH at the real executable")
    return Engine(policy.backend, [found], engine_env(host_env))


# ---------------------------------------------------------------------------- dependencies


def dependency_key(policy: Policy, lock_bytes: bytes) -> str:
    digest = hashlib.sha256()
    for part in (PROVISION_VERSION, policy.image or "", " ".join(PROVISION_ARGV)):
        digest.update(part.encode("utf-8") + b"\0")
    digest.update(lock_bytes)
    return digest.hexdigest()[:24]


def volume_names(key: str) -> tuple[str, str]:
    return f"task-flow-venv-{key}", f"task-flow-uvcache-{key}"


def lock_problems(text: str) -> list[str]:
    """Dependency forms provisioning refuses: they need repository code, credentials
    or a source build during the network-on phase."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"uv.lock is not valid TOML: {exc}"]
    members = set((data.get("manifest") or {}).get("members") or [])
    problems = []
    for package in data.get("package") or []:
        name = package.get("name", "?")
        source = package.get("source") or {}
        if "registry" in source:
            parts = urlsplit(str(source["registry"]))
            if "@" in parts.netloc:
                problems.append(f"{name}: the registry URL carries credentials")
            elif parts.scheme not in ("https", "http"):
                problems.append(f"{name}: unsupported registry {source['registry']!r}")
            elif not package.get("wheels"):
                problems.append(
                    f"{name}: no wheel in the lockfile, so it needs a source build"
                )
            continue
        local = source.get("editable", source.get("virtual"))
        if local is not None and (name in members or (not members and local == ".")):
            continue  # the project or a workspace member: never installed here
        kind = next(iter(source), "unknown")
        problems.append(
            f"{name}: {kind} source {source.get(kind)!r} needs repository code, "
            "credentials or a source build"
        )
    return problems


def ready(engine: Engine, policy: Policy, user: str, key: str) -> bool:
    """True when the keyed volume holds a completed environment (cached per engine)."""
    cache_key = ("ready", key)
    if engine._cache.get(cache_key):
        return True
    venv, _cache = volume_names(key)
    if not engine.volume_exists(venv):
        return False
    done = engine.call(
        "run",
        "--rm",
        "--network",
        "none",
        *base_run_args(policy, user, keep_id=engine.keep_id()),
        "--volume",
        f"{venv}:{VENV}:ro",
        policy.image,
        "cat",
        READY_MARKER,
    )
    result = done.returncode == 0 and _text(done.stdout).strip() == key
    if result:
        engine._cache[cache_key] = True
    return result


# ---------------------------------------------------------------------------- run


@dataclass
class Result:
    exit_code: int | None
    output: str
    timed_out: bool
    duration: float
    error: str | None = None
    container: str = ""


def _read_bounded(handle) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    if size <= MAX_OUTPUT_BYTES:
        data = handle.read()
    else:
        half = MAX_OUTPUT_BYTES // 2
        head = handle.read(half)
        handle.seek(size - half)
        tail = handle.read()
        data = (
            head
            + f"\n...[{size - 2 * half} bytes of output not kept]...\n".encode()
            + tail
        )
    return data.decode("utf-8", errors="replace")


def _kill_client(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        subprocess.run(
            [
                os.path.join(root, "System32", "taskkill.exe"),
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def prepare_mounts(
    policy: Policy, engine: Engine, worktree: Path, user: str
) -> tuple[str, str | None, bool, str | None]:
    """(mount, git_mount, mask_work, venv_volume) for one workload; refuses problems."""
    worktree = Path(worktree).resolve()
    if not worktree.is_dir():
        raise SandboxError(f"worktree not found: {worktree}")
    mount = host_mount_path(worktree)
    git_mount = join_host(mount, ".git") if (worktree / ".git").exists() else None
    mask_work = (worktree / ".work").exists()
    venv_volume = None
    if policy.provision:
        lock = worktree / "uv.lock"
        try:
            lock_bytes = lock.read_bytes()
        except OSError as exc:
            raise SandboxError(
                f"[sandbox] provision = true but {lock} cannot be read: {exc}"
            ) from exc
        key = dependency_key(policy, lock_bytes)
        if not ready(engine, policy, user, key):
            raise SandboxError(
                "the dependency environment for this uv.lock is not provisioned; run "
                "`uv run --script plugins/task-flow/scripts/sandbox.py provision "
                f"--project {worktree}` (network on, locked dependencies only)"
            )
        venv_volume = volume_names(key)[0]
    return mount, git_mount, mask_work, venv_volume


def run(
    policy: Policy,
    argv: list[str],
    worktree: Path,
    *,
    timeout: float,
    forwarded: dict[str, str],
    engine: Engine | None = None,
    popen=subprocess.Popen,
) -> Result:
    """Run one argv in a fresh workload container. Pre-start problems raise SandboxError."""
    if not argv:
        raise SandboxError("the command is empty")
    problem = program_problem(argv[0])
    if problem is not None:
        raise SandboxError(problem)
    engine = engine or engine_for(policy)
    user = container_user()
    mount, git_mount, mask_work, venv_volume = prepare_mounts(
        policy, engine, worktree, user
    )
    name = NAME_PREFIX + uuid.uuid4().hex[:16]
    command = run_argv(
        policy,
        engine.argv,
        name=name,
        argv=list(argv),
        mount=mount,
        git_mount=git_mount,
        mask_work=mask_work,
        venv_volume=venv_volume,
        forwarded=list(forwarded),
        user=user,
        keep_id=engine.keep_id(),
    )
    env = dict(engine.env)
    env.update(forwarded)
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as output_file:
        try:
            proc = popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except OSError as exc:
            raise SandboxError(f"cannot start {policy.backend}: {exc}") from exc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            engine.remove_container(name)
            _kill_client(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        except BaseException:
            engine.remove_container(name)
            _kill_client(proc)
            raise
        duration = time.monotonic() - started
        output = _read_bounded(output_file)
    result = Result(
        exit_code=None if timed_out else proc.returncode,
        output=output,
        timed_out=timed_out,
        duration=duration,
        container=name,
    )
    if timed_out:
        return result
    if proc.returncode == 125:
        result.error = (
            f"{policy.backend} failed to run the container (exit 125): "
            + " ".join(output.strip().splitlines()[-3:])[:400]
        )
    elif proc.returncode in (126, 127) and NOT_FOUND.search(output):
        result.error = (
            f"the program {argv[0]!r} is not available in the sandbox image "
            f"{policy.image}; use a program the image provides, or provision it"
        )
    return result


# ---------------------------------------------------------------------------- provisioning


def provision(policy: Policy, project: Path, engine: Engine | None = None) -> str:
    """Build the read-only dependency environment from uv.lock; return a status line."""
    if not policy.enabled:
        raise SandboxError("the sandbox is disabled; nothing to provision")
    if not policy.provision:
        raise SandboxError(
            "[sandbox] provision is false: workloads run stdlib-only; set provision = "
            "true to use locked dependencies"
        )
    project = Path(project).resolve()
    lock = project / "uv.lock"
    if not (project / "pyproject.toml").is_file() or not lock.is_file():
        raise SandboxError(
            f"{project} has no pyproject.toml and uv.lock; stdlib-only workloads need "
            "no provisioning"
        )
    lock_bytes = lock.read_bytes()
    problems = lock_problems(lock_bytes.decode("utf-8", errors="replace"))
    if problems:
        raise SandboxError(
            "unsupported dependencies for offline provisioning: " + "; ".join(problems)
        )
    engine = engine or engine_for(policy)
    if not engine.image_present(policy.image):
        raise SandboxError(
            f"image {policy.image} is not present locally; task-flow never pulls. "
            f"Pull it yourself: {policy.backend} pull {policy.image}"
        )
    user = container_user()
    key = dependency_key(policy, lock_bytes)
    venv, cache = volume_names(key)
    if ready(engine, policy, user, key):
        return f"already provisioned: {venv}"
    remove_volumes(engine, key)
    for volume in (venv, cache):
        done = engine.call(
            "volume",
            "create",
            "--label",
            f"{LABEL}=dependencies",
            "--label",
            f"task-flow.key={key}",
            volume,
        )
        if done.returncode != 0:
            remove_volumes(engine, key)
            raise SandboxError(
                f"cannot create volume {volume}: {_text(done.stderr).strip()[:300]}"
            )
    suffix = ""
    if policy.backend == "podman":
        suffix = ":U"  # task-owned volumes only, never the project bind mount
    else:
        done = engine.call(
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{venv}:{VENV}",
            "--volume",
            f"{cache}:{CACHE}",
            policy.image,
            "chown",
            user,
            VENV,
            CACHE,
        )
        if done.returncode != 0:
            remove_volumes(engine, key)
            raise SandboxError(
                "cannot initialize the dependency volumes: "
                + _text(done.stdout + done.stderr).strip()[-400:]
            )
    mount = host_mount_path(project)
    args = [
        "run",
        "--rm",
        "--name",
        NAME_PREFIX + uuid.uuid4().hex[:16],
        "--label",
        f"{LABEL}=provision",
        *base_run_args(policy, user, keep_id=engine.keep_id()),
        "--workdir",
        WORK,
        "--volume",
        f"{mount}:{WORK}:ro",
    ]
    if (project / ".work").exists():
        args += ["--tmpfs", f"{WORK}/.work:rw,noexec,nosuid,nodev,size=1m"]
    args += [
        "--volume",
        f"{venv}:{VENV}{suffix}",
        "--volume",
        f"{cache}:{CACHE}{suffix}",
    ]
    for name, value in sorted(provision_env().items()):
        args += ["--env", f"{name}={value}"]
    args += [policy.image, *PROVISION_ARGV]
    done = engine.call(*args, timeout=1800)
    if done.returncode != 0:
        remove_volumes(engine, key)
        raise SandboxError(
            "uv sync failed; no reusable environment was kept:\n"
            + _text(done.stdout + done.stderr).strip()[-2000:]
        )
    done = engine.call(
        "run",
        "--rm",
        "--network",
        "none",
        *base_run_args(policy, user, keep_id=engine.keep_id()),
        "--volume",
        f"{venv}:{VENV}",
        policy.image,
        "python",
        "-c",
        "import sys; open(sys.argv[1], 'w', encoding='utf-8').write(sys.argv[2])",
        READY_MARKER,
        key,
    )
    if done.returncode != 0:
        remove_volumes(engine, key)
        raise SandboxError(
            "cannot mark the environment ready: "
            + _text(done.stdout + done.stderr).strip()[-400:]
        )
    return f"provisioned: {venv}"


def provision_env() -> dict[str, str]:
    return {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "UV_CACHE_DIR": CACHE,
        "UV_PROJECT_ENVIRONMENT": VENV,
        "UV_LINK_MODE": "copy",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_PROGRESS": "1",
    }


def remove_volumes(engine: Engine, key: str) -> None:
    """Remove only the two volumes of this key (never other resources)."""
    for volume in volume_names(key):
        try:
            engine.call("volume", "rm", "--force", volume)
        except SandboxError:
            pass


# ---------------------------------------------------------------------------- diagnostics

PROBE_SCRIPT = r"""
import json, os, socket, sys
out = {"uid": getattr(os, "getuid", lambda: -1)()}
try:
    with open("probe-written.txt", "w", encoding="utf-8") as handle:
        handle.write("sandbox probe\n")
    out["work_write"] = True
except OSError as exc:
    out["work_write"] = repr(exc)
for key, path in (("etc_write", "/etc/task-flow-probe"), ("git_write", ".git")):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("x")
        out[key] = True
    except OSError:
        out[key] = False
out["outside_read"] = os.path.exists(sys.argv[1])
out["sentinel_env"] = sys.argv[2] in os.environ
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    out["tcp"] = True
except OSError:
    out["tcp"] = False
print("PROBE " + json.dumps(out, sort_keys=True))
"""
SENTINEL_ENV = "TASK_FLOW_SANDBOX_SENTINEL_TOKEN"


def probe(policy: Policy, engine: Engine | None = None) -> list[tuple[str, bool, str]]:
    """A disposable isolation probe: temporary fixture only, no pull, no model."""
    engine = engine or engine_for(policy)
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="task-flow-probe ") as tmp:
        root = Path(tmp).resolve()
        work = root / "work"
        outside = root / "outside"
        work.mkdir()
        outside.mkdir()
        (work / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        sentinel = outside / "secret.txt"
        sentinel.write_text("secret\n", encoding="utf-8")
        engine.env[SENTINEL_ENV] = "sentinel-value"
        try:
            result = run(
                Policy(**{**policy.table(), "provision": False, "pass_env": ()}),
                ["python", "-c", PROBE_SCRIPT, sentinel.as_posix(), SENTINEL_ENV],
                work,
                timeout=120,
                forwarded={},
                engine=engine,
            )
        finally:
            engine.env.pop(SENTINEL_ENV, None)
        if result.error or result.timed_out or result.exit_code != 0:
            detail = result.error or (
                "timed out" if result.timed_out else f"exit {result.exit_code}"
            )
            return [("probe container", False, f"{detail}: {result.output[-300:]}")]
        line = next(
            (x for x in result.output.splitlines() if x.startswith("PROBE ")), None
        )
        if line is None:
            return [("probe container", False, result.output[-300:])]
        seen = json.loads(line[len("PROBE ") :])
        written = work / "probe-written.txt"
        results.append(("non-root user", seen["uid"] != 0, f"uid {seen['uid']}"))
        results.append(
            (
                "worktree write, host-readable",
                seen["work_write"] is True and written.is_file(),
                str(seen["work_write"]),
            )
        )
        results.append(("root filesystem read-only", not seen["etc_write"], ""))
        results.append(("worktree .git read-only", not seen["git_write"], ""))
        results.append(
            ("host file outside the worktree absent", not seen["outside_read"], "")
        )
        results.append(("host secret variable absent", not seen["sentinel_env"], ""))
        results.append(("TCP connection refused", not seen["tcp"], ""))
    return results


def status(
    policy: Policy, project: Path, engine: Engine | None = None
) -> list[tuple[str, str, str]]:
    """(check, PASS|WARN|FAIL, detail) rows; no pull, no provisioning, no network."""
    if not policy.enabled:
        return [("sandbox", "PASS", "disabled: commands run on the host")]
    rows = [("sandbox", "PASS", policy.describe())]
    try:
        engine = engine or engine_for(policy)
    except SandboxError as exc:
        return rows + [("sandbox runtime", "FAIL", str(exc))]
    try:
        version = engine.call("version", "--format", "{{.Client.Version}}")
        info = engine.call("info", "--format", "{{json .}}")
    except SandboxError as exc:
        return rows + [("sandbox runtime", "FAIL", str(exc))]
    if info.returncode != 0:
        detail = " ".join(_text(info.stderr).split())[:300]
        return rows + [
            (
                "sandbox runtime",
                "FAIL",
                f"{policy.backend} cannot reach its runtime: {detail}",
            )
        ]
    rootless = engine.rootless()
    mode = "rootless" if rootless else "rootful (container root is engine root)"
    rows.append(
        (
            "sandbox runtime",
            "PASS",
            f"{policy.backend} {_text(version.stdout).strip() or '?'}, {mode}",
        )
    )
    if engine.image_present(policy.image):
        rows.append(("sandbox image", "PASS", f"{policy.image} is local"))
    else:
        rows.append(
            (
                "sandbox image",
                "FAIL",
                (
                    f"{policy.image} is not local; task-flow never pulls — run "
                    f"`{policy.backend} pull {policy.image}` yourself"
                ),
            )
        )
        return rows
    if policy.provision:
        lock = Path(project) / "uv.lock"
        if not lock.is_file():
            rows.append(
                (
                    "sandbox dependencies",
                    "FAIL",
                    f"provision = true but {lock} is missing",
                )
            )
        else:
            key = dependency_key(policy, lock.read_bytes())
            user = container_user()
            if ready(engine, policy, user, key):
                rows.append(
                    ("sandbox dependencies", "PASS", f"{volume_names(key)[0]} ready")
                )
            else:
                rows.append(
                    (
                        "sandbox dependencies",
                        "WARN",
                        "not provisioned for this uv.lock; run sandbox.py provision",
                    )
                )
    return rows


def project_policy(project: Path) -> Policy:
    import models

    try:
        return models.sandbox_policy(project)
    except models.ConfigError as exc:
        raise ConfigError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("show", "print the configured policy as JSON"),
        ("status", "engine, runtime, image and provisioning; no pull, no network"),
        ("provision", "build the dependency volume from uv.lock (network on)"),
        ("probe", "run a disposable isolation probe (no pull)"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--project", default=".", help="repository root (default: .)"
        )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    project = Path(args.project).resolve()
    try:
        policy = project_policy(project)
    except ConfigError as exc:
        print(f"sandbox.py: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "show":
            print(
                json.dumps(
                    {"enabled": policy.enabled, **policy.table()}, sort_keys=True
                )
            )
            return 0
        if args.command == "status":
            rows = status(policy, project)
            for check, result, detail in rows:
                print(f"{result} {check}: {detail}")
            return 1 if any(r == "FAIL" for _c, r, _d in rows) else 0
        if not policy.enabled:
            print("sandbox.py: the sandbox is disabled", file=sys.stderr)
            return 2
        if args.command == "provision":
            print(provision(policy, project))
            return 0
        rows = probe(policy)
        for check, ok, detail in rows:
            print(
                f"{'PASS' if ok else 'FAIL'} {check}"
                + (f": {detail}" if detail else "")
            )
        return 0 if all(ok for _c, ok, _d in rows) else 1
    except SandboxError as exc:
        print(f"sandbox.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

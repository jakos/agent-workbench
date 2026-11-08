# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Prepare local publication evidence; publish only from a separate trusted launcher.

No operation commits, merges, rebases, deletes branches or provisions GitHub settings.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import epics
import phase_gate
import policy
import publish_credentials
import risk
import task_record


class Refusal(ValueError):
    """Fail closed without exposing subprocess output or authentication material."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def save(task: Path, name: str, value: dict) -> None:
    path = task / name
    if path.is_symlink():
        raise Refusal("unsafe publication evidence path")
    phase_gate.publish(
        path, "# Publication evidence\n\n```json\n" + encode(value) + "\n```\n"
    )


def evidence(task: Path, name: str) -> dict:
    text = phase_gate.read(task / name)
    match = re.fullmatch(r"# Publication evidence\n\n```json\n([^\n]+)\n```\n", text)
    if not match:
        raise Refusal("invalid publication evidence")
    try:
        value = json.loads(match[1], object_pairs_hook=phase_gate.unique_object)
    except ValueError:
        raise Refusal("invalid publication evidence") from None
    if not isinstance(value, dict):
        raise Refusal("invalid publication evidence")
    return value


def run(
    argv: list[str], cwd: Path, env: dict, *, data: str | None = None, ok: tuple = (0,)
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise Refusal(
            "publication command unavailable or timed out; safe to resume"
        ) from None
    if result.returncode not in ok:
        raise Refusal(
            "publication command failed; check credentials and remote state, then resume"
        )
    return result.stdout


def local_env(env: dict) -> dict:
    # No inherited Git configuration, askpass, tracing or GitHub authentication.
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
    }
    result = {k: v for k, v in env.items() if k.upper() in allowed}
    result.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,
        GCM_INTERACTIVE="never",
        GH_PROMPT_DISABLED="1",
    )
    return result


def git(worktree: Path, *args: str, env: dict | None = None) -> str:
    output = run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "credential.helper=",
            "-c",
            "http.extraHeader=",
            "-c",
            "http.https://github.com/.extraHeader=",
            "-c",
            "http.https://github.com/.sslVerify=true",
            *(
                ["-c", "credential.https://github.com.helper=!gh auth git-credential"]
                if env and env.get("GH_TOKEN")
                else []
            ),
            "-C",
            str(worktree),
            *args,
        ],
        worktree,
        env if env is not None else local_env(os.environ),
    )
    return output if "\0" in output else output.strip()


def context(task: Path, home: Path) -> tuple[Path, dict, dict]:
    task = task.resolve()
    if task.parent.name != ".work" or not task.is_dir():
        raise Refusal("expected an ordinary task directory")
    _, state = phase_gate.status(task)
    project = task.parent.parent
    loaded = policy.effective(policy.load(project, home))
    if not loaded["publish"]["enabled"]:
        raise Refusal("publication requires explicit project opt-in")
    worktree = project if state["worktree"] == "none" else project / state["worktree"]
    if worktree.resolve() != worktree or not worktree.is_dir():
        raise Refusal("unsafe task worktree")
    if (
        git(worktree, "rev-parse", "--show-toplevel").replace("\\", "/").casefold()
        != str(worktree).replace("\\", "/").casefold()
    ):
        raise Refusal("worktree identity mismatch")
    expected_common = Path(
        git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    actual_common = Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    if actual_common != expected_common:
        raise Refusal("task worktree belongs to a different repository")
    if (
        state["branch"] != "task/" + task.name
        or git(worktree, "branch", "--show-current") != state["branch"]
    ):
        raise Refusal("publication requires the exact task branch")
    return worktree, state, loaded


def binding(task: Path, state: dict, *, checked: bool = True) -> dict:
    text = phase_gate.read(task / "status.md")
    number = state["round"]
    names = ["plan.md", "task.md", "checks-0.md"]
    if checked:
        names.append(f"checks-{number}.md")
    return {
        "task": task.name,
        "attempt": phase_gate.attempt(text, state, task),
        "round": number,
        "inputs": {name: sha((task / name).read_bytes()) for name in names},
    }


def source_tree(worktree: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="task-flow-index-") as tmp:
        env = local_env(os.environ)
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        git(worktree, "read-tree", "HEAD", env=env)
        git(worktree, "add", "-A", "--", env=env)
        return git(worktree, "write-tree", env=env)


def finish_files(task: Path, worktree: Path) -> dict:
    """Compute exact status-derived finish bytes before review, using source copies."""
    import sdlc_status

    paths = [
        sdlc_status.SOURCE_RELPATH,
        sdlc_status.README_RELPATH,
        sdlc_status.PAGE_RELPATH,
    ]
    if not (worktree / paths[0]).exists():
        return {}
    with tempfile.TemporaryDirectory(prefix="task-flow-finish-") as tmp:
        dest = Path(tmp)
        # SDLC validation requires evidence paths to exist. Copy regular tracked source
        # files only; no executable code from the checkout is loaded or run.
        names = git(
            worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        ).split("\0")
        for name in filter(None, names):
            source = worktree / name
            if source.is_symlink() or not source.is_file():
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sdlc_status.cmd_finish(dest, task / "task.md")
        except (sdlc_status.StatusError, sdlc_status.MarkerError):
            raise Refusal("cannot derive deterministic SDLC finish files") from None
        return {
            str(path).replace("\\", "/"): sha((dest / path).read_bytes())
            for path in paths
            if (dest / path).is_file()
        }


def capture_check(task: Path, *, home: Path | None = None) -> None:
    worktree, state, _ = context(task, home or Path.home())
    value = {**binding(task, state, checked=False), "tree": source_tree(worktree)}
    name = f"publication-check-{state['round']}.md"
    if (task / name).exists():
        if evidence(task, name) != value:
            raise Refusal("checked source or inputs changed; use a new checked round")
    else:
        save(task, name, value)


def capture_review(task: Path, *, home: Path | None = None) -> None:
    worktree, state, _ = context(task, home or Path.home())
    tree = source_tree(worktree)
    checked = {**binding(task, state, checked=False), "tree": tree}
    if evidence(task, f"publication-check-{state['round']}.md") != checked:
        raise Refusal("checked source changed before review")
    value = binding(task, state)
    value.update(
        tree=tree,
        base=git(worktree, "rev-parse", state["base"] + "^{commit}"),
        base_ref=state["base"],
        finish=finish_files(task, worktree),
    )
    path = task / f"publication-review-{state['round']}.md"
    if path.exists():
        if evidence(task, path.name) != value:
            raise Refusal("review source changed; a new checked review is required")
    else:
        save(task, path.name, value)


def approval_binding(task: Path, state: dict, snapshot: dict) -> dict:
    return {
        "snapshot": sha(encode(snapshot).encode()),
        "review": sha((task / f"review-{state['round']}.md").read_bytes()),
        **binding(task, state),
    }


def seal_review(task: Path, *, home: Path | None = None) -> None:
    worktree, state, _ = context(task, home or Path.home())
    snapshot = evidence(task, f"publication-review-{state['round']}.md")
    if any(snapshot.get(key) != value for key, value in binding(task, state).items()):
        raise Refusal("review inputs changed during review")
    name = f"publication-approval-{state['round']}.md"
    value = approval_binding(task, state, snapshot)
    if (task / name).exists():
        if evidence(task, name) != value:
            raise Refusal("approved review evidence changed")
        return
    if source_tree(worktree) != snapshot["tree"]:
        raise Refusal("source changed during review")
    save(task, name, value)


def validate_review(task: Path, state: dict, loaded: dict) -> dict:
    if (
        state["phase"] not in ("review-approved", "done")
        or state["last-verdict"] != "APPROVED"
    ):
        raise Refusal("publication requires current approved review")
    plan = phase_gate.read(task / "plan.md")
    validated = phase_gate.role_contracts.validate_plan(plan)
    if not validated.ok or validated.blocked:
        raise Refusal("invalid publication plan")
    if (
        phase_gate.review(task, state["round"], plan, validated.commands)[0]
        != "APPROVED"
    ):
        raise Refusal("publication requires passing checks and approved review")
    # Publication requires every command to pass, even a baseline failure.
    checks = phase_gate.read(task / f"checks-{state['round']}.md")
    if re.search(r"\| (?:FAIL|ERROR) \|", checks):
        raise Refusal("publication requires all checks passing")
    auth = {
        "plan": sha(plan.encode()),
        "attempt": phase_gate.attempt(phase_gate.read(task / "status.md"), state, task),
    }
    phase_gate.authorize(phase_gate.log_entries(task), auth, loaded, task)
    snapshot = evidence(task, f"publication-review-{state['round']}.md")
    current = binding(task, state)
    if any(snapshot.get(key) != value for key, value in current.items()):
        raise Refusal("review/check/attempt evidence is stale")
    if evidence(task, f"publication-approval-{state['round']}.md") != approval_binding(
        task, state, snapshot
    ):
        raise Refusal("approved review evidence is stale")
    return snapshot


def record_body(task: Path, worktree: Path, state: dict) -> tuple[str, str]:
    attention = risk.parse_attention(
        phase_gate.read(task / f"review-{state['round']}.md")
    )
    if not attention:
        raise Refusal("publication requires current review risk evidence")
    record = task_record.build_record(task, risk_enabled=True)
    expected = task_record.render_record(record)
    path = task_record.safe_destination(worktree, task.name)
    if not path.is_file() or path.read_bytes() != expected.encode("utf-8"):
        raise Refusal("missing or inconsistent deterministic task record")
    return "\n".join(
        [
            "risk: " + attention["risk"],
            "why: " + attention["why"],
            *(
                [
                    f"- {item['path']}:{item['line']} \u2014 {item['what']}"
                    for item in attention["attention"]
                ]
                or ["- none"]
            ),
            "",
            expected,
        ]
    ), attention["risk"]


def parent(task: Path, state: dict) -> str | None:
    base = state["base"]
    if base == "main":
        return None
    if not re.fullmatch(r"task/[a-z0-9][a-z0-9-]*", base) or base == state["branch"]:
        raise Refusal("unsupported recorded publication base")
    parent_task = task.parent / base[5:]
    _, parent_state = phase_gate.status(parent_task)
    if parent_state["branch"] != base or parent_state["phase"] != "done":
        raise Refusal("parent task is not locally complete")
    return base[5:]


def prepare(task: Path, home: Path) -> dict:
    worktree, state, loaded = context(task, home)
    if not task_record.load_records_config(worktree / task_record.CONFIG_RELPATH):
        raise Refusal("publication requires task records enabled")
    snapshot = validate_review(task, state, loaded)
    if git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise Refusal("publication requires a clean index and worktree")
    head = git(worktree, "rev-parse", "HEAD")
    # The local finish commit must have the recorded base as its sole parent.
    if git(worktree, "show", "-s", "--format=%P", head) != snapshot["base"]:
        raise Refusal("unexpected final commit ancestry")
    if state["base"] != snapshot["base_ref"]:
        raise Refusal("recorded base changed")
    body, rating = record_body(task, worktree, state)
    trailers = task_record.render_trailers(
        task_record.build_record(task, risk_enabled=True),
        epics.epic_for_task(task.parent.parent, task.name),
    )
    message = git(worktree, "show", "-s", "--format=%B", head)
    if any(message.splitlines().count(line) != 1 for line in trailers):
        raise Refusal("final commit lacks deterministic task trailers")
    changed = git(
        worktree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        snapshot["tree"],
        head,
        "--",
    ).split("\0")
    allowed = {
        **snapshot["finish"],
        f"docs/tasks/{task.name}.md": sha(
            task_record.render_record(
                task_record.build_record(task, risk_enabled=True)
            ).encode()
        ),
    }
    for name in filter(None, changed):
        path = worktree / name
        if (
            name not in allowed
            or path.is_symlink()
            or not path.is_file()
            or sha(path.read_bytes()) != allowed[name]
        ):
            raise Refusal("unexpected post-review source edit")
        if git(worktree, "ls-tree", head, "--", name).split()[0] != "100644":
            raise Refusal("unexpected finish file mode")
    value = {
        "schema": "publication/1",
        **binding(task, state),
        "review": sha((task / f"review-{state['round']}.md").read_bytes()),
        "snapshot": sha(encode(snapshot).encode()),
        "head": head,
        "tree": git(worktree, "rev-parse", "HEAD^{tree}"),
        "config": loaded["publish"],
        "body": body,
        "risk": rating,
        "parent": parent(task, state),
        "parent_head": snapshot["base"] if state["base"] != "main" else None,
    }
    path = task / "publication-request.md"
    if path.exists() and evidence(task, path.name) != value:
        raise Refusal("publication request changed; do not reuse old grants")
    save(task, path.name, value)
    return value


def request_digest(request: dict) -> str:
    return sha(encode(request).encode())


def permit(task: Path, action: str, request: dict, loaded: dict, home: Path) -> None:
    if policy.decide(action, loaded, task.parent.parent)["allowed"]:
        return
    path = task / ("publication-grant-" + action + ".md")
    if not path.exists() or evidence(task, path.name) != {
        "action": action,
        "request": request_digest(request),
    }:
        raise Refusal(action + " requires a separate request-bound human grant")


def grant(
    task: Path, action: str, home: Path, confirm: Callable[[str], str] = input
) -> None:
    request = prepare(task, home)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Refusal("grant requires a separate interactive human terminal")
    challenge = action + " " + task.name + " " + request_digest(request)
    if (
        confirm("Inspect publication-request.md; type exactly: " + challenge + "\n")
        != challenge
    ):
        raise Refusal("publication grant not confirmed")
    save(
        task,
        "publication-grant-" + action + ".md",
        {"action": action, "request": request_digest(request)},
    )


class Publisher:
    """Process-scoped authentication; only instantiated by the external launcher."""

    def __init__(self, task: Path, home: Path, env: dict, config_dir: Path) -> None:
        self.task, self.home = task, home
        self.request = prepare(task, home)
        self.worktree, _, self.loaded = context(task, home)
        config = self.request["config"]
        token = env.get(config["credential_env"])
        if not token or any(c in token for c in "\r\n\0"):
            raise Refusal("publication credential missing or invalid")
        self.env = local_env(env)
        self.env.update(
            GH_TOKEN=token,
            GH_CONFIG_DIR=str(config_dir),
            GH_HOST="github.com",
            HOME=str(config_dir),
            USERPROFILE=str(config_dir),
        )
        self.repo = config["repository"]
        self.url = "https://github.com/" + self.repo + ".git"
        # Empty helper resets inherited helpers; gh reads GH_TOKEN from the child env.
        self.env.update(
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="credential.https://github.com.helper",
            GIT_CONFIG_VALUE_1="!gh auth git-credential",
        )
        self.branch = "task/" + task.name

    def revalidate(self, action: str) -> None:
        current = prepare(self.task, self.home)
        if current != self.request:
            raise Refusal("publication head or evidence changed")
        _, _, self.loaded = context(self.task, self.home)
        if action in ("push", "pr"):
            permit(self.task, action, current, self.loaded, self.home)
        # Validate the named remote but always address the canonical URL directly.
        remote = current["config"]["remote"]
        urls = git(self.worktree, "remote", "get-url", "--all", remote).splitlines()
        pushes = git(
            self.worktree, "remote", "get-url", "--push", "--all", remote
        ).splitlines()
        accepted = {
            self.url,
            self.url.removesuffix(".git"),
            "git@github.com:" + self.repo + ".git",
        }
        if (
            len(urls) != 1
            or len(pushes) != 1
            or urls[0] not in accepted
            or pushes[0] not in accepted
        ):
            raise Refusal(
                "publication remote does not match configured GitHub repository"
            )
        # URL rewriting or path-specific HTTP auth could override the destination
        # or process token. Never expose values while refusing those local settings.
        result = run(
            [
                "git",
                "-C",
                str(self.worktree),
                "config",
                "--includes",
                "--name-only",
                "--get-regexp",
                r"^(url\.|http\.)",
            ],
            self.worktree,
            local_env(os.environ),
            ok=(0, 1),
        )
        if result:
            raise Refusal(
                "publication refuses repository URL rewriting or HTTP overrides"
            )

    def api(
        self, endpoint: str, payload: dict | None = None, method: str = "GET"
    ) -> object:
        argv = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        data = None
        if payload is not None:
            argv += ["--input", "-"]
            data = json.dumps(payload)
        try:
            return json.loads(run(argv, self.worktree, self.env, data=data))
        except ValueError:
            raise Refusal("invalid GitHub publication response") from None

    def find_pr(self) -> dict | None:
        owner = self.repo.split("/")[0]
        rows = self.api(
            f"repos/{self.repo}/pulls?state=all&head={owner}:{self.branch}&per_page=100"
        )
        if not isinstance(rows, list) or len(rows) > 1:
            raise Refusal("ambiguous existing pull request")
        if not rows:
            return None
        row = rows[0]
        if (
            row["head"]["repo"]["full_name"] != self.repo
            or row["base"]["repo"]["full_name"] != self.repo
            or row["head"]["ref"] != self.branch
            or row["head"]["sha"] != self.request["head"]
        ):
            raise Refusal("existing PR head or repository conflicts")
        return row

    def receipt(self, **fields: object) -> None:
        prior = (
            evidence(self.task, "publication-receipt.md")
            if (self.task / "publication-receipt.md").exists()
            else {}
        )
        if prior and prior.get("request") != request_digest(self.request):
            raise Refusal("receipt belongs to another publication request")
        save(
            self.task,
            "publication-receipt.md",
            {**prior, "request": request_digest(self.request), **fields},
        )

    def publish(self) -> None:
        self.revalidate("push")
        # Exact non-force refspec; hooks disabled; explicit destination bypasses pushurl.
        git(
            self.worktree,
            "push",
            "--no-verify",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--",
            self.url,
            self.request["head"] + ":refs/heads/" + self.branch,
            env=self.env,
        )
        self.receipt(pushed=True)
        self.revalidate("pr")
        base = "task/" + self.request["parent"] if self.request["parent"] else "main"
        row = self.find_pr()
        if row is None:
            self.revalidate("pr")
            row = self.api(
                f"repos/{self.repo}/pulls",
                {
                    "title": "Task: " + self.task.name,
                    "head": self.branch,
                    "base": base,
                    "draft": bool(self.request["parent"]),
                    "body": self.request["body"],
                },
                "POST",
            )
        if (
            row["base"]["ref"] != base
            or row["body"] != self.request["body"]
            or row["state"] != "open"
        ):
            raise Refusal("existing PR base, body or state conflicts")
        if self.request["parent"] and not row["draft"]:
            raise Refusal("child PR must remain draft against its parent")
        number = row["number"]
        if type(number) is not int or number < 1:
            raise Refusal("invalid PR number")
        self.receipt(
            pushed=True, pr=number, base=base, draft=row["draft"], delivered=False
        )
        self.revalidate("pr")
        self.api(
            f"repos/{self.repo}/issues/{number}/labels",
            {"labels": ["agent", "risk:" + self.request["risk"]]},
            "POST",
        )
        self.receipt(
            pushed=True,
            pr=number,
            base=base,
            draft=row["draft"],
            labeled=True,
            delivered=False,
        )

    def fetched_main(self) -> str:
        self.revalidate("read")
        git(
            self.worktree,
            "fetch",
            "--no-tags",
            "--recurse-submodules=no",
            "--",
            self.url,
            "refs/heads/main",
            env=self.env,
        )
        return git(self.worktree, "rev-parse", "FETCH_HEAD")

    def contains(self, commit: str, main: str) -> bool:
        return not git(
            self.worktree, "rev-list", "--max-count=1", commit, "--not", main
        )

    def delivery(self) -> bool:
        delivered = self.contains(self.request["head"], self.fetched_main())
        self.receipt(delivered=delivered, head=self.request["head"])
        return delivered

    def advance(self) -> None:
        if not self.request["parent"]:
            raise Refusal("only a recorded child PR can advance")
        parent_task = self.task.parent / self.request["parent"]
        parent_request = evidence(parent_task, "publication-request.md")
        if (
            parent_request["task"] != parent_task.name
            or parent_request["head"] != self.request["parent_head"]
            or not self.contains(parent_request["head"], self.fetched_main())
        ):
            raise Refusal("parent task commit is not delivered to main")
        row = self.find_pr()
        if not row or row["state"] != "open":
            raise Refusal("open child PR required")
        number = row["number"]
        if row["base"]["ref"] == "task/" + self.request["parent"]:
            if not row["draft"]:
                raise Refusal("child must remain draft before retargeting")
            self.revalidate("pr")
            self.api(f"repos/{self.repo}/pulls/{number}", {"base": "main"}, "PATCH")
            self.receipt(pr=number, base="main", draft=True, delivered=False)
            return  # New base checks must run in a later invocation.
        if row["base"]["ref"] != "main":
            raise Refusal("child PR has an unexpected base")
        # GitHub's test merge commit binds checks to the current head AND base.
        main = git(self.worktree, "rev-parse", "FETCH_HEAD")
        merge = row.get("merge_commit_sha")
        if not merge or row["base"]["sha"] != main:
            raise Refusal("retargeted PR merge checks are not current")
        self.revalidate("pr")
        git(
            self.worktree,
            "fetch",
            "--no-tags",
            "--recurse-submodules=no",
            "--",
            self.url,
            f"refs/pull/{number}/merge",
            env=self.env,
        )
        if (
            git(self.worktree, "rev-parse", "FETCH_HEAD") != merge
            or git(self.worktree, "show", "-s", "--format=%P", merge)
            != main + " " + self.request["head"]
        ):
            raise Refusal("PR test merge does not bind current head and main")
        protection = self.api(
            f"repos/{self.repo}/branches/main/protection/required_status_checks"
        )
        required = set(protection.get("contexts", [])) | {
            item["context"] for item in protection.get("checks", [])
        }
        checks = self.api(f"repos/{self.repo}/commits/{merge}/check-runs?per_page=100")
        statuses = self.api(f"repos/{self.repo}/commits/{merge}/status?per_page=100")
        # Reject truncated evidence and use the newest result for a repeated context.
        runs = checks.get("check_runs", [])
        states = statuses.get("statuses", [])
        if (
            checks.get("total_count", len(runs)) > len(runs)
            or statuses.get("total_count", len(states)) > len(states)
            or len(states) >= 100
        ):
            raise Refusal("required check evidence is incomplete")
        latest_runs = {}
        for item in sorted(runs, key=lambda item: item.get("id", 0)):
            latest_runs[item["name"]] = item
        latest_states = {}
        for item in sorted(states, key=lambda item: item.get("id", 0)):
            latest_states[item["context"]] = item
        passed = set()
        for name in required:
            check = latest_runs.get(name)
            status = latest_states.get(name)
            if (
                (check or status)
                and (
                    not check
                    or check["status"] == "completed"
                    and check["conclusion"] == "success"
                )
                and (not status or status["state"] == "success")
            ):
                passed.add(name)
        for check in protection.get("checks", []):
            item = latest_runs.get(check["context"], {})
            if (
                check.get("app_id") is not None
                and item.get("app", {}).get("id") != check["app_id"]
            ):
                raise Refusal("required check application identity conflicts")
        if not required or not required <= passed:
            raise Refusal("required checks for the retargeted PR have not passed")
        latest = self.find_pr()
        if latest["base"]["sha"] != main or latest.get("merge_commit_sha") != merge:
            raise Refusal("PR base or merge changed during checks")
        if latest["draft"]:
            self.revalidate("pr")
            self.api(
                "graphql",
                {
                    "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id}}}",
                    "variables": {"id": latest["node_id"]},
                },
                "POST",
            )
        self.receipt(pr=number, base="main", draft=False, delivered=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "grant-push", "grant-pr", "publish", "advance", "delivery"),
    )
    parser.add_argument("--task-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        task = args.task_dir.resolve()
        if args.command == "prepare":
            publish_credentials.runner(os.environ)
            request = prepare(task, Path.home())
            print("publication request: " + request_digest(request))
        elif args.command.startswith("grant-"):
            publish_credentials.runner(os.environ)
            grant(task, args.command[6:], Path.home())
            print("publication grant recorded")
        else:
            with tempfile.TemporaryDirectory(prefix="task-flow-gh-") as tmp:
                publisher = Publisher(task, Path.home(), dict(os.environ), Path(tmp))
                if args.command == "delivery":
                    print(
                        "delivered to main"
                        if publisher.delivery()
                        else "undelivered to main"
                    )
                else:
                    getattr(publisher, args.command)()
                    print(
                        "publication operation completed; inspect publication-receipt.md"
                    )
        return 0
    except (
        Refusal,
        phase_gate.Refusal,
        policy.ConfigError,
        publish_credentials.CredentialError,
        task_record.RecordError,
        epics.EpicError,
        risk.RiskError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):
        # Never print arbitrary CLI/server/environment-derived strings.
        print(
            "publication refused; inspect local evidence, configuration and authorization before retrying",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())

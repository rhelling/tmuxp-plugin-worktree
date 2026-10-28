"""End-to-end tests for tmuxp-worktree.

They use real git worktrees, the real tmuxp CLI and a private tmux server (its
own socket per test), so your own tmux sessions are never touched. No dev
servers are started: the panes only run short shell commands.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TMUXP = shutil.which("tmuxp", path=os.path.dirname(sys.executable)) or shutil.which("tmuxp")

pytestmark = pytest.mark.skipif(
    not (TMUXP and shutil.which("tmux") and shutil.which("git")),
    reason="needs tmuxp, tmux and git",
)


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def git(cwd, *args):
    result = run(["git", "-C", cwd, "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args])
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """myrepo with four usable worktrees (one nested, one detached, one with a dot in
    its name) plus a prunable one, a repo with a single worktree and a non-git dir."""
    base = tmp_path_factory.mktemp("projects")
    main = base / "myrepo"
    for sub in ("src/api", "src/web"):
        (main / sub).mkdir(parents=True)
        (main / sub / ".keep").touch()
    git(str(main), "init", "-q", "-b", "main")
    git(str(main), "add", "-A")
    git(str(main), "commit", "-q", "-m", "init")
    git(str(main), "worktree", "add", "-q", "-b", "feature/x", "../myrepo-feature-x")
    git(str(main), "worktree", "add", "-q", "-b", "agent", ".claude/worktrees/agent")
    git(str(main), "worktree", "add", "-q", "--detach", "../myrepo-detached")
    git(str(main), "worktree", "add", "-q", "-b", "release/v1.2", "../myrepo-v1.2")
    git(str(main), "worktree", "add", "-q", "-b", "gone", "../myrepo-gone")
    shutil.rmtree(base / "myrepo-gone")  # becomes "prunable"

    single = base / "single"
    (single / "src").mkdir(parents=True)
    (single / "src" / ".keep").touch()
    git(str(single), "init", "-q")
    git(str(single), "add", "-A")
    git(str(single), "commit", "-q", "-m", "init")

    (base / "nogit" / "src").mkdir(parents=True)
    real = os.path.realpath(base)
    return {
        "base": real,
        "main": os.path.join(real, "myrepo"),
        "single": os.path.join(real, "single"),
        "nogit": os.path.join(real, "nogit"),
    }


@pytest.fixture(scope="module")
def sparse(tmp_path_factory):
    """A repo with an untracked setup.sh whose linked worktree has no src/ directory."""
    base = tmp_path_factory.mktemp("sparse")
    main = base / "tool"
    (main / "src").mkdir(parents=True)
    (main / "src" / ".keep").touch()
    git(str(main), "init", "-q", "-b", "main")
    git(str(main), "add", "-A")
    git(str(main), "commit", "-q", "-m", "init")
    git(str(main), "worktree", "add", "-q", "-b", "slim", "../tool-slim")
    git(str(base / "tool-slim"), "rm", "-q", "-r", "src")
    git(str(base / "tool-slim"), "commit", "-q", "-m", "drop src")
    script = main / "setup.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    real = os.path.realpath(base)
    return {"main": os.path.join(real, "tool"), "slim": os.path.join(real, "tool-slim")}


@pytest.fixture
def project(tmp_path):
    main = tmp_path.resolve() / "tool"
    (main / "src").mkdir(parents=True)
    (main / "src" / ".keep").touch()
    script = main / "setup.sh"
    script.write_text("#!/bin/sh\nprintf 'default\\n' > script-origin\n")
    script.chmod(0o755)
    git(str(main), "init", "-q", "-b", "main")
    git(str(main), "add", "-A")
    git(str(main), "commit", "-q", "-m", "init")
    return main


class Harness:
    """tmuxp against a private tmux server, plus a tiny pseudo-terminal driver."""

    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.socket = f"tmuxp-wt-{uuid.uuid4().hex[:8]}"

    def env(self, wt=None, no_fzf=False):
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.pop("WT_PATH", None)
        env["SHELL"] = "/bin/sh"
        if no_fzf:  # a PATH with only git/tmux/sh forces the numbered fallback menu
            shim = self.dir / "shim"
            shim.mkdir(exist_ok=True)
            for tool in ("git", "tmux", "sh"):
                link = shim / tool
                if not link.exists():
                    link.symlink_to(shutil.which(tool))
            env["PATH"] = str(shim)
        if wt is not None:
            env["WT_PATH"] = wt
        return env

    def workspace(self, session, start, cds=("cd api", "cd web"), extra=""):
        panes = "".join(f"    - shell_command:\n      - {cd}\n" for cd in cds) or "    -\n"
        path = self.dir / f"{session}.yaml"
        path.write_text(
            f"session_name: {session}\n"
            f"start_directory: {start}\n"
            f"{extra}"
            "workspace_builder_paths:\n"
            f"  - {SRC}\n"
            "plugins:\n"
            "  - tmuxp_worktree.WorktreePlugin\n"
            "windows:\n"
            "  - window_name: main\n"
            "    start_directory: src\n"
            "    panes:\n"
            f"{panes}"
            "  - window_name: tools\n"
            "    start_directory: src\n"
            "    panes:\n"
            "    -\n"
        )
        return str(path)

    def config(self, **config):
        path = self.dir / f"{config['session_name']}.json"
        path.write_text(json.dumps({
            "workspace_builder_paths": [SRC],
            "plugins": ["tmuxp_worktree.WorktreePlugin"],
            **config,
        }))
        return str(path)

    def argv(self, path):
        return [TMUXP, "load", "-L", self.socket, "-d", "-f", "/dev/null", path]

    def load(self, path, wt=None):
        return run(self.argv(path), env=self.env(wt), stdin=subprocess.DEVNULL)

    def sessions(self):
        result = run(["tmux", "-L", self.socket, "list-sessions", "-F", "#{session_name}"])
        return set(result.stdout.splitlines()) if result.returncode == 0 else set()

    def panes(self, session, expected=None, timeout=8.0):
        """'window|current path' of every pane; waits until it equals *expected*."""
        deadline = time.time() + timeout
        while True:
            result = run(["tmux", "-L", self.socket, "list-panes", "-s", "-t", f"={session}",
                          "-F", "#{window_name}|#{pane_current_path}"])
            got = sorted(result.stdout.splitlines()) if result.returncode == 0 else []
            if expected is None or got == sorted(expected) or time.time() > deadline:
                return got
            time.sleep(0.2)

    def drive(self, path, steps, no_fzf=False, timeout=25):
        """Run tmuxp in a pseudo-terminal; steps = [(text to wait for, keys to send), ...]."""
        env = self.env(no_fzf=no_fzf)
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.execvpe(TMUXP, self.argv(path), env)
            finally:
                os._exit(127)
        terminal_before = termios.tcgetattr(fd)
        out, pos, step = b"", 0, 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                out += data
                # minimal terminal emulation: fzf asks for the cursor position
                for _ in range(data.count(b"\x1b[6n")):
                    os.write(fd, b"\x1b[1;1R")
            if step < len(steps):
                wait, keys = steps[step]
                idx = out.find(wait.encode(), pos)
                if idx != -1:
                    time.sleep(0.4)
                    os.write(fd, keys)
                    pos, step = idx + len(wait), step + 1
        else:
            os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        self.tty_restored = termios.tcgetattr(fd) == terminal_before
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    def close(self):
        run(["tmux", "-L", self.socket, "kill-server"])
        # tmux keeps the socket file after its server exits
        tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        try:
            os.unlink(os.path.join(tmpdir, f"tmux-{os.getuid()}", self.socket))
        except FileNotFoundError:
            pass


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def expected(root):
    return sorted([f"main|{root}/src/api", f"main|{root}/src/web", f"tools|{root}/src"])


def test_not_interactive_uses_default(h, repos):
    ws = h.workspace("app", repos["main"])
    result = h.load(ws)
    assert result.returncode == 0, result.stderr
    assert h.panes("app", expected(repos["main"])) == expected(repos["main"])


def test_wt_path_moves_session_next_to_default(h, repos):
    ws = h.workspace("app", repos["main"])
    assert h.load(ws).returncode == 0
    result = h.load(ws, wt="myrepo-feature-x")
    assert result.returncode == 0, result.stderr
    root = os.path.join(repos["base"], "myrepo-feature-x")
    assert h.panes("app-feature-x", expected(root)) == expected(root)
    assert {"app", "app-feature-x"} <= h.sessions()


def test_wt_path_absolute_nested_worktree(h, repos):
    nested = os.path.join(repos["main"], ".claude", "worktrees", "agent")
    ws = h.workspace("app", repos["main"])
    assert h.load(ws, wt=nested).returncode == 0
    assert h.panes("app-agent", expected(nested)) == expected(nested)


def test_dots_in_worktree_name_are_sanitized(h, repos):
    ws = h.workspace("app", repos["main"])
    assert h.load(ws, wt="myrepo-v1.2").returncode == 0
    root = os.path.join(repos["base"], "myrepo-v1.2")
    assert h.panes("app-v1-2", expected(root)) == expected(root)


def test_detached_worktree(h, repos):
    ws = h.workspace("app", repos["main"])
    assert h.load(ws, wt="myrepo-detached").returncode == 0
    assert "app-detached" in h.sessions()


@pytest.mark.parametrize("wt", ["myrepo-gone", "does-not-exist"])
def test_prunable_or_unknown_worktree_is_rejected(h, repos, wt):
    ws = h.workspace("app", repos["main"])
    result = h.load(ws, wt=wt)
    assert result.returncode == 1
    assert "is not a worktree" in result.stderr
    assert h.sessions() == set()


def test_single_worktree_repo_starts_normally(h, repos):
    ws = h.workspace("single", repos["single"], cds=())
    assert h.load(ws).returncode == 0
    assert "single" in h.sessions()


def test_non_git_start_directory_is_untouched(h, repos):
    ws = h.workspace("nogit", repos["nogit"], cds=())
    assert h.load(ws).returncode == 0
    assert "nogit" in h.sessions()


def test_directories_missing_in_the_worktree_fall_back_to_its_root(h, sparse):
    ws = h.workspace("app", sparse["main"], cds=())
    result = h.load(ws, wt="tool-slim")
    assert result.returncode == 0, result.stderr
    assert "tool-slim/src does not exist; using tool-slim" in result.stderr
    want = sorted([f"main|{sparse['slim']}", f"tools|{sparse['slim']}"])
    assert h.panes("app-slim", want) == want


def test_before_script_missing_in_the_worktree_does_not_abort_the_load(h, sparse):
    script = os.path.join(sparse["main"], "setup.sh")
    ws = h.workspace("app", sparse["main"], cds=(), extra=f"before_script: {script}\n")
    result = h.load(ws, wt="tool-slim")
    assert result.returncode == 0, result.stderr
    assert "tool-slim/setup.sh does not exist" in result.stderr
    assert "app-slim" in h.sessions()


@pytest.mark.parametrize("first,second,session", [
    ("tool-v1.2", "tool-v1-2", "app-v1-2"),
    ("nested/checkout", "other/checkout", "app-checkout"),
])
def test_colliding_session_names_do_not_reuse_another_worktree(h, project, first, second, session):
    first, second = project.parent / first, project.parent / second
    git(str(project), "worktree", "add", "-q", "-b", "first", str(first))
    ws = h.workspace("app", str(project), cds=())
    result = h.load(ws, wt=str(first))
    assert result.returncode == 0, result.stderr
    want = [f"main|{first}/src", f"tools|{first}/src"]
    assert h.panes(session, want) == want

    git(str(project), "worktree", "add", "-q", "-b", "second", str(second))
    result = h.load(ws, wt=str(second))
    assert result.returncode == 1, result.stderr
    assert "ambiguous" in result.stderr
    assert str(first) in result.stderr and str(second) in result.stderr
    assert h.sessions() == {session}
    assert h.panes(session) == want


@pytest.mark.parametrize("quoted,checkout", [(True, "checkout"), (False, "checkout space")])
def test_before_script_runs_the_selected_worktrees_copy(h, project, quoted, checkout):
    chosen = project.parent / checkout
    git(str(project), "worktree", "add", "-q", "-b", "feature", str(chosen))
    (chosen / "setup.sh").write_text(
        "#!/bin/sh\nprintf 'selected\\n' > script-origin\nprintf '%s\\n' \"$@\" > script-args\n"
    )
    executable = f'"{project}/setup.sh"' if quoted else f"{project}/setup.sh"
    ws = h.workspace(
        "app", str(project), cds=(),
        extra=f"before_script: '{executable} --fast \"two words\" \"\"'\n",
    )
    result = h.load(ws, wt=str(chosen))
    assert result.returncode == 0, result.stderr
    assert (chosen / "script-origin").read_text() == "selected\n"
    assert (chosen / "script-args").read_text() == "--fast\ntwo words\n\n"
    assert not (project / "script-origin").exists()


def test_directory_fallback_waits_for_setup_before_starting_windows_and_panes(h, project):
    chosen = project.parent / "tool-feature"
    git(str(project), "worktree", "add", "-q", "-b", "feature", str(chosen))
    for directory in ("generated/nested", "missing"):
        (project / directory).mkdir(parents=True)
    (chosen / "setup.sh").write_text(
        "#!/bin/sh\nmkdir -p generated/nested\nprintf 'run\\n' >> script-runs\n"
    )
    marker = shlex.quote(str(chosen / "shell-starts"))
    shell = f"pwd >> {marker}; exec /bin/sh"
    ws = h.config(
        session_name="app", start_directory=str(project),
        before_script=str(project / "setup.sh"),
        windows=[
            {
                "window_name": "created", "start_directory": str(project / "generated"),
                "window_shell": shell, "panes": [{}],
            },
            {
                "window_name": "fallback", "start_directory": str(project / "missing"),
                "panes": [{}, {"start_directory": str(project / "generated" / "nested")}],
            },
            {
                "window_name": "pane", "start_directory": str(project),
                "panes": [{"start_directory": str(project / "generated"), "shell": shell}],
            },
        ],
    )
    result = h.load(ws, wt=str(chosen))
    assert result.returncode == 0, result.stderr
    want = [
        f"created|{chosen}/generated", f"fallback|{chosen}",
        f"fallback|{chosen}/generated/nested", f"pane|{chosen}/generated",
    ]
    assert h.panes("app-feature", want) == want
    assert (chosen / "script-runs").read_text() == "run\n"
    assert (chosen / "shell-starts").read_text().splitlines() == [str(chosen / "generated")] * 2
    assert "tool-feature/missing does not exist" in result.stderr
    assert "tool-feature/generated does not exist" not in result.stderr

    # Loading an existing session must not run setup again, even with a missing directory.
    result = h.load(ws, wt=str(chosen))
    assert result.returncode == 0, result.stderr
    assert (chosen / "script-runs").read_text() == "run\n"
    assert h.panes("app-feature") == want


def test_failed_deferred_setup_removes_the_new_session(h, project):
    (project / "missing").mkdir()
    chosen = project.parent / "tool-feature"
    git(str(project), "worktree", "add", "-q", "-b", "feature", str(chosen))
    (chosen / "setup.sh").write_text("#!/bin/sh\nexit 7\n")
    ws = h.config(
        session_name="app", start_directory=str(project),
        before_script=str(project / "setup.sh"),
        windows=[{"window_name": "main", "start_directory": str(project / "missing"), "panes": [{}]}],
    )
    result = h.load(ws, wt=str(chosen))
    assert result.returncode != 0
    assert h.sessions() == set()


def test_external_directory_with_parent_components_is_not_rebased(h, project):
    shared = project.parent / "shared"
    shared.mkdir()
    chosen = project / ".worktrees" / "feature"
    git(str(project), "worktree", "add", "-q", "-b", "feature", str(chosen))
    ws = h.config(
        session_name="app", start_directory=str(project),
        windows=[{"window_name": "main", "start_directory": f"{project}/../shared", "panes": [{}]}],
    )
    result = h.load(ws, wt=str(chosen))
    assert result.returncode == 0, result.stderr
    assert h.panes("app-feature", [f"main|{shared}"]) == [f"main|{shared}"]


def test_numbered_menu_without_fzf(h, repos):
    # order: default, myrepo-detached, myrepo-feature-x, myrepo-v1.2, nested agent
    ws = h.workspace("menu", repos["main"])
    rc, out = h.drive(ws, [("Choice", b"\r")], no_fzf=True)
    assert rc == 0 and "(default)" in out and h.sessions() == {"menu"}, out[-500:]
    assert h.tty_restored
    rc, out = h.drive(ws, [("Choice", b"9\x7f2\r")], no_fzf=True)
    assert rc == 0 and "menu-detached" in h.sessions(), out[-500:]
    assert h.tty_restored
    rc, out = h.drive(ws, [("Choice", b"x\r"), ("Choice", b"3\r")], no_fzf=True)
    assert rc == 0 and "menu-feature-x" in h.sessions(), out[-500:]
    before = h.sessions()
    rc, out = h.drive(ws, [("Choice", b"\x04")], no_fzf=True)
    assert rc == 1 and "cancelled" in out and h.sessions() == before, out[-500:]
    assert h.tty_restored


@pytest.mark.parametrize("cancel", [b"\x1b", b"\x03"])
def test_numbered_menu_cancels_immediately_and_restores_the_terminal(h, repos, cancel):
    ws = h.workspace("menu", repos["main"])
    rc, out = h.drive(ws, [("Choice", cancel)], no_fzf=True, timeout=5)
    assert rc == 1 and "cancelled" in out, out[-500:]
    assert h.sessions() == set()
    assert h.tty_restored


@pytest.mark.skipif(not shutil.which("fzf"), reason="fzf not installed")
def test_fzf_ignores_global_output_options(h, repos, monkeypatch):
    options_file = h.dir / "fzf.opts"
    options_file.write_text("--print0\n")
    monkeypatch.setenv("FZF_DEFAULT_OPTS", "--print-query --filter=feature")
    monkeypatch.setenv("FZF_DEFAULT_OPTS_FILE", str(options_file))
    ws = h.workspace("fzf", repos["main"])
    rc, out = h.drive(ws, [("worktree>", b"\r")])
    assert rc == 0 and h.sessions() == {"fzf"}, out[-500:]
    assert os.environ["FZF_DEFAULT_OPTS"] == "--print-query --filter=feature"
    assert os.environ["FZF_DEFAULT_OPTS_FILE"] == str(options_file)


@pytest.mark.skipif(not shutil.which("fzf"), reason="fzf not installed")
def test_fzf_menu(h, repos):
    ws = h.workspace("fzf", repos["main"])
    rc, out = h.drive(ws, [("worktree>", b"\r")])
    assert rc == 0 and h.sessions() == {"fzf"}, out[-500:]
    rc, out = h.drive(ws, [("worktree>", b"\x1b[B"), ("", b"\r")])
    assert rc == 0 and "fzf-detached" in h.sessions(), out[-500:]
    # fzf filters asynchronously: wait for its "1/5" match count before Enter
    rc, out = h.drive(ws, [("worktree>", b"feature"), ("1/5", b"\r")])
    assert rc == 0 and "fzf-feature-x" in h.sessions(), out[-500:]
    before = h.sessions()
    rc, out = h.drive(ws, [("worktree>", b"\x1b")])
    assert rc == 1 and "cancelled" in out and h.sessions() == before, out[-500:]


def test_no_menu_for_single_worktree(h, repos):
    ws = h.workspace("solo", repos["single"], cds=())
    rc, out = h.drive(ws, [], timeout=15)
    assert rc == 0 and "Choice" not in out and "worktree>" not in out, out[-500:]
    assert "solo" in h.sessions()

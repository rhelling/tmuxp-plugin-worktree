"""tmuxp-plugin-worktree: pick a git worktree when loading a tmuxp session.

Enable it in a workspace file::

    workspace_builder_paths:   # tmuxp >= 1.73; not needed when the plugin is installed
      - ~/Documents/Projects/tmuxp-plugin-worktree/src
    plugins:
      - tmuxp_plugin_worktree.WorktreePlugin

The session's ``start_directory`` is the default. When that repository has more
worktrees, the plugin shows a picker (fzf, or a numbered list without fzf) with
the default preselected. Picking another worktree moves every session, window
and pane path into it and renames the session to ``<session>-<worktree>``, so it
can run next to the default session.

Non-interactive: ``WT_PATH=<worktree> tmuxp load <session>``. A relative path is
resolved against the directory that contains the default repository (for example
``WT_PATH=myrepo-feature-x``). Esc or Ctrl-C in the picker cancels the load;
nothing is started.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
import tty
from collections.abc import Callable

from libtmux import Session
from tmuxp.plugin import TmuxpPlugin
from tmuxp.util import run_before_script

__version__ = "0.2.0"

ENV_VAR = "WT_PATH"


class WorktreePlugin(TmuxpPlugin):
    """Let the user pick the git worktree a session starts in."""

    def __init__(self) -> None:
        super().__init__(plugin_name="tmuxp-plugin-worktree")
        self._config = _workspace_config()
        self._finish: Callable[[], None] | None = None
        if self._config is None:
            _say("workspace config not found; starting in the default directory")
            return
        self._finish = choose_worktree(self._config)

    def before_workspace_builder(self, session: Session) -> None:
        if self._config is None or self._finish is None:
            return
        # This hook runs only for new builds, before any configured windows.
        # Take ownership of setup so fallback decisions can follow it.
        try:
            if "before_script" in self._config:
                run_before_script(
                    self._config.pop("before_script"),
                    cwd=self._config.get("start_directory"),
                )
            self._finish()
        except Exception:
            session.kill()
            raise
        finally:
            self._finish = None


def choose_worktree(config: dict) -> Callable[[], None] | None:
    """Point an (already expanded) tmuxp workspace config at the chosen worktree."""
    wanted = os.environ.get(ENV_VAR)
    start = config.get("start_directory")
    if not isinstance(start, str) or not os.path.isdir(start):
        _ignored(wanted, "the session has no existing start_directory")
        return
    toplevel = _git(start, "rev-parse", "--show-toplevel")
    if not toplevel:
        _ignored(wanted, f"{start} is not in a git repository")
        return

    worktrees = _worktrees(start)
    default = next((w for w in worktrees if _same(w["path"], toplevel)), None)
    if default is None:
        _ignored(wanted, f"git does not list {toplevel} as a worktree")
        return
    others = sorted((w for w in worktrees if w is not default), key=lambda w: w["path"])
    choices = [default, *others]

    if wanted:
        chosen = _from_env(wanted, toplevel, choices)
    elif others:
        chosen = _menu(str(config.get("session_name", "")), choices, toplevel)
    else:
        return
    if chosen is default:
        return

    session = str(config.get("session_name", ""))
    name = _session_name(session, chosen["path"], toplevel)
    conflicts = [
        w for w in choices if w is not chosen
        and (session if w is default else _session_name(session, w["path"], toplevel)) == name
    ]
    if conflicts:
        _say(f"session name {name!r} is ambiguous; rename the conflicting worktree directories:")
        for worktree in [chosen, *conflicts]:
            _say("  " + worktree["path"])
        raise SystemExit(1)
    finish = _rebase_config(config, _config_root(start, toplevel), chosen["path"])
    config["session_name"] = name
    _say(f"{_short(chosen['path'], toplevel)} -> session {config['session_name']}")
    return finish


def _workspace_config() -> dict | None:
    """Return the workspace dict tmuxp is creating this plugin for.

    tmuxp calls ``load_plugins(session_config)`` after expanding the workspace but
    before checking for an existing session and before building it, so changes to
    that dict (paths, session name) apply to the whole session.
    """
    frame = inspect.currentframe()
    try:
        caller = frame.f_back if frame else None
        for _ in range(20):
            if caller is None:
                break
            for value in list(caller.f_locals.values()):
                if (
                    isinstance(value, dict)
                    and "session_name" in value
                    and isinstance(value.get("windows"), list)
                ):
                    return value
            caller = caller.f_back
    finally:
        del frame
    return None


def _git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return proc.stdout.strip()


def _worktrees(cwd: str) -> list[dict]:
    """Usable worktrees according to ``git worktree list --porcelain``."""
    out = _git(cwd, "worktree", "list", "--porcelain") or ""
    worktrees: list[dict] = []
    current: dict | None = None
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            current = {"path": line[9:], "branch": None, "head": "", "usable": True}
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "bare" or line.startswith("prunable"):
            current["usable"] = False
        elif not line:
            if current["usable"] and os.path.isdir(current["path"]):
                worktrees.append(current)
            current = None
    return worktrees


def _same(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _from_env(wanted: str, toplevel: str, choices: list[dict]) -> dict:
    path = os.path.expanduser(wanted)
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(toplevel), path)
    for worktree in choices:
        if _same(worktree["path"], path):
            return worktree
    _say(f"{ENV_VAR}={wanted} is not a worktree of {_short(toplevel, toplevel)}. Choose from:")
    for worktree in choices:
        _say("  " + _short(worktree["path"], toplevel))
    raise SystemExit(1)


def _ignored(wanted: str | None, reason: str) -> None:
    """Tell the user that an explicit WT_PATH could not be applied to this session."""
    if wanted:
        _say(f"{ENV_VAR}={wanted} ignored: {reason}")


def _menu(session: str, choices: list[dict], toplevel: str) -> dict:
    if not _interactive():
        return choices[0]
    labels = [_label(w, toplevel, default=(i == 0)) for i, w in enumerate(choices)]
    fzf = shutil.which("fzf")
    try:
        index = _fzf(fzf, session, labels) if fzf else _numbered(session, labels)
    except KeyboardInterrupt:
        index = None
    if index is None:
        _say("cancelled; nothing was started")
        raise SystemExit(1)
    return choices[index]


def _interactive() -> bool:
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def _fzf(fzf: str, session: str, labels: list[str]) -> int | None:
    lines = "".join(f"{i}\t{label}\n" for i, label in enumerate(labels))
    proc = subprocess.run(
        [
            fzf,
            "--height=40%",
            "--layout=reverse",
            "--border=rounded",
            "--no-multi",
            "--delimiter=\t",
            "--with-nth=2..",
            "--prompt=worktree> ",
            f"--header=Session {session}: pick a worktree (Enter = start, Esc = cancel)",
        ],
        input=lines,
        stdout=subprocess.PIPE,
        text=True,
        env={
            key: value for key, value in os.environ.items()
            if key not in {"FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE"}
        },
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        index = int(proc.stdout.split("\t", 1)[0])
    except ValueError:
        return None
    return index if 0 <= index < len(labels) else None


def _numbered(session: str, labels: list[str]) -> int | None:
    with open("/dev/tty") as tty_in, open("/dev/tty", "w") as tty_out:
        tty_out.write(f"Session {session}: pick a worktree\n")
        for i, label in enumerate(labels, 1):
            tty_out.write(f"  {i}) {label}\n")
        fd = tty_in.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            while True:
                tty_out.write(f"Choice [1-{len(labels)}, Enter = 1, Esc = cancel]: ")
                tty_out.flush()
                answer = ""
                while True:
                    char = tty_in.read(1)
                    if char in ("", "\x04", "\x1b"):
                        tty_out.write("\n")
                        return None
                    if char in ("\r", "\n"):
                        tty_out.write("\n")
                        answer = answer.strip()
                        if not answer:
                            return 0
                        if answer.isdecimal() and 1 <= int(answer) <= len(labels):
                            return int(answer) - 1
                        break
                    if char in ("\x7f", "\b"):
                        if answer:
                            answer = answer[:-1]
                            tty_out.write("\b \b")
                    elif char.isprintable():
                        answer += char
                        tty_out.write(char)
                    tty_out.flush()
        except KeyboardInterrupt:
            tty_out.write("\n")
            raise
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, previous)


def _label(worktree: dict, toplevel: str, default: bool) -> str:
    ref = worktree["branch"] or f"detached {worktree['head'][:9]}".strip()
    text = f"{_short(worktree['path'], toplevel):<40}  [{ref}]"
    return f"{text}  (default)" if default else text


def _short(path: str, toplevel: str) -> str:
    """Path relative to the directory containing the default repository, else with ~."""
    parent = os.path.dirname(os.path.realpath(toplevel))
    real = os.path.realpath(path)
    if real.startswith(parent + os.sep):
        return os.path.relpath(real, parent)
    home = os.path.expanduser("~")
    return "~" + real[len(home):] if real.startswith(home + os.sep) else real


def _suffix(path: str, toplevel: str) -> str:
    """'myrepo-feature-x' -> 'feature-x'; otherwise the worktree's directory name."""
    name = os.path.basename(os.path.realpath(path))
    main = os.path.basename(os.path.realpath(toplevel))
    match = re.fullmatch(rf"{re.escape(main)}[-_. ]+(.+)", name)
    return match.group(1) if match else name


def _session_name(session: str, path: str, toplevel: str) -> str:
    return re.sub(r"[.:]", "-", f"{session}-{_suffix(path, toplevel)}").strip("-")


def _config_root(start: str, toplevel: str) -> str:
    """The repository root as spelled in the config (start_directory may be a subdirectory)."""
    prefix = (_git(start, "rev-parse", "--show-prefix") or "").strip("/")
    start = start.rstrip(os.sep)
    if not prefix:
        return start
    tail = os.sep + prefix
    return start[: -len(tail)] if start.endswith(tail) else toplevel


def _rebase(path: object, old_root: str, new_root: str) -> object:
    if not isinstance(path, str) or not os.path.isabs(path):
        return path
    real_root, real_path = os.path.realpath(old_root), os.path.realpath(path)
    if os.path.commonpath((real_root, real_path)) != real_root:
        return path
    # Keep internal symlink spellings, except when resolving .. requires following them.
    if os.pardir not in path.split(os.sep):
        normalized = os.path.normpath(path)
        for root in dict.fromkeys((os.path.normpath(old_root), real_root)):
            if normalized == root or normalized.startswith(root + os.sep):
                return os.path.normpath(new_root + normalized[len(root):])
    return os.path.normpath(os.path.join(new_root, os.path.relpath(real_path, real_root)))


def _rebase_dir(path: object, old_root: str, new_root: str, warned: set[str]) -> object:
    """Like ``_rebase``, but a directory the chosen worktree lacks becomes its nearest
    existing parent; tmux would silently start in $HOME instead."""
    rebased = _rebase(path, old_root, new_root)
    # A directory missing from the default worktree too may be created later (for
    # example by before_script); only directories the other branch lost need a fallback.
    if rebased == path or not os.path.isdir(path) or os.path.isdir(rebased):
        return rebased
    fallback = rebased
    while fallback != new_root and not os.path.isdir(fallback):
        fallback = os.path.dirname(fallback)
    if rebased not in warned:
        warned.add(rebased)
        _say(f"{_short(rebased, old_root)} does not exist; using {_short(fallback, old_root)}")
    return fallback


def _rebase_script(command: object, old_root: str, new_root: str) -> object:
    """Like ``_rebase``, but keep the default worktree's script when the chosen one has
    no copy (for example an untracked file); tmuxp aborts when before_script is missing."""
    if not isinstance(command, str):
        return command
    try:
        args = shlex.split(command)
    except ValueError:
        return command
    if not args:
        return command
    script = _rebase(args[0], old_root, new_root)
    if script == args[0]:
        return command
    if os.path.isfile(script):
        args[0] = script
        return shlex.join(args)
    _say(f"{_short(script, old_root)} does not exist; keeping the default worktree's script")
    return command


def _rebase_config(config: dict, old_root: str, new_root: str) -> Callable[[], None] | None:
    """Rebase paths, returning any fallback work that must wait for setup."""
    warned: set[str] = set()
    pending: list[tuple[dict, str]] = []

    def rebase(holder: dict, defer: bool = False) -> None:
        if "start_directory" in holder:
            path = holder["start_directory"]
            rebased = _rebase(path, old_root, new_root)
            if defer and rebased != path and os.path.isdir(path) and not os.path.isdir(rebased):
                holder["start_directory"] = rebased
                pending.append((holder, path))
            else:
                holder["start_directory"] = _rebase_dir(path, old_root, new_root, warned)

    rebase(config)
    if "before_script" in config:
        config["before_script"] = _rebase_script(config["before_script"], old_root, new_root)
    for window in config.get("windows") or []:
        if not isinstance(window, dict):
            continue
        rebase(window, defer="before_script" in config)
        for pane in window.get("panes") or []:
            if isinstance(pane, dict):
                rebase(pane, defer="before_script" in config)

    def finish() -> None:
        for holder, path in pending:
            holder["start_directory"] = _rebase_dir(path, old_root, new_root, warned)

    return finish if pending else None


def _say(message: str) -> None:
    print(f"[worktree] {message}", file=sys.stderr, flush=True)

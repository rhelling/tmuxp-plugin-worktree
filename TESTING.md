# Testing tmuxp-worktree

How to run the automated tests, what they cover, and how to check the plugin by
hand against your own repositories.

## Requirements

- macOS or Linux (the interactive tests drive tmuxp through a pseudo-terminal)
- Python 3.10 or newer, with a tmuxp version that supports that Python (see
  [Troubleshooting](#troubleshooting))
- `tmux` and `git` on your `PATH`
- `fzf` (optional; without it the two fzf tests are skipped)

## Run the test suite

```bash
cd ~/Documents/Projects/tmuxp-worktree
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

`pip install -e '.[test]'` installs the plugin in editable mode together with
tmuxp and pytest, so code changes are picked up without reinstalling. A full run
takes about 20 seconds and ends with `37 passed`, or `35 passed, 2 skipped` when
fzf is not installed. You can run it from inside tmux: the tests ignore `TMUX`
and `WT_PATH` from your shell.

Useful variations:

```bash
.venv/bin/pytest -v                            # one line per test
.venv/bin/pytest tests/test_helpers.py         # unit tests only, no tmux server needed
.venv/bin/pytest -k fzf_menu                   # only the fzf picker test
.venv/bin/pytest -k numbered_menu              # only the numbered fallback menu tests
.venv/bin/pytest -k wt_path                    # tests whose name contains "wt_path"
.venv/bin/pytest -k "before_script or setup"   # the before_script (setup) tests
.venv/bin/pytest -x                            # stop at the first failure
```

### Against a specific tmuxp version

The tests use the tmuxp that is installed next to the Python running pytest. To
check another version, install it into the same virtualenv and run the suite
again:

```bash
.venv/bin/pip install 'tmuxp==1.73.0'
.venv/bin/pytest
.venv/bin/pip install -U tmuxp    # back to the newest version
```

The no-install setup (`workspace_builder_paths`) needs tmuxp 1.73 or newer, so
that is the oldest version worth testing. The suite passes on tmuxp 1.73, 1.74
and 1.76.

tmuxp 1.73 and 1.74 do not start on Python 3.15 (see
[Troubleshooting](#troubleshooting)). To test them there, make the virtualenv
with Python 3.14 and switch back afterwards:

```bash
python3.14 -m venv --clear .venv
.venv/bin/pip install -e '.[test]' 'tmuxp==1.73.0'
.venv/bin/pytest
python3 -m venv --clear .venv && .venv/bin/pip install -e '.[test]'
```

## What the tests cover

| File | Tests | Checks | Needs |
|------|-------|--------|-------|
| `tests/test_helpers.py` | 13 | the helpers on their own: the session-name suffix, moving paths into a worktree (including `..` and symlinks), the fallback for missing directories, rewriting `before_script`, the note when `WT_PATH` cannot be applied, and validating fzf's output | the tmuxp package; no tmux server |
| `tests/test_e2e.py` | 24 | the real `tmuxp load` with the plugin enabled | tmuxp, tmux and git (skipped without them); fzf for two tests |

The end-to-end tests build throwaway repositories in pytest's temporary
directory:

- `myrepo` with four usable worktrees: `myrepo-feature-x`, `myrepo-detached`
  (detached HEAD), `myrepo-v1.2` (a dot in its name) and the nested
  `myrepo/.claude/worktrees/agent`, plus `myrepo-gone`, whose directory was
  deleted so git marks it prunable;
- `single`, a repository with only its main worktree;
- `nogit`, a plain directory;
- `tool` with a worktree `tool-slim` whose branch deleted `src/`, and an
  untracked `setup.sh` that only the default worktree has;
- for the tests that add their own worktrees, a fresh `tool` repository with
  `src/` and a `setup.sh` per test.

They check that:

- without a terminal and without `WT_PATH`, the default worktree is used;
- `WT_PATH` (relative, absolute or nested) moves every pane into the worktree and
  renames the session, which then runs next to the default session;
- dots in the worktree name become `-` in the session name;
- prunable and unknown worktrees are rejected with exit code 1, and nothing is
  started;
- a repository with a single worktree and a non-git start directory load
  normally, without a picker;
- a directory the chosen worktree lacks falls back to its nearest existing
  parent, with a note; a directory outside the repository (such as
  `myrepo/../shared`) is left alone;
- `before_script` runs the chosen worktree's copy with its arguments intact
  (quotes, spaces, empty arguments), or the default worktree's copy when the
  chosen worktree does not have one;
- directories that `before_script` creates are used instead of a fallback,
  loading an existing session does not run setup again, and a failing setup
  removes the new session;
- two worktrees that would get the same session name abort the load with exit
  code 1 and leave the running session alone;
- the numbered menu (fzf hidden from `PATH`) handles Enter, a number, Backspace,
  invalid input, Esc, Ctrl-C and Ctrl-D, and restores the terminal settings;
- the fzf picker handles Enter (default preselected), arrow down, typing a filter
  and Esc, and ignores `FZF_DEFAULT_OPTS` and `FZF_DEFAULT_OPTS_FILE`.

### How the tests stay out of your way

- Every test starts its own tmux server on a random socket
  (`tmux -L tmuxp-wt-<id>`) and kills it afterwards, so your running sessions are
  never touched.
- Sessions are loaded detached with `-f /dev/null` (your `~/.tmux.conf` is not
  read) and `SHELL=/bin/sh`.
- Panes and setup scripts only run short shell commands inside the temporary
  directory, so no project processes are started.
- Interactive cases run tmuxp in a pseudo-terminal. The driver waits for the
  picker (`worktree>` for fzf, `Choice` for the numbered menu), sends keys, and
  answers fzf's cursor-position query the way a real terminal would.

## Manual testing

Use a scratch repository or one of your own; replace `~/code/myrepo` and `myapp`
with yours.

1. Create a second worktree next to the repository, and a directory that only
   the default worktree has (git does not track empty directories, so the new
   worktree does not get it):

   ```bash
   git -C ~/code/myrepo worktree add -b feature/x ../myrepo-feature-x
   mkdir ~/code/myrepo/only-in-main
   ```

2. Add a workspace file, for example `~/.tmuxp/myapp.yaml`:

   ```yaml
   session_name: myapp
   start_directory: ~/code/myrepo
   workspace_builder_paths:
     - ~/Documents/Projects/tmuxp-worktree/src
   plugins:
     - tmuxp_worktree.WorktreePlugin
   windows:
     - window_name: shell
       panes:
         -
     - window_name: local
       start_directory: only-in-main
       panes:
         -
   ```

3. Go through the checks below. `-d` keeps each session detached, so you stay in
   your shell between checks; `tmux ls` shows which sessions exist.

   | Run | Expect |
   |-----|--------|
   | `tmuxp load -d myapp`, press Enter | picker with `myrepo … (default)` on top; session `myapp` |
   | `tmuxp load -d myapp`, press Esc | `[worktree] cancelled; nothing was started`, exit code 1 |
   | `WT_PATH=myrepo-feature-x tmuxp load -d myapp` | no picker; `[worktree] myrepo-feature-x/only-in-main does not exist; using myrepo-feature-x`; session `myapp-feature-x` next to `myapp` |
   | `tmux kill-session -t myapp-feature-x`, then `tmuxp load -d myapp` and pick `myrepo-feature-x` | session `myapp-feature-x` again, this time from the picker |
   | `WT_PATH=nope tmuxp load -d myapp` | `[worktree] WT_PATH=nope is not a worktree of myrepo. Choose from:` and the valid worktrees, exit code 1 |

   Check where a session's panes are:

   ```bash
   tmux list-panes -s -t myapp-feature-x -F '#{window_name}: #{pane_current_path}'
   ```

   Both windows are in `myrepo-feature-x`: `local` fell back to the worktree
   root because `only-in-main` does not exist there. In `myapp`, `local` is in
   `myrepo/only-in-main`.

   To see the numbered menu instead of fzf, hide fzf from `PATH`. It lists the
   same worktrees; press Esc to cancel:

   ```bash
   mkdir -p /tmp/nofzf
   ln -sf "$(command -v tmux)" "$(command -v git)" /tmp/nofzf/
   PATH=/tmp/nofzf "$(command -v tmuxp)" load -d myapp
   ```

4. Clean up:

   ```bash
   tmux kill-session -t myapp
   tmux kill-session -t myapp-feature-x
   git -C ~/code/myrepo worktree remove ../myrepo-feature-x
   rmdir ~/code/myrepo/only-in-main
   rm -r /tmp/nofzf    # if you tried the numbered menu
   ```

## Troubleshooting

- **`pip install -e '.[test]'` fails with `requires a different Python`,
  `No matching distribution found for tmuxp>=1.73` or `Directory cannot be
  installed in editable mode`**: check `.venv/bin/python --version`. Below 3.10
  (the `python3` that ships with macOS is 3.9), recreate the virtualenv with a
  newer Python, for example `python3.12 -m venv --clear .venv`. On 3.10 or newer,
  pip is too old for editable installs: run `.venv/bin/pip install -U pip` first.
- **tmuxp crashes at start with `AttributeError: 'NoneType' object has no
  attribute 'reset'` in argparse**: that tmuxp version does not support your
  Python (seen with tmuxp 1.73 and 1.74 on Python 3.15). Recreate the virtualenv
  with an older Python, for example `python3.14 -m venv --clear .venv`, or
  install a newer tmuxp with `.venv/bin/pip install -U tmuxp`.
- **`[Plugin Error] Couldn't load tmuxp_worktree.WorktreePlugin` with
  `No module named 'tmuxp_worktree'`**: tmuxp cannot import the plugin. Check the
  `workspace_builder_paths` entry (tmuxp 1.73 or newer), or install the plugin
  into tmuxp's environment.
- **`[Builder Error] workspace_builder_paths entry is invalid: …`**: that
  directory does not exist.
- **Only the Ctrl-C case of
  `test_numbered_menu_cancels_immediately_and_restores_the_terminal` fails, with
  `-9`**: pytest was started with Ctrl-C (SIGINT) ignored, so tmuxp never gets it.
  A shell script that starts pytest in the background (`pytest &`) does this.
  Run pytest in the foreground.
- **A picker test fails after waiting 25 seconds**: the driver never saw
  `worktree>` or `Choice`. The assertion message ends with the last part of the
  terminal output, which usually shows why the picker did not appear.
- **Leftover tmux servers after an interrupted run**: they are named
  `tmuxp-wt-<id>`. List them with `ps ax | grep '[t]muxp-wt-'` and stop one
  with `tmux -L tmuxp-wt-<id> kill-server`. Every run also leaves empty socket
  files in `${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)`, because tmux does not delete
  them on exit. Remove them when no tests are running:
  `rm "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"/tmuxp-wt-*`.

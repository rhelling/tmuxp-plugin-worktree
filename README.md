# tmuxp-worktree

A [tmuxp](https://github.com/tmux-python/tmuxp) plugin that lets you pick the git
worktree a session starts in.

When the repository of a session's `start_directory` has more than one worktree,
`tmuxp load <session>` first shows a picker with the configured directory
preselected:

```text
worktree>
  Session myapp: pick a worktree (Enter = start, Esc = cancel)
> myrepo                                    [main]  (default)
  myrepo-feature-x                          [feature/x]
  myrepo-hotfix                             [hotfix/login]
```

Picking another worktree:

- moves every session, window and pane `start_directory` inside the repository
  into that worktree (relative `cd` commands in your panes keep working), and a
  `before_script` that lives in the repository;
- renames the session to `<session>-<worktree>` (for example `myapp-feature-x`),
  so it can run next to the default session.

A branch may not have every directory your workspace uses. A directory that
exists in the default worktree but is missing in the chosen worktree is replaced
by its nearest existing parent, with a note on stderr (tmux would otherwise
silently start the pane in `$HOME`). Window and pane directories are checked
after `before_script`, so setup can create them first. The session directory is
resolved earlier to provide setup with a working directory.

A `before_script` the chosen worktree does not have, such as an untracked file,
keeps running from the default worktree. Its arguments are preserved, including
quoting and spaces; only the executable's path is rewritten.

With a single worktree, or when the start directory is not a git repository,
nothing changes and no picker is shown.

## Requirements

- tmuxp 1.73 or newer for the no-install setup (`workspace_builder_paths`);
  tested with tmuxp 1.73, 1.74 and 1.76
- git
- fzf (optional; without it you get a numbered menu)
- Python 3.10 or newer (the Python that tmuxp runs on)

## Installation

Put the project somewhere permanent, for example:

```bash
git clone <repository-url> ~/Documents/Projects/tmuxp-worktree
```

### Option A: no install (recommended, works with Homebrew's tmuxp)

Since 1.73, tmuxp can add import paths per workspace with
`workspace_builder_paths`. Add these keys to every workspace file that should get
the picker:

```yaml
session_name: myapp
start_directory: ~/code/myrepo
workspace_builder_paths:
  - ~/Documents/Projects/tmuxp-worktree/src
plugins:
  - tmuxp_worktree.WorktreePlugin
windows:
  - window_name: editor
    panes:
      - nvim
```

Relative entries in `workspace_builder_paths` are resolved against the directory
of the workspace file. Homebrew installs tmuxp in a private virtualenv without
pip, so this is the easiest route there.

### Option B: install into tmuxp's Python environment

Install the package where tmuxp can import it; then only the `plugins` key is
needed in your workspace files.

```bash
# tmuxp installed with pipx
pipx inject tmuxp ~/Documents/Projects/tmuxp-worktree

# tmuxp installed with uv
uv tool install tmuxp --with ~/Documents/Projects/tmuxp-worktree

# tmuxp installed with pip in a virtualenv (activate it first)
python -m pip install ~/Documents/Projects/tmuxp-worktree
```

```yaml
plugins:
  - tmuxp_worktree.WorktreePlugin
```

### Check that it works

```bash
tmuxp load myapp                                # picker when the repo has 2+ worktrees
WT_PATH=myrepo-feature-x tmuxp load -d myapp    # straight into that worktree, detached
```

## Usage

- **fzf picker:** Enter starts the highlighted entry (the default is preselected),
  typing filters the list, Esc or Ctrl-C cancels without starting anything.
  `FZF_DEFAULT_OPTS` and `FZF_DEFAULT_OPTS_FILE` are ignored for this picker to
  keep its output format predictable.
- **Without fzf:** type a number and press Enter, or press Enter for the default.
  Esc, Ctrl-C or Ctrl-D cancels.
- **No picker:** `WT_PATH=myrepo-feature-x tmuxp load myapp`. A relative
  `WT_PATH` is resolved against the directory that contains the default
  repository; absolute paths and `~/...` work too. An unknown worktree aborts
  with the list of valid choices. A session whose start directory is not in a
  git repository (or that has no `start_directory`) ignores `WT_PATH`, with a
  note.
- **No terminal** (scripts, cron, CI): the default worktree is used unless
  `WT_PATH` is set.
- Prunable worktrees (their directory is gone) and bare repositories are not
  offered.

## How it works

tmuxp creates plugins after it has expanded the workspace file, but before it
checks for an existing session and builds the windows. The plugin uses that
moment to rewrite the expanded configuration: every path inside the default
repository is moved into the chosen worktree and the session name gets the
worktree suffix. tmuxp does not pass the configuration to plugins, so the plugin
finds it in the calling frames; this is verified against tmuxp 1.73, 1.74 and 1.76.

When a directory fallback needs to wait for `before_script`, the plugin runs
setup once through tmuxp's script runner in its `before_workspace_builder` hook,
then finalizes the window and pane paths. Existing sessions do not rerun setup.
For this case, tmuxp's script-progress callbacks are bypassed. If using other
plugins, list this plugin last so their preparation hooks still run before setup.

## Limitations

- Absolute paths inside `shell_command` or `shell_command_before` are not
  rewritten; use `start_directory` and relative `cd` commands instead.
- The session-name suffix comes from the worktree's directory name:
  `myrepo-feature-x` becomes `feature-x`, any other name is used as is. Dots and
  colons become `-`, because tmux does not allow them in session names. If
  multiple worktrees produce the same session name, selecting one aborts with
  a list of the conflicting paths. Rename those worktree directories rather
  than silently reusing another worktree's session.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The end-to-end tests need `tmux` and `git`. They create throwaway repositories
and run tmuxp against a private tmux server, so your own sessions are never
touched. The fzf test is skipped when fzf is not installed.

## License

[MIT](LICENSE).

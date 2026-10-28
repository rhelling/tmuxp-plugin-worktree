"""Unit tests for the path and picker helpers (no tmux needed)."""

import os
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import tmuxp_worktree as wt  # noqa: E402


def test_suffix_strips_the_repository_name():
    assert wt._suffix("/p/myrepo-feature-x", "/p/myrepo") == "feature-x"
    assert wt._suffix("/p/myrepo_hotfix", "/p/myrepo") == "hotfix"


def test_suffix_falls_back_to_the_directory_name():
    assert wt._suffix("/p/myrepo/.claude/worktrees/agent", "/p/myrepo") == "agent"
    assert wt._suffix("/elsewhere/checkout", "/p/myrepo") == "checkout"


def test_suffix_needs_a_separator_after_the_repository_name():
    assert wt._suffix("/p/myrepository", "/p/myrepo") == "myrepository"
    assert wt._suffix("/p/application", "/p/app") == "application"
    assert wt._suffix("/p/web2", "/p/web") == "web2"
    assert wt._suffix("/q/myrepo", "/p/myrepo") == "myrepo"


def test_rebase_only_touches_paths_inside_the_root():
    assert wt._rebase("/p/myrepo", "/p/myrepo", "/p/myrepo-x") == "/p/myrepo-x"
    assert wt._rebase("/p/myrepo/src/api", "/p/myrepo", "/p/myrepo-x") == "/p/myrepo-x/src/api"
    assert wt._rebase("/p/myrepository/src", "/p/myrepo", "/p/myrepo-x") == "/p/myrepository/src"
    assert wt._rebase(None, "/p/myrepo", "/p/myrepo-x") is None


def test_rebase_resolves_parent_paths_and_preserves_external_directories(tmp_path):
    old, new = tmp_path / "myrepo", tmp_path / "myrepo-x"
    (old / "src" / "nested").mkdir(parents=True)
    (tmp_path / "shared" / "nested").mkdir(parents=True)
    (old / "internal").symlink_to(old / "src" / "nested", target_is_directory=True)
    (old / "external").symlink_to(tmp_path / "shared" / "nested", target_is_directory=True)
    for path in (f"{old}/../shared", f"{old}/external/.."):
        assert wt._rebase(path, str(old), str(new)) == path
    assert wt._rebase(f"{old}/src/../docs", str(old), str(new)) == str(new / "docs")
    assert wt._rebase(f"{old}/internal/..", str(old), str(new)) == str(new / "src")


def test_rebase_preserves_internal_symlinks_and_accepts_a_symlinked_root(tmp_path):
    old, new, alias = tmp_path / "myrepo", tmp_path / "myrepo-x", tmp_path / "alias"
    (old / "src").mkdir(parents=True)
    alias.symlink_to(old, target_is_directory=True)
    (old / "link").symlink_to("src", target_is_directory=True)
    assert wt._rebase(str(alias / "link"), str(alias), str(new)) == str(new / "link")
    assert wt._rebase(str(old / "src"), str(alias), str(new)) == str(new / "src")


def test_rebase_config_rewrites_session_window_and_pane_paths():
    config = {
        "session_name": "app",
        "start_directory": "/p/myrepo",
        "windows": [
            {"start_directory": "/p/myrepo/src", "panes": [{"start_directory": "/p/myrepo/src/api"}, {}]},
            {"start_directory": "/tmp"},
        ],
    }
    wt._rebase_config(config, "/p/myrepo", "/p/myrepo-x")
    assert config["start_directory"] == "/p/myrepo-x"
    assert config["windows"][0]["start_directory"] == "/p/myrepo-x/src"
    assert config["windows"][0]["panes"][0]["start_directory"] == "/p/myrepo-x/src/api"
    assert config["windows"][1]["start_directory"] == "/tmp"


def test_rebase_config_falls_back_to_the_nearest_existing_directory(tmp_path, capsys):
    old, new = tmp_path / "myrepo", tmp_path / "myrepo-x"
    for directory in (old / "src" / "api", old / "src" / "web", old / "docs", new / "src" / "web"):
        directory.mkdir(parents=True)
    config = {
        "start_directory": str(old / "src" / "api"),
        "windows": [
            {"start_directory": str(old / "src" / "api"), "panes": [{"start_directory": str(old / "docs")}, {}]},
            {"start_directory": str(old / "src" / "web")},
        ],
    }
    wt._rebase_config(config, str(old), str(new))
    assert config["start_directory"] == str(new / "src")
    assert config["windows"][0]["start_directory"] == str(new / "src")
    assert config["windows"][0]["panes"][0]["start_directory"] == str(new)
    assert config["windows"][1]["start_directory"] == str(new / "src" / "web")
    err = capsys.readouterr().err
    assert err.count("myrepo-x/src/api does not exist; using myrepo-x/src") == 1
    assert "myrepo-x/docs does not exist; using myrepo-x\n" in err


def test_rebase_config_keeps_paths_that_do_not_exist_yet(tmp_path, capsys):
    old, new = tmp_path / "myrepo", tmp_path / "myrepo-x"
    old.mkdir()
    new.mkdir()
    # a directory the default worktree lacks too may still be created by before_script
    config = {"start_directory": str(old), "windows": [{"start_directory": str(old / "generated")}]}
    wt._rebase_config(config, str(old), str(new))
    assert config["windows"][0]["start_directory"] == str(new / "generated")
    assert capsys.readouterr().err == ""


def test_rebase_config_moves_before_script_only_when_the_worktree_has_it(tmp_path, capsys):
    old, new = tmp_path / "myrepo", tmp_path / "myrepo-x"
    old.mkdir()
    new.mkdir()
    for script in (old / "setup.sh", old / "local.sh", new / "setup.sh"):  # local.sh is untracked
        script.touch()
    tracked = {"start_directory": str(old), "before_script": f"{old}/setup.sh --fast"}
    wt._rebase_config(tracked, str(old), str(new))
    assert tracked["before_script"] == f"{new}/setup.sh --fast"
    untracked = {"start_directory": str(old), "before_script": f"{old}/local.sh --fast"}
    wt._rebase_config(untracked, str(old), str(new))
    assert untracked["before_script"] == f"{old}/local.sh --fast"
    assert "myrepo-x/local.sh does not exist; keeping" in capsys.readouterr().err


def test_rebase_script_preserves_quoted_arguments_and_only_moves_the_executable(tmp_path):
    old, new = tmp_path / "myrepo", tmp_path / "checkout space"
    old.mkdir()
    new.mkdir()
    (new / "setup.sh").touch()
    command = f'"{old}/setup.sh" --fast "two words" "" "{old}/input.txt"'
    rebased = wt._rebase_script(command, str(old), str(new))
    assert shlex.split(rebased) == [
        str(new / "setup.sh"), "--fast", "two words", "", str(old / "input.txt"),
    ]
    missing = f'"{old}/local.sh" "two words"'
    assert wt._rebase_script(missing, str(old), str(new)) == missing


def test_wt_path_that_cannot_be_applied_is_reported(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    cases = [
        ({"start_directory": str(plain)}, "is not in a git repository"),
        ({}, "no existing start_directory"),
    ]
    for extra, reason in cases:
        config = {"session_name": "app", "windows": [], **extra}
        monkeypatch.setenv(wt.ENV_VAR, "myrepo-x")
        wt.choose_worktree(config)
        err = capsys.readouterr().err
        assert "WT_PATH=myrepo-x ignored: " in err and reason in err
        assert config["session_name"] == "app"
    monkeypatch.delenv(wt.ENV_VAR)
    wt.choose_worktree({"session_name": "app", "start_directory": str(plain), "windows": []})
    assert capsys.readouterr().err == ""


def test_fzf_rejects_invalid_selection_output(monkeypatch):
    for output, expected in [
        ("1\tfeature\n", 1),
        ("query\n1\tfeature\n", None),
        ("-1\tmain\n", None),
        ("2\tmain\n", None),
    ]:
        monkeypatch.setattr(
            wt.subprocess, "run",
            lambda *args, **kwargs: wt.subprocess.CompletedProcess(args, 0, stdout=output),
        )
        assert wt._fzf("fzf", "app", ["main", "feature"]) == expected

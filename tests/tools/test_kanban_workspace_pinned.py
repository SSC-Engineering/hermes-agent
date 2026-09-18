"""Regression tests for the local-kanban workspace invariant (H2 / MCP-INC-002).

The dispatcher pins ``HERMES_KANBAN_WORKSPACE`` on worker spawn and stamps
``TERMINAL_CWD`` to the same path when it is a valid absolute directory.
Between spawn and the first tool call, three things can still drift
``TERMINAL_CWD`` away from the assigned worktree for a LOCAL backend
worker — all of them silently redirect file writes into the wrong
checkout, which is the MCP-INC-002 failure mode:

1. ``_ensure_terminal_env_bridged()`` fires on first ``_get_env_config()``
   call and, when ``terminal.cwd`` is in ``config.yaml`` and no launcher
   already set TERMINAL_CWD, the ``apply_terminal_config_to_env`` bridge
   fills in the profile default.

2. The parent gateway/dashboard process has already applied its own
   startup bridge (``hermes_cli/main.py`` calls
   ``apply_terminal_config_to_env()`` at dashboard/serve startup, which
   defaults to overriding when the file has a terminal section), so a
   worker's inherited ``TERMINAL_CWD`` already points at the profile
   default before the dispatcher's assignment fires.

3. A stale launcher-provided TERMINAL_CWD survives when the dispatcher
   skipped its own stamp because the workspace was not (yet) a valid
   directory.

``_ensure_kanban_workspace_pinned()`` runs on every ``_get_env_config()``
call and reinforces the invariant: for local kanban workers,
``TERMINAL_CWD`` is always the assigned ``HERMES_KANBAN_WORKSPACE``, and
an invalid assignment fails closed rather than silently falling back to
a profile directory.
"""

from __future__ import annotations

import os

import pytest

import tools.terminal_tool as terminal_tool
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with an un-attempted bridge and no leaked kanban env."""
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", False)
    for key in (
        "TERMINAL_ENV",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_IMAGE",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_TASK_ID",
        "HERMES_KANBAN_WORKSPACE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def _write_config_with_terminal_cwd(decoy_dir: str) -> None:
    """Mimic a profile with a real `terminal.cwd` set to a non-workspace dir."""
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "terminal:\n"
        "  backend: local\n"
        f"  cwd: {decoy_dir}\n",
    )


@pytest.mark.parametrize(
    "task_key", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID"]
)
def test_valid_workspace_wins_over_inherited_terminal_cwd(
    monkeypatch, tmp_path, task_key
):
    """Even when the parent bridged a profile default in, the worker's
    TERMINAL_CWD is re-anchored to its assigned worktree."""
    workspace = tmp_path / "assigned-worktree"
    decoy = tmp_path / "profile-default"
    workspace.mkdir()
    decoy.mkdir()

    # Simulate a launcher / parent that stamped a stale TERMINAL_CWD *and*
    # left TERMINAL_ENV=local set. The dispatcher's own spawn stamp would
    # normally overwrite TERMINAL_CWD, but we test the belt-and-braces path
    # where the worker itself re-asserts the invariant.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(decoy))
    monkeypatch.setenv(task_key, "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    config = terminal_tool._get_env_config()

    assert config["cwd"] == str(workspace)
    # Idempotent: a second resolution must not drift.
    config2 = terminal_tool._get_env_config()
    assert config2["cwd"] == str(workspace)
    # And the invariant is anchored via os.environ, not just the returned dict.
    assert os.environ["TERMINAL_CWD"] == str(workspace)


def test_valid_workspace_overrides_config_bridge_default(monkeypatch, tmp_path):
    """When no launcher set TERMINAL_ENV, the config bridge fires and
    would otherwise set TERMINAL_CWD to config's terminal.cwd. The kanban
    invariant runs AFTER the bridge and repoints to the workspace."""
    workspace = tmp_path / "assigned-worktree"
    decoy = tmp_path / "profile-default"
    workspace.mkdir()
    decoy.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_config_with_terminal_cwd(str(decoy))

    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    # Deliberately no TERMINAL_ENV / TERMINAL_CWD → force the bridge to
    # fire before the kanban invariant runs.

    config = terminal_tool._get_env_config()

    assert config["cwd"] == str(workspace)
    assert os.environ["TERMINAL_CWD"] == str(workspace)


@pytest.mark.parametrize(
    "invalid_workspace_factory,label",
    [
        (lambda tmp_path: "", "empty"),
        (lambda tmp_path: "relative/path", "relative"),
        (lambda tmp_path: str(tmp_path / "does-not-exist"), "nonexistent"),
        # File instead of directory: create a file and point at it.
        (
            lambda tmp_path: str(
                (tmp_path / "not-a-dir").write_text("x") or (tmp_path / "not-a-dir")
            ),
            "file",
        ),
    ],
)
def test_invalid_workspace_fails_closed_for_local_kanban_worker(
    monkeypatch, tmp_path, invalid_workspace_factory, label
):
    """Invalid workspace assignment MUST raise, never silently fall back to
    a profile / launcher-provided default. This is the fail-closed direction
    of the invariant."""
    decoy = tmp_path / "profile-default"
    decoy.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(decoy))  # would otherwise "win"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-worker")
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACE", invalid_workspace_factory(tmp_path)
    )

    with pytest.raises(ValueError, match="Invalid local kanban workspace"):
        terminal_tool._get_env_config()
    # Second call: still fails closed; not a one-shot bypass.
    with pytest.raises(ValueError, match="Invalid local kanban workspace"):
        terminal_tool._get_env_config()

    # And the stale TERMINAL_CWD from the parent is NEVER promoted to a
    # workspace-anchored value.
    assert os.environ["TERMINAL_CWD"] == str(decoy)


def test_nonkanban_local_worker_untouched(monkeypatch, tmp_path):
    """A non-kanban local process is untouched by the invariant."""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(other))

    config = terminal_tool._get_env_config()

    assert config["cwd"] == str(other)


def test_delegated_child_bypasses_invariant(monkeypatch, tmp_path):
    """A delegate_task subagent inherits the parent worker's HERMES_KANBAN_*
    env but is not the dispatcher's run owner. The workspace pin must not
    apply — it would otherwise make the subagent fail closed on the parent's
    workspace, which is not the subagent's concern."""
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    # No HERMES_KANBAN_WORKSPACE — the subagent code path does not need it.

    with delegated_child_context():
        # Must not raise despite the "invalid workspace".
        config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"


def test_nonlocal_backend_untouched(monkeypatch, tmp_path):
    """Container / remote backends map their own cwd; the invariant is
    strictly a local-backend concern."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-worker")
    # Even a bogus workspace assignment must NOT trip the invariant for
    # non-local backends (they route cwd inside the container).
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/does/not/exist")

    # Should not raise; docker's own default cwd applies.
    config = terminal_tool._get_env_config()
    assert config["env_type"] == "docker"

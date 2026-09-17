"""Exercise worker cwd selection through real config and registered file tools."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def probe(tmp_path, script, *, task_key=None, workspace_kind="directory", backend="local"):
    home, workspace, decoy = (tmp_path / name for name in ("home", "worktree", "profile-default"))
    for path in (home, workspace, decoy):
        path.mkdir()
    (home / "config.yaml").write_text(
        f"terminal:\n  backend: {backend}\n  cwd: {decoy}\n", encoding="utf-8"
    )
    env = os.environ.copy()
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID", "HERMES_KANBAN_WORKSPACE"):
        env.pop(key, None)
    env.update(HERMES_HOME=str(home), TERMINAL_ENV=backend, TERMINAL_CWD=str(workspace),
               PYTHONPATH=str(ROOT), TEST_WORKSPACE=str(workspace), TEST_DECOY=str(decoy))
    if task_key:
        env[task_key] = "test-worker"
        value = {"directory": str(workspace), "relative": "worktree", "missing": "",
                 "nonexistent": str(tmp_path / "absent"), "file": str(home / "config.yaml")}[workspace_kind]
        env["HERMES_KANBAN_WORKSPACE"] = value
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=workspace,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("task_key", [None, "HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID"])
def test_profile_defaults_do_not_redirect_local_worker_writes(tmp_path, task_key):
    probe(tmp_path, r'''
import json, os
from pathlib import Path
from tools.terminal_tool import _get_env_config
import tools.file_tools
from tools.registry import registry
expected = Path(os.environ["TEST_WORKSPACE"] if (
    os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_TASK_ID")
) else os.environ["TEST_DECOY"])
assert Path(_get_env_config()["cwd"]) == expected
assert Path(os.environ["TERMINAL_CWD"]) == expected
# A second resolution must retain the same result after the one-shot bridge.
assert Path(_get_env_config()["cwd"]) == expected
result = json.loads(registry.dispatch("write_file", {"path": "sentinel.txt", "content": "assigned\n"}, task_id="test-worker"))
assert not result.get("error"), result
assert (expected / "sentinel.txt").read_text() == "assigned\n"
other = Path(os.environ["TEST_DECOY"] if expected == Path(os.environ["TEST_WORKSPACE"]) else os.environ["TEST_WORKSPACE"])
assert not (other / "sentinel.txt").exists()
''', task_key=task_key)


@pytest.mark.parametrize("kind", ["missing", "relative", "nonexistent", "file"])
def test_invalid_local_workspace_never_falls_back_to_profile_directory(tmp_path, kind):
    probe(tmp_path, r'''
import json, os
from pathlib import Path
from tools.terminal_tool import _get_env_config
import tools.file_tools
from tools.registry import registry
for _ in range(2):
    try:
        _get_env_config()
    except ValueError as exc:
        assert str(exc) == "Invalid local kanban workspace"
    else:
        raise AssertionError("Invalid workspace silently fell back")
result = json.loads(registry.dispatch("write_file", {"path": "sentinel.txt", "content": "wrong\n"}, task_id="test-worker"))
assert result.get("error"), result
assert not (Path(os.environ["TEST_DECOY"]) / "sentinel.txt").exists()
assert not (Path(os.environ["TEST_WORKSPACE"]) / "sentinel.txt").exists()
''', task_key="HERMES_KANBAN_TASK", workspace_kind=kind)

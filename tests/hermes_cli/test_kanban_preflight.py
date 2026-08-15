from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _profile(home: Path, name: str = "worker") -> Path:
    profile = home / "profiles" / name
    (profile / "skills").mkdir(parents=True)
    (profile / "SOUL.md").write_text(
        "You are Worker, CEA.\n"
        "CERTIFICATION (binding): your certification is the `worker-cert`.\n",
        encoding="utf-8",
    )
    _skill(profile, "worker-cert")
    return profile


def _skill(profile: Path, name: str, frontmatter: str = "") -> None:
    skill = profile / "skills" / name
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n{frontmatter}---\n"
        "# Operating procedure\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n",
        encoding="utf-8",
    )


def _dispatch(home: Path, *, skill: str, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    profile = _profile(home)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(home),
            skills=[skill],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)
    assert task is not None
    return profile, task_id, result, task, events, runs, spawned


def test_missing_skill_fails_once_before_claim(kanban_home, monkeypatch):
    _, task_id, result, task, events, runs, spawned = _dispatch(
        kanban_home, skill="missing-skill", monkeypatch=monkeypatch,
    )

    assert result.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert runs == []
    assert spawned == []
    failures = [event for event in events if event.kind == "pre_dispatch_validation_failed"]
    assert len(failures) == 1
    assert failures[0].payload is not None
    assert failures[0].payload["action"] == "install_or_sync_required_skill"


def test_missing_certification_package_fails_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    (profile / "skills" / "worker-cert" / "SKILL.md").unlink()
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="certification preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"
    assert runs == []


def test_missing_credential_file_fails_once_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(
        profile,
        "credentialed",
        "required_credential_files:\n  - service-token.json\n",
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="credential preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["credentialed"],
        )
        first = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        second = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task is not None
    assert first.pre_dispatch_failed == [
        (task_id, "missing_required_credential_file"),
    ]
    assert second.pre_dispatch_failed == []
    assert task is not None
    assert task.status == "blocked"
    assert runs == []
    assert len([
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    ]) == 1


def test_wrong_node_fails_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(
        profile,
        "node-bound",
        "metadata:\n  hermes:\n    allowed_nodes:\n      - definitely-not-this-node\n",
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="node preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["node-bound"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "wrong_node")]
    assert task is not None
    assert task.status == "blocked"
    assert runs == []
    failure = next(
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    )
    assert failure.payload is not None
    assert failure.payload["action"] == "route_to_eligible_node"


def test_legacy_profile_without_soul_remains_dispatchable(kanban_home, monkeypatch):
    profile = kanban_home / "profiles" / "legacy"
    profile.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy profile preflight",
            assignee="legacy",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task.status == "running"


def test_valid_candidate_is_claimed_and_spawned(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(profile, "ready-skill")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="valid preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["ready-skill"],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task.status == "running"


def _canonical_skill(home: Path, name: str, *, category: str = "helios-agents", frontmatter: str = "") -> Path:
    """Install a golden skill package under the shared Hermes skills tree."""
    package = home / "skills" / category / name
    package.mkdir(parents=True, exist_ok=True)
    package.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: canonical golden copy\n{frontmatter}---\n"
        f"# Canonical {name}\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n",
        encoding="utf-8",
    )
    return package


def test_missing_certification_skill_is_repaired_from_canonical(kanban_home, monkeypatch):
    """Hollow/missing declared cert is installed from fleet golden copy; dispatch proceeds."""
    profile = _profile(kanban_home)
    # Remove the local certification package (simulates bianca-steele gap).
    import shutil

    shutil.rmtree(profile / "skills" / "worker-cert")
    _canonical_skill(kanban_home, "worker-cert")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="skill-sync repair",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"
    repaired = profile / "skills" / "helios-agents" / "worker-cert" / "SKILL.md"
    assert repaired.is_file()
    assert "Canonical worker-cert" in repaired.read_text(encoding="utf-8")


def test_hollow_skill_is_repaired_from_canonical(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    hollow = profile / "skills" / "worker-cert" / "SKILL.md"
    hollow.write_text(
        "---\nname: worker-cert\ndescription: hollow\n---\n# Empty\n",
        encoding="utf-8",
    )
    _canonical_skill(kanban_home, "worker-cert")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="hollow skill repair",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"
    body = hollow.read_text(encoding="utf-8")
    assert "Canonical worker-cert" in body


def test_missing_skill_without_canonical_still_fails_closed(kanban_home, monkeypatch):
    """No golden source anywhere → still missing_required_skill (no invented policy)."""
    profile = _profile(kanban_home)
    # Certificate present; task skill has no local or canonical package.
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="unknown skill fail-closed",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["never-seen-ssc-cert"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"
    assert not (profile / "skills" / "never-seen-ssc-cert").exists()
    failure = next(
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    )
    assert failure.payload is not None
    assert failure.payload["requirement"] == "never-seen-ssc-cert"


def test_wrong_platform_skill_is_not_overwritten_by_sync(kanban_home, monkeypatch):
    """Present wrong-platform packages are routing failures — not silently replaced."""
    profile = _profile(kanban_home)
    original = (
        "---\nname: platform-bound\ndescription: local\nplatforms: [definitely-not-a-real-os]\n---\n"
        "# Local platform-bound package\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n"
    )
    skill_dir = profile / "skills" / "platform-bound"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(original, encoding="utf-8")
    # Golden would "fix" platform if installed — must not clobber the local copy.
    _canonical_skill(
        kanban_home,
        "platform-bound",
        frontmatter="",  # no platforms → all platforms
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="wrong platform no overwrite",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["platform-bound"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "wrong_node_platform")]
    assert task is not None
    assert task.status == "blocked"
    assert skill_path.read_text(encoding="utf-8") == original
    failure = next(
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    )
    assert failure.payload is not None
    assert failure.payload["action"] == "route_to_eligible_node"


def test_symlink_skill_package_is_visible_to_preflight(kanban_home, monkeypatch, tmp_path):
    """A dest that is a directory symlink is indexed the same way stock Hermes lists it."""
    profile = _profile(kanban_home)
    golden = tmp_path / "golden-hal"
    golden.mkdir()
    golden.joinpath("SKILL.md").write_text(
        "---\nname: helios-activity-ledger\ndescription: test\n---\n"
        "# Operating procedure\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n",
        encoding="utf-8",
    )
    dest = profile / "skills" / "helios-activity-ledger"
    dest.symlink_to(golden, target_is_directory=True)
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="symlink skill visible",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["helios-activity-ledger"],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"


def test_real_package_wins_over_earlier_symlink(kanban_home):
    """A later real package replaces a first-wins symlink dest of the same name."""
    from hermes_cli.kanban_preflight import _profile_skill_files

    profile = _profile(kanban_home)
    hollow = kanban_home / "hollow-hal"
    hollow.mkdir()
    hollow.joinpath("SKILL.md").write_text(
        "---\nname: helios-activity-ledger\ndescription: hollow\n---\n# Empty\n",
        encoding="utf-8",
    )
    (profile / "skills" / "zzz-early").mkdir()
    (profile / "skills" / "zzz-early" / "helios-activity-ledger").symlink_to(
        hollow, target_is_directory=True
    )
    real = profile / "skills" / "aaa-real" / "helios-activity-ledger"
    real.mkdir(parents=True)
    real.joinpath("SKILL.md").write_text(
        "---\nname: helios-activity-ledger\ndescription: real\n---\n"
        "# Operating procedure\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n",
        encoding="utf-8",
    )
    indexed = _profile_skill_files(profile)
    chosen = indexed["helios-activity-ledger"]
    assert chosen == real / "SKILL.md"
    assert not chosen.parent.is_symlink()


def test_binding_cert_is_optional_unless_configured(kanban_home, monkeypatch):
    """Stock default: a named profile with SOUL but no cert still dispatches."""
    profile = _profile(kanban_home)
    (profile / "SOUL.md").write_text("You are Worker, CEA.\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="optional cert",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"


def test_binding_cert_required_when_configured(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    (profile / "SOUL.md").write_text("You are Worker, CEA.\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.kanban_preflight._kanban_setting",
        lambda name, default=None: True if name == "require_binding_certification" else default,
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="required cert",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "missing_profile_certification")]
    assert task is not None
    assert task.status == "blocked"


def test_capability_block_revalidates_when_configured(kanban_home, monkeypatch):
    """After a profile repair, a capability-blocked card can promote again."""
    profile = _profile(kanban_home)
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="heal after repair",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["missing-then-installed"],
        )
        first = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
    assert first.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"

    _skill(profile, "missing-then-installed")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"revalidate_capability_blocks": True}},
    )
    with kb.connect() as conn:
        spawned = []
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert second.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"


def test_org_root_harness_keys_win_when_hermes_home_is_a_profile(
    kanban_home, monkeypatch, tmp_path
):
    """HELIos policy lives once at ~/.hermes/config.yaml — not on each seat."""
    import yaml

    from hermes_cli.kanban_preflight import _kanban_setting

    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "kanban": {
                    "require_binding_certification": True,
                    "default_parent_link_type": "gates",
                    "revalidate_capability_blocks": True,
                }
            }
        ),
        encoding="utf-8",
    )
    profile_home = kanban_home / "profiles" / "01-max-headroom"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "kanban": {
                    "require_binding_certification": False,
                    "default_parent_link_type": "",
                    "revalidate_capability_blocks": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert _kanban_setting("require_binding_certification", False) is True
    assert _kanban_setting("default_parent_link_type") == "gates"
    assert _kanban_setting("revalidate_capability_blocks", False) is True


def test_create_task_uses_org_root_parent_link_when_profile_omits_it(
    kanban_home, monkeypatch
):
    import yaml

    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": {"default_parent_link_type": "gates"}}),
        encoding="utf-8",
    )
    profile_home = kanban_home / "profiles" / "paul-park"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="programme")
        child = kb.create_task(conn, title="writer", parents=[parent])
        row = conn.execute(
            "SELECT link_type FROM task_links WHERE parent_id=? AND child_id=?",
            (parent, child),
        ).fetchone()
    assert row is not None
    assert row["link_type"] == "gates"


def test_hollow_symlink_dest_is_overwritten_from_canonical(kanban_home, monkeypatch, tmp_path):
    """Install must replace a dest that is a directory symlink, not walk it."""
    profile = _profile(kanban_home)
    golden_outside = tmp_path / "shared-golden"
    golden_outside.mkdir()
    golden_outside.joinpath("SKILL.md").write_text(
        "---\nname: worker-cert\ndescription: hollow shared\n---\n# Empty\n",
        encoding="utf-8",
    )
    dest = profile / "skills" / "worker-cert"
    import shutil

    shutil.rmtree(dest)
    dest.symlink_to(golden_outside, target_is_directory=True)
    _canonical_skill(kanban_home, "worker-cert")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="overwrite hollow symlink dest",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task is not None
    assert task.status == "running"
    assert dest.is_dir() and not dest.is_symlink()
    assert "Canonical worker-cert" in (dest / "SKILL.md").read_text(encoding="utf-8")
    leftover = golden_outside.joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "Canonical worker-cert" not in leftover
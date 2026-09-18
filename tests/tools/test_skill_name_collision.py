"""H3 / MCP-INC-003: installer and resolver refuse duplicate logical names.

Two skill directories claiming the same SKILL.md ``name:`` frontmatter cannot
coexist. ``skill_view`` already refuses to guess between ambiguous candidates
at resolve time (the "collision cannot preload" invariant is enforced there).
This test file pins BOTH sides:

* installer — ``install_from_quarantine`` refuses to land a second directory
  that claims a logical name already claimed by an installed skill (fail
  closed with ``SkillNameCollisionError``);
* resolver — ``skill_view`` returns a structured collision error when two
  installed directories already claim the same logical name (belt-and-braces
  for legacy trees that predate the installer refusal);
* helper — ``find_skill_name_collisions`` surfaces the collision map for
  doctor / operator inspection.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_skill(dir_path: Path, name: str, description: str = "A skill.") -> Path:
    """Materialize a minimal well-formed SKILL.md at *dir_path*."""
    dir_path.mkdir(parents=True, exist_ok=True)
    md = dir_path / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nbody.\n",
        encoding="utf-8",
    )
    return md


@pytest.fixture
def hub_env(tmp_path):
    """Isolated skills root with the hub directory tree prepared."""
    import tools.skills_hub as hub

    skills_dir = tmp_path / "skills"
    quarantine_root = skills_dir / ".hub" / "quarantine"
    quarantine_root.mkdir(parents=True)
    with (
        patch.object(hub, "SKILLS_DIR", skills_dir),
        patch.object(hub, "QUARANTINE_DIR", quarantine_root),
        patch.object(hub, "HUB_DIR", skills_dir / ".hub"),
        patch.object(hub, "LOCK_FILE", skills_dir / ".hub" / "lock.json"),
        patch.object(hub, "AUDIT_LOG", skills_dir / ".hub" / "audit.log"),
        patch.object(hub, "TAPS_FILE", skills_dir / ".hub" / "taps.json"),
        patch.object(hub, "INDEX_CACHE_DIR", skills_dir / ".hub" / "index-cache"),
    ):
        yield skills_dir, quarantine_root


def _make_bundle_and_scan(name):
    from tools.skills_guard import ScanResult
    import tools.skills_hub as hub

    bundle = hub.SkillBundle(
        name=name,
        files={"SKILL.md": f"---\nname: {name}\n---\n"},
        source="community",
        identifier="test-id",
        trust_level="community",
    )
    scan_result = ScanResult(
        skill_name=name,
        source="community",
        trust_level="community",
        verdict="safe",
    )
    return bundle, scan_result


# ── Helper: owner map + collisions surface ───────────────────────────

def test_owner_map_returns_all_declared_names(hub_env):
    import tools.skills_hub as hub

    skills_dir, _ = hub_env
    _write_skill(skills_dir / "alpha", "alpha")
    _write_skill(skills_dir / "beta", "beta")
    _write_skill(skills_dir / "category" / "gamma", "gamma")

    owners = hub.find_installed_logical_name_owners()

    assert set(owners) == {"alpha", "beta", "gamma"}
    for paths in owners.values():
        assert len(paths) == 1


def test_owner_map_falls_back_to_directory_name_when_no_frontmatter(hub_env):
    import tools.skills_hub as hub

    skills_dir, _ = hub_env
    (skills_dir / "no-fm").mkdir(parents=True)
    (skills_dir / "no-fm" / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

    owners = hub.find_installed_logical_name_owners()

    assert owners["no-fm"][0].parent.name == "no-fm"


def test_owner_map_ignores_hub_and_archive_paths(hub_env):
    import tools.skills_hub as hub

    skills_dir, _ = hub_env
    # A stray SKILL.md under .hub / .archive must not participate in
    # logical-name resolution — those are Hermes-internal metadata paths.
    _write_skill(skills_dir / ".hub" / "leftover-quarantine", "phantom")
    _write_skill(skills_dir / ".archive" / "old-copy", "phantom")
    _write_skill(skills_dir / "real", "real")

    owners = hub.find_installed_logical_name_owners()

    assert "phantom" not in owners
    assert "real" in owners


def test_find_collisions_returns_only_multi_owner_names(hub_env):
    import tools.skills_hub as hub

    skills_dir, _ = hub_env
    _write_skill(skills_dir / "alpha", "council-gateway-verdict")
    _write_skill(skills_dir / "community" / "beta", "council-gateway-verdict")
    _write_skill(skills_dir / "unique", "unique-name")

    collisions = hub.find_skill_name_collisions()

    assert set(collisions) == {"council-gateway-verdict"}
    assert len(collisions["council-gateway-verdict"]) == 2


# ── Installer refusal ────────────────────────────────────────────────

def test_installer_refuses_colliding_logical_name(hub_env):
    """Two directories, same logical name → fail closed at install time."""
    import tools.skills_hub as hub

    skills_dir, quarantine_root = hub_env
    # An already-installed skill under `skills/existing/foo` claims
    # `name: council-gateway-verdict`.
    _write_skill(skills_dir / "existing" / "foo", "council-gateway-verdict")
    # A NEW bundle wants to install into `skills/community/bar` with the
    # same logical name.
    q = quarantine_root / "council-gateway-verdict"
    q.mkdir()
    (q / "SKILL.md").write_text(
        "---\nname: council-gateway-verdict\n---\n", encoding="utf-8"
    )
    bundle, scan = _make_bundle_and_scan("council-gateway-verdict")

    with pytest.raises(hub.SkillNameCollisionError) as excinfo:
        hub.install_from_quarantine(q, "council-gateway-verdict", "community", bundle, scan)

    err = excinfo.value
    assert err.skill_name == "council-gateway-verdict"
    assert err.existing_path == skills_dir / "existing" / "foo" / "SKILL.md"
    # The would-be install target was never created — nothing landed.
    assert not (skills_dir / "community" / "council-gateway-verdict").exists()


def test_installer_allows_reinstall_of_same_logical_skill(hub_env):
    """Overwriting the same install target is a re-install, not a collision."""
    import tools.skills_hub as hub

    skills_dir, quarantine_root = hub_env
    # Existing install at the SAME target the new bundle will land in.
    _write_skill(skills_dir / "community" / "foo", "foo")
    q = quarantine_root / "foo"
    q.mkdir()
    (q / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
    bundle, scan = _make_bundle_and_scan("foo")

    result = hub.install_from_quarantine(q, "foo", "community", bundle, scan)

    assert result == skills_dir / "community" / "foo"
    assert (result / "SKILL.md").exists()


def test_installer_refuses_when_incoming_frontmatter_diverges_from_skill_name(hub_env):
    """SKILL.md frontmatter is the LOGICAL name that resolvers use. A bundle
    whose directory name is unique but frontmatter matches an existing skill
    must still fail closed — otherwise skill_view() collides at load time."""
    import tools.skills_hub as hub

    skills_dir, quarantine_root = hub_env
    _write_skill(skills_dir / "official" / "verdict", "council-gateway-verdict")

    q = quarantine_root / "wrapper-bundle"
    q.mkdir()
    # Directory is `wrapper-bundle`, but the LOGICAL name inside is the
    # already-claimed `council-gateway-verdict`.
    (q / "SKILL.md").write_text(
        "---\nname: council-gateway-verdict\n---\n", encoding="utf-8"
    )
    bundle, scan = _make_bundle_and_scan("wrapper-bundle")

    with pytest.raises(hub.SkillNameCollisionError) as excinfo:
        hub.install_from_quarantine(q, "wrapper-bundle", "community", bundle, scan)

    assert excinfo.value.skill_name == "council-gateway-verdict"
    # The would-be target directory is NOT the previous one, so this counts
    # as a collision even though the on-disk names differ.
    assert not (skills_dir / "community" / "wrapper-bundle").exists()


# ── Resolver: collision cannot preload ───────────────────────────────

def test_resolver_refuses_to_load_colliding_logical_name(hub_env, monkeypatch):
    """When two directories already claim the same logical name (e.g. legacy
    trees that predate the installer refusal), ``skill_view`` must refuse
    to guess between them: the collision cannot preload."""
    import tools.skills_tool as skills_tool

    skills_dir, _ = hub_env
    _write_skill(skills_dir / "alpha", "council-gateway-verdict")
    _write_skill(skills_dir / "community" / "beta", "council-gateway-verdict")

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: skills_dir)

    payload = json.loads(skills_tool.skill_view("council-gateway-verdict"))

    assert payload["success"] is False
    assert "Ambiguous skill name" in payload["error"]
    assert len(payload.get("matches") or []) >= 2

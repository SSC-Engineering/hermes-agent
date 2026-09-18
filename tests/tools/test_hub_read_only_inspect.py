"""H4 / MCP-INC-004: read-only / inspect hub ops must not mutate the FS.

``hermes skills inspect`` (and the dashboard's programmatic preview) is a
read-only preview affordance. The historical implementation happily seeded
``~/.hermes/skills/.hub`` + subdirectories, wrote cache entries, and created
``lock.json`` / ``audit.log`` / ``taps.json`` while previewing a skill —
silently mutating the inspect target's disk state so a doctor / operator
running "look, don't touch" could no longer distinguish "never queried"
from "seeded on inspect".

``hub_read_only()`` gates every hub mutation site: cache writes become
no-ops, and installer / quarantine / lock / taps / audit / ``ensure_hub_dirs``
paths raise ``HubReadOnlyViolation``. Read paths (`_read_index_cache`,
source ``inspect`` / ``fetch`` HTTP calls, ``HubLockFile.load``) still work.

The tests here pin the failure surface:

* every mutating entry point raises under the guard;
* cache writes become no-ops (do not surface as errors, but leave no trace);
* the guard is per-context (nested exit restores prior state);
* absent and pre-existing hub state stays byte-identical after an inspect;
* the guard does NOT block reads.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import patch

import pytest


def _tree_snapshot(root: Path) -> Dict[str, Tuple[int, str]]:
    """Return ``{relpath: (size, sha256)}`` for every file under root.

    Directories are included as ``(-1, "")`` sentinels so a pure ``mkdir``
    also shows up as a diff. Missing root → empty dict.
    """
    snapshot: Dict[str, Tuple[int, str]] = {}
    if not root.exists():
        return snapshot
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            snapshot[rel_dir + "/"] = (-1, "")
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                data = p.read_bytes()
            except OSError:
                continue
            rel = os.path.relpath(p, root)
            snapshot[rel] = (len(data), hashlib.sha256(data).hexdigest())
    return snapshot


@pytest.fixture
def hub_fs(tmp_path):
    """Isolated skills root; hub-dir paths point inside tmp_path."""
    import tools.skills_hub as hub

    skills_dir = tmp_path / "skills"
    hub_dir = skills_dir / ".hub"
    with (
        patch.object(hub, "SKILLS_DIR", skills_dir),
        patch.object(hub, "HUB_DIR", hub_dir),
        patch.object(hub, "LOCK_FILE", hub_dir / "lock.json"),
        patch.object(hub, "QUARANTINE_DIR", hub_dir / "quarantine"),
        patch.object(hub, "AUDIT_LOG", hub_dir / "audit.log"),
        patch.object(hub, "TAPS_FILE", hub_dir / "taps.json"),
        patch.object(hub, "INDEX_CACHE_DIR", hub_dir / "index-cache"),
    ):
        yield skills_dir


# ── The guard blocks every mutating entry point ──────────────────────

def test_ensure_hub_dirs_refuses_under_read_only(hub_fs):
    import tools.skills_hub as hub

    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="hub directory tree"):
            hub.ensure_hub_dirs()

    # And absolutely nothing was created.
    assert not hub_fs.exists() or _tree_snapshot(hub_fs) == {}


def test_quarantine_bundle_refuses_under_read_only(hub_fs):
    import tools.skills_hub as hub

    bundle = hub.SkillBundle(
        name="preview-only",
        files={"SKILL.md": "---\nname: preview-only\n---\n"},
        source="community",
        identifier="test-id",
        trust_level="community",
    )
    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="quarantine"):
            hub.quarantine_bundle(bundle)

    assert not hub_fs.exists() or _tree_snapshot(hub_fs) == {}


def test_install_from_quarantine_refuses_under_read_only(hub_fs, tmp_path):
    import tools.skills_hub as hub
    from tools.skills_guard import ScanResult

    q_dir = tmp_path / "skills" / ".hub" / "quarantine" / "pending"
    q_dir.mkdir(parents=True)
    (q_dir / "SKILL.md").write_text("---\nname: pending\n---\n", encoding="utf-8")

    bundle = hub.SkillBundle(
        name="pending",
        files={"SKILL.md": "---\nname: pending\n---\n"},
        source="community",
        identifier="x",
        trust_level="community",
    )
    scan = ScanResult(
        skill_name="pending",
        source="community",
        trust_level="community",
        verdict="safe",
    )

    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="install"):
            hub.install_from_quarantine(q_dir, "pending", "", bundle, scan)


def test_lock_file_save_refuses_under_read_only(hub_fs):
    import tools.skills_hub as hub

    lock = hub.HubLockFile()
    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="lock"):
            lock.save({"version": 1, "installed": {}})


def test_taps_save_refuses_under_read_only(hub_fs):
    import tools.skills_hub as hub

    taps = hub.TapsManager()
    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="taps"):
            taps.save([])


def test_audit_log_refuses_under_read_only(hub_fs):
    import tools.skills_hub as hub

    with hub.hub_read_only():
        with pytest.raises(hub.HubReadOnlyViolation, match="audit"):
            hub.append_audit_log("INSPECT", "x", "src", "community", "safe")


# ── Cache writes are silent no-ops under the guard ───────────────────

def test_write_index_cache_is_noop_under_read_only(hub_fs):
    """Cache writes silently no-op (never raise) so a source that
    speculatively caches after a fetch does not blow up an inspect."""
    import tools.skills_hub as hub

    with hub.hub_read_only():
        hub._write_index_cache("some-key", {"cached": "value"})

    # Cache dir was never created.
    assert not (hub_fs / ".hub" / "index-cache").exists()

    # ...but the same call OUTSIDE the guard still writes.
    hub._write_index_cache("some-key", {"cached": "value"})
    assert (hub_fs / ".hub" / "index-cache" / "some-key.json").is_file()


# ── The guard is per-context and nests correctly ─────────────────────

def test_guard_is_scoped_to_the_context(hub_fs):
    import tools.skills_hub as hub

    assert hub.is_hub_read_only() is False
    with hub.hub_read_only():
        assert hub.is_hub_read_only() is True
    assert hub.is_hub_read_only() is False


def test_guard_nests_and_restores_prior_state(hub_fs):
    import tools.skills_hub as hub

    with hub.hub_read_only():
        assert hub.is_hub_read_only() is True
        with hub.hub_read_only():
            assert hub.is_hub_read_only() is True
        assert hub.is_hub_read_only() is True
    assert hub.is_hub_read_only() is False


# ── Read paths still work ────────────────────────────────────────────

def test_read_paths_still_work_under_guard(hub_fs):
    """`HubLockFile.load` and `_read_index_cache` must not be gated."""
    import tools.skills_hub as hub

    lock = hub.HubLockFile()
    with hub.hub_read_only():
        # Empty state read = default lock payload; no fs write.
        data = lock.load()
        assert data == {"version": 1, "installed": {}}
        # Missing cache entry = None; no fs write.
        assert hub._read_index_cache("never-cached") is None


# ── Exit criteria: fs stays byte-identical after an inspect ──────────

def test_inspect_leaves_absent_hub_state_unchanged(hub_fs, monkeypatch):
    """The plan's exit criterion: 'Existing and absent targets remain
    unchanged during inspection.' Simulate a full-fidelity inspect: fake
    a source router that returns metadata (no bundle), and prove nothing
    was created under skills/ afterward.
    """
    import hermes_cli.skills_hub as cli_hub

    calls = {"fetch": 0, "inspect": 0}

    class _FakeSkillMeta:
        def __init__(self):
            self.name = "preview-only"
            self.description = "read-only"
            self.source = "community"
            self.trust_level = "community"
            self.identifier = "gh/example/preview-only"
            self.tags = []
            self.extra = {}

    class _FakeSource:
        def inspect(self, identifier):
            calls["inspect"] += 1
            return _FakeSkillMeta()

        def fetch(self, identifier):
            calls["fetch"] += 1
            # Return None so no bundle-write attempt fires; the inspect path
            # is still fully exercised.
            return None

    def _fake_router(auth=None):
        return [_FakeSource()]

    monkeypatch.setattr(cli_hub, "_console", type("Q", (), {"print": lambda *a, **k: None})())
    monkeypatch.setattr("tools.skills_hub.create_source_router", _fake_router)

    before = _tree_snapshot(hub_fs)
    cli_hub.do_inspect("gh/example/preview-only")
    after = _tree_snapshot(hub_fs)

    assert calls["inspect"] >= 1
    assert before == after, f"inspect mutated skills tree: {sorted(set(after) - set(before))}"


def test_inspect_leaves_existing_hub_state_unchanged(hub_fs, monkeypatch):
    """A pre-existing hub tree with lock / audit / taps content must stay
    byte-identical after the inspect."""
    import hermes_cli.skills_hub as cli_hub
    import tools.skills_hub as hub

    hub_dir = hub_fs / ".hub"
    hub_dir.mkdir(parents=True)
    (hub_dir / "lock.json").write_text('{"version": 1, "installed": {"pre": {}}}\n')
    (hub_dir / "audit.log").write_text("2026-09-18T00:00:00Z PRE existing\n")
    (hub_dir / "taps.json").write_text('{"taps": [{"repo": "org/repo"}]}\n')
    (hub_dir / "index-cache").mkdir()
    (hub_dir / "index-cache" / "seed.json").write_text('{"cached": true}\n')

    class _FakeSkillMeta:
        def __init__(self):
            self.name = "preview-only"
            self.description = ""
            self.source = "community"
            self.trust_level = "community"
            self.identifier = "gh/example/preview-only"
            self.tags = []
            self.extra = {}

    class _FakeSource:
        def inspect(self, _):
            return _FakeSkillMeta()

        def fetch(self, _):
            return None

    monkeypatch.setattr(cli_hub, "_console", type("Q", (), {"print": lambda *a, **k: None})())
    monkeypatch.setattr("tools.skills_hub.create_source_router", lambda auth=None: [_FakeSource()])

    before = _tree_snapshot(hub_fs)
    cli_hub.do_inspect("gh/example/preview-only")
    after = _tree_snapshot(hub_fs)

    assert before == after, (
        "inspect mutated pre-existing hub state; diff:\n"
        f"  added:   {sorted(set(after) - set(before))}\n"
        f"  removed: {sorted(set(before) - set(after))}"
    )

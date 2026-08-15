"""Tests for external skill directories (skills.external_dirs config)."""

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def external_skills_dir(tmp_path):
    """Create a temp dir with a sample external skill."""
    ext_dir = tmp_path / "external-skills"
    skill_dir = ext_dir / "my-external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-external-skill\ndescription: A skill from an external directory\n---\n\n# My External Skill\n\nDo external things.\n"
    )
    return ext_dir


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Create a minimal HERMES_HOME with config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HELIOS_AGENTIC_OS", raising=False)
    monkeypatch.delenv("HELIOS_REPO", raising=False)
    from agent.skill_utils import _external_dirs_cache_clear

    _external_dirs_cache_clear()
    yield home
    _external_dirs_cache_clear()


class TestGetExternalSkillsDirs:
    def test_empty_config(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  external_dirs: []\n")
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HELIOS_AGENTIC_OS": "",
                "HELIOS_REPO": "",
            },
        ):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert result == []


    def test_valid_dir_returned(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert len(result) == 1
        assert result[0] == external_skills_dir.resolve()






class TestGetAllSkillsDirs:
    def test_local_always_first(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_all_skills_dirs
            result = get_all_skills_dirs()
        assert result[0] == hermes_home / "skills"
        assert result[1] == external_skills_dir.resolve()


class TestExternalSkillsInFindAll:
    def test_external_skills_found(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        names = [s["name"] for s in skills]
        assert "my-external-skill" in names

    def test_local_takes_precedence(self, hermes_home, external_skills_dir):
        """If the same skill name exists locally and externally, local wins."""
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "my-external-skill"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: my-external-skill\ndescription: Local version\n---\n\nLocal.\n"
        )
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        matching = [s for s in skills if s["name"] == "my-external-skill"]
        assert len(matching) == 1
        assert matching[0]["description"] == "Local version"


class TestExternalSkillView:
    def test_skill_view_finds_external(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("my-external-skill"))
        assert result["success"] is True
        assert "external things" in result["content"]


def _write_skill(root, rel, name, description):
    skill_dir = root / rel
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    )
    return skill_dir


class TestHeliosRootSkillDirs:
    def test_helios_root_expands_existing_trees(self, hermes_home, tmp_path):
        checkout = tmp_path / "HELIOS-AGENTIC-OS"
        helios_skills = checkout / "HELIos" / "skills"
        cursor_skills = checkout / ".cursor" / "skills"
        _write_skill(helios_skills, "prd-to-production", "helios-prd-to-production", "HELIos")
        _write_skill(cursor_skills, "helios-cortex-routing", "helios-cortex-routing", "cursor")
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  helios_root: {checkout}\n"
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HELIOS_AGENTIC_OS": "",
                "HELIOS_REPO": "",
            },
        ):
            from agent.skill_utils import (
                _external_dirs_cache_clear,
                get_external_skills_dirs,
            )

            _external_dirs_cache_clear()
            result = get_external_skills_dirs()
        assert helios_skills.resolve() in result
        assert cursor_skills.resolve() in result
        assert not any(p.as_posix().endswith(".agents/skills") for p in result)

    def test_helios_env_without_config_key(self, hermes_home, tmp_path):
        checkout = tmp_path / "HELIOS-AGENTIC-OS"
        helios_skills = checkout / "HELIos" / "skills"
        _write_skill(helios_skills, "ssc-dan-final-approver", "ssc-dan-final-approver", "env")
        (hermes_home / "config.yaml").write_text("skills:\n  external_dirs: []\n")
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HELIOS_AGENTIC_OS": str(checkout),
                "HELIOS_REPO": "",
            },
        ):
            from agent.skill_utils import (
                _external_dirs_cache_clear,
                get_external_skills_dirs,
            )

            _external_dirs_cache_clear()
            result = get_external_skills_dirs()
        assert result == [helios_skills.resolve()]

    def test_config_helios_root_wins_over_env(self, hermes_home, tmp_path):
        configured = tmp_path / "configured-helios"
        env_checkout = tmp_path / "env-helios"
        configured_skills = configured / "HELIos" / "skills"
        env_skills = env_checkout / "HELIos" / "skills"
        _write_skill(configured_skills, "from-config", "from-config", "config")
        _write_skill(env_skills, "from-env", "from-env", "env")
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  helios_root: {configured}\n"
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HELIOS_AGENTIC_OS": str(env_checkout),
                "HELIOS_REPO": "",
            },
        ):
            from agent.skill_utils import (
                _external_dirs_cache_clear,
                get_external_skills_dirs,
            )

            _external_dirs_cache_clear()
            result = get_external_skills_dirs()
        assert result == [configured_skills.resolve()]

    def test_scan_registers_helios_only_skill(self, hermes_home, tmp_path):
        checkout = tmp_path / "HELIOS-AGENTIC-OS"
        helios_skills = checkout / "HELIos" / "skills"
        _write_skill(
            helios_skills,
            "software-development/ssc-dan-final-approver",
            "ssc-dan-final-approver",
            "HELIos-only",
        )
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  helios_root: {checkout}\n"
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HELIOS_AGENTIC_OS": "",
                "HELIOS_REPO": "",
            },
        ):
            from agent.skill_commands import scan_skill_commands
            from agent.skill_utils import _external_dirs_cache_clear

            _external_dirs_cache_clear()
            commands = scan_skill_commands()
        assert "/ssc-dan-final-approver" in commands

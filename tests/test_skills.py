from pathlib import Path
from unittest.mock import patch

import bub.configure as configure
from bub.channels.telegram import TelegramSettings
from bub.skills import (
    SKILL_FILE_NAME,
    SkillMetadata,
    _parse_frontmatter,
    _read_skill,
    discover_skills,
    render_skills_prompt,
)


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "A skill",
    body: str = "Skill body",
    metadata: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if metadata is not None:
        lines.append("metadata:")
        for key, value in metadata.items():
            lines.append(f"  {key}: {value}")
    lines.extend(["---", body])
    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text("\n".join(lines), encoding="utf-8")
    return skill_file


def test_skill_metadata_body_strips_frontmatter(tmp_path: Path) -> None:
    skill_file = _write_skill(tmp_path, "demo-skill", body="Line 1\nLine 2")
    metadata = SkillMetadata(
        name="demo-skill",
        description="Demo",
        location=skill_file,
        source="project",
    )
    assert metadata.body() == "Line 1\nLine 2"


def test_skill_metadata_body_renders_config_templates(tmp_path: Path, load_config) -> None:
    assert TelegramSettings.__name__ == "TelegramSettings"
    skill_file = _write_skill(
        tmp_path,
        "demo-skill",
        body='Token: "${config.telegram.token}"\nSkill dir: $SKILL_DIR',
    )
    metadata = SkillMetadata(
        name="demo-skill",
        description="Demo",
        location=skill_file,
        source="project",
    )

    with patch.dict("os.environ", {}, clear=True):
        load_config(
            """
telegram:
  token: yaml-token
""".strip(),
        )

        body = metadata.body()

    assert 'Token: "yaml-token"' in body
    assert f"Skill dir: {tmp_path / 'demo-skill'}" in body


def test_skill_metadata_body_renders_env_over_config(tmp_path: Path, load_config) -> None:
    assert TelegramSettings.__name__ == "TelegramSettings"
    skill_file = _write_skill(tmp_path, "demo-skill", body='Token: "${config.telegram.token}"')
    metadata = SkillMetadata(
        name="demo-skill",
        description="Demo",
        location=skill_file,
        source="project",
    )
    load_config(
        """
telegram:
  token: yaml-token
""".strip(),
    )

    with patch.dict("os.environ", {"BUB_TELEGRAM_TOKEN": "env-token"}, clear=True):
        configure._global_config.clear()

        assert metadata.body() == 'Token: "env-token"'


def test_read_skill_rejects_invalid_metadata_field_type(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    content = "---\nname: bad-skill\ndescription: bad\nmetadata:\n  retries: 3\n---\nBody\n"
    (skill_dir / SKILL_FILE_NAME).write_text(content, encoding="utf-8")

    assert _read_skill(skill_dir, source="project") is None


def test_parse_frontmatter_returns_empty_on_invalid_yaml() -> None:
    content = "---\nname: [broken\n---\nbody\n"
    assert _parse_frontmatter(content) == {}


def test_discover_skills_prefers_project_over_global_and_builtin(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    global_root = tmp_path / "global"
    builtin_root = tmp_path / "builtin"
    for root in (project_root, global_root, builtin_root):
        root.mkdir(parents=True)

    _write_skill(project_root, "shared", description="project version")
    _write_skill(global_root, "shared", description="global version")
    _write_skill(builtin_root, "shared", description="builtin version")
    _write_skill(global_root, "global-only", description="global only")

    monkeypatch.setattr(
        "bub.skills._iter_skill_roots",
        lambda _workspace: [
            (project_root, "project"),
            (global_root, "global"),
            (builtin_root, "builtin"),
        ],
    )

    discovered = discover_skills(tmp_path)
    index = {item.name: item for item in discovered}
    assert index["shared"].description == "project version"
    assert index["shared"].source == "project"
    assert index["global-only"].source == "global"


def test_render_skills_prompt_includes_expanded_body(tmp_path: Path) -> None:
    skill_file = _write_skill(tmp_path, "skill-a", description="desc", body="expanded body")
    skills = [
        SkillMetadata(name="skill-a", description="desc", location=skill_file, source="project"),
        SkillMetadata(name="skill-b", description="desc-b", location=skill_file, source="project"),
    ]

    rendered = render_skills_prompt(skills, expanded_skills={"skill-a"})
    assert "<available_skills>" in rendered
    assert "- skill-a: desc" in rendered
    assert "expanded body" in rendered
    assert "- skill-b: desc-b" in rendered

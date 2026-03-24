from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from search_agent.dynamic_skills.registry import DynamicToolRegistry


def _set_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def test_allowlist_blocks_non_whitelisted_tool(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "blocked"
    skill_dir.mkdir(parents=True)

    tool_path = skill_dir / "tool.py"
    tool_path.write_text(
        "def run(**kwargs):\n"
        "    return kwargs\n",
        encoding="utf-8",
    )

    tool_hash = hashlib.sha256(tool_path.read_bytes()).hexdigest()
    manifest = {
        "name": "blocked_tool",
        "package_version": "1.0.0",
        "package_files_sha256": {"tool.py": tool_hash},
        "entrypoint": "tool.py",
        "function": "run",
        "schema": {"type": "object", "properties": {}, "required": []},
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True), encoding="utf-8")

    old_allowlist = os.environ.get("DYNAMIC_TOOLS_ALLOWLIST")
    try:
        _set_env("DYNAMIC_TOOLS_ALLOWLIST", "allowed_tool")
        registry = DynamicToolRegistry(skills_root)
        tools = registry.refresh()
        assert tools == {}
        assert "blocked" in registry.errors
        assert "blocked by DYNAMIC_TOOLS_ALLOWLIST" in registry.errors["blocked"]
    finally:
        _set_env("DYNAMIC_TOOLS_ALLOWLIST", old_allowlist)


def test_argument_policy_rejects_unexpected_field() -> None:
    registry = DynamicToolRegistry(Path("skills"))
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    try:
        registry._enforce_argument_policy(
            tool_name="demo_tool",
            schema=schema,
            arguments={"query": "ok", "extra": "nope"},
        )
        assert False, "Expected ValueError for unexpected argument"
    except ValueError as exc:
        assert "Unexpected argument" in str(exc)


def test_extract_skill_summary_reads_first_text_paragraph(tmp_path: Path) -> None:
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        "# My Skill\n\n"
        "This skill performs a focused SEO analysis for websites.\n"
        "It prioritizes technical issues and ranking opportunities.\n\n"
        "## Details\n"
        "Extra details here.\n",
        encoding="utf-8",
    )

    summary = DynamicToolRegistry._extract_skill_summary(skill_md)
    assert "focused SEO analysis" in summary
    assert "technical issues" in summary


def test_build_tool_description_appends_skill_summary(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "# Demo\n\n"
        "Use this tool to create concise keyword plans for landing pages.\n",
        encoding="utf-8",
    )

    description = DynamicToolRegistry._build_tool_description(
        skill_dir=skill_dir,
        manifest={"description": "Keyword planning tool"},
        tool_name="demo_tool",
    )

    assert description.startswith("Keyword planning tool")
    assert "Skill context:" in description
    assert "keyword plans" in description.lower()

"""Dynamic Skill Package Service — zip package parsing, validation, and storage.

Handles the lifecycle of Python Dynamic Skill package files:
- ZIP format parsing and validation
- Manifest validation (JSON Schema compliance)
- Code validation (syntax, imports, async)
- Storage management on filesystem
"""

import io
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

from app.services.dynamic_python_skill_support import (
    SKILL_CODE_FILE,
    SKILL_MANIFEST_FILE,
    SKILL_README_FILE,
    dynamic_skill_storage_root,
    extract_description_from_md,
    store_skill_package,
    validate_python_skill_code,
)

logger = logging.getLogger(__name__)

# Max ZIP package size: 2 MB
DYNAMIC_SKILL_MAX_ZIP_BYTES = 2 * 1024 * 1024

# Required files in skill package
DYNAMIC_SKILL_REQUIRED_FILES = {SKILL_MANIFEST_FILE, SKILL_CODE_FILE, SKILL_README_FILE}


class DynamicSkillPackageService:
    """ZIP package parsing, validation, and storage for dynamic skills."""

    @staticmethod
    def validate_skill_manifest(manifest: dict) -> None:
        """Validate skill manifest.json structure.

        Requirements:
        - Must have 'name' (string) and 'input_schema' (JSON Schema object)
        - input_schema.type must be 'object'
        - capabilities (optional) must be dict if provided

        Args:
            manifest: Parsed manifest dict

        Raises:
            ValueError: If manifest is invalid
        """
        required = ["name", "input_schema"]
        missing = [key for key in required if key not in manifest]
        if missing:
            raise ValueError(f"manifest missing required fields: {', '.join(missing)}")

        input_schema = manifest.get("input_schema")
        if not isinstance(input_schema, dict) or str(input_schema.get("type") or "") != "object":
            raise ValueError("manifest.input_schema must be JSON Schema object with type='object'")

        capabilities = manifest.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise ValueError("manifest.capabilities must be an object when provided")

    @staticmethod
    def validate_python_skill_code(*, skill_code: str, function_name: str) -> None:
        """Validate Python skill code.

        Checks:
        - Syntax validity
        - Required imports available
        - async def function_name exists
        - No forbidden imports

        Args:
            skill_code: Python source code
            function_name: Expected function name (default 'run')

        Raises:
            ValueError: If code is invalid
        """
        validate_python_skill_code(skill_code=skill_code, function_name=function_name)

    @staticmethod
    def extract_description_from_md(skill_md: str) -> str:
        """Extract description from skill README.

        Reads first non-empty non-title line as description.

        Args:
            skill_md: Markdown content from skill.md

        Returns:
            Description string or empty string
        """
        return extract_description_from_md(skill_md)

    @staticmethod
    def parse_skill_zip(content: bytes) -> dict[str, Any]:
        """Parse and validate skill.zip package.

        Requirements:
        - Valid ZIP format
        - Contains manifest.json, skill.py, skill.md at any directory level
        - No path traversal attacks
        - All required files present

        Args:
            content: ZIP file bytes

        Returns:
            Dict with keys:
            - manifest: Parsed manifest.json dict
            - skill_py: Skill code string
            - skill_md: Readme string

        Raises:
            ValueError: If ZIP is invalid or missing required files
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except Exception as exc:
            raise ValueError(f"zip parse failed: {exc}") from exc

        with zf:
            names = [str(n or "") for n in zf.namelist()]
            files = {name for name in names if not name.endswith("/")}
            for name in files:
                normalized = name.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    raise ValueError(f"unsafe zip path: {name}")

            flat_names = {Path(name).name for name in files}
            missing = [required for required in DYNAMIC_SKILL_REQUIRED_FILES if required not in flat_names]
            if missing:
                raise ValueError(f"missing required files: {', '.join(sorted(missing))}")

            manifest_name = next(name for name in files if Path(name).name == "manifest.json")
            skill_name = next(name for name in files if Path(name).name == "skill.py")
            md_name = next(name for name in files if Path(name).name == "skill.md")

            manifest_raw = zf.read(manifest_name).decode("utf-8")
            skill_py = zf.read(skill_name).decode("utf-8")
            skill_md = zf.read(md_name).decode("utf-8")

        try:
            manifest = json.loads(manifest_raw)
        except Exception as exc:
            raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc

        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must be an object")

        DynamicSkillPackageService.validate_skill_manifest(manifest)
        return {
            "manifest": manifest,
            "skill_py": skill_py,
            "skill_md": skill_md,
        }

    @staticmethod
    def dynamic_skill_storage_root() -> Path:
        """Get root directory for skill package storage.

        Returns:
            Path to storage root directory
        """
        return dynamic_skill_storage_root()

    @staticmethod
    def store_skill_package(
        *,
        user_id: UUID,
        name: str,
        version: str,
        skill_code: str,
        skill_md: str,
        manifest: dict,
        zip_payload: bytes,
    ) -> dict[str, str]:
        """Store skill package to filesystem.

        Creates user-specific directory structure:
        {root}/user_{user_id}/{version}/{name}/

        Args:
            user_id: User identifier
            name: Skill name
            version: Skill version
            skill_code: Python code content
            skill_md: Readme content
            manifest: Parsed manifest dict
            zip_payload: Original ZIP bytes

        Returns:
            Dict with:
            - storage_dir: Full path where skill was stored
            - skill_py_path: Path to skill.py
            - manifest_path: Path to manifest.json
        """
        return store_skill_package(
            user_id=user_id,
            name=name,
            version=version,
            skill_code=skill_code,
            skill_md=skill_md,
            manifest=manifest,
            zip_payload=zip_payload,
        )

    @staticmethod
    def cleanup_tool_storage(tool: Any) -> None:
        """Clean up filesystem storage for deleted tool.

        Safely removes skill storage directory if path exists.
        Failures are logged but do not raise.

        Args:
            tool: DynamicTool model instance
        """
        auth_data = tool.auth_data if isinstance(tool.auth_data, dict) else {}
        storage_dir = str(auth_data.get("storage_dir") or "").strip()
        if not storage_dir:
            return
        path = Path(storage_dir)
        try:
            if path.exists() and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.debug("failed to cleanup dynamic skill storage: %s", storage_dir)


dynamic_skill_package_service = DynamicSkillPackageService()

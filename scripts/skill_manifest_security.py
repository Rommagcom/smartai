from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
MANIFEST_FILE = "manifest.json"
SIGNING_KEY_HELP = "Signing key (fallback: DYNAMIC_SKILL_SIGNING_KEY)"


def _canonical_manifest_for_signing(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("package_signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {manifest_path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Manifest must be an object: {manifest_path}")
    return parsed


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == MANIFEST_FILE:
            continue
        if "__pycache__" in path.parts:
            continue
        files.append(path)
    return files


def _compute_file_hashes(skill_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in _iter_skill_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes[rel] = digest
    return hashes


def _validate_common_fields(manifest: dict[str, Any], manifest_path: Path) -> None:
    for key in ("name", "entrypoint", "function", "package_version"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid field '{key}' in {manifest_path}")

    version = str(manifest.get("package_version") or "").strip()
    if not SEMVER_RE.match(version):
        raise ValueError(f"package_version must be semantic version in {manifest_path}")


def _verify_hashes(skill_dir: Path, manifest: dict[str, Any], manifest_path: Path) -> None:
    expected = manifest.get("package_files_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ValueError(f"package_files_sha256 must be non-empty object in {manifest_path}")

    actual = _compute_file_hashes(skill_dir)
    normalized_expected: dict[str, str] = {}
    for rel, digest in expected.items():
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError(f"Invalid package_files_sha256 key in {manifest_path}")
        if not isinstance(digest, str) or not digest.strip():
            raise ValueError(f"Invalid package_files_sha256 value for {rel} in {manifest_path}")
        normalized_expected[rel.replace("\\", "/")] = digest.lower()

    if normalized_expected != actual:
        details = _build_hash_diff_details(actual=actual, expected=normalized_expected)
        raise ValueError(f"Integrity validation failed in {manifest_path}: {'; '.join(details)}")


def _build_hash_diff_details(*, actual: dict[str, str], expected: dict[str, str]) -> list[str]:
    missing = sorted(set(actual) - set(expected))
    stale = sorted(set(expected) - set(actual))
    mismatch = sorted(rel for rel in set(actual).intersection(expected) if actual[rel] != expected[rel])

    details: list[str] = []
    if missing:
        details.append(f"missing hashes for files: {', '.join(missing)}")
    if stale:
        details.append(f"hashes reference non-existent files: {', '.join(stale)}")
    if mismatch:
        details.append(f"hash mismatch: {', '.join(mismatch)}")
    return details


def _calculate_signature(manifest: dict[str, Any], signing_key: str) -> str:
    payload = _canonical_manifest_for_signing(manifest)
    return hmac.new(signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_signature(manifest: dict[str, Any], manifest_path: Path, signing_key: str | None) -> None:
    signature = manifest.get("package_signature")
    if signature is None:
        return

    if not isinstance(signature, str) or not signature.strip():
        raise ValueError(f"package_signature must be non-empty string in {manifest_path}")

    algorithm = str(manifest.get("package_signature_algorithm") or "hmac-sha256").strip().lower()
    if algorithm != "hmac-sha256":
        raise ValueError(f"Unsupported package_signature_algorithm in {manifest_path}: {algorithm}")

    if not signing_key:
        raise ValueError(
            f"Cannot verify package_signature in {manifest_path}: provide --key or DYNAMIC_SKILL_SIGNING_KEY"
        )

    expected = _calculate_signature(manifest, signing_key)
    if not hmac.compare_digest(expected, signature.lower()):
        raise ValueError(f"Signature verification failed for {manifest_path}")


def sign_manifest(
    *,
    skill_dir: Path,
    signing_key: str,
    package_version: str | None,
    include_signature: bool,
) -> None:
    manifest_path = skill_dir / MANIFEST_FILE
    manifest = _read_manifest(manifest_path)

    if package_version is not None:
        if not SEMVER_RE.match(package_version):
            raise ValueError("--package-version must follow semantic version (for example 1.2.3)")
        manifest["package_version"] = package_version

    if not manifest.get("package_version"):
        manifest["package_version"] = "1.0.0"

    manifest["package_files_sha256"] = _compute_file_hashes(skill_dir)

    if include_signature:
        manifest["package_signature_algorithm"] = "hmac-sha256"
        manifest["package_signature"] = _calculate_signature(manifest, signing_key)

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def verify_manifest(*, skill_dir: Path, signing_key: str | None) -> None:
    manifest_path = skill_dir / MANIFEST_FILE
    manifest = _read_manifest(manifest_path)
    _validate_common_fields(manifest, manifest_path)
    _verify_hashes(skill_dir, manifest, manifest_path)
    _verify_signature(manifest, manifest_path, signing_key)


def _all_skill_dirs(skills_root: Path) -> list[Path]:
    if not skills_root.exists():
        return []
    return sorted(path for path in skills_root.iterdir() if path.is_dir() and (path / MANIFEST_FILE).exists())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign and verify dynamic-skill manifests")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sign_cmd = subparsers.add_parser("sign", help="Update package hashes and optional signature for one skill")
    sign_cmd.add_argument("--skill-dir", required=True, help="Path to skill directory containing manifest.json")
    sign_cmd.add_argument("--package-version", default=None, help="Optional semantic package version to set")
    sign_cmd.add_argument(
        "--with-signature",
        action="store_true",
        help="Attach HMAC-SHA256 signature (requires --key or DYNAMIC_SKILL_SIGNING_KEY)",
    )
    sign_cmd.add_argument("--key", default=None, help=SIGNING_KEY_HELP)

    verify_cmd = subparsers.add_parser("verify", help="Verify one skill manifest")
    verify_cmd.add_argument("--skill-dir", required=True, help="Path to skill directory containing manifest.json")
    verify_cmd.add_argument("--key", default=None, help=SIGNING_KEY_HELP)

    verify_all_cmd = subparsers.add_parser("verify-all", help="Verify all manifests under skills root")
    verify_all_cmd.add_argument("--skills-root", default="skills", help="Path to skills root directory")
    verify_all_cmd.add_argument("--key", default=None, help=SIGNING_KEY_HELP)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    key = args.key or os.getenv("DYNAMIC_SKILL_SIGNING_KEY")

    try:
        if args.command == "sign":
            return _run_sign(args=args, key=key)

        if args.command == "verify":
            return _run_verify(args=args, key=key)

        if args.command == "verify-all":
            return _run_verify_all(args=args, key=key)

        parser.print_help()
        return 1
    except Exception as exc:
        print(str(exc))
        return 1


def _run_sign(*, args: argparse.Namespace, key: str | None) -> int:
    if args.with_signature and not key:
        raise ValueError("Signing key is required when using --with-signature")
    sign_manifest(
        skill_dir=Path(args.skill_dir),
        signing_key=key or "",
        package_version=args.package_version,
        include_signature=bool(args.with_signature),
    )
    print(f"Signed manifest: {Path(args.skill_dir) / MANIFEST_FILE}")
    return 0


def _run_verify(*, args: argparse.Namespace, key: str | None) -> int:
    verify_manifest(skill_dir=Path(args.skill_dir), signing_key=key)
    print(f"Verified manifest: {Path(args.skill_dir) / MANIFEST_FILE}")
    return 0


def _run_verify_all(*, args: argparse.Namespace, key: str | None) -> int:
    skills_root = Path(args.skills_root)
    dirs = _all_skill_dirs(skills_root)
    if not dirs:
        print(f"No skill directories found in {skills_root}")
        return 0

    failed = 0
    for skill_dir in dirs:
        try:
            verify_manifest(skill_dir=skill_dir, signing_key=key)
            print(f"OK: {skill_dir / MANIFEST_FILE}")
        except Exception as exc:
            failed += 1
            print(f"FAIL: {skill_dir / MANIFEST_FILE} -> {exc}")

    if failed:
        print(f"Verification failed for {failed} skill(s)")
        return 1
    print("All skill manifests verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

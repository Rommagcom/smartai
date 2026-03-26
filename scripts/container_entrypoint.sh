#!/bin/sh
set -eu

is_true() {
  value="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

if is_true "${ENABLE_DYNAMIC_TOOLS:-true}"; then
  skills_root="/app/skills"
  signer_script="/app/scripts/skill_manifest_security.py"

  if [ -d "$skills_root" ] && [ -f "$signer_script" ]; then
    sign_with_signature=false
    if is_true "${DYNAMIC_SKILL_REQUIRE_SIGNATURE:-false}"; then
      sign_with_signature=true
      if [ -z "${DYNAMIC_SKILL_SIGNING_KEY:-}" ]; then
        echo "DYNAMIC_SKILL_SIGNING_KEY is required when DYNAMIC_SKILL_REQUIRE_SIGNATURE=true" >&2
        exit 1
      fi
    fi

    for d in "$skills_root"/*; do
      [ -d "$d" ] || continue
      if [ "$sign_with_signature" = "true" ]; then
        python "$signer_script" sign --skill-dir "$d" --with-signature --key "${DYNAMIC_SKILL_SIGNING_KEY}"
      else
        python "$signer_script" sign --skill-dir "$d"
      fi
    done

    if is_true "${DYNAMIC_SKILL_REQUIRE_INTEGRITY:-false}" || is_true "${DYNAMIC_SKILL_REQUIRE_SIGNATURE:-false}"; then
      if [ -n "${DYNAMIC_SKILL_SIGNING_KEY:-}" ]; then
        python "$signer_script" verify-all --skills-root "$skills_root" --key "${DYNAMIC_SKILL_SIGNING_KEY}"
      else
        python "$signer_script" verify-all --skills-root "$skills_root"
      fi
    fi
  fi
fi

exec "$@"

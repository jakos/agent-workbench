# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Publication credentials belong only to a separate trusted launcher."""

import re

PREFIX = "TASK_FLOW_PUBLISH_"
AUTH = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)


class CredentialError(ValueError):
    """A refusal that never includes environment values."""


def valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(
        re.fullmatch(PREFIX + r"[A-Z][A-Z0-9_]{0,63}", name)
    )


def reserved(name: str) -> bool:
    return name.upper().startswith(PREFIX) or name.upper() in AUTH


def clean(env: dict) -> dict:
    return {name: value for name, value in env.items() if not reserved(name)}


def runner(env: dict, profiles: dict | None = None) -> None:
    if any(name.upper().startswith(PREFIX) for name in env):
        raise CredentialError(
            "publication credential present in model runner; use a separate launcher"
        )
    for profile in (profiles or {}).values():
        names = [profile.get("api_key_env") or "", *profile.get("pass_env", [])]
        if any(reserved(name) for name in names):
            raise CredentialError(
                "publication authentication collides with model profile"
            )

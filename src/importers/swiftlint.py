"""
Translates .swiftlint.yml into Preflight rule overrides.

Only handles enable/disable signals — severity overrides and custom rules.
SwiftLint rule semantics are not re-implemented; the model handles detection.
"""

from pathlib import Path
import yaml

# Maps SwiftLint rule names → Preflight rule IDs where there's a direct equivalent.
# SwiftLint rules not in this map are imported as custom/swiftlint/<rule_name>.
SWIFTLINT_TO_PREFLIGHT = {
    "force_unwrapping":          "preflight/ios/forced-unwrap",
    "weak_delegate":             "preflight/ios/delegate-strong",
    "notification_center_detachment": "preflight/ios/notification-observer-leak",
}

SEVERITY_MAP = {
    "error":   "high",
    "warning": "medium",
    "note":    "low",
}


def translate(path: Path) -> list[dict]:
    """Returns a list of Preflight rule dicts derived from a .swiftlint.yml."""
    if not path.exists():
        return []

    raw = yaml.safe_load(path.read_text()) or {}
    rules = []

    # disabled_rules → severity: ignore
    for rule_name in raw.get("disabled_rules", []):
        rules.append({
            "id": _map_rule_id(rule_name),
            "severity": "ignore",
            "source": f"swiftlint:{rule_name}",
        })

    # opt_in_rules → enable with default medium severity
    for rule_name in raw.get("opt_in_rules", []):
        rules.append({
            "id": _map_rule_id(rule_name),
            "severity": "medium",
            "source": f"swiftlint:{rule_name}",
        })

    # Per-rule severity overrides (e.g. force_unwrapping: error)
    for rule_name, config in raw.items():
        if not isinstance(config, dict):
            continue
        if "severity" in config:
            rules.append({
                "id": _map_rule_id(rule_name),
                "severity": SEVERITY_MAP.get(config["severity"], "medium"),
                "source": f"swiftlint:{rule_name}",
            })

    # excluded paths → convert to paths_ignore on matching rules
    excluded = raw.get("excluded", [])
    if excluded:
        for rule in rules:
            rule.setdefault("paths_ignore", excluded)

    return rules


def _map_rule_id(swiftlint_name: str) -> str:
    return SWIFTLINT_TO_PREFLIGHT.get(
        swiftlint_name,
        f"custom/swiftlint/{swiftlint_name.replace('_', '-')}"
    )

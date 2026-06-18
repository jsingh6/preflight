"""
Translates ESLint config into Preflight rule overrides.

Supported formats:
  .eslintrc.json
  .eslintrc.yml / .eslintrc.yaml
  .eslintrc (JSON or YAML)
  package.json  (eslintConfig field)

Not supported (require JS execution to parse):
  .eslintrc.js / .eslintrc.cjs
  eslint.config.js / eslint.config.mjs  (ESLint v9 flat config)
"""

from pathlib import Path
import json
import yaml


# ESLint rule name → Preflight rule ID for direct equivalents.
# Rules not in this map are imported as custom/eslint/<rule-name>.
ESLINT_TO_PREFLIGHT = {
    "eqeqeq":                        "preflight/typescript/loose-equality",
    "react-hooks/exhaustive-deps":   "preflight/typescript/useeffect-missing-deps",
    "no-floating-promises":          "preflight/typescript/floating-promise",
    "@typescript-eslint/no-explicit-any": "preflight/typescript/any-cast",
    "@typescript-eslint/no-non-null-assertion": "preflight/typescript/non-null-assertion",
    "react/no-danger":               "preflight/typescript/dangerously-set-inner-html",
}

ESLINT_SEVERITY_MAP = {
    "off":   "ignore",
    "warn":  "medium",
    "error": "high",
    0: "ignore",
    1: "medium",
    2: "high",
}

# Config file candidates in priority order
CONFIG_CANDIDATES = [
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".eslintrc",
]

UNSUPPORTED = [
    ".eslintrc.js",
    ".eslintrc.cjs",
    "eslint.config.js",
    "eslint.config.mjs",
]


def find_eslint_config(repo_path: Path):
    """
    Returns (config_path, warning).
    warning is set when a config exists but can't be parsed statically.
    """
    for candidate in CONFIG_CANDIDATES:
        p = repo_path / candidate
        if p.exists():
            return p, None

    for candidate in UNSUPPORTED:
        p = repo_path / candidate
        if p.exists():
            return None, (
                f"{candidate} found but cannot be statically parsed. "
                "Add rule overrides to .preflight.yml manually."
            )

    # Check package.json eslintConfig field
    pkg = repo_path / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            if "eslintConfig" in data:
                return pkg, None
        except json.JSONDecodeError:
            pass

    return None, None


def translate(path: Path) -> list[dict]:
    """Returns a list of Preflight rule dicts derived from an ESLint config."""
    if not path or not path.exists():
        return []

    raw = _parse(path)
    if raw is None:
        return []

    # package.json: extract the eslintConfig field
    if path.name == "package.json":
        raw = raw.get("eslintConfig", {})

    return _extract_rules(raw)


def _parse(path: Path):
    text = path.read_text(errors="ignore")

    # Try JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try YAML
    try:
        result = yaml.safe_load(text)
        if isinstance(result, dict):
            return result
    except yaml.YAMLError:
        pass

    return None


def _extract_rules(config: dict) -> list[dict]:
    rules = []
    for rule_name, rule_config in config.get("rules", {}).items():
        severity_raw = _parse_severity(rule_config)
        if severity_raw is None:
            continue

        preflight_severity = ESLINT_SEVERITY_MAP.get(severity_raw)
        if preflight_severity is None:
            continue

        rules.append({
            "id": _map_rule_id(rule_name),
            "severity": preflight_severity,
            "source": f"eslint:{rule_name}",
        })

    return rules


def _parse_severity(rule_config):
    """ESLint rules can be a string, int, or [severity, ...options] array."""
    if isinstance(rule_config, (str, int)):
        return rule_config
    if isinstance(rule_config, list) and rule_config:
        return rule_config[0]
    return None


def _map_rule_id(eslint_name: str) -> str:
    if eslint_name in ESLINT_TO_PREFLIGHT:
        return ESLINT_TO_PREFLIGHT[eslint_name]

    # Normalize: strip plugin prefix separators, kebab-case
    slug = eslint_name.replace("/", "-").replace("@", "").replace("--", "-").strip("-")
    return f"custom/eslint/{slug}"

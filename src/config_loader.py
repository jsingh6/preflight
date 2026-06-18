from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .platform_detect import detect_stacks, rules_for_stack
from .importers import swiftlint, android_lint, eslint


@dataclass
class Rule:
    id: str
    severity: str           # high | medium | low | ignore
    description: str = ""
    paths_ignore: list[str] = field(default_factory=list)
    source: str = "preflight"


@dataclass
class PreflightConfig:
    platforms: list[str]
    rules: dict[str, Rule]      # keyed by rule ID
    guidelines: str
    sensitivity: str = "balanced"
    max_findings: int = 10

    @classmethod
    def defaults(cls, stacks: list[str]) -> "PreflightConfig":
        rules = {}
        for stack in stacks:
            for rule in _load_builtin_rules(stack):
                rules[rule.id] = rule
        return cls(platforms=stacks, rules=rules, guidelines="")

    def rules_for_platform(self, platform: str) -> list[Rule]:
        prefix = f"preflight/{platform}/"
        return [r for r in self.rules.values() if r.id.startswith(prefix) or r.id.startswith("custom/")]


def load(repo_path: Path) -> PreflightConfig:
    platforms = detect_stacks(repo_path)
    config = PreflightConfig.defaults(platforms)

    config_file = repo_path / ".preflight.yml"
    if not config_file.exists():
        return config

    raw = yaml.safe_load(config_file.read_text()) or {}

    # Guidelines: inline text takes precedence over file reference
    if "guidelines" in raw:
        config.guidelines = raw["guidelines"]
    elif "guidelines_file" in raw:
        gf = repo_path / raw["guidelines_file"]
        if gf.exists():
            config.guidelines = gf.read_text()

    # Sensitivity and cap
    settings = raw.get("settings", {})
    config.sensitivity = settings.get("sensitivity", "balanced")
    config.max_findings = settings.get("max_findings", 10)

    # Translate imported linter configs
    for import_path in raw.get("imports", []):
        imported = _translate_import(repo_path / import_path)
        _merge_rules(config.rules, imported, precedence="repo_wins")

    # Apply repo-level rule overrides from .preflight.yml
    for platform_key, overrides in raw.get("rules", {}).items():
        for override in overrides:
            rule_id = override["id"]
            if rule_id in config.rules:
                existing = config.rules[rule_id]
                config.rules[rule_id] = Rule(
                    id=rule_id,
                    severity=override.get("severity", existing.severity),
                    description=override.get("description", existing.description),
                    paths_ignore=override.get("paths_ignore", existing.paths_ignore),
                    source="repo-config",
                )
            else:
                # Custom rule not in built-ins
                config.rules[rule_id] = Rule(
                    id=rule_id,
                    severity=override.get("severity", "medium"),
                    description=override.get("description", ""),
                    paths_ignore=override.get("paths_ignore", []),
                    source="repo-config",
                )

    return config


def _load_builtin_rules(platform: str) -> list[Rule]:
    rule_file = rules_for_stack(platform)
    if not rule_file.exists():
        return []

    raw = yaml.safe_load(rule_file.read_text()) or {}
    return [
        Rule(
            id=r["id"],
            severity=r["severity"],
            description=r.get("description", ""),
            paths_ignore=r.get("paths_ignore", []),
        )
        for r in raw.get("rules", [])
    ]


ESLINT_CONFIG_NAMES = {".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml", ".eslintrc", "package.json"}

def _translate_import(path: Path) -> list[Rule]:
    name = path.name.lower()
    if name == ".swiftlint.yml":
        raw_rules = swiftlint.translate(path)
    elif name == "lint.xml":
        raw_rules = android_lint.translate(path)
    elif name in ESLINT_CONFIG_NAMES:
        raw_rules = eslint.translate(path)
    else:
        return []

    return [
        Rule(
            id=r["id"],
            severity=r["severity"],
            description=r.get("description", ""),
            paths_ignore=r.get("paths_ignore", []),
            source=r.get("source", "import"),
        )
        for r in raw_rules
    ]


def _merge_rules(
    base: dict[str, Rule],
    incoming: list[Rule],
    precedence: str,  # "repo_wins" | "import_wins"
) -> None:
    for rule in incoming:
        if rule.id not in base or precedence == "import_wins":
            base[rule.id] = rule
        # if repo_wins and rule already exists, keep existing

"""
preflight init — one-time setup command.

Scans the repo, discovers existing configs, asks targeted questions,
and generates .preflight.yml.
"""

from pathlib import Path
import yaml

from ..src.platform_detect import detect_stacks
from ..src.importers import swiftlint, android_lint, eslint


def run(repo_path: Path) -> None:
    print("\nPreflight — Initial Setup\n")
    print("Scanning repository...")

    platforms = detect_stacks(repo_path)
    discovered, warnings = _discover_existing_configs(repo_path)
    guidelines_source = _find_guidelines_doc(repo_path)

    _print_scan_summary(platforms, discovered, warnings, guidelines_source)

    config = {"version": 1}

    # Guidelines
    if guidelines_source:
        use_guidelines = _ask(f'Use {guidelines_source.name} as guidelines source?', default=True)
        if use_guidelines:
            config["guidelines_file"] = f"./{guidelines_source.name}"

    # Imports
    imports = []
    for path in discovered:
        if _ask(f"Import rules from {path.name}?", default=True):
            imports.append(f"./{path.name}")
    if imports:
        config["imports"] = imports

    # Custom rules (free-form input)
    print("\nAny team rules not covered by your existing configs?")
    print("Example: 'No singleton pattern outside AppDelegate'")
    print("(press Enter to skip)")
    custom_input = input("> ").strip()

    custom_rules = []
    if custom_input:
        platform_key = platforms[0] if platforms else "swift"
        rule_id = _slugify_to_rule_id(custom_input, platform_key)
        custom_rules.append({
            "id": f"custom/{platform_key}/{rule_id}",
            "severity": "medium",
            "description": custom_input,
        })

    if custom_rules:
        config["rules"] = {platforms[0] if platforms else "swift": custom_rules}

    # Sensitivity
    print("\nReview sensitivity:")
    print("  1. strict   — fewer findings, high confidence only")
    print("  2. balanced — default")
    print("  3. broad    — more findings, may include uncertain cases")
    choice = input("Choice [2]: ").strip() or "2"
    sensitivity_map = {"1": "strict", "2": "balanced", "3": "broad"}
    config["settings"] = {
        "sensitivity": sensitivity_map.get(choice, "balanced"),
        "max_findings": 8 if choice == "1" else 10,
    }

    output_path = repo_path / ".preflight.yml"
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n✓ Generated {output_path}")
    print("  Review it before committing — especially any imported rule overrides.")


def _discover_existing_configs(repo_path: Path) -> tuple[list[Path], list[str]]:
    """Returns (parseable configs, warnings for unsupported formats)."""
    found = []
    warnings = []

    # SwiftLint
    sl = repo_path / ".swiftlint.yml"
    if sl.exists():
        found.append(sl)

    # Android Lint
    for candidate in ["lint.xml", "app/lint.xml", "android/lint.xml"]:
        p = repo_path / candidate
        if p.exists():
            found.append(p)
            break

    # ESLint — uses its own finder to handle the unsupported-format case
    eslint_path, eslint_warning = eslint.find_eslint_config(repo_path)
    if eslint_path:
        found.append(eslint_path)
    if eslint_warning:
        warnings.append(eslint_warning)

    return found, warnings


def _find_guidelines_doc(repo_path: Path) -> Path | None:
    candidates = ["CONTRIBUTING.md", "DEVELOPMENT.md", "STYLE_GUIDE.md", "docs/CONTRIBUTING.md"]
    for name in candidates:
        p = repo_path / name
        if p.exists():
            return p
    return None


def _print_scan_summary(platforms, discovered, warnings, guidelines_source):
    print(f"  ✓ Detected stacks: {', '.join(platforms)}")
    for path in discovered:
        print(f"  ✓ Found {path.name}")
    for warning in warnings:
        print(f"  ⚠ {warning}")
    if guidelines_source:
        word_count = len(guidelines_source.read_text().split())
        print(f"  ✓ Found {guidelines_source.name} ({word_count:,} words)")
    print()


def _ask(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _slugify_to_rule_id(text: str, platform: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40]

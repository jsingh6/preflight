from pathlib import Path


def detect_stacks(repo_path: Path) -> list[str]:
    """
    Returns every stack detected in the repo, ordered by confidence.
    A repo can have multiple (e.g. React Native = ios + android + typescript).

    Stacks with dedicated platform rule files: ios, android.
    Stacks with language-only rule files: typescript, python, go, swift, kotlin.
    """
    stacks = []

    # Mobile platforms first — they have the most specific signals
    if _is_ios(repo_path):
        stacks.append("ios")
    if _is_android(repo_path):
        stacks.append("android")

    # Language stacks — detected by file presence
    if _has_files(repo_path, ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs"]) and "ios" not in stacks and "android" not in stacks:
        stacks.append("typescript")
    elif _has_files(repo_path, ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs"]):
        # React Native: already have ios/android but also has TypeScript/JavaScript
        stacks.append("typescript")

    if _has_files(repo_path, ["*.py"]) and _has_marker(repo_path, ["requirements.txt", "pyproject.toml", "setup.py"]):
        stacks.append("python")

    if _has_marker(repo_path, ["go.mod", "go.sum"]):
        stacks.append("go")

    # Swift without iOS (server-side Swift, CLI tools)
    if _has_files(repo_path, ["*.swift"]) and "ios" not in stacks:
        stacks.append("swift")

    return stacks or ["unknown"]


def rules_for_stack(stack: str) -> "Path":
    """Returns path to the built-in rule file for a given stack."""
    rules_dir = Path(__file__).parent.parent / "rules"
    return rules_dir / f"{stack}.yml"


def display_language(stack: str) -> str:
    mapping = {
        "ios":        "Swift (iOS)",
        "android":    "Kotlin/Java (Android)",
        "typescript": "TypeScript / JavaScript",
        "python":     "Python",
        "go":         "Go",
        "swift":      "Swift",
        "kotlin":     "Kotlin",
    }
    return mapping.get(stack, stack)


# ── Private helpers ──────────────────────────────────────────────────────────

def _is_ios(repo_path: Path) -> bool:
    if any([
        list(repo_path.glob("**/*.xcodeproj")),
        list(repo_path.glob("**/*.xcworkspace")),
        list(repo_path.glob("**/*.pbxproj")),
    ]):
        return True

    ios_dir = repo_path / "ios"
    if ios_dir.exists() and any(ios_dir.glob("*.xcodeproj")):
        return True

    return False


def _is_android(repo_path: Path) -> bool:
    for gradle_file in ["build.gradle", "build.gradle.kts"]:
        gradle = repo_path / gradle_file
        if gradle.exists():
            content = gradle.read_text(errors="ignore")
            if "com.android.application" in content or "com.android.library" in content:
                return True

    android_dir = repo_path / "android"
    if android_dir.exists():
        for f in ["build.gradle", "build.gradle.kts"]:
            if (android_dir / f).exists():
                return True

    if any(repo_path.glob("**/AndroidManifest.xml")):
        return True

    return False


def _has_files(repo_path: Path, patterns: list[str]) -> bool:
    for pattern in patterns:
        if any(repo_path.glob(f"**/{pattern}")):
            return True
    return False


def _has_marker(repo_path: Path, markers: list[str]) -> bool:
    return any((repo_path / m).exists() for m in markers)

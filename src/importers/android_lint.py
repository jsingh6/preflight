"""
Translates Android lint.xml into Preflight rule overrides.

lint.xml controls severity overrides and issue suppression at the project level.
Preflight imports those signals so reviews respect team-level decisions.
"""

from pathlib import Path
import xml.etree.ElementTree as ET

LINT_TO_PREFLIGHT = {
    "StaticFieldLeak":          "preflight/android/context-leak",
    "FragmentLeak":             "preflight/android/fragment-view-leak",
    "UnprotectedSMSBroadcastReceiver": "preflight/android/broadcast-not-unregistered",
    "Wakelock":                 "preflight/android/wakelock-unreleased",
    "HardcodedText":            "preflight/android/hardcoded-secret",
    "ExportedWithoutPermission": "preflight/android/exported-no-permission",
    "SetJavaScriptEnabled":     "preflight/android/webview-javascript-file-access",
}

LINT_SEVERITY_MAP = {
    "ignore":      "ignore",
    "informational": "low",
    "warning":     "medium",
    "error":       "high",
    "fatal":       "high",
}

# Common lint.xml locations in an Android project
LINT_XML_CANDIDATES = [
    "lint.xml",
    "app/lint.xml",
    "android/lint.xml",
]


def find_lint_xml(repo_path: Path):
    for candidate in LINT_XML_CANDIDATES:
        p = repo_path / candidate
        if p.exists():
            return p
    return None


def translate(path: Path) -> list[dict]:
    """Returns a list of Preflight rule dicts derived from a lint.xml."""
    if not path or not path.exists():
        return []

    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []

    root = tree.getroot()
    rules = []

    for issue in root.findall("issue"):
        lint_id = issue.attrib.get("id", "")
        severity = issue.attrib.get("severity", "")

        preflight_id = LINT_TO_PREFLIGHT.get(lint_id, f"custom/android-lint/{_to_kebab(lint_id)}")
        preflight_severity = LINT_SEVERITY_MAP.get(severity.lower(), "medium")

        rule = {
            "id": preflight_id,
            "severity": preflight_severity,
            "source": f"android-lint:{lint_id}",
        }

        # Respect path-level ignores in lint.xml
        # <issue id="X"><ignore path="..." /></issue>
        ignored_paths = [
            child.attrib["path"]
            for child in issue.findall("ignore")
            if "path" in child.attrib
        ]
        if ignored_paths:
            rule["paths_ignore"] = ignored_paths

        rules.append(rule)

    return rules


def _to_kebab(camel: str) -> str:
    import re
    s = re.sub(r"([A-Z])", r"-\1", camel).lower()
    return s.lstrip("-")

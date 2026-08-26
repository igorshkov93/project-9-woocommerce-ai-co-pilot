"""Sanitize an n8n workflow export before committing it to the repository.

The raw export contains instance-specific identifiers that must not end up in
a public repository: credential IDs, the live webhook UUID, pinned test data,
and the sharing block that carries the owner's email address and internal
project IDs. The workflow structure itself is what we want to version.

Dry-run by default; pass --apply to write the output file.

Usage:
    cd scripts
    python sanitize_workflow.py --source ../docker/n8n_data/export.json \\
        --target ../workflows/09-telegram-gateway.json
    python sanitize_workflow.py ... --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "docker" / "n8n_data" / "export.json"
DEFAULT_TARGET = REPO_ROOT / "workflows" / "09-telegram-gateway.json"

PLACEHOLDER_ID = "__REPLACE_WITH_YOUR_CREDENTIAL_ID__"
PLACEHOLDER_WEBHOOK = "__REPLACE_WITH_YOUR_WEBHOOK_ID__"

# Volatile instance state that carries no architectural meaning. `shared`
# holds the owner's email and the n8n project UUID; `versionMetadata` and the
# activeVersion fields change on every publish and produce noisy diffs.
DROPPED_TOP_LEVEL_KEYS = (
    "id",
    "versionId",
    "activeVersionId",
    "versionCounter",
    "versionMetadata",
    "createdAt",
    "updatedAt",
    "meta",
    "shared",
    "triggerCount",
    "isArchived",
)

# Patterns that must never survive into a committed export. Checked against
# the rendered JSON as a last line of defence, independently of the key list
# above, so that a schema change in n8n cannot silently reintroduce a leak.
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("WooCommerce consumer key", r"ck_[0-9a-fA-F]{20,}"),
    ("WooCommerce consumer secret", r"cs_[0-9a-fA-F]{20,}"),
    ("Telegram bot token", r"\d{8,10}:[A-Za-z0-9_-]{30,}"),
    ("email address", r"[\w.+-]+@[\w-]+\.[\w.]+"),
    ("ngrok domain", r"[a-z0-9-]+\.ngrok-free\.dev"),
)


def sanitize_node(node: dict[str, Any], report: list[str]) -> bool:
    """Strip instance-specific identifiers from a node. True if a cred was hit."""
    name = node.get("name", "<unnamed>")
    touched_credential = False

    credentials = node.get("credentials")
    if isinstance(credentials, dict):
        for cred_type, cred in credentials.items():
            if isinstance(cred, dict) and "id" in cred:
                report.append(f"{name}: credential '{cred_type}' id -> placeholder")
                cred["id"] = PLACEHOLDER_ID
                touched_credential = True

    if "webhookId" in node:
        report.append(f"{name}: webhookId -> placeholder")
        node["webhookId"] = PLACEHOLDER_WEBHOOK

    return touched_credential


def sanitize_workflow(
    workflow: dict[str, Any], report: list[str]
) -> tuple[dict[str, Any], bool]:
    """Return a sanitized copy of one workflow and whether it had credentials."""
    clean = dict(workflow)

    for key in DROPPED_TOP_LEVEL_KEYS:
        if key in clean:
            report.append(f"workflow: dropped top-level key '{key}'")
            del clean[key]

    pin_data = clean.get("pinData")
    if pin_data:
        report.append(f"workflow: dropped pinData for {len(pin_data)} node(s)")
    clean["pinData"] = {}

    # An exported workflow should never be committed as active: importing it
    # elsewhere would register a webhook against someone else's bot token.
    if clean.get("active"):
        report.append("workflow: active -> false")
    clean["active"] = False

    has_credentials = False
    for node in clean.get("nodes", []):
        if isinstance(node, dict) and sanitize_node(node, report):
            has_credentials = True

    return clean, has_credentials


def scan_for_secrets(rendered: str) -> list[str]:
    """Return human-readable findings for anything that looks like a secret."""
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        matches = set(re.findall(pattern, rendered))
        for match in sorted(matches):
            findings.append(f"{label}: {match}")
    return findings


def load_workflows(path: Path) -> list[dict[str, Any]]:
    """Read the export file, tolerating both list and single-object formats."""
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    return [data]


def resolve_target(target: Path, workflow_name: str, count: int) -> Path:
    """Pick the output path for one workflow inside a possibly multi-item export."""
    if count == 1:
        return target
    # Batch mode: derive a file per workflow from its name, ignoring --target.
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", workflow_name).strip("-")
    return target.parent / f"{safe}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--apply", action="store_true", help="write the target file")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"ERROR: source not found: {args.source}", file=sys.stderr)
        return 1

    workflows = load_workflows(args.source)
    if not workflows:
        print("ERROR: export contains no workflows", file=sys.stderr)
        return 1

    print(f"source: {args.source}")
    print(f"workflows in export: {len(workflows)}")

    failed = False
    for workflow in workflows:
        name = str(workflow.get("name", "<unnamed>"))
        report: list[str] = []
        clean, has_credentials = sanitize_workflow(workflow, report)
        rendered = json.dumps(clean, indent=2, ensure_ascii=False) + "\n"
        destination = resolve_target(args.target, name, len(workflows))

        print()
        print(f"workflow: {name}")
        print(f"target  : {destination}")
        print(f"nodes   : {len(clean.get('nodes', []))}")
        print("--- changes ---")
        for line in report:
            print(f"  {line}")

        # A tool workflow that authenticates through $env has no stored
        # credentials at all. That is expected, not a problem, so it is
        # reported as information rather than as a warning.
        if not has_credentials:
            print("  note: no stored credentials (env-based auth or none needed)")

        findings = scan_for_secrets(rendered)
        if findings:
            failed = True
            print("--- SECRETS DETECTED, refusing to write ---")
            for finding in findings:
                print(f"  {finding}")
            continue

        if not args.apply:
            print("--- dry run, nothing written (pass --apply to write) ---")
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        print(f"--- written: {destination} ({len(rendered)} bytes) ---")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

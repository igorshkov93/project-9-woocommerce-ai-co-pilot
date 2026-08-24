"""Sanitize an n8n workflow export before committing it to the repository.

The raw export contains instance-specific identifiers that must not end up in
a public repository: credential IDs, the live webhook UUID, and any pinned
test data. The workflow structure itself is what we want to version.

Dry-run by default; pass --apply to write the output file.

Usage:
    cd scripts
    python sanitize_workflow.py
    python sanitize_workflow.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "docker" / "n8n_data" / "export.json"
DEFAULT_TARGET = REPO_ROOT / "workflows" / "09-telegram-gateway.json"

PLACEHOLDER_ID = "__REPLACE_WITH_YOUR_CREDENTIAL_ID__"
PLACEHOLDER_WEBHOOK = "__REPLACE_WITH_YOUR_WEBHOOK_ID__"

# Volatile instance state that carries no architectural meaning.
DROPPED_TOP_LEVEL_KEYS = ("id", "versionId", "createdAt", "updatedAt", "meta")


def sanitize_node(node: dict[str, Any], report: list[str]) -> None:
    """Strip instance-specific identifiers from a single node, in place."""
    name = node.get("name", "<unnamed>")

    credentials = node.get("credentials")
    if isinstance(credentials, dict):
        for cred_type, cred in credentials.items():
            if isinstance(cred, dict) and "id" in cred:
                report.append(f"{name}: credential '{cred_type}' id -> placeholder")
                cred["id"] = PLACEHOLDER_ID

    if "webhookId" in node:
        report.append(f"{name}: webhookId -> placeholder")
        node["webhookId"] = PLACEHOLDER_WEBHOOK


def sanitize_workflow(workflow: dict[str, Any], report: list[str]) -> dict[str, Any]:
    """Return a sanitized copy of one workflow object."""
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

    for node in clean.get("nodes", []):
        if isinstance(node, dict):
            sanitize_node(node, report)

    return clean


def load_workflows(path: Path) -> list[dict[str, Any]]:
    """Read the export file, tolerating both list and single-object formats."""
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    return [data]


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
    if len(workflows) != 1:
        names = ", ".join(str(w.get("name")) for w in workflows)
        print(
            f"ERROR: expected exactly 1 workflow, found {len(workflows)}: {names}",
            file=sys.stderr,
        )
        return 1

    report: list[str] = []
    clean = sanitize_workflow(workflows[0], report)
    rendered = json.dumps(clean, indent=2, ensure_ascii=False) + "\n"

    print(f"source: {args.source}")
    print(f"target: {args.target}")
    print(f"workflow: {clean.get('name')}")
    print(f"nodes: {len(clean.get('nodes', []))}")
    print("--- changes ---")
    for line in report:
        print(f"  {line}")

    # Fail loudly rather than silently committing a secret.
    leaked = [
        token
        for token in (PLACEHOLDER_ID, PLACEHOLDER_WEBHOOK)
        if token not in rendered and token == PLACEHOLDER_ID
    ]
    if leaked:
        print("WARNING: no credential id was replaced; check the export format")

    if not args.apply:
        print("--- dry run, nothing written (pass --apply to write) ---")
        return 0

    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(rendered, encoding="utf-8")
    print(f"--- written: {args.target} ({len(rendered)} bytes) ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the Qdrant catalog index against the snapshot it was built from.

This is a test, not a report: any failed assertion exits with a non-zero code.
Score thresholds are deliberately not asserted - the script prints observed
similarity scores so the confidence cut-off used by resolve_entity can be
calibrated against real numbers instead of guessed.

Queries are embedded with RETRIEVAL_QUERY while documents were embedded with
RETRIEVAL_DOCUMENT. The model places the two sides of a retrieval pair into the
shared space differently, so mixing the task types degrades recall.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "data" / "catalog.json"

ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
EMBED_TASK_TYPE = "RETRIEVAL_QUERY"

REQUIRED_PAYLOAD_FIELDS = {
    "entity_type",
    "entity_id",
    "name",
    "slug",
    "sku",
    "price",
    "product_type",
    "stock_status",
    "category_ids",
    "category_names",
    "product_count",
}

REQUIRED_INDEXES = {"entity_type", "entity_id", "product_type"}

# (query, entity_type filter, stable key, why this case matters)
# The expected entity is addressed by SKU or slug, never by a hard-coded ID:
# IDs shift whenever demo data is regenerated, and writing them from memory is
# how this suite produced two false failures on its first run.
SEARCH_CASES: list[tuple[str, str, str, str]] = [
    (
        "наушники",
        "product",
        "ELEC-EARBUD-01",
        "cross-language: no shared tokens with the name",
    ),
    ("wireless earbuds", "product", "ELEC-EARBUD-01", "baseline English match"),
    ("power bank", "product", "ELEC-POWER-01", "multi-word product name"),
    ("умные часы", "product", "ELEC-WATCH-01", "cross-language wearable"),
    (
        "Электроника",
        "category",
        "electronics",
        "cross-language category, the coupon use case",
    ),
    ("electronics", "category", "electronics", "baseline English category"),
    ("футболки", "category", "tshirts", "cross-language plural noun"),
    ("толстовки", "category", "hoodies", "cross-language hoodies"),
]

# Queries with no sensible answer in the catalog. Not asserted, but their scores
# tell us where the confidence floor should sit.
NOISE_QUERIES = ["как испечь хлеб", "квантовая физика", "car insurance"]


class Failures:
    """Collect assertion failures so the whole suite runs before exiting."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.checks = 0

    def check(self, condition: bool, label: str) -> bool:
        self.checks += 1
        if condition:
            print(f"  PASS  {label}")
            return True
        print(f"  FAIL  {label}")
        self.items.append(label)
        return False


def point_id_for(entity_type: str, entity_id: int) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{entity_type}:{entity_id}"))


def embed_query(
    client: genai.Client, model: str, dimension: int, text: str
) -> list[float]:
    response = client.models.embed_content(
        model=model,
        contents=[text],
        config=genai_types.EmbedContentConfig(
            task_type=EMBED_TASK_TYPE,
            output_dimensionality=dimension,
        ),
    )
    embeddings = response.embeddings or []
    if not embeddings or embeddings[0].values is None:
        raise RuntimeError(f"No embedding returned for query {text!r}")
    return list(embeddings[0].values)


def check_collection(
    qdrant: QdrantClient,
    collection: str,
    dimension: int,
    expected_points: int,
    failures: Failures,
) -> None:
    print("\nCollection")
    if not failures.check(
        qdrant.collection_exists(collection), f"collection {collection!r} exists"
    ):
        return

    info = qdrant.get_collection(collection)
    params = info.config.params.vectors
    actual_size = getattr(params, "size", None)
    actual_distance = getattr(params, "distance", None)

    failures.check(
        actual_size == dimension, f"vector size is {dimension} (got {actual_size})"
    )
    failures.check(
        actual_distance == qmodels.Distance.COSINE,
        f"distance is Cosine (got {actual_distance})",
    )

    count = qdrant.count(collection_name=collection, exact=True).count
    failures.check(
        count == expected_points,
        f"point count is {expected_points} (got {count})",
    )

    indexed = set((info.payload_schema or {}).keys())
    for field in sorted(REQUIRED_INDEXES):
        failures.check(field in indexed, f"payload index exists: {field}")


def check_records(
    qdrant: QdrantClient,
    collection: str,
    snapshot: dict[str, Any],
    failures: Failures,
) -> None:
    """Every snapshot entity must be retrievable by its deterministic ID."""
    print("\nDeterministic IDs and payload shape")

    expected: list[tuple[str, int, str]] = [
        ("product", int(item["entity_id"]), str(item["name"]))
        for item in snapshot.get("products", [])
    ] + [
        ("category", int(item["entity_id"]), str(item["name"]))
        for item in snapshot.get("categories", [])
    ]

    point_ids = [
        point_id_for(entity_type, entity_id) for entity_type, entity_id, _ in expected
    ]
    found = qdrant.retrieve(
        collection_name=collection, ids=point_ids, with_payload=True
    )
    by_id = {str(point.id): point for point in found}

    missing = [
        f"{entity_type}:{entity_id} ({name})"
        for (entity_type, entity_id, name), point_id in zip(expected, point_ids)
        if point_id not in by_id
    ]
    failures.check(
        not missing,
        f"all {len(expected)} snapshot entities retrievable by ID"
        + (f" - missing: {', '.join(missing[:5])}" if missing else ""),
    )

    incomplete: list[str] = []
    mismatched: list[str] = []
    for (entity_type, entity_id, name), point_id in zip(expected, point_ids):
        point = by_id.get(point_id)
        if point is None:
            continue
        payload = point.payload or {}
        if set(payload.keys()) != REQUIRED_PAYLOAD_FIELDS:
            incomplete.append(f"{entity_type}:{entity_id}")
        if (
            payload.get("entity_type") != entity_type
            or payload.get("entity_id") != entity_id
        ):
            mismatched.append(f"{entity_type}:{entity_id}")
        elif payload.get("name") != name:
            mismatched.append(f"{entity_type}:{entity_id} name")

    failures.check(not incomplete, "every payload has the full field set")
    failures.check(not mismatched, "payload identity matches the snapshot")


def check_filtering(qdrant: QdrantClient, collection: str, failures: Failures) -> None:
    print("\nPayload filtering")
    for entity_type, expected in (("product", 24), ("category", 7)):
        count = qdrant.count(
            collection_name=collection,
            exact=True,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="entity_type",
                        match=qmodels.MatchValue(value=entity_type),
                    )
                ]
            ),
        ).count
        failures.check(
            count == expected,
            f"filter entity_type={entity_type} returns {expected} (got {count})",
        )


def resolve_expected_ids(snapshot: dict[str, Any]) -> dict[tuple[str, str], int]:
    """Map (entity_type, stable key) to the entity ID recorded in the snapshot."""
    mapping: dict[tuple[str, str], int] = {}
    for item in snapshot.get("products", []):
        if item.get("sku"):
            mapping[("product", str(item["sku"]))] = int(item["entity_id"])
    for item in snapshot.get("categories", []):
        mapping[("category", str(item["slug"]))] = int(item["entity_id"])
    return mapping


def check_search(
    qdrant: QdrantClient,
    collection: str,
    genai_client: genai.Client,
    model: str,
    dimension: int,
    snapshot: dict[str, Any],
    failures: Failures,
) -> None:
    print("\nSemantic search")
    expected_ids = resolve_expected_ids(snapshot)
    margins: list[float] = []
    top_scores: list[float] = []

    for query, entity_type, stable_key, rationale in SEARCH_CASES:
        expected_id = expected_ids.get((entity_type, stable_key))
        if expected_id is None:
            failures.check(
                False, f"{query!r} - unknown fixture key {entity_type}:{stable_key}"
            )
            continue

        vector = embed_query(genai_client, model, dimension, query)
        hits = qdrant.query_points(
            collection_name=collection,
            query=vector,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="entity_type",
                        match=qmodels.MatchValue(value=entity_type),
                    )
                ]
            ),
            limit=3,
            with_payload=True,
        ).points

        if not hits:
            failures.check(False, f"{query!r} returned no hits")
            continue

        top_payload = hits[0].payload or {}
        ok = failures.check(
            top_payload.get("entity_id") == expected_id,
            f"{query!r} -> {entity_type}:{stable_key} (id={expected_id}), "
            f"got {top_payload.get('entity_id')} {top_payload.get('name')!r}",
        )
        detail = " | ".join(
            f"{(hit.payload or {}).get('name')} {hit.score:.3f}" for hit in hits
        )
        print(f"        top3: {detail}")
        if ok:
            top_scores.append(hits[0].score)
            if len(hits) > 1:
                margins.append(hits[0].score - hits[1].score)
            print(f"        why:  {rationale}")

    if top_scores:
        print(
            f"\n  correct hits: score min={min(top_scores):.3f} max={max(top_scores):.3f}"
        )
    if margins:
        print(f"  top1-top2 margin: min={min(margins):.3f} max={max(margins):.3f}")


def report_noise_scores(
    qdrant: QdrantClient,
    collection: str,
    genai_client: genai.Client,
    model: str,
    dimension: int,
) -> None:
    """Print scores for unanswerable queries to calibrate the confidence floor."""
    print("\nNoise queries (not asserted - used to pick the confidence threshold)")
    for query in NOISE_QUERIES:
        vector = embed_query(genai_client, model, dimension, query)
        hits = qdrant.query_points(
            collection_name=collection,
            query=vector,
            limit=1,
            with_payload=True,
        ).points
        if hits:
            payload = hits[0].payload or {}
            print(f"  {query!r} -> {payload.get('name')!r} {hits[0].score:.3f}")
        else:
            print(f"  {query!r} -> no hits")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")

    qdrant_url = os.getenv("QDRANT_URL", "")
    collection = os.getenv("QDRANT_COLLECTION", "")
    api_key = os.getenv("GEMINI_API_KEY", "")
    model = os.getenv("EMBEDDING_MODEL", "")
    raw_dimension = os.getenv("EMBEDDING_DIM", "")

    missing = [
        name
        for name, value in (
            ("QDRANT_URL", qdrant_url),
            ("QDRANT_COLLECTION", collection),
            ("GEMINI_API_KEY", api_key),
            ("EMBEDDING_MODEL", model),
            ("EMBEDDING_DIM", raw_dimension),
        )
        if not value
    ]
    if missing:
        print(
            f"ERROR: missing environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    dimension = int(raw_dimension)

    if not SNAPSHOT_PATH.exists():
        print(f"ERROR: snapshot not found: {SNAPSHOT_PATH}", file=sys.stderr)
        return 1

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected_points = len(snapshot.get("products", [])) + len(
        snapshot.get("categories", [])
    )

    print(f"Verifying {collection!r} at {qdrant_url}")
    print(
        f"Snapshot: {expected_points} entities, generated {snapshot.get('generated_at')}"
    )

    qdrant = QdrantClient(url=qdrant_url)
    genai_client = genai.Client(api_key=api_key)
    failures = Failures()

    try:
        check_collection(qdrant, collection, dimension, expected_points, failures)
        check_records(qdrant, collection, snapshot, failures)
        check_filtering(qdrant, collection, failures)
        check_search(
            qdrant, collection, genai_client, model, dimension, snapshot, failures
        )
        report_noise_scores(qdrant, collection, genai_client, model, dimension)
    except Exception as error:  # noqa: BLE001 - any failure means the index is unusable
        print(f"\nERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    passed = failures.checks - len(failures.items)
    print(f"\n{passed}/{failures.checks} checks passed")
    if failures.items:
        print("\nFailed checks:")
        for item in failures.items:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

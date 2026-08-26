"""Embed the catalog snapshot and upsert it into Qdrant.

Reads data/catalog.json produced by fetch_catalog.py. Embeddings are billed
against a limited free-tier quota, so a dry run makes no API calls at all: it
prints the exact texts that would be embedded and stops. Only --apply talks to
Gemini and Qdrant.

Point IDs are derived from the entity identity (UUID5 over "product:2864"), so
re-indexing overwrites instead of duplicating. Qdrant only accepts UUIDs or
unsigned integers as point IDs, which is why the natural key is hashed rather
than used directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data" / "catalog.json"

# Stable namespace for deterministic point IDs. Changing it re-keys every
# point, which would orphan the previous generation instead of overwriting it.
ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

BATCH_SIZE = 20
# Free-tier limits reset on a per-minute basis, so a short pause between
# batches is more effective than retry backoff.
BATCH_PAUSE_SECONDS = 2.0
EMBED_TASK_TYPE = "RETRIEVAL_DOCUMENT"

# WooCommerce sample data ships placeholder descriptions that carry no
# information and are shared by most of the catalog. Cosine similarity compares
# direction, so an identical trailing sentence pulls unrelated products closer
# together - exactly the separation we need for retrieval. An absent
# description is an honest signal; a fake one is actively harmful.
BOILERPLATE_DESCRIPTIONS = {
    "this is a simple product.",
    "this is a variable product.",
    "this is a grouped product.",
    "this is an external product.",
    "this is a simple, virtual product.",
    "this is a simple, downloadable, virtual product.",
}


def is_boilerplate(description: str) -> bool:
    """Return True for WooCommerce placeholder descriptions."""
    return description.strip().lower() in BOILERPLATE_DESCRIPTIONS


def point_id_for(entity_type: str, entity_id: int) -> str:
    """Derive a deterministic point ID from the entity identity."""
    return str(uuid.uuid5(ID_NAMESPACE, f"{entity_type}:{entity_id}"))


def build_product_text(item: dict[str, Any]) -> str:
    """Compose the text that represents a product in vector space.

    The bare name is too sparse: "Wireless Earbuds Pro" shares no tokens with
    the Russian word for headphones. Category, SKU and a real description widen
    the semantic surface the query can latch onto. Placeholder descriptions are
    dropped rather than embedded.
    """
    parts: list[str] = [str(item["name"])]
    category_names = item.get("category_names") or []
    if category_names:
        parts.append("Category: " + ", ".join(str(name) for name in category_names))
    if item.get("sku"):
        parts.append(f"SKU: {item['sku']}")
    description = str(item.get("description") or "")
    if description and not is_boilerplate(description):
        parts.append(description)
    return " | ".join(parts)


def build_category_text(item: dict[str, Any]) -> str:
    """Compose the text that represents a category in vector space."""
    parts: list[str] = [f"Product category: {item['name']}"]
    description = str(item.get("description") or "")
    if description and not is_boilerplate(description):
        parts.append(description)
    parts.append(f"Contains {item['product_count']} product(s)")
    return " | ".join(parts)


def build_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten products and categories into a single list of indexable records."""
    records: list[dict[str, Any]] = []

    for item in snapshot.get("products", []):
        records.append(
            {
                "id": point_id_for("product", int(item["entity_id"])),
                "text": build_product_text(item),
                "payload": {
                    "entity_type": "product",
                    "entity_id": int(item["entity_id"]),
                    "name": item["name"],
                    "slug": item["slug"],
                    "sku": item.get("sku"),
                    "price": item.get("price"),
                    "product_type": item.get("product_type"),
                    "stock_status": item.get("stock_status"),
                    "category_ids": item.get("category_ids", []),
                    "category_names": item.get("category_names", []),
                    "product_count": None,
                },
            }
        )

    for item in snapshot.get("categories", []):
        records.append(
            {
                "id": point_id_for("category", int(item["entity_id"])),
                "text": build_category_text(item),
                "payload": {
                    "entity_type": "category",
                    "entity_id": int(item["entity_id"]),
                    "name": item["name"],
                    "slug": item["slug"],
                    "sku": None,
                    "price": None,
                    "product_type": None,
                    "stock_status": None,
                    "category_ids": [],
                    "category_names": [],
                    "product_count": int(item["product_count"]),
                },
            }
        )

    return records


def assert_unique_ids(records: list[dict[str, Any]]) -> None:
    """Fail loudly if two records collide on the same point ID."""
    seen: dict[str, str] = {}
    for record in records:
        point_id = str(record["id"])
        label = f"{record['payload']['entity_type']}:{record['payload']['entity_id']}"
        if point_id in seen:
            raise RuntimeError(f"Point ID collision: {seen[point_id]} and {label}")
        seen[point_id] = label


def embed_texts(
    client: genai.Client,
    model: str,
    dimension: int,
    texts: list[str],
) -> list[list[float]]:
    """Embed texts in batches, pausing between calls to respect rate limits."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        print(f"  embedding {start + 1}-{start + len(batch)} of {len(texts)} ...")
        response = client.models.embed_content(
            model=model,
            contents=batch,
            config=genai_types.EmbedContentConfig(
                task_type=EMBED_TASK_TYPE,
                output_dimensionality=dimension,
            ),
        )
        embeddings = response.embeddings or []
        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"Expected {len(batch)} embeddings, received {len(embeddings)}"
            )
        for embedding in embeddings:
            values = embedding.values
            if values is None:
                raise RuntimeError("Received an embedding without values")
            if len(values) != dimension:
                raise RuntimeError(
                    f"Expected {dimension} dimensions, received {len(values)}"
                )
            vectors.append(list(values))
        if start + BATCH_SIZE < len(texts):
            time.sleep(BATCH_PAUSE_SECONDS)
    return vectors


def ensure_collection(
    client: QdrantClient,
    collection: str,
    dimension: int,
    recreate: bool,
) -> None:
    """Create the collection and payload indexes if they are missing."""
    exists = client.collection_exists(collection)

    if exists and recreate:
        print(f"  dropping existing collection {collection!r}")
        client.delete_collection(collection)
        exists = False

    if not exists:
        print(f"  creating collection {collection!r} (dim={dimension}, cosine)")
        client.create_collection(
            collection_name=collection,
            vectors_config=qmodels.VectorParams(
                size=dimension,
                distance=qmodels.Distance.COSINE,
            ),
        )
    else:
        print(f"  collection {collection!r} already exists, reusing")

    # Filtering on an unindexed payload field falls back to a full scan. It is
    # irrelevant at 31 points and very relevant in production.
    for field_name, schema in (
        ("entity_type", qmodels.PayloadSchemaType.KEYWORD),
        ("entity_id", qmodels.PayloadSchemaType.INTEGER),
        ("product_type", qmodels.PayloadSchemaType.KEYWORD),
    ):
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=schema,
            wait=True,
        )
    print("  payload indexes ensured: entity_type, entity_id, product_type")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="call the embedding API and write to Qdrant (otherwise read-only)",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="drop the collection before indexing instead of upserting into it",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"catalog snapshot path (default: {DEFAULT_INPUT})",
    )
    args = parser.parse_args()

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

    try:
        dimension = int(raw_dimension)
    except ValueError:
        print(
            f"ERROR: EMBEDDING_DIM is not an integer: {raw_dimension!r}",
            file=sys.stderr,
        )
        return 1

    if not args.input.exists():
        print(
            f"ERROR: snapshot not found: {args.input}. Run fetch_catalog.py --apply first.",
            file=sys.stderr,
        )
        return 1

    snapshot = json.loads(args.input.read_text(encoding="utf-8"))
    records = build_records(snapshot)

    if not records:
        print("ERROR: snapshot contains no indexable records", file=sys.stderr)
        return 1

    try:
        assert_unique_ids(records)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    products = sum(1 for r in records if r["payload"]["entity_type"] == "product")
    categories = len(records) - products

    print(f"Snapshot: {args.input}")
    print(f"  generated_at: {snapshot.get('generated_at')}")
    print(f"Records: {len(records)} ({products} products, {categories} categories)")
    print(f"Model: {model} @ {dimension} dims")
    print(f"Target: {qdrant_url} / {collection}")

    print("\nTexts to embed")
    for record in records:
        print(f"  [{record['payload']['entity_type'][:4]}] {record['text']}")

    if not args.apply:
        print("\nDry run. No API calls made. Re-run with --apply to index.")
        return 0

    print("\nEmbedding ...")
    genai_client = genai.Client(api_key=api_key)
    try:
        vectors = embed_texts(
            genai_client, model, dimension, [str(r["text"]) for r in records]
        )
    except Exception as error:
        print(
            f"ERROR: embedding failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print("\nPreparing Qdrant ...")
    qdrant = QdrantClient(url=qdrant_url)
    try:
        ensure_collection(qdrant, collection, dimension, args.recreate)
        points = [
            qmodels.PointStruct(
                id=str(record["id"]),
                vector=vector,
                payload=dict(record["payload"]),
            )
            for record, vector in zip(records, vectors, strict=True)
        ]
        qdrant.upsert(collection_name=collection, points=points, wait=True)
        count = qdrant.count(collection_name=collection, exact=True).count
    except Exception as error:
        print(
            f"ERROR: Qdrant write failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"\nUpserted {len(points)} point(s). Collection now holds {count}.")
    if count != len(records):
        print(
            f"WARNING: expected {len(records)} points, collection reports {count}. "
            "Stale points from an earlier schema may remain - consider --recreate.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

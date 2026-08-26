"""Minimal WooCommerce REST API client shared by the seeding scripts.

Standard library only: these scripts must run on a bare Windows host without a
virtual environment. Authentication uses HTTP Basic Auth over HTTPS, which is
the method WooCommerce documents for SSL-enabled sites.

Orders live in HPOS tables, not in wp_posts, so the REST API is the only
supported way to read and write them. Never touch the database directly.

Timeouts are generous because LocalWP is slow: creating an order writes to
several HPOS tables and refreshes the analytics lookup tables.

Retries are restricted to idempotent methods. A POST that times out while
reading the response has most likely already been executed on the server, so
retrying it would duplicate data.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

REQUIRED_KEYS = (
    "WOO_STORE_URL",
    "WOO_CONSUMER_KEY",
    "WOO_CONSUMER_SECRET",
    "BUSINESS_TIMEZONE",
)

TIMEOUT_SECONDS = 180
MAX_RETRIES = 3
IDEMPOTENT_METHODS = ("GET", "HEAD", "PUT", "DELETE")


class WooError(RuntimeError):
    """Raised when the WooCommerce API returns an unrecoverable response."""


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Read a .env file into a dict and fail loudly on missing keys."""
    if not path.exists():
        raise WooError(f".env not found at {path}")

    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")

    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    if missing:
        raise WooError(f"Missing keys in .env: {', '.join(missing)}")

    # Environment variables win over the file, which keeps CI overrides simple.
    for key in REQUIRED_KEYS + ("WOO_VERIFY_SSL",):
        override = os.environ.get(key)
        if override:
            env[key] = override

    return env


class WooClient:
    """Thin wrapper over the WooCommerce v3 REST API."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env if env is not None else load_env()
        self.base_url = self.env["WOO_STORE_URL"].rstrip("/") + "/wp-json/wc/v3"

        credentials = (
            f"{self.env['WOO_CONSUMER_KEY']}:{self.env['WOO_CONSUMER_SECRET']}"
        )
        self.auth_header = "Basic " + base64.b64encode(
            credentials.encode("utf-8")
        ).decode("ascii")

        verify = self.env.get("WOO_VERIFY_SSL", "true").lower() != "false"
        self.verify_ssl = verify
        if verify:
            self.ssl_context = ssl.create_default_context()
        else:
            # Local stand only: LocalWP issues a self-signed certificate.
            # This must never be used against a production store.
            self.ssl_context = ssl._create_unverified_context()

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | list[Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Perform one API call and return (parsed_body, headers)."""
        method = method.upper()
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        retries = MAX_RETRIES if method in IDEMPOTENT_METHODS else 1
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            request = urllib.request.Request(url=url, data=body, method=method)
            request.add_header("Authorization", self.auth_header)
            request.add_header("Content-Type", "application/json")
            request.add_header("Accept", "application/json")

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=timeout or TIMEOUT_SECONDS,
                    context=self.ssl_context,
                ) as response:
                    raw = response.read().decode("utf-8")
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    return (json.loads(raw) if raw else None), headers

            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:300]
                # 4xx means the request itself is wrong: retrying cannot help.
                if error.code < 500:
                    raise WooError(
                        f"{method} {url} -> HTTP {error.code}: {detail}"
                    ) from error
                last_error = WooError(f"{method} {url} -> HTTP {error.code}: {detail}")

            except urllib.error.URLError as error:
                last_error = WooError(
                    f"{method} {url} -> network error: {error.reason}"
                )

            except TimeoutError as error:
                # Note: TimeoutError does not inherit from URLError, so it must
                # be handled separately. For a write this is never retried.
                last_error = WooError(
                    f"{method} {url} -> timed out after {timeout or TIMEOUT_SECONDS}s: {error}"
                )
                if method not in IDEMPOTENT_METHODS:
                    raise WooError(
                        f"{method} {url} timed out. The server may have applied the change "
                        "anyway. Verify the store state before retrying."
                    ) from error

            if attempt < retries:
                time.sleep(2**attempt)

        raise WooError(f"Failed after {retries} attempt(s): {last_error}")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)[0]

    def post(self, path: str, payload: dict[str, Any] | list[Any]) -> Any:
        return self.request("POST", path, payload=payload)[0]

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)[0]

    def count(self, path: str, params: dict[str, Any] | None = None) -> int:
        """Return the total number of records via the X-WP-Total header."""
        query = dict(params or {})
        query["per_page"] = 1
        _, headers = self.request("GET", path, params=query)
        return int(headers.get("x-wp-total", 0))

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Fetch every page of a collection endpoint."""
        results: list[Any] = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"per_page": 100, "page": page})
            batch = self.get(path, params=query)
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results


def main() -> int:
    """Smoke test: prove the host can authenticate against the store."""
    try:
        client = WooClient()
    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    print(f"store:      {client.env['WOO_STORE_URL']}")
    print(f"verify_ssl: {client.verify_ssl}")
    print(f"timezone:   {client.env['BUSINESS_TIMEZONE']}")

    try:
        system_status = client.get("system_status")
        settings = system_status.get("settings", {})
        print(f"currency:   {settings.get('currency')}")
        print(f"products:   {client.count('products')}")
        print(f"orders:     {client.count('orders', {'status': 'any'})}")
        print(f"coupons:    {client.count('coupons')}")
        print("OK: authenticated write-capable connection confirmed")
    except WooError as error:
        print(f"FAIL: {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

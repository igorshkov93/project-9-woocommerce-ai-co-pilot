"""Minimal HTTP client for the Copilot REST namespace.

Uses only the standard library, matching woo_client.py. The point is that this
project's scripts run on a bare Python install without a dependency step.

Authentication is a WordPress Application Password over HTTP Basic. WooCommerce
consumer keys do not work here: that scheme is scoped to the wc/v3 namespace.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class CopilotClientError(RuntimeError):
    """Raised when the Copilot API cannot be reached or returns an error."""


def load_env(path: Path | None = None) -> dict[str, str]:
    """Reads key=value pairs from the project .env file.

    Values are not unquoted or expanded: the file is written by hand and kept
    simple on purpose. Lines without an equals sign are ignored.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"

    if not path.is_file():
        raise CopilotClientError(f".env not found at {path}")

    values: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    return values


class CopilotClient:
    """Thin wrapper around the copilot/v1 REST namespace."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        timeout: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        credentials = f"{username}:{password}".encode()
        self.auth_header = "Basic " + base64.b64encode(credentials).decode("ascii")

        # The LocalWP certificate is trusted on the host, so verification stays
        # on by default. Inside containers it is not, which is why the flag
        # exists at all.
        self.ssl_context = ssl.create_default_context()

        if not verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    @classmethod
    def from_env(cls, verify_ssl: bool = True) -> CopilotClient:
        """Builds a client from the project .env file."""
        env = load_env()

        missing = [
            key
            for key in ("COPILOT_API_BASE", "WP_APP_USER", "WP_APP_PASSWORD")
            if not env.get(key)
        ]

        if missing:
            raise CopilotClientError(
                "Missing required .env keys: " + ", ".join(missing)
            )

        return cls(
            base_url=env["COPILOT_API_BASE"],
            username=env["WP_APP_USER"],
            password=env["WP_APP_PASSWORD"],
            verify_ssl=verify_ssl,
        )

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, Any]:
        """Performs a GET request and returns the status code and parsed body.

        HTTP errors are returned rather than raised: the verification script
        needs to assert on a 401, and an exception would make that awkward.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        if params:
            normalised = {
                key: ("true" if value is True else "false" if value is False else value)
                for key, value in params.items()
            }
            url = f"{url}?{urllib.parse.urlencode(normalised)}"

        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/json")

        if authenticated:
            request.add_header("Authorization", self.auth_header)

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")

            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, {"raw": raw}
        except urllib.error.URLError as error:
            raise CopilotClientError(f"Cannot reach {url}: {error.reason}") from error
        except TimeoutError as error:
            # TimeoutError does not inherit from URLError, so it needs its own
            # branch. This bit us during order seeding in step 7.
            raise CopilotClientError(f"Timed out calling {url}") from error

    def get_abandoned_carts(self, **params: Any) -> tuple[int, Any]:
        """Convenience wrapper for the abandoned carts route."""
        return self.get("abandoned-carts", params=params)


def get_env_value(key: str, default: str = "") -> str:
    """Reads a value from the environment, falling back to the .env file."""
    if key in os.environ:
        return os.environ[key]

    return load_env().get(key, default)
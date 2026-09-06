"""Turso client over HTTPS (no native libsql dependency).

TURSO_DATABASE_URL looks like ``libsql://<host>``; the HTTP HRM endpoint is
``https://<host>/v2/pipeline`` with ``Authorization: Bearer <token>``.
This keeps the service pure-Python so it runs on Render Free and Termux.

Docs: https://docs.turso.tech/sdk/http/reference
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Sequence

import httpx

log = logging.getLogger(__name__)


def libsql_to_https(url: str) -> str:
    url = url.strip()
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return url
    return "https://" + url


class TursoClient:
    """Minimal HRM pipeline client: execute / query with positional args."""

    def __init__(self, database_url: str, auth_token: str, timeout: int = 30):
        if not database_url:
            raise ValueError("TURSO_DATABASE_URL is not set")
        self.base = libsql_to_https(database_url).rstrip("/")
        self.token = auth_token or ""
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _stmt(self, sql: str, args: Sequence[Any] | None):
        arg_list = []
        for value in args or []:
            if value is None:
                arg_list.append({"type": "null"})
            elif isinstance(value, bool):
                arg_list.append({"type": "integer", "value": "1" if value else "0"})
            elif isinstance(value, int):
                arg_list.append({"type": "integer", "value": str(value)})
            elif isinstance(value, float):
                arg_list.append({"type": "float", "value": value})
            else:
                arg_list.append({"type": "text", "value": str(value)})
        return {"sql": sql, "args": arg_list}

    def batch(self, statements: Sequence[tuple[str, Sequence[Any] | None]]) -> list[dict]:
        payload = {
            "requests": [
                {"type": "execute", "stmt": self._stmt(sql, args)}
                for sql, args in statements
            ]
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base}/v2/pipeline", headers=self._headers(), json=payload
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        results = []
        for res in data.get("results", []):
            if res.get("type") == "error":
                raise RuntimeError(f"Turso error: {res.get('error')}")
            results.append(res.get("response", {}).get("result", {}))
        return results

    def execute(self, sql: str, args: Sequence[Any] | None = None) -> dict:
        return self.batch([(sql, args)])[0]

    def query_dicts(self, sql: str, args: Sequence[Any] | None = None) -> list[dict]:
        result = self.execute(sql, args)
        cols = [c["name"] for c in result.get("cols", [])]
        out = []
        for row in result.get("rows", []):
            item = {}
            for name, cell in zip(cols, row):
                if cell is None or cell.get("type") == "null":
                    item[name] = None
                else:
                    item[name] = cell.get("value")
            out.append(item)
        return out


def memory_conn() -> sqlite3.Connection:
    """In-memory sqlite for unit tests (never used in production)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn

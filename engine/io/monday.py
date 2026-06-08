"""Monday GraphQL client.

Wraps httpx with Monday-specific concerns: auth header, API version, error
unwrapping, complexity-budget awareness, batched mutations.

Two account contexts (Phase 1 uses only Gray Space; Phase 2 adds Nexiuum):
- gray_space_client() — token for the Gray Space Monday account
- nexiuum_client() — token for the Nexiuum Monday account (empty in Phase 1)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from engine.config import get_settings

log = logging.getLogger(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2024-10"  # current stable at time of writing


# GraphQL fragment for column_values that surfaces typed unions for
# board_relation, dependency, and mirror columns (which return null in the
# generic `value` field).
_COLUMN_VALUES_FRAGMENT = """
column_values {
  id
  text
  value
  type
  ... on BoardRelationValue { linked_item_ids display_value }
  ... on DependencyValue { linked_item_ids display_value }
  ... on MirrorValue { display_value }
  ... on FormulaValue { display_value }
}
"""

_FIRST_ITEMS_PAGE_QUERY = (
    "query ($board: ID!) { boards(ids: [$board]) { items_page(limit: %d) { cursor items { id name "
    + _COLUMN_VALUES_FRAGMENT
    + " } } } }"
)

_NEXT_ITEMS_PAGE_QUERY = (
    "query ($cursor: String!) { next_items_page(limit: %d, cursor: $cursor) { cursor items { id name "
    + _COLUMN_VALUES_FRAGMENT
    + " } } }"
)

# Column-filtered variants — pull only the named columns, which slashes Monday
# query complexity (and the per-minute rate-limit pressure) when the caller
# needs just a field or two off a whole board (e.g. the backlog scan reading
# Spec Sheet Payload + N#). Mirror the typed unions so board_relation /
# mirror columns still surface display_value.
_FILTERED_COLUMN_VALUES = """
column_values(ids: $cols) {
  id
  text
  value
  type
  ... on BoardRelationValue { linked_item_ids display_value }
  ... on MirrorValue { display_value }
}
"""

_FIRST_ITEMS_PAGE_QUERY_COLS = (
    "query ($board: ID!, $cols: [String!]) { boards(ids: [$board]) { items_page(limit: %d) { cursor items { id name "
    + _FILTERED_COLUMN_VALUES
    + " } } } }"
)

_NEXT_ITEMS_PAGE_QUERY_COLS = (
    "query ($cursor: String!, $cols: [String!]) { next_items_page(limit: %d, cursor: $cursor) { cursor items { id name "
    + _FILTERED_COLUMN_VALUES
    + " } } }"
)


class MondayError(RuntimeError):
    """Raised when Monday returns errors in the GraphQL response."""

    def __init__(self, errors: list[dict[str, Any]], query: str | None = None):
        self.errors = errors
        self.query = query
        super().__init__(self._format())

    def _format(self) -> str:
        msgs = [e.get("message", str(e)) for e in self.errors]
        return "Monday API errors: " + "; ".join(msgs)


class MondayClient:
    """Async Monday GraphQL client. One per account token."""

    def __init__(self, token: str, *, timeout_seconds: float = 30.0):
        self._token = token
        self._timeout = timeout_seconds
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MondayClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "Authorization": self._token,
                "API-Version": MONDAY_API_VERSION,
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query/mutation and return the `data` object.

        Raises MondayError on response errors. Raises httpx.HTTPError on
        transport errors.
        """
        if self._http is None:
            raise RuntimeError("MondayClient must be used as an async context manager")
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        resp = await self._http.post(MONDAY_API_URL, json=body)
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload and payload["errors"]:
            raise MondayError(payload["errors"], query=query)
        data = payload.get("data", {})
        if log.isEnabledFor(logging.DEBUG):
            complexity = payload.get("extensions", {}).get("complexity")
            if complexity:
                log.debug("Monday complexity: %s", complexity)
        return data

    async def query_collecting_errors(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Like `query`, but returns (data, errors) instead of raising on errors.

        Used for batched aliased mutations where Monday may partially succeed:
        some aliases return rows in `data`, others are nulled with corresponding
        entries in `errors` (each with a `path` indicating the failed alias).
        Callers reconcile per-alias themselves.

        Transport errors still raise (httpx.HTTPError, status_code != 2xx).
        If `data` is missing entirely (top-level query parse failure), returns
        ({}, errors) — caller must treat that as full failure.
        """
        if self._http is None:
            raise RuntimeError("MondayClient must be used as an async context manager")
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        resp = await self._http.post(MONDAY_API_URL, json=body)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        errors = payload.get("errors") or []
        return data, errors

    # ── High-level mutations ────────────────────────────────────────────

    async def delete_items(
        self, item_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        """Batch-delete items by id. Best-effort, never raises on Monday errors.

        Returns `(deleted_ids, error_messages)`. Used by apply_plan's rollback
        (#12) to remove slots created during a failed apply. Like the batched
        write path, Monday executes aliased deletes sequentially and may
        partially fail: each alias either returns its id (deleted) or produces a
        per-alias error routed back to its slot id. Transport errors still raise.
        """
        if not item_ids:
            return [], []

        pieces: list[str] = []
        variables: dict[str, Any] = {}
        var_decls: list[str] = []
        alias_to_id: dict[str, str] = {}
        for i, item_id in enumerate(item_ids):
            alias = f"d{i}"
            var = f"id_{i}"
            alias_to_id[alias] = str(item_id)
            variables[var] = str(item_id)
            var_decls.append(f"${var}: ID!")
            pieces.append(f"{alias}: delete_item(item_id: ${var}) {{ id }}")

        mutation = (
            f"mutation({', '.join(var_decls)}) {{\n  " + "\n  ".join(pieces) + "\n}"
        )
        data, gql_errors = await self.query_collecting_errors(mutation, variables=variables)

        errors_by_alias: dict[str, list[str]] = {}
        unrouted: list[str] = []
        for err in gql_errors:
            msg = err.get("message", "Monday returned an unspecified error")
            path = err.get("path") or []
            if path and isinstance(path[0], str):
                errors_by_alias.setdefault(path[0], []).append(msg)
            else:
                unrouted.append(msg)

        deleted: list[str] = []
        error_messages: list[str] = []
        for alias, item_id in alias_to_id.items():
            payload = data.get(alias)
            alias_errors = errors_by_alias.get(alias) or []
            # An id coming back means the item is gone — treat it as deleted even
            # if Monday co-returns a warning (mirrors the create path's "id ==
            # exists" signal). Only a missing id is a real rollback failure.
            if payload and "id" in payload:
                deleted.append(item_id)
            else:
                detail = "; ".join(alias_errors) if alias_errors else (
                    "no id returned and no per-alias error from Monday"
                )
                error_messages.append(f"item {item_id}: {detail}")
        error_messages.extend(unrouted)
        return deleted, error_messages

    # ── High-level board reads ──────────────────────────────────────────

    async def fetch_board_items(
        self,
        board_id: int,
        *,
        page_size: int = 500,
        column_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all items on a board with their column values.

        Paginates via items_page cursor. Returns a flat list of item dicts:
            { "id": str, "name": str, "column_values": [...] }

        When `column_ids` is given, only those columns are requested — far less
        Monday query complexity (and rate-limit pressure) than pulling every
        column. Pass it whenever the caller needs only a field or two off a
        whole board.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        filtered = bool(column_ids)

        while True:
            if cursor:
                if filtered:
                    query = _NEXT_ITEMS_PAGE_QUERY_COLS % page_size
                    data = await self.query(query, {"cursor": cursor, "cols": column_ids})
                else:
                    query = _NEXT_ITEMS_PAGE_QUERY % page_size
                    data = await self.query(query, {"cursor": cursor})
                page = data["next_items_page"]
            else:
                if filtered:
                    query = _FIRST_ITEMS_PAGE_QUERY_COLS % page_size
                    data = await self.query(query, {"board": str(board_id), "cols": column_ids})
                else:
                    query = _FIRST_ITEMS_PAGE_QUERY % page_size
                    data = await self.query(query, {"board": str(board_id)})
                boards = data.get("boards", [])
                if not boards:
                    return items
                page = boards[0]["items_page"]

            items.extend(page["items"])
            cursor = page.get("cursor")
            if not cursor:
                break

        return items

    async def fetch_board_relation_links(
        self,
        item_ids: list[str],
        column_id: str,
    ) -> dict[str, list[str]]:
        """For a list of items, resolve a board_relation column to linked item IDs.

        Returns a mapping `{item_id: [linked_id, ...]}`. Necessary because the
        generic `column_values` query doesn't always surface linked IDs reliably.
        """
        if not item_ids:
            return {}
        query = """
        query ($items: [ID!]!, $col: [String!]) {
          items(ids: $items) {
            id
            column_values(ids: $col) {
              ... on BoardRelationValue {
                linked_item_ids
              }
            }
          }
        }
        """
        data = await self.query(query, {"items": item_ids, "col": [column_id]})
        result: dict[str, list[str]] = {}
        for item in data.get("items", []) or []:
            col_values = item.get("column_values") or []
            linked: list[str] = []
            for cv in col_values:
                if cv and "linked_item_ids" in cv:
                    linked = [str(x) for x in (cv["linked_item_ids"] or [])]
                    break
            result[str(item["id"])] = linked
        return result


    async def fetch_item(
        self, item_id: str, *, column_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single item by ID with selected column values.

        Returns `None` when the item doesn't exist or the API surfaces no
        item (e.g., the engine's token can't see the board). The caller
        decides whether that's an error.

        `column_ids` filter narrows the response payload to just the
        columns the caller needs — Production Schedule items have a lot
        of columns and the engine only reads `Spec Sheet Payload` plus a
        few status fields.
        """
        if column_ids:
            query = """
            query ($items: [ID!]!, $cols: [String!]) {
              items(ids: $items) {
                id
                name
                column_values(ids: $cols) {
                  id
                  text
                  value
                  ... on BoardRelationValue { display_value }
                  ... on MirrorValue { display_value }
                }
              }
            }
            """
            data = await self.query(
                query, {"items": [item_id], "cols": column_ids},
            )
        else:
            query = """
            query ($items: [ID!]!) {
              items(ids: $items) {
                id
                name
                column_values {
                  id
                  text
                  value
                }
              }
            }
            """
            data = await self.query(query, {"items": [item_id]})
        items = data.get("items") or []
        return items[0] if items else None


# ── Convenience factories ───────────────────────────────────────────────


def gray_space_client() -> MondayClient:
    return MondayClient(token=get_settings().gray_space_monday_token)


def nexiuum_client() -> MondayClient:
    token = get_settings().nexiuum_monday_token
    if not token:
        raise RuntimeError("NEXIUUM_MONDAY_TOKEN not configured (Phase 2)")
    return MondayClient(token=token)

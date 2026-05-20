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

    # ── High-level board reads ──────────────────────────────────────────

    async def fetch_board_items(
        self,
        board_id: int,
        *,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch all items on a board with their column values.

        Paginates via items_page cursor. Returns a flat list of item dicts:
            { "id": str, "name": str, "column_values": [...] }
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            if cursor:
                query = _NEXT_ITEMS_PAGE_QUERY % page_size
                data = await self.query(query, {"cursor": cursor})
                page = data["next_items_page"]
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


# ── Convenience factories ───────────────────────────────────────────────


def gray_space_client() -> MondayClient:
    return MondayClient(token=get_settings().gray_space_monday_token)


def nexiuum_client() -> MondayClient:
    token = get_settings().nexiuum_monday_token
    if not token:
        raise RuntimeError("NEXIUUM_MONDAY_TOKEN not configured (Phase 2)")
    return MondayClient(token=token)

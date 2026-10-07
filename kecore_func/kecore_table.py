"""Table storage for real tickets (V10 slice 4): the Function's managed identity, no key.

One table (``KECORE_TICKETS_TABLE``, default ``tickets``), one partition per client
(``PartitionKey``), one row per ticket (``RowKey`` = ticket id, see ``kecore.tickets``).
"""

from __future__ import annotations

import os

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential


class TableStorage:
    """``kecore_func.tickets_service`` over one storage account (KECORE_BLOB_ENDPOINT's account,
    its Table endpoint instead of its Blob endpoint)."""

    def __init__(self, endpoint: str | None = None, credential=None, table: str | None = None):
        account_url = endpoint or os.environ["KECORE_BLOB_ENDPOINT"].replace(".blob.", ".table.")
        self._service = TableServiceClient(endpoint=account_url, credential=credential or DefaultAzureCredential())
        self._table_name = table or os.environ.get("KECORE_TICKETS_TABLE", "tickets")
        self._client = self._service.create_table_if_not_exists(self._table_name)

    def upsert(self, entity: dict) -> None:
        self._client.upsert_entity(entity, mode="replace")

    def list(self, client: str) -> list[dict]:
        return list(self._client.query_entities(f"PartitionKey eq '{client}'"))

    def count(self, client: str) -> int:
        return sum(1 for _ in self._client.query_entities(f"PartitionKey eq '{client}'", select=["RowKey"]))

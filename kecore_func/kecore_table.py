"""Table storage for the V10 engine's own tables: the Function's managed identity, no key.

One partition per client (``PartitionKey``) in every table:
  tickets       one row per scrubbed ticket (``RowKey`` = ticket id, ``kecore.tickets``), plus the
                last blank run's finding for it (``kefind_*`` properties, merged in)
  kefindfiches  the fiches a labeler may pick (one row per fiche of the KB map last used)
  ticketlabels  the human labels, written by the Web App's labeling tab, read here
  kecorescores  one row per scoreboard run (the headline numbers)

Writes MERGE by default: a property this code does not write (a label, a finding) is never
erased by a write that does not carry it.
"""

from __future__ import annotations

import os

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential

TICKETS = "tickets"
FICHES = "kefindfiches"
LABELS = "ticketlabels"
SCORES = "kecorescores"


def _endpoint() -> str:
    endpoint = os.environ.get("KECORE_TABLE_ENDPOINT")
    if endpoint:
        return endpoint
    return os.environ["KECORE_BLOB_ENDPOINT"].replace(".blob.", ".table.")


class TableStorage:
    """One table of the storage account (``KECORE_TABLE_ENDPOINT``), keyless."""

    def __init__(self, table: str | None = None, endpoint: str | None = None, credential=None,
                 service: TableServiceClient | None = None):
        self._service = service or TableServiceClient(endpoint=endpoint or _endpoint(),
                                                      credential=credential or DefaultAzureCredential())
        self.name = table or os.environ.get("KECORE_TICKETS_TABLE", TICKETS)
        self._client = self._service.create_table_if_not_exists(self.name)

    def upsert(self, entity: dict) -> None:
        self._client.upsert_entity(entity, mode=UpdateMode.MERGE)

    merge = upsert

    def replace(self, entity: dict) -> None:
        self._client.upsert_entity(entity, mode=UpdateMode.REPLACE)

    def get(self, client: str, row_key: str) -> dict | None:
        try:
            return dict(self._client.get_entity(partition_key=client, row_key=row_key))
        except ResourceNotFoundError:
            return None

    def list(self, client: str, select: list[str] | None = None) -> list[dict]:
        rows = self._client.query_entities("PartitionKey eq @pk", parameters={"pk": client}, select=select)
        return sorted((dict(row) for row in rows), key=lambda row: row["RowKey"])

    def count(self, client: str) -> int:
        return len(self.list(client, select=["RowKey"]))

    def delete(self, client: str, row_key: str) -> None:
        try:
            self._client.delete_entity(partition_key=client, row_key=row_key)
        except ResourceNotFoundError:
            pass

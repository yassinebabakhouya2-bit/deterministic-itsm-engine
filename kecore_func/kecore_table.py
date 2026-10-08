"""Table storage for the V10 engine's own tables: the Function's managed identity, no key.

One partition per client (``PartitionKey``) in every table:
  tickets       one row per scrubbed ticket (``RowKey`` = ticket id, ``kecore.tickets``), plus the
                last blank run's finding for it (``kefind_*`` properties, merged in)
  kefindfiches  the fiches a labeler may pick (one row per fiche of the KB map last used)
  ticketlabels  the human labels, written by the Web App's labeling tab, read here
  kecorescores  one row per scoreboard run (the headline numbers)
  kefindpending the dictionary's candidate names seen in live questions (dictionary_service.py)

Writes MERGE by default: a property this code does not write (a label, a finding) is never
erased by a write that does not carry it. Where two writers may meet on one row (the dictionary's
candidates and decisions), ``read`` / ``create`` / ``merge_if`` give optimistic concurrency: an
atomic insert, then merges that only apply to the row as it was read (ETag, If-Match).
"""

from __future__ import annotations

import os

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential

TICKETS = "tickets"
FICHES = "kefindfiches"
LABELS = "ticketlabels"
SCORES = "kecorescores"
PENDING = "kefindpending"


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

    def read(self, client: str, row_key: str) -> tuple[dict | None, str | None]:
        """The row and its ETag, or (None, None)."""
        try:
            entity = self._client.get_entity(partition_key=client, row_key=row_key)
        except ResourceNotFoundError:
            return None, None
        return dict(entity), entity.metadata.get("etag")

    def create(self, entity: dict) -> bool:
        """Inserts the row; False when it already exists (another writer created it first)."""
        try:
            self._client.create_entity(entity)
        except ResourceExistsError:
            return False
        return True

    def merge_if(self, entity: dict, etag: str) -> bool:
        """Merges into the row only as it was read (If-Match); False when it changed or vanished."""
        try:
            self._client.update_entity(entity, mode=UpdateMode.MERGE, etag=etag,
                                       match_condition=MatchConditions.IfNotModified)
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        return True

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

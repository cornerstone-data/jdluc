"""BQ-to-GCS publish target.

Exports the regional ``transitions`` and ``crops`` BigQuery tables to GCS
as CSV files. BQ may shard large tables into multiple files; the wildcard
destination URI (``{name}-*.csv``) handles that transparently.

Architecture mirrors ``bigquery.py``:
- One shared primitive (``export_bq_table_to_gcs``) carries the extract-job
  and cache-probe logic.
- Two thin per-table wrappers exist for call-site clarity.
"""

import logging

from jdluc.publish.publish import GCSExportResult
from jdluc.utils.asset_management import parse_compound_version_from_asset_id
from jdluc.utils.constants import BQ_PROJECT, GCS_BUCKET_NAME, GCS_TABLE_PREFIX

logger = logging.getLogger(__name__)


def build_table_gcs_uri(table_id: str) -> str:
    _, _, table_name = table_id.rpartition('.')
    return f'gs://{GCS_BUCKET_NAME}/{GCS_TABLE_PREFIX}/{table_name}-*.csv'


def gcs_csv_exists(gcs_uri: str) -> bool:
    from google.cloud import storage

    # Strip 'gs://{bucket}/' and the trailing wildcard to get a list prefix.
    prefix = gcs_uri.removeprefix(f'gs://{GCS_BUCKET_NAME}/').replace('*', '')
    client = storage.Client()
    blobs = client.list_blobs(GCS_BUCKET_NAME, prefix=prefix, max_results=1)
    return any(True for _ in blobs)


def export_bq_table_to_gcs(table_id: str, force: bool = False) -> GCSExportResult:
    versions = parse_compound_version_from_asset_id(table_id)
    if versions is None:
        raise ValueError(
            f'table_id {table_id!r} does not end in a compound '
            '{transform_sha}_{publish_sha} suffix.'
        )
    transform_version, publish_version = versions
    gcs_uri = build_table_gcs_uri(table_id)

    if not force and gcs_csv_exists(gcs_uri):
        logger.info(f'BQ->GCS cache hit: {gcs_uri}')
        return GCSExportResult(
            gcs_uri=gcs_uri,
            transform_version=transform_version,
            publish_version=publish_version,
            from_cache=True,
        )

    logger.info(f'BQ->GCS export: {table_id} -> {gcs_uri}')
    try:
        from google.cloud import bigquery

        job = bigquery.Client(project=BQ_PROJECT).extract_table(
            table_id,
            gcs_uri,
            job_config=bigquery.ExtractJobConfig(
                destination_format=bigquery.DestinationFormat.CSV,
            ),
        )
        job.result()
    except Exception as exc:
        logger.error(f'BQ->GCS export failed for {table_id}: {exc}')
        return GCSExportResult(
            gcs_uri=gcs_uri,
            transform_version=transform_version,
            publish_version=publish_version,
            from_cache=False,
            error=str(exc),
        )

    return GCSExportResult(
        gcs_uri=gcs_uri,
        transform_version=transform_version,
        publish_version=publish_version,
        from_cache=False,
    )

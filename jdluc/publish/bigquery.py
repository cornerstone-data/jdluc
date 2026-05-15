"""BigQuery publish target.

Exports the regional ``transitions`` and ``crops`` GEE table assets
produced by the transform stage into BigQuery for downstream SQL
queries. Both tables land in the same ``{BQ_PROJECT}.{BQ_DATASET}`` and
carry compound-keyed names of the form
``{prefix}_{region}_{transform_sha}_{publish_sha}`` — the name alone is
the cache key, so a content change in either dimension produces a new
table and no silent overwrites are possible.

Architecture:
- One shared primitive (``export_feature_collection_to_bigquery``)
  carries the real export + cache-probe logic.
- Two thin per-table wrappers (``export_transitions_to_bigquery``,
  ``export_crops_to_bigquery``) exist for call-site clarity and to
  anchor per-table schema documentation.

See specs/pipeline_tech_design.md § Publish.
"""

import logging

import ee

from jdluc.publish.publish import BigQueryExportResult
from jdluc.utils.asset_management import (
    parse_compound_version_from_asset_id,
)
from jdluc.utils.constants import (
    BQ_CROPS_TABLE_PREFIX,
    BQ_DATASET,
    BQ_JOB_LOCATION,
    BQ_PROJECT,
    BQ_TRANSITIONS_TABLE_PREFIX,
)
from jdluc.utils.gee import wait_for_export_task

# [[NOTE: ``BQ_JOB_LOCATION`` in utils/constants.py pins the dataset
# location for documentation/future use. ``ee.batch.Export.table.toBigQuery``
# does not accept an explicit location — it infers from the target
# project.dataset pair — so the constant is not referenced at the call
# site here. If we ever switch to a ``google-cloud-bigquery`` load-job
# pattern, the constant becomes load-bearing and gets passed through.]]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table-ID builders
# ---------------------------------------------------------------------------


def build_transitions_bq_table_id(
    region_name: str,
    transform_version: str,
    publish_version: str,
) -> str:
    """Compound-keyed BQ table ID for the ``transitions`` target.

    Schema: ``TRANSITIONS_TABLE_COLUMNS`` — county_fips × epoch_transition ×
    emissions_type grain.
    """
    return _build_table_id(
        BQ_TRANSITIONS_TABLE_PREFIX, region_name, transform_version, publish_version
    )


def build_crops_bq_table_id(
    region_name: str,
    transform_version: str,
    publish_version: str,
) -> str:
    """Compound-keyed BQ table ID for the ``crops`` target.

    Schema: ``CROPS_TABLE_COLUMNS`` — county_fips × crop_code grain, with
    emissions factors, yields, and driver-breakdown columns attached.
    """
    return _build_table_id(
        BQ_CROPS_TABLE_PREFIX, region_name, transform_version, publish_version
    )


def _build_table_id(
    prefix: str,
    region_name: str,
    transform_version: str,
    publish_version: str,
) -> str:
    table_short = f'{prefix}_{region_name}_{transform_version}_{publish_version}'
    return f'{BQ_PROJECT}.{BQ_DATASET}.{table_short}'


# ---------------------------------------------------------------------------
# Cache probe
# ---------------------------------------------------------------------------


def bq_table_exists(table_id: str) -> bool:
    """Return True iff the BQ table exists.

    Uses ``google-cloud-bigquery.Client().get_table``; a ``NotFound``
    exception (google-api-core) resolves to False. Lazy imports the BQ
    client so modules that don't need a cache probe (e.g. the `publish.py`
    import) don't pay for it.
    """
    # Lazy imports: the BQ client is a heavy transitive dep.
    from google.api_core import exceptions as gcs_exceptions
    from google.cloud import bigquery

    client = bigquery.Client(project=BQ_PROJECT)
    try:
        client.get_table(table_id)
    except gcs_exceptions.NotFound:
        return False
    return True


def ensure_bq_dataset_exists() -> None:
    """Create ``{BQ_PROJECT}.{BQ_DATASET}`` if it doesn't already exist.

    The first publish on a fresh project would otherwise fail with
    ``Not found: Dataset`` from ``Export.table.toBigQuery`` — auto-creating
    here removes a manual prereq step from pipeline bring-up. Idempotent:
    a 409 (already-exists, including races between parallel runs) is
    treated as success.

    Raises whatever ``google-cloud-bigquery`` raises if the caller lacks
    BigQuery dataset-create permission — fail-fast is correct since the
    ``Export.table.toBigQuery`` calls would also fail.
    """
    from google.api_core import exceptions as gcs_exceptions
    from google.cloud import bigquery

    client = bigquery.Client(project=BQ_PROJECT)
    dataset_id = f'{BQ_PROJECT}.{BQ_DATASET}'
    try:
        client.get_dataset(dataset_id)
        return
    except gcs_exceptions.NotFound:
        pass

    logger.info(f'Creating BQ dataset {dataset_id} (location={BQ_JOB_LOCATION})')
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = BQ_JOB_LOCATION
    try:
        client.create_dataset(dataset)
    except gcs_exceptions.Conflict:
        # Race: another process beat us to it. Treat as success.
        pass


# ---------------------------------------------------------------------------
# Export primitives
# ---------------------------------------------------------------------------


def export_feature_collection_to_bigquery(
    asset_id: str,
    table_id: str,
    force: bool = False,
) -> BigQueryExportResult:
    """Shared export primitive for any GEE ``FeatureCollection`` asset.

    Decision tree:
    - ``force=False`` AND ``bq_table_exists(table_id)`` → skip; return a
      ``BigQueryExportResult`` with ``from_cache=True``.
    - Otherwise → submit ``ee.batch.Export.table.toBigQuery`` with
      ``overwrite=True`` and block on ``wait_for_export_task``.

    Hard failures in the export task are captured on the returned result's
    ``error`` field rather than raised — ``run_publish`` decides whether
    to raise ``PublishError``.
    """
    versions = parse_compound_version_from_asset_id(table_id)
    if versions is None:
        raise ValueError(
            f'table_id {table_id!r} does not end in a compound '
            '{transform_sha}_{publish_sha} suffix — build it via '
            'build_transitions_bq_table_id / build_crops_bq_table_id.'
        )
    transform_version, publish_version = versions

    if not force and bq_table_exists(table_id):
        logger.info(f'BQ cache hit: {table_id}')
        return BigQueryExportResult(
            table_id=table_id,
            transform_version=transform_version,
            publish_version=publish_version,
            from_cache=True,
        )

    ensure_bq_dataset_exists()
    logger.info(f'BQ export: {asset_id} -> {table_id}')
    try:
        task = ee.batch.Export.table.toBigQuery(
            collection=ee.FeatureCollection(asset_id),
            description=_export_description(table_id),
            table=table_id,
            overwrite=True,
            append=False,
        )
        task.start()
        wait_for_export_task(task, table_id)
    except Exception as exc:
        logger.error(f'BQ export failed for {table_id}: {exc}')
        return BigQueryExportResult(
            table_id=table_id,
            transform_version=transform_version,
            publish_version=publish_version,
            from_cache=False,
            error=str(exc),
        )

    return BigQueryExportResult(
        table_id=table_id,
        transform_version=transform_version,
        publish_version=publish_version,
        from_cache=False,
    )


def _export_description(table_id: str) -> str:
    """GEE export-task description string. Truncated to stay under the 100-char cap."""
    short = table_id.split('.', 2)[-1]
    return f'publish_{short}'[:100]


# ---------------------------------------------------------------------------
# Per-table wrappers
# ---------------------------------------------------------------------------


def export_transitions_to_bigquery(
    transitions_asset_id: str,
    table_id: str,
    force: bool = False,
) -> BigQueryExportResult:
    """Export the regional ``transitions`` table to BigQuery.

    ``transitions_asset_id`` must point at a GEE FeatureCollection with
    ``TRANSITIONS_TABLE_COLUMNS`` — the (county_fips, epoch_transition,
    emissions_type) grain the transform stage emits.
    """
    return export_feature_collection_to_bigquery(
        transitions_asset_id, table_id, force=force
    )


def export_crops_to_bigquery(
    crops_asset_id: str,
    table_id: str,
    force: bool = False,
) -> BigQueryExportResult:
    """Export the regional ``crops`` table to BigQuery.

    ``crops_asset_id`` must point at a GEE FeatureCollection with
    ``CROPS_TABLE_COLUMNS`` — the (county_fips, crop_code) grain with
    emissions factors, yields, and driver breakdown columns attached.
    """
    return export_feature_collection_to_bigquery(crops_asset_id, table_id, force=force)

"""Publish stage entry point.

Orchestrates publish targets. Today the only target is BigQuery (two
tables: ``transitions`` and ``crops``); the dataclass surface is
target-keyed so future publish targets (tile-serving, GCS exports,
report generation) land as additional fields on ``PublishResult``
without reshaping ``pipeline.py``.

See specs/pipeline_tech_design.md § Publish for the full design.
"""

import dataclasses
import logging
from concurrent.futures import ThreadPoolExecutor

from jdluc.utils.version import compute_publish_version

# GCSExportResult is defined here (not in gcs.py) so publish.py stays the
# single home for all publish-stage result types and gcs.py can import it
# without a circular dependency.


@dataclasses.dataclass
class GCSExportResult:
    """Outcome of a single GCS raster export.

    - ``gcs_uri`` — ``gs://{bucket}/{prefix}`` (no ``.tif`` suffix).
    - ``transform_version`` / ``publish_version`` — provenance.
    - ``from_cache`` — True iff blobs matching the prefix already existed.
    - ``error`` — non-None if the export failed.
    """

    gcs_uri: str
    transform_version: str
    publish_version: str
    from_cache: bool
    error: str | None = None


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class BigQueryExportResult:
    """Outcome of a single BigQuery export.

    - ``table_id`` — fully qualified BQ table ID
      (``{project}.{dataset}.{prefix}_{region}_{t_sha}_{p_sha}``).
    - ``transform_version`` / ``publish_version`` — parsed back out of
      ``table_id`` for provenance. Kept as explicit fields so callers
      don't have to re-parse the name themselves.
    - ``from_cache`` — True iff the table already existed at the expected
      name and the export was skipped.
    - ``error`` — non-None if the export failed. ``run_publish`` raises
      ``PublishError`` iff any target's ``error`` is non-None.
    """

    table_id: str
    transform_version: str
    publish_version: str
    from_cache: bool
    error: str | None = None


@dataclasses.dataclass
class PublishResult:
    """Aggregated outcome of a ``run_publish`` invocation.

    One field per publish target.
    """

    transitions: BigQueryExportResult
    crops: BigQueryExportResult
    land_use: GCSExportResult
    emissions: GCSExportResult
    transitions_csv: GCSExportResult
    crops_csv: GCSExportResult


class PublishError(Exception):
    """Raised by ``run_publish`` when any target's ``error`` field is set.

    Carries the full ``PublishResult`` so callers can see which targets
    succeeded (cached or freshly exported) and which failed, with the
    per-target error messages. Analogous to ``ExtractError`` and
    ``TransformError``.
    """

    def __init__(self, result: PublishResult) -> None:
        self.result = result
        failed = [
            (name, target.error)
            for name, target in target_entries(result)
            if target.error is not None
        ]
        failed_summary = ', '.join(f'{name}({msg})' for name, msg in failed)
        super().__init__(
            f'Publish failed for {len(failed)} target(s): {failed_summary}'
        )


def target_entries(
    result: PublishResult,
) -> list[tuple[str, BigQueryExportResult | GCSExportResult]]:
    """Enumerate the per-target fields on a ``PublishResult``.

    Kept as a module-level helper (not a method on ``PublishResult``) so
    ``PublishError`` and any future iterator needs — for example the
    ``cli.py`` output block — have one place to update
    when targets are added. Order is stable: matches the dataclass
    field order.
    """
    return [
        ('transitions', result.transitions),
        ('crops', result.crops),
        ('land_use', result.land_use),
        ('emissions', result.emissions),
        ('transitions_csv', result.transitions_csv),
        ('crops_csv', result.crops_csv),
    ]


def run_publish(
    region_name: str,
    transitions_asset_id: str,
    crops_asset_id: str,
    land_use_asset_id: str,
    emissions_asset_id: str,
    transform_version: str,
    force: bool = False,
) -> PublishResult:
    """Publish-stage entry point. Exports all four targets in parallel.

    Args:
        region_name: Region label; embedded in table/object names.
        transitions_asset_id: GEE FeatureCollection asset ID for the
            regional ``transitions`` table.
        crops_asset_id: GEE FeatureCollection asset ID for the regional
            ``crops`` table.
        land_use_asset_id: GEE Image asset ID for the regional
            ``land_use`` raster.
        emissions_asset_id: GEE Image asset ID for the regional
            ``emissions`` raster.
        transform_version: SHA carried on the input GEE assets; embedded
            in published artifact names.
        force: If True, re-export all targets ignoring caches.

    Returns:
        ``PublishResult`` with one result per target.

    Raises:
        ``PublishError``: iff any target's ``error`` field is non-None.
            All four targets attempt regardless of each other's outcome,
            so a ``PublishResult`` is always produced before this raise.
    """
    publish_version = compute_publish_version()
    logger.info(
        f'Publish: region={region_name} transform={transform_version} '
        f'publish={publish_version} force={force}'
    )

    # Lazy imports keep ``publish/publish.py`` cheap to import (no
    # ``ee`` / ``google-cloud-bigquery`` / ``google-cloud-storage`` cost
    # when only the dataclasses are needed, e.g. by ``pipeline.py``).
    from jdluc.publish.bigquery import (
        build_crops_bq_table_id,
        build_transitions_bq_table_id,
        export_crops_to_bigquery,
        export_transitions_to_bigquery,
    )
    from jdluc.publish.bq_to_gcs import export_bq_table_to_gcs
    from jdluc.publish.gcs import (
        build_raster_gcs_prefix,
        export_emissions_to_gcs,
        export_land_use_to_gcs,
    )

    transitions_table_id = build_transitions_bq_table_id(
        region_name, transform_version, publish_version
    )
    crops_table_id = build_crops_bq_table_id(
        region_name, transform_version, publish_version
    )
    land_use_prefix = build_raster_gcs_prefix(
        'land_use', region_name, transform_version, publish_version
    )
    emissions_prefix = build_raster_gcs_prefix(
        'emissions', region_name, transform_version, publish_version
    )

    # All four targets attempt regardless of each other's outcome (each
    # captures its own errors on the returned result), so running them in
    # parallel reduces wall-clock for non-cached publish runs.
    with ThreadPoolExecutor(max_workers=6) as ex:
        transitions_future = ex.submit(
            export_transitions_to_bigquery,
            transitions_asset_id,
            transitions_table_id,
            force=force,
        )
        crops_future = ex.submit(
            export_crops_to_bigquery, crops_asset_id, crops_table_id, force=force
        )
        land_use_future = ex.submit(
            export_land_use_to_gcs,
            land_use_asset_id,
            land_use_prefix,
            region_name,
            transform_version,
            publish_version,
            force=force,
        )
        emissions_future = ex.submit(
            export_emissions_to_gcs,
            emissions_asset_id,
            emissions_prefix,
            region_name,
            transform_version,
            publish_version,
            force=force,
        )

        # Resolve BQ futures first so tables exist before phase 2 submits.
        transitions_result = transitions_future.result()
        crops_result = crops_future.result()

        transitions_csv_future = ex.submit(
            export_bq_table_to_gcs, transitions_table_id, force=force
        )
        crops_csv_future = ex.submit(
            export_bq_table_to_gcs, crops_table_id, force=force
        )

        land_use_result = land_use_future.result()
        emissions_result = emissions_future.result()
        transitions_csv_result = transitions_csv_future.result()
        crops_csv_result = crops_csv_future.result()

    result = PublishResult(
        transitions=transitions_result,
        crops=crops_result,
        land_use=land_use_result,
        emissions=emissions_result,
        transitions_csv=transitions_csv_result,
        crops_csv=crops_csv_result,
    )

    if any(target.error is not None for _, target in target_entries(result)):
        raise PublishError(result)

    logger.info(
        f'Publish: transitions {"cached" if transitions_result.from_cache else "exported"} '
        f'-> {transitions_result.table_id}'
    )
    logger.info(
        f'Publish: crops {"cached" if crops_result.from_cache else "exported"} '
        f'-> {crops_result.table_id}'
    )
    logger.info(
        f'Publish: land_use {"cached" if land_use_result.from_cache else "exported"} '
        f'-> {land_use_result.gcs_uri}'
    )
    logger.info(
        f'Publish: emissions {"cached" if emissions_result.from_cache else "exported"} '
        f'-> {emissions_result.gcs_uri}'
    )
    logger.info(
        f'Publish: transitions_csv {"cached" if transitions_csv_result.from_cache else "exported"} '
        f'-> {transitions_csv_result.gcs_uri}'
    )
    logger.info(
        f'Publish: crops_csv {"cached" if crops_csv_result.from_cache else "exported"} '
        f'-> {crops_csv_result.gcs_uri}'
    )
    return result

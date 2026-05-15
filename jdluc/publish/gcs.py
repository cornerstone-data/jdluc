"""GCS publish target.

Exports the regional ``land_use`` and ``emissions`` GEE ``ee.Image`` assets
produced by the transform stage into GCS as cloud-optimised GeoTIFFs.
Objects land at ``gs://{GCS_BUCKET_NAME}/{GCS_RASTER_PREFIX}/{name}_{region}_{t_sha}_{p_sha}``.
GEE may write a single ``{prefix}.tif`` (single tile) or
``{prefix}-00000-of-NNNNN.tif`` (multi-tile) depending on the image footprint.

Architecture mirrors ``bigquery.py``:
- One shared primitive (``export_image_to_gcs``) carries the real export +
  cache-probe logic.
- Two thin per-asset wrappers (``export_land_use_to_gcs``,
  ``export_emissions_to_gcs``) exist for call-site clarity.

See specs/pipeline_tech_design.md § Publish.
"""

import logging

import ee

from jdluc.publish.publish import GCSExportResult
from jdluc.utils.constants import GCS_BUCKET_NAME, GCS_RASTER_PREFIX
from jdluc.utils.gee import wait_for_export_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GCS path builder
# ---------------------------------------------------------------------------


def build_raster_gcs_prefix(
    name: str,
    region_name: str,
    transform_version: str,
    publish_version: str,
) -> str:
    """GCS object prefix (without bucket) for a named raster export."""
    return f'{GCS_RASTER_PREFIX}/{name}_{region_name}_{transform_version}_{publish_version}'


# ---------------------------------------------------------------------------
# Cache probe
# ---------------------------------------------------------------------------


def gcs_raster_exists(prefix: str) -> bool:
    """Return True iff any GCS object matching ``prefix`` already exists.

    Uses ``list_blobs(prefix=prefix, max_results=1)`` — if any blob matches
    the prefix the export is already present. Lazy import, same posture as
    the BQ probe in ``bigquery.py``.
    """
    from google.cloud import storage

    client = storage.Client()
    blobs = client.list_blobs(GCS_BUCKET_NAME, prefix=prefix, max_results=1)
    return any(True for _ in blobs)


# ---------------------------------------------------------------------------
# Export primitive
# ---------------------------------------------------------------------------


def export_image_to_gcs(
    asset_id: str,
    gcs_prefix: str,
    name: str,
    region_name: str,
    transform_version: str,
    publish_version: str,
    force: bool = False,
) -> GCSExportResult:
    """Shared export primitive for any GEE ``ee.Image`` asset.

    Decision tree:
    - ``force=False`` AND ``gcs_raster_exists(gcs_prefix)`` → skip; return
      a ``GCSExportResult`` with ``from_cache=True``.
    - Otherwise → submit ``ee.batch.Export.image.toCloudStorage`` and block
      on ``wait_for_export_task``.

    Hard failures are captured on the returned result's ``error`` field
    rather than raised — ``run_publish`` decides whether to raise
    ``PublishError``.
    """
    gcs_uri = f'gs://{GCS_BUCKET_NAME}/{gcs_prefix}'

    if not force and gcs_raster_exists(gcs_prefix):
        logger.info(f'GCS cache hit: {gcs_uri}')
        return GCSExportResult(
            gcs_uri=gcs_uri,
            transform_version=transform_version,
            publish_version=publish_version,
            from_cache=True,
        )

    logger.info(f'GCS export: {asset_id} -> {gcs_uri}')
    try:
        img = ee.Image(asset_id)
        projection = img.projection().getInfo()
        description = f'publish_{name}_{region_name}'[:100]

        task = ee.batch.Export.image.toCloudStorage(
            image=img,
            description=description,
            bucket=GCS_BUCKET_NAME,
            fileNamePrefix=gcs_prefix,
            crs=projection['crs'],
            crsTransform=projection['transform'],
            region=img.geometry(),
            fileFormat='GeoTIFF',
            maxPixels=int(1e13),
            formatOptions={'cloudOptimized': True},
        )
        task.start()
        wait_for_export_task(task, gcs_uri)
    except Exception as exc:
        logger.error(f'GCS export failed for {gcs_uri}: {exc}')
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


# ---------------------------------------------------------------------------
# Per-asset wrappers
# ---------------------------------------------------------------------------


def export_land_use_to_gcs(
    asset_id: str,
    gcs_prefix: str,
    region_name: str,
    transform_version: str,
    publish_version: str,
    *,
    force: bool = False,
) -> GCSExportResult:
    """Export the regional ``land_use`` raster to GCS as a cloud-optimised GeoTIFF."""
    return export_image_to_gcs(
        asset_id,
        gcs_prefix,
        name='land_use',
        region_name=region_name,
        transform_version=transform_version,
        publish_version=publish_version,
        force=force,
    )


def export_emissions_to_gcs(
    asset_id: str,
    gcs_prefix: str,
    region_name: str,
    transform_version: str,
    publish_version: str,
    *,
    force: bool = False,
) -> GCSExportResult:
    """Export the regional ``emissions`` raster to GCS as a cloud-optimised GeoTIFF."""
    return export_image_to_gcs(
        asset_id,
        gcs_prefix,
        name='emissions',
        region_name=region_name,
        transform_version=transform_version,
        publish_version=publish_version,
        force=force,
    )

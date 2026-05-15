"""IPCC 2006 climate zone raster extract.

Fetches the single 0.5° GeoTIFF from Zenodo record 7303808, stages it
to GCS, and ingests it at a scratch native-projection asset. A server-
side ``Export.image.toAsset`` then reprojects onto the GLAD 0.00025°
grid via nearest-neighbor (the source is categorical zone codes) and
writes the final versioned asset. The scratch asset and GCS staging
object are removed on success.

Precomputing the reprojection here (rather than at read time in
``transform/emissions.py::_load_ipcc_climate``) keeps the GLAD-grid
transform out of every pipeline run — the 0.5° → 0.00025° lift is
non-trivial and the output is static.
"""

import logging
import os
import shutil
import tempfile
import uuid

import ee
import requests

from jdluc.extract.mirror import fetch_with_mirror
from jdluc.utils.constants import (
    GCS_BUCKET_NAME,
    GEE_ASSET_ROOT,
    GEE_IPCC_CLIMATE_ZONES,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    IPCC_CLIMATE_ZONES_ZENODO_URL,
)
from jdluc.utils.gee import (
    asset_exists,
    delete_asset_if_present,
    delete_gcs_blob,
    start_ingestion_and_wait,
    upload_to_gcs,
    wait_for_export_task,
)

logger = logging.getLogger(__name__)


GCS_STAGING_BLOB: str = 'luc_high_res/staging/ipcc_climate_zones_v2006.tif'

# Stable local + mirror filename. The upstream Zenodo filename could
# drift across record revisions; we use our own name so the mirror key
# is stable and the Zenodo probe is skipped on cache hit.
SOURCE_FILENAME: str = 'ipcc_climate_zones_v2006.tif'

# Scratch asset holds the ingested native-resolution TIF; the final
# asset is produced by a server-side GEE export that reprojects onto
# the GLAD grid.
SCRATCH_ASSET_ID: str = f'{GEE_ASSET_ROOT}/ipcc_climate_zones_v2006_native'

BAND_NAME: str = 'ipcc_climate_zone'


def _discover_zenodo_tif_url() -> tuple[str, str]:
    """Resolve the IPCC climate-zone file URL on the Zenodo record.

    Returns ``(filename, download_url)``. Uses the record-level API
    endpoint stored in ``IPCC_CLIMATE_ZONES_ZENODO_URL`` — the record
    contains exactly one .tif file; fail fast if that changes.
    """
    logger.info(f'Fetching Zenodo record: {IPCC_CLIMATE_ZONES_ZENODO_URL}')
    response = requests.get(IPCC_CLIMATE_ZONES_ZENODO_URL, timeout=60)
    response.raise_for_status()
    files = response.json().get('files', [])
    tifs = [f for f in files if str(f.get('key', '')).lower().endswith('.tif')]
    if len(tifs) != 1:
        keys = [f.get('key') for f in files]
        raise RuntimeError(
            f'Zenodo record expected to contain exactly 1 .tif; found {keys}'
        )
    entry = tifs[0]
    return entry['key'], entry['links']['self']


def _start_reproject_export(src_asset_id: str, dst_asset_id: str) -> ee.batch.Task:
    """Reproject src onto the GLAD grid and export to dst (server-side).

    Default GEE resampling for integer-typed rasters is nearest-neighbor,
    which is what this categorical raster needs. We still call
    ``.reproject(...)`` explicitly so the intent is visible at the call
    site.
    """
    src = ee.Image(src_asset_id).reproject(
        crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM
    )
    global_region = ee.Geometry.Rectangle(
        coords=[-180, -90, 180, 90], proj=GLAD_CRS, geodesic=False
    )
    task = ee.batch.Export.image.toAsset(
        image=src,
        description='ipcc_climate_zones_v2006_reproject',
        assetId=dst_asset_id,
        region=global_region,
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e13),
    )
    task.start()
    logger.info(f'Reproject export task started: {dst_asset_id}')
    return task


def extract_ipcc_climate_zones(gcp_project: str, force: bool = False) -> str:
    """Fetch Zenodo TIF, ingest to scratch, reproject to the final asset.

    Args:
        gcp_project: GCP project for GCS + EE.
        force: If True, delete existing scratch and final assets before run.

    Returns:
        Final GEE asset ID.
    """
    if force:
        delete_asset_if_present(SCRATCH_ASSET_ID)
        delete_asset_if_present(GEE_IPCC_CLIMATE_ZONES)
    elif asset_exists(SCRATCH_ASSET_ID):
        # A prior partial run left scratch behind but the final asset is
        # missing (orchestrator only invoked us because of that). Clear
        # scratch so the ingest can run cleanly.
        logger.info('IPCC: clearing leftover scratch asset from prior run')
        delete_asset_if_present(SCRATCH_ASSET_ID)

    run_id = uuid.uuid4().hex[:8]
    work_dir = tempfile.mkdtemp(prefix=f'ipcc_climate_zones_{run_id}_')

    try:
        local_tif = os.path.join(work_dir, SOURCE_FILENAME)
        # Mirror-first fetch; Zenodo record probe is deferred to mirror miss.
        fetch_with_mirror(
            local_tif,
            dataset='ipcc_climate_zones',
            filename=SOURCE_FILENAME,
            gcp_project=gcp_project,
            source=lambda: _discover_zenodo_tif_url()[1],
            timeout_s=300.0,
        )

        gcs_uri = upload_to_gcs(
            gcp_project, GCS_BUCKET_NAME, GCS_STAGING_BLOB, local_tif
        )
        try:
            start_ingestion_and_wait(
                gcs_uri,
                SCRATCH_ASSET_ID,
                band_name=BAND_NAME,
                allow_overwrite=force,
            )

            export_task = _start_reproject_export(
                SCRATCH_ASSET_ID, GEE_IPCC_CLIMATE_ZONES
            )
            wait_for_export_task(export_task, GEE_IPCC_CLIMATE_ZONES)
        finally:
            delete_gcs_blob(gcp_project, GCS_BUCKET_NAME, GCS_STAGING_BLOB)

        # Final asset is written; scratch no longer needed.
        delete_asset_if_present(SCRATCH_ASSET_ID)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    logger.info(f'IPCC: extract complete → {GEE_IPCC_CLIMATE_ZONES}')
    return GEE_IPCC_CLIMATE_ZONES

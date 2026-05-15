"""Huang et al. (2021) below-ground biomass extract.

Fetches ``data_code_to_submit.zip`` from Figshare (DOI
10.6084/m9.figshare.12199637.v1), extracts the ``pergridarea_bgb.nc``
NetCDF, writes a CONUS-clipped GeoTIFF, stages it to GCS, and ingests it
as a single ``Image`` asset. CRS reprojection and NoData gap-fill are
NOT done here — ``transform/emissions.py::_load_bgb`` owns that grid
work so the extract output stays as close to the source as possible.
"""

import logging
import os
import shutil
import tempfile
import uuid
import zipfile

import rioxarray  # noqa: F401 — registers the .rio accessor on DataArray.
import xarray as xr

from jdluc.extract.mirror import fetch_with_mirror
from jdluc.utils.constants import (
    GCS_BUCKET_NAME,
    GEE_HUANG_BGB,
    HUANG_BGB_FIGSHARE_URL,
)
from jdluc.utils.gee import (
    delete_asset_if_present,
    delete_gcs_blob,
    start_ingestion_and_wait,
    upload_to_gcs,
)

logger = logging.getLogger(__name__)

# CONUS bounding box (with small buffer).
CONUS_WEST: float = -130.0
CONUS_EAST: float = -65.0
CONUS_SOUTH: float = 24.0
CONUS_NORTH: float = 50.0


GCS_STAGING_PREFIX: str = 'luc_high_res/staging/huang_bgb'

NETCDF_MEMBER_NAME: str = 'pergridarea_bgb.nc'
# AROOT is Huang's below-ground biomass density variable (Mg/ha).
NETCDF_VARIABLE: str = 'AROOT'

BAND_NAME: str = 'bgb_mg_ha'


def _write_conus_geotiff(netcdf_path: str, geotiff_path: str) -> None:
    """Read NetCDF, select AROOT, clip to CONUS bounds, write GeoTIFF.

    No reproject, no NoData fill — downstream transform handles both.
    """

    logger.info(f'Huang BGB: loading NetCDF {netcdf_path}')
    with xr.open_dataset(netcdf_path) as ds:
        da = ds[NETCDF_VARIABLE].sel(
            LAT=slice(CONUS_SOUTH, CONUS_NORTH),
            LON=slice(CONUS_WEST, CONUS_EAST),
        )
        da = da.rio.set_spatial_dims(x_dim='LON', y_dim='LAT')
        da = da.rio.write_crs('EPSG:4326')
        logger.info(
            f'Huang BGB: clipped shape={tuple(da.shape)}, '
            f'lat=[{float(da.LAT.min()):.4f}, {float(da.LAT.max()):.4f}], '
            f'lon=[{float(da.LON.min()):.4f}, {float(da.LON.max()):.4f}]'
        )
        da.rio.to_raster(geotiff_path, compress='LZW', dtype='float32')


def _extract_netcdf(zip_path: str, work_dir: str) -> str:
    """Extract ``NETCDF_MEMBER_NAME`` from ``zip_path`` into ``work_dir``."""
    logger.info(f'Huang BGB: extracting {NETCDF_MEMBER_NAME} from {zip_path}')
    with zipfile.ZipFile(zip_path) as zf:
        candidates = [n for n in zf.namelist() if n.endswith(NETCDF_MEMBER_NAME)]
        if not candidates:
            raise RuntimeError(
                f'Huang BGB: {NETCDF_MEMBER_NAME!r} not found in archive at '
                f'{zip_path}; archive members: {zf.namelist()[:10]}...'
            )
        zf.extract(candidates[0], work_dir)
    return os.path.join(work_dir, candidates[0])


def extract_huang_bgb(gcp_project: str, force: bool = False) -> str:
    """Fetch Huang BGB, clip to CONUS, stage to GCS, ingest into GEE.

    Args:
        gcp_project: GCP project for GCS + EE.
        force: If True, delete + re-ingest the existing asset.

    Returns:
        GEE Image asset ID.
    """
    run_id = uuid.uuid4().hex[:8]

    if force:
        delete_asset_if_present(GEE_HUANG_BGB)

    work_dir = tempfile.mkdtemp(prefix=f'huang_bgb_{run_id}_')
    logger.info(f'Huang BGB: work_dir={work_dir}')

    zip_path = os.path.join(work_dir, 'data_code_to_submit.zip')
    geotiff_path = os.path.join(work_dir, 'huang_bgb_conus.tif')
    blob_name = f'{GCS_STAGING_PREFIX}/{run_id}/huang_bgb_conus.tif'

    try:
        fetch_with_mirror(
            zip_path,
            dataset='huang_bgb',
            filename='data_code_to_submit.zip',
            gcp_project=gcp_project,
            source=HUANG_BGB_FIGSHARE_URL,
            timeout_s=900.0,
        )
        netcdf_path = _extract_netcdf(zip_path, work_dir)
        _write_conus_geotiff(netcdf_path, geotiff_path)

        gcs_uri = upload_to_gcs(gcp_project, GCS_BUCKET_NAME, blob_name, geotiff_path)
        try:
            start_ingestion_and_wait(
                gcs_uri,
                GEE_HUANG_BGB,
                band_name=BAND_NAME,
                allow_overwrite=force,
            )
        finally:
            delete_gcs_blob(gcp_project, GCS_BUCKET_NAME, blob_name)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    logger.info(f'Huang BGB: extract complete → {GEE_HUANG_BGB}')
    return GEE_HUANG_BGB

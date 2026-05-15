"""Harris et al. (2021) above-ground live woody biomass extract.

Discovers 19 CONUS tiles via Global Forest Watch's ArcGIS FeatureServer,
downloads each GeoTIFF, stages it to GCS, and ingests each as an Image
member of an ``ImageCollection`` asset. The orchestrator (Phases A/B/C/D)
lives in ``extract/_tile_pipeline.py``; this module supplies the
Harris-specific URL discovery + dataset constants.

Source: Harris et al. (2021) *Global maps of twenty-first century forest
carbon fluxes*. Nature Climate Change, 11, 234–240.
DOI 10.1038/s41558-020-00976-6.
Portal:
https://data.globalforestwatch.org/datasets/gfw::aboveground-live-woody-biomass-density
"""

from typing import Any

import requests

from jdluc.extract._tile_pipeline import run_tile_collection_extract
from jdluc.utils.constants import (
    GCS_BUCKET_NAME,
    GEE_HARRIS_AGB,
    HARRIS_AGB_ARCGIS_FEATURESERVER,
)

# 10°×10° tiles covering CONUS landmass. Tile IDs are the upper-left
# corner; e.g. 40N_080W covers 30°N–40°N, 80°W–70°W (includes Delaware).
# The full 3×7 grid would be 21 tiles, but Harris AGB's FeatureServer
# omits 30N_070W (Caribbean) and 30N_130W (Pacific) — both ocean with
# no biomass — so we list only the 19 tiles the FeatureServer actually
# publishes. (GFW's peatlands manifest has the same gap; see
# extract/gfw_peatlands.py.)
CONUS_TILE_IDS: list[str] = [
    f'{row}_{col}'
    for row in ('30N', '40N', '50N')
    for col in ('070W', '080W', '090W', '100W', '110W', '120W', '130W')
    if (row, col) not in {('30N', '070W'), ('30N', '130W')}
]

# GCS staging. Tiles live at ``{GCS_PATH}/{tile_id}.tif``.
GCS_STAGING_PREFIX: str = 'luc_high_res/staging/harris_agb'

BAND_NAME: str = 'agb_mg_ha'


def _fetch_tile_download_url(tile_id: str, *, timeout_s: float = 30.0) -> str:
    """Resolve ``tile_id`` to its ArcGIS-signed download URL."""
    params = {
        'where': f"tile_id='{tile_id}'",
        'outFields': 'tile_id,Mg_ha_1_download',
        'f': 'json',
    }
    response = requests.get(
        HARRIS_AGB_ARCGIS_FEATURESERVER, params=params, timeout=timeout_s
    )
    response.raise_for_status()
    features = response.json().get('features', [])
    if not features:
        raise RuntimeError(f'Harris AGB: tile {tile_id!r} not found on FeatureServer')
    url = features[0]['attributes'].get('Mg_ha_1_download')
    if not url:
        raise RuntimeError(
            f'Harris AGB: FeatureServer has no Mg_ha_1_download for {tile_id!r}'
        )
    return str(url)


def extract_harris_agb(
    gcp_project: str,
    force: bool = False,
    *,
    tile_ids: list[str] | None = None,
    feature_server_params: dict[str, Any] | None = None,
) -> str:
    """Discover, download, stage, and ingest Harris AGB tiles into GEE.

    Args:
        gcp_project: GCP project for GCS + EE.
        force: If True, delete and re-ingest any existing tiles.
        tile_ids: Override tile list (tests). Defaults to CONUS_TILE_IDS.

    Returns:
        GEE ImageCollection asset ID. Returned even when individual tiles
        failed to ingest — callers must inspect the collection and re-run
        with ``force=True`` to retry. (The orchestrator's
        ``asset_is_populated`` cache check sees a partial collection as
        populated, matching the behavior for any non-empty IC.)
    """
    return run_tile_collection_extract(
        dataset_label='Harris AGB',
        mirror_dataset='harris_agb',
        gcs_bucket=GCS_BUCKET_NAME,
        gcs_staging_prefix=GCS_STAGING_PREFIX,
        asset_root=GEE_HARRIS_AGB,
        band_name=BAND_NAME,
        gcp_project=gcp_project,
        force=force,
        default_tile_ids=CONUS_TILE_IDS,
        tile_ids=tile_ids,
        resolve_tile_url=_fetch_tile_download_url,
    )

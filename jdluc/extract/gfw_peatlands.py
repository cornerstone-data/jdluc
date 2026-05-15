"""GFW Global Peatlands raster extract (CC BY 4.0 composite).

Downloads 19 CONUS GeoTIFF tiles from Global Forest Watch's Global
Peatlands dataset, stages each to GCS, and ingests them as Image
members of an ``ImageCollection`` asset. The transform consumer
(``transform/land_use.py::_load_is_peatland``) lazy-mosaics the
collection on read.

The orchestrator (Phases A/B/C/D) lives in
``extract/_tile_pipeline.py``; this module supplies the GFW-specific
URL template + dataset constants.

GFW's product is a composite raster:

- Above 40°N: Xu et al. (2018) PEATMAP
- Below 40°N (default): Gumbricht et al. (2017) tropical wetlands/peatlands
- Indonesia/Malaysia override: Miettinen et al. (2016)
- Lowland Peruvian Amazon override: Hastie et al. (2022)
- Congo basin override: Crezee et al. (2022)

All layers are rasterized to the 30 m Hansen Global Forest Change
grid and merged into a single raster, distributed as 10°×10° tiles
on the same grid identifiers as Harris AGB. CONUS coverage is split
across the 40°N cutoff: tiles fully above 40°N (the northern tier
through the Corn Belt and Great Plains) read from Xu PEATMAP;
tiles below 40°N (the mid-Atlantic, Southeast, southern California,
Arizona, New Mexico, and most of Texas) fall through to Gumbricht.
This is a known limitation — Gumbricht is tropical-tuned and a poor
fit for temperate-US peatlands; see `specs/methodology.md` Appendix 2
(Peatland dataset choice) and `specs/analyses/orbae_de_corn_comparison.ipynb`
for the empirical impact.

The tile-list (``CONUS_TILE_IDS``) is GFW-Peatlands-specific: GFW omits
tiles with no peatland pixels, so two of Harris AGB's 21 CONUS
tiles (30N_070W in the Caribbean, 30N_130W in the Pacific) don't
exist in the manifest. The 19-tile constant below is hard-coded
against the v20230315 manifest the user downloaded; if GFW ever
republishes with denser coverage, regenerate from the new CSV.

Source: https://data.globalforestwatch.org/datasets/gfw::global-peatlands
License: CC BY 4.0.

API key (``GFW_DATA_API_KEY`` in ``utils/constants.py``): the key
ships embedded in any user's CSV download from the GFW Open Data
Portal — it's a public rate-limited token, not a secret. If the key
401s in the future, re-download the CSV from the portal and bump
the constant in ``utils/constants.py``.
"""

from jdluc.extract._tile_pipeline import run_tile_collection_extract

# GCS staging. Tiles live at ``{GCS_PATH}/{tile_id}.tif``.
from jdluc.utils.constants import (
    GCS_BUCKET_NAME,
    GEE_GFW_PEATLANDS,
    GFW_DATA_API_KEY,
    GFW_PEATLANDS_URL_TEMPLATE,
)

GCS_STAGING_PREFIX: str = 'luc_high_res/staging/gfw_peatlands'

BAND_NAME: str = 'is_peatland'

# CONUS tiles GFW Global Peatlands actually publishes. The full Harris
# AGB grid has 21 CONUS tiles, but GFW omits tiles with no peatland
# pixels — 30N_070W (Caribbean, no land) and 30N_130W (Pacific, off
# coast) are absent from the v20230315 manifest. Hard-coded against
# the user's downloaded CSV manifest rather than re-derived from
# Harris AGB's grid; we are version-pinned to v20230315 anyway. If
# GFW ever republishes with denser coverage, regenerate from the new
# CSV manifest.
CONUS_TILE_IDS: tuple[str, ...] = (
    '30N_080W',
    '30N_090W',
    '30N_100W',
    '30N_110W',
    '30N_120W',
    '40N_070W',
    '40N_080W',
    '40N_090W',
    '40N_100W',
    '40N_110W',
    '40N_120W',
    '40N_130W',
    '50N_070W',
    '50N_080W',
    '50N_090W',
    '50N_100W',
    '50N_110W',
    '50N_120W',
    '50N_130W',
)

__all__ = ('CONUS_TILE_IDS', 'extract_gfw_peatlands')


def _resolve_tile_url(tile_id: str) -> str:
    """Format the GFW per-tile GeoTIFF download URL.

    Unlike Harris AGB (which discovers per-tile signed URLs via an
    ArcGIS FeatureServer query), GFW exposes a stable URL template
    keyed on ``tile_id`` and a public API key, so the URL is
    template-constructed without any HTTP probe.
    """
    return GFW_PEATLANDS_URL_TEMPLATE.format(tile_id=tile_id, api_key=GFW_DATA_API_KEY)


def extract_gfw_peatlands(
    gcp_project: str,
    force: bool = False,
    *,
    tile_ids: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Download GFW Global Peatlands tiles, stage to GCS, ingest into GEE.

    Args:
        gcp_project: GCP project for GCS + EE.
        force: If True, delete and re-ingest any existing tiles.
        tile_ids: Override tile list (tests). Defaults to CONUS_TILE_IDS.

    Returns:
        GEE ImageCollection asset ID. Returned even when individual tiles
        failed to ingest — callers must inspect the collection and re-run
        with ``force=True`` to retry.
    """
    return run_tile_collection_extract(
        dataset_label='GFW Peatlands',
        mirror_dataset='gfw_peatlands',
        gcs_bucket=GCS_BUCKET_NAME,
        gcs_staging_prefix=GCS_STAGING_PREFIX,
        asset_root=GEE_GFW_PEATLANDS,
        band_name=BAND_NAME,
        gcp_project=gcp_project,
        force=force,
        default_tile_ids=CONUS_TILE_IDS,
        tile_ids=tile_ids,
        resolve_tile_url=_resolve_tile_url,
    )

"""USDA NASS QuickStats yield extract.

Downloads the gzipped ``qs.crops_{RELEASE_DATE}.txt.gz`` TSV from NASS,
pre-filters to CORN / SOYBEANS / WHEAT YIELD rows, writes those rows to
a CSV, stages the CSV to GCS, and ingests it as a FeatureCollection
asset via ``ee.data.startTableIngestion``. The CSV has no geometry
columns, so GEE creates features with null geometry — fine because
``transform/summary_tables.py`` reads only ``feature['properties']``
and never touches geometry.

The semantic filters (``AGG_LEVEL_DESC == 'STATE'``,
``UNIT_DESC == 'BU / ACRE'``, year ∈ ``NASS_YIELD_YEARS``), the 4-year
arithmetic mean, and the bu/acre → kg/ha conversion all live at the
transform layer (``transform/summary_tables.py``). Keeping those out of
extract means a NASS_YIELD_YEARS bump doesn't force a re-extract.

The earlier draft of this module shipped rows inline as
``ee.FeatureCollection([ee.Feature, ...])`` + ``Export.table.toAsset``.
That hit GEE's 10 MB per-request payload limit on full-archive runs
(≈1.5 M rows). The GCS-staged CSV path mirrors how the raster extractors
(Harris AGB, GFW Peatlands) stay cloud-native and is unbounded in size.
"""

import gzip
import logging
import os
import shutil
import tempfile
import uuid

import pandas as pd

from jdluc.extract.mirror import fetch_with_mirror
from jdluc.utils.constants import (
    GCS_BUCKET_NAME,
    GEE_NASS_YIELDS,
    NASS_QUICKSTATS_RELEASE_DATE,
    NASS_QUICKSTATS_URL_TEMPLATE,
    NASS_TO_CROP_GROUP,
)
from jdluc.utils.gee import (
    delete_asset_if_present,
    delete_gcs_blob,
    start_table_ingestion_and_wait,
    upload_to_gcs,
)

logger = logging.getLogger(__name__)

# Column set kept in the upload payload. Anything else from QuickStats
# is discarded client-side to keep the FeatureCollection small.
ROW_COLUMNS: list[str] = [
    'state_fips',
    'state_name',
    'commodity_desc',
    'year',
    'value_bu_per_acre',
    'agg_level_desc',
    'unit_desc',
]

# QuickStats value-column entries we cannot numerically coerce; rows with
# these get dropped (consistent with the legacy script's behavior).
_SUPPRESSED_VALUE_TOKENS: frozenset[str] = frozenset({'(D)', '(Z)', '(S)', '(NA)'})

# Commodity families we care about — methodology's three row crops.
_VALID_COMMODITIES: frozenset[str] = frozenset(NASS_TO_CROP_GROUP)

# Chunk size for streaming the ~1.5 GB compressed TSV through pandas.
_TSV_CHUNK_ROWS: int = 100_000

GCS_STAGING_PREFIX: str = 'luc_high_res/staging/nass_yields'


def _download_and_parse(gzip_path: str, gcp_project: str) -> pd.DataFrame:
    """Download the QuickStats TSV and return a pre-filtered DataFrame."""
    url = NASS_QUICKSTATS_URL_TEMPLATE.format(date=NASS_QUICKSTATS_RELEASE_DATE)
    filename = f'qs.crops_{NASS_QUICKSTATS_RELEASE_DATE}.txt.gz'
    fetch_with_mirror(
        gzip_path,
        dataset='nass_yields',
        filename=filename,
        gcp_project=gcp_project,
        source=url,
        timeout_s=1800.0,
    )

    frames: list[pd.DataFrame] = []
    logger.info(f'NASS: parsing TSV at {gzip_path}')
    with gzip.open(gzip_path, 'rt', encoding='utf-8', errors='replace') as fh:
        reader = pd.read_csv(
            fh,
            sep='\t',
            dtype=str,
            chunksize=_TSV_CHUNK_ROWS,
            on_bad_lines='skip',
        )
        for chunk in reader:
            mask = (chunk['STATISTICCAT_DESC'] == 'YIELD') & (
                chunk['COMMODITY_DESC'].isin(_VALID_COMMODITIES)
            )
            filtered = chunk.loc[mask]
            if not filtered.empty:
                frames.append(filtered)

    if not frames:
        raise RuntimeError('NASS: no YIELD rows found after pre-filter')

    df = pd.concat(frames, ignore_index=True)
    df = df[~df['VALUE'].str.strip().isin(_SUPPRESSED_VALUE_TOKENS)]
    df['value_bu_per_acre'] = df['VALUE'].str.replace(',', '').astype(float)
    df['state_fips'] = df['STATE_FIPS_CODE'].str.zfill(2)
    df['year'] = df['YEAR'].astype(int)
    df = df.rename(
        columns={
            'STATE_NAME': 'state_name',
            'COMMODITY_DESC': 'commodity_desc',
            'AGG_LEVEL_DESC': 'agg_level_desc',
            'UNIT_DESC': 'unit_desc',
        }
    )
    return df[ROW_COLUMNS].reset_index(drop=True)


def extract_nass_yields(gcp_project: str, force: bool = False) -> str:
    """Download QuickStats, filter, then ingest as a CSV via GCS.

    Args:
        gcp_project: GCP project for both the mirror read/write path
            in ``fetch_with_mirror`` and the GCS staging upload that
            backs ``ee.data.startTableIngestion``.
        force: If True, delete the existing asset before re-ingesting.

    Returns:
        GEE FeatureCollection asset ID.
    """
    if force:
        delete_asset_if_present(GEE_NASS_YIELDS)

    run_id = uuid.uuid4().hex[:8]
    work_dir = tempfile.mkdtemp(prefix=f'nass_yields_{run_id}_')
    gzip_path = os.path.join(
        work_dir, f'qs.crops_{NASS_QUICKSTATS_RELEASE_DATE}.txt.gz'
    )
    csv_path = os.path.join(work_dir, 'nass_yields_raw.csv')
    blob_name = f'{GCS_STAGING_PREFIX}/{run_id}/nass_yields_raw.csv'
    try:
        df = _download_and_parse(gzip_path, gcp_project)
        logger.info(f'NASS: writing {len(df)} raw rows to {csv_path} for table ingest')
        # No geometry columns — GEE will produce null-geometry features.
        # That's fine: transform/summary_tables.py reads only properties.
        df.to_csv(csv_path, index=False)

        gcs_uri = upload_to_gcs(gcp_project, GCS_BUCKET_NAME, blob_name, csv_path)
        try:
            start_table_ingestion_and_wait(
                gcs_uri,
                GEE_NASS_YIELDS,
                allow_overwrite=force,
            )
        finally:
            delete_gcs_blob(gcp_project, GCS_BUCKET_NAME, blob_name)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    logger.info(f'NASS: extract complete → {GEE_NASS_YIELDS}')
    return GEE_NASS_YIELDS

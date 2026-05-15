"""Unit tests for extract/nass_yields.py.

Mocks HTTP + pandas chunk reader + EE/GCS so the tests run offline.
Covers the upload-size filter, the value-coercion rules, the rename
map that the transform consumer expects, and the GCS-staged
ingestion path that replaced the inline FeatureCollection upload
(which hit GEE's 10 MB request payload limit on full-archive runs).
"""

import gzip
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from jdluc.extract import nass_yields
from jdluc.utils.constants import GCS_BUCKET_NAME, GEE_NASS_YIELDS


def _fake_tsv_frame() -> pd.DataFrame:
    """Minimum-column chunk matching QuickStats' schema."""
    return pd.DataFrame(
        [
            # YIELD-CORN-STATE — kept.
            {
                'STATISTICCAT_DESC': 'YIELD',
                'COMMODITY_DESC': 'CORN',
                'AGG_LEVEL_DESC': 'STATE',
                'UNIT_DESC': 'BU / ACRE',
                'YEAR': '2020',
                'VALUE': '180.0',
                'STATE_FIPS_CODE': '10',
                'STATE_NAME': 'DELAWARE',
            },
            # Unknown commodity — dropped by upload filter.
            {
                'STATISTICCAT_DESC': 'YIELD',
                'COMMODITY_DESC': 'BARLEY',
                'AGG_LEVEL_DESC': 'STATE',
                'UNIT_DESC': 'BU / ACRE',
                'YEAR': '2020',
                'VALUE': '80.0',
                'STATE_FIPS_CODE': '10',
                'STATE_NAME': 'DELAWARE',
            },
            # Non-YIELD STATISTICCAT — dropped.
            {
                'STATISTICCAT_DESC': 'AREA PLANTED',
                'COMMODITY_DESC': 'CORN',
                'AGG_LEVEL_DESC': 'STATE',
                'UNIT_DESC': 'ACRES',
                'YEAR': '2020',
                'VALUE': '1000.0',
                'STATE_FIPS_CODE': '10',
                'STATE_NAME': 'DELAWARE',
            },
            # Suppressed value token — dropped.
            {
                'STATISTICCAT_DESC': 'YIELD',
                'COMMODITY_DESC': 'CORN',
                'AGG_LEVEL_DESC': 'STATE',
                'UNIT_DESC': 'BU / ACRE',
                'YEAR': '2020',
                'VALUE': '(D)',
                'STATE_FIPS_CODE': '10',
                'STATE_NAME': 'DELAWARE',
            },
            # County-level row — kept at extract (transform filters it out).
            {
                'STATISTICCAT_DESC': 'YIELD',
                'COMMODITY_DESC': 'SOYBEANS',
                'AGG_LEVEL_DESC': 'COUNTY',
                'UNIT_DESC': 'BU / ACRE',
                'YEAR': '2020',
                'VALUE': '48.5',
                'STATE_FIPS_CODE': '19',
                'STATE_NAME': 'IOWA',
            },
        ]
    )


def test_download_and_parse_keeps_only_valid_commodity_yield_rows(
    tmp_path: Any,
) -> None:
    gzip_path = tmp_path / 'qs.fake.txt.gz'
    # Write a trivial gzip blob so _download_and_parse's gzip.open works;
    # we'll mock download_with_retries so no actual HTTP is fired, and
    # mock pd.read_csv to return our synthetic chunk.
    with gzip.open(gzip_path, 'wt') as fh:
        fh.write('header\n')

    chunk = _fake_tsv_frame()
    with (
        patch.object(nass_yields, 'fetch_with_mirror'),
        patch(
            'jdluc.extract.nass_yields.pd.read_csv',
            return_value=iter([chunk]),
        ),
    ):
        df = nass_yields._download_and_parse(str(gzip_path), 'ws-dev')

    # CORN-STATE-YIELD, SOYBEANS-COUNTY-YIELD survive. BARLEY /
    # AREA PLANTED / suppressed rows are dropped.
    assert list(df.columns) == nass_yields.ROW_COLUMNS
    commodities = sorted(df['commodity_desc'].unique().tolist())
    assert commodities == ['CORN', 'SOYBEANS']
    assert df['value_bu_per_acre'].dtype == float
    assert df['year'].dtype == int
    assert set(df['state_fips'].unique()) == {'10', '19'}


def test_download_and_parse_raises_when_nothing_matches(tmp_path: Any) -> None:
    gzip_path = tmp_path / 'qs.empty.txt.gz'
    with gzip.open(gzip_path, 'wt') as fh:
        fh.write('header\n')

    empty_chunk = pd.DataFrame(
        {
            'STATISTICCAT_DESC': ['AREA PLANTED'],
            'COMMODITY_DESC': ['CORN'],
            'AGG_LEVEL_DESC': ['STATE'],
            'UNIT_DESC': ['ACRES'],
            'YEAR': ['2020'],
            'VALUE': ['1'],
            'STATE_FIPS_CODE': ['10'],
            'STATE_NAME': ['DELAWARE'],
        }
    )
    with (
        patch.object(nass_yields, 'fetch_with_mirror'),
        patch(
            'jdluc.extract.nass_yields.pd.read_csv',
            return_value=iter([empty_chunk]),
        ),
        pytest.raises(RuntimeError, match='no YIELD rows'),
    ):
        nass_yields._download_and_parse(str(gzip_path), 'ws-dev')


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                'state_fips': '10',
                'state_name': 'DELAWARE',
                'commodity_desc': 'CORN',
                'year': 2020,
                'value_bu_per_acre': 180.0,
                'agg_level_desc': 'STATE',
                'unit_desc': 'BU / ACRE',
            }
        ]
    )


def test_extract_nass_yields_stages_csv_to_gcs_and_ingests() -> None:
    df = _sample_df()
    with (
        patch.object(nass_yields, '_download_and_parse', return_value=df),
        patch.object(
            nass_yields,
            'upload_to_gcs',
            side_effect=lambda project, bucket, blob, path: f'gs://{bucket}/{blob}',
        ) as upload,
        patch.object(nass_yields, 'start_table_ingestion_and_wait') as ingest,
        patch.object(nass_yields, 'delete_gcs_blob') as delete_gcs,
        patch.object(nass_yields, 'delete_asset_if_present') as delete_asset,
    ):
        asset_id = nass_yields.extract_nass_yields(gcp_project='ws-dev')

    assert asset_id == GEE_NASS_YIELDS
    delete_asset.assert_not_called()  # force=False

    # CSV gets uploaded to a stable per-run GCS path under the staging
    # prefix; the ingestion request reads from that GCS URI.
    upload.assert_called_once()
    project, bucket, blob, path = upload.call_args.args
    assert project == 'ws-dev'
    assert bucket == GCS_BUCKET_NAME
    assert blob.startswith(f'{nass_yields.GCS_STAGING_PREFIX}/')
    assert blob.endswith('/nass_yields_raw.csv')
    # Local path written by pandas was a real CSV (deleted by cleanup
    # before the assertion runs, so we can only check the upload arg).
    assert path.endswith('.csv')

    ingest.assert_called_once()
    args, kwargs = ingest.call_args.args, ingest.call_args.kwargs
    assert args[0] == f'gs://{bucket}/{blob}'
    assert args[1] == GEE_NASS_YIELDS
    assert kwargs.get('allow_overwrite') is False

    # Staging blob is cleaned up after ingestion (success path).
    delete_gcs.assert_called_once_with('ws-dev', bucket, blob)


def test_extract_nass_yields_force_clears_existing_asset() -> None:
    df = _sample_df()
    with (
        patch.object(nass_yields, '_download_and_parse', return_value=df),
        patch.object(
            nass_yields,
            'upload_to_gcs',
            side_effect=lambda project, bucket, blob, path: f'gs://{bucket}/{blob}',
        ),
        patch.object(nass_yields, 'start_table_ingestion_and_wait') as ingest,
        patch.object(nass_yields, 'delete_gcs_blob'),
        patch.object(nass_yields, 'delete_asset_if_present') as delete_asset,
    ):
        nass_yields.extract_nass_yields(gcp_project='ws-dev', force=True)

    delete_asset.assert_called_once_with(GEE_NASS_YIELDS)
    # force=True propagates to allow_overwrite on the ingestion request.
    assert ingest.call_args.kwargs.get('allow_overwrite') is True


def test_extract_nass_yields_cleans_up_gcs_blob_on_ingestion_failure() -> None:
    df = _sample_df()
    with (
        patch.object(nass_yields, '_download_and_parse', return_value=df),
        patch.object(
            nass_yields,
            'upload_to_gcs',
            side_effect=lambda project, bucket, blob, path: f'gs://{bucket}/{blob}',
        ),
        patch.object(
            nass_yields,
            'start_table_ingestion_and_wait',
            side_effect=RuntimeError('GEE 500'),
        ),
        patch.object(nass_yields, 'delete_gcs_blob') as delete_gcs,
        patch.object(nass_yields, 'delete_asset_if_present'),
    ):
        with pytest.raises(RuntimeError, match='GEE 500'):
            nass_yields.extract_nass_yields(gcp_project='ws-dev')

    # GCS blob still gets cleaned up on the failure path so we don't
    # leak staging objects when the orchestrator records the failure
    # and continues with other datasets.
    delete_gcs.assert_called_once()


def test_row_columns_match_transform_consumer_schema() -> None:
    # transform/summary_tables.py::_filter_average_convert_nass_rows reads
    # exactly these keys — lock the schema so a later drift is caught here.
    assert set(nass_yields.ROW_COLUMNS) == {
        'state_fips',
        'state_name',
        'commodity_desc',
        'year',
        'value_bu_per_acre',
        'agg_level_desc',
        'unit_desc',
    }

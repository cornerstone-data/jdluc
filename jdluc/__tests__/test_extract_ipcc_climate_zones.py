"""Unit tests for extract/ipcc_climate_zones.py.

Mocks Zenodo HTTP + GCS + GEE so the tests run offline. Covers the
scratch-then-reproject two-step upload flow and the cleanup contract.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import ipcc_climate_zones
from jdluc.utils.constants import (
    GEE_IPCC_CLIMATE_ZONES,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    IPCC_CLIMATE_ZONES_ZENODO_URL,
)


@pytest.fixture
def mock_io() -> Any:
    with (
        patch.object(ipcc_climate_zones, 'asset_exists') as asset_exists,
        patch.object(ipcc_climate_zones, 'delete_asset_if_present') as delete_asset,
        patch.object(ipcc_climate_zones, 'delete_gcs_blob') as delete_gcs,
        patch.object(ipcc_climate_zones, 'fetch_with_mirror') as fetch,
        patch.object(ipcc_climate_zones, 'start_ingestion_and_wait') as ingest,
        patch.object(ipcc_climate_zones, 'upload_to_gcs') as upload,
        patch.object(ipcc_climate_zones, 'wait_for_export_task') as wait_export,
    ):
        ns = MagicMock()
        ns.asset_exists = asset_exists
        ns.delete_asset = delete_asset
        ns.delete_gcs = delete_gcs
        ns.fetch = fetch
        ns.ingest = ingest
        ns.upload = upload
        ns.wait_export = wait_export
        asset_exists.return_value = False
        upload.side_effect = lambda project, bucket, blob, path: f'gs://{bucket}/{blob}'
        yield ns


def _fake_zenodo_record(tif_key: str = 'CLIMATE_ZONE.tif') -> Any:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={
            'files': [
                {
                    'key': 'README.md',
                    'links': {'self': 'https://zenodo/readme'},
                },
                {
                    'key': tif_key,
                    'links': {'self': f'https://zenodo/{tif_key}'},
                },
            ]
        }
    )
    return resp


def test_discover_zenodo_tif_url_finds_single_tif() -> None:
    with patch('jdluc.extract.ipcc_climate_zones.requests.get') as get:
        get.return_value = _fake_zenodo_record()
        filename, url = ipcc_climate_zones._discover_zenodo_tif_url()

    assert filename.lower().endswith('.tif')
    assert url == 'https://zenodo/CLIMATE_ZONE.tif'
    assert get.call_args.args[0] == IPCC_CLIMATE_ZONES_ZENODO_URL


def test_discover_zenodo_tif_url_rejects_multiple_tifs() -> None:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={
            'files': [
                {'key': 'a.tif', 'links': {'self': 'x'}},
                {'key': 'b.tif', 'links': {'self': 'y'}},
            ]
        }
    )
    with (
        patch(
            'jdluc.extract.ipcc_climate_zones.requests.get',
            return_value=resp,
        ),
        pytest.raises(RuntimeError, match='expected to contain exactly 1 .tif'),
    ):
        ipcc_climate_zones._discover_zenodo_tif_url()


def test_extract_ipcc_runs_ingest_then_reproject_then_cleanup(mock_io: Any) -> None:
    export_task = MagicMock()

    with (
        patch.object(
            ipcc_climate_zones,
            '_discover_zenodo_tif_url',
            return_value=('CLIMATE_ZONE.tif', 'https://zenodo/CLIMATE_ZONE.tif'),
        ),
        patch.object(
            ipcc_climate_zones,
            '_start_reproject_export',
            return_value=export_task,
        ) as start_export,
    ):
        asset_id = ipcc_climate_zones.extract_ipcc_climate_zones(gcp_project='ws-dev')

    assert asset_id == GEE_IPCC_CLIMATE_ZONES
    mock_io.fetch.assert_called_once()
    fetch_kwargs = mock_io.fetch.call_args.kwargs
    assert fetch_kwargs['dataset'] == 'ipcc_climate_zones'
    assert fetch_kwargs['filename'] == ipcc_climate_zones.SOURCE_FILENAME
    # IPCC uses a deferred-discovery callable source.
    assert callable(fetch_kwargs['source'])
    mock_io.upload.assert_called_once()
    mock_io.ingest.assert_called_once()
    start_export.assert_called_once_with(
        ipcc_climate_zones.SCRATCH_ASSET_ID, GEE_IPCC_CLIMATE_ZONES
    )
    mock_io.wait_export.assert_called_once()
    # GCS blob is cleaned up (in a try/finally around the ingest+export).
    mock_io.delete_gcs.assert_called_once()
    # Scratch asset is deleted after the final asset is written.
    mock_io.delete_asset.assert_called_with(ipcc_climate_zones.SCRATCH_ASSET_ID)


def test_extract_ipcc_force_deletes_scratch_and_final_first(mock_io: Any) -> None:
    with (
        patch.object(
            ipcc_climate_zones,
            '_discover_zenodo_tif_url',
            return_value=('CLIMATE_ZONE.tif', 'https://zenodo/CLIMATE_ZONE.tif'),
        ),
        patch.object(ipcc_climate_zones, '_start_reproject_export') as start_export,
    ):
        start_export.return_value = MagicMock()
        ipcc_climate_zones.extract_ipcc_climate_zones(gcp_project='ws-dev', force=True)

    # Both scratch and final assets are deleted at the top on force=True.
    delete_targets = [call.args[0] for call in mock_io.delete_asset.call_args_list]
    assert ipcc_climate_zones.SCRATCH_ASSET_ID in delete_targets
    assert GEE_IPCC_CLIMATE_ZONES in delete_targets
    # ingest is invoked with allow_overwrite=True on force.
    assert mock_io.ingest.call_args.kwargs['allow_overwrite'] is True


def test_extract_ipcc_clears_stale_scratch_when_not_forcing(mock_io: Any) -> None:
    # asset_exists returns True for the scratch probe only — simulates a
    # prior partial run that left scratch but not the final asset.
    mock_io.asset_exists.side_effect = lambda aid: (
        aid == ipcc_climate_zones.SCRATCH_ASSET_ID
    )

    with (
        patch.object(
            ipcc_climate_zones,
            '_discover_zenodo_tif_url',
            return_value=('CLIMATE_ZONE.tif', 'https://zenodo/CLIMATE_ZONE.tif'),
        ),
        patch.object(
            ipcc_climate_zones, '_start_reproject_export', return_value=MagicMock()
        ),
    ):
        ipcc_climate_zones.extract_ipcc_climate_zones(gcp_project='ws-dev')

    # Expect an extra delete_asset_if_present(scratch) at the top.
    scratch_deletes = [
        c
        for c in mock_io.delete_asset.call_args_list
        if c.args[0] == ipcc_climate_zones.SCRATCH_ASSET_ID
    ]
    assert len(scratch_deletes) >= 1


def test_reproject_export_targets_glad_grid() -> None:
    # Guards the methodology invariant: IPCC final asset is pinned to the
    # GLAD 0.00025° grid. Mocks ee so the test runs without credentials.
    with patch('jdluc.extract.ipcc_climate_zones.ee') as ee_mock:
        image = MagicMock()
        ee_mock.Image.return_value = image
        ee_mock.Geometry.Rectangle.return_value = 'region'
        export_task = MagicMock()
        ee_mock.batch.Export.image.toAsset.return_value = export_task
        image.reproject.return_value = 'reprojected'

        result = ipcc_climate_zones._start_reproject_export('src-asset', 'dst-asset')

    assert result is export_task
    image.reproject.assert_called_once_with(
        crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM
    )
    kwargs = ee_mock.batch.Export.image.toAsset.call_args.kwargs
    assert kwargs['assetId'] == 'dst-asset'
    assert kwargs['crs'] == GLAD_CRS
    assert kwargs['crsTransform'] == GLAD_CRS_TRANSFORM
    export_task.start.assert_called_once()

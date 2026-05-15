"""Unit tests for extract/huang_bgb.py.

Mocks HTTP + zip + NetCDF → GeoTIFF + GCS + ingestion so the tests run
offline. Guards the Design Decision 4 migration: no reproject / NoData
fill at extract (that moved to transform/emissions.py).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import huang_bgb
from jdluc.utils.constants import GEE_HUANG_BGB, HUANG_BGB_FIGSHARE_URL


@pytest.fixture
def mock_io() -> Any:
    """Patch every _io helper used by huang_bgb with MagicMocks."""
    with (
        patch.object(huang_bgb, 'delete_asset_if_present') as delete_asset,
        patch.object(huang_bgb, 'delete_gcs_blob') as delete_gcs,
        patch.object(huang_bgb, 'fetch_with_mirror') as fetch,
        patch.object(huang_bgb, 'start_ingestion_and_wait') as ingest,
        patch.object(huang_bgb, 'upload_to_gcs') as upload,
    ):

        ns = MagicMock()
        ns.delete_asset = delete_asset
        ns.delete_gcs = delete_gcs
        ns.fetch = fetch
        ns.ingest = ingest
        ns.upload = upload
        upload.side_effect = lambda project, bucket, blob, path: f'gs://{bucket}/{blob}'
        yield ns


def test_extract_huang_bgb_happy_path(mock_io: Any) -> None:
    with (
        patch.object(huang_bgb, '_extract_netcdf') as extract_nc,
        patch.object(huang_bgb, '_write_conus_geotiff') as write_tif,
    ):
        extract_nc.return_value = '/tmp/fake/pergridarea_bgb.nc'
        asset_id = huang_bgb.extract_huang_bgb(gcp_project='ws-dev')

    assert asset_id == GEE_HUANG_BGB
    mock_io.fetch.assert_called_once()
    fetch_kwargs = mock_io.fetch.call_args.kwargs
    assert fetch_kwargs['dataset'] == 'huang_bgb'
    assert fetch_kwargs['filename'] == 'data_code_to_submit.zip'
    assert fetch_kwargs['source'] == HUANG_BGB_FIGSHARE_URL
    assert fetch_kwargs['gcp_project'] == 'ws-dev'

    extract_nc.assert_called_once()
    write_tif.assert_called_once()

    mock_io.upload.assert_called_once()
    mock_io.ingest.assert_called_once()
    ingest_kwargs = mock_io.ingest.call_args.kwargs
    assert mock_io.ingest.call_args.args[1] == GEE_HUANG_BGB
    assert ingest_kwargs['band_name'] == huang_bgb.BAND_NAME
    assert ingest_kwargs['allow_overwrite'] is False

    mock_io.delete_gcs.assert_called_once()
    mock_io.delete_asset.assert_not_called()


def test_extract_huang_bgb_force_deletes_existing_asset_first(mock_io: Any) -> None:
    with (
        patch.object(huang_bgb, '_extract_netcdf') as extract_nc,
        patch.object(huang_bgb, '_write_conus_geotiff'),
    ):
        extract_nc.return_value = '/tmp/fake/pergridarea_bgb.nc'
        huang_bgb.extract_huang_bgb(gcp_project='ws-dev', force=True)

    # force=True → delete existing asset before starting.
    mock_io.delete_asset.assert_called_once_with(GEE_HUANG_BGB)
    # And pass through to the ingest with allow_overwrite=True.
    assert mock_io.ingest.call_args.kwargs['allow_overwrite'] is True


def test_extract_netcdf_missing_member_raises(tmp_path: Any) -> None:
    import zipfile

    zip_path = tmp_path / 'test.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('README.md', 'nothing to see here')

    with pytest.raises(RuntimeError, match='not found in archive'):
        huang_bgb._extract_netcdf(str(zip_path), str(tmp_path))


def test_extract_huang_bgb_does_not_reproject_or_gap_fill() -> None:
    # CRS + NoData handling lives in transform/emissions.py, not extract.
    # Guard the public surface so a future contributor doesn't silently
    # reintroduce reprojection here.
    import inspect

    src = inspect.getsource(huang_bgb)
    assert '.reproject(' not in src
    assert '.unmask(' not in src


def test_conus_bounds_are_mainland_usa() -> None:
    assert huang_bgb.CONUS_WEST == -130.0
    assert huang_bgb.CONUS_EAST == -65.0
    assert huang_bgb.CONUS_SOUTH == 24.0
    assert huang_bgb.CONUS_NORTH == 50.0

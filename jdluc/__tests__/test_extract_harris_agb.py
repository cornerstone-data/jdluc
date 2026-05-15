"""Unit tests for extract/harris_agb.py.

Mocks HTTP, GCS, and GEE ingestion so the tests run offline. Validates
URL construction, per-tile dispatch, the force-rebuild path, and the
cache-hit path (all 19 tiles already ingested).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import _tile_pipeline, harris_agb
from jdluc.utils.constants import (
    GEE_HARRIS_AGB,
    HARRIS_AGB_ARCGIS_FEATURESERVER,
)


@pytest.fixture
def mock_io() -> Any:
    """Patch every _io helper used by the shared tile orchestrator.

    The submit (``start_ingestion_no_wait``) and poll (``wait_for_tasks``)
    halves of ingestion are patched separately — together they replace
    the pre-Phase-9 single ``start_ingestion_and_wait`` call. ``submit``
    returns a stable task ID per asset_id so wait/cleanup bookkeeping is
    deterministic in assertions. Patches land on ``_tile_pipeline`` (the
    orchestrator's import surface) since the per-dataset module no
    longer references these helpers directly.
    """
    with (
        patch.object(_tile_pipeline, 'asset_exists') as asset_exists,
        patch.object(
            _tile_pipeline, 'create_image_collection_if_absent'
        ) as create_coll,
        patch.object(_tile_pipeline, 'delete_asset_if_present') as delete_asset,
        patch.object(_tile_pipeline, 'delete_gcs_blob') as delete_gcs,
        patch.object(_tile_pipeline, 'fetch_with_mirror') as fetch,
        patch.object(_tile_pipeline, 'start_ingestion_no_wait') as submit,
        patch.object(_tile_pipeline, 'upload_to_gcs') as upload,
        patch.object(_tile_pipeline, 'wait_for_tasks') as poll,
    ):

        ns = MagicMock()
        ns.asset_exists = asset_exists
        ns.create_coll = create_coll
        ns.delete_asset = delete_asset
        ns.delete_gcs = delete_gcs
        ns.fetch = fetch
        ns.submit = submit
        ns.upload = upload
        ns.poll = poll

        upload.side_effect = lambda project, bucket, blob, path: f'gs://{bucket}/{blob}'
        # Default: every submission returns task_<asset_id>; every poll
        # reports zero failures.
        submit.side_effect = lambda gcs_uri, asset_id, **_kw: f'task_{asset_id}'
        poll.return_value = {}
        yield ns


def _fake_feature_server_response(tile_id: str) -> Any:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={
            'features': [
                {
                    'attributes': {
                        'tile_id': tile_id,
                        'Mg_ha_1_download': f'https://signed/{tile_id}.tif',
                    }
                }
            ]
        }
    )
    return resp


def test_conus_tile_ids_match_feature_server_manifest() -> None:
    # Full 3×7 grid would be 21 tiles, but the FeatureServer omits
    # 30N_070W (Caribbean) and 30N_130W (Pacific) — both ocean with
    # no biomass — so we list only the 19 tiles it publishes.
    assert len(harris_agb.CONUS_TILE_IDS) == 19
    assert len(set(harris_agb.CONUS_TILE_IDS)) == 19
    assert '30N_070W' not in harris_agb.CONUS_TILE_IDS
    assert '30N_130W' not in harris_agb.CONUS_TILE_IDS
    for expected in ('40N_080W', '40N_090W', '50N_100W', '30N_080W'):
        assert expected in harris_agb.CONUS_TILE_IDS


def test_feature_server_url_resolution() -> None:
    tile_id = '40N_080W'
    with patch('jdluc.extract.harris_agb.requests.get') as get:
        get.return_value = _fake_feature_server_response(tile_id)
        url = harris_agb._fetch_tile_download_url(tile_id)

    assert url == f'https://signed/{tile_id}.tif'
    call = get.call_args
    assert call.args[0] == HARRIS_AGB_ARCGIS_FEATURESERVER
    assert call.kwargs['params']['where'] == f"tile_id='{tile_id}'"
    assert 'Mg_ha_1_download' in call.kwargs['params']['outFields']


def test_feature_server_missing_tile_raises() -> None:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={'features': []})
    with patch('jdluc.extract.harris_agb.requests.get', return_value=resp):
        with pytest.raises(RuntimeError, match='not found on FeatureServer'):
            harris_agb._fetch_tile_download_url('99N_999W')


def test_extract_harris_agb_all_tiles_cache_hit_skips_download(mock_io: Any) -> None:
    mock_io.asset_exists.return_value = True

    with patch('jdluc.extract.harris_agb.requests.get') as get:
        asset_id = harris_agb.extract_harris_agb(
            gcp_project='ws-dev',
            tile_ids=['40N_080W', '30N_090W'],
        )

    assert asset_id == GEE_HARRIS_AGB
    get.assert_not_called()
    mock_io.fetch.assert_not_called()
    mock_io.upload.assert_not_called()
    mock_io.submit.assert_not_called()
    mock_io.poll.assert_not_called()
    mock_io.create_coll.assert_called_once_with(GEE_HARRIS_AGB)


def test_extract_harris_agb_missing_tiles_are_ingested(mock_io: Any) -> None:
    mock_io.asset_exists.return_value = False

    harris_agb.extract_harris_agb(
        gcp_project='ws-dev',
        tile_ids=['40N_080W', '30N_090W'],
    )

    # Both tiles got fetched via the mirror helper + uploaded + submitted.
    # Phase A staging runs in a ThreadPoolExecutor, so call ordering is
    # non-deterministic — assertions are unordered.
    assert mock_io.fetch.call_count == 2
    fetch_kwargs_list = [c.kwargs for c in mock_io.fetch.call_args_list]
    assert {kw['dataset'] for kw in fetch_kwargs_list} == {'harris_agb'}
    assert {kw['filename'] for kw in fetch_kwargs_list} == {
        '40N_080W.tif',
        '30N_090W.tif',
    }
    # The source is a callable (deferred FeatureServer resolver), not a bare string.
    for kw in fetch_kwargs_list:
        assert callable(kw['source'])

    assert mock_io.upload.call_count == 2
    # Phase B submit fans out one task per staged tile.
    assert mock_io.submit.call_count == 2
    submit_asset_ids = {call.args[1] for call in mock_io.submit.call_args_list}
    assert submit_asset_ids == {
        f'{GEE_HARRIS_AGB}/tile_40N_080W',
        f'{GEE_HARRIS_AGB}/tile_30N_090W',
    }
    # Phase C is one bulk poll over both task IDs.
    assert mock_io.poll.call_count == 1
    polled_task_ids = set(mock_io.poll.call_args.args[0])
    assert polled_task_ids == {
        f'task_{GEE_HARRIS_AGB}/tile_40N_080W',
        f'task_{GEE_HARRIS_AGB}/tile_30N_090W',
    }
    # Phase D cleans up every staged blob.
    assert mock_io.delete_gcs.call_count == 2


def test_extract_harris_agb_tile_url_resolver_uses_feature_server(mock_io: Any) -> None:
    """The source callable passed to fetch_with_mirror hits the FeatureServer."""
    mock_io.asset_exists.return_value = False
    captured_sources: list[Any] = []

    def _capture(
        local_path: str,
        *,
        dataset: str,
        filename: str,
        gcp_project: str,
        source: Any,
        **_kwargs: Any,
    ) -> None:
        del local_path, dataset, filename, gcp_project
        captured_sources.append(source)

    mock_io.fetch.side_effect = _capture

    with patch('jdluc.extract.harris_agb.requests.get') as get:
        get.return_value = _fake_feature_server_response('40N_080W')
        harris_agb.extract_harris_agb(
            gcp_project='ws-dev',
            tile_ids=['40N_080W'],
        )
        # Invoking the resolver triggers the FeatureServer request.
        resolved = captured_sources[0]()

    assert resolved == 'https://signed/40N_080W.tif'
    assert get.call_count == 1


def test_extract_harris_agb_force_with_subset_only_deletes_those_tiles(
    mock_io: Any,
) -> None:
    """force=True with explicit tile_ids deletes only those tiles, not the collection.

    Regression test: force=True historically triggered
    _delete_collection_if_present() which iterated CONUS_TILE_IDS,
    silently destroying any tile assets outside the caller's tile_ids
    list.
    """
    mock_io.asset_exists.return_value = False

    with patch.object(_tile_pipeline, '_delete_collection_if_present') as clear:
        harris_agb.extract_harris_agb(
            gcp_project='ws-dev',
            force=True,
            tile_ids=['40N_080W'],
        )

    clear.assert_not_called()
    # The single tile asset was pre-deleted before re-extraction.
    delete_targets = [c.args[0] for c in mock_io.delete_asset.call_args_list]
    assert f'{GEE_HARRIS_AGB}/tile_40N_080W' in delete_targets
    # No sibling tiles touched.
    for sibling in ('30N_090W', '50N_100W'):
        assert f'{GEE_HARRIS_AGB}/tile_{sibling}' not in delete_targets
    # force → submission runs with allow_overwrite=True.
    assert mock_io.submit.call_args.kwargs['allow_overwrite'] is True


def test_extract_harris_agb_force_with_default_tile_ids_clears_collection(
    mock_io: Any,
) -> None:
    """force=True with the default tile list (None → CONUS) drops the collection.

    Pairs with the subset-only-deletes-those-tiles regression test:
    full-rebuild semantics keep the existing nuke-and-recreate behavior
    so partial collections from prior crashes get cleaned up.
    """
    mock_io.asset_exists.return_value = True

    with patch.object(_tile_pipeline, '_delete_collection_if_present') as clear:
        harris_agb.extract_harris_agb(
            gcp_project='ws-dev',
            force=True,
            # tile_ids=None → defaults to CONUS_TILE_IDS
        )

    clear.assert_called_once()


def test_extract_harris_agb_partial_ingest_failure_isolated(mock_io: Any) -> None:
    """One FAILED task does not abort siblings; orchestrator returns the IC."""
    mock_io.asset_exists.return_value = False
    mock_io.poll.return_value = {
        f'task_{GEE_HARRIS_AGB}/tile_40N_080W': 'manifest invalid',
    }

    asset_id = harris_agb.extract_harris_agb(
        gcp_project='ws-dev',
        tile_ids=['40N_080W', '30N_090W'],
    )

    assert asset_id == GEE_HARRIS_AGB
    # Both blobs still got cleaned up — partial failure shouldn't leak GCS.
    assert mock_io.delete_gcs.call_count == 2


def test_extract_harris_agb_staging_failure_does_not_leave_gcs_blob(
    mock_io: Any,
) -> None:
    """A Phase A staging exception aborts that tile's submit — no blob, no leak."""
    mock_io.asset_exists.return_value = False
    mock_io.fetch.side_effect = RuntimeError('simulated download failure')

    # The orchestrator logs and continues — staging exceptions don't abort
    # the run, but with the only tile failing there's nothing left to submit.
    asset_id = harris_agb.extract_harris_agb(
        gcp_project='ws-dev',
        tile_ids=['40N_080W'],
    )

    assert asset_id == GEE_HARRIS_AGB
    # Failed staging never reached upload, so there's no blob to delete.
    mock_io.upload.assert_not_called()
    mock_io.submit.assert_not_called()
    mock_io.delete_gcs.assert_not_called()

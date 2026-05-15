"""Unit tests for extract/gfw_peatlands.py.

Mocks GCS and GEE ingestion so the tests run offline. Validates URL
construction (no HTTP probe — the URL is template-constructed),
per-tile dispatch, the force-rebuild path, the cache-hit path (all
19 tiles already ingested), and a custom tile-list override.

The GFW Peatlands module is structurally a clone of extract/harris_agb.py
under the GFW Global Peatlands raster swap. Tests mirror the
test_extract_harris_agb.py shape with two key differences:
  - URL construction is done from a template + API key (no
    FeatureServer query), so no HTTP mocks are needed for resolution.
  - The tile list is GFW-Peatlands-specific: GFW omits tiles with no
    peatland pixels, so CONUS coverage is 19 tiles, not Harris
    AGB's 21.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import _tile_pipeline, gfw_peatlands
from jdluc.utils.constants import (
    GEE_GFW_PEATLANDS,
    GFW_DATA_API_KEY,
    GFW_PEATLANDS_URL_TEMPLATE,
)


@pytest.fixture
def mock_io() -> Any:
    """Patch every IO helper used by the shared tile orchestrator.

    Submit and poll halves of ingestion are patched separately to match
    the fan-out shape; ``submit`` returns a stable task ID per
    asset_id so wait/cleanup bookkeeping is deterministic in assertions.
    Patches land on ``_tile_pipeline`` (the orchestrator's import
    surface) since the per-dataset module no longer references these
    helpers directly.
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
        submit.side_effect = lambda gcs_uri, asset_id, **_kw: f'task_{asset_id}'
        poll.return_value = {}
        yield ns


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_resolve_tile_url_uses_template_and_api_key() -> None:
    url = gfw_peatlands._resolve_tile_url('40N_080W')
    # URL incorporates the dataset version, tile_id, pixel_meaning, and key.
    assert 'gfw_peatlands' in url
    assert 'v20230315' in url
    assert 'tile_id=40N_080W' in url
    assert 'pixel_meaning=is' in url
    assert f'x-api-key={GFW_DATA_API_KEY}' in url


def test_resolve_tile_url_template_round_trip() -> None:
    """The constants module's URL template formats round-trip cleanly."""
    expected = GFW_PEATLANDS_URL_TEMPLATE.format(
        tile_id='30N_090W', api_key=GFW_DATA_API_KEY
    )
    assert gfw_peatlands._resolve_tile_url('30N_090W') == expected


def test_tile_image_asset_id_format() -> None:
    aid = _tile_pipeline._tile_image_asset_id(GEE_GFW_PEATLANDS, '40N_080W')
    assert aid == f'{GEE_GFW_PEATLANDS}/tile_40N_080W'


def test_conus_tile_ids_match_gfw_manifest() -> None:
    """CONUS_TILE_IDS lists the 19 CONUS tiles GFW actually publishes.

    GFW's v20230315 manifest omits tiles with no peatland pixels.
    The Harris AGB grid has 21 CONUS tiles; GFW publishes 19.
    30N_070W (Caribbean) and 30N_130W (Pacific) are absent — both
    have no land at the corresponding latitudes.
    """
    assert len(gfw_peatlands.CONUS_TILE_IDS) == 19
    assert '30N_070W' not in gfw_peatlands.CONUS_TILE_IDS
    assert '30N_130W' not in gfw_peatlands.CONUS_TILE_IDS
    # Spot-check land-bearing tiles are included.
    for expected in ('40N_080W', '40N_090W', '50N_100W', '30N_080W'):
        assert expected in gfw_peatlands.CONUS_TILE_IDS


# ---------------------------------------------------------------------------
# Main extract flow
# ---------------------------------------------------------------------------


def test_extract_gfw_peatlands_all_tiles_cache_hit_skips_download(
    mock_io: Any,
) -> None:
    """Every tile asset already exists → no fetch / upload / ingest."""
    mock_io.asset_exists.return_value = True

    asset_id = gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W', '30N_090W'],
    )

    assert asset_id == GEE_GFW_PEATLANDS
    mock_io.fetch.assert_not_called()
    mock_io.upload.assert_not_called()
    mock_io.submit.assert_not_called()
    mock_io.poll.assert_not_called()
    mock_io.create_coll.assert_called_once_with(GEE_GFW_PEATLANDS)


def test_extract_gfw_peatlands_missing_tiles_are_ingested(mock_io: Any) -> None:
    """Cache misses trigger fetch + upload + submit per tile, single bulk poll."""
    mock_io.asset_exists.return_value = False

    gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W', '30N_090W'],
    )

    # Phase A staging runs in a ThreadPoolExecutor, so call ordering is
    # non-deterministic — assertions are unordered.
    assert mock_io.fetch.call_count == 2
    fetch_kwargs_list = [c.kwargs for c in mock_io.fetch.call_args_list]
    assert {kw['dataset'] for kw in fetch_kwargs_list} == {'gfw_peatlands'}
    assert {kw['filename'] for kw in fetch_kwargs_list} == {
        '40N_080W.tif',
        '30N_090W.tif',
    }
    for kw in fetch_kwargs_list:
        assert callable(kw['source'])

    assert mock_io.upload.call_count == 2
    assert mock_io.submit.call_count == 2
    submit_asset_ids = {call.args[1] for call in mock_io.submit.call_args_list}
    assert submit_asset_ids == {
        f'{GEE_GFW_PEATLANDS}/tile_40N_080W',
        f'{GEE_GFW_PEATLANDS}/tile_30N_090W',
    }
    # Phase C is one bulk poll over both task IDs.
    assert mock_io.poll.call_count == 1
    polled_task_ids = set(mock_io.poll.call_args.args[0])
    assert polled_task_ids == {
        f'task_{GEE_GFW_PEATLANDS}/tile_40N_080W',
        f'task_{GEE_GFW_PEATLANDS}/tile_30N_090W',
    }
    # GCS staging blobs are cleaned up after the run.
    assert mock_io.delete_gcs.call_count == 2
    # Band name is passed through to submission.
    for call in mock_io.submit.call_args_list:
        assert call.kwargs['band_name'] == 'is_peatland'


def test_extract_gfw_peatlands_tile_url_resolver_returns_template_url(
    mock_io: Any,
) -> None:
    """The source callable formats the GFW URL template — no HTTP probe."""
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

    gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W'],
    )
    resolved = captured_sources[0]()
    expected = gfw_peatlands._resolve_tile_url('40N_080W')
    assert resolved == expected
    assert 'tile_id=40N_080W' in resolved


def test_extract_gfw_peatlands_force_with_subset_only_deletes_those_tiles(
    mock_io: Any,
) -> None:
    """force=True with explicit tile_ids deletes only those tiles, not the collection.

    Regression test mirrored from harris_agb: subset force-rebuilds must
    not destroy sibling tiles in the parent collection.
    """
    mock_io.asset_exists.return_value = False

    with patch.object(_tile_pipeline, '_delete_collection_if_present') as clear:
        gfw_peatlands.extract_gfw_peatlands(
            gcp_project='ws-dev',
            force=True,
            tile_ids=['40N_080W'],
        )

    clear.assert_not_called()
    delete_targets = [c.args[0] for c in mock_io.delete_asset.call_args_list]
    assert f'{GEE_GFW_PEATLANDS}/tile_40N_080W' in delete_targets
    for sibling in ('30N_090W', '50N_100W'):
        assert f'{GEE_GFW_PEATLANDS}/tile_{sibling}' not in delete_targets
    assert mock_io.submit.call_args.kwargs['allow_overwrite'] is True


def test_extract_gfw_peatlands_force_with_default_tile_ids_clears_collection(
    mock_io: Any,
) -> None:
    """force=True with the default tile list drops the parent collection."""
    mock_io.asset_exists.return_value = True

    with patch.object(_tile_pipeline, '_delete_collection_if_present') as clear:
        gfw_peatlands.extract_gfw_peatlands(
            gcp_project='ws-dev',
            force=True,
        )

    clear.assert_called_once()


def test_extract_gfw_peatlands_uses_default_conus_tile_list_when_none(
    mock_io: Any,
) -> None:
    """Default tile_ids=None resolves to CONUS_TILE_IDS (19 tiles)."""
    mock_io.asset_exists.return_value = True  # all cache-hit so no real work
    gfw_peatlands.extract_gfw_peatlands(gcp_project='ws-dev')
    # 19 cache-hit log paths, no fetches.
    mock_io.fetch.assert_not_called()
    # asset_exists was probed once per tile.
    assert mock_io.asset_exists.call_count >= len(gfw_peatlands.CONUS_TILE_IDS)


def test_extract_gfw_peatlands_custom_tile_list_processes_only_given_tiles(
    mock_io: Any,
) -> None:
    """Custom tile_ids override; any other tile IDs are not touched."""
    mock_io.asset_exists.return_value = False
    gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_100W', '50N_120W', '30N_080W'],
    )

    assert mock_io.fetch.call_count == 3
    # Phase A staging is non-deterministic, compare as a set.
    fetch_filenames = {c.kwargs['filename'] for c in mock_io.fetch.call_args_list}
    assert fetch_filenames == {'40N_100W.tif', '50N_120W.tif', '30N_080W.tif'}


# ---------------------------------------------------------------------------
# Failure & cleanup contracts
# ---------------------------------------------------------------------------


def test_staging_failure_does_not_leave_gcs_blob_behind(mock_io: Any) -> None:
    """A Phase A staging exception is logged and isolated to that tile.

    With the only tile failing, there's nothing to submit and no blob to
    clean up. The orchestrator returns the (empty) collection asset ID
    rather than aborting — sibling tiles in a multi-tile run would still
    complete.
    """
    mock_io.asset_exists.return_value = False
    mock_io.fetch.side_effect = RuntimeError('simulated download failure')

    asset_id = gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W'],
    )
    assert asset_id == GEE_GFW_PEATLANDS

    mock_io.upload.assert_not_called()
    mock_io.submit.assert_not_called()
    mock_io.delete_gcs.assert_not_called()


def test_partial_ingest_failure_isolated(mock_io: Any) -> None:
    """A FAILED ingest task does not abort siblings; cleanup still happens."""
    mock_io.asset_exists.return_value = False
    mock_io.poll.return_value = {
        f'task_{GEE_GFW_PEATLANDS}/tile_40N_080W': 'manifest invalid',
    }

    asset_id = gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W', '30N_090W'],
    )

    assert asset_id == GEE_GFW_PEATLANDS
    # Both blobs still got cleaned up — partial failure shouldn't leak GCS.
    assert mock_io.delete_gcs.call_count == 2


def test_submit_exception_still_cleans_up_orphan_blob(mock_io: Any) -> None:
    """A Phase B submission exception still cleans the staged GCS blob.

    The blob made it to GCS during Phase A but no task ID was issued, so
    the orchestrator records it in ``orphan_blobs`` for Phase D cleanup.
    """
    mock_io.asset_exists.return_value = False
    mock_io.submit.side_effect = RuntimeError('GEE 500 startIngestion')

    asset_id = gfw_peatlands.extract_gfw_peatlands(
        gcp_project='ws-dev',
        tile_ids=['40N_080W'],
    )
    assert asset_id == GEE_GFW_PEATLANDS

    mock_io.upload.assert_called_once()
    # Submission failed — no task IDs to poll.
    mock_io.poll.assert_not_called()
    # Blob still gets cleaned up via orphan_blobs.
    mock_io.delete_gcs.assert_called_once()


# ---------------------------------------------------------------------------
# Regression guard: no shapefile / vector libs in the import graph
# ---------------------------------------------------------------------------


def test_no_shapefile_or_shapely_imports() -> None:
    """No shapefile/shapely imports — this is a raster ingest, not a vector one."""
    import sys

    sys.modules.pop('jdluc.extract.gfw_peatlands', None)
    import jdluc.extract.gfw_peatlands as fresh  # noqa: F401

    assert 'shapefile' not in sys.modules  # pyshp
    assert 'shapely' not in sys.modules
    assert 'geopandas' not in sys.modules

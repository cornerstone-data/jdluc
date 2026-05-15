"""Integration tests for the fan-out shape of extract/harris_agb.py.

Marked ``@pytest.mark.integration`` — requires live GEE credentials and
write access to the GCS staging bucket. Single-tile coverage is enough
to validate the orchestrator structurally; CONUS-scale wall-time validation
is a separate manual verification step.

Tests are DESTRUCTIVE against the per-tile asset (delete + rebuild) but
leave the rest of the collection untouched. They run with ``force=True``
so a parent ``ImageCollection`` and unrelated tile siblings are preserved.
"""

import pytest

from jdluc.extract import _tile_pipeline, harris_agb
from jdluc.extract.harris_agb import (
    GCS_STAGING_PREFIX,
    extract_harris_agb,
)
from jdluc.utils.constants import (
    GCP_PROJECT,
    GCS_BUCKET_NAME,
    GEE_HARRIS_AGB,
)
from jdluc.utils.gee import (
    asset_exists,
    delete_asset_if_present,
    initialize_gee,
)

# 40N_080W is the Delaware-containing tile; smaller payload than the
# heavier 50N_* tiles to the north, so this is the cheapest single-tile
# round trip for validating orchestrator structure.
_TEST_TILE_ID: str = '40N_080W'


@pytest.fixture(scope='module')
def _ee_initialized() -> None:
    initialize_gee(GCP_PROJECT)


def _list_staging_blobs(run_id_prefix: str = '') -> list[str]:
    """Return the names of any GCS staging blobs under the harris_agb prefix.

    Used by the cleanup-leak check; ``run_id_prefix`` filters to a single
    run when set.
    """
    from google.cloud import storage

    client = storage.Client(project=GCP_PROJECT)
    prefix = (
        f'{GCS_STAGING_PREFIX}/{run_id_prefix}' if run_id_prefix else GCS_STAGING_PREFIX
    )
    return [b.name for b in client.list_blobs(GCS_BUCKET_NAME, prefix=prefix)]


@pytest.mark.integration
def test_harris_agb_single_tile_fan_out_round_trip(_ee_initialized: None) -> None:
    """Single-tile clean-cache extract round-trips through the fan-out path."""
    tile_asset_id = _tile_pipeline._tile_image_asset_id(GEE_HARRIS_AGB, _TEST_TILE_ID)

    # DESTRUCTIVE: tear down just this tile, leaving sibling tiles untouched.
    delete_asset_if_present(tile_asset_id)

    asset_id = extract_harris_agb(
        gcp_project=GCP_PROJECT,
        force=True,
        tile_ids=[_TEST_TILE_ID],
    )

    assert asset_id == GEE_HARRIS_AGB
    assert asset_exists(
        tile_asset_id
    ), f'fan-out extract did not materialize {tile_asset_id}'


@pytest.mark.integration
def test_harris_agb_no_gcs_staging_objects_left_behind(_ee_initialized: None) -> None:
    """After a clean-cache extract, the GCS staging prefix is empty.

    Phase D defers cleanup to end-of-run; this test asserts that nothing
    is left in the bucket after the prior round-trip test completes.
    Depends on the previous test having run and seeded the asset.
    """
    leftover = _list_staging_blobs()
    assert leftover == [], (
        f'GCS staging objects leaked under '
        f'gs://{GCS_BUCKET_NAME}/{GCS_STAGING_PREFIX}/: {leftover}'
    )


def test_harris_agb_integration_module_imports_cleanly() -> None:
    """Non-integration sanity check — catches import-time regressions."""
    assert callable(extract_harris_agb)
    assert _TEST_TILE_ID in harris_agb.CONUS_TILE_IDS

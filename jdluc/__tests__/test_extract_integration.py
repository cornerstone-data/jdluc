"""End-to-end integration test for the extract stage.

Marked ``@pytest.mark.integration`` — requires live GEE credentials and
write access to the GCS staging bucket. Only IPCC climate zones is
exercised end-to-end (it is the smallest of the four datasets, with
~18 KB source and sub-minute full round-trip). Harris / Huang / NASS
end-to-end runs are covered by the per-module unit tests with mocks
plus the BigQuery publish path and CONUS pipeline run as real-scale
validators.

The test is DESTRUCTIVE against the GEE IPCC asset (deletes + rebuilds
it). Do not run against production if anything else depends on the
asset being continuously available.
"""

from collections import Counter
from typing import Any

import ee
import pytest

from jdluc.extract.extract import extract_all
from jdluc.utils.constants import (
    GCP_PROJECT,
    GEE_IPCC_CLIMATE_ZONES,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    NON_NATIVE_DATASETS,
)
from jdluc.utils.gee import (
    asset_exists,
    delete_asset_if_present,
    initialize_gee,
)

# Delaware bounding box for the histogram snapshot. Kept small so the
# reducer runs in a handful of seconds.
_DELAWARE_BBOX: list[float] = [-75.8, 38.4, -74.9, 39.9]


def _ipcc_histogram_over_delaware() -> Counter[int]:
    """Return a {climate_zone_code: pixel_count} histogram over Delaware.

    Uses the GLAD_CRS_TRANSFORM so the sampling grid matches what
    transform/emissions.py actually reads.
    """
    image = ee.Image(GEE_IPCC_CLIMATE_ZONES)
    region = ee.Geometry.Rectangle(_DELAWARE_BBOX, proj=GLAD_CRS, geodesic=False)
    # frequencyHistogram returns {stringified_value: count}.
    result = image.reduceRegion(
        reducer=ee.Reducer.frequencyHistogram(),
        geometry=region,
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e9),
        bestEffort=True,
    ).getInfo()
    raw = next(iter(result.values())) if result else {}
    return Counter({int(k): int(v) for k, v in (raw or {}).items()})


@pytest.fixture(scope='module')
def _ee_initialized() -> None:
    initialize_gee(GCP_PROJECT)


@pytest.mark.integration
def test_ipcc_end_to_end_rebuild_matches_snapshot(_ee_initialized: None) -> None:
    """Delete + re-extract IPCC, confirm histogram matches pre-deletion."""
    if not asset_exists(GEE_IPCC_CLIMATE_ZONES):
        pytest.skip(
            f'No baseline {GEE_IPCC_CLIMATE_ZONES} asset to snapshot against. '
            f'Run the orchestrator once before the test to seed it.'
        )

    baseline = _ipcc_histogram_over_delaware()
    assert sum(baseline.values()) > 0, 'baseline histogram is empty'

    delete_asset_if_present(GEE_IPCC_CLIMATE_ZONES)

    result = extract_all(gcp_project=GCP_PROJECT, force=False)
    assert 'ipcc_climate_zones' in result.extracted, result
    assert result.failed == {}, result.failed
    assert asset_exists(GEE_IPCC_CLIMATE_ZONES)

    rebuilt = _ipcc_histogram_over_delaware()
    # Exact match — same source, same reprojection, categorical nearest-
    # neighbor resampling is deterministic.
    assert rebuilt == baseline, (rebuilt, baseline)


@pytest.mark.integration
def test_extract_all_is_idempotent_on_second_run(_ee_initialized: None) -> None:
    """Second force=False invocation finds every asset cached, does no work."""
    # Pre-condition: every expected asset should already exist from the
    # previous test (or a prior pipeline run).
    for family in NON_NATIVE_DATASETS:
        from jdluc.utils.constants import DATASET_INVENTORY

        aid = DATASET_INVENTORY[family]['gee_asset_id']
        if not asset_exists(aid):
            pytest.skip(f'precondition: asset missing for {family} ({aid})')

    result = extract_all(gcp_project=GCP_PROJECT, force=False)
    assert sorted(result.cached) == sorted(NON_NATIVE_DATASETS)
    assert result.extracted == []
    assert result.failed == {}


def test_extract_integration_test_module_imports_cleanly() -> None:
    """Trivial non-integration sanity check — catches import-time regressions
    in the integration test module so they surface in the fast CI pass
    rather than only when the integration suite runs.
    """
    # Guards the method under test exists on the public surface the
    # integration body depends on.
    assert callable(extract_all)
    assert callable(_ipcc_histogram_over_delaware)
    assert len(_DELAWARE_BBOX) == 4
    _: Any = ee  # Silence "imported but unused" for the integration branch.

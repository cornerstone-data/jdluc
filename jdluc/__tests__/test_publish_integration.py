"""End-to-end integration tests for the publish stage.

Marked ``@pytest.mark.integration`` — requires live GEE credentials,
write access to the BigQuery destination dataset (``jdluc``),
and a previously-completed transform-stage run for Delaware (so the
transitions / crops GEE table assets exist for the export to read).

Validates the four invariants the plan calls out:
1. End-to-end Delaware run lands both BQ tables with the right
   schemas and a coherent (transform_sha, publish_sha) pair.
2. Cache-hit re-run: second invocation reports both targets cached
   and queues no new BQ export tasks.
3. Publish-version bump produces a new pair of tables, leaves the
   original pair intact.

The legacy `test_aggregation_integration.py` / `test_export_integration.py` /
`test_bigquery_integration.py` covered the legacy aggregation / export /
bigquery modules; their behavior is now covered by
test_transform_*_integration.py (raster invariants), test_publish.py
(target dispatch), and the integration tests in this file.
"""

from typing import Any
from unittest.mock import patch

import pytest

from jdluc import pipeline
from jdluc.publish.bigquery import bq_table_exists
from jdluc.publish.publish import run_publish
from jdluc.utils.asset_management import (
    parse_compound_version_from_asset_id,
)
from jdluc.utils.constants import (
    BQ_CROPS_TABLE_PREFIX,
    BQ_TRANSITIONS_TABLE_PREFIX,
    CROPS_TABLE_COLUMNS,
    GCP_PROJECT,
    GEE_ASSET_ROOT,
    TRANSITIONS_TABLE_COLUMNS,
)
from jdluc.utils.gee import initialize_gee

# Columns that GEE's Export.table.toBigQuery always materializes alongside
# the FeatureCollection's actual properties: `geo` is the WKB-encoded
# geometry (a Point(0, 0) placeholder for tabular outputs); `system:index`
# is the per-feature ID from the source FC. Neither is meaningful for the
# transitions / crops tables, but they're unavoidable in the BQ schema.
_GEE_BQ_AUTO_COLUMNS: set[str] = {'geo', 'system:index'}


def _bq_table_columns(table_id: str) -> set[str]:
    """Return the column names of a BQ table, in any order."""
    from google.cloud import bigquery

    client = bigquery.Client(project=GCP_PROJECT)
    table = client.get_table(table_id)
    return {field.name for field in table.schema}


@pytest.fixture(scope='module')
def _ee_initialized() -> None:
    initialize_gee(GCP_PROJECT)


@pytest.mark.integration
def test_end_to_end_delaware_publishes_both_bq_tables(
    _ee_initialized: None,
) -> None:
    """run_pipeline lands two BQ tables, both with correct schemas, and
    both names parse back to the same (transform_sha, publish_sha) pair.
    """
    result = pipeline.run_pipeline(
        gcp_project=GCP_PROJECT,
        states=['10'],
        region_name='delaware',
        force=False,
    )

    assert result.publish_result is not None
    transitions_table = result.publish_result.transitions.table_id
    crops_table = result.publish_result.crops.table_id
    assert transitions_table is not None
    assert crops_table is not None
    assert bq_table_exists(transitions_table)
    assert bq_table_exists(crops_table)

    # Schemas carry the canonical transitions / crops columns. BQ also
    # auto-adds `geo` (the FeatureCollection geometry — a constant
    # Point(0, 0) placeholder) and `system:index` (the GEE feature ID)
    # when Export.table.toBigQuery materializes a FeatureCollection;
    # both are inert for analysis but unavoidable.
    transitions_cols = _bq_table_columns(transitions_table)
    assert set(TRANSITIONS_TABLE_COLUMNS).issubset(transitions_cols), (
        set(TRANSITIONS_TABLE_COLUMNS) - transitions_cols
    )
    assert _GEE_BQ_AUTO_COLUMNS.issubset(transitions_cols), transitions_cols
    crops_cols = _bq_table_columns(crops_table)
    assert set(CROPS_TABLE_COLUMNS).issubset(crops_cols), (
        set(CROPS_TABLE_COLUMNS) - crops_cols
    )
    assert _GEE_BQ_AUTO_COLUMNS.issubset(crops_cols), crops_cols

    # Both tables share the same (transform_sha, publish_sha) pair —
    # cross-table joins are safe.
    transitions_versions = parse_compound_version_from_asset_id(transitions_table)
    crops_versions = parse_compound_version_from_asset_id(crops_table)
    assert transitions_versions is not None
    assert transitions_versions == crops_versions

    # Sanity: transitions name carries the transitions prefix and crops
    # name carries the crops prefix.
    assert BQ_TRANSITIONS_TABLE_PREFIX in transitions_table
    assert BQ_CROPS_TABLE_PREFIX in crops_table


@pytest.mark.integration
def test_pipeline_re_run_is_fully_cached(_ee_initialized: None) -> None:
    """Second consecutive run with no code change reports from_cache=True
    at every stage and queues no new BQ exports."""
    result = pipeline.run_pipeline(
        gcp_project=GCP_PROJECT,
        states=['10'],
        region_name='delaware',
        force=False,
    )
    assert result.from_cache is True
    assert result.publish_result is not None
    assert result.publish_result.transitions.from_cache is True
    assert result.publish_result.crops.from_cache is True


@pytest.mark.integration
def test_publish_version_bump_produces_new_tables(_ee_initialized: None) -> None:
    """Monkey-patch compute_publish_version() to a synthetic SHA and
    invoke run_publish directly. New tables must materialize at the new
    name; the previous tables must still be intact."""
    from jdluc.publish import publish as publish_module
    from jdluc.utils.version import compute_transform_version

    transform_version = compute_transform_version()
    region = 'delaware'
    transitions_asset = f'{GEE_ASSET_ROOT}/transitions_{region}_{transform_version}'
    crops_asset = f'{GEE_ASSET_ROOT}/crops_{region}_{transform_version}'

    # Capture the "current code" run's table IDs first.
    baseline = run_publish(
        region_name=region,
        transitions_asset_id=transitions_asset,
        crops_asset_id=crops_asset,
        land_use_asset_id="...",
        emissions_asset_id="...",
        transform_version=transform_version,
        force=False,
    )

    bumped_publish_sha = 'deadbeef0000'
    with patch.object(
        publish_module, 'compute_publish_version', return_value=bumped_publish_sha
    ):
        bumped = run_publish(
            region_name=region,
            transitions_asset_id=transitions_asset,
            crops_asset_id=crops_asset,
            transform_version=transform_version,
            land_use_asset_id="...",
            emissions_asset_id="...",
            force=False,
        )

    # Distinct table IDs (synthetic publish SHA in the suffix).
    assert bumped.transitions.table_id != baseline.transitions.table_id
    assert bumped.crops.table_id != baseline.crops.table_id
    assert bumped.transitions.publish_version == bumped_publish_sha
    assert bumped.crops.publish_version == bumped_publish_sha
    # Both new tables exist, original tables still exist.
    assert bq_table_exists(bumped.transitions.table_id)
    assert bq_table_exists(bumped.crops.table_id)
    assert bq_table_exists(baseline.transitions.table_id)
    assert bq_table_exists(baseline.crops.table_id)


def test_publish_integration_module_imports_cleanly() -> None:
    """Trivial non-integration sanity check — keeps import-time
    regressions in this module surfacing in the fast CI pass."""
    assert callable(run_publish)
    assert callable(_bq_table_columns)
    _: Any = pytest  # silence unused-import in the integration-only path

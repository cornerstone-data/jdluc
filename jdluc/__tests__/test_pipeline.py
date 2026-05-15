"""Unit tests for pipeline.run_pipeline.

Mocks extract_all, run_transform, run_publish, initialize_gee so the
tests run without GEE / BQ credentials. Covers the happy-path
propagation across all three stages, the fail-fast contracts on extract
and publish failures, and the from_cache aggregation rule.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc import pipeline
from jdluc.extract.extract import ExtractError, ExtractResult
from jdluc.publish.publish import (
    BigQueryExportResult,
    GCSExportResult,
    PublishError,
    PublishResult,
)
from jdluc.utils.constants import (
    BQ_CROPS_TABLE_PREFIX,
    BQ_DATASET,
    BQ_PROJECT,
    BQ_TRANSITIONS_TABLE_PREFIX,
)

_TRANSITIONS_TABLE_ID = (
    f'{BQ_PROJECT}.{BQ_DATASET}.{BQ_TRANSITIONS_TABLE_PREFIX}_delaware_abc_def'
)
_CROPS_TABLE_ID = f'{BQ_PROJECT}.{BQ_DATASET}.{BQ_CROPS_TABLE_PREFIX}_delaware_abc_def'


def _fake_transform_result(**overrides: Any) -> MagicMock:
    tr = MagicMock()
    tr.version = 'abc123456789'
    tr.land_use_asset_id = 'projects/.../land_use_delaware_abc'
    tr.emissions_asset_id = 'projects/.../emissions_delaware_abc'
    tr.transitions_table_id = 'projects/.../transitions_delaware_abc'
    tr.crops_table_id = 'projects/.../crops_delaware_abc'
    tr.from_cache = False
    for key, value in overrides.items():
        setattr(tr, key, value)
    return tr


def _fake_publish_result(
    transitions_from_cache: bool = False,
    crops_from_cache: bool = False,
    transitions_error: str | None = None,
    crops_error: str | None = None,
) -> PublishResult:
    return PublishResult(
        transitions=BigQueryExportResult(
            table_id=_TRANSITIONS_TABLE_ID,
            transform_version='abc',
            publish_version='def',
            from_cache=transitions_from_cache,
            error=transitions_error,
        ),
        crops=BigQueryExportResult(
            table_id=_CROPS_TABLE_ID,
            transform_version='abc',
            publish_version='def',
            from_cache=crops_from_cache,
            error=crops_error,
        ),
        land_use=GCSExportResult(
            gcs_uri="...",
            transform_version="...",
            publish_version="...",
            from_cache=True,
            error=None,
        ),
        emissions=GCSExportResult(
            gcs_uri="...",
            transform_version="...",
            publish_version="...",
            from_cache=True,
            error=None,
        ),
        transitions_csv=GCSExportResult(
            gcs_uri="...",
            transform_version="...",
            publish_version="...",
            from_cache=True,
            error=None,
        ),
        crops_csv=GCSExportResult(
            gcs_uri="...",
            transform_version="...",
            publish_version="...",
            from_cache=True,
            error=None,
        ),
    )


def test_run_pipeline_propagates_all_three_stages() -> None:
    extract_result = ExtractResult(
        cached=['harris_agb', 'huang_bgb'],
        extracted=['ipcc_climate_zones', 'nass_yields'],
        failed={},
    )
    publish_result = _fake_publish_result()

    with (
        patch.object(pipeline, 'initialize_gee') as init_gee,
        patch.object(
            pipeline, 'extract_all', return_value=extract_result
        ) as extract_mock,
        patch.object(
            pipeline, 'run_transform', return_value=_fake_transform_result()
        ) as transform_mock,
        patch.object(
            pipeline, 'run_publish', return_value=publish_result
        ) as publish_mock,
    ):
        result = pipeline.run_pipeline(
            gcp_project='ws-dev', states=['10'], region_name='delaware'
        )

    init_gee.assert_called_once_with('ws-dev')
    extract_mock.assert_called_once_with(gcp_project='ws-dev', force=False)
    transform_mock.assert_called_once()
    publish_mock.assert_called_once()
    assert result.extract_result is extract_result
    assert result.publish_result is publish_result
    assert result.publish_result.transitions.table_id == _TRANSITIONS_TABLE_ID
    assert result.publish_result.crops.table_id == _CROPS_TABLE_ID
    assert result.land_use_asset_id == 'projects/.../land_use_delaware_abc'


def test_run_pipeline_force_propagates_through_all_stages() -> None:
    with (
        patch.object(pipeline, 'initialize_gee'),
        patch.object(
            pipeline, 'extract_all', return_value=ExtractResult(cached=['a'])
        ) as extract_mock,
        patch.object(
            pipeline, 'run_transform', return_value=_fake_transform_result()
        ) as transform_mock,
        patch.object(
            pipeline, 'run_publish', return_value=_fake_publish_result()
        ) as publish_mock,
    ):
        pipeline.run_pipeline(
            gcp_project='ws-dev',
            states=['10'],
            region_name='delaware',
            force=True,
        )

    assert extract_mock.call_args.kwargs['force'] is True
    assert transform_mock.call_args.kwargs['force'] is True
    assert publish_mock.call_args.kwargs['force'] is True


def test_run_pipeline_raises_extract_error_without_running_transform_or_publish() -> (
    None
):
    failing_result = ExtractResult(
        cached=['harris_agb'],
        extracted=[],
        failed={'nass_yields': 'HTTP 500'},
    )
    with (
        patch.object(pipeline, 'initialize_gee'),
        patch.object(pipeline, 'extract_all', return_value=failing_result),
        patch.object(pipeline, 'run_transform') as transform_mock,
        patch.object(pipeline, 'run_publish') as publish_mock,
    ):
        with pytest.raises(ExtractError, match='nass_yields'):
            pipeline.run_pipeline(
                gcp_project='ws-dev', states=['10'], region_name='delaware'
            )
    transform_mock.assert_not_called()
    publish_mock.assert_not_called()


def test_run_pipeline_propagates_publish_error_with_transform_completed() -> None:
    failing_publish = _fake_publish_result(transitions_error='BQ 403')
    with (
        patch.object(pipeline, 'initialize_gee'),
        patch.object(pipeline, 'extract_all', return_value=ExtractResult(cached=['a'])),
        patch.object(pipeline, 'run_transform', return_value=_fake_transform_result()),
        patch.object(
            pipeline, 'run_publish', side_effect=PublishError(failing_publish)
        ),
        pytest.raises(PublishError, match='transitions'),
    ):
        pipeline.run_pipeline(
            gcp_project='ws-dev', states=['10'], region_name='delaware'
        )


def test_from_cache_is_true_only_when_every_stage_was_cached() -> None:
    cases = [
        # (transform_from_cache, transitions_from_cache, crops_from_cache, expected)
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (False, False, False, False),
    ]
    for tf, ttc, tcc, expected in cases:
        with (
            patch.object(pipeline, 'initialize_gee'),
            patch.object(pipeline, 'extract_all', return_value=ExtractResult()),
            patch.object(
                pipeline,
                'run_transform',
                return_value=_fake_transform_result(from_cache=tf),
            ),
            patch.object(
                pipeline,
                'run_publish',
                return_value=_fake_publish_result(
                    transitions_from_cache=ttc, crops_from_cache=tcc
                ),
            ),
        ):
            result = pipeline.run_pipeline(
                gcp_project='ws-dev', states=['10'], region_name='delaware'
            )
        assert result.from_cache is expected, (tf, ttc, tcc, expected)


def test_run_pipeline_aborts_if_transform_did_not_produce_tables() -> None:
    # If run_transform somehow returns without populating
    # transitions_table_id or crops_table_id, we should raise rather
    # than feed None into run_publish.
    incomplete = _fake_transform_result(transitions_table_id=None)
    with (
        patch.object(pipeline, 'initialize_gee'),
        patch.object(pipeline, 'extract_all', return_value=ExtractResult()),
        patch.object(pipeline, 'run_transform', return_value=incomplete),
        patch.object(pipeline, 'run_publish') as publish_mock,
    ):
        with pytest.raises(RuntimeError, match='publish stage cannot proceed'):
            pipeline.run_pipeline(
                gcp_project='ws-dev', states=['10'], region_name='delaware'
            )
    publish_mock.assert_not_called()

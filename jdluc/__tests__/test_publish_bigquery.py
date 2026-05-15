"""Unit tests for ``publish/bigquery.py``.

Mocks ``google-cloud-bigquery`` + ``ee.batch.Export.table.toBigQuery``
so every path runs offline. Covers table-ID format invariance,
cache-hit skip, force bypass, and the per-table wrappers funneling
through the shared primitive.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.publish import bigquery as pub_bq
from jdluc.publish.publish import BigQueryExportResult
from jdluc.utils.constants import (
    BQ_CROPS_TABLE_PREFIX,
    BQ_DATASET,
    BQ_PROJECT,
    BQ_TRANSITIONS_TABLE_PREFIX,
)

_T_SHA = 'a0d76ac0aa12'
_P_SHA = 'f91c22ab77d8'
_DIRTY_T_SHA = 'a0d76ac0aa12-dirty-3f2b8c01'


# ---------- build_*_bq_table_id --------------------------------------------


def test_build_transitions_bq_table_id_format() -> None:
    table_id = pub_bq.build_transitions_bq_table_id('delaware', _T_SHA, _P_SHA)
    assert (
        table_id == f'{BQ_PROJECT}.{BQ_DATASET}.{BQ_TRANSITIONS_TABLE_PREFIX}'
        f'_delaware_{_T_SHA}_{_P_SHA}'
    )


def test_build_crops_bq_table_id_format() -> None:
    table_id = pub_bq.build_crops_bq_table_id('iowa', _T_SHA, _P_SHA)
    assert (
        table_id == f'{BQ_PROJECT}.{BQ_DATASET}.{BQ_CROPS_TABLE_PREFIX}'
        f'_iowa_{_T_SHA}_{_P_SHA}'
    )


def test_table_ids_survive_dirty_sha_components() -> None:
    # The compound parser has to handle a dirty transform SHA + clean
    # publish SHA at export time; table-id builder must produce a name
    # that parse_compound_version_from_asset_id can round-trip.
    from jdluc.utils.asset_management import (
        parse_compound_version_from_asset_id,
    )

    table_id = pub_bq.build_transitions_bq_table_id('delaware', _DIRTY_T_SHA, _P_SHA)
    parsed = parse_compound_version_from_asset_id(table_id)
    assert parsed == (_DIRTY_T_SHA, _P_SHA)


def test_prefixes_dont_overlap() -> None:
    transitions = pub_bq.build_transitions_bq_table_id('delaware', _T_SHA, _P_SHA)
    crops = pub_bq.build_crops_bq_table_id('delaware', _T_SHA, _P_SHA)
    assert transitions != crops
    # Same (region, t, p) triplet across both prefixes — only the prefix differs.
    assert (
        transitions.replace(BQ_TRANSITIONS_TABLE_PREFIX, BQ_CROPS_TABLE_PREFIX) == crops
    )


# ---------- bq_table_exists ------------------------------------------------


def _patch_bq_client() -> Any:
    return patch('google.cloud.bigquery.Client')


def test_bq_table_exists_true_when_get_table_succeeds() -> None:
    with _patch_bq_client() as mock_client:
        mock_client.return_value.get_table = MagicMock(return_value=MagicMock())
        assert pub_bq.bq_table_exists('p.d.t') is True


def test_bq_table_exists_false_on_notfound() -> None:
    from google.api_core import exceptions as gcs_exceptions

    with _patch_bq_client() as mock_client:
        mock_client.return_value.get_table.side_effect = gcs_exceptions.NotFound(
            'no such table'
        )
        assert pub_bq.bq_table_exists('p.d.t') is False


# ---------- ensure_bq_dataset_exists ---------------------------------------


def test_ensure_bq_dataset_exists_noop_when_dataset_present() -> None:
    with _patch_bq_client() as mock_client:
        mock_client.return_value.get_dataset = MagicMock(return_value=MagicMock())
        pub_bq.ensure_bq_dataset_exists()
    mock_client.return_value.create_dataset.assert_not_called()


def test_ensure_bq_dataset_exists_creates_when_absent() -> None:
    from google.api_core import exceptions as gcs_exceptions

    with _patch_bq_client() as mock_client:
        mock_client.return_value.get_dataset.side_effect = gcs_exceptions.NotFound(
            'no such dataset'
        )
        pub_bq.ensure_bq_dataset_exists()
    mock_client.return_value.create_dataset.assert_called_once()


def test_ensure_bq_dataset_exists_swallows_create_race_conflict() -> None:
    from google.api_core import exceptions as gcs_exceptions

    with _patch_bq_client() as mock_client:
        mock_client.return_value.get_dataset.side_effect = gcs_exceptions.NotFound(
            'no such dataset'
        )
        mock_client.return_value.create_dataset.side_effect = gcs_exceptions.Conflict(
            'someone else already created it'
        )
        # Should not raise — race with parallel run is treated as success.
        pub_bq.ensure_bq_dataset_exists()


# ---------- export_feature_collection_to_bigquery --------------------------


def _valid_table_id(prefix: str = BQ_TRANSITIONS_TABLE_PREFIX) -> str:
    return f'{BQ_PROJECT}.{BQ_DATASET}.{prefix}_delaware_{_T_SHA}_{_P_SHA}'


def test_export_primitive_rejects_table_id_without_compound_tail() -> None:
    with pytest.raises(ValueError, match='compound'):
        pub_bq.export_feature_collection_to_bigquery(
            'projects/.../transitions_delaware_abc',
            'not-a-compound-id',
        )


def test_export_primitive_cache_hit_skips_export() -> None:
    table_id = _valid_table_id()
    with (
        patch.object(pub_bq, 'bq_table_exists', return_value=True) as exists,
        patch.object(pub_bq, 'ensure_bq_dataset_exists') as ensure_dataset,
        patch('jdluc.publish.bigquery.ee.batch.Export.table.toBigQuery') as export,
        patch.object(pub_bq, 'wait_for_export_task') as wait,
    ):
        result = pub_bq.export_feature_collection_to_bigquery(
            'projects/.../transitions_delaware_abc',
            table_id,
            force=False,
        )

    exists.assert_called_once_with(table_id)
    # Cache hit returns before dataset existence check — avoids a needless
    # round-trip on the warm path.
    ensure_dataset.assert_not_called()
    export.assert_not_called()
    wait.assert_not_called()
    assert result.from_cache is True
    assert result.transform_version == _T_SHA
    assert result.publish_version == _P_SHA
    assert result.error is None


def test_export_primitive_force_bypasses_cache() -> None:
    table_id = _valid_table_id()
    fake_task = MagicMock()
    with (
        patch.object(pub_bq, 'bq_table_exists', return_value=True),
        patch.object(pub_bq, 'ensure_bq_dataset_exists') as ensure_dataset,
        patch(
            'jdluc.publish.bigquery.ee.batch.Export.table.toBigQuery',
            return_value=fake_task,
        ) as export,
        patch.object(pub_bq, 'wait_for_export_task') as wait,
        patch(
            'jdluc.publish.bigquery.ee.FeatureCollection',
            return_value='fc',
        ),
    ):
        result = pub_bq.export_feature_collection_to_bigquery(
            'projects/.../transitions_delaware_abc',
            table_id,
            force=True,
        )

    ensure_dataset.assert_called_once()
    export.assert_called_once()
    export_kwargs = export.call_args.kwargs
    assert export_kwargs['table'] == table_id
    assert export_kwargs['overwrite'] is True
    assert export_kwargs['append'] is False
    fake_task.start.assert_called_once()
    wait.assert_called_once()
    assert result.from_cache is False
    assert result.error is None


def test_export_primitive_cache_miss_runs_export() -> None:
    table_id = _valid_table_id()
    fake_task = MagicMock()
    with (
        patch.object(pub_bq, 'bq_table_exists', return_value=False),
        patch.object(pub_bq, 'ensure_bq_dataset_exists') as ensure_dataset,
        patch(
            'jdluc.publish.bigquery.ee.batch.Export.table.toBigQuery',
            return_value=fake_task,
        ),
        patch.object(pub_bq, 'wait_for_export_task') as wait,
        patch(
            'jdluc.publish.bigquery.ee.FeatureCollection',
            return_value='fc',
        ),
    ):
        result = pub_bq.export_feature_collection_to_bigquery(
            'projects/.../transitions_delaware_abc',
            table_id,
            force=False,
        )

    ensure_dataset.assert_called_once()
    fake_task.start.assert_called_once()
    wait.assert_called_once()
    assert result.from_cache is False
    assert result.error is None


def test_export_primitive_captures_task_failure_as_error_field() -> None:
    table_id = _valid_table_id()
    fake_task = MagicMock()
    with (
        patch.object(pub_bq, 'bq_table_exists', return_value=False),
        patch.object(pub_bq, 'ensure_bq_dataset_exists'),
        patch(
            'jdluc.publish.bigquery.ee.batch.Export.table.toBigQuery',
            return_value=fake_task,
        ),
        patch.object(pub_bq, 'wait_for_export_task', side_effect=RuntimeError('boom')),
        patch(
            'jdluc.publish.bigquery.ee.FeatureCollection',
            return_value='fc',
        ),
    ):
        result = pub_bq.export_feature_collection_to_bigquery(
            'projects/.../transitions_delaware_abc',
            table_id,
            force=False,
        )

    assert result.from_cache is False
    assert result.error == 'boom'
    assert result.transform_version == _T_SHA
    assert result.publish_version == _P_SHA


# ---------- Per-table wrappers funnel through the primitive ----------------


@pytest.mark.parametrize(
    'wrapper, prefix',
    [
        (pub_bq.export_transitions_to_bigquery, BQ_TRANSITIONS_TABLE_PREFIX),
        (pub_bq.export_crops_to_bigquery, BQ_CROPS_TABLE_PREFIX),
    ],
)
def test_per_table_wrappers_delegate_to_shared_primitive(
    wrapper: Callable[..., BigQueryExportResult], prefix: str
) -> None:
    table_id = _valid_table_id(prefix=prefix)
    expected = BigQueryExportResult(
        table_id=table_id,
        transform_version=_T_SHA,
        publish_version=_P_SHA,
        from_cache=True,
    )
    with patch.object(
        pub_bq, 'export_feature_collection_to_bigquery', return_value=expected
    ) as primitive:
        result = wrapper('projects/.../asset', table_id, force=True)

    primitive.assert_called_once_with('projects/.../asset', table_id, force=True)
    assert result is expected

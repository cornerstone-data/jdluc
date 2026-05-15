"""Unit tests for transform/transform.py (no GEE credentials required).

The four-task integration is covered by the live-GEE test
test_transform_multistate_integration.py.
"""

from contextlib import ExitStack
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from jdluc.transform import transform as transform_module
from jdluc.transform.transform import (
    TransformError,
    TransformResult,
    TransformStage,
    _load_fips_band_for_states,
    _validate_inputs,
    run_transform,
)

# ---------------------------------------------------------------------------
# TransformError shape
# ---------------------------------------------------------------------------


def test_transform_error_carries_stage_and_message() -> None:
    err = TransformError(TransformStage.LAND_USE.value, 'Export failed: Internal error')
    assert err.failed_stage == 'LAND_USE'
    assert err.error_message == 'Export failed: Internal error'
    assert 'LAND_USE' in str(err)
    assert 'Export failed: Internal error' in str(err)


def test_transform_error_stages_match_enum() -> None:
    """Every TransformStage value can be embedded in a TransformError."""
    for stage in TransformStage:
        err = TransformError(stage.value, 'oops')
        assert err.failed_stage == stage.value


def test_transform_stage_enum_values() -> None:
    """Public stage labels match the four-task design."""
    assert {s.name for s in TransformStage} == {'LAND_USE', 'EMISSIONS', 'TABLES'}


# ---------------------------------------------------------------------------
# _validate_inputs
# ---------------------------------------------------------------------------


def test_validate_inputs_rejects_empty_states() -> None:
    with pytest.raises(ValueError, match='states must not be empty'):
        _validate_inputs([], 'delaware')


def test_validate_inputs_rejects_empty_region_name() -> None:
    with pytest.raises(ValueError, match='region_name must not be empty'):
        _validate_inputs(['10'], '')


def test_validate_inputs_rejects_unknown_fips() -> None:
    with pytest.raises(ValueError, match='Unknown state FIPS codes'):
        _validate_inputs(['10', '99'], 'test')


def test_validate_inputs_excludes_alaska_and_hawaii() -> None:
    # Alaska (02) and Hawaii (15) are explicitly not in STATE_FIPS_TO_NAME.
    with pytest.raises(ValueError, match='Unknown state FIPS codes'):
        _validate_inputs(['02'], 'alaska')
    with pytest.raises(ValueError, match='Unknown state FIPS codes'):
        _validate_inputs(['15'], 'hawaii')


def test_validate_inputs_accepts_valid_conus_fips() -> None:
    _validate_inputs(['10', '19', '46'], 'test_region')


# ---------------------------------------------------------------------------
# run_transform shape (mocked GEE)
# ---------------------------------------------------------------------------
# The four-task orchestration is well-tested at integration level. These
# unit tests cover the shape of the public API: what `run_transform`
# returns, that `from_cache=True` falls out of all-cached runs, and that
# stage failures wrap into TransformError with the right stage label.


def _enter_baseline_patches(stack: ExitStack, **overrides: dict[str, object]) -> None:
    """Enter the standard mock-everything patches on `stack`.

    Default patches make `run_transform` bypass GEE entirely: geometry +
    counties resolve to MagicMocks, build/export functions return MagicMock
    images / `None` (cache hit), and `compute_region_tables` returns a
    fully-cached result. Override individual patches via `overrides`
    keyword args, where the key is the attribute name relative to
    `jdluc.transform.transform`.
    """
    fake_tables = {
        'transitions_asset_id': 'projects/p/transitions',
        'crops_asset_id': 'projects/p/crops',
        'transitions_task': None,
        'crops_task': None,
    }
    defaults: dict[str, dict[str, object]] = {
        'get_multi_state_boundary': {'return_value': MagicMock(name='region_geometry')},
        '_load_fips_band_for_states': {'return_value': MagicMock(name='fips_band')},
        'build_land_use_image': {'return_value': MagicMock()},
        'export_land_use_asset': {'return_value': None},
        'build_emissions_image': {'return_value': MagicMock()},
        'export_emissions_asset': {'return_value': None},
        'compute_region_tables': {'return_value': fake_tables},
    }
    for attr, kwargs in defaults.items():
        merged = {**kwargs, **overrides.get(attr, {})}
        # ``patch()`` overloads enumerate specific kwargs (return_value,
        # side_effect, …) rather than accepting an arbitrary dict-splat,
        # so we cast through Any to satisfy the call site.
        stack.enter_context(
            patch(f'jdluc.transform.transform.{attr}', **cast(dict[str, Any], merged))
        )


def test_run_transform_all_cache_hit_yields_from_cache_true() -> None:
    """When every export is a cache hit, result.from_cache=True."""
    with ExitStack() as stack:
        _enter_baseline_patches(stack)
        result = run_transform(
            gcp_project='test',
            states=['10'],
            region_name='delaware',
            force=False,
        )
    assert isinstance(result, TransformResult)
    assert result.from_cache is True
    assert result.region_name == 'delaware'
    assert result.transitions_table_id == 'projects/p/transitions'
    assert result.crops_table_id == 'projects/p/crops'


def test_run_transform_wraps_land_use_failure_in_transform_error() -> None:
    """Build failure on the LAND_USE stage raises TransformError(LAND_USE, ...)."""
    with ExitStack() as stack:
        _enter_baseline_patches(
            stack,
            build_land_use_image={
                'side_effect': RuntimeError('synthetic land_use failure'),
                'return_value': None,
            },
        )
        with pytest.raises(TransformError) as exc_info:
            run_transform(
                gcp_project='test',
                states=['10'],
                region_name='delaware',
                force=False,
            )
    assert exc_info.value.failed_stage == TransformStage.LAND_USE.value
    assert 'synthetic land_use failure' in exc_info.value.error_message


def test_run_transform_wraps_tables_failure_in_transform_error() -> None:
    """compute_region_tables failure raises TransformError(TABLES, ...)."""
    with ExitStack() as stack:
        _enter_baseline_patches(
            stack,
            compute_region_tables={
                'side_effect': RuntimeError('synthetic tables failure'),
                'return_value': None,
            },
        )
        with pytest.raises(TransformError) as exc_info:
            run_transform(
                gcp_project='test',
                states=['10'],
                region_name='delaware',
                force=False,
            )
    assert exc_info.value.failed_stage == TransformStage.TABLES.value
    assert 'synthetic tables failure' in exc_info.value.error_message


# ---------------------------------------------------------------------------
# _load_fips_mask_for_states
# ---------------------------------------------------------------------------


def test_load_fips_band_single_state_filters_to_that_state_code() -> None:
    """For one state, band = fips_label.updateMask(state_band == that_code)."""
    with patch.object(transform_module, 'ee') as ee_mock:
        fips_label = MagicMock(name='fips_label')
        divided = MagicMock(name='divided')
        state_band = MagicMock(name='state_band')
        match_image = MagicMock(name='eq_match')
        masked = MagicMock(name='masked')
        renamed = MagicMock(name='renamed')
        ee_mock.Image.return_value = fips_label
        fips_label.divide.return_value = divided
        divided.toInt.return_value = state_band
        state_band.eq.return_value = match_image
        fips_label.updateMask.return_value = masked
        masked.rename.return_value = renamed

        result = _load_fips_band_for_states(['10'])

    assert result is renamed
    fips_label.divide.assert_called_once_with(1000)
    divided.toInt.assert_called_once_with()
    state_band.eq.assert_called_once_with(10)
    # No .Or() on the eq match for the single-state case.
    match_image.Or.assert_not_called()
    fips_label.updateMask.assert_called_once_with(match_image)
    masked.rename.assert_called_once_with('county_fips')


def test_load_fips_band_multiple_states_chains_or_per_state_code() -> None:
    """For N states, each subsequent state extends the mask via .Or()."""
    with patch.object(transform_module, 'ee') as ee_mock:
        fips_label = MagicMock(name='fips_label')
        divided = MagicMock(name='divided')
        state_band = MagicMock(name='state_band')
        match_19 = MagicMock(name='eq_19')
        match_31 = MagicMock(name='eq_31')
        match_46 = MagicMock(name='eq_46')
        or_19_31 = MagicMock(name='or_19_31')
        or_19_31_46 = MagicMock(name='or_19_31_46')
        masked = MagicMock(name='masked')
        renamed = MagicMock(name='renamed')

        ee_mock.Image.return_value = fips_label
        fips_label.divide.return_value = divided
        divided.toInt.return_value = state_band
        state_band.eq.side_effect = [match_19, match_31, match_46]
        match_19.Or.return_value = or_19_31
        or_19_31.Or.return_value = or_19_31_46
        fips_label.updateMask.return_value = masked
        masked.rename.return_value = renamed

        result = _load_fips_band_for_states(['19', '31', '46'])

    assert result is renamed
    eq_calls = [c.args[0] for c in state_band.eq.call_args_list]
    assert eq_calls == [19, 31, 46]
    # First state's match is or'd with the second, then the third.
    match_19.Or.assert_called_once_with(match_31)
    or_19_31.Or.assert_called_once_with(match_46)
    fips_label.updateMask.assert_called_once_with(or_19_31_46)


def test_load_fips_band_uses_canonical_county_fips_asset() -> None:
    """The asset path is the canonical GEE_COUNTY_FIPS_LABEL constant."""
    from jdluc.utils.constants import GEE_COUNTY_FIPS_LABEL

    with patch.object(transform_module, 'ee') as ee_mock:
        # Set up a minimal chain so the function returns without error.
        fips_label = MagicMock()
        ee_mock.Image.return_value = fips_label
        fips_label.divide.return_value.toInt.return_value.eq.return_value = MagicMock()
        fips_label.updateMask.return_value.rename.return_value = MagicMock()

        _load_fips_band_for_states(['19'])

    ee_mock.Image.assert_called_once_with(GEE_COUNTY_FIPS_LABEL)

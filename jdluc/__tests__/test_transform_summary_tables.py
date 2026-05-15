"""Unit tests for transform/summary_tables.py (no GEE credentials required).

Integration tests that hit live GEE live in
test_transform_summary_tables_integration.py.
"""

import math
from unittest.mock import MagicMock, patch

from jdluc.transform import summary_tables as st
from jdluc.transform.summary_tables import (
    _FOREST_ENCODED_CODES,
    _METRIC_BAND_NAMES,
    _SHORT_VEG_ENCODED_CODES,
    _filter_average_convert_nass_rows,
    _nass_yield_dict_from_features,
)
from jdluc.utils.constants import (
    BUSHEL_WEIGHT_KG,
    CROP_DRIVER_MAPPING,
    EMISSIONS_TYPE_NAMES,
    EMISSIVE_ENCODED_CODES,
    HA_PER_ACRE,
    LAND_USE_CATEGORIES,
    NASS_YIELD_YEARS,
)
from jdluc.utils.transitions import encode_transition


def _raw_row(
    state: str,
    commodity: str,
    year: int,
    bu_per_acre: float,
    *,
    agg_level: str = 'STATE',
    unit_desc: str = 'BU / ACRE',
) -> dict[str, object]:
    return {
        'state_fips': state,
        'state_name': {'10': 'DELAWARE', '19': 'IOWA', '20': 'KANSAS'}.get(
            state, state
        ),
        'commodity_desc': commodity,
        'year': year,
        'value_bu_per_acre': bu_per_acre,
        'agg_level_desc': agg_level,
        'unit_desc': unit_desc,
    }


def _features() -> list[dict[str, object]]:
    """Synthetic raw-row NASS fixture: 3 states × 3 commodities × 4 years."""
    # Per-state × per-commodity arithmetic mean reduces to one of the values
    # below. 4-year series per (state, commodity) keeps the mean simple.
    _values: dict[tuple[str, str], list[float]] = {
        ('10', 'CORN'): [170.0, 180.0, 185.0, 185.0],
        ('10', 'SOYBEANS'): [48.0, 50.0, 51.0, 51.0],
        ('10', 'WHEAT'): [68.0, 70.0, 71.0, 71.0],
        ('19', 'CORN'): [195.0, 200.0, 202.0, 203.0],
        ('19', 'SOYBEANS'): [58.0, 60.0, 61.0, 61.0],
        ('19', 'WHEAT'): [53.0, 55.0, 56.0, 56.0],
        ('20', 'CORN'): [125.0, 130.0, 132.0, 133.0],
        ('20', 'SOYBEANS'): [38.0, 40.0, 41.0, 41.0],
        ('20', 'WHEAT'): [46.0, 48.0, 49.0, 49.0],
    }
    rows: list[dict[str, object]] = []
    for (state, commodity), values in _values.items():
        for year, value in zip(NASS_YIELD_YEARS, values, strict=True):
            rows.append(_raw_row(state, commodity, year, value))
    return rows


def test_key_format() -> None:
    result = _nass_yield_dict_from_features(_features())
    for key in result:
        state, crop_code = key.split('|')
        assert len(state) == 2 and state.isdigit()
        assert crop_code.isdigit()


def test_wheat_fans_out_to_three_cdl_codes() -> None:
    result = _nass_yield_dict_from_features(_features())
    # Delaware wheat has four fixture rows (68, 70, 71, 71); mean = 70.0.
    # All three CDL codes share the same yield after fan-out.
    de_wheat = {code: result[f'10|{code}'] for code in (22, 23, 24)}
    assert de_wheat[22] == de_wheat[23] == de_wheat[24]
    assert math.isclose(de_wheat[22]['yield_bu_per_acre'], 70.0)


def test_corn_and_soybeans_single_code_each() -> None:
    result = _nass_yield_dict_from_features(_features())
    assert '10|1' in result  # corn
    assert '10|5' in result  # soybeans
    # No spurious codes for corn / soybeans.
    assert '10|2' not in result
    assert '10|6' not in result


def test_key_cardinality() -> None:
    # 3 states × (1 corn + 1 soy + 3 wheat codes) = 15 keys.
    result = _nass_yield_dict_from_features(_features())
    assert len(result) == 15


def test_yield_conversion_matches_methodology_example() -> None:
    # Corn at 180 bu/acre → 180 × BUSHEL_WEIGHT_KG[CORN] / HA_PER_ACRE ≈ 11300 kg/ha.
    # Use a 4-year fixture whose mean is exactly 180 bu/acre so we can assert
    # the conversion in both units.
    rows = [_raw_row('10', 'CORN', year, 180.0) for year in NASS_YIELD_YEARS]
    out = _nass_yield_dict_from_features(rows)
    assert math.isclose(out['10|1']['yield_bu_per_acre'], 180.0)
    expected_kg = 180.0 * BUSHEL_WEIGHT_KG['CORN'] / HA_PER_ACRE
    assert math.isclose(out['10|1']['yield_kg_per_ha'], expected_kg, rel_tol=1e-9)


def test_out_of_window_years_are_dropped() -> None:
    rows = [
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0] - 10, 1000.0),
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 180.0),
    ]
    averaged = _filter_average_convert_nass_rows(rows)
    # Mean reflects only the in-window row — the 1000 bu/acre outlier is gone.
    assert math.isclose(averaged[('10', 'CORN')]['yield_bu_per_acre'], 180.0)


def test_non_state_agg_level_is_dropped() -> None:
    rows = [
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 999.0, agg_level='COUNTY'),
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 180.0),
    ]
    averaged = _filter_average_convert_nass_rows(rows)
    assert math.isclose(averaged[('10', 'CORN')]['yield_bu_per_acre'], 180.0)


def test_non_bu_acre_unit_is_dropped() -> None:
    rows = [
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 999.0, unit_desc='LB / ACRE'),
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 180.0),
    ]
    averaged = _filter_average_convert_nass_rows(rows)
    assert math.isclose(averaged[('10', 'CORN')]['yield_bu_per_acre'], 180.0)


def test_unknown_commodity_is_dropped_silently() -> None:
    rows = [
        _raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 180.0),
        _raw_row('10', 'BARLEY', NASS_YIELD_YEARS[0], 80.0),
    ]
    averaged = _filter_average_convert_nass_rows(rows)
    assert ('10', 'BARLEY') not in averaged
    assert ('10', 'CORN') in averaged


def test_missing_state_commodity_pair_is_absent() -> None:
    # Delaware corn only; soybeans / wheat rows for state '10' should not
    # appear in the fan-out result.
    rows = [_raw_row('10', 'CORN', NASS_YIELD_YEARS[0], 180.0)]
    out = _nass_yield_dict_from_features(rows)
    assert '10|1' in out
    assert '10|5' not in out  # soybeans
    assert '10|22' not in out  # wheat


def test_empty_features_yield_empty_dict() -> None:
    assert _nass_yield_dict_from_features([]) == {}


# ---------------------------------------------------------------------------
# Crops-table precomputed driver-pair encoded codes
# ---------------------------------------------------------------------------
# `_FOREST_ENCODED_CODES` and `_SHORT_VEG_ENCODED_CODES` partition
# `EMISSIVE_ENCODED_CODES` by source family per `CROP_DRIVER_MAPPING`. They're
# derived once at module load — these tests guard against a future
# CROP_DRIVER_MAPPING / EMISSIONS_TYPE_NAMES drift breaking the partition
# silently.


def test_metric_band_names_has_canonical_10_entries() -> None:
    """Every band the crops reducer sums via `repeat(10)` is in the canonical list."""
    assert len(_METRIC_BAND_NAMES) == 10
    assert _METRIC_BAND_NAMES == (
        'area_total',
        'area_peatland',
        'allocated_forest',
        'allocated_short_veg',
        'allocated_peatland_conversion',
        'allocated_peatland_occupation',
        'allocated_2005',
        'allocated_2010',
        'allocated_2015',
        'allocated_2020',
    )


def _expected_codes_for_driver(target_col: str) -> set[int]:
    """Independent re-derivation of forest / short_veg encoded codes."""
    return {
        EMISSIVE_ENCODED_CODES[i]
        for i in range(len(EMISSIVE_ENCODED_CODES))
        if CROP_DRIVER_MAPPING[EMISSIONS_TYPE_NAMES[i]] == target_col
    }


def test_forest_encoded_codes_match_crop_driver_mapping() -> None:
    """`_FOREST_ENCODED_CODES` = encoded codes for forest+wetland_forest sources."""
    assert set(_FOREST_ENCODED_CODES) == _expected_codes_for_driver('pct_forest')
    # Sanity: forest_to_cropland (forest=1, cropland=5) must be present.
    forest_code = LAND_USE_CATEGORIES['forest'].code
    cropland_code = LAND_USE_CATEGORIES['cropland'].code
    assert encode_transition(forest_code, cropland_code) in _FOREST_ENCODED_CODES


def test_short_veg_encoded_codes_match_crop_driver_mapping() -> None:
    """`_SHORT_VEG_ENCODED_CODES` = encoded codes for short_veg+wetland_short_veg sources."""
    assert set(_SHORT_VEG_ENCODED_CODES) == _expected_codes_for_driver('pct_short_veg')
    short_veg_code = LAND_USE_CATEGORIES['short_vegetation'].code
    cropland_code = LAND_USE_CATEGORIES['cropland'].code
    assert encode_transition(short_veg_code, cropland_code) in _SHORT_VEG_ENCODED_CODES


def test_forest_and_short_veg_encoded_codes_partition_emissive_vocab() -> None:
    """Together they cover every entry of EMISSIVE_ENCODED_CODES, with no overlap."""
    forest_set = set(_FOREST_ENCODED_CODES)
    short_veg_set = set(_SHORT_VEG_ENCODED_CODES)
    assert forest_set.isdisjoint(short_veg_set)
    assert forest_set | short_veg_set == set(EMISSIVE_ENCODED_CODES)


# ---------------------------------------------------------------------------
# compute_region_tables cache-check ordering
# ---------------------------------------------------------------------------
# The build_transitions_table / build_crops_table calls trigger eager
# server-side ``value:compute`` RPCs (NASS yield join, county
# reductions). Probing asset existence first turns a cached region into
# a near-instant skip instead of paying that graph-construction cost.


def _fake_fips_band() -> MagicMock:
    return MagicMock(name='fips_band')


def _fake_region_bbox() -> MagicMock:
    return MagicMock(name='region_bbox')


def test_compute_region_tables_short_circuits_when_both_cached() -> None:
    with (
        patch.object(st, 'asset_exists', return_value=True),
        patch.object(st, 'build_transitions_table') as build_t,
        patch.object(st, 'build_crops_table') as build_c,
        patch.object(st, 'export_table_asset') as export,
    ):
        result = st.compute_region_tables(
            land_use_asset_id='projects/p/assets/land_use_iowa_abc',
            emissions_asset_id='projects/p/assets/emissions_iowa_abc',
            region='iowa',
            version='abc',
            fips_band=_fake_fips_band(),
            region_bbox=_fake_region_bbox(),
        )

    build_t.assert_not_called()
    build_c.assert_not_called()
    export.assert_not_called()
    assert result['transitions_task'] is None
    assert result['crops_task'] is None


def test_compute_region_tables_builds_only_missing_side_when_one_cached() -> None:
    def _fake_exists(asset_id: str) -> bool:
        return 'transitions_' in asset_id

    fake_export_task = MagicMock()
    with (
        patch.object(st, 'asset_exists', side_effect=_fake_exists),
        patch.object(st, 'build_transitions_table') as build_t,
        patch.object(st, 'build_crops_table', return_value=MagicMock()) as build_c,
        patch.object(st, 'export_table_asset', return_value=fake_export_task) as export,
        patch('jdluc.transform.summary_tables.ee.Image'),
    ):
        result = st.compute_region_tables(
            land_use_asset_id='projects/p/assets/land_use_iowa_abc',
            emissions_asset_id='projects/p/assets/emissions_iowa_abc',
            region='iowa',
            version='abc',
            fips_band=_fake_fips_band(),
            region_bbox=_fake_region_bbox(),
        )

    build_t.assert_not_called()
    build_c.assert_called_once()
    export.assert_called_once()
    assert result['transitions_task'] is None
    assert result['crops_task'] is fake_export_task


def test_compute_region_tables_builds_both_when_neither_cached() -> None:
    fake_task = MagicMock()
    with (
        patch.object(st, 'asset_exists', return_value=False),
        patch.object(st, 'build_transitions_table', return_value=MagicMock()) as bt,
        patch.object(st, 'build_crops_table', return_value=MagicMock()) as bc,
        patch.object(st, 'export_table_asset', return_value=fake_task) as export,
        patch('jdluc.transform.summary_tables.ee.Image'),
    ):
        st.compute_region_tables(
            land_use_asset_id='projects/p/assets/land_use_iowa_abc',
            emissions_asset_id='projects/p/assets/emissions_iowa_abc',
            region='iowa',
            version='abc',
            fips_band=_fake_fips_band(),
            region_bbox=_fake_region_bbox(),
        )

    bt.assert_called_once()
    bc.assert_called_once()
    assert export.call_count == 2


def test_compute_region_tables_force_rebuilds_even_when_cached() -> None:
    fake_task = MagicMock()
    with (
        patch.object(st, 'asset_exists', return_value=True) as asset_exists_mock,
        patch.object(st, 'build_transitions_table', return_value=MagicMock()) as bt,
        patch.object(st, 'build_crops_table', return_value=MagicMock()) as bc,
        patch.object(st, 'export_table_asset', return_value=fake_task) as export,
        patch('jdluc.transform.summary_tables.ee.Image'),
    ):
        st.compute_region_tables(
            land_use_asset_id='projects/p/assets/land_use_iowa_abc',
            emissions_asset_id='projects/p/assets/emissions_iowa_abc',
            region='iowa',
            version='abc',
            fips_band=_fake_fips_band(),
            region_bbox=_fake_region_bbox(),
            force=True,
        )

    asset_exists_mock.assert_not_called()
    bt.assert_called_once()
    bc.assert_called_once()
    assert export.call_count == 2

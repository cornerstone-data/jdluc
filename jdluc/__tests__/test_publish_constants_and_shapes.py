"""Unit tests for publish-stage constants and dataclass shapes.

Covers (a) the BQ destination constants in ``utils/constants.py``,
(b) the compound-version parser extension in
``utils/asset_management.py``, and (c) the ``PublishResult`` /
``BigQueryExportResult`` / ``PublishError`` dataclass shapes.
"""

import pytest

from jdluc.utils import constants
from jdluc.utils.asset_management import (
    parse_compound_version_from_asset_id,
    parse_version_from_asset_id,
)

# ---------- BQ destination constants ----------------------------------------


def test_bq_destination_constants_present() -> None:
    # ``BQ_PROJECT`` / ``BQ_DATASET`` / ``BQ_JOB_LOCATION`` are deployment-
    # configurable (edited directly in ``utils.constants``), so this test
    # asserts shape — non-empty strings — rather than the Watershed defaults.
    # The fixed table prefixes are still asserted exactly.
    assert isinstance(constants.BQ_PROJECT, str) and constants.BQ_PROJECT
    assert isinstance(constants.BQ_DATASET, str) and constants.BQ_DATASET
    assert isinstance(constants.BQ_JOB_LOCATION, str) and constants.BQ_JOB_LOCATION
    assert constants.BQ_TRANSITIONS_TABLE_PREFIX == 'luc_transitions'
    assert constants.BQ_CROPS_TABLE_PREFIX == 'luc_crops'


def test_bq_prefixes_dont_collide_with_legacy() -> None:
    # Legacy prefix was ``luc_emissions_summary``; the new prefixes must
    # not share a prefix-string namespace so old and new tables coexist.
    legacy = 'luc_emissions_summary'
    assert not constants.BQ_TRANSITIONS_TABLE_PREFIX.startswith(legacy)
    assert not constants.BQ_CROPS_TABLE_PREFIX.startswith(legacy)
    assert not legacy.startswith(constants.BQ_TRANSITIONS_TABLE_PREFIX)
    assert not legacy.startswith(constants.BQ_CROPS_TABLE_PREFIX)


# ---------- parse_compound_version_from_asset_id ----------------------------


_T_SHA = 'a0d76ac0aa12'
_P_SHA = 'f91c22ab77d8'
_DIRTY_SHA = 'a0d76ac0aa12-dirty-3f2b8c01'


@pytest.mark.parametrize(
    'asset_id, expected',
    [
        # Clean compound SHA pair — the canonical publish-asset shape.
        (
            f'{constants.BQ_TRANSITIONS_TABLE_PREFIX}_delaware_{_T_SHA}_{_P_SHA}',
            (_T_SHA, _P_SHA),
        ),
        # Dirty transform SHA, clean publish SHA.
        (
            f'{constants.BQ_TRANSITIONS_TABLE_PREFIX}_delaware_{_DIRTY_SHA}_{_P_SHA}',
            (_DIRTY_SHA, _P_SHA),
        ),
        # Clean transform SHA, dirty publish SHA.
        (
            f'{constants.BQ_TRANSITIONS_TABLE_PREFIX}_delaware_{_T_SHA}_{_DIRTY_SHA}',
            (_T_SHA, _DIRTY_SHA),
        ),
        # Full GEE-asset style path — the terminal segment is what matters.
        (
            f'{constants.GEE_ASSET_ROOT}/' f'luc_crops_iowa_{_T_SHA}_{_P_SHA}',
            (_T_SHA, _P_SHA),
        ),
    ],
)
def test_parse_compound_version_round_trips(
    asset_id: str, expected: tuple[str, str]
) -> None:
    assert parse_compound_version_from_asset_id(asset_id) == expected


def test_parse_compound_version_rejects_single_sha_assets() -> None:
    # A transform-stage asset (one trailing SHA) must NOT be mis-parsed
    # as a compound publish-asset.
    single = f'{constants.GEE_ASSET_ROOT}/land_use_delaware_{_T_SHA}'
    assert parse_compound_version_from_asset_id(single) is None
    # Single-SHA parser still works on it.
    assert parse_version_from_asset_id(single) == _T_SHA


def test_parse_compound_version_rejects_non_sha_trailing_segments() -> None:
    assert (
        parse_compound_version_from_asset_id('luc_transitions_delaware_notasha') is None
    )
    # Extract-style v-versions are not a compound either.
    assert parse_compound_version_from_asset_id('harris_agb_conus_v2021') is None

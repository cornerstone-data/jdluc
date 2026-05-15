"""Unit tests for extract-side metadata.

These checks lock in the invariants extract/extract.py relies on: every
non-native dataset carries an expected version and source URL, every
versioned asset ID ends with its expected version, and the NASS
(release_date, version) mapping is mechanical (bumping the release date
without registering the new version blows up at import).
"""

import pytest

from jdluc.utils import constants


def test_non_native_datasets_covers_six_families() -> None:
    assert set(constants.NON_NATIVE_DATASETS) == {
        'harris_agb',
        'huang_bgb',
        'nass_yields',
        'ipcc_climate_zones',
        'gfw_peatlands',
        'county_fips',
    }


def test_county_fips_is_first_in_orchestrator_order() -> None:
    # County-FIPS is the cheapest extract (server-side paint with no
    # HTTP / GCS round-trip), so the orchestrator runs it first per the
    # "fast-failing mis-configurations surface first" convention. Mostly
    # this guards the comment in NON_NATIVE_DATASETS from drifting away
    # from the actual list order.
    assert constants.NON_NATIVE_DATASETS[0] == 'county_fips'


def test_gpm_is_not_in_inventory() -> None:
    # We use the GFW Global Peatlands composite (CC BY 4.0) rather than
    # GPM 2.0 (CC BY-NC-SA). Guards against a careless revert
    # that brings GPM back as a live dependency — license flow-through
    # would block commercial use of Cornerstone's outputs.
    assert 'global_peatland_map' not in constants.DATASET_INVENTORY
    for entry in constants.DATASET_INVENTORY.values():
        assert 'GLOBAL-PEATLAND-DATABASE' not in entry['gee_asset_id']


@pytest.mark.parametrize(
    'dataset_key, asset_id, expected_version',
    [
        ('harris_agb', constants.GEE_HARRIS_AGB, constants.EXPECTED_HARRIS_VERSION),
        ('huang_bgb', constants.GEE_HUANG_BGB, constants.EXPECTED_HUANG_VERSION),
        ('nass_yields', constants.GEE_NASS_YIELDS, constants.EXPECTED_NASS_VERSION),
        (
            'ipcc_climate_zones',
            constants.GEE_IPCC_CLIMATE_ZONES,
            constants.EXPECTED_IPCC_VERSION,
        ),
        (
            'gfw_peatlands',
            constants.GEE_GFW_PEATLANDS,
            constants.EXPECTED_GFW_PEATLANDS_VERSION,
        ),
        (
            'county_fips',
            constants.GEE_COUNTY_FIPS_LABEL,
            constants.EXPECTED_TIGER_COUNTIES_VERSION,
        ),
    ],
)
def test_asset_id_carries_expected_version(
    dataset_key: str, asset_id: str, expected_version: str
) -> None:
    assert asset_id.endswith(f'_{expected_version}')
    entry = constants.DATASET_INVENTORY[dataset_key]
    assert entry['gee_asset_id'] == asset_id
    assert entry['expected_version'] == expected_version
    assert entry['native_or_extracted'] == 'extracted'
    assert entry['source_url']


def test_native_dataset_entries_have_no_extract_metadata() -> None:
    # Native datasets intentionally omit expected_version/source_url — the
    # orchestrator iterates NON_NATIVE_DATASETS only, never these.
    for key, entry in constants.DATASET_INVENTORY.items():
        if entry['native_or_extracted'] == 'native':
            assert 'expected_version' not in entry, key
            assert 'source_url' not in entry, key


def test_nass_version_derivation_is_mechanical() -> None:
    # The registered release date maps to the transform-window-aligned
    # version tag, and the derivation helper round-trips.
    derived = constants._derive_nass_version_from_release_date(
        constants.NASS_QUICKSTATS_RELEASE_DATE
    )
    assert derived == constants.EXPECTED_NASS_VERSION
    expected_window = (
        f'v{constants.NASS_YIELD_YEARS[0]}_{constants.NASS_YIELD_YEARS[-1]}'
    )
    assert derived == expected_window


def test_nass_version_derivation_rejects_unregistered_release_date() -> None:
    with pytest.raises(ValueError, match='has no registered version'):
        constants._derive_nass_version_from_release_date('19990101')


def test_nass_url_template_formats_with_release_date() -> None:
    # Round-trip the template ↔ release date so a template change that
    # drops the {date} placeholder is caught at test time.
    url = constants.NASS_QUICKSTATS_URL_TEMPLATE.format(
        date=constants.NASS_QUICKSTATS_RELEASE_DATE
    )
    assert constants.NASS_QUICKSTATS_RELEASE_DATE in url
    assert url.endswith('.txt.gz')


def test_harris_source_url_is_arcgis_featureserver() -> None:
    # Harris extract uses the ArcGIS FeatureServer query endpoint to
    # discover per-tile signed download URLs; the source_url therefore
    # points at the FeatureServer rather than a direct tile file.
    assert 'arcgis.com' in constants.HARRIS_AGB_ARCGIS_FEATURESERVER
    assert constants.HARRIS_AGB_ARCGIS_FEATURESERVER.endswith('/query')
    entry = constants.DATASET_INVENTORY['harris_agb']
    assert entry['source_url'] == constants.HARRIS_AGB_ARCGIS_FEATURESERVER


def test_gfw_peatlands_source_url_is_gfw_url_template() -> None:
    # The peatland source is the GFW Global Peatlands raster tile
    # composite. The URL template carries {tile_id} and {api_key}
    # placeholders; per-tile URLs are formatted at extract time. The
    # DATASET_INVENTORY entry publishes a key-elided form of the same
    # template (same posture as Harris AGB's FeatureServer URL).
    assert 'data-api.globalforestwatch.org' in constants.GFW_PEATLANDS_URL_TEMPLATE
    assert '{tile_id}' in constants.GFW_PEATLANDS_URL_TEMPLATE
    assert '{api_key}' in constants.GFW_PEATLANDS_URL_TEMPLATE
    assert 'gfw_peatlands' in constants.GFW_PEATLANDS_URL_TEMPLATE
    assert 'v20230315' in constants.GFW_PEATLANDS_URL_TEMPLATE

    entry = constants.DATASET_INVENTORY['gfw_peatlands']
    assert 'data-api.globalforestwatch.org' in entry['source_url']
    # Inventory exposes the URL template with the api-key placeholder
    # elided to '<api-key>'; the {tile_id} placeholder remains.
    assert '<api-key>' in entry['source_url']
    assert '{tile_id}' in entry['source_url']
    # The actual API key never leaks into the inventory.
    assert constants.GFW_DATA_API_KEY not in entry['source_url']

"""Run the jdLUC pipeline for one region (default: Delaware).

Usage examples:
    uv run python jdluc/cli.py
    uv run python jdluc/cli.py --state 10 --region delaware
    uv run python jdluc/cli.py --states 19,31,46 --region great_plains_test
    uv run python jdluc/cli.py --region great_plains_test
    uv run python jdluc/cli.py --region conus
    uv run python jdluc/cli.py --force -v

REGIONS registry below is dev scaffolding for local iteration — named
multi-state regions whose state lists live in one place. Unknown
--region values are treated as free-form labels paired with --state /
--states. 
"""

import argparse
import logging
import sys
from typing import TypedDict

from jdluc.utils.constants import CONUS_STATE_FIPS, GCP_PROJECT


class _RegionSpec(TypedDict):
    states: list[str]
    region_name: str


REGIONS: dict[str, _RegionSpec] = {
    # Single state.
    'delaware': {'states': ['10'], 'region_name': 'delaware'},
    # Multi-state test: Corn Belt + northern Plains.
    # Iowa=19, Nebraska=31, South Dakota=46.
    'great_plains_test': {
        'states': ['19', '31', '46'],
        'region_name': 'great_plains_test',
    },
    # Full 48-state + DC list from utils/constants.py.
    'conus': {'states': list(CONUS_STATE_FIPS), 'region_name': 'conus'},
}


def _resolve_states_and_region(args: argparse.Namespace) -> tuple[list[str], str]:
    """Resolve the effective (states, region_name) from CLI args.

    Precedence:
      1. --states (explicit comma-separated FIPS list) + --region.
      2. --region matches REGIONS registry -> lookup.
      3. --state (single) + --region.
    """
    if args.states:
        states = [fips.strip() for fips in args.states.split(',') if fips.strip()]
        return states, args.region
    if args.region in REGIONS:
        entry = REGIONS[args.region]
        return list(entry['states']), entry['region_name']
    return [args.state], args.region


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Run the jdLUC pipeline for one region.'
    )
    parser.add_argument(
        '--state',
        default='10',
        help='Single state FIPS code (default: 10 = Delaware). Ignored if --states is set.',
    )
    parser.add_argument(
        '--states',
        default='',
        help='Comma-separated state FIPS codes (e.g. 19,31,46). Takes precedence over --state.',
    )
    parser.add_argument(
        '--region',
        default='delaware',
        help=(
            'Region label for asset naming. If it matches a REGIONS key '
            '(delaware/great_plains_test/conus) the state list is resolved '
            'from the registry; otherwise treated as a free-form label '
            'paired with --state / --states.'
        ),
    )
    parser.add_argument(
        '--gcp-project',
        default=GCP_PROJECT,
        help=(
            'GCP project ID (default: utils.constants.GCP_PROJECT — edit '
            'that file to repoint at a different deployment).'
        ),
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force re-export even if the asset already exists',
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Enable verbose (DEBUG) logging',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    states, region_name = _resolve_states_and_region(args)

    from jdluc.pipeline import run_pipeline

    result = run_pipeline(
        gcp_project=args.gcp_project,
        states=states,
        region_name=region_name,
        force=args.force,
    )

    status = 'loaded from cache' if result.from_cache else 'computed + exported'
    print(f'\nPipeline version: {result.version}')
    print(f'Region: {result.region_name}')
    print(f'States: {result.states}')
    if result.extract_result is not None:
        er = result.extract_result
        print(
            f'\nExtract: cached={er.cached} extracted={er.extracted} '
            f'failed={list(er.failed)}'
        )
    print(f'\nland_use asset: {result.land_use_asset_id}')
    print(f'emissions asset: {result.emissions_asset_id}')
    print(f'transitions table: {result.transitions_table_id}')
    print(f'crops table: {result.crops_table_id}')
    if result.publish_result is not None:
        from jdluc.publish.publish import target_entries

        pr = result.publish_result
        print('\nPublish:')
        for name, target in target_entries(pr):
            kind = 'cached' if target.from_cache else 'exported'
            print(f'  {name:<11s}: {target}  ({kind})')
        print(f'  publish_version: {pr.transitions.publish_version}')
    print(f'\nStatus: {status}')

    return 0


if __name__ == '__main__':
    sys.exit(main())

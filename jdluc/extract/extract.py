"""Extract stage entry point.

Orchestrates ingestion of all external non-GEE-native datasets into Google
Earth Engine. Iterates through ``NON_NATIVE_DATASETS``, compares each
dataset's ``expected_version`` against what's already in GEE, and calls the
per-dataset extractor on a miss.

GEE-native datasets (GLAD GLC, CDL, SoilGrids, TIGER states, TIGER
counties) are used directly and require no ingestion, so they do not
appear in ``NON_NATIVE_DATASETS`` and are never touched by this module.

Extracted assets are tagged with an upstream-dataset version suffix
(e.g. ``harris_agb_conus_v2021``) — no code-SHA suffix, since extract
logic is stable and re-running at the same upstream version produces
byte-identical assets.

See specs/pipeline_tech_design.md § Extract for details.
"""

import dataclasses
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Protocol

from jdluc.extract import (
    county_fips,
    gfw_peatlands,
    harris_agb,
    huang_bgb,
    ipcc_climate_zones,
    nass_yields,
)
from jdluc.utils.constants import DATASET_INVENTORY, NON_NATIVE_DATASETS
from jdluc.utils.gee import asset_is_populated

logger = logging.getLogger(__name__)


class ExtractorCallable(Protocol):
    def __call__(self, gcp_project: str, force: bool = False) -> str: ...


@dataclasses.dataclass
class ExtractResult:
    cached: list[str] = dataclasses.field(default_factory=list)
    extracted: list[str] = dataclasses.field(default_factory=list)
    failed: dict[str, str] = dataclasses.field(default_factory=dict)


class ExtractError(Exception):
    def __init__(self, result: ExtractResult) -> None:
        self.result = result
        failed_summary = ', '.join(f'{k}({v})' for k, v in result.failed.items())
        super().__init__(
            f'Extract failed for {len(result.failed)} dataset(s): {failed_summary}'
        )


DATASET_FAMILY_TO_EXTRACTOR = {
    'harris_agb': harris_agb.extract_harris_agb,
    'huang_bgb': huang_bgb.extract_huang_bgb,
    'nass_yields': nass_yields.extract_nass_yields,
    'ipcc_climate_zones': ipcc_climate_zones.extract_ipcc_climate_zones,
    'gfw_peatlands': gfw_peatlands.extract_gfw_peatlands,
    'county_fips': county_fips.extract_county_fips,
}


def extract_all(
    gcp_project: str,
    force: bool = False,
    *,
    extractor_factory: Callable[
        [str], ExtractorCallable
    ] = DATASET_FAMILY_TO_EXTRACTOR.__getitem__,
    asset_exists: Callable[[str], bool] = asset_is_populated,
) -> ExtractResult:
    """Run every non-native dataset extractor, skipping cache hits.

    Args:
        gcp_project: GCP project ID for Earth Engine client initialization
            inside per-dataset extractors. Caller is responsible for having
            initialized ``ee`` already (see ``run_pipeline``).
        force: If True, ignore existing assets and re-ingest unconditionally.
        extractor_factory: Dispatch from dataset family to its extractor function.
        asset_exists: Override for the cache-hit probe. Injectable for unit
            testing; defaults to ``utils.gee.asset_is_populated`` so that an
            empty ImageCollection (e.g. left behind by a partial tile
            ingestion) is treated as a miss, not a hit.

    Returns:
        ExtractResult with ``cached`` / ``extracted`` / ``failed`` populated.
        Failures do NOT abort the orchestrator — other datasets still run,
        so a single broken source doesn't block unrelated work.
    """
    result = ExtractResult()

    # Step 1: probe cache and split into cached / work queues. The probes
    # are cheap getAsset calls and run sequentially — keeps the log order
    # for cache-hit reporting deterministic.
    work: list[str] = []
    for dataset_family in NON_NATIVE_DATASETS:
        entry = DATASET_INVENTORY[dataset_family]
        asset_id = entry['gee_asset_id']
        expected_version = entry.get('expected_version', '<unversioned>')

        if not force and asset_exists(asset_id):
            logger.info(
                f'extract[{dataset_family}]: cache hit at '
                f'{asset_id} (version={expected_version})'
            )
            result.cached.append(dataset_family)
            continue

        logger.info(
            f'extract[{dataset_family}]: cache miss, '
            f'running extractor (version={expected_version}, force={force})'
        )
        work.append(dataset_family)

    # Step 2: fan out the cache-misses in parallel. The 6 extractors have
    # no inter-dependencies; each writes a distinct GEE asset and handles
    # its own internal threading. The GEE per-project ingest task cap (~20)
    # is the binding upstream constraint, well above 6 outer workers.
    outcomes: dict[str, str | Exception] = {}
    if work:
        futures: dict[Future[str], str] = {}
        with ThreadPoolExecutor(max_workers=len(work)) as executor:
            for dataset_family in work:
                extractor = extractor_factory(dataset_family)
                futures[executor.submit(extractor, gcp_project, force)] = dataset_family
            for future in as_completed(futures):
                family = futures[future]
                try:
                    outcomes[family] = future.result()
                except Exception as exc:
                    outcomes[family] = exc

    # Step 3: collate in NON_NATIVE_DATASETS order so result.extracted /
    # result.failed are deterministic regardless of completion order.
    for dataset_family in NON_NATIVE_DATASETS:
        if dataset_family not in outcomes:
            continue
        outcome = outcomes[dataset_family]
        asset_id = DATASET_INVENTORY[dataset_family]['gee_asset_id']
        if isinstance(outcome, Exception):
            logger.error(f'extract[{dataset_family}]: failed — {outcome}')
            result.failed[dataset_family] = str(outcome)
            continue
        if outcome != asset_id:
            logger.warning(
                f'extract[{dataset_family}]: extractor returned '
                f'{outcome!r} but inventory expects {asset_id!r}'
            )
        logger.info(f'extract[{dataset_family}]: extracted → {outcome}')
        result.extracted.append(dataset_family)

    logger.info(
        f'extract_all: cached={result.cached}, '
        f'extracted={result.extracted}, failed={list(result.failed)}'
    )
    return result

"""Ingest a named dataset over a continent's tile set.

The parser is positional — `continent_name dataset_name` — plus optional `--concurrency`
and `--overwrite`. Continents come from `continents.Continent`; datasets from
`datasets.DatasetName`. Whole-world datasets are ingested over the "world" tile based on
their partitioning, so the continent only bounds tiled datasets.

Example invocations:
  uv run python -m jdluc.ingest NORTH_AMERICA IPCC_CLIMATE_ZONES
  uv run python -m jdluc.ingest NORTH_AMERICA GLAD_GLCLUC
  uv run python -m jdluc.ingest AFRICA GFW_GLOBAL_PEATLANDS --concurrency=8 --overwrite
"""

import argparse
import concurrent.futures
import logging

from jdluc import config, continents, datasets, tiling
from jdluc.datasets import base

logger = logging.getLogger(__name__)


def workflow(
    concurrency: int,
    dataset: base.RasterDataset | base.TabularDataset | base.VectorDataset,
    overwrite: bool,
    root: str,
    tile_ids: tuple[str, ...],
) -> dict[str, str | Exception]:
    if dataset.partitioning == tiling.Partitioning.WHOLE_WORLD:
        tile_ids = (tiling.WHOLE_WORLD_TILE_ID,)
        logger.warning(f"Ingesting {dataset=} for {tile_ids=}")
    tile_id_is_valid_func = tiling.PARTITIONING_TO_IS_VALID_TILE_ID[
        dataset.partitioning
    ]
    if not all(map(tile_id_is_valid_func, tile_ids)):
        raise ValueError(f"{tile_ids=:} are not valid for {dataset=:}")
    results: dict[str, str | Exception] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures: dict[concurrent.futures.Future[str], str] = {}
        for tile_id in tile_ids:
            futures[
                executor.submit(
                    dataset.ingest_a_tile,
                    overwrite=overwrite,
                    root=root,
                    tile_id=tile_id,
                )
            ] = tile_id
        for future in concurrent.futures.as_completed(futures):
            tile_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                results[tile_id] = exc
            else:
                results[tile_id] = result
    return results


DEFAULT_CONCURRENCY = 4


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "continent_name", choices=sorted(e.name for e in continents.Continent)
    )
    parser.add_argument(
        "dataset_name", choices=sorted(e.name for e in datasets.DatasetName)
    )
    parser.add_argument("--concurrency", default=DEFAULT_CONCURRENCY, type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    dataset = datasets.NAME_TO_CLS[datasets.DatasetName[str(args.dataset_name)]]
    tile_id_to_path_or_exc: dict[str, str | Exception] = workflow(
        concurrency=int(args.concurrency),
        dataset=dataset,
        overwrite=args.overwrite,
        root=config.Config.from_dot_env().ingest_root,
        tile_ids=continents.Continent[str(args.continent_name)].value,
    )
    for tile_id, path_or_exc in tile_id_to_path_or_exc.items():
        print(tile_id, repr(path_or_exc))
    return sum(
        1 for result in tile_id_to_path_or_exc.values() if isinstance(result, Exception)
    )


if __name__ == "__main__":
    raise SystemExit(main())

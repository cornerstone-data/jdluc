"""Ingest a named dataset over a named tile set.

The parser is positional — `tile_set_name dataset_name` — plus optional `--concurrency`
and `--overwrite`. Tile sets come from `tiling.TileSetName`; datasets from
`datasets.DatasetName`.

Example invocations:
  uv run python -m jdluc.ingest WHOLE_WORLD IPCC_CLIMATE_ZONES
  uv run python -m jdluc.ingest CONUS GLAD_GLCLUC
  uv run python -m jdluc.ingest DELAWARE GFW_GLOBAL_PEATLANDS --concurrency=8 --overwrite
"""

import argparse
import concurrent.futures
import logging

from jdluc import config, datasets, tiling
from jdluc.datasets import base


def workflow(
    bucket_name: str,
    concurrency: int,
    gcp_project: str,
    overwrite: bool,
    dataset: base.RasterDataset | base.TabularDataset | base.VectorDataset,
    tile_set: tiling.TileSetType,
) -> dict[str, str | Exception]:
    tile_id_is_valid_func = tiling.PARTITIONING_TO_IS_VALID_TILE_ID[
        dataset.partitioning
    ]
    if not all(map(tile_id_is_valid_func, tile_set)):
        raise ValueError(f"{tile_set=:} are not valid for {dataset=:}")
    results: dict[str, str | Exception] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures: dict[concurrent.futures.Future[str], str] = {}
        for tile_id in tile_set:
            futures[
                executor.submit(
                    dataset.ingest_a_tile,
                    bucket_name=bucket_name,
                    gcp_project=gcp_project,
                    overwrite=overwrite,
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
        "tile_set_name", choices=sorted(e.name for e in tiling.TileSetName)
    )
    parser.add_argument(
        "dataset_name", choices=sorted(e.name for e in datasets.DatasetName)
    )
    parser.add_argument("--concurrency", default=DEFAULT_CONCURRENCY, type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    dataset_name = datasets.DatasetName[str(args.dataset_name)]
    tile_set_name = tiling.TileSetName[str(args.tile_set_name)]
    cfg = config.Config.from_dot_env()
    results: dict[str, str | Exception] = workflow(
        bucket_name=cfg.ingest_bucket_name,
        concurrency=int(args.concurrency),
        dataset=datasets.NAME_TO_CLS[dataset_name],
        gcp_project=cfg.gcp_project,
        overwrite=args.overwrite,
        tile_set=tiling.NAME_TO_TILE_SET[tile_set_name],
    )
    for tile_id, result in results.items():
        print(tile_id, repr(result))
    return sum(1 for result in results.values() if isinstance(result, Exception))


if __name__ == "__main__":
    raise SystemExit(main())

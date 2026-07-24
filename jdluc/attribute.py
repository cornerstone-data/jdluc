"""Roll up per-pixel emissions to per-(jurisdiction, crop) totals.

Dispatches each ISO 3166 country to a per-methodology workflow (clustering countries by
continent so they share tile sets):
  - STATISTICAL (default): downscales the per-pixel emissions to the MAPSPAM grid and
    attributes them across crops by MAPSPAM crop-expansion shares.
  - JURISDICTIONAL_DIRECT: masks the per-pixel emissions to each crop's CDL codes.
Both clip to provincial (World Bank admin-1) polygons, restrict to GLAD 2020 cropland
unless `--skip-glad-crop-filter` is set, and sum crop area, peatland crop area,
peatland-occupation emissions, and total emissions. Returns a pandas.DataFrame indexed by
(admin level, crop, jurisdiction); the per-country sub-workflows are cached.

Example invocation:
  uv run python jdluc/attribute.py --methodology-name STATISTICAL USA
"""

import argparse
import collections.abc
import enum
import logging

import pandas

from jdluc import (
    continents,
    jurisdictional_direct,
    statistical,
)

logger = logging.getLogger(__name__)


def iso_3166_str(s: str) -> str:
    assert len(s) == 3, f"iso_3166 code {s=:s} must be three characters"
    return s


class Methodology(enum.IntEnum):
    JURISDICTIONAL_DIRECT = enum.auto()
    STATISTICAL = enum.auto()


def workflow(
    crop_names: tuple[str, ...],
    iso_3166s: collections.abc.Sequence[str],
    methodology: Methodology,
    skip_glad_crop_filter: bool,
) -> pandas.DataFrame:
    workflow_for_tile_ids = (
        jurisdictional_direct.workflow
        if methodology == Methodology.JURISDICTIONAL_DIRECT
        else statistical.workflow
    )
    dfs = (
        workflow_for_tile_ids(
            crop_names=crop_names,
            iso_3166=iso_3166,
            skip_glad_crop_filter=skip_glad_crop_filter,
            tile_ids=tuple(sorted(tile_cluster)),
        )
        for tile_cluster, iso_3166_cluster in continents.iter_tile_cluster_to_iso_3166s(
            iso_3166s=iso_3166s
        )
        for iso_3166 in sorted(iso_3166_cluster)
    )
    return (
        pandas.concat(list(dfs))
        .assign(methodology=methodology.name)
        .set_index("methodology", append=True)
        .sort_index()
    )


def get_crop_names(
    methodology: Methodology, process_few_crops: bool
) -> tuple[str, ...]:
    if process_few_crops:
        return ("MAIZE", "SOYBEAN", "WHEAT")
    elif methodology == Methodology.STATISTICAL:
        return tuple(sorted(c.name for c in statistical.Crop))
    else:
        assert methodology == Methodology.JURISDICTIONAL_DIRECT
        return tuple(sorted(c.name for c in jurisdictional_direct.Crop))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso_3166s", nargs=argparse.ONE_OR_MORE, type=iso_3166_str)
    parser.add_argument(
        "--methodology-name",
        choices=sorted(e.name for e in Methodology),
        default=Methodology.STATISTICAL.name,
    )
    parser.add_argument("--process-few-crops", action="store_true")
    parser.add_argument("--skip-display", action="store_true")
    parser.add_argument("--skip-glad-crop-filter", action="store_true")
    args = parser.parse_args()

    methodology = Methodology[str(args.methodology_name)]
    df = workflow(
        crop_names=get_crop_names(
            methodology=methodology, process_few_crops=args.process_few_crops
        ),
        iso_3166s=args.iso_3166s,
        methodology=methodology,
        skip_glad_crop_filter=args.skip_glad_crop_filter,
    )
    if not args.skip_display:
        print(df.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

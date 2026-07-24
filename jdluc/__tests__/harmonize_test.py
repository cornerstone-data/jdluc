import contextlib
import functools

import pytest

from jdluc import tiling
from jdluc.datasets.base import BandType
from jdluc.harmonize import Grid


@pytest.mark.parametrize(
    ("x", "y", "fails"),
    (
        (0, 0, False),
        (100, 100, False),
        (-1, -1, True),
    ),
)
def test_xy_validated(x: int, y: int, fails: bool) -> None:
    xy = tiling.XY(x=x, y=y)
    context = (
        functools.partial(pytest.raises, AssertionError)
        if fails
        else contextlib.nullcontext
    )
    with context():
        xy.validated()


@pytest.mark.parametrize(
    (
        "tile_ids",
        "origin",
        "tiles",
        "transform",
        "resolution",
    ),
    (
        pytest.param(
            ("00N_000W",),
            tiling.XY(0, 0),
            tiling.XY(1, 1),
            (0, 1 / 4_000, 0, 0, 0, -1 / 4_000),
            tiling.XY(40_000, 40_000),
            id="One ten-degree-tile",
        ),
        pytest.param(
            ("00N_000W", "00N_010E", "10S_000W", "10S_010E"),
            tiling.XY(0, 0),
            tiling.XY(2, 2),
            (0, 1 / 4_000, 0, 0, 0, -1 / 4_000),
            tiling.XY(2 * 40_000, 2 * 40_000),
            id="2x2 ten-degree-tiles with NW=0,0",
        ),
        pytest.param(
            ("60N_060W", "60S_060E"),
            tiling.XY(-60, 60),
            tiling.XY(13, 13),
            (-60, 1 / 4_000, 0, 60, 0, -1 / 4_000),
            tiling.XY(13 * 40_000, 13 * 40_000),
            id="two tiles spanning a 13x13 area",
        ),
    ),
)
def test_grid_from_tile_ids_resolution(
    tile_ids: tuple[str, ...],
    origin: tiling.XY,
    tiles: tiling.XY,
    transform: tuple[float, float, float, float, float, float],
    resolution: tiling.XY,
) -> None:
    result = Grid.from_tile_ids_resolution(
        tile_ids=tile_ids, tile_resolution=tiling.TileResolution.GLAD
    )
    assert result.origin == origin
    assert result.tiles == tiles
    assert result.tile_resolution == tiling.TileResolution.GLAD
    assert result.transform == transform
    assert result.resolution == resolution


@pytest.mark.parametrize(
    ("tile_id", "offset"),
    (("00N_000W", tiling.XY(0, 0)), ("90S_180E", tiling.XY(180, 90))),
)
def test_grid_get_offset_for_tile(tile_id: str, offset: tiling.XY) -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("00N_000W",), tile_resolution=tiling.XY(10, 10)
    )
    assert grid.get_offset_for_tile(tile_id=tile_id) == offset


def test_grid_get_offset_for_tile_raises() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("00N_000W",), tile_resolution=tiling.XY(10, 10)
    )
    with pytest.raises(AssertionError):
        grid.get_offset_for_tile(tile_id="10N_010W")


def test_grid_get_offset_for_world() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("60N_060W",), tile_resolution=tiling.XY(10, 10)
    )
    assert grid.get_offset_for_world(
        resolution=tiling.XY(360, 180), span=tiling.XY(360, 180)
    ) == tiling.XY(120, 30)


def test_grid_get_resolution_for_world() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("60N_060W",), tile_resolution=tiling.XY(10, 10)
    )
    assert grid.get_resolution_for_world(
        resolution=tiling.XY(360, 180), span=tiling.XY(360, 180)
    ) == tiling.XY(10, 10)


@pytest.mark.parametrize(
    ("band_type", "resolution", "expected"),
    (
        (BandType.CATEGORICAL, tiling.XY(1, 1), "nearest"),
        (BandType.CATEGORICAL, tiling.XY(2, 2), "nearest"),
        (BandType.CATEGORICAL, tiling.XY(3, 3), "mode"),
        (BandType.INTENSIVE, tiling.XY(1, 1), "bilinear"),
        (BandType.INTENSIVE, tiling.XY(2, 2), "nearest"),
        (BandType.INTENSIVE, tiling.XY(3, 3), "average"),
        (BandType.EXTENSIVE, tiling.XY(2, 2), "nearest"),
    ),
)
def test_grid_get_resampling_for_band_type(
    band_type: BandType, resolution: tiling.XY, expected: str
) -> None:
    grid = Grid(
        origin=tiling.XY(0, 0), tiles=tiling.XY(2, 2), tile_resolution=tiling.XY(2, 2)
    )
    assert (
        grid.get_resampling_for_band_type(
            band_type=band_type,
            src_resolution=resolution,
            dest_resolution=grid.tile_resolution,
        ).name
        == expected
    )


@pytest.mark.parametrize(
    ("src_resolution", "match"),
    (
        (tiling.XY(3, 3), "GDAL doesn't implement sum resampling"),
        (tiling.XY(1, 1), "GDAL doesn't implement distribution resampling"),
    ),
)
def test_grid_get_resampling_for_band_type_raises(
    src_resolution: tiling.XY, match: str
) -> None:
    grid = Grid(
        origin=tiling.XY(0, 0), tiles=tiling.XY(2, 2), tile_resolution=tiling.XY(2, 2)
    )
    with pytest.raises(NotImplementedError, match=match):
        assert grid.get_resampling_for_band_type(
            band_type=BandType.EXTENSIVE,
            src_resolution=src_resolution,
            dest_resolution=grid.tile_resolution,
        )


def test_grid_get_resampling_for_band_type_upsamples_clipped_world() -> None:
    # A whole-world source that is large in absolute pixels but coarse per
    # degree: comparing its full resolution against the grid would pick
    # downsampling, but once clipped to the grid extent it is coarser than the
    # destination and must be upsampled.
    grid = Grid(
        origin=tiling.XY(0, 0), tiles=tiling.XY(1, 1), tile_resolution=tiling.XY(36, 36)
    )
    world_resolution = tiling.XY(360, 180)  # 1 px/degree, coarser than the grid
    src_resolution = grid.get_resolution_for_world(
        resolution=world_resolution, span=tiling.XY(360, 180)
    )
    # Clipped to the grid's 10x10-degree extent the source is only 10x10 px,
    # versus the 36x36 px destination, even though the full source is 360x180.
    assert src_resolution == tiling.XY(10, 10)
    assert world_resolution.x > grid.tile_resolution.x  # the old, buggy comparison
    assert (
        grid.get_resampling_for_band_type(
            band_type=BandType.INTENSIVE,
            src_resolution=src_resolution,
            dest_resolution=grid.resolution,
        ).name
        == "bilinear"
    )

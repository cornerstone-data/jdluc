import contextlib
import functools

import pytest

from jdluc.harmonize import GLAD_TILE_RESOLUTION, XY, Grid


@pytest.mark.parametrize(
    ("x", "y", "fails"),
    (
        (0, 0, False),
        (100, 100, False),
        (-1, -1, True),
    ),
)
def test_xy_validated(x: int, y: int, fails: bool) -> None:
    xy = XY(x=x, y=y)
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
            XY(0, 0),
            XY(1, 1),
            (0, 1 / 4_000, 0, 0, 0, -1 / 4_000),
            XY(40_000, 40_000),
            id="One ten-degree-tile",
        ),
        pytest.param(
            ("00N_000W", "00N_010E", "10S_000W", "10S_010E"),
            XY(0, 0),
            XY(2, 2),
            (0, 1 / 4_000, 0, 0, 0, -1 / 4_000),
            XY(2 * 40_000, 2 * 40_000),
            id="2x2 ten-degree-tiles with NW=0,0",
        ),
        pytest.param(
            ("60N_060W", "60S_060E"),
            XY(-60, 60),
            XY(13, 13),
            (-60, 1 / 4_000, 0, 60, 0, -1 / 4_000),
            XY(13 * 40_000, 13 * 40_000),
            id="two tiles spanning a 13x13 area",
        ),
    ),
)
def test_grid_from_tile_ids_resolution(
    tile_ids: tuple[str, ...],
    origin: XY,
    tiles: XY,
    transform: tuple[float, float, float, float, float, float],
    resolution: XY,
) -> None:
    result = Grid.from_tile_ids_resolution(
        tile_ids=tile_ids, tile_resolution=GLAD_TILE_RESOLUTION
    )
    assert result.origin == origin
    assert result.tiles == tiles
    assert result.tile_resolution == GLAD_TILE_RESOLUTION
    assert result.transform == transform
    assert result.resolution == resolution


@pytest.mark.parametrize(
    ("tile_id", "offset"), (("00N_000W", XY(0, 0)), ("90S_180E", XY(180, 90)))
)
def test_grid_get_offset_for_tile(tile_id: str, offset: XY) -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("00N_000W",), tile_resolution=XY(10, 10)
    )
    assert grid.get_offset_for_tile(tile_id=tile_id) == offset


def test_grid_get_offset_for_tile_raises() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("00N_000W",), tile_resolution=XY(10, 10)
    )
    with pytest.raises(AssertionError):
        grid.get_offset_for_tile(tile_id="10N_010W")


def test_grid_get_offset_for_world() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("60N_060W",), tile_resolution=XY(10, 10)
    )
    assert grid.get_offset_for_world(resolution=XY(360, 180), span=XY(360, 180)) == XY(
        120, 30
    )


def test_grid_get_resolution_for_world() -> None:
    grid = Grid.from_tile_ids_resolution(
        tile_ids=("60N_060W",), tile_resolution=XY(10, 10)
    )
    assert grid.get_resolution_for_world(
        resolution=XY(360, 180), span=XY(360, 180)
    ) == XY(10, 10)

import numpy
import pytest

from jdluc.geo import (
    get_chunk_size,
    get_overview_level,
)


@pytest.mark.parametrize(
    ("height", "width", "minimum_pixels", "expected"),
    (
        (1024, 1024, 1024, 0),
        (2, 2, 1, 1),
        (1024, 1024, 1, 10),
        (1 << 32, 1 << 24, 1 << 6, 18),
    ),
)
def test_get_overview_level(
    height: int, width: int, minimum_pixels: int, expected: int
) -> None:
    assert (
        get_overview_level(height=height, width=width, minimum_pixels=minimum_pixels)
        == expected
    )


@pytest.mark.parametrize(
    ("dtypes", "number_of_dimensions", "chunk_size"),
    (
        ([numpy.dtype("uint8")], 1, 1 << 32),
        ([numpy.dtype("uint8")], 2, 1 << 16),
        ([numpy.dtype("uint8")], 3, 1 << 10),
        ([numpy.dtype("uint8")], 4, 1 << 8),
        ([numpy.dtype("uint16")], 1, 1 << 31),
        ([numpy.dtype("uint16")], 2, 1 << 15),
        ([numpy.dtype("uint16")], 3, 1 << 10),
        ([numpy.dtype("uint16")], 4, 1 << 7),
        ([numpy.dtype("float32")], 1, 1 << 30),
        ([numpy.dtype("float32")], 2, 1 << 15),
        ([numpy.dtype("float32")], 3, 1 << 10),
        ([numpy.dtype("float32")], 4, 1 << 7),
        ([numpy.dtype("float32")] * 1, 2, 1 << 15),
        ([numpy.dtype("float32")] * 2, 2, 1 << 14),
        ([numpy.dtype("float32")] * 3, 2, 1 << 14),
        ([numpy.dtype("float32")] * 4, 2, 1 << 14),
        ([numpy.dtype("float32")] * 5, 2, 1 << 13),
        ([numpy.dtype("float32")] * 6, 2, 1 << 13),
        ([numpy.dtype("float32")] * 7, 2, 1 << 13),
        ([numpy.dtype("float32")] * 8, 2, 1 << 13),
        ([numpy.dtype("float32")] * 9, 2, 1 << 13),
        ([numpy.dtype("float32")] * 10, 2, 1 << 13),
    ),
)
def test_get_chunk_size(
    dtypes: list[numpy.dtype], number_of_dimensions: int, chunk_size: int
) -> None:
    assert (
        get_chunk_size(
            dtypes=dtypes,
            number_of_dimensions=number_of_dimensions,
        )
        == chunk_size
    )

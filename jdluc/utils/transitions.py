"""Land use transition encoding/decoding helpers.

Transitions between two land use categories are packed into a single uint8:
the upper 4 bits hold from_code and the lower 4 bits hold to_code, with
0x00 reserved for the no-transition case. This scheme supports up to 16
land use categories per side — we currently have     9.

Two forms of each operation live in this file so the bit layout is defined
in exactly one place: scalar helpers for lookup-table construction and
.where() matchers, and raster helpers for the GEE pipeline.

See specs/pipeline_tech_design.md § transitions.py for details.
"""

from jdluc.utils._ee_types import EEImage

### scalar helpers ###


def encode_transition(from_code: int, to_code: int) -> int:
    """Pack a (from, to) category pair into a single uint8.

    Returns (from_code << 4) | to_code, or 0 when the categories are
    equal (the no-transition case).
    """
    assert (
        0 <= from_code < 16 and 0 <= to_code < 16
    ), f'category codes must fit in 4 bits, got from={from_code}, to={to_code}'
    if from_code == to_code:
        return 0
    return (from_code << 4) | to_code


### raster helpers ###


def encode_transition_image(from_img: EEImage, to_img: EEImage) -> EEImage:
    """Pack two classified category bands into a single uint8 transition band.

    Pixels where from_img == to_img are set to 0 (no-transition
    sentinel); all other pixels encode (from << 4) | to.
    """
    encoded = from_img.leftShift(4).bitwiseOr(to_img).uint8()
    return encoded.where(from_img.eq(to_img), 0)


def decode_from_image(encoded: EEImage) -> EEImage:
    """Extract the from category band from an encoded transition image."""
    return encoded.rightShift(4)


def decode_to_image(encoded: EEImage) -> EEImage:
    """Extract the to category band from an encoded transition image."""
    return encoded.bitwiseAnd(0xF)

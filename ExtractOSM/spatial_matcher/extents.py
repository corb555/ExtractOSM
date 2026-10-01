"""Named rectangular extents used by the spatial matching pipeline."""

from __future__ import annotations

from dataclasses import dataclass

WEB_MERCATOR_EPSG = 3857


@dataclass(frozen=True, slots=True)
class Extent:
    """Rectangular processing extent."""

    name: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    epsg: int = WEB_MERCATOR_EPSG


USWEST = Extent(name="USWEST", min_x=-14_137_600, min_y=2_879_275, max_x=-11_472_571,
    max_y=6_439_082, )

# Coarse padded rectangle around the contiguous United States.
# It is a processing filter, not a political boundary.
CONUS = Extent(name="CONUS", min_x=-14_150_000, min_y=2_600_000, max_x=-7_200_000,
    max_y=6_700_000, )

EXTENTS: dict[str, Extent] = {extent.name: extent for extent in (CONUS, USWEST)}


def get_extent(name: str) -> Extent:
    """Return a configured extent by name.

    Args:
        name: Case-insensitive extent name.

    Raises:
        ValueError: If the extent is not configured.
    """
    normalized = name.strip().upper()
    try:
        return EXTENTS[normalized]
    except KeyError as exc:
        valid_names = ", ".join(sorted(EXTENTS))
        raise ValueError(f"Unknown extent {name!r}. Valid extents: {valid_names}") from exc

"""Spatial preparation helpers."""

from __future__ import annotations

import pandas as pd
from pyproj import Transformer

from .extents import Extent

WGS84_EPSG = 4326


def filter_points_to_extent(
        frame: pd.DataFrame, *, longitude: str, latitude: str, extent: Extent, ) -> pd.DataFrame:
    """Filter WGS84 points to a rectangular projected extent.

    The projected coordinates are retained in ``match_x`` and ``match_y`` for
    later database loading and candidate matching.
    """
    transformer = Transformer.from_crs(WGS84_EPSG, extent.epsg, always_xy=True, )

    x_values, y_values = transformer.transform(frame[longitude].to_numpy(),
        frame[latitude].to_numpy(), )

    prepared = frame.copy()
    prepared["match_x"] = x_values
    prepared["match_y"] = y_values

    inside = (prepared["match_x"].between(extent.min_x, extent.max_x, inclusive="both") & prepared[
        "match_y"].between(extent.min_y, extent.max_y, inclusive="both"))
    return prepared.loc[inside].reset_index(drop=True)

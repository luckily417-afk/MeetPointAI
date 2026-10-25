"""几何与坐标系工具包。"""

from .crs import (
    GCJ02,
    WGS84,
    circle_bbox,
    destination_point,
    gcj02_to_wgs84,
    haversine,
    local_scale,
    wgs84_to_gcj02,
)
from .regions import (
    CircleRegion,
    PolygonRegion,
    Region,
    RegionError,
    build_region,
    region_from_dict,
    tile_circle,
    tile_polygon,
)

__all__ = [
    "GCJ02",
    "WGS84",
    "circle_bbox",
    "destination_point",
    "gcj02_to_wgs84",
    "haversine",
    "local_scale",
    "wgs84_to_gcj02",
    "CircleRegion",
    "PolygonRegion",
    "Region",
    "RegionError",
    "build_region",
    "region_from_dict",
    "tile_circle",
    "tile_polygon",
]

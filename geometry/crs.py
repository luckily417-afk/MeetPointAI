"""坐标系与地球几何基础算法。

约定：
* 高德全系（JS API / Web 服务 / POI / 路线）返回的坐标均为 **GCJ-02**；
* 程序内部统一使用 GCJ-02 经纬度存储与展示；
* 距离一律用 haversine 球面公式计算，绝不使用「经纬度差当成米」的近似；
* 多边形包含判断在局部等距投影平面内用 Shapely 完成（含边界），
  投影以区域质心为原点，避免大范围 cos(lat) 失真。
"""

from __future__ import annotations

import math

WGS84 = "WGS84"
GCJ02 = "GCJ-02"

EARTH_RADIUS_M = 6371008.8
_A = 6378245.0
_EE = 0.00669342162296594323


def _rad(deg: float) -> float:
    return math.radians(deg)


def haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """球面大圆距离（米）。"""
    p1, p2 = _rad(lat1), _rad(lat2)
    dphi = p2 - p1
    dlambda = _rad(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def local_scale(lat: float) -> float:
    """该纬度处 1 度经度对应的米数。"""
    return EARTH_RADIUS_M * _rad(1.0) * math.cos(_rad(lat))


def destination_point(lng: float, lat: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """由起点、方位角、距离推算终点经纬度。"""
    ang = distance_m / EARTH_RADIUS_M
    brg = _rad(bearing_deg)
    p1, l1 = _rad(lat), _rad(lng)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(brg))
    l2 = l1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(p1),
        math.cos(ang) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(l2), math.degrees(p2)


def circle_bbox(lng: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
    """圆形区域外接矩形 (min_lng, min_lat, max_lng, max_lat)。"""
    north = destination_point(lng, lat, 0.0, radius_m)
    south = destination_point(lng, lat, 180.0, radius_m)
    east = destination_point(lng, lat, 90.0, radius_m)
    west = destination_point(lng, lat, 270.0, radius_m)
    return (
        min(west[0], east[0]),
        min(south[1], north[1]),
        max(west[0], east[0]),
        max(south[1], north[1]),
    )


class LocalProjector:
    """以 (lon0, lat0) 为原点的局部等距圆柱投影。

    在 <= 50 km 范围内误差 < 0.1%，足以做点面关系判断；
    距离度量仍由 haversine 负责。
    """

    __slots__ = ("lon0", "lat0", "_kx", "_ky")

    def __init__(self, lon0: float, lat0: float) -> None:
        self.lon0 = lon0
        self.lat0 = lat0
        self._kx = EARTH_RADIUS_M * _rad(1.0) * math.cos(_rad(lat0))
        self._ky = EARTH_RADIUS_M * _rad(1.0)

    def project(self, lng: float, lat: float) -> tuple[float, float]:
        return (lng - self.lon0) * self._kx, (lat - self.lat0) * self._ky

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        return self.lon0 + x / self._kx, self.lat0 + y / self._ky

    def distance(self, ax: float, ay: float, bx: float, by: float) -> float:
        return haversine(ax, ay, bx, by)


# --------------------------------------------------------------------------- #
# WGS-84 <-> GCJ-02（用于「明确坐标系」的输入转换与调试）
# --------------------------------------------------------------------------- #
def _out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = _rad(lat)
    magic = 1 - _EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    # 迭代反解，精度 < 0.5 m
    wlng, wlat = lng, lat
    for _ in range(3):
        glng, glat = wgs84_to_gcj02(wlng, wlat)
        wlng += lng - glng
        wlat += lat - glat
    return wlng, wlat

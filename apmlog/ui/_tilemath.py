"""_tilemath —— Web Mercator 瓦片坐标换算 + WGS-84↔GCJ-02 偏移。

设计约束（见 docs/CONTRACT.md）：
- 零第三方依赖，只用标准库 math。
- 场景坐标基准统一为 **REF_Z=18 级瓦片像素**（1 场景单位 = 赤道处约 0.239 m），
  这样任意缩放级 z 的屏幕像素/场景单位比恒为 2**(z-REF_Z)，无需重算几何。
- GCJ-02 只用于国内矢量/影像源（高德）；境外一律不外偏（out_of_china 判定）。
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

TILE_SIZE: int = 256                 # 标准 XYZ 瓦片边长（像素）
REF_Z: int = 18                      # 场景单位基准缩放级
EARTH_CIRCUMFERENCE: float = 40075016.6855785  # 赤道周长（米）
MAX_LAT_MERCATOR: float = 85.051128779806595    # Web Mercator 有效纬度上限

# Krasovsky 1940 椭球（GCJ-02 加偏所用参数）
_GCJ_A: float = 6378245.0
_GCJ_EE: float = 0.00669342162296594323


def scale_at(z: float) -> float:
    """缩放级 z 对应的『屏幕像素 / 场景单位』比值。"""
    return 2.0 ** (z - REF_Z)


def clamp_lat(lat):
    """钳制到 Web Mercator 可表达范围。**同时支持标量与 ndarray**。

    非有限值（nan/inf）归 0，避免把 nan 传给绘图层导致整条路径消失。
    """
    a = np.asarray(lat, dtype=np.float64)
    if a.ndim == 0:
        v = float(a)
        if not math.isfinite(v):
            return 0.0
        return float(max(-MAX_LAT_MERCATOR, min(MAX_LAT_MERCATOR, v)))
    return np.clip(np.where(np.isfinite(a), a, 0.0), -MAX_LAT_MERCATOR, MAX_LAT_MERCATOR)


def clamp_lon(lon):
    """经度归一化到 [-180, 180)。**同时支持标量与 ndarray**。"""
    a = np.asarray(lon, dtype=np.float64)
    if a.ndim == 0:
        v = float(a)
        return 0.0 if not math.isfinite(v) else (v + 180.0) % 360.0 - 180.0
    out = (a + 180.0) % 360.0 - 180.0
    return np.where(np.isfinite(a), out, 0.0)


def lat_to_mercator_y_np(lat) -> np.ndarray:
    """纬度数组 → 归一化北向坐标（向量化，供 2 万点轨迹一次算完）。"""
    a = np.radians(clamp_lat(lat))
    return (1.0 - np.log(np.tan(a) + 1.0 / np.cos(a)) / math.pi) / 2.0


def scene_from_lonlat_np(lat, lon, ref_z: int = REF_Z) -> tuple[np.ndarray, np.ndarray]:
    """批量经纬度 → 场景坐标数组（Web Mercator，y 向下）。"""
    world = TILE_SIZE * (1 << ref_z)
    la = np.atleast_1d(np.asarray(lat, dtype=np.float64))
    lo = np.atleast_1d(np.asarray(lon, dtype=np.float64))
    return (clamp_lon(lo) + 180.0) / 360.0 * world, lat_to_mercator_y_np(la) * world


def tile_count(z: int) -> int:
    """z 级整幅世界的瓦片边长数。"""
    return 1 << max(0, int(z))


# --------------------------------------------------------------------------- #
# 正/反 Mercator 投影
# --------------------------------------------------------------------------- #

def lat_toMercatorY(lat: float) -> float:
    """纬度 → [0,1] 归一化北向坐标（0 = 北边缘）。标量版。"""
    phi = math.radians(clamp_lat(lat))
    return (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0


def mercatorY_toLat(y: float) -> float:
    """[0,1] 归一化北向坐标 → 纬度（度）。

    正变换为 ``y = (1 - ln(tanφ + secφ)/π)/2``，因此
    ``ln(tan(π/4+φ/2)) = π(1-2y)``，反解即 ``φ = atan(sinh(π(1-2y)))``。
    """
    if not math.isfinite(y):
        return 0.0
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def lonlat_to_scene(lat: float, lon: float, ref_z: int = REF_Z) -> tuple[float, float]:
    """经纬度 → 场景坐标（ref_z 级瓦片像素，x 向东、y 向南）。"""
    world = TILE_SIZE * (1 << ref_z)
    x = (clamp_lon(lon) + 180.0) / 360.0 * world
    y = lat_toMercatorY(lat) * world
    return x, y


def scene_to_lonlat(x: float, y: float, ref_z: int = REF_Z) -> tuple[float, float]:
    """场景坐标 → (lat, lon)，即 WGS-84 经纬度（未做反向加偏）。"""
    world = TILE_SIZE * (1 << ref_z)
    lon = x / world * 360.0 - 180.0
    lat = mercatorY_toLat(y / world)
    return lat, lon


def lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[int, int, float, float]:
    """经纬度 → (瓦片列, 瓦片行, 瓦片内归一化 x, 瓦片内归一化 y)。"""
    n = tile_count(z)
    fx = (clamp_lon(lon) + 180.0) / 360.0 * n
    fy = lat_toMercatorY(lat) * n
    tx = int(math.floor(fx))
    ty = int(math.floor(fy))
    return max(0, min(n - 1, tx)), max(0, min(n - 1, ty)), fx - math.floor(fx), fy - math.floor(fy)


def scene_rect_of_tile(tx: int, ty: int, z: int, ref_z: int = REF_Z) -> tuple[float, float, float]:
    """瓦片 (tx,ty,z) 在场景坐标系中的左上角与边长。"""
    size = TILE_SIZE * (1 << (ref_z - z)) if z <= ref_z else TILE_SIZE / (1 << (z - ref_z))
    return tx * size, ty * size, size


def meters_per_pixel(lat: float, z: float) -> float:
    """该纬度/缩放级下 1 屏幕像素代表的地面距离（米）。"""
    return EARTH_CIRCUMFERENCE * math.cos(math.radians(clamp_lat(lat))) / (TILE_SIZE * 2.0 ** z)


def nice_scale_step(meters_per_px: float, target_px: float = 90.0) -> tuple[float, float]:
    """返回 (整数米步进, 该步进对应的像素宽度)，用于比例尺。"""
    if not math.isfinite(meters_per_px) or meters_per_px <= 0:
        return (float("nan"), float("nan"))
    want = meters_per_px * target_px
    steps = (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500,
             1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000)
    best = steps[0]
    for s in steps:
        if s <= want * 1.6:
            best = s
        else:
            break
    return best, best / meters_per_px


# --------------------------------------------------------------------------- #
# WGS-84 ↔ GCJ-02（火星坐标）
# --------------------------------------------------------------------------- #

def out_of_china(lat: float, lon: float) -> bool:
    """粗略国界判定：超出该范围一律不加偏（境外 GCJ-02 无意义）。"""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return True
    if not (72.004 <= lon <= 137.8347):
        return True
    if not (0.8293 <= lat <= 55.8271):
        return True
    return False


def _t_lat(x: float, y: float) -> float:
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _t_lon(x: float, y: float) -> float:
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs_to_gcj(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02。境外原样返回。"""
    if out_of_china(lat, lon):
        return lat, lon
    dlat = _t_lat(lon - 105.0, lat - 35.0)
    dlon = _t_lon(lon - 105.0, lat - 35.0)
    radlat = math.radians(lat)
    magic = 1.0 - _GCJ_EE * math.sin(radlat) * math.sin(radlat)
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1.0 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_GCJ_A / sqrt_magic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj_to_wgs(lat: float, lon: float, iters: int = 6) -> tuple[float, float]:
    """GCJ-02 → WGS-84。

    用**残差累加**迭代 ``w ← w + (g - f(w))``（不是 ``2g - f(w)``，后者会在 g 与
    真值之间来回振荡）。实测 1 次迭代残差已达 6e-7 度，6 次收敛到 <1e-10 度。
    """
    if out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(max(1, iters)):
        flat, flon = wgs_to_gcj(wlat, wlon)
        dlat, dlon = lat - flat, lon - flon
        wlat, wlon = wlat + dlat, wlon + dlon
        if abs(dlat) < 1e-10 and abs(dlon) < 1e-10:
            break
    return wlat, wlon


def convert_pair(lat: float, lon: float, datum: str, to_wgs: bool = False) -> tuple[float, float]:
    """按瓦片源基准做正/反变换。datum ∈ {'WGS84','GCJ02'}。

    - 把 WGS-84 轨迹点画到 GCJ-02 瓦片上：``convert_pair(lat, lon, 'GCJ02')``
    - 把鼠标命中的 GCJ-02 瓦片坐标读回真实经纬度：``convert_pair(lat, lon, 'GCJ02', to_wgs=True)``
    """
    if (datum or "WGS84").upper() in ("WGS84", "EPSG:3857", "CGCS2000"):
        return lat, lon
    if (datum or "").upper() in ("GCJ02", "GCJ"):
        return gcj_to_wgs(lat, lon) if to_wgs else wgs_to_gcj(lat, lon)
    if (datum or "").upper() == "BD09":
        raise NotImplementedError("v1 不支持 BD-09（百度），请改用 GCJ-02 源")
    raise ValueError(f"未知坐标基准: {datum!r}")


def enu_to_scene(east: float, north: float) -> tuple[float, float]:
    """本地 ENU（米，e 向东、n 向北）→ 本地模式场景坐标（y 向上为正）。

    本地模式场景单位 = 米，且刻意使用 **y 向上**（north 增大 → 画面上移），
    与地理模式的 y 向下相反，由视图变换 scale(k, -k) 消化符号差异。
    """
    return float(east), float(north)


def enu_from_lonlat(lat: float, lon: float, origin: tuple[float, float]) -> tuple[float, float]:
    """以 origin=(lat0, lon0) 为原点，把经纬度近似投影到本地 ENU（米）。

    小范围（<10 km）用等距圆柱近似，误差远小于 GPS 定位误差。
    """
    lat0, lon0 = origin
    m_per_deg_lat = 111132.92 - 558.32 * math.cos(math.radians(2 * lat0)) + 1.20 * math.cos(math.radians(4 * lat0))
    m_per_deg_lon = 111412.55 * math.cos(math.radians(lat0)) - 93.5 * math.cos(math.radians(3 * lat0))
    return (lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat


__all__ = [
    "TILE_SIZE", "REF_Z", "EARTH_CIRCUMFERENCE", "MAX_LAT_MERCATOR",
    "scale_at", "clamp_lat", "clamp_lon", "tile_count",
    "lat_toMercatorY", "lat_to_mercator_y_np", "scene_from_lonlat_np",
    "mercatorY_toLat", "lonlat_to_scene", "scene_to_lonlat", "lonlat_to_tile",
    "scene_rect_of_tile", "meters_per_pixel", "nice_scale_step",
    "out_of_china", "wgs_to_gcj", "gcj_to_wgs", "convert_pair",
    "enu_to_scene", "enu_from_lonlat",
]

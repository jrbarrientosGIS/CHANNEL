# -*- coding: utf-8 -*-
import locale
from functools import cmp_to_key
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from shapely.wkb import loads
import numpy as np
from qgis.core import QgsProject, Qgis, QgsPointXY, QgsWkbTypes, QgsCoordinateTransform


EPS = 1e-9


def frange(start, end, step):
    while start < end:
        yield start
        start += step



def _sorted_locale(values):
    try:
        return sorted(values, key=cmp_to_key(locale.strcoll))
    except Exception:
        return sorted(values)



def _project_layers():
    return list(QgsProject.instance().mapLayers().values())



def getLineLayerNames():
    layer_names = []
    for layer in _project_layers():
        if layer.type() == Qgis.LayerType.Vector and QgsWkbTypes.geometryType(layer.wkbType()) == Qgis.GeometryType.Line and int(layer.featureCount()) == 1:
            srs = layer.crs().authid()
            layer_names.append(f"{layer.name()} {srs}")
    return _sorted_locale(layer_names)



def getRasterLayerNames():
    layer_names = []
    for layer in _project_layers():
        if layer.type() == Qgis.LayerType.Raster and layer.providerType() != 'wms':
            srs = layer.crs().authid()
            layer_names.append(f"{layer.name()} {srs}")
    return _sorted_locale(layer_names)



def getVectorLayerByName(layerName):
    for layer in _project_layers():
        if layer.type() == Qgis.LayerType.Vector and layer.name() == layerName:
            return layer if layer.isValid() else None
    return None



def getRasterLayerByName(layerName):
    for layer in _project_layers():
        if layer.type() == Qgis.LayerType.Raster and layer.name() == layerName:
            return layer if layer.isValid() else None
    return None



def valRaster(x, y, rLayer):
    if rLayer is None or not rLayer.isValid():
        return None
    point = QgsPointXY(float(x), float(y))
    provider = rLayer.dataProvider()
    try:
        identify_result = provider.identify(point, Qgis.RasterIdentifyFormat.Value)
    except Exception:
        return None

    try:
        if hasattr(identify_result, 'isValid') and not identify_result.isValid():
            return None
        results = identify_result.results() if hasattr(identify_result, 'results') else identify_result
    except Exception:
        return None

    if not isinstance(results, dict) or not results:
        return None

    value = None
    for band_key in sorted(results.keys()):
        if results.get(band_key) is not None:
            value = results.get(band_key)
            break
    if value is None:
        return None

    try:
        value = float(value)
    except Exception:
        return None

    try:
        if np.isnan(value):
            return None
    except Exception:
        pass

    try:
        nodata = provider.sourceNoDataValue(1)
        if nodata is not None and np.isfinite(nodata) and abs(value - float(nodata)) <= EPS:
            return None
    except Exception:
        pass

    return value




def _iter_feature_geometries(vLayer):
    """Yield non-empty QgsGeometry objects from selected features first, then all features.

    The original plugin assumed the first feature was the centerline. In real QGIS
    editing sessions a scratch layer may contain an empty/null feature before the
    valid axis, or the user may have selected the intended feature. This iterator
    makes the centerline resolver tolerant without changing the UI workflow.
    """
    if vLayer is None or not vLayer.isValid():
        return

    seen = set()
    try:
        selected = list(vLayer.selectedFeatures())
    except Exception:
        selected = []

    for feature in selected:
        try:
            fid = int(feature.id())
            seen.add(fid)
            geom = feature.geometry()
            if geom is not None and not geom.isEmpty():
                yield geom
        except Exception:
            pass

    try:
        features = vLayer.getFeatures()
    except Exception:
        features = []

    for feature in features:
        try:
            fid = int(feature.id())
            if fid in seen:
                continue
            geom = feature.geometry()
            if geom is not None and not geom.isEmpty():
                yield geom
        except Exception:
            continue


def _ensure_linestring(shapely_geom):
    """Return a usable Shapely LineString from common QGIS line geometries."""
    if shapely_geom is None:
        return None

    geom_type = getattr(shapely_geom, 'geom_type', '')

    if geom_type == 'LineString':
        return shapely_geom if shapely_geom.length > EPS else None

    if geom_type == 'MultiLineString' or isinstance(shapely_geom, MultiLineString):
        parts = [g for g in getattr(shapely_geom, 'geoms', []) if getattr(g, 'length', 0.0) > EPS]
        if not parts:
            return None
        try:
            merged = linemerge(MultiLineString(parts))
            if getattr(merged, 'geom_type', '') == 'LineString' and merged.length > EPS:
                return merged
            if getattr(merged, 'geom_type', '') == 'MultiLineString':
                return max(list(merged.geoms), key=lambda geom: geom.length)
        except Exception:
            return max(parts, key=lambda geom: geom.length)

    if geom_type == 'GeometryCollection':
        lines = []
        for g in getattr(shapely_geom, 'geoms', []):
            line = _ensure_linestring(g)
            if line is not None and line.length > EPS:
                lines.append(line)
        if not lines:
            return None
        try:
            merged = linemerge(MultiLineString(lines))
            return _ensure_linestring(merged)
        except Exception:
            return max(lines, key=lambda geom: geom.length)

    return None


def _line_from_qgis_geometry(qgs_geom):
    """Robustly convert QgsGeometry line/curve/multipart into Shapely LineString."""
    if qgs_geom is None or qgs_geom.isEmpty():
        return None

    try:
        geom_obj = qgs_geom.constGet()
        if geom_obj is not None and QgsWkbTypes.isCurvedType(geom_obj.wkbType()):
            qgs_geom = qgs_geom.segmentize()
    except Exception:
        pass

    candidate_lines = []

    # Native QGIS conversion first. This handles most 2D/3D line and multiline
    # geometries and avoids brittle WKB/Shapely conversion edge cases.
    try:
        pts = qgs_geom.asPolyline()
        if pts and len(pts) >= 2:
            candidate_lines.append(LineString([(float(p.x()), float(p.y())) for p in pts]))
    except Exception:
        pass

    try:
        multi = qgs_geom.asMultiPolyline()
        for part in multi or []:
            if part and len(part) >= 2:
                candidate_lines.append(LineString([(float(p.x()), float(p.y())) for p in part]))
    except Exception:
        pass

    # Generic QGIS vertex iterator fallback. This is deliberately permissive for
    # temporary/editing layers that advertise odd WKB variants, Z/M dimensions or
    # segmentized curves. It preserves only XY, which is exactly what the DEM
    # sampler needs.
    try:
        verts = []
        for v in qgs_geom.vertices():
            verts.append((float(v.x()), float(v.y())))
        # Remove consecutive duplicate vertices.
        cleaned = []
        for xy in verts:
            if not cleaned or (abs(cleaned[-1][0] - xy[0]) > EPS or abs(cleaned[-1][1] - xy[1]) > EPS):
                cleaned.append(xy)
        if len(cleaned) >= 2:
            candidate_lines.append(LineString(cleaned))
    except Exception:
        pass

    # Fallback for any remaining valid WKB geometry that Shapely can understand.
    try:
        line = _ensure_linestring(loads(bytes(qgs_geom.asWkb())))
        if line is not None:
            candidate_lines.append(line)
    except Exception:
        pass

    candidate_lines = [line for line in candidate_lines if line is not None and line.length > EPS]
    if not candidate_lines:
        return None
    if len(candidate_lines) == 1:
        return candidate_lines[0]

    try:
        merged = linemerge(MultiLineString(candidate_lines))
        line = _ensure_linestring(merged)
        if line is not None:
            return line
    except Exception:
        pass
    return max(candidate_lines, key=lambda geom: geom.length)




def _to_raster_point(x, y, vLayer, rLayer):
    """Return a point in raster layer CRS for reliable DEM sampling."""
    pt = QgsPointXY(float(x), float(y))
    try:
        if vLayer is not None and rLayer is not None and vLayer.isValid() and rLayer.isValid() and vLayer.crs() != rLayer.crs():
            tr = QgsCoordinateTransform(vLayer.crs(), rLayer.crs(), QgsProject.instance())
            pt = tr.transform(pt)
    except Exception:
        pass
    return pt

def valRasterLayerPoint(x, y, vLayer, rLayer):
    pt = _to_raster_point(x, y, vLayer, rLayer)
    return valRaster(pt.x(), pt.y(), rLayer)

def getCenterlineShape(vLayer):
    """Resolve the selected channel centerline as one usable LineString.

    Behaviour in r5.4.6:
    - Prefer selected line features when the user selected one or more axes.
    - Skip null/empty features instead of failing on the first feature.
    - Accept singlepart, multipart and curved geometries.
    - If several valid line parts exist, merge them where possible; otherwise use
      the longest valid line part. This keeps the plot alive in scratch layers.
    """
    candidate_lines = []
    for geom in _iter_feature_geometries(vLayer):
        line = _line_from_qgis_geometry(geom)
        if line is not None and line.length > EPS:
            candidate_lines.append(line)

    if not candidate_lines:
        raise ValueError('Selected centerline layer has no usable line geometry. Select or create one non-empty line feature in the centerline layer.')

    if len(candidate_lines) == 1:
        return candidate_lines[0]

    try:
        merged = linemerge(MultiLineString(candidate_lines))
        line = _ensure_linestring(merged)
        if line is not None:
            return line
    except Exception:
        pass
    return max(candidate_lines, key=lambda geom: geom.length)

def getVectorLineLength(vLayer):
    line = getCenterlineShape(vLayer)
    return float(line.length) if line is not None else 0.0


def getNativeVectorLineLength(vLayer):
    """Last-resort QGIS-native length for UI fallbacks.

    This avoids a blank profile panel when a scratch/edit layer temporarily has a
    geometry representation that Shapely refuses but QGIS can still measure.
    """
    if vLayer is None or not vLayer.isValid():
        return 0.0
    lengths = []
    for geom in _iter_feature_geometries(vLayer):
        try:
            g = geom
            try:
                obj = g.constGet()
                if obj is not None and QgsWkbTypes.isCurvedType(obj.wkbType()):
                    g = g.segmentize()
            except Exception:
                pass
            length = float(g.length())
            if length > EPS:
                lengths.append(length)
        except Exception:
            pass
    if not lengths:
        return 0.0
    return max(lengths)



def buildStationList(total_length, res):
    if total_length <= 0:
        return np.array([0.0])
    stations = np.arange(0.0, total_length, max(float(res), EPS), dtype=float)
    if stations.size == 0 or stations[0] != 0.0:
        stations = np.insert(stations, 0, 0.0)
    if abs(stations[-1] - total_length) > EPS:
        stations = np.append(stations, total_length)
    return stations



def normalizeProfilePoints(profile_points, total_length, tolerance=EPS):
    points = [(float(station), float(elev)) for station, elev in profile_points]
    points.sort(key=lambda item: item[0])
    normalized = []
    for station, elev in points:
        if station < -tolerance or station > total_length + tolerance:
            raise ValueError('Grade control point station is outside the channel length.')
        station = max(0.0, min(total_length, station))
        if normalized and abs(normalized[-1][0] - station) <= tolerance:
            raise ValueError('There are repeated grade control stations.')
        normalized.append((station, elev))
    return normalized



def interpolateProfile(profile_points, stations):
    x = np.array([point[0] for point in profile_points], dtype=float)
    y = np.array([point[1] for point in profile_points], dtype=float)
    return np.interp(np.array(stations, dtype=float), x, y)



def sampleLineAtStations(line_shape, stations, z_values=None, reference_length=None):
    if reference_length is None:
        reference_length = float(stations[-1]) if len(stations) else 0.0
    geom_length = float(line_shape.length)
    if geom_length <= 0 or reference_length <= 0:
        fractions = np.zeros(len(stations), dtype=float)
    else:
        fractions = np.array(stations, dtype=float) / float(reference_length)
    x = []
    y = []
    for fraction in fractions:
        distance = min(max(fraction, 0.0), 1.0) * geom_length
        point = line_shape.interpolate(distance)
        x.append(point.x)
        y.append(point.y)
    z = list(z_values) if z_values is not None else []
    return [x, y, z, list(np.array(stations, dtype=float))]



def _prepare_offset_line(line_shape, offset_distance, side):
    offset = line_shape.parallel_offset(offset_distance, side)
    offset = _ensure_linestring(offset)
    coords = list(offset.coords)
    if side == 'right':
        coords = coords[::-1]
        offset = type(offset)(coords)
    return offset



def formatProfilePoints(profile_points):
    return '; '.join(['{0:.2f}:{1:.2f}'.format(station, elev) for station, elev in profile_points])



def _fill_missing_profile_values(stations, values):
    stations = np.array(stations, dtype=float)
    arr = np.array([np.nan if value is None else float(value) for value in values], dtype=float)
    valid = np.isfinite(arr)
    if valid.sum() == 0:
        raise ValueError('No DEM samples were found along the selected centerline.')
    if valid.sum() == 1:
        arr[~valid] = arr[valid][0]
        return arr.tolist()
    arr[~valid] = np.interp(stations[~valid], stations[valid], arr[valid])
    return arr.tolist()



def sampleProfileElevations(vLayer, rLayer, stations):
    line = getCenterlineShape(vLayer)
    total_length = float(line.length)
    sampled = []
    for station in stations:
        if total_length <= 0:
            point = line.interpolate(0)
        else:
            point = line.interpolate(max(0.0, min(total_length, float(station))))
        sampled.append(valRasterLayerPoint(point.x, point.y, vLayer, rLayer))
    return _fill_missing_profile_values(stations, sampled)



def isVectorWithinRasterDomain(vLayer, rLayer, samples=256):
    line = getCenterlineShape(vLayer)
    if line is None or rLayer is None or not rLayer.isValid():
        return False
    total_length = float(line.length)
    sample_count = max(2, int(samples))
    valid = 0
    for station in np.linspace(0.0, total_length, sample_count):
        point = line.interpolate(float(station))
        if valRasterLayerPoint(point.x, point.y, vLayer, rLayer) is not None:
            valid += 1
    return valid >= max(2, int(sample_count * 0.98))



def hasProfileCoverage(vLayer, rLayer, samples=64):
    line = getCenterlineShape(vLayer)
    if line is None or rLayer is None or not rLayer.isValid():
        return False
    total_length = float(line.length)
    sample_count = max(2, int(samples))
    valid = 0
    for station in np.linspace(0.0, total_length, sample_count):
        point = line.interpolate(float(station))
        if valRasterLayerPoint(point.x, point.y, vLayer, rLayer) is not None:
            valid += 1
            if valid >= 2:
                return True
    return False



def calcElev(self):
    line = getCenterlineShape(self.vLayer)
    if line is None:
        return [None, None]
    startPoint = line.interpolate(0)
    endPoint = line.interpolate(line.length)
    startPointZdem = valRasterLayerPoint(startPoint.x, startPoint.y, self.vLayer, self.rLayer)
    endPointZdem = valRasterLayerPoint(endPoint.x, endPoint.y, self.vLayer, self.rLayer)
    if startPointZdem is None:
        self.labelStartDepth.setText('Start point outside of raster')
    if endPointZdem is None:
        self.labelEndDepth.setText('End point outside of raster')
    return [startPointZdem, endPointZdem]



def getPlotArray(vLayer, rLayer, profile_points, res):
    clSHP = getCenterlineShape(vLayer)
    total_length = float(clSHP.length)
    profile_points = normalizeProfilePoints(profile_points, total_length)
    station = buildStationList(total_length, res)
    zProp = interpolateProfile(profile_points, station)
    pClXYZd = sampleLineAtStations(clSHP, station, zProp, reference_length=total_length)
    zExisting = []
    for x, y in zip(pClXYZd[0], pClXYZd[1]):
        value = valRasterLayerPoint(x, y, vLayer, rLayer)
        zExisting.append(np.nan if value is None else float(value))
    zExisting = np.array(zExisting, dtype=float)
    valid = np.isfinite(zExisting)
    stations_arr = np.array(station, dtype=float)
    zprop_arr = np.array(zProp, dtype=float)
    x_arr = np.array(pClXYZd[0], dtype=float)
    y_arr = np.array(pClXYZd[1], dtype=float)
    if valid.sum() >= 2:
        return np.array([stations_arr[valid], zExisting[valid], zprop_arr[valid], x_arr[valid], y_arr[valid]])

    # No/insufficient DEM values, but the profile control line is still useful.
    # Return a drawable synthetic terrain coincident with grade profile instead
    # of raising and leaving Matplotlib blank.
    return np.array([stations_arr, zprop_arr, zprop_arr, x_arr, y_arr])



def depthQMLwriter(maxVal):
    xmlTemplate = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.44.0" minimumScale="0" maximumScale="1e+08" hasScaleBasedVisibilityFlag="0">
  <pipe>
    <rasterrenderer opacity="1" alphaBand="-1" classificationMax="{0:.02f}" classificationMinMaxOrigin="User" band="1" classificationMin="0" type="singlebandpseudocolor">
      <rasterTransparency/>
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" clip="0">
          <item alpha="255" value="0" label="0.00" color="#fff5eb"/>
          <item alpha="255" value="{1:.02f}" label="{1:.02f}" color="#fee6cf"/>
          <item alpha="255" value="{2:.02f}" label="{2:.02f}" color="#fdd1a5"/>
          <item alpha="255" value="{3:.02f}" label="{3:.02f}" color="#fdb171"/>
          <item alpha="255" value="{4:.02f}" label="{4:.02f}" color="#fd9243"/>
          <item alpha="255" value="{5:.02f}" label="{5:.02f}" color="#f36f1a"/>
          <item alpha="255" value="{6:.02f}" label="{6:.02f}" color="#de4f05"/>
          <item alpha="255" value="{7:.02f}" label="{7:.02f}" color="#b03902"/>
          <item alpha="255" value="{0:.02f}" label="{0:.02f}" color="#7f2704"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0"/>
    <huesaturation colorizeGreen="3" colorizeOn="0" colorizeRed="240" colorizeBlue="3" grayscaleMode="0" saturation="0" colorizeStrength="100"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
""".format(maxVal,maxVal*1/8,maxVal*2/8,maxVal*3/8,maxVal*4/8,maxVal*5/8,maxVal*6/8,maxVal*7/8)
    return xmlTemplate



def calcDepth(self):
    startPointZdem, endPointZdem = calcElev(self)
    startDepth = None if startPointZdem is None else startPointZdem - self.spinElevStart.value()
    endDepth = None if endPointZdem is None else endPointZdem - self.spinElevEnd.value()

    if startDepth is None:
        self.labelStartDepth.setText('Start point outside of raster')
    else:
        self.labelStartDepth.setText('Start Depth = {:.2f}'.format(startDepth))

    if endDepth is None:
        self.labelEndDepth.setText('End point outside of raster')
    else:
        self.labelEndDepth.setText('End Depth = {:.2f}'.format(endDepth))

    if startDepth is not None and endDepth is not None:
        self.btn1Dsave.setEnabled(True)
        return True

    self.btnOk.setEnabled(False)
    return False



def elevationSampler(vectSHP, res, raster):
    x = []
    y = []
    z = []
    dist = []
    vectLength = vectSHP.length
    for currentDist in buildStationList(vectLength, res):
        point = vectSHP.interpolate(float(currentDist))
        xp, yp = point.x, point.y
        x.append(xp)
        y.append(yp)
        zp = valRaster(xp, yp, raster)
        z.append(np.nan if zp is None else zp)
        dist.append(float(currentDist))
    return [x, y, z, dist]



def Zcalc(vectSHP, zStart, zPropSlope, res):
    stations = buildStationList(vectSHP.length, res)
    z = [zStart - currentDist * zPropSlope for currentDist in stations]
    return sampleLineAtStations(vectSHP, stations, z, reference_length=vectSHP.length)



def _sample_centerline_xy(line_shape, stations, reference_length=None):
    if reference_length is None:
        reference_length = float(stations[-1]) if len(stations) else 0.0
    geom_length = float(line_shape.length)
    x = []
    y = []
    for station in np.array(stations, dtype=float):
        if reference_length <= 0 or geom_length <= 0:
            distance = 0.0
        else:
            distance = min(max(float(station), 0.0), float(reference_length)) / float(reference_length) * geom_length
        point = line_shape.interpolate(distance)
        x.append(point.x)
        y.append(point.y)
    return np.array(x, dtype=float), np.array(y, dtype=float)


def _line_normals_at_stations(line_shape, stations, reference_length=None):
    if reference_length is None:
        reference_length = float(stations[-1]) if len(stations) else 0.0
    geom_length = float(line_shape.length)
    if geom_length <= 0:
        n = len(stations)
        return np.zeros(n, dtype=float), np.ones(n, dtype=float)

    step = max(min(geom_length / max(len(stations), 2), geom_length / 50.0), 1e-6)
    nx = []
    ny = []
    for station in np.array(stations, dtype=float):
        if reference_length <= 0:
            dist = 0.0
        else:
            dist = min(max(float(station), 0.0), float(reference_length)) / float(reference_length) * geom_length
        d0 = max(0.0, dist - step)
        d1 = min(geom_length, dist + step)
        if d1 <= d0:
            d0 = max(0.0, dist - 1e-6)
            d1 = min(geom_length, dist + 1e-6)
        p0 = line_shape.interpolate(d0)
        p1 = line_shape.interpolate(d1)
        dx = float(p1.x - p0.x)
        dy = float(p1.y - p0.y)
        norm = float(np.hypot(dx, dy))
        if norm <= EPS:
            dx, dy, norm = 1.0, 0.0, 1.0
        nx.append(-dy / norm)
        ny.append(dx / norm)
    return np.array(nx, dtype=float), np.array(ny, dtype=float)


def _offset_profile_from_centerline(line_shape, stations, offsets, z_values, reference_length=None):
    x_center, y_center = _sample_centerline_xy(line_shape, stations, reference_length=reference_length)
    nx, ny = _line_normals_at_stations(line_shape, stations, reference_length=reference_length)
    offsets = np.array(offsets, dtype=float)
    x = (x_center + nx * offsets).tolist()
    y = (y_center + ny * offsets).tolist()
    z = list(np.array(z_values, dtype=float)) if z_values is not None else []
    return [x, y, z, list(np.array(stations, dtype=float))]


def channelPoints(vLayer, raster, profile_points, width, leftSideSlope, rightSideSlope, bankWidth, res):
    "Returns xyz and station distance list for channel centerline, toe slopes, and top slopes at vertices for specified elevation, width, and side slope"
    clSHP = getCenterlineShape(vLayer)
    total_length = float(clSHP.length)
    profile_points = normalizeProfilePoints(profile_points, total_length)
    stations = buildStationList(total_length, res)
    z_center = interpolateProfile(profile_points, stations)

    pClXYZd = sampleLineAtStations(clSHP, stations, z_center, reference_length=total_length)

    lToeXYZd = _offset_profile_from_centerline(
        clSHP, stations, np.full(len(stations), width / 2.0, dtype=float), z_center, reference_length=total_length
    )
    rToeXYZd = _offset_profile_from_centerline(
        clSHP, stations, np.full(len(stations), -width / 2.0, dtype=float), z_center, reference_length=total_length
    )
    lTopXYZd = _offset_profile_from_centerline(
        clSHP, stations, np.full(len(stations), width / 2.0 + bankWidth, dtype=float),
        z_center + bankWidth / leftSideSlope, reference_length=total_length
    )
    rTopXYZd = _offset_profile_from_centerline(
        clSHP, stations, np.full(len(stations), -(width / 2.0 + bankWidth), dtype=float),
        z_center + bankWidth / rightSideSlope, reference_length=total_length
    )

    x = lTopXYZd[0] + lToeXYZd[0] + pClXYZd[0] + rToeXYZd[0] + rTopXYZd[0]
    y = lTopXYZd[1] + lToeXYZd[1] + pClXYZd[1] + rToeXYZd[1] + rTopXYZd[1]
    z = lTopXYZd[2] + lToeXYZd[2] + pClXYZd[2] + rToeXYZd[2] + rTopXYZd[2]

    if width / 2 >= res:
        for offset in frange(res, width / 2.0, res):
            leftStepXYZd = _offset_profile_from_centerline(
                clSHP, stations, np.full(len(stations), offset, dtype=float), z_center, reference_length=total_length
            )
            rightStepXYZd = _offset_profile_from_centerline(
                clSHP, stations, np.full(len(stations), -offset, dtype=float), z_center, reference_length=total_length
            )
            x = x + leftStepXYZd[0] + rightStepXYZd[0]
            y = y + leftStepXYZd[1] + rightStepXYZd[1]
            z = z + leftStepXYZd[2] + rightStepXYZd[2]

    for offset in frange(res, bankWidth, res):
        leftStepXYZd = _offset_profile_from_centerline(
            clSHP, stations, np.full(len(stations), width / 2.0 + offset, dtype=float),
            z_center + offset / leftSideSlope, reference_length=total_length
        )
        rightStepXYZd = _offset_profile_from_centerline(
            clSHP, stations, np.full(len(stations), -(width / 2.0 + offset), dtype=float),
            z_center + offset / rightSideSlope, reference_length=total_length
        )
        x = x + leftStepXYZd[0] + rightStepXYZd[0]
        y = y + leftStepXYZd[1] + rightStepXYZd[1]
        z = z + leftStepXYZd[2] + rightStepXYZd[2]

    return pClXYZd, lToeXYZd, rToeXYZd, lTopXYZd, rTopXYZd, x, y, z

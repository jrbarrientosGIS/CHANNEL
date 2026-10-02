# -*- coding: utf-8 -*-
try:
    from shapely.geometry import Polygon, MultiPolygon
except ImportError as _shapely_import_error:
    Polygon = None
    MultiPolygon = tuple()
    SHAPELY_IMPORT_ERROR = _shapely_import_error
else:
    SHAPELY_IMPORT_ERROR = None
from osgeo import ogr, gdal
import numpy as np
from qgis.PyQt.QtCore import QObject, pyqtSignal
import traceback
import time
from . import DrainageChannelBuilder_utils as utils
from . import GdalTools_utils as gdalUtils
import os
import platform
import subprocess
import sys
import shlex
import re
import math
try:
    from osgeo_utils import gdal_calc
except Exception:
    gdal_calc = None


def _safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "")
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "layer"


def _run_cmd(cmd, progress_signal, step_name):
    """Run GDAL/OGR commands safely with Windows paths containing spaces.

    Accepts either a list/tuple of arguments or, for legacy callers, a string.
    String commands are split without using shell=True.
    """
    cmd_args = list(cmd) if isinstance(cmd, (list, tuple)) else shlex.split(str(cmd), posix=(platform.system() != 'Windows'))
    progress_signal.emit('\n{}: {}\n'.format(step_name, ' '.join('"{}"'.format(c) if ' ' in str(c) else str(c) for c in cmd_args)))
    result = subprocess.run(
        cmd_args,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{step_name} failed with exit code {result.returncode}.\n"
            f"Command: {' '.join(map(str, cmd_args))}\nOutput:\n{result.stdout}"
        )
    if result.stdout:
        progress_signal.emit("\n" + result.stdout.strip() + "\n")
    return result


def _run_gdal_calc(calc, outfile, a_path, b_path, progress_signal, step_name):
    """Execute gdal_calc inside the current QGIS Python process.

    This avoids using sys.executable. In some QGIS for Windows installs,
    sys.executable resolves to qgis/qgis-ltr-bin.exe and subprocess calls
    can open a second QGIS session instead of running a headless Python calc.
    """
    if gdal_calc is None:
        raise RuntimeError('osgeo_utils.gdal_calc is not available in this QGIS Python environment.')
    progress_signal.emit(f'\n{step_name}: internal osgeo_utils.gdal_calc -> {outfile}\n')
    try:
        gdal_calc.Calc(
            calc=calc,
            outfile=outfile,
            A=a_path,
            B=b_path,
            overwrite=True,
            NoDataValue=0,
            quiet=True,
        )
    except TypeError:
        # Compatibility fallback for older osgeo_utils signatures.
        gdal_calc.Calc(
            calc=calc,
            outfile=outfile,
            A=a_path,
            B=b_path,
            overwrite=True,
            NoDataValue=0,
        )


def _ensure_file_exists(path, label):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{label} was not created: {path}")


def _aligned_extent_to_raster(bounds, raster_path, fallback_res):
    """Snap an extent to the DEM pixel grid to avoid half-cell XY shifts."""
    x_min, y_min, x_max, y_max = [float(v) for v in bounds]
    ds = gdal.Open(raster_path)
    if ds is None:
        return x_min, y_min, x_max, y_max, float(fallback_res), float(fallback_res)

    gt = ds.GetGeoTransform()
    ds = None
    if gt is None:
        return x_min, y_min, x_max, y_max, float(fallback_res), float(fallback_res)

    origin_x = float(gt[0])
    origin_y = float(gt[3])
    pixel_w = abs(float(gt[1])) if gt[1] not in (None, 0) else float(fallback_res)
    pixel_h = abs(float(gt[5])) if gt[5] not in (None, 0) else float(fallback_res)

    x_min_aligned = origin_x + math.floor((x_min - origin_x) / pixel_w) * pixel_w
    x_max_aligned = origin_x + math.ceil((x_max - origin_x) / pixel_w) * pixel_w
    y_max_aligned = origin_y - math.floor((origin_y - y_max) / pixel_h) * pixel_h
    y_min_aligned = origin_y - math.ceil((origin_y - y_min) / pixel_h) * pixel_h

    return x_min_aligned, y_min_aligned, x_max_aligned, y_max_aligned, pixel_w, pixel_h

class DrainageChannelBuilder(QObject):
    workerFinished = pyqtSignal(list)
    workerInterrupted = pyqtSignal()
    updateProgressText = pyqtSignal(str)
    error = pyqtSignal(object, str)

    def __init__(self, args):
        super().__init__()
        self.killed = False
        (self.rLayer, self.vLayer, self.width, self.res, self.profilePoints,
         self.leftSideSlope, self.rightSideSlope,
         self.bankWidth, self.dirName) = args
        gdalUtils.setProcessEnvironment(QObject)

    def run(self):
        t0 = time.time()
        self.values = None
        self.updateProgressText.emit('starting')
        success = False
        try:
            self.DrainageChannelWorker()
            success = True
        except Exception as e:
            self.error.emit(e, traceback.format_exc())
        finally:
            tdelta = (time.time() - t0) / 60.0
            if success:
                self.workerFinished.emit([self.values])
                self.updateProgressText.emit('\nFinished - Runtime was {:6.2f} minutes\n'.format(tdelta))
            else:
                self.workerFinished.emit([])
                self.updateProgressText.emit('\nStopped after error - Runtime was {:6.2f} minutes\n'.format(tdelta))

    def kill(self):
        self.killed = True

    def DrainageChannelWorker(self):
        if SHAPELY_IMPORT_ERROR is not None:
            raise ImportError('The Python package shapely is required by Omicron-Channel but is not available in this QGIS Python environment.') from SHAPELY_IMPORT_ERROR
        home = os.path.expanduser('~')
        if self.dirName == '':
            self.dirName = os.path.join(home, 'Desktop', 'QGIS2OmicronChannelFiles')
            os.makedirs(self.dirName, exist_ok=True)
        self.dirName = os.path.normpath(self.dirName)
        os.makedirs(self.dirName, exist_ok=True)
        epsgCode = self.vLayer.crs().authid()
        fileDEM = self.rLayer.dataProvider().dataSourceUri().split('|')[0]
        safe_vlayer_name = _safe_name(self.vLayer.name())
        safe_rlayer_name = _safe_name(self.rLayer.name())
        self.updateProgressText.emit('\nCreating point grid of synthetic channel\n')
        pCL, lToe, rToe, lTop, rTop, x, y, z = utils.channelPoints(
            self.vLayer, self.rLayer, self.profilePoints, self.width,
            self.leftSideSlope, self.rightSideSlope, self.bankWidth, self.res)
        XYZ = list(zip(x, y, z))
        polyCoords = list(zip(lTop[0] + rTop[0][::-1], lTop[1] + rTop[1][::-1]))
        polyShap = Polygon(polyCoords)
        if not polyShap.is_valid:
            self.updateProgressText.emit('\nChannel cutline polygon was invalid; repairing geometry before clipping\n')
            poly_fixed = polyShap.buffer(0)
            if poly_fixed.is_empty:
                raise RuntimeError('Unable to repair invalid channel cutline polygon.')
            if isinstance(poly_fixed, MultiPolygon):
                poly_fixed = max(poly_fixed.geoms, key=lambda g: g.area)
            polyShap = poly_fixed
        xMin_raw, yMin_raw, xMax_raw, yMax_raw = polyShap.bounds
        xMin, yMin, xMax, yMax, demResX, demResY = _aligned_extent_to_raster((xMin_raw, yMin_raw, xMax_raw, yMax_raw), fileDEM, self.res)
        gridResX = float(self.res)
        gridResY = float(self.res)
        xSize = max(1, int(round((xMax - xMin) / gridResX)))
        ySize = max(1, int(round((yMax - yMin) / gridResY)))
        self.updateProgressText.emit('\nWriting out points to vrt format for gdal_grid\n')
        tmpCSVBase = 'ChannelPoints_{}'.format(safe_vlayer_name)
        tmpCSV = os.path.join(self.dirName, tmpCSVBase)
        for suffix in ('.csv', '.vrt'):
            try:
                os.remove(tmpCSV + suffix)
            except OSError:
                pass
        with open(tmpCSV + '.csv', 'w', encoding='utf-8') as outCSV:
            outCSV.write('x,y,z\n')
            for idx, i in enumerate(XYZ):
                outCSV.write('{:.3f},{:.3f},{:.3f}'.format(i[0], i[1], i[2]))
                if idx < len(XYZ) - 1:
                    outCSV.write('\n')
        with open(tmpCSV + '.vrt', 'w', encoding='utf-8') as outCSVheader:
            outCSVheader.write('<OGRVRTDataSource>\n\t<OGRVRTLayer name="{0}">\n\t\t<SrcDataSource relativeToVRT="1">{0}.csv</SrcDataSource>\n\t\t<GeometryType>wkbPoint</GeometryType>\n\t\t<GeometryField encoding="PointFromColumns" x="x" y="y" z="z"/>\n\t</OGRVRTLayer>\n</OGRVRTDataSource>'.format(tmpCSVBase))
        self.updateProgressText.emit('\nGridding synthetic channel points with gdal_grid.py\n')
        tmpChanRast = os.path.normpath(os.path.join(self.dirName, safe_vlayer_name + 'ChannelGrid.tif'))
        try:
            os.remove(tmpChanRast)
        except OSError:
            pass
        inFile = tmpCSV + '.vrt'
        outFile = tmpChanRast
        cmd = ['gdal_grid', '-zfield', 'z', '-a_srs', epsgCode, '-a', f'nearest:radius1={gridResX}:radius2={gridResY}', '-outsize', str(xSize), str(ySize), '-l', tmpCSVBase, '-txe', str(xMin), str(xMax), '-tye', str(yMin), str(yMax), inFile, outFile]
        _run_cmd(cmd, self.updateProgressText, 'gdal_grid')
        _ensure_file_exists(outFile, 'Synthetic channel grid')
        outShape = os.path.normpath(os.path.join(self.dirName, safe_vlayer_name + 'Poly.shp'))
        outDriver = ogr.GetDriverByName('Esri Shapefile')
        if os.path.exists(outShape):
            outDriver.DeleteDataSource(outShape)
        outDataSource = outDriver.CreateDataSource(outShape)
        outLayer = outDataSource.CreateLayer(outShape, geom_type=ogr.wkbMultiPolygon)
        outLayer.CreateField(ogr.FieldDefn('id', ogr.OFTInteger))
        featureDefn = outLayer.GetLayerDefn()
        feat = ogr.Feature(featureDefn)
        feat.SetField('id', 1)
        geom = ogr.CreateGeometryFromWkb(polyShap.wkb)
        feat.SetGeometry(geom)
        outLayer.CreateFeature(feat)
        with open(outShape.split('.shp')[0] + '.prj', 'w', encoding='utf-8') as out_prj:
            out_prj.write(self.vLayer.crs().toWkt())
        outDataSource = outLayer = feat = geom = None
        self.updateProgressText.emit('\nclip DEM to channel polygon\n')
        tmpChanRastClip = os.path.normpath(os.path.join(self.dirName, safe_vlayer_name + 'ChannelGridClip.tif'))
        cmd = ['gdalwarp', '-t_srs', epsgCode, '-dstnodata', '-99999', '-q', '-cutline', outShape, '-crop_to_cutline', '-tap', '-te', str(xMin), str(yMin), str(xMax), str(yMax), '-tr', str(gridResX), str(gridResY), '-overwrite', outFile, tmpChanRastClip]
        _run_cmd(cmd, self.updateProgressText, 'gdalwarp channel clip')
        _ensure_file_exists(tmpChanRastClip, 'Clipped synthetic channel grid')
        self.updateProgressText.emit('\nInserting synthetic channel grid into DEM where channel elevation is less than DEM\n')
        tmpDEMclip = os.path.normpath(os.path.join(self.dirName, safe_rlayer_name + 'Clip.tif'))
        cmd = ['gdalwarp', '-r', 'bilinear', '-tap', '-te', str(xMin), str(yMin), str(xMax), str(yMax), '-tr', str(demResX), str(demResY), '-overwrite', fileDEM, tmpDEMclip]
        _run_cmd(cmd, self.updateProgressText, 'gdalwarp DEM clip')
        _ensure_file_exists(tmpDEMclip, 'Clipped DEM')
        demChannel = os.path.normpath(os.path.join(self.dirName, f'ChannelElev_{safe_vlayer_name}.tif'))
        _run_gdal_calc('B*(B<A)+A*(B>=A)', demChannel, tmpDEMclip, tmpChanRastClip, self.updateProgressText, 'gdal_calc channel elevation')
        _ensure_file_exists(demChannel, 'Channel elevation raster')
        demCutDepth = os.path.normpath(os.path.join(self.dirName, f'ChannelDepthCut_{safe_vlayer_name}.tif'))
        _run_gdal_calc('A*(B<0)+(A-B)*(B>0)', demCutDepth, tmpDEMclip, demChannel, self.updateProgressText, 'gdal_calc cut depth')
        _ensure_file_exists(demCutDepth, 'Cut depth raster')
        ds = gdal.Open(demCutDepth)
        if ds is None:
            raise RuntimeError(f'GDAL could not open cut depth raster: {demCutDepth}')
        a = np.array(ds.GetRasterBand(1).ReadAsArray())
        maxCut = a.max()
        numPix = np.size(a[np.where(a > 0.005)])
        valid_cut = a[np.where(a > 0.01)]
        avgCut = valid_cut.mean() if valid_cut.size else 0.0
        vol = avgCut * numPix * self.res * self.res
        channelLength = pCL[3][-1]
        self.values = maxCut, avgCut, vol, self.dirName, demChannel, demCutDepth, channelLength
        removeWorkingFiles = True
        if removeWorkingFiles:
            for path in (tmpDEMclip, tmpDEMclip + '.aux.xml', tmpChanRastClip, tmpChanRast):
                try:
                    os.remove(path)
                except Exception:
                    pass
            try:
                outDriver.DeleteDataSource(outShape)
            except Exception:
                pass
        return self.values


# Backward-compatible alias after plugin rebranding
OmicronChannel = DrainageChannelBuilder

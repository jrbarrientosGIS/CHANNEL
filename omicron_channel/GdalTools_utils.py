# -*- coding: utf-8 -*-
import os

def setProcessEnvironment(_obj=None):
    """Compatibility shim for legacy plugin code on QGIS 3.44.
    The original plugin used a large GDAL helper from QGIS 2.x. For the
    current plugin code path only environment propagation is required.
    """
    os.environ.setdefault('GDAL_FILENAME_IS_UTF8', 'YES')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

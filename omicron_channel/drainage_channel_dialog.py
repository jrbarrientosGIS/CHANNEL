# -*- coding: utf-8 -*-
import io
import os
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QThread, QFileInfo, QSignalBlocker, Qt, QT_VERSION_STR
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QGroupBox, QVBoxLayout, QHBoxLayout,
    QLabel, QTableWidget, QHeaderView, QAbstractItemView, QPushButton,
    QTableWidgetItem, QMessageBox, QSizePolicy, QWidget, QGridLayout, QScrollArea, QFrame
)
from . import DrainageChannelBuilder_utils as utils
from . import DrainageChannelThread
from qgis.core import QgsMessageLog, QgsRasterLayer, QgsProject, Qgis
try:
    # matplotlib >= 3.5: backend genérico que usa el binding Qt ya cargado (PyQt6 en QGIS 4)
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
except ImportError:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib import rcParams
from matplotlib.ticker import ScalarFormatter
from matplotlib.offsetbox import AnchoredText
import numpy as np

_UI_PATH = os.path.join(os.path.dirname(__file__), 'drainage_channel_dialog_base.ui')

# Enums sin ámbito del .ui -> forma con ámbito completo exigida por PyQt6 (QGIS 4).
# El .ui original se mantiene intacto para poder seguir editándolo con Qt Designer.
_UI_QT6_ENUMS = (
    ('QTabWidget::East', 'QTabWidget::TabPosition::East'),
    ('QLayout::SetDefaultConstraint', 'QLayout::SizeConstraint::SetDefaultConstraint'),
    ('QLayout::SetNoConstraint', 'QLayout::SizeConstraint::SetNoConstraint'),
    ('Qt::Horizontal', 'Qt::Orientation::Horizontal'),
    ('Qt::Vertical', 'Qt::Orientation::Vertical'),
    ('QDialogButtonBox::Close', 'QDialogButtonBox::StandardButton::Close'),
    ('QDialogButtonBox::Ok', 'QDialogButtonBox::StandardButton::Ok'),
    ('QFormLayout::FieldsStayAtSizeHint', 'QFormLayout::FieldGrowthPolicy::FieldsStayAtSizeHint'),
)


def _load_form_class():
    if int(QT_VERSION_STR.split('.')[0]) >= 6:
        with open(_UI_PATH, encoding='utf-8') as fh:
            ui_text = fh.read()
        for old, new in _UI_QT6_ENUMS:
            if new not in ui_text:
                ui_text = ui_text.replace(old, new)
        return uic.loadUiType(io.StringIO(ui_text))[0]
    return uic.loadUiType(_UI_PATH)[0]


FORM_CLASS = _load_form_class()


class OmicronChannelDialog(QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setupUi(self)
        self.resWarning = False
        self.layersOverlap = False
        self.canBuild1D = False
        self.worker = None
        self.thread = None
        self.profilePoints = []
        self._updatingGradeTable = False
        self._refreshingPlot = False
        self._handlingGradeChange = False
        self._draggingGradePointIndex = None
        self._draggingGradePointLabel = None
        self.btnOk = self.buttonBox.button(QDialogButtonBox.StandardButton.Ok)
        self.btnOk.setText(self.tr('Run 2D'))
        self.btnClose = self.buttonBox.button(QDialogButtonBox.StandardButton.Close)
        self.cbDEM.currentIndexChanged.connect(self.updateRasterRes)
        self.cbDEM.currentIndexChanged.connect(self.checkLayerExtents)
        self.cbCL.currentIndexChanged.connect(self.checkLayerExtents)
        self.spinElevStart.valueChanged.connect(self.onGradeProfileChanged)
        self.spinElevEnd.valueChanged.connect(self.onGradeProfileChanged)
        self.spinRightSideSlope.valueChanged.connect(self.updateMaxBankWidth)
        self.spinLeftSideSlope.valueChanged.connect(self.updateMaxBankWidth)
        self.spinWidth.valueChanged.connect(self.checkRes)
        self.spinRes.valueChanged.connect(self.checkRes)
        self.browseBtn.clicked.connect(self.writeDirName)
        self.btn1Dsave.clicked.connect(self.writeOut1Dresults)
        self.figure = Figure()
        self.axes = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=.1, bottom=0.1, right=.95, top=.9, hspace=.2)
        self.canvas = FigureCanvas(self.figure)
        self.widgetPlotToolbar = NavigationToolbar(self.canvas, self.widgetPlot)
        lstActions = self.widgetPlotToolbar.actions()
        if len(lstActions) > 7:
            self.widgetPlotToolbar.removeAction(lstActions[7])
        self.gPlot.addWidget(self.canvas)
        self.gPlot.addWidget(self.widgetPlotToolbar)
        self.canvas.mpl_connect('button_press_event', self.onPlotMousePress)
        self.canvas.mpl_connect('motion_notify_event', self.onPlotMouseMove)
        self.canvas.mpl_connect('button_release_event', self.onPlotMouseRelease)
        self.figure.patch.set_visible(False)
        rcParams['font.serif'] = 'Verdana, Arial, Liberation Serif'
        rcParams['font.sans-serif'] = 'Tahoma, Arial, Liberation Sans'
        rcParams['font.cursive'] = 'Courier New, Arial, Liberation Sans'
        rcParams['font.fantasy'] = 'Comic Sans MS, Arial, Liberation Sans'
        rcParams['font.monospace'] = 'Courier New, Liberation Mono'
        self.buildGradePointsUi()
        self.rebuildChannelDimensionsTab()
        self.manageGui()
        self.tuneDialogLayout()
        self.updateRasterRes()
        self.checkLayerExtents()

    def buildGradePointsUi(self):
        self.groupGradePoints = QGroupBox(self.tr('Grade profile control points (r5.4.2 PRO)'))
        gradeLayout = QVBoxLayout(self.groupGradePoints)
        gradeLayout.setContentsMargins(10, 10, 10, 10)
        gradeLayout.setSpacing(8)
        lblInfo = QLabel(self.tr('Define the vertical grade with 4 or more control points along the selected centerline. Start and end rows are generated automatically and stay synchronized with the start/end elevation inputs. Add or remove intermediate rows as needed.'))
        lblInfo.setWordWrap(True)
        gradeLayout.addWidget(lblInfo)

        self.tblGradePoints = QTableWidget(0, 3)
        self.tblGradePoints.setHorizontalHeaderLabels([self.tr('Point'), self.tr('Station'), self.tr('Elevation')])
        self.tblGradePoints.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tblGradePoints.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tblGradePoints.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tblGradePoints.verticalHeader().setVisible(False)
        self.tblGradePoints.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tblGradePoints.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tblGradePoints.setMinimumHeight(180)
        self.tblGradePoints.setAlternatingRowColors(True)
        self.tblGradePoints.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        gradeLayout.addWidget(self.tblGradePoints, 1)

        self.lblGradeSummary = QLabel(self.tr('Minimum grade definition: Start + 2 intermediate points + End.'))
        self.lblGradeSummary.setWordWrap(True)
        gradeLayout.addWidget(self.lblGradeSummary)

        buttonLayout = QHBoxLayout()
        self.btnGradeAuto = QPushButton(self.tr('Reset to 4 points'))
        self.btnGradeAdd = QPushButton(self.tr('Add intermediate'))
        self.btnGradeDelete = QPushButton(self.tr('Delete selected'))
        buttonLayout.addWidget(self.btnGradeAuto)
        buttonLayout.addWidget(self.btnGradeAdd)
        buttonLayout.addWidget(self.btnGradeDelete)
        buttonLayout.addStretch()
        gradeLayout.addLayout(buttonLayout)

        self.tblGradePoints.itemChanged.connect(self.onGradeTableItemChanged)
        self.btnGradeAuto.clicked.connect(lambda: self.populateDefaultIntermediatePoints(force=True, intermediate_count=2))
        self.btnGradeAdd.clicked.connect(self.addIntermediateGradePoint)
        self.btnGradeDelete.clicked.connect(self.removeSelectedGradePoint)

    def _clearLayout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                self._clearLayout(child_layout)

    def rebuildChannelDimensionsTab(self):
        try:
            self._clearLayout(self.horizontalLayout_5)

            scroll = QScrollArea(self.tab_4)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)

            content = QWidget()
            mainLayout = QVBoxLayout(content)
            mainLayout.setContentsMargins(0, 0, 0, 0)
            mainLayout.setSpacing(10)

            topWidget = QWidget(content)
            topGrid = QGridLayout(topWidget)
            topGrid.setContentsMargins(0, 0, 0, 0)
            topGrid.setHorizontalSpacing(18)
            topGrid.setVerticalSpacing(8)

            for label in (self.label_2, self.label_5, self.label_7, self.label_4, self.label_9):
                label.setWordWrap(True)

            self.labelStartDepth.setWordWrap(True)
            self.labelEndDepth.setWordWrap(True)

            topGrid.addWidget(self.label_2, 0, 0)
            topGrid.addWidget(self.spinElevStart, 0, 1)
            topGrid.addWidget(self.label_7, 0, 2)
            topGrid.addWidget(self.spinWidth, 0, 3)

            topGrid.addWidget(self.labelStartDepth, 1, 0, 1, 2)
            topGrid.addWidget(self.label_4, 1, 2)
            topGrid.addWidget(self.spinLeftSideSlope, 1, 3)

            topGrid.addWidget(self.label_5, 2, 0)
            topGrid.addWidget(self.spinElevEnd, 2, 1)
            topGrid.addWidget(self.label_9, 2, 2)
            topGrid.addWidget(self.spinRightSideSlope, 2, 3)

            topGrid.addWidget(self.labelEndDepth, 3, 0, 1, 2)
            topGrid.setColumnStretch(0, 3)
            topGrid.setColumnStretch(1, 1)
            topGrid.setColumnStretch(2, 3)
            topGrid.setColumnStretch(3, 1)

            self.groupGradePoints.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

            mainLayout.addWidget(topWidget)
            mainLayout.addWidget(self.groupGradePoints, 1)
            mainLayout.addStretch()

            scroll.setWidget(content)
            self.horizontalLayout_5.addWidget(scroll)
            self.channelDimScroll = scroll
        except Exception as exc:
            QgsMessageLog.logMessage('UI rebuild failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)

    def tuneDialogLayout(self):
        try:
            self.setSizeGripEnabled(True)
            self.setMinimumSize(1020, 900)
            if self.width() < 1020 or self.height() < 900:
                self.resize(1020, 900)
            self.tabWidgetInput.setUsesScrollButtons(True)
            self.tblGradePoints.horizontalHeader().setStretchLastSection(True)
            self.tblGradePoints.setMinimumHeight(200)
            self.groupGradePoints.setMinimumHeight(320)
            self.groupGradePoints.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.labelErrMessage.setMaximumHeight(48)
            self.labelErrMessage.setMinimumHeight(24)
        except Exception:
            pass

    def manageGui(self):
        self.cbCL.clear()
        self.cbCL.addItems(utils.getLineLayerNames())
        self.cbDEM.clear()
        self.cbDEM.addItems(utils.getRasterLayerNames())

    def currentChannelLength(self):
        if not hasattr(self, 'vLayer') or self.vLayer is None or not self.vLayer.isValid():
            return 0.0
        try:
            length = float(utils.getVectorLineLength(self.vLayer))
            if length > 0:
                return length
        except Exception as exc:
            QgsMessageLog.logMessage('Centerline length calculation failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
        try:
            length = float(utils.getNativeVectorLineLength(self.vLayer))
            if length > 0:
                return length
        except Exception as exc:
            QgsMessageLog.logMessage('Native centerline length fallback failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
        return 0.0

    def _makeTableItem(self, text, editable=True, raw_value=None):
        item = QTableWidgetItem(text)
        if raw_value is not None:
            item.setData(Qt.ItemDataRole.UserRole, float(raw_value))
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _setGradeRow(self, row, label, station, elevation, editable_station=False, editable_elevation=False):
        self.tblGradePoints.setItem(row, 0, self._makeTableItem(label, editable=False))
        self.tblGradePoints.setItem(row, 1, self._makeTableItem('{:.3f}'.format(float(station)), editable=editable_station, raw_value=station))
        self.tblGradePoints.setItem(row, 2, self._makeTableItem('{:.3f}'.format(float(elevation)), editable=editable_elevation, raw_value=elevation))

    def syncGradeSummary(self):
        rows = self.tblGradePoints.rowCount()
        minimum_ok = rows >= 4
        self.lblGradeSummary.setText(self.tr('Control points: {0} ({1})').format(rows, self.tr('OK') if minimum_ok else self.tr('minimum 4 required')))

    def _numericCellValue(self, row, column):
        item = self.tblGradePoints.item(row, column)
        if item is None:
            raise ValueError('Missing table item')
        text = item.text().strip()
        try:
            value = float(text)
        except Exception:
            raw_value = item.data(Qt.ItemDataRole.UserRole)
            if raw_value is None:
                raise
            value = float(raw_value)
        else:
            item.setData(Qt.ItemDataRole.UserRole, float(value))
        return float(value)

    def syncGradeTableWithSpinBoxes(self):
        total_length = self.currentChannelLength()
        if total_length <= 0 or self.tblGradePoints.rowCount() < 2:
            return
        blocker = QSignalBlocker(self.tblGradePoints)
        self._setGradeRow(0, self.tr('Start'), 0.0, start_elev if 'start_elev' in locals() else self.spinElevStart.value(), editable_station=False, editable_elevation=False)
        last_row = self.tblGradePoints.rowCount() - 1
        self._setGradeRow(last_row, self.tr('End'), total_length, self.spinElevEnd.value(), editable_station=False, editable_elevation=False)
        del blocker
        self.syncGradeSummary()

    def _coerceGradeValue(self, item, fallback=None):
        if item is None:
            return fallback
        text = item.text().strip().replace(',', '.')
        try:
            value = float(text)
        except Exception:
            raw_value = item.data(Qt.ItemDataRole.UserRole)
            return fallback if raw_value is None else float(raw_value)
        item.setText('{:.3f}'.format(value))
        item.setData(Qt.ItemDataRole.UserRole, float(value))
        return float(value)

    def normalizeGradeTableRows(self):
        total_length = self.currentChannelLength()
        if total_length <= 0 or self.tblGradePoints.rowCount() < 2:
            return
        rows = []
        for row in range(1, self.tblGradePoints.rowCount() - 1):
            station_item = self.tblGradePoints.item(row, 1)
            elev_item = self.tblGradePoints.item(row, 2)
            station = self._coerceGradeValue(station_item)
            elevation = self._coerceGradeValue(elev_item)
            if station is None or elevation is None:
                continue
            station = max(0.0, min(float(total_length), float(station)))
            rows.append((station, float(elevation)))
        rows.sort(key=lambda item: item[0])
        min_gap = max(0.001, float(total_length) * 1e-6)
        normalized_rows = []
        prev_station = 0.0
        for idx, (station, elevation) in enumerate(rows, start=1):
            min_station = prev_station + min_gap
            max_station = float(total_length) - min_gap * (len(rows) - idx + 1)
            if max_station < min_station:
                max_station = min_station
            station = min(max(station, min_station), max_station)
            normalized_rows.append((station, elevation))
            prev_station = station
        blocker = QSignalBlocker(self.tblGradePoints)
        for row in range(1, self.tblGradePoints.rowCount() - 1):
            station, elevation = normalized_rows[row - 1]
            self._setGradeRow(row, self.tr('P{0}').format(row), station, elevation, editable_station=True, editable_elevation=True)
        del blocker
        self.syncGradeTableWithSpinBoxes()

    def addIntermediateGradePoint(self, station=None, elevation=None):
        total_length = self.currentChannelLength()
        if total_length <= 0:
            return
        if self.tblGradePoints.rowCount() < 2:
            self.populateDefaultIntermediatePoints(force=True, intermediate_count=2)
            return
        insert_row = self.tblGradePoints.currentRow()
        if insert_row < 1:
            insert_row = self.tblGradePoints.rowCount() - 1
        if insert_row >= self.tblGradePoints.rowCount() - 1:
            insert_row = self.tblGradePoints.rowCount() - 1
        prev_station = self._numericCellValue(insert_row - 1, 1)
        next_station = self._numericCellValue(insert_row, 1)
        prev_elev = self._numericCellValue(insert_row - 1, 2)
        next_elev = self._numericCellValue(insert_row, 2)
        if station is None:
            station = (prev_station + next_station) / 2.0
        if elevation is None:
            elevation = (prev_elev + next_elev) / 2.0
        blocker = QSignalBlocker(self.tblGradePoints)
        self.tblGradePoints.insertRow(insert_row)
        self._setGradeRow(insert_row, self.tr('Intermediate'), station, elevation, editable_station=True, editable_elevation=True)
        self._renumberIntermediateRows()
        del blocker
        self.onGradeProfileChanged()

    def _renumberIntermediateRows(self):
        last_row = self.tblGradePoints.rowCount() - 1
        for row in range(self.tblGradePoints.rowCount()):
            if row == 0:
                label = self.tr('Start')
                editable_station = editable_elevation = False
            elif row == last_row:
                label = self.tr('End')
                editable_station = editable_elevation = False
            else:
                label = self.tr('P{0}').format(row)
                editable_station = editable_elevation = True
            station = self._numericCellValue(row, 1)
            elevation = self._numericCellValue(row, 2)
            self._setGradeRow(row, label, station, elevation, editable_station=editable_station, editable_elevation=editable_elevation)
        self.syncGradeSummary()

    def removeSelectedGradePoint(self):
        current_row = self.tblGradePoints.currentRow()
        if current_row <= 0 or current_row >= self.tblGradePoints.rowCount() - 1:
            QMessageBox.information(self, self.tr('Grade profile'), self.tr('Only intermediate points can be deleted.'))
            return
        if self.tblGradePoints.rowCount() <= 4:
            QMessageBox.information(self, self.tr('Grade profile'), self.tr('At least 4 control points must remain.'))
            return
        self.tblGradePoints.removeRow(current_row)
        blocker = QSignalBlocker(self.tblGradePoints)
        self._renumberIntermediateRows()
        del blocker
        self.onGradeProfileChanged()

    def populateDefaultIntermediatePoints(self, force=False, intermediate_count=2):
        if not hasattr(self, 'rLayer') or not hasattr(self, 'vLayer') or self.rLayer is None or self.vLayer is None:
            return
        if not force and self.tblGradePoints.rowCount() >= intermediate_count + 2:
            self.syncGradeTableWithSpinBoxes()
            return
        total_length = self.currentChannelLength()
        if total_length <= 0:
            return
        intermediate_count = max(2, int(intermediate_count))
        start_elev, end_elev = self._safeEndpointElevations()
        stations = [total_length * (idx + 1) / float(intermediate_count + 1) for idx in range(intermediate_count)]
        try:
            elevs = utils.sampleProfileElevations(self.vLayer, self.rLayer, stations)
        except Exception:
            elevs = [np.interp(station, [0.0, total_length], [start_elev, end_elev]) for station in stations]
        self._updatingGradeTable = True
        blocker = QSignalBlocker(self.tblGradePoints)
        self.tblGradePoints.setRowCount(0)
        self.tblGradePoints.insertRow(0)
        self._setGradeRow(0, self.tr('Start'), 0.0, start_elev if 'start_elev' in locals() else self.spinElevStart.value(), editable_station=False, editable_elevation=False)
        for idx, (station, elev) in enumerate(zip(stations, elevs), start=1):
            self.tblGradePoints.insertRow(self.tblGradePoints.rowCount())
            self._setGradeRow(idx, self.tr('P{0}').format(idx), station, elev, editable_station=True, editable_elevation=True)
        self.tblGradePoints.insertRow(self.tblGradePoints.rowCount())
        self._setGradeRow(self.tblGradePoints.rowCount() - 1, self.tr('End'), total_length, end_elev if 'end_elev' in locals() else self.spinElevEnd.value(), editable_station=False, editable_elevation=False)
        del blocker
        self._updatingGradeTable = False
        self.syncGradeSummary()
        self.onGradeProfileChanged()

    def _safeEndpointElevations(self):
        """Return robust start/end grade elevations even when one or both endpoints fall outside the DEM."""
        total_length = self.currentChannelLength()
        start_elev = float(self.spinElevStart.value())
        end_elev = float(self.spinElevEnd.value())
        if total_length <= 0 or not hasattr(self, 'rLayer') or not hasattr(self, 'vLayer'):
            return start_elev, end_elev
        try:
            sample_stations = np.linspace(0.0, float(total_length), 101)
            sampled = []
            valid_stations = []
            for station in sample_stations:
                try:
                    vals = utils.sampleProfileElevations(self.vLayer, self.rLayer, [float(station)])
                    val = vals[0] if vals else None
                except Exception:
                    val = None
                if val is not None and np.isfinite(float(val)):
                    valid_stations.append(float(station))
                    sampled.append(float(val))
            if len(sampled) >= 1:
                # Use the nearest valid DEM sample as fallback for missing endpoints.
                if not np.isfinite(start_elev) or abs(start_elev) < 1e-12:
                    start_elev = sampled[0]
                if not np.isfinite(end_elev) or abs(end_elev) < 1e-12:
                    end_elev = sampled[-1]
                return float(start_elev), float(end_elev)
        except Exception:
            pass
        return start_elev, end_elev

    def _defaultGradeProfilePoints(self):
        total_length = self.currentChannelLength()
        if total_length <= 0:
            return None
        start_elev, end_elev = self._safeEndpointElevations()
        stations = [0.0, total_length / 3.0, (2.0 * total_length) / 3.0, float(total_length)]
        try:
            elevs = utils.sampleProfileElevations(self.vLayer, self.rLayer, stations)
        except Exception:
            elevs = np.interp(stations, [0.0, float(total_length)], [start_elev, end_elev])
        return [(float(st), float(el)) for st, el in zip(stations, elevs)]

    def ensureMinimumGradePoints(self, force_reset=False):
        total_length = self.currentChannelLength()
        if total_length <= 0:
            return None
        if force_reset or self.tblGradePoints.rowCount() < 4:
            blocker = QSignalBlocker(self.tblGradePoints)
            self.tblGradePoints.setRowCount(0)
            defaults = self._defaultGradeProfilePoints()
            if defaults is None:
                del blocker
                return None
            labels = [self.tr('Start'), self.tr('P1'), self.tr('P2'), self.tr('End')]
            for idx, (station, elev) in enumerate(defaults):
                self.tblGradePoints.insertRow(idx)
                editable = idx not in (0, len(defaults)-1)
                self._setGradeRow(idx, labels[idx], station, elev, editable_station=editable, editable_elevation=editable)
            del blocker
            self.syncGradeSummary()
        return self.getGradeProfilePoints(show_errors=False, allow_fallback=False)

    def getGradeProfilePoints(self, show_errors=False, allow_fallback=True):
        total_length = self.currentChannelLength()
        if total_length <= 0:
            return None
        if self.tblGradePoints.rowCount() < 4:
            if allow_fallback:
                return self._defaultGradeProfilePoints()
            return None
        station_tolerance = max(0.005, total_length * 1e-6)
        points = []
        for row in range(self.tblGradePoints.rowCount()):
            try:
                station = self._numericCellValue(row, 1)
                elev = self._numericCellValue(row, 2)
            except Exception:
                if allow_fallback and not show_errors:
                    return self._defaultGradeProfilePoints()
                if show_errors:
                    QMessageBox.warning(self, self.tr('Grade profile'), self.tr('All grade profile rows must contain numeric station and elevation values.'))
                return None
            if row == 0:
                if abs(station) > station_tolerance:
                    if show_errors:
                        QMessageBox.warning(self, self.tr('Grade profile'), self.tr('The first control point must be at station 0.'))
                    return None
                station = 0.0
            if row == self.tblGradePoints.rowCount() - 1:
                if abs(station - total_length) > station_tolerance:
                    if show_errors:
                        QMessageBox.warning(self, self.tr('Grade profile'), self.tr('The last control point must match the channel length. Use the reset button if needed.'))
                    return None
                station = float(total_length)
            points.append((station, elev))
        try:
            points = utils.normalizeProfilePoints(points, total_length, tolerance=station_tolerance)
        except ValueError as exc:
            if allow_fallback and not show_errors:
                return self._defaultGradeProfilePoints()
            if show_errors:
                QMessageBox.warning(self, self.tr('Grade profile'), str(exc))
            return None
        if len(points) < 4:
            if allow_fallback and not show_errors:
                return self._defaultGradeProfilePoints()
            if show_errors:
                QMessageBox.warning(self, self.tr('Grade profile'), self.tr('Define at least 4 grade control points: start, 2 intermediate points, and end.'))
            return None
        return points

    def onGradeTableItemChanged(self, item):
        if self._updatingGradeTable:
            return
        self._updatingGradeTable = True
        try:
            if item is not None and item.column() in (1, 2):
                self._coerceGradeValue(item)
                self.normalizeGradeTableRows()
            self.syncGradeSummary()
            profile_points = self.getGradeProfilePoints(show_errors=False, allow_fallback=False)
            if profile_points is not None:
                self.profilePoints = profile_points
        finally:
            self._updatingGradeTable = False
        self.refreshPlot()
        self.btnOk.setEnabled(self.layersOverlap and self.getGradeProfilePoints(show_errors=False) is not None)

    def _plotNavigationActive(self):
        try:
            return bool(getattr(self.widgetPlotToolbar, 'mode', ''))
        except Exception:
            return False

    def _pickGradePointIndex(self, event, max_pixels=12):
        if event is None or event.inaxes != self.axes or event.xdata is None or event.ydata is None:
            return None
        profile_points = self.getGradeProfilePoints(show_errors=False, allow_fallback=False)
        if not profile_points:
            return None
        coords = np.array([[pt[0], pt[1]] for pt in profile_points], dtype=float)
        try:
            display = self.axes.transData.transform(coords)
        except Exception:
            return None
        distances = np.hypot(display[:, 0] - float(event.x), display[:, 1] - float(event.y))
        if distances.size == 0:
            return None
        idx = int(np.argmin(distances))
        return idx if float(distances[idx]) <= float(max_pixels) else None

    def _applyDraggedGradePoint(self, point_index, station, elevation):
        total_length = self.currentChannelLength()
        row_count = self.tblGradePoints.rowCount()
        if total_length <= 0 or row_count < 2 or point_index is None or point_index < 0 or point_index >= row_count:
            return
        point_index = int(point_index)
        min_gap = max(0.001, float(total_length) * 1e-6)
        elevation = float(elevation)

        table_blocker = QSignalBlocker(self.tblGradePoints)
        start_blocker = None
        end_blocker = None
        try:
            if point_index == 0:
                station = 0.0
                start_blocker = QSignalBlocker(self.spinElevStart)
                self.spinElevStart.setValue(float(elevation))
                self._setGradeRow(0, self.tr('Start'), 0.0, float(elevation), editable_station=False, editable_elevation=False)
            elif point_index == row_count - 1:
                station = float(total_length)
                end_blocker = QSignalBlocker(self.spinElevEnd)
                self.spinElevEnd.setValue(float(elevation))
                self._setGradeRow(point_index, self.tr('End'), float(total_length), float(elevation), editable_station=False, editable_elevation=False)
            else:
                prev_station = self._numericCellValue(point_index - 1, 1)
                next_station = self._numericCellValue(point_index + 1, 1)
                station = max(prev_station + min_gap, min(next_station - min_gap, float(station)))
                self._setGradeRow(point_index, self.tr('P{0}').format(point_index), float(station), float(elevation), editable_station=True, editable_elevation=True)
        finally:
            if start_blocker is not None:
                del start_blocker
            if end_blocker is not None:
                del end_blocker
            del table_blocker

        profile_points = self.getGradeProfilePoints(show_errors=False, allow_fallback=False)
        if profile_points is not None:
            self.profilePoints = profile_points
        self.syncGradeSummary()
        self.calcDepth()
        self.refreshPlot()
        self.btnOk.setEnabled(self.layersOverlap and self.getGradeProfilePoints(show_errors=False) is not None)

    def onPlotMousePress(self, event):
        if event is None or event.button != 1 or self._plotNavigationActive():
            return
        point_index = self._pickGradePointIndex(event)
        if point_index is None:
            return
        self._draggingGradePointIndex = int(point_index)
        try:
            self._draggingGradePointLabel = self.tblGradePoints.item(point_index, 0).text()
        except Exception:
            self._draggingGradePointLabel = None
        self.canvas.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def onPlotMouseMove(self, event):
        if self._draggingGradePointIndex is None or self._plotNavigationActive():
            return
        if event is None or event.inaxes != self.axes or event.ydata is None:
            return
        row = int(self._draggingGradePointIndex)
        try:
            current_station = self._numericCellValue(row, 1)
        except Exception:
            current_station = 0.0
        station = current_station if event.xdata is None else float(event.xdata)
        self._applyDraggedGradePoint(row, station, float(event.ydata))

    def onPlotMouseRelease(self, event):
        if self._draggingGradePointIndex is None:
            return
        if event is not None and event.inaxes == self.axes and event.ydata is not None:
            row = int(self._draggingGradePointIndex)
            try:
                current_station = self._numericCellValue(row, 1)
            except Exception:
                current_station = 0.0
            station = current_station if event.xdata is None else float(event.xdata)
            self._applyDraggedGradePoint(row, station, float(event.ydata))
        self._draggingGradePointIndex = None
        self._draggingGradePointLabel = None
        self.canvas.unsetCursor()

    def _finalizePlotCanvas(self):
        try:
            self.axes.grid(True)
            self.axes.relim()
            self.axes.autoscale_view()
            self.figure.tight_layout()
        except Exception:
            pass
        try:
            self.canvas.draw_idle()
            self.canvas.flush_events()
        except Exception:
            self.canvas.draw()

    def _drawControlProfileOnly(self, profile_points, message=None):
        controlStations = [float(point[0]) for point in profile_points]
        controlElevs = [float(point[1]) for point in profile_points]
        if len(controlStations) >= 2:
            self.axes.plot(controlStations, controlElevs, marker='o', linewidth=1.5, label=self.tr('Grade profile'))
            for idx, (station, elev) in enumerate(profile_points):
                label = 'S' if idx == 0 else ('E' if idx == len(profile_points) - 1 else 'P{}'.format(idx))
                self.axes.annotate(label, (station, elev), textcoords='offset points', xytext=(0, 8), ha='center')
            span = max(controlStations) - min(controlStations)
            if span <= 0:
                span = 1.0
            zmin = min(controlElevs)
            zmax = max(controlElevs)
            zspan = max(zmax - zmin, 1.0)
            self.axes.set_xlim(min(controlStations) - span * 0.03, max(controlStations) + span * 0.03)
            self.axes.set_ylim(zmin - zspan * 0.15, zmax + zspan * 0.20)
            self.axes.legend(loc='lower left')
        else:
            self.axes.set_xlim(0, 1)
            self.axes.set_ylim(0, 1)
        if message:
            self.axes.text(0.5, 0.95, str(message), ha='center', va='top', wrap=True, transform=self.axes.transAxes)
        self.axes.set_ylabel(str(self.tr('Elevation, z field units')))
        self.axes.set_xlabel(str(self.tr('Station, layer units')))
        self.axes.set_title(str(self.tr('1D profile preview')))
        self._finalizePlotCanvas()

    def _fallbackGradeProfilePoints(self):
        """Emergency profile points so the preview panel never stays blank."""
        try:
            total_length = float(self.currentChannelLength())
        except Exception:
            total_length = 0.0
        if total_length <= 0:
            total_length = 1.0
        try:
            start_elev = float(self.spinElevStart.value())
        except Exception:
            start_elev = 0.0
        try:
            end_elev = float(self.spinElevEnd.value())
        except Exception:
            end_elev = start_elev
        if abs(start_elev) < 1e-12 and abs(end_elev) < 1e-12:
            start_elev, end_elev = 0.0, 0.01
        return [
            (0.0, start_elev),
            (total_length / 3.0, start_elev + (end_elev - start_elev) / 3.0),
            ((2.0 * total_length) / 3.0, start_elev + 2.0 * (end_elev - start_elev) / 3.0),
            (total_length, end_elev),
        ]

    def refreshPlot(self):
        if self._refreshingPlot:
            return
        self._refreshingPlot = True
        try:
            self.axes.clear()
            self.btn1Dsave.setEnabled(False)
            has_layers = hasattr(self, 'rLayer') and hasattr(self, 'vLayer') and self.rLayer is not None and self.vLayer is not None and self.rLayer.isValid() and self.vLayer.isValid()
            profile_points = None
            if has_layers:
                try:
                    profile_points = self.ensureMinimumGradePoints(force_reset=False)
                    if profile_points is None:
                        profile_points = self._defaultGradeProfilePoints()
                except Exception as exc:
                    QgsMessageLog.logMessage('Profile control point preparation failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
                    profile_points = None
            if not has_layers:
                self.axes.text(0.5, 0.5, self.tr('Select a valid DEM and centerline to preview the profile.'), ha='center', va='center', wrap=True, transform=self.axes.transAxes)
                self.axes.set_xlim(0, 1)
                self.axes.set_ylim(0, 1)
                self._finalizePlotCanvas()
                return
            if profile_points is None:
                profile_points = self._fallbackGradeProfilePoints()
                self.profilePoints = profile_points
                self._drawControlProfileOnly(profile_points, self.tr('Centerline/DEM sampling is not valid yet; showing a fallback grade profile.'))
                return
            self.profilePoints = profile_points
            try:
                results1D = utils.getPlotArray(self.vLayer, self.rLayer, profile_points, self.xRes)
            except Exception as exc:
                QgsMessageLog.logMessage('1D profile DEM sampling failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
                self._drawControlProfileOnly(profile_points, self.tr('DEM sampling failed; showing only the grade control profile. {}').format(exc))
                return
            self.stationA = results1D[0, :]
            self.zExistA = results1D[1, :]
            self.zPropA = results1D[2, :]
            self.xA = results1D[3, :]
            self.yA = results1D[4, :]
            if len(self.stationA) < 2:
                self._drawControlProfileOnly(profile_points, self.tr('Not enough DEM samples; showing only the grade control profile.'))
                return
            self.calcCutAvgAreaEndMeth()
            self.axes.plot(self.stationA, self.zExistA, label=self.tr('Existing terrain'))
            self.axes.plot(self.stationA, self.zPropA, label=self.tr('Grade profile'))
            valid_fill = np.isfinite(self.zExistA) & np.isfinite(self.zPropA)
            if valid_fill.any():
                self.axes.fill_between(self.stationA, self.zExistA, self.zPropA, where=(self.zExistA >= self.zPropA), alpha=0.2, interpolate=True)

            controlStations = [float(point[0]) for point in profile_points]
            controlElevs = [float(point[1]) for point in profile_points]
            if len(controlStations) >= 2:
                self.axes.plot(controlStations, controlElevs, linestyle='--', linewidth=1.0, alpha=0.8, label=self.tr('Control polygon'))

            start_station = [controlStations[0]]
            start_elev = [controlElevs[0]]
            end_station = [controlStations[-1]]
            end_elev = [controlElevs[-1]]
            intermediate_stations = controlStations[1:-1]
            intermediate_elevs = controlElevs[1:-1]

            self.axes.plot(start_station, start_elev, linestyle='None', marker='s', markersize=8, label=self.tr('Start / End'))
            self.axes.plot(end_station, end_elev, linestyle='None', marker='s', markersize=8)
            if intermediate_stations:
                self.axes.plot(intermediate_stations, intermediate_elevs, linestyle='None', marker='o', markersize=7, label=self.tr('Intermediate control points'))

            for idx, (station, elev) in enumerate(profile_points):
                label = 'S' if idx == 0 else ('E' if idx == len(profile_points) - 1 else 'P{}'.format(idx))
                self.axes.annotate(label, (station, elev), textcoords='offset points', xytext=(0, 8), ha='center')
            self.axes.legend(loc='lower left')
            formatter = ScalarFormatter(useOffset=False)
            self.axes.yaxis.set_major_formatter(formatter)
            self.axes.set_ylabel(str(self.tr('Elevation, z field units')))
            self.axes.set_xlabel(str(self.tr('Station, layer units')))
            self.axes.set_title(str(self.tr('Channel {} Profile and 1D Calculation Results'.format(self.vLayer.name()))))
            self.axes.text(0.01, 0.01, self.tr('Drag control points on the profile with the left mouse button.'), transform=self.axes.transAxes, fontsize=9, va='bottom', ha='left')
            if hasattr(self, 'outText'):
                at = AnchoredText(self.outText, prop=dict(size=12), frameon=True, loc='upper right')
                at.patch.set_boxstyle('round,pad=0.,rounding_size=0.2')
                self.axes.add_artist(at)
            self._finalizePlotCanvas()
            self.btn1Dsave.setEnabled(True)
        finally:
            self._refreshingPlot = False
    def refreshPlotText(self):
        if not hasattr(self, 'outText'):
            return
        self.refreshPlot()

    def calcCutAvgAreaEndMeth(self):
        self.depth1D = self.zExistA - self.zPropA
        self.maxCLdepth = np.max(self.depth1D)
        leftSlope = self.spinLeftSideSlope.value()
        rightSlope = self.spinRightSideSlope.value()
        width = self.spinWidth.value()
        self.area = self.depth1D * (width + self.depth1D * leftSlope / 2.0 + self.depth1D * rightSlope / 2.0)
        self.length = self.stationA[1:] - self.stationA[:-1]
        self.vol1D = (self.area[1:] + self.area[:-1]) * self.length / 2.0
        self.vol1D = np.append(0, self.vol1D)
        self.totVol1D = np.sum(self.vol1D)
        self.maxDepth1D = np.max(self.depth1D)
        self.totLength = np.max(self.stationA)
        self.channelSlope = (self.zPropA[-1] - self.zPropA[0]) / self.totLength if self.totLength else 0.0
        self.outText = 'Length: {2:.2f} layer units\nChannel Slope: {3:.6f}\n1D Max. Cut Depth: {0:,.2f} layer units\n1D Tot. Vol.: {1:,.2f} layer units$^3$'.format(self.maxDepth1D, self.totVol1D, self.totLength, self.channelSlope)

    def writeOut1Dresults(self):
        if not hasattr(self, 'stationA'):
            return
        self.out1Dresults = np.zeros((self.stationA.size, 8))
        self.out1Dresults[:, 0] = self.stationA
        self.out1Dresults[:, 1] = self.vol1D
        self.out1Dresults[:, 2] = self.zExistA
        self.out1Dresults[:, 3] = self.zPropA
        self.out1Dresults[:, 4] = self.depth1D
        self.out1Dresults[:, 5] = self.area
        self.out1Dresults[:, 6] = self.xA
        self.out1Dresults[:, 7] = self.yA
        outPath = self.outputDir.text()
        home = os.path.expanduser('~')
        if outPath == '':
            outPath = os.path.join(home, 'Desktop', 'QGIS2OmicronChannelFiles')
            self.outputDir.setText(outPath)
            os.makedirs(outPath, exist_ok=True)
        outPath = os.path.normpath(outPath)
        os.makedirs(outPath, exist_ok=True)
        safe_layer_name = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in self.vLayer.name()).strip('_') or 'layer'
        fileName = os.path.join(outPath, 'Channel1Dresults_{}.txt'.format(safe_layer_name))
        gradeSummary = utils.formatProfilePoints(self.profilePoints)
        outHeader = 'DEM Layer:\t{0}\nChannel Centerline:\t{1}\nProjection:\t{13}\nChannel Bottom Width:\t{3}\nChannel Start Elevation:\t{4}\nChannel End Elevation:\t{5}\nGrade Control Points (station:elev):\t{14}\nChannel Slope:\t{12:.06f}\nLeft Side Slope:\t{6}\nRight Side Slope:\t{7}\nLength:\t{9:,.2f}\n1D Max. Cut Depth:\t{10:,.2f}\n1D Tot. Vol:\t{11:,.2f}\nNote:\tAll units of length correspond to layer units, all units of area and volume are a combination of layer units and raster elevation units\n\nstation\tvol\tzExist\tzProp\tdepth\tcutArea\tx\ty\n'.format(self.cbDEM.currentText(), self.cbCL.currentText(), self.spinRes.value(), self.spinWidth.value(), self.spinElevStart.value(), self.spinElevEnd.value(), self.spinLeftSideSlope.value(), self.spinRightSideSlope.value(), self.spinBankWidth.value(), self.totLength, self.maxDepth1D, self.totVol1D, self.channelSlope, self._crsSummaryText(), gradeSummary)
        np.savetxt(fileName, self.out1Dresults, fmt='%.3f', header=outHeader, delimiter='\t')
        self.canvas.print_figure(os.path.join(outPath, 'Channel1DresultsFigure_{}.png'.format(safe_layer_name)))

    def updateMaxBankWidth(self):
        if not hasattr(self, 'maxCLdepth'):
            return
        self.maxBankWidth = self.maxCLdepth * np.max(np.array([self.spinLeftSideSlope.value(), self.spinRightSideSlope.value()]))
        self.spinBankWidth.setValue(self.maxBankWidth + self.spinRes.value() / 2.0)
        self.calcCutAvgAreaEndMeth()
        self.refreshPlotText()

    def writeDirName(self):
        self.outputDir.clear()
        self.dirName = QFileDialog.getExistingDirectory(self, 'Select Output Directory')
        self.outputDir.setText(self.dirName)
        self.checkBoxLoadLayers.setChecked(True)

    def updateRasterRes(self):
        layer_name = self.cbDEM.currentText().split(' EPSG')[0] if self.cbDEM.currentText() else ''
        layer = utils.getRasterLayerByName(layer_name)
        self.rLayer = layer
        if self.rLayer and self.rLayer.isValid():
            try:
                extent = layer.extent()
                width = max(int(layer.width()), 1)
                height = max(int(layer.height()), 1)
                self.xRes = float(extent.width()) / float(width)
                self.yRes = float(extent.height()) / float(height)
            except Exception:
                self.xRes = layer.rasterUnitsPerPixelX()
                self.yRes = layer.rasterUnitsPerPixelY()
            self.rasterArea = self.xRes * self.yRes
            self.spinRes.setValue(self.xRes)
            self.spinRes.setEnabled(True)
            self.spinElevStart.setEnabled(True)
            self.spinElevEnd.setEnabled(True)
            self.labelDEMres.setText('DEM Layer Resolution = {:.3f}'.format((self.xRes + self.yRes) / 2.0))
        else:
            self.labelDEMres.setText('DEM Layer Resolution = ###')

    def checkRes(self):
        self.resWarning = self.spinWidth.value() <= self.spinRes.value() * 10
        self.errOutput()
        if hasattr(self, 'zExistA') and hasattr(self, 'zPropA'):
            self.calcCutAvgAreaEndMeth()
            self.refreshPlotText()

    def checkLayerExtents(self):
        self.rLayer = utils.getRasterLayerByName(self.cbDEM.currentText().split(' EPSG')[0]) if self.cbDEM.currentText() else None
        self.vLayer = utils.getVectorLayerByName(self.cbCL.currentText().split(' EPSG')[0]) if self.cbCL.currentText() else None
        self.layersOverlap = False
        self.canBuild1D = False
        if not (self.rLayer and self.vLayer and self.rLayer.isValid() and self.vLayer.isValid()):
            self.spinElevStart.setEnabled(False)
            self.spinElevEnd.setEnabled(False)
            self.btn1Dsave.setEnabled(False)
            self.btnOk.setEnabled(False)
            self.errOutput()
            self.refreshPlot()
            return
        try:
            self.layersOverlap = utils.isVectorWithinRasterDomain(self.vLayer, self.rLayer)
        except Exception as exc:
            QgsMessageLog.logMessage('Raster-domain check failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
            self.layersOverlap = False
        # For the 1D preview, do not require full domain containment. Try to draw and let getPlotArray
        # keep only valid DEM samples. This prevents a blank plot when the line partly leaves the raster.
        self.canBuild1D = True
        self.spinElevStart.setEnabled(True)
        self.spinElevEnd.setEnabled(True)
        self.btn1Dsave.setEnabled(False)
        self.btnOk.setEnabled(False)
        try:
            self.calcElev()
        except Exception as exc:
            QgsMessageLog.logMessage('Endpoint elevation calculation failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
        try:
            self.populateDefaultIntermediatePoints(force=True, intermediate_count=max(2, self.tblGradePoints.rowCount() - 2 if self.tblGradePoints.rowCount() >= 4 else 2))
        except Exception as exc:
            QgsMessageLog.logMessage('Grade-point initialization failed: {}'.format(exc), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
            try:
                self.ensureMinimumGradePoints(force_reset=True)
            except Exception as exc2:
                QgsMessageLog.logMessage('Fallback grade-point initialization failed: {}'.format(exc2), 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
        self.refreshPlot()
        self.btn1Dsave.setEnabled(hasattr(self, 'stationA'))
        if self.layersOverlap and self.getGradeProfilePoints(show_errors=False) is not None:
            self.btnOk.setEnabled(True)
        self.errOutput()

    def onGradeProfileChanged(self):
        self.calcDepth()
        has_layers = hasattr(self, 'rLayer') and hasattr(self, 'vLayer') and self.rLayer is not None and self.vLayer is not None and self.rLayer.isValid() and self.vLayer.isValid()
        if has_layers:
            self._updatingGradeTable = True
            try:
                self.syncGradeTableWithSpinBoxes()
            finally:
                self._updatingGradeTable = False
            profile_points = self.getGradeProfilePoints(show_errors=False, allow_fallback=False)
            if profile_points is not None:
                self.profilePoints = profile_points
            self.refreshPlot()
        self.btnOk.setEnabled(self.layersOverlap and self.getGradeProfilePoints(show_errors=False) is not None)
    def calcDepth(self):
        has_layers = hasattr(self, 'rLayer') and hasattr(self, 'vLayer') and self.rLayer is not None and self.vLayer is not None and self.rLayer.isValid() and self.vLayer.isValid()
        if has_layers:
            utils.calcDepth(self)

    def calcElev(self):
        has_layers = hasattr(self, 'rLayer') and hasattr(self, 'vLayer') and self.rLayer is not None and self.vLayer is not None and self.rLayer.isValid() and self.vLayer.isValid()
        if has_layers:
            self.spinElevStart.setEnabled(True)
            self.spinElevEnd.setEnabled(True)
            elevs = utils.calcElev(self)
            blocker1 = QSignalBlocker(self.spinElevStart)
            blocker2 = QSignalBlocker(self.spinElevEnd)
            if elevs[0] is not None:
                self.spinElevStart.setValue(elevs[0])
            if elevs[1] is not None:
                self.spinElevEnd.setValue(elevs[1])
            del blocker1
            del blocker2
            self.syncGradeTableWithSpinBoxes()

    def updateProgressText(self, message):
        self.labelProgress.setText(message)

    def updateOutputText(self, message):
        self.textBrowser.setPlainText(message)

    def workerError(self, e, exception_string):
        QgsMessageLog.logMessage('Worker thread raised an exception:\n{}'.format(exception_string), 'Omicron-Channel', level=Qgis.MessageLevel.Critical)
        self.iface.messageBar().pushMessage('Omicron-Channel', 'It did not work, see log for details', level=Qgis.MessageLevel.Critical, duration=5)
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self._enableRunControls(True)

    def stopWorker(self):
        """Safely stop/cleanup the worker thread without killing a completed worker."""
        try:
            if self.thread is not None and self.thread.isRunning():
                self.thread.quit()
                self.thread.wait(10000)
        finally:
            try:
                if self.worker is not None:
                    self.worker.deleteLater()
            except Exception:
                pass
            try:
                if self.thread is not None:
                    self.thread.deleteLater()
            except Exception:
                pass
            self.worker = None
            self.thread = None

    def _enableRunControls(self, enabled=True):
        for w in (self.cbCL, self.cbDEM, self.spinRes, self.spinWidth, self.spinElevStart, self.spinElevEnd, self.spinLeftSideSlope, self.spinRightSideSlope, self.spinBankWidth, self.browseBtn, self.btn1Dsave, self.btnClose, self.btnOk, self.tblGradePoints, self.btnGradeAuto, self.btnGradeAdd, self.btnGradeDelete):
            try:
                w.setEnabled(enabled)
            except Exception:
                pass
        self.btnOk.setEnabled(bool(enabled and self.layersOverlap))

    def addOutputsToProject(self, output_paths):
        """Load generated rasters into the current project canvas only.

        Never opens a .qgz/.gpkg or external QGIS process. All layer insertion
        is done in the GUI thread via QgsProject.instance().
        """
        if not output_paths:
            return []
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group_name = 'Omicron-Channel Results'
        group = root.findGroup(group_name) or root.insertGroup(0, group_name)
        loaded = []
        for path in output_paths:
            if not path or not os.path.exists(path):
                QgsMessageLog.logMessage(f'Output not found, not loaded: {path}', 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
                continue
            base_name = QFileInfo(path).baseName()
            layer = QgsRasterLayer(path, base_name)
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)
                loaded.append(layer)
            else:
                QgsMessageLog.logMessage(f'Invalid output raster, not loaded: {path}', 'Omicron-Channel', level=Qgis.MessageLevel.Warning)
        if loaded:
            self.iface.mapCanvas().refresh()
        return loaded

    def errOutput(self):
        if self.canBuild1D and not self.layersOverlap and self.resWarning:
            self.labelErrMessage.setText('Warning: centerline extends outside raster domain. 2D run disabled; 1D preview only.\nWarning: For best 2D results, channel bottom width should be at least ten times greater than 2D grid calculation resolution')
        elif self.canBuild1D and not self.layersOverlap:
            self.labelErrMessage.setText('Warning: centerline extends outside raster domain. 2D run disabled; 1D preview only.')
        elif self.resWarning and not self.layersOverlap:
            self.labelErrMessage.setText('Error: Vector is not completely within raster domain\nWarning: For best 2D results, channel bottom width should be at least ten times greater than 2D grid calculation resolution')
        elif self.resWarning:
            self.labelErrMessage.setText('Warning: For best 2D results, channel bottom width should be at least ten times greater than 2D grid calculation resolution')
        elif not self.layersOverlap:
            self.labelErrMessage.setText('Error: Vector is not completely within raster domain')
        else:
            self.labelErrMessage.setText('')

    def _crsSummaryText(self):
        try:
            crs = self.rLayer.crs() if hasattr(self, 'rLayer') and self.rLayer is not None else None
            if crs is None or not crs.isValid():
                return 'Unknown CRS'
            authid = crs.authid() or 'Unknown CRS'
            try:
                proj = crs.toProj()
            except Exception:
                proj = ''
            proj = (proj or '').strip()
            return f'{authid} | {proj}' if proj else authid
        except Exception:
            return 'Unknown CRS'

    def workerFinished(self, values):
        self.stopWorker()
        self.values = values
        if not values or not values[0]:
            self.progressBar.setMaximum(100)
            self.progressBar.setValue(0)
            self._enableRunControls(True)
            return
        maxCut, avgCut, totVol, self.dirName, demChannelPath, demCutDepth, length = values[0]
        self.outputDir.setText(self.dirName)
        outText = 'Summary of 2D Results:\nTot. Vol.\t{2:,.2f}\nLength\t{3:,.2f}\nMax. Cut Depth\t{0:.2f}\nAvg. Cut Depth\t{1:.2f}'.format(maxCut, avgCut, totVol, length)
        self.updateOutputText(outText)
        self.iface.messageBar().pushMessage('Omicron-Channel', 'Channel elevation DEM, channel depth of cut DEM, and channel grid points located at {}. Please delete when finished'.format(self.dirName), duration=30)
        self.rLayer.saveNamedStyle(os.path.join(self.dirName, demChannelPath.split('.tif')[0] + '.qml'))
        xmlString = utils.depthQMLwriter(maxCut)
        with open(demCutDepth.split('.tif')[0] + '.qml', 'w', encoding='utf-8') as demCutDepthQML:
            demCutDepthQML.write(xmlString)
        # r5.4.9 PRO: always incorporate the generated 2D result rasters into
        # the current QGIS project canvas. This is done in the GUI thread and
        # never through external project/file launching.
        loaded_layers = self.addOutputsToProject([demChannelPath, demCutDepth])
        if loaded_layers:
            self.iface.messageBar().pushMessage('Omicron-Channel', '{} output layer(s) loaded into the current project'.format(len(loaded_layers)), level=Qgis.MessageLevel.Success, duration=5)
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(100)
        self._enableRunControls(True)
        self.writeOut1Dresults()
        safe_layer_name = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in self.vLayer.name()).strip('_') or 'layer'
        fileName = os.path.join(self.dirName, 'Channel2DresultsSummary_{}.txt'.format(safe_layer_name))
        gradeSummary = utils.formatProfilePoints(self.profilePoints)
        outText = 'DEM Layer:\t{0}\nChannel Centerline:\t{1}\nProjection:\t{14}\n2D Grid Calc Res.:\t{2}\nChannel Bottom Width:\t{3}\nChannel Start Elevation:\t{4}\nChannel End Elevation:\t{5}\nGrade Control Points (station:elev):\t{15}\nChannel Slope:\t{13:.06f}\nLeft Side Slope:\t{6}\nRight Side Slope:\t{7}\n2D Maximum Bank Width:\t{8}\n\nLength:\t{9:,.2f}\n2D Max. Cut Depth:\t{10:,.2f}\n2D Avg. Cut Depth:\t{11:,.2f}\n2D Tot. Vol:\t{12:,.2f}\n\nNote:\tAll units of length correspond to layer units, all units of area and volume are a combination of layer units and raster elevation units\n\n'.format(self.cbDEM.currentText(), self.cbCL.currentText(), self.spinRes.value(), self.spinWidth.value(), self.spinElevStart.value(), self.spinElevEnd.value(), self.spinLeftSideSlope.value(), self.spinRightSideSlope.value(), self.spinBankWidth.value(), self.totLength, maxCut, avgCut, totVol, self.channelSlope, self._crsSummaryText(), gradeSummary)
        with open(fileName, 'w', encoding='utf-8') as outFile:
            outFile.write(outText)
        self.checkBoxLoadLayers.setChecked(True)
        self.iface.mapCanvas().refresh()
        self.btnOk.setEnabled(self.layersOverlap)

    def accept(self):
        if not self.layersOverlap:
            QMessageBox.warning(self, self.tr('Omicron-Channel'), self.tr('The selected centerline is not fully contained within the DEM extent, so Run 2D is disabled.'))
            return
        profile_points = self.getGradeProfilePoints(show_errors=True)
        if profile_points is None:
            return
        self.profilePoints = profile_points
        args = [self.rLayer, self.vLayer, self.spinWidth.value(), self.spinRes.value(), self.profilePoints, self.spinLeftSideSlope.value(), self.spinRightSideSlope.value(), self.spinBankWidth.value(), self.outputDir.text()]
        self.thread = QThread()
        self.worker = DrainageChannelThread.DrainageChannelBuilder(args)
        self.worker.moveToThread(self.thread)
        self.worker.updateProgressText.connect(self.updateProgressText)
        self.worker.workerFinished.connect(self.workerFinished)
        self.worker.error.connect(self.workerError)
        for w in (self.btnClose, self.cbCL, self.cbDEM, self.spinRes, self.spinWidth, self.spinElevStart, self.spinElevEnd, self.spinLeftSideSlope, self.spinRightSideSlope, self.spinBankWidth, self.browseBtn, self.btn1Dsave, self.btnOk, self.tblGradePoints, self.btnGradeAuto, self.btnGradeAdd, self.btnGradeDelete):
            w.setEnabled(False)
        self.tabWidgetOutput.setCurrentWidget(self.tabOutputSummary)
        self.progressBar.setMaximum(0)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def reject(self):
        super().reject()
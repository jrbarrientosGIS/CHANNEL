# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtCore import QCoreApplication, QSettings, QTranslator
from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction  # Qt6 / QGIS 4
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5 / QGIS 3
from .drainage_channel_dialog import OmicronChannelDialog


class OmicronChannel:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr('&Omicron-Channel')
        self.toolbar = None
        self.windowOpened = False
        self.translator = None

        locale = str(QSettings().value('locale/userLocale', 'en'))[0:2]
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'OmicronChannel_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        return QCoreApplication.translate('OmicronChannel', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True, add_to_menu=True,
                   add_to_toolbar=True, status_tip=None, whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar and self.toolbar is not None:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        if self.toolbar is None:
            self.toolbar = self.iface.addToolBar('OmicronChannel')
            self.toolbar.setObjectName('OmicronChannel')
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.add_action(icon_path, text=self.tr('Omicron-Channel'), callback=self.run,
                        parent=self.iface.mainWindow())

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        self.actions = []
        if self.toolbar is not None:
            try:
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.deleteLater()
            except Exception:
                pass
            self.toolbar = None

    def run(self):
        if self.windowOpened:
            return
        self.windowOpened = True
        try:
            d = OmicronChannelDialog(self.iface)
            d.exec()
        finally:
            self.windowOpened = False

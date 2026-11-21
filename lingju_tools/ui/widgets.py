# coding: utf-8
"""主界面可拖拽选项卡组件。

- _DraggableTabBar：可拖拽重排序、双击触发弹出的选项卡条
- _DetachedWindow：选项卡弹出后的独立窗口，关闭时回调合并
- _DraggableTabWidget：主界面 QTabWidget，负责弹出 / 合并 / 右键菜单

不使用 QtCore.Signal：Cython 编译进 pyd 后在 Maya 2023（Py3.9 + PySide2）
导入期会触发 shiboken2 崩溃，统一用普通回调。
"""

import maya.cmds as cmds
import maya.OpenMayaUI as omui

from ..core.qt_compat import QtCore, QtWidgets, wrapInstance
from ..core.ui_helpers import _qt_valid

# 主窗口（workspaceControl）名字：弹出页面的临时 pane 挂在它下面
LJGT_WINDOW_NAME = "LingJuGeometryTools_Window"

class _DraggableTabBar(QtWidgets.QTabBar):
    """可拖拽重排序 + 双击弹出独立窗口的选项卡条。

    - 左键按住标签可在 tab 条内拖拽重排序
    - 双击标签调用 detach_handler 回调（参数 tab_index, tab_text），由外部弹出

    注意：不使用 QtCore.Signal——Cython 3.1.1 编译进 pyd 后在 Maya 2023
    （Py3.9 + PySide2 5.15.2）导入期触发 shiboken2 崩溃，普通回调跨版本安全。
    """

    def __init__(self, parent=None):
        super(_DraggableTabBar, self).__init__(parent)
        self.setMovable(True)          # 允许 tab 重排
        self.setDocumentMode(True)     # 更扁平的 tab 外观
        self.setElideMode(QtCore.Qt.ElideNone)
        self._press_pos = None
        self._pressed_index = -1
        self.detach_handler = None     # 双击回调: f(index, label)

    def mousePressEvent(self, event):
        """记录按下位置和 tab 索引，用于区分拖拽 vs 双击。"""
        if event.button() == QtCore.Qt.LeftButton:
            self._press_pos = event.globalPos()
            self._pressed_index = self.tabAt(event.pos())
        super(_DraggableTabBar, self).mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击标签：请求弹出为独立窗口。"""
        if event.button() == QtCore.Qt.LeftButton:
            index = self.tabAt(event.pos())
            if index >= 0 and self.detach_handler is not None:
                self.detach_handler(index, self.tabText(index))
                return
        super(_DraggableTabBar, self).mouseDoubleClickEvent(event)


class _DetachedWindow(QtWidgets.QWidget):
    """选项卡弹出后的独立窗口：以 Maya 主窗口为父 + Qt.Window 标志，保持在 Maya 前置。

    关闭时调用 on_closed 回调（参数为标签名），由 _DraggableTabWidget 回收到
    原选项卡位置。用真正的子类重载 closeEvent：实例属性打补丁在从属窗口下
    不生效，导致关闭后不合并。不使用 QtCore.Signal（见 _DraggableTabBar 注释）。
    """

    def __init__(self, label, parent=None):
        super(_DetachedWindow, self).__init__(parent, QtCore.Qt.Window)
        self._ljgt_label = label
        self.on_closed = None          # 关闭回调: f(label)
        self.setWindowTitle(u"零距工具集 · " + label)

    def closeEvent(self, event):
        """点 X / Alt+F4 关闭时通知主选项卡把自己收回去。"""
        if self.on_closed is not None:
            self.on_closed(self._ljgt_label)
        event.accept()


class _DraggableTabWidget(QtWidgets.QTabWidget):
    """主界面选项卡容器：横向 tab、支持重排序、支持弹出独立窗口。

    设计说明：
    - 主窗口内用 QTabWidget 承载各功能页，每页内部仍是 cmds 构建的 UI
      （通过 cmds.paneLayout 作为 cmds 与 Qt 之间的桥接）
    - 双击 tab 标签 → _pop_out_tab()：原页面只从 tab 条移除（仍留在
      QStackedWidget 内隐藏，C++ 与路径保持有效）；_DetachedWindow 独立窗口
      （Maya 主窗口的从属顶层窗口，保持前置）里放的是用构建函数重建的临时副本
    - 独立窗口关闭时 → _merge_back()：删除副本，把原页面插回主 QTabWidget
      原来的 tab 位置（记住弹出前的 index）
    - 右键 tab 标题弹出菜单：「弹出此页」/「合并全部页」
    """

    # 保存所有弹出窗口：{tab_label: (独立窗口, 原页面 widget, 弹出前 tab 索引, 副本 pane 名)}
    _detached = {}
    # create_ui 存入：{label: (pane_name, builder)}，弹出时用于构建临时副本
    _pane_builders = {}
    _pane_grey = None

    def __init__(self, parent=None):
        super(_DraggableTabWidget, self).__init__(parent)
        # 用自定义 TabBar 替换默认的
        self._tab_bar = _DraggableTabBar(self)
        self.setTabBar(self._tab_bar)
        self.setTabPosition(QtWidgets.QTabWidget.North)   # 横向 tab
        self.setDocumentMode(True)
        self.setMovable(True)

        self._tab_bar.setMinimumHeight(28)
        self._tab_bar.setStyleSheet(
            u"QTabBar::tab { min-height: 18px; padding: 2px 18px;"
            u" font-size: 12px; }"
            u"QTabBar::tab:selected { background-color: #2e674f;"
            u" color: #FFFFFF; font-weight: bold; border-radius: 4px; }"
            u"QTabBar::tab:hover:!selected { background-color: #1b3e2f;"
            u" border-radius: 4px; }")

        # 双击弹出：普通回调（不用 Signal，规避 pyd 编译在 Maya 2023 的崩溃）
        self._tab_bar.detach_handler = self._pop_out_tab
        self._tab_bar.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._tab_bar.customContextMenuRequested.connect(self._show_context_menu)

    # ------------------------------------------------------------------
    # 弹出独立窗口
    # ------------------------------------------------------------------
    def _pop_out_tab(self, index, label):
        """把第 index 个 tab 的页面弹出为独立顶层窗口。

        v174 修正：原页面绝不重挂到非 Maya 窗口（那是空白页、路径失效、
        C++ 被销毁、布局错乱等一系列问题的根源）。
        - 原页面只 removeTab，留在 QStackedWidget 内隐藏，C++ 与 Maya
          路径始终有效；
        - 独立窗口里放的是用构建函数重建的临时副本 pane（名字加 _pop
          后缀），关闭时删掉副本、原页面插回原位。
        """
        # 如果已经弹出了一个同名窗口，先合并再弹新的，避免重复
        if label in self._detached:
            self._merge_back(label)

        page = self.widget(index)
        if page is None or not _qt_valid(page):
            return

        info = self._pane_builders.get(label)
        if info is None:
            return
        pane_name, builder = info

        # 1. 在 workspaceControl 下建临时副本（Maya 管理的布局，显示才正确）
        tmp_name = pane_name + "_pop"
        try:
            if cmds.paneLayout(tmp_name, q=True, exists=True):
                cmds.deleteUI(tmp_name)
            cmds.paneLayout(tmp_name,
                            p=LJGT_WINDOW_NAME,
                            width=325, height=600)
            cmds.setParent(tmp_name)
            builder(self._pane_grey)
            cmds.setParent('..')
            ptr = omui.MQtUtil.findControl(tmp_name)
            if not ptr:
                return
            tmp_widget = wrapInstance(int(ptr), QtWidgets.QWidget)
        except Exception:
            return

        # 2. 原页面从 tab 条移除（仍留在 QStackedWidget 里，只是隐藏）
        self.removeTab(index)

        # 3. 临时副本交给独立窗口（父级 Maya 主窗口，保持前置）
        maya_ptr = omui.MQtUtil.mainWindow()
        maya_win = (wrapInstance(int(maya_ptr), QtWidgets.QWidget)
                    if maya_ptr else None)
        detached_win = _DetachedWindow(label, maya_win)

        layout = QtWidgets.QVBoxLayout(detached_win)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        tmp_widget.setParent(None)
        layout.addWidget(tmp_widget)
        tmp_widget.show()

        # 关闭时删除副本、把原页面插回原位
        detached_win.on_closed = self._merge_back

        self._detached[label] = (detached_win, page, index, tmp_name)
        detached_win.resize(325, 600)
        detached_win.show()
        detached_win.raise_()
        detached_win.activateWindow()

    def _merge_back(self, label):
        """关闭独立窗口：删除临时副本 pane，销毁窗口，把原页面插回原位。

        原页面从未离开 QStackedWidget（C++ 与路径一直有效），这里不需要
        搬动任何控件，也不再出现「页面失效 / 合并后布局错乱」的问题。
        """
        entry = self._detached.pop(label, None)
        if entry is None:
            return
        detached_win, page, insert_index = entry[0], entry[1], entry[2]
        tmp_name = entry[3] if len(entry) > 3 else None

        # 1. 删除临时副本（Maya 侧 UI）
        try:
            if tmp_name and cmds.paneLayout(tmp_name, q=True, exists=True):
                cmds.deleteUI(tmp_name)
        except Exception:
            pass

        # 2. hide + deleteLater：不能用 close()，否则会再次触发 closeEvent 递归
        try:
            if _qt_valid(detached_win):
                detached_win.hide()
                detached_win.deleteLater()
        except RuntimeError:
            pass

        # 放回主 QTabWidget 原位置
        try:
            if insert_index < 0 or insert_index > self.count():
                insert_index = self.count()
            self.insertTab(insert_index, page, label)
            self.setCurrentIndex(insert_index)
            page.show()
        except RuntimeError:
            # 主选项卡本体也已被销毁（如工具面板已关闭），只能放弃合并
            return

        # 重挂回 QTabWidget 后 Maya 不会自动重排 pane 内部布局（内容会挤成
        # 一小块、下方大片空白）；用 create_ui 同款手段强制重新布局
        try:
            if cmds.workspaceControl(LJGT_WINDOW_NAME, q=True, exists=True):
                cmds.workspaceControl(LJGT_WINDOW_NAME, e=True, restore=True)
        except Exception:
            pass

    def merge_all(self):
        """合并所有已弹出的独立窗口回主选项卡。"""
        for label in list(self._detached.keys()):
            self._merge_back(label)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos):
        """右键 tab 标题弹出菜单。"""
        index = self._tab_bar.tabAt(pos)
        if index < 0:
            return
        label = self.tabText(index)

        menu = QtWidgets.QMenu(self)
        act_detach = menu.addAction(u"弹出此页：" + label)
        act_detach.triggered.connect(lambda: self._pop_out_tab(index, label))

        if self._detached:
            menu.addSeparator()
            act_merge = menu.addAction(u"合并全部已弹出页")
            act_merge.triggered.connect(self.merge_all)

        menu.exec_(self._tab_bar.mapToGlobal(pos))



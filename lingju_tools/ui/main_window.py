# coding: utf-8
"""LingJuGeometryTools 主窗口：窗口 / 选项卡组装、各功能回调与命名材质管理器。

本模块只保留 UI 与回调绑定；具体功能实现全部来自 features 层模块。
"""

import contextlib
import math
import os
import re
import sys
import traceback
from collections import Counter

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMaya as om
import maya.OpenMayaUI as omui
import maya.api.OpenMaya as oma

try:
    import numpy as np
except ImportError:  # 扭曲 / 边重建依赖 numpy，缺失时由对应功能给出提示
    np = None

from ..core.maya_helpers import MeshError, active_mesh, undo_chunk
from ..core.messaging import lj_error, lj_info, lj_warn
from ..core.qt_compat import QtCore, QtGui, QtWidgets, wrapInstance
from ..core.resources.polyraven_uv_mel import POLYRAVEN_UV_MEL
from ..core.resources.weight_normal_mel import WEIGHT_NORMAL_MEL
from ..core.ui_helpers import _WheelStepper, _ui_short_name
from ..features.maya2marmoset import (
    m2m_scene_assign_material_selection,
    m2m_scene_ensure_shading_group,
    m2m_scene_refresh_material_combo,
    m2m_ui_show,
)
from ..features.modeling.align_drag import Align_Edge, Instant_Drag
from ..features.modeling.bottom_face import (
    BF_MODE_MENU,
    _bf_traceback,
    bf_attach_by_faces,
    bf_choose_bottom_face,
    bf_clear_choices,
)
from ..features.modeling.edge_rebuild import (
    SPLINE_TYPES,
    _rm_traceback,
    bridge,
    chain_info,
    circularize,
    distribute,
    read_points,
    resample,
    smooth,
    unknot,
    write_points,
)
from ..features.modeling.fill_selection import Fill_Selection
from ..features.modeling.ring_split import (
    RING_UI_EDGE_FLOW,
    RING_UI_MODE,
    ring_batch_split,
)
from ..features.modeling.round_inset import Round_Inset, Snap_2_Plane
from ..features.modeling.twist import (
    TWIST_DEFAULT_THRESHOLD,
    TWIST_MAX_THRESHOLD,
    TWIST_MIN_THRESHOLD,
    TWIST_THRESHOLD_OPTION,
    _twist_format_check_result,
    _twist_format_spin_result,
    _twist_traceback,
    twist_check_and_select,
    twist_spin_selected_edges,
    twist_triangulate_selection,
)
from ..features.uv.uv_checker import (
    UV_CHECKER_UI_CHK1,
    UV_CHECKER_UI_CHK2,
    UV_CHECKER_UI_RES,
    _UV_CHECKER_SETTINGS,
    uv_checker_create_shader,
    uv_checker_cycle_shader,
)
from . import components, theme
from .widgets import _DraggableTabWidget

class LingJuGeometryTools:
    LJGT_WINDOW_NAME = "LingJuGeometryTools_Window"

    initial_faces = cmds.ls(sl=True, fl=True)

    def __init__(self):
        self.FILL_SELECTION = None
        self.keep_original = None
        self.separate_radio_grp = None
        self.mode = None
        self.window = None
        self.progress_bar = None
        self.layout_mode = 1
        self.ALIGN_EDGE = Align_Edge()
        self.INSTANT_DRAG = Instant_Drag()
        self.ROUND_INSET = Round_Inset()
        self.FILL_SELECTION = Fill_Selection()
        self.SNAP_2_PLANE = Snap_2_Plane()

        # 配色统一取 ui.theme，改风格只改主题文件
        self.grey_blue = theme.GREY_BLUE
        self.grey = theme.GREY
        self.inner_grey = theme.INNER_GREY
        self.yellow_hex = 0xCC7F00
        self.inner_margins4 = (12, 2, 2, 2)

        # Pivot 边界吸附状态：当前操作平面与三个平面按钮的 UI 名
        self.pivot_plane = 'xy'
        self._pivot_plane_btns = {}

        # 传递UV 的参考物体（Python 侧保存，不做 mel 全局变量往返）
        self._uv_transfer_ref = ""
        self._uv_transfer_ref_label = None

        # 数值框滚轮步进：扭曲阈值 / 按角度选面 / UV 检查 / 边重建共用
        self._wheel_binders = []
        self._wheel_specs = []
        self._wheel_attached = set()

        self.create_ui()

    def _ensure_polyraven_loaded(self):
        """首次调用时用 mel.eval source 内嵌的 PolyravenUV mel，之后不再重复。

        返回 True 表示 proc 已可用，False 表示加载失败（按钮应提示）。
        """
        global _POLYRAVEN_LOADED
        if _POLYRAVEN_LOADED:
            return True
        if not POLYRAVEN_UV_MEL:
            lj_error(u"v170 内嵌的 mel 为空，无法加载")
            return False
        try:
            # Polyraven 依赖的全局变量：$MayaVersion / $prUV_GlobalShellList
            # （Rectangulate 走 shellList 分支时会读后者）
            mel.eval('global int $MayaVersion; $MayaVersion = {};'.format(
                int(cmds.about(version=True).split()[0].split(".")[0])))
            mel.eval('global string $prUV_GlobalShellList[]; $prUV_GlobalShellList = {};')
            mel.eval(POLYRAVEN_UV_MEL)
            _POLYRAVEN_LOADED = True
            return True
        except Exception as error:
            lj_error(u"PolyravenUV 内嵌 mel 加载失败：%s" % error, log=True)
            return False

    """
        ------------------------------------ Layouts ------------------------------------
    """

    def create_ui(self):
        """主界面入口：workspaceControl + QTabWidget 架构。

        流程：
        1. 清理旧窗口 / 独立弹出窗口
        2. 创建 workspaceControl（Maya 原生可停靠面板容器）
        3. 在 workspaceControl 内用 cmds 创建 5 个 paneLayout 桥接容器
        4. 每个 paneLayout 内构建对应功能区的 cmds UI（复用原有子布局）
        5. 通过 MQtUtil.wrapInstance 获取各 paneLayout 的 Qt 句柄
        6. 创建 _DraggableTabWidget，把 5 个 paneLayout 移入作为 tab pages
        7. 把 QTabWidget 放回 workspaceControl 的 Qt layout
        """
        # --- 1. 清理旧窗口 ---
        if cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME, q=True, exists=True):
            cmds.deleteUI(LingJuGeometryTools.LJGT_WINDOW_NAME)
        if cmds.window("prUV_CheckerFlowWin", exists=True):
            cmds.deleteUI("prUV_CheckerFlowWin")

        # 先清理上次运行遗留的弹出独立窗口：原页面留在 QStackedWidget 内，
        # 随 workspaceControl 一起销毁；临时副本 pane 活在独立窗口里，
        # 必须 deleteUI 显式删除，释放其占用的控件名
        for entry in list(_DraggableTabWidget._detached.values()):
            win = entry[0]
            tmp_name = entry[3] if len(entry) > 3 else None
            try:
                win.hide()
            except Exception:
                pass
            try:
                if tmp_name and cmds.paneLayout(tmp_name, q=True, exists=True):
                    cmds.deleteUI(tmp_name)
            except Exception:
                pass
            try:
                win.deleteLater()
            except Exception:
                pass
        _DraggableTabWidget._detached.clear()

        # --- 2. 创建 workspaceControl ---
        # 最小宽度 = 最宽控件(310) + 纵向滚动条(13) + 余量 = 325，
        # 保证默认/最小宽度下滚动条就可见，不用拉宽窗口才露出来
        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                              label=u"成都零距数码工具集v179a1",
                              retain=True,
                              initialWidth=325,
                              minimumWidth=325,
                              mh=650,
                              resizeWidth=2,
                              widthProperty='fixed')

        grey_value = (0.20, 0.16, 0.20)

        # --- 3. 创建 columnLayout 作为 cmds 临时父容器（随后整体移入 QTabWidget） ---
        top_col = cmds.columnLayout(p=LingJuGeometryTools.LJGT_WINDOW_NAME)

        # --- 4. 为每个功能区创建 paneLayout + cmds 子内容 ---
        # paneLayout 是 cmds 与 Qt 之间的桥梁：cmds 命令在 pane 内构建 UI，
        # 之后通过 wrapInstance 拿到 pane 的 Qt 句柄移到 QTabWidget。
        tab_panes = {}

        # tab 1：建模
        pane_m = cmds.paneLayout('ljgt_pane_modeling', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_m)
        self._build_tab_content_modeling(grey_value)
        tab_panes[u"建模"] = pane_m

        # tab 2：选择
        pane_s = cmds.paneLayout('ljgt_pane_selection', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_s)
        self._build_tab_content_selection(grey_value)
        tab_panes[u"选择"] = pane_s

        # tab 3：命名
        pane_n = cmds.paneLayout('ljgt_pane_naming', p=top_col,
                                 width=325, height=500) 
        cmds.setParent(pane_n)
        self._build_tab_content_naming(grey_value)
        tab_panes[u"命名"] = pane_n

        # tab 4：UV
        pane_u = cmds.paneLayout('ljgt_pane_uv', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_u)
        self._build_tab_content_uv(grey_value)
        tab_panes[u"UV"] = pane_u

        # tab 5：烘焙
        pane_b = cmds.paneLayout('ljgt_pane_bake', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_b)
        self._build_tab_content_bake(grey_value)
        tab_panes[u"烘焙"] = pane_b

        # --- 5. Qt 层：获取 workspaceControl 句柄 ---
        ctrl_ptr = omui.MQtUtil.findControl(LingJuGeometryTools.LJGT_WINDOW_NAME)
        ctrl_widget = wrapInstance(int(ctrl_ptr), QtWidgets.QWidget)
        # 统一 Qt 侧外观（选项卡 / 滚动条 / 提示框）
        theme.apply_theme(ctrl_widget)

        # --- 6. 创建 DraggableTabWidget ---
        self._tab_widget = _DraggableTabWidget(ctrl_widget)
        self._tab_widget.setObjectName("LJGT_TabWidget")
        # 存入各 tab 的构建函数：弹出页面若被 Maya 销毁，_merge_back 用它重建
        self._tab_widget._pane_builders = {
            u"建模": ("ljgt_pane_modeling", self._build_tab_content_modeling),
            u"选择": ("ljgt_pane_selection", self._build_tab_content_selection),
            u"命名": ("ljgt_pane_naming", self._build_tab_content_naming),
            u"UV": ("ljgt_pane_uv", self._build_tab_content_uv),
            u"烘焙": ("ljgt_pane_bake", self._build_tab_content_bake),
        }
        self._tab_widget._pane_grey = grey_value

        # --- 7. 把每个 paneLayout 从旧布局移到 QTabWidget ---
        for label, pane_name in tab_panes.items():
            pane_ptr = omui.MQtUtil.findControl(pane_name)
            pane_widget = wrapInstance(int(pane_ptr), QtWidgets.QWidget)
            pane_widget.setParent(None)
            self._tab_widget.addTab(pane_widget, label)

        # --- 8. 清空 workspaceControl 内部旧布局，放入 QTabWidget ---
        ctrl_layout = ctrl_widget.layout()
        if ctrl_layout is not None:
            # 取出所有旧子 widget（scrollLayout 等）并删除
            while ctrl_layout.count() > 0:
                item = ctrl_layout.takeAt(0)
                old_widget = item.widget()
                if old_widget is not None:
                    old_widget.setParent(None)
                    old_widget.deleteLater()
        else:
            ctrl_layout = QtWidgets.QVBoxLayout(ctrl_widget)

        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(0)
        ctrl_layout.addWidget(self._tab_widget)

        # --- 9. 恢复 workspaceControl ---
        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                              e=True, restore=True)

    # ==================================================================
    # 每个选项卡页面的 cmds 内容构建方法
    # （去掉了原来的外层 frameLayout——tab 标签已替代分组角色）
    # ==================================================================

    def _build_tab_content_modeling(self, bg_color):
        """建模选项卡：scrollLayout + columnLayout + 各子折叠栏 + 建模按钮区。"""
        components.scroll_page(row_spacing=2)

        self.create_pivots_settings_layout()
        self.create_ring_split_layout()
        self.create_twist_face_check_layout()
        self.create_quick_bottom_face_layout()
        self.create_edge_rebuild_layout()
        self.create_smart_combine_separate_layout()
        cmds.setParent('..')  # 回到 scrollLayout

        cmds.text(label=" ", height=5)

        # create_modeling_buttons 内部创建了自己的 columnLayout 但不关闭它，
        # 需要调用者补一次 setParent('..')
        self.create_modeling_buttons()
        cmds.setParent('..')  # 关闭 create_modeling_buttons 内部的 columnLayout，回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_selection(self, bg_color):
        """选择选项卡：scrollLayout + columnLayout + 按角度选面 / 相似选择 / 选择按钮。"""
        components.scroll_page()

        self.create_select_by_angle_layout()
        self.create_select_similar_layout()
        cmds.text(label=" ", height=5)

        self.create_selection_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_naming(self, bg_color):
        """命名选项卡：scrollLayout + columnLayout + 命名按钮区。"""
        components.scroll_page()

        self.create_naming_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_uv(self, bg_color):
        """UV 选项卡：scrollLayout + UV检查 + 传递UV + 打直UV 按钮。"""
        components.scroll_page(row_spacing=4)

        self.create_UV_checker_frame()
        self.create_UV_transfer_frame()

        cmds.text(label=" ", height=5)

        # 打直UV：Rectangulate 独立按钮
        components.button(label=u"打直UV",
                    command=lambda *_: self.uv_rectangulate(),
                    width=310, height=28, bgc=self.grey,
                    annotation=u"把当前选中的 UV shell 尽可能拉直成矩形")

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

        # 注：create_UV_checker_frame 和 create_UV_transfer_frame 内部已各自
        #     做过一次展开→折回以保证高度计算正确，这里不再重复

    def _build_tab_content_bake(self, bg_color):
        """烘焙选项卡：scrollLayout + columnLayout + Maya2Marmoset 按钮。"""
        components.scroll_page()

        self.create_bake_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    # ------------------------------------------------------------------
    # 传递 UV（搬运自 PolyravenUV_v1.0.5.mel，直接调 mel proc）
    # ------------------------------------------------------------------
    def create_UV_transfer_frame(self):
        """传递 UV 折叠栏：参考物体 + 四种 transfer 模式

        完全调用 PolyravenUV 的 mel proc（prUV_TransferUV / prUV_GetSelected），
        不转译逻辑，仅提供 Python 侧的 UI 和状态保持。
        """
        frame = components.section(label=u"传递UV", width=299,
                                 bgc=self.grey_blue, collapsable=True, collapse=False,
                                 marginWidth=10, marginHeight=5)
        # 行距与快速底面一致：默认间距，不额外加 rowSpacing
        cmds.columnLayout(adjustableColumn=True)

        # 参考物体显示 label（状态）
        label_text = self._uv_transfer_ref or u"(请先选中参考物体)"
        self._uv_transfer_ref_label = _ui_short_name(cmds.text(label=label_text, align="center"))

        cmds.rowLayout(numberOfColumns=1)
        components.button(label=u"设为参考物体",
                    command=lambda *_: self.uv_transfer_set_reference(),
                    width=275, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"传递UV (ID)",
                    command=lambda *_: self.uv_transfer(4),
                    width=136, height=28, bgc=self.inner_grey)
        components.button(label=u"传递UV (拓扑)",
                    command=lambda *_: self.uv_transfer(5),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"投影UV",
                    command=lambda *_: self.uv_transfer(0),
                    width=136, height=28, bgc=self.inner_grey)
        components.button(label=u"传递形状",
                    command=lambda *_: self.uv_transfer(-1),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.setParent('..')   # columnLayout -> frameLayout
        cmds.setParent('..')   # frameLayout -> UV 的 columnLayout

        # 以 collapse=True 创建时 Maya 不计算子控件高度，展开后会裁剪内容并把
        # 烘焙折叠栏挤出可视区；建完先展开算一次高度，再折回去
        cmds.frameLayout(frame, edit=True, collapse=False)
        cmds.frameLayout(frame, edit=True, collapse=True)

    def uv_transfer_set_reference(self, *args):
        """把当前选中物体设为 TransferUV 的参考对象（包装 mel 调用）"""
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        sel = cmds.ls(selection=True, long=True) or []
        if not sel:
            lj_warn(u"请先选中一个物体作为参考")
            return
        # Polyraven 用最后选中的物体当参考，这里显式把最后一个设进去
        last = sel[-1]
        self._uv_transfer_ref = last
        cmds.text(self._uv_transfer_ref_label, edit=True, label=last)
        lj_info(u"参考物体已设置：%s" % last)

    def uv_transfer(self, mode):
        """调用 PolyravenUV.prUV_TransferUV 把参考物体的 UV/形状传给当前选中物体

        :param mode: int，对应 Polyraven 的 sampleSpace / transferMode
                     4 = Transfer UV (ID, sampleSpace=4)
                     5 = Transfer UV (Topo, sampleSpace=5)
                     0 = Project UV (sampleSpace=0)
                    -1 = Transfer Shape（走 transferPositions）
        """
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        ref_obj = self._uv_transfer_ref
        if not ref_obj:
            lj_warn(u"请先设置参考物体")
            return
        sel = cmds.ls(selection=True, long=True) or []
        if not sel:
            lj_warn(u"请先选中要接收 UV 的目标物体")
            return
        try:
            # Polyraven 的 TransferUV 逻辑：它内部用 `ls -os -o -l` 取当前 selection；
            # 这里把目标 + 参考一起选上再交给它
            cmds.select(sel, replace=True)
            cmds.select(ref_obj, add=True)
            mel.eval('prUV_TransferUV("{}", {});'.format(ref_obj.replace('\\', '/'), mode))
            cmds.select(sel, replace=True)  # 恢复原选择
        except Exception as error:
            lj_error(u"TransferUV 执行失败：%s" % error)

    def uv_rectangulate(self, *args):
        """调用 PolyravenUV.prUV_RectangulateShell 把当前 UV shell 打直成矩形"""
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        sel = cmds.ls(selection=True) or []
        if not sel:
            lj_warn(u"请先选中要打直的 UV shell 或面")
            return
        try:
            mel.eval('prUV_RectangulateShell();')
        except Exception as error:
            lj_error(u"打直UV 执行失败：%s" % error)

    def create_bake_buttons(self):
        cmds.rowLayout(numberOfColumns=1)
        components.button(label=u"Maya2Marmoset", command=self.show_maya2marmoset,
                    width=300, height=28, bgc=self.grey,
                    annotation=u"打开 Maya2Marmoset 桥接面板：自动分组、导出 FBX 并推送给 Toolbag")
        cmds.setParent('..')

    def show_maya2marmoset(self, *args):
        try:
            m2m_ui_show()
        except RecursionError as error:
            LingJuGeometryTools.report_recursion(error)

    @staticmethod
    def report_recursion(error, head=3, top=3):
        """爆栈时只报「哪些位置在反复」：同一行出现 N 次就折叠成 xN。

        脚本编辑器里 RecursionError 会刷上千行帧，真正有用的是重复段。对敲的两个
        函数是交替出现的，所以按位置计数（而不是只压相邻重复）才能把它们点出来。
        """
        from collections import Counter
        import traceback
        names = ['%s:%s %s()' % (os.path.basename(frame.filename),
                                 frame.lineno, frame.name)
                 for frame in traceback.extract_tb(error.__traceback__)]
        if not names:
            print('Maya2Marmoset: 递归爆栈，但拿不到栈帧')
            lj_error(u'Maya2Marmoset: 面板打开失败（递归爆栈），详见 Script Editor')
            return
        counts = Counter(names)
        lj_error(u'Maya2Marmoset: 面板打开失败（递归爆栈），详见 Script Editor')
        print('Maya2Marmoset: 递归爆栈（limit=%s，共 %s 帧 / %s 个位置）' % (
            sys.getrecursionlimit(), len(names), len(counts)))
        for name in names[:head]:
            print('    入口 %s  共 x%s' % (name, counts[name]))
        for name, count in counts.most_common(top):
            if count > 1:
                print('    重复 x%-6s %s' % (count, name))

    def create_UV_checker_frame(self):
        """UV 检查材质：内嵌在 UV 布局里，不弹窗。"""
        components.section(label=u"UV检查材质", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True,
                         marginWidth=10, marginHeight=5,
                         expandCommand=self._wheel_attach_all)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

        cmds.rowColumnLayout(numberOfColumns=2,
                             columnWidth=[(1, 96), (2, 80)],
                             columnAttach=[(1, "both", 2), (2, "both", 2)])
        cmds.text(label="贴图大小:", align="right")
        res_field = components.int_field(UV_CHECKER_UI_RES, value=_UV_CHECKER_SETTINGS["resolution"],
                                  minValue=0, maxValue=8192, step=256)
        self._wheel_register(res_field, integer=True, step=256, low=0, high=8192)
        cmds.text(label="棋盘格1大小", align="right")
        checker1_field = components.int_field(UV_CHECKER_UI_CHK1, value=_UV_CHECKER_SETTINGS["checker1"],
                                       minValue=0, maxValue=1024, step=256)
        self._wheel_register(checker1_field, integer=True, step=256, low=0, high=1024)
        cmds.text(label="棋盘格2大小", align="right")
        checker2_field = components.int_field(UV_CHECKER_UI_CHK2, value=_UV_CHECKER_SETTINGS["checker2"],
                                       minValue=4, step=8)
        self._wheel_register(checker2_field, integer=True, step=8, low=4)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label="创建/更新", width=136, height=28, bgc=self.inner_grey,
                    command=lambda *_: self.uv_checker_apply())
        components.button(label="循环指定材质", width=136, height=28, bgc=self.inner_grey,
                    command=lambda *_: uv_checker_cycle_shader())
        cmds.setParent('..')

        cmds.setParent('..')
        cmds.setParent('..')

    def uv_checker_apply(self):
        """读取输入框并创建/更新 UV 检查材质。"""
        _UV_CHECKER_SETTINGS.update(
            resolution=components.int_field(UV_CHECKER_UI_RES, query=True, value=True),
            checker1=components.int_field(UV_CHECKER_UI_CHK1, query=True, value=True),
            checker2=components.int_field(UV_CHECKER_UI_CHK2, query=True, value=True))
        uv_checker_create_shader(_UV_CHECKER_SETTINGS["resolution"],
                                 _UV_CHECKER_SETTINGS["checker1"],
                                 _UV_CHECKER_SETTINGS["checker2"])

    def open_modeling_fold(self, label):
        """建模区折叠栏统一规格：同宽、同内边距、默认收起；栏间距由父列 rowSpacing 统一给"""
        frame = components.section(label=label, width=299,
                                 bgc=self.grey_blue, collapsable=True, collapse=True,
                                 marginWidth=10, marginHeight=5)
        cmds.columnLayout()
        return frame

    def close_modeling_fold(self):
        cmds.setParent('..')
        cmds.setParent('..')

    def create_twist_face_check_layout(self):
        """扭曲面检查折叠栏：内嵌 twist_face_check.py 的检查/三角化/Spin 逻辑"""
        fold = self.open_modeling_fold(u"扭曲面检查")
        cmds.frameLayout(fold, edit=True, expandCommand=self._wheel_attach_all)

        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=3,
                       columnAttach3=("left", "left", "left"),
                       columnWidth3=(52, 172, 50), columnOffset3=(0, 2, 2))
        cmds.text(label=u"扭曲阈值", align="left", width=48, height=18)
        self._twist_threshold_slider = _ui_short_name(components.slider(
            minValue=TWIST_MIN_THRESHOLD, maxValue=TWIST_MAX_THRESHOLD,
            step=1, value=self._twist_load_threshold(), width=172,
            changeCommand=self._twist_slider_changed,
            dragCommand=self._twist_slider_changed,
            annotation=u"四边面两个三角化三角形法线的夹角阈值（度）"))
        self._twist_threshold_field = _ui_short_name(components.float_field(
            minValue=TWIST_MIN_THRESHOLD, maxValue=TWIST_MAX_THRESHOLD,
            step=1, value=self._twist_load_threshold(), precision=1,
            width=50, changeCommand=self._twist_field_changed))
        self._wheel_register(
            self._twist_threshold_field, integer=False, step=1,
            callback=self._twist_field_wheel_changed,
            low=TWIST_MIN_THRESHOLD, high=TWIST_MAX_THRESHOLD, precision=1)
        cmds.setParent('..')
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"检查并选择", command=lambda *args: self.twist_check_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"只检查四边面，选中法线夹角超过阈值的四边面。\n"
                               u"有选择时只检查当前选择；无选择时扫描全场景。\n"
                               u"结果在视口提示中显示。")
        components.button(label=u"三角化", command=lambda *args: self.twist_triangulate_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"对当前选择执行 Maya 原生 polyTriangulate。\n"
                               u"通常先检查并选中扭曲四边面，再点击本按钮。")
        cmds.setParent('..')

        cmds.columnLayout()
        components.button(label=u"Spin 连线", command=lambda *args: self.twist_spin_clicked(),
                    width=275, height=28, bgc=self.inner_grey,
                    annotation=u"只对当前选中的边执行 polyFlipEdge。\n"
                               u"请先三角化，再选中内部连线；三角面、边界边、非流形边自动忽略。")
        cmds.setParent('..')

        self.close_modeling_fold()

    def _twist_load_threshold(self):
        """从 optionVar 读取扭曲阈值，缺省 5.0"""
        try:
            if cmds.optionVar(exists=TWIST_THRESHOLD_OPTION):
                value = float(cmds.optionVar(query=TWIST_THRESHOLD_OPTION))
                return max(TWIST_MIN_THRESHOLD, min(TWIST_MAX_THRESHOLD, value))
        except Exception:
            pass
        return TWIST_DEFAULT_THRESHOLD

    def _twist_slider_changed(self, value, *args):
        self._twist_set_threshold(value, from_slider=True)

    def _twist_field_changed(self, value, *args):
        self._twist_set_threshold(value, from_slider=False)

    def _twist_field_wheel_changed(self):
        """滚轮已经改过输入框，这里把滑条与 optionVar 同步过去。"""
        value = components.float_field(self._twist_threshold_field, query=True, value=True)
        self._twist_set_threshold(value, from_slider=False)

    def _twist_set_threshold(self, value, from_slider=True):
        """滑条与输入框互相同步，并把阈值存回 optionVar"""
        try:
            value = max(TWIST_MIN_THRESHOLD, min(TWIST_MAX_THRESHOLD, float(value)))
            if from_slider:
                components.float_field(self._twist_threshold_field, edit=True, value=value)
            else:
                components.slider(self._twist_threshold_slider, edit=True, value=value)
            cmds.optionVar(fv=(TWIST_THRESHOLD_OPTION, value))
        except Exception:
            pass

    def _twist_save_threshold(self):
        """把滑条当前值存回 optionVar"""
        try:
            value = components.slider(self._twist_threshold_slider, query=True, value=True)
            cmds.optionVar(fv=(TWIST_THRESHOLD_OPTION, float(value)))
        except Exception:
            pass

    def _twist_numpy_missing(self):
        if np is None:
            lj_error(u"未找到 NumPy，扭曲面检查不可用（需 Maya 2022+）")
            return True
        return False

    def twist_check_clicked(self, *args):
        if self._twist_numpy_missing():
            return
        self._twist_save_threshold()
        threshold = components.slider(self._twist_threshold_slider, query=True, value=True)
        try:
            result = twist_check_and_select(threshold)
            text = _twist_format_check_result(result, threshold)
            lj_info(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"扭曲检查失败：%s" % error)

    def twist_triangulate_clicked(self, *args):
        try:
            text = twist_triangulate_selection()
            if text.startswith(u"已对当前选择"):
                lj_info(text)
            else:
                lj_warn(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"三角化失败：%s" % error)

    def twist_spin_clicked(self, *args):
        if self._twist_numpy_missing():
            return
        self._twist_save_threshold()
        try:
            result = twist_spin_selected_edges()
            text = _twist_format_spin_result(result)
            if result.failed:
                lj_warn(text)
            else:
                lj_info(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"Spin 失败：%s" % error)

    def create_quick_bottom_face_layout(self):
        """快速底面折叠栏：内嵌 SGCG Placement Tool 的候选底面生成与绿面吸附"""
        self.open_modeling_fold(u"快速底面")

        cmds.columnLayout()
        components.option_menu(BF_MODE_MENU, label=u"候选模式：", width=280)
        cmds.menuItem(label=u"凸包(高模)")
        cmds.menuItem(label=u"六面安全(包围盒)")
        components.option_menu(BF_MODE_MENU, e=True, select=1)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"生成候选底面", command=lambda *args: self.bf_place_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"选中一个网格物体后点击，会在模型各侧面生成绿色候选面。\n"
                               u"再点任意一个绿色面片，模型就把那一面朝下、贴地并冻结变换。\n"
                               u"凸包(高模)适合非盒子状的高模；六面安全只用包围盒，永远有结果。")
        components.button(label=u"清除候选", command=lambda *args: self.bf_clear_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"删除场景里的绿色候选面，并停止点选放置的监听。")
        cmds.setParent('..')

        cmds.columnLayout()
        components.button(label=u"对齐两个候选面", command=lambda *args: self.bf_attach_clicked(),
                    width=275, height=28, bgc=self.inner_grey,
                    annotation=u"先选目标物体，再选要移动的物体，然后点击本按钮。\n"
                               u"两个物体会各生成一组绿色候选面：\n"
                               u"先点目标物体的面，再点待移动物体的面，即旋转+平移贴合到该面上。")
        cmds.setParent('..')
        self.close_modeling_fold()

    def bf_place_clicked(self, *args):
        try:
            bf_choose_bottom_face()
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：生成候选面失败：%s" % error)

    def bf_clear_clicked(self, *args):
        try:
            bf_clear_choices()
            lj_info(u"快速底面：已清除候选面")
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：清除候选面失败：%s" % error)

    def bf_attach_clicked(self, *args):
        try:
            bf_attach_by_faces()
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：生成对齐候选面失败：%s" % error)

    # ------------------------------------------------------------------
    # 边重建（内嵌自 ResampleMesh_Maya.py）
    # ------------------------------------------------------------------
    def create_edge_rebuild_layout(self):
        """边重建折叠栏：实时预览在最外层，五个子折叠栏放各运算符。"""
        self._rm_preview_cache = None
        self._rm_two_chain_key = None
        self._rm_two_chain_current = []
        self._rm_two_chain_info = []

        fold = self.open_modeling_fold(u"边重建")
        cmds.frameLayout(fold, edit=True, expandCommand=self._wheel_attach_all)

        self._rm_preview_cb = _ui_short_name(components.check_box(
            label=u"实时预览", value=False, align="left", width=255,
            changeCommand=self._rm_preview_toggled,
            annotation=u"勾选后先缓存当前网格的原始点位：修改参数时先还原再重新计算。\n"
                       u"只对改点位的操作生效；重采样预览固定为只调点位、不改点数/拓扑。"))

        self._rm_build_resample()
        self._rm_build_smooth()
        self._rm_build_circularize()
        self._rm_build_unknot()
        self._rm_build_bridge()

        self.close_modeling_fold()

        # 建 UI 时 workspaceControl 还没显示，控件未必有 Qt 实件；等界面出来再挂滚轮
        cmds.evalDeferred(self._wheel_attach_all, lowestPriority=True)

    def _rm_open_fold(self, label):
        """边重建的子折叠栏：比外层窄一圈，配色与建模区其它折叠栏一致。"""
        frame = components.section(label=label, width=277,
                                 bgc=self.grey, collapsable=True, collapse=True,
                                 marginWidth=8, marginHeight=4,
                                 expandCommand=self._wheel_attach_all)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=3)
        return frame

    def _rm_close_fold(self, frame):
        cmds.setParent('..')
        cmds.setParent('..')
        # 以 collapse=True 创建时 Maya 不计算子控件高度，先展开算一次再折回
        cmds.frameLayout(frame, edit=True, collapse=False)
        cmds.frameLayout(frame, edit=True, collapse=True)

    def _rm_open_fields(self):
        # 新版 Maya 的 rowSpacing/columnSpacing 要求 [序号, 间距] 成对；旧版也接受
        cmds.rowColumnLayout(numberOfColumns=2,
                             columnWidth=[(1, 92), (2, 158)],
                             columnAttach=[(1, "right", 4), (2, "left", 4)],
                             rowSpacing=[(1, 3), (2, 3), (3, 3), (4, 3)],
                             columnSpacing=[(2, 4)])

    def _rm_number_field(self, integer, step, callback, **kwargs):
        """数值框：悬停滚轮按 step 调节，和 ResampleMesh_Maya 的 QSpinBox 一致。"""
        factory = cmds.intField if integer else cmds.floatField
        if callback is not None:
            kwargs["changeCommand"] = lambda *_: callback()
        kwargs.setdefault("width", 158)
        low = kwargs.get("minValue")
        high = kwargs.get("maxValue")
        precision = kwargs.get("precision", 3)
        control = factory(step=step, **kwargs)
        short = _ui_short_name(control)
        self._wheel_register(short, integer, step, callback, low, high, precision)
        return short

    def _wheel_register(self, control, integer, step, callback=None,
                        low=None, high=None, precision=3, bump=None):
        """登记悬停滚轮步进；bump 用于 intSliderGrp 这类要整体改值的控件。"""
        # 重挂到选项卡/弹出窗口后全路径失效，统一存短名
        control = _ui_short_name(control)
        spec = (control, integer, step, callback, low, high, precision, bump)
        self._wheel_specs.append(spec)
        if self._attach_wheel(*spec):
            self._wheel_attached.add(control)
        return control

    def _wheel_apply_step(self, control, step, sign, callback, integer, low, high, precision):
        factory = cmds.intField if integer else cmds.floatField
        value = factory(control, query=True, value=True) + sign * step
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        if integer:
            value = int(round(value))
        else:
            value = round(value, precision)
        factory(control, edit=True, value=value)
        if callback is not None:
            callback()

    def _find_ui_widget(self, control):
        """按控件名找 Qt 实件。

        先试全路径（未重挂时最快）；重挂到选项卡 / 弹出独立窗口后全路径失效，
        改按短名遍历顶层窗口找 objectName（Maya 把控件短名写入 objectName）。
        """
        try:
            pointer = omui.MQtUtil.findControl(control)
            if pointer:
                return wrapInstance(int(pointer), QtWidgets.QWidget)
        except Exception:
            pass
        name = _ui_short_name(control)
        app = QtWidgets.QApplication.instance()
        if app is None:
            return None
        for top in app.topLevelWidgets():
            try:
                widget = top.findChild(QtWidgets.QWidget, name)
            except RuntimeError:
                continue
            if widget is not None:
                return widget
        return None

    def _attach_wheel(self, control, integer, step, callback, low, high, precision, bump=None):
        """悬停滚轮改值：上滚 +step、下滚 -step，和 Qt SpinBox 的手感一致。

        返回是否挂上；控件还没有 Qt 实件时返回 False，交给 _wheel_attach_all 重试。
        """
        try:
            widget = self._find_ui_widget(control)
            if widget is None:
                return False
            binder = _WheelStepper(
                widget,
                lambda delta: self._wheel_step(
                    control, integer, step, callback, low, high, precision, bump, delta))
            self._wheel_binders.append(binder)
            targets = [widget] + widget.findChildren(QtWidgets.QWidget)
            for target in targets:
                target.installEventFilter(binder)
            return True
        except Exception:
            # Qt 侧不可用时退回到 Ctrl+拖拽，不影响 UI 构建
            return False

    def _wheel_attach_all(self, *args):
        """界面显示后再挂一次，并重试几轮：滚动布局/折叠栏可能延后建出 Qt 实件。"""
        self._wheel_attach_pass(0)

    def _wheel_attach_pass(self, attempt):
        pending = False
        for spec in self._wheel_specs:
            if spec[0] in self._wheel_attached:
                continue
            if self._attach_wheel(*spec):
                self._wheel_attached.add(spec[0])
            else:
                pending = True
        if pending and attempt < 5:
            cmds.evalDeferred(lambda: self._wheel_attach_pass(attempt + 1),
                              lowestPriority=True)

    def _wheel_step(self, control, integer, step, callback, low, high, precision, bump, delta):
        if not cmds.control(control, query=True, enable=True):
            return
        sign = 1 if delta > 0 else -1
        if bump is not None:
            bump(sign)
            return
        self._wheel_apply_step(control, step, sign, callback, integer, low, high, precision)

    def _rm_set_enabled(self, control, enabled):
        cmds.control(control, edit=True, enable=enabled)

    def _rm_check(self, control):
        return bool(components.check_box(control, query=True, value=True))

    def _rm_spline_value(self):
        return components.option_menu(self._rm_spline, query=True, value=True)

    def _rm_numpy_missing(self):
        if np is None:
            lj_error(u"未找到 NumPy，边重建不可用（需 Maya 2022+）")
            return True
        return False

    def _rm_build_resample(self):
        frame = self._rm_open_fold(u"重采样")

        self._rm_open_fields()
        cmds.text(label=u"采样类型", align="right")
        self._rm_spline = _ui_short_name(components.option_menu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("resample")))
        for name in SPLINE_TYPES:
            cmds.menuItem(label=name)
        components.option_menu(self._rm_spline, edit=True, select=1)

        cmds.text(label=u"增减点数", align="right")
        self._rm_step = self._rm_number_field(
            True, 1, lambda: self._rm_preview_apply("resample"),
            minValue=-1000, maxValue=1000, value=4,
            annotation=u">0 加点，<0 减点，0 只重排点位")
        cmds.text(label=u"多项式参数化", align="right")
        self._rm_p_tension = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("resample"),
            minValue=0.15, maxValue=0.5, value=0.15, precision=3,
            annotation=u"POLYNOMIAL 的弦长指数（原版 Ctrl+滚轮）")
        cmds.setParent('..')

        self._rm_space = _ui_short_name(components.check_box(
            label=u"按弧长均匀分布", value=False, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("resample"),
            annotation=u"按弧长重新分布采样点（原版 T 键）"))
        self._rm_auto_clean = _ui_short_name(components.check_box(
            label=u"自动清理共线边", value=False, align="left", width=255,
            annotation=u"重连后溶解共面的新边（原版 Shift+C）"))

        self._rm_two_chain = _ui_short_name(components.check_box(
            label=u"双链分别设点数", value=False, align="left", width=255,
            changeCommand=self._rm_two_chain_toggled,
            annotation=u"正好选中两条开放边链时，分别指定各自的目标点数。\n"
                       u"两条链点数相同时可把不对称的一对补齐。"))

        self._rm_open_fields()
        cmds.text(label=u"链 1 点数", align="right")
        self._rm_chain_a_count = self._rm_number_field(
            True, 1, self._rm_update_chain_labels,
            minValue=3, maxValue=1000, value=3)
        cmds.text(label=u"链 2 点数", align="right")
        self._rm_chain_b_count = self._rm_number_field(
            True, 1, self._rm_update_chain_labels,
            minValue=3, maxValue=1000, value=3)
        cmds.setParent('..')

        self._rm_chain_a_label = _ui_short_name(cmds.text(label=u"链 1：未启用", align="left", width=255))
        self._rm_chain_b_label = _ui_short_name(cmds.text(label=u"链 2：未启用", align="left", width=255))

        cmds.rowLayout(numberOfColumns=2)
        self._rm_read_two = _ui_short_name(components.button(
            label=u"读取两链", command=lambda *args: self._rm_read_two_clicked(),
            width=126, height=26, bgc=self.inner_grey,
            annotation=u"读取当前选中的两条开放边链，并填入各自现有点数"))
        self._rm_equal_two = _ui_short_name(components.button(
            label=u"两链同点数", command=lambda *args: self._rm_equalize_counts(),
            width=126, height=26, bgc=self.inner_grey,
            annotation=u"把链 2 的目标点数设为与链 1 相同"))
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"重采样", command=lambda *args: self._rm_run("resample"),
                    width=126, height=28, bgc=self.inner_grey,
                    annotation=u"按当前样条与点数重采样选中的开放边链")
        components.button(label=u"均匀分布", command=lambda *args: self._rm_run("distribute"),
                    width=126, height=28, bgc=self.inner_grey,
                    annotation=u"Akima + 弧长均匀分布，只改点位不动拓扑（原版 D 键）")
        cmds.setParent('..')

        self._rm_two_chain_toggled()
        self._rm_close_fold(frame)

    def _rm_build_smooth(self):
        frame = self._rm_open_fold(u"平滑")

        self._rm_open_fields()
        cmds.text(label=u"迭代次数", align="right")
        self._rm_smooth_iterations = self._rm_number_field(
            True, 1, lambda: self._rm_preview_apply("smooth"),
            minValue=1, maxValue=50, value=1)
        cmds.text(label=u"强度", align="right")
        self._rm_smooth_factor = self._rm_number_field(
            False, 0.05, lambda: self._rm_preview_apply("smooth"),
            minValue=0.001, maxValue=1.0, value=0.5, precision=3)
        cmds.text(label=u"权重", align="right")
        self._rm_smooth_weight = _ui_short_name(components.option_menu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("smooth")))
        cmds.menuItem(label="distance")
        cmds.menuItem(label="uniform")
        components.option_menu(self._rm_smooth_weight, edit=True, select=1)
        cmds.text(label=u"类型", align="right")
        self._rm_smooth_type = _ui_short_name(components.option_menu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("smooth")))
        cmds.menuItem(label="SMOOTH")
        cmds.menuItem(label="SHARP")
        components.option_menu(self._rm_smooth_type, edit=True, select=1)
        cmds.setParent('..')

        components.button(label=u"平滑", command=lambda *args: self._rm_run("smooth"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"SMOOTH = Laplacian 平滑，SHARP = Taubin 先平滑再反向锐化")
        self._rm_close_fold(frame)

    def _rm_build_circularize(self):
        frame = self._rm_open_fold(u"圆化")

        self._rm_open_fields()
        cmds.text(label=u"角度", align="right")
        self._rm_arc_angle = self._rm_number_field(
            False, 5.0, lambda: self._rm_preview_apply("circularize"),
            minValue=0.1, maxValue=359.9, value=90.0, precision=2)
        cmds.setParent('..')

        self._rm_arc_flip = _ui_short_name(components.check_box(
            label=u"反向", value=False, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("circularize")))

        components.button(label=u"圆化", command=lambda *args: self._rm_run("circularize"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"把选中的开放边链拟合成圆弧，链的两个端点保持不动")
        self._rm_close_fold(frame)

    def _rm_build_unknot(self):
        frame = self._rm_open_fold(u"解结")

        self._rm_open_fields()
        cmds.text(label=u"宽度", align="right")
        self._rm_unknot_width = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("unknot"),
            minValue=-1000.0, maxValue=1000.0, value=0.0, precision=4)
        cmds.text(label=u"张力", align="right")
        self._rm_unknot_tension = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("unknot"),
            minValue=-1.0, maxValue=1.0, value=0.5, precision=3)
        cmds.text(label=u"采样类型", align="right")
        self._rm_unknot_spline = _ui_short_name(components.option_menu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("unknot")))
        for name in SPLINE_TYPES:
            cmds.menuItem(label=name)
        components.option_menu(self._rm_unknot_spline, edit=True, select=1)
        cmds.setParent('..')

        self._rm_unknot_uniform = _ui_short_name(components.check_box(
            label=u"均匀", value=True, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("unknot"),
            annotation=u"按弧长均匀分布内部点"))

        components.button(label=u"解结", command=lambda *args: self._rm_run("unknot"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"用两端切线引导中间点，缓解打结和扭曲")
        self._rm_close_fold(frame)

    def _rm_build_bridge(self):
        frame = self._rm_open_fold(u"桥接")

        self._rm_open_fields()
        cmds.text(label=u"匹配模式", align="right")
        self._rm_bridge_mode = _ui_short_name(components.option_menu(width=158))
        cmds.menuItem(label="shortest")
        cmds.menuItem(label="basic")
        components.option_menu(self._rm_bridge_mode, edit=True, select=1)
        cmds.text(label=u"扭转", align="right")
        self._rm_bridge_twist = self._rm_number_field(
            True, 1, None, minValue=-1000, maxValue=1000, value=0)
        cmds.setParent('..')

        self._rm_bridge_connect = _ui_short_name(components.check_box(
            label=u"仅生成连接边", value=True, align="left", width=255,
            annotation=u"勾选：只生成连接边（原版 C 键）；\n"
                       u"取消：用 polyBridgeEdge 生成桥接面（要求边循环是开放边界）"))
        self._rm_bridge_reverse = _ui_short_name(components.check_box(
            label=u"反转", value=False, align="left", width=255))

        components.button(label=u"桥接", command=lambda *args: self._rm_run("bridge"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"在两个或多个边循环之间桥接")
        self._rm_close_fold(frame)

    # -- 实时预览 --------------------------------------------------------

    def _rm_preview_toggled(self, *args):
        if not self._rm_check(self._rm_preview_cb):
            self._rm_preview_cache = None
            lj_info(u"实时预览已关闭，当前结果保留")
            return
        self._rm_capture_preview()

    def _rm_capture_preview(self, report=True):
        try:
            transform, shape = active_mesh()
        except MeshError as exc:
            if report:
                lj_warn(str(exc))
            return False
        self._rm_preview_cache = (shape, read_points(shape))
        if report:
            lj_info(u"已缓存原始点位：%s" % transform)
        return True

    def _rm_preview_apply(self, kind):
        if not self._rm_check(self._rm_preview_cb):
            return
        if self._rm_numpy_missing():
            return
        if self._rm_preview_cache is None and not self._rm_capture_preview():
            return
        try:
            _, current = active_mesh()
        except MeshError as exc:
            lj_warn(str(exc))
            return
        shape, points = self._rm_preview_cache
        if current != shape or not cmds.objExists(shape):
            if not self._rm_capture_preview():
                return
            shape, points = self._rm_preview_cache
        try:
            with undo_chunk(u"边重建预览"):
                write_points(shape, points)
                self._rm_apply(kind, preview=True)
        except MeshError as exc:
            lj_warn(str(exc))
        except Exception as exc:
            _rm_traceback.print_exc()
            lj_error(u"边重建预览失败：%s" % exc)

    def _rm_after_apply(self):
        if self._rm_check(self._rm_preview_cb):
            self._rm_capture_preview(report=False)

    # -- 运算符调度 ------------------------------------------------------

    def _rm_apply(self, kind, preview=False):
        if kind == "resample":
            self._rm_resample(preview)
        elif kind == "distribute":
            self._rm_distribute(preview)
        elif kind == "smooth":
            self._rm_smooth(preview)
        elif kind == "circularize":
            self._rm_circularize(preview)
        elif kind == "unknot":
            self._rm_unknot(preview)
        elif kind == "bridge" and not preview:
            self._rm_bridge(preview)

    def _rm_run(self, kind):
        if self._rm_numpy_missing():
            return
        try:
            self._rm_apply(kind)
        except MeshError as exc:
            lj_error(u"边重建：%s" % exc)
        except Exception as exc:
            _rm_traceback.print_exc()
            lj_error(u"边重建：%s" % exc)

    def _rm_resample(self, preview=False):
        if not preview and self._rm_check(self._rm_two_chain):
            if not self._rm_two_chain_valid_for_apply():
                return
            counts = [components.int_field(self._rm_chain_a_count, query=True, value=True),
                      components.int_field(self._rm_chain_b_count, query=True, value=True)]
            chain_count = resample(
                step=0,
                spline_type=self._rm_spline_value(),
                space=self._rm_check(self._rm_space),
                p_tension=components.float_field(self._rm_p_tension, query=True, value=True),
                auto_clean=self._rm_check(self._rm_auto_clean),
                counts=counts)
            self._rm_sync_chain_counts(report=False)
            lj_info(u"已按两链点数重采样 %d 条链：链 1 %d 点，链 2 %d 点。"
                    % (chain_count, counts[0], counts[1]))
            self._rm_after_apply()
            return
        count = resample(
            step=0 if preview else int(components.int_field(self._rm_step, query=True, value=True)),
            spline_type=self._rm_spline_value(),
            space=self._rm_check(self._rm_space),
            p_tension=components.float_field(self._rm_p_tension, query=True, value=True),
            auto_clean=self._rm_check(self._rm_auto_clean),
            positions_only=preview)
        if not preview:
            lj_info(u"已重采样 %d 条边链。" % count)
            self._rm_after_apply()

    def _rm_distribute(self, preview=False):
        count = distribute()
        if not preview:
            lj_info(u"已均匀分布 %d 条边链（拓扑未改动）。" % count)
            self._rm_after_apply()

    def _rm_smooth(self, preview=False):
        count = smooth(
            iterations=int(components.int_field(self._rm_smooth_iterations, query=True, value=True)),
            factor=components.float_field(self._rm_smooth_factor, query=True, value=True),
            weight_type=components.option_menu(self._rm_smooth_weight, query=True, value=True),
            smooth_type=components.option_menu(self._rm_smooth_type, query=True, value=True))
        if not preview:
            lj_info(u"已平滑 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_circularize(self, preview=False):
        count = circularize(
            angle=components.float_field(self._rm_arc_angle, query=True, value=True),
            flip=self._rm_check(self._rm_arc_flip))
        if not preview:
            lj_info(u"已圆化 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_unknot(self, preview=False):
        count = unknot(
            width=components.float_field(self._rm_unknot_width, query=True, value=True),
            tension=components.float_field(self._rm_unknot_tension, query=True, value=True),
            spline_type=components.option_menu(self._rm_unknot_spline, query=True, value=True),
            uniform=self._rm_check(self._rm_unknot_uniform))
        if not preview:
            lj_info(u"已解结 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_bridge(self, preview=False):
        count = bridge(
            connect=self._rm_check(self._rm_bridge_connect),
            mode=components.option_menu(self._rm_bridge_mode, query=True, value=True),
            reverse=self._rm_check(self._rm_bridge_reverse),
            twist=int(components.int_field(self._rm_bridge_twist, query=True, value=True)))
        lj_info(u"已桥接 %d 个循环。" % count)

    # -- 双链点数 --------------------------------------------------------

    def _rm_two_chain_toggled(self, *args):
        enabled = self._rm_check(self._rm_two_chain)
        for control in (self._rm_chain_a_count, self._rm_chain_b_count,
                        self._rm_read_two, self._rm_equal_two,
                        self._rm_chain_a_label, self._rm_chain_b_label):
            self._rm_set_enabled(control, enabled)
        self._rm_set_enabled(self._rm_step, not enabled)
        if not enabled:
            self._rm_two_chain_key = None
            self._rm_two_chain_current = []
            self._rm_two_chain_info = []
            self._rm_set_chain_labels(u"链 1：未启用", u"链 2：未启用")
            return
        self._rm_sync_chain_counts()

    def _rm_set_chain_labels(self, first, second):
        cmds.text(self._rm_chain_a_label, edit=True, label=first)
        cmds.text(self._rm_chain_b_label, edit=True, label=second)

    def _rm_sync_chain_counts(self, report=True):
        self._rm_two_chain_key = None
        self._rm_two_chain_current = []
        self._rm_two_chain_info = []
        try:
            info = chain_info()
        except MeshError as exc:
            self._rm_set_chain_labels(u"链 1：未读取", u"链 2：未读取")
            if report:
                lj_warn(str(exc))
            return False
        if len(info) != 2:
            self._rm_set_chain_labels(u"链 1：未读取", u"链 2：未读取")
            if report:
                lj_warn(u"双链点数需要正好选中 2 条开放边链（当前 %d 条）。" % len(info))
            return False
        self._rm_two_chain_key = [(item["start_vertex"], item["end_vertex"])
                                  for item in info]
        self._rm_two_chain_current = [item["count"] for item in info]
        self._rm_two_chain_info = info
        for control, item in zip((self._rm_chain_a_count, self._rm_chain_b_count), info):
            components.int_field(control, edit=True, value=item["count"])
        self._rm_update_chain_labels()
        if report:
            lj_info(u"已读取两链：链 1 %d 点，链 2 %d 点。"
                    % (info[0]["count"], info[1]["count"]))
        return True

    def _rm_update_chain_labels(self):
        for index, (label, field) in enumerate(
                zip((self._rm_chain_a_label, self._rm_chain_b_label),
                    (self._rm_chain_a_count, self._rm_chain_b_count))):
            if index >= len(self._rm_two_chain_current):
                cmds.text(label, edit=True,
                          label=u"链 %d：未读取（点「读取两链」）" % (index + 1))
                continue
            current = self._rm_two_chain_current[index]
            target = int(components.int_field(field, query=True, value=True))
            cmds.text(label, edit=True,
                      label=u"链 %d：当前 %d 点 / %d 段 → %d 点 / %d 段"
                            % (index + 1, current, max(0, current - 1),
                               target, max(0, target - 1)))

    def _rm_read_two_clicked(self):
        self._rm_sync_chain_counts()

    def _rm_equalize_counts(self):
        value = components.int_field(self._rm_chain_a_count, query=True, value=True)
        components.int_field(self._rm_chain_b_count, edit=True, value=value)
        self._rm_update_chain_labels()

    def _rm_two_chain_valid_for_apply(self):
        if self._rm_two_chain_key is None:
            lj_warn(u"请先点「读取两链」同步当前选择。")
            return False
        try:
            info = chain_info()
        except MeshError as exc:
            lj_warn(str(exc))
            return False
        if len(info) != 2:
            lj_warn(u"双链点数需要正好选中 2 条开放边链（当前 %d 条）。" % len(info))
            return False
        key = [(item["start_vertex"], item["end_vertex"]) for item in info]
        if key != self._rm_two_chain_key:
            lj_warn(u"选择已变化，请重新点「读取两链」后再应用。")
            return False
        self._rm_two_chain_current = [item["count"] for item in info]
        self._rm_two_chain_info = info
        self._rm_update_chain_labels()
        return True

    def create_smart_combine_separate_layout(self):
        self.open_modeling_fold(u"智能合并分离")

        self.create_combine_separate_buttons()

        self.close_modeling_fold()

    def create_ring_split_layout(self):
        self.open_modeling_fold(u"Ring批量分割")

        self.create_ring_split_buttons()

        self.close_modeling_fold()

    def create_ring_split_buttons(self):
        cmds.columnLayout()
        components.option_menu(RING_UI_MODE, label=u"模式：", width=280)
        cmds.menuItem(label=u"仅处理选中单条边(单四边面对边)")
        cmds.menuItem(label=u"整条Ring分割(等同maya自带工具)")
        components.option_menu(RING_UI_MODE, e=True, select=1)
        cmds.setParent('..')

        cmds.columnLayout()
        components.check_box(RING_UI_EDGE_FLOW, label=u"Insert With EdgeFlow 开启边流", v=0)
        cmds.setParent('..')

        cmds.columnLayout()
        components.button(label=u"执行分割", command=ring_batch_split,
                    width=275, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

    def create_pivots_settings_layout(self):
        self.open_modeling_fold(u"坐标轴")

        self.create_pivot_buttons()

        cmds.text(label=" ", height=4)

        self.create_pivot_boundary_layout()

        self.close_modeling_fold()

    def create_select_by_angle_layout(self):
        components.section(label=u"按角度选面", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True,
                         marginWidth=0, marginHeight=5,
                         expandCommand=self._wheel_attach_all)
        cmds.columnLayout()

        self.create_select_by_angle_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_select_similar_layout(self):
        components.section(label=u"选择相似物体", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=10, marginHeight=5)
        cmds.columnLayout()

        self.create_select_similar_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    """
        ------------------------------------ Buttons ------------------------------------
    """

    def create_select_similar_buttons(self):
        cmds.columnLayout()
        components.button(label=u"选择相似物体", command=lambda *args: self.select_similar_mesh(mode=self.mode),
                    width=275, height=25, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.columnLayout()
        self.mode = 0
        self.separate_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(120, 50, 50),
                                                      label=u"忽略大小：",
                                                      labelArray2=["是", "否"],
                                                      select=1,
                                                      on1=lambda *args: self.set_mode(0),
                                                      on2=lambda *args: self.set_mode(1),
                                                      vertical=False,
                                                      width=300)
        cmds.setParent('..')

    def create_select_by_angle_buttons(self):
        cmds.rowLayout(numberOfColumns=2)
        # 用短名引用：pane 重挂到选项卡后创建时返回的全路径会失效
        angle_slider = "LJGT_AngleByNormalsSlider"
        cmds.intSliderGrp(
            angle_slider,
            label=u"角度", field=True, columnWidth3=(50, 30, 60),
            minValue=0, maxValue=180, fieldMinValue=0,
            value=25, width=250,
            cc=lambda *args: self.update_angle(
                cmds.intSliderGrp(angle_slider, q=True, value=True)))
        self._wheel_register(
            angle_slider + "Field", integer=True, step=1,
            callback=lambda: self.update_angle(
                cmds.intSliderGrp(angle_slider, q=True, value=True)),
            low=0, high=180,
            bump=lambda sign: self._angle_wheel_bump(angle_slider, sign))

        components.button(label=u"选择",
                    bgc=self.inner_grey,
                    command=lambda *args: [self.update_initial_faces(), self.grow_selection_by_normal_angle(
                        cmds.intSliderGrp(angle_slider, q=True, value=True))])
        cmds.setParent('..')

    def _angle_wheel_bump(self, group, sign):
        value = cmds.intSliderGrp(group, query=True, value=True) + sign
        value = max(0, min(180, value))
        cmds.intSliderGrp(group, edit=True, value=value)
        self.update_angle(value)

    def create_selection_buttons(self):
        cmds.columnLayout()

        components.button(label=u"隔段选线", command=self.select_every_N_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        components.button(label=u"选区选择", command=lambda *args: self.FILL_SELECTION.fill_selection(),
                    width=300, height=28, bgc=self.grey,
                    annotation=u"用选中的线划分区域")

        cmds.text(label=" ", height=2)

        components.button(label=u"选硬边", command=self.get_hard_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        components.button(label=u"测试", command=self.test,
                    width=300, height=28, bgc=self.grey)

        cmds.setParent('..')
    
    def create_naming_buttons(self):
        cmds.columnLayout()

        components.button(label=u"自动命名", command=self.show_naming_window,
                    width=300, height=28, bgc=self.grey)

        cmds.setParent('..')

    def set_mode(self, value):
        self.mode = value

    def create_combine_separate_buttons(self):
        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"智能合并", command=lambda *args: self.smart_clean_combine(weld=self.weld_vertices),
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"能自动合并近的片")
        components.button(label=u"智能分离", command=lambda *args: self.smart_clean_separate(keepOriginal=self.keep_original),
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"选面：走 Maya 原生 Extract 分离；选物体：按壳分离")
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2, columnWidth2=(128, 128),
                       columnAttach2=("left", "left"), columnOffset2=(2, 8))
        self.weld_vertices = True
        self.weld_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(50, 38, 38),
                                                  label=u"焊接点",
                                                  labelArray2=['是', "否"],
                                                  select=1,
                                                  on1=lambda *args: self.set_weld_vertices(True),
                                                  on2=lambda *args: self.set_weld_vertices(False),
                                                  vertical=False,
                                                  width=126)
        self.keep_original = False
        self.separate_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(50, 38, 38),
                                                      label=u"复制面",
                                                      labelArray2=['是', "否"],
                                                      select=2,
                                                      on1=lambda *args: self.set_keep_original(True),
                                                      on2=lambda *args: self.set_keep_original(False),
                                                      vertical=False,
                                                      width=126)
        cmds.setParent('..')

    def set_keep_original(self, value):
        self.keep_original = value

    def set_weld_vertices(self, value):
        self.weld_vertices = value

    def create_modeling_buttons(self):
        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2)

        components.button(label=u"转角改线", command=self.corner_killer,
                    width=149, height=28, bgc=self.grey,
                    annotation=u"更改转角布线，上下面如果有需要一起选中")

        components.button(label=u"选中元素加环线", command=lambda *args: self.split_around(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"选中点线面都可以以选中元素为中心加环线")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"智能插入 I", command=lambda *args: self.ROUND_INSET.roundInset(),
                    width=149, height=28, bgc=self.grey)

        components.button(label=u"智能插入 II", command=lambda *args: self.insert_face(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"能单面片插入")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"顶点法线对齐", command=lambda *args: self.match_vertex_normal(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"顶点法线对齐到选中的点法线方向")

        components.button(label=u"吸附平面", command=lambda *args: self.SNAP_2_PLANE.create_plane_ui(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"吸附到自定义三点确定的平面")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"环线随意滑移", command=lambda *args: self.polyEdgeShift(),
            width=149, height=28, bgc=self.grey,
            annotation=u"增加的环线随意旋转滑移，不影响外轮廓")
        components.button(label=u"法线权重置平", command=lambda *args: self.weight_normal(),
            width=149, height=28, bgc=self.grey,
            annotation=u"法线权重（Smart Outside）：按容差角度把平滑法线推给外侧相邻面")
        
        cmds.setParent('..')

        cmds.text(label=" ", height=10)
        cmds.rowLayout(numberOfColumns=2)
        components.button(label=u"拖拽吸附", command=lambda *args: self.INSTANT_DRAG.instantDrag(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"Ctrl = 5°旋转\n"
                               u"Shift = 复制\n"
                               u"Shift + Ctrl = 对齐到最近边方向")

        components.button(label=u"沿最近边对齐", command=lambda *args: self.ALIGN_EDGE.alignEdge(),
                    width=149, height=28, bgc=self.grey)

        cmds.setParent('..')

    def create_pivot_buttons(self):
        cmds.rowLayout(numberOfColumns=3, )

        components.button(label=u"冻结", command=self.freeze_pivots,
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标系归0")

        components.button(label=u"居中", command="cmds.CenterPivot()",
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标移动到物体中心")

        components.button(label=u"重置", command=self.reset_pivots,
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标重置到世界中心")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2, )

        components.button(label=u"置底", command=self.move_pivot_to_bottom_center,
                    width=136, height=20, bgc=self.inner_grey,
                    annotation=u"坐标移动到物体底部中心")

        components.button(label=u"物体至世界中心", command=self.snap_to_world_center_bottom,
                    width=136, height=20, bgc=self.inner_grey,
                    annotation=u"物体底部为底移动")
        
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=1, )

        components.button(label=u"匹配坐标轴", command=self.match_axis_to_first,
                    width=275, height=20, bgc=self.inner_grey,
                    annotation=u"后选物体的坐标轴原点搬到第一个选中物体的 pivot 位置，物体本身不移动")

        cmds.setParent('..')

    def freeze_pivots(self, *args):
        """冻结选中物体的变换，无选择/失败时用视口提示反馈"""
        if not cmds.ls(selection=True):
            lj_warn(u"冻结失败：请先选择物体")
            return
        try:
            cmds.FreezeTransformations()
            lj_info(u"冻结完成")
        except Exception as error:
            lj_error(u"冻结失败：%s" % error)

    def reset_pivots(self, *args):
        """重置选中物体的变换，无选择/失败时用视口提示反馈"""
        if not cmds.ls(selection=True):
            lj_warn(u"重置失败：请先选择物体")
            return
        try:
            cmds.ResetTransformations()
            lj_info(u"重置完成")
        except Exception as error:
            lj_error(u"重置失败：%s" % error)

    def create_pivot_boundary_layout(self):
        """创建嵌入主窗口的物体边界对齐可折叠标签，使用 formLayout 精确定位按钮"""
        # 子 frame：物体边界对齐
        components.section(label=u"物体边界对齐", width=280,
                         bgc=self.inner_grey, collapsable=True, collapse=True,
                         marginWidth=4, marginHeight=4)

        # formLayout 参数（和原弹窗一致的布局逻辑，但尺寸适配主窗口）
        margin = 4
        gap = 4
        arrow_w, arrow_h = 56, 36
        plane_w, plane_h, plane_gap = 46, 26, 4

        form = cmds.formLayout()

        # 创建所有按钮
        btn_up = components.button(label=u"↑", width=arrow_w, height=arrow_h,
                             command=lambda *_: self.pivot_move_in_plane("up"),
                             bgc=self.grey_blue)
        btn_down = components.button(label=u"↓", width=arrow_w, height=arrow_h,
                               command=lambda *_: self.pivot_move_in_plane("down"),
                               bgc=self.grey_blue)
        btn_left = components.button(label=u"←", width=arrow_w, height=arrow_h,
                               command=lambda *_: self.pivot_move_in_plane("left"),
                               bgc=self.grey_blue)
        btn_right = components.button(label=u"→", width=arrow_w, height=arrow_h,
                                command=lambda *_: self.pivot_move_in_plane("right"),
                                bgc=self.grey_blue)

        # 三个平面按钮，保存UI路径供 pivot_set_plane 改色
        btn_xy = components.button(label="XY", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("xy"),
                             bgc=self.PIVOT_PLANE_COLORS['xy'][1])
        btn_zy = components.button(label="ZY", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("zy"),
                             bgc=self.PIVOT_PLANE_COLORS['zy'][1])
        btn_zx = components.button(label="ZX", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("zx"),
                             bgc=self.PIVOT_PLANE_COLORS['zx'][1])

        # 记录平面按钮名
        self._pivot_plane_btns = {
            'xy': _ui_short_name(btn_xy),
            'zy': _ui_short_name(btn_zy),
            'zx': _ui_short_name(btn_zx),
        }

        # 计算布局位置
        row_up = margin
        row_mid = row_up + arrow_h + 6
        mid_h = plane_h * 3 + plane_gap * 2
        row_down = row_mid + mid_h + 6
        # 各按钮按自身宽度相对 50% 居中，保证上下箭头与平面按钮垂直对齐
        arrow_offset = -(arrow_w // 2)
        plane_offset = -(plane_w // 2)
        side_top = row_mid + (mid_h - arrow_h) // 2  # 左右箭头与中间列垂直居中

        cmds.formLayout(form, edit=True,
            attachForm=[
                (btn_up, "top", row_up),
                (btn_left, "top", side_top),
                (btn_left, "left", margin),
                (btn_right, "top", side_top),
                (btn_right, "right", margin),
                (btn_xy, "top", row_mid),
                (btn_zy, "top", row_mid + plane_h + plane_gap),
                (btn_zx, "top", row_mid + (plane_h + plane_gap) * 2),
                (btn_down, "top", row_down),
            ],
            attachPosition=[
                (btn_up, "left", arrow_offset, 50),
                (btn_xy, "left", plane_offset, 50),
                (btn_zy, "left", plane_offset, 50),
                (btn_zx, "left", plane_offset, 50),
                (btn_down, "left", arrow_offset, 50),
            ]
        )

        # 设默认平面为 XY
        self.pivot_set_plane("xy")

        cmds.setParent('..')  # 退回 frameLayout
        cmds.setParent('..')  # 退回建模 columnLayout

    """
         ------------------------------------ Functions ------------------------------------
    """

    # --------------------- combine & separate
    def smart_clean_combine(self, weld=True, *args):

        selection = cmds.ls(selection=True, type='transform')

        if not len(selection) > 1:
            lj_warn(u"选择两个以上的物体!!!")
            return

        current_ctx = cmds.currentCtx()
        objects = cmds.ls(os=True, l=True, tr=True)
        last_obj = cmds.ls(os=True, l=True, tail=1, tr=True)
        group_parent = cmds.listRelatives(last_obj, f=True, p=True)
        source_parent = None
        init_list = []
        outliner_pos_a = 0
        combined_mesh = []
        post_list = []
        outliner_pos_b = 0
        relative_num = 0
        piv_rot = cmds.xform(last_obj[0], q=True, ws=True, ro=True)
        pivot = 1

        cmds.undoInfo(openChunk=True, cn="SmartCleanCombine")
        cmds.setToolTo('Move')

        if group_parent:
            source_parent = group_parent[0]
            init_list = cmds.listRelatives(source_parent, f=True, c=True)
            outliner_pos_a = init_list.index(last_obj[0]) + 1 if last_obj[0] in init_list else -1

            if pivot == 0:
                combined_mesh = cmds.polyUnite(ch=True, cp=True, muv=True)
            elif pivot == 1:
                combined_mesh = cmds.polyUnite(ch=True, op=True, muv=True)
                cmds.manipPivot(o=piv_rot)
                mel.eval('BakeCustomPivot')
            elif pivot == 2:
                combined_mesh = cmds.polyUnite(ch=True, muv=True)

            # if in_place == 1:
            cmds.parent(combined_mesh[0], source_parent)
            cmds.delete(combined_mesh[0], ch=True)
            post_list = cmds.listRelatives(source_parent, f=True, c=True)
            outliner_pos_b = post_list.index(combined_mesh[0]) + 1 if combined_mesh[0] in post_list else -1
            cmds.rename(combined_mesh[0], mel.eval('plugNodeStripped {}'.format(last_obj[0])))

        else:
            init_list = cmds.ls(l=True, assemblies=True)
            outliner_pos_a = init_list.index(last_obj[0]) + 1 if last_obj[0] in init_list else -1

            if pivot == 0:
                combined_mesh = cmds.polyUnite(ch=True, cp=True, muv=True)
            elif pivot == 1:
                combined_mesh = cmds.polyUnite(ch=True, op=True, muv=True)
                cmds.manipPivot(o=piv_rot)
                mel.eval('BakeCustomPivot')
            elif pivot == 2:
                combined_mesh = cmds.polyUnite(ch=True, muv=True)

            # if in_place == 1:
            cmds.delete(combined_mesh[0], ch=True)
            post_list = cmds.ls(l=True, assemblies=True)
            outliner_pos_b = post_list.index(combined_mesh[0]) + 1 if combined_mesh[0] in post_list else -1
            cmds.rename(combined_mesh[0], mel.eval('plugNodeStripped {}'.format(last_obj[0])))

        if current_ctx in ["Move", "moveSuperContext"]:
            cmds.setToolTo('Move')
        elif current_ctx in ["Rotate", "RotateSuperContext"]:
            cmds.setToolTo('Rotate')
        elif current_ctx in ["Scale", "scaleSuperContext"]:
            cmds.setToolTo('Scale')

        cmds.ConvertSelectionToEdges()
        cmds.polySelectConstraint(m=2, t=0x8000, border=1)
        cmds.polySelectConstraint(disable=True)

        if weld:
            cmds.polyMergeVertex(d=0.001)
        cmds.delete(last_obj, constructionHistory=True)
        cmds.select(last_obj, r=True)

        cmds.FreezeTransformations()
        cmds.CenterPivot()
        cmds.undoInfo(closeChunk=True)

    def smart_clean_separate(self, keepOriginal, *args):
        selected_objects = cmds.ls(selection=True)
        faces = cmds.filterExpand(selectionMask=34)

        if not selected_objects or (not faces and not cmds.ls(selected_objects, type='transform')):
            lj_warn(u"至少选择一个物体!!!")
            return

        cmds.undoInfo(openChunk=True, cn="SmartCleanSeparate")

        try:
            if faces:
                # 选面：直接走 Maya 原生 Extract Face（Edit Mesh > Extract Face，
                # 内部为 performPolyChipOff，含“分离抽出面”步骤）
                def face_center(face):
                    pts = cmds.xform(face, q=True, ws=True, t=True)
                    n = len(pts) / 3
                    return (round(sum(pts[0::3]) / n, 4),
                            round(sum(pts[1::3]) / n, 4),
                            round(sum(pts[2::3]) / n, 4))

                centers = set(face_center(f) for f in faces)

                owners = []
                for f in faces:
                    shape = cmds.listRelatives(f, parent=True, fullPath=True)
                    tr = cmds.listRelatives(shape[0], parent=True, fullPath=True) if shape else None
                    if tr:
                        owners.append(tr[0])
                owners = list(set(owners))

                transforms_before = set(cmds.ls(type='transform', long=True))
                cmds.select(faces, r=True)

                try:
                    # 复制面 = 是 → Duplicate Face；否 → Extract Face
                    mel.eval('performPolyChipOff 0 %d' % (1 if keepOriginal else 0))
                except RuntimeError:
                    lj_warn(u"面分离失败：请检查所选面是否有效!!!")
                    return

                new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                  if t not in transforms_before]

                if not new_transforms and owners:
                    # 原生命令的“分离抽出面”未生效时补一次分离
                    cmds.selectMode(object=True)
                    cmds.select(owners, r=True)
                    cmds.polySeparate(ch=False)
                    new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                      if t not in transforms_before]

                if not new_transforms:
                    lj_warn(u"面分离失败：没有生成新物体!!!")
                    return

                # polySeparate 会把抽出的新片挂在原物体变成的组下；
                # 解组让所有片成为独立物体，不残留组（与选物体分支一致）
                cmds.selectMode(object=True)
                if owners:
                    cmds.select(owners, r=True)
                    try:
                        cmds.ungroup()
                    except RuntimeError:
                        pass

                # 解组后长路径变化，按面心重新匹配抽出的新片
                new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                  if t not in transforms_before]
                extracted = []
                for obj in new_transforms:
                    obj_faces = cmds.ls(obj + '.f[*]', flatten=True)
                    if not obj_faces:
                        continue
                    if all(face_center(f) in centers for f in obj_faces):
                        extracted.append(obj)
                if not extracted:
                    extracted = new_transforms

                cmds.select(extracted, r=True)
                cmds.CenterPivot()

                # 清理构造历史（保留下来的原流程）
                history_targets = [o for o in extracted + owners if cmds.objExists(o)]
                if history_targets:
                    cmds.delete(history_targets, ch=True)
                    remaining = [o for o in extracted if cmds.objExists(o)]
                    if remaining:
                        cmds.select(remaining, r=True)
            else:
                try:
                    cmds.polySeparate(ch=0)
                    cmds.pickWalk(d='up')
                    transforms = cmds.ls(selection=True, type='transform')
                    counter = 0

                    for grp in transforms:
                        striped_name = grp.replace("_grp", "")
                        children = cmds.listRelatives(grp, children=True, fullPath=True, type='transform') or []
                        gd_children = []

                        for child in children:
                            if cmds.objectType(child, isType='transform') and not cmds.listRelatives(child,
                                                                                                     shapes=True):
                                continue
                            gd_children.append(child)

                        for child in gd_children:
                            counter += 1
                            padding_string = '%03d' % counter
                            cmds.rename(child, '%s_%s_geo' % (striped_name, padding_string))

                    cmds.ungroup()
                    cmds.CenterPivot()
                except RuntimeError:
                    lj_warn(u"单个物体不能分离!!!")
        finally:
            cmds.undoInfo(closeChunk=True)

    # ------------------------------------------ selection ------------------------------------------

    # --------------------- LingJuGeometryTools.select_every_N_edge
    def select_every_N_edge(self, *args):
        selected_objects = cmds.ls(selection=True)

        if not selected_objects:
            lj_warn(u"先选择两条循环线!!!")
            return

        initial_edge_selection = cmds.ls(selection=True, flatten=True)
        num_edges = len(initial_edge_selection)

        if not num_edges == 2:
            lj_warn(u"先选择两条循环线!!!")
            return

        first_edge = initial_edge_selection[0]
        name_parts = first_edge.split('.e[')
        object_name = name_parts[0]
        first_edge_id = self.get_edge_id_from_selection_string(first_edge)

        if num_edges == 1:
            cmds.polySelect(selected_objects[0], edgeLoop=first_edge_id, noSelection=True)
            return

        second_edge = initial_edge_selection[1]
        second_edge_id = self.get_edge_id_from_selection_string(second_edge)

        edge_loop_ids = cmds.polySelect(selected_objects[0], edgeLoop=first_edge_id, noSelection=True)

        first_edge_index = self.get_edge_id_index(first_edge_id, edge_loop_ids)
        if first_edge_index == -1:
            return

        second_edge_index = self.get_edge_id_index(second_edge_id, edge_loop_ids)
        if second_edge_index == -1:
            edge_loop_ids = cmds.polySelect(selected_objects[0], edgeRing=first_edge_id, noSelection=True)
            first_edge_index = self.get_edge_id_index(first_edge_id, edge_loop_ids)
            if first_edge_index == -1:
                return
            second_edge_index = self.get_edge_id_index(second_edge_id, edge_loop_ids)
            if second_edge_index == -1:
                return

        num_edges_in_loop = len(edge_loop_ids)
        is_full_loop = (edge_loop_ids[0] == edge_loop_ids[-1])

        edges_to_skip = abs(second_edge_index - first_edge_index)

        if is_full_loop and edges_to_skip > (num_edges_in_loop - edges_to_skip - 1):
            edges_to_skip = num_edges_in_loop - edges_to_skip - 1

        selection_list = []

        for i in range(first_edge_index, num_edges_in_loop, edges_to_skip):
            selection_list.append(f"{object_name}.e[{edge_loop_ids[i]}]")
        for i in range(first_edge_index - edges_to_skip, -1, -edges_to_skip):
            selection_list.append(f"{object_name}.e[{edge_loop_ids[i]}]")

        cmds.select(selection_list)

    @staticmethod
    def get_edge_id_from_selection_string(sel):
        tokens = sel.split('[')
        if len(tokens) != 2 or not tokens[1].endswith(']'):
            return -1
        edge_id_str = tokens[1][:-1]
        try:
            return int(edge_id_str)
        except ValueError:
            return -1

    @staticmethod
    def get_edge_id_index(edge_id, edge_ids):

        try:
            return edge_ids.index(edge_id)
        except ValueError:
            return -1

    # --------------------- select_hard_edge
    def get_hard_edge(self, *args):
        edge_sel = cmds.filterExpand(fp=True, sm=32, ex=True)
        transforms_sel = cmds.ls(sl=True, transforms=True)

        if len(transforms_sel) >= 1:
            cmds.dR_DoCmd("modeEdge")
            cmds.polySelectConstraint(m=3, t=0x8000, sm=1)
            cmds.polySelectConstraint(disable=True)
        elif edge_sel and len(edge_sel) >= 1:
            cmds.polySelectConstraint(m=2, t=0x8000, sm=1)
            cmds.polySelectConstraint(disable=True)
        else:
            lj_warn(u"先选中物体或线")

    # --------------------- select similar mesh
    def select_similar_mesh(self, mode, *args):
        selection = cmds.ls(selection=True, long=True)
        if not selection:
            lj_warn(u"选择一个物体先！")
            return

        items = cmds.ls(selection=True, l=True, fl=True)
        all_transforms = cmds.ls(items, long=True, type='transform')
        mesh_items = []
        for transform in all_transforms:
            shapes = cmds.listRelatives(transform, shapes=True, fullPath=True, type='mesh') or []
            if shapes:
                LingJuGeometryTools.short_name = cmds.ls(transform, tail=True)[0]
                mesh_items.append(LingJuGeometryTools.short_name)

        scene_mesh = cmds.ls(long=True, type='transform')
        mesh_transforms = cmds.filterExpand(scene_mesh, sm=12) or []

        similar_meshes = []
        scale_source = []
        area_source = []

        if len(items) != len(mesh_items):
            lj_warn(u"选择一个物体先！")
        else:
            for item in items:
                scale = cmds.getAttr(item + ".scale")[0]
                scale_source.append(scale)
                temp_float = cmds.polyEvaluate(item, worldArea=True)
                area_source.append(temp_float)

                for mesh_transform in mesh_transforms:
                    if cmds.polyCompare(item, mesh_transform, fd=True) == 0:
                        temp_float2 = cmds.polyEvaluate(mesh_transform, worldArea=True)
                        area_target = temp_float2

                        if mode == 0:
                            similar_meshes.append(mesh_transform)
                        elif mode == 1:
                            if self.equivalent_tol(area_target, area_source[-1], 0.5):
                                similar_meshes.append(mesh_transform)

            cmds.select(similar_meshes, add=True)

    @staticmethod
    def equivalent_tol(val1, val2, tolerance):
        return abs(val1 - val2) <= tolerance

    # --------------------- select_by_angle
    @staticmethod
    def get_face_normal(face_name):
        poly_info_result = cmds.polyInfo(face_name, fn=True)[0]
        items = poly_info_result.split()
        x, y, z = float(items[2]), float(items[3]), float(items[4])
        normal = oma.MVector(x, y, z)

        # 获取面所在的物体
        parent_shape = cmds.listRelatives(face_name, parent=True, path=True)
        if not parent_shape:
            lj_error(f"Failed to get parent shape for {face_name}.")
            return normal

        parent_transform = cmds.listRelatives(parent_shape[0], parent=True, path=True)
        if not parent_transform:
            lj_error(f"Failed to get parent transform for {parent_shape[0]}.")
            return normal

        transform_matrix = cmds.xform(parent_transform[0], q=True, m=True, ws=True)

        m_matrix = oma.MMatrix(transform_matrix)
        world_normal = normal * m_matrix
        unit_world_normal = world_normal.normal()

        return unit_world_normal

    def grow_selection_by_normal_angle(self, angle):
        # 与滑块更新共用同一套扩选逻辑
        self.update_angle(angle)

    def update_angle(self, new_angle):
        if not LingJuGeometryTools.initial_faces:
            lj_warn(u"选择一个面！")
            return

        # Clear previous constraints and selection
        cmds.polySelectConstraint(dis=True)
        cmds.select(clear=True)

        all_selected_faces = set()
        for face_name in LingJuGeometryTools.initial_faces:
            poly_normal = self.get_face_normal(face_name)
            cmds.polySelectConstraint(mode=2, type=0x0008, orient=1,
                                      orientaxis=(poly_normal.x, poly_normal.y, poly_normal.z),
                                      orientbound=(0, new_angle))  # Set angle bounds

            # Select faces based on the new constraint
            cmds.select(face_name, r=True)
            for _ in range(50):
                cmds.polySelectConstraint(pp=1)

            # Collect selected faces
            selected_faces = cmds.ls(sl=True, fl=True, l=True)
            if selected_faces:
                all_selected_faces.update(selected_faces)

        # Update selection to reflect the new angle
        cmds.select(list(all_selected_faces))  # Select all faces that match the constraint
        cmds.polySelectConstraint(dis=True)  # Clear constraints

    def update_initial_faces(self):
        LingJuGeometryTools.initial_faces = cmds.ls(sl=True, fl=True)
        if not LingJuGeometryTools.initial_faces:
            lj_warn(u"请重新选择面！")

    # --------------------- LingJuGeometryTools.polyEdgeShift

    def polyEdgeShift(self, *args):
        # 初始化全局状态，避免首次点击 Flatten 复选框时变量未定义
        global polyEdgeShiftFlatten
        polyEdgeShiftFlatten = ''
        if cmds.window("polyEdgeShiftUI", exists = True):
            cmds.deleteUI("polyEdgeShiftUI")
        polyEdgeShiftUI = cmds.window('polyEdgeShiftUI' , t = "poly Edge Shift v1.01", w = 280, h = 50, s = 1 ,mxb = False,mnb = False)
        cmds.columnLayout(adj=1)
        cmds.text(l='')
        cmds.rowColumnLayout(nc= 6 ,cw=[(1,20),(2,75),(3,10),(4,75),(5,5),(6,75)])
        cmds.text(l='')
        # 类方法不能用字符串回调（Maya 在全局命名空间执行字符串），改用 lambda 闭包
        components.check_box('polyEdgeShiftFlattenCheck',label="Flatten", value= 1,cc=lambda *a: self.polyEdgeToggleFlatten())
        cmds.text(l='')
        components.button('PESGo', l= "Go", c = lambda *a: self.polyEdgeShiftRun(),bgc = (0.24, 0.4, 0.25 ))
        cmds.text(l='')
        components.button('PESUndo', en=0, l= "Undo", bgc = (0.24, 0.24, 0.24), c = lambda *a: self.PEStempLoadPointData() )
        cmds.showWindow(polyEdgeShiftUI)


    def polyEdgeToggleFlatten(self):
        global polyEdgeShiftFlatten
        if cmds.objExists(polyEdgeShiftFlatten):
            checkCurrent = components.check_box('polyEdgeShiftFlattenCheck',q=1,value= 1) 
            if polyEdgeShiftFlatten:
                if checkCurrent == 1.0:
                    cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 0)
                else:
                    cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 1)

    def polyEdgeShiftRun(self):
        global cutFaceSpineCv
        global cutFaceSpineEdge
        global cutFaceSpineEdgeID
        global dataDict
        global keepStartSelection
        global polyEdgeShiftFlatten
        global polyEdgeShiftData
        self.PEStempStorePointData()
        dataDict =[]
        cutFaceSpineCv = []
        cutFaceSpineEdge = []
        cutFaceSpineEdgeID = []
        innerCvList = []
        unwwantInnerEdge = []
        keepStartSelection = []
        polyEdgeShiftFlatten = ''
        components.button('PESGo',e=1, en=0,bgc=(0.24, 0.24, 0.24))
        components.button('PESUndo',e=1, en=0,bgc=(0.24, 0.24, 0.24))
        flattenIt = components.check_box('polyEdgeShiftFlattenCheck',q=1, value= 1) 
        cleanList = ('cutPlane','snapPlane','showCutCurv*','shapeKeeper','shapeFlatten')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        
        cutFaceSpineFace =  cmds.filterExpand( sm=34)
        cutFaceSpineEdge =  cmds.filterExpand( sm=32)
        selMesh = ''
        if cutFaceSpineEdge:
            keepStartSelection = cutFaceSpineEdge
            cutFaceSpineCv = cmds.polyListComponentConversion(cutFaceSpineEdge,fe=1, tv=1,)
            cutFaceSpineCv = cmds.ls(cutFaceSpineCv,fl=1)
            unwwantInnerEdge = []
            self.createCutPinkLine(cutFaceSpineEdge)
            selMesh = cutFaceSpineEdge[0].split('.')[0]
        if cutFaceSpineFace:
            #create blendshape
            keepStartSelection = cutFaceSpineFace
            selMesh = cutFaceSpineFace[0].split('.')[0]
            toSpinFace = []
            cutFaceShapeMaster = selMesh
            dulpicateMesh = cmds.duplicate(selMesh,smartTransform=1,rr=1)
            cmds.rename(dulpicateMesh,'shapeKeeper')
            cmds.select(cutFaceSpineFace)
            #cmds.scale(0,1,1, cs=1, ls=1, r=1)
            dulpicateMesh = cmds.duplicate(selMesh,smartTransform=1,rr=1)
            cmds.rename(dulpicateMesh,'shapeFlatten')
            for c in cutFaceSpineFace:
                newName = c.replace(selMesh,'shapeFlatten')
                toSpinFace.append(newName)
            innerCvList = cmds.polyListComponentConversion(toSpinFace, ff=1, fv=1, fe=1, fuv=1, fvf=1, tv=1, internal=1)
            innerCvList = cmds.ls(innerCvList,fl=1)
            fullCvList = cmds.polyListComponentConversion(toSpinFace, ff=1, fv=1, fe=1, fuv=1, fvf=1, tv=1)
            fullCvList = cmds.ls(fullCvList,fl=1)
            cutFaceSpineCv = list(set(fullCvList)-set(innerCvList))
            cutFaceSpineEdge = cmds.polyListComponentConversion(toSpinFace, ff=1, te=1)
            cutFaceSpineEdge = cmds.ls(cutFaceSpineEdge,fl=1)
            unwwantInnerEdge = cmds.polyListComponentConversion(innerCvList, fv=1, te=1)  
            unwwantInnerEdge = cmds.ls(unwwantInnerEdge,fl=1)
            innerEdge = cmds.polyListComponentConversion(toSpinFace, ff=1, te=1, internal=1)
            borderEdges = list(set(cutFaceSpineEdge)-set(innerEdge))
            cmds.select(cutFaceSpineFace)
            cmds.ConvertSelectionToEdgePerimeter()
            borderEdge =  cmds.filterExpand( sm=32)
            self.createCutPinkLine(borderEdge)
            
        dataDict = []
        verPosiDict = []
        for b in cutFaceSpineCv:
            aimPoint = cmds.pointPosition(b,w=1)
            edgeList = cmds.polyListComponentConversion(b, fv=1, te=1)
            edgeX = cmds.ls(edgeList,fl=1)
            getEdge = list(set(edgeX) - set(cutFaceSpineEdge))
            cvList = cmds.polyListComponentConversion(getEdge, fe=1, tv=1)
            cvX =  cmds.ls(cvList,fl=1)
            getCV = list(set(cvX) - set(cutFaceSpineCv)-set(unwwantInnerEdge))
            basePoint = cmds.pointPosition(getCV[0],w=1)
            vectBtwPnts= ((basePoint [0] -aimPoint [0])*-1), ((basePoint [1] -aimPoint [1])*-1), ((basePoint [2] -aimPoint [2])*-1)
            vectorToFinish = om.MFloatVector(vectBtwPnts[0],vectBtwPnts[1],vectBtwPnts[2])
            raySource = om.MFloatPoint(basePoint[0],basePoint[1],basePoint[2])
            rayDirection = vectorToFinish
            rayDirection = rayDirection.normal()
            dataCollect = [b,raySource,rayDirection]
            dataDict.append(dataCollect)
            verPosiDict.append(aimPoint)
        avgPosition = self.getAvgVertPosition(cutFaceSpineCv)
        avgNormal = []
        mesh = cmds.polyCreateFacet(p=verPosiDict)
        avgNormal   = self.getAvgFaceNormal(mesh[0]+'.f[0]')
        if innerCvList:
            mfloatVectorSum = om.MFloatVector()
            for d in dataDict:
                mfloatVectorSum += dataDict[0][2]
            mfloatVectorAverage = mfloatVectorSum / len(dataDict)
            for i in innerCvList:
                basePoint = cmds.pointPosition(i,w=1)
                raySource = om.MFloatPoint(basePoint[0],basePoint[1],basePoint[2])
                dataCollect = [i,raySource,mfloatVectorAverage]
                dataDict.append(dataCollect)
        cmds.delete(mesh)
        snapPlane = cmds.polyPlane(sx=1,sy=1,n='cutPlane')[0]
        snapPlaneParent = cmds.group(name="snapPlane")
        attr = '{}.translate'.format(snapPlaneParent)
        for i, axis in enumerate(['X', 'Y', 'Z']):
            cmds.setAttr('{}{}'.format(attr,axis), avgPosition[i])
        attr = '{}.scale'.format(snapPlane)
        for axis in ['X', 'Y', 'Z']:
            cmds.setAttr('{}{}'.format(attr,axis), 10000)
        orientLocator = cmds.spaceLocator()
        attr = '{}.translate'.format(orientLocator[0])
        for i, axis in enumerate(['X', 'Y', 'Z']):
            cmds.setAttr('{}{}'.format(attr,axis), avgPosition[i])
        oc = cmds.orientConstraint(selMesh, orientLocator, offset=(0,0,0))
        cmds.delete(oc)
        cmds.makeIdentity(apply=1, t=1, r=1, s=1, n=0, pn=1)
        cmds.move(avgNormal[0], avgNormal[1], avgNormal[2], orientLocator, r=1, os=1)
        ac = cmds.aimConstraint(orientLocator, snapPlaneParent, aimVector=[0,1,0])
        offsetPVGrp = cmds.group(em=True, name="offsetPVGrp")
        cmds.matchTransform( orientLocator[0], 'snapPlane', rot=True)
        # Match the group's position to the locator's position
        cmds.matchTransform(offsetPVGrp, orientLocator[0], pos=True,rot=True)
        cmds.parent(orientLocator,offsetPVGrp)
        cmds.setAttr(orientLocator[0]+'.translateY',10000)
        cmds.delete(ac, offsetPVGrp)
        self.updateSpinFace()
        if cmds.objExists('shapeFlatten'):#edge mode
            cmds.transferAttributes('shapeFlatten',selMesh,transferPositions=True,transferNormals=False,transferUVs=False,transferColors=False,sampleSpace=4,sourceUvSpace="map1",targetUvSpace="map1",searchMethod=3,flipUVs=False,colorBorders=True)
            cmds.delete(selMesh ,ch=1)
            polyEdgeShiftFlatten = cmds.blendShape('shapeKeeper', 'shapeFlatten', selMesh)[0]
            cmds.setAttr(polyEdgeShiftFlatten + ".shapeFlatten", 1)
            cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 1)
        sellist = om.MSelectionList()
        sellist.add('cutPlane')
        node = om.MObject()
        sellist.getDependNode(0, node)
        cutFaceSpineEdgeID = om.MNodeMessage.addAttributeChangedCallback(node, self.spinCutFace)
        cmds.select('cutPlane')
        hideList = ('cutPlane','shapeFlatten','shapeKeeper','snapPlane')
        for c in hideList:
            if cmds.objExists(c):
                cmds.setAttr(c + '.visibility',0)
                cmds.setAttr(c + '.hiddenInOutliner',1)
        cmds.scriptJob (ro=1, event = ["SelectionChanged", self.killCallBackRun])
        cmds.setToolTo("Rotate")
        cmds.undoInfo(ock=1)
        self.polyEdgeToggleFlatten()
        
    def weight_normal(self, *args):
        """Weight Normal（Smart Outside）：整段 MEL 内嵌在文件末尾，eval 即弹窗。"""
        mel.eval(WEIGHT_NORMAL_MEL)
        
    def createCutPinkLine(self,cutFaceSpineEdge):
        sortEdgeLoopGrp =  self.getEdgeRingGroup(0,cutFaceSpineEdge)
        i=1
        for s in sortEdgeLoopGrp:
            cmds.select(s)
            cmds.polyToCurve(form=2, degree=1, conformToSmoothMeshPreview=1)
            #cmds.CreateCurveFromPoly()
            cmds.rename('showCutCurve_'+str(i))
            curveName ='showCutCurve_'+str(i)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.lineWidth', 5)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideEnabled', 1)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideRGBColors', 1)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideColorRGB', 1, 0 , 0.8)
            cmds.setAttr(curveName + '.hiddenInOutliner',1)
            i= i+1

    def killCallBackRun(self):
        global keepStartSelection
        global cutFaceSpineEdgeID
        om.MMessage.removeCallback(cutFaceSpineEdgeID)
        cleanList = ('cutPlane','snapPlane','showCutCurv*','shapeKeeper','shapeFlatten')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        selMesh =keepStartSelection[0].split('.')[0]
        cmds.delete(selMesh ,ch=1)
        cmds.select(keepStartSelection)
        cmds.undoInfo(cck=1)
        cutFaceSpineFace =  cmds.filterExpand( sm=34)
        cutFaceSpineEdge =  cmds.filterExpand( sm=32)
        if cutFaceSpineFace:
            mel.eval('doMenuComponentSelectionExt("' + selMesh + '", "facet", 0);')
        elif cutFaceSpineEdge:
            mel.eval('doMenuComponentSelectionExt("' + selMesh + '", "edge", 0);')
        
        components.button('PESUndo',e=1, en=1,bgc = (0.4, 0.24, 0.25) )
        components.button('PESGo',e=1, en=1,bgc=(0.24, 0.4, 0.24))
        
    def spinCutFace(self, msg, m_plug, otherMplug, clientData):
        #print(m_plug.partialName())
        if 'r'  in m_plug.partialName() or 't' in m_plug.partialName():
            self.updateSpinFace()


    def getAvgVertPosition(self,vList):
        totalPos = oma.MPoint(0., 0., 0.)
        for vert in vList:
            pos = oma.MPoint(cmds.pointPosition(vert, world=True))
            totalPos += pos
        count = len(vList)
        return list(totalPos / float(count))[0:3]


    def getAvgFaceNormal(self,selFace):
        face_normals = cmds.polyInfo(selFace, fn=True)
        normals = [[], [], []]
        for normal in face_normals:
            label, vertex, x, y, z = normal.split()
            normals[0].append(float(x))
            normals[1].append(float(y))
            normals[2].append(float(z))    
        normals_count = len(face_normals)
        x_avg = sum(normals[0]) / normals_count
        y_avg = sum(normals[1]) / normals_count
        z_avg = sum(normals[2]) / normals_count
        return x_avg, y_avg,z_avg


    def updateSpinFace(self):
        global dataDict
        selectionList = om.MSelectionList()
        selectionList.add('cutPlane')
        node = om.MDagPath()
        selectionList.getDagPath( 0, node )
        meshFn = om.MFnMesh()
        meshFn.setObject( node )
        for d in dataDict:     
            aimPoint = cmds.pointPosition(d[0],w=1)  
            hitpoint = om.MFloatPoint()
            finalX = []
            finalY = []
            finalZ = []
            shortDistance = 10000000000
            distanceBetween = 1000000000
            intersection = meshFn.closestIntersection(
                om.MFloatPoint(d[1]),
                om.MFloatVector(d[2]),
                None,
                None,
                False,
                om.MSpace.kWorld,
                99999,
                False,
                None,
                hitpoint,
                None,
                None,
                None,
                None,
                None)
            if intersection:
                x = hitpoint.x
                y = hitpoint.y
                z = hitpoint.z
                distanceBetween = math.sqrt( ((float(aimPoint[0]) - x)**2)  + ((float(aimPoint[1]) - y)**2) + ((float(aimPoint[2]) - z)**2))
                if distanceBetween < shortDistance:
                    shortDistance = distanceBetween
                cmds.move(x, y, z, d[0], a=1, ws=1)
                
            else:
                shortDistance = 10000000000
                distanceBetween = 1000000000
                mfloatVectorReverse = -d[2]
                intersection = meshFn.closestIntersection(
                    om.MFloatPoint(d[1]),
                    om.MFloatVector(mfloatVectorReverse),
                    None,
                    None,
                    False,
                    om.MSpace.kWorld,
                    99999,
                    False,
                    None,
                    hitpoint,
                    None,
                    None,
                    None,
                    None,
                    None)
                if intersection:
                    x = hitpoint.x
                    y = hitpoint.y
                    z = hitpoint.z
                    distanceBetween = math.sqrt( ((float(aimPoint[0]) - x)**2)  + ((float(aimPoint[1]) - y)**2) + ((float(aimPoint[2]) - z)**2))
                    if distanceBetween < shortDistance:
                        shortDistance = distanceBetween
                    cmds.move(x, y, z, d[0], a=1, ws=1)

    def getEdgeRingGroup(self,listSort,listInput):
        selEdges = cmds.ls(sl=1,fl=1)
        trans = selEdges[0].split(".")[0]
        e2vInfos = cmds.polyInfo(selEdges, ev=True)
        e2vDict = {}
        fEdges = []
        for info in e2vInfos:
            evList = [ int(i) for i in re.findall('\\d+', info) ]
            e2vDict.update(dict([(evList[0], evList[1:])]))
        while True:
            try:
                startEdge, startVtxs = e2vDict.popitem()
            except:
                break
            edgesGrp = [startEdge]
            num = 0
            for vtx in startVtxs:
                curVtx = vtx
                while True:

                    nextEdges = []
                    for k in e2vDict:
                        if curVtx in e2vDict[k]:
                            nextEdges.append(k)
                    if nextEdges:
                        if len(nextEdges) == 1:
                            if num == 0:
                                edgesGrp.append(nextEdges[0])
                            else:
                                edgesGrp.insert(0, nextEdges[0])
                            nextVtxs = e2vDict[nextEdges[0]]
                            curVtx = [ vtx for vtx in nextVtxs if vtx != curVtx ][0]
                            e2vDict.pop(nextEdges[0])
                        else:
                            break
                    else:
                        break
                num += 1
            fEdges.append(edgesGrp)
        retEdges =[]
        for f in fEdges:
            f= list(map(lambda x: (trans +".e["+ str(x) +"]"), f))
            retEdges.append(f)
        if listSort == 1:
            sortEdgeLoopOrder=[]
            getCircleState,listVtx = vtxLoopOrderCheck(listInput)
            for l in listVtx:
                for r in retEdges:
                    checkCvList = cmds.ls(cmds.polyListComponentConversion( r,fe=True, tv=True), fl=True,l=True)
                    if l in checkCvList:
                        sortEdgeLoopOrder.append(r)
            return sortEdgeLoopOrder
        else:
            return retEdges


    def PEStempStorePointData(self):
        global polyEdgeShiftData
        polyEdgeShiftData=[]
        selPoint = []
        selFace = cmds.filterExpand(expand=True ,sm=34)
        if selFace:
            selPoint = cmds.polyListComponentConversion(selFace, ff=1, tv=1)
        
        selEdge = cmds.filterExpand(expand=True ,sm=32)
        if selEdge:
            selPoint = cmds.polyListComponentConversion(selEdge, fe=1, tv=1)
        selPoint = cmds.ls(selPoint,fl=1)
        if selPoint:
            storePointData = ''
            meshName = selPoint[0].split('.')[0]
            shapeNode = cmds.listRelatives(meshName, fullPath=True , c=True )
            if not cmds.attributeQuery('pointPosData', node = shapeNode[0], ex=True ):
                cmds.addAttr(shapeNode[0], ln='pointPosData',  dt= 'string')
                cmds.setAttr((shapeNode[0]+'.pointPosData'),e=True, keyable=True)
            for s in selPoint:
                xA, yA, zA = cmds.pointPosition(s, w=1)
                xA = round(xA,4)
                yA = round(yA,4)
                zA = round(zA,4)
                storePointData = storePointData + (str(s) + ',' + str(xA) + ',' + str(yA) + ',' + str(zA) + '@')
            polyEdgeShiftData = storePointData


    def PEStempLoadPointData(self):
        global polyEdgeShiftData
        meshName = polyEdgeShiftData.split('.')[0]
        if cmds.objExists(meshName):
            dataList = polyEdgeShiftData.split('@')
            del dataList[-1]
            for r in dataList:
                rawData = r.split(',')
                cmds.move(float(rawData[1]), float(rawData[2]), float(rawData[3]),rawData[0] , a =True, ws=True)
        components.button('PESUndo',e=1, en=0,bgc = (0.24, 0.24, 0.24) )
                        

    # ------------------------------------------ modeling ------------------------------------------

    # --------------------- LingJuGeometryTools.corner_killer
    def corner_killer(self, *args):
        selection = cmds.ls(sl=True, fl=True)
        if not selection:
            lj_warn(u"请选择转角面！")
            return

        if cmds.objExists('sortFaceKeep'):
            cmds.delete('sortFaceKeep')

        cmds.sets(name="sortFaceKeep", text="sortFaceKeep")
        selFace = cmds.ls(sl=1, fl=1)
        threeLessEdgeFaceList = []
        doneFaceList = []

        meshName = selFace[0].split('.')[0]
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            num_edges = 0
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges < 4:
                    threeLessEdgeFaceList.append(s)

        if threeLessEdgeFaceList:
            cmds.select(threeLessEdgeFaceList)
            cmds.sets(threeLessEdgeFaceList, rm="sortFaceKeep")
            self.corner_topoL()

        try:
            cmds.select('sortFaceKeep')
            CMD = 'doMenuComponentSelectionExt("' + meshName + '", "facet", 0);'
            mel.eval(CMD)
        except:
            cmds.select(meshName)

        selFace = cmds.ls(sl=1, fl=1)
        fiveEdgeFaceList = []

        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges == 5:
                    fiveEdgeFaceList.append(s)

        if fiveEdgeFaceList:
            cmds.select(fiveEdgeFaceList)
            cmds.sets(fiveEdgeFaceList, rm="sortFaceKeep")
            self.corner_topoB()
        try:
            cmds.select('sortFaceKeep')
            CMD = 'doMenuComponentSelectionExt("' + meshName + '", "facet", 0);'
            mel.eval(CMD)
        except:
            cmds.select(meshName)

    def corner_topoB(self):
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')
        selFace = cmds.ls(sl=1, fl=1)
        recordRemoveList = []
        recordSecondFacesList = []
        recordConnetCVList = []
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            vertices_with_5_edges = []
            adjVertex = []
            removeList = []
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges == 5:
                    vertices_with_5_edges.append(vertex)
            cmds.select(vertices_with_5_edges)
            cmds.ConvertSelectionToEdges()
            cmds.ConvertSelectionToVertices()
            checkVertex = cmds.ls(sl=1, fl=1)
            adjVertex = list(set(AllVertex) - set(checkVertex))
            connectVertex = list(set(AllVertex) - set(vertices_with_5_edges) - set(adjVertex))
            recordConnetCVList.append(adjVertex[0])
            recordConnetCVList.append(vertices_with_5_edges[0])
            removeList = removeList + connectVertex + vertices_with_5_edges
            checkA = cmds.polyListComponentConversion(adjVertex, te=True)
            checkB = cmds.polyListComponentConversion(checkA, tv=True)
            checkB = cmds.ls(checkB, fl=1)
            checkC = list(set(checkB) - set(AllVertex))
            checkDA = cmds.polyListComponentConversion(checkC[0], te=True)
            checkDAB = cmds.polyListComponentConversion(checkDA, tv=True)
            checkDAB = cmds.ls(checkDAB, fl=1)
            checkEA = cmds.polyListComponentConversion(checkC[1], te=True)
            checkEAB = cmds.polyListComponentConversion(checkEA, tv=True)
            checkEAB = cmds.ls(checkEAB, fl=1)
            adjFar = list(set(checkDAB) & set(checkEAB))
            cmds.select(adjFar, checkC)
            cmds.ConvertSelectionToContainedFaces()
            getF = cmds.ls(sl=1, fl=1)[0]
            recordSecondFacesList.append(getF)
            cmds.select(removeList)
            cmds.select(adjFar, d=1)
            cmds.ConvertSelectionToContainedEdges()
            cmds.SelectEdgeLoopSp()
            removeEdgeList = cmds.ls(sl=1, fl=1)
            recordRemoveList = recordRemoveList + removeEdgeList
            cmds.select(cl=1)
        cmds.select(recordSecondFacesList)
        cmds.sets(name="secFaceKeep", text="secFaceKeep")
        cmds.select(recordConnetCVList)
        cmds.polyConnectComponents()
        cmds.select(recordRemoveList)
        cmds.polyDelEdge(cv=1)
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')

    def corner_topoL(self):
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')
        selFace = cmds.ls(sl=1, fl=1)
        cmds.sets(name="secFaceKeep", text="secFaceKeep")
        recordConnetCVList = []
        recordAdjCVList = []
        recordOutCVList = []
        meshName = selFace[0].split('.')[0]
        cmds.select(meshName)
        cmds.polySelectConstraint(mode=3, type=0x8000, where=1)
        checkHole = cmds.ls(sl=1, fl=1)
        cmds.polySelectConstraint(disable=True)
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            verticesLessEdges = ''
            maxEdgeNo = 100000
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges < maxEdgeNo:
                    maxEdgeNo = num_edges
                    verticesLessEdges = vertex
            cmds.select(verticesLessEdges)
            cmds.ConvertSelectionToEdges()
            cmds.ConvertSelectionToVertices()
            checkVertex = cmds.ls(sl=1, fl=1)
            adjVertex = list(set(AllVertex) - set(checkVertex))
            connectVertex = list(set(AllVertex) - set(adjVertex))
            connectVertex.remove(verticesLessEdges)
            recordAdjCVList.append(adjVertex[0])
            recordConnetCVList.append(connectVertex[0])
            recordConnetCVList.append(connectVertex[1])
            recordOutCVList.append(verticesLessEdges)
            cmds.select(cl=1)
        cmds.select(recordAdjCVList, recordOutCVList)
        cmds.polyConnectComponents()
        cmds.select('secFaceKeep')
        oldFaceNewEdge = cmds.polyListComponentConversion(te=True)
        oldFaceNewEdge = cmds.ls(oldFaceNewEdge, fl=1)
        cmds.select(recordAdjCVList, recordConnetCVList)
        cmds.ConvertSelectionToContainedEdges()
        if len(selFace) == 1:
            if len(checkHole) > 0:
                cmds.SelectEdgeLoopSp()
        if len(selFace) > 1:
            cmds.SelectEdgeLoopSp()
        cmds.polyDelEdge(cv=1)
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')

    # --------------------- split around
    def split_around(self):
        cmds.ConvertSelectionToVertices()
        originalVerts = cmds.ls(selection=True, fl=True)
        cmds.ConvertSelectionToContainedEdges()
        originalEdges = cmds.ls(selection=True, fl=True)

        cmds.ConvertSelectionToVertices()
        cmds.ConvertSelectionToEdges()
        expandedEdges = cmds.ls(selection=True, fl=True)

        if originalEdges:
            cmds.select(deselect=True, *originalEdges)

        containedFaces = cmds.polyListComponentConversion(tf=True, ff=True)
        containedFaces = cmds.ls(selection=True, fl=True)

        if containedFaces:
            cmds.select(containedFaces, r=True)
            containedFacesVerts = cmds.ls(selection=True, fl=True)

            intersectingVerts = list(set(originalVerts) & set(containedFacesVerts))

            cmds.select(originalVerts, r=True)

            cmds.select(deselect=True, *containedFacesVerts)

            if intersectingVerts:
                cmds.select(toggle=True, *intersectingVerts[0])

            cmds.ConvertSelectionToVertices()
            cmds.ConvertSelectionToEdges()
            expandedEdges = cmds.ls(selection=True, fl=True)

        else:
            cmds.select(expandedEdges, r=True)

        if originalEdges:
            cmds.select(deselect=True, *originalEdges)

        cmds.ls(selection=True, fl=True)

        try:
            cmds.polySplitRing(wt=0.5)
        except TypeError:
            lj_warn(u"选择点线面元素")

    # --------------------- smart_insert_face II
    def insert_face(self):
        if len(cmds.ls(selection=True, transforms=True)) == 1:
            cmds.ConvertSelectionToFaces()

        facelist = cmds.ls(selection=True, flatten=True)
        cmds.select(facelist, replace=True)

        faceTotal = cmds.polyEvaluate(face=True)
        faceComp = cmds.polyEvaluate(faceComponent=True)
        shells = cmds.polyEvaluate(shell=True)

        if faceTotal == faceComp:
            extrude = cmds.polyExtrudeFacet(offset=0.05)
            cmds.GrowPolygonSelectionRegion()
            cmds.InvertSelection()
            cmds.delete()
            cmds.select(facelist, replace=True)
            intFaceList = cmds.polyListComponentConversion(ff=True, tf=True, internal=True)
            cmds.select(intFaceList, add=True)
            cmds.select(extrude[0], add=True)
            cmds.ShowManipulatorTool()
        else:
            extrude = cmds.polyExtrudeFacet(offset=0.05)
            cmds.select(facelist, replace=True)
            intFaceList = cmds.polyListComponentConversion(ff=True, tf=True, internal=True)
            cmds.select(extrude[0], add=True)
            cmds.ShowManipulatorTool()

    # --------------------- LingJuGeometryTools.match_vertex_normal
    def get_face_area(self, mesh_fn, face_id):
        """ 计算多边形面的面积（替代不存在的 getFaceArea 方法） """
        vertices = mesh_fn.getPolygonVertices(face_id)
        points = mesh_fn.getPoints(oma.MSpace.kWorld)

        vec_points = [oma.MVector(points[v]) for v in vertices]

        total_area = 0.0
        for i in range(1, len(vec_points) - 1):
            v0 = vec_points[0]
            v1 = vec_points[i]
            v2 = vec_points[i + 1]

            cross = (v1 - v0) ^ (v2 - v0)
            total_area += cross.length() / 2.0

        return total_area

    def transfer_vertex_normals(self, source_vtx, target_vtx):
        source_mesh = source_vtx.split('.')[0]
        source_id = int(source_vtx.split('[')[1].split(']')[0])

        sel_list = oma.MSelectionList()
        sel_list.add(source_mesh)
        source_dag = sel_list.getDagPath(0)
        source_fn = oma.MFnMesh(source_dag)

        connected_faces = cmds.polyListComponentConversion(source_vtx, fv=True, tf=True)
        connected_faces = cmds.ls(connected_faces, fl=True)

        weighted_normals = []
        for face in connected_faces:
            face_id = int(face.split('[')[1].split(']')[0])

            face_area = self.get_face_area(source_fn, face_id)
            normal = source_fn.getFaceVertexNormal(face_id, source_id, oma.MSpace.kWorld)
            weighted_normals.append(normal * face_area)

        avg_normal = sum(weighted_normals, oma.MVector(0, 0, 0))
        avg_normal.normalize()

        target_mesh = target_vtx.split('.')[0]
        target_id = int(target_vtx.split('[')[1].split(']')[0])

        sel_list.clear()
        sel_list.add(target_mesh)
        target_dag = sel_list.getDagPath(0)
        target_fn = oma.MFnMesh(target_dag)

        target_faces = cmds.polyListComponentConversion(target_vtx, fv=True, tf=True)
        target_faces = cmds.ls(target_faces, fl=True)

        for face in target_faces:
            face_id = int(face.split('[')[1].split(']')[0])
            face_normal = target_fn.getPolygonNormal(face_id, oma.MSpace.kWorld)

            if avg_normal * face_normal < 0:
                final_normal = -avg_normal
            else:
                final_normal = avg_normal

            target_fn.setFaceVertexNormal(final_normal, face_id, target_id, oma.MSpace.kWorld)

        target_fn.updateSurface()

    def find_matching_vertices(self, selection, threshold=0.0001):
        """ 根据选中的顶点生成源到目标的顶点对，目标为第一个选中的模型顶点 """
        if not selection:
            return []

        target_mesh = selection[0].split('.')[0]
        target_vertices = [v for v in selection if v.split('.')[0] == target_mesh]
        source_vertices = [v for v in selection if v.split('.')[0] != target_mesh]

        if not target_vertices or not source_vertices:
            return []

        pairs = []

        for tgt in target_vertices:
            tgt_pos = cmds.pointPosition(tgt)
            closest_src = None
            min_dist = threshold

            for src in source_vertices:
                src_pos = cmds.pointPosition(src)
                dist = sum((tgt_pos[i] - src_pos[i]) ** 2 for i in range(3)) ** 0.5
                if dist < min_dist:
                    min_dist = dist
                    closest_src = src

            if closest_src:
                pairs.append((closest_src, tgt))

        return pairs

    def match_vertex_normal(self):
        """ 主执行函数：将法线从源顶点传递到第一个选中的模型顶点 """
        selection = cmds.ls(sl=True, fl=True)

        if len(selection) < 2:
            lj_warn("请选择至少两个不同物体的顶点")
            return

        vertex_pairs = self.find_matching_vertices(selection)

        if not vertex_pairs:
            lj_warn("未找到重合顶点（间距需<0.0001单位）或目标模型无对应源顶点")
            return

        for src, tgt in vertex_pairs:
            self.transfer_vertex_normals(src, tgt)

        cmds.refresh(force=True)

    # --------------------- pivot

    def move_pivot_to_bottom_center(self, *args):
        selection = cmds.ls(sl=True, l=True)

        for sel in selection:
            bBox = cmds.xform(sel, q=True, ws=True, bb=True)
            centerX = (bBox[0] + bBox[3]) / 2
            bottomY = bBox[1]
            centerZ = (bBox[2] + bBox[5]) / 2

            cmds.xform(sel, ws=True, piv=(centerX, bottomY, centerZ))

    def is_dag_visible(self, node):
        """沿父级链检查节点及其所有祖先的 visibility 属性

        :param node: DAG 节点名（transform 或 shape 均可）
        :return: bool，任意一层的 visibility 为 0 时返回 False
        """
        current = node
        while current:
            try:
                if not cmds.getAttr("%s.visibility" % current):
                    return False
            except Exception:
                # 部分节点没有 visibility 属性时按可见处理
                pass
            parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
            current = parents[0] if parents else None
        return True

    def get_visible_world_bbox(self, node):
        """返回节点"可见几何"的世界包围盒，用作居中计算的基准

        exactWorldBoundingBox 默认会把隐藏子物体（cluster handle、locator、残留
        节点等）算进来，选中"组"时也按整棵子树取并集，都会把居中基准拉偏；这里
        只统计节点自身及子孙里可见的网格。

        :param node: transform 节点（长名）
        :return: [xmin, ymin, zmin, xmax, ymax, zmax]；没有网格时退回节点自身包围盒
        """
        shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, fullPath=True) or []
        shapes = [s for s in shapes if self.is_dag_visible(s)]
        if not shapes:
            meshes = cmds.listRelatives(node, allDescendents=True, type="mesh",
                                       noIntermediate=True, fullPath=True) or []
            shapes = [m for m in meshes if self.is_dag_visible(m)]
        if shapes:
            return cmds.exactWorldBoundingBox(shapes, ignoreInvisible=True)
        return cmds.exactWorldBoundingBox(node, ignoreInvisible=True)

    def can_snap_transform(self, node):
        """检查节点的 transform 是否可安全移动/冻结

        带约束或锁定的通道会让 setAttr / makeIdentity 报错，旧实现会因此中断整个
        循环，留下"只移动了一半"的半成品，所以先做预检。

        :param node: transform 节点（长名）
        :return: (bool, str)，第二个值是不可处理的原因
        """
        channels = ("translateX", "translateY", "translateZ",
                    "rotateX", "rotateY", "rotateZ",
                    "scaleX", "scaleY", "scaleZ")
        for attr in ("translate", "rotate", "scale") + channels:
            plugs = cmds.listConnections("%s.%s" % (node, attr), source=True,
                                         destination=False, plugs=True) or []
            if plugs:
                return False, u"%s 有输入连接（%s）" % (attr, plugs[0])
        for attr in channels:
            try:
                if cmds.getAttr("%s.%s" % (node, attr), lock=True):
                    return False, u"%s 已锁定" % attr
            except Exception:
                pass
        return True, ""

    def snap_to_world_center_bottom(self, *args):
        """把选中物体（或组）的可见几何居中到世界原点，最低点压到 Y=0 后冻结变换

        v146 修复要点：
        1. 位移改用世界空间相对移动（cmds.move -ws -r），父级带旋转/缩放时不再产生
           偏移。旧实现把世界空间增量直接加到本地 translate 通道上，误差正比于物体
           离原点的距离，大场景里尤其明显；
        2. 包围盒只统计可见网格，隐藏子物体不再拉偏居中基准；
        3. translate/rotate/scale 有输入连接或锁定时跳过并提示，冻结失败只警告不中断，
           避免留下"只移动了一半"的半成品状态；
        4. 整段放进一个 undo chunk，撤销一次即可回到原位（旧实现是三笔独立操作）；
        5. 只冻结自身带网格且没有子 transform 的叶子物体，选中"组"时只移动不冻结，
           避免把子物体的旋转/缩放级联烘焙掉。

        :param args: Maya button callback 附带的多余参数
        :return: None
        """
        nodes = cmds.ls(selection=True, type="transform", long=True) or []
        if not nodes:
            lj_warn(u"没有选中任何物体！")
            return

        skipped = []
        cmds.undoInfo(openChunk=True, chunkName="Snap To World Center")
        try:
            for node in nodes:
                can_snap, reason = self.can_snap_transform(node)
                if not can_snap:
                    skipped.append(u"%s：%s" % (node, reason))
                    continue

                bbox = self.get_visible_world_bbox(node)
                if bbox is None:
                    skipped.append(u"%s：没有可见几何" % node)
                    continue

                # 世界空间把包围盒中心推到原点（父级旋转/缩放由 Maya 负责换算）
                cmds.move(-(bbox[0] + bbox[3]) / 2.0,
                          -(bbox[1] + bbox[4]) / 2.0,
                          -(bbox[2] + bbox[5]) / 2.0,
                          node, worldSpace=True, relative=True)

                # 世界空间把最低点压到 Y=0
                bbox = self.get_visible_world_bbox(node)
                if bbox:
                    cmds.move(0, -bbox[1], 0, node, worldSpace=True, relative=True)

                # 只冻结叶子网格；组（含子级 transform）只移动，避免级联烘焙
                has_mesh = bool(cmds.listRelatives(node, shapes=True, noIntermediate=True))
                has_child_transform = bool(cmds.listRelatives(node, children=True, type="transform"))
                if has_mesh and not has_child_transform:
                    try:
                        cmds.makeIdentity(node, apply=True, translate=True,
                                          rotate=True, scale=True, normal=False)
                    except Exception as error:
                        lj_warn(u"%s 冻结失败：%s" % (node, error))
        finally:
            cmds.undoInfo(closeChunk=True)

        # 移动后恢复原有选择，便于连续操作
        if cmds.ls(selection=True, type="transform", long=True) != nodes:
            cmds.select(nodes, replace=True)

        if skipped:
            lj_warn(u"已跳过 %d 个物体：%s" % (len(skipped), skipped[0]))

        
    def get_selected_transforms(self):
        """按选择顺序返回选中的物体，组件选择归到它所属的 transform

        只用 ls(type="transform") 拿到的是"整物件选择"，一旦先选的是点/面就会漏掉
        物体、甚至把后面才选的对象当成参考，所以逐项往上追溯到 transform 并去重。

        :return: transform 长名列表，第一个即最早选中的物体
        """
        nodes = []
        for item in cmds.ls(selection=True, long=True) or []:
            node = item
            while node and cmds.nodeType(node) != "transform":
                parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
                node = parents[0] if parents else None
            if node and node not in nodes:
                nodes.append(node)
        return nodes

    def can_move_axis(self, node):
        """检查节点的坐标轴原点能否改动（pivot 通道没锁没连接、translate 可写）

        pivot_set_pivot 是先搬 pivot、再用世界矩阵的位移差回写 translate 做补偿，
        通道被锁定或被连接时会在中间那一步报错，留下 pivot 已经搬走、补偿却没做
        的错位物体，所以逐个预检，不合格的整体跳过。

        :param node: transform 节点（长名）
        :return: (bool, str)，第二个值是不可处理的原因
        """
        channels = ("rotatePivotX", "rotatePivotY", "rotatePivotZ",
                    "scalePivotX", "scalePivotY", "scalePivotZ",
                    "translateX", "translateY", "translateZ")
        for attr in ("rotatePivot", "scalePivot", "translate") + channels:
            plugs = cmds.listConnections("%s.%s" % (node, attr), source=True,
                                         destination=False, plugs=True) or []
            if plugs:
                return False, u"%s 有输入连接（%s）" % (attr, plugs[0])
        for attr in channels:
            try:
                if cmds.getAttr("%s.%s" % (node, attr), lock=True):
                    return False, u"%s 已锁定" % attr
            except Exception:
                pass
        return True, ""

    def match_axis_to_first(self, *args):
        """把后选中物体的坐标轴原点搬到第一个选中物体的 pivot 位置，物体本身不动

        选择顺序就是优先级：第一个选中的是参考，其余物体只搬坐标轴原点
        （rotate pivot 和 scale pivot 一起对齐到同一个点），坐标轴的朝向保持各自
        原值，所以几何在世界空间里一步都不会动，之后转动物体时才会绕这个共同
        的原点转。

        :param args: Maya button callback 附带的多余参数
        :return: None
        """
        nodes = self.get_selected_transforms()
        if len(nodes) < 2:
            lj_warn(u"请先选中参考物体，再加选需要匹配的物体！")
            return

        ref_pivot = cmds.xform(nodes[0], query=True, worldSpace=True, rotatePivot=True)
        skipped = []
        cmds.undoInfo(openChunk=True, chunkName="Match Axis")
        try:
            for node in nodes[1:]:
                can_move, reason = self.can_move_axis(node)
                if not can_move:
                    skipped.append(u"%s：%s" % (node, reason))
                    continue
                self.pivot_set_pivot(node, ref_pivot)
        finally:
            cmds.undoInfo(closeChunk=True)

        if skipped:
            lj_warn(u"已跳过 %d 个物体：%s" % (len(skipped), skipped[0]))

    # --------------------- pivot boundary（Pivot 物体边界对齐） ---------------------

    # 平面按钮配色：{平面: (激活色, 未激活色)}
    PIVOT_PLANE_COLORS = {
        'xy': ((0.25, 0.45, 0.7), (0.10, 0.12, 0.16)),   # 蓝
        'zy': ((0.7, 0.35, 0.25), (0.16, 0.11, 0.10)),   # 红
        'zx': ((0.35, 0.7, 0.35), (0.10, 0.15, 0.11)),   # 绿
    }

    def pivot_get_bbox(self):
        """返回当前选中物体及其世界包围盒

        :return: (物体名, [xmin, ymin, zmin, xmax, ymax, zmax])，未选中时返回 (None, None)
        """
        sel = cmds.ls(selection=True, long=True)
        if not sel:
            lj_warn(u"请先选中物体！")
            return None, None
        obj = sel[0]
        return obj, cmds.exactWorldBoundingBox(obj)

    def pivot_set_pivot(self, obj, point):
        """把物体 rotate/scale pivot 设到世界坐标 point，并做位移补偿保证物体不被带跑

        pivot 只改变旋转/缩放中心，不影响世界矩阵的线性部分，
        对比设置前后世界矩阵的平移分量，反向移动即可让物体留在原位。

        :param obj: transform 节点名
        :param point: [x, y, z] 世界坐标
        """
        before = cmds.xform(obj, query=True, matrix=True, worldSpace=True)

        cmds.xform(obj, worldSpace=True, rotatePivot=point)
        cmds.xform(obj, worldSpace=True, scalePivot=point)

        after = cmds.xform(obj, query=True, matrix=True, worldSpace=True)
        dx = after[12] - before[12]
        dy = after[13] - before[13]
        dz = after[14] - before[14]

        if abs(dx) > 1e-9 or abs(dy) > 1e-9 or abs(dz) > 1e-9:
            cmds.move(-dx, -dy, -dz, obj, relative=True, worldSpace=True)

    def pivot_move_in_plane(self, direction):
        """沿当前平面把 pivot 推到包围盒对应方向的边缘

        :param direction: "left" / "right" / "up" / "down"
        """
        obj, bbox = self.pivot_get_bbox()
        if not obj:
            return

        xmin, ymin, zmin, xmax, ymax, zmax = bbox
        px, py, pz = cmds.xform(obj, query=True, worldSpace=True, rotatePivot=True)

        # 各平面下左右/上下方向对应的轴与包围盒边界
        axis_map = {
            "xy": {"lr_axis": 0, "ud_axis": 1, "lr_min": xmin, "lr_max": xmax, "ud_min": ymin, "ud_max": ymax},
            "zy": {"lr_axis": 2, "ud_axis": 1, "lr_min": zmin, "lr_max": zmax, "ud_min": ymin, "ud_max": ymax},
            "zx": {"lr_axis": 0, "ud_axis": 2, "lr_min": xmin, "lr_max": xmax, "ud_min": zmin, "ud_max": zmax},
        }
        cfg = axis_map[self.pivot_plane]

        new_rp = [px, py, pz]
        if direction == "left":
            new_rp[cfg["lr_axis"]] = cfg["lr_min"]
        elif direction == "right":
            new_rp[cfg["lr_axis"]] = cfg["lr_max"]
        elif direction == "up":
            new_rp[cfg["ud_axis"]] = cfg["ud_max"]
        elif direction == "down":
            new_rp[cfg["ud_axis"]] = cfg["ud_min"]

        cmds.undoInfo(openChunk=True, chunkName="Pivot To BBox Edge")
        try:
            self.pivot_set_pivot(obj, new_rp)
        finally:
            cmds.undoInfo(closeChunk=True)

    def pivot_set_plane(self, plane):
        """切换当前操作平面，并用颜色标出激活的平面按钮

        :param plane: "xy" / "zy" / "zx"
        """
        self.pivot_plane = plane
        for p, btn_name in self._pivot_plane_btns.items():
            active_color, inactive_color = self.PIVOT_PLANE_COLORS[p]
            components.button(btn_name, e=True,
                        bgc=(active_color if p == plane else inactive_color))

    # ------------------------------------------ naming ------------------------------------------

    TEX_EXTS = ('.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr')
    DEFAULT_TOKENS = (
        # key, label, default text, placeholder hint
        ('mesh_pre', u'Mesh 前缀', '', u'例如 SM_'),
        ('mesh_suf', u'Mesh 后缀', '', u'例如 _01'),
        ('shd_pre', u'材质前缀', '', u'例如 M_'),
        ('shd_suf', u'材质后缀', '', u'例如 _v01 或 _001'),
        ('tex_pre', u'贴图前缀', '', u'例如 T_'),
        ('col_suf', u'颜色后缀', '', u'例如 _color'),
        ('nml_suf', u'法线后缀', '', u'例如 _normal'),
    )

    # Column order: Select, Tris, UV set, Mesh name, Shader name,
    # Color map, Normal map, Delete

    _WINDOW = None


    def strip_ns(name):
        return name.split('|')[-1].rsplit(':', 1)[-1]


    def short_name(node):
        return node.split('|')[-1].rsplit(':', 1)[-1]


    def _token_list(token):
        """A token field may hold several alternatives: 'SM_, SK_'."""
        return [t.strip() for t in (token or '').split(',') if t.strip()]


    def _add_token(token):
        toks = LingJuGeometryTools._token_list(token)
        return toks[0] if toks else ''


    def _strip_token(name, token, suffix=False):
        """Remove a leading (or trailing) token, case-insensitive."""
        for t in LingJuGeometryTools._token_list(token):
            if suffix:
                if name.lower().endswith(t.lower()):
                    return name[: -len(t)]
            elif name.lower().startswith(t.lower()):
                return name[len(t):]
        return name


    def suffix_sequence(base, index):
        """Material suffix for the index-th shader of one mesh.

        A numeric tail counts up (_v01 -> _v02, _001 -> _002) and keeps its
        zero padding; a non-numeric base appends _01, _02 ...; an empty base
        returns an empty string — no auto-generated numeric suffix when the
        user left the suffix field blank.
        """
        base = base or ''
        if not base:
            return ''
        m = re.search(r'^(.*?)(\d+)$', base)
        if m:
            return m.group(1) + str(int(m.group(2)) + index).zfill(len(m.group(2)))
        return '%s_%02d' % (base, index + 1)


    def strip_suffix_sequence(name, base):
        """Remove a previously applied suffix sequence from a shader stem so
        numbering starts from the base again (_v01 / _001).

        If base is empty the user does not want any suffix numbering — leave
        the name completely untouched, including any trailing digits that were
        part of the original scene name."""
        base = base or ''
        if not base:
            return name
        m = re.match(r'^(.*?)(\d+)$', base)
        if m:
            pattern = re.escape(m.group(1)) + r'\d{%d,}$' % len(m.group(2))
        else:
            pattern = re.escape(base) + r'_\d+$'
        return re.sub(pattern, '', name)


    def mesh_stem(name, tokens):
        """Mesh short name minus its prefix/suffix tokens."""
        stem = LingJuGeometryTools._strip_token(LingJuGeometryTools.strip_ns(name), tokens['mesh_pre'])
        return LingJuGeometryTools._strip_token(stem, tokens['mesh_suf'], suffix=True)


    def shader_stem(blinn, tokens):
        """Blinn stem for renaming: own shader tokens first, then the mesh
        prefix (M_SM_Crate -> Crate)."""
        stem = LingJuGeometryTools._strip_token(LingJuGeometryTools.strip_ns(blinn), tokens['shd_pre'])
        stem = LingJuGeometryTools._strip_token(stem, tokens['shd_suf'], suffix=True)
        return LingJuGeometryTools._strip_token(stem, tokens['mesh_pre'])


    def shader_match_stems(blinn, mesh, tokens):
        """Ordered stems for texture matching, most specific first: the
        Blinn's own stem, then that stem with a trailing auto-number removed
        (multi-material meshes carry _01/_02 or _v02 style tails that shared
        textures do not), and finally the mesh stem as a fallback for scenes
        whose materials were never renamed after the mesh."""
        stem = LingJuGeometryTools.shader_stem(blinn, tokens)
        out = [stem]
        if LingJuGeometryTools._add_token(tokens['shd_suf']):
            base = LingJuGeometryTools.strip_suffix_sequence(
                stem, tokens['shd_suf'])
        else:
            base = re.sub(r'_\d+$', '', stem)
        if base != stem:
            out.append(base)
        out.append(LingJuGeometryTools.mesh_stem(mesh, tokens))
        return out


    def _attr_exists(node, attr):
        return cmds.attributeQuery(attr, node=node, exists=True)


    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None


    @contextlib.contextmanager
    def _undo_chunk(name='AssetManager'):
        cmds.undoInfo(openChunk=True, chunkName=name)
        try:
            yield
        finally:
            cmds.undoInfo(closeChunk=True)


    def _warn(msg):
        lj_warn('AssetManager: %s' % msg)


    def delete_unused_nodes():
        """Maya's own Delete Unused Nodes pass (Hypershade button). Returns the
        number of nodes removed."""
        try:
            return int(mel.eval('MLdeleteUnused()'))
        except Exception:
            return 0


    # ---------------------------------------------------------------------------
    # scene queries

    def scene_meshes():
        """All renderable polygon meshes as (transform, shape) long-name pairs.

        Intermediate objects are skipped: they are byproducts of modelling
        operations, never rendered, and giving them material only leaves a
        pile of shading nodes that nothing actually uses."""
        out = []
        for shape in cmds.ls(type='mesh', long=True) or []:
            if LingJuGeometryTools._safe(cmds.getAttr,
                    shape + '.intermediateObject'):
                continue
            parents = LingJuGeometryTools._safe(cmds.listRelatives, shape, parent=True,
                            fullPath=True) or []
            if parents:
                out.append((parents[0], shape))
        return out


    def _is_shading_engine(node):
        """True when node exists and is a shadingEngine."""
        return bool(node) and LingJuGeometryTools._safe(
            cmds.nodeType, node) == 'shadingEngine'


    def iog_group_count(shape):
        """Number of instObjGroups groups on a mesh shape. More than one means
        the mesh carries face-level (multi-material) assignments."""
        idx = LingJuGeometryTools._safe(cmds.getAttr, shape + '.iog',
                    multiIndices=True) or []
        return len(idx)


    def _iog_engines(shape):
        """Shading engines reached through the iog component groups of a shape."""
        out = []
        idx = LingJuGeometryTools._safe(cmds.getAttr, shape + '.iog',
                    multiIndices=True) or []
        for i in idx:
            for sg in (LingJuGeometryTools._safe(cmds.listConnections,
                    '%s.iog[%d]' % (shape, i), type='shadingEngine',
                    destination=True, source=False) or []):
                if sg not in out:
                    out.append(sg)
        return out


    def engines_for_shape(shape):
        """Shading engines of a mesh shape, without initialShadingGroup.

        Face-level assignments live on the shape's iog component groups and
        an object-level listSets call does not report them, so every source is
        merged: object-level sets, component-level sets, direct shadingEngine
        connections and the iog group lookup. Missing any of these used to
        make a multi-material mesh look like "no material" and get overwritten.
        """
        engs = []
        for src in (
            LingJuGeometryTools._safe(cmds.listSets, object=shape, type=1),
            LingJuGeometryTools._safe(cmds.listSets, object=shape, type=2),
            LingJuGeometryTools._safe(cmds.listConnections, shape,
                type='shadingEngine', destination=True, source=False),
            LingJuGeometryTools._iog_engines(shape),
        ):
            for e in src or []:
                if LingJuGeometryTools._is_shading_engine(e) and e not in engs:
                    engs.append(e)
        return [e for e in engs
                if LingJuGeometryTools.strip_ns(e) != 'initialShadingGroup']


    def shader_of_engine(engine):
        mats = LingJuGeometryTools._safe(cmds.listConnections, engine + '.surfaceShader',
                    source=True, destination=False) or []
        for m in mats:
            if cmds.nodeType(m) == 'blinn':
                return m
        return mats[0] if mats else None


    def uv_sets_of(shape):
        return cmds.polyUVSet(shape, query=True, allUVSets=True) or ['map1']


    def current_uv(shape):
        v = LingJuGeometryTools._safe(cmds.polyUVSet, shape, query=True, currentUVSet=True)
        if isinstance(v, (list, tuple)):
            return v[0] if v else 'map1'
        return v or 'map1'


    def tris_of(shape):
        try:
            return int(cmds.polyEvaluate(shape, triangle=True) or 0)
        except Exception:
            return 0


    def color_file_of(blinn):
        plugs = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.color',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'file':
                return node
        return ''


    def bump_of(blinn):
        plugs = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.normalCamera',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'bump2d':
                return node
        return ''


    def bump_file_of(bump):
        if not bump:
            return ''
        plugs = LingJuGeometryTools._safe(cmds.listConnections, bump + '.bumpValue',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'file':
                return node
        return ''


    # ---------------------------------------------------------------------------
    # material slot capture / restore

    PLACE2D_ATTRS = ('coverage', 'translateFrame', 'rotateFrame', 'mirrorU',
                    'mirrorV', 'stagger', 'wrapU', 'wrapV', 'repeatUV',
                    'offset', 'rotateUV', 'noiseUV')


    def _place2d_of(fnode):
        srcs = LingJuGeometryTools._safe(cmds.listConnections, fnode + '.uvCoord',
                    source=True, destination=False, type='place2dTexture') or []
        return srcs[0] if srcs else ''


    def capture_place2d(fnode):
        place = LingJuGeometryTools._place2d_of(fnode)
        out = {}
        if not place:
            return out
        for attr in LingJuGeometryTools.PLACE2D_ATTRS:
            val = LingJuGeometryTools._safe(cmds.getAttr, place + '.' + attr)
            if val is not None:
                out[attr] = val
        return out


    def apply_attrs(node, values):
        for attr, val in (values or {}).items():
            if not node or not cmds.objExists(node):
                continue
            if not LingJuGeometryTools._attr_exists(node, attr):
                continue
            try:
                if isinstance(val, (list, tuple)):
                    cmds.setAttr(node + '.' + attr, *val)
                else:
                    cmds.setAttr(node + '.' + attr, val)
            except Exception:
                pass


    def capture_material_maps(blinn):
        """Remember a Blinn's color / normal input: file path plus place2d
        parameters, or the flat color when there is no color file."""
        color = color_value = normal = None
        fnode = LingJuGeometryTools.color_file_of(blinn)
        if fnode:
            color = {
                'path': LingJuGeometryTools._safe(
                    cmds.getAttr, fnode + '.fileTextureName') or '',
                'place': LingJuGeometryTools.capture_place2d(fnode)}
        else:
            val = LingJuGeometryTools._safe(cmds.getAttr, blinn + '.color')
            if val:
                color_value = tuple(val[0])
        bump = LingJuGeometryTools.bump_of(blinn)
        nnode = LingJuGeometryTools.bump_file_of(bump)
        if nnode:
            normal = {
                'path': LingJuGeometryTools._safe(
                    cmds.getAttr, nnode + '.fileTextureName') or '',
                'place': LingJuGeometryTools.capture_place2d(nnode),
                'depth': LingJuGeometryTools._safe(
                    cmds.getAttr, bump + '.bumpDepth')}
        return color, color_value, normal


    def restore_material_maps(blinn, slot, color_on, normal_on,
                              connect_alpha=False):
        if color_on:
            color = slot.get('color')
            if color and color.get('path'):
                fnode = LingJuGeometryTools.set_color_file(
                    blinn, color['path'],
                    LingJuGeometryTools._node_safe_name(color['path']),
                    connect_alpha=connect_alpha)
                LingJuGeometryTools.apply_attrs(
                    LingJuGeometryTools._place2d_of(fnode), color.get('place'))
            elif slot.get('color_value'):
                try:
                    cmds.setAttr(blinn + '.color', *slot['color_value'])
                except Exception:
                    pass
        if normal_on:
            normal = slot.get('normal')
            if normal and normal.get('path'):
                fnode, bump = LingJuGeometryTools.set_normal_file(
                    blinn, normal['path'],
                    LingJuGeometryTools._node_safe_name(normal['path']))
                LingJuGeometryTools.apply_attrs(
                    LingJuGeometryTools._place2d_of(fnode), normal.get('place'))
                if normal.get('depth') is not None:
                    LingJuGeometryTools._safe(cmds.setAttr,
                                            bump + '.bumpDepth', normal['depth'])


    # ---------------------------------------------------------------------------
    # scene mutations

    # Shader node types that may legitimately occupy a material name and are
    # safe to reclaim when nothing references them any more.
    SHADER_TYPES = frozenset((
        'lambert', 'blinn', 'phong', 'phongE', 'anisotropic',
        'rampShader', 'surfaceShader', 'useBackground', 'layeredShader',
        'shaderGlow', 'shadingMap', 'aiStandardSurface', 'standardSurface',
    ))


    def _take_name(node, name):
        """Rename node to name exactly. shadingNode appends the node type, so
        we rename afterwards. If the name is occupied by an orphan shader
        (no shading group using it), free it; otherwise fall back to a numeric
        suffix and keep the node alive."""
        try:
            return cmds.rename(node, name)
        except Exception:
            pass
        if cmds.objExists(name) and cmds.nodeType(name) in LingJuGeometryTools.SHADER_TYPES:
            sgs = LingJuGeometryTools._safe(cmds.listConnections,
                        name + '.outColor', destination=True, source=False,
                        type='shadingEngine') or []
            if not sgs:
                try:
                    cmds.delete(name)
                    return cmds.rename(node, name)
                except Exception:
                    pass
        i = 1
        while cmds.objExists('%s%d' % (name, i)):
            i += 1
        try:
            return cmds.rename(node, '%s%d' % (name, i))
        except Exception:
            return node


    def create_blinn(name):
        node = cmds.shadingNode('blinn', asShader=True, name=name)
        return LingJuGeometryTools._take_name(node, name)


    def create_engine_for(blinn):
        engine = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                        name=blinn + 'SG')
        cmds.connectAttr(blinn + '.outColor', engine + '.surfaceShader',
                        force=True)
        return engine


    def assign_engine(mesh, engine):
        cmds.sets(mesh, edit=True, forceElement=engine)


    def _node_safe_name(name):
        """File-node name from anything typed in a map field: folders and the
        extension are stripped so both D:\\tex\\T_Wood.png and T_Wood give
        T_Wood."""
        stem = name.replace('\\', '/').split('/')[-1]
        stem = os.path.splitext(stem)[0]
        return stem or 'file'


    def _same_path(a, b):
        return bool(a) and bool(b) and (
            os.path.normcase(os.path.normpath(a))
            == os.path.normcase(os.path.normpath(b)))


    def _make_file_node(name):
        stem = LingJuGeometryTools._node_safe_name(name)
        fnode = cmds.shadingNode('file', asTexture=True, name=stem)
        fnode = LingJuGeometryTools._take_name(fnode, stem)
        LingJuGeometryTools._ensure_place2d(fnode)
        return fnode


    PLACE2D_LINKS = (
        ('coverage', 'coverage'), ('translateFrame', 'translateFrame'),
        ('rotateFrame', 'rotateFrame'), ('mirrorU', 'mirrorU'),
        ('mirrorV', 'mirrorV'), ('stagger', 'stagger'),
        ('wrapU', 'wrapU'), ('wrapV', 'wrapV'), ('repeatUV', 'repeatUV'),
        ('offset', 'offset'), ('rotateUV', 'rotateUV'), ('noiseUV', 'noiseUV'),
        ('vertexUvOne', 'vertexUvOne'), ('vertexUvTwo', 'vertexUvTwo'),
        ('vertexUvThree', 'vertexUvThree'),
        ('vertexCameraOne', 'vertexCameraOne'),
        ('outUV', 'uvCoord'), ('outUvFilterSize', 'uvFilterSize'),
    )


    def _ensure_place2d(fnode):
        """Give a file node the standard place2dTexture wiring that Maya's own
        UI creates, if it does not have one yet."""
        srcs = LingJuGeometryTools._safe(cmds.listConnections, fnode + '.uvCoord',
                    source=True, destination=False) or []
        if srcs:
            return
        place_name = LingJuGeometryTools.short_name(fnode) + '_place2d'
        place = cmds.shadingNode('place2dTexture', asUtility=True, name=place_name)
        place = LingJuGeometryTools._take_name(place, place_name)
        for src, dst in LingJuGeometryTools.PLACE2D_LINKS:
            if LingJuGeometryTools._attr_exists(place, src) and LingJuGeometryTools._attr_exists(fnode, dst):
                cmds.connectAttr('%s.%s' % (place, src),
                                '%s.%s' % (fnode, dst), force=True)


    def _set_file_path(fnode, path, raw=False):
        cmds.setAttr(fnode + '.fileTextureName', path, type='string')
        if raw and LingJuGeometryTools._attr_exists(fnode, 'colorSpace'):
            cmds.setAttr(fnode + '.colorSpace', 'Raw', type='string')


    def set_transparency_link(blinn, fnode, enabled):
        """Match the material's transparency input to the checkbox: enabled
        wires file.outTransparency -> material.transparency, disabled cuts
        exactly that link. Only this material and this file node are touched;
        nodes are never deleted, and shaders without the attributes (ai etc.)
        are skipped. Called from every entry point that connects a color map."""
        if not fnode or not LingJuGeometryTools._attr_exists(
                fnode, 'outTransparency') or not LingJuGeometryTools._attr_exists(
                blinn, 'transparency'):
            return
        src = fnode + '.outTransparency'
        dest = blinn + '.transparency'
        linked = [d for d in LingJuGeometryTools._safe(
            cmds.listConnections, src, source=False, destination=True,
            plugs=True) or [] if d == dest or d in (
                dest + 'R', dest + 'G', dest + 'B')]
        if enabled:
            if not linked:
                cmds.connectAttr(src, dest, force=True)
        else:
            for plug in linked:
                try:
                    cmds.disconnectAttr(src, plug)
                except Exception:
                    pass


    def set_color_file(blinn, path, name, connect_alpha=False):
        """Different file name: the old feed is deleted (shared nodes kept) and
        a new file node named after the typed name is created. Same name: only
        the file path is updated. connect_alpha mirrors the checkbox through
        set_transparency_link (connect when on, cut when off)."""
        name = LingJuGeometryTools._node_safe_name(name)
        fnode = LingJuGeometryTools.color_file_of(blinn)
        if fnode and LingJuGeometryTools.short_name(fnode) == name:
            LingJuGeometryTools._ensure_place2d(fnode)
        else:
            # cut the old file's transparency feed first, otherwise it counts
            # as a user and _clear_plug_sources keeps the stale node
            LingJuGeometryTools.set_transparency_link(blinn, fnode, False)
            LingJuGeometryTools._clear_plug_sources(blinn + '.color')
            fnode = LingJuGeometryTools._make_file_node(name)
        LingJuGeometryTools._set_file_path(fnode, path)
        cmds.connectAttr(fnode + '.outColor', blinn + '.color', force=True)
        LingJuGeometryTools.set_transparency_link(
            blinn, fnode, bool(connect_alpha))
        return fnode


    def set_normal_file(blinn, path, name):
        """Connect a tangent-space normal map exactly like Maya's UI does:
        place2dTexture -> file (Raw) -> file.outAlpha -> bump2d.bumpValue with
        bumpInterp = 1 (Tangent Space Normals) -> bump2d.outNormal ->
        blinn.normalCamera. A different file name replaces the old file tree
        and renames the bump to match; the same name only updates the path."""
        name = LingJuGeometryTools._node_safe_name(name)
        bump = LingJuGeometryTools.bump_of(blinn)
        fnode = LingJuGeometryTools.bump_file_of(bump) if bump else ''
        if fnode and LingJuGeometryTools.short_name(fnode) == name:
            LingJuGeometryTools._ensure_place2d(fnode)
        else:
            if bump:
                LingJuGeometryTools._clear_plug_sources(bump + '.bumpValue')
            else:
                LingJuGeometryTools._clear_plug_sources(blinn + '.normalCamera')
            fnode = LingJuGeometryTools._make_file_node(name)
        if not bump:
            bump = cmds.shadingNode('bump2d', asUtility=True,
                                    name=name + '_bump')
            bump = LingJuGeometryTools._take_name(bump, name + '_bump')
            cmds.connectAttr(bump + '.outNormal', blinn + '.normalCamera',
                            force=True)
        elif LingJuGeometryTools.short_name(bump) != name + '_bump':
            bump = LingJuGeometryTools._take_name(bump, name + '_bump')
        if LingJuGeometryTools._attr_exists(bump, 'bumpInterp'):
            cmds.setAttr(bump + '.bumpInterp', 1)
        LingJuGeometryTools._set_file_path(fnode, path, raw=True)
        cmds.connectAttr(fnode + '.outAlpha', bump + '.bumpValue', force=True)
        return fnode, bump


    def _has_other_users(node):
        dests = LingJuGeometryTools._safe(cmds.listConnections, node, destination=True,
                    source=False, plugs=True) or []
        for d in dests:
            if d.split('.')[0] != node:
                return True
        return False


    def _delete_file_node_tree(fnode):
        """Delete a file node plus its place2dTexture when nothing else
        uses them."""
        if not fnode or not cmds.objExists(fnode):
            return
        places = LingJuGeometryTools._safe(cmds.listConnections, fnode, source=True,
                    destination=False, type='place2dTexture') or []
        if not LingJuGeometryTools._has_other_users(fnode):
            cmds.delete(fnode)
        for p in places:
            if cmds.objExists(p) and not LingJuGeometryTools._has_other_users(p):
                cmds.delete(p)


    def _clear_plug_sources(plug):
        """Disconnect every source feeding plug and delete the freed nodes:
        file trees (file + place2dTexture) via LingJuGeometryTools._delete_file_node_tree, any
        other node nothing else uses. Shared nodes are always kept."""
        plugs = LingJuGeometryTools._safe(cmds.listConnections, plug, source=True,
                    destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            try:
                cmds.disconnectAttr(p, plug)
            except Exception:
                pass
            if not cmds.objExists(node):
                continue
            if cmds.nodeType(node) == 'file':
                LingJuGeometryTools._delete_file_node_tree(node)
            elif not LingJuGeometryTools._has_other_users(node):
                try:
                    cmds.delete(node)
                except Exception:
                    pass


    def disconnect_color(blinn):
        """Empty color field: remove everything feeding blinn.color — the file
        node with its place2dTexture, or any other texture source. The file's
        transparency feed is cut too so the node is not kept alive."""
        LingJuGeometryTools.set_transparency_link(
            blinn, LingJuGeometryTools.color_file_of(blinn), False)
        LingJuGeometryTools._clear_plug_sources(blinn + '.color')


    def disconnect_normal(blinn):
        """Empty normal field: remove bump2d plus the normal file node with its
        place2dTexture, and any stray direct feed on normalCamera."""
        bump = LingJuGeometryTools.bump_of(blinn)
        fnode = LingJuGeometryTools.bump_file_of(bump)
        if bump:
            try:
                cmds.disconnectAttr(bump + '.outNormal',
                                    blinn + '.normalCamera')
            except Exception:
                pass
            if cmds.objExists(bump) and not LingJuGeometryTools._has_other_users(bump):
                try:
                    cmds.delete(bump)
                except Exception:
                    pass
        LingJuGeometryTools._clear_plug_sources(blinn + '.normalCamera')
        LingJuGeometryTools._delete_file_node_tree(fnode)


    def delete_shader_tree(blinn, mesh_transform):
        """Delete a Blinn and its file / bump2d nodes. Shading engines are only
        deleted when no other mesh still uses them."""
        engines = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.outColor',
                        destination=True, type='shadingEngine') or []
        bump = LingJuGeometryTools.bump_of(blinn)
        color_f = LingJuGeometryTools.color_file_of(blinn)
        nml_f = LingJuGeometryTools.bump_file_of(bump)
        try:
            cmds.delete(blinn)
        except Exception:
            pass
        mesh_short = LingJuGeometryTools.short_name(mesh_transform)
        for e in engines:
            if not cmds.objExists(e):
                continue
            members = LingJuGeometryTools._safe(cmds.sets, e, query=True) or []
            others = [m for m in members
                    if LingJuGeometryTools.short_name(m).split('.')[0] != mesh_short]
            if not others:
                try:
                    cmds.delete(e)
                except Exception:
                    pass
        for node in (bump, color_f, nml_f):
            if node and cmds.objExists(node) and not LingJuGeometryTools._has_other_users(node):
                try:
                    cmds.delete(node)
                except Exception:
                    pass


    # ---------------------------------------------------------------------------
    # texture matching / namespaces

    def _map_key(stem, tokens, suffix_key):
        """Case-insensitive match key for a texture file stem: texture prefix
        and the given map-type suffix (tokens[suffix_key]) stripped."""
        stem = LingJuGeometryTools._strip_token(stem, tokens['tex_pre'])
        stem = LingJuGeometryTools._strip_token(stem, tokens[suffix_key], suffix=True)
        return stem.lower()


    def find_texture_for(stems, tokens, files, suffix_key):
        """First file matching the stems in priority order. Stems are tried
        most specific first (exact numbered shader stem before the shared
        base, mesh stem last), so a per-slot texture like T_Crate_03_D wins
        over the shared T_Crate_D. Each file's stem is compared minus the
        texture prefix and the map-type suffix tokens[suffix_key]; matching
        is case-insensitive."""
        keyed = []
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            keyed.append(
                (LingJuGeometryTools._map_key(stem, tokens, suffix_key), f))
        for s in stems:
            s = LingJuGeometryTools.strip_ns(s)
            wanted = (s.lower(),
                      LingJuGeometryTools._strip_token(
                          s, tokens['mesh_pre']).lower())
            for key, f in keyed:
                if key in wanted:
                    return f
        return None


    def clear_namespaces():
        """Remove every namespace, merging contents into root."""
        removed = 0
        for _ in range(10):
            try:
                nss = cmds.namespaceInfo(listOnlyNamespaces=True) or []
            except Exception:
                break
            nss = [n for n in nss if n not in ('UI', 'shared')]
            if not nss:
                break
            progress = False
            for ns in nss:
                try:
                    cmds.namespace(removeNamespace=ns,
                                mergeNamespaceWithRoot=True)
                    removed += 1
                    progress = True
                except Exception:
                    pass
            if not progress:
                break
        return removed


    # ---------------------------------------------------------------------------
    # UI

    def _center_label(text):
        lab = QtWidgets.QLabel(text)
        lab.setAlignment(QtCore.Qt.AlignCenter)
        return lab


    def _image_drop_text(md):
        """Image file candidate from drag mime data: a local file URL (OS file
        explorer) or a plain-text file name. Returns None when the drag carries
        no usable image file."""
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in LingJuGeometryTools.TEX_EXTS:
                    return path
            return None
        if md.hasText():
            text = md.text().strip()
            ext = os.path.splitext(text)[1].lower()
            if not ext or ext in LingJuGeometryTools.TEX_EXTS:
                return text
        return None


    class _MapDropLineEdit(QtWidgets.QLineEdit):
        """Map field that accepts a dragged image file; the drop connects the
        texture right away.  Uses a plain on_dropped callback instead of
        QtCore.Signal (see _DraggableTabBar for the pyd/Maya2023 reason)."""

        def __init__(self, text='', parent=None):
            QtWidgets.QLineEdit.__init__(self, text, parent)
            self.setAcceptDrops(True)
            self.on_dropped = None     # 拖入回调: f(path_text)

        def dragEnterEvent(self, event):
            if LingJuGeometryTools._image_drop_text(event.mimeData()):
                event.acceptProposedAction()

        def dropEvent(self, event):
            text = LingJuGeometryTools._image_drop_text(event.mimeData())
            if text and self.on_dropped is not None:
                self.on_dropped(text)


    class AssetManagerUI(QtWidgets.QWidget):

        def __init__(self, parent=None):
            QtWidgets.QWidget.__init__(self, parent)
            # Floating tool window parented to the Maya main window, so it
            # always stacks above Maya like the toolset workspace control
            # instead of sinking behind it.
            self.setWindowFlags(QtCore.Qt.Tool)
            self.setWindowTitle(u'零距命名材质管理')
            self.setObjectName('PolygonAssetManagerUI')
            # All UI text uses Microsoft YaHei so Chinese renders consistently;
            # child widgets and dialogs inherit it from the window.
            self.setFont(QtGui.QFont('Microsoft YaHei', 9))
            self.resize(1240, 560)
            self.rows = []
            self.token_edits = {}
            # Material slots remembered when a mesh's materials are deleted:
            # {shape: [{'faces': [...], 'whole': bool, 'color': ...,
            #           'color_value': ..., 'normal': ...}, ...]}.
            self._deleted_slots = {}
            self._edits = {'shd': {}, 'color': {}, 'normal': {}}
            self._fields = []
            self._select_buttons = {}
            self._build_ui()
            # Read-only on open: the window must never touch materials that
            # are already wired up. 更新场景 is the pass that may create /
            # convert them.
            self.refresh_scene(mutate=False)

        # ---------------- construction ----------------

        def _build_ui(self):
            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)

            # Title bar
            title = QtWidgets.QHBoxLayout()
            tlab = QtWidgets.QLabel(u'零距命名材质管理')
            tlab.setStyleSheet('font-weight: 500;')
            title.addWidget(tlab)
            title.addStretch(1)
            title.addWidget(QtWidgets.QLabel(u'vieport Maya · Blinn'))
            root.addLayout(title)

            # Texture folder: label above, then input + Browse
            folder = QtWidgets.QVBoxLayout()
            folder.setSpacing(4)
            flab = QtWidgets.QLabel(u'贴图文件夹')
            flab.setStyleSheet('color: #c2c2c2c; font-size: 12px;')
            folder.addWidget(flab)
            folder_row = QtWidgets.QHBoxLayout()
            self.path_edit = QtWidgets.QLineEdit(r'')
            self.path_edit.editingFinished.connect(self._update_format_hint)
            folder_row.addWidget(self.path_edit, 1)
            self.fmt_hint = QtWidgets.QLabel('')
            self.fmt_hint.setStyleSheet('color: #53892c; font-size: 12px;')
            folder_row.addWidget(self.fmt_hint)
            browse = QtWidgets.QPushButton(u'浏览')
            browse.clicked.connect(self._browse_folder)
            folder_row.addWidget(browse)
            folder.addLayout(folder_row)
            root.addLayout(folder)
            self._update_format_hint()

            # 场景材质球：与烘焙页共用读取/刷新/赋予逻辑
            material_row = QtWidgets.QHBoxLayout()
            material_row.addWidget(QtWidgets.QLabel(u'场景材质球'))
            self.material_combo = QtWidgets.QComboBox()
            self.material_combo.setMinimumWidth(160)
            material_row.addWidget(self.material_combo, 1)
            refresh_mat = QtWidgets.QPushButton(u'刷新')
            refresh_mat.setToolTip(u'等同重开窗口：只读重读模型面数与材质对应；'
                                   u'场景挪动后在贴图文件夹按同名文件修复丢失的贴图路径')
            refresh_mat.clicked.connect(self.on_refresh_materials)
            material_row.addWidget(refresh_mat)
            assign_btn = QtWidgets.QPushButton(u'赋予选中物件 / 面')
            assign_btn.setToolTip(u'选中面时逐面赋予；只选物件时按整物体赋予')
            assign_btn.clicked.connect(self.on_assign_material)
            material_row.addWidget(assign_btn)
            material_row.addSpacing(16)
            material_row.addWidget(QtWidgets.QLabel(u'材质平面'))
            self.mat_count = QtWidgets.QSpinBox()
            self.mat_count.setRange(1, 20)
            self.mat_count.setValue(3)
            self.mat_count.setSuffix(u' 个材质')
            self.mat_count.setToolTip(u'生成的平面面数与材质数相同，逐面赋予一个材质球')
            material_row.addWidget(self.mat_count)
            self.mat_type = QtWidgets.QComboBox()
            self.mat_type.addItems(['lambert', 'blinn'])
            material_row.addWidget(self.mat_type)
            make_btn = QtWidgets.QPushButton(u'生成')
            make_btn.setToolTip(u'生成一个多材质平面：每个面赋予一个默认命名的新材质球')
            make_btn.clicked.connect(self.on_create_test_plane)
            material_row.addWidget(make_btn)
            material_row.addStretch(1)
            root.addLayout(material_row)
            m2m_scene_refresh_material_combo(self.material_combo)

            btn_row = QtWidgets.QHBoxLayout()
            refresh_btn = QtWidgets.QPushButton(u'更新场景')
            # clicked(bool) would land in refresh_scene's mutate argument, so
            # bind it explicitly to keep this a mutating pass.
            refresh_btn.clicked.connect(lambda: self.refresh_scene(mutate=True))
            refresh_btn.setStyleSheet(
                'QPushButton { background-color: #375b1d; color: #FFFFFF; }'
                'QPushButton:hover { background-color: #53892c; }'
                'QPushButton:pressed { background-color: #1c2e0f; }')
            btn_row.addWidget(refresh_btn)
            for label, slot in ((u'应用命名', self.on_apply_names),
                                (u'材质命名匹配模型', self.on_match_shaders),
                                (u'一键添加颜色贴图', self.on_assign_colors),
                                (u'一键连接法线', self.on_assign_normals)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                btn_row.addWidget(b)
            self.alpha_cb = QtWidgets.QCheckBox(u'颜色贴图连 Transparency')
            self.alpha_cb.setToolTip(
                u'勾选后，连接颜色贴图时同时把贴图节点的 outTransparency '
                u'连接到材质的 transparency；取消勾选后，各操作（含刷新）'
                u'会断开该颜色贴图节点到材质的 transparency 连线')
            btn_row.addWidget(self.alpha_cb)
            btn_row.addStretch(1)
            root.addLayout(btn_row)

            # Restore scope for multi-material meshes: 更新场景 rebuilds the
            # recorded material slots; tick these to bring the maps back too.
            restore_row = QtWidgets.QHBoxLayout()
            restore_row.addWidget(QtWidgets.QLabel(u'多维材质恢复范围:'))
            self.restore_color_cb = QtWidgets.QCheckBox(u'多维材质：恢复颜色贴图')
            self.restore_color_cb.setToolTip(
                u'更新场景时，为多材质 mesh 重建的材质球重新连接颜色贴图')
            restore_row.addWidget(self.restore_color_cb)
            self.restore_normal_cb = QtWidgets.QCheckBox(u'多维材质：恢复法线贴图')
            self.restore_normal_cb.setToolTip(
                u'更新场景时，为多材质 mesh 重建的材质球重新连接法线贴图')
            restore_row.addWidget(self.restore_normal_cb)
            restore_row.addStretch(1)
            root.addLayout(restore_row)

            # Second button row: scene cleanup tools
            btn_row2 = QtWidgets.QHBoxLayout()
            for label, slot in ((u'清除命名空间', self.on_clear_namespaces),
                                (u'删除未用节点', self.on_delete_unused)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                btn_row2.addWidget(b)
            btn_row2.addStretch(1)
            root.addLayout(btn_row2)

            # UV batch rename: renames the current UV set on the selected
            # meshes, or on every listed mesh when nothing is selected
            uv_row = QtWidgets.QHBoxLayout()
            uv_row.addWidget(QtWidgets.QLabel(u'重命名 UV 集:'))
            self.uv_batch = QtWidgets.QLineEdit('map1')
            self.uv_batch.setPlaceholderText('map1')
            self.uv_batch.setFixedWidth(160)
            uv_row.addWidget(self.uv_batch)
            uv_btn = QtWidgets.QPushButton(u'重命名 UV 集')
            uv_btn.clicked.connect(self.on_rename_uv_sets)
            uv_row.addWidget(uv_btn)
            uv_del_btn = QtWidgets.QPushButton(u'删除当前 UV 集')
            uv_del_btn.clicked.connect(self.on_delete_uv_sets)
            uv_row.addWidget(uv_del_btn)
            uv_row.addStretch(1)
            root.addLayout(uv_row)

            # Scrollable table area: header + token row + data rows
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            scroll.setFrameShape(QtWidgets.QFrame.StyledPanel)
            root.addWidget(scroll, 1)

            self.scroll_content = QtWidgets.QWidget()
            self.scroll_content.setMinimumWidth(1180)
            scroll.setWidget(self.scroll_content)

            self.rows_layout = QtWidgets.QVBoxLayout(self.scroll_content)
            self.rows_layout.setContentsMargins(0, 0, 0, 0)
            self.rows_layout.setSpacing(0)
            
            self.rows_layout.addWidget(self._make_token_row())
            self.rows_layout.addWidget(self._make_header_row())

            self.data_container = QtWidgets.QWidget()
            self.data_layout = QtWidgets.QVBoxLayout(self.data_container)
            self.data_layout.setContentsMargins(0, 0, 0, 0)
            self.data_layout.setSpacing(0)
            self.rows_layout.addWidget(self.data_container)
            self.rows_layout.addStretch(1)

            # Bottom status bar: all messages render in dark yellow
            self.status = QtWidgets.QLabel('')
            self.status.setStyleSheet('color: #9e7309;')
            root.addWidget(self.status)

        def _make_header_row(self):
            """Header row matching the canvas column widths."""
            w = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.setSpacing(8)

            self.select_all_btn = QtWidgets.QPushButton(u'全选')
            self.select_all_btn.setFixedWidth(50)
            self.select_all_btn.clicked.connect(self.on_toggle_select_all)
            lay.addWidget(self.select_all_btn)

            headers = (
                (u'面数', 50, False),
                (u'UV 集', 60, False),
                (u'Mesh 名称', 60, True),
                (u'材质名称', 60, True),
                (u'颜色贴图', 60, True),
                (u'法线贴图', 60, True),
            )
            for text, width, stretch in headers:
                lab = QtWidgets.QLabel(text)
                lab.setAlignment(QtCore.Qt.AlignCenter)
                lab.setStyleSheet(
                    'font-family: "Microsoft YaHei", sans-serif; font-weight: bold; color: #909090; font-size: 9px;'
                    'font-size: 16px;')
                if stretch:
                    lab.setMinimumWidth(width)
                    lay.addWidget(lab, 1)
                else:
                    lab.setFixedWidth(width)
                    lay.addWidget(lab)

            self.delete_all_btn = QtWidgets.QPushButton(u'全部删除')
            self.delete_all_btn.setFixedWidth(60)
            self.delete_all_btn.setStyleSheet(
                'QPushButton { background-color: #630000; color: #FFFFFF; }'
                'QPushButton:hover { background-color: #8d0000; }'
                'QPushButton:pressed { background-color: #470000; }')
            self.delete_all_btn.clicked.connect(self.on_delete_all)
            lay.addWidget(self.delete_all_btn)
            return w

        def _make_token_row(self):
            """Prefix/suffix token row sitting under the header"""
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(8, 8, 8, 8)
            v.setSpacing(8)

            grid = QtWidgets.QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(4)
            
            for col, (key, label, default, placeholder) in enumerate(LingJuGeometryTools.DEFAULT_TOKENS):
                grid.addWidget(QtWidgets.QLabel(label), 0, col)
                edit = QtWidgets.QLineEdit(default)
                edit.setPlaceholderText(placeholder)
                self.token_edits[key] = edit
                grid.addWidget(edit, 1, col)
            v.addLayout(grid)
            return w

        # ---------------- data ----------------

        def _tokens(self):
            return dict((k, e.text().strip()) for k, e in self.token_edits.items())

        def _status(self, text):
            self.status.setText(text)

        def _alpha_checked(self):
            return bool(self.alpha_cb.isChecked())

        def _default_mesh_name(self, mesh, tokens):
            """Tokenized mesh name; never doubles the prefix (SM_Crate stays
            SM_Crate even when it was spelled sm_crate)."""
            return (LingJuGeometryTools._add_token(tokens['mesh_pre']) + LingJuGeometryTools.mesh_stem(mesh, tokens)
                    + LingJuGeometryTools._add_token(tokens['mesh_suf']))

        def _default_shader_name(self, blinn, tokens, seq=None):
            """Tokenized shader name built from the Blinn stem with the mesh
            prefix removed (SM_Crate_01 / M_SM_Crate_01 -> M_Crate_01).

            seq numbers the shaders of a multi-material mesh, but only when
            the user actually filled in a shd_suf token — if the suffix field
            is blank we leave any original trailing digits alone and do not
            synthesize new ones."""
            stem = LingJuGeometryTools.shader_stem(blinn, tokens)
            suffix = LingJuGeometryTools._add_token(tokens['shd_suf'])
            if not suffix or seq is None:
                return (LingJuGeometryTools._add_token(tokens['shd_pre'])
                        + stem + suffix)
            stem = LingJuGeometryTools.strip_suffix_sequence(stem, suffix)
            return (LingJuGeometryTools._add_token(tokens['shd_pre']) + stem
                    + LingJuGeometryTools.suffix_sequence(suffix, seq))

        def refresh_scene(self, mutate=True):
            """Rebuild rows from the scene.

            mutate=False is what the window uses when it first opens: the pass
            is read-only, so no Blinn is created, no material is converted and
            no shading assignment is touched — opening the manager can never
            disturb materials that are already set up. The 更新场景 button
            calls it with mutate=True, which is what may create / convert
            materials and rebuild recorded material slots."""
            sel = set(cmds.ls(selection=True, long=True) or [])
            tokens = self._tokens()
            rows = []
            restored = pending = missing = 0
            self._prune_deleted_slots()
            with LingJuGeometryTools._undo_chunk('AssetManagerRefresh'):
                for transform, shape in LingJuGeometryTools.scene_meshes():
                    engines = LingJuGeometryTools.engines_for_shape(shape)
                    record = self._deleted_slots.get(shape)

                    def add_row(engine, mat):
                        rows.append({
                            'mesh': transform,
                            'shape': shape,
                            'engine': engine,
                            'blinn': mat,
                            'uv_sets': LingJuGeometryTools.uv_sets_of(shape),
                            'uv': LingJuGeometryTools.current_uv(shape),
                            'tris': LingJuGeometryTools.tris_of(shape),
                            'selected': transform in sel,
                        })

                    if not mutate:
                        # Read-only pass: list what already exists, change
                        # nothing in the scene.
                        if not engines:
                            missing += 1
                            continue
                        for engine in engines:
                            mat = LingJuGeometryTools.shader_of_engine(engine)
                            if mat is None:
                                missing += 1
                                continue
                            add_row(engine, mat)
                        continue

                    if record is not None and not engines:
                        # Trigger A: rebuild the recorded material slots on the
                        # mesh and clear the record right away.
                        engines = self._restore_material_slots(
                            transform, shape, tokens,
                            record['slots'], record.get('covered', 0))
                        self._deleted_slots.pop(shape, None)
                        restored += len(engines)
                    elif record is not None:
                        # Mesh already has material: keep the record for later.
                        pending += 1
                    elif not engines:
                        if LingJuGeometryTools.iog_group_count(shape) > 1:
                            # Face-level assignments exist but no engine could
                            # be resolved: never overwrite them with a single
                            # material, just skip this mesh.
                            missing += 1
                            LingJuGeometryTools._warn(
                                u'%s 有面级材质但引擎读取失败，已跳过'
                                % LingJuGeometryTools.short_name(transform))
                            continue
                        blinn = LingJuGeometryTools.create_blinn(
                            LingJuGeometryTools._add_token(tokens['shd_pre'])
                            + LingJuGeometryTools.mesh_stem(transform, tokens)
                            + LingJuGeometryTools._add_token(tokens['shd_suf']))
                        engine = LingJuGeometryTools.create_engine_for(blinn)
                        # Assign to the shape, never to the transform: one
                        # transform can carry several shapes, and assigning at
                        # transform level overwrites the material that the
                        # other shapes already had.
                        LingJuGeometryTools.assign_engine(shape, engine)
                        engines = [engine]
                    for engine in engines:
                        mat = LingJuGeometryTools.shader_of_engine(engine)
                        if mat is None:
                            mat = LingJuGeometryTools.create_blinn(
                                LingJuGeometryTools._add_token(tokens['shd_pre'])
                                + LingJuGeometryTools.mesh_stem(transform, tokens)
                                + LingJuGeometryTools._add_token(tokens['shd_suf']))
                            cmds.connectAttr(mat + '.outColor',
                                            engine + '.surfaceShader',
                                            force=True)
                        # Existing materials keep their own node type. Converting
                        # them to Blinn used to silently change how they look
                        # (a lambert gains specular), which is the "材质全乱"
                        # that was reported.
                        add_row(engine, mat)
            self.rows = rows
            self._populate_rows()
            note = ''
            if restored:
                note += u' · 恢复 %d 个材质位置' % restored
            if pending:
                note += u' · %d 个 mesh 已有材质，位置登记保留' % pending
            if missing:
                note += u' · %d 个 mesh 未读取到材质' % missing
            self._status(u'%d 个 mesh · %d 个材质 · %d 行%s' % (
                len(set(r['mesh'] for r in rows)),
                len(set(r['blinn'] for r in rows)), len(rows), note))

        def _populate_rows(self):
            """Rebuild the data row widgets under the header / token strip.
            Mesh-scoped controls appear only on the first row of each mesh;
            shader-scoped fields appear on every row."""
            tokens = self._tokens()
            while self.data_layout.count():
                item = self.data_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._edits = {'shd': {}, 'color': {}, 'normal': {}}
            self._fields = []
            self._select_buttons = {}
            first_of_mesh = {}
            users = {}
            mesh_blinns = {}
            for idx, r in enumerate(self.rows):
                # Keep the ROW index of a mesh's first occurrence: the render
                # loop below compares it against enumerate's i, so storing
                # len(first_of_mesh) here would shift once any mesh spans
                # more than one row and mislabel every later row as 同一 mesh.
                first_of_mesh.setdefault(r['mesh'], idx)
                users[r['blinn']] = users.get(r['blinn'], 0) + 1
                blinns = mesh_blinns.setdefault(r['mesh'], [])
                if r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            # One model with several materials: number each shader's suffix
            # (M_Crate_v01 / _v02 ... or _01 / _02 when shd_suf is empty).
            shader_seq = {}
            for blinns in mesh_blinns.values():
                if len(blinns) < 2:
                    continue
                for i, blinn in enumerate(blinns):
                    shader_seq.setdefault(blinn, i)

            for i, r in enumerate(self.rows):
                r['index'] = i
                first = first_of_mesh[r['mesh']] == i
                row_w = QtWidgets.QWidget()
                lay = QtWidgets.QHBoxLayout(row_w)
                lay.setContentsMargins(1, 1, 8, 8)
                lay.setSpacing(8)

                if first:
                    btn = QtWidgets.QPushButton(
                        u'取消选择' if r['selected'] else u'选择')
                    btn.setFixedWidth(50)
                    btn.clicked.connect(
                        lambda _=False, row=r: self.on_toggle_select(row))
                    lay.addWidget(btn)
                    self._select_buttons[r['mesh']] = btn

                    tri = LingJuGeometryTools._center_label(str(r['tris']))
                    tri.setFixedWidth(64)
                    lay.addWidget(tri)

                    combo = QtWidgets.QComboBox()
                    combo.addItems(r['uv_sets'])
                    if r['uv'] in r['uv_sets']:
                        combo.setCurrentIndex(r['uv_sets'].index(r['uv']))
                    combo.currentIndexChanged.connect(
                        lambda _=0, row=r, c=combo: self.on_uv_change(row, c))
                    combo.setFixedWidth(60)
                    lay.addWidget(combo)

                    mesh_edit = QtWidgets.QLineEdit(
                        r.get('mesh_edit')
                        or self._default_mesh_name(r['mesh'], tokens))
                    mesh_edit.editingFinished.connect(
                        lambda e=mesh_edit, row=r: self._store_mesh(e, row))
                    self._fields.append((mesh_edit, r, 'mesh_edit'))
                    mesh_edit.setMinimumWidth(90)
                    lay.addWidget(mesh_edit, 1)

                    dele = QtWidgets.QPushButton(u'删除')
                    dele.setFixedWidth(60)
                    dele.clicked.connect(
                        lambda _=False, row=r: self.on_delete_mesh(row))
                    lay.addWidget(dele)
                else:
                    sm = LingJuGeometryTools._center_label(u'同一 mesh')
                    sm.setFixedWidth(96)
                    lay.addWidget(sm)

                    dash = LingJuGeometryTools._center_label(u'—')
                    dash.setFixedWidth(50)
                    lay.addWidget(dash)

                    uv_lab = LingJuGeometryTools._center_label(r['uv'])
                    uv_lab.setFixedWidth(80)
                    lay.addWidget(uv_lab)

                    mesh_lab = LingJuGeometryTools._center_label(LingJuGeometryTools.strip_ns(r['mesh']))
                    mesh_lab.setMinimumWidth(80)
                    lay.addWidget(mesh_lab, 1)

                    dash_del = LingJuGeometryTools._center_label(u'—')
                    dash_del.setFixedWidth(60)
                    lay.addWidget(dash_del)

                # shader-scoped widgets on every row; insert between the mesh
                # column and the Delete column
                shd_edit = QtWidgets.QLineEdit(
                    r.get('shd_edit')
                    or self._default_shader_name(r['blinn'], tokens,
                                                 shader_seq.get(r['blinn'])))
                if users[r['blinn']] > 1:
                    shd_edit.setToolTip(
                        u'共享 · %d 个 mesh' % users[r['blinn']])
                shd_edit.editingFinished.connect(
                    lambda e=shd_edit, row=r: self._store_shader(e, row))
                self._fields.append((shd_edit, r, 'shd_edit'))
                shd_edit.setMinimumWidth(110)
                lay.insertWidget(4, shd_edit, 1)
                self._edits['shd'].setdefault(r['blinn'], []).append(shd_edit)

                color_edit = LingJuGeometryTools._MapDropLineEdit(
                    r['color_edit'] if 'color_edit' in r
                    else LingJuGeometryTools.short_name(LingJuGeometryTools.color_file_of(r['blinn'])))
                color_edit.setPlaceholderText(u'无')
                color_edit.setToolTip(
                    u'清空此字段将立即删除颜色 file 节点及其 place2dTexture；'
                    u'可拖入图片直接连接')
                color_edit.editingFinished.connect(
                    lambda e=color_edit, row=r: self._store_map(e, row, 'color'))
                color_edit.on_dropped = lambda fname, row=r: self._assign_dropped_map(
                    row, 'color', fname)
                self._fields.append((color_edit, r, 'color_edit'))
                color_edit.setMinimumWidth(110)
                lay.insertWidget(5, color_edit, 1)
                self._edits['color'].setdefault(r['blinn'], []).append(color_edit)

                normal_edit = LingJuGeometryTools._MapDropLineEdit(
                    r['normal_edit'] if 'normal_edit' in r
                    else LingJuGeometryTools.short_name(LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(r['blinn']))))
                normal_edit.setPlaceholderText(u'无')
                if not normal_edit.text():
                    normal_edit.setToolTip(
                        u'清空此字段将立即删除 bump2d 及法线 file 节点；'
                        u'可拖入图片直接连接')
                normal_edit.editingFinished.connect(
                    lambda e=normal_edit, row=r: self._store_map(
                        e, row, 'normal'))
                normal_edit.on_dropped = lambda fname, row=r: self._assign_dropped_map(
                    row, 'normal', fname)
                self._fields.append((normal_edit, r, 'normal_edit'))
                normal_edit.setMinimumWidth(110)
                lay.insertWidget(6, normal_edit, 1)
                self._edits['normal'].setdefault(r['blinn'], []).append(
                    normal_edit)

                self.data_layout.addWidget(row_w)
            self._update_select_all_btn()

        # ---------------- field storage (shared Blinn sync) ----------------

        def _sync_field_texts(self):
            """Pull the live text of every field widget into its row dict so
            actions pick up edits that were typed but never committed with
            Enter / focus loss."""
            for edit, row, key in self._fields:
                row[key] = edit.text()

        def _store_mesh(self, edit, row):
            row['mesh_edit'] = edit.text()

        def _store_shader(self, edit, row):
            text = edit.text()
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r['shd_edit'] = text
            for e in self._edits['shd'].get(row['blinn'], ()):
                if e is not edit:
                    e.blockSignals(True)
                    e.setText(text)
                    e.blockSignals(False)

        def _store_map(self, edit, row, kind):
            text = edit.text()
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r[kind + '_edit'] = text
            for e in self._edits[kind].get(row['blinn'], ()):
                if e is not edit:
                    e.blockSignals(True)
                    e.setText(text)
                    e.blockSignals(False)
            # Clearing the field deletes the connected node tree right away.
            if not text.strip():
                self._clear_map_nodes(row['blinn'], kind)

        def _clear_map_nodes(self, blinn, kind):
            """Delete the node tree behind a cleared color/normal field:
            file + place2dTexture (+ bump2d for normal). Shared Blinns are
            handled once; other rows referencing the same nodes refresh."""
            try:
                if kind == 'normal':
                    LingJuGeometryTools.disconnect_normal(blinn)
                else:
                    LingJuGeometryTools.disconnect_color(blinn)
            except Exception as exc:
                LingJuGeometryTools._warn(u'%s 清空失败（%s）: %s'
                    % (kind, LingJuGeometryTools.short_name(blinn), exc))
                return
            self._status(u'%s 贴图节点已从 %s 删除'
                        % (kind, LingJuGeometryTools.short_name(blinn)))

        # ---------------- per-cell actions ----------------

        def _engine_faces(self, engine, mesh):
            """Faces of mesh covered by one shading engine, as component
            suffixes (f[0:5]); whole=True when the object is assigned as-is.
            Members may come back short, shape-based or transform-based, so
            every one is normalized to long names before matching."""
            members = LingJuGeometryTools._safe(
                cmds.sets, engine, query=True) or []
            faces, whole = [], False
            for m in members:
                for long_m in (LingJuGeometryTools._safe(
                        cmds.ls, m, long=True) or []):
                    node, comp = (long_m.split('.', 1) + [''])[:2] \
                        if '.' in long_m else (long_m, '')
                    if node != mesh:
                        parents = LingJuGeometryTools._safe(
                            cmds.listRelatives, node, parent=True,
                            fullPath=True) or []
                        if not parents or parents[0] != mesh:
                            continue
                    if comp:
                        faces.append(comp)
                    else:
                        whole = True
            return faces, whole

        def _register_material_slots(self, mesh):
            """Remember which faces each material of a mesh was assigned to
            before deleting it, plus its color/normal input, so 更新场景 can
            rebuild the slots. A single material on the whole object needs no
            record. Returns the number of materials registered."""
            rows = [r for r in self.rows if r['mesh'] == mesh]
            if not rows:
                return 0
            shape = rows[0]['shape']
            engines = []
            for r in rows:
                if r['engine'] not in engines:
                    engines.append(r['engine'])
            if not engines:
                return 0
            total = len(LingJuGeometryTools._safe(
                cmds.ls, shape + '.f[*]', flatten=True) or [])
            slots = []
            covered = set()
            whole = False
            for engine in engines:
                faces, object_level = self._engine_faces(engine, mesh)
                whole = whole or object_level
                covered.update(faces)
                blinn = LingJuGeometryTools.shader_of_engine(engine)
                color = color_value = normal = None
                if blinn:
                    color, color_value, normal = \
                        LingJuGeometryTools.capture_material_maps(blinn)
                slots.append({'faces': faces, 'whole': object_level,
                            'color': color, 'color_value': color_value,
                            'normal': normal})
            # One material already covering the whole object: nothing to keep.
            if len(slots) == 1 and (whole or len(covered) >= total):
                return 0
            # covered holds range strings like 'f[0:2]': flatten to count the
            # actual faces the restore pass must put back.
            covered_count = len(LingJuGeometryTools._safe(cmds.ls,
                [shape + '.' + f for f in covered], flatten=True) or [])
            self._deleted_slots[shape] = {'slots': slots,
                                        'covered': covered_count}
            return len(slots)

        def _restore_material_slots(self, transform, shape, tokens, slots,
                                covered=0):
            """Rebuild one Blinn per recorded slot on the same faces. A spare
            material is created only when fewer faces came back than were
            recorded (covered): faces that sat in initialShadingGroup before
            the delete had no material and must not gain one now, otherwise
            every delete/refresh cycle grows one extra Blinn."""
            base = LingJuGeometryTools._add_token(tokens['shd_suf'])
            color_on = self.restore_color_cb.isChecked()
            normal_on = self.restore_normal_cb.isChecked()
            created = []

            def make(index):
                blinn = LingJuGeometryTools.create_blinn(
                    LingJuGeometryTools._add_token(tokens['shd_pre'])
                    + LingJuGeometryTools.mesh_stem(transform, tokens)
                    + LingJuGeometryTools.suffix_sequence(base, index))
                return blinn, LingJuGeometryTools.create_engine_for(blinn)

            used = set()
            whole = False
            for i, slot in enumerate(slots):
                blinn, engine = make(i)
                if slot.get('whole'):
                    members = [shape]
                    whole = True
                else:
                    members = [shape + '.' + c
                            for c in slot.get('faces') or []]
                # objExists is always False for components (a face is not a
                # node); ls() resolves them. With objExists every face-level
                # slot silently failed to restore and left an orphan Blinn.
                members = [m for m in members
                        if LingJuGeometryTools._safe(cmds.ls, m)]
                if members:
                    try:
                        cmds.sets(members, edit=True, forceElement=engine)
                        # Read the members back flattened: 'f[0:2]' must count
                        # as f[0], f[1] or the spare-material check below
                        # always thinks nothing was restored.
                        got = LingJuGeometryTools._safe(
                            cmds.sets, engine, query=True) or []
                        for g in got:
                            for f in (LingJuGeometryTools._safe(
                                    cmds.ls, g, flatten=True) or [g]):
                                if '.f[' in f:
                                    used.add(f.rsplit('.', 1)[-1])
                    except Exception as exc:
                        LingJuGeometryTools._warn(
                            u'%s 材质位置恢复失败: %s'
                            % (LingJuGeometryTools.short_name(transform), exc))
                elif i == 0 and len(slots) == 1:
                    LingJuGeometryTools.assign_engine(shape, engine)
                    whole = True
                LingJuGeometryTools.restore_material_maps(
                    blinn, slot, color_on, normal_on,
                    connect_alpha=self._alpha_checked())
                created.append(engine)
            if not whole and len(used) < covered:
                rest = [shape + '.' + f.rsplit('.', 1)[-1]
                        for f in (LingJuGeometryTools._safe(
                            cmds.ls, shape + '.f[*]', flatten=True) or [])
                        if f.rsplit('.', 1)[-1] not in used]
                if rest:
                    _, engine = make(len(slots))
                    try:
                        cmds.sets(rest, edit=True, forceElement=engine)
                    except Exception:
                        pass
                    created.append(engine)
            return created

        def _prune_deleted_slots(self):
            for shape in list(self._deleted_slots):
                if not cmds.objExists(shape):
                    self._deleted_slots.pop(shape, None)

        def _update_select_all_btn(self):
            all_sel = bool(self.rows) and all(r['selected'] for r in self.rows)
            self.select_all_btn.setText(
                u'取消全选' if all_sel else u'全选')

        def on_toggle_select_all(self):
            if not self.rows:
                return
            target = not all(r['selected'] for r in self.rows)
            try:
                meshes = [r['mesh'] for r in self.rows
                        if r['selected'] != target]
                if meshes:
                    if target:
                        cmds.select(meshes, add=True)
                    else:
                        cmds.select(meshes, deselect=True)
            except Exception as exc:
                LingJuGeometryTools._warn(u'全选失败: %s' % exc)
                return
            for r in self.rows:
                r['selected'] = target
            for btn in self._select_buttons.values():
                btn.setText(u'取消选择' if target else u'选择')
            self._update_select_all_btn()

        def on_delete_all(self):
            """Delete every material in the list (Blinns + file / bump trees),
            after a confirmation prompt. Meshes themselves are not deleted."""
            blinns = []
            for r in self.rows:
                if r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            if not blinns:
                self._status(u'没有可删除的材质')
                return
            ret = QtWidgets.QMessageBox.question(
                self, u'删除全部材质',
                u'确定删除全部 %d 个材质？\n'
                u'每个 Blinn 及其 file / bump2d 节点都会被删除；\n'
                u'mesh 保留在场景中。' % len(blinns),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ret != QtWidgets.QMessageBox.Yes:
                return
            slots = 0
            for mesh in set(r['mesh'] for r in self.rows):
                slots += self._register_material_slots(mesh)
            with LingJuGeometryTools._undo_chunk('DeleteAllShaders'):
                seen = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in seen:
                        continue
                    seen.add(b)
                    LingJuGeometryTools.delete_shader_tree(b, r['mesh'])
                unused = LingJuGeometryTools.delete_unused_nodes()
            self.rows = []
            self._populate_rows()
            self._status(u'已删除 %d 个材质 · 清理 %d 个未用节点%s'
                        % (len(seen), unused,
                            (u' · 已登记 %d 个材质位置' % slots) if slots else ''))

        def on_toggle_select(self, row):
            try:
                if row['selected']:
                    cmds.select(row['mesh'], deselect=True)
                    row['selected'] = False
                else:
                    cmds.select(row['mesh'], add=True)
                    row['selected'] = True
            except Exception as exc:
                LingJuGeometryTools._warn(u'选中失败: %s' % exc)
                return
            btn = self._select_buttons.get(row['mesh'])
            if btn:
                btn.setText(u'取消选中' if row['selected'] else u'选中')
            self._update_select_all_btn()

        def on_uv_change(self, row, combo):
            uv = combo.currentText()
            try:
                cmds.polyUVSet(row['shape'], edit=True, currentUVSet=True,
                            uvSet=uv)
                row['uv'] = uv
            except Exception as exc:
                LingJuGeometryTools._warn(u'UV 集切换失败: %s' % exc)

        def on_refresh_materials(self):
            # 等同重开窗口：只读重读场景（面数 / 材质赋予），并刷新材质球下拉
            self.refresh_scene(mutate=False)
            m2m_scene_refresh_material_combo(self.material_combo)
            # 透明通道跟着勾选状态走（全场景已有的颜色贴图，与贴图文件夹无关）
            self._sync_transparency(self._alpha_checked())
            # 全场景：材质上的贴图若因场景挪动而失效（绝对路径找不到文件），
            # 在贴图文件夹里按同名文件找，找到就改路径，找不到不动。
            result = self._relink_missing_textures()
            if result is None:
                self._status(u'未设置贴图文件夹或文件夹中没有贴图: %s'
                            % self.path_edit.text().strip())
                return
            relinked, missing = result
            if relinked:
                # 重新读取行数据，让颜色 / 法线字段显示新路径
                self.refresh_scene(mutate=False)
            text = self.status.text()
            if relinked:
                text += u' · 修复丢失贴图 %d 个' % relinked
            if missing:
                shown = u'、'.join(missing[:3])
                if len(missing) > 3:
                    shown += u' 等'
                text += u' · 未找到 %d 个: %s' % (len(missing), shown)
            if not relinked and not missing:
                text += u' · 贴图路径全部有效'
            self._status(text)

        def _sync_transparency(self, enabled):
            """Apply 颜色贴图连 Transparency to the whole scene: every material
            that has a color file node gets that one link connected (checked)
            or cut (unchecked). Other transparency inputs are left alone."""
            done = set()
            with LingJuGeometryTools._undo_chunk('SyncTransparency'):
                for blinn in cmds.ls(materials=True) or []:
                    if blinn in done:
                        continue
                    done.add(blinn)
                    LingJuGeometryTools.set_transparency_link(
                        blinn, LingJuGeometryTools.color_file_of(blinn), enabled)

        def _relink_missing_textures(self):
            """Repair texture paths broken by moving the scene: for every
            color / normal file node in the scene whose fileTextureName no
            longer resolves, look for a file with the same name under the
            texture folder. Found -> only fileTextureName changes, nodes and
            links stay; not found -> left untouched.
            Returns (relinked, missing_names); None when the folder is empty."""
            folder = self.path_edit.text().strip()
            if not folder or not os.path.isdir(folder):
                return None
            found = {}
            for root, dirs, names in os.walk(folder):
                dirs.sort()
                for name in sorted(names):
                    if os.path.splitext(name)[1].lower() in LingJuGeometryTools.TEX_EXTS:
                        found.setdefault(name.lower(), os.path.join(root, name))
            if not found:
                return None
            project_root = LingJuGeometryTools._safe(
                cmds.workspace, query=True, rootDirectory=True)
            relinked = 0
            missing = []
            done = set()
            with LingJuGeometryTools._undo_chunk('RelinkMissingTextures'):
                for blinn in cmds.ls(materials=True) or []:
                    if blinn in done:
                        continue
                    done.add(blinn)
                    for fnode in (LingJuGeometryTools.color_file_of(blinn),
                                  LingJuGeometryTools.bump_file_of(
                                      LingJuGeometryTools.bump_of(blinn))):
                        if not fnode:
                            continue
                        current = LingJuGeometryTools._safe(
                            cmds.getAttr, fnode + '.fileTextureName') or ''
                        # <UDIM>/<f> 这类序列路径无法用 isfile 判断，跳过
                        if not current or '<' in current:
                            continue
                        if os.path.isfile(current):
                            continue
                        if not os.path.isabs(current) and project_root and \
                                os.path.isfile(os.path.join(project_root, current)):
                            continue
                        name = os.path.basename(current.replace('\\', '/'))
                        path = found.get(name.lower())
                        if not path:
                            if name not in missing:
                                missing.append(name)
                            continue
                        try:
                            LingJuGeometryTools._set_file_path(fnode, path)
                        except Exception as exc:
                            LingJuGeometryTools._warn(
                                u'%s 贴图路径修复失败: %s'
                                % (LingJuGeometryTools.short_name(fnode), exc))
                            continue
                        relinked += 1
            return relinked, missing

        def on_assign_material(self):
            shader = self.material_combo.currentText()
            if not shader:
                self._status(u'请先选择材质球')
                return
            try:
                with LingJuGeometryTools._undo_chunk('AssignMaterial'):
                    kind, count = m2m_scene_assign_material_selection(shader)
            except Exception as exc:
                LingJuGeometryTools._warn(u'赋予材质失败: %s' % exc)
                return
            if kind is None:
                self._status(u'请先选择物件或面')
            elif kind == 'faces':
                self._status(u'%d 个面已赋予 %s' % (count, shader))
            else:
                self._status(u'%d 个物件已赋予 %s' % (count, shader))

        def on_create_test_plane(self):
            """生成多材质材质平面：面数 = 材质数，逐面赋予一个默认命名材质。"""
            mat_type = self.mat_type.currentText()
            count = self.mat_count.value()
            try:
                with LingJuGeometryTools._undo_chunk('MaterialTestPlane'):
                    plane = cmds.polyPlane(subdivisionsX=count,
                                           subdivisionsY=1)[0]
                    shape = (cmds.listRelatives(plane, shapes=True,
                                                fullPath=True) or [plane])[0]
                    faces = cmds.ls(shape + '.f[*]', flatten=True) or []
                    for face in faces:
                        shader = cmds.shadingNode(mat_type, asShader=True)
                        group = m2m_scene_ensure_shading_group(shader)
                        cmds.sets(face, edit=True, forceElement=group)
            except Exception as exc:
                LingJuGeometryTools._warn(u'生成材质平面失败: %s' % exc)
                return
            cmds.select(plane, r=True)
            self.on_refresh_materials()
            self._status(u'已生成材质平面：%d 个面 / %d 个 %s'
                         % (len(faces), len(faces), mat_type))

        def on_rename_uv_sets(self):
            """Rename the current UV set. With objects selected in the scene
            only the selected meshes are processed; with an empty selection
            every listed mesh is renamed."""
            new = self.uv_batch.text().strip()
            if not new:
                return
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            changed = 0
            with LingJuGeometryTools._undo_chunk('RenameUVSets'):
                for mesh in sorted(set(r['mesh'] for r in target)):
                    row = next(r for r in target if r['mesh'] == mesh)
                    old = row['uv']
                    if old == new:
                        continue
                    try:
                        cmds.polyUVSet(row['shape'], edit=True, rename=True,
                                    uvSet=old, newUVSet=new)
                        changed += 1
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的 UV 集重命名失败: %s'
                            % (LingJuGeometryTools.short_name(mesh), exc))
            self.refresh_scene()
            self._status(u'已将 %d 个 mesh%s 的当前 UV 集重命名为 "%s"'
                        % (changed,
                            u'（仅选中）' if scoped else '', new))

        def on_delete_uv_sets(self):
            """Delete the current UV set of each mesh in scope. A global delete
            (empty selection) asks for confirmation first; with a selection the
            delete runs immediately. Meshes with a single UV set are skipped —
            Maya forbids deleting the last one."""
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            if not scoped:
                meshes = set(r['mesh'] for r in target)
                ret = QtWidgets.QMessageBox.question(
                    self, u'删除 UV 集',
                    u'确定删除全部 %d 个列表 mesh 的当前 UV 集？\n'
                    u'只有一个 UV 集的 mesh 会被跳过。' % len(meshes),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if ret != QtWidgets.QMessageBox.Yes:
                    return
            deleted = skipped = 0
            with LingJuGeometryTools._undo_chunk('DeleteUVSets'):
                for mesh in sorted(set(r['mesh'] for r in target)):
                    row = next(r for r in target if r['mesh'] == mesh)
                    if len(row['uv_sets']) < 2:
                        skipped += 1
                        continue
                    try:
                        cmds.polyUVSet(row['shape'], edit=True, delete=True,
                                    uvSet=row['uv'])
                        deleted += 1
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的 UV 集删除失败: %s'
                            % (LingJuGeometryTools.short_name(mesh), exc))
                        skipped += 1
            self.refresh_scene()
            self._status(u'已删除 %d 个 mesh%s 的当前 UV 集 · 跳过 %d 个'
                        % (deleted, u'（仅选中）' if scoped else '',
                            skipped))


        # ---------------- main actions ----------------

        def _browse_folder(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, u'选择贴图文件夹')
            if folder:
                self.path_edit.setText(folder)
                self._update_format_hint()

        def _update_format_hint(self):
            """Show which supported texture extensions exist in the folder next
            to the Browse button; empty folder / bad path says so instead."""
            folder = self.path_edit.text().strip()
            found = set()
            if folder and os.path.isdir(folder):
                for f in os.listdir(folder):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in LingJuGeometryTools.TEX_EXTS:
                        found.add(ext.lstrip('.'))
            if found:
                order = [e.lstrip('.') for e in LingJuGeometryTools.TEX_EXTS]
                text = u'格式: ' + ' '.join(e for e in order if e in found)
            elif folder and os.path.isdir(folder):
                text = u'格式: 无贴图'
            else:
                text = u'格式: 文件夹不存在'
            self.fmt_hint.setText(text)

        def _folder_files(self):
            folder = self.path_edit.text().strip()
            if not folder or not os.path.isdir(folder):
                return []
            return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                    if os.path.splitext(f)[1].lower() in LingJuGeometryTools.TEX_EXTS]

        def _resolve_texture(self, name):
            """Turn a typed name into a texture path: an absolute path, a file in
            the texture folder (with or without extension), or an existing scene
            file node with that name."""
            name = name.strip()
            if not name:
                return None
            if os.path.isfile(name):
                return name
            folder = self.path_edit.text().strip()
            if folder and os.path.isdir(folder):
                for fn in sorted(os.listdir(folder)):
                    base, ext = os.path.splitext(fn)
                    if ext.lower() in LingJuGeometryTools.TEX_EXTS and base == name:
                        return os.path.join(folder, fn)
                direct = os.path.join(folder, name)
                if os.path.isfile(direct):
                    return direct
            if cmds.objExists(name) and cmds.nodeType(name) == 'file':
                return cmds.getAttr(name + '.fileTextureName') or None
            return None

        def _commit_map_fields(self, rows=None, kinds=None):
            """Commit pending color/normal field edits. Clearing a field deletes
            the connected nodes: file + place2dTexture for color, plus bump2d for
            normal. Shared Blinns are committed once. rows limits the commit to a
            subset of self.rows (selection-scoped Apply names); None means every
            row. kinds limits which map types are committed (default both)."""
            if kinds is None:
                kinds = ('normal', 'color')
            set_count = disc_count = 0
            alpha_on = self._alpha_checked()
            done = set()
            for r in (self.rows if rows is None else rows):
                b = r['blinn']
                if b in done:
                    continue
                done.add(b)
                for kind in kinds:
                    key = kind + '_edit'
                    if key not in r:
                        continue
                    want = r[key].strip()
                    if kind == 'color':
                        fnode = LingJuGeometryTools.color_file_of(b)
                    else:
                        fnode = LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(b))
                    cur = LingJuGeometryTools.short_name(fnode)
                    if want == '':
                        if cur:
                            try:
                                if kind == 'normal':
                                    LingJuGeometryTools.disconnect_normal(b)
                                else:
                                    LingJuGeometryTools.disconnect_color(b)
                            except Exception as exc:
                                LingJuGeometryTools._warn(u'%s 清空失败（%s）: %s'
                                    % (kind, LingJuGeometryTools.short_name(b), exc))
                                continue
                            disc_count += 1
                        continue
                    want_node = LingJuGeometryTools._node_safe_name(want)
                    path = self._resolve_texture(want)
                    if not path:
                        if want_node != cur:
                            LingJuGeometryTools._warn(u'%s「%s」在文件夹或场景中未找到'
                                % (kind, want))
                        continue
                    if (want_node == cur and fnode and LingJuGeometryTools._same_path(
                            path, LingJuGeometryTools._safe(cmds.getAttr,
                                        fnode + '.fileTextureName'))):
                        if kind == 'color':
                            # field untouched, but the checkbox still applies
                            LingJuGeometryTools.set_transparency_link(
                                b, fnode, alpha_on)
                        continue  # same node name and same file: nothing to do
                    try:
                        if kind == 'color':
                            LingJuGeometryTools.set_color_file(
                                b, path, want, connect_alpha=alpha_on)
                        else:
                            LingJuGeometryTools.set_normal_file(b, path, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s「%s」应用到 %s 失败: %s'
                            % (kind, want, LingJuGeometryTools.short_name(b), exc))
                        continue
                    set_count += 1
            return set_count, disc_count

        def _scoped_rows(self):
            """Rows a batch action should touch under the current selection:
            the rows of the selected meshes when anything is selected, every
            row when the selection is empty. Returns (rows, selection_active);
            rows is empty when the selected objects are not in the list."""
            sel = set(cmds.ls(selection=True, long=True, type='transform') or [])
            for node in cmds.ls(selection=True, long=True, type='shape') or []:
                sel.update(LingJuGeometryTools._safe(cmds.listRelatives, node, parent=True,
                                fullPath=True) or [])
            if not sel:
                return list(self.rows), False
            return [r for r in self.rows if r['mesh'] in sel], True

        def on_apply_names(self):
            """Apply pending names. With objects selected in the scene only the
            rows of the selected meshes are processed; with an empty selection
            the rename runs on every row."""
            self._sync_field_texts()
            tokens = self._tokens()
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            mesh_n = shd_n = 0
            with LingJuGeometryTools._undo_chunk('ApplyNames'):
                # 1) mesh renames, one per transform
                seen = set()
                for r in target:
                    mesh = r['mesh']
                    if mesh in seen:
                        continue
                    seen.add(mesh)
                    want = LingJuGeometryTools.strip_ns(r.get('mesh_edit') or '')
                    if not want or want == LingJuGeometryTools.short_name(mesh):
                        continue
                    try:
                        new = cmds.rename(mesh, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'重命名 %s 失败: %s' % (LingJuGeometryTools.short_name(mesh), exc))
                        continue
                    mesh_n += 1
                    for row in self.rows:
                        if row['mesh'] == mesh:
                            row['mesh'] = new
                            shapes = LingJuGeometryTools._safe(cmds.listRelatives, new,
                                        shapes=True, fullPath=True) or []
                            if shapes:
                                row['shape'] = shapes[0]
                # 2) shader renames, one per Blinn.
                #    Unless the user typed a custom name in the shader field,
                #    regenerate the material name from the (possibly just
                #    renamed) mesh stem + tokens so materials follow the mesh.
                #    Shared blinns (used by several meshes) are excluded from
                #    multi-material sequence numbering — they keep the single-
                #    material name because they have no unique owner mesh.
                blinn_to_meshes = {}
                for r in target:
                    blinn_to_meshes.setdefault(r['blinn'], set()).add(r['mesh'])
                shared_blinns = {b for b, meshes in blinn_to_meshes.items()
                                 if len(meshes) > 1}

                mesh_blinns = {}
                for r in target:
                    blinns = mesh_blinns.setdefault(r['mesh'], [])
                    if r['blinn'] not in blinns:
                        blinns.append(r['blinn'])
                blinn_seq = {}
                for blinns in mesh_blinns.values():
                    if len(blinns) < 2:
                        continue
                    for i, b in enumerate(blinns):
                        if b in shared_blinns:
                            continue  # shared: no sequence number
                        blinn_seq.setdefault(b, i)
                shd_pre = LingJuGeometryTools._add_token(tokens['shd_pre'])
                shd_suf = LingJuGeometryTools._add_token(tokens['shd_suf'])
                no_suffix_numbering = not shd_suf
                seen = set()
                for r in target:
                    b = r['blinn']
                    if b in seen:
                        continue
                    seen.add(b)
                    user_text = LingJuGeometryTools.strip_ns(r.get('shd_edit') or '')
                    seq = blinn_seq.get(b)
                    old_default = self._default_shader_name(b, tokens, seq)
                    if user_text and user_text != old_default:
                        want = user_text
                    elif b in shared_blinns or no_suffix_numbering:
                        # Shared blinn has no unique owner mesh, so stem
                        # comes from the blinn's own name. Same when the user
                        # left shd_suf blank — we keep whatever the scene
                        # already had (including trailing digits like 01,
                        # _A, ...) and only re-apply the shd_pre token.
                        stem = LingJuGeometryTools.shader_stem(b, tokens)
                        want = shd_pre + stem + shd_suf
                    elif seq is None:
                        stem = LingJuGeometryTools.mesh_stem(r['mesh'], tokens)
                        want = shd_pre + stem + shd_suf
                    else:
                        stem = LingJuGeometryTools.mesh_stem(r['mesh'], tokens)
                        want = (shd_pre + stem
                                + LingJuGeometryTools.suffix_sequence(shd_suf, seq))
                    if not want or want == LingJuGeometryTools.short_name(b):
                        continue
                    try:
                        new = LingJuGeometryTools._take_name(b, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'重命名 %s 失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    shd_n += 1
                    for row in self.rows:
                        if row['blinn'] == b:
                            row['blinn'] = new
                # 3) color / normal field commits
                map_n, disc_n = self._commit_map_fields(rows=target)
            self.refresh_scene()
            self._status(u'已应用%s: mesh 重命名 %d 个 · Blinn 重命名 %d 个 · '
                        u'贴图更新 %d 个 · 移除贴图节点树 %d 个'
                        % (u'（仅选中）' if scoped else '',
                            mesh_n, shd_n, map_n, disc_n))

        def on_match_shaders(self):
            """Rename materials to match their mesh: shd_pre + mesh stem
            (mesh_pre/mesh_suf stripped) + shd_suf.

            A mesh with several materials gets numbered: suffix_sequence on
            shd_suf when the suffix token is set, plain _01/_02 otherwise.
            A blinn shared by several meshes is named after the first mesh
            that uses it (row order) and is skipped for the later meshes.
            With objects selected in the scene only their rows are renamed;
            with an empty selection every row is processed. Mesh names and
            manually typed shader fields are left untouched."""
            tokens = self._tokens()
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            shd_pre = LingJuGeometryTools._add_token(tokens['shd_pre'])
            shd_suf = LingJuGeometryTools._add_token(tokens['shd_suf'])
            # Per-mesh material lists, in row order, no duplicates.
            mesh_order = []
            mesh_blinns = {}
            for r in target:
                if r['mesh'] not in mesh_blinns:
                    mesh_order.append(r['mesh'])
                    mesh_blinns[r['mesh']] = []
                if r['blinn'] not in mesh_blinns[r['mesh']]:
                    mesh_blinns[r['mesh']].append(r['blinn'])
            renamed = {}
            n = 0
            with LingJuGeometryTools._undo_chunk('MatchShaders'):
                for mesh in mesh_order:
                    stem = LingJuGeometryTools.mesh_stem(mesh, tokens)
                    # Blinns already named through an earlier (shared) mesh do
                    # not take part in this mesh's numbering.
                    own = [b for b in mesh_blinns[mesh] if b not in renamed]
                    for i, b in enumerate(own):
                        if len(own) == 1:
                            want = shd_pre + stem + shd_suf
                        elif shd_suf:
                            want = (shd_pre + stem
                                    + LingJuGeometryTools.suffix_sequence(shd_suf, i))
                        else:
                            want = shd_pre + stem + '_%02d' % (i + 1)
                        if not want or want == LingJuGeometryTools.short_name(b):
                            renamed[b] = b
                            continue
                        try:
                            renamed[b] = LingJuGeometryTools._take_name(b, want)
                        except Exception as exc:
                            LingJuGeometryTools._warn(
                                u'重命名 %s 失败: %s'
                                % (LingJuGeometryTools.short_name(b), exc))
                            continue
                        n += 1
            # Read-only rebuild: rows pick up the new names from the scene.
            self.refresh_scene(mutate=False)
            self._status(u'材质匹配模型%s: 重命名 %d 个材质'
                        % (u'（仅选中）' if scoped else '', n))

        def on_assign_normals(self):
            files = self._folder_files()
            if not files:
                self._status(u'文件夹中未找到贴图: %s'
                            % self.path_edit.text().strip())
                return
            tokens = self._tokens()
            self._sync_field_texts()
            assigned = 0
            with LingJuGeometryTools._undo_chunk('AssignNormals'):
                # commit pending normal edits first (empty field -> disconnect)
                map_n, disc_n = self._commit_map_fields(kinds=('normal',))
                # auto-match: file stem equals mesh/shader stem + normal suffix
                done = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in done:
                        continue
                    done.add(b)
                    bump = LingJuGeometryTools.bump_of(b)
                    if bump and LingJuGeometryTools._safe(cmds.listConnections,
                                    bump + '.bumpValue',
                                    source=True, destination=False):
                        continue  # already has a normal / bump input
                    stems = LingJuGeometryTools.shader_match_stems(
                        b, r['mesh'], tokens)
                    path = LingJuGeometryTools.find_texture_for(stems, tokens, files, 'nml_suf')
                    if not path:
                        continue
                    stem = os.path.splitext(os.path.basename(path))[0]
                    try:
                        LingJuGeometryTools.set_normal_file(b, path, stem)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的法线指定失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    assigned += 1
            self.refresh_scene()
            self._status(u'指定法线: 自动匹配 %d 个 · 手动更新 %d 个 · '
                        u'移除贴图节点树 %d 个' % (assigned, map_n, disc_n))

        def on_assign_colors(self):
            """Auto-match color maps from the texture folder: a file whose stem
            (minus the texture prefix and the color suffix) equals the mesh or
            Blinn stem is connected to Blinns that have no color input yet."""
            files = self._folder_files()
            if not files:
                self._status(u'文件夹中未找到贴图: %s'
                            % self.path_edit.text().strip())
                return
            tokens = self._tokens()
            self._sync_field_texts()
            alpha_on = self._alpha_checked()
            assigned = 0
            with LingJuGeometryTools._undo_chunk('AssignColors'):
                # commit pending color edits first (empty field -> disconnect)
                map_n, disc_n = self._commit_map_fields(kinds=('color',))
                # auto-match: file stem minus texture prefix + color suffix
                done = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in done:
                        continue
                    done.add(b)
                    if LingJuGeometryTools._safe(cmds.listConnections, b + '.color',
                            source=True, destination=False):
                        # already has a color input: still apply the checkbox
                        # to its existing file node (connect or cut the link)
                        LingJuGeometryTools.set_transparency_link(
                            b, LingJuGeometryTools.color_file_of(b), alpha_on)
                        continue
                    stems = LingJuGeometryTools.shader_match_stems(
                        b, r['mesh'], tokens)
                    path = LingJuGeometryTools.find_texture_for(stems, tokens, files, 'col_suf')
                    if not path:
                        continue
                    stem = os.path.splitext(os.path.basename(path))[0]
                    try:
                        LingJuGeometryTools.set_color_file(
                            b, path, stem, connect_alpha=alpha_on)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的颜色贴图指定失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    assigned += 1
            self.refresh_scene()
            self._status(u'指定颜色贴图: 自动匹配 %d 个 · 手动更新 %d 个 · '
                        u'移除贴图节点树 %d 个' % (assigned, map_n, disc_n))

        def on_delete_mesh(self, row):
            self._sync_field_texts()
            mesh = row['mesh']
            slots = self._register_material_slots(mesh)
            others = [r for r in self.rows if r['mesh'] != mesh]
            blinns = []
            for r in self.rows:
                if r['mesh'] == mesh and r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            with LingJuGeometryTools._undo_chunk('DeleteMeshShaders'):
                for b in blinns:
                    if any(r['blinn'] == b for r in others):
                        continue  # shared Blinn still used by another mesh
                    LingJuGeometryTools.delete_shader_tree(b, mesh)
                unused = LingJuGeometryTools.delete_unused_nodes()
            self.rows = [r for r in self.rows if r['mesh'] != mesh]
            self._populate_rows()
            self._status(u'已删除 %s 的材质 · 清理 %d 个未用节点%s'
                        % (LingJuGeometryTools.short_name(mesh), unused,
                            (u' · 已登记 %d 个材质位置' % slots) if slots else ''))

        def on_clear_namespaces(self):
            n = LingJuGeometryTools.clear_namespaces()
            self.refresh_scene()
            self._status(u'已移除 %d 个命名空间' % n)

        def on_delete_unused(self):
            """Whole-scene Delete Unused Nodes pass (same as the Hypershade
            button), undoable as one step."""
            with LingJuGeometryTools._undo_chunk('DeleteUnusedNodes'):
                removed = LingJuGeometryTools.delete_unused_nodes()
            self._status(u'已删除 %d 个未用节点' % removed)

        def _assign_dropped_map(self, row, kind, filename):
            """Connect a dragged or selected image to the row's Blinn right
            away (kind is 'color' or 'normal'), then sync every field of the
            Blinn so shared rows show the new node name."""
            folder = self.path_edit.text().strip()
            path = filename if os.path.isabs(filename) else os.path.join(
                folder, filename)
            if not os.path.isfile(path):
                found = [f for f in self._folder_files()
                        if os.path.basename(f) == filename]
                if not found:
                    LingJuGeometryTools._warn(u'文件未找到: %s' % filename)
                    return
                path = found[0]
            stem = os.path.splitext(os.path.basename(path))[0]
            with LingJuGeometryTools._undo_chunk('AssignMap'):
                if kind == 'color':
                    LingJuGeometryTools.set_color_file(
                        row['blinn'], path, stem,
                        connect_alpha=self._alpha_checked())
                else:
                    LingJuGeometryTools.set_normal_file(row['blinn'], path, stem)
            if kind == 'color':
                fn = LingJuGeometryTools.color_file_of(row['blinn'])
            else:
                fn = LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(row['blinn']))
            shown = LingJuGeometryTools.short_name(fn) if fn else stem
            key = kind + '_edit'
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r.pop(key, None)
                    for e in self._edits[kind].get(r['blinn'], ()):
                        e.blockSignals(True)
                        e.setText(shown)
                        e.blockSignals(False)
            label = u'颜色贴图' if kind == 'color' else u'法线贴图'
            self._status(u'%s %s → %s' % (label, shown,
                                        LingJuGeometryTools.short_name(row['blinn'])))


    # ---------------------------------------------------------------------------
    # entry

    def _maya_main_window():
        try:
            from shiboken2 import wrapInstance
        except ImportError:
            try:
                from shiboken6 import wrapInstance
            except ImportError:
                return None
        try:
            from maya.OpenMayaUI import MQtUtil
            ptr = MQtUtil.mainWindow()
            if ptr:
                return wrapInstance(int(ptr), QtWidgets.QWidget)
        except Exception:
            pass
        return None


    def show_naming_window(self, *args):
        """Open the Polygon Asset Manager window."""
        # 类级别变量 LingJuGeometryTools._WINDOW 不能用 global 访问，必须显式引用类名
        if LingJuGeometryTools._WINDOW is not None:
            try:
                LingJuGeometryTools._WINDOW.close()
                LingJuGeometryTools._WINDOW.deleteLater()
            except Exception:
                pass
            LingJuGeometryTools._WINDOW = None
        LingJuGeometryTools._WINDOW = LingJuGeometryTools.AssetManagerUI(LingJuGeometryTools._maya_main_window())
        LingJuGeometryTools._WINDOW.show()
        LingJuGeometryTools._WINDOW.raise_()
        LingJuGeometryTools._WINDOW.activateWindow()
        return LingJuGeometryTools._WINDOW

    # --------------------- LingJuGeometryTools.test
    def test(self, *args):
        lj_info(u"测试内容！")

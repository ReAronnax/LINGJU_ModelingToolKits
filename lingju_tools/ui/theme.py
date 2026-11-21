# coding: utf-8
"""统一视觉主题：颜色、尺寸、间距与 Qt 样式表。

界面里所有控件都通过 `ui.components` 创建，调样式只改这里：
- 颜色：Maya 原生控件用 0-1 的 RGB 元组，Qt 样式表用 #RRGGBB
- 尺寸：宽度 / 高度 / 行距 / 内边距统一取常量
"""

# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------
GREY = (0.310, 0.320, 0.350)        # 主按钮 / 强调块
GREY_BLUE = (0.198, 0.218, 0.238)   # 折叠栏底色
INNER_GREY = (0.258, 0.278, 0.298)  # 折叠栏内按钮

INFO_HEX = 0x4F5259
WARN_HEX = 0xA66F0F
ERROR_HEX = 0x811A1A

BG_HEX = "#2a2c30"
PANEL_HEX = "#313439"
CONTROL_HEX = "#3a3d42"
CONTROL_HOVER_HEX = "#454952"
BORDER_HEX = "#4a4e56"
TEXT_HEX = "#c9ced6"
ACCENT_HEX = "#5aa0dc"

# ---------------------------------------------------------------------------
# 尺寸与间距
# ---------------------------------------------------------------------------
WINDOW_MIN_WIDTH = 325   # 主窗口最小宽度（纵向滚动条在默认宽度即可见）
SECTION_WIDTH = 299      # 折叠栏宽度
BUTTON_WIDTH = 300       # 整行按钮
BUTTON_WIDTH_WIDE = 275  # 折叠栏内整行按钮
BUTTON_WIDTH_HALF = 136  # 两列按钮
BUTTON_HEIGHT = 28
FIELD_HEIGHT = 24        # 数值框 / 下拉框
CHECK_HEIGHT = 22
SLIDER_HEIGHT = 26
SCROLLBAR_THICKNESS = 13
ROW_SPACING = 2
SECTION_MARGIN_WIDTH = 10
SECTION_MARGIN_HEIGHT = 5

# ---------------------------------------------------------------------------
# Qt 样式表：选项卡、滚动条、提示框
# ---------------------------------------------------------------------------
QSS = """
QWidget#LJGT_TabWidget {
    background: %(bg)s;
}
QTabWidget#LJGT_TabWidget::pane {
    border: none;
    background: %(bg)s;
}
QTabBar::tab {
    background: %(control)s;
    color: %(text)s;
    padding: 6px 12px;
    margin: 2px 2px 0px 0px;
    border: 1px solid %(border)s;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
QTabBar::tab:hover {
    background: %(hover)s;
}
QTabBar::tab:selected {
    background: %(hover)s;
    color: #ffffff;
    border-bottom: 2px solid %(accent)s;
}
QScrollBar:vertical {
    background: %(bg)s;
    width: 12px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: %(border)s;
    min-height: 24px;
    border-radius: 5px;
    margin: 2px;
}
QScrollBar::handle:vertical:hover {
    background: %(hover)s;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}
QToolTip {
    background: %(panel)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    padding: 4px;
}
""" % {
    "bg": BG_HEX,
    "panel": PANEL_HEX,
    "control": CONTROL_HEX,
    "hover": CONTROL_HOVER_HEX,
    "border": BORDER_HEX,
    "text": TEXT_HEX,
    "accent": ACCENT_HEX,
}


def apply_theme(widget):
    """给 Qt 控件及其所有子控件应用统一样式表。

    :param widget: QWidget（主窗口根控件或任一容器）
    """
    if widget is None:
        return
    try:
        widget.setStyleSheet(QSS)
    except Exception:
        # 非 Qt 控件或已被销毁时静默跳过，不影响功能
        pass

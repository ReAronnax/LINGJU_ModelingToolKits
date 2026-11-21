# coding: utf-8
"""统一 UI 组件工厂：按钮、折叠栏、文本框、下拉、滑块、进度条、滚动页。

所有界面控件都从这里创建，默认尺寸与配色来自 `ui.theme`：
调整界面风格只改主题常量，不用逐个控件翻代码。
每个函数都是 maya.cmds 的薄包装：创建时补默认值，edit / query 调用原样转发。
"""

import maya.cmds as cmds

from . import theme


def _is_edit_or_query(kwargs):
    """判断这次调用是在修改/查询已有控件。

    :param kwargs: 调用参数
    :return: 含 edit / query（含缩写 e / q）时为 True
    """
    return any(kwargs.get(flag) for flag in ("edit", "query", "e", "q"))


def section(label, width=None, bgc=None, collapse=True, **kwargs):
    """统一折叠栏。

    :param label: 折叠栏标题
    :param width: 宽度，默认取主题 SECTION_WIDTH
    :param bgc: 底色，默认取主题 GREY_BLUE
    :param collapse: 创建后是否收起
    :param kwargs: 其余参数原样透传（marginWidth / expandCommand 等）
    :return: 折叠栏（frameLayout）控件名
    """
    kwargs.setdefault("collapsable", True)
    kwargs.setdefault("marginWidth", theme.SECTION_MARGIN_WIDTH)
    kwargs.setdefault("marginHeight", theme.SECTION_MARGIN_HEIGHT)
    return cmds.frameLayout(label=label,
                            width=theme.SECTION_WIDTH if width is None else width,
                            bgc=theme.GREY_BLUE if bgc is None else bgc,
                            collapse=collapse, **kwargs)


def button(*args, **kwargs):
    """统一按钮：默认高度与内灰底色，宽度由调用方或布局决定。

    :param args: 透传给 cmds.button 的位置参数
    :param kwargs: label / command / width / bgc / annotation 等
    :return: 按钮控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("height", theme.BUTTON_HEIGHT)
        kwargs.setdefault("bgc", theme.INNER_GREY)
    return cmds.button(*args, **kwargs)


def _field(command, *args, **kwargs):
    """输入框类控件的公共创建逻辑（统一高度）。

    :param command: cmds.intField / cmds.floatField / cmds.textField
    :param args: 透传的位置参数
    :param kwargs: 透传的关键字参数
    :return: 输入框控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("height", theme.FIELD_HEIGHT)
    return command(*args, **kwargs)


def int_field(*args, **kwargs):
    """统一整数输入框。

    :return: 控件名
    """
    return _field(cmds.intField, *args, **kwargs)


def float_field(*args, **kwargs):
    """统一浮点输入框。

    :return: 控件名
    """
    return _field(cmds.floatField, *args, **kwargs)


def text_field(*args, **kwargs):
    """统一单行文本框。

    :return: 控件名
    """
    return _field(cmds.textField, *args, **kwargs)


def text_area(*args, **kwargs):
    """统一多行文本区（Maya scrollField）。

    :param kwargs: editable / wordWrap 未指定时默认可编辑、自动换行
    :return: 控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("editable", True)
        kwargs.setdefault("wordWrap", True)
        kwargs.setdefault("height", 120)
    return cmds.scrollField(*args, **kwargs)


def option_menu(*args, **kwargs):
    """统一下拉菜单。

    :return: 控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("height", theme.FIELD_HEIGHT)
    return cmds.optionMenu(*args, **kwargs)


def check_box(*args, **kwargs):
    """统一复选框。

    :return: 控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("height", theme.CHECK_HEIGHT)
    return cmds.checkBox(*args, **kwargs)


def slider(*args, **kwargs):
    """统一滑块。

    :param kind: 关键字参数，默认 "float"；"int" 改用 intSlider
    :param args: 透传位置参数
    :param kwargs: minValue / maxValue / step / value / changeCommand 等
    :return: 控件名
    """
    kind = kwargs.pop("kind", "float")
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("height", theme.SLIDER_HEIGHT)
    command = cmds.intSlider if kind == "int" else cmds.floatSlider
    return command(*args, **kwargs)


def progress(*args, **kwargs):
    """统一进度条。

    :return: 控件名
    """
    if not _is_edit_or_query(kwargs):
        kwargs.setdefault("width", theme.BUTTON_WIDTH)
        kwargs.setdefault("height", 14)
    return cmds.progressBar(*args, **kwargs)


def scroll_page(row_spacing=None, adjustable=True):
    """创建统一滚动页：固定厚度的滚动条 + 统一行距的列布局。

    :param row_spacing: 行距，默认取主题 ROW_SPACING
    :param adjustable: 列宽是否随容器宽度自适应
    :return: 内层 columnLayout 控件名
    """
    cmds.scrollLayout(childResizable=True,
                      horizontalScrollBarThickness=0,
                      verticalScrollBarThickness=theme.SCROLLBAR_THICKNESS)
    return cmds.columnLayout(
        rowSpacing=theme.ROW_SPACING if row_spacing is None else row_spacing,
        adjustableColumn=adjustable)

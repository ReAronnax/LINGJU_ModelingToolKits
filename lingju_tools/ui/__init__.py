# coding: utf-8
"""ui 层：主窗口与通用控件（阶段 3 迁入）。"""

__all__ = ["LingJuGeometryTools"]


def __getattr__(name):
    """延迟导出主窗口类，避免 import lingju_tools.ui 时就拉起整条功能链。

    :param name: 属性名
    :return: 主窗口类
    :raises AttributeError: 未知属性
    """
    if name == "LingJuGeometryTools":
        from .main_window import LingJuGeometryTools
        return LingJuGeometryTools
    raise AttributeError(name)

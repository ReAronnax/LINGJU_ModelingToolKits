# coding: utf-8
"""Lingju Modeling Tools 包入口。

版本号只在这里维护，不再写进文件名；发布产物按 Maya 版本目录区分。
"""

__version__ = "1.79a1"

__all__ = ["show_ui", "show_m2m", "__version__"]


def show_ui():
    """打开建模工具主窗口（延迟导入，避免包初始化时就加载 Qt/UI 代码）。"""
    from .commands import show_ui as _show_ui
    return _show_ui()


def show_m2m():
    """打开 Maya2Marmoset 烘焙面板（延迟导入）。"""
    from .commands import show_m2m as _show_m2m
    return _show_m2m()

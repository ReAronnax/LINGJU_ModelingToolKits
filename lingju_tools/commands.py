# coding: utf-8
"""对外命令入口：shelf 按钮 / 拖拽安装器最终调用的就是这里的函数。

所有导入都放在函数内部完成，保证 import lingju_tools 本身不触发 UI 构建。
"""


def show_ui():
    """构建并显示建模工具主窗口。

    :return: 主窗口控制器实例
    """
    from .ui.main_window import LingJuGeometryTools
    return LingJuGeometryTools()


def show_m2m():
    """显示 Maya2Marmoset 烘焙面板。

    :return: 面板构建函数的返回值
    """
    from .features.maya2marmoset import m2m_ui_show
    return m2m_ui_show()

# coding: utf-8
"""对外命令入口：shelf 按钮 / 拖拽安装器最终调用的就是这里的函数。

所有导入都放在函数内部完成，保证 import lingju_tools 本身不触发 UI 构建。
"""

# 主窗口与桥接模块在拆分过程中逐步迁入，未完成前给出明确提示而不是 ImportError 堆栈。
_PENDING = "模块尚未迁移完成（重构进行中），当前请继续使用 Lingju_Modeling_Tools.py"


def show_ui():
    """构建并显示建模工具主窗口。

    :return: 主窗口控制器实例
    :raises RuntimeError: UI 模块尚未迁移完成时抛出
    """
    try:
        from .ui.main_window import LingJuGeometryTools
    except ImportError as exc:
        raise RuntimeError("%s：%s" % (_PENDING, exc))
    return LingJuGeometryTools()


def show_m2m():
    """显示 Maya2Marmoset 烘焙面板。

    :return: 面板构建函数的返回值
    :raises RuntimeError: 桥接模块尚未迁移完成时抛出
    """
    try:
        from .features.maya2marmoset import m2m_ui_show
    except ImportError as exc:
        raise RuntimeError("%s：%s" % (_PENDING, exc))
    return m2m_ui_show()

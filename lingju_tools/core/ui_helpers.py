# coding: utf-8
"""通用 UI 小工具：Maya 控件短名、Qt 对象存活检查、滚轮步进器。"""

from .qt_compat import QtCore, _WHEEL_EVENT, _shiboken_isValid


def _ui_short_name(handle):
    """取 Maya UI 控件的短名。

    v174 把 paneLayout 重挂进 QTabWidget / 弹出独立窗口后，创建时返回的
    全路径会失效；短名在会话内唯一且跨层级稳定，所有延迟引用统一用短名。

    :param handle: Maya 控件全路径或短名
    :return: 短名；入参为空时原样返回
    """
    if not handle:
        return handle
    return handle.split('|')[-1]


def _qt_valid(widget):
    """检查 Qt 对象的 C++ 侧是否还活着。

    C++ 被销毁后 Python wrapper 仍在，一碰就抛
    RuntimeError: Internal C++ object already deleted。

    :param widget: 待检查的 Qt 对象
    :return: C++ 侧仍有效返回 True
    """
    if widget is None:
        return False
    try:
        return _shiboken_isValid(widget)
    except Exception:
        return False


class _WheelStepper(QtCore.QObject):
    """把滚轮事件转成步进回调：Qt 的 SpinBox 原生支持滚轮，Maya 数值框没有。"""

    def __init__(self, parent, handler):
        """初始化滚轮步进器。

        :param parent: 事件过滤器的父对象（Qt 对象）
        :param handler: 收到滚动时调用的回调，参数为滚动增量
        """
        super(_WheelStepper, self).__init__(parent)
        self._handler = handler

    def eventFilter(self, obj, event):
        """拦截滚轮事件并转成步进回调。

        :param obj: 事件来源对象
        :param event: Qt 事件
        :return: 已处理返回 True，否则 False
        """
        if event.type() == _WHEEL_EVENT:
            delta = event.angleDelta().y()
            if not delta:
                delta = event.pixelDelta().y()
            if delta:
                self._handler(delta)
                return True
        return False

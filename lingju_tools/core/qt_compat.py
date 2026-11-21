# coding: utf-8
"""Qt / shiboken 版本兼容层：Maya 2022 用 PySide2，Maya 2023+ 用 PySide6。"""

try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # Maya 2023+
    from PySide6 import QtCore, QtGui, QtWidgets

try:
    from shiboken2 import wrapInstance, isValid as _shiboken_isValid
except ImportError:  # Maya 2023+
    from shiboken6 import wrapInstance, isValid as _shiboken_isValid

try:
    _WHEEL_EVENT = QtCore.QEvent.Wheel
except AttributeError:  # Qt6 把枚举收进 QEvent.Type
    _WHEEL_EVENT = QtCore.QEvent.Type.Wheel

__all__ = [
    "QtCore", "QtGui", "QtWidgets",
    "wrapInstance", "_shiboken_isValid", "_WHEEL_EVENT",
]

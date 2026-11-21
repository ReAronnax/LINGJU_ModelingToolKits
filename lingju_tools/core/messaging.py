# coding: utf-8
"""统一消息提示：灰=信息，黄=警告，红=错误。

所有界面提示必须走这里的 lj_message/lj_info/lj_warn/lj_error，
不要直接调用 cmds.inViewMessage。
"""

import maya.cmds as cmds

from .qt_compat import QtCore

LJ_MSG_COLORS = {
    "info": 0x4F5259,   # 正常灰 #4F5259：操作结果、状态说明
    "warn": 0xA66F0F,   # 警告黄 #A66F0F：条件不满足、需要用户注意
    "error": 0x811A1A,  # 错误红 #811A1A：执行失败、依赖缺失
}

# 每个显示位置的最近一次消息序号，用于避免过期定时器误清新消息
_LJ_MSG_SEQ = [0]
_LJ_MSG_TOKENS = {}


def _lj_clear_message(pos, token):
    """0.8 秒后清除该位置的提示（Maya 的 fade 在部分环境不生效，这里兜底）。

    :param pos: inViewMessage 位置
    :param token: 消息序号，只有仍是最新消息时才清除
    """
    if _LJ_MSG_TOKENS.get(pos) != token:
        return
    try:
        cmds.inViewMessage(clear=pos)
    except Exception:
        pass


def lj_message(text, level="info", pos="topRight", fade=True, log=False,
               fadeStayTime=1.5, fadeOutTime=0.3):
    """所有界面提示的唯一出口：灰=信息，黄=警告，红=错误。

    :param text: 提示文字
    :param level: "info" / "warn" / "error"
    :param pos: inViewMessage 显示位置，默认右上角
    :param fade: 是否自动淡出（False 时一直保留，直到被清除）
    :param log: 是否同时打印到 Script Editor（需要保留详细日志时用）
    :param fadeStayTime: 停留时长（秒）
    :param fadeOutTime: 淡出时长（秒），默认 0.3
    """
    if log:
        print(text)
    try:
        cmds.inViewMessage(msg=text, pos=pos,
                           bkc=LJ_MSG_COLORS.get(level, LJ_MSG_COLORS["info"]),
                           fadeStayTime=int(fadeStayTime * 1000),
                           fadeOutTime=int(fadeOutTime * 1000),
                           fade=fade, fontSize=11)
    except Exception:
        # 批处理 / 无界面环境下没有视口提示，静默跳过
        pass
    if fade:
        try:
            _LJ_MSG_SEQ[0] += 1
            token = _LJ_MSG_SEQ[0]
            _LJ_MSG_TOKENS[pos] = token
            delay = int((fadeStayTime + fadeOutTime) * 1000) + 30
            QtCore.QTimer.singleShot(
                delay, lambda p=pos, t=token: _lj_clear_message(p, t))
        except Exception:
            pass


def lj_info(text, pos="topRight", fade=True, log=False, fadeStayTime=1.5):
    """显示信息级提示（灰色）。

    :param text: 提示文字
    :param pos: 显示位置
    :param fade: 是否自动淡出
    :param log: 是否同时打印到 Script Editor
    :param fadeStayTime: 停留时长（秒）
    """
    lj_message(text, "info", pos, fade, log, fadeStayTime)


def lj_warn(text, pos="topRight", fade=True, log=False, fadeStayTime=1.5):
    """显示警告级提示（黄色）。

    :param text: 提示文字
    :param pos: 显示位置
    :param fade: 是否自动淡出
    :param log: 是否同时打印到 Script Editor
    :param fadeStayTime: 停留时长（秒）
    """
    lj_message(text, "warn", pos, fade, log, fadeStayTime)


def lj_error(text, pos="topRight", fade=True, log=False, fadeStayTime=1.5):
    """显示错误级提示（红色）。

    :param text: 提示文字
    :param pos: 显示位置
    :param fade: 是否自动淡出
    :param log: 是否同时打印到 Script Editor
    :param fadeStayTime: 停留时长（秒）
    """
    lj_message(text, "error", pos, fade, log, fadeStayTime)

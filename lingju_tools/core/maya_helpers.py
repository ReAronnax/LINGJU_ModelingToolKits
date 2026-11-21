# coding: utf-8
"""Maya 场景通用工具：撤销块、DAG 路径、活动网格、组件名拼接。

feature 模块共用的底层操作集中在这里，避免各功能模块重复实现。
"""

from contextlib import contextmanager

import maya.cmds as cmds
import maya.api.OpenMaya as oma


class MeshError(Exception):
    """网格选择/类型不满足前置条件时抛出。"""
    pass


_UNDO_DEPTH = 0


@contextmanager
def undo_chunk(name):
    """Nested calls collapse into the outermost chunk (used by live preview).

    :param name: 撤销块名称（Maya 撤销列表里显示的文字）
    """
    global _UNDO_DEPTH
    if _UNDO_DEPTH:
        _UNDO_DEPTH += 1
        try:
            yield
        finally:
            _UNDO_DEPTH -= 1
        return
    cmds.undoInfo(openChunk=True, chunkName=name)
    _UNDO_DEPTH = 1
    try:
        yield
    finally:
        _UNDO_DEPTH = 0
        cmds.undoInfo(closeChunk=True)


def dag_path(node):
    """把节点名转成 MDagPath。

    :param node: 节点名（可带组件后缀）
    :return: MDagPath
    """
    sel = oma.MSelectionList()
    sel.add(node)
    return sel.getDagPath(0)


def active_mesh():
    """Return (transform_full_path, shape_full_path) for the selected mesh.

    :return: (变换节点全路径, 网格 shape 全路径)
    :raises MeshError: 未选中网格或选中对象不是多边形网格
    """
    selection = cmds.ls(selection=True, long=True, flatten=True) or []
    if not selection:
        raise MeshError("请先选择一个多边形网格物体或它的边/点组件。")

    base = selection[0].split(".")[0]
    if cmds.objectType(base, isAType="mesh"):
        shape = base
    elif cmds.objectType(base, isAType="transform"):
        shapes = cmds.listRelatives(base, shapes=True, noIntermediate=True, fullPath=True) or []
        shapes = [s for s in shapes if cmds.nodeType(s) == "mesh"]
        if not shapes:
            raise MeshError("选中的物体没有多边形网格：%s" % base)
        shape = shapes[0]
    else:
        raise MeshError("选中的不是多边形网格：%s" % base)

    parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
    if not parents:
        raise MeshError("网格没有父变换节点：%s" % shape)
    return parents[0], shape


def component_name(transform, kind, index):
    """拼接单个组件名。

    :param transform: 变换节点名
    :param kind: 组件类型（vtx/edge/face）
    :param index: 组件索引
    :return: 形如 "pCube1.vtx[0]" 的组件名
    """
    return "%s.%s[%d]" % (transform, kind, index)


def component_names(transform, kind, indices):
    """批量拼接组件名（去重升序）。

    :param transform: 变换节点名
    :param kind: 组件类型（vtx/edge/face）
    :param indices: 组件索引集合
    :return: 组件名列表
    """
    return ["%s.%s[%d]" % (transform, kind, i) for i in sorted(set(int(v) for v in indices))]

# coding: utf-8
"""
    Author: Reggie Tu
Date: Nov 09, 2026

version: v1.79a1
    1. 「零距命名材质管理」新增可选勾选「颜色贴图连 Transparency」：
       勾选后，连接颜色贴图时同时把贴图节点的 outTransparency 连接到
       材质的 transparency；取消勾选后，各入口（一键添加颜色贴图、
       拖拽/选择贴图、手动提交贴图字段、更新场景恢复颜色贴图、「刷新」）
       会断开「该颜色贴图节点 → 该材质 transparency」的连线，
       不影响其他材质或其他透明输入；材质或贴图节点没有该属性时自动跳过。
       已有颜色贴图的材质同样适用。
    2. 其余内容与 v1.79 一致
       （v1.79：代码收敛：删除从未被引用的死代码，按角度扩选与滑块更新
        的重复实现合并）
    3. 历史版本
       （v1.78：命名材质管理新增场景材质球行：下拉 / 刷新 / 赋予选中物件或面 /
        材质平面
        在贴图文件夹按同名文件查找，找到改路径，找不到不动）
       （v1.77：烘焙「赋予材质」支持选中面，按钮「赋予选中物件 / 面」）
       （v1.76：选项卡内容包进 scrollLayout 修复无滚动条；最宽控件 310、
        窗口 325 让滚动条在默认宽度即可见；智能分离选面后解组不残留组）
       （v1.75：智能分离选面改用 Maya 原生 Extract Face / Duplicate Face）
       （v1.74：主界面改为横向选项卡布局，支持拖拽排序与双击弹出）
       （v1.73：扭曲阈值 / 按角度选面 / UV检查 / 棋盘格数值框支持悬停滚轮调节）
       （v1.72：UV 检查 / 传递UV 折叠栏宽度统一）
       （v1.71：边重建各数值输入框支持悬停滚轮调节）
       （v1.70：内嵌 PolyravenUV v1.0.5 MEL）
       （v1.68：Round Inset 圆角内嵌）
"""

import maya.cmds as cmds
import re
import math
import maya.mel as mel
import maya.api.OpenMaya as oma
import maya.OpenMaya as om
import maya.OpenMayaUI as omui
from maya.OpenMaya import MGlobal
import contextlib
import os
import sys
import string
import json
import io
import hashlib
import shutil
import socket
import subprocess
import threading
import time
import tempfile

try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # Maya 2023+
    from PySide6 import QtCore, QtGui, QtWidgets


# Polyraven proc 是否已通过 mel.eval source 过（全局只 source 一次）
_POLYRAVEN_LOADED = False

try:
    from shiboken2 import wrapInstance, isValid as _shiboken_isValid
except ImportError:  # Maya 2023+
    from shiboken6 import wrapInstance, isValid as _shiboken_isValid

try:
    _WHEEL_EVENT = QtCore.QEvent.Wheel
except AttributeError:  # Qt6 把枚举收进 QEvent.Type
    _WHEEL_EVENT = QtCore.QEvent.Type.Wheel


def _ui_short_name(handle):
    """取 Maya UI 控件的短名。

    v174 把 paneLayout 重挂进 QTabWidget / 弹出独立窗口后，创建时返回的
    全路径会失效；短名在会话内唯一且跨层级稳定，所有延迟引用统一用短名。
    """
    if not handle:
        return handle
    return handle.split('|')[-1]


def _qt_valid(widget):
    """检查 Qt 对象的 C++ 侧是否还活着。

    C++ 被销毁后 Python wrapper 仍在，一碰就抛
    RuntimeError: Internal C++ object already deleted。
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
        super(_WheelStepper, self).__init__(parent)
        self._handler = handler

    def eventFilter(self, obj, event):
        if event.type() == _WHEEL_EVENT:
            delta = event.angleDelta().y()
            if not delta:
                delta = event.pixelDelta().y()
            if delta:
                self._handler(delta)
                return True
        return False


# ---------------------------------------------------------------------------
# 统一消息提示：灰=信息，黄=警告，红=错误
# ---------------------------------------------------------------------------

LJ_MSG_COLORS = {
    "info": 0x4F5259,   # 正常灰 #4F5259：操作结果、状态说明
    "warn": 0xA66F0F,   # 警告黄 #A66F0F：条件不满足、需要用户注意
    "error": 0x811A1A,  # 错误红 #811A1A：执行失败、依赖缺失
}

# 每个显示位置的最近一次消息序号，用于避免过期定时器误清新消息
_LJ_MSG_SEQ = [0]
_LJ_MSG_TOKENS = {}


def _lj_clear_message(pos, token):
    """0.8 秒后清除该位置的提示（Maya 的 fade 在部分环境不生效，这里兜底）"""
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
    lj_message(text, "info", pos, fade, log, fadeStayTime)


def lj_warn(text, pos="topRight", fade=True, log=False, fadeStayTime=1.5):
    lj_message(text, "warn", pos, fade, log, fadeStayTime)


def lj_error(text, pos="topRight", fade=True, log=False, fadeStayTime=1.5):
    lj_message(text, "error", pos, fade, log, fadeStayTime)

class BaseAlign:

    def alignEdge(self):
        global outParent
        mesh = cmds.ls(sl=1, fl=1)
        if len(mesh) == 1:
            checkLongName = cmds.ls(mesh[0], l=1)
            parentNode = checkLongName[0].split('|')
            if len(parentNode) > 2:
                outParent = ''
                outParent = '|'.join(parentNode[1:-1])
                cmds.parent(mesh[0], w=1)
            cleanList = ('sampleCurv*', 'sampleMes*', 'rotationPlan*')
            for c in cleanList:
                if cmds.objExists(c):
                    cmds.delete(c)
            gface, gHitp, cEdge, cEdgePos = self.getClosestEdge()
            cmds.select(cEdge)
            checkCVList = cmds.ls(cmds.polyListComponentConversion(cEdge, fe=True, tv=True), flatten=True)
            mx, my, mz = cmds.pointPosition(checkCVList[0], w=1)
            cmds.polyPlane(w=1, h=1, sx=1, sy=1, ax=(0, 1, 0), cuv=2, ch=0, n='rotationPlane')
            cmds.polyCreateFacet(
                p=[(mx, my, mz), (cEdgePos[0], cEdgePos[1], cEdgePos[2]), (gHitp[0], gHitp[1], gHitp[2])])
            cmds.rename('sampleMesh')
            cmds.select("rotationPlane.vtx[0:2]", "sampleMesh.vtx[0:2]")
            CMD = 'snap3PointsTo3Points(0);'
            mel.eval(CMD)
            cmds.parent(mesh[0], 'rotationPlane')
            axes = ["X", "Y", "Z"]
            for a in axes:
                val = cmds.getAttr(mesh[0] + ".rotate" + a)
                valTmp = ''
                if val > 0:
                    valTmp = val + 45
                else:
                    valTmp = val - 45
                valNew = int(valTmp / 90)
                cmds.setAttr((mesh[0] + ".rotate" + a), (valNew * 90))

            cmds.move(gHitp[0], gHitp[1], gHitp[2], mesh[0], rpr=True, wd=True)
            cmds.select(mesh[0])
            cmds.parent(w=1)
            if len(parentNode) > 2:
                cmds.parent(mesh[0], outParent)
            for c in cleanList:
                if cmds.objExists(c):
                    cmds.delete(c)

    def getClosestEdge(self):
        mayaMesh = cmds.ls(sl=1, fl=1)
        gFace = ''
        gHitP = ''
        gFace, gHitP = self.getClosestMeshHit(mayaMesh[0])
        listF2E = cmds.ls(cmds.polyListComponentConversion(gFace, ff=True, te=True), flatten=True)
        cEdge = ''
        smallestDist = 1000000
        cEdgePos = []
        for l in listF2E:
            cmds.select(l)
            cmds.polyToCurve(form=2, degree=1, conformToSmoothMeshPreview=1)
            sampleCurve = cmds.ls(sl=1)
            selectionList = om.MSelectionList()
            selectionList.add(sampleCurve[0])
            dagPath = om.MDagPath()
            selectionList.getDagPath(0, dagPath)
            omCurveOut = om.MFnNurbsCurve(dagPath)
            pointInSpace = om.MPoint(gHitP[0], gHitP[1], gHitP[2])
            closestPoint = om.MPoint()
            closestPoint = omCurveOut.closestPoint(pointInSpace)
            getDist = math.sqrt(((closestPoint[0] - gHitP[0]) ** 2) + ((closestPoint[1] - gHitP[1]) ** 2) + (
                    (closestPoint[2] - gHitP[2]) ** 2))
            if getDist < smallestDist:
                smallestDist = getDist
                cEdge = l
                cEdgePos = [closestPoint[0], closestPoint[1], closestPoint[2]]
            cmds.delete(sampleCurve)
        cmds.select(cEdge)
        return (gFace, gHitP, cEdge, cEdgePos)

    def getClosestMeshHit(self, mayaMesh):
        myShape = cmds.listRelatives(mayaMesh, shapes=True, f=True)
        checkList = self.screenVisPoly()
        checkList.remove(myShape[0])
        meshPos = cmds.xform(mayaMesh, q=1, ws=1, a=1, piv=1)
        posXXX = [meshPos[0], meshPos[1], meshPos[2]]
        shortDistanceCheck = 10000
        resultFace = []
        resultCV = []
        resultHitPoint = []
        for c in checkList:
            transNode = cmds.listRelatives(c, p=True)
            getFaceDist, getFace, getHitPoint = self.getClosestPointOnFace(transNode[0], posXXX)
            if getFaceDist < shortDistanceCheck:
                shortDistanceCheck = getFaceDist
                resultFace = getFace
                resultHitPoint = getHitPoint
        return (resultFace, resultHitPoint)

    def getClosestPointOnFace(self, mayaMesh, pos=None):
        if pos is None:
            pos = [0, 0, 0]
        mVector = oma.MVector(pos)  # using MVector type to represent position
        selectionList = oma.MSelectionList()
        selectionList.add(mayaMesh)
        dPath = selectionList.getDagPath(0)
        mMesh = oma.MFnMesh(dPath)
        ID = mMesh.getClosestPoint(oma.MPoint(mVector), space=oma.MSpace.kWorld)[1]
        closestPoint = mMesh.getClosestPoint(oma.MPoint(mVector), space=oma.MSpace.kWorld)[0]
        cpx = closestPoint[0]
        cpy = closestPoint[1]
        cpz = closestPoint[2]
        hitPointPosition = [cpx, cpy, cpz]
        hitFaceName = (mayaMesh + '.f[' + str(ID) + ']')
        getFaceDist = math.sqrt(((pos[0] - cpx) ** 2) + ((pos[1] - cpy) ** 2) + ((pos[2] - cpz) ** 2))
        return (getFaceDist, hitFaceName, hitPointPosition)

    def screenVisPoly(self):
        commonList = []
        view = omui.M3dView.active3dView()
        MGlobal.selectFromScreen(0, 0, view.portWidth(), view.portHeight(), MGlobal.kReplaceList)
        objects = om.MSelectionList()
        sel = om.MSelectionList()
        MGlobal.getActiveSelectionList(objects)
        MGlobal.setActiveSelectionList(sel, MGlobal.kReplaceList)
        fromScreen = []
        objects.getSelectionStrings(fromScreen)
        shapesOnScreen = cmds.listRelatives(fromScreen, shapes=True, f=True)
        meshList = cmds.ls(type='mesh', l=True)  # only polygon
        if len(meshList) > 0 and shapesOnScreen is not None:
            commonList = list(set(meshList) & set(shapesOnScreen))
            return commonList
        else:
            commonList = []
            return commonList


class Align_Edge(BaseAlign):
    pass


class Instant_Drag(BaseAlign):
    def __init__(self):
        self.alignEdge = Align_Edge()

    def instantDrag(self):
        cleanList = ('instPicker', 'instRot')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        global ctx
        ctx = 'Click2dTo3dCtx'
        global storeHitFace
        storeHitFace = ''
        if cmds.draggerContext(ctx, exists=True):
            cmds.deleteUI(ctx)
        cmds.draggerContext(ctx, pressCommand=self.instDragPick, rc=self.instDragClean, dragCommand=self.instDragMove,
                            name=ctx,
                            cursor='crossHair', undoMode='step')
        cmds.setToolTo(ctx)

    def instDragPick(self):
        preSelect = cmds.ls(sl=1, fl=1, l=1)
        global ctx
        global screenX, screenY
        global checkScreenMeshList
        global storeCameraPosition
        global storeMeshNode
        global parentDir
        global targetMeshName
        global instDul
        global storeHitFace
        global cameraFarClip
        global storeRotCount
        global lockCount
        global edgeAlignRecord
        edgeAlignRecord = 0
        storeRotCount = 0
        vpX, vpY, _ = cmds.draggerContext(ctx, query=True, anchorPoint=True)
        screenX = vpX
        screenY = vpY
        pos = om.MPoint()
        dir = om.MVector()
        omui.M3dView().active3dView().viewToWorld(int(vpX), int(vpY), pos, dir)
        pos2 = om.MFloatPoint(pos.x, pos.y, pos.z)
        view = omui.M3dView.active3dView()
        cam = om.MDagPath()
        view.getCamera(cam)
        camPath = cam.fullPathName()
        cameraTrans = cmds.listRelatives(camPath, type='transform', p=True)
        cameraFarClip = cmds.getAttr(cameraTrans[0] + '.farClipPlane')
        storeCameraPosition = cmds.xform(cameraTrans, q=1, ws=1, rp=1)
        ##########################################################
        storeMeshNode = []
        instDul = 0
        checkHit = 0
        finalMesh = []
        shortDistance = cameraFarClip
        distanceBetween = cameraFarClip
        hitpoint = om.MFloatPoint()
        hitFace = om.MScriptUtil()
        hitFace.createFromInt(0)
        hitFacePtr = hitFace.asIntPtr()
        ##########################################################
        checkScreenMeshList = self.screenVisPoly()
        for mesh in checkScreenMeshList:
            selectionList = om.MSelectionList()
            selectionList.add(mesh)
            dagPath = om.MDagPath()
            selectionList.getDagPath(0, dagPath)
            fnMesh = om.MFnMesh(dagPath)
            intersection = fnMesh.closestIntersection(
                om.MFloatPoint(pos2),
                om.MFloatVector(dir),
                None,
                None,
                False,
                om.MSpace.kWorld,
                cameraFarClip,
                False,
                None,
                hitpoint,
                None,
                hitFacePtr,
                None,
                None,
                None)
            if intersection:
                x = hitpoint.x
                y = hitpoint.y
                z = hitpoint.z
                distanceBetween = math.sqrt(
                    ((float(storeCameraPosition[0]) - x) ** 2) + ((float(storeCameraPosition[1]) - y) ** 2) + (
                            (float(storeCameraPosition[2]) - z) ** 2))
                if distanceBetween < shortDistance:
                    shortDistance = distanceBetween
                    finalMesh = mesh
        if preSelect:
            checkShape = cmds.listRelatives(preSelect, shapes=True, fullPath=True)
            finalMesh = checkShape

        if len(finalMesh) > 0:
            # preSelect = cmds.ls(sl=1,fl=1,l=1)
            # checkShape = cmds.listRelatives(preSelect, shapes=True, fullPath=True)
            # finalMesh = checkShape
            storeMeshNode = cmds.listRelatives(finalMesh, type='transform', p=True, f=True)
            shapeNode = cmds.listRelatives(storeMeshNode[0], fullPath=True, ad=True)
            parentDir = '|'.join(storeMeshNode[0].split('|')[0:-1])
            targetMeshName = storeMeshNode[0].split('|')[-1]
            # move pivot to bbox base
            rotSave = cmds.getAttr(targetMeshName + '.rotate')
            posSave = cmds.xform(targetMeshName, q=True, ws=True, piv=True)[:3]
            cmds.setAttr(targetMeshName + '.rotate', 0, 0, 0)
            bbox = cmds.exactWorldBoundingBox(targetMeshName)
            base_position = bbox[1]
            cmds.move(posSave[0], base_position, posSave[2], (targetMeshName + '.scalePivot'),
                      (targetMeshName + '.rotatePivot'), ws=True)

            ##########################################################
            cmds.group(empty=1, n='instPicker')
            cmds.duplicate('instPicker')
            cmds.rename('instRot')
            cmds.parent('instRot', 'instPicker')
            cmds.select('instPicker', storeMeshNode[0])
            cmds.matchTransform(pos=1, rot=1)
            cmds.parent(storeMeshNode[0], 'instRot')
            cmds.select('instPicker|instRot|' + targetMeshName)
            cmds.setAttr('instPicker.rotate', rotSave[0][0], rotSave[0][1], rotSave[0][2])
            cmds.makeIdentity(apply=True, t=1, r=1, s=1, n=0)
            cmds.delete(constructionHistory=True)
            ##########################################################
            for s in shapeNode:
                if s in checkScreenMeshList:
                    checkScreenMeshList.remove(s)
            currentRoteY = cmds.getAttr('instRot.rotateY')
            lockCount = int(currentRoteY / 15) * 15

    def instDragClean(self):
        global parentDir
        global targetMeshName
        global instDul
        global edgeAlignRecord
        edgeAlignRecord = 0
        if cmds.objExists('instPicker'):
            if len(parentDir) == 0:
                cmds.select('instPicker|instRot|' + targetMeshName)
                cmds.parent(w=1)
            else:
                cmds.parent(('|instPicker|instRot|' + targetMeshName), (parentDir))

        cleanList = ('instPicker', 'instRot')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        instDul = 0
        cmds.select(cl=1)

    def instDragMove(self):
        global screenX
        global storeMeshNode
        global storeHitFace
        global cameraFarClip
        global storeRotCount
        global storeRotCount
        global lockCount
        global edgeAlignRecord
        if storeMeshNode:
            if cmds.objExists('instPicker'):
                global ctx
                global screenX, screenY
                global storeCameraPosition
                global checkScreenMeshList
                global parentDir
                global targetMeshName
                global instDul

                modifiers = cmds.getModifiers()
                vpX, vpY, _ = cmds.draggerContext(ctx, query=True, dragPoint=True)

                if (modifiers == 4):
                    # press Ctrl ----> rotate
                    if screenX > vpX:
                        lockCount = lockCount - 2
                    else:
                        lockCount = lockCount + 2
                    screenX = vpX
                    if lockCount < -360:
                        lockCount = -360
                    elif lockCount > 360:
                        lockCount = 360

                    getX = int(lockCount / 15) * 15

                    if storeRotCount != getX:
                        storeRotCount = getX
                    cmds.setAttr('instRot.rotateY', storeRotCount)
                    cmds.refresh(cv=True, f=True)
                elif (modifiers == 5):
                    # press Shift + Ctrl
                    if edgeAlignRecord == 0:
                        self.alignEdge.alignEdge()
                        edgeAlignRecord = 1
                        cmds.refresh(cv=True, f=True)
                elif (modifiers == 1):
                    # press Shift -----> dulpicate current mesh
                    if instDul == 0:
                        newD = cmds.duplicate(('|instPicker|instRot|' + targetMeshName), rr=1)
                        if len(parentDir) == 0:
                            cmds.select('instPicker|instRot|' + targetMeshName)
                            cmds.parent(w=1)
                        else:
                            cmds.parent(('|instPicker|instRot|' + targetMeshName), (parentDir))
                        targetMeshName = newD[0]
                        cmds.select(targetMeshName)
                        instDul = 1
                    cmds.refresh(cv=True, f=True)
                else:
                    pos = om.MPoint()
                    dir = om.MVector()
                    omui.M3dView().active3dView().viewToWorld(int(vpX), int(vpY), pos, dir)
                    pos2 = om.MFloatPoint(pos.x, pos.y, pos.z)
                    ############################################################
                    checkHit = 0
                    finalMesh = ''
                    hitFaceName = ''
                    finalX = 0
                    finalY = 0
                    finalZ = 0
                    shortDistance = cameraFarClip
                    distanceBetween = cameraFarClip
                    hitpoint = om.MFloatPoint()
                    hitFace = om.MScriptUtil()
                    hitFace.createFromInt(0)
                    hitFacePtr = hitFace.asIntPtr()
                    ############################################################
                    for mesh in checkScreenMeshList:
                        selectionList = om.MSelectionList()
                        selectionList.add(mesh)
                        dagPath = om.MDagPath()
                        selectionList.getDagPath(0, dagPath)
                        fnMesh = om.MFnMesh(dagPath)
                        intersection = fnMesh.closestIntersection(
                            om.MFloatPoint(pos2),
                            om.MFloatVector(dir),
                            None,
                            None,
                            False,
                            om.MSpace.kWorld,
                            cameraFarClip,
                            False,
                            None,
                            hitpoint,
                            None,
                            hitFacePtr,
                            None,
                            None,
                            None)
                        if intersection:
                            x = hitpoint.x
                            y = hitpoint.y
                            z = hitpoint.z
                            distanceBetween = math.sqrt(
                                ((float(storeCameraPosition[0]) - x) ** 2) + (
                                        (float(storeCameraPosition[1]) - y) ** 2) + (
                                        (float(storeCameraPosition[2]) - z) ** 2))
                            if distanceBetween < shortDistance:
                                shortDistance = distanceBetween
                                finalMesh = mesh

                    if finalMesh:
                        selectionList = om.MSelectionList()
                        selectionList.add(finalMesh)
                        dagPath = om.MDagPath()
                        selectionList.getDagPath(0, dagPath)
                        fnMesh = om.MFnMesh(dagPath)
                        intersection = fnMesh.closestIntersection(
                            om.MFloatPoint(pos2),
                            om.MFloatVector(dir),
                            None,
                            None,
                            False,
                            om.MSpace.kWorld,
                            cameraFarClip,
                            False,
                            None,
                            hitpoint,
                            None,
                            hitFacePtr,
                            None,
                            None,
                            None)
                        finalX = hitpoint.x
                        finalY = hitpoint.y
                        finalZ = hitpoint.z
                        hitFace = om.MScriptUtil(hitFacePtr).asInt()
                        hitFaceName = (finalMesh + '.f[' + str(hitFace) + ']')
                        instDul = 0
                        cmds.setAttr('instPicker.translate', finalX, finalY, finalZ)
                        if storeHitFace != hitFaceName:
                            rx, ry, rz = self.checkFaceAngle(hitFaceName)
                            cmds.setAttr('instPicker.rotate', rx, ry, rz)
                            storeHitFace = hitFaceName
                        cmds.refresh(cv=True, f=True)

    def checkFaceAngle(self, faceName):
        shapeNode = cmds.listRelatives(faceName, fullPath=True, parent=True)
        transformNode = cmds.listRelatives(shapeNode[0], fullPath=True, parent=True)
        obj_matrix = oma.MMatrix(cmds.xform(transformNode, query=True, worldSpace=True, matrix=True))
        face_normals_text = cmds.polyInfo(faceName, faceNormals=True)[0]
        face_normals = [float(digit) for digit in re.findall(r'-?\d*\.\d*', face_normals_text)]
        v = oma.MVector(face_normals) * obj_matrix
        upvector = oma.MVector(0, 1, 0)
        getHitNormal = v
        quat = oma.MQuaternion(upvector, getHitNormal)
        quatAsEuler = oma.MEulerRotation()
        quatAsEuler = quat.asEulerRotation()
        rx, ry, rz = math.degrees(quatAsEuler.x), math.degrees(quatAsEuler.y), math.degrees(quatAsEuler.z)
        return rx, ry, rz


class Fill_Selection:
    def fill_selection(self):
        self.selection_fill_start()
        cmds.scriptJob(runOnce=True, event=["SelectionChanged", self.selection_fill_finish])

    def selection_fill_start(self):
        sel_edges = cmds.filterExpand(expand=True, selectionMask=32) or []
        if not sel_edges:
            cmds.ConvertSelectionToEdges()
            cmds.polySelectConstraint(mode=2, type=0x8000)
            cmds.polySelectConstraint(disable=True)

        edge_selection = cmds.ls(selection=True)
        object_selection = cmds.ls(selection=True, objectsOnly=True)

        split_node_check = cmds.polySplitEdge()

        if cmds.objExists("fillSelectionTempNode"):
            cmds.delete("fillSelectionTempNode")

        if split_node_check:
            cmds.rename(split_node_check[0], "fillSelectionTempNode")

        cmds.SelectFacetMask()
        cmds.polySelectConstraint(type=0x0008, shell=True, mode=3)
        cmds.select(clear=True)

    def selection_fill_finish(self):
        fill_selection = cmds.ls(selection=True)
        cmds.polySelectConstraint(shell=False)
        self.reset_poly_select_constraint()

        if cmds.objExists("fillSelectionTempNode"):
            cmds.delete("fillSelectionTempNode")

        if fill_selection:
            cmds.select(fill_selection)

    def reset_poly_select_constraint(self):
        cmds.polySelectConstraint(mode=0, type=0, shell=False)
        cmds.polySelectConstraint(disable=True)


class Round_Inset:
    def roundInset(self):
        cmd = 'source dagMenuProc;'
        mel.eval(cmd)
        global insetDataPP
        global insetMesh
        global insetFace
        global insetDataEdgeLoopList
        global insetMeshVolume
        global insetInnerEdges
        global updatedNewSelEdge
        updatedNewSelEdge = []
        insetInnerEdges = []
        insetDataEdgeLoopList = []
        insetDataPP = []
        insetMesh = ''
        insetFace = ''
        insetMeshVolume = 0
        if cmds.window('RoundInsetUI', exists=True):
            cmds.deleteUI('RoundInsetUI')
        RoundInsetUI = cmds.window('RoundInsetUI', title='Round Inset v1.68', w=240, s=1, mxb=False, mnb=False)
        cmds.columnLayout(adj=1)
        cmds.text(l='')
        cmds.rowColumnLayout(nc=3, cw=[(1, 300), (2, 20), (3, 5), (4, 90), (5, 10)])
        cmds.columnLayout(adj=1)
        cmds.rowColumnLayout(nc=2, cw=[(1, 270), (2, 20)])
        cmds.floatSliderGrp('rInsetV', en=0, cw3=[60, 40, 0], label='Offset   ', field=True, v=0.01, min=-1, max=1,
                            step=0.001)
        cmds.button('rInsetVMax', l='+', c='slipderMax("rInsetV")', en=1, bgc=[0.28, 0.28, 0.28])
        cmds.floatSliderGrp('rBevelRound', en=0, cw3=[60, 40, 0], label='Round   ', field=True, v=0, min=-1, max=1,
                            step=0.001)
        cmds.button('rBevelRoundMax', l='+', c='slipderMax("rBevelRound")', en=1, bgc=[0.28, 0.28, 0.28])
        cmds.floatSliderGrp('rBevelAngle', en=0, cw3=[60, 40, 0], cc='rBevelAngleUpdate()', dc='rBevelAngleUpdate()',
                            label='Angle   ', field=True, v=80, min=60, max=90, fmn=0, fmx=180, step=0.1)
        # cmds.button('rBevelLengthMax',l='+',  c='slipderMax("rBevelLength")', en = 1,bgc=[0.28,0.28,0.28])
        cmds.setParent('..')
        cmds.setParent('..')
        cmds.setParent('..')
        cmds.text(l='')
        cmds.rowColumnLayout(nc=6, cw=[(1, 10), (2, 60), (3, 60), (4, 60), (5, 60), (6, 60)])
        cmds.text(l='')
        cmds.button('InsetButton', l='Inset', en=1, c=lambda *args: self.roundInsetRun(),
                    bgc=[0.18, 0.48, 0.18])
        cmds.button('reFineButton', l='Refine', en=0, c=lambda *args: self.reFineSwtich(),
                    bgc=[0.18, 0.18, 0.18])
        cmds.button('InnerCornerEvenButton', l='Even', en=0, c=lambda *args: self.evenInnerCorner(),
                    bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetRemoveButton', l='Remove', en=0, c=lambda *args: self.roundInsetRemove(),
                    bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetCleaneButton', l='Done', en=1, c=lambda *args: self.roundInsetClean(),
                    bgc=[0.48, 0.18, 0.18])
        cmds.setParent('..')
        cmds.text(l='')
        cmds.showWindow(RoundInsetUI)

    def slipderMax(self, name):
        sliderName = name
        currentMaxV = cmds.floatSliderGrp(sliderName, q=1, max=1)
        currentMinV = cmds.floatSliderGrp(sliderName, q=1, min=1)
        cmds.floatSliderGrp(sliderName, e=1, min=currentMinV * 2, max=currentMaxV * 2)

    def roundInsetRemove(self):
        global insetFace
        global insetMesh
        global insetDataEdgeLoopList
        shape_node = cmds.listRelatives(insetMesh, shapes=True)
        source_shape = shape_node[-1]
        destination_shape = shape_node[0]
        if insetFace:
            history_nodes = cmds.listHistory(insetMesh)
            delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
            for d in delList:
                if cmds.objExists(d):
                    cmds.delete(d)
            cmds.select(insetFace)
        cmds.floatSliderGrp('rInsetV', e=1, v=0.01, min=-1, max=1, fmx=10, step=0.001)
        cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
        cmds.floatSliderGrp('rBevelRound', e=1, en=0, v=0, min=-1, max=1, step=0.001)
        if cmds.objExists('insetDataEdgeLoopListKeep'):
            cmds.delete('insetDataEdgeLoopListKeep')
        if cmds.objExists('cornerDisp'):
            cmds.setAttr('cornerDisp.creaseLevel', 0)
            cmds.delete('cornerDisp')
        if insetMesh:
            cmds.select(insetMesh)
            cmds.delete(all=1, e=1, ch=1)
            cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "facet" , 0);'
            mel.eval(cmd)
            cmds.select(insetFace)
        insetFace = ''
        insetMesh = ''
        insetDataEdgeLoopList = []
        cmds.setToolTo('Move')
        cmds.button('InsetButton', e=1, en=1, bgc=[0.18, 0.48, 0.18])
        cmds.button('reFineButton', l='Refine', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])

    def roundInsetClean(self):
        currentsel = cmds.ls(sl=1, fl=1)
        if currentsel:
            geoSel = currentsel[0].split('.')[0]
            if geoSel:
                cmds.delete(geoSel, ch=1)
        global insetFace
        global insetMesh
        if cmds.objExists("insetOffsetNod*"):
            listNode = cmds.ls("insetOffsetNod*")
            for s in listNode:
                getOldMesh = cmds.listConnections((s + '.outputGeometry'), scn=True)
                try:
                    getOldShape = cmds.listConnections((getOldMesh[0] + '.outputGeometry'), scn=True)
                    cmds.delete(getOldShape, ch=1)
                except:
                    cmds.delete(getOldMesh, ch=1)

        cleanList = (
            'insetOffsetNod*', 'roundV', 'insetOffsetV', 'insetDataEdgeLoopListKeep', 'blendOffsetNode',
            'tempLoopListKeep')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)

        cmds.floatSliderGrp('rInsetV', e=1, v=0.01, min=-1, max=1, fmx=10, step=0.001)
        cmds.floatSliderGrp('rBevelAngle', e=1, en=0, cw3=[60, 40, 0], field=True, v=80, min=60, max=90, fmn=0, fmx=180,
                            step=0.1)
        cmds.floatSliderGrp('rBevelRound', e=1, en=0, v=0, min=-1, max=1, step=0.001)
        if cmds.objExists('insetDataEdgeLoopListKeep'):
            cmds.delete('insetDataEdgeLoopListKeep')
        if cmds.objExists('cornerDisp'):
            cmds.setAttr('cornerDisp.creaseLevel', 0)
            cmds.delete('cornerDisp')
        if insetFace:
            cmds.select(insetFace)
            cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "facet", 0);'
            mel.eval(cmd)
            cmds.select(insetFace)
        insetFace = ''
        insetMesh = ''
        cmds.button('InsetButton', e=1, en=1, bgc=[0.18, 0.48, 0.18])
        cmds.button('reFineButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])
        cmds.setToolTo('Move')
        # clean storeBevel Attr
        transformsNodeList = cmds.ls(dag=1, type='transform', l=1)
        for l in transformsNodeList:
            anyUserAttr = cmds.listAttr(l, userDefined=1)
            if anyUserAttr:
                for a in anyUserAttr:
                    if a == 'storeBevelV':
                        if cmds.attributeQuery(a, node=l, ex=True):
                            cmds.setAttr((l + "." + a), l=0)
                            cmds.deleteAttr(l + "." + a)

    def evenInnerCorner(self):
        global recordInnerCornerList
        cmds.select(recordInnerCornerList)
        sortGrp = []
        sortGrp = self.getEdgeRingGroup(recordInnerCornerList)
        if len(sortGrp) > 0:
            for g in sortGrp:
                if cmds.objExists('tempEvenCurve'):
                    cmds.delete('tempEvenCurve')
                listVtx = self.vtxLoopOrder(g)
                cmds.select(g)
                cmds.polyToCurve(form=2, degree=1, conformToSmoothMeshPreview=1)
                cmds.rename('tempEvenCurve')
                curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
                posCurve = cmds.xform(curveCVs[0], a=1, ws=1, q=1, t=1)
                posEdge = cmds.xform(listVtx[0], a=1, ws=1, q=1, t=1)
                if posCurve == posEdge:
                    pass
                else:
                    listVtx = listVtx[::-1]
                if len(curveCVs) > 2:
                    cmds.rebuildCurve('tempEvenCurve', ch=1, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0, s=0, d=1,
                                      tol=0)
                    if len(curveCVs) < 4:
                        cmds.delete('tempEvenCurve.cv[1]', 'tempEvenCurve.cv[3]')
                        curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
                    posCurve = cmds.xform(curveCVs[0], a=1, ws=1, q=1, t=1)
                    posEdge = cmds.xform(listVtx[0], a=1, ws=1, q=1, t=1)
                    posEdge[0] = round(posEdge[0], 3)
                    posEdge[1] = round(posEdge[1], 3)
                    posEdge[2] = round(posEdge[2], 3)
                    posCurve[0] = round(posCurve[0], 3)
                    posCurve[1] = round(posCurve[1], 3)
                    posCurve[2] = round(posCurve[2], 3)
                for i in range(len(curveCVs)):
                    pos = cmds.xform(curveCVs[i], a=1, ws=1, q=1, t=1)
                    cmds.xform(listVtx[i], a=1, ws=1, t=(pos[0], pos[1], pos[2]))
                cmds.delete('tempEvenCurve')
            cmds.select('cornerDisp')
            cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
            mel.eval(cmd)
            cmds.select(insetFace, add=1)
            cmds.setToolTo('selectSuperContext')

    def matchCorner(self, edgeLoop, getRoundV):
        global insetFace
        global insetInnerEdges
        global insetDataEdgeLoopList
        selLoopShort = edgeLoop
        toCV = cmds.polyListComponentConversion(selLoopShort, tv=True)
        toEdge = cmds.polyListComponentConversion(toCV, te=True)
        toEdge = cmds.ls(toEdge, fl=1)
        toFace = cmds.polyListComponentConversion(selLoopShort, tf=True)
        toFace = cmds.ls(toFace, fl=1)
        toFace = list(set(toFace) - set(insetFace))
        toEdgeB = cmds.polyListComponentConversion(toFace, te=True)
        toEdgeB = cmds.ls(toEdgeB, fl=1)
        selLoopLong = list(set(toEdgeB) - set(toEdge))
        totalLengthA = 0
        for s in selLoopLong:
            intSelCV = cmds.polyListComponentConversion(s, tv=True)
            intSelCV = cmds.ls(intSelCV, fl=1)
            distanceX = self.distanceBetween(intSelCV[0], intSelCV[1])
            totalLengthA = totalLengthA + distanceX
        totalLengthB = 0
        for s in selLoopShort:
            intSelCV = cmds.polyListComponentConversion(s, tv=True)
            intSelCV = cmds.ls(intSelCV, fl=1)
            distanceX = self.distanceBetween(intSelCV[0], intSelCV[1])
            totalLengthB = totalLengthB + distanceX
        scaleV = totalLengthA / totalLengthB * getRoundV
        # cmds.select(toDO)
        toDO = list(set(toEdge) - set(toEdgeB) - set(insetInnerEdges))
        toDO = toDO + selLoopShort
        toDO = list(set(toDO))
        if len(insetDataEdgeLoopList) == len(toDO):
            pass
        else:
            cmds.sets(selLoopLong, forceElement="cornerDisp")
            pPoint, vList, cList = self.unBevelEdgeLoop(toDO)
            for v in vList:
                cmds.scale(scaleV, scaleV, scaleV, v, cs=1, r=1, p=(pPoint[0], pPoint[1], pPoint[2]))

    def distanceBetween(self, p1, p2):
        pA = cmds.pointPosition(p1, w=1)
        pB = cmds.pointPosition(p2, w=1)
        dist = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
        return dist

    def getEdgeRingGroup(self, selEdges):
        # selEdges = cmds.ls(sl=1,fl=1)
        trans = selEdges[0].split(".")[0]
        e2vInfos = cmds.polyInfo(selEdges, ev=True)
        e2vDict = {}
        fEdges = []
        for info in e2vInfos:
            evList = [int(i) for i in re.findall('\\d+', info)]
            e2vDict.update(dict([(evList[0], evList[1:])]))
        while True:
            try:
                startEdge, startVtxs = e2vDict.popitem()
            except:
                break
            edgesGrp = [startEdge]
            num = 0
            for vtx in startVtxs:
                curVtx = vtx
                while True:
                    nextEdges = []
                    for k in e2vDict:
                        if curVtx in e2vDict[k]:
                            nextEdges.append(k)
                    if nextEdges:
                        if len(nextEdges) == 1:
                            if num == 0:
                                edgesGrp.append(nextEdges[0])
                            else:
                                edgesGrp.insert(0, nextEdges[0])
                            nextVtxs = e2vDict[nextEdges[0]]
                            curVtx = [vtx for vtx in nextVtxs if vtx != curVtx][0]
                            e2vDict.pop(nextEdges[0])
                        else:
                            break
                    else:
                        break
                num += 1
            fEdges.append(edgesGrp)
        retEdges = []
        for f in fEdges:
            collectList = []
            for x in f:
                getCom = (trans + ".e[" + str(x) + "]")
                collectList.append(getCom)
            retEdges.append(collectList)
        return retEdges

    def unBevelEdgeLoop(self, edgelist):
        listVtx = self.vtxLoopOrder(edgelist)
        checkA = self.angleBetweenThreeP(listVtx[1], listVtx[0], listVtx[-1])
        angleA = math.degrees(checkA)
        checkB = self.angleBetweenThreeP(listVtx[-2], listVtx[-1], listVtx[0])
        angleB = math.degrees(checkB)
        angleC = 180 - angleA - angleB
        distanceC = self.distanceBetween(listVtx[0], listVtx[-1])
        # distanceA = distanceC / math.sin(math.radians(angleC)) * math.sin(math.radians(angleA))
        distanceB = distanceC / math.sin(math.radians(angleC)) * math.sin(math.radians(angleB))
        oldDistA = self.distanceBetween(listVtx[-2], listVtx[-1])
        oldDistB = self.distanceBetween(listVtx[0], listVtx[1])
        scalarB = distanceB / oldDistB
        pA = cmds.pointPosition(listVtx[0], w=1)
        pB = cmds.pointPosition(listVtx[1], w=1)
        newP = [0, 0, 0]
        newP[0] = ((pB[0] - pA[0]) * scalarB) + pA[0]
        newP[1] = ((pB[1] - pA[1]) * scalarB) + pA[1]
        newP[2] = ((pB[2] - pA[2]) * scalarB) + pA[2]
        listVtx = listVtx[1:-1]
        storeDist = []
        for l in listVtx:
            sotreXYZ = [0, 0, 0]
            p = cmds.xform(l, q=True, t=True, ws=True)
            sotreXYZ[0] = (newP[0] - p[0]) / 100
            sotreXYZ[1] = (newP[1] - p[1]) / 100
            sotreXYZ[2] = (newP[2] - p[2]) / 100
            storeDist.append(sotreXYZ)
        return newP, listVtx, storeDist

    def vtxLoopOrder(self, edgelist):
        selEdges = edgelist
        # selEdges = cmds.ls(sl=1, fl=1)
        shapeNode = cmds.listRelatives(selEdges[0], fullPath=True, parent=True)
        transformNode = cmds.listRelatives(shapeNode[0], fullPath=True, parent=True)
        edgeNumberList = []
        for a in selEdges:
            checkNumber = a.split('.')[1].split('\n')[0].split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    edgeNumberList.append(findNumber)
        getNumber = []
        for s in selEdges:
            evlist = cmds.polyInfo(s, ev=True)
            checkNumber = evlist[0].split(':')[1].split('\n')[0].split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    getNumber.append(findNumber)
        dup = set([x for x in getNumber if getNumber.count(x) > 1])
        getHeadTail = list(set(getNumber) - dup)
        checkCircleState = 0
        if not getHeadTail:
            checkCircleState = 1
            getHeadTail.append(getNumber[0])
        vftOrder = []
        vftOrder.append(getHeadTail[0])
        count = 0
        while len(dup) > 0 and count < 3000:
            checkVtx = transformNode[0] + '.vtx[' + vftOrder[-1] + ']'
            velist = cmds.polyInfo(checkVtx, ve=True)
            getNumber = []
            checkNumber = velist[0].split(':')[1].split('\n')[0].split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    getNumber.append(findNumber)
            findNextEdge = []
            for g in getNumber:
                if g in edgeNumberList:
                    findNextEdge = g
            edgeNumberList.remove(findNextEdge)
            checkVtx = transformNode[0] + '.e[' + findNextEdge + ']'
            findVtx = cmds.polyInfo(checkVtx, ev=True)
            getNumber = []
            checkNumber = findVtx[0].split(':')[1].split('\n')[0].split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    getNumber.append(findNumber)
            gotNextVtx = []
            for g in getNumber:
                if g in dup:
                    gotNextVtx = g
            dup.remove(gotNextVtx)
            vftOrder.append(gotNextVtx)
            count += 1
        if checkCircleState == 0:
            vftOrder.append(getHeadTail[1])
        elif vftOrder[0] == vftOrder[1]:
            vftOrder = vftOrder[1:]
        elif vftOrder[0] == vftOrder[-1]:
            vftOrder = vftOrder[0:-1]
        finalList = []
        for v in vftOrder:
            finalList.append(transformNode[0] + '.vtx[' + v + ']')
        return (finalList)

    def angleBetweenThreeP(self, pA, pB, pC):
        a = cmds.pointPosition(pA, w=1)
        b = cmds.pointPosition(pB, w=1)
        c = cmds.pointPosition(pC, w=1)
        ba = [aa - bb for aa, bb in zip(a, b)]
        bc = [cc - bb for cc, bb in zip(c, b)]
        nba = math.sqrt(sum((x ** 2.0 for x in ba)))
        ba = [x / nba for x in ba]
        nbc = math.sqrt(sum((x ** 2.0 for x in bc)))
        bc = [x / nbc for x in bc]
        scalar = sum((aa * bb for aa, bb in zip(ba, bc)))
        angle = math.acos(scalar)
        return angle

    def getfaceArea(self, mesh, faceId):
        if cmds.objectType(mesh) == 'transform':
            mesh = cmds.listRelatives(mesh, s=True, ni=True, pa=True)[0]
        selectionList = om.MSelectionList()
        om.MGlobal.getSelectionListByName(mesh, selectionList)
        mDagPath = om.MDagPath()
        selectionList.getDagPath(0, mDagPath)
        meshFaceIt = om.MItMeshPolygon(mDagPath)
        if faceId != None:
            meshFaceUtil = om.MScriptUtil()
            meshFacePtr = meshFaceUtil.asIntPtr()
            meshFaceIt.setIndex(faceId, meshFacePtr)
        faceArea = om.MScriptUtil()
        faceArea.createFromDouble(0.0)
        faceAreaPtr = faceArea.asDoublePtr()
        meshFaceIt.getArea(faceAreaPtr)
        areaCheck = om.MScriptUtil(faceAreaPtr).asDouble()
        return areaCheck

    def edgeLoopByAngle(self, selList):
        global edgeLoopOverLengthLib
        edgeLengthData = {}
        listVtx = self.vtxLoopOrder(selList)
        listVtx.append(listVtx[0])
        listVtx.append(listVtx[1])
        collectList = []
        for r in range(len(listVtx) - 2):
            pA = cmds.pointPosition(listVtx[r], w=True)
            pB = cmds.pointPosition(listVtx[r + 1], w=True)
            pC = cmds.pointPosition(listVtx[r + 2], w=True)
            direction_vectorA = [pA[i] - pB[i] for i in range(3)]
            lengthA = sum(y ** 2 for y in direction_vectorA) ** 0.5
            normalized_directionA = [y / lengthA for y in direction_vectorA]
            direction_vectorB = [pB[i] - pC[i] for i in range(3)]
            lengthB = sum(y ** 2 for y in direction_vectorB) ** 0.5
            normalized_directionB = [y / lengthB for y in direction_vectorB]
            dot_product = sum([normalized_directionA[z] * normalized_directionB[z] for z in range(3)])
            # checkAngle = abs(abs(dot_product) - 1.0)
            angle_degrees = math.degrees(math.acos(dot_product))
            if angle_degrees > 10:
                edgeFoundA = cmds.polyListComponentConversion(listVtx[r], listVtx[r + 1], fv=True, te=True,
                                                              internal=True)
                distA = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
                edgeFoundB = cmds.polyListComponentConversion(listVtx[r + 1], listVtx[r + 2], fv=True, te=True,
                                                              internal=True)
                distB = math.sqrt(((pB[0] - pC[0]) ** 2) + ((pB[1] - pC[1]) ** 2) + ((pB[2] - pC[2]) ** 2))
                collectList = collectList + edgeFoundA + edgeFoundB
                edgeLengthData[edgeFoundA[0]] = distA
                edgeLengthData[edgeFoundB[0]] = distB

        if collectList:
            # avoid long edge
            values = list(edgeLengthData.values())
            # Calculate the threshold for the top 20% and bottom 20%
            num_values = len(values)
            top_threshold = sorted(values)[int(0.95 * num_values)]
            bottom_threshold = sorted(values)[int(0.05 * num_values)]
            # Filter out values outside the range
            filtered_data = {key: value for key, value in edgeLengthData.items() if
                             value >= bottom_threshold and value <= top_threshold}
            filtered_values = list(filtered_data.values())
            average_length = sum(filtered_values) / len(filtered_values)
            edgeLoopOverLengthLib = 2 * average_length
            overLength = [edge for edge, length in edgeLengthData.items() if length > edgeLoopOverLengthLib]
            collectList = list(set(collectList) - set(overLength))
            return collectList

    def roundInsetRun(self):
        currentsel = cmds.ls(sl=1, fl=1)
        if currentsel:
            geoSel = currentsel[0].split('.')[0]
            if geoSel:
                cmds.delete(geoSel, ch=1)
        getRoundV = cmds.floatSliderGrp('rBevelRound', q=1, v=1)
        if cmds.objExists("insetOffsetNod*"):
            listNode = cmds.ls("insetOffsetNod*")
            for s in listNode:
                getOldMesh = cmds.listConnections((s + '.outputGeometry'), scn=True)
                try:
                    getOldShape = cmds.listConnections((getOldMesh[0] + '.outputGeometry'), scn=True)
                    cmds.delete(getOldShape, ch=1)
                except:
                    cmds.delete(getOldMesh, ch=1)
        if cmds.objExists('insetOffsetNod*'):
            cmds.delete('insetOffsetNod*')
        if cmds.objExists('roundV'):
            cmds.delete('roundV')
        if cmds.objExists("insetOffsetV"):
            cmds.delete('nsetOffsetV')
        if cmds.objExists('insetDataEdgeLoopListKeep'):
            cmds.delete('insetDataEdgeLoopListKeep')
        if cmds.objExists('cornerDisp'):
            cmds.setAttr("cornerDisp.creaseLevel", 0)
            cmds.delete('cornerDisp*')
        global insetDataPP
        global insetMesh
        global insetInnerEdges
        global insetFace
        global insetDataEdgeLoopList
        global insetFaceArea
        global newLoop
        global recordInnerCornerList
        global edgeLoopAngleLib
        global edgeLoopOverLengthLib
        global updatedNewSelEdge
        edgeLoopOverLengthLib = []
        recordInnerCornerList = []
        newLoop = []
        insetDataEdgeLoopList = []
        insetDataPP = []
        insetMesh = ''
        insetFace = ''
        insetInnerEdges = []
        insetFaceArea = 0
        selComponent = cmds.filterExpand(ex=1, sm=34)
        if selComponent:
            geo = cmds.ls(hl=1)
            cmds.makeIdentity(geo[0], apply=1, t=0, r=0, s=1, n=0, pn=1)
            insetMesh = geo[0]
            faceID = selComponent[0].split('[')[-1].split(']')[0]
            faceID = int(faceID)
            insetFaceArea = self.getfaceArea(insetMesh, faceID)
            edgeLoopCheck = cmds.polyListComponentConversion(selComponent, te=True)
            edgeLoopCheck = cmds.ls(edgeLoopCheck, fl=1)
            edgeLoopCheckInternal = cmds.polyListComponentConversion(selComponent, te=True, internal=1)
            edgeLoopCheckInternal = cmds.ls(edgeLoopCheckInternal, fl=1)
            tempCheck = []
            if edgeLoopCheckInternal:
                tempCheck = list(set(edgeLoopCheck) - set(edgeLoopCheckInternal))
            else:
                tempCheck = edgeLoopCheck
            insetDataEdgeLoopList = tempCheck
            cmds.sets(insetDataEdgeLoopList, name='insetDataEdgeLoopListKeep', text='insetDataEdgeLoopListKeep')
            cmds.setAttr('insetDataEdgeLoopListKeep.hiddenInOutliner', 1)
            if not cmds.attributeQuery('storeBevelV', node=geo[0], ex=True):
                cmds.addAttr(geo[0], ln='storeBevelV')
            cmds.setAttr((insetMesh + '.storeBevelV'), 0.01)
            cmds.polyExtrudeFacet(selComponent, constructionHistory=1, keepFacesTogether=1, divisions=1, twist=0,
                                  taper=1,
                                  offset=0.01, thickness=0, smoothingAngle=30)
            insetFace = cmds.ls(sl=1, fl=1)
            if 'Shape' in insetFace[0]:
                insetFace = insetFace[1:]
            newLoop = cmds.polyListComponentConversion(insetFace, te=True)
            newLoop = cmds.ls(newLoop, fl=1)
            newLoopInternal = cmds.polyListComponentConversion(insetFace, te=True, internal=1)
            newLoopInternal = cmds.ls(newLoopInternal, fl=1)
            newEdgeLoopCheck = []
            if newLoopInternal:
                newEdgeLoopCheck = list(set(newLoop) - set(newLoopInternal))
            else:
                newEdgeLoopCheck = newLoop
            cmds.select(cl=1)
            findCorner = []
            newLoop = newEdgeLoopCheck
            checkEdgeRingGrp = self.getEdgeRingGroup(newLoop)
            cornerLoopCollect = []
            for c in checkEdgeRingGrp:
                getList = self.edgeLoopByAngle(c)
                if getList:
                    cornerLoopCollect = cornerLoopCollect + getList
            cornerLoop = cornerLoopCollect
            recordInnerCornerList = cornerLoop
            if cmds.objExists('tempLoopListKeep'):
                updatedNewSelEdge = cmds.sets('tempLoopListKeep', q=1)
                cmds.select(updatedNewSelEdge)
                cmds.ConvertSelectionToFaces()
                cmds.ConvertSelectionToEdgePerimeter()
                tempCheckList = cmds.ls(sl=1, fl=1)
                newCorner = list(set(newLoop) & set(tempCheckList))
                cornerLoop = newCorner
                cmds.delete('tempLoopListKeep')
            insetInnerEdges = cmds.polyListComponentConversion(insetFace, te=True, internal=True)
            insetInnerEdges = cmds.ls(insetInnerEdges, fl=1)
            if cornerLoop:
                cmds.createNode('creaseSet')
                cmds.rename('cornerDisp')
                cmds.setAttr("cornerDisp.creaseLevel", 1)
                cmds.setAttr('cornerDisp.hiddenInOutliner', 1)
                # cmds.select(cornerLoop)
                cornerLoopVtx = cmds.polyListComponentConversion(cornerLoop, tv=True)
                cornerLoopVtx = cmds.ls(cornerLoopVtx, fl=1)
                sortGrp = []
                sortGrp = self.getEdgeRingGroup(cornerLoop)
                if len(sortGrp) > 0:  # need a method to check loop number = protect corner number
                    ################ BUG #######################
                    for g in sortGrp:
                        self.matchCorner(g, 1)
                    point_positions = {}
                    for n in cornerLoopVtx:
                        vertex_position = cmds.pointPosition(n, w=True)
                        point_positions[n] = vertex_position

                    for g in sortGrp:
                        self.matchCorner(g, 1.3)
                    newRoundMesh = cmds.duplicate(insetMesh, rr=1)
                    cmds.rename(newRoundMesh, 'roundV')

                    for point_name, new_position in point_positions.items():
                        cmds.xform(point_name, translation=new_position, worldSpace=True)

                    ##################################################################
                    innerCVList = cmds.polyListComponentConversion(cornerLoop, tv=True)
                    innerCVList = cmds.ls(innerCVList, fl=1)
                    edgeBorderFaceA = cmds.polyListComponentConversion(newLoop, tf=True)
                    edgeBorderFaceA = cmds.ls(edgeBorderFaceA, fl=1)
                    insetDataEdgeLoopList = cmds.sets("insetDataEdgeLoopListKeep", q=True)
                    edgeBorderFaceB = cmds.polyListComponentConversion(insetDataEdgeLoopList, tf=True)
                    edgeBorderFaceB = cmds.ls(edgeBorderFaceB, fl=1)
                    setA = set(edgeBorderFaceA)
                    setB = set(edgeBorderFaceB)
                    edgeBorderFace = list(setA.intersection(setB))
                    findRingList = cmds.polyListComponentConversion(edgeBorderFace, te=True, internal=True)
                    loopRingList = cmds.ls(findRingList, fl=1)
                    insetDataPP = []
                    moveP = []
                    baseP = []
                    checkCV = cmds.polyListComponentConversion(loopRingList[0], tv=True)
                    checkCV = cmds.ls(checkCV, fl=1)
                    bevelDistance = self.distanceBetween(checkCV[0], checkCV[-1])
                    for r in loopRingList:
                        checkCV = cmds.polyListComponentConversion(r, tv=True)
                        checkCV = cmds.ls(checkCV, fl=1)
                        if checkCV[0] in innerCVList:
                            moveP = checkCV[0]
                            baseP = checkCV[1]
                        else:
                            moveP = checkCV[1]
                            baseP = checkCV[0]
                        basePPos = cmds.pointPosition(baseP, w=1)
                        movePPos = cmds.pointPosition(moveP, w=1)
                        dataCollect = [moveP, basePPos, movePPos]
                        insetDataPP.append(dataCollect)
                    newMesh = cmds.duplicate(insetMesh, rr=1)
                    cmds.rename(newMesh, 'insetOffsetV')
                    refBevelV = math.sqrt(insetFaceArea) * 4
                    for v in range(len(insetDataPP)):
                        currentPos = cmds.pointPosition(insetDataPP[v][0], w=1)
                        posX = ((currentPos[0] - insetDataPP[v][1][0]) * (refBevelV)) + insetDataPP[v][1][0]
                        posY = ((currentPos[1] - insetDataPP[v][1][1]) * (refBevelV)) + insetDataPP[v][1][1]
                        posZ = ((currentPos[2] - insetDataPP[v][1][2]) * (refBevelV)) + insetDataPP[v][1][2]
                        cmds.move(posX, posY, posZ, insetDataPP[v][0].replace(insetMesh, 'insetOffsetV'), a=True,
                                  ws=True)
                    # cmds.delete(insetMesh, ch=1)
                    blendName = cmds.blendShape('insetOffsetV', 'roundV', insetMesh)
                    cmds.delete('insetOffsetV', 'roundV')
                    cmds.rename(blendName, 'insetOffsetNode')
                    cmds.setAttr("insetOffsetNode.envelope", 2)
                    if cmds.objExists('blendOffsetNode') == 0:
                        cmds.group(em=1, n='blendOffsetNode')
                        cmds.addAttr('blendOffsetNode', longName='offset', attributeType='double', defaultValue=0)
                        cmds.setAttr('blendOffsetNode.offset', keyable=True)
                        cmds.setAttr('blendOffsetNode.hiddenInOutliner', 1)
                        cmds.connectControl('rInsetV', 'blendOffsetNode.offset')
                    cmds.connectAttr('blendOffsetNode.offset', 'insetOffsetNode.insetOffsetV', force=True)
                    cmds.connectControl('rBevelRound', 'insetOffsetNode.roundV')
                    cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
                    cmds.floatSliderGrp('rBevelRound', e=1, en=1, v=0)
                    cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
                    cmds.button('reFineButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
                    cmds.button('InsetRemoveButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
                    cmds.button('InsetCleaneButton', e=1, en=1)
                    cmds.button('InnerCornerEvenButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
                    cmds.select(cl=1)
                    cmds.select('cornerDisp')
                    cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
                    mel.eval(cmd)
                    cmds.select(insetFace, add=1)
            outliner_editor = 'outlinerPanel1'
            cmds.outlinerEditor(outliner_editor, e=1, refresh=True)

    def reFineSwtich(self):
        cmds.floatSliderGrp('rBevelAngle', e=1, en=1)
        cmds.floatSliderGrp('rInsetV', e=1, en=0)
        cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('reFineButton', l='update', e=1, en=1, bgc=[0.18, 0.48, 0.18], c='reFineMySelect()')
        cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])
        self.reviewProtectCorner()
        self.edgeLoopByAngleUpdate()
        self.rBevelAngleUpdate()
        cmds.select('cornerDisp')
        cmds.setAttr('cornerDisp.creaseLevel', 1)
        cmds.scriptJob(event=["SelectionChanged", self.updateSelToCrease])
        cmds.scriptJob(uiDeleted=["RoundInsetUI", self.RoundInsetScriptJobClean])

    def edgeLoopByAngleUpdate(self):
        global insetDataEdgeLoopList
        global edgeLoopAngleLib
        global edgeLoopOverLengthLib
        insetDataEdgeLoopList = cmds.sets("insetDataEdgeLoopListKeep", q=True)
        edgeLoopAngleLib = {}
        sortGrp = self.getEdgeRingGroup(insetDataEdgeLoopList)
        for s in sortGrp:
            listVtx = self.vtxLoopOrder(s)
            listVtx.append(listVtx[0])
            listVtx.append(listVtx[1])
            for r in range(len(listVtx) - 2):
                pA = cmds.pointPosition(listVtx[r], w=True)
                pB = cmds.pointPosition(listVtx[r + 1], w=True)
                pC = cmds.pointPosition(listVtx[r + 2], w=True)
                edgeFoundA = cmds.polyListComponentConversion(listVtx[r], listVtx[r + 1], fv=True, te=True,
                                                              internal=True)
                distA = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
                edgeFoundB = cmds.polyListComponentConversion(listVtx[r + 1], listVtx[r + 2], fv=True, te=True,
                                                              internal=True)
                distB = math.sqrt(((pB[0] - pC[0]) ** 2) + ((pB[1] - pC[1]) ** 2) + ((pB[2] - pC[2]) ** 2))
                direction_vectorA = [pA[i] - pB[i] for i in range(3)]
                lengthA = sum(y ** 2 for y in direction_vectorA) ** 0.5
                normalized_directionA = [y / lengthA for y in direction_vectorA]
                direction_vectorB = [pB[i] - pC[i] for i in range(3)]
                lengthB = sum(y ** 2 for y in direction_vectorB) ** 0.5
                normalized_directionB = [y / lengthB for y in direction_vectorB]
                dot_product = sum([normalized_directionA[z] * normalized_directionB[z] for z in range(3)])
                angle_degrees = math.degrees(math.acos(dot_product))
                rounded_angle = round(angle_degrees, 4) + ((r + 1) * 0.0001)
                edgeFound = []
                edgeFound = [edgeFoundA[0], edgeFoundB[0]]
                if distA > edgeLoopOverLengthLib * 2:
                    edgeFound.remove(edgeFoundA[0])
                if distB > edgeLoopOverLengthLib * 2:
                    edgeFound.remove(edgeFoundB[0])
                if edgeFound:
                    edgeLoopAngleLib[edgeFound[0]] = rounded_angle

    def reviewProtectCorner(self):
        global insetFace
        global insetMesh
        shape_node = cmds.listRelatives(insetMesh, shapes=True)
        source_shape = shape_node[-1]
        destination_shape = shape_node[0]
        if insetFace:
            history_nodes = cmds.listHistory(insetMesh)
            delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
            for d in delList:
                if cmds.objExists(d):
                    cmds.delete(d)
        cmds.select(cl=1)

    def rBevelAngleUpdate(self):
        currentList = cmds.ls(sl=1, fl=1)
        global edgeLoopAngleLib
        checkListAA = []
        newV = cmds.floatSliderGrp('rBevelAngle', q=1, v=1)
        toCheck = 90 - newV
        overLength = [edge for edge, value in edgeLoopAngleLib.items() if value > toCheck]
        newList = list(set(overLength))
        if currentList != newList:
            cmds.select(newList, r=1)
            cmds.sets(clear="cornerDisp")
            cmds.sets(newList, forceElement="cornerDisp")

    def updateSelToCrease(self):
        updateList = cmds.ls(sl=1, fl=1)
        cmds.sets(clear="cornerDisp")
        cmds.sets(updateList, forceElement="cornerDisp")

    def RoundInsetScriptJobClean(self):
        foundError = 1
        while foundError > 0:
            jobs = cmds.scriptJob(listJobs=True)
            foundError = 0
            for j in jobs:
                if "updateSelTo" in j:
                    jID = j.split(':')[0]
                    try:
                        cmds.scriptJob(kill=int(jID))
                    except:
                        foundError = 1

    def reFineMySelect(self):
        updatedNewSelEdge = cmds.filterExpand(ex=1, sm=32)
        cmds.sets(updatedNewSelEdge, name='tempLoopListKeep', text='tempLoopListKeep')
        cmds.setAttr('tempLoopListKeep.hiddenInOutliner', 0)
        self.RoundInsetScriptJobClean()
        global insetFace
        global insetMesh
        global insetDataEdgeLoopList
        insetDataEdgeLoopList = []
        getRoundV = cmds.floatSliderGrp('rBevelRound', q=1, v=1)
        getInsetV = cmds.floatSliderGrp('rInsetV', q=1, v=1)
        shape_node = cmds.listRelatives(insetMesh, shapes=True)
        source_shape = shape_node[-1]
        destination_shape = shape_node[0]
        if insetFace:
            history_nodes = cmds.listHistory(insetMesh)
            delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
            for d in delList:
                if cmds.objExists(d):
                    cmds.delete(d)
            cmds.select(insetFace)
        if cmds.objExists('insetDataEdgeLoopListKeep'):
            cmds.delete('insetDataEdgeLoopListKeep')
        if cmds.objExists('cornerDisp'):
            cmds.setAttr('cornerDisp.creaseLevel', 0)
            cmds.delete('cornerDisp')
        self.roundInsetRun()
        cmds.setAttr('blendOffsetNode.offset', getInsetV)
        # cmds.setAttr('insetOffsetNode.roundV', getRoundV)
        # cmds.select('cornerDisp')
        cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
        mel.eval(cmd)
        cmds.select(insetFace, add=1)
        cmds.setToolTo('selectSuperContext')
        cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
        cmds.button('reFineButton', l='Refine', e=1, en=1, c='reFineSwtich()', bgc=[0.28, 0.18, 0.38])
        cmds.button('InsetRemoveButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
        cmds.button('InsetCleaneButton', e=1, en=1)
        cmds.button('InnerCornerEvenButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
        cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
        cmds.floatSliderGrp('rInsetV', e=1, en=1)


class Snap_2_Plane:
    def get_selected_points(self):
        selection = cmds.ls(selection=True, flatten=True)
        for mask in [31, 28, 30]:
            verts = cmds.filterExpand(selection, selectionMask=mask)
            if verts:
                return verts
        return []

    def plane_normal(self, vtx1, vtx2, vtx3):
        v1 = vtx2 - vtx1
        v2 = vtx3 - vtx1
        normal = v1 ^ v2
        normal.normalize()
        return normal

    def plane_dist(self, normal, vtx):
        return -(normal * vtx)

    def plane_vtx_dist(self, vtx, normal, plane_vtx):
        D = self.plane_dist(normal, plane_vtx)
        return normal * vtx + D

    def define_plane(self, *args):
        verts = self.get_selected_points()
        if len(verts) != 3:
            if cmds.objExists(self.info_text):
                lj_warn(u"选择三个点定义平面!")
            lj_warn(u"选择三个点定义一个平面!")
            return
        for i in range(3):
            pos = cmds.pointPosition(verts[i])
            self.m_vPlane[i] = om.MVector(*pos)
        if cmds.objExists(self.info_text):
            lj_info(u"平面已定义!")
        lj_info(u"平面已定义!")
        
    def snap_plane_cmd(self, *args):
        verts = self.get_selected_points()
        if not verts:
            lj_warn("No points selected to snap.")
            return
        normal = self.plane_normal(self.m_vPlane[0], self.m_vPlane[1], self.m_vPlane[2])
        for vert in verts:
            pos = cmds.pointPosition(vert)
            vert_pos = om.MVector(*pos)
            dist = self.plane_vtx_dist(vert_pos, normal, self.m_vPlane[0])
            move_vector = normal * (-dist)
            cmds.move(move_vector.x, move_vector.y, move_vector.z, vert, relative=True)

    def create_plane_ui(self):
        self.window = "SnapToPlaneWindow"
        self.info_text = "SnapToPlaneInfoText"

        if cmds.window(self.window, exists=True):
            cmds.deleteUI(self.window)

        self.m_vPlane = [om.MVector(), om.MVector(), om.MVector()]
        cmds.window(self.window, title="Snap to Plane")
        cmds.columnLayout(adjustableColumn=True)
        cmds.button(label=u"定义平面", w=100,command=self.define_plane, bgc=(0.31, 0.32, 0.35))
        cmds.button(label=u"吸附到平面", w=100, command=self.snap_plane_cmd, bgc=(0.31, 0.32, 0.35))
        cmds.showWindow()


# ---------------------------------------------------------------------------
# Checker / Flow / Chrome materials for UV checking. Python 3 port of the
# "Checker And Flow Material" part of PolyRaven's UV_checker.mel (Maya 2022+).
# Embedded into LingJuGeometryTools: the tool's UV layout drives
# uv_checker_create_shader() / uv_checker_cycle_shader() directly.
# ---------------------------------------------------------------------------

UV_CHECKER_SG = "prUV_checkerSG"
UV_FLOW_SG = "prUV_flowSG"
UV_CHROME_SG = "prUV_ChromeSG"

# Nodes that more than one function reaches for; keep the names in one place.
UV_CHECKER_MTL = "prUV_checkerMTL"           # the checker blinn
UV_CHECKER_GRID = "prUV_uvchecker_edgegrid"  # the yellow block outline
UV_CHECKER_RES = "prUV_CheckerRes"           # resolution / checker1 / checker2 -> repeats
UV_CHECKER_MD = "prUV_checkerMD"             # half of UV_CHECKER_RES, drives the small rows

UV_FLOW_MTL = "prUV_flowMTL"                 # the flow blinn
UV_FLOW_GRID = "prUV_flowEdgeGrid"           # the yellow arrow outline
UV_FLOW_LAYERS = "prUV_layeredTexture1"      # arrow ramp, re-layered by the updater

UV_DEFAULT_RESOLUTION = 2048
UV_DEFAULT_CHECKER_1 = 512
UV_DEFAULT_CHECKER_2 = 32


# ---------------------------------------------------------------- viewport

def _uv_change_viewport():
    """Turn on shaded + textured display for the focused model panel."""
    panel = cmds.getPanel(withFocus=True)
    model_panels = cmds.getPanel(type="modelPanel") or []
    if panel in model_panels:
        cmds.modelEditor(panel, edit=True,
                         useDefaultMaterial=False,
                         displayAppearance="smoothShaded",
                         displayTextures=True)


# ----------------------------------------------------------------- helpers

def _uv_set_remap_hsv(node, hue, saturation, value):
    """Set the three remapHsv channels from (position, floatValue, interp) points."""
    for channel, points in (("h", hue), ("s", saturation), ("v", value)):
        for index, point in enumerate(points):
            cmds.setAttr("{0}.{1}[{2}]".format(node, channel, index),
                         *point, type="float3")


def _uv_make_sets(name):
    """Create a renderable, empty shading group; leave an existing one alone."""
    if cmds.objExists(name):
        return
    cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name)
    cmds.setAttr(name + ".isHistoricallyInteresting", 0)
    cmds.setAttr(name + ".renderableOnlySet", True)


# ------------------------------------------------------------- chrome pass

def _uv_create_chrome_shader():
    if cmds.objExists(UV_CHROME_SG):
        return

    _uv_make_sets(UV_CHROME_SG)

    cmds.shadingNode("blinn", asShader=True, name="prUV_ChromeMTL", skipSelect=True)
    cmds.setAttr("prUV_ChromeMTL.specularColor", 0.8, 0.8, 0.8, type="float3")
    cmds.setAttr("prUV_ChromeMTL.color", 0, 0, 0, type="float3")
    cmds.setAttr("prUV_ChromeMTL.eccentricity", 0.35)
    cmds.setAttr("prUV_ChromeMTL.reflectivity", 0.15)

    cmds.shadingNode("remapHsv", asUtility=True, name="prUV_ChromeHSV", skipSelect=True)
    _uv_set_remap_hsv("prUV_ChromeHSV",
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0, 1), (1, 0.56, 1)],
                      [(0, 0, 1), (1, 1, 1)])

    cmds.shadingNode("place3dTexture", asUtility=True, name="prUV_Chrome_P3DT", skipSelect=True)
    cmds.setAttr("prUV_Chrome_P3DT.overrideEnabled", 1)
    cmds.setAttr("prUV_Chrome_P3DT.overrideDisplayType", 2)
    cmds.setAttr("prUV_Chrome_P3DT.hiddenInOutliner", True)
    cmds.setAttr("prUV_Chrome_P3DT.visibility", 0)

    cmds.shadingNode("envSky", asTexture=True, name="prUV_ChromeSKY", skipSelect=True)
    cmds.setAttr("prUV_ChromeSKY.sunBrightness", 0.78321677, 0.78321677, 0.78321677,
                 type="float3")
    cmds.setAttr("prUV_ChromeSKY.blur", 4.7552447319030762)
    cmds.setAttr("prUV_ChromeSKY.skyBrightness", 1, 1, 1, type="float3")
    cmds.setAttr("prUV_ChromeSKY.hasFloor", False)

    cmds.connectAttr("prUV_ChromeMTL.outColor", UV_CHROME_SG + ".surfaceShader", force=True)
    cmds.connectAttr("prUV_Chrome_P3DT.worldInverseMatrix[0]", "prUV_ChromeSKY.placementMatrix")
    cmds.connectAttr("prUV_ChromeHSV.outColor", "prUV_ChromeMTL.reflectedColor")
    cmds.connectAttr("prUV_ChromeSKY.outColor", "prUV_ChromeHSV.color")


# ------------------------------------------------------ resolution digits

UV_TEXT_PREFIX = "prUV_flowTxt"
UV_TEXT_BLEND = UV_TEXT_PREFIX + "_blend"

# Label geometry of the flow arrow grid: one label inside the base of each of
# the 64x64 tiny triangles. The big label row on the 0.25-UV cube faces was
# dropped: it was the largest piece of the flow shader graph, and Viewport 2.0
# paid for it with a long compile every time the material was first drawn.
UV_TEXT_SMALL_CELL_MAX_WIDTH = 0.095
UV_TEXT_SMALL_CELL_FIT = 0.55
UV_TEXT_SMALL_START_V = 0.015
UV_TEXT_SMALL_THICKNESS = 0.24  # strokes of the small label, as a fraction of its cell

# 7-segment layout: a = top, g = middle, d = bottom,
# f/b = upper left/right, e/c = lower left/right.
_UV_SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcfgd",
}

_UV_DIGIT_ASPECT = 1.85        # cell height / cell width
_UV_CORNER_GAP = 0.4           # gap between neighbouring segment tips, in thicknesses
_UV_CELL_GAP = 0.35            # gap between digit cells, in cell widths
UV_TEXT_COLOR = (0.35, 0.65, 0.35)  # RGB of the resolution digits, composited additively
_UV_MASK_THRESHOLD = 0.5       # a mask counts as "on" above this sum

# Prefix of the checker watermark built by earlier versions of this tool. It is
# only kept so create/update can delete those nodes from a scene that still
# carries them; no checker watermark is built any more.
UV_CHECKER_TEXT_PREFIX = "prUV_chkTxt"


def _uv_grey_ramp(name, ramp_type, stops, linear, uv_source):
    """Greyscale ramp texture driven by a place2dTexture's tile space."""
    node = cmds.shadingNode("ramp", asTexture=True, name=name, skipSelect=True)
    cmds.setAttr(node + ".type", ramp_type)  # 0 = V, 1 = U
    cmds.setAttr(node + ".interpolation", 1 if linear else 0)
    for index, (position, value) in enumerate(stops):
        cmds.setAttr("{0}.colorEntryList[{1}].position".format(node, index), position)
        cmds.setAttr("{0}.colorEntryList[{1}].color".format(node, index),
                     value, value, value, type="float3")
    cmds.connectAttr(uv_source + ".outUV", node + ".uvCoord")
    cmds.connectAttr(uv_source + ".outUvFilterSize", node + ".uvFilterSize")
    return node


def _uv_band(name, axis, low, high):
    """Hard-edged band: 1 inside [low, high], 0 outside.

    `axis` is a per-pixel coordinate (see `_uv_digit_row`), so the mask is just
    `axis >= low` and `axis <= high`: two `condition` nodes and one
    `multiplyDivide`, and no texture sampler at all. Building the same mask as a
    hard ramp would need fewer nodes but one more ramp, and Viewport 2.0 refuses
    to compile a material that binds more than fifteen textures, so keeping the
    texture count down wins over keeping the node count down.
    """
    return _uv_mult(name, _uv_cond(name + "lo", axis, 3, low),
                    _uv_cond(name + "hi", axis, 5, high))


def _uv_pma(name, operation, inputs):
    """plusMinusAverage on float3 plugs / constants (1 = sum, 2 = subtract)."""
    node = cmds.createNode("plusMinusAverage", name=name, skipSelect=True)
    cmds.setAttr(node + ".operation", operation)
    for index, source in enumerate(inputs):
        plug = "{0}.input3D[{1}]".format(node, index)
        if isinstance(source, str):
            cmds.connectAttr(source, plug)
        else:
            cmds.setAttr(plug, source, source, source, type="float3")
    return node


def _uv_cond(name, first, operation, second, true_color=(1.0, 1.0, 1.0),
             false_color=(0.0, 0.0, 0.0)):
    """Mask where `first operation second`; operation 3 = >=, 5 = <=.

    The two colours let a caller turn the comparison straight into a colour
    factor, as the checker watermark does; every other caller keeps the plain
    white / black pair so the result reads as a multiplier.
    """
    cond = cmds.createNode("condition", name=name, skipSelect=True)
    cmds.setAttr(cond + ".operation", operation)
    cmds.setAttr(cond + ".secondTerm", second)
    cmds.setAttr(cond + ".colorIfTrue", *true_color, type="double3")
    cmds.setAttr(cond + ".colorIfFalse", *false_color, type="double3")
    cmds.connectAttr(first, cond + ".firstTerm")
    return cond + ".outColor"


def _uv_mult(name, *sources):
    result = sources[0]
    for index, source in enumerate(sources[1:]):
        node = cmds.createNode("multiplyDivide", name="{0}{1}".format(name, index), skipSelect=True)
        cmds.setAttr(node + ".operation", 1)
        cmds.connectAttr(result, node + ".input1")
        cmds.connectAttr(source, node + ".input2")
        result = node + ".output"
    return result


def _uv_digit_row(prefix, text, uv_source, cell_width, start_v, thickness):
    """Masks drawing `text` as 7-segment digits inside one tile of `uv_source`.

    Every mask lives in the tile space of the given place2dTexture, so the same
    layout can be stamped at any scale just by changing the cell size. Every
    band and column is built once per row and shared by all digits and segments.
    The only ramps of a row are the two coordinates (`ug` / `vg`): Viewport 2.0
    refuses to compile a material that binds more than fifteen textures, so
    every other mask is spelled out with condition nodes instead of one ramp
    per band.
    """
    count = len(text)
    cell_height = cell_width * _UV_DIGIT_ASPECT
    gap = cell_width * _UV_CELL_GAP
    thick = cell_width * thickness
    half = thick / 2.0
    corner = thick * _UV_CORNER_GAP
    start_u = 0.5 - (count * cell_width + gap * (count - 1)) / 2.0
    middle_v = start_v + cell_height / 2.0

    # Per-pixel u / v of the tile: a bare place2dTexture output is treated as a
    # constant by Viewport 2.0, a ramp of it is not.
    grad_u = _uv_grey_ramp(prefix + "_ug", 1, [(0.0, 0.0), (1.0, 1.0)], True, uv_source)
    grad_v = _uv_grey_ramp(prefix + "_vg", 0, [(0.0, 0.0), (1.0, 1.0)], True, uv_source)
    u_lum = grad_u + ".outColorR"
    v_lum = grad_v + ".outColorR"

    # Absolute v spans: bottom bar (d), lower bars (e/c), middle bar (g),
    # upper bars (f/b) and top bar (a).
    bands = {}
    for group, low, high in (
            ("bottom", start_v, start_v + thick),
            ("lower", start_v + thick, middle_v - half),
            ("middle", middle_v - half, middle_v + half),
            ("upper", middle_v + half, start_v + cell_height - thick),
            ("top", start_v + cell_height - thick, start_v + cell_height)):
        bands[group] = _uv_band(prefix + "_b" + group, v_lum, low, high)

    # v extent of the lower and upper vertical bars, in every digit.
    spans = {}
    for group, low, high in (
            ("lower", start_v + half + corner, middle_v - corner),
            ("upper", middle_v + corner, start_v + cell_height - half - corner)):
        spans[group] = _uv_mult(prefix + "_s" + group,
                                _uv_cond(prefix + "_s" + group + "lo", v_lum, 3, low),
                                _uv_cond(prefix + "_s" + group + "hi", v_lum, 5, high))

    # Every lower / upper bar of a row shares its vertical extent, so fold the
    # band into the span once and let each segment only add its own column.
    vbars = dict((group, _uv_mult(prefix + "_vb" + group, bands[group], spans[group]))
                 for group in ("lower", "upper"))

    # u extent of the vertical bars, and of the inner span of the horizontal
    # bars, for every digit.
    columns = []
    inner = []
    for index in range(count):
        u0 = start_u + index * (cell_width + gap)
        columns.append((_uv_band(prefix + "_cl{0}".format(index), u_lum,
                                 u0, u0 + thick),
                        _uv_band(prefix + "_cr{0}".format(index), u_lum,
                                 u0 + cell_width - thick, u0 + cell_width)))
        inner.append(_uv_mult(prefix + "_h{0}".format(index),
                              _uv_cond(prefix + "_h{0}lo".format(index), u_lum, 3, u0 + corner),
                              _uv_cond(prefix + "_h{0}hi".format(index), u_lum, 5,
                                       u0 + cell_width - corner)))

    masks = []
    for index, digit in enumerate(text):
        cell = "{0}_d{1}".format(prefix, index)
        for segment, group in (("a", "top"), ("g", "middle"), ("d", "bottom")):
            if segment in _UV_SEGMENTS[digit]:
                masks.append(_uv_mult(cell + segment + "m", bands[group], inner[index]))
        for segment, group, side in (("f", "upper", 0), ("b", "upper", 1),
                                     ("e", "lower", 0), ("c", "lower", 1)):
            if segment in _UV_SEGMENTS[digit]:
                masks.append(_uv_mult(cell + segment + "m", vbars[group],
                                      columns[index][side]))
    return masks


def _uv_clean_flow_layered_texture():
    """Rebuild the specular layered texture without empty layers.

    Viewport 2.0 refuses to compile the flow material when the arrows and ramp
    live on layers 3/4 while layers 0-2 are empty, so keep only the two used
    layers at index 0 and 1.
    """
    if not cmds.objExists(UV_FLOW_LAYERS + ".inputs[3].color") \
            or not cmds.objExists(UV_FLOW_LAYERS + ".inputs[4].color"):
        return
    sources = []
    for index in (3, 4):
        source = cmds.listConnections("{0}.inputs[{1}].color".format(UV_FLOW_LAYERS, index),
                                      source=True, destination=False,
                                      plugs=True) or []
        sources.append(source[0] if source else None)
    if not all(sources):
        return

    cmds.delete(UV_FLOW_LAYERS)
    cmds.shadingNode("layeredTexture", asTexture=True, name=UV_FLOW_LAYERS, skipSelect=True)
    for index, (blend_mode, source) in enumerate(zip((8, 1), sources)):
        cmds.setAttr("{0}.inputs[{1}].alpha".format(UV_FLOW_LAYERS, index), 1)
        cmds.setAttr("{0}.inputs[{1}].blendMode".format(UV_FLOW_LAYERS, index), blend_mode)
        cmds.setAttr("{0}.inputs[{1}].isVisible".format(UV_FLOW_LAYERS, index), True)
        cmds.connectAttr(source, "{0}.inputs[{1}].color".format(UV_FLOW_LAYERS, index))
    cmds.connectAttr(UV_FLOW_LAYERS + ".outColor", UV_FLOW_MTL + ".specularColor", force=True)
    if cmds.objExists(UV_FLOW_GRID):
        cmds.connectAttr(UV_FLOW_LAYERS + ".outColor", UV_FLOW_GRID + ".fillerColor", force=True)


def _uv_clear_digit_network(prefix, material, fallback):
    """Delete a digit-row network and hand `material.color` back to `fallback`."""
    nodes = cmds.ls(prefix + "*") or []
    if nodes:
        cmds.delete(nodes)

    # The composite node owned the material colour; hand it back to the grid.
    if cmds.objExists(material) and cmds.objExists(fallback):
        sources = cmds.listConnections(material + ".color",
                                       source=True, destination=False) or []
        if not sources:
            cmds.connectAttr(fallback, material + ".color", force=True)


def _uv_build_text_network(resolution):
    """Stamp the resolution at the base of the small arrow grid of the flow material.

    The flow material tiles 64x64 small arrows off `prUV_p2d6`; the label is
    scaled down so that it fits inside the base of each tiny triangle. The big
    label row that used to sit on the 0.25-UV cube faces is gone: it was the
    largest piece of the flow shader graph and only cost Viewport 2.0 compile
    time.
    """
    if not cmds.objExists("prUV_p2d6"):
        return

    text = str(int(round(float(resolution))))
    count = len(text)
    sum_node = _uv_pma(UV_TEXT_PREFIX + "_sum", 1, _uv_digit_row(
        UV_TEXT_PREFIX, text, "prUV_p2d6",
        cell_width=min(UV_TEXT_SMALL_CELL_MAX_WIDTH,
                       UV_TEXT_SMALL_CELL_FIT / (count + _UV_CELL_GAP * (count - 1))),
        start_v=UV_TEXT_SMALL_START_V,
        thickness=UV_TEXT_SMALL_THICKNESS))
    white = _uv_cond(UV_TEXT_PREFIX + "_white", sum_node + ".output3Dx", 3,
                     _UV_MASK_THRESHOLD, true_color=UV_TEXT_COLOR)

    # Composite the digits over the flow texture with a dedicated two-layer
    # texture, leaving the original arrow/ramp layers untouched.
    comp = cmds.shadingNode("layeredTexture", asTexture=True, name=UV_TEXT_BLEND, skipSelect=True)
    cmds.setAttr(comp + ".inputs[0].alpha", 1)
    cmds.setAttr(comp + ".inputs[0].blendMode", 4)   # Add
    cmds.setAttr(comp + ".inputs[0].isVisible", True)
    cmds.connectAttr(white, comp + ".inputs[0].color")
    cmds.setAttr(comp + ".inputs[1].alpha", 1)
    cmds.setAttr(comp + ".inputs[1].blendMode", 1)   # Over
    cmds.setAttr(comp + ".inputs[1].isVisible", True)
    cmds.connectAttr(UV_FLOW_GRID + ".outColor", comp + ".inputs[1].color")
    cmds.connectAttr(comp + ".outColor", UV_FLOW_MTL + ".color", force=True)


# --------------------------------------------------- checker / flow shaders

# Digits of the last built flow text network. A rebuild is only needed when the
# digit text itself changes (the network geometry depends on it, not on the raw
# resolution values), so repeated clicks with identical values are a no-op.
_UV_LAST_DIGIT_TEXTS = {"res": None}


def _uv_restore_selection(selection):
    if selection:
        cmds.select(selection, replace=True)
    else:
        cmds.select(clear=True)


def _uv_build_shader(resolution, checker1, checker2):
    """Build the node network; `uv_checker_create_shader` handles undo and refresh."""
    _uv_change_viewport()
    selection = cmds.ls(selection=True) or []

    if cmds.objExists(UV_CHECKER_SG):
        if not cmds.objExists(UV_CHECKER_RES):
            lj_error("Shader can not be created, because there are some possibly damaged nodes.\n"
                     "Assign Lambert and \"Delete Unused Nodes\", if you find it safe.\n"
                     "Afterwards you shall try to create the shader again.")
            return
        cmds.setAttr(UV_CHECKER_RES + ".input1", resolution, resolution, resolution,
                     type="double3")
        cmds.setAttr(UV_CHECKER_RES + ".input2Y", checker2)
        cmds.setAttr(UV_CHECKER_RES + ".input2Z", checker1)
        _uv_clean_flow_layered_texture()
        # Delete the checker watermark built by older versions of this tool.
        _uv_clear_digit_network(UV_CHECKER_TEXT_PREFIX, UV_CHECKER_MTL,
                                UV_CHECKER_GRID + ".outColor")
        # The digit network only depends on the printed text, so skip the
        # rebuild when the number did not change.
        res_text = str(int(round(float(resolution))))
        if _UV_LAST_DIGIT_TEXTS["res"] != res_text or not cmds.objExists(UV_TEXT_BLEND):
            _uv_clear_digit_network(UV_TEXT_PREFIX, UV_FLOW_MTL, UV_FLOW_GRID + ".outColor")
            _uv_build_text_network(resolution)
            _UV_LAST_DIGIT_TEXTS["res"] = res_text
        _uv_restore_selection(selection)
        return

    cmds.shadingNode("remapHsv", asUtility=True, name="prUV_remapHSV", skipSelect=True)
    _uv_set_remap_hsv("prUV_remapHSV",
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0.1, 1), (0.037096772, 0.25242719, 1)])

    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_checkert", skipSelect=True)

    cmds.shadingNode("multiplyDivide", asUtility=True, name=UV_CHECKER_MD, skipSelect=True)
    cmds.setAttr(UV_CHECKER_MD + ".operation", 2)
    cmds.setAttr(UV_CHECKER_MD + ".input2", 1, 2, 2, type="float3")

    _uv_make_sets(UV_FLOW_SG)

    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_p2d6", skipSelect=True)
    cmds.setAttr("prUV_p2d6.stagger", True)

    cmds.shadingNode("multiplyDivide", asUtility=True, name=UV_CHECKER_RES, skipSelect=True)
    cmds.setAttr(UV_CHECKER_RES + ".operation", 2)
    cmds.setAttr(UV_CHECKER_RES + ".input1", resolution, resolution, resolution, type="float3")
    cmds.setAttr(UV_CHECKER_RES + ".input2", 1, checker2, checker1, type="float3")

    cmds.shadingNode("checker", asTexture=True, name="prUV_uvchecker_big", skipSelect=True)
    cmds.setAttr("prUV_uvchecker_big.contrast", 0.3)

    _uv_make_sets(UV_CHECKER_SG)

    cmds.shadingNode("remapHsv", asUtility=True, name="prUV_uvcheckerHsv", skipSelect=True)
    _uv_set_remap_hsv("prUV_uvcheckerHsv",
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0.22, 1), (0.93043476, 1, 1)])

    cmds.shadingNode("grid", asTexture=True, name=UV_CHECKER_GRID, skipSelect=True)
    cmds.setAttr(UV_CHECKER_GRID + ".lineColor", 1, 1, 0, type="float3")
    cmds.setAttr(UV_CHECKER_GRID + ".uWidth", 0.001)
    cmds.setAttr(UV_CHECKER_GRID + ".vWidth", 0.001)

    cmds.shadingNode("ramp", asTexture=True, name="prUV_ramp1", skipSelect=True)
    cmds.setAttr("prUV_ramp1.type", 3)
    cmds.setAttr("prUV_ramp1.interpolation", 0)
    for index, (position, color) in enumerate(((1, (0, 0, 0)),
                                               (0.95, (1, 1, 1)),
                                               (0.05, (0, 0, 0)),
                                               (0, (1, 1, 1)))):
        cmds.setAttr("prUV_ramp1.colorEntryList[{0}].position".format(index), position)
        cmds.setAttr("prUV_ramp1.colorEntryList[{0}].color".format(index),
                     *color, type="float3")

    cmds.shadingNode("layeredTexture", asTexture=True, name=UV_FLOW_LAYERS, skipSelect=True)
    for index, blend_mode in ((0, 8), (1, 1)):
        cmds.setAttr(UV_FLOW_LAYERS + ".inputs[{0}].alpha".format(index), 1)
        cmds.setAttr(UV_FLOW_LAYERS + ".inputs[{0}].blendMode".format(index), blend_mode)
        cmds.setAttr(UV_FLOW_LAYERS + ".inputs[{0}].isVisible".format(index), True)

    cmds.shadingNode("remapHsv", asUtility=True, name="prUV_remapHSV2", skipSelect=True)
    _uv_set_remap_hsv("prUV_remapHSV2",
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0, 1), (1, 1, 1)],
                      [(0, 0, 1), (0.1016129, 0.30097088, 1)])

    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_checkert4", skipSelect=True)

    cmds.shadingNode("ramp", asTexture=True, name="prUV_arrowramp", skipSelect=True)
    cmds.setAttr("prUV_arrowramp.defaultColor", 0, 0, 0, type="float3")
    cmds.setAttr("prUV_arrowramp.type", 3)
    cmds.setAttr("prUV_arrowramp.interpolation", 0)
    for index, (position, color) in enumerate(((0, (1, 1, 1)),
                                               (1, (0, 0, 0)),
                                               (0.95975232124328613, (1, 1, 1)),
                                               (0.03405572846531868, (0, 0, 0)))):
        cmds.setAttr("prUV_arrowramp.colorEntryList[{0}].position".format(index), position)
        cmds.setAttr("prUV_arrowramp.colorEntryList[{0}].color".format(index),
                     *color, type="float3")

    cmds.shadingNode("grid", asTexture=True, name="prUV_uvcheckergpixelgrid", skipSelect=True)
    cmds.setAttr("prUV_uvcheckergpixelgrid.lineColor",
                 0.30322582, 0.30322582, 0.30322582, type="float3")
    cmds.setAttr("prUV_uvcheckergpixelgrid.uWidth", 0.05)
    cmds.setAttr("prUV_uvcheckergpixelgrid.vWidth", 0.05)

    cmds.shadingNode("blinn", asShader=True, name=UV_FLOW_MTL, skipSelect=True)

    cmds.shadingNode("checker", asTexture=True, name="prUV_uvcheckerch_small", skipSelect=True)
    cmds.setAttr("prUV_uvcheckerch_small.color1",
                 0.3571353, 0.43935934, 0.51786023, type="float3")
    cmds.setAttr("prUV_uvcheckerch_small.contrast", 0.3)

    cmds.shadingNode("blinn", asShader=True, name=UV_CHECKER_MTL, skipSelect=True)

    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_place2dTexture5", skipSelect=True)
    cmds.setAttr("prUV_place2dTexture5.stagger", True)

    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_checkert2", skipSelect=True)
    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_checkert1", skipSelect=True)

    cmds.connectAttr("prUV_arrowramp.outColor", "prUV_remapHSV.color")
    cmds.connectAttr(UV_CHECKER_MD + ".outputZ", "prUV_checkert.repeatU")
    cmds.connectAttr(UV_CHECKER_MD + ".outputZ", "prUV_checkert.repeatV")
    cmds.connectAttr(UV_CHECKER_RES + ".output", UV_CHECKER_MD + ".input1")
    cmds.connectAttr(UV_FLOW_MTL + ".outColor", UV_FLOW_SG + ".surfaceShader")
    cmds.connectAttr(UV_CHECKER_MD + ".outputY", "prUV_p2d6.repeatU")
    cmds.connectAttr(UV_CHECKER_MD + ".outputY", "prUV_p2d6.repeatV")

    cmds.connectAttr("prUV_checkert.outUV", "prUV_uvchecker_big.uvCoord")
    cmds.connectAttr("prUV_checkert.outUvFilterSize", "prUV_uvchecker_big.uvFilterSize")
    cmds.connectAttr("prUV_uvcheckerch_small.outColor", "prUV_uvchecker_big.color1")
    cmds.connectAttr("prUV_uvcheckergpixelgrid.outColor", "prUV_uvchecker_big.color2")
    cmds.connectAttr(UV_CHECKER_MTL + ".outColor", UV_CHECKER_SG + ".surfaceShader")
    cmds.connectAttr("prUV_uvcheckergpixelgrid.outColor", "prUV_uvcheckerHsv.color")
    cmds.connectAttr("prUV_checkert2.outUV", UV_CHECKER_GRID + ".uvCoord")
    cmds.connectAttr("prUV_checkert2.outUvFilterSize", UV_CHECKER_GRID + ".uvFilterSize")
    cmds.connectAttr("prUV_p2d6.outUV", "prUV_ramp1.uvCoord")
    cmds.connectAttr("prUV_p2d6.outUvFilterSize", "prUV_ramp1.uvFilterSize")
    cmds.connectAttr("prUV_remapHSV.outColor", UV_FLOW_LAYERS + ".inputs[0].color")
    cmds.connectAttr("prUV_remapHSV2.outColor", UV_FLOW_LAYERS + ".inputs[1].color")
    cmds.connectAttr("prUV_ramp1.outColor", "prUV_remapHSV2.color")
    cmds.connectAttr(UV_CHECKER_MD + ".outputX", "prUV_checkert4.repeatU")
    cmds.connectAttr(UV_CHECKER_MD + ".outputX", "prUV_checkert4.repeatV")
    cmds.connectAttr("prUV_place2dTexture5.outUV", "prUV_arrowramp.uvCoord")
    cmds.connectAttr("prUV_place2dTexture5.outUvFilterSize", "prUV_arrowramp.uvFilterSize")
    cmds.connectAttr("prUV_checkert4.outUV", "prUV_uvcheckergpixelgrid.uvCoord")
    cmds.connectAttr("prUV_checkert4.outUvFilterSize", "prUV_uvcheckergpixelgrid.uvFilterSize")

    cmds.connectAttr(UV_FLOW_LAYERS + ".outColor", UV_FLOW_MTL + ".specularColor")
    cmds.connectAttr("prUV_checkert1.outUV", "prUV_uvcheckerch_small.uvCoord")
    cmds.connectAttr("prUV_checkert1.outUvFilterSize", "prUV_uvcheckerch_small.uvFilterSize")

    cmds.connectAttr("prUV_uvchecker_big.outColor", UV_CHECKER_GRID + ".fillerColor",
                     force=True)
    cmds.connectAttr(UV_CHECKER_GRID + ".outColor", UV_CHECKER_MTL + ".color")
    cmds.connectAttr("prUV_uvcheckerHsv.outColor", UV_CHECKER_MTL + ".specularColor")
    cmds.connectAttr(UV_CHECKER_MD + ".outputZ", "prUV_place2dTexture5.repeatU")
    cmds.connectAttr(UV_CHECKER_MD + ".outputZ", "prUV_place2dTexture5.repeatV")
    cmds.connectAttr(UV_CHECKER_MD + ".outputY", "prUV_checkert1.repeatU")
    cmds.connectAttr(UV_CHECKER_MD + ".outputY", "prUV_checkert1.repeatV")

    cmds.shadingNode("grid", asTexture=True, name=UV_FLOW_GRID, skipSelect=True)
    cmds.shadingNode("place2dTexture", asUtility=True, name="prUV_flowEdgeGrid_p2dT", skipSelect=True)
    cmds.connectAttr("prUV_flowEdgeGrid_p2dT.outUV", UV_FLOW_GRID + ".uvCoord", force=True)
    cmds.connectAttr("prUV_flowEdgeGrid_p2dT.outUvFilterSize", UV_FLOW_GRID + ".uvFilterSize",
                     force=True)
    cmds.setAttr(UV_FLOW_GRID + ".uWidth", 0.001)
    cmds.setAttr(UV_FLOW_GRID + ".vWidth", 0.001)
    cmds.setAttr(UV_FLOW_GRID + ".lineColor", 1, 1, 0, type="float3")
    cmds.connectAttr(UV_FLOW_LAYERS + ".outColor", UV_FLOW_GRID + ".fillerColor", force=True)
    cmds.connectAttr(UV_FLOW_GRID + ".outColor", UV_FLOW_MTL + ".color")

    _uv_create_chrome_shader()
    _uv_build_text_network(resolution)
    _UV_LAST_DIGIT_TEXTS["res"] = str(int(round(float(resolution))))
    _uv_restore_selection(selection)


def uv_checker_create_shader(resolution=UV_DEFAULT_RESOLUTION,
                             checker1=UV_DEFAULT_CHECKER_1,
                             checker2=UV_DEFAULT_CHECKER_2):
    """Create the checker + flow materials, or update their resolutions.

    The whole build is one undo step and the viewport redraws once at the end.
    """
    try:
        cmds.refresh(suspend=True)
        cmds.undoInfo(openChunk=True, chunkName="prUV checker build")
        _uv_build_shader(resolution, checker1, checker2)
    finally:
        # Resume the refresh BEFORE touching the undo chunk: a bare refresh()
        # is ignored while suspension is active, and any failure here must
        # never leave the viewport suspended (it freezes the camera).
        cmds.refresh(suspend=False)
        try:
            cmds.undoInfo(closeChunk=True)
        except Exception:
            pass
        cmds.refresh(force=True)


# ------------------------------------------------------------- cycle shader

def uv_checker_cycle_shader():
    """Cycle selected objects: default -> checker -> flow -> chrome -> default."""
    # Recover from a leaked refresh suspension (e.g. a build interrupted by
    # Esc): the viewport then never redraws and the camera seems dead.
    # refresh has no query flag; suspend=False is a no-op when not suspended.
    cmds.refresh(suspend=False)
    _uv_change_viewport()

    objects = cmds.ls(selection=True, objectsOnly=True, dag=True, shapes=True)
    if not objects:
        lj_warn("Please select at least one polygon object.")
        return

    current = cmds.listConnections(objects[0], source=False, destination=True,
                                   type="shadingEngine")
    if not current:
        current = cmds.listConnections(objects[0], type="shadingEngine")
    if not current:
        lj_warn("No shading group assigned to {0}.".format(objects[0]))
        return

    target = "initialShadingGroup"
    if current[0] == "initialShadingGroup":
        target = UV_CHECKER_SG
    elif current[0] == UV_CHECKER_SG:
        target = UV_FLOW_SG
    elif current[0] == UV_FLOW_SG:
        target = UV_CHROME_SG

    if not cmds.objExists(target):
        lj_warn('Shading group "{0}" does not exist. Please create the shader first.'.format(target))
        return

    cmds.sets(edit=True, forceElement=target)


# --------------------------------------------- embedded UV layout UI state

# Control names of the intFields inside the tool's UV frame.
UV_CHECKER_UI_RES = "prUV_UICheckerRes"
UV_CHECKER_UI_CHK1 = "prUV_UIChecker1Res"
UV_CHECKER_UI_CHK2 = "prUV_UIChecker2Res"

# Last values handed to uv_checker_create_shader; they seed the fields when
# the tool window is reopened.
_UV_CHECKER_SETTINGS = {
    "resolution": UV_DEFAULT_RESOLUTION,
    "checker1": UV_DEFAULT_CHECKER_1,
    "checker2": UV_DEFAULT_CHECKER_2,
}


# ---------------------------------------------------------- Ring 批量分割
# 来自 RI_project_v1.5 的功能1：四边面对边中点连接（Ring 分割）。
# 模式1：对选中的单条边找单四边面的对边做中点分割；
# 模式2：等同 Maya 自带 To Edge Ring Split，对整条 Ring 一次分割。

# UI 控件名（跨布局读取值）
RING_UI_MODE = "ljgtRingModeMenu"
RING_UI_EDGE_FLOW = "ljgtRingEdgeFlowChk"


def ring_unpack_single_edge(edge_str):
    """解析单条边组件字符串，返回 (dag路径, 边ID)，失败返回 (None, None)"""
    selList = oma.MSelectionList()
    selList.add(edge_str)
    dag, compObj = selList.getComponent(0)
    if compObj.isNull():
        return None, None
    sic = oma.MFnSingleIndexedComponent(compObj)
    ids = sic.getElements()
    if len(ids) != 1:
        return None, None
    return dag, ids[0]


def ring_parse_ring_result(raw):
    """将 polySelect 返回的 'a:b' 区间列表展开为边ID列表"""
    out = []
    for item in raw:
        if isinstance(item, str) and ':' in item:
            s, e = item.split(':')
            out.extend(range(int(s), int(e) + 1))
        else:
            out.append(int(item))
    return out


def ring_get_ring_id_list(mesh_name, edge_id):
    """获取指定边的整条 Ring 边ID列表，失败返回 None"""
    try:
        raw = cmds.polySelect(mesh_name, edgeRing=edge_id, q=True)
        return ring_parse_ring_result(raw)
    except Exception:
        return None


def ring_get_edge_vertices(dagPath, edgeId):
    """获取边两端顶点ID，返回 (顶点0, 顶点1)"""
    itEdge = oma.MItMeshEdge(dagPath)
    itEdge.setIndex(edgeId)
    return itEdge.vertexId(0), itEdge.vertexId(1)


def ring_get_quad_opposite_edge(dagPath, edgeId):
    """单四边面获取对边，模式1使用"""
    v0, v1 = ring_get_edge_vertices(dagPath, edgeId)
    edgeIter = oma.MItMeshEdge(dagPath)
    edgeIter.setIndex(edgeId)
    face_ids = edgeIter.getConnectedFaces()

    for fid in face_ids:
        faceIt = oma.MItMeshPolygon(dagPath)
        faceIt.setIndex(fid)
        if faceIt.polygonVertexCount() != 4:
            continue
        verts = list(faceIt.getVertices())
        face_edge_vert_pairs = [
            (verts[0], verts[1]),
            (verts[1], verts[2]),
            (verts[2], verts[3]),
            (verts[3], verts[0]),
        ]
        opp_vert_pair = None
        for i, (va, vb) in enumerate(face_edge_vert_pairs):
            if (va == v0 and vb == v1) or (va == v1 and vb == v0):
                opp_i = (i + 2) % 4
                opp_vert_pair = face_edge_vert_pairs[opp_i]
                break
        if not opp_vert_pair:
            continue
        a, b = opp_vert_pair
        face_edges = list(faceIt.getEdges())
        found_opp_eid = None
        for eid in face_edges:
            ea, eb = ring_get_edge_vertices(dagPath, eid)
            if (ea == a and eb == b) or (ea == b and eb == a):
                found_opp_eid = eid
                break
        if found_opp_eid is not None:
            return found_opp_eid, fid
    return None, None


def ring_find_quad_opposite_edge(edge_str):
    """由边字符串查找单四边面对边，返回 (对边字符串, 边对去重key)，失败返回 (None, None)"""
    dag, eid = ring_unpack_single_edge(edge_str)
    if dag is None:
        return None, None
    mesh_fullname = dag.fullPathName()
    opp_eid, face_id = ring_get_quad_opposite_edge(dag, eid)
    if opp_eid is None:
        return None, None
    opposite_edge_str = f"{mesh_fullname}.e[{opp_eid}]"
    pair_key = tuple(sorted([edge_str, opposite_edge_str]))
    return opposite_edge_str, pair_key


def ring_process_one_edge(edge_str, use_edge_flow, processed_set):
    """
    模式1：单条边处理，单四边面对边分割
    返回 (状态, 失败边)：True=已分割，False=无需分割，None=该边对已被处理过（跳过）
    """
    neighbor, pair_key = ring_find_quad_opposite_edge(edge_str)
    if not neighbor:
        return False, edge_str
    # 同一对对边只分割一次，重复命中返回 None 表示跳过（不计入成功）
    if pair_key in processed_set:
        return None, None
    processed_set.add(pair_key)

    cmds.select([edge_str, neighbor], r=True)
    cmds.polySplitRing(
        ch=0,
        splitType=1,
        weight=0.5,
        insertWithEdgeFlow=use_edge_flow,
        adjustEdgeFlow=1.0
    )
    flow_text = "ON" if use_edge_flow else "OFF"
    print(f"[OK] Split(insertWithEdgeFlow:{flow_text}): {edge_str} <-> {neighbor}")
    return True, None


def ring_batch_split(*args):
    """执行分割按钮回调：按所选模式对选中边进行批量分割"""
    use_edge_flow = cmds.checkBox(RING_UI_EDGE_FLOW, q=True, v=True)
    mode_idx = cmds.optionMenu(RING_UI_MODE, q=True, select=True)

    edge_sel = cmds.filterExpand(selectionMask=32, expand=True)
    if not edge_sel:
        lj_warn("请选中至少一条边！")
        return

    processed = set()
    # 已处理过的 Ring 去重集合：选中的多条边落在同一 Ring 时只分割一次
    processed_rings = set()
    success = 0
    failed = []
    original = edge_sel[:]

    for sel_edge in edge_sel:
        if mode_idx == 1:
            # 模式1：仅处理选中单条边（单四边面对边分割）
            ok, fail_item = ring_process_one_edge(sel_edge, use_edge_flow, processed)
            if ok is True:
                success += 1
            elif ok is False:
                failed.append(fail_item)
        else:
            # 模式2：整条Ring分割，对齐 edgeRingUtilities -> To Edge Ring Split
            dag, eid = ring_unpack_single_edge(sel_edge)
            if dag is None:
                failed.append(sel_edge)
                continue
            mesh_name = dag.fullPathName()
            ring_id_list = ring_get_ring_id_list(mesh_name, eid)
            if not ring_id_list or len(ring_id_list) < 2:
                failed.append(sel_edge)
                continue
            # 同一 Ring 被多条选中边命中时只分割一次，避免重复切开
            ring_key = (mesh_name, tuple(sorted(ring_id_list)))
            if ring_key in processed_rings:
                continue
            processed_rings.add(ring_key)
            # 组装完整edgeRing组件字符串，一次性传给polySplitRing（官方工具就是这样做）
            ring_edge_str_list = [f"{mesh_name}.e[{x}]" for x in ring_id_list]
            cmds.select(ring_edge_str_list, r=True)
            try:
                cmds.polySplitRing(
                    ch=0,
                    splitType=1,
                    weight=0.5,
                    insertWithEdgeFlow=use_edge_flow,
                    adjustEdgeFlow=1.0
                )
                success += 1
                flow_text = "ON" if use_edge_flow else "OFF"
                print(f"[OK] RingSplit(insertWithEdgeFlow:{flow_text}) 整条Ring:{mesh_name} ringId:{eid}")
            except Exception as err:
                failed.append(sel_edge)
                print(f"整条Ring分割失败 {sel_edge} : {err}")

    cmds.select(original, r=True)
    print("\n==== Summary ====")
    print(f"成功：{success}")
    unique_failed = list(set(failed))
    if unique_failed:
        print(f"失败边：{unique_failed}")
        lj_warn(u"分割完成：成功 %d 项，失败 %d 项（失败边见 Script Editor）"
                % (success, len(unique_failed)))
    else:
        lj_info(u"分割完成：成功 %d 项" % success)


# =========================================================================
# 扭曲面检查（内嵌自 twist_face_check.py v1.0.0，仅核心逻辑）
#   - 检查并选择：只检查顶点数为 4 的 polygon face，用 Maya 当前三角化结果
#     计算两个三角形几何法线夹角，选中超过阈值的四边面，不修改场景。
#     有选择时只检查当前选择；无选择时扫描整个场景。
#   - 三角化：直接调用 Maya 原生 polyTriangulate。
#   - Spin 连线：只对当前选中的、被正好两个三角面共享的边执行 polyFlipEdge。
#   原独立文件的 Qt 窗口 / 菜单 / 插件入口未搬运，UI 由
#   LingJuGeometryTools.create_twist_face_check_layout 提供；
#   maya.api.OpenMaya 统一使用本文件已有的 oma 别名。需要 NumPy（Maya 2022+ 自带）。
# =========================================================================

import traceback as _twist_traceback
from collections import Counter as _TwistCounter


try:
    import numpy as np
except ImportError:  # NumPy 缺失时折叠栏按钮给出提示，不影响插件其余功能
    np = None

TWIST_THRESHOLD_OPTION = "twistFaceCheckThreshold"
TWIST_DEFAULT_THRESHOLD = 5.0
TWIST_MIN_THRESHOLD = 0.0
TWIST_MAX_THRESHOLD = 180.0

_TWIST_AREA_EPSILON = 1e-12
_TWIST_NORMAL_EPSILON = 1e-12
_TWIST_FACE_RE = re.compile(r"^(.*)\.f\[(\d+)\]$")
_TWIST_EDGE_RE = re.compile(r"^(.*)\.e\[(\d+)\]$")


# ---------------------------------------------------------------------------
# Small data containers
# ---------------------------------------------------------------------------


class TwistMeshScope:
    """One mesh and the faces that should be inspected."""

    def __init__(self, mesh, faces=None):
        # faces 为 None 表示整个网格；否则为 np.ndarray 面号集合
        self.mesh = mesh
        self.faces = faces


class TwistCheckResult:
    """扭曲检查结果汇总统计。"""

    def __init__(self, scope_label="", mesh_count=0, scanned_quads=0,
                 ignored_faces=0, degenerate=0, hits=0, elapsed=0.0,
                 selections=None):
        self.scope_label = scope_label
        self.mesh_count = mesh_count
        self.scanned_quads = scanned_quads
        self.ignored_faces = ignored_faces
        self.degenerate = degenerate
        self.hits = hits
        self.elapsed = elapsed
        self.selections = [] if selections is None else selections


class TwistSpinResult:
    """边翻转（Spin Edge）结果统计。"""

    def __init__(self, selected_edges=0, ignored_non_edges=0, valid_edges=0,
                 success=0, skipped=0, failed=0, elapsed=0.0, reasons=None):
        self.selected_edges = selected_edges
        self.ignored_non_edges = ignored_non_edges
        self.valid_edges = valid_edges
        self.success = success
        self.skipped = skipped
        self.failed = failed
        self.elapsed = elapsed
        self.reasons = _TwistCounter() if reasons is None else reasons


class TwistEdgeInfo:
    """单条待处理边的前后配对信息。"""

    def __init__(self, mesh, edge_id, old_pair, desired_pair):
        self.mesh = mesh
        self.edge_id = edge_id
        self.old_pair = old_pair
        self.desired_pair = desired_pair


# ---------------------------------------------------------------------------
# Maya selection / scope helpers
# ---------------------------------------------------------------------------


def _twist_as_mesh_shape(base):
    """Resolve a transform or shape name to a non-intermediate mesh shape."""

    if not base:
        return None

    try:
        object_type = cmds.objectType(base)
    except Exception:
        return None

    if object_type == "mesh":
        try:
            if cmds.getAttr(base + ".intermediateObject"):
                return None
        except Exception:
            pass
        return base

    try:
        shapes = (
            cmds.listRelatives(
                base,
                shapes=True,
                type="mesh",
                fullPath=True,
                noIntermediate=True,
            )
            or []
        )
    except Exception:
        shapes = []

    return shapes[0] if shapes else None


def _twist_mesh_shapes_from_object(obj):
    """Return non-intermediate mesh shapes belonging to an object selection."""

    try:
        if cmds.objectType(obj) == "mesh":
            try:
                if cmds.getAttr(obj + ".intermediateObject"):
                    return []
            except Exception:
                pass
            return [obj]
    except Exception:
        return []

    try:
        direct_shapes = (
            cmds.listRelatives(
                obj,
                shapes=True,
                type="mesh",
                fullPath=True,
                noIntermediate=True,
            )
            or []
        )
    except Exception:
        direct_shapes = []

    if direct_shapes:
        return direct_shapes

    # Selecting a group is a common workflow; include meshes below it.
    try:
        return (
            cmds.listRelatives(
                obj,
                allDescendents=True,
                type="mesh",
                fullPath=True,
                noIntermediate=True,
            )
            or []
        )
    except Exception:
        return []


def _twist_all_mesh_shapes():
    """All non-intermediate polygon mesh shapes in the scene."""

    result = []
    for mesh in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(mesh + ".intermediateObject"):
                continue
        except Exception:
            pass
        result.append(mesh)
    return result


def _twist_resolve_scope():
    """Resolve the current selection to meshes and face filters.

    Empty selection -> whole scene.
    Object selection -> all faces of those objects.
    Component selection -> the faces touched by those components.
    """

    selection = cmds.ls(selection=True, flatten=True, long=True) or []
    if not selection:
        return [TwistMeshScope(mesh, None) for mesh in _twist_all_mesh_shapes()], "全场景"

    component_items = [item for item in selection if "." in item and "[" in item]
    object_items = [item for item in selection if item not in component_items]

    scope = {}

    # Components (faces, edges, vertices, UVs) -> faces.
    if component_items:
        try:
            converted = cmds.polyListComponentConversion(component_items, toFace=True)
            face_components = cmds.ls(converted, flatten=True, long=True) or []
        except Exception:
            face_components = []

        for component in face_components:
            match = _TWIST_FACE_RE.match(component)
            if not match:
                continue
            base = match.group(1)
            face_id = int(match.group(2))
            mesh = _twist_as_mesh_shape(base)
            if not mesh:
                continue
            scope.setdefault(mesh, set()).add(face_id)

    # Object selections mean every face on those objects.
    for obj in object_items:
        for mesh in _twist_mesh_shapes_from_object(obj):
            scope[mesh] = None

    resolved = []
    for mesh in sorted(scope):
        face_set = scope[mesh]
        if face_set is None:
            resolved.append(TwistMeshScope(mesh, None))
        else:
            faces = np.fromiter(sorted(face_set), dtype=np.int64, count=len(face_set))
            resolved.append(TwistMeshScope(mesh, faces))

    return resolved, "当前选择"


def _twist_parse_edge_component(component):
    match = _TWIST_EDGE_RE.match(component)
    if not match:
        return None
    mesh = _twist_as_mesh_shape(match.group(1))
    if not mesh:
        return None
    return mesh, int(match.group(2))


def _twist_collect_selected_edges(selection):
    """Collect selected edge components grouped by mesh shape."""

    groups = {}
    non_edges = 0

    for component in selection:
        parsed = _twist_parse_edge_component(component)
        if parsed is None:
            non_edges += 1
            continue
        mesh, edge_id = parsed
        groups.setdefault(mesh, set()).add(edge_id)

    return groups, non_edges


# ---------------------------------------------------------------------------
# Mesh snapshot and vectorized twist math
# ---------------------------------------------------------------------------


class TwistMeshSnapshot:
    """Bulk topology and point data for one mesh.

    All heavy lifting is done through MFnMesh bulk accessors.  No Maya command
    is called per face or per edge.
    """

    def __init__(self, mesh):
        self.mesh = mesh

        selection = oma.MSelectionList()
        selection.add(mesh)
        self.dag_path = selection.getDagPath(0)
        self.fn = oma.MFnMesh(self.dag_path)

        points = self.fn.getPoints(oma.MSpace.kObject)
        self.points = np.asarray(points, dtype=np.float64)[:, :3]

        counts, _vertex_ids = self.fn.getVertices()
        self.counts = np.asarray(counts, dtype=np.int64)

        self.quad_face_ids = np.nonzero(self.counts == 4)[0].astype(np.int64)

        tri_counts, tri_vertices = self.fn.getTriangles()
        self.tri_counts = np.asarray(tri_counts, dtype=np.int64)
        self.tri_vertices = np.asarray(tri_vertices, dtype=np.int64)

        self.tri_offsets = np.empty(self.tri_counts.size + 1, dtype=np.int64)
        self.tri_offsets[0] = 0
        if self.tri_counts.size:
            np.cumsum(self.tri_counts * 3, out=self.tri_offsets[1:])

    @property
    def face_count(self):
        return int(self.counts.size)

    def quad_angles(self, quad_ids):
        """Return (face ids, angles in degrees, valid mask) for quads.

        The two triangles come from Maya's current triangulation, so the
        measured diagonal matches what Maya is displaying.
        """

        quad_ids = np.asarray(quad_ids, dtype=np.int64)
        if quad_ids.size == 0:
            empty = np.empty(0, dtype=np.float64)
            return quad_ids, empty, np.empty(0, dtype=bool)

        tri_count_for_quad = self.tri_counts[quad_ids]
        valid = tri_count_for_quad == 2

        if self.tri_vertices.size == 0:
            return quad_ids, np.full(quad_ids.size, np.nan), np.zeros(quad_ids.size, dtype=bool)

        offsets = self.tri_offsets[quad_ids]
        triangle_slots = offsets[:, None] + np.arange(6, dtype=np.int64)[None, :]
        triangle_slots = np.clip(triangle_slots, 0, self.tri_vertices.size - 1)
        triangle_vertices = self.tri_vertices[triangle_slots].reshape(-1, 6)

        # (Q, 2, 3, 3): two triangles per quad, three points, XYZ.
        triangle_points = self.points[triangle_vertices.reshape(-1, 3), :].reshape(
            -1, 2, 3, 3
        )

        triangle_a = triangle_points[:, 0]
        triangle_b = triangle_points[:, 1]

        normal_a = np.cross(
            triangle_a[:, 1] - triangle_a[:, 0],
            triangle_a[:, 2] - triangle_a[:, 0],
        )
        normal_b = np.cross(
            triangle_b[:, 1] - triangle_b[:, 0],
            triangle_b[:, 2] - triangle_b[:, 0],
        )
        normals = np.stack((normal_a, normal_b), axis=1)

        lengths = np.linalg.norm(normals, axis=2)
        valid = valid & (lengths[:, 0] > _TWIST_AREA_EPSILON) & (
            lengths[:, 1] > _TWIST_AREA_EPSILON
        )

        safe_lengths = np.maximum(lengths[..., None], _TWIST_NORMAL_EPSILON)
        normals = normals / safe_lengths

        normal_a = normals[:, 0]
        normal_b = normals[:, 1]
        cross_ab = np.cross(normal_a, normal_b)
        sin_angle = np.linalg.norm(cross_ab, axis=1)
        cos_angle = np.einsum("ij,ij->i", normal_a, normal_b)
        angles = np.degrees(np.arctan2(sin_angle, cos_angle))

        angles = np.where(valid, angles, np.nan)
        return quad_ids, angles, valid


# ---------------------------------------------------------------------------
# Component strings and selection
# ---------------------------------------------------------------------------


def _twist_consecutive_ranges(ids):
    """Compress sorted unique integer ids into inclusive ranges."""

    if ids.size == 0:
        return []

    ids = np.unique(ids.astype(np.int64, copy=False))
    if ids.size == 1:
        return [(int(ids[0]), int(ids[0]))]

    breaks = np.nonzero(np.diff(ids) != 1)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [ids.size - 1]))

    return [(int(ids[start]), int(ids[end])) for start, end in zip(starts, ends)]


def _twist_face_component_strings(mesh, face_ids):
    components = []
    for start, end in _twist_consecutive_ranges(face_ids):
        if start == end:
            components.append(f"{mesh}.f[{start}]")
        else:
            components.append(f"{mesh}.f[{start}:{end}]")
    return components


def _twist_select_faces(selections):
    components = []
    for mesh, face_ids in selections:
        if face_ids.size:
            components.extend(_twist_face_component_strings(mesh, face_ids))

    if components:
        cmds.select(components, replace=True)
    else:
        cmds.select(clear=True)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


def twist_check_and_select(threshold=TWIST_DEFAULT_THRESHOLD):
    """Scan quad faces, select twisted quads, and return statistics."""

    started = time.perf_counter()
    threshold = max(TWIST_MIN_THRESHOLD, min(TWIST_MAX_THRESHOLD, float(threshold)))

    scope, scope_label = _twist_resolve_scope()
    result = TwistCheckResult(scope_label=scope_label)

    if not scope:
        _twist_select_faces([])
        result.elapsed = time.perf_counter() - started
        return result

    selections = []

    for entry in scope:
        try:
            snapshot = TwistMeshSnapshot(entry.mesh)
        except Exception:
            continue

        all_face_ids = np.arange(snapshot.face_count, dtype=np.int64)
        if entry.faces is None:
            scope_faces = all_face_ids
        else:
            scope_faces = np.intersect1d(all_face_ids, entry.faces, assume_unique=False)

        if scope_faces.size == 0:
            continue

        result.mesh_count += 1

        quad_ids = np.intersect1d(
            snapshot.quad_face_ids, scope_faces, assume_unique=True
        )
        result.ignored_faces += int(scope_faces.size - quad_ids.size)
        result.scanned_quads += int(quad_ids.size)

        if quad_ids.size == 0:
            continue

        face_ids, angles, valid = snapshot.quad_angles(quad_ids)
        result.degenerate += int((~valid).sum())

        if not valid.any():
            continue

        hit_mask = valid & (angles > threshold)
        hit_ids = face_ids[hit_mask]
        if hit_ids.size:
            selections.append((entry.mesh, hit_ids))
            result.hits += int(hit_ids.size)

    result.selections = selections
    _twist_select_faces(selections)
    result.elapsed = time.perf_counter() - started
    return result


# ---------------------------------------------------------------------------
# Triangulate
# ---------------------------------------------------------------------------


def twist_triangulate_selection():
    """Triangulate the current selection with Maya's polyTriangulate."""

    selection = cmds.ls(selection=True, long=True) or []
    if not selection:
        return u"当前没有选择任何组件，无法三角化。"

    try:
        cmds.undoInfo(openChunk=True, chunkName="Twist Face Check: Triangulate")
        try:
            cmds.polyTriangulate()
        finally:
            cmds.undoInfo(closeChunk=True)
    except Exception as exc:
        return u"三角化失败：%s" % exc

    return u"已对当前选择的 %d 个组件执行三角化（polyTriangulate）。" % len(selection)


# ---------------------------------------------------------------------------
# Spin
# ---------------------------------------------------------------------------


def _twist_inspect_edge(snapshot, edge_iterator, edge_id):
    """Validate one selected edge for polyFlipEdge.

    返回 (TwistEdgeInfo, None) 或 (None, 忽略原因)。
    """

    try:
        edge_iterator.setIndex(edge_id)
        connected_faces = list(edge_iterator.getConnectedFaces())
    except Exception:
        return None, u"无效边"

    if len(connected_faces) != 2:
        return None, u"边界/非流形边"

    try:
        if any(snapshot.counts[face_id] != 3 for face_id in connected_faces):
            return None, u"相邻面不是三角面"
    except Exception:
        return None, u"面信息异常"

    try:
        vertex_a = int(edge_iterator.vertexId(0))
        vertex_b = int(edge_iterator.vertexId(1))
    except Exception:
        return None, u"边顶点异常"

    if vertex_a == vertex_b:
        return None, u"退化边"

    opposite_vertices = []
    for face_id in connected_faces:
        try:
            face_vertices = list(snapshot.fn.getPolygonVertices(face_id))
        except Exception:
            return None, u"面顶点异常"

        third = [
            int(vertex)
            for vertex in face_vertices
            if int(vertex) not in (vertex_a, vertex_b)
        ]
        if len(third) != 1:
            return None, u"无法确定对角顶点"
        opposite_vertices.append(third[0])

    four_vertices = {vertex_a, vertex_b, opposite_vertices[0], opposite_vertices[1]}
    if len(four_vertices) != 4:
        return None, u"顶点重复"

    old_pair = tuple(sorted((vertex_a, vertex_b)))
    desired_pair = tuple(sorted((opposite_vertices[0], opposite_vertices[1])))
    if old_pair == desired_pair:
        return None, u"对角线相同"

    return (
        TwistEdgeInfo(
            mesh=snapshot.mesh,
            edge_id=edge_id,
            old_pair=old_pair,
            desired_pair=desired_pair,
        ),
        None,
    )


def _twist_mesh_editable(mesh):
    """Return whether a mesh can be edited by polyFlipEdge."""

    try:
        if cmds.referenceQuery(mesh, isNodeReferenced=True):
            return False, u"引用节点"
    except Exception:
        pass

    try:
        locked = cmds.lockNode(mesh, query=True, lock=True)
        if locked and locked[0]:
            return False, u"锁定节点"
    except Exception:
        pass

    return True, ""


def _twist_edge_pair_set(snapshot):
    """All edge vertex pairs in a mesh, used as a reindexing fallback."""

    pairs = set()
    try:
        iterator = oma.MItMeshEdge(snapshot.dag_path)
    except Exception:
        return pairs

    while not iterator.isDone():
        try:
            vertex_a = int(iterator.vertexId(0))
            vertex_b = int(iterator.vertexId(1))
            pairs.add(tuple(sorted((vertex_a, vertex_b))))
        except Exception:
            pass
        iterator.next()

    return pairs


def twist_spin_selected_edges():
    """Flip selected edges that are shared by exactly two triangles."""

    started = time.perf_counter()
    result = TwistSpinResult()

    selection = cmds.ls(selection=True, flatten=True, long=True) or []
    if not selection:
        result.elapsed = time.perf_counter() - started
        return result

    groups, non_edges = _twist_collect_selected_edges(selection)
    result.ignored_non_edges += non_edges
    result.selected_edges = sum(len(edge_ids) for edge_ids in groups.values())

    if not groups:
        result.elapsed = time.perf_counter() - started
        return result

    valid_by_mesh = {}

    for mesh in sorted(groups):
        editable, reason = _twist_mesh_editable(mesh)
        if not editable:
            result.skipped += len(groups[mesh])
            result.reasons[reason] += len(groups[mesh])
            continue

        try:
            snapshot = TwistMeshSnapshot(mesh)
            edge_iterator = oma.MItMeshEdge(snapshot.dag_path)
        except Exception:
            result.failed += len(groups[mesh])
            result.reasons[u"无法读取网格"] += len(groups[mesh])
            continue

        infos = []
        for edge_id in sorted(groups[mesh]):
            info, reason = _twist_inspect_edge(snapshot, edge_iterator, edge_id)
            if info is not None:
                infos.append(info)
            else:
                result.skipped += 1
                result.reasons[reason or u"不可Spin"] += 1

        if infos:
            valid_by_mesh[mesh] = infos

    result.valid_edges = sum(len(infos) for infos in valid_by_mesh.values())

    if not valid_by_mesh:
        result.elapsed = time.perf_counter() - started
        return result

    # Keep before-state for post-operation verification.
    before_pairs = {}
    for mesh, infos in valid_by_mesh.items():
        for info in infos:
            before_pairs[(mesh, info.edge_id)] = info.old_pair

    failed_meshes = set()
    chunk_open = False
    try:
        try:
            cmds.undoInfo(openChunk=True, chunkName="Twist Face Check: Spin Edges")
            chunk_open = True
        except Exception:
            chunk_open = False

        for mesh in sorted(valid_by_mesh):
            infos = valid_by_mesh[mesh]
            components = [f"{mesh}.e[{info.edge_id}]" for info in infos]
            try:
                cmds.select(components, replace=True)
                cmds.polyFlipEdge()
            except Exception as exc:
                failed_meshes.add(mesh)
                lj_warn(u"polyFlipEdge 在 %s 上执行失败：%s" % (mesh, exc))
    finally:
        if chunk_open:
            try:
                cmds.undoInfo(closeChunk=True)
            except Exception:
                pass

    for mesh in sorted(failed_meshes):
        result.failed += len(valid_by_mesh[mesh])
        result.reasons[u"polyFlipEdge 执行失败"] += len(valid_by_mesh[mesh])

    # Verify each edge by its desired vertex pair.
    for mesh in sorted(valid_by_mesh):
        if mesh in failed_meshes:
            continue

        try:
            after_snapshot = TwistMeshSnapshot(mesh)
        except Exception:
            result.failed += len(valid_by_mesh[mesh])
            result.reasons[u"无法读取翻转后的网格"] += len(valid_by_mesh[mesh])
            continue

        after_pairs = None

        for info in valid_by_mesh[mesh]:
            new_pair = None
            try:
                new_pair = tuple(
                    sorted(int(v) for v in after_snapshot.fn.getEdgeVertices(info.edge_id))
                )
            except Exception:
                new_pair = None

            if new_pair == info.desired_pair:
                result.success += 1
                continue

            if new_pair == before_pairs.get((mesh, info.edge_id), info.old_pair):
                result.skipped += 1
                result.reasons[u"未翻转（相邻边冲突或凹面）"] += 1
                continue

            # Edge ids can be renumbered by topology edits.  Fall back to a
            # vertex-pair lookup for this mesh.
            if after_pairs is None:
                after_pairs = _twist_edge_pair_set(after_snapshot)

            if info.desired_pair in after_pairs:
                result.success += 1
            elif info.old_pair in after_pairs:
                result.skipped += 1
                result.reasons[u"未翻转（相邻边冲突或凹面）"] += 1
            else:
                result.failed += 1
                result.reasons[u"翻转结果异常"] += 1

    result.elapsed = time.perf_counter() - started
    return result


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def _twist_format_check_result(result, threshold):
    if result.mesh_count == 0:
        if result.scope_label == u"当前选择":
            return u"当前选择中没有可检查的多边形网格。"
        return u"场景中没有可检查的多边形网格。"

    if result.scanned_quads == 0:
        return (
            u"%s：没有四边面（忽略 %s 个三角面/ngon）。"
            % (result.scope_label, "{:,}".format(result.ignored_faces))
        )

    parts = [
        result.scope_label,
        u"四边面 {:,}".format(result.scanned_quads),
        u"命中 {:,}".format(result.hits),
    ]
    if result.ignored_faces:
        parts.append(u"忽略三角面/ngon {:,}".format(result.ignored_faces))
    if result.degenerate:
        parts.append(u"退化 {:,}".format(result.degenerate))
    parts.append("%.2fs" % result.elapsed)

    text = " | ".join(parts)
    if result.hits:
        text += u"\n阈值 %.1f°，已选择 %s 个四边面。" % (threshold, "{:,}".format(result.hits))
    else:
        text += u"\n阈值 %.1f°，未发现超过阈值的扭曲四边面。" % threshold
    return text


def _twist_format_spin_result(result):
    if result.selected_edges == 0:
        if result.ignored_non_edges:
            return u"当前选择中没有边。Spin 只处理边，请先选择三角化后的内部连线。"
        return u"请先选择一条或多条边。"

    parts = [
        u"选中边 {:,}".format(result.selected_edges),
        u"可Spin {:,}".format(result.valid_edges),
        u"成功 {:,}".format(result.success),
    ]
    if result.skipped:
        parts.append(u"跳过 {:,}".format(result.skipped))
    if result.failed:
        parts.append(u"失败 {:,}".format(result.failed))
    if result.ignored_non_edges:
        parts.append(u"忽略非边 {:,}".format(result.ignored_non_edges))
    parts.append("%.2fs" % result.elapsed)

    text = " | ".join(parts)
    if result.reasons:
        reason_text = u"；".join(
            u"%s %s" % (reason, count) for reason, count in result.reasons.most_common(3)
        )
        text += u"\n%s" % reason_text
    return text


# =========================================================================
# 快速底面（内嵌自 SGCG_Placement_Tool_v1.1.0 的 Face Placement & Alignment）
#   - 选底面：给单个网格生成可点击的候选底面（绿色小面片），点哪个面片就把
#     模型旋转到该面朝下、贴地并冻结变换。候选模式有两种：
#       六面安全(包围盒)：直接用世界包围盒的 6 个面，永远能出结果。
#       凸包(高模)：先采样顶点建轻量凸包，再聚合成平面候选，适合非盒子状高模。
#   - 绿面对齐：给两个物体（先目标、后待移动）各生成一组绿色候选面，
#     分别点选一个面片后把待移动物体旋转+平移到贴合位置。
#   - 候选面生成后会挂 SelectionChanged 监听，点选绿色面片即触发放置。
#   原独立插件的窗口 / 菜单 / 命令注册 / Y-Z up / 一键摆放 / 贴地 / 轴心
#   等部分未搬运；UI 由 LingJuGeometryTools.create_quick_bottom_face_layout
#   提供，提示统一走 lj_info / lj_warn，maya.api.OpenMaya 用本文件已有的
#   oma 别名。
# =========================================================================

BF_TITLE = u"快速底面"
BF_MODE_MENU = "LingJu_bottomFaceModeMenu"
BF_GROUP_NAME = "LingJu_bottomFaceChoices_GRP"
BF_MATERIAL = "LingJu_bottomFaceChoice_MAT"
BF_CHOICE_ATTR = "lingjuBottomFaceChoice"
BF_TARGET_ATTR = "lingjuBottomFaceTarget"
BF_LABEL_ATTR = "lingjuBottomFaceLabel"
BF_MODE_ATTR = "lingjuBottomFaceMode"
BF_ROLE_ATTR = "lingjuBottomFaceRole"
BF_NORMAL_ATTRS = (
    "lingjuBottomFaceNormalX",
    "lingjuBottomFaceNormalY",
    "lingjuBottomFaceNormalZ",
)
BF_CENTER_ATTRS = (
    "lingjuBottomFaceCenterX",
    "lingjuBottomFaceCenterY",
    "lingjuBottomFaceCenterZ",
)
BF_MAX_POINTS = 5000
BF_MAX_ABS_COORDINATE = 1.0e9
BF_HULL_HEAVY_FACE_NOTE = 50000
BF_COLLISION_HULL_POINTS = 72
BF_HULL_CHOICE_POINTS = 420
BF_HULL_MAX_CHOICES = 24
BF_HULL_MIN_AREA_RATIO = 0.02
BF_COLLISION_NORMAL_TOLERANCE = 0.985

# SelectionChanged 监听：job id 记在模块全局，listJobs 里靠回调函数名兜底清理
BF_JOB_MARK = "_bf_selection_changed"
_BF_SCRIPT_JOB = None
_BF_APPLYING = False
_BF_ATTACH_PENDING = None

import traceback as _bf_traceback


def _bf_message(text):
    """信息级提示：统一走视口提示出口"""
    lj_info(u"%s：%s" % (BF_TITLE, text), pos="topCenter", fadeStayTime=2.0)


def _bf_warning(text):
    """警告级提示：统一走视口提示出口"""
    lj_warn(u"%s：%s" % (BF_TITLE, text), pos="topCenter", fadeStayTime=2.5)


def _bf_mode_from_ui():
    """读折叠栏里的候选模式下拉：凸包 -> hull，其余 -> six"""
    try:
        if cmds.optionMenu(BF_MODE_MENU, exists=True):
            value = cmds.optionMenu(BF_MODE_MENU, query=True, value=True)
            if value and u"凸包" in value:
                return "hull"
    except RuntimeError:
        pass

    return "six"


def _bf_as_vector(value):
    if value is None:
        return None

    try:
        return oma.MVector(float(value.x), float(value.y), float(value.z))
    except (AttributeError, TypeError, ValueError):
        pass

    try:
        values = list(value)
    except TypeError:
        return None

    if len(values) < 3:
        return None

    try:
        return oma.MVector(float(values[0]), float(values[1]), float(values[2]))
    except (TypeError, ValueError):
        return None


def _bf_is_finite_number(value):
    return math.isfinite(float(value)) and abs(float(value)) <= BF_MAX_ABS_COORDINATE


def _bf_is_finite_vector(point):
    point = _bf_as_vector(point)
    if point is None:
        return False

    return (
        _bf_is_finite_number(point.x)
        and _bf_is_finite_number(point.y)
        and _bf_is_finite_number(point.z)
    )


def _bf_bbox_from_points(points):
    safe_points = []
    for point in points:
        vector = _bf_as_vector(point)
        if vector is not None and _bf_is_finite_vector(vector):
            safe_points.append(vector)

    if not safe_points:
        return None

    xs = [point.x for point in safe_points]
    ys = [point.y for point in safe_points]
    zs = [point.z for point in safe_points]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _bf_unique_vectors(points, precision=5):
    unique = {}
    for point in points:
        point = _bf_as_vector(point)
        if point is None or not _bf_is_finite_vector(point):
            continue

        key = (
            round(point.x, precision),
            round(point.y, precision),
            round(point.z, precision),
        )
        unique[key] = point

    return list(unique.values())


def _bf_stride_sample(items, max_count):
    if not max_count or len(items) <= max_count:
        return items

    stride = max(1, (len(items) + max_count - 1) // max_count)
    return items[::stride][:max_count]


def _bf_vector_cross(left, right):
    return oma.MVector(
        left.y * right.z - left.z * right.y,
        left.z * right.x - left.x * right.z,
        left.x * right.y - left.y * right.x,
    )


def _bf_dag_path(node):
    selection = oma.MSelectionList()
    selection.add(node)
    return selection.getDagPath(0)


def _bf_mesh_shapes(transform):
    shapes = (
        cmds.listRelatives(
            transform,
            allDescendents=True,
            fullPath=True,
            shapes=True,
            noIntermediate=True,
        )
        or []
    )
    return [shape for shape in shapes if cmds.nodeType(shape) == "mesh"]


def _bf_selected_transforms():
    """Return unique selected transform nodes, accepting shapes/components too."""
    selection = cmds.ls(selection=True, long=True, flatten=True) or []
    transforms = []

    for item in selection:
        node = item.split(".", 1)[0]
        if not cmds.objExists(node):
            continue

        if cmds.nodeType(node) == "transform":
            transform = node
        else:
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            if not parents:
                continue
            transform = parents[0]

        if transform not in transforms:
            transforms.append(transform)

    return transforms


def _bf_ordered_selected_transforms():
    """Return selected transforms in Maya selection order when available."""
    try:
        cmds.selectPref(trackSelectionOrder=True)
    except RuntimeError:
        pass

    selection = cmds.ls(orderedSelection=True, long=True, flatten=True) or []
    if not selection:
        selection = cmds.ls(selection=True, long=True, flatten=True) or []

    transforms = []
    for item in selection:
        node = item.split(".", 1)[0]
        if not cmds.objExists(node):
            continue

        if cmds.nodeType(node) == "transform":
            transform = node
        else:
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            if not parents:
                continue
            transform = parents[0]

        if transform not in transforms:
            transforms.append(transform)

    return transforms


def _bf_world_bounding_box(transform):
    shapes = (
        cmds.listRelatives(
            transform,
            allDescendents=True,
            fullPath=True,
            shapes=True,
            noIntermediate=True,
        )
        or []
    )
    targets = shapes or [transform]

    try:
        return cmds.exactWorldBoundingBox(targets, ignoreInvisible=True)
    except TypeError:
        return cmds.exactWorldBoundingBox(targets)


def _bf_up_axis():
    up_axis = (cmds.upAxis(query=True, axis=True) or "y").lower()
    if up_axis == "z":
        return 2, oma.MVector(0.0, 0.0, 1.0)

    return 1, oma.MVector(0.0, 1.0, 0.0)


def _bf_short_name(node):
    return node.rsplit("|", 1)[-1]


def _bf_strip_namespace(name):
    return name.rsplit(":", 1)[-1]


def _bf_clean_base_name(name):
    cleaned = []
    previous_was_underscore = False

    for char in name.strip():
        if char.isalnum() or char == "_":
            replacement = char
        else:
            replacement = "_"

        if replacement == "_":
            if previous_was_underscore:
                continue
            previous_was_underscore = True
        else:
            previous_was_underscore = False

        cleaned.append(replacement)

    base = "".join(cleaned).strip("_")
    if base and base[0].isdigit():
        base = "_" + base

    return base


def _bf_has_attr(node, attr):
    return cmds.objExists("%s.%s" % (node, attr))


def _bf_safe_set_attr(node_attr, value, attr_type=None):
    try:
        if attr_type:
            cmds.setAttr(node_attr, value, type=attr_type)
        else:
            cmds.setAttr(node_attr, value)
        return True
    except Exception:
        return False


def _bf_set_string_attr(node, attr, value):
    if not _bf_has_attr(node, attr):
        try:
            cmds.addAttr(node, longName=attr, dataType="string")
        except RuntimeError:
            pass
    return _bf_safe_set_attr("%s.%s" % (node, attr), str(value), "string")


def _bf_set_bool_attr(node, attr, value):
    if not _bf_has_attr(node, attr):
        try:
            cmds.addAttr(node, longName=attr, attributeType="bool", defaultValue=False)
        except RuntimeError:
            pass
    return _bf_safe_set_attr("%s.%s" % (node, attr), bool(value))


def _bf_set_double_attr(node, attr, value):
    if not _bf_has_attr(node, attr):
        cmds.addAttr(node, longName=attr, attributeType="double")
    cmds.setAttr("%s.%s" % (node, attr), float(value))


def _bf_string_attr_or_default(node, attr, default=""):
    if not _bf_has_attr(node, attr):
        return default

    try:
        value = cmds.getAttr("%s.%s" % (node, attr))
    except RuntimeError:
        return default

    return value if value is not None else default


def _bf_matrix_element(matrix, row, column):
    try:
        return matrix(row, column)
    except (AttributeError, TypeError):
        pass

    try:
        return matrix.getElement(row, column)
    except (AttributeError, TypeError):
        pass

    row_value = matrix[row]
    if isinstance(row_value, (list, tuple)):
        return row_value[column]

    return matrix[row * 4 + column]


def _bf_matrix_values(matrix):
    return [
        _bf_matrix_element(matrix, row, column)
        for row in range(4)
        for column in range(4)
    ]


def _bf_translation_matrix(vector):
    return oma.MMatrix(
        [
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            vector.x,
            vector.y,
            vector.z,
            1.0,
        ]
    )


def _bf_world_matrix(transform):
    return oma.MMatrix(cmds.xform(transform, query=True, worldSpace=True, matrix=True))


def _bf_world_rotate_pivot(transform):
    pivot = cmds.xform(transform, query=True, worldSpace=True, rotatePivot=True)
    return oma.MVector(pivot[0], pivot[1], pivot[2])


def _bf_rotated_point(point, pivot, quaternion):
    return (point - pivot).rotateBy(quaternion) + pivot


def _bf_apply_world_rotation(transform, quaternion):
    if abs(quaternion.x) <= 1.0e-10 and abs(quaternion.y) <= 1.0e-10 and abs(quaternion.z) <= 1.0e-10:
        return

    pivot = _bf_world_rotate_pivot(transform)
    current_matrix = _bf_world_matrix(transform)
    rotation_matrix = quaternion.asMatrix()
    new_matrix = (
        current_matrix
        * _bf_translation_matrix(pivot * -1.0)
        * rotation_matrix
        * _bf_translation_matrix(pivot)
    )

    cmds.xform(transform, worldSpace=True, matrix=_bf_matrix_values(new_matrix))


def _bf_rotate_normal_to_vector(transform, source_normal, target_normal):
    if source_normal.length() <= 1.0e-8 or target_normal.length() <= 1.0e-8:
        return

    source_normal.normalize()
    target_normal.normalize()

    if source_normal * target_normal > 0.9999:
        return

    delta_quaternion = source_normal.rotateTo(target_normal)
    _bf_apply_world_rotation(transform, delta_quaternion)


def _bf_snap_to_ground(transform):
    up_index, _ = _bf_up_axis()
    bbox = _bf_world_bounding_box(transform)
    offset = -bbox[up_index]

    move_vector = [0.0, 0.0, 0.0]
    move_vector[up_index] = offset

    cmds.move(
        move_vector[0],
        move_vector[1],
        move_vector[2],
        transform,
        relative=True,
        worldSpace=True,
    )


def _bf_clean_history_and_transforms(transforms):
    warnings = []

    for transform in transforms:
        try:
            cmds.delete(transform, constructionHistory=True)
        except RuntimeError as error:
            warnings.append("%s history: %s" % (_bf_short_name(transform), error))

    for transform in transforms:
        try:
            cmds.makeIdentity(
                transform,
                apply=True,
                translate=True,
                rotate=True,
                scale=True,
                normal=False,
            )
        except RuntimeError as error:
            warnings.append("%s transform: %s" % (_bf_short_name(transform), error))

    for transform in transforms:
        try:
            cmds.delete(transform, constructionHistory=True)
        except RuntimeError:
            pass

    return warnings


def _bf_finish_placement(target):
    _bf_snap_to_ground(target)
    warnings = _bf_clean_history_and_transforms([target])
    _bf_snap_to_ground(target)
    return warnings


def _bf_polygon_area_from_points(points):
    if len(points) < 3:
        return 0.0

    origin = points[0]
    area = 0.0
    for index in range(1, len(points) - 1):
        area += _bf_vector_cross(
            points[index] - origin,
            points[index + 1] - origin,
        ).length() * 0.5

    return area


def _bf_polygon_area_2d(points):
    if len(points) < 3:
        return 0.0

    area = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        area += point[0] * next_point[1] - next_point[0] * point[1]

    return abs(area) * 0.5


def _bf_basis_from_normal(normal):
    normal = _bf_as_vector(normal)
    if normal is None:
        normal = oma.MVector(0.0, 1.0, 0.0)
    normal.normalize()

    reference = oma.MVector(1.0, 0.0, 0.0)
    if abs(normal * reference) > 0.9:
        reference = oma.MVector(0.0, 1.0, 0.0)

    axis_u = _bf_vector_cross(reference, normal)
    if axis_u.length() <= 1.0e-8:
        axis_u = _bf_vector_cross(oma.MVector(0.0, 0.0, 1.0), normal)
    axis_u.normalize()

    axis_v = _bf_vector_cross(normal, axis_u)
    axis_v.normalize()

    return axis_u, axis_v


def _bf_convex_hull_2d(points):
    unique = sorted(
        {
            (round(point[0], 6), round(point[1], 6)): point
            for point in points
        }.values()
    )
    if len(unique) <= 2:
        return unique

    def cross(origin, point_a, point_b):
        return (
            (point_a[0] - origin[0]) * (point_b[1] - origin[1])
            - (point_a[1] - origin[1]) * (point_b[0] - origin[0])
        )

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 1.0e-8:
            lower.pop()
        lower.append(point)

    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 1.0e-8:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def _bf_distance_to_line(point, line_start, line_end):
    direction = line_end - line_start
    length = direction.length()
    if length <= 1.0e-12:
        return 0.0

    return _bf_vector_cross(point - line_start, direction).length() / length


def _bf_make_oriented_hull_face(points, index_a, index_b, index_c, inside_point):
    point_a = points[index_a]
    point_b = points[index_b]
    point_c = points[index_c]
    normal = _bf_vector_cross(point_b - point_a, point_c - point_a)
    normal_length = normal.length()
    if normal_length <= 1.0e-12:
        return None

    normal *= 1.0 / normal_length
    support = point_a * normal

    if inside_point is not None and inside_point * normal - support > 0.0:
        index_b, index_c = index_c, index_b
        normal *= -1.0
        support = point_a * normal

    return {
        "verts": (index_a, index_b, index_c),
        "normal": normal,
        "support": support,
    }


def _bf_initial_tetrahedron(points, diagonal):
    if len(points) < 4:
        return None

    min_x, min_y, min_z, max_x, max_y, max_z = _bf_bbox_from_points(points)
    extents = (
        max_x - min_x,
        max_y - min_y,
        max_z - min_z,
    )
    axis = max(range(3), key=lambda item: extents[item])

    def axis_value(point):
        return (point.x, point.y, point.z)[axis]

    index_a = min(range(len(points)), key=lambda item: axis_value(points[item]))
    index_b = max(range(len(points)), key=lambda item: axis_value(points[item]))
    if index_a == index_b or (points[index_b] - points[index_a]).length() <= max(diagonal * 1.0e-8, 1.0e-8):
        return None

    index_c = max(
        range(len(points)),
        key=lambda item: _bf_distance_to_line(points[item], points[index_a], points[index_b]),
    )
    if _bf_distance_to_line(points[index_c], points[index_a], points[index_b]) <= max(diagonal * 1.0e-8, 1.0e-8):
        return None

    base_normal = _bf_vector_cross(
        points[index_b] - points[index_a],
        points[index_c] - points[index_a],
    )
    if base_normal.length() <= 1.0e-12:
        return None
    base_normal.normalize()
    base_support = points[index_a] * base_normal

    index_d = max(
        range(len(points)),
        key=lambda item: abs(points[item] * base_normal - base_support),
    )
    if abs(points[index_d] * base_normal - base_support) <= max(diagonal * 1.0e-8, 1.0e-8):
        return None

    return index_a, index_b, index_c, index_d


def _bf_convex_hull_3d(points):
    """Build a triangulated convex hull from world-space points.

    This is deliberately Maya-command-free: no polyReduce, no booleans, no
    temporary scene geometry. It keeps high-poly models from crashing Maya while
    still producing the actual outer hull triangles.
    """
    points = _bf_unique_vectors(points, precision=6)
    if len(points) < 4:
        return points, []

    bbox = _bf_bbox_from_points(points)
    if not bbox:
        return points, []

    min_x, min_y, min_z, max_x, max_y, max_z = bbox
    diagonal = oma.MVector(max_x - min_x, max_y - min_y, max_z - min_z).length()
    if diagonal <= 1.0e-8:
        return points, []

    tetra = _bf_initial_tetrahedron(points, diagonal)
    if not tetra:
        return points, []

    index_a, index_b, index_c, index_d = tetra
    inside_point = (
        points[index_a] + points[index_b] + points[index_c] + points[index_d]
    ) * 0.25
    face_specs = (
        (index_a, index_c, index_b),
        (index_a, index_b, index_d),
        (index_b, index_c, index_d),
        (index_c, index_a, index_d),
    )
    faces = []
    for spec in face_specs:
        face = _bf_make_oriented_hull_face(points, spec[0], spec[1], spec[2], inside_point)
        if face:
            faces.append(face)

    if len(faces) < 4:
        return points, []

    epsilon = max(diagonal * 1.0e-7, 1.0e-6)
    initial = set(tetra)
    remaining = [index for index in range(len(points)) if index not in initial]
    remaining.sort(key=lambda item: (points[item] - inside_point).length(), reverse=True)

    for point_index in remaining:
        point = points[point_index]
        visible_indices = []
        for face_index, face in enumerate(faces):
            if point * face["normal"] - face["support"] > epsilon:
                visible_indices.append(face_index)

        if not visible_indices:
            continue

        visible_set = set(visible_indices)
        edge_records = {}
        for face_index in visible_indices:
            verts = faces[face_index]["verts"]
            for edge in ((verts[0], verts[1]), (verts[1], verts[2]), (verts[2], verts[0])):
                key = tuple(sorted(edge))
                record = edge_records.get(key)
                if record is None:
                    edge_records[key] = {"edge": edge, "count": 1}
                else:
                    record["count"] += 1

        horizon_edges = [
            record["edge"]
            for record in edge_records.values()
            if record["count"] == 1
        ]
        if len(horizon_edges) < 3:
            continue

        faces = [
            face
            for face_index, face in enumerate(faces)
            if face_index not in visible_set
        ]

        seen_faces = {tuple(sorted(face["verts"])) for face in faces}
        for edge_start, edge_end in horizon_edges:
            face = _bf_make_oriented_hull_face(
                points,
                edge_start,
                edge_end,
                point_index,
                inside_point,
            )
            if not face:
                continue

            key = tuple(sorted(face["verts"]))
            if key in seen_faces:
                continue

            faces.append(face)
            seen_faces.add(key)

    return points, faces


def _bf_collision_hull_directions():
    directions = []

    for x in (-1.0, 0.0, 1.0):
        for y in (-1.0, 0.0, 1.0):
            for z in (-1.0, 0.0, 1.0):
                if x == 0.0 and y == 0.0 and z == 0.0:
                    continue
                direction = oma.MVector(x, y, z)
                direction.normalize()
                directions.append(direction)

    golden_angle = math.pi * (3.0 - math.sqrt(5.0))
    for index in range(54):
        z = 1.0 - (2.0 * (index + 0.5) / 54.0)
        radius = math.sqrt(max(0.0, 1.0 - z * z))
        theta = golden_angle * index
        direction = oma.MVector(
            math.cos(theta) * radius,
            math.sin(theta) * radius,
            z,
        )
        if direction.length() > 1.0e-8:
            direction.normalize()
            directions.append(direction)

    clean = []
    for direction in directions:
        duplicate = False
        for existing in clean:
            if direction * existing > 0.995:
                duplicate = True
                break
        if not duplicate:
            clean.append(direction)

    return clean


def _bf_collision_hull_points(points, max_points=BF_COLLISION_HULL_POINTS):
    if len(points) <= max_points:
        return points

    selected = {}
    for direction in _bf_collision_hull_directions():
        best_point = None
        best_value = None
        for point in points:
            value = point * direction
            if best_value is None or value > best_value:
                best_value = value
                best_point = point

        if best_point is not None:
            key = (
                round(best_point.x, 5),
                round(best_point.y, 5),
                round(best_point.z, 5),
            )
            selected[key] = best_point

    hull_points = list(selected.values())
    if len(hull_points) <= max_points:
        return hull_points

    center = sum(hull_points, oma.MVector()) / float(len(hull_points))
    hull_points.sort(key=lambda point: (point - center).length(), reverse=True)
    return hull_points[:max_points]


def _bf_hull_faces_to_planes(points, faces, center, diagonal):
    if not points or not faces:
        return []

    support_tolerance = max(diagonal * 0.003, 0.005)
    display_offset = max(diagonal * 0.006, 0.02)
    shrink = 0.9
    min_area = max(diagonal * diagonal * 0.00001, 1.0e-6)
    plane_bins = []

    for face in faces:
        normal = _bf_as_vector(face["normal"])
        if normal is None:
            continue
        if normal.length() <= 1.0e-8:
            continue
        normal.normalize()
        verts = face["verts"]
        area = _bf_vector_cross(
            points[verts[1]] - points[verts[0]],
            points[verts[2]] - points[verts[0]],
        ).length() * 0.5
        if area <= 1.0e-10:
            continue

        support = max(points[index] * normal for index in verts)
        matched = None
        for plane_bin in plane_bins:
            if (
                normal * plane_bin["normal"] >= BF_COLLISION_NORMAL_TOLERANCE
                and abs(support - plane_bin["support"]) <= support_tolerance
            ):
                matched = plane_bin
                break

        if matched is None:
            plane_bins.append(
                {
                    "normal": normal,
                    "support": support,
                    "weighted": normal * area,
                    "area_hint": area,
                    "verts": set(verts),
                }
            )
        else:
            matched["weighted"] += normal * area
            matched["area_hint"] += area
            matched["verts"].update(verts)

    candidates = []
    for index, plane_bin in enumerate(plane_bins):
        normal = _bf_as_vector(plane_bin["weighted"])
        if normal is None:
            continue
        if normal.length() <= 1.0e-8:
            normal = _bf_as_vector(plane_bin["normal"])
            if normal is None:
                continue
        normal.normalize()
        support = max(point * normal for point in points)

        support_points = []
        for point in points:
            distance = support - point * normal
            if -support_tolerance <= distance <= support_tolerance:
                support_points.append(point + normal * distance)

        for vertex_index in plane_bin["verts"]:
            point = points[vertex_index]
            support_points.append(point + normal * (support - point * normal))

        support_points = _bf_unique_vectors(support_points, precision=5)
        if len(support_points) < 3:
            continue

        axis_u, axis_v = _bf_basis_from_normal(normal)
        projected = [(point * axis_u, point * axis_v) for point in support_points]
        hull = _bf_convex_hull_2d(projected)
        area = _bf_polygon_area_2d(hull)
        if len(hull) < 3 or area <= min_area:
            continue

        ordered_points = [
            axis_u * point[0] + axis_v * point[1] + normal * support
            for point in hull
        ]
        face_center = sum(ordered_points, oma.MVector()) / float(len(ordered_points))
        if normal * (face_center - center) < 0.0:
            normal *= -1.0
            ordered_points.reverse()

        display_points = [
            face_center + (point - face_center) * shrink + normal * display_offset
            for point in ordered_points
        ]
        if any(not _bf_is_finite_vector(point) for point in display_points):
            continue

        candidates.append(
            {
                "label": "hull%02d" % (index + 1),
                "normal": normal,
                "center": face_center,
                "points": display_points,
                "area": area,
            }
        )

    candidates.sort(key=lambda item: item["area"], reverse=True)
    if not candidates:
        return []

    largest_area = candidates[0]["area"]
    filtered = [
        candidate
        for candidate in candidates
        if candidate["area"] >= largest_area * BF_HULL_MIN_AREA_RATIO
    ]
    return filtered[:BF_HULL_MAX_CHOICES]


def _bf_hull_candidates(transform):
    world_points = _bf_mesh_world_points(transform, max_points=0)
    if len(world_points) < 4:
        return []

    bbox = _bf_bbox_from_points(world_points)
    if not bbox:
        return []

    min_x, min_y, min_z, max_x, max_y, max_z = bbox
    center = oma.MVector(
        (min_x + max_x) * 0.5,
        (min_y + max_y) * 0.5,
        (min_z + max_z) * 0.5,
    )
    diagonal = oma.MVector(max_x - min_x, max_y - min_y, max_z - min_z).length()
    if diagonal <= 1.0e-8:
        return []

    choice_points = _bf_collision_hull_points(
        world_points,
        max_points=BF_HULL_CHOICE_POINTS,
    )
    if len(choice_points) < 4:
        choice_points = world_points

    hull_points, hull_faces = _bf_convex_hull_3d(choice_points)
    if len(hull_points) < 4 or len(hull_faces) < 4:
        return []

    candidates = _bf_hull_faces_to_planes(
        hull_points,
        hull_faces,
        center,
        diagonal,
    )
    if candidates:
        return candidates

    return _bf_bbox_candidates(transform)


def _bf_bbox_face_specs(bbox):
    min_x, min_y, min_z, max_x, max_y, max_z = bbox
    return (
        (
            "posX",
            (1.0, 0.0, 0.0),
            (
                (max_x, min_y, min_z),
                (max_x, max_y, min_z),
                (max_x, max_y, max_z),
                (max_x, min_y, max_z),
            ),
        ),
        (
            "negX",
            (-1.0, 0.0, 0.0),
            (
                (min_x, min_y, min_z),
                (min_x, min_y, max_z),
                (min_x, max_y, max_z),
                (min_x, max_y, min_z),
            ),
        ),
        (
            "posY",
            (0.0, 1.0, 0.0),
            (
                (min_x, max_y, min_z),
                (min_x, max_y, max_z),
                (max_x, max_y, max_z),
                (max_x, max_y, min_z),
            ),
        ),
        (
            "negY",
            (0.0, -1.0, 0.0),
            (
                (min_x, min_y, min_z),
                (max_x, min_y, min_z),
                (max_x, min_y, max_z),
                (min_x, min_y, max_z),
            ),
        ),
        (
            "posZ",
            (0.0, 0.0, 1.0),
            (
                (min_x, min_y, max_z),
                (max_x, min_y, max_z),
                (max_x, max_y, max_z),
                (min_x, max_y, max_z),
            ),
        ),
        (
            "negZ",
            (0.0, 0.0, -1.0),
            (
                (min_x, min_y, min_z),
                (min_x, max_y, min_z),
                (max_x, max_y, min_z),
                (max_x, min_y, min_z),
            ),
        ),
    )


def _bf_bbox_candidates(transform):
    bbox = _bf_world_bounding_box(transform)
    if not bbox or len(bbox) < 6:
        return []

    min_x, min_y, min_z, max_x, max_y, max_z = [float(value) for value in bbox[:6]]
    world_center = oma.MVector(
        (min_x + max_x) * 0.5,
        (min_y + max_y) * 0.5,
        (min_z + max_z) * 0.5,
    )
    world_corners = [
        oma.MVector(x, y, z)
        for x in (min_x, max_x)
        for y in (min_y, max_y)
        for z in (min_z, max_z)
    ]
    diagonal = max((corner - world_center).length() for corner in world_corners) * 2.0
    if diagonal <= 1.0e-8:
        return []

    offset = max(diagonal * 0.018, 0.05)
    shrink = 0.32
    candidates = []

    for label, normal_values, point_values in _bf_bbox_face_specs(bbox):
        world_points = [oma.MVector(*point) for point in point_values]
        area = _bf_polygon_area_from_points(world_points)
        if area <= 1.0e-8:
            continue

        face_center = sum(world_points, oma.MVector()) / float(len(world_points))
        normal = oma.MVector(*normal_values)
        if normal.length() <= 1.0e-8:
            normal = _bf_vector_cross(
                world_points[1] - world_points[0],
                world_points[2] - world_points[0],
            )

        if normal.length() <= 1.0e-8:
            continue

        normal.normalize()
        if normal * (face_center - world_center) < 0.0:
            normal *= -1.0

        display_points = [
            face_center + (point - face_center) * shrink + normal * offset
            for point in world_points
        ]
        candidates.append(
            {
                "label": label,
                "normal": normal,
                "center": face_center,
                "points": display_points,
                "area": area,
            }
        )

    candidates.sort(key=lambda item: item["area"], reverse=True)
    return candidates


def _bf_mesh_world_points(transform, max_points=BF_MAX_POINTS):
    points = []

    for shape in _bf_mesh_shapes(transform):
        try:
            mesh_fn = oma.MFnMesh(_bf_dag_path(shape))
        except RuntimeError:
            continue

        vertex_count = mesh_fn.numVertices
        vertex_stride = 1
        if max_points and vertex_count > max_points:
            vertex_stride = max(1, (vertex_count + max_points - 1) // max_points)

        for vertex_index in range(0, vertex_count, vertex_stride):
            try:
                point = _bf_as_vector(mesh_fn.getPoint(vertex_index, oma.MSpace.kWorld))
            except RuntimeError:
                continue
            if point is not None and _bf_is_finite_vector(point):
                points.append(point)

    points = _bf_unique_vectors(points)
    return _bf_stride_sample(points, max_points)


def _bf_mesh_complexity(transform):
    vertex_count = 0
    polygon_count = 0

    for shape in _bf_mesh_shapes(transform):
        try:
            vertices = cmds.polyEvaluate(shape, vertex=True)
            polygons = cmds.polyEvaluate(shape, face=True)
        except RuntimeError:
            continue

        vertex_count += int(vertices or 0)
        polygon_count += int(polygons or 0)

    return vertex_count, polygon_count


def _bf_can_build_hull_candidates(transform):
    vertex_count, polygon_count = _bf_mesh_complexity(transform)
    if vertex_count <= 0 or polygon_count <= 0:
        _bf_warning(u"凸包模式需要一个 polygon 网格物体")
        return False

    return True


def _bf_direct_hull_candidates(transform):
    vertex_count, polygon_count = _bf_mesh_complexity(transform)
    if polygon_count > BF_HULL_HEAVY_FACE_NOTE:
        _bf_message(u"凸包：用 %d 个顶点先构建轻量包围壳" % vertex_count)

    return _bf_hull_candidates(transform)


def _bf_candidates_for_transform(transform, mode):
    if mode == "hull":
        if not _bf_can_build_hull_candidates(transform):
            return []

        return _bf_direct_hull_candidates(transform)

    return _bf_bbox_candidates(transform)


def _bf_ensure_material():
    material = BF_MATERIAL
    if not cmds.objExists(material):
        material = cmds.shadingNode("lambert", asShader=True, name=material)

    if _bf_has_attr(material, "color"):
        cmds.setAttr("%s.color" % material, 0.0, 0.85, 0.35, type="double3")
    if _bf_has_attr(material, "transparency"):
        cmds.setAttr("%s.transparency" % material, 0.55, 0.55, 0.55, type="double3")
    if _bf_has_attr(material, "incandescence"):
        cmds.setAttr("%s.incandescence" % material, 0.0, 0.12, 0.04, type="double3")

    shading_group = "%s_SG" % material
    if not cmds.objExists(shading_group):
        shading_group = cmds.sets(
            renderable=True,
            noSurfaceShader=True,
            empty=True,
            name=shading_group,
        )
        cmds.connectAttr(
            "%s.outColor" % material,
            "%s.surfaceShader" % shading_group,
            force=True,
        )

    return material, shading_group


def _bf_create_face_transform(name, vectors):
    points = [(point.x, point.y, point.z) for point in vectors]
    try:
        created = cmds.polyCreateFacet(
            point=points,
            constructionHistory=False,
            name=name,
        )
        return created[0] if isinstance(created, (list, tuple)) else created
    except RuntimeError:
        pass

    pieces = []
    for index in range(1, len(vectors) - 1):
        triangle = [vectors[0], vectors[index], vectors[index + 1]]
        try:
            created = cmds.polyCreateFacet(
                point=[(point.x, point.y, point.z) for point in triangle],
                constructionHistory=False,
                name="%s_tri%02d" % (name, index),
            )
        except RuntimeError:
            continue

        piece = created[0] if isinstance(created, (list, tuple)) else created
        pieces.append(piece)

    if not pieces:
        return None

    if len(pieces) == 1:
        try:
            return cmds.rename(pieces[0], name)
        except RuntimeError:
            return pieces[0]

    try:
        combined = cmds.polyUnite(
            pieces,
            constructionHistory=False,
            mergeUVSets=True,
            name=name,
        )[0]
        try:
            cmds.delete(pieces)
        except RuntimeError:
            pass
        return combined
    except RuntimeError:
        group = cmds.group(pieces, name=name)
        return group


def _bf_choice_name(target, label):
    base = _bf_clean_base_name(_bf_strip_namespace(_bf_short_name(target))) or "model"
    return "LJ_bottomFace_%s_%s" % (base, label)


def _bf_create_choice(
    target,
    group,
    candidate,
    shading_group,
    choice_mode="place",
    attach_role="",
):
    vectors = []
    seen = set()
    for point in candidate["points"]:
        vector = _bf_as_vector(point)
        if vector is None or not _bf_is_finite_vector(vector):
            continue

        key = (round(vector.x, 5), round(vector.y, 5), round(vector.z, 5))
        if key in seen:
            continue

        seen.add(key)
        vectors.append(vector)

    if len(vectors) < 3:
        return None

    name = _bf_choice_name(target, candidate["label"])
    choice = _bf_create_face_transform(name, vectors)
    if not choice or not cmds.objExists(choice):
        return None

    choice = cmds.parent(choice, group)[0]

    _bf_set_bool_attr(choice, BF_CHOICE_ATTR, True)
    _bf_set_string_attr(choice, BF_TARGET_ATTR, target)
    _bf_set_string_attr(choice, BF_LABEL_ATTR, candidate["label"])
    _bf_set_string_attr(choice, BF_MODE_ATTR, choice_mode)
    _bf_set_string_attr(choice, BF_ROLE_ATTR, attach_role)
    _bf_set_double_attr(choice, BF_NORMAL_ATTRS[0], candidate["normal"].x)
    _bf_set_double_attr(choice, BF_NORMAL_ATTRS[1], candidate["normal"].y)
    _bf_set_double_attr(choice, BF_NORMAL_ATTRS[2], candidate["normal"].z)
    center = _bf_as_vector(candidate.get("center"))
    if center is None:
        center = sum(vectors, oma.MVector()) / float(len(vectors))
    _bf_set_double_attr(choice, BF_CENTER_ATTRS[0], center.x)
    _bf_set_double_attr(choice, BF_CENTER_ATTRS[1], center.y)
    _bf_set_double_attr(choice, BF_CENTER_ATTRS[2], center.z)

    shapes = cmds.listRelatives(choice, shapes=True, fullPath=True) or []
    for shape in shapes:
        try:
            cmds.sets(shape, edit=True, forceElement=shading_group)
        except RuntimeError:
            pass
        if _bf_has_attr(shape, "visibility"):
            cmds.setAttr("%s.visibility" % shape, True)
        if _bf_has_attr(shape, "intermediateObject"):
            cmds.setAttr("%s.intermediateObject" % shape, False)
        if _bf_has_attr(shape, "doubleSided"):
            cmds.setAttr("%s.doubleSided" % shape, True)
        if _bf_has_attr(shape, "displayEdges"):
            cmds.setAttr("%s.displayEdges" % shape, True)
        if _bf_has_attr(shape, "wireframeOnShaded"):
            cmds.setAttr("%s.wireframeOnShaded" % shape, False)
        if _bf_has_attr(shape, "backfaceCulling"):
            cmds.setAttr("%s.backfaceCulling" % shape, 0)
        if _bf_has_attr(shape, "castsShadows"):
            cmds.setAttr("%s.castsShadows" % shape, False)
        if _bf_has_attr(shape, "receiveShadows"):
            cmds.setAttr("%s.receiveShadows" % shape, False)
        if _bf_has_attr(shape, "primaryVisibility"):
            cmds.setAttr("%s.primaryVisibility" % shape, False)
        if _bf_has_attr(shape, "overrideEnabled"):
            cmds.setAttr("%s.overrideEnabled" % shape, True)
        if _bf_has_attr(shape, "overrideColor"):
            cmds.setAttr("%s.overrideColor" % shape, 14)

    if _bf_has_attr(choice, "visibility"):
        cmds.setAttr("%s.visibility" % choice, True)
    if _bf_has_attr(choice, "overrideEnabled"):
        cmds.setAttr("%s.overrideEnabled" % choice, True)
    if _bf_has_attr(choice, "overrideColor"):
        cmds.setAttr("%s.overrideColor" % choice, 14)
    if _bf_has_attr(choice, "displayHandle"):
        cmds.setAttr("%s.displayHandle" % choice, True)

    return choice


def _bf_choice_groups():
    return cmds.ls("%s*" % BF_GROUP_NAME, long=True) or []


def _bf_candidate_from_node(node):
    node = node.split(".", 1)[0]
    if not cmds.objExists(node):
        return None

    if cmds.nodeType(node) != "transform":
        parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
        if not parents:
            return None
        node = parents[0]

    if not _bf_has_attr(node, BF_CHOICE_ATTR):
        return None

    try:
        if cmds.getAttr("%s.%s" % (node, BF_CHOICE_ATTR)):
            return node
    except RuntimeError:
        return None

    return None


def _bf_selected_candidate():
    for item in cmds.ls(selection=True, long=True, flatten=True) or []:
        candidate = _bf_candidate_from_node(item)
        if candidate:
            return candidate

    return None


def _bf_selected_candidates():
    candidates = []
    for item in cmds.ls(selection=True, long=True, flatten=True) or []:
        candidate = _bf_candidate_from_node(item)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    return candidates


def _bf_choice_mode(candidate):
    return _bf_string_attr_or_default(candidate, BF_MODE_ATTR, "place")


def _bf_choice_role(candidate):
    return _bf_string_attr_or_default(candidate, BF_ROLE_ATTR, "")


def _bf_resolve_target(candidate):
    try:
        target = cmds.getAttr("%s.%s" % (candidate, BF_TARGET_ATTR))
    except RuntimeError:
        return None

    matches = cmds.ls(target, long=True) or []
    if matches:
        return matches[0]

    short_matches = cmds.ls(_bf_short_name(target), long=True) or []
    if len(short_matches) == 1:
        return short_matches[0]

    return None


def _bf_normal_from_candidate(candidate):
    try:
        values = [
            cmds.getAttr("%s.%s" % (candidate, attr))
            for attr in BF_NORMAL_ATTRS
        ]
    except RuntimeError:
        return None

    normal = oma.MVector(values[0], values[1], values[2])
    if normal.length() <= 1.0e-8:
        return None

    normal.normalize()
    return normal


def _bf_center_from_candidate(candidate):
    try:
        values = [
            cmds.getAttr("%s.%s" % (candidate, attr))
            for attr in BF_CENTER_ATTRS
        ]
    except RuntimeError:
        return None

    center = oma.MVector(values[0], values[1], values[2])
    if not _bf_is_finite_vector(center):
        return None

    return center


def _bf_clear_groups_deferred(groups, kill_jobs=False):
    if kill_jobs:
        _bf_kill_jobs()

    groups = list(groups or _bf_choice_groups())
    if not groups:
        return

    command = (
        "import maya.cmds as cmds\n"
        "undo_state = cmds.undoInfo(query=True, state=True)\n"
        "try:\n"
        "    cmds.undoInfo(stateWithoutFlush=False)\n"
        "    for node in %r:\n"
        "        try:\n"
        "            if cmds.objExists(node):\n"
        "                cmds.delete(node)\n"
        "        except RuntimeError:\n"
        "            pass\n"
        "finally:\n"
        "    cmds.undoInfo(stateWithoutFlush=undo_state)\n"
    ) % groups

    try:
        cmds.evalDeferred(command, lowestPriority=True)
    except TypeError:
        cmds.evalDeferred(command)


def _bf_apply_candidate(candidate):
    global _BF_APPLYING

    if _BF_APPLYING:
        return

    target = _bf_resolve_target(candidate)
    normal = _bf_normal_from_candidate(candidate)
    if not target or not normal:
        _bf_warning(u"候选面已失效，请重新生成")
        bf_clear_choices()
        return

    choice_groups = _bf_choice_groups()
    _BF_APPLYING = True
    cmds.undoInfo(openChunk=True, chunkName=BF_TITLE + u" - 底面放置")

    try:
        _bf_kill_jobs()
        cmds.select(target, replace=True)
        _, up_vector = _bf_up_axis()
        _bf_rotate_normal_to_vector(target, normal, up_vector * -1.0)
        clean_warnings = _bf_finish_placement(target)
        cmds.select(target, replace=True)
        _bf_clear_groups_deferred(choice_groups, kill_jobs=False)
        if clean_warnings:
            _bf_warning(u"清理未全部完成：%s" % u"；".join(clean_warnings[:3]))
    finally:
        cmds.undoInfo(closeChunk=True)
        _BF_APPLYING = False

    _bf_message(u"已把 %s 按选中的底面朝下贴地" % _bf_short_name(target))


def _bf_apply_attach_candidates(candidates):
    global _BF_APPLYING
    global _BF_ATTACH_PENDING

    if _BF_APPLYING:
        return

    attach_candidates = [
        candidate
        for candidate in candidates
        if _bf_choice_mode(candidate) == "attach"
    ]
    if len(attach_candidates) < 2:
        current = attach_candidates[0] if attach_candidates else None
        pending = _BF_ATTACH_PENDING
        if (
            current
            and pending
            and current != pending
            and cmds.objExists(pending)
            and _bf_choice_mode(pending) == "attach"
        ):
            attach_candidates = [pending, current]
        elif current:
            _BF_ATTACH_PENDING = current
            _bf_message(u"再点另一个物体上的对应绿色面")
            return
        else:
            return

    target_choice = None
    moving_choice = None
    for candidate in attach_candidates:
        role = _bf_choice_role(candidate)
        if role == "target" and target_choice is None:
            target_choice = candidate
        elif role == "moving" and moving_choice is None:
            moving_choice = candidate

    if not target_choice or not moving_choice:
        if attach_candidates:
            _BF_ATTACH_PENDING = attach_candidates[-1]
        return

    target_transform = _bf_resolve_target(target_choice)
    moving_transform = _bf_resolve_target(moving_choice)
    target_normal = _bf_normal_from_candidate(target_choice)
    moving_normal = _bf_normal_from_candidate(moving_choice)
    target_center = _bf_center_from_candidate(target_choice)
    moving_center = _bf_center_from_candidate(moving_choice)
    if (
        not target_transform
        or not moving_transform
        or target_transform == moving_transform
        or not target_normal
        or not moving_normal
        or not target_center
        or not moving_center
    ):
        _bf_warning(u"请在两个物体上各点选一个绿色候选面")
        return

    choice_groups = _bf_choice_groups()
    target_normal.normalize()
    moving_normal.normalize()
    quaternion = moving_normal.rotateTo(target_normal * -1.0)

    _BF_APPLYING = True
    cmds.undoInfo(openChunk=True, chunkName=BF_TITLE + u" - 绿面吸附")
    try:
        _bf_kill_jobs()
        _BF_ATTACH_PENDING = None
        pivot = _bf_world_rotate_pivot(moving_transform)
        rotated_moving_center = _bf_rotated_point(moving_center, pivot, quaternion)
        _bf_apply_world_rotation(moving_transform, quaternion)
        move_vector = target_center - rotated_moving_center
        cmds.move(
            move_vector.x,
            move_vector.y,
            move_vector.z,
            moving_transform,
            relative=True,
            worldSpace=True,
        )
        cmds.select(moving_transform, replace=True)
        _bf_clear_groups_deferred(choice_groups, kill_jobs=False)
    finally:
        cmds.undoInfo(closeChunk=True)
        _BF_APPLYING = False

    _bf_message(u"已把 %s 按绿色面对齐到 %s"
                  % (_bf_short_name(moving_transform), _bf_short_name(target_transform)))


def bf_choose_bottom_face(*_):
    """Show safe selectable bottom-face candidates for one selected model."""
    candidate = _bf_selected_candidate()
    if candidate:
        _bf_apply_candidate(candidate)
        return

    transforms = [
        transform
        for transform in _bf_selected_transforms()
        if not _bf_candidate_from_node(transform)
    ]
    if len(transforms) != 1:
        _bf_warning(u"请先只选中一个网格物体")
        return

    target = transforms[0]
    mode = _bf_mode_from_ui()
    try:
        candidates = _bf_candidates_for_transform(target, mode)
    except RuntimeError as error:
        _bf_warning(u"生成候选面失败：%s" % error)
        return

    if mode == "hull" and not candidates:
        _bf_warning(u"凸包没有生成候选面，请改用六面安全模式")
        return

    if not candidates:
        _bf_warning(u"%s 没有可用的候选面" % _bf_short_name(target))
        return

    cmds.undoInfo(openChunk=True, chunkName=BF_TITLE + u" - 生成候选面")
    try:
        bf_clear_choices()
        group = cmds.group(empty=True, name=BF_GROUP_NAME)
        _bf_set_string_attr(group, BF_TARGET_ATTR, target)
        _, shading_group = _bf_ensure_material()

        choices = []
        for candidate_data in candidates:
            choice = _bf_create_choice(
                target,
                group,
                candidate_data,
                shading_group,
            )
            if choice:
                choices.append(choice)

        if not choices:
            bf_clear_choices(kill_jobs=False)
            _bf_warning(u"候选面创建失败")
            return

        cmds.select(target, replace=True)
        _bf_start_job()
    finally:
        cmds.undoInfo(closeChunk=True)

    _bf_message(u"已生成 %d 个绿色候选面，点选其中一个即可让 %s 该面朝下贴地"
                  % (len(choices), _bf_short_name(target)))


def bf_attach_by_faces(*_):
    """Create green face choices for two selected objects, then attach second to first."""
    transforms = [
        transform
        for transform in _bf_ordered_selected_transforms()
        if not _bf_candidate_from_node(transform)
    ]
    if len(transforms) != 2:
        _bf_warning(u"请先选目标物体，再选要移动的物体（共两个）")
        return

    target_transform, moving_transform = transforms
    mode = _bf_mode_from_ui()
    try:
        target_candidates = _bf_candidates_for_transform(target_transform, mode)
        moving_candidates = _bf_candidates_for_transform(moving_transform, mode)
    except RuntimeError as error:
        _bf_warning(u"生成对齐候选面失败：%s" % error)
        return

    if not target_candidates or not moving_candidates:
        _bf_warning(u"两个物体没有都生成出候选面，请检查选择")
        return

    cmds.undoInfo(openChunk=True, chunkName=BF_TITLE + u" - 生成对齐候选面")
    try:
        bf_clear_choices()
        group = cmds.group(empty=True, name=BF_GROUP_NAME)
        _bf_set_string_attr(group, BF_MODE_ATTR, "attach")
        _, shading_group = _bf_ensure_material()

        choices = []
        for transform, role, candidates in (
            (target_transform, "target", target_candidates),
            (moving_transform, "moving", moving_candidates),
        ):
            for candidate_data in candidates:
                choice = _bf_create_choice(
                    transform,
                    group,
                    candidate_data,
                    shading_group,
                    choice_mode="attach",
                    attach_role=role,
                )
                if choice:
                    choices.append(choice)

        if not choices:
            bf_clear_choices(kill_jobs=False)
            _bf_warning(u"对齐候选面创建失败")
            return

        cmds.select(transforms, replace=True)
        _bf_start_job()
    finally:
        cmds.undoInfo(closeChunk=True)

    _bf_message(u"先点 %s 上的一个绿色面，再点 %s 上的一个绿色面"
                  % (_bf_short_name(target_transform), _bf_short_name(moving_transform)))


def bf_clear_choices(*_, kill_jobs=True):
    """删除候选面组并停掉 SelectionChanged 监听"""
    global _BF_ATTACH_PENDING

    _BF_ATTACH_PENDING = None
    if kill_jobs:
        _bf_kill_jobs()

    for group in _bf_choice_groups():
        if cmds.objExists(group):
            try:
                cmds.delete(group)
            except RuntimeError:
                pass


def _bf_kill_jobs():
    """杀掉本折叠栏的选择监听，包含上一次运行脚本遗留的"""
    global _BF_SCRIPT_JOB

    if _BF_SCRIPT_JOB is not None:
        try:
            cmds.scriptJob(kill=_BF_SCRIPT_JOB, force=True)
        except (RuntimeError, ValueError):
            pass
        _BF_SCRIPT_JOB = None

    for job in cmds.scriptJob(listJobs=True) or []:
        if BF_JOB_MARK not in job:
            continue
        try:
            cmds.scriptJob(kill=int(job.split(":", 1)[0].strip()), force=True)
        except (RuntimeError, ValueError):
            pass


def _bf_start_job():
    """候选面生成后监听选择：点绿色面片即放置 / 吸附"""
    global _BF_SCRIPT_JOB

    if cmds.about(batch=True):
        return

    _bf_kill_jobs()
    _BF_SCRIPT_JOB = cmds.scriptJob(
        event=["SelectionChanged", _bf_selection_changed],
        killWithScene=True,
    )


def _bf_selection_changed(*_):
    global _BF_APPLYING

    if _BF_APPLYING:
        return

    try:
        candidates = _bf_selected_candidates()
        if not candidates:
            return

        if any(_bf_choice_mode(candidate) == "attach" for candidate in candidates):
            _bf_apply_attach_candidates(candidates)
            return

        _bf_apply_candidate(candidates[0])
    except Exception as error:
        _BF_APPLYING = False
        _bf_traceback.print_exc()
        _bf_warning(u"点选候选面失败：%s" % error)


# =========================================================================
# 边重建：内嵌自 ResampleMesh_Maya.py（原版 AV: ResampleMesh v1.0.10，GPL-3.0-or-later）
# 只保留样条 / 拓扑 / 运算符 / 网格 IO；原 Qt 面板、工具架、快捷键未搬运。
# maya.api.OpenMaya 统一使用本文件已有的 oma 别名。
# =========================================================================

from collections import defaultdict
from contextlib import contextmanager
import traceback as _rm_traceback

SPLINE_TYPES = ("AKIMA", "CATMULL_ROM", "POLYNOMIAL")


def _normalize(vector):
    length = float(np.linalg.norm(vector))
    return vector / length if length > 1e-12 else np.zeros(3)



# ---------------------------------------------------------------------------
# Splines
# ---------------------------------------------------------------------------



def _as_array(points):
    arr = np.asarray(points, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError("points must be an (N, 3) sequence")
    return arr


def _akima_derivatives(coords, h):
    n = len(coords)
    if n <= 1:
        return [0.0] * n

    slopes = [(coords[i + 1] - coords[i]) / h for i in range(n - 1)]

    extended = []
    s0 = slopes[0]
    s1 = slopes[1] if len(slopes) > 1 else slopes[0]
    s_1 = 2 * s0 - s1
    s_2 = 2 * s_1 - s0
    extended.extend([s_2, s_1])
    extended.extend(slopes)
    sn_1 = slopes[-1]
    sn_2 = slopes[-2] if len(slopes) > 1 else slopes[-1]
    sn = 2 * sn_1 - sn_2
    sn1 = 2 * sn - sn_1
    extended.extend([sn, sn1])

    derivatives = []
    for i in range(n):
        im2 = extended[i]
        im1 = extended[i + 1]
        i0 = extended[i + 2]
        ip1 = extended[i + 3]
        w1 = abs(ip1 - i0)
        w2 = abs(im1 - im2)
        if w1 + w2 == 0:
            derivatives.append((im1 + i0) / 2.0)
        else:
            derivatives.append((w1 * im1 + w2 * i0) / (w1 + w2))
    return derivatives


def akima_spline(points, num_points=100):
    """Local Akima cubic Hermite on a uniform parameter, sampled uniformly in t."""
    points = _as_array(points)
    n = len(points)
    if n < 2:
        return points.copy()
    if num_points < 1:
        return np.empty((0, 3))

    h = 1.0 / (n - 1)
    axes = [points[:, k] for k in range(3)]
    derivs = [_akima_derivatives(a, h) for a in axes]

    out = np.empty((num_points, 3))
    for row, t in enumerate(np.linspace(0.0, 1.0, num_points)):
        idx = n - 2 if t >= 1.0 else min(max(int(t * (n - 1)), 0), n - 2)
        dt = t - idx * h
        for k in range(3):
            a = axes[k]
            d = derivs[k]
            s = (a[idx + 1] - a[idx]) / h
            c = (3 * s - 2 * d[idx] - d[idx + 1]) / h
            e = (d[idx] + d[idx + 1] - 2 * s) / (h ** 2)
            out[row, k] = a[idx] + d[idx] * dt + c * dt ** 2 + e * dt ** 3
    return out


def polynomial_interpolation(points, num_points=100, tension=0.125):
    """Natural cubic spline (C2).

    ``tension`` is the parameterization exponent, not spline tension:
    t_i = t_{i-1} + |P_i - P_{i-1}| ** (2 * tension).
    """
    points = _as_array(points)
    n = len(points)
    if n < 2:
        raise ValueError("Need at least two points for interpolation")
    if num_points == 1:
        return points[:1].copy()

    t = np.zeros(n)
    for i in range(1, n):
        delta_sq = float(np.sum((points[i] - points[i - 1]) ** 2))
        t[i] = t[i - 1] + (delta_sq ** tension if delta_sq >= 1e-12 else 0.0)

    # The original tested this inside the loop, which corrupted t part-way through.
    if np.any(np.diff(t) <= 0.0):
        t = np.arange(n, dtype=float)

    h = np.diff(t)
    m = n - 2
    coeffs = []
    for k in range(3):
        y = points[:, k]
        c = np.zeros(n)
        if m > 0:
            A = h[:m]
            B = 2.0 * (h[:m] + h[1:m + 1])
            C = h[1:m + 1].copy()
            R = 3.0 * ((y[2:] - y[1:m + 1]) / h[1:m + 1]
                       - (y[1:m + 1] - y[:m]) / h[:m])
            for i in range(1, m):
                f = A[i] / B[i - 1]
                B[i] -= f * C[i - 1]
                R[i] -= f * R[i - 1]
            c[n - 2] = R[-1] / B[-1]
            for i in range(m - 2, -1, -1):
                c[i + 1] = (R[i] - C[i] * c[i + 2]) / B[i]

        b = (y[1:] - y[:-1]) / h - h * (c[1:] + 2 * c[:-1]) / 3.0
        d = (c[1:] - c[:-1]) / (3.0 * h)
        coeffs.append((y[:-1], b, c[:-1], d))

    out = np.empty((num_points, 3))
    total = t[-1]
    for row, tv in enumerate(np.linspace(0.0, total, num_points)):
        if tv >= t[-1]:
            out[row] = points[-1]
            continue
        if tv <= t[0]:
            out[row] = points[0]
            continue
        idx = int(np.searchsorted(t, tv, side="left")) - 1
        idx = max(0, min(idx, n - 2))
        dt = tv - t[idx]
        for k in range(3):
            a, b, c, d = coeffs[k]
            out[row, k] = a[idx] + b[idx] * dt + c[idx] * dt ** 2 + d[idx] * dt ** 3
    return out


def catmull_rom_spline(points, num_points=100, tension=0.5, mode="uniform", centripetal=True):
    """Cubic Hermite with Catmull-Rom style tangents and an overshoot limiter.

    ``mode='uniform'`` uses chord-derived knots (centripetal when enabled);
    any other value uses integer knots. The names are inherited from the
    original addon and are the reverse of the usual convention.
    """
    points = _as_array(points)
    n = len(points)
    if n < 2:
        return np.empty((0, 3))
    if num_points < 1:
        return np.empty((0, 3))

    extended = np.vstack([points[0], points, points[-1]])

    if mode == "uniform":
        distances = np.linalg.norm(np.diff(points, axis=0), axis=1)
        dist_param = np.sqrt(distances) if centripetal else distances
        knots = np.concatenate([[0.0], np.cumsum(dist_param)])
        span = knots[-1] - knots[0]
        knots = (knots - knots[0]) / span if span > 0 else np.arange(n, dtype=float)
    else:
        knots = np.linspace(0.0, n - 1, n)

    out = np.empty((num_points, 3))
    for row, t in enumerate(np.linspace(knots[0], knots[-1], num_points)):
        i = int(np.searchsorted(knots, t)) - 1
        i = max(0, min(i, n - 2))
        t0, t1 = knots[i], knots[i + 1]
        ts = (t - t0) / (t1 - t0) if t1 != t0 else 0.0

        p0, p1, p2, p3 = extended[i:i + 4]

        dt0 = t0 - knots[i - 1] if i > 0 else 0.0
        dt1 = t1 - t0
        dt2 = knots[i + 2] - t1 if i + 2 < len(knots) else 0.0

        m1 = (p2 - p0) * (tension * dt1 / (dt0 + dt1)) if (dt0 + dt1) != 0 else p2 - p0
        m2 = (p3 - p1) * (tension * dt1 / (dt1 + dt2)) if (dt1 + dt2) != 0 else p3 - p1

        max_tangent = float(np.linalg.norm(p2 - p1))
        for m in (m1, m2):
            length = float(np.linalg.norm(m))
            if length > max_tangent and length > 0:
                m *= max_tangent / length

        t2 = ts ** 2
        t3 = ts ** 3
        out[row] = ((2 * t3 - 3 * t2 + 1) * p1 + (t3 - 2 * t2 + ts) * m1
                    + (-2 * t3 + 3 * t2) * p2 + (t3 - t2) * m2)
    return out


def resample_uniform(points, num_points):
    """Redistribute a polyline to ``num_points`` evenly spaced by arc length."""
    points = _as_array(points)
    if len(points) < 2 or num_points < 1:
        return points.copy()

    dists = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cum = np.zeros(len(points))
    cum[1:] = np.cumsum(dists)
    total = cum[-1]
    if total == 0:
        return points.copy()

    targets = np.linspace(0.0, total, num_points)
    idx = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, len(dists) - 1)
    start, end = cum[idx], cum[idx + 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.nan_to_num((targets - start) / (end - start))
    return points[idx] + t[:, np.newaxis] * (points[idx + 1] - points[idx])


def fit(coords, num_points, spline_type, space=False, p_tension=0.15):
    """Dispatch matching AV_OT_ResampleMesh.get_splines (resample_mesh.py:837)."""
    coords = _as_array(coords)
    if spline_type == "AKIMA":
        highres = akima_spline(coords, num_points)
        return resample_uniform(highres, num_points) if space else highres
    if spline_type == "CATMULL_ROM":
        if space:
            return catmull_rom_spline(coords, num_points, tension=1.1, mode="uniform")
        return catmull_rom_spline(coords, num_points, tension=1.0, mode="non-uniform")
    if spline_type == "POLYNOMIAL":
        return polynomial_interpolation(coords, num_points, tension=p_tension)
    raise ValueError("Unknown spline type: %r" % (spline_type,))


# ---------------------------------------------------------------------------
# Topology and chains
# ---------------------------------------------------------------------------

class Topology(object):
    """Indexed mesh snapshot: points, edges, faces and their adjacency."""

    def __init__(self, points, edges, faces):
        self.points = np.asarray(points, dtype=float).reshape(-1, 3)
        self.edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
        self.faces = [tuple(int(v) for v in f) for f in faces]

        self.vert_edges = defaultdict(list)
        self.edge_faces = defaultdict(list)
        self.vert_faces = defaultdict(list)
        self._edge_lookup = {}

        for eid, (a, b) in enumerate(self.edges):
            a, b = int(a), int(b)
            self.vert_edges[a].append(eid)
            self.vert_edges[b].append(eid)
            self._edge_lookup[(min(a, b), max(a, b))] = eid

        for fid, face in enumerate(self.faces):
            count = len(face)
            for i in range(count):
                a, b = face[i], face[(i + 1) % count]
                self.vert_faces[a].append(fid)
                eid = self.edge_id(a, b)
                if eid is not None:
                    self.edge_faces[eid].append(fid)

    @property
    def num_verts(self):
        return len(self.points)

    @property
    def num_edges(self):
        return len(self.edges)

    def edge_id(self, a, b):
        return self._edge_lookup.get((min(a, b), max(a, b)))

    def edge_verts(self, eid):
        a, b = self.edges[eid]
        return int(a), int(b)

    def other_vert(self, eid, vid):
        a, b = self.edge_verts(eid)
        return b if vid == a else a

    def verts_of_edges(self, edge_ids):
        out = set()
        for eid in edge_ids:
            out.update(self.edge_verts(eid))
        return out

    def face_normals(self):
        """Newell normals, one per face, normalized (zero for degenerate faces)."""
        out = np.zeros((len(self.faces), 3))
        for fid, face in enumerate(self.faces):
            pts = self.points[list(face)]
            nxt = np.roll(pts, -1, axis=0)
            n = np.array([
                np.sum((pts[:, 1] - nxt[:, 1]) * (pts[:, 2] + nxt[:, 2])),
                np.sum((pts[:, 2] - nxt[:, 2]) * (pts[:, 0] + nxt[:, 0])),
                np.sum((pts[:, 0] - nxt[:, 0]) * (pts[:, 1] + nxt[:, 1])),
            ])
            length = float(np.linalg.norm(n))
            if length > 1e-12:
                out[fid] = n / length
        return out


    def selected_incident(self, vert_ids, selected_edges):
        selected = set(int(e) for e in selected_edges)
        return {v: [e for e in self.vert_edges[v] if e in selected] for v in vert_ids}

    def outermost(self, vert_ids, selected_edges):
        """Split into (chain ends, chain interiors) by selected-edge valence."""
        incident = self.selected_incident(vert_ids, selected_edges)
        ends = sorted(v for v, e in incident.items() if len(e) < 2)
        interior = sorted(v for v, e in incident.items() if len(e) >= 2)
        return ends, interior

    def sort_chains(self, vert_ids, selected_edges):
        """Order selected edges into open chains walked from their end vertices.

        Returns a list of dicts with ``verts`` (full ordered chain), ``edges``
        (ordered chain edges) and ``interior`` (``verts`` with the two ends
        dropped when the chain has more than three vertices).
        """
        incident = self.selected_incident(vert_ids, selected_edges)
        ends, _ = self.outermost(vert_ids, selected_edges)

        chains = []
        cache = set()
        for start in ends:
            if start in cache:
                continue
            available = incident.get(start, [])
            verts = [start]
            edges = []
            if not available:
                chains.append({"verts": verts, "edges": edges, "interior": []})
                continue

            target = available[0]
            edges.append(target)
            nxt = self.other_vert(target, start)
            verts.append(nxt)
            cache.add(nxt)

            for _ in range(100000):
                cur = verts[-1]
                others = [e for e in incident.get(cur, []) if e != target]
                step = others[0] if others else target
                nxt = self.other_vert(step, cur)
                if nxt not in cache:
                    verts.append(nxt)
                    cache.add(nxt)
                if step == target:
                    break
                edges.append(step)
                target = step

            interior = verts[1:-1] if len(verts) > 3 else list(verts)
            chains.append({"verts": verts, "edges": edges, "interior": interior})
        return chains

    def face_runs(self, fid, chain_edges, chain_verts=None):
        """Boundary runs of a face adjacent to a resampled chain.

        Mirrors resample_mesh.py:1463-1512. Blender selects the chain edges
        plus every face edge that touches no chain vertex (the opposite row)
        and deselects the end caps; grouping that selection yields one run per
        chain crossing the face. The run has to come from the real chain edge
        set: when a face is the strip between two selected chains, every edge
        of its boundary joins two chain vertices, so vertex membership alone
        would merge both chains into one run and nothing would be reconnected.

        Each run loses its two end vertices when it is longer than three.
        """
        face = self.faces[fid]
        count = len(face)
        if count < 3:
            return []

        chain_edges = set(int(e) for e in chain_edges)
        if chain_verts is None:
            chain_verts = set()
            for eid in chain_edges:
                chain_verts.update(self.edge_verts(eid))
        else:
            chain_verts = set(int(v) for v in chain_verts)

        flags = []
        for i in range(count):
            a, b = face[i], face[(i + 1) % count]
            eid = self.edge_id(a, b)
            staged = eid is not None and eid in chain_edges
            flags.append(staged or (a not in chain_verts and b not in chain_verts))

        runs = []
        current = []
        for i in range(count):
            if flags[i]:
                current.append(i)
            elif current:
                runs.append(current)
                current = []
        if current:
            if runs and flags[0] and flags[-1]:
                runs[0] = current + runs[0]
            else:
                runs.append(current)

        trimmed = []
        covered = set()
        for run in runs:
            verts = [face[i] for i in run] + [face[(run[-1] + 1) % count]]
            covered.update(verts)
            trimmed.append(verts[1:-1] if len(verts) > 3 else verts)

        if len(trimmed) < 2:
            caps = []
            for i in range(count):
                if flags[i]:
                    continue
                for v in (face[i], face[(i + 1) % count]):
                    if v not in covered and v not in caps:
                        caps.append(v)
            if caps:
                trimmed.append(caps)
        return trimmed


    def connected_loops(self, selected_edges):
        """Ordered vertex loops with a circularity flag, for Bridge.

        Equivalent to bridge.get_connected_selections (modules/bridge.py:23).
        """
        adjacency = defaultdict(set)
        for eid in selected_edges:
            a, b = self.edge_verts(eid)
            adjacency[a].add(b)
            adjacency[b].add(a)

        loops = []
        unvisited = set(adjacency)
        while unvisited:
            start = min(unvisited)
            walk = [start]
            unvisited.discard(start)
            while True:
                nxts = sorted(n for n in adjacency[walk[-1]] if n in unvisited)
                if not nxts:
                    break
                walk.append(nxts[0])
                unvisited.discard(nxts[0])
            while True:
                prevs = sorted(n for n in adjacency[walk[0]] if n in unvisited)
                if not prevs:
                    break
                walk.insert(0, prevs[0])
                unvisited.discard(prevs[0])

            circular = len(walk) > 2 and walk[0] in adjacency[walk[-1]]
            loops.append((walk, circular))
        return loops


class PositionIndex(object):
    """Spatial hash used to re-find vertices after Maya renumbers components."""

    def __init__(self, points, tolerance=1e-6):
        self.points = np.asarray(points, dtype=float)
        self.tol = max(float(tolerance), 1e-12)
        self.cells = defaultdict(list)
        for vid, p in enumerate(self.points):
            self.cells[self._cell(p)].append(vid)

    def _cell(self, p):
        return tuple(int(np.floor(c / self.tol)) for c in p)

    def find(self, pos):
        cx, cy, cz = self._cell(pos)
        best, best_dist = None, self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for vid in self.cells.get((cx + dx, cy + dy, cz + dz), ()):
                        dist = float(np.linalg.norm(self.points[vid] - pos))
                        if dist <= best_dist:
                            best, best_dist = vid, dist
        return best

    def map_all(self, positions):
        return [self.find(p) for p in positions]


def chain_steps(chains, step=0, counts=None):
    """Resolve one vertex delta per chain.

    ``counts`` holds absolute target vertex counts, one per chain, and
    overrides ``step``. It lets two selected chains grow and shrink by
    different amounts in a single operation.
    """
    if counts is not None:
        try:
            targets = [int(c) for c in counts]
        except (TypeError, ValueError):
            raise ValueError("counts 必须是每条链一个整数点数。")
        if len(targets) != len(chains):
            raise ValueError(
                "分别设点数需要每条选中链对应一个值：当前 %d 条链，收到 %d 个。"
                % (len(chains), len(targets)))
        deltas = []
        for index, (target, chain) in enumerate(zip(targets, chains)):
            current = len(chain["verts"])
            if target < 3:
                raise ValueError("链 %d 的目标点数必须 >= 3（当前 %d 点）。"
                                 % (index + 1, current))
            if target > 1000:
                raise ValueError("链 %d 的目标点数不能超过 1000。" % (index + 1))
            deltas.append(target - current)
        return deltas
    return [int(step)] * len(chains)


# ---------------------------------------------------------------------------
# Loop pairing
# ---------------------------------------------------------------------------

LIMIT = 1.5707964  # 0.5 * pi, as written in the original


def vec_angle(a, b, fallback=0.0):
    """mathutils.Vector.angle(other, fallback): fallback when either is zero."""
    la = float(np.linalg.norm(a))
    lb = float(np.linalg.norm(b))
    if la == 0.0 or lb == 0.0:
        return fallback
    return float(np.arccos(np.clip(np.dot(a, b) / (la * lb), -1.0, 1.0)))


def rotation_matrix(angle, axis):
    axis = np.asarray(axis, dtype=float)
    length = float(np.linalg.norm(axis))
    if length == 0.0:
        return np.eye(3)
    x, y, z = axis / length
    c, s = np.cos(angle), np.sin(angle)
    t = 1.0 - c
    return np.array([
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ])


def _plane_normal(coords, center):
    d = coords - center
    mat = d.T @ d
    plurity = any(float(mat[i].sum()) < 1e-6 for i in range(3))

    normal = None
    if abs(float(np.linalg.det(mat))) < 1e-12:
        for i in range(3):
            if float(mat[i].sum()) == 0.0:
                normal = np.zeros(3)
                normal[i] = 1.0
                break

    if normal is None:
        vec = np.ones(3)
        mv = mat @ vec
        norm = float(np.linalg.norm(mv))
        vec2 = mv / norm if norm else vec
        for _ in range(500):
            if np.array_equal(vec, vec2):
                break
            vec = vec2
            mv = mat @ vec
            norm = float(np.linalg.norm(mv))
            vec2 = mv / norm if norm else vec
        if float(np.linalg.norm(vec2)) == 0.0:
            vec2 = np.ones(3)
        normal = vec2
    return normal, plurity


def calculate_lines(points, loops, mode="shortest", twist=0, reverse=False):
    """Return the vertex-index pairs connecting two loops.

    ``points`` is the (N, 3) position array; ``loops`` is a pair of
    ``(index_list, is_circular)`` tuples.
    """
    points = np.asarray(points, dtype=float)
    (loop1, loop1_circular), (loop2, loop2_circular) = loops
    loop1, loop2 = list(loop1), list(loop2)
    if len(loop1) < 2 or len(loop2) < 2:
        return []

    circular = loop1_circular or loop2_circular
    circle_full = False
    lines = []

    centers = []
    for loop in (loop1, loop2):
        center = points[loop].mean(axis=0)
        if np.any(np.all(points[loop] == center, axis=1)):
            center = center + np.array([0.01, 0.0, 0.0])
        centers.append(center)
    center1, center2 = centers

    normals = []
    normal_plurity = False
    for i, loop in enumerate((loop1, loop2)):
        normal, plurity = _plane_normal(points[loop], centers[i])
        normal_plurity = normal_plurity or plurity
        normals.append(normal)

    if np.linalg.norm((center1 + normals[0]) - center2) < np.linalg.norm((center1 - normals[0]) - center2):
        normals[0] = -normals[0]
    if np.linalg.norm((center2 + normals[1]) - center1) > np.linalg.norm((center2 - normals[1]) - center1):
        normals[1] = -normals[1]

    axis = np.cross(normals[0], normals[1])
    axis = np.array([v if abs(v) > 1e-8 else 0.0 for v in axis])
    if vec_angle(axis, np.array([0.0, 0.0, 1.0])) > LIMIT:
        axis = -axis
    # The original passes the dot product (a cosine) where an angle is expected.
    rot = rotation_matrix(float(np.dot(normals[0], normals[1])), axis)

    if circular:
        if loop2_circular and not loop1_circular:
            loop1_circular, loop2_circular = True, False
            loop1, loop2 = loop2, loop1
            center1, center2 = center2, center1
            normals[0], normals[1] = normals[1], normals[0]

        target_vector = points[loop2[0]] - center2
        dif_angles = sorted(
            (vec_angle(rot @ (points[v] - center1), target_vector), i)
            for i, v in enumerate(loop1)
        )
        best = dif_angles[0][1]
        if len(loop1) != len(loop2):
            angle_limit = dif_angles[0][0] * 1.2
            candidates = sorted(
                (float(np.linalg.norm(points[loop2[0]] - points[loop1[i]])), a, i)
                for a, i in dif_angles if a <= angle_limit
            )
            if candidates:
                best = candidates[0][2]
        loop1 = loop1[best:] + loop1[:best]

    if normal_plurity and not circular:
        second = [vec_angle(points[loop1[1]] - center1, points[loop2[i]] - center2)
                  for i in (0, 1, -1)]
        last = [vec_angle(points[loop1[-1]] - center1, points[loop2[i]] - center2)
                for i in (0, 1)]
        if (min(last) * 1.1 < min(second[0], second[1])) or (
                loop2_circular and second[2] * 1.1 < min(second[0], second[1])):
            loop1.reverse()
            if circular:
                loop1 = [loop1[-1]] + loop1[:-1]
    else:
        angle = vec_angle(np.cross(points[loop1[0]] - center1, points[loop1[1]] - center1),
                          normals[0])
        target_angle = vec_angle(np.cross(points[loop2[0]] - center2, points[loop2[1]] - center2),
                                 normals[1])
        if not ((angle > LIMIT and target_angle > LIMIT) or (angle < LIMIT and target_angle < LIMIT)):
            loop1.reverse()
            if circular:
                loop1 = [loop1[-1]] + loop1[:-1]
        elif vec_angle(normals[0], normals[1]) > LIMIT:
            loop1.reverse()
            if circular:
                loop1 = [loop1[-1]] + loop1[:-1]

    first_to_first = vec_angle(points[loop1[0]] - center1, points[loop2[0]] - center2)
    first_to_last = vec_angle(points[loop1[0]] - center1, points[loop2[-1]] - center2)
    if first_to_first > first_to_last:
        loop1.reverse()

    if len(loop1) == len(loop2):
        if twist and abs(twist) < len(loop1):
            loop1 = loop1[twist:] + loop1[:twist]
        if reverse:
            loop1.reverse()
        return [[loop1[i], loop2[i]] for i in range(len(loop1))]

    if len(loop2) > len(loop1):
        loop1, loop2 = loop2, loop1
        loop1_circular, loop2_circular = loop2_circular, loop1_circular

    if twist and abs(twist) < len(loop1):
        loop1 = loop1[twist:] + loop1[:twist]
    if reverse:
        loop1.reverse()

    if loop1_circular and not loop2_circular:
        shifting = 1
        while shifting:
            if len(loop1) - shifting < len(loop2):
                break
            to_last = vec_angle(rot @ (points[loop1[-1]] - center1), points[loop2[-1]] - center2)
            to_first = vec_angle(rot @ (points[loop1[-1]] - center1), points[loop2[0]] - center2)
            if to_first < to_last:
                loop1 = [loop1[-1]] + loop1[:-1]
                shifting += 1
            else:
                break

    if mode == "basic":
        lines.append([loop1[0], loop2[0]])
        for i in range(1, len(loop1)):
            lines.append([loop1[i], loop2[-1] if i >= len(loop2) - 1 else loop2[i]])
        return lines

    lines.append([loop1[0], loop2[0]])
    prev_vert2 = 0
    for i in range(len(loop1) - 1):
        nxt = points[loop1[i + 1]]
        if prev_vert2 == len(loop2) - 1 and not loop2_circular:
            tri, quad = 0.0, 1.0
        elif prev_vert2 == len(loop2) - 1 and loop2_circular:
            tri = float(np.linalg.norm(nxt - points[loop2[prev_vert2]]))
            quad = float(np.linalg.norm(nxt - points[loop2[0]]))
            circle_full = 2
        elif len(loop1) - 1 - i == len(loop2) - 1 - prev_vert2 and not circle_full:
            tri, quad = 1.0, 0.0
        else:
            tri = float(np.linalg.norm(nxt - points[loop2[prev_vert2]]))
            quad = float(np.linalg.norm(nxt - points[loop2[prev_vert2 + 1]]))

        if tri < quad:
            lines.append([loop1[i + 1], loop2[prev_vert2]])
            if circle_full == 2:
                circle_full = False
        elif not circle_full:
            lines.append([loop1[i + 1], loop2[prev_vert2 + 1]])
            prev_vert2 += 1
        else:
            lines.append([loop1[i + 1], loop2[0]])
            prev_vert2 = 0
            circle_full = True

    if loop1_circular and loop2_circular:
        lines.append([loop1[0], loop2[0]])
    return lines


def sort_loops(points, loops):
    """Nearest-neighbour ordering of three or more loops (bridge.py:503)."""
    if len(loops) < 3:
        return list(loops)
    nodes = [np.asarray(points[loop[0]], dtype=float).mean(axis=0) for loop in loops]
    active = 0
    open_nodes = list(range(1, len(loops)))
    path = [0]
    while open_nodes:
        distances = [float(np.linalg.norm(nodes[active] - nodes[i])) for i in open_nodes]
        best = int(np.argmin(distances))
        active = open_nodes[best]
        open_nodes.pop(best)
        path.append(active)
    for i in range(2, len(path)):
        if float(np.linalg.norm(nodes[path[i]] - nodes[0])) < float(
                np.linalg.norm(nodes[path[i]] - nodes[path[i - 1]])):
            path = path[:i][::-1] + path[i:]
            break
    return [loops[i] for i in path]


# ---------------------------------------------------------------------------
# Smoothing
# ---------------------------------------------------------------------------

def _weights(neighbor_coords, current, weight_type):
    count = len(neighbor_coords)
    if weight_type == "uniform":
        return np.full(count, 1.0 / count)
    d = np.linalg.norm(neighbor_coords - current, axis=1)
    inv = 1.0 / np.maximum(d, 1e-8)
    return inv / inv.sum()


def laplacian_smooth(positions, neighbors, valence, iterations=1, lambda_factor=0.5,
                     weight_type="distance", max_displacement=0.1,
                     lambda_decay=1.0, boundary_lambda_factor=0.2):
    """Distance-weighted Laplacian relax toward frozen neighbour positions.

    Neighbour coordinates are captured once before the iteration loop, exactly
    as in smooth.py:47-56, so repeated iterations relax toward a fixed target.
    """
    frozen = {}
    for v in positions:
        nbrs = neighbors.get(v) or []
        if nbrs:
            frozen[v] = np.array([positions[n] for n in nbrs], dtype=float)

    for _ in range(int(iterations)):
        updates = {}
        for v, nbrs in frozen.items():
            current = positions[v]
            weights = _weights(nbrs, current, weight_type)
            average = weights @ nbrs

            if boundary_lambda_factor is not None and valence.get(v, 0) < 3:
                current_lambda = boundary_lambda_factor
            else:
                current_lambda = lambda_factor

            delta = (average - current) * current_lambda
            if max_displacement is not None:
                length = float(np.linalg.norm(delta))
                if length > max_displacement:
                    delta = delta * (max_displacement / length)
            updates[v] = current + delta

        for v, pos in updates.items():
            positions[v] = pos
        lambda_factor *= lambda_decay


def taubin_sharp(positions, neighbors, valence, iterations=1, lambda_factor=0.5,
                 mu_factor=-0.6, weight_type="distance", max_displacement=0.1,
                 lambda_decay=1.0, boundary_lambda_factor=0.2):
    """Smooth/sharpen pair (smooth.py:170). Each pass re-freezes neighbours."""
    for _ in range(int(iterations)):
        laplacian_smooth(positions, neighbors, valence, iterations=1,
                         lambda_factor=lambda_factor, weight_type=weight_type,
                         max_displacement=max_displacement, lambda_decay=lambda_decay,
                         boundary_lambda_factor=boundary_lambda_factor)
        laplacian_smooth(positions, neighbors, valence, iterations=1,
                         lambda_factor=mu_factor, weight_type=weight_type,
                         max_displacement=max_displacement, lambda_decay=lambda_decay,
                         boundary_lambda_factor=boundary_lambda_factor)


def apply_smooth(positions, neighbors, valence, smooth_type="SMOOTH", iterations=1,
           factor=0.5, weight_type="distance"):
    """Dispatch matching AV_OT_SmoothMesh.main (resample_mesh.py:1746)."""
    if smooth_type == "SHARP":
        taubin_sharp(positions, neighbors, valence, iterations=iterations,
                     lambda_factor=0.5, mu_factor=-0.6, weight_type=weight_type,
                     max_displacement=1.0, lambda_decay=1.0, boundary_lambda_factor=0.2)
    else:
        laplacian_smooth(positions, neighbors, valence, iterations=iterations,
                         lambda_factor=factor, weight_type=weight_type,
                         max_displacement=0.1, lambda_decay=1.0, boundary_lambda_factor=0.2)


# ---------------------------------------------------------------------------
# Circularize
# ---------------------------------------------------------------------------

def _rotate(v, axis, angle):
    """Rodrigues rotation, equivalent to Quaternion(axis, angle) @ v."""
    axis = _normalize(axis)
    if not np.any(axis):
        return v.copy()
    return (v * math.cos(angle)
            + np.cross(axis, v) * math.sin(angle)
            + axis * float(np.dot(axis, v)) * (1.0 - math.cos(angle)))


def _fallback_axis(direction):
    """A straight chain has no defined arc plane; pick a stable perpendicular."""
    direction = _normalize(direction)
    candidate = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(direction, candidate))) > 0.9:
        candidate = np.array([0.0, 1.0, 0.0])
    return _normalize(np.cross(direction, candidate))


def arc_points(center, start, num_vertices, axis, angle):
    radius = float(np.linalg.norm(start - center))
    base = _normalize(start - center)
    points = []
    for i in range(num_vertices):
        t = i / (num_vertices - 1) if num_vertices > 1 else 0.0
        points.append(center + radius * _rotate(base, axis, t * angle))
    return np.array(points[1:-1], dtype=float).reshape(-1, 3)


def circularize_chain(coords, angle=90.0, flip=False):
    """Return the new positions for the chain's interior vertices.

    The two endpoints are never moved. The returned array has
    ``len(coords) - 2`` rows, matching ``chain[1:-1]``.
    """
    coords = np.asarray(coords, dtype=float)
    if len(coords) < 3:
        return np.empty((0, 3))

    v1, v2 = coords[0], coords[1]
    v3, v4 = coords[-1], coords[-2]

    chord_center = (v1 + v3) * 0.5
    radians = math.radians(-angle if flip else angle)

    y = chord_center - v1
    a = float(np.linalg.norm(y))
    z = np.cross(y, v1 - v2)
    if float(np.linalg.norm(z)) < 1e-12:
        z = _fallback_axis(v3 - v1) * max(a, 1e-6)

    x = _normalize(np.cross(z, y)) * (a / (math.tan(radians * 0.5) + 2e-6))
    center = chord_center + x

    return arc_points(center, v1, len(coords), _normalize(z), radians)


# ---------------------------------------------------------------------------
# Maya mesh IO
# ---------------------------------------------------------------------------

KIND_FILTER = {
    "vtx": oma.MFn.kMeshVertComponent,
    "e": oma.MFn.kMeshEdgeComponent,
    "f": oma.MFn.kMeshPolygonComponent,
}


class MeshError(Exception):
    pass


_UNDO_DEPTH = 0


@contextmanager
def undo_chunk(name):
    """Nested calls collapse into the outermost chunk (used by live preview)."""
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
    sel = oma.MSelectionList()
    sel.add(node)
    return sel.getDagPath(0)


def active_mesh():
    """Return (transform_full_path, shape_full_path) for the selected mesh."""
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
    return "%s.%s[%d]" % (transform, kind, index)


def component_names(transform, kind, indices):
    return ["%s.%s[%d]" % (transform, kind, i) for i in sorted(set(int(v) for v in indices))]


def read_topology(shape):
    """Snapshot the mesh into a Topology using Maya's own component indices."""
    dag = dag_path(shape)
    fn = oma.MFnMesh(dag)

    points = [(p.x, p.y, p.z) for p in fn.getPoints(oma.MSpace.kObject)]

    counts, connects = fn.getVertices()
    faces = []
    offset = 0
    for count in counts:
        faces.append(tuple(int(connects[offset + i]) for i in range(count)))
        offset += count

    edge_count = int(cmds.polyEvaluate(shape, edge=True))
    edges = [(0, 0)] * edge_count
    it = oma.MItMeshEdge(dag)
    while not it.isDone():
        edges[it.index()] = (it.vertexId(0), it.vertexId(1))
        it.next()

    return Topology(points, edges, faces)


def write_points(shape, updates):
    """Write {vertex_id: (x, y, z)} in object space."""
    if not updates:
        return
    dag = dag_path(shape)
    fn = oma.MFnMesh(dag)
    points = fn.getPoints(oma.MSpace.kObject)
    for vid, pos in updates.items():
        points[int(vid)] = oma.MPoint(float(pos[0]), float(pos[1]), float(pos[2]))
    fn.setPoints(points, oma.MSpace.kObject)


def read_points(shape):
    fn = oma.MFnMesh(dag_path(shape))
    return {i: (p.x, p.y, p.z) for i, p in enumerate(fn.getPoints(oma.MSpace.kObject))}


def selected_ids(shape, kind):
    """Component indices of ``kind`` selected on ``shape``."""
    wanted = oma.MFnMesh(dag_path(shape)).object()
    selection = oma.MGlobal.getActiveSelectionList()
    it = oma.MItSelectionList(selection, KIND_FILTER[kind])
    found = set()
    while not it.isDone():
        try:
            dag, component = it.getComponent()
        except TypeError:
            it.next()
            continue
        if dag.hasFn(oma.MFn.kTransform):
            try:
                dag.extendToShape()
            except RuntimeError:
                it.next()
                continue
        if component.isNull() or oma.MFnMesh(dag).object() != wanted:
            it.next()
            continue
        found.update(int(i) for i in oma.MFnSingleIndexedComponent(component).getElements())
        it.next()
    return sorted(found)


def select_components(transform, kind, indices, replace=True):
    names = component_names(transform, kind, indices)
    if replace:
        cmds.select(clear=True)
    if names:
        cmds.select(names, add=bool(not replace))


def delete_edges(transform, edge_ids, clean=True):
    """Dissolve edges, optionally removing vertices left with no edges.

    ``clean=False`` keeps every vertex index valid, which matters when the
    caller still holds vertex ids. A poly command that deletes vertices
    renumbers the rest, and Maya can drop vertices the caller still needs.
    """
    names = component_names(transform, "e", edge_ids)
    if names:
        cmds.polyDelEdge(names, cleanVertices=bool(clean),
                         constructionHistory=False)


def delete_vertices(transform, vert_ids):
    names = component_names(transform, "vtx", vert_ids)
    if names:
        cmds.polyDelVertex(names, constructionHistory=False)


def subdivide_edge(transform, edge_id, divisions):
    name = component_name(transform, "e", edge_id)
    cmds.polySubdivideEdge(name, divisions=int(divisions), constructionHistory=False)


def connect_vertices(transform, shape, pairs):
    """Split the face shared by each vertex pair, one call per pair.

    polyConnectComponents gathers its arguments into ONE continuous path, so
    batching disjoint pairs would create a zigzag chain instead of separate
    edges. Blender connects them one at a time for the same reason
    (resample_mesh.py:1341, bmesh.ops.connect_verts). A pair only yields an edge
    when both vertices already share a face; splitting adds an edge and a face
    but no vertices, so the ids stay valid for the whole loop.

    Returns ``(created, failed)``. A pair is counted as failed when the edge
    count did not grow, so a silent no-op in Maya cannot leave a dissolved
    region behind without the caller noticing.
    """
    created = 0
    failed = []
    for a, b in pairs:
        if a == b:
            continue
        before = int(cmds.polyEvaluate(shape, edge=True))
        try:
            cmds.polyConnectComponents(component_name(transform, "vtx", a),
                                       component_name(transform, "vtx", b),
                                       constructionHistory=False)
        except RuntimeError as exc:
            failed.append((int(a), int(b), str(exc)))
            continue
        if int(cmds.polyEvaluate(shape, edge=True)) > before:
            created += 1
        else:
            failed.append((int(a), int(b), "no edge created"))
    return created, failed


def refresh():
    cmds.refresh(currentView=True)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

TOLERANCE = 1e-6


# --------------------------------------------------------------------------
# selection helpers
# --------------------------------------------------------------------------

def _point_tolerance(points):
    """Lookup slack that follows float32 precision at the mesh's scale.

    Maya stores points as float32, so a poly operation that re-evaluates the
    DG (construction history, deformers) can nudge a coordinate by a few
    ulps. At coordinate 1000 that is already about 1e-4, far above the 1e-6
    used for an untouched mesh.
    """
    if len(points) == 0:
        return TOLERANCE
    scale = float(np.max(np.abs(points)))
    return max(TOLERANCE, scale * 1.2e-7 * 16.0)


def _map_positions(topo, positions):
    """Map positions to vertex ids, retrying with a scale-aware tolerance."""
    index = PositionIndex(topo.points, TOLERANCE)
    vids = index.map_all(positions)
    if any(v is None for v in vids):
        loose = PositionIndex(topo.points, _point_tolerance(topo.points))
        vids = [v if v is not None else loose.find(p)
                for v, p in zip(vids, positions)]
    return vids


def _ids_still_valid(topo, chains):
    """True when a native command renumbered edges but kept the vertices."""
    if any(v >= topo.num_verts for c in chains for v in c["verts"]):
        return False
    tol = _point_tolerance(topo.points)
    for chain in chains:
        actual = topo.points[chain["verts"]]
        if float(np.max(np.linalg.norm(actual - chain["positions"], axis=1))) > tol:
            return False
    return True


def _chain_selection(shape, allow_faces=False):
    topo = read_topology(shape)
    faces = selected_ids(shape, "f")
    edges = selected_ids(shape, "e")
    if faces and not allow_faces:
        raise MeshError("检测到面选择。请只选择边链，Blender 原版同样会拒绝面选择。")
    if not edges:
        raise MeshError("请先选择一条或多条开放边链（Edge 模式）。")

    verts = topo.verts_of_edges(edges)
    chains = [c for c in topo.sort_chains(verts, edges) if len(c["verts"]) >= 2]
    if not chains:
        ends, _ = topo.outermost(verts, edges)
        if not ends:
            raise MeshError(
                "未找到开放边链：所选 %d 条边连成闭合循环（%d 个顶点，每个顶点都连着 2 条以上"
                "选中边），没有端点。Resample / Smooth / Circularize / Unknot 与 Blender "
                "原版一致，只支持开放边链；闭合循环请用 Bridge，或先断开一处、只选其中一段。"
                % (len(edges), len(verts)))
        raise MeshError("未找到开放边链。请只选择一条连续边链后再试。")
    for chain in chains:
        chain["positions"] = np.array([topo.points[v] for v in chain["verts"]])
    return topo, edges, verts, chains


def chain_info():
    """Describe the selected open chains without modifying the mesh."""
    _, shape = active_mesh()
    _, _, _, chains = _chain_selection(shape)
    info = []
    for chain in chains:
        positions = chain["positions"]
        if len(positions) > 1:
            length = float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1)))
        else:
            length = 0.0
        info.append({
            "count": len(chain["verts"]),
            "segments": max(0, len(chain["verts"]) - 1),
            "length": length,
            "start_vertex": int(chain["verts"][0]),
            "end_vertex": int(chain["verts"][-1]),
            "start": tuple(float(v) for v in positions[0]),
            "end": tuple(float(v) for v in positions[-1]),
        })
    return info


def _relocate(topo, position_arrays, drop_missing=False):
    """Re-find chain vertices after a native command renumbered components."""
    chains = []
    for positions in position_arrays:
        vids = _map_positions(topo, positions)
        if drop_missing:
            vids = [v for v in vids if v is not None]
            if len(vids) < 2:
                continue
        elif any(v is None for v in vids):
            missing = [p for p, v in zip(positions, vids) if v is None]
            if topo.num_verts:
                nearest = min(float(np.min(np.linalg.norm(topo.points - p, axis=1)))
                              for p in missing)
            else:
                nearest = float("inf")
            raise MeshError(
                "拓扑修改后无法定位原始链顶点（%d/%d 个，最近距离 %.3g）。"
                "请 Ctrl+Z 撤销；若模型带有构造历史或变形器，"
                "请先删除历史再重试。" % (len(missing), len(vids), nearest))
        edges = []
        for a, b in zip(vids, vids[1:]):
            eid = topo.edge_id(a, b)
            if eid is None:
                raise MeshError("拓扑修改后边链断开，操作中止。请 Ctrl+Z 撤销。")
            edges.append(eid)
        chains.append({
            "verts": vids,
            "edges": edges,
            "interior": vids[1:-1] if len(vids) > 3 else list(vids),
            "positions": np.array([topo.points[v] for v in vids]),
        })
    if not chains:
        raise MeshError("剩余的链顶点不足，操作中止。请 Ctrl+Z 撤销。")
    return chains


def _point_on_segment(point, a, b, tol):
    ab = b - a
    length_sq = float(np.dot(ab, ab))
    if length_sq < 1e-24:
        return float(np.linalg.norm(point - a)) <= tol
    t = float(np.dot(point - a, ab)) / length_sq
    if t < -tol or t > 1.0 + tol:
        return False
    return float(np.linalg.norm(point - (a + ab * min(max(t, 0.0), 1.0)))) <= tol


def _walk_chain(topo, original_vids, tol=None):
    """Walk a chain that may have gained vertices on its first edge."""
    if tol is None:
        tol = max(1e-5, _point_tolerance(topo.points) * 2.0)
    start, end = original_vids[0], original_vids[-1]
    path = [start]
    current = start
    prev_edge = None
    next_idx = 1

    for _ in range(100000):
        if current == end or next_idx >= len(original_vids):
            break
        target = original_vids[next_idx]
        a, b = topo.points[current], topo.points[target]
        best = None
        for eid in topo.vert_edges[current]:
            if eid == prev_edge:
                continue
            other = topo.other_vert(eid, current)
            if not _point_on_segment(topo.points[other], a, b, tol):
                continue
            score = 0 if other == target else 1
            if best is None or score < best[0]:
                best = (score, eid, other)
        if best is None:
            raise MeshError("细分后无法追踪边链，操作中止。请 Ctrl+Z 撤销。")
        _, prev_edge, other = best
        path.append(other)
        current = other
        if other == target:
            next_idx += 1

    if path[-1] != end:
        raise MeshError("细分后边链未连接到终点，操作中止。请 Ctrl+Z 撤销。")
    edges = [topo.edge_id(a, b) for a, b in zip(path, path[1:])]
    return {"verts": path, "edges": edges,
            "interior": path[1:-1] if len(path) > 3 else list(path),
            "positions": np.array([topo.points[v] for v in path])}


def _closest_points_on_lines(p1, p2, p3, p4):
    """mathutils.geometry.intersect_line_line: closest points, None if parallel."""
    d1, d2 = p2 - p1, p4 - p3
    r = p1 - p3
    a, b, c = float(np.dot(d1, d1)), float(np.dot(d1, d2)), float(np.dot(d2, d2))
    d, e = float(np.dot(d1, r)), float(np.dot(d2, r))
    denom = a * c - b * b
    if abs(denom) < 1e-12:
        return None
    s = (b * e - c * d) / denom
    t = (a * e - b * d) / denom
    return p1 + d1 * s, p3 + d2 * t


# --------------------------------------------------------------------------
# Resample
# --------------------------------------------------------------------------

def resample(step=0, spline_type="AKIMA", space=False, p_tension=0.15,
             auto_clean=False, distribute_only=False, positions_only=False,
             counts=None):
    """Resample selected open edge chains with shape preservation.

    step > 0 adds that many vertices to each chain, step < 0 removes them,
    step == 0 only repositions. ``space`` redistributes by arc length.
    ``distribute_only`` is the Blender ``D`` key: Akima + arc length, no
    topology change at all. ``positions_only`` keeps the chosen spline but
    still skips the topology rebuild, which is what live preview uses.

    ``counts`` gives an absolute target vertex count per chain, in chain order.
    It overrides ``step`` and lets two chains grow/shrink independently, so
    asymmetric pairs can be matched (or symmetric pairs broken) in one pass.
    """
    if spline_type not in SPLINE_TYPES:
        raise MeshError("未知样条类型：%s" % spline_type)
    step = int(step)
    if not -1000 <= step <= 1000:
        raise MeshError("Accuracy(step) 必须在 -1000 到 1000 之间。")
    if positions_only:
        step = 0
        counts = None

    transform, shape = active_mesh()
    with undo_chunk("边重建 重采样"):
        topo, sel_edges, sel_verts, chains = _chain_selection(shape)
        guides = [c["positions"] for c in chains]
        edit_topology = not (distribute_only or positions_only)

        if distribute_only:
            spline_type, space = "AKIMA", True
            steps = [0] * len(chains)
        elif not edit_topology:
            steps = [0] * len(chains)
        else:
            try:
                steps = chain_steps(chains, step, counts)
            except ValueError as exc:
                raise MeshError(str(exc))
            _, interior = topo.outermost(sel_verts, sel_edges)
            selected = set(sel_edges)
            cross = {e for v in interior for e in topo.vert_edges[v] if e not in selected}
            if cross:
                # cleanVertices=False keeps every vertex index valid, as in
                # Blender's dissolve_edges. Removing vertices here would
                # renumber the chain, and Maya may also drop chain vertices
                # when the selection touches a boundary.
                delete_edges(transform, cross, clean=False)
                topo = read_topology(shape)
                if not _ids_still_valid(topo, chains):
                    chains = _relocate(topo, guides)
                else:
                    broken = [(int(a), int(b)) for c in chains
                              for a, b in zip(c["verts"], c["verts"][1:])
                              if topo.edge_id(a, b) is None]
                    if broken:
                        raise MeshError(
                            "溶解横边时链本身也被删掉了（%d 条边，例如 vtx[%d]-vtx[%d]）。"
                            "请 Ctrl+Z 撤销。" % (len(broken), broken[0][0], broken[0][1]))

        if edit_topology:
            grow = [i for i, delta in enumerate(steps) if delta > 0]
            shrink = [i for i, delta in enumerate(steps) if delta < 0]

            if shrink:
                targets = []
                for i in shrink:
                    positions = guides[i]
                    if len(positions) > 3:
                        count = min(-steps[i], len(positions) - 3)
                        targets.extend(positions[1:1 + count])
                if targets:
                    vids = _map_positions(topo, targets)
                    delete_vertices(transform, [v for v in vids if v is not None])
                    topo = read_topology(shape)

            # Each subdivide adds vertices and renumbers components, so re-find
            # the next chain's first edge from the untouched guide positions
            # instead of reusing an id captured before the previous subdivide.
            for i in grow:
                topo = read_topology(shape)
                originals = _map_positions(topo, guides[i])
                if any(v is None for v in originals):
                    raise MeshError("细分前无法定位原始链顶点，请 Ctrl+Z 撤销。")
                eid = topo.edge_id(originals[0], originals[1])
                if eid is None:
                    raise MeshError("链的第一条边已不存在，请 Ctrl+Z 撤销。")
                subdivide_edge(transform, eid, steps[i])

            if grow:
                topo = read_topology(shape)
                for i in grow:
                    originals = _map_positions(topo, guides[i])
                    if any(v is None for v in originals):
                        raise MeshError("细分后无法定位原始链顶点，请 Ctrl+Z 撤销。")
                    chain = _walk_chain(topo, originals)
                    # polySubdivideEdge treats divisions as a maximum and inserts
                    # fewer when the resulting edges would be shorter than
                    # Maya's tolerance.
                    added = len(chain["verts"]) - len(guides[i])
                    if added != steps[i]:
                        raise MeshError(
                            "链 %d 只插入了 %d 个顶点（请求 %d 个），通常是因为边太短。"
                            "请 Ctrl+Z 撤销，然后减小点数或放大模型。"
                            % (i + 1, added, steps[i]))
                    chains[i] = chain

            for i in range(len(chains)):
                if i in grow:
                    continue
                relocated = _relocate(topo, [guides[i]], drop_missing=True)
                if not relocated:
                    raise MeshError("链 %d 的顶点不足，操作中止。请 Ctrl+Z 撤销。"
                                    % (i + 1))
                chains[i] = relocated[0]

            controls = [guides[i] if steps[i] >= 0 else chains[i]["positions"]
                        for i in range(len(chains))]
        else:
            controls = guides

        updates = {}
        for chain, control in zip(chains, controls):
            verts = chain["verts"]
            if len(verts) < 3 or len(control) < 2:
                continue
            samples = fit(control, len(verts), spline_type,
                                     space=space, p_tension=p_tension)
            for j in range(1, min(len(verts) - 1, len(samples) - 1)):
                updates[verts[j]] = samples[j]
        write_points(shape, updates)
        topo = read_topology(shape)
        final_guides = [np.array([topo.points[v] for v in c["verts"]]) for c in chains]

        if edit_topology:
            _reconnect(transform, shape, chains, auto_clean)

        try:
            topo = read_topology(shape)
            chains = _relocate(topo, final_guides)
            select_components(transform, "e", [e for c in chains for e in c["edges"]])
        except MeshError:
            cmds.select(clear=True)
        refresh()
    return len(chains)


def _reconnect(transform, shape, chains, auto_clean):
    """Re-bridge the faces around the chain (resample_mesh.py:1463-1516)."""
    topo = read_topology(shape)
    chain_verts = {v for c in chains for v in c["verts"]}
    chain_edges = set()
    for chain in chains:
        for a, b in zip(chain["verts"], chain["verts"][1:]):
            eid = topo.edge_id(a, b)
            if eid is not None:
                chain_edges.add(eid)

    link_faces = sorted({f for e in chain_edges for f in topo.edge_faces.get(e, [])})
    pairs = []
    for fid in link_faces:
        runs = topo.face_runs(fid, chain_edges, chain_verts)
        for i in range(1, len(runs)):
            if len(runs[i - 1]) < 2 or len(runs[i]) < 2:
                continue
            pairs.extend(calculate_lines(
                topo.points, [(runs[i - 1], False), (runs[i], False)]))

    pairs = [(a, b) for a, b in pairs if a != b and topo.edge_id(a, b) is None]
    if not pairs:
        return
    created, failed = connect_vertices(transform, shape, pairs)
    if failed and created == 0:
        raise MeshError(
            "溶解横边后未能重建 %d 条连接边（Maya 的 polyConnectComponents "
            "没有生成任何边）。请 Ctrl+Z 撤销，并确认网格没有构造历史或变形器。"
            % len(pairs))
    if failed:
        cmds.warning("边重建: %d/%d 条连接边未能生成，例如 vtx[%d]-vtx[%d]。"
                     % (len(failed), len(pairs), failed[0][0], failed[0][1]))

    if auto_clean:
        topo = read_topology(shape)
        normals = topo.face_normals()
        coplanar = math.cos(0.001)
        stale = []
        for a, b in pairs:
            eid = topo.edge_id(a, b)
            if eid is None:
                continue
            faces = topo.edge_faces.get(eid, [])
            if len(faces) == 2 and float(np.dot(normals[faces[0]], normals[faces[1]])) > coplanar:
                stale.append(eid)
        delete_edges(transform, stale)


def distribute():
    """Blender modal ``D``: even arc-length distribution, topology untouched."""
    return resample(distribute_only=True)


# --------------------------------------------------------------------------
# Smooth
# --------------------------------------------------------------------------

def smooth(iterations=1, factor=0.5, weight_type="distance", smooth_type="SMOOTH"):
    """Laplacian (SMOOTH) or Taubin smooth/sharpen (SHARP) on the selection."""
    iterations = max(1, min(50, int(iterations)))
    if smooth_type not in ("SMOOTH", "SHARP"):
        raise MeshError("Smooth type 只能是 SMOOTH 或 SHARP。")
    if weight_type not in ("distance", "uniform"):
        raise MeshError("Parametrization 只能是 distance 或 uniform。")

    transform, shape = active_mesh()
    with undo_chunk("边重建 平滑"):
        topo = read_topology(shape)
        edges = selected_ids(shape, "e")
        faces = selected_ids(shape, "f")
        verts = topo.verts_of_edges(edges)
        chains = [c for c in topo.sort_chains(verts, edges) if len(c["verts"]) >= 2] if edges else []

        if chains:
            targets = [v for c in chains for v in c["verts"][1:-1]]
            neighbor_edges = set(edges)
        elif len(faces) < 2:
            targets = sorted(verts | set(selected_ids(shape, "vtx")))
            neighbor_edges = set(edges)
        else:
            face_set = set(faces)
            targets = []
            for v in sorted(verts | set(selected_ids(shape, "vtx"))):
                incident = topo.vert_faces.get(v, [])
                if not incident or not all(f in face_set for f in incident):
                    continue
                if any(len(topo.edge_faces.get(e, [])) < 2 for e in topo.vert_edges[v]):
                    continue
                targets.append(v)
            # The original left the neighbour set empty here; restrict to the
            # smoothed region instead so face-region smoothing actually moves.
            target_set = set(targets)
            neighbor_edges = {e for e in range(topo.num_edges)
                              if all(v in target_set for v in topo.edge_verts(e))}

        if not targets:
            raise MeshError("没有可平滑的顶点。请选择边链、顶点或至少两个面。")

        target_set = set(targets)
        neighbors = {v: [] for v in targets}
        for eid in neighbor_edges:
            a, b = topo.edge_verts(eid)
            if a in target_set:
                neighbors[a].append(b)
            if b in target_set:
                neighbors[b].append(a)

        positions = {v: topo.points[v].copy() for v in set(targets) | set(
            n for nbrs in neighbors.values() for n in nbrs)}
        valence = {v: len(topo.vert_edges[v]) for v in positions}
        apply_smooth(positions, neighbors, valence, smooth_type=smooth_type,
                         iterations=iterations, factor=float(factor), weight_type=weight_type)
        write_points(shape, {v: positions[v] for v in targets})
        refresh()
    return len(targets)


# --------------------------------------------------------------------------
# Circularize
# --------------------------------------------------------------------------

def circularize(angle=90.0, flip=False):
    """Turn selected open chains into circular arcs; endpoints stay fixed."""
    angle = float(angle)
    if not 0.1 <= abs(angle) <= 359.9:
        raise MeshError("Angle 必须在 0.1 到 359.9 度之间。")

    transform, shape = active_mesh()
    with undo_chunk("边重建 圆化"):
        topo, _, _, chains = _chain_selection(shape)
        if sum(len(c["verts"]) for c in chains) < 3:
            raise MeshError("至少需要 3 个相连的顶点。")

        updates = {}
        moved = 0
        for chain in chains:
            verts = chain["verts"]
            if len(verts) < 3:
                continue
            arc = circularize_chain(topo.points[verts], angle=angle, flip=flip)
            for vid, pos in zip(verts[1:-1], arc):
                updates[vid] = pos
                moved += 1
        if not moved:
            raise MeshError("没有可圆化的链（每条链至少需要 3 个顶点）。")
        write_points(shape, updates)
        select_components(transform, "e", [e for c in chains for e in c["edges"]])
        refresh()
    return moved


# --------------------------------------------------------------------------
# Unknot
# --------------------------------------------------------------------------

def unknot(width=0.0, tension=0.5, spline_type="AKIMA", uniform=True):
    """Relax a chain using end-tangent guides (resample_mesh.py:1610-1676)."""
    if spline_type not in SPLINE_TYPES:
        raise MeshError("未知样条类型：%s" % spline_type)
    width, tension = float(width), float(tension)

    transform, shape = active_mesh()
    with undo_chunk("边重建 解结"):
        topo, _, _, chains = _chain_selection(shape)
        updates = {}
        moved = 0

        for chain in chains:
            verts = chain["verts"]
            if len(verts) < 4:
                continue
            coords = topo.points[verts].copy()
            v1, v2, v3, v4 = coords[0], coords[1], coords[-1], coords[-2]

            coords[1] = v2 + _normalize(v1 - v2) * width
            coords[-2] = v4 + _normalize(v3 - v4) * width
            updates[verts[1]] = coords[1]
            updates[verts[-2]] = coords[-2]
            v2, v4 = coords[1], coords[-2]

            closest = _closest_points_on_lines(v2, v1, v4, v3)
            if closest is not None:
                p = (closest[0] + closest[1]) * 0.5
                guide = p + ((v2 + v4) * 0.5 - p) * tension
            else:
                p = (v2 + v4) * 0.5
                guide = p + _normalize((v2 - v1) + (v4 - v3)) * (
                    float(np.linalg.norm(v2 - v4)) * 0.5) * tension

            interior = verts[1:-1]
            count = len(interior)
            samples = fit(np.array([v2, guide, v4]), count, spline_type,
                                     space=uniform)
            if uniform:
                samples = resample_uniform(samples, count)
            for j in range(1, min(count - 1, len(samples) - 1)):
                updates[interior[j]] = samples[j]
                moved += 1

        if not updates:
            raise MeshError("Unknot 需要至少 4 个顶点的开放链。")
        write_points(shape, updates)
        select_components(transform, "e", [e for c in chains for e in c["edges"]])
        refresh()
    return moved


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------

def bridge(connect=True, mode="shortest", reverse=False, twist=0):
    """Bridge two, or loft several, selected edge loops.

    connect=True creates connecting edges only (the Blender ``C`` key).
    connect=False creates faces via Maya's native polyBridgeEdge.
    """
    if mode not in ("basic", "shortest"):
        raise MeshError("Bridge mode 只能是 basic 或 shortest。")

    transform, shape = active_mesh()
    with undo_chunk("边重建 桥接"):
        if selected_ids(shape, "f"):
            raise MeshError("检测到面选择，请只选择边循环。")
        edges = selected_ids(shape, "e")
        if not edges:
            raise MeshError("请先选择两个或多个边循环。")

        topo = read_topology(shape)
        loops = topo.connected_loops(edges)
        if len(loops) < 2:
            raise MeshError("需要至少两个独立的边循环。")
        if len(loops) > 2:
            loops = sort_loops(topo.points, loops)

        if connect:
            pairs = []
            for i in range(1, len(loops)):
                pairs.extend(calculate_lines(
                    topo.points, loops[i - 1:i + 1], mode=mode,
                    twist=int(twist), reverse=bool(reverse)))
            pairs = [(a, b) for a, b in pairs if a != b and topo.edge_id(a, b) is None]
            if not pairs:
                raise MeshError("两个循环之间已经存在连接边。")
            connect_vertices(transform, shape, pairs)
        else:
            border = []
            for loop, _ in loops:
                loop_edges = [topo.edge_id(a, b) for a, b in zip(loop, loop[1:])]
                if any(e is None or len(topo.edge_faces.get(e, [])) != 1 for e in loop_edges):
                    raise MeshError("生成面模式（Connect 关闭）要求循环是开放边界。"
                                    "内部循环请改用 Connect 打开，只生成连接边。")
                border.append(loop_edges)
            for i in range(1, len(border)):
                cmds.polyBridgeEdge(
                    component_names(transform, "e", border[i - 1]),
                    component_names(transform, "e", border[i]),
                    divisions=0, twist=int(twist), constructionHistory=False)
        refresh()
    return len(loops)


class _DraggableTabBar(QtWidgets.QTabBar):
    """可拖拽重排序 + 双击弹出独立窗口的选项卡条。

    - 左键按住标签可在 tab 条内拖拽重排序
    - 双击标签调用 detach_handler 回调（参数 tab_index, tab_text），由外部弹出

    注意：不使用 QtCore.Signal——Cython 3.1.1 编译进 pyd 后在 Maya 2023
    （Py3.9 + PySide2 5.15.2）导入期触发 shiboken2 崩溃，普通回调跨版本安全。
    """

    def __init__(self, parent=None):
        super(_DraggableTabBar, self).__init__(parent)
        self.setMovable(True)          # 允许 tab 重排
        self.setDocumentMode(True)     # 更扁平的 tab 外观
        self.setElideMode(QtCore.Qt.ElideNone)
        self._press_pos = None
        self._pressed_index = -1
        self.detach_handler = None     # 双击回调: f(index, label)

    def mousePressEvent(self, event):
        """记录按下位置和 tab 索引，用于区分拖拽 vs 双击。"""
        if event.button() == QtCore.Qt.LeftButton:
            self._press_pos = event.globalPos()
            self._pressed_index = self.tabAt(event.pos())
        super(_DraggableTabBar, self).mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击标签：请求弹出为独立窗口。"""
        if event.button() == QtCore.Qt.LeftButton:
            index = self.tabAt(event.pos())
            if index >= 0 and self.detach_handler is not None:
                self.detach_handler(index, self.tabText(index))
                return
        super(_DraggableTabBar, self).mouseDoubleClickEvent(event)


class _DetachedWindow(QtWidgets.QWidget):
    """选项卡弹出后的独立窗口：以 Maya 主窗口为父 + Qt.Window 标志，保持在 Maya 前置。

    关闭时调用 on_closed 回调（参数为标签名），由 _DraggableTabWidget 回收到
    原选项卡位置。用真正的子类重载 closeEvent：实例属性打补丁在从属窗口下
    不生效，导致关闭后不合并。不使用 QtCore.Signal（见 _DraggableTabBar 注释）。
    """

    def __init__(self, label, parent=None):
        super(_DetachedWindow, self).__init__(parent, QtCore.Qt.Window)
        self._ljgt_label = label
        self.on_closed = None          # 关闭回调: f(label)
        self.setWindowTitle(u"零距工具集 · " + label)

    def closeEvent(self, event):
        """点 X / Alt+F4 关闭时通知主选项卡把自己收回去。"""
        if self.on_closed is not None:
            self.on_closed(self._ljgt_label)
        event.accept()


class _DraggableTabWidget(QtWidgets.QTabWidget):
    """主界面选项卡容器：横向 tab、支持重排序、支持弹出独立窗口。

    设计说明：
    - 主窗口内用 QTabWidget 承载各功能页，每页内部仍是 cmds 构建的 UI
      （通过 cmds.paneLayout 作为 cmds 与 Qt 之间的桥接）
    - 双击 tab 标签 → _pop_out_tab()：原页面只从 tab 条移除（仍留在
      QStackedWidget 内隐藏，C++ 与路径保持有效）；_DetachedWindow 独立窗口
      （Maya 主窗口的从属顶层窗口，保持前置）里放的是用构建函数重建的临时副本
    - 独立窗口关闭时 → _merge_back()：删除副本，把原页面插回主 QTabWidget
      原来的 tab 位置（记住弹出前的 index）
    - 右键 tab 标题弹出菜单：「弹出此页」/「合并全部页」
    """

    # 保存所有弹出窗口：{tab_label: (独立窗口, 原页面 widget, 弹出前 tab 索引, 副本 pane 名)}
    _detached = {}
    # create_ui 存入：{label: (pane_name, builder)}，弹出时用于构建临时副本
    _pane_builders = {}
    _pane_grey = None

    def __init__(self, parent=None):
        super(_DraggableTabWidget, self).__init__(parent)
        # 用自定义 TabBar 替换默认的
        self._tab_bar = _DraggableTabBar(self)
        self.setTabBar(self._tab_bar)
        self.setTabPosition(QtWidgets.QTabWidget.North)   # 横向 tab
        self.setDocumentMode(True)
        self.setMovable(True)

        self._tab_bar.setMinimumHeight(28)
        self._tab_bar.setStyleSheet(
            u"QTabBar::tab { min-height: 18px; padding: 2px 18px;"
            u" font-size: 12px; }"
            u"QTabBar::tab:selected { background-color: #2e674f;"
            u" color: #FFFFFF; font-weight: bold; border-radius: 4px; }"
            u"QTabBar::tab:hover:!selected { background-color: #1b3e2f;"
            u" border-radius: 4px; }")

        # 双击弹出：普通回调（不用 Signal，规避 pyd 编译在 Maya 2023 的崩溃）
        self._tab_bar.detach_handler = self._pop_out_tab
        self._tab_bar.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._tab_bar.customContextMenuRequested.connect(self._show_context_menu)

    # ------------------------------------------------------------------
    # 弹出独立窗口
    # ------------------------------------------------------------------
    def _pop_out_tab(self, index, label):
        """把第 index 个 tab 的页面弹出为独立顶层窗口。

        v174 修正：原页面绝不重挂到非 Maya 窗口（那是空白页、路径失效、
        C++ 被销毁、布局错乱等一系列问题的根源）。
        - 原页面只 removeTab，留在 QStackedWidget 内隐藏，C++ 与 Maya
          路径始终有效；
        - 独立窗口里放的是用构建函数重建的临时副本 pane（名字加 _pop
          后缀），关闭时删掉副本、原页面插回原位。
        """
        # 如果已经弹出了一个同名窗口，先合并再弹新的，避免重复
        if label in self._detached:
            self._merge_back(label)

        page = self.widget(index)
        if page is None or not _qt_valid(page):
            return

        info = self._pane_builders.get(label)
        if info is None:
            return
        pane_name, builder = info

        # 1. 在 workspaceControl 下建临时副本（Maya 管理的布局，显示才正确）
        tmp_name = pane_name + "_pop"
        try:
            if cmds.paneLayout(tmp_name, q=True, exists=True):
                cmds.deleteUI(tmp_name)
            cmds.paneLayout(tmp_name,
                            p=LingJuGeometryTools.LJGT_WINDOW_NAME,
                            width=325, height=600)
            cmds.setParent(tmp_name)
            builder(self._pane_grey)
            cmds.setParent('..')
            ptr = omui.MQtUtil.findControl(tmp_name)
            if not ptr:
                return
            tmp_widget = wrapInstance(int(ptr), QtWidgets.QWidget)
        except Exception:
            return

        # 2. 原页面从 tab 条移除（仍留在 QStackedWidget 里，只是隐藏）
        self.removeTab(index)

        # 3. 临时副本交给独立窗口（父级 Maya 主窗口，保持前置）
        maya_ptr = omui.MQtUtil.mainWindow()
        maya_win = (wrapInstance(int(maya_ptr), QtWidgets.QWidget)
                    if maya_ptr else None)
        detached_win = _DetachedWindow(label, maya_win)

        layout = QtWidgets.QVBoxLayout(detached_win)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        tmp_widget.setParent(None)
        layout.addWidget(tmp_widget)
        tmp_widget.show()

        # 关闭时删除副本、把原页面插回原位
        detached_win.on_closed = self._merge_back

        self._detached[label] = (detached_win, page, index, tmp_name)
        detached_win.resize(325, 600)
        detached_win.show()
        detached_win.raise_()
        detached_win.activateWindow()

    def _merge_back(self, label):
        """关闭独立窗口：删除临时副本 pane，销毁窗口，把原页面插回原位。

        原页面从未离开 QStackedWidget（C++ 与路径一直有效），这里不需要
        搬动任何控件，也不再出现「页面失效 / 合并后布局错乱」的问题。
        """
        entry = self._detached.pop(label, None)
        if entry is None:
            return
        detached_win, page, insert_index = entry[0], entry[1], entry[2]
        tmp_name = entry[3] if len(entry) > 3 else None

        # 1. 删除临时副本（Maya 侧 UI）
        try:
            if tmp_name and cmds.paneLayout(tmp_name, q=True, exists=True):
                cmds.deleteUI(tmp_name)
        except Exception:
            pass

        # 2. hide + deleteLater：不能用 close()，否则会再次触发 closeEvent 递归
        try:
            if _qt_valid(detached_win):
                detached_win.hide()
                detached_win.deleteLater()
        except RuntimeError:
            pass

        # 放回主 QTabWidget 原位置
        try:
            if insert_index < 0 or insert_index > self.count():
                insert_index = self.count()
            self.insertTab(insert_index, page, label)
            self.setCurrentIndex(insert_index)
            page.show()
        except RuntimeError:
            # 主选项卡本体也已被销毁（如工具面板已关闭），只能放弃合并
            return

        # 重挂回 QTabWidget 后 Maya 不会自动重排 pane 内部布局（内容会挤成
        # 一小块、下方大片空白）；用 create_ui 同款手段强制重新布局
        try:
            if cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                                     q=True, exists=True):
                cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                                      e=True, restore=True)
        except Exception:
            pass

    def merge_all(self):
        """合并所有已弹出的独立窗口回主选项卡。"""
        for label in list(self._detached.keys()):
            self._merge_back(label)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos):
        """右键 tab 标题弹出菜单。"""
        index = self._tab_bar.tabAt(pos)
        if index < 0:
            return
        label = self.tabText(index)

        menu = QtWidgets.QMenu(self)
        act_detach = menu.addAction(u"弹出此页：" + label)
        act_detach.triggered.connect(lambda: self._pop_out_tab(index, label))

        if self._detached:
            menu.addSeparator()
            act_merge = menu.addAction(u"合并全部已弹出页")
            act_merge.triggered.connect(self.merge_all)

        menu.exec_(self._tab_bar.mapToGlobal(pos))


class LingJuGeometryTools:
    LJGT_WINDOW_NAME = "LingJuGeometryTools_Window"

    initial_faces = cmds.ls(sl=True, fl=True)

    def __init__(self):
        self.FILL_SELECTION = None
        self.keep_original = None
        self.separate_radio_grp = None
        self.mode = None
        self.window = None
        self.progress_bar = None
        self.layout_mode = 1
        self.ALIGN_EDGE = Align_Edge()
        self.INSTANT_DRAG = Instant_Drag()
        self.ROUND_INSET = Round_Inset()
        self.FILL_SELECTION = Fill_Selection()
        self.SNAP_2_PLANE = Snap_2_Plane()

        self.grey_blue = (0.198, 0.218, 0.238)
        self.grey = (0.31, 0.32, 0.35)
        self.inner_grey = (0.258, 0.278, 0.298)
        self.yellow_hex = 0xCC7F00
        self.inner_margins4 = (12, 2, 2, 2)

        # Pivot 边界吸附状态：当前操作平面与三个平面按钮的 UI 名
        self.pivot_plane = 'xy'
        self._pivot_plane_btns = {}

        # 传递UV 的参考物体（Python 侧保存，不做 mel 全局变量往返）
        self._uv_transfer_ref = ""
        self._uv_transfer_ref_label = None

        # 数值框滚轮步进：扭曲阈值 / 按角度选面 / UV 检查 / 边重建共用
        self._wheel_binders = []
        self._wheel_specs = []
        self._wheel_attached = set()

        self.create_ui()

    def _ensure_polyraven_loaded(self):
        """首次调用时用 mel.eval source 内嵌的 PolyravenUV mel，之后不再重复。

        返回 True 表示 proc 已可用，False 表示加载失败（按钮应提示）。
        """
        global _POLYRAVEN_LOADED
        if _POLYRAVEN_LOADED:
            return True
        if not POLYRAVEN_UV_MEL:
            lj_error(u"v170 内嵌的 mel 为空，无法加载")
            return False
        try:
            # Polyraven 依赖的全局变量：$MayaVersion / $prUV_GlobalShellList
            # （Rectangulate 走 shellList 分支时会读后者）
            mel.eval('global int $MayaVersion; $MayaVersion = {};'.format(
                int(cmds.about(version=True).split()[0].split(".")[0])))
            mel.eval('global string $prUV_GlobalShellList[]; $prUV_GlobalShellList = {};')
            mel.eval(POLYRAVEN_UV_MEL)
            _POLYRAVEN_LOADED = True
            return True
        except Exception as error:
            lj_error(u"PolyravenUV 内嵌 mel 加载失败：%s" % error, log=True)
            return False

    """
        ------------------------------------ Layouts ------------------------------------
    """

    def create_ui(self):
        """主界面入口：workspaceControl + QTabWidget 架构。

        流程：
        1. 清理旧窗口 / 独立弹出窗口
        2. 创建 workspaceControl（Maya 原生可停靠面板容器）
        3. 在 workspaceControl 内用 cmds 创建 5 个 paneLayout 桥接容器
        4. 每个 paneLayout 内构建对应功能区的 cmds UI（复用原有子布局）
        5. 通过 MQtUtil.wrapInstance 获取各 paneLayout 的 Qt 句柄
        6. 创建 _DraggableTabWidget，把 5 个 paneLayout 移入作为 tab pages
        7. 把 QTabWidget 放回 workspaceControl 的 Qt layout
        """
        # --- 1. 清理旧窗口 ---
        if cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME, q=True, exists=True):
            cmds.deleteUI(LingJuGeometryTools.LJGT_WINDOW_NAME)
        if cmds.window("prUV_CheckerFlowWin", exists=True):
            cmds.deleteUI("prUV_CheckerFlowWin")

        # 先清理上次运行遗留的弹出独立窗口：原页面留在 QStackedWidget 内，
        # 随 workspaceControl 一起销毁；临时副本 pane 活在独立窗口里，
        # 必须 deleteUI 显式删除，释放其占用的控件名
        for entry in list(_DraggableTabWidget._detached.values()):
            win = entry[0]
            tmp_name = entry[3] if len(entry) > 3 else None
            try:
                win.hide()
            except Exception:
                pass
            try:
                if tmp_name and cmds.paneLayout(tmp_name, q=True, exists=True):
                    cmds.deleteUI(tmp_name)
            except Exception:
                pass
            try:
                win.deleteLater()
            except Exception:
                pass
        _DraggableTabWidget._detached.clear()

        # --- 2. 创建 workspaceControl ---
        # 最小宽度 = 最宽控件(310) + 纵向滚动条(13) + 余量 = 325，
        # 保证默认/最小宽度下滚动条就可见，不用拉宽窗口才露出来
        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                              label=u"成都零距数码工具集v179a1",
                              retain=True,
                              initialWidth=325,
                              minimumWidth=325,
                              mh=650,
                              resizeWidth=2,
                              widthProperty='fixed')

        grey_value = (0.20, 0.16, 0.20)

        # --- 3. 创建 columnLayout 作为 cmds 临时父容器（随后整体移入 QTabWidget） ---
        top_col = cmds.columnLayout(p=LingJuGeometryTools.LJGT_WINDOW_NAME)

        # --- 4. 为每个功能区创建 paneLayout + cmds 子内容 ---
        # paneLayout 是 cmds 与 Qt 之间的桥梁：cmds 命令在 pane 内构建 UI，
        # 之后通过 wrapInstance 拿到 pane 的 Qt 句柄移到 QTabWidget。
        tab_panes = {}

        # tab 1：建模
        pane_m = cmds.paneLayout('ljgt_pane_modeling', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_m)
        self._build_tab_content_modeling(grey_value)
        tab_panes[u"建模"] = pane_m

        # tab 2：选择
        pane_s = cmds.paneLayout('ljgt_pane_selection', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_s)
        self._build_tab_content_selection(grey_value)
        tab_panes[u"选择"] = pane_s

        # tab 3：命名
        pane_n = cmds.paneLayout('ljgt_pane_naming', p=top_col,
                                 width=325, height=500) 
        cmds.setParent(pane_n)
        self._build_tab_content_naming(grey_value)
        tab_panes[u"命名"] = pane_n

        # tab 4：UV
        pane_u = cmds.paneLayout('ljgt_pane_uv', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_u)
        self._build_tab_content_uv(grey_value)
        tab_panes[u"UV"] = pane_u

        # tab 5：烘焙
        pane_b = cmds.paneLayout('ljgt_pane_bake', p=top_col,
                                 width=325, height=500)
        cmds.setParent(pane_b)
        self._build_tab_content_bake(grey_value)
        tab_panes[u"烘焙"] = pane_b

        # --- 5. Qt 层：获取 workspaceControl 句柄 ---
        ctrl_ptr = omui.MQtUtil.findControl(LingJuGeometryTools.LJGT_WINDOW_NAME)
        ctrl_widget = wrapInstance(int(ctrl_ptr), QtWidgets.QWidget)

        # --- 6. 创建 DraggableTabWidget ---
        self._tab_widget = _DraggableTabWidget(ctrl_widget)
        self._tab_widget.setObjectName("LJGT_TabWidget")
        # 存入各 tab 的构建函数：弹出页面若被 Maya 销毁，_merge_back 用它重建
        self._tab_widget._pane_builders = {
            u"建模": ("ljgt_pane_modeling", self._build_tab_content_modeling),
            u"选择": ("ljgt_pane_selection", self._build_tab_content_selection),
            u"命名": ("ljgt_pane_naming", self._build_tab_content_naming),
            u"UV": ("ljgt_pane_uv", self._build_tab_content_uv),
            u"烘焙": ("ljgt_pane_bake", self._build_tab_content_bake),
        }
        self._tab_widget._pane_grey = grey_value

        # --- 7. 把每个 paneLayout 从旧布局移到 QTabWidget ---
        for label, pane_name in tab_panes.items():
            pane_ptr = omui.MQtUtil.findControl(pane_name)
            pane_widget = wrapInstance(int(pane_ptr), QtWidgets.QWidget)
            pane_widget.setParent(None)
            self._tab_widget.addTab(pane_widget, label)

        # --- 8. 清空 workspaceControl 内部旧布局，放入 QTabWidget ---
        ctrl_layout = ctrl_widget.layout()
        if ctrl_layout is not None:
            # 取出所有旧子 widget（scrollLayout 等）并删除
            while ctrl_layout.count() > 0:
                item = ctrl_layout.takeAt(0)
                old_widget = item.widget()
                if old_widget is not None:
                    old_widget.setParent(None)
                    old_widget.deleteLater()
        else:
            ctrl_layout = QtWidgets.QVBoxLayout(ctrl_widget)

        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(0)
        ctrl_layout.addWidget(self._tab_widget)

        # --- 9. 恢复 workspaceControl ---
        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                              e=True, restore=True)

    # ==================================================================
    # 每个选项卡页面的 cmds 内容构建方法
    # （去掉了原来的外层 frameLayout——tab 标签已替代分组角色）
    # ==================================================================

    def _build_tab_content_modeling(self, bg_color):
        """建模选项卡：scrollLayout + columnLayout + 各子折叠栏 + 建模按钮区。"""
        cmds.scrollLayout(childResizable=True,
                          horizontalScrollBarThickness=0,
                          verticalScrollBarThickness=13)
        cmds.columnLayout(rowSpacing=2, adjustableColumn=True)

        self.create_pivots_settings_layout()
        self.create_ring_split_layout()
        self.create_twist_face_check_layout()
        self.create_quick_bottom_face_layout()
        self.create_edge_rebuild_layout()
        self.create_smart_combine_separate_layout()
        cmds.setParent('..')  # 回到 scrollLayout

        cmds.text(label=" ", height=5)

        # create_modeling_buttons 内部创建了自己的 columnLayout 但不关闭它，
        # 需要调用者补一次 setParent('..')
        self.create_modeling_buttons()
        cmds.setParent('..')  # 关闭 create_modeling_buttons 内部的 columnLayout，回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_selection(self, bg_color):
        """选择选项卡：scrollLayout + columnLayout + 按角度选面 / 相似选择 / 选择按钮。"""
        cmds.scrollLayout(childResizable=True,
                          horizontalScrollBarThickness=0,
                          verticalScrollBarThickness=13)
        cmds.columnLayout(adjustableColumn=True)

        self.create_select_by_angle_layout()
        self.create_select_similar_layout()
        cmds.text(label=" ", height=5)

        self.create_selection_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_naming(self, bg_color):
        """命名选项卡：scrollLayout + columnLayout + 命名按钮区。"""
        cmds.scrollLayout(childResizable=True,
                          horizontalScrollBarThickness=0,
                          verticalScrollBarThickness=13)
        cmds.columnLayout(adjustableColumn=True)

        self.create_naming_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    def _build_tab_content_uv(self, bg_color):
        """UV 选项卡：scrollLayout + UV检查 + 传递UV + 打直UV 按钮。"""
        cmds.scrollLayout(childResizable=True,
                          horizontalScrollBarThickness=0,
                          verticalScrollBarThickness=13)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

        self.create_UV_checker_frame()
        self.create_UV_transfer_frame()

        cmds.text(label=" ", height=5)

        # 打直UV：Rectangulate 独立按钮
        cmds.button(label=u"打直UV",
                    command=lambda *_: self.uv_rectangulate(),
                    width=310, height=28, bgc=self.grey,
                    annotation=u"把当前选中的 UV shell 尽可能拉直成矩形")

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

        # 注：create_UV_checker_frame 和 create_UV_transfer_frame 内部已各自
        #     做过一次展开→折回以保证高度计算正确，这里不再重复

    def _build_tab_content_bake(self, bg_color):
        """烘焙选项卡：scrollLayout + columnLayout + Maya2Marmoset 按钮。"""
        cmds.scrollLayout(childResizable=True,
                          horizontalScrollBarThickness=0,
                          verticalScrollBarThickness=13)
        cmds.columnLayout(adjustableColumn=True)

        self.create_bake_buttons()

        cmds.setParent('..')  # 回到 scrollLayout
        cmds.setParent('..')  # 关闭 scrollLayout，回到 paneLayout

    # ------------------------------------------------------------------
    # 传递 UV（搬运自 PolyravenUV_v1.0.5.mel，直接调 mel proc）
    # ------------------------------------------------------------------
    def create_UV_transfer_frame(self):
        """传递 UV 折叠栏：参考物体 + 四种 transfer 模式

        完全调用 PolyravenUV 的 mel proc（prUV_TransferUV / prUV_GetSelected），
        不转译逻辑，仅提供 Python 侧的 UI 和状态保持。
        """
        frame = cmds.frameLayout(label=u"传递UV", width=299,
                                 bgc=self.grey_blue, collapsable=True, collapse=False,
                                 marginWidth=10, marginHeight=5)
        # 行距与快速底面一致：默认间距，不额外加 rowSpacing
        cmds.columnLayout(adjustableColumn=True)

        # 参考物体显示 label（状态）
        label_text = self._uv_transfer_ref or u"(请先选中参考物体)"
        self._uv_transfer_ref_label = _ui_short_name(cmds.text(label=label_text, align="center"))

        cmds.rowLayout(numberOfColumns=1)
        cmds.button(label=u"设为参考物体",
                    command=lambda *_: self.uv_transfer_set_reference(),
                    width=275, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"传递UV (ID)",
                    command=lambda *_: self.uv_transfer(4),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.button(label=u"传递UV (拓扑)",
                    command=lambda *_: self.uv_transfer(5),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"投影UV",
                    command=lambda *_: self.uv_transfer(0),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.button(label=u"传递形状",
                    command=lambda *_: self.uv_transfer(-1),
                    width=136, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.setParent('..')   # columnLayout -> frameLayout
        cmds.setParent('..')   # frameLayout -> UV 的 columnLayout

        # 以 collapse=True 创建时 Maya 不计算子控件高度，展开后会裁剪内容并把
        # 烘焙折叠栏挤出可视区；建完先展开算一次高度，再折回去
        cmds.frameLayout(frame, edit=True, collapse=False)
        cmds.frameLayout(frame, edit=True, collapse=True)

    def uv_transfer_set_reference(self, *args):
        """把当前选中物体设为 TransferUV 的参考对象（包装 mel 调用）"""
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        sel = cmds.ls(selection=True, long=True) or []
        if not sel:
            lj_warn(u"请先选中一个物体作为参考")
            return
        # Polyraven 用最后选中的物体当参考，这里显式把最后一个设进去
        last = sel[-1]
        self._uv_transfer_ref = last
        cmds.text(self._uv_transfer_ref_label, edit=True, label=last)
        lj_info(u"参考物体已设置：%s" % last)

    def uv_transfer(self, mode):
        """调用 PolyravenUV.prUV_TransferUV 把参考物体的 UV/形状传给当前选中物体

        :param mode: int，对应 Polyraven 的 sampleSpace / transferMode
                     4 = Transfer UV (ID, sampleSpace=4)
                     5 = Transfer UV (Topo, sampleSpace=5)
                     0 = Project UV (sampleSpace=0)
                    -1 = Transfer Shape（走 transferPositions）
        """
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        ref_obj = self._uv_transfer_ref
        if not ref_obj:
            lj_warn(u"请先设置参考物体")
            return
        sel = cmds.ls(selection=True, long=True) or []
        if not sel:
            lj_warn(u"请先选中要接收 UV 的目标物体")
            return
        try:
            # Polyraven 的 TransferUV 逻辑：它内部用 `ls -os -o -l` 取当前 selection；
            # 这里把目标 + 参考一起选上再交给它
            cmds.select(sel, replace=True)
            cmds.select(ref_obj, add=True)
            mel.eval('prUV_TransferUV("{}", {});'.format(ref_obj.replace('\\', '/'), mode))
            cmds.select(sel, replace=True)  # 恢复原选择
        except Exception as error:
            lj_error(u"TransferUV 执行失败：%s" % error)

    def uv_rectangulate(self, *args):
        """调用 PolyravenUV.prUV_RectangulateShell 把当前 UV shell 打直成矩形"""
        if not self._ensure_polyraven_loaded():
            lj_error(u"PolyravenUV 内嵌 mel 未加载，详见 Script Editor")
            return
        sel = cmds.ls(selection=True) or []
        if not sel:
            lj_warn(u"请先选中要打直的 UV shell 或面")
            return
        try:
            mel.eval('prUV_RectangulateShell();')
        except Exception as error:
            lj_error(u"打直UV 执行失败：%s" % error)

    def create_bake_buttons(self):
        cmds.rowLayout(numberOfColumns=1)
        cmds.button(label=u"Maya2Marmoset", command=self.show_maya2marmoset,
                    width=300, height=28, bgc=self.grey,
                    annotation=u"打开 Maya2Marmoset 桥接面板：自动分组、导出 FBX 并推送给 Toolbag")
        cmds.setParent('..')

    def show_maya2marmoset(self, *args):
        try:
            m2m_ui_show()
        except RecursionError as error:
            LingJuGeometryTools.report_recursion(error)

    @staticmethod
    def report_recursion(error, head=3, top=3):
        """爆栈时只报「哪些位置在反复」：同一行出现 N 次就折叠成 xN。

        脚本编辑器里 RecursionError 会刷上千行帧，真正有用的是重复段。对敲的两个
        函数是交替出现的，所以按位置计数（而不是只压相邻重复）才能把它们点出来。
        """
        from collections import Counter
        import traceback
        names = ['%s:%s %s()' % (os.path.basename(frame.filename),
                                 frame.lineno, frame.name)
                 for frame in traceback.extract_tb(error.__traceback__)]
        if not names:
            print('Maya2Marmoset: 递归爆栈，但拿不到栈帧')
            lj_error(u'Maya2Marmoset: 面板打开失败（递归爆栈），详见 Script Editor')
            return
        counts = Counter(names)
        lj_error(u'Maya2Marmoset: 面板打开失败（递归爆栈），详见 Script Editor')
        print('Maya2Marmoset: 递归爆栈（limit=%s，共 %s 帧 / %s 个位置）' % (
            sys.getrecursionlimit(), len(names), len(counts)))
        for name in names[:head]:
            print('    入口 %s  共 x%s' % (name, counts[name]))
        for name, count in counts.most_common(top):
            if count > 1:
                print('    重复 x%-6s %s' % (count, name))

    def create_UV_checker_frame(self):
        """UV 检查材质：内嵌在 UV 布局里，不弹窗。"""
        cmds.frameLayout(label=u"UV检查材质", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True,
                         marginWidth=10, marginHeight=5,
                         expandCommand=self._wheel_attach_all)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

        cmds.rowColumnLayout(numberOfColumns=2,
                             columnWidth=[(1, 96), (2, 80)],
                             columnAttach=[(1, "both", 2), (2, "both", 2)])
        cmds.text(label="贴图大小:", align="right")
        res_field = cmds.intField(UV_CHECKER_UI_RES, value=_UV_CHECKER_SETTINGS["resolution"],
                                  minValue=0, maxValue=8192, step=256)
        self._wheel_register(res_field, integer=True, step=256, low=0, high=8192)
        cmds.text(label="棋盘格1大小", align="right")
        checker1_field = cmds.intField(UV_CHECKER_UI_CHK1, value=_UV_CHECKER_SETTINGS["checker1"],
                                       minValue=0, maxValue=1024, step=256)
        self._wheel_register(checker1_field, integer=True, step=256, low=0, high=1024)
        cmds.text(label="棋盘格2大小", align="right")
        checker2_field = cmds.intField(UV_CHECKER_UI_CHK2, value=_UV_CHECKER_SETTINGS["checker2"],
                                       minValue=4, step=8)
        self._wheel_register(checker2_field, integer=True, step=8, low=4)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label="创建/更新", width=136, height=28, bgc=self.inner_grey,
                    command=lambda *_: self.uv_checker_apply())
        cmds.button(label="循环指定材质", width=136, height=28, bgc=self.inner_grey,
                    command=lambda *_: uv_checker_cycle_shader())
        cmds.setParent('..')

        cmds.setParent('..')
        cmds.setParent('..')

    def uv_checker_apply(self):
        """读取输入框并创建/更新 UV 检查材质。"""
        _UV_CHECKER_SETTINGS.update(
            resolution=cmds.intField(UV_CHECKER_UI_RES, query=True, value=True),
            checker1=cmds.intField(UV_CHECKER_UI_CHK1, query=True, value=True),
            checker2=cmds.intField(UV_CHECKER_UI_CHK2, query=True, value=True))
        uv_checker_create_shader(_UV_CHECKER_SETTINGS["resolution"],
                                 _UV_CHECKER_SETTINGS["checker1"],
                                 _UV_CHECKER_SETTINGS["checker2"])

    def open_modeling_fold(self, label):
        """建模区折叠栏统一规格：同宽、同内边距、默认收起；栏间距由父列 rowSpacing 统一给"""
        frame = cmds.frameLayout(label=label, width=299,
                                 bgc=self.grey_blue, collapsable=True, collapse=True,
                                 marginWidth=10, marginHeight=5)
        cmds.columnLayout()
        return frame

    def close_modeling_fold(self):
        cmds.setParent('..')
        cmds.setParent('..')

    def create_twist_face_check_layout(self):
        """扭曲面检查折叠栏：内嵌 twist_face_check.py 的检查/三角化/Spin 逻辑"""
        fold = self.open_modeling_fold(u"扭曲面检查")
        cmds.frameLayout(fold, edit=True, expandCommand=self._wheel_attach_all)

        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=3,
                       columnAttach3=("left", "left", "left"),
                       columnWidth3=(52, 172, 50), columnOffset3=(0, 2, 2))
        cmds.text(label=u"扭曲阈值", align="left", width=48, height=18)
        self._twist_threshold_slider = _ui_short_name(cmds.floatSlider(
            minValue=TWIST_MIN_THRESHOLD, maxValue=TWIST_MAX_THRESHOLD,
            step=1, value=self._twist_load_threshold(), width=172,
            changeCommand=self._twist_slider_changed,
            dragCommand=self._twist_slider_changed,
            annotation=u"四边面两个三角化三角形法线的夹角阈值（度）"))
        self._twist_threshold_field = _ui_short_name(cmds.floatField(
            minValue=TWIST_MIN_THRESHOLD, maxValue=TWIST_MAX_THRESHOLD,
            step=1, value=self._twist_load_threshold(), precision=1,
            width=50, changeCommand=self._twist_field_changed))
        self._wheel_register(
            self._twist_threshold_field, integer=False, step=1,
            callback=self._twist_field_wheel_changed,
            low=TWIST_MIN_THRESHOLD, high=TWIST_MAX_THRESHOLD, precision=1)
        cmds.setParent('..')
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"检查并选择", command=lambda *args: self.twist_check_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"只检查四边面，选中法线夹角超过阈值的四边面。\n"
                               u"有选择时只检查当前选择；无选择时扫描全场景。\n"
                               u"结果在视口提示中显示。")
        cmds.button(label=u"三角化", command=lambda *args: self.twist_triangulate_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"对当前选择执行 Maya 原生 polyTriangulate。\n"
                               u"通常先检查并选中扭曲四边面，再点击本按钮。")
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.button(label=u"Spin 连线", command=lambda *args: self.twist_spin_clicked(),
                    width=275, height=28, bgc=self.inner_grey,
                    annotation=u"只对当前选中的边执行 polyFlipEdge。\n"
                               u"请先三角化，再选中内部连线；三角面、边界边、非流形边自动忽略。")
        cmds.setParent('..')

        self.close_modeling_fold()

    def _twist_load_threshold(self):
        """从 optionVar 读取扭曲阈值，缺省 5.0"""
        try:
            if cmds.optionVar(exists=TWIST_THRESHOLD_OPTION):
                value = float(cmds.optionVar(query=TWIST_THRESHOLD_OPTION))
                return max(TWIST_MIN_THRESHOLD, min(TWIST_MAX_THRESHOLD, value))
        except Exception:
            pass
        return TWIST_DEFAULT_THRESHOLD

    def _twist_slider_changed(self, value, *args):
        self._twist_set_threshold(value, from_slider=True)

    def _twist_field_changed(self, value, *args):
        self._twist_set_threshold(value, from_slider=False)

    def _twist_field_wheel_changed(self):
        """滚轮已经改过输入框，这里把滑条与 optionVar 同步过去。"""
        value = cmds.floatField(self._twist_threshold_field, query=True, value=True)
        self._twist_set_threshold(value, from_slider=False)

    def _twist_set_threshold(self, value, from_slider=True):
        """滑条与输入框互相同步，并把阈值存回 optionVar"""
        try:
            value = max(TWIST_MIN_THRESHOLD, min(TWIST_MAX_THRESHOLD, float(value)))
            if from_slider:
                cmds.floatField(self._twist_threshold_field, edit=True, value=value)
            else:
                cmds.floatSlider(self._twist_threshold_slider, edit=True, value=value)
            cmds.optionVar(fv=(TWIST_THRESHOLD_OPTION, value))
        except Exception:
            pass

    def _twist_save_threshold(self):
        """把滑条当前值存回 optionVar"""
        try:
            value = cmds.floatSlider(self._twist_threshold_slider, query=True, value=True)
            cmds.optionVar(fv=(TWIST_THRESHOLD_OPTION, float(value)))
        except Exception:
            pass

    def _twist_numpy_missing(self):
        if np is None:
            lj_error(u"未找到 NumPy，扭曲面检查不可用（需 Maya 2022+）")
            return True
        return False

    def twist_check_clicked(self, *args):
        if self._twist_numpy_missing():
            return
        self._twist_save_threshold()
        threshold = cmds.floatSlider(self._twist_threshold_slider, query=True, value=True)
        try:
            result = twist_check_and_select(threshold)
            text = _twist_format_check_result(result, threshold)
            lj_info(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"扭曲检查失败：%s" % error)

    def twist_triangulate_clicked(self, *args):
        try:
            text = twist_triangulate_selection()
            if text.startswith(u"已对当前选择"):
                lj_info(text)
            else:
                lj_warn(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"三角化失败：%s" % error)

    def twist_spin_clicked(self, *args):
        if self._twist_numpy_missing():
            return
        self._twist_save_threshold()
        try:
            result = twist_spin_selected_edges()
            text = _twist_format_spin_result(result)
            if result.failed:
                lj_warn(text)
            else:
                lj_info(text)
        except Exception as error:
            _twist_traceback.print_exc()
            lj_error(u"Spin 失败：%s" % error)

    def create_quick_bottom_face_layout(self):
        """快速底面折叠栏：内嵌 SGCG Placement Tool 的候选底面生成与绿面吸附"""
        self.open_modeling_fold(u"快速底面")

        cmds.columnLayout()
        cmds.optionMenu(BF_MODE_MENU, label=u"候选模式：", width=280)
        cmds.menuItem(label=u"凸包(高模)")
        cmds.menuItem(label=u"六面安全(包围盒)")
        cmds.optionMenu(BF_MODE_MENU, e=True, select=1)
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"生成候选底面", command=lambda *args: self.bf_place_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"选中一个网格物体后点击，会在模型各侧面生成绿色候选面。\n"
                               u"再点任意一个绿色面片，模型就把那一面朝下、贴地并冻结变换。\n"
                               u"凸包(高模)适合非盒子状的高模；六面安全只用包围盒，永远有结果。")
        cmds.button(label=u"清除候选", command=lambda *args: self.bf_clear_clicked(),
                    width=136, height=28, bgc=self.inner_grey,
                    annotation=u"删除场景里的绿色候选面，并停止点选放置的监听。")
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.button(label=u"对齐两个候选面", command=lambda *args: self.bf_attach_clicked(),
                    width=275, height=28, bgc=self.inner_grey,
                    annotation=u"先选目标物体，再选要移动的物体，然后点击本按钮。\n"
                               u"两个物体会各生成一组绿色候选面：\n"
                               u"先点目标物体的面，再点待移动物体的面，即旋转+平移贴合到该面上。")
        cmds.setParent('..')
        self.close_modeling_fold()

    def bf_place_clicked(self, *args):
        try:
            bf_choose_bottom_face()
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：生成候选面失败：%s" % error)

    def bf_clear_clicked(self, *args):
        try:
            bf_clear_choices()
            lj_info(u"快速底面：已清除候选面")
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：清除候选面失败：%s" % error)

    def bf_attach_clicked(self, *args):
        try:
            bf_attach_by_faces()
        except Exception as error:
            _bf_traceback.print_exc()
            lj_error(u"快速底面：生成对齐候选面失败：%s" % error)

    # ------------------------------------------------------------------
    # 边重建（内嵌自 ResampleMesh_Maya.py）
    # ------------------------------------------------------------------
    def create_edge_rebuild_layout(self):
        """边重建折叠栏：实时预览在最外层，五个子折叠栏放各运算符。"""
        self._rm_preview_cache = None
        self._rm_two_chain_key = None
        self._rm_two_chain_current = []
        self._rm_two_chain_info = []

        fold = self.open_modeling_fold(u"边重建")
        cmds.frameLayout(fold, edit=True, expandCommand=self._wheel_attach_all)

        self._rm_preview_cb = _ui_short_name(cmds.checkBox(
            label=u"实时预览", value=False, align="left", width=255,
            changeCommand=self._rm_preview_toggled,
            annotation=u"勾选后先缓存当前网格的原始点位：修改参数时先还原再重新计算。\n"
                       u"只对改点位的操作生效；重采样预览固定为只调点位、不改点数/拓扑。"))

        self._rm_build_resample()
        self._rm_build_smooth()
        self._rm_build_circularize()
        self._rm_build_unknot()
        self._rm_build_bridge()

        self.close_modeling_fold()

        # 建 UI 时 workspaceControl 还没显示，控件未必有 Qt 实件；等界面出来再挂滚轮
        cmds.evalDeferred(self._wheel_attach_all, lowestPriority=True)

    def _rm_open_fold(self, label):
        """边重建的子折叠栏：比外层窄一圈，配色与建模区其它折叠栏一致。"""
        frame = cmds.frameLayout(label=label, width=277,
                                 bgc=self.grey, collapsable=True, collapse=True,
                                 marginWidth=8, marginHeight=4,
                                 expandCommand=self._wheel_attach_all)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=3)
        return frame

    def _rm_close_fold(self, frame):
        cmds.setParent('..')
        cmds.setParent('..')
        # 以 collapse=True 创建时 Maya 不计算子控件高度，先展开算一次再折回
        cmds.frameLayout(frame, edit=True, collapse=False)
        cmds.frameLayout(frame, edit=True, collapse=True)

    def _rm_open_fields(self):
        # 新版 Maya 的 rowSpacing/columnSpacing 要求 [序号, 间距] 成对；旧版也接受
        cmds.rowColumnLayout(numberOfColumns=2,
                             columnWidth=[(1, 92), (2, 158)],
                             columnAttach=[(1, "right", 4), (2, "left", 4)],
                             rowSpacing=[(1, 3), (2, 3), (3, 3), (4, 3)],
                             columnSpacing=[(2, 4)])

    def _rm_number_field(self, integer, step, callback, **kwargs):
        """数值框：悬停滚轮按 step 调节，和 ResampleMesh_Maya 的 QSpinBox 一致。"""
        factory = cmds.intField if integer else cmds.floatField
        if callback is not None:
            kwargs["changeCommand"] = lambda *_: callback()
        kwargs.setdefault("width", 158)
        low = kwargs.get("minValue")
        high = kwargs.get("maxValue")
        precision = kwargs.get("precision", 3)
        control = factory(step=step, **kwargs)
        short = _ui_short_name(control)
        self._wheel_register(short, integer, step, callback, low, high, precision)
        return short

    def _wheel_register(self, control, integer, step, callback=None,
                        low=None, high=None, precision=3, bump=None):
        """登记悬停滚轮步进；bump 用于 intSliderGrp 这类要整体改值的控件。"""
        # 重挂到选项卡/弹出窗口后全路径失效，统一存短名
        control = _ui_short_name(control)
        spec = (control, integer, step, callback, low, high, precision, bump)
        self._wheel_specs.append(spec)
        if self._attach_wheel(*spec):
            self._wheel_attached.add(control)
        return control

    def _wheel_apply_step(self, control, step, sign, callback, integer, low, high, precision):
        factory = cmds.intField if integer else cmds.floatField
        value = factory(control, query=True, value=True) + sign * step
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        if integer:
            value = int(round(value))
        else:
            value = round(value, precision)
        factory(control, edit=True, value=value)
        if callback is not None:
            callback()

    def _find_ui_widget(self, control):
        """按控件名找 Qt 实件。

        先试全路径（未重挂时最快）；重挂到选项卡 / 弹出独立窗口后全路径失效，
        改按短名遍历顶层窗口找 objectName（Maya 把控件短名写入 objectName）。
        """
        try:
            pointer = omui.MQtUtil.findControl(control)
            if pointer:
                return wrapInstance(int(pointer), QtWidgets.QWidget)
        except Exception:
            pass
        name = _ui_short_name(control)
        app = QtWidgets.QApplication.instance()
        if app is None:
            return None
        for top in app.topLevelWidgets():
            try:
                widget = top.findChild(QtWidgets.QWidget, name)
            except RuntimeError:
                continue
            if widget is not None:
                return widget
        return None

    def _attach_wheel(self, control, integer, step, callback, low, high, precision, bump=None):
        """悬停滚轮改值：上滚 +step、下滚 -step，和 Qt SpinBox 的手感一致。

        返回是否挂上；控件还没有 Qt 实件时返回 False，交给 _wheel_attach_all 重试。
        """
        try:
            widget = self._find_ui_widget(control)
            if widget is None:
                return False
            binder = _WheelStepper(
                widget,
                lambda delta: self._wheel_step(
                    control, integer, step, callback, low, high, precision, bump, delta))
            self._wheel_binders.append(binder)
            targets = [widget] + widget.findChildren(QtWidgets.QWidget)
            for target in targets:
                target.installEventFilter(binder)
            return True
        except Exception:
            # Qt 侧不可用时退回到 Ctrl+拖拽，不影响 UI 构建
            return False

    def _wheel_attach_all(self, *args):
        """界面显示后再挂一次，并重试几轮：滚动布局/折叠栏可能延后建出 Qt 实件。"""
        self._wheel_attach_pass(0)

    def _wheel_attach_pass(self, attempt):
        pending = False
        for spec in self._wheel_specs:
            if spec[0] in self._wheel_attached:
                continue
            if self._attach_wheel(*spec):
                self._wheel_attached.add(spec[0])
            else:
                pending = True
        if pending and attempt < 5:
            cmds.evalDeferred(lambda: self._wheel_attach_pass(attempt + 1),
                              lowestPriority=True)

    def _wheel_step(self, control, integer, step, callback, low, high, precision, bump, delta):
        if not cmds.control(control, query=True, enable=True):
            return
        sign = 1 if delta > 0 else -1
        if bump is not None:
            bump(sign)
            return
        self._wheel_apply_step(control, step, sign, callback, integer, low, high, precision)

    def _rm_set_enabled(self, control, enabled):
        cmds.control(control, edit=True, enable=enabled)

    def _rm_check(self, control):
        return bool(cmds.checkBox(control, query=True, value=True))

    def _rm_spline_value(self):
        return cmds.optionMenu(self._rm_spline, query=True, value=True)

    def _rm_numpy_missing(self):
        if np is None:
            lj_error(u"未找到 NumPy，边重建不可用（需 Maya 2022+）")
            return True
        return False

    def _rm_build_resample(self):
        frame = self._rm_open_fold(u"重采样")

        self._rm_open_fields()
        cmds.text(label=u"采样类型", align="right")
        self._rm_spline = _ui_short_name(cmds.optionMenu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("resample")))
        for name in SPLINE_TYPES:
            cmds.menuItem(label=name)
        cmds.optionMenu(self._rm_spline, edit=True, select=1)

        cmds.text(label=u"增减点数", align="right")
        self._rm_step = self._rm_number_field(
            True, 1, lambda: self._rm_preview_apply("resample"),
            minValue=-1000, maxValue=1000, value=4,
            annotation=u">0 加点，<0 减点，0 只重排点位")
        cmds.text(label=u"多项式参数化", align="right")
        self._rm_p_tension = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("resample"),
            minValue=0.15, maxValue=0.5, value=0.15, precision=3,
            annotation=u"POLYNOMIAL 的弦长指数（原版 Ctrl+滚轮）")
        cmds.setParent('..')

        self._rm_space = _ui_short_name(cmds.checkBox(
            label=u"按弧长均匀分布", value=False, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("resample"),
            annotation=u"按弧长重新分布采样点（原版 T 键）"))
        self._rm_auto_clean = _ui_short_name(cmds.checkBox(
            label=u"自动清理共线边", value=False, align="left", width=255,
            annotation=u"重连后溶解共面的新边（原版 Shift+C）"))

        self._rm_two_chain = _ui_short_name(cmds.checkBox(
            label=u"双链分别设点数", value=False, align="left", width=255,
            changeCommand=self._rm_two_chain_toggled,
            annotation=u"正好选中两条开放边链时，分别指定各自的目标点数。\n"
                       u"两条链点数相同时可把不对称的一对补齐。"))

        self._rm_open_fields()
        cmds.text(label=u"链 1 点数", align="right")
        self._rm_chain_a_count = self._rm_number_field(
            True, 1, self._rm_update_chain_labels,
            minValue=3, maxValue=1000, value=3)
        cmds.text(label=u"链 2 点数", align="right")
        self._rm_chain_b_count = self._rm_number_field(
            True, 1, self._rm_update_chain_labels,
            minValue=3, maxValue=1000, value=3)
        cmds.setParent('..')

        self._rm_chain_a_label = _ui_short_name(cmds.text(label=u"链 1：未启用", align="left", width=255))
        self._rm_chain_b_label = _ui_short_name(cmds.text(label=u"链 2：未启用", align="left", width=255))

        cmds.rowLayout(numberOfColumns=2)
        self._rm_read_two = _ui_short_name(cmds.button(
            label=u"读取两链", command=lambda *args: self._rm_read_two_clicked(),
            width=126, height=26, bgc=self.inner_grey,
            annotation=u"读取当前选中的两条开放边链，并填入各自现有点数"))
        self._rm_equal_two = _ui_short_name(cmds.button(
            label=u"两链同点数", command=lambda *args: self._rm_equalize_counts(),
            width=126, height=26, bgc=self.inner_grey,
            annotation=u"把链 2 的目标点数设为与链 1 相同"))
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"重采样", command=lambda *args: self._rm_run("resample"),
                    width=126, height=28, bgc=self.inner_grey,
                    annotation=u"按当前样条与点数重采样选中的开放边链")
        cmds.button(label=u"均匀分布", command=lambda *args: self._rm_run("distribute"),
                    width=126, height=28, bgc=self.inner_grey,
                    annotation=u"Akima + 弧长均匀分布，只改点位不动拓扑（原版 D 键）")
        cmds.setParent('..')

        self._rm_two_chain_toggled()
        self._rm_close_fold(frame)

    def _rm_build_smooth(self):
        frame = self._rm_open_fold(u"平滑")

        self._rm_open_fields()
        cmds.text(label=u"迭代次数", align="right")
        self._rm_smooth_iterations = self._rm_number_field(
            True, 1, lambda: self._rm_preview_apply("smooth"),
            minValue=1, maxValue=50, value=1)
        cmds.text(label=u"强度", align="right")
        self._rm_smooth_factor = self._rm_number_field(
            False, 0.05, lambda: self._rm_preview_apply("smooth"),
            minValue=0.001, maxValue=1.0, value=0.5, precision=3)
        cmds.text(label=u"权重", align="right")
        self._rm_smooth_weight = _ui_short_name(cmds.optionMenu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("smooth")))
        cmds.menuItem(label="distance")
        cmds.menuItem(label="uniform")
        cmds.optionMenu(self._rm_smooth_weight, edit=True, select=1)
        cmds.text(label=u"类型", align="right")
        self._rm_smooth_type = _ui_short_name(cmds.optionMenu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("smooth")))
        cmds.menuItem(label="SMOOTH")
        cmds.menuItem(label="SHARP")
        cmds.optionMenu(self._rm_smooth_type, edit=True, select=1)
        cmds.setParent('..')

        cmds.button(label=u"平滑", command=lambda *args: self._rm_run("smooth"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"SMOOTH = Laplacian 平滑，SHARP = Taubin 先平滑再反向锐化")
        self._rm_close_fold(frame)

    def _rm_build_circularize(self):
        frame = self._rm_open_fold(u"圆化")

        self._rm_open_fields()
        cmds.text(label=u"角度", align="right")
        self._rm_arc_angle = self._rm_number_field(
            False, 5.0, lambda: self._rm_preview_apply("circularize"),
            minValue=0.1, maxValue=359.9, value=90.0, precision=2)
        cmds.setParent('..')

        self._rm_arc_flip = _ui_short_name(cmds.checkBox(
            label=u"反向", value=False, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("circularize")))

        cmds.button(label=u"圆化", command=lambda *args: self._rm_run("circularize"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"把选中的开放边链拟合成圆弧，链的两个端点保持不动")
        self._rm_close_fold(frame)

    def _rm_build_unknot(self):
        frame = self._rm_open_fold(u"解结")

        self._rm_open_fields()
        cmds.text(label=u"宽度", align="right")
        self._rm_unknot_width = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("unknot"),
            minValue=-1000.0, maxValue=1000.0, value=0.0, precision=4)
        cmds.text(label=u"张力", align="right")
        self._rm_unknot_tension = self._rm_number_field(
            False, 0.01, lambda: self._rm_preview_apply("unknot"),
            minValue=-1.0, maxValue=1.0, value=0.5, precision=3)
        cmds.text(label=u"采样类型", align="right")
        self._rm_unknot_spline = _ui_short_name(cmds.optionMenu(
            width=158, changeCommand=lambda *_: self._rm_preview_apply("unknot")))
        for name in SPLINE_TYPES:
            cmds.menuItem(label=name)
        cmds.optionMenu(self._rm_unknot_spline, edit=True, select=1)
        cmds.setParent('..')

        self._rm_unknot_uniform = _ui_short_name(cmds.checkBox(
            label=u"均匀", value=True, align="left", width=255,
            changeCommand=lambda *_: self._rm_preview_apply("unknot"),
            annotation=u"按弧长均匀分布内部点"))

        cmds.button(label=u"解结", command=lambda *args: self._rm_run("unknot"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"用两端切线引导中间点，缓解打结和扭曲")
        self._rm_close_fold(frame)

    def _rm_build_bridge(self):
        frame = self._rm_open_fold(u"桥接")

        self._rm_open_fields()
        cmds.text(label=u"匹配模式", align="right")
        self._rm_bridge_mode = _ui_short_name(cmds.optionMenu(width=158))
        cmds.menuItem(label="shortest")
        cmds.menuItem(label="basic")
        cmds.optionMenu(self._rm_bridge_mode, edit=True, select=1)
        cmds.text(label=u"扭转", align="right")
        self._rm_bridge_twist = self._rm_number_field(
            True, 1, None, minValue=-1000, maxValue=1000, value=0)
        cmds.setParent('..')

        self._rm_bridge_connect = _ui_short_name(cmds.checkBox(
            label=u"仅生成连接边", value=True, align="left", width=255,
            annotation=u"勾选：只生成连接边（原版 C 键）；\n"
                       u"取消：用 polyBridgeEdge 生成桥接面（要求边循环是开放边界）"))
        self._rm_bridge_reverse = _ui_short_name(cmds.checkBox(
            label=u"反转", value=False, align="left", width=255))

        cmds.button(label=u"桥接", command=lambda *args: self._rm_run("bridge"),
                    width=255, height=28, bgc=self.inner_grey,
                    annotation=u"在两个或多个边循环之间桥接")
        self._rm_close_fold(frame)

    # -- 实时预览 --------------------------------------------------------

    def _rm_preview_toggled(self, *args):
        if not self._rm_check(self._rm_preview_cb):
            self._rm_preview_cache = None
            lj_info(u"实时预览已关闭，当前结果保留")
            return
        self._rm_capture_preview()

    def _rm_capture_preview(self, report=True):
        try:
            transform, shape = active_mesh()
        except MeshError as exc:
            if report:
                lj_warn(str(exc))
            return False
        self._rm_preview_cache = (shape, read_points(shape))
        if report:
            lj_info(u"已缓存原始点位：%s" % transform)
        return True

    def _rm_preview_apply(self, kind):
        if not self._rm_check(self._rm_preview_cb):
            return
        if self._rm_numpy_missing():
            return
        if self._rm_preview_cache is None and not self._rm_capture_preview():
            return
        try:
            _, current = active_mesh()
        except MeshError as exc:
            lj_warn(str(exc))
            return
        shape, points = self._rm_preview_cache
        if current != shape or not cmds.objExists(shape):
            if not self._rm_capture_preview():
                return
            shape, points = self._rm_preview_cache
        try:
            with undo_chunk(u"边重建预览"):
                write_points(shape, points)
                self._rm_apply(kind, preview=True)
        except MeshError as exc:
            lj_warn(str(exc))
        except Exception as exc:
            _rm_traceback.print_exc()
            lj_error(u"边重建预览失败：%s" % exc)

    def _rm_after_apply(self):
        if self._rm_check(self._rm_preview_cb):
            self._rm_capture_preview(report=False)

    # -- 运算符调度 ------------------------------------------------------

    def _rm_apply(self, kind, preview=False):
        if kind == "resample":
            self._rm_resample(preview)
        elif kind == "distribute":
            self._rm_distribute(preview)
        elif kind == "smooth":
            self._rm_smooth(preview)
        elif kind == "circularize":
            self._rm_circularize(preview)
        elif kind == "unknot":
            self._rm_unknot(preview)
        elif kind == "bridge" and not preview:
            self._rm_bridge(preview)

    def _rm_run(self, kind):
        if self._rm_numpy_missing():
            return
        try:
            self._rm_apply(kind)
        except MeshError as exc:
            lj_error(u"边重建：%s" % exc)
        except Exception as exc:
            _rm_traceback.print_exc()
            lj_error(u"边重建：%s" % exc)

    def _rm_resample(self, preview=False):
        if not preview and self._rm_check(self._rm_two_chain):
            if not self._rm_two_chain_valid_for_apply():
                return
            counts = [cmds.intField(self._rm_chain_a_count, query=True, value=True),
                      cmds.intField(self._rm_chain_b_count, query=True, value=True)]
            chain_count = resample(
                step=0,
                spline_type=self._rm_spline_value(),
                space=self._rm_check(self._rm_space),
                p_tension=cmds.floatField(self._rm_p_tension, query=True, value=True),
                auto_clean=self._rm_check(self._rm_auto_clean),
                counts=counts)
            self._rm_sync_chain_counts(report=False)
            lj_info(u"已按两链点数重采样 %d 条链：链 1 %d 点，链 2 %d 点。"
                    % (chain_count, counts[0], counts[1]))
            self._rm_after_apply()
            return
        count = resample(
            step=0 if preview else int(cmds.intField(self._rm_step, query=True, value=True)),
            spline_type=self._rm_spline_value(),
            space=self._rm_check(self._rm_space),
            p_tension=cmds.floatField(self._rm_p_tension, query=True, value=True),
            auto_clean=self._rm_check(self._rm_auto_clean),
            positions_only=preview)
        if not preview:
            lj_info(u"已重采样 %d 条边链。" % count)
            self._rm_after_apply()

    def _rm_distribute(self, preview=False):
        count = distribute()
        if not preview:
            lj_info(u"已均匀分布 %d 条边链（拓扑未改动）。" % count)
            self._rm_after_apply()

    def _rm_smooth(self, preview=False):
        count = smooth(
            iterations=int(cmds.intField(self._rm_smooth_iterations, query=True, value=True)),
            factor=cmds.floatField(self._rm_smooth_factor, query=True, value=True),
            weight_type=cmds.optionMenu(self._rm_smooth_weight, query=True, value=True),
            smooth_type=cmds.optionMenu(self._rm_smooth_type, query=True, value=True))
        if not preview:
            lj_info(u"已平滑 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_circularize(self, preview=False):
        count = circularize(
            angle=cmds.floatField(self._rm_arc_angle, query=True, value=True),
            flip=self._rm_check(self._rm_arc_flip))
        if not preview:
            lj_info(u"已圆化 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_unknot(self, preview=False):
        count = unknot(
            width=cmds.floatField(self._rm_unknot_width, query=True, value=True),
            tension=cmds.floatField(self._rm_unknot_tension, query=True, value=True),
            spline_type=cmds.optionMenu(self._rm_unknot_spline, query=True, value=True),
            uniform=self._rm_check(self._rm_unknot_uniform))
        if not preview:
            lj_info(u"已解结 %d 个顶点。" % count)
            self._rm_after_apply()

    def _rm_bridge(self, preview=False):
        count = bridge(
            connect=self._rm_check(self._rm_bridge_connect),
            mode=cmds.optionMenu(self._rm_bridge_mode, query=True, value=True),
            reverse=self._rm_check(self._rm_bridge_reverse),
            twist=int(cmds.intField(self._rm_bridge_twist, query=True, value=True)))
        lj_info(u"已桥接 %d 个循环。" % count)

    # -- 双链点数 --------------------------------------------------------

    def _rm_two_chain_toggled(self, *args):
        enabled = self._rm_check(self._rm_two_chain)
        for control in (self._rm_chain_a_count, self._rm_chain_b_count,
                        self._rm_read_two, self._rm_equal_two,
                        self._rm_chain_a_label, self._rm_chain_b_label):
            self._rm_set_enabled(control, enabled)
        self._rm_set_enabled(self._rm_step, not enabled)
        if not enabled:
            self._rm_two_chain_key = None
            self._rm_two_chain_current = []
            self._rm_two_chain_info = []
            self._rm_set_chain_labels(u"链 1：未启用", u"链 2：未启用")
            return
        self._rm_sync_chain_counts()

    def _rm_set_chain_labels(self, first, second):
        cmds.text(self._rm_chain_a_label, edit=True, label=first)
        cmds.text(self._rm_chain_b_label, edit=True, label=second)

    def _rm_sync_chain_counts(self, report=True):
        self._rm_two_chain_key = None
        self._rm_two_chain_current = []
        self._rm_two_chain_info = []
        try:
            info = chain_info()
        except MeshError as exc:
            self._rm_set_chain_labels(u"链 1：未读取", u"链 2：未读取")
            if report:
                lj_warn(str(exc))
            return False
        if len(info) != 2:
            self._rm_set_chain_labels(u"链 1：未读取", u"链 2：未读取")
            if report:
                lj_warn(u"双链点数需要正好选中 2 条开放边链（当前 %d 条）。" % len(info))
            return False
        self._rm_two_chain_key = [(item["start_vertex"], item["end_vertex"])
                                  for item in info]
        self._rm_two_chain_current = [item["count"] for item in info]
        self._rm_two_chain_info = info
        for control, item in zip((self._rm_chain_a_count, self._rm_chain_b_count), info):
            cmds.intField(control, edit=True, value=item["count"])
        self._rm_update_chain_labels()
        if report:
            lj_info(u"已读取两链：链 1 %d 点，链 2 %d 点。"
                    % (info[0]["count"], info[1]["count"]))
        return True

    def _rm_update_chain_labels(self):
        for index, (label, field) in enumerate(
                zip((self._rm_chain_a_label, self._rm_chain_b_label),
                    (self._rm_chain_a_count, self._rm_chain_b_count))):
            if index >= len(self._rm_two_chain_current):
                cmds.text(label, edit=True,
                          label=u"链 %d：未读取（点「读取两链」）" % (index + 1))
                continue
            current = self._rm_two_chain_current[index]
            target = int(cmds.intField(field, query=True, value=True))
            cmds.text(label, edit=True,
                      label=u"链 %d：当前 %d 点 / %d 段 → %d 点 / %d 段"
                            % (index + 1, current, max(0, current - 1),
                               target, max(0, target - 1)))

    def _rm_read_two_clicked(self):
        self._rm_sync_chain_counts()

    def _rm_equalize_counts(self):
        value = cmds.intField(self._rm_chain_a_count, query=True, value=True)
        cmds.intField(self._rm_chain_b_count, edit=True, value=value)
        self._rm_update_chain_labels()

    def _rm_two_chain_valid_for_apply(self):
        if self._rm_two_chain_key is None:
            lj_warn(u"请先点「读取两链」同步当前选择。")
            return False
        try:
            info = chain_info()
        except MeshError as exc:
            lj_warn(str(exc))
            return False
        if len(info) != 2:
            lj_warn(u"双链点数需要正好选中 2 条开放边链（当前 %d 条）。" % len(info))
            return False
        key = [(item["start_vertex"], item["end_vertex"]) for item in info]
        if key != self._rm_two_chain_key:
            lj_warn(u"选择已变化，请重新点「读取两链」后再应用。")
            return False
        self._rm_two_chain_current = [item["count"] for item in info]
        self._rm_two_chain_info = info
        self._rm_update_chain_labels()
        return True

    def create_smart_combine_separate_layout(self):
        self.open_modeling_fold(u"智能合并分离")

        self.create_combine_separate_buttons()

        self.close_modeling_fold()

    def create_ring_split_layout(self):
        self.open_modeling_fold(u"Ring批量分割")

        self.create_ring_split_buttons()

        self.close_modeling_fold()

    def create_ring_split_buttons(self):
        cmds.columnLayout()
        cmds.optionMenu(RING_UI_MODE, label=u"模式：", width=280)
        cmds.menuItem(label=u"仅处理选中单条边(单四边面对边)")
        cmds.menuItem(label=u"整条Ring分割(等同maya自带工具)")
        cmds.optionMenu(RING_UI_MODE, e=True, select=1)
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.checkBox(RING_UI_EDGE_FLOW, label=u"Insert With EdgeFlow 开启边流", v=0)
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.button(label=u"执行分割", command=ring_batch_split,
                    width=275, height=28, bgc=self.inner_grey)
        cmds.setParent('..')

    def create_pivots_settings_layout(self):
        self.open_modeling_fold(u"坐标轴")

        self.create_pivot_buttons()

        cmds.text(label=" ", height=4)

        self.create_pivot_boundary_layout()

        self.close_modeling_fold()

    def create_select_by_angle_layout(self):
        cmds.frameLayout(label=u"按角度选面", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True,
                         marginWidth=0, marginHeight=5,
                         expandCommand=self._wheel_attach_all)
        cmds.columnLayout()

        self.create_select_by_angle_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_select_similar_layout(self):
        cmds.frameLayout(label=u"选择相似物体", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=10, marginHeight=5)
        cmds.columnLayout()

        self.create_select_similar_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    """
        ------------------------------------ Buttons ------------------------------------
    """

    def create_select_similar_buttons(self):
        cmds.columnLayout()
        cmds.button(label=u"选择相似物体", command=lambda *args: self.select_similar_mesh(mode=self.mode),
                    width=275, height=25, bgc=self.inner_grey)
        cmds.setParent('..')

        cmds.columnLayout()
        self.mode = 0
        self.separate_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(120, 50, 50),
                                                      label=u"忽略大小：",
                                                      labelArray2=["是", "否"],
                                                      select=1,
                                                      on1=lambda *args: self.set_mode(0),
                                                      on2=lambda *args: self.set_mode(1),
                                                      vertical=False,
                                                      width=300)
        cmds.setParent('..')

    def create_select_by_angle_buttons(self):
        cmds.rowLayout(numberOfColumns=2)
        # 用短名引用：pane 重挂到选项卡后创建时返回的全路径会失效
        angle_slider = "LJGT_AngleByNormalsSlider"
        cmds.intSliderGrp(
            angle_slider,
            label=u"角度", field=True, columnWidth3=(50, 30, 60),
            minValue=0, maxValue=180, fieldMinValue=0,
            value=25, width=250,
            cc=lambda *args: self.update_angle(
                cmds.intSliderGrp(angle_slider, q=True, value=True)))
        self._wheel_register(
            angle_slider + "Field", integer=True, step=1,
            callback=lambda: self.update_angle(
                cmds.intSliderGrp(angle_slider, q=True, value=True)),
            low=0, high=180,
            bump=lambda sign: self._angle_wheel_bump(angle_slider, sign))

        cmds.button(label=u"选择",
                    bgc=self.inner_grey,
                    command=lambda *args: [self.update_initial_faces(), self.grow_selection_by_normal_angle(
                        cmds.intSliderGrp(angle_slider, q=True, value=True))])
        cmds.setParent('..')

    def _angle_wheel_bump(self, group, sign):
        value = cmds.intSliderGrp(group, query=True, value=True) + sign
        value = max(0, min(180, value))
        cmds.intSliderGrp(group, edit=True, value=value)
        self.update_angle(value)

    def create_selection_buttons(self):
        cmds.columnLayout()

        cmds.button(label=u"隔段选线", command=self.select_every_N_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        cmds.button(label=u"选区选择", command=lambda *args: self.FILL_SELECTION.fill_selection(),
                    width=300, height=28, bgc=self.grey,
                    annotation=u"用选中的线划分区域")

        cmds.text(label=" ", height=2)

        cmds.button(label=u"选硬边", command=self.get_hard_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        cmds.button(label=u"测试", command=self.test,
                    width=300, height=28, bgc=self.grey)

        cmds.setParent('..')
    
    def create_naming_buttons(self):
        cmds.columnLayout()

        cmds.button(label=u"自动命名", command=self.show_naming_window,
                    width=300, height=28, bgc=self.grey)

        cmds.setParent('..')

    def set_mode(self, value):
        self.mode = value

    def create_combine_separate_buttons(self):
        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"智能合并", command=lambda *args: self.smart_clean_combine(weld=self.weld_vertices),
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"能自动合并近的片")
        cmds.button(label=u"智能分离", command=lambda *args: self.smart_clean_separate(keepOriginal=self.keep_original),
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"选面：走 Maya 原生 Extract 分离；选物体：按壳分离")
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2, columnWidth2=(128, 128),
                       columnAttach2=("left", "left"), columnOffset2=(2, 8))
        self.weld_vertices = True
        self.weld_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(50, 38, 38),
                                                  label=u"焊接点",
                                                  labelArray2=['是', "否"],
                                                  select=1,
                                                  on1=lambda *args: self.set_weld_vertices(True),
                                                  on2=lambda *args: self.set_weld_vertices(False),
                                                  vertical=False,
                                                  width=126)
        self.keep_original = False
        self.separate_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(50, 38, 38),
                                                      label=u"复制面",
                                                      labelArray2=['是', "否"],
                                                      select=2,
                                                      on1=lambda *args: self.set_keep_original(True),
                                                      on2=lambda *args: self.set_keep_original(False),
                                                      vertical=False,
                                                      width=126)
        cmds.setParent('..')

    def set_keep_original(self, value):
        self.keep_original = value

    def set_weld_vertices(self, value):
        self.weld_vertices = value

    def create_modeling_buttons(self):
        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2)

        cmds.button(label=u"转角改线", command=self.corner_killer,
                    width=149, height=28, bgc=self.grey,
                    annotation=u"更改转角布线，上下面如果有需要一起选中")

        cmds.button(label=u"选中元素加环线", command=lambda *args: self.split_around(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"选中点线面都可以以选中元素为中心加环线")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"智能插入 I", command=lambda *args: self.ROUND_INSET.roundInset(),
                    width=149, height=28, bgc=self.grey)

        cmds.button(label=u"智能插入 II", command=lambda *args: self.insert_face(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"能单面片插入")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"顶点法线对齐", command=lambda *args: self.match_vertex_normal(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"顶点法线对齐到选中的点法线方向")

        cmds.button(label=u"吸附平面", command=lambda *args: self.SNAP_2_PLANE.create_plane_ui(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"吸附到自定义三点确定的平面")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"环线随意滑移", command=lambda *args: self.polyEdgeShift(),
            width=149, height=28, bgc=self.grey,
            annotation=u"增加的环线随意旋转滑移，不影响外轮廓")
        cmds.button(label=u"法线权重置平", command=lambda *args: self.weight_normal(),
            width=149, height=28, bgc=self.grey,
            annotation=u"法线权重（Smart Outside）：按容差角度把平滑法线推给外侧相邻面")
        
        cmds.setParent('..')

        cmds.text(label=" ", height=10)
        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"拖拽吸附", command=lambda *args: self.INSTANT_DRAG.instantDrag(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"Ctrl = 5°旋转\n"
                               u"Shift = 复制\n"
                               u"Shift + Ctrl = 对齐到最近边方向")

        cmds.button(label=u"沿最近边对齐", command=lambda *args: self.ALIGN_EDGE.alignEdge(),
                    width=149, height=28, bgc=self.grey)

        cmds.setParent('..')

    def create_pivot_buttons(self):
        cmds.rowLayout(numberOfColumns=3, )

        cmds.button(label=u"冻结", command=self.freeze_pivots,
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标系归0")

        cmds.button(label=u"居中", command="cmds.CenterPivot()",
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标移动到物体中心")

        cmds.button(label=u"重置", command=self.reset_pivots,
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标重置到世界中心")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2, )

        cmds.button(label=u"置底", command=self.move_pivot_to_bottom_center,
                    width=136, height=20, bgc=self.inner_grey,
                    annotation=u"坐标移动到物体底部中心")

        cmds.button(label=u"物体至世界中心", command=self.snap_to_world_center_bottom,
                    width=136, height=20, bgc=self.inner_grey,
                    annotation=u"物体底部为底移动")
        
        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=1, )

        cmds.button(label=u"匹配坐标轴", command=self.match_axis_to_first,
                    width=275, height=20, bgc=self.inner_grey,
                    annotation=u"后选物体的坐标轴原点搬到第一个选中物体的 pivot 位置，物体本身不移动")

        cmds.setParent('..')

    def freeze_pivots(self, *args):
        """冻结选中物体的变换，无选择/失败时用视口提示反馈"""
        if not cmds.ls(selection=True):
            lj_warn(u"冻结失败：请先选择物体")
            return
        try:
            cmds.FreezeTransformations()
            lj_info(u"冻结完成")
        except Exception as error:
            lj_error(u"冻结失败：%s" % error)

    def reset_pivots(self, *args):
        """重置选中物体的变换，无选择/失败时用视口提示反馈"""
        if not cmds.ls(selection=True):
            lj_warn(u"重置失败：请先选择物体")
            return
        try:
            cmds.ResetTransformations()
            lj_info(u"重置完成")
        except Exception as error:
            lj_error(u"重置失败：%s" % error)

    def create_pivot_boundary_layout(self):
        """创建嵌入主窗口的物体边界对齐可折叠标签，使用 formLayout 精确定位按钮"""
        # 子 frame：物体边界对齐
        cmds.frameLayout(label=u"物体边界对齐", width=280,
                         bgc=self.inner_grey, collapsable=True, collapse=True,
                         marginWidth=4, marginHeight=4)

        # formLayout 参数（和原弹窗一致的布局逻辑，但尺寸适配主窗口）
        margin = 4
        gap = 4
        arrow_w, arrow_h = 56, 36
        plane_w, plane_h, plane_gap = 46, 26, 4

        form = cmds.formLayout()

        # 创建所有按钮
        btn_up = cmds.button(label=u"↑", width=arrow_w, height=arrow_h,
                             command=lambda *_: self.pivot_move_in_plane("up"),
                             bgc=self.grey_blue)
        btn_down = cmds.button(label=u"↓", width=arrow_w, height=arrow_h,
                               command=lambda *_: self.pivot_move_in_plane("down"),
                               bgc=self.grey_blue)
        btn_left = cmds.button(label=u"←", width=arrow_w, height=arrow_h,
                               command=lambda *_: self.pivot_move_in_plane("left"),
                               bgc=self.grey_blue)
        btn_right = cmds.button(label=u"→", width=arrow_w, height=arrow_h,
                                command=lambda *_: self.pivot_move_in_plane("right"),
                                bgc=self.grey_blue)

        # 三个平面按钮，保存UI路径供 pivot_set_plane 改色
        btn_xy = cmds.button(label="XY", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("xy"),
                             bgc=self.PIVOT_PLANE_COLORS['xy'][1])
        btn_zy = cmds.button(label="ZY", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("zy"),
                             bgc=self.PIVOT_PLANE_COLORS['zy'][1])
        btn_zx = cmds.button(label="ZX", width=plane_w, height=plane_h,
                             command=lambda *_: self.pivot_set_plane("zx"),
                             bgc=self.PIVOT_PLANE_COLORS['zx'][1])

        # 记录平面按钮名
        self._pivot_plane_btns = {
            'xy': _ui_short_name(btn_xy),
            'zy': _ui_short_name(btn_zy),
            'zx': _ui_short_name(btn_zx),
        }

        # 计算布局位置
        row_up = margin
        row_mid = row_up + arrow_h + 6
        mid_h = plane_h * 3 + plane_gap * 2
        row_down = row_mid + mid_h + 6
        # 各按钮按自身宽度相对 50% 居中，保证上下箭头与平面按钮垂直对齐
        arrow_offset = -(arrow_w // 2)
        plane_offset = -(plane_w // 2)
        side_top = row_mid + (mid_h - arrow_h) // 2  # 左右箭头与中间列垂直居中

        cmds.formLayout(form, edit=True,
            attachForm=[
                (btn_up, "top", row_up),
                (btn_left, "top", side_top),
                (btn_left, "left", margin),
                (btn_right, "top", side_top),
                (btn_right, "right", margin),
                (btn_xy, "top", row_mid),
                (btn_zy, "top", row_mid + plane_h + plane_gap),
                (btn_zx, "top", row_mid + (plane_h + plane_gap) * 2),
                (btn_down, "top", row_down),
            ],
            attachPosition=[
                (btn_up, "left", arrow_offset, 50),
                (btn_xy, "left", plane_offset, 50),
                (btn_zy, "left", plane_offset, 50),
                (btn_zx, "left", plane_offset, 50),
                (btn_down, "left", arrow_offset, 50),
            ]
        )

        # 设默认平面为 XY
        self.pivot_set_plane("xy")

        cmds.setParent('..')  # 退回 frameLayout
        cmds.setParent('..')  # 退回建模 columnLayout

    """
         ------------------------------------ Functions ------------------------------------
    """

    # --------------------- combine & separate
    def smart_clean_combine(self, weld=True, *args):

        selection = cmds.ls(selection=True, type='transform')

        if not len(selection) > 1:
            lj_warn(u"选择两个以上的物体!!!")
            return

        current_ctx = cmds.currentCtx()
        objects = cmds.ls(os=True, l=True, tr=True)
        last_obj = cmds.ls(os=True, l=True, tail=1, tr=True)
        group_parent = cmds.listRelatives(last_obj, f=True, p=True)
        source_parent = None
        init_list = []
        outliner_pos_a = 0
        combined_mesh = []
        post_list = []
        outliner_pos_b = 0
        relative_num = 0
        piv_rot = cmds.xform(last_obj[0], q=True, ws=True, ro=True)
        pivot = 1

        cmds.undoInfo(openChunk=True, cn="SmartCleanCombine")
        cmds.setToolTo('Move')

        if group_parent:
            source_parent = group_parent[0]
            init_list = cmds.listRelatives(source_parent, f=True, c=True)
            outliner_pos_a = init_list.index(last_obj[0]) + 1 if last_obj[0] in init_list else -1

            if pivot == 0:
                combined_mesh = cmds.polyUnite(ch=True, cp=True, muv=True)
            elif pivot == 1:
                combined_mesh = cmds.polyUnite(ch=True, op=True, muv=True)
                cmds.manipPivot(o=piv_rot)
                mel.eval('BakeCustomPivot')
            elif pivot == 2:
                combined_mesh = cmds.polyUnite(ch=True, muv=True)

            # if in_place == 1:
            cmds.parent(combined_mesh[0], source_parent)
            cmds.delete(combined_mesh[0], ch=True)
            post_list = cmds.listRelatives(source_parent, f=True, c=True)
            outliner_pos_b = post_list.index(combined_mesh[0]) + 1 if combined_mesh[0] in post_list else -1
            cmds.rename(combined_mesh[0], mel.eval('plugNodeStripped {}'.format(last_obj[0])))

        else:
            init_list = cmds.ls(l=True, assemblies=True)
            outliner_pos_a = init_list.index(last_obj[0]) + 1 if last_obj[0] in init_list else -1

            if pivot == 0:
                combined_mesh = cmds.polyUnite(ch=True, cp=True, muv=True)
            elif pivot == 1:
                combined_mesh = cmds.polyUnite(ch=True, op=True, muv=True)
                cmds.manipPivot(o=piv_rot)
                mel.eval('BakeCustomPivot')
            elif pivot == 2:
                combined_mesh = cmds.polyUnite(ch=True, muv=True)

            # if in_place == 1:
            cmds.delete(combined_mesh[0], ch=True)
            post_list = cmds.ls(l=True, assemblies=True)
            outliner_pos_b = post_list.index(combined_mesh[0]) + 1 if combined_mesh[0] in post_list else -1
            cmds.rename(combined_mesh[0], mel.eval('plugNodeStripped {}'.format(last_obj[0])))

        if current_ctx in ["Move", "moveSuperContext"]:
            cmds.setToolTo('Move')
        elif current_ctx in ["Rotate", "RotateSuperContext"]:
            cmds.setToolTo('Rotate')
        elif current_ctx in ["Scale", "scaleSuperContext"]:
            cmds.setToolTo('Scale')

        cmds.ConvertSelectionToEdges()
        cmds.polySelectConstraint(m=2, t=0x8000, border=1)
        cmds.polySelectConstraint(disable=True)

        if weld:
            cmds.polyMergeVertex(d=0.001)
        cmds.delete(last_obj, constructionHistory=True)
        cmds.select(last_obj, r=True)

        cmds.FreezeTransformations()
        cmds.CenterPivot()
        cmds.undoInfo(closeChunk=True)

    def smart_clean_separate(self, keepOriginal, *args):
        selected_objects = cmds.ls(selection=True)
        faces = cmds.filterExpand(selectionMask=34)

        if not selected_objects or (not faces and not cmds.ls(selected_objects, type='transform')):
            lj_warn(u"至少选择一个物体!!!")
            return

        cmds.undoInfo(openChunk=True, cn="SmartCleanSeparate")

        try:
            if faces:
                # 选面：直接走 Maya 原生 Extract Face（Edit Mesh > Extract Face，
                # 内部为 performPolyChipOff，含“分离抽出面”步骤）
                def face_center(face):
                    pts = cmds.xform(face, q=True, ws=True, t=True)
                    n = len(pts) / 3
                    return (round(sum(pts[0::3]) / n, 4),
                            round(sum(pts[1::3]) / n, 4),
                            round(sum(pts[2::3]) / n, 4))

                centers = set(face_center(f) for f in faces)

                owners = []
                for f in faces:
                    shape = cmds.listRelatives(f, parent=True, fullPath=True)
                    tr = cmds.listRelatives(shape[0], parent=True, fullPath=True) if shape else None
                    if tr:
                        owners.append(tr[0])
                owners = list(set(owners))

                transforms_before = set(cmds.ls(type='transform', long=True))
                cmds.select(faces, r=True)

                try:
                    # 复制面 = 是 → Duplicate Face；否 → Extract Face
                    mel.eval('performPolyChipOff 0 %d' % (1 if keepOriginal else 0))
                except RuntimeError:
                    lj_warn(u"面分离失败：请检查所选面是否有效!!!")
                    return

                new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                  if t not in transforms_before]

                if not new_transforms and owners:
                    # 原生命令的“分离抽出面”未生效时补一次分离
                    cmds.selectMode(object=True)
                    cmds.select(owners, r=True)
                    cmds.polySeparate(ch=False)
                    new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                      if t not in transforms_before]

                if not new_transforms:
                    lj_warn(u"面分离失败：没有生成新物体!!!")
                    return

                # polySeparate 会把抽出的新片挂在原物体变成的组下；
                # 解组让所有片成为独立物体，不残留组（与选物体分支一致）
                cmds.selectMode(object=True)
                if owners:
                    cmds.select(owners, r=True)
                    try:
                        cmds.ungroup()
                    except RuntimeError:
                        pass

                # 解组后长路径变化，按面心重新匹配抽出的新片
                new_transforms = [t for t in cmds.ls(type='transform', long=True)
                                  if t not in transforms_before]
                extracted = []
                for obj in new_transforms:
                    obj_faces = cmds.ls(obj + '.f[*]', flatten=True)
                    if not obj_faces:
                        continue
                    if all(face_center(f) in centers for f in obj_faces):
                        extracted.append(obj)
                if not extracted:
                    extracted = new_transforms

                cmds.select(extracted, r=True)
                cmds.CenterPivot()

                # 清理构造历史（保留下来的原流程）
                history_targets = [o for o in extracted + owners if cmds.objExists(o)]
                if history_targets:
                    cmds.delete(history_targets, ch=True)
                    remaining = [o for o in extracted if cmds.objExists(o)]
                    if remaining:
                        cmds.select(remaining, r=True)
            else:
                try:
                    cmds.polySeparate(ch=0)
                    cmds.pickWalk(d='up')
                    transforms = cmds.ls(selection=True, type='transform')
                    counter = 0

                    for grp in transforms:
                        striped_name = grp.replace("_grp", "")
                        children = cmds.listRelatives(grp, children=True, fullPath=True, type='transform') or []
                        gd_children = []

                        for child in children:
                            if cmds.objectType(child, isType='transform') and not cmds.listRelatives(child,
                                                                                                     shapes=True):
                                continue
                            gd_children.append(child)

                        for child in gd_children:
                            counter += 1
                            padding_string = '%03d' % counter
                            cmds.rename(child, '%s_%s_geo' % (striped_name, padding_string))

                    cmds.ungroup()
                    cmds.CenterPivot()
                except RuntimeError:
                    lj_warn(u"单个物体不能分离!!!")
        finally:
            cmds.undoInfo(closeChunk=True)

    # ------------------------------------------ selection ------------------------------------------

    # --------------------- LingJuGeometryTools.select_every_N_edge
    def select_every_N_edge(self, *args):
        selected_objects = cmds.ls(selection=True)

        if not selected_objects:
            lj_warn(u"先选择两条循环线!!!")
            return

        initial_edge_selection = cmds.ls(selection=True, flatten=True)
        num_edges = len(initial_edge_selection)

        if not num_edges == 2:
            lj_warn(u"先选择两条循环线!!!")
            return

        first_edge = initial_edge_selection[0]
        name_parts = first_edge.split('.e[')
        object_name = name_parts[0]
        first_edge_id = self.get_edge_id_from_selection_string(first_edge)

        if num_edges == 1:
            cmds.polySelect(selected_objects[0], edgeLoop=first_edge_id, noSelection=True)
            return

        second_edge = initial_edge_selection[1]
        second_edge_id = self.get_edge_id_from_selection_string(second_edge)

        edge_loop_ids = cmds.polySelect(selected_objects[0], edgeLoop=first_edge_id, noSelection=True)

        first_edge_index = self.get_edge_id_index(first_edge_id, edge_loop_ids)
        if first_edge_index == -1:
            return

        second_edge_index = self.get_edge_id_index(second_edge_id, edge_loop_ids)
        if second_edge_index == -1:
            edge_loop_ids = cmds.polySelect(selected_objects[0], edgeRing=first_edge_id, noSelection=True)
            first_edge_index = self.get_edge_id_index(first_edge_id, edge_loop_ids)
            if first_edge_index == -1:
                return
            second_edge_index = self.get_edge_id_index(second_edge_id, edge_loop_ids)
            if second_edge_index == -1:
                return

        num_edges_in_loop = len(edge_loop_ids)
        is_full_loop = (edge_loop_ids[0] == edge_loop_ids[-1])

        edges_to_skip = abs(second_edge_index - first_edge_index)

        if is_full_loop and edges_to_skip > (num_edges_in_loop - edges_to_skip - 1):
            edges_to_skip = num_edges_in_loop - edges_to_skip - 1

        selection_list = []

        for i in range(first_edge_index, num_edges_in_loop, edges_to_skip):
            selection_list.append(f"{object_name}.e[{edge_loop_ids[i]}]")
        for i in range(first_edge_index - edges_to_skip, -1, -edges_to_skip):
            selection_list.append(f"{object_name}.e[{edge_loop_ids[i]}]")

        cmds.select(selection_list)

    @staticmethod
    def get_edge_id_from_selection_string(sel):
        tokens = sel.split('[')
        if len(tokens) != 2 or not tokens[1].endswith(']'):
            return -1
        edge_id_str = tokens[1][:-1]
        try:
            return int(edge_id_str)
        except ValueError:
            return -1

    @staticmethod
    def get_edge_id_index(edge_id, edge_ids):

        try:
            return edge_ids.index(edge_id)
        except ValueError:
            return -1

    # --------------------- select_hard_edge
    def get_hard_edge(self, *args):
        edge_sel = cmds.filterExpand(fp=True, sm=32, ex=True)
        transforms_sel = cmds.ls(sl=True, transforms=True)

        if len(transforms_sel) >= 1:
            cmds.dR_DoCmd("modeEdge")
            cmds.polySelectConstraint(m=3, t=0x8000, sm=1)
            cmds.polySelectConstraint(disable=True)
        elif edge_sel and len(edge_sel) >= 1:
            cmds.polySelectConstraint(m=2, t=0x8000, sm=1)
            cmds.polySelectConstraint(disable=True)
        else:
            lj_warn(u"先选中物体或线")

    # --------------------- select similar mesh
    def select_similar_mesh(self, mode, *args):
        selection = cmds.ls(selection=True, long=True)
        if not selection:
            lj_warn(u"选择一个物体先！")
            return

        items = cmds.ls(selection=True, l=True, fl=True)
        all_transforms = cmds.ls(items, long=True, type='transform')
        mesh_items = []
        for transform in all_transforms:
            shapes = cmds.listRelatives(transform, shapes=True, fullPath=True, type='mesh') or []
            if shapes:
                LingJuGeometryTools.short_name = cmds.ls(transform, tail=True)[0]
                mesh_items.append(LingJuGeometryTools.short_name)

        scene_mesh = cmds.ls(long=True, type='transform')
        mesh_transforms = cmds.filterExpand(scene_mesh, sm=12) or []

        similar_meshes = []
        scale_source = []
        area_source = []

        if len(items) != len(mesh_items):
            lj_warn(u"选择一个物体先！")
        else:
            for item in items:
                scale = cmds.getAttr(item + ".scale")[0]
                scale_source.append(scale)
                temp_float = cmds.polyEvaluate(item, worldArea=True)
                area_source.append(temp_float)

                for mesh_transform in mesh_transforms:
                    if cmds.polyCompare(item, mesh_transform, fd=True) == 0:
                        temp_float2 = cmds.polyEvaluate(mesh_transform, worldArea=True)
                        area_target = temp_float2

                        if mode == 0:
                            similar_meshes.append(mesh_transform)
                        elif mode == 1:
                            if self.equivalent_tol(area_target, area_source[-1], 0.5):
                                similar_meshes.append(mesh_transform)

            cmds.select(similar_meshes, add=True)

    @staticmethod
    def equivalent_tol(val1, val2, tolerance):
        return abs(val1 - val2) <= tolerance

    # --------------------- select_by_angle
    @staticmethod
    def get_face_normal(face_name):
        poly_info_result = cmds.polyInfo(face_name, fn=True)[0]
        items = poly_info_result.split()
        x, y, z = float(items[2]), float(items[3]), float(items[4])
        normal = oma.MVector(x, y, z)

        # 获取面所在的物体
        parent_shape = cmds.listRelatives(face_name, parent=True, path=True)
        if not parent_shape:
            lj_error(f"Failed to get parent shape for {face_name}.")
            return normal

        parent_transform = cmds.listRelatives(parent_shape[0], parent=True, path=True)
        if not parent_transform:
            lj_error(f"Failed to get parent transform for {parent_shape[0]}.")
            return normal

        transform_matrix = cmds.xform(parent_transform[0], q=True, m=True, ws=True)

        m_matrix = oma.MMatrix(transform_matrix)
        world_normal = normal * m_matrix
        unit_world_normal = world_normal.normal()

        return unit_world_normal

    def grow_selection_by_normal_angle(self, angle):
        # 与滑块更新共用同一套扩选逻辑
        self.update_angle(angle)

    def update_angle(self, new_angle):
        if not LingJuGeometryTools.initial_faces:
            lj_warn(u"选择一个面！")
            return

        # Clear previous constraints and selection
        cmds.polySelectConstraint(dis=True)
        cmds.select(clear=True)

        all_selected_faces = set()
        for face_name in LingJuGeometryTools.initial_faces:
            poly_normal = self.get_face_normal(face_name)
            cmds.polySelectConstraint(mode=2, type=0x0008, orient=1,
                                      orientaxis=(poly_normal.x, poly_normal.y, poly_normal.z),
                                      orientbound=(0, new_angle))  # Set angle bounds

            # Select faces based on the new constraint
            cmds.select(face_name, r=True)
            for _ in range(50):
                cmds.polySelectConstraint(pp=1)

            # Collect selected faces
            selected_faces = cmds.ls(sl=True, fl=True, l=True)
            if selected_faces:
                all_selected_faces.update(selected_faces)

        # Update selection to reflect the new angle
        cmds.select(list(all_selected_faces))  # Select all faces that match the constraint
        cmds.polySelectConstraint(dis=True)  # Clear constraints

    def update_initial_faces(self):
        LingJuGeometryTools.initial_faces = cmds.ls(sl=True, fl=True)
        if not LingJuGeometryTools.initial_faces:
            lj_warn(u"请重新选择面！")

    # --------------------- LingJuGeometryTools.polyEdgeShift

    def polyEdgeShift(self, *args):
        # 初始化全局状态，避免首次点击 Flatten 复选框时变量未定义
        global polyEdgeShiftFlatten
        polyEdgeShiftFlatten = ''
        if cmds.window("polyEdgeShiftUI", exists = True):
            cmds.deleteUI("polyEdgeShiftUI")
        polyEdgeShiftUI = cmds.window('polyEdgeShiftUI' , t = "poly Edge Shift v1.01", w = 280, h = 50, s = 1 ,mxb = False,mnb = False)
        cmds.columnLayout(adj=1)
        cmds.text(l='')
        cmds.rowColumnLayout(nc= 6 ,cw=[(1,20),(2,75),(3,10),(4,75),(5,5),(6,75)])
        cmds.text(l='')
        # 类方法不能用字符串回调（Maya 在全局命名空间执行字符串），改用 lambda 闭包
        cmds.checkBox('polyEdgeShiftFlattenCheck',label="Flatten", value= 1,cc=lambda *a: self.polyEdgeToggleFlatten())
        cmds.text(l='')
        cmds.button('PESGo', l= "Go", c = lambda *a: self.polyEdgeShiftRun(),bgc = (0.24, 0.4, 0.25 ))
        cmds.text(l='')
        cmds.button('PESUndo', en=0, l= "Undo", bgc = (0.24, 0.24, 0.24), c = lambda *a: self.PEStempLoadPointData() )
        cmds.showWindow(polyEdgeShiftUI)


    def polyEdgeToggleFlatten(self):
        global polyEdgeShiftFlatten
        if cmds.objExists(polyEdgeShiftFlatten):
            checkCurrent = cmds.checkBox('polyEdgeShiftFlattenCheck',q=1,value= 1) 
            if polyEdgeShiftFlatten:
                if checkCurrent == 1.0:
                    cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 0)
                else:
                    cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 1)

    def polyEdgeShiftRun(self):
        global cutFaceSpineCv
        global cutFaceSpineEdge
        global cutFaceSpineEdgeID
        global dataDict
        global keepStartSelection
        global polyEdgeShiftFlatten
        global polyEdgeShiftData
        self.PEStempStorePointData()
        dataDict =[]
        cutFaceSpineCv = []
        cutFaceSpineEdge = []
        cutFaceSpineEdgeID = []
        innerCvList = []
        unwwantInnerEdge = []
        keepStartSelection = []
        polyEdgeShiftFlatten = ''
        cmds.button('PESGo',e=1, en=0,bgc=(0.24, 0.24, 0.24))
        cmds.button('PESUndo',e=1, en=0,bgc=(0.24, 0.24, 0.24))
        flattenIt = cmds.checkBox('polyEdgeShiftFlattenCheck',q=1, value= 1) 
        cleanList = ('cutPlane','snapPlane','showCutCurv*','shapeKeeper','shapeFlatten')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        
        cutFaceSpineFace =  cmds.filterExpand( sm=34)
        cutFaceSpineEdge =  cmds.filterExpand( sm=32)
        selMesh = ''
        if cutFaceSpineEdge:
            keepStartSelection = cutFaceSpineEdge
            cutFaceSpineCv = cmds.polyListComponentConversion(cutFaceSpineEdge,fe=1, tv=1,)
            cutFaceSpineCv = cmds.ls(cutFaceSpineCv,fl=1)
            unwwantInnerEdge = []
            self.createCutPinkLine(cutFaceSpineEdge)
            selMesh = cutFaceSpineEdge[0].split('.')[0]
        if cutFaceSpineFace:
            #create blendshape
            keepStartSelection = cutFaceSpineFace
            selMesh = cutFaceSpineFace[0].split('.')[0]
            toSpinFace = []
            cutFaceShapeMaster = selMesh
            dulpicateMesh = cmds.duplicate(selMesh,smartTransform=1,rr=1)
            cmds.rename(dulpicateMesh,'shapeKeeper')
            cmds.select(cutFaceSpineFace)
            #cmds.scale(0,1,1, cs=1, ls=1, r=1)
            dulpicateMesh = cmds.duplicate(selMesh,smartTransform=1,rr=1)
            cmds.rename(dulpicateMesh,'shapeFlatten')
            for c in cutFaceSpineFace:
                newName = c.replace(selMesh,'shapeFlatten')
                toSpinFace.append(newName)
            innerCvList = cmds.polyListComponentConversion(toSpinFace, ff=1, fv=1, fe=1, fuv=1, fvf=1, tv=1, internal=1)
            innerCvList = cmds.ls(innerCvList,fl=1)
            fullCvList = cmds.polyListComponentConversion(toSpinFace, ff=1, fv=1, fe=1, fuv=1, fvf=1, tv=1)
            fullCvList = cmds.ls(fullCvList,fl=1)
            cutFaceSpineCv = list(set(fullCvList)-set(innerCvList))
            cutFaceSpineEdge = cmds.polyListComponentConversion(toSpinFace, ff=1, te=1)
            cutFaceSpineEdge = cmds.ls(cutFaceSpineEdge,fl=1)
            unwwantInnerEdge = cmds.polyListComponentConversion(innerCvList, fv=1, te=1)  
            unwwantInnerEdge = cmds.ls(unwwantInnerEdge,fl=1)
            innerEdge = cmds.polyListComponentConversion(toSpinFace, ff=1, te=1, internal=1)
            borderEdges = list(set(cutFaceSpineEdge)-set(innerEdge))
            cmds.select(cutFaceSpineFace)
            cmds.ConvertSelectionToEdgePerimeter()
            borderEdge =  cmds.filterExpand( sm=32)
            self.createCutPinkLine(borderEdge)
            
        dataDict = []
        verPosiDict = []
        for b in cutFaceSpineCv:
            aimPoint = cmds.pointPosition(b,w=1)
            edgeList = cmds.polyListComponentConversion(b, fv=1, te=1)
            edgeX = cmds.ls(edgeList,fl=1)
            getEdge = list(set(edgeX) - set(cutFaceSpineEdge))
            cvList = cmds.polyListComponentConversion(getEdge, fe=1, tv=1)
            cvX =  cmds.ls(cvList,fl=1)
            getCV = list(set(cvX) - set(cutFaceSpineCv)-set(unwwantInnerEdge))
            basePoint = cmds.pointPosition(getCV[0],w=1)
            vectBtwPnts= ((basePoint [0] -aimPoint [0])*-1), ((basePoint [1] -aimPoint [1])*-1), ((basePoint [2] -aimPoint [2])*-1)
            vectorToFinish = om.MFloatVector(vectBtwPnts[0],vectBtwPnts[1],vectBtwPnts[2])
            raySource = om.MFloatPoint(basePoint[0],basePoint[1],basePoint[2])
            rayDirection = vectorToFinish
            rayDirection = rayDirection.normal()
            dataCollect = [b,raySource,rayDirection]
            dataDict.append(dataCollect)
            verPosiDict.append(aimPoint)
        avgPosition = self.getAvgVertPosition(cutFaceSpineCv)
        avgNormal = []
        mesh = cmds.polyCreateFacet(p=verPosiDict)
        avgNormal   = self.getAvgFaceNormal(mesh[0]+'.f[0]')
        if innerCvList:
            mfloatVectorSum = om.MFloatVector()
            for d in dataDict:
                mfloatVectorSum += dataDict[0][2]
            mfloatVectorAverage = mfloatVectorSum / len(dataDict)
            for i in innerCvList:
                basePoint = cmds.pointPosition(i,w=1)
                raySource = om.MFloatPoint(basePoint[0],basePoint[1],basePoint[2])
                dataCollect = [i,raySource,mfloatVectorAverage]
                dataDict.append(dataCollect)
        cmds.delete(mesh)
        snapPlane = cmds.polyPlane(sx=1,sy=1,n='cutPlane')[0]
        snapPlaneParent = cmds.group(name="snapPlane")
        attr = '{}.translate'.format(snapPlaneParent)
        for i, axis in enumerate(['X', 'Y', 'Z']):
            cmds.setAttr('{}{}'.format(attr,axis), avgPosition[i])
        attr = '{}.scale'.format(snapPlane)
        for axis in ['X', 'Y', 'Z']:
            cmds.setAttr('{}{}'.format(attr,axis), 10000)
        orientLocator = cmds.spaceLocator()
        attr = '{}.translate'.format(orientLocator[0])
        for i, axis in enumerate(['X', 'Y', 'Z']):
            cmds.setAttr('{}{}'.format(attr,axis), avgPosition[i])
        oc = cmds.orientConstraint(selMesh, orientLocator, offset=(0,0,0))
        cmds.delete(oc)
        cmds.makeIdentity(apply=1, t=1, r=1, s=1, n=0, pn=1)
        cmds.move(avgNormal[0], avgNormal[1], avgNormal[2], orientLocator, r=1, os=1)
        ac = cmds.aimConstraint(orientLocator, snapPlaneParent, aimVector=[0,1,0])
        offsetPVGrp = cmds.group(em=True, name="offsetPVGrp")
        cmds.matchTransform( orientLocator[0], 'snapPlane', rot=True)
        # Match the group's position to the locator's position
        cmds.matchTransform(offsetPVGrp, orientLocator[0], pos=True,rot=True)
        cmds.parent(orientLocator,offsetPVGrp)
        cmds.setAttr(orientLocator[0]+'.translateY',10000)
        cmds.delete(ac, offsetPVGrp)
        self.updateSpinFace()
        if cmds.objExists('shapeFlatten'):#edge mode
            cmds.transferAttributes('shapeFlatten',selMesh,transferPositions=True,transferNormals=False,transferUVs=False,transferColors=False,sampleSpace=4,sourceUvSpace="map1",targetUvSpace="map1",searchMethod=3,flipUVs=False,colorBorders=True)
            cmds.delete(selMesh ,ch=1)
            polyEdgeShiftFlatten = cmds.blendShape('shapeKeeper', 'shapeFlatten', selMesh)[0]
            cmds.setAttr(polyEdgeShiftFlatten + ".shapeFlatten", 1)
            cmds.setAttr(polyEdgeShiftFlatten + ".shapeKeeper", 1)
        sellist = om.MSelectionList()
        sellist.add('cutPlane')
        node = om.MObject()
        sellist.getDependNode(0, node)
        cutFaceSpineEdgeID = om.MNodeMessage.addAttributeChangedCallback(node, self.spinCutFace)
        cmds.select('cutPlane')
        hideList = ('cutPlane','shapeFlatten','shapeKeeper','snapPlane')
        for c in hideList:
            if cmds.objExists(c):
                cmds.setAttr(c + '.visibility',0)
                cmds.setAttr(c + '.hiddenInOutliner',1)
        cmds.scriptJob (ro=1, event = ["SelectionChanged", self.killCallBackRun])
        cmds.setToolTo("Rotate")
        cmds.undoInfo(ock=1)
        self.polyEdgeToggleFlatten()
        
    def weight_normal(self, *args):
        """Weight Normal（Smart Outside）：整段 MEL 内嵌在文件末尾，eval 即弹窗。"""
        mel.eval(WEIGHT_NORMAL_MEL)
        
    def createCutPinkLine(self,cutFaceSpineEdge):
        sortEdgeLoopGrp =  self.getEdgeRingGroup(0,cutFaceSpineEdge)
        i=1
        for s in sortEdgeLoopGrp:
            cmds.select(s)
            cmds.polyToCurve(form=2, degree=1, conformToSmoothMeshPreview=1)
            #cmds.CreateCurveFromPoly()
            cmds.rename('showCutCurve_'+str(i))
            curveName ='showCutCurve_'+str(i)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.lineWidth', 5)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideEnabled', 1)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideRGBColors', 1)
            cmds.setAttr('showCutCurve_Shape'  +str(i) + '.overrideColorRGB', 1, 0 , 0.8)
            cmds.setAttr(curveName + '.hiddenInOutliner',1)
            i= i+1

    def killCallBackRun(self):
        global keepStartSelection
        global cutFaceSpineEdgeID
        om.MMessage.removeCallback(cutFaceSpineEdgeID)
        cleanList = ('cutPlane','snapPlane','showCutCurv*','shapeKeeper','shapeFlatten')
        for c in cleanList:
            if cmds.objExists(c):
                cmds.delete(c)
        selMesh =keepStartSelection[0].split('.')[0]
        cmds.delete(selMesh ,ch=1)
        cmds.select(keepStartSelection)
        cmds.undoInfo(cck=1)
        cutFaceSpineFace =  cmds.filterExpand( sm=34)
        cutFaceSpineEdge =  cmds.filterExpand( sm=32)
        if cutFaceSpineFace:
            mel.eval('doMenuComponentSelectionExt("' + selMesh + '", "facet", 0);')
        elif cutFaceSpineEdge:
            mel.eval('doMenuComponentSelectionExt("' + selMesh + '", "edge", 0);')
        
        cmds.button('PESUndo',e=1, en=1,bgc = (0.4, 0.24, 0.25) )
        cmds.button('PESGo',e=1, en=1,bgc=(0.24, 0.4, 0.24))
        
    def spinCutFace(self, msg, m_plug, otherMplug, clientData):
        #print(m_plug.partialName())
        if 'r'  in m_plug.partialName() or 't' in m_plug.partialName():
            self.updateSpinFace()


    def getAvgVertPosition(self,vList):
        totalPos = oma.MPoint(0., 0., 0.)
        for vert in vList:
            pos = oma.MPoint(cmds.pointPosition(vert, world=True))
            totalPos += pos
        count = len(vList)
        return list(totalPos / float(count))[0:3]


    def getAvgFaceNormal(self,selFace):
        face_normals = cmds.polyInfo(selFace, fn=True)
        normals = [[], [], []]
        for normal in face_normals:
            label, vertex, x, y, z = normal.split()
            normals[0].append(float(x))
            normals[1].append(float(y))
            normals[2].append(float(z))    
        normals_count = len(face_normals)
        x_avg = sum(normals[0]) / normals_count
        y_avg = sum(normals[1]) / normals_count
        z_avg = sum(normals[2]) / normals_count
        return x_avg, y_avg,z_avg


    def updateSpinFace(self):
        global dataDict
        selectionList = om.MSelectionList()
        selectionList.add('cutPlane')
        node = om.MDagPath()
        selectionList.getDagPath( 0, node )
        meshFn = om.MFnMesh()
        meshFn.setObject( node )
        for d in dataDict:     
            aimPoint = cmds.pointPosition(d[0],w=1)  
            hitpoint = om.MFloatPoint()
            finalX = []
            finalY = []
            finalZ = []
            shortDistance = 10000000000
            distanceBetween = 1000000000
            intersection = meshFn.closestIntersection(
                om.MFloatPoint(d[1]),
                om.MFloatVector(d[2]),
                None,
                None,
                False,
                om.MSpace.kWorld,
                99999,
                False,
                None,
                hitpoint,
                None,
                None,
                None,
                None,
                None)
            if intersection:
                x = hitpoint.x
                y = hitpoint.y
                z = hitpoint.z
                distanceBetween = math.sqrt( ((float(aimPoint[0]) - x)**2)  + ((float(aimPoint[1]) - y)**2) + ((float(aimPoint[2]) - z)**2))
                if distanceBetween < shortDistance:
                    shortDistance = distanceBetween
                cmds.move(x, y, z, d[0], a=1, ws=1)
                
            else:
                shortDistance = 10000000000
                distanceBetween = 1000000000
                mfloatVectorReverse = -d[2]
                intersection = meshFn.closestIntersection(
                    om.MFloatPoint(d[1]),
                    om.MFloatVector(mfloatVectorReverse),
                    None,
                    None,
                    False,
                    om.MSpace.kWorld,
                    99999,
                    False,
                    None,
                    hitpoint,
                    None,
                    None,
                    None,
                    None,
                    None)
                if intersection:
                    x = hitpoint.x
                    y = hitpoint.y
                    z = hitpoint.z
                    distanceBetween = math.sqrt( ((float(aimPoint[0]) - x)**2)  + ((float(aimPoint[1]) - y)**2) + ((float(aimPoint[2]) - z)**2))
                    if distanceBetween < shortDistance:
                        shortDistance = distanceBetween
                    cmds.move(x, y, z, d[0], a=1, ws=1)

    def getEdgeRingGroup(self,listSort,listInput):
        selEdges = cmds.ls(sl=1,fl=1)
        trans = selEdges[0].split(".")[0]
        e2vInfos = cmds.polyInfo(selEdges, ev=True)
        e2vDict = {}
        fEdges = []
        for info in e2vInfos:
            evList = [ int(i) for i in re.findall('\\d+', info) ]
            e2vDict.update(dict([(evList[0], evList[1:])]))
        while True:
            try:
                startEdge, startVtxs = e2vDict.popitem()
            except:
                break
            edgesGrp = [startEdge]
            num = 0
            for vtx in startVtxs:
                curVtx = vtx
                while True:

                    nextEdges = []
                    for k in e2vDict:
                        if curVtx in e2vDict[k]:
                            nextEdges.append(k)
                    if nextEdges:
                        if len(nextEdges) == 1:
                            if num == 0:
                                edgesGrp.append(nextEdges[0])
                            else:
                                edgesGrp.insert(0, nextEdges[0])
                            nextVtxs = e2vDict[nextEdges[0]]
                            curVtx = [ vtx for vtx in nextVtxs if vtx != curVtx ][0]
                            e2vDict.pop(nextEdges[0])
                        else:
                            break
                    else:
                        break
                num += 1
            fEdges.append(edgesGrp)
        retEdges =[]
        for f in fEdges:
            f= list(map(lambda x: (trans +".e["+ str(x) +"]"), f))
            retEdges.append(f)
        if listSort == 1:
            sortEdgeLoopOrder=[]
            getCircleState,listVtx = vtxLoopOrderCheck(listInput)
            for l in listVtx:
                for r in retEdges:
                    checkCvList = cmds.ls(cmds.polyListComponentConversion( r,fe=True, tv=True), fl=True,l=True)
                    if l in checkCvList:
                        sortEdgeLoopOrder.append(r)
            return sortEdgeLoopOrder
        else:
            return retEdges


    def PEStempStorePointData(self):
        global polyEdgeShiftData
        polyEdgeShiftData=[]
        selPoint = []
        selFace = cmds.filterExpand(expand=True ,sm=34)
        if selFace:
            selPoint = cmds.polyListComponentConversion(selFace, ff=1, tv=1)
        
        selEdge = cmds.filterExpand(expand=True ,sm=32)
        if selEdge:
            selPoint = cmds.polyListComponentConversion(selEdge, fe=1, tv=1)
        selPoint = cmds.ls(selPoint,fl=1)
        if selPoint:
            storePointData = ''
            meshName = selPoint[0].split('.')[0]
            shapeNode = cmds.listRelatives(meshName, fullPath=True , c=True )
            if not cmds.attributeQuery('pointPosData', node = shapeNode[0], ex=True ):
                cmds.addAttr(shapeNode[0], ln='pointPosData',  dt= 'string')
                cmds.setAttr((shapeNode[0]+'.pointPosData'),e=True, keyable=True)
            for s in selPoint:
                xA, yA, zA = cmds.pointPosition(s, w=1)
                xA = round(xA,4)
                yA = round(yA,4)
                zA = round(zA,4)
                storePointData = storePointData + (str(s) + ',' + str(xA) + ',' + str(yA) + ',' + str(zA) + '@')
            polyEdgeShiftData = storePointData


    def PEStempLoadPointData(self):
        global polyEdgeShiftData
        meshName = polyEdgeShiftData.split('.')[0]
        if cmds.objExists(meshName):
            dataList = polyEdgeShiftData.split('@')
            del dataList[-1]
            for r in dataList:
                rawData = r.split(',')
                cmds.move(float(rawData[1]), float(rawData[2]), float(rawData[3]),rawData[0] , a =True, ws=True)
        cmds.button('PESUndo',e=1, en=0,bgc = (0.24, 0.24, 0.24) )
                        

    # ------------------------------------------ modeling ------------------------------------------

    # --------------------- LingJuGeometryTools.corner_killer
    def corner_killer(self, *args):
        selection = cmds.ls(sl=True, fl=True)
        if not selection:
            lj_warn(u"请选择转角面！")
            return

        if cmds.objExists('sortFaceKeep'):
            cmds.delete('sortFaceKeep')

        cmds.sets(name="sortFaceKeep", text="sortFaceKeep")
        selFace = cmds.ls(sl=1, fl=1)
        threeLessEdgeFaceList = []
        doneFaceList = []

        meshName = selFace[0].split('.')[0]
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            num_edges = 0
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges < 4:
                    threeLessEdgeFaceList.append(s)

        if threeLessEdgeFaceList:
            cmds.select(threeLessEdgeFaceList)
            cmds.sets(threeLessEdgeFaceList, rm="sortFaceKeep")
            self.corner_topoL()

        try:
            cmds.select('sortFaceKeep')
            CMD = 'doMenuComponentSelectionExt("' + meshName + '", "facet", 0);'
            mel.eval(CMD)
        except:
            cmds.select(meshName)

        selFace = cmds.ls(sl=1, fl=1)
        fiveEdgeFaceList = []

        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges == 5:
                    fiveEdgeFaceList.append(s)

        if fiveEdgeFaceList:
            cmds.select(fiveEdgeFaceList)
            cmds.sets(fiveEdgeFaceList, rm="sortFaceKeep")
            self.corner_topoB()
        try:
            cmds.select('sortFaceKeep')
            CMD = 'doMenuComponentSelectionExt("' + meshName + '", "facet", 0);'
            mel.eval(CMD)
        except:
            cmds.select(meshName)

    def corner_topoB(self):
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')
        selFace = cmds.ls(sl=1, fl=1)
        recordRemoveList = []
        recordSecondFacesList = []
        recordConnetCVList = []
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            vertices_with_5_edges = []
            adjVertex = []
            removeList = []
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges == 5:
                    vertices_with_5_edges.append(vertex)
            cmds.select(vertices_with_5_edges)
            cmds.ConvertSelectionToEdges()
            cmds.ConvertSelectionToVertices()
            checkVertex = cmds.ls(sl=1, fl=1)
            adjVertex = list(set(AllVertex) - set(checkVertex))
            connectVertex = list(set(AllVertex) - set(vertices_with_5_edges) - set(adjVertex))
            recordConnetCVList.append(adjVertex[0])
            recordConnetCVList.append(vertices_with_5_edges[0])
            removeList = removeList + connectVertex + vertices_with_5_edges
            checkA = cmds.polyListComponentConversion(adjVertex, te=True)
            checkB = cmds.polyListComponentConversion(checkA, tv=True)
            checkB = cmds.ls(checkB, fl=1)
            checkC = list(set(checkB) - set(AllVertex))
            checkDA = cmds.polyListComponentConversion(checkC[0], te=True)
            checkDAB = cmds.polyListComponentConversion(checkDA, tv=True)
            checkDAB = cmds.ls(checkDAB, fl=1)
            checkEA = cmds.polyListComponentConversion(checkC[1], te=True)
            checkEAB = cmds.polyListComponentConversion(checkEA, tv=True)
            checkEAB = cmds.ls(checkEAB, fl=1)
            adjFar = list(set(checkDAB) & set(checkEAB))
            cmds.select(adjFar, checkC)
            cmds.ConvertSelectionToContainedFaces()
            getF = cmds.ls(sl=1, fl=1)[0]
            recordSecondFacesList.append(getF)
            cmds.select(removeList)
            cmds.select(adjFar, d=1)
            cmds.ConvertSelectionToContainedEdges()
            cmds.SelectEdgeLoopSp()
            removeEdgeList = cmds.ls(sl=1, fl=1)
            recordRemoveList = recordRemoveList + removeEdgeList
            cmds.select(cl=1)
        cmds.select(recordSecondFacesList)
        cmds.sets(name="secFaceKeep", text="secFaceKeep")
        cmds.select(recordConnetCVList)
        cmds.polyConnectComponents()
        cmds.select(recordRemoveList)
        cmds.polyDelEdge(cv=1)
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')

    def corner_topoL(self):
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')
        selFace = cmds.ls(sl=1, fl=1)
        cmds.sets(name="secFaceKeep", text="secFaceKeep")
        recordConnetCVList = []
        recordAdjCVList = []
        recordOutCVList = []
        meshName = selFace[0].split('.')[0]
        cmds.select(meshName)
        cmds.polySelectConstraint(mode=3, type=0x8000, where=1)
        checkHole = cmds.ls(sl=1, fl=1)
        cmds.polySelectConstraint(disable=True)
        for s in selFace:
            AllVertex = cmds.polyListComponentConversion(s, tv=True)
            AllVertex = cmds.ls(AllVertex, fl=1)
            verticesLessEdges = ''
            maxEdgeNo = 100000
            for vertex in AllVertex:
                edges = cmds.polyListComponentConversion(vertex, te=True)
                num_edges = len(cmds.ls(edges, fl=True))
                if num_edges < maxEdgeNo:
                    maxEdgeNo = num_edges
                    verticesLessEdges = vertex
            cmds.select(verticesLessEdges)
            cmds.ConvertSelectionToEdges()
            cmds.ConvertSelectionToVertices()
            checkVertex = cmds.ls(sl=1, fl=1)
            adjVertex = list(set(AllVertex) - set(checkVertex))
            connectVertex = list(set(AllVertex) - set(adjVertex))
            connectVertex.remove(verticesLessEdges)
            recordAdjCVList.append(adjVertex[0])
            recordConnetCVList.append(connectVertex[0])
            recordConnetCVList.append(connectVertex[1])
            recordOutCVList.append(verticesLessEdges)
            cmds.select(cl=1)
        cmds.select(recordAdjCVList, recordOutCVList)
        cmds.polyConnectComponents()
        cmds.select('secFaceKeep')
        oldFaceNewEdge = cmds.polyListComponentConversion(te=True)
        oldFaceNewEdge = cmds.ls(oldFaceNewEdge, fl=1)
        cmds.select(recordAdjCVList, recordConnetCVList)
        cmds.ConvertSelectionToContainedEdges()
        if len(selFace) == 1:
            if len(checkHole) > 0:
                cmds.SelectEdgeLoopSp()
        if len(selFace) > 1:
            cmds.SelectEdgeLoopSp()
        cmds.polyDelEdge(cv=1)
        if cmds.objExists('secFaceKeep'):
            cmds.delete('secFaceKeep')

    # --------------------- split around
    def split_around(self):
        cmds.ConvertSelectionToVertices()
        originalVerts = cmds.ls(selection=True, fl=True)
        cmds.ConvertSelectionToContainedEdges()
        originalEdges = cmds.ls(selection=True, fl=True)

        cmds.ConvertSelectionToVertices()
        cmds.ConvertSelectionToEdges()
        expandedEdges = cmds.ls(selection=True, fl=True)

        if originalEdges:
            cmds.select(deselect=True, *originalEdges)

        containedFaces = cmds.polyListComponentConversion(tf=True, ff=True)
        containedFaces = cmds.ls(selection=True, fl=True)

        if containedFaces:
            cmds.select(containedFaces, r=True)
            containedFacesVerts = cmds.ls(selection=True, fl=True)

            intersectingVerts = list(set(originalVerts) & set(containedFacesVerts))

            cmds.select(originalVerts, r=True)

            cmds.select(deselect=True, *containedFacesVerts)

            if intersectingVerts:
                cmds.select(toggle=True, *intersectingVerts[0])

            cmds.ConvertSelectionToVertices()
            cmds.ConvertSelectionToEdges()
            expandedEdges = cmds.ls(selection=True, fl=True)

        else:
            cmds.select(expandedEdges, r=True)

        if originalEdges:
            cmds.select(deselect=True, *originalEdges)

        cmds.ls(selection=True, fl=True)

        try:
            cmds.polySplitRing(wt=0.5)
        except TypeError:
            lj_warn(u"选择点线面元素")

    # --------------------- smart_insert_face II
    def insert_face(self):
        if len(cmds.ls(selection=True, transforms=True)) == 1:
            cmds.ConvertSelectionToFaces()

        facelist = cmds.ls(selection=True, flatten=True)
        cmds.select(facelist, replace=True)

        faceTotal = cmds.polyEvaluate(face=True)
        faceComp = cmds.polyEvaluate(faceComponent=True)
        shells = cmds.polyEvaluate(shell=True)

        if faceTotal == faceComp:
            extrude = cmds.polyExtrudeFacet(offset=0.05)
            cmds.GrowPolygonSelectionRegion()
            cmds.InvertSelection()
            cmds.delete()
            cmds.select(facelist, replace=True)
            intFaceList = cmds.polyListComponentConversion(ff=True, tf=True, internal=True)
            cmds.select(intFaceList, add=True)
            cmds.select(extrude[0], add=True)
            cmds.ShowManipulatorTool()
        else:
            extrude = cmds.polyExtrudeFacet(offset=0.05)
            cmds.select(facelist, replace=True)
            intFaceList = cmds.polyListComponentConversion(ff=True, tf=True, internal=True)
            cmds.select(extrude[0], add=True)
            cmds.ShowManipulatorTool()

    # --------------------- LingJuGeometryTools.match_vertex_normal
    def get_face_area(self, mesh_fn, face_id):
        """ 计算多边形面的面积（替代不存在的 getFaceArea 方法） """
        vertices = mesh_fn.getPolygonVertices(face_id)
        points = mesh_fn.getPoints(oma.MSpace.kWorld)

        vec_points = [oma.MVector(points[v]) for v in vertices]

        total_area = 0.0
        for i in range(1, len(vec_points) - 1):
            v0 = vec_points[0]
            v1 = vec_points[i]
            v2 = vec_points[i + 1]

            cross = (v1 - v0) ^ (v2 - v0)
            total_area += cross.length() / 2.0

        return total_area

    def transfer_vertex_normals(self, source_vtx, target_vtx):
        source_mesh = source_vtx.split('.')[0]
        source_id = int(source_vtx.split('[')[1].split(']')[0])

        sel_list = oma.MSelectionList()
        sel_list.add(source_mesh)
        source_dag = sel_list.getDagPath(0)
        source_fn = oma.MFnMesh(source_dag)

        connected_faces = cmds.polyListComponentConversion(source_vtx, fv=True, tf=True)
        connected_faces = cmds.ls(connected_faces, fl=True)

        weighted_normals = []
        for face in connected_faces:
            face_id = int(face.split('[')[1].split(']')[0])

            face_area = self.get_face_area(source_fn, face_id)
            normal = source_fn.getFaceVertexNormal(face_id, source_id, oma.MSpace.kWorld)
            weighted_normals.append(normal * face_area)

        avg_normal = sum(weighted_normals, oma.MVector(0, 0, 0))
        avg_normal.normalize()

        target_mesh = target_vtx.split('.')[0]
        target_id = int(target_vtx.split('[')[1].split(']')[0])

        sel_list.clear()
        sel_list.add(target_mesh)
        target_dag = sel_list.getDagPath(0)
        target_fn = oma.MFnMesh(target_dag)

        target_faces = cmds.polyListComponentConversion(target_vtx, fv=True, tf=True)
        target_faces = cmds.ls(target_faces, fl=True)

        for face in target_faces:
            face_id = int(face.split('[')[1].split(']')[0])
            face_normal = target_fn.getPolygonNormal(face_id, oma.MSpace.kWorld)

            if avg_normal * face_normal < 0:
                final_normal = -avg_normal
            else:
                final_normal = avg_normal

            target_fn.setFaceVertexNormal(final_normal, face_id, target_id, oma.MSpace.kWorld)

        target_fn.updateSurface()

    def find_matching_vertices(self, selection, threshold=0.0001):
        """ 根据选中的顶点生成源到目标的顶点对，目标为第一个选中的模型顶点 """
        if not selection:
            return []

        target_mesh = selection[0].split('.')[0]
        target_vertices = [v for v in selection if v.split('.')[0] == target_mesh]
        source_vertices = [v for v in selection if v.split('.')[0] != target_mesh]

        if not target_vertices or not source_vertices:
            return []

        pairs = []

        for tgt in target_vertices:
            tgt_pos = cmds.pointPosition(tgt)
            closest_src = None
            min_dist = threshold

            for src in source_vertices:
                src_pos = cmds.pointPosition(src)
                dist = sum((tgt_pos[i] - src_pos[i]) ** 2 for i in range(3)) ** 0.5
                if dist < min_dist:
                    min_dist = dist
                    closest_src = src

            if closest_src:
                pairs.append((closest_src, tgt))

        return pairs

    def match_vertex_normal(self):
        """ 主执行函数：将法线从源顶点传递到第一个选中的模型顶点 """
        selection = cmds.ls(sl=True, fl=True)

        if len(selection) < 2:
            lj_warn("请选择至少两个不同物体的顶点")
            return

        vertex_pairs = self.find_matching_vertices(selection)

        if not vertex_pairs:
            lj_warn("未找到重合顶点（间距需<0.0001单位）或目标模型无对应源顶点")
            return

        for src, tgt in vertex_pairs:
            self.transfer_vertex_normals(src, tgt)

        cmds.refresh(force=True)

    # --------------------- pivot

    def move_pivot_to_bottom_center(self, *args):
        selection = cmds.ls(sl=True, l=True)

        for sel in selection:
            bBox = cmds.xform(sel, q=True, ws=True, bb=True)
            centerX = (bBox[0] + bBox[3]) / 2
            bottomY = bBox[1]
            centerZ = (bBox[2] + bBox[5]) / 2

            cmds.xform(sel, ws=True, piv=(centerX, bottomY, centerZ))

    def is_dag_visible(self, node):
        """沿父级链检查节点及其所有祖先的 visibility 属性

        :param node: DAG 节点名（transform 或 shape 均可）
        :return: bool，任意一层的 visibility 为 0 时返回 False
        """
        current = node
        while current:
            try:
                if not cmds.getAttr("%s.visibility" % current):
                    return False
            except Exception:
                # 部分节点没有 visibility 属性时按可见处理
                pass
            parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
            current = parents[0] if parents else None
        return True

    def get_visible_world_bbox(self, node):
        """返回节点"可见几何"的世界包围盒，用作居中计算的基准

        exactWorldBoundingBox 默认会把隐藏子物体（cluster handle、locator、残留
        节点等）算进来，选中"组"时也按整棵子树取并集，都会把居中基准拉偏；这里
        只统计节点自身及子孙里可见的网格。

        :param node: transform 节点（长名）
        :return: [xmin, ymin, zmin, xmax, ymax, zmax]；没有网格时退回节点自身包围盒
        """
        shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, fullPath=True) or []
        shapes = [s for s in shapes if self.is_dag_visible(s)]
        if not shapes:
            meshes = cmds.listRelatives(node, allDescendents=True, type="mesh",
                                       noIntermediate=True, fullPath=True) or []
            shapes = [m for m in meshes if self.is_dag_visible(m)]
        if shapes:
            return cmds.exactWorldBoundingBox(shapes, ignoreInvisible=True)
        return cmds.exactWorldBoundingBox(node, ignoreInvisible=True)

    def can_snap_transform(self, node):
        """检查节点的 transform 是否可安全移动/冻结

        带约束或锁定的通道会让 setAttr / makeIdentity 报错，旧实现会因此中断整个
        循环，留下"只移动了一半"的半成品，所以先做预检。

        :param node: transform 节点（长名）
        :return: (bool, str)，第二个值是不可处理的原因
        """
        channels = ("translateX", "translateY", "translateZ",
                    "rotateX", "rotateY", "rotateZ",
                    "scaleX", "scaleY", "scaleZ")
        for attr in ("translate", "rotate", "scale") + channels:
            plugs = cmds.listConnections("%s.%s" % (node, attr), source=True,
                                         destination=False, plugs=True) or []
            if plugs:
                return False, u"%s 有输入连接（%s）" % (attr, plugs[0])
        for attr in channels:
            try:
                if cmds.getAttr("%s.%s" % (node, attr), lock=True):
                    return False, u"%s 已锁定" % attr
            except Exception:
                pass
        return True, ""

    def snap_to_world_center_bottom(self, *args):
        """把选中物体（或组）的可见几何居中到世界原点，最低点压到 Y=0 后冻结变换

        v146 修复要点：
        1. 位移改用世界空间相对移动（cmds.move -ws -r），父级带旋转/缩放时不再产生
           偏移。旧实现把世界空间增量直接加到本地 translate 通道上，误差正比于物体
           离原点的距离，大场景里尤其明显；
        2. 包围盒只统计可见网格，隐藏子物体不再拉偏居中基准；
        3. translate/rotate/scale 有输入连接或锁定时跳过并提示，冻结失败只警告不中断，
           避免留下"只移动了一半"的半成品状态；
        4. 整段放进一个 undo chunk，撤销一次即可回到原位（旧实现是三笔独立操作）；
        5. 只冻结自身带网格且没有子 transform 的叶子物体，选中"组"时只移动不冻结，
           避免把子物体的旋转/缩放级联烘焙掉。

        :param args: Maya button callback 附带的多余参数
        :return: None
        """
        nodes = cmds.ls(selection=True, type="transform", long=True) or []
        if not nodes:
            lj_warn(u"没有选中任何物体！")
            return

        skipped = []
        cmds.undoInfo(openChunk=True, chunkName="Snap To World Center")
        try:
            for node in nodes:
                can_snap, reason = self.can_snap_transform(node)
                if not can_snap:
                    skipped.append(u"%s：%s" % (node, reason))
                    continue

                bbox = self.get_visible_world_bbox(node)
                if bbox is None:
                    skipped.append(u"%s：没有可见几何" % node)
                    continue

                # 世界空间把包围盒中心推到原点（父级旋转/缩放由 Maya 负责换算）
                cmds.move(-(bbox[0] + bbox[3]) / 2.0,
                          -(bbox[1] + bbox[4]) / 2.0,
                          -(bbox[2] + bbox[5]) / 2.0,
                          node, worldSpace=True, relative=True)

                # 世界空间把最低点压到 Y=0
                bbox = self.get_visible_world_bbox(node)
                if bbox:
                    cmds.move(0, -bbox[1], 0, node, worldSpace=True, relative=True)

                # 只冻结叶子网格；组（含子级 transform）只移动，避免级联烘焙
                has_mesh = bool(cmds.listRelatives(node, shapes=True, noIntermediate=True))
                has_child_transform = bool(cmds.listRelatives(node, children=True, type="transform"))
                if has_mesh and not has_child_transform:
                    try:
                        cmds.makeIdentity(node, apply=True, translate=True,
                                          rotate=True, scale=True, normal=False)
                    except Exception as error:
                        lj_warn(u"%s 冻结失败：%s" % (node, error))
        finally:
            cmds.undoInfo(closeChunk=True)

        # 移动后恢复原有选择，便于连续操作
        if cmds.ls(selection=True, type="transform", long=True) != nodes:
            cmds.select(nodes, replace=True)

        if skipped:
            lj_warn(u"已跳过 %d 个物体：%s" % (len(skipped), skipped[0]))

        
    def get_selected_transforms(self):
        """按选择顺序返回选中的物体，组件选择归到它所属的 transform

        只用 ls(type="transform") 拿到的是"整物件选择"，一旦先选的是点/面就会漏掉
        物体、甚至把后面才选的对象当成参考，所以逐项往上追溯到 transform 并去重。

        :return: transform 长名列表，第一个即最早选中的物体
        """
        nodes = []
        for item in cmds.ls(selection=True, long=True) or []:
            node = item
            while node and cmds.nodeType(node) != "transform":
                parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
                node = parents[0] if parents else None
            if node and node not in nodes:
                nodes.append(node)
        return nodes

    def can_move_axis(self, node):
        """检查节点的坐标轴原点能否改动（pivot 通道没锁没连接、translate 可写）

        pivot_set_pivot 是先搬 pivot、再用世界矩阵的位移差回写 translate 做补偿，
        通道被锁定或被连接时会在中间那一步报错，留下 pivot 已经搬走、补偿却没做
        的错位物体，所以逐个预检，不合格的整体跳过。

        :param node: transform 节点（长名）
        :return: (bool, str)，第二个值是不可处理的原因
        """
        channels = ("rotatePivotX", "rotatePivotY", "rotatePivotZ",
                    "scalePivotX", "scalePivotY", "scalePivotZ",
                    "translateX", "translateY", "translateZ")
        for attr in ("rotatePivot", "scalePivot", "translate") + channels:
            plugs = cmds.listConnections("%s.%s" % (node, attr), source=True,
                                         destination=False, plugs=True) or []
            if plugs:
                return False, u"%s 有输入连接（%s）" % (attr, plugs[0])
        for attr in channels:
            try:
                if cmds.getAttr("%s.%s" % (node, attr), lock=True):
                    return False, u"%s 已锁定" % attr
            except Exception:
                pass
        return True, ""

    def match_axis_to_first(self, *args):
        """把后选中物体的坐标轴原点搬到第一个选中物体的 pivot 位置，物体本身不动

        选择顺序就是优先级：第一个选中的是参考，其余物体只搬坐标轴原点
        （rotate pivot 和 scale pivot 一起对齐到同一个点），坐标轴的朝向保持各自
        原值，所以几何在世界空间里一步都不会动，之后转动物体时才会绕这个共同
        的原点转。

        :param args: Maya button callback 附带的多余参数
        :return: None
        """
        nodes = self.get_selected_transforms()
        if len(nodes) < 2:
            lj_warn(u"请先选中参考物体，再加选需要匹配的物体！")
            return

        ref_pivot = cmds.xform(nodes[0], query=True, worldSpace=True, rotatePivot=True)
        skipped = []
        cmds.undoInfo(openChunk=True, chunkName="Match Axis")
        try:
            for node in nodes[1:]:
                can_move, reason = self.can_move_axis(node)
                if not can_move:
                    skipped.append(u"%s：%s" % (node, reason))
                    continue
                self.pivot_set_pivot(node, ref_pivot)
        finally:
            cmds.undoInfo(closeChunk=True)

        if skipped:
            lj_warn(u"已跳过 %d 个物体：%s" % (len(skipped), skipped[0]))

    # --------------------- pivot boundary（Pivot 物体边界对齐） ---------------------

    # 平面按钮配色：{平面: (激活色, 未激活色)}
    PIVOT_PLANE_COLORS = {
        'xy': ((0.25, 0.45, 0.7), (0.10, 0.12, 0.16)),   # 蓝
        'zy': ((0.7, 0.35, 0.25), (0.16, 0.11, 0.10)),   # 红
        'zx': ((0.35, 0.7, 0.35), (0.10, 0.15, 0.11)),   # 绿
    }

    def pivot_get_bbox(self):
        """返回当前选中物体及其世界包围盒

        :return: (物体名, [xmin, ymin, zmin, xmax, ymax, zmax])，未选中时返回 (None, None)
        """
        sel = cmds.ls(selection=True, long=True)
        if not sel:
            lj_warn(u"请先选中物体！")
            return None, None
        obj = sel[0]
        return obj, cmds.exactWorldBoundingBox(obj)

    def pivot_set_pivot(self, obj, point):
        """把物体 rotate/scale pivot 设到世界坐标 point，并做位移补偿保证物体不被带跑

        pivot 只改变旋转/缩放中心，不影响世界矩阵的线性部分，
        对比设置前后世界矩阵的平移分量，反向移动即可让物体留在原位。

        :param obj: transform 节点名
        :param point: [x, y, z] 世界坐标
        """
        before = cmds.xform(obj, query=True, matrix=True, worldSpace=True)

        cmds.xform(obj, worldSpace=True, rotatePivot=point)
        cmds.xform(obj, worldSpace=True, scalePivot=point)

        after = cmds.xform(obj, query=True, matrix=True, worldSpace=True)
        dx = after[12] - before[12]
        dy = after[13] - before[13]
        dz = after[14] - before[14]

        if abs(dx) > 1e-9 or abs(dy) > 1e-9 or abs(dz) > 1e-9:
            cmds.move(-dx, -dy, -dz, obj, relative=True, worldSpace=True)

    def pivot_move_in_plane(self, direction):
        """沿当前平面把 pivot 推到包围盒对应方向的边缘

        :param direction: "left" / "right" / "up" / "down"
        """
        obj, bbox = self.pivot_get_bbox()
        if not obj:
            return

        xmin, ymin, zmin, xmax, ymax, zmax = bbox
        px, py, pz = cmds.xform(obj, query=True, worldSpace=True, rotatePivot=True)

        # 各平面下左右/上下方向对应的轴与包围盒边界
        axis_map = {
            "xy": {"lr_axis": 0, "ud_axis": 1, "lr_min": xmin, "lr_max": xmax, "ud_min": ymin, "ud_max": ymax},
            "zy": {"lr_axis": 2, "ud_axis": 1, "lr_min": zmin, "lr_max": zmax, "ud_min": ymin, "ud_max": ymax},
            "zx": {"lr_axis": 0, "ud_axis": 2, "lr_min": xmin, "lr_max": xmax, "ud_min": zmin, "ud_max": zmax},
        }
        cfg = axis_map[self.pivot_plane]

        new_rp = [px, py, pz]
        if direction == "left":
            new_rp[cfg["lr_axis"]] = cfg["lr_min"]
        elif direction == "right":
            new_rp[cfg["lr_axis"]] = cfg["lr_max"]
        elif direction == "up":
            new_rp[cfg["ud_axis"]] = cfg["ud_max"]
        elif direction == "down":
            new_rp[cfg["ud_axis"]] = cfg["ud_min"]

        cmds.undoInfo(openChunk=True, chunkName="Pivot To BBox Edge")
        try:
            self.pivot_set_pivot(obj, new_rp)
        finally:
            cmds.undoInfo(closeChunk=True)

    def pivot_set_plane(self, plane):
        """切换当前操作平面，并用颜色标出激活的平面按钮

        :param plane: "xy" / "zy" / "zx"
        """
        self.pivot_plane = plane
        for p, btn_name in self._pivot_plane_btns.items():
            active_color, inactive_color = self.PIVOT_PLANE_COLORS[p]
            cmds.button(btn_name, e=True,
                        bgc=(active_color if p == plane else inactive_color))

    # ------------------------------------------ naming ------------------------------------------

    TEX_EXTS = ('.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr')
    DEFAULT_TOKENS = (
        # key, label, default text, placeholder hint
        ('mesh_pre', u'Mesh 前缀', '', u'例如 SM_'),
        ('mesh_suf', u'Mesh 后缀', '', u'例如 _01'),
        ('shd_pre', u'材质前缀', '', u'例如 M_'),
        ('shd_suf', u'材质后缀', '', u'例如 _v01 或 _001'),
        ('tex_pre', u'贴图前缀', '', u'例如 T_'),
        ('col_suf', u'颜色后缀', '', u'例如 _color'),
        ('nml_suf', u'法线后缀', '', u'例如 _normal'),
    )

    # Column order: Select, Tris, UV set, Mesh name, Shader name,
    # Color map, Normal map, Delete

    _WINDOW = None


    def strip_ns(name):
        return name.split('|')[-1].rsplit(':', 1)[-1]


    def short_name(node):
        return node.split('|')[-1].rsplit(':', 1)[-1]


    def _token_list(token):
        """A token field may hold several alternatives: 'SM_, SK_'."""
        return [t.strip() for t in (token or '').split(',') if t.strip()]


    def _add_token(token):
        toks = LingJuGeometryTools._token_list(token)
        return toks[0] if toks else ''


    def _strip_token(name, token, suffix=False):
        """Remove a leading (or trailing) token, case-insensitive."""
        for t in LingJuGeometryTools._token_list(token):
            if suffix:
                if name.lower().endswith(t.lower()):
                    return name[: -len(t)]
            elif name.lower().startswith(t.lower()):
                return name[len(t):]
        return name


    def suffix_sequence(base, index):
        """Material suffix for the index-th shader of one mesh.

        A numeric tail counts up (_v01 -> _v02, _001 -> _002) and keeps its
        zero padding; a non-numeric base appends _01, _02 ...; an empty base
        returns an empty string — no auto-generated numeric suffix when the
        user left the suffix field blank.
        """
        base = base or ''
        if not base:
            return ''
        m = re.search(r'^(.*?)(\d+)$', base)
        if m:
            return m.group(1) + str(int(m.group(2)) + index).zfill(len(m.group(2)))
        return '%s_%02d' % (base, index + 1)


    def strip_suffix_sequence(name, base):
        """Remove a previously applied suffix sequence from a shader stem so
        numbering starts from the base again (_v01 / _001).

        If base is empty the user does not want any suffix numbering — leave
        the name completely untouched, including any trailing digits that were
        part of the original scene name."""
        base = base or ''
        if not base:
            return name
        m = re.match(r'^(.*?)(\d+)$', base)
        if m:
            pattern = re.escape(m.group(1)) + r'\d{%d,}$' % len(m.group(2))
        else:
            pattern = re.escape(base) + r'_\d+$'
        return re.sub(pattern, '', name)


    def mesh_stem(name, tokens):
        """Mesh short name minus its prefix/suffix tokens."""
        stem = LingJuGeometryTools._strip_token(LingJuGeometryTools.strip_ns(name), tokens['mesh_pre'])
        return LingJuGeometryTools._strip_token(stem, tokens['mesh_suf'], suffix=True)


    def shader_stem(blinn, tokens):
        """Blinn stem for renaming: own shader tokens first, then the mesh
        prefix (M_SM_Crate -> Crate)."""
        stem = LingJuGeometryTools._strip_token(LingJuGeometryTools.strip_ns(blinn), tokens['shd_pre'])
        stem = LingJuGeometryTools._strip_token(stem, tokens['shd_suf'], suffix=True)
        return LingJuGeometryTools._strip_token(stem, tokens['mesh_pre'])


    def shader_match_stems(blinn, mesh, tokens):
        """Ordered stems for texture matching, most specific first: the
        Blinn's own stem, then that stem with a trailing auto-number removed
        (multi-material meshes carry _01/_02 or _v02 style tails that shared
        textures do not), and finally the mesh stem as a fallback for scenes
        whose materials were never renamed after the mesh."""
        stem = LingJuGeometryTools.shader_stem(blinn, tokens)
        out = [stem]
        if LingJuGeometryTools._add_token(tokens['shd_suf']):
            base = LingJuGeometryTools.strip_suffix_sequence(
                stem, tokens['shd_suf'])
        else:
            base = re.sub(r'_\d+$', '', stem)
        if base != stem:
            out.append(base)
        out.append(LingJuGeometryTools.mesh_stem(mesh, tokens))
        return out


    def _attr_exists(node, attr):
        return cmds.attributeQuery(attr, node=node, exists=True)


    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None


    @contextlib.contextmanager
    def _undo_chunk(name='AssetManager'):
        cmds.undoInfo(openChunk=True, chunkName=name)
        try:
            yield
        finally:
            cmds.undoInfo(closeChunk=True)


    def _warn(msg):
        lj_warn('AssetManager: %s' % msg)


    def delete_unused_nodes():
        """Maya's own Delete Unused Nodes pass (Hypershade button). Returns the
        number of nodes removed."""
        try:
            return int(mel.eval('MLdeleteUnused()'))
        except Exception:
            return 0


    # ---------------------------------------------------------------------------
    # scene queries

    def scene_meshes():
        """All renderable polygon meshes as (transform, shape) long-name pairs.

        Intermediate objects are skipped: they are byproducts of modelling
        operations, never rendered, and giving them material only leaves a
        pile of shading nodes that nothing actually uses."""
        out = []
        for shape in cmds.ls(type='mesh', long=True) or []:
            if LingJuGeometryTools._safe(cmds.getAttr,
                    shape + '.intermediateObject'):
                continue
            parents = LingJuGeometryTools._safe(cmds.listRelatives, shape, parent=True,
                            fullPath=True) or []
            if parents:
                out.append((parents[0], shape))
        return out


    def _is_shading_engine(node):
        """True when node exists and is a shadingEngine."""
        return bool(node) and LingJuGeometryTools._safe(
            cmds.nodeType, node) == 'shadingEngine'


    def iog_group_count(shape):
        """Number of instObjGroups groups on a mesh shape. More than one means
        the mesh carries face-level (multi-material) assignments."""
        idx = LingJuGeometryTools._safe(cmds.getAttr, shape + '.iog',
                    multiIndices=True) or []
        return len(idx)


    def _iog_engines(shape):
        """Shading engines reached through the iog component groups of a shape."""
        out = []
        idx = LingJuGeometryTools._safe(cmds.getAttr, shape + '.iog',
                    multiIndices=True) or []
        for i in idx:
            for sg in (LingJuGeometryTools._safe(cmds.listConnections,
                    '%s.iog[%d]' % (shape, i), type='shadingEngine',
                    destination=True, source=False) or []):
                if sg not in out:
                    out.append(sg)
        return out


    def engines_for_shape(shape):
        """Shading engines of a mesh shape, without initialShadingGroup.

        Face-level assignments live on the shape's iog component groups and
        an object-level listSets call does not report them, so every source is
        merged: object-level sets, component-level sets, direct shadingEngine
        connections and the iog group lookup. Missing any of these used to
        make a multi-material mesh look like "no material" and get overwritten.
        """
        engs = []
        for src in (
            LingJuGeometryTools._safe(cmds.listSets, object=shape, type=1),
            LingJuGeometryTools._safe(cmds.listSets, object=shape, type=2),
            LingJuGeometryTools._safe(cmds.listConnections, shape,
                type='shadingEngine', destination=True, source=False),
            LingJuGeometryTools._iog_engines(shape),
        ):
            for e in src or []:
                if LingJuGeometryTools._is_shading_engine(e) and e not in engs:
                    engs.append(e)
        return [e for e in engs
                if LingJuGeometryTools.strip_ns(e) != 'initialShadingGroup']


    def shader_of_engine(engine):
        mats = LingJuGeometryTools._safe(cmds.listConnections, engine + '.surfaceShader',
                    source=True, destination=False) or []
        for m in mats:
            if cmds.nodeType(m) == 'blinn':
                return m
        return mats[0] if mats else None


    def uv_sets_of(shape):
        return cmds.polyUVSet(shape, query=True, allUVSets=True) or ['map1']


    def current_uv(shape):
        v = LingJuGeometryTools._safe(cmds.polyUVSet, shape, query=True, currentUVSet=True)
        if isinstance(v, (list, tuple)):
            return v[0] if v else 'map1'
        return v or 'map1'


    def tris_of(shape):
        try:
            return int(cmds.polyEvaluate(shape, triangle=True) or 0)
        except Exception:
            return 0


    def color_file_of(blinn):
        plugs = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.color',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'file':
                return node
        return ''


    def bump_of(blinn):
        plugs = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.normalCamera',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'bump2d':
                return node
        return ''


    def bump_file_of(bump):
        if not bump:
            return ''
        plugs = LingJuGeometryTools._safe(cmds.listConnections, bump + '.bumpValue',
                    source=True, destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            if cmds.nodeType(node) == 'file':
                return node
        return ''


    # ---------------------------------------------------------------------------
    # material slot capture / restore

    PLACE2D_ATTRS = ('coverage', 'translateFrame', 'rotateFrame', 'mirrorU',
                    'mirrorV', 'stagger', 'wrapU', 'wrapV', 'repeatUV',
                    'offset', 'rotateUV', 'noiseUV')


    def _place2d_of(fnode):
        srcs = LingJuGeometryTools._safe(cmds.listConnections, fnode + '.uvCoord',
                    source=True, destination=False, type='place2dTexture') or []
        return srcs[0] if srcs else ''


    def capture_place2d(fnode):
        place = LingJuGeometryTools._place2d_of(fnode)
        out = {}
        if not place:
            return out
        for attr in LingJuGeometryTools.PLACE2D_ATTRS:
            val = LingJuGeometryTools._safe(cmds.getAttr, place + '.' + attr)
            if val is not None:
                out[attr] = val
        return out


    def apply_attrs(node, values):
        for attr, val in (values or {}).items():
            if not node or not cmds.objExists(node):
                continue
            if not LingJuGeometryTools._attr_exists(node, attr):
                continue
            try:
                if isinstance(val, (list, tuple)):
                    cmds.setAttr(node + '.' + attr, *val)
                else:
                    cmds.setAttr(node + '.' + attr, val)
            except Exception:
                pass


    def capture_material_maps(blinn):
        """Remember a Blinn's color / normal input: file path plus place2d
        parameters, or the flat color when there is no color file."""
        color = color_value = normal = None
        fnode = LingJuGeometryTools.color_file_of(blinn)
        if fnode:
            color = {
                'path': LingJuGeometryTools._safe(
                    cmds.getAttr, fnode + '.fileTextureName') or '',
                'place': LingJuGeometryTools.capture_place2d(fnode)}
        else:
            val = LingJuGeometryTools._safe(cmds.getAttr, blinn + '.color')
            if val:
                color_value = tuple(val[0])
        bump = LingJuGeometryTools.bump_of(blinn)
        nnode = LingJuGeometryTools.bump_file_of(bump)
        if nnode:
            normal = {
                'path': LingJuGeometryTools._safe(
                    cmds.getAttr, nnode + '.fileTextureName') or '',
                'place': LingJuGeometryTools.capture_place2d(nnode),
                'depth': LingJuGeometryTools._safe(
                    cmds.getAttr, bump + '.bumpDepth')}
        return color, color_value, normal


    def restore_material_maps(blinn, slot, color_on, normal_on,
                              connect_alpha=False):
        if color_on:
            color = slot.get('color')
            if color and color.get('path'):
                fnode = LingJuGeometryTools.set_color_file(
                    blinn, color['path'],
                    LingJuGeometryTools._node_safe_name(color['path']),
                    connect_alpha=connect_alpha)
                LingJuGeometryTools.apply_attrs(
                    LingJuGeometryTools._place2d_of(fnode), color.get('place'))
            elif slot.get('color_value'):
                try:
                    cmds.setAttr(blinn + '.color', *slot['color_value'])
                except Exception:
                    pass
        if normal_on:
            normal = slot.get('normal')
            if normal and normal.get('path'):
                fnode, bump = LingJuGeometryTools.set_normal_file(
                    blinn, normal['path'],
                    LingJuGeometryTools._node_safe_name(normal['path']))
                LingJuGeometryTools.apply_attrs(
                    LingJuGeometryTools._place2d_of(fnode), normal.get('place'))
                if normal.get('depth') is not None:
                    LingJuGeometryTools._safe(cmds.setAttr,
                                            bump + '.bumpDepth', normal['depth'])


    # ---------------------------------------------------------------------------
    # scene mutations

    # Shader node types that may legitimately occupy a material name and are
    # safe to reclaim when nothing references them any more.
    SHADER_TYPES = frozenset((
        'lambert', 'blinn', 'phong', 'phongE', 'anisotropic',
        'rampShader', 'surfaceShader', 'useBackground', 'layeredShader',
        'shaderGlow', 'shadingMap', 'aiStandardSurface', 'standardSurface',
    ))


    def _take_name(node, name):
        """Rename node to name exactly. shadingNode appends the node type, so
        we rename afterwards. If the name is occupied by an orphan shader
        (no shading group using it), free it; otherwise fall back to a numeric
        suffix and keep the node alive."""
        try:
            return cmds.rename(node, name)
        except Exception:
            pass
        if cmds.objExists(name) and cmds.nodeType(name) in LingJuGeometryTools.SHADER_TYPES:
            sgs = LingJuGeometryTools._safe(cmds.listConnections,
                        name + '.outColor', destination=True, source=False,
                        type='shadingEngine') or []
            if not sgs:
                try:
                    cmds.delete(name)
                    return cmds.rename(node, name)
                except Exception:
                    pass
        i = 1
        while cmds.objExists('%s%d' % (name, i)):
            i += 1
        try:
            return cmds.rename(node, '%s%d' % (name, i))
        except Exception:
            return node


    def create_blinn(name):
        node = cmds.shadingNode('blinn', asShader=True, name=name)
        return LingJuGeometryTools._take_name(node, name)


    def create_engine_for(blinn):
        engine = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                        name=blinn + 'SG')
        cmds.connectAttr(blinn + '.outColor', engine + '.surfaceShader',
                        force=True)
        return engine


    def assign_engine(mesh, engine):
        cmds.sets(mesh, edit=True, forceElement=engine)


    def _node_safe_name(name):
        """File-node name from anything typed in a map field: folders and the
        extension are stripped so both D:\\tex\\T_Wood.png and T_Wood give
        T_Wood."""
        stem = name.replace('\\', '/').split('/')[-1]
        stem = os.path.splitext(stem)[0]
        return stem or 'file'


    def _same_path(a, b):
        return bool(a) and bool(b) and (
            os.path.normcase(os.path.normpath(a))
            == os.path.normcase(os.path.normpath(b)))


    def _make_file_node(name):
        stem = LingJuGeometryTools._node_safe_name(name)
        fnode = cmds.shadingNode('file', asTexture=True, name=stem)
        fnode = LingJuGeometryTools._take_name(fnode, stem)
        LingJuGeometryTools._ensure_place2d(fnode)
        return fnode


    PLACE2D_LINKS = (
        ('coverage', 'coverage'), ('translateFrame', 'translateFrame'),
        ('rotateFrame', 'rotateFrame'), ('mirrorU', 'mirrorU'),
        ('mirrorV', 'mirrorV'), ('stagger', 'stagger'),
        ('wrapU', 'wrapU'), ('wrapV', 'wrapV'), ('repeatUV', 'repeatUV'),
        ('offset', 'offset'), ('rotateUV', 'rotateUV'), ('noiseUV', 'noiseUV'),
        ('vertexUvOne', 'vertexUvOne'), ('vertexUvTwo', 'vertexUvTwo'),
        ('vertexUvThree', 'vertexUvThree'),
        ('vertexCameraOne', 'vertexCameraOne'),
        ('outUV', 'uvCoord'), ('outUvFilterSize', 'uvFilterSize'),
    )


    def _ensure_place2d(fnode):
        """Give a file node the standard place2dTexture wiring that Maya's own
        UI creates, if it does not have one yet."""
        srcs = LingJuGeometryTools._safe(cmds.listConnections, fnode + '.uvCoord',
                    source=True, destination=False) or []
        if srcs:
            return
        place_name = LingJuGeometryTools.short_name(fnode) + '_place2d'
        place = cmds.shadingNode('place2dTexture', asUtility=True, name=place_name)
        place = LingJuGeometryTools._take_name(place, place_name)
        for src, dst in LingJuGeometryTools.PLACE2D_LINKS:
            if LingJuGeometryTools._attr_exists(place, src) and LingJuGeometryTools._attr_exists(fnode, dst):
                cmds.connectAttr('%s.%s' % (place, src),
                                '%s.%s' % (fnode, dst), force=True)


    def _set_file_path(fnode, path, raw=False):
        cmds.setAttr(fnode + '.fileTextureName', path, type='string')
        if raw and LingJuGeometryTools._attr_exists(fnode, 'colorSpace'):
            cmds.setAttr(fnode + '.colorSpace', 'Raw', type='string')


    def set_transparency_link(blinn, fnode, enabled):
        """Match the material's transparency input to the checkbox: enabled
        wires file.outTransparency -> material.transparency, disabled cuts
        exactly that link. Only this material and this file node are touched;
        nodes are never deleted, and shaders without the attributes (ai etc.)
        are skipped. Called from every entry point that connects a color map."""
        if not fnode or not LingJuGeometryTools._attr_exists(
                fnode, 'outTransparency') or not LingJuGeometryTools._attr_exists(
                blinn, 'transparency'):
            return
        src = fnode + '.outTransparency'
        dest = blinn + '.transparency'
        linked = [d for d in LingJuGeometryTools._safe(
            cmds.listConnections, src, source=False, destination=True,
            plugs=True) or [] if d == dest or d in (
                dest + 'R', dest + 'G', dest + 'B')]
        if enabled:
            if not linked:
                cmds.connectAttr(src, dest, force=True)
        else:
            for plug in linked:
                try:
                    cmds.disconnectAttr(src, plug)
                except Exception:
                    pass


    def set_color_file(blinn, path, name, connect_alpha=False):
        """Different file name: the old feed is deleted (shared nodes kept) and
        a new file node named after the typed name is created. Same name: only
        the file path is updated. connect_alpha mirrors the checkbox through
        set_transparency_link (connect when on, cut when off)."""
        name = LingJuGeometryTools._node_safe_name(name)
        fnode = LingJuGeometryTools.color_file_of(blinn)
        if fnode and LingJuGeometryTools.short_name(fnode) == name:
            LingJuGeometryTools._ensure_place2d(fnode)
        else:
            # cut the old file's transparency feed first, otherwise it counts
            # as a user and _clear_plug_sources keeps the stale node
            LingJuGeometryTools.set_transparency_link(blinn, fnode, False)
            LingJuGeometryTools._clear_plug_sources(blinn + '.color')
            fnode = LingJuGeometryTools._make_file_node(name)
        LingJuGeometryTools._set_file_path(fnode, path)
        cmds.connectAttr(fnode + '.outColor', blinn + '.color', force=True)
        LingJuGeometryTools.set_transparency_link(
            blinn, fnode, bool(connect_alpha))
        return fnode


    def set_normal_file(blinn, path, name):
        """Connect a tangent-space normal map exactly like Maya's UI does:
        place2dTexture -> file (Raw) -> file.outAlpha -> bump2d.bumpValue with
        bumpInterp = 1 (Tangent Space Normals) -> bump2d.outNormal ->
        blinn.normalCamera. A different file name replaces the old file tree
        and renames the bump to match; the same name only updates the path."""
        name = LingJuGeometryTools._node_safe_name(name)
        bump = LingJuGeometryTools.bump_of(blinn)
        fnode = LingJuGeometryTools.bump_file_of(bump) if bump else ''
        if fnode and LingJuGeometryTools.short_name(fnode) == name:
            LingJuGeometryTools._ensure_place2d(fnode)
        else:
            if bump:
                LingJuGeometryTools._clear_plug_sources(bump + '.bumpValue')
            else:
                LingJuGeometryTools._clear_plug_sources(blinn + '.normalCamera')
            fnode = LingJuGeometryTools._make_file_node(name)
        if not bump:
            bump = cmds.shadingNode('bump2d', asUtility=True,
                                    name=name + '_bump')
            bump = LingJuGeometryTools._take_name(bump, name + '_bump')
            cmds.connectAttr(bump + '.outNormal', blinn + '.normalCamera',
                            force=True)
        elif LingJuGeometryTools.short_name(bump) != name + '_bump':
            bump = LingJuGeometryTools._take_name(bump, name + '_bump')
        if LingJuGeometryTools._attr_exists(bump, 'bumpInterp'):
            cmds.setAttr(bump + '.bumpInterp', 1)
        LingJuGeometryTools._set_file_path(fnode, path, raw=True)
        cmds.connectAttr(fnode + '.outAlpha', bump + '.bumpValue', force=True)
        return fnode, bump


    def _has_other_users(node):
        dests = LingJuGeometryTools._safe(cmds.listConnections, node, destination=True,
                    source=False, plugs=True) or []
        for d in dests:
            if d.split('.')[0] != node:
                return True
        return False


    def _delete_file_node_tree(fnode):
        """Delete a file node plus its place2dTexture when nothing else
        uses them."""
        if not fnode or not cmds.objExists(fnode):
            return
        places = LingJuGeometryTools._safe(cmds.listConnections, fnode, source=True,
                    destination=False, type='place2dTexture') or []
        if not LingJuGeometryTools._has_other_users(fnode):
            cmds.delete(fnode)
        for p in places:
            if cmds.objExists(p) and not LingJuGeometryTools._has_other_users(p):
                cmds.delete(p)


    def _clear_plug_sources(plug):
        """Disconnect every source feeding plug and delete the freed nodes:
        file trees (file + place2dTexture) via LingJuGeometryTools._delete_file_node_tree, any
        other node nothing else uses. Shared nodes are always kept."""
        plugs = LingJuGeometryTools._safe(cmds.listConnections, plug, source=True,
                    destination=False, plugs=True) or []
        for p in plugs:
            node = p.split('.')[0]
            try:
                cmds.disconnectAttr(p, plug)
            except Exception:
                pass
            if not cmds.objExists(node):
                continue
            if cmds.nodeType(node) == 'file':
                LingJuGeometryTools._delete_file_node_tree(node)
            elif not LingJuGeometryTools._has_other_users(node):
                try:
                    cmds.delete(node)
                except Exception:
                    pass


    def disconnect_color(blinn):
        """Empty color field: remove everything feeding blinn.color — the file
        node with its place2dTexture, or any other texture source. The file's
        transparency feed is cut too so the node is not kept alive."""
        LingJuGeometryTools.set_transparency_link(
            blinn, LingJuGeometryTools.color_file_of(blinn), False)
        LingJuGeometryTools._clear_plug_sources(blinn + '.color')


    def disconnect_normal(blinn):
        """Empty normal field: remove bump2d plus the normal file node with its
        place2dTexture, and any stray direct feed on normalCamera."""
        bump = LingJuGeometryTools.bump_of(blinn)
        fnode = LingJuGeometryTools.bump_file_of(bump)
        if bump:
            try:
                cmds.disconnectAttr(bump + '.outNormal',
                                    blinn + '.normalCamera')
            except Exception:
                pass
            if cmds.objExists(bump) and not LingJuGeometryTools._has_other_users(bump):
                try:
                    cmds.delete(bump)
                except Exception:
                    pass
        LingJuGeometryTools._clear_plug_sources(blinn + '.normalCamera')
        LingJuGeometryTools._delete_file_node_tree(fnode)


    def delete_shader_tree(blinn, mesh_transform):
        """Delete a Blinn and its file / bump2d nodes. Shading engines are only
        deleted when no other mesh still uses them."""
        engines = LingJuGeometryTools._safe(cmds.listConnections, blinn + '.outColor',
                        destination=True, type='shadingEngine') or []
        bump = LingJuGeometryTools.bump_of(blinn)
        color_f = LingJuGeometryTools.color_file_of(blinn)
        nml_f = LingJuGeometryTools.bump_file_of(bump)
        try:
            cmds.delete(blinn)
        except Exception:
            pass
        mesh_short = LingJuGeometryTools.short_name(mesh_transform)
        for e in engines:
            if not cmds.objExists(e):
                continue
            members = LingJuGeometryTools._safe(cmds.sets, e, query=True) or []
            others = [m for m in members
                    if LingJuGeometryTools.short_name(m).split('.')[0] != mesh_short]
            if not others:
                try:
                    cmds.delete(e)
                except Exception:
                    pass
        for node in (bump, color_f, nml_f):
            if node and cmds.objExists(node) and not LingJuGeometryTools._has_other_users(node):
                try:
                    cmds.delete(node)
                except Exception:
                    pass


    # ---------------------------------------------------------------------------
    # texture matching / namespaces

    def _map_key(stem, tokens, suffix_key):
        """Case-insensitive match key for a texture file stem: texture prefix
        and the given map-type suffix (tokens[suffix_key]) stripped."""
        stem = LingJuGeometryTools._strip_token(stem, tokens['tex_pre'])
        stem = LingJuGeometryTools._strip_token(stem, tokens[suffix_key], suffix=True)
        return stem.lower()


    def find_texture_for(stems, tokens, files, suffix_key):
        """First file matching the stems in priority order. Stems are tried
        most specific first (exact numbered shader stem before the shared
        base, mesh stem last), so a per-slot texture like T_Crate_03_D wins
        over the shared T_Crate_D. Each file's stem is compared minus the
        texture prefix and the map-type suffix tokens[suffix_key]; matching
        is case-insensitive."""
        keyed = []
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            keyed.append(
                (LingJuGeometryTools._map_key(stem, tokens, suffix_key), f))
        for s in stems:
            s = LingJuGeometryTools.strip_ns(s)
            wanted = (s.lower(),
                      LingJuGeometryTools._strip_token(
                          s, tokens['mesh_pre']).lower())
            for key, f in keyed:
                if key in wanted:
                    return f
        return None


    def clear_namespaces():
        """Remove every namespace, merging contents into root."""
        removed = 0
        for _ in range(10):
            try:
                nss = cmds.namespaceInfo(listOnlyNamespaces=True) or []
            except Exception:
                break
            nss = [n for n in nss if n not in ('UI', 'shared')]
            if not nss:
                break
            progress = False
            for ns in nss:
                try:
                    cmds.namespace(removeNamespace=ns,
                                mergeNamespaceWithRoot=True)
                    removed += 1
                    progress = True
                except Exception:
                    pass
            if not progress:
                break
        return removed


    # ---------------------------------------------------------------------------
    # UI

    def _center_label(text):
        lab = QtWidgets.QLabel(text)
        lab.setAlignment(QtCore.Qt.AlignCenter)
        return lab


    def _image_drop_text(md):
        """Image file candidate from drag mime data: a local file URL (OS file
        explorer) or a plain-text file name. Returns None when the drag carries
        no usable image file."""
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in LingJuGeometryTools.TEX_EXTS:
                    return path
            return None
        if md.hasText():
            text = md.text().strip()
            ext = os.path.splitext(text)[1].lower()
            if not ext or ext in LingJuGeometryTools.TEX_EXTS:
                return text
        return None


    class _MapDropLineEdit(QtWidgets.QLineEdit):
        """Map field that accepts a dragged image file; the drop connects the
        texture right away.  Uses a plain on_dropped callback instead of
        QtCore.Signal (see _DraggableTabBar for the pyd/Maya2023 reason)."""

        def __init__(self, text='', parent=None):
            QtWidgets.QLineEdit.__init__(self, text, parent)
            self.setAcceptDrops(True)
            self.on_dropped = None     # 拖入回调: f(path_text)

        def dragEnterEvent(self, event):
            if LingJuGeometryTools._image_drop_text(event.mimeData()):
                event.acceptProposedAction()

        def dropEvent(self, event):
            text = LingJuGeometryTools._image_drop_text(event.mimeData())
            if text and self.on_dropped is not None:
                self.on_dropped(text)


    class AssetManagerUI(QtWidgets.QWidget):

        def __init__(self, parent=None):
            QtWidgets.QWidget.__init__(self, parent)
            # Floating tool window parented to the Maya main window, so it
            # always stacks above Maya like the toolset workspace control
            # instead of sinking behind it.
            self.setWindowFlags(QtCore.Qt.Tool)
            self.setWindowTitle(u'零距命名材质管理')
            self.setObjectName('PolygonAssetManagerUI')
            # All UI text uses Microsoft YaHei so Chinese renders consistently;
            # child widgets and dialogs inherit it from the window.
            self.setFont(QtGui.QFont('Microsoft YaHei', 9))
            self.resize(1240, 560)
            self.rows = []
            self.token_edits = {}
            # Material slots remembered when a mesh's materials are deleted:
            # {shape: [{'faces': [...], 'whole': bool, 'color': ...,
            #           'color_value': ..., 'normal': ...}, ...]}.
            self._deleted_slots = {}
            self._edits = {'shd': {}, 'color': {}, 'normal': {}}
            self._fields = []
            self._select_buttons = {}
            self._build_ui()
            # Read-only on open: the window must never touch materials that
            # are already wired up. 更新场景 is the pass that may create /
            # convert them.
            self.refresh_scene(mutate=False)

        # ---------------- construction ----------------

        def _build_ui(self):
            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)

            # Title bar
            title = QtWidgets.QHBoxLayout()
            tlab = QtWidgets.QLabel(u'零距命名材质管理')
            tlab.setStyleSheet('font-weight: 500;')
            title.addWidget(tlab)
            title.addStretch(1)
            title.addWidget(QtWidgets.QLabel(u'vieport Maya · Blinn'))
            root.addLayout(title)

            # Texture folder: label above, then input + Browse
            folder = QtWidgets.QVBoxLayout()
            folder.setSpacing(4)
            flab = QtWidgets.QLabel(u'贴图文件夹')
            flab.setStyleSheet('color: #c2c2c2c; font-size: 12px;')
            folder.addWidget(flab)
            folder_row = QtWidgets.QHBoxLayout()
            self.path_edit = QtWidgets.QLineEdit(r'')
            self.path_edit.editingFinished.connect(self._update_format_hint)
            folder_row.addWidget(self.path_edit, 1)
            self.fmt_hint = QtWidgets.QLabel('')
            self.fmt_hint.setStyleSheet('color: #53892c; font-size: 12px;')
            folder_row.addWidget(self.fmt_hint)
            browse = QtWidgets.QPushButton(u'浏览')
            browse.clicked.connect(self._browse_folder)
            folder_row.addWidget(browse)
            folder.addLayout(folder_row)
            root.addLayout(folder)
            self._update_format_hint()

            # 场景材质球：与烘焙页共用读取/刷新/赋予逻辑
            material_row = QtWidgets.QHBoxLayout()
            material_row.addWidget(QtWidgets.QLabel(u'场景材质球'))
            self.material_combo = QtWidgets.QComboBox()
            self.material_combo.setMinimumWidth(160)
            material_row.addWidget(self.material_combo, 1)
            refresh_mat = QtWidgets.QPushButton(u'刷新')
            refresh_mat.setToolTip(u'等同重开窗口：只读重读模型面数与材质对应；'
                                   u'场景挪动后在贴图文件夹按同名文件修复丢失的贴图路径')
            refresh_mat.clicked.connect(self.on_refresh_materials)
            material_row.addWidget(refresh_mat)
            assign_btn = QtWidgets.QPushButton(u'赋予选中物件 / 面')
            assign_btn.setToolTip(u'选中面时逐面赋予；只选物件时按整物体赋予')
            assign_btn.clicked.connect(self.on_assign_material)
            material_row.addWidget(assign_btn)
            material_row.addSpacing(16)
            material_row.addWidget(QtWidgets.QLabel(u'材质平面'))
            self.mat_count = QtWidgets.QSpinBox()
            self.mat_count.setRange(1, 20)
            self.mat_count.setValue(3)
            self.mat_count.setSuffix(u' 个材质')
            self.mat_count.setToolTip(u'生成的平面面数与材质数相同，逐面赋予一个材质球')
            material_row.addWidget(self.mat_count)
            self.mat_type = QtWidgets.QComboBox()
            self.mat_type.addItems(['lambert', 'blinn'])
            material_row.addWidget(self.mat_type)
            make_btn = QtWidgets.QPushButton(u'生成')
            make_btn.setToolTip(u'生成一个多材质平面：每个面赋予一个默认命名的新材质球')
            make_btn.clicked.connect(self.on_create_test_plane)
            material_row.addWidget(make_btn)
            material_row.addStretch(1)
            root.addLayout(material_row)
            m2m_scene_refresh_material_combo(self.material_combo)

            btn_row = QtWidgets.QHBoxLayout()
            refresh_btn = QtWidgets.QPushButton(u'更新场景')
            # clicked(bool) would land in refresh_scene's mutate argument, so
            # bind it explicitly to keep this a mutating pass.
            refresh_btn.clicked.connect(lambda: self.refresh_scene(mutate=True))
            refresh_btn.setStyleSheet(
                'QPushButton { background-color: #375b1d; color: #FFFFFF; }'
                'QPushButton:hover { background-color: #53892c; }'
                'QPushButton:pressed { background-color: #1c2e0f; }')
            btn_row.addWidget(refresh_btn)
            for label, slot in ((u'应用命名', self.on_apply_names),
                                (u'材质命名匹配模型', self.on_match_shaders),
                                (u'一键添加颜色贴图', self.on_assign_colors),
                                (u'一键连接法线', self.on_assign_normals)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                btn_row.addWidget(b)
            self.alpha_cb = QtWidgets.QCheckBox(u'颜色贴图连 Transparency')
            self.alpha_cb.setToolTip(
                u'勾选后，连接颜色贴图时同时把贴图节点的 outTransparency '
                u'连接到材质的 transparency；取消勾选后，各操作（含刷新）'
                u'会断开该颜色贴图节点到材质的 transparency 连线')
            btn_row.addWidget(self.alpha_cb)
            btn_row.addStretch(1)
            root.addLayout(btn_row)

            # Restore scope for multi-material meshes: 更新场景 rebuilds the
            # recorded material slots; tick these to bring the maps back too.
            restore_row = QtWidgets.QHBoxLayout()
            restore_row.addWidget(QtWidgets.QLabel(u'多维材质恢复范围:'))
            self.restore_color_cb = QtWidgets.QCheckBox(u'多维材质：恢复颜色贴图')
            self.restore_color_cb.setToolTip(
                u'更新场景时，为多材质 mesh 重建的材质球重新连接颜色贴图')
            restore_row.addWidget(self.restore_color_cb)
            self.restore_normal_cb = QtWidgets.QCheckBox(u'多维材质：恢复法线贴图')
            self.restore_normal_cb.setToolTip(
                u'更新场景时，为多材质 mesh 重建的材质球重新连接法线贴图')
            restore_row.addWidget(self.restore_normal_cb)
            restore_row.addStretch(1)
            root.addLayout(restore_row)

            # Second button row: scene cleanup tools
            btn_row2 = QtWidgets.QHBoxLayout()
            for label, slot in ((u'清除命名空间', self.on_clear_namespaces),
                                (u'删除未用节点', self.on_delete_unused)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                btn_row2.addWidget(b)
            btn_row2.addStretch(1)
            root.addLayout(btn_row2)

            # UV batch rename: renames the current UV set on the selected
            # meshes, or on every listed mesh when nothing is selected
            uv_row = QtWidgets.QHBoxLayout()
            uv_row.addWidget(QtWidgets.QLabel(u'重命名 UV 集:'))
            self.uv_batch = QtWidgets.QLineEdit('map1')
            self.uv_batch.setPlaceholderText('map1')
            self.uv_batch.setFixedWidth(160)
            uv_row.addWidget(self.uv_batch)
            uv_btn = QtWidgets.QPushButton(u'重命名 UV 集')
            uv_btn.clicked.connect(self.on_rename_uv_sets)
            uv_row.addWidget(uv_btn)
            uv_del_btn = QtWidgets.QPushButton(u'删除当前 UV 集')
            uv_del_btn.clicked.connect(self.on_delete_uv_sets)
            uv_row.addWidget(uv_del_btn)
            uv_row.addStretch(1)
            root.addLayout(uv_row)

            # Scrollable table area: header + token row + data rows
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            scroll.setFrameShape(QtWidgets.QFrame.StyledPanel)
            root.addWidget(scroll, 1)

            self.scroll_content = QtWidgets.QWidget()
            self.scroll_content.setMinimumWidth(1180)
            scroll.setWidget(self.scroll_content)

            self.rows_layout = QtWidgets.QVBoxLayout(self.scroll_content)
            self.rows_layout.setContentsMargins(0, 0, 0, 0)
            self.rows_layout.setSpacing(0)
            
            self.rows_layout.addWidget(self._make_token_row())
            self.rows_layout.addWidget(self._make_header_row())

            self.data_container = QtWidgets.QWidget()
            self.data_layout = QtWidgets.QVBoxLayout(self.data_container)
            self.data_layout.setContentsMargins(0, 0, 0, 0)
            self.data_layout.setSpacing(0)
            self.rows_layout.addWidget(self.data_container)
            self.rows_layout.addStretch(1)

            # Bottom status bar: all messages render in dark yellow
            self.status = QtWidgets.QLabel('')
            self.status.setStyleSheet('color: #9e7309;')
            root.addWidget(self.status)

        def _make_header_row(self):
            """Header row matching the canvas column widths."""
            w = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.setSpacing(8)

            self.select_all_btn = QtWidgets.QPushButton(u'全选')
            self.select_all_btn.setFixedWidth(50)
            self.select_all_btn.clicked.connect(self.on_toggle_select_all)
            lay.addWidget(self.select_all_btn)

            headers = (
                (u'面数', 50, False),
                (u'UV 集', 60, False),
                (u'Mesh 名称', 60, True),
                (u'材质名称', 60, True),
                (u'颜色贴图', 60, True),
                (u'法线贴图', 60, True),
            )
            for text, width, stretch in headers:
                lab = QtWidgets.QLabel(text)
                lab.setAlignment(QtCore.Qt.AlignCenter)
                lab.setStyleSheet(
                    'font-family: "Microsoft YaHei", sans-serif; font-weight: bold; color: #909090; font-size: 9px;'
                    'font-size: 16px;')
                if stretch:
                    lab.setMinimumWidth(width)
                    lay.addWidget(lab, 1)
                else:
                    lab.setFixedWidth(width)
                    lay.addWidget(lab)

            self.delete_all_btn = QtWidgets.QPushButton(u'全部删除')
            self.delete_all_btn.setFixedWidth(60)
            self.delete_all_btn.setStyleSheet(
                'QPushButton { background-color: #630000; color: #FFFFFF; }'
                'QPushButton:hover { background-color: #8d0000; }'
                'QPushButton:pressed { background-color: #470000; }')
            self.delete_all_btn.clicked.connect(self.on_delete_all)
            lay.addWidget(self.delete_all_btn)
            return w

        def _make_token_row(self):
            """Prefix/suffix token row sitting under the header"""
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(8, 8, 8, 8)
            v.setSpacing(8)

            grid = QtWidgets.QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(4)
            
            for col, (key, label, default, placeholder) in enumerate(LingJuGeometryTools.DEFAULT_TOKENS):
                grid.addWidget(QtWidgets.QLabel(label), 0, col)
                edit = QtWidgets.QLineEdit(default)
                edit.setPlaceholderText(placeholder)
                self.token_edits[key] = edit
                grid.addWidget(edit, 1, col)
            v.addLayout(grid)
            return w

        # ---------------- data ----------------

        def _tokens(self):
            return dict((k, e.text().strip()) for k, e in self.token_edits.items())

        def _status(self, text):
            self.status.setText(text)

        def _alpha_checked(self):
            return bool(self.alpha_cb.isChecked())

        def _default_mesh_name(self, mesh, tokens):
            """Tokenized mesh name; never doubles the prefix (SM_Crate stays
            SM_Crate even when it was spelled sm_crate)."""
            return (LingJuGeometryTools._add_token(tokens['mesh_pre']) + LingJuGeometryTools.mesh_stem(mesh, tokens)
                    + LingJuGeometryTools._add_token(tokens['mesh_suf']))

        def _default_shader_name(self, blinn, tokens, seq=None):
            """Tokenized shader name built from the Blinn stem with the mesh
            prefix removed (SM_Crate_01 / M_SM_Crate_01 -> M_Crate_01).

            seq numbers the shaders of a multi-material mesh, but only when
            the user actually filled in a shd_suf token — if the suffix field
            is blank we leave any original trailing digits alone and do not
            synthesize new ones."""
            stem = LingJuGeometryTools.shader_stem(blinn, tokens)
            suffix = LingJuGeometryTools._add_token(tokens['shd_suf'])
            if not suffix or seq is None:
                return (LingJuGeometryTools._add_token(tokens['shd_pre'])
                        + stem + suffix)
            stem = LingJuGeometryTools.strip_suffix_sequence(stem, suffix)
            return (LingJuGeometryTools._add_token(tokens['shd_pre']) + stem
                    + LingJuGeometryTools.suffix_sequence(suffix, seq))

        def refresh_scene(self, mutate=True):
            """Rebuild rows from the scene.

            mutate=False is what the window uses when it first opens: the pass
            is read-only, so no Blinn is created, no material is converted and
            no shading assignment is touched — opening the manager can never
            disturb materials that are already set up. The 更新场景 button
            calls it with mutate=True, which is what may create / convert
            materials and rebuild recorded material slots."""
            sel = set(cmds.ls(selection=True, long=True) or [])
            tokens = self._tokens()
            rows = []
            restored = pending = missing = 0
            self._prune_deleted_slots()
            with LingJuGeometryTools._undo_chunk('AssetManagerRefresh'):
                for transform, shape in LingJuGeometryTools.scene_meshes():
                    engines = LingJuGeometryTools.engines_for_shape(shape)
                    record = self._deleted_slots.get(shape)

                    def add_row(engine, mat):
                        rows.append({
                            'mesh': transform,
                            'shape': shape,
                            'engine': engine,
                            'blinn': mat,
                            'uv_sets': LingJuGeometryTools.uv_sets_of(shape),
                            'uv': LingJuGeometryTools.current_uv(shape),
                            'tris': LingJuGeometryTools.tris_of(shape),
                            'selected': transform in sel,
                        })

                    if not mutate:
                        # Read-only pass: list what already exists, change
                        # nothing in the scene.
                        if not engines:
                            missing += 1
                            continue
                        for engine in engines:
                            mat = LingJuGeometryTools.shader_of_engine(engine)
                            if mat is None:
                                missing += 1
                                continue
                            add_row(engine, mat)
                        continue

                    if record is not None and not engines:
                        # Trigger A: rebuild the recorded material slots on the
                        # mesh and clear the record right away.
                        engines = self._restore_material_slots(
                            transform, shape, tokens,
                            record['slots'], record.get('covered', 0))
                        self._deleted_slots.pop(shape, None)
                        restored += len(engines)
                    elif record is not None:
                        # Mesh already has material: keep the record for later.
                        pending += 1
                    elif not engines:
                        if LingJuGeometryTools.iog_group_count(shape) > 1:
                            # Face-level assignments exist but no engine could
                            # be resolved: never overwrite them with a single
                            # material, just skip this mesh.
                            missing += 1
                            LingJuGeometryTools._warn(
                                u'%s 有面级材质但引擎读取失败，已跳过'
                                % LingJuGeometryTools.short_name(transform))
                            continue
                        blinn = LingJuGeometryTools.create_blinn(
                            LingJuGeometryTools._add_token(tokens['shd_pre'])
                            + LingJuGeometryTools.mesh_stem(transform, tokens)
                            + LingJuGeometryTools._add_token(tokens['shd_suf']))
                        engine = LingJuGeometryTools.create_engine_for(blinn)
                        # Assign to the shape, never to the transform: one
                        # transform can carry several shapes, and assigning at
                        # transform level overwrites the material that the
                        # other shapes already had.
                        LingJuGeometryTools.assign_engine(shape, engine)
                        engines = [engine]
                    for engine in engines:
                        mat = LingJuGeometryTools.shader_of_engine(engine)
                        if mat is None:
                            mat = LingJuGeometryTools.create_blinn(
                                LingJuGeometryTools._add_token(tokens['shd_pre'])
                                + LingJuGeometryTools.mesh_stem(transform, tokens)
                                + LingJuGeometryTools._add_token(tokens['shd_suf']))
                            cmds.connectAttr(mat + '.outColor',
                                            engine + '.surfaceShader',
                                            force=True)
                        # Existing materials keep their own node type. Converting
                        # them to Blinn used to silently change how they look
                        # (a lambert gains specular), which is the "材质全乱"
                        # that was reported.
                        add_row(engine, mat)
            self.rows = rows
            self._populate_rows()
            note = ''
            if restored:
                note += u' · 恢复 %d 个材质位置' % restored
            if pending:
                note += u' · %d 个 mesh 已有材质，位置登记保留' % pending
            if missing:
                note += u' · %d 个 mesh 未读取到材质' % missing
            self._status(u'%d 个 mesh · %d 个材质 · %d 行%s' % (
                len(set(r['mesh'] for r in rows)),
                len(set(r['blinn'] for r in rows)), len(rows), note))

        def _populate_rows(self):
            """Rebuild the data row widgets under the header / token strip.
            Mesh-scoped controls appear only on the first row of each mesh;
            shader-scoped fields appear on every row."""
            tokens = self._tokens()
            while self.data_layout.count():
                item = self.data_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._edits = {'shd': {}, 'color': {}, 'normal': {}}
            self._fields = []
            self._select_buttons = {}
            first_of_mesh = {}
            users = {}
            mesh_blinns = {}
            for idx, r in enumerate(self.rows):
                # Keep the ROW index of a mesh's first occurrence: the render
                # loop below compares it against enumerate's i, so storing
                # len(first_of_mesh) here would shift once any mesh spans
                # more than one row and mislabel every later row as 同一 mesh.
                first_of_mesh.setdefault(r['mesh'], idx)
                users[r['blinn']] = users.get(r['blinn'], 0) + 1
                blinns = mesh_blinns.setdefault(r['mesh'], [])
                if r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            # One model with several materials: number each shader's suffix
            # (M_Crate_v01 / _v02 ... or _01 / _02 when shd_suf is empty).
            shader_seq = {}
            for blinns in mesh_blinns.values():
                if len(blinns) < 2:
                    continue
                for i, blinn in enumerate(blinns):
                    shader_seq.setdefault(blinn, i)

            for i, r in enumerate(self.rows):
                r['index'] = i
                first = first_of_mesh[r['mesh']] == i
                row_w = QtWidgets.QWidget()
                lay = QtWidgets.QHBoxLayout(row_w)
                lay.setContentsMargins(1, 1, 8, 8)
                lay.setSpacing(8)

                if first:
                    btn = QtWidgets.QPushButton(
                        u'取消选择' if r['selected'] else u'选择')
                    btn.setFixedWidth(50)
                    btn.clicked.connect(
                        lambda _=False, row=r: self.on_toggle_select(row))
                    lay.addWidget(btn)
                    self._select_buttons[r['mesh']] = btn

                    tri = LingJuGeometryTools._center_label(str(r['tris']))
                    tri.setFixedWidth(64)
                    lay.addWidget(tri)

                    combo = QtWidgets.QComboBox()
                    combo.addItems(r['uv_sets'])
                    if r['uv'] in r['uv_sets']:
                        combo.setCurrentIndex(r['uv_sets'].index(r['uv']))
                    combo.currentIndexChanged.connect(
                        lambda _=0, row=r, c=combo: self.on_uv_change(row, c))
                    combo.setFixedWidth(60)
                    lay.addWidget(combo)

                    mesh_edit = QtWidgets.QLineEdit(
                        r.get('mesh_edit')
                        or self._default_mesh_name(r['mesh'], tokens))
                    mesh_edit.editingFinished.connect(
                        lambda e=mesh_edit, row=r: self._store_mesh(e, row))
                    self._fields.append((mesh_edit, r, 'mesh_edit'))
                    mesh_edit.setMinimumWidth(90)
                    lay.addWidget(mesh_edit, 1)

                    dele = QtWidgets.QPushButton(u'删除')
                    dele.setFixedWidth(60)
                    dele.clicked.connect(
                        lambda _=False, row=r: self.on_delete_mesh(row))
                    lay.addWidget(dele)
                else:
                    sm = LingJuGeometryTools._center_label(u'同一 mesh')
                    sm.setFixedWidth(96)
                    lay.addWidget(sm)

                    dash = LingJuGeometryTools._center_label(u'—')
                    dash.setFixedWidth(50)
                    lay.addWidget(dash)

                    uv_lab = LingJuGeometryTools._center_label(r['uv'])
                    uv_lab.setFixedWidth(80)
                    lay.addWidget(uv_lab)

                    mesh_lab = LingJuGeometryTools._center_label(LingJuGeometryTools.strip_ns(r['mesh']))
                    mesh_lab.setMinimumWidth(80)
                    lay.addWidget(mesh_lab, 1)

                    dash_del = LingJuGeometryTools._center_label(u'—')
                    dash_del.setFixedWidth(60)
                    lay.addWidget(dash_del)

                # shader-scoped widgets on every row; insert between the mesh
                # column and the Delete column
                shd_edit = QtWidgets.QLineEdit(
                    r.get('shd_edit')
                    or self._default_shader_name(r['blinn'], tokens,
                                                 shader_seq.get(r['blinn'])))
                if users[r['blinn']] > 1:
                    shd_edit.setToolTip(
                        u'共享 · %d 个 mesh' % users[r['blinn']])
                shd_edit.editingFinished.connect(
                    lambda e=shd_edit, row=r: self._store_shader(e, row))
                self._fields.append((shd_edit, r, 'shd_edit'))
                shd_edit.setMinimumWidth(110)
                lay.insertWidget(4, shd_edit, 1)
                self._edits['shd'].setdefault(r['blinn'], []).append(shd_edit)

                color_edit = LingJuGeometryTools._MapDropLineEdit(
                    r['color_edit'] if 'color_edit' in r
                    else LingJuGeometryTools.short_name(LingJuGeometryTools.color_file_of(r['blinn'])))
                color_edit.setPlaceholderText(u'无')
                color_edit.setToolTip(
                    u'清空此字段将立即删除颜色 file 节点及其 place2dTexture；'
                    u'可拖入图片直接连接')
                color_edit.editingFinished.connect(
                    lambda e=color_edit, row=r: self._store_map(e, row, 'color'))
                color_edit.on_dropped = lambda fname, row=r: self._assign_dropped_map(
                    row, 'color', fname)
                self._fields.append((color_edit, r, 'color_edit'))
                color_edit.setMinimumWidth(110)
                lay.insertWidget(5, color_edit, 1)
                self._edits['color'].setdefault(r['blinn'], []).append(color_edit)

                normal_edit = LingJuGeometryTools._MapDropLineEdit(
                    r['normal_edit'] if 'normal_edit' in r
                    else LingJuGeometryTools.short_name(LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(r['blinn']))))
                normal_edit.setPlaceholderText(u'无')
                if not normal_edit.text():
                    normal_edit.setToolTip(
                        u'清空此字段将立即删除 bump2d 及法线 file 节点；'
                        u'可拖入图片直接连接')
                normal_edit.editingFinished.connect(
                    lambda e=normal_edit, row=r: self._store_map(
                        e, row, 'normal'))
                normal_edit.on_dropped = lambda fname, row=r: self._assign_dropped_map(
                    row, 'normal', fname)
                self._fields.append((normal_edit, r, 'normal_edit'))
                normal_edit.setMinimumWidth(110)
                lay.insertWidget(6, normal_edit, 1)
                self._edits['normal'].setdefault(r['blinn'], []).append(
                    normal_edit)

                self.data_layout.addWidget(row_w)
            self._update_select_all_btn()

        # ---------------- field storage (shared Blinn sync) ----------------

        def _sync_field_texts(self):
            """Pull the live text of every field widget into its row dict so
            actions pick up edits that were typed but never committed with
            Enter / focus loss."""
            for edit, row, key in self._fields:
                row[key] = edit.text()

        def _store_mesh(self, edit, row):
            row['mesh_edit'] = edit.text()

        def _store_shader(self, edit, row):
            text = edit.text()
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r['shd_edit'] = text
            for e in self._edits['shd'].get(row['blinn'], ()):
                if e is not edit:
                    e.blockSignals(True)
                    e.setText(text)
                    e.blockSignals(False)

        def _store_map(self, edit, row, kind):
            text = edit.text()
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r[kind + '_edit'] = text
            for e in self._edits[kind].get(row['blinn'], ()):
                if e is not edit:
                    e.blockSignals(True)
                    e.setText(text)
                    e.blockSignals(False)
            # Clearing the field deletes the connected node tree right away.
            if not text.strip():
                self._clear_map_nodes(row['blinn'], kind)

        def _clear_map_nodes(self, blinn, kind):
            """Delete the node tree behind a cleared color/normal field:
            file + place2dTexture (+ bump2d for normal). Shared Blinns are
            handled once; other rows referencing the same nodes refresh."""
            try:
                if kind == 'normal':
                    LingJuGeometryTools.disconnect_normal(blinn)
                else:
                    LingJuGeometryTools.disconnect_color(blinn)
            except Exception as exc:
                LingJuGeometryTools._warn(u'%s 清空失败（%s）: %s'
                    % (kind, LingJuGeometryTools.short_name(blinn), exc))
                return
            self._status(u'%s 贴图节点已从 %s 删除'
                        % (kind, LingJuGeometryTools.short_name(blinn)))

        # ---------------- per-cell actions ----------------

        def _engine_faces(self, engine, mesh):
            """Faces of mesh covered by one shading engine, as component
            suffixes (f[0:5]); whole=True when the object is assigned as-is.
            Members may come back short, shape-based or transform-based, so
            every one is normalized to long names before matching."""
            members = LingJuGeometryTools._safe(
                cmds.sets, engine, query=True) or []
            faces, whole = [], False
            for m in members:
                for long_m in (LingJuGeometryTools._safe(
                        cmds.ls, m, long=True) or []):
                    node, comp = (long_m.split('.', 1) + [''])[:2] \
                        if '.' in long_m else (long_m, '')
                    if node != mesh:
                        parents = LingJuGeometryTools._safe(
                            cmds.listRelatives, node, parent=True,
                            fullPath=True) or []
                        if not parents or parents[0] != mesh:
                            continue
                    if comp:
                        faces.append(comp)
                    else:
                        whole = True
            return faces, whole

        def _register_material_slots(self, mesh):
            """Remember which faces each material of a mesh was assigned to
            before deleting it, plus its color/normal input, so 更新场景 can
            rebuild the slots. A single material on the whole object needs no
            record. Returns the number of materials registered."""
            rows = [r for r in self.rows if r['mesh'] == mesh]
            if not rows:
                return 0
            shape = rows[0]['shape']
            engines = []
            for r in rows:
                if r['engine'] not in engines:
                    engines.append(r['engine'])
            if not engines:
                return 0
            total = len(LingJuGeometryTools._safe(
                cmds.ls, shape + '.f[*]', flatten=True) or [])
            slots = []
            covered = set()
            whole = False
            for engine in engines:
                faces, object_level = self._engine_faces(engine, mesh)
                whole = whole or object_level
                covered.update(faces)
                blinn = LingJuGeometryTools.shader_of_engine(engine)
                color = color_value = normal = None
                if blinn:
                    color, color_value, normal = \
                        LingJuGeometryTools.capture_material_maps(blinn)
                slots.append({'faces': faces, 'whole': object_level,
                            'color': color, 'color_value': color_value,
                            'normal': normal})
            # One material already covering the whole object: nothing to keep.
            if len(slots) == 1 and (whole or len(covered) >= total):
                return 0
            # covered holds range strings like 'f[0:2]': flatten to count the
            # actual faces the restore pass must put back.
            covered_count = len(LingJuGeometryTools._safe(cmds.ls,
                [shape + '.' + f for f in covered], flatten=True) or [])
            self._deleted_slots[shape] = {'slots': slots,
                                        'covered': covered_count}
            return len(slots)

        def _restore_material_slots(self, transform, shape, tokens, slots,
                                covered=0):
            """Rebuild one Blinn per recorded slot on the same faces. A spare
            material is created only when fewer faces came back than were
            recorded (covered): faces that sat in initialShadingGroup before
            the delete had no material and must not gain one now, otherwise
            every delete/refresh cycle grows one extra Blinn."""
            base = LingJuGeometryTools._add_token(tokens['shd_suf'])
            color_on = self.restore_color_cb.isChecked()
            normal_on = self.restore_normal_cb.isChecked()
            created = []

            def make(index):
                blinn = LingJuGeometryTools.create_blinn(
                    LingJuGeometryTools._add_token(tokens['shd_pre'])
                    + LingJuGeometryTools.mesh_stem(transform, tokens)
                    + LingJuGeometryTools.suffix_sequence(base, index))
                return blinn, LingJuGeometryTools.create_engine_for(blinn)

            used = set()
            whole = False
            for i, slot in enumerate(slots):
                blinn, engine = make(i)
                if slot.get('whole'):
                    members = [shape]
                    whole = True
                else:
                    members = [shape + '.' + c
                            for c in slot.get('faces') or []]
                # objExists is always False for components (a face is not a
                # node); ls() resolves them. With objExists every face-level
                # slot silently failed to restore and left an orphan Blinn.
                members = [m for m in members
                        if LingJuGeometryTools._safe(cmds.ls, m)]
                if members:
                    try:
                        cmds.sets(members, edit=True, forceElement=engine)
                        # Read the members back flattened: 'f[0:2]' must count
                        # as f[0], f[1] or the spare-material check below
                        # always thinks nothing was restored.
                        got = LingJuGeometryTools._safe(
                            cmds.sets, engine, query=True) or []
                        for g in got:
                            for f in (LingJuGeometryTools._safe(
                                    cmds.ls, g, flatten=True) or [g]):
                                if '.f[' in f:
                                    used.add(f.rsplit('.', 1)[-1])
                    except Exception as exc:
                        LingJuGeometryTools._warn(
                            u'%s 材质位置恢复失败: %s'
                            % (LingJuGeometryTools.short_name(transform), exc))
                elif i == 0 and len(slots) == 1:
                    LingJuGeometryTools.assign_engine(shape, engine)
                    whole = True
                LingJuGeometryTools.restore_material_maps(
                    blinn, slot, color_on, normal_on,
                    connect_alpha=self._alpha_checked())
                created.append(engine)
            if not whole and len(used) < covered:
                rest = [shape + '.' + f.rsplit('.', 1)[-1]
                        for f in (LingJuGeometryTools._safe(
                            cmds.ls, shape + '.f[*]', flatten=True) or [])
                        if f.rsplit('.', 1)[-1] not in used]
                if rest:
                    _, engine = make(len(slots))
                    try:
                        cmds.sets(rest, edit=True, forceElement=engine)
                    except Exception:
                        pass
                    created.append(engine)
            return created

        def _prune_deleted_slots(self):
            for shape in list(self._deleted_slots):
                if not cmds.objExists(shape):
                    self._deleted_slots.pop(shape, None)

        def _update_select_all_btn(self):
            all_sel = bool(self.rows) and all(r['selected'] for r in self.rows)
            self.select_all_btn.setText(
                u'取消全选' if all_sel else u'全选')

        def on_toggle_select_all(self):
            if not self.rows:
                return
            target = not all(r['selected'] for r in self.rows)
            try:
                meshes = [r['mesh'] for r in self.rows
                        if r['selected'] != target]
                if meshes:
                    if target:
                        cmds.select(meshes, add=True)
                    else:
                        cmds.select(meshes, deselect=True)
            except Exception as exc:
                LingJuGeometryTools._warn(u'全选失败: %s' % exc)
                return
            for r in self.rows:
                r['selected'] = target
            for btn in self._select_buttons.values():
                btn.setText(u'取消选择' if target else u'选择')
            self._update_select_all_btn()

        def on_delete_all(self):
            """Delete every material in the list (Blinns + file / bump trees),
            after a confirmation prompt. Meshes themselves are not deleted."""
            blinns = []
            for r in self.rows:
                if r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            if not blinns:
                self._status(u'没有可删除的材质')
                return
            ret = QtWidgets.QMessageBox.question(
                self, u'删除全部材质',
                u'确定删除全部 %d 个材质？\n'
                u'每个 Blinn 及其 file / bump2d 节点都会被删除；\n'
                u'mesh 保留在场景中。' % len(blinns),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ret != QtWidgets.QMessageBox.Yes:
                return
            slots = 0
            for mesh in set(r['mesh'] for r in self.rows):
                slots += self._register_material_slots(mesh)
            with LingJuGeometryTools._undo_chunk('DeleteAllShaders'):
                seen = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in seen:
                        continue
                    seen.add(b)
                    LingJuGeometryTools.delete_shader_tree(b, r['mesh'])
                unused = LingJuGeometryTools.delete_unused_nodes()
            self.rows = []
            self._populate_rows()
            self._status(u'已删除 %d 个材质 · 清理 %d 个未用节点%s'
                        % (len(seen), unused,
                            (u' · 已登记 %d 个材质位置' % slots) if slots else ''))

        def on_toggle_select(self, row):
            try:
                if row['selected']:
                    cmds.select(row['mesh'], deselect=True)
                    row['selected'] = False
                else:
                    cmds.select(row['mesh'], add=True)
                    row['selected'] = True
            except Exception as exc:
                LingJuGeometryTools._warn(u'选中失败: %s' % exc)
                return
            btn = self._select_buttons.get(row['mesh'])
            if btn:
                btn.setText(u'取消选中' if row['selected'] else u'选中')
            self._update_select_all_btn()

        def on_uv_change(self, row, combo):
            uv = combo.currentText()
            try:
                cmds.polyUVSet(row['shape'], edit=True, currentUVSet=True,
                            uvSet=uv)
                row['uv'] = uv
            except Exception as exc:
                LingJuGeometryTools._warn(u'UV 集切换失败: %s' % exc)

        def on_refresh_materials(self):
            # 等同重开窗口：只读重读场景（面数 / 材质赋予），并刷新材质球下拉
            self.refresh_scene(mutate=False)
            m2m_scene_refresh_material_combo(self.material_combo)
            # 透明通道跟着勾选状态走（全场景已有的颜色贴图，与贴图文件夹无关）
            self._sync_transparency(self._alpha_checked())
            # 全场景：材质上的贴图若因场景挪动而失效（绝对路径找不到文件），
            # 在贴图文件夹里按同名文件找，找到就改路径，找不到不动。
            result = self._relink_missing_textures()
            if result is None:
                self._status(u'未设置贴图文件夹或文件夹中没有贴图: %s'
                            % self.path_edit.text().strip())
                return
            relinked, missing = result
            if relinked:
                # 重新读取行数据，让颜色 / 法线字段显示新路径
                self.refresh_scene(mutate=False)
            text = self.status.text()
            if relinked:
                text += u' · 修复丢失贴图 %d 个' % relinked
            if missing:
                shown = u'、'.join(missing[:3])
                if len(missing) > 3:
                    shown += u' 等'
                text += u' · 未找到 %d 个: %s' % (len(missing), shown)
            if not relinked and not missing:
                text += u' · 贴图路径全部有效'
            self._status(text)

        def _sync_transparency(self, enabled):
            """Apply 颜色贴图连 Transparency to the whole scene: every material
            that has a color file node gets that one link connected (checked)
            or cut (unchecked). Other transparency inputs are left alone."""
            done = set()
            with LingJuGeometryTools._undo_chunk('SyncTransparency'):
                for blinn in cmds.ls(materials=True) or []:
                    if blinn in done:
                        continue
                    done.add(blinn)
                    LingJuGeometryTools.set_transparency_link(
                        blinn, LingJuGeometryTools.color_file_of(blinn), enabled)

        def _relink_missing_textures(self):
            """Repair texture paths broken by moving the scene: for every
            color / normal file node in the scene whose fileTextureName no
            longer resolves, look for a file with the same name under the
            texture folder. Found -> only fileTextureName changes, nodes and
            links stay; not found -> left untouched.
            Returns (relinked, missing_names); None when the folder is empty."""
            folder = self.path_edit.text().strip()
            if not folder or not os.path.isdir(folder):
                return None
            found = {}
            for root, dirs, names in os.walk(folder):
                dirs.sort()
                for name in sorted(names):
                    if os.path.splitext(name)[1].lower() in LingJuGeometryTools.TEX_EXTS:
                        found.setdefault(name.lower(), os.path.join(root, name))
            if not found:
                return None
            project_root = LingJuGeometryTools._safe(
                cmds.workspace, query=True, rootDirectory=True)
            relinked = 0
            missing = []
            done = set()
            with LingJuGeometryTools._undo_chunk('RelinkMissingTextures'):
                for blinn in cmds.ls(materials=True) or []:
                    if blinn in done:
                        continue
                    done.add(blinn)
                    for fnode in (LingJuGeometryTools.color_file_of(blinn),
                                  LingJuGeometryTools.bump_file_of(
                                      LingJuGeometryTools.bump_of(blinn))):
                        if not fnode:
                            continue
                        current = LingJuGeometryTools._safe(
                            cmds.getAttr, fnode + '.fileTextureName') or ''
                        # <UDIM>/<f> 这类序列路径无法用 isfile 判断，跳过
                        if not current or '<' in current:
                            continue
                        if os.path.isfile(current):
                            continue
                        if not os.path.isabs(current) and project_root and \
                                os.path.isfile(os.path.join(project_root, current)):
                            continue
                        name = os.path.basename(current.replace('\\', '/'))
                        path = found.get(name.lower())
                        if not path:
                            if name not in missing:
                                missing.append(name)
                            continue
                        try:
                            LingJuGeometryTools._set_file_path(fnode, path)
                        except Exception as exc:
                            LingJuGeometryTools._warn(
                                u'%s 贴图路径修复失败: %s'
                                % (LingJuGeometryTools.short_name(fnode), exc))
                            continue
                        relinked += 1
            return relinked, missing

        def on_assign_material(self):
            shader = self.material_combo.currentText()
            if not shader:
                self._status(u'请先选择材质球')
                return
            try:
                with LingJuGeometryTools._undo_chunk('AssignMaterial'):
                    kind, count = m2m_scene_assign_material_selection(shader)
            except Exception as exc:
                LingJuGeometryTools._warn(u'赋予材质失败: %s' % exc)
                return
            if kind is None:
                self._status(u'请先选择物件或面')
            elif kind == 'faces':
                self._status(u'%d 个面已赋予 %s' % (count, shader))
            else:
                self._status(u'%d 个物件已赋予 %s' % (count, shader))

        def on_create_test_plane(self):
            """生成多材质材质平面：面数 = 材质数，逐面赋予一个默认命名材质。"""
            mat_type = self.mat_type.currentText()
            count = self.mat_count.value()
            try:
                with LingJuGeometryTools._undo_chunk('MaterialTestPlane'):
                    plane = cmds.polyPlane(subdivisionsX=count,
                                           subdivisionsY=1)[0]
                    shape = (cmds.listRelatives(plane, shapes=True,
                                                fullPath=True) or [plane])[0]
                    faces = cmds.ls(shape + '.f[*]', flatten=True) or []
                    for face in faces:
                        shader = cmds.shadingNode(mat_type, asShader=True)
                        group = m2m_scene_ensure_shading_group(shader)
                        cmds.sets(face, edit=True, forceElement=group)
            except Exception as exc:
                LingJuGeometryTools._warn(u'生成材质平面失败: %s' % exc)
                return
            cmds.select(plane, r=True)
            self.on_refresh_materials()
            self._status(u'已生成材质平面：%d 个面 / %d 个 %s'
                         % (len(faces), len(faces), mat_type))

        def on_rename_uv_sets(self):
            """Rename the current UV set. With objects selected in the scene
            only the selected meshes are processed; with an empty selection
            every listed mesh is renamed."""
            new = self.uv_batch.text().strip()
            if not new:
                return
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            changed = 0
            with LingJuGeometryTools._undo_chunk('RenameUVSets'):
                for mesh in sorted(set(r['mesh'] for r in target)):
                    row = next(r for r in target if r['mesh'] == mesh)
                    old = row['uv']
                    if old == new:
                        continue
                    try:
                        cmds.polyUVSet(row['shape'], edit=True, rename=True,
                                    uvSet=old, newUVSet=new)
                        changed += 1
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的 UV 集重命名失败: %s'
                            % (LingJuGeometryTools.short_name(mesh), exc))
            self.refresh_scene()
            self._status(u'已将 %d 个 mesh%s 的当前 UV 集重命名为 "%s"'
                        % (changed,
                            u'（仅选中）' if scoped else '', new))

        def on_delete_uv_sets(self):
            """Delete the current UV set of each mesh in scope. A global delete
            (empty selection) asks for confirmation first; with a selection the
            delete runs immediately. Meshes with a single UV set are skipped —
            Maya forbids deleting the last one."""
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            if not scoped:
                meshes = set(r['mesh'] for r in target)
                ret = QtWidgets.QMessageBox.question(
                    self, u'删除 UV 集',
                    u'确定删除全部 %d 个列表 mesh 的当前 UV 集？\n'
                    u'只有一个 UV 集的 mesh 会被跳过。' % len(meshes),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if ret != QtWidgets.QMessageBox.Yes:
                    return
            deleted = skipped = 0
            with LingJuGeometryTools._undo_chunk('DeleteUVSets'):
                for mesh in sorted(set(r['mesh'] for r in target)):
                    row = next(r for r in target if r['mesh'] == mesh)
                    if len(row['uv_sets']) < 2:
                        skipped += 1
                        continue
                    try:
                        cmds.polyUVSet(row['shape'], edit=True, delete=True,
                                    uvSet=row['uv'])
                        deleted += 1
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的 UV 集删除失败: %s'
                            % (LingJuGeometryTools.short_name(mesh), exc))
                        skipped += 1
            self.refresh_scene()
            self._status(u'已删除 %d 个 mesh%s 的当前 UV 集 · 跳过 %d 个'
                        % (deleted, u'（仅选中）' if scoped else '',
                            skipped))


        # ---------------- main actions ----------------

        def _browse_folder(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, u'选择贴图文件夹')
            if folder:
                self.path_edit.setText(folder)
                self._update_format_hint()

        def _update_format_hint(self):
            """Show which supported texture extensions exist in the folder next
            to the Browse button; empty folder / bad path says so instead."""
            folder = self.path_edit.text().strip()
            found = set()
            if folder and os.path.isdir(folder):
                for f in os.listdir(folder):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in LingJuGeometryTools.TEX_EXTS:
                        found.add(ext.lstrip('.'))
            if found:
                order = [e.lstrip('.') for e in LingJuGeometryTools.TEX_EXTS]
                text = u'格式: ' + ' '.join(e for e in order if e in found)
            elif folder and os.path.isdir(folder):
                text = u'格式: 无贴图'
            else:
                text = u'格式: 文件夹不存在'
            self.fmt_hint.setText(text)

        def _folder_files(self):
            folder = self.path_edit.text().strip()
            if not folder or not os.path.isdir(folder):
                return []
            return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                    if os.path.splitext(f)[1].lower() in LingJuGeometryTools.TEX_EXTS]

        def _resolve_texture(self, name):
            """Turn a typed name into a texture path: an absolute path, a file in
            the texture folder (with or without extension), or an existing scene
            file node with that name."""
            name = name.strip()
            if not name:
                return None
            if os.path.isfile(name):
                return name
            folder = self.path_edit.text().strip()
            if folder and os.path.isdir(folder):
                for fn in sorted(os.listdir(folder)):
                    base, ext = os.path.splitext(fn)
                    if ext.lower() in LingJuGeometryTools.TEX_EXTS and base == name:
                        return os.path.join(folder, fn)
                direct = os.path.join(folder, name)
                if os.path.isfile(direct):
                    return direct
            if cmds.objExists(name) and cmds.nodeType(name) == 'file':
                return cmds.getAttr(name + '.fileTextureName') or None
            return None

        def _commit_map_fields(self, rows=None, kinds=None):
            """Commit pending color/normal field edits. Clearing a field deletes
            the connected nodes: file + place2dTexture for color, plus bump2d for
            normal. Shared Blinns are committed once. rows limits the commit to a
            subset of self.rows (selection-scoped Apply names); None means every
            row. kinds limits which map types are committed (default both)."""
            if kinds is None:
                kinds = ('normal', 'color')
            set_count = disc_count = 0
            alpha_on = self._alpha_checked()
            done = set()
            for r in (self.rows if rows is None else rows):
                b = r['blinn']
                if b in done:
                    continue
                done.add(b)
                for kind in kinds:
                    key = kind + '_edit'
                    if key not in r:
                        continue
                    want = r[key].strip()
                    if kind == 'color':
                        fnode = LingJuGeometryTools.color_file_of(b)
                    else:
                        fnode = LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(b))
                    cur = LingJuGeometryTools.short_name(fnode)
                    if want == '':
                        if cur:
                            try:
                                if kind == 'normal':
                                    LingJuGeometryTools.disconnect_normal(b)
                                else:
                                    LingJuGeometryTools.disconnect_color(b)
                            except Exception as exc:
                                LingJuGeometryTools._warn(u'%s 清空失败（%s）: %s'
                                    % (kind, LingJuGeometryTools.short_name(b), exc))
                                continue
                            disc_count += 1
                        continue
                    want_node = LingJuGeometryTools._node_safe_name(want)
                    path = self._resolve_texture(want)
                    if not path:
                        if want_node != cur:
                            LingJuGeometryTools._warn(u'%s「%s」在文件夹或场景中未找到'
                                % (kind, want))
                        continue
                    if (want_node == cur and fnode and LingJuGeometryTools._same_path(
                            path, LingJuGeometryTools._safe(cmds.getAttr,
                                        fnode + '.fileTextureName'))):
                        if kind == 'color':
                            # field untouched, but the checkbox still applies
                            LingJuGeometryTools.set_transparency_link(
                                b, fnode, alpha_on)
                        continue  # same node name and same file: nothing to do
                    try:
                        if kind == 'color':
                            LingJuGeometryTools.set_color_file(
                                b, path, want, connect_alpha=alpha_on)
                        else:
                            LingJuGeometryTools.set_normal_file(b, path, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s「%s」应用到 %s 失败: %s'
                            % (kind, want, LingJuGeometryTools.short_name(b), exc))
                        continue
                    set_count += 1
            return set_count, disc_count

        def _scoped_rows(self):
            """Rows a batch action should touch under the current selection:
            the rows of the selected meshes when anything is selected, every
            row when the selection is empty. Returns (rows, selection_active);
            rows is empty when the selected objects are not in the list."""
            sel = set(cmds.ls(selection=True, long=True, type='transform') or [])
            for node in cmds.ls(selection=True, long=True, type='shape') or []:
                sel.update(LingJuGeometryTools._safe(cmds.listRelatives, node, parent=True,
                                fullPath=True) or [])
            if not sel:
                return list(self.rows), False
            return [r for r in self.rows if r['mesh'] in sel], True

        def on_apply_names(self):
            """Apply pending names. With objects selected in the scene only the
            rows of the selected meshes are processed; with an empty selection
            the rename runs on every row."""
            self._sync_field_texts()
            tokens = self._tokens()
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            mesh_n = shd_n = 0
            with LingJuGeometryTools._undo_chunk('ApplyNames'):
                # 1) mesh renames, one per transform
                seen = set()
                for r in target:
                    mesh = r['mesh']
                    if mesh in seen:
                        continue
                    seen.add(mesh)
                    want = LingJuGeometryTools.strip_ns(r.get('mesh_edit') or '')
                    if not want or want == LingJuGeometryTools.short_name(mesh):
                        continue
                    try:
                        new = cmds.rename(mesh, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'重命名 %s 失败: %s' % (LingJuGeometryTools.short_name(mesh), exc))
                        continue
                    mesh_n += 1
                    for row in self.rows:
                        if row['mesh'] == mesh:
                            row['mesh'] = new
                            shapes = LingJuGeometryTools._safe(cmds.listRelatives, new,
                                        shapes=True, fullPath=True) or []
                            if shapes:
                                row['shape'] = shapes[0]
                # 2) shader renames, one per Blinn.
                #    Unless the user typed a custom name in the shader field,
                #    regenerate the material name from the (possibly just
                #    renamed) mesh stem + tokens so materials follow the mesh.
                #    Shared blinns (used by several meshes) are excluded from
                #    multi-material sequence numbering — they keep the single-
                #    material name because they have no unique owner mesh.
                blinn_to_meshes = {}
                for r in target:
                    blinn_to_meshes.setdefault(r['blinn'], set()).add(r['mesh'])
                shared_blinns = {b for b, meshes in blinn_to_meshes.items()
                                 if len(meshes) > 1}

                mesh_blinns = {}
                for r in target:
                    blinns = mesh_blinns.setdefault(r['mesh'], [])
                    if r['blinn'] not in blinns:
                        blinns.append(r['blinn'])
                blinn_seq = {}
                for blinns in mesh_blinns.values():
                    if len(blinns) < 2:
                        continue
                    for i, b in enumerate(blinns):
                        if b in shared_blinns:
                            continue  # shared: no sequence number
                        blinn_seq.setdefault(b, i)
                shd_pre = LingJuGeometryTools._add_token(tokens['shd_pre'])
                shd_suf = LingJuGeometryTools._add_token(tokens['shd_suf'])
                no_suffix_numbering = not shd_suf
                seen = set()
                for r in target:
                    b = r['blinn']
                    if b in seen:
                        continue
                    seen.add(b)
                    user_text = LingJuGeometryTools.strip_ns(r.get('shd_edit') or '')
                    seq = blinn_seq.get(b)
                    old_default = self._default_shader_name(b, tokens, seq)
                    if user_text and user_text != old_default:
                        want = user_text
                    elif b in shared_blinns or no_suffix_numbering:
                        # Shared blinn has no unique owner mesh, so stem
                        # comes from the blinn's own name. Same when the user
                        # left shd_suf blank — we keep whatever the scene
                        # already had (including trailing digits like 01,
                        # _A, ...) and only re-apply the shd_pre token.
                        stem = LingJuGeometryTools.shader_stem(b, tokens)
                        want = shd_pre + stem + shd_suf
                    elif seq is None:
                        stem = LingJuGeometryTools.mesh_stem(r['mesh'], tokens)
                        want = shd_pre + stem + shd_suf
                    else:
                        stem = LingJuGeometryTools.mesh_stem(r['mesh'], tokens)
                        want = (shd_pre + stem
                                + LingJuGeometryTools.suffix_sequence(shd_suf, seq))
                    if not want or want == LingJuGeometryTools.short_name(b):
                        continue
                    try:
                        new = LingJuGeometryTools._take_name(b, want)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'重命名 %s 失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    shd_n += 1
                    for row in self.rows:
                        if row['blinn'] == b:
                            row['blinn'] = new
                # 3) color / normal field commits
                map_n, disc_n = self._commit_map_fields(rows=target)
            self.refresh_scene()
            self._status(u'已应用%s: mesh 重命名 %d 个 · Blinn 重命名 %d 个 · '
                        u'贴图更新 %d 个 · 移除贴图节点树 %d 个'
                        % (u'（仅选中）' if scoped else '',
                            mesh_n, shd_n, map_n, disc_n))

        def on_match_shaders(self):
            """Rename materials to match their mesh: shd_pre + mesh stem
            (mesh_pre/mesh_suf stripped) + shd_suf.

            A mesh with several materials gets numbered: suffix_sequence on
            shd_suf when the suffix token is set, plain _01/_02 otherwise.
            A blinn shared by several meshes is named after the first mesh
            that uses it (row order) and is skipped for the later meshes.
            With objects selected in the scene only their rows are renamed;
            with an empty selection every row is processed. Mesh names and
            manually typed shader fields are left untouched."""
            tokens = self._tokens()
            target, scoped = self._scoped_rows()
            if scoped and not target:
                self._status(u'选中的物体不在 mesh 列表中')
                return
            shd_pre = LingJuGeometryTools._add_token(tokens['shd_pre'])
            shd_suf = LingJuGeometryTools._add_token(tokens['shd_suf'])
            # Per-mesh material lists, in row order, no duplicates.
            mesh_order = []
            mesh_blinns = {}
            for r in target:
                if r['mesh'] not in mesh_blinns:
                    mesh_order.append(r['mesh'])
                    mesh_blinns[r['mesh']] = []
                if r['blinn'] not in mesh_blinns[r['mesh']]:
                    mesh_blinns[r['mesh']].append(r['blinn'])
            renamed = {}
            n = 0
            with LingJuGeometryTools._undo_chunk('MatchShaders'):
                for mesh in mesh_order:
                    stem = LingJuGeometryTools.mesh_stem(mesh, tokens)
                    # Blinns already named through an earlier (shared) mesh do
                    # not take part in this mesh's numbering.
                    own = [b for b in mesh_blinns[mesh] if b not in renamed]
                    for i, b in enumerate(own):
                        if len(own) == 1:
                            want = shd_pre + stem + shd_suf
                        elif shd_suf:
                            want = (shd_pre + stem
                                    + LingJuGeometryTools.suffix_sequence(shd_suf, i))
                        else:
                            want = shd_pre + stem + '_%02d' % (i + 1)
                        if not want or want == LingJuGeometryTools.short_name(b):
                            renamed[b] = b
                            continue
                        try:
                            renamed[b] = LingJuGeometryTools._take_name(b, want)
                        except Exception as exc:
                            LingJuGeometryTools._warn(
                                u'重命名 %s 失败: %s'
                                % (LingJuGeometryTools.short_name(b), exc))
                            continue
                        n += 1
            # Read-only rebuild: rows pick up the new names from the scene.
            self.refresh_scene(mutate=False)
            self._status(u'材质匹配模型%s: 重命名 %d 个材质'
                        % (u'（仅选中）' if scoped else '', n))

        def on_assign_normals(self):
            files = self._folder_files()
            if not files:
                self._status(u'文件夹中未找到贴图: %s'
                            % self.path_edit.text().strip())
                return
            tokens = self._tokens()
            self._sync_field_texts()
            assigned = 0
            with LingJuGeometryTools._undo_chunk('AssignNormals'):
                # commit pending normal edits first (empty field -> disconnect)
                map_n, disc_n = self._commit_map_fields(kinds=('normal',))
                # auto-match: file stem equals mesh/shader stem + normal suffix
                done = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in done:
                        continue
                    done.add(b)
                    bump = LingJuGeometryTools.bump_of(b)
                    if bump and LingJuGeometryTools._safe(cmds.listConnections,
                                    bump + '.bumpValue',
                                    source=True, destination=False):
                        continue  # already has a normal / bump input
                    stems = LingJuGeometryTools.shader_match_stems(
                        b, r['mesh'], tokens)
                    path = LingJuGeometryTools.find_texture_for(stems, tokens, files, 'nml_suf')
                    if not path:
                        continue
                    stem = os.path.splitext(os.path.basename(path))[0]
                    try:
                        LingJuGeometryTools.set_normal_file(b, path, stem)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的法线指定失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    assigned += 1
            self.refresh_scene()
            self._status(u'指定法线: 自动匹配 %d 个 · 手动更新 %d 个 · '
                        u'移除贴图节点树 %d 个' % (assigned, map_n, disc_n))

        def on_assign_colors(self):
            """Auto-match color maps from the texture folder: a file whose stem
            (minus the texture prefix and the color suffix) equals the mesh or
            Blinn stem is connected to Blinns that have no color input yet."""
            files = self._folder_files()
            if not files:
                self._status(u'文件夹中未找到贴图: %s'
                            % self.path_edit.text().strip())
                return
            tokens = self._tokens()
            self._sync_field_texts()
            alpha_on = self._alpha_checked()
            assigned = 0
            with LingJuGeometryTools._undo_chunk('AssignColors'):
                # commit pending color edits first (empty field -> disconnect)
                map_n, disc_n = self._commit_map_fields(kinds=('color',))
                # auto-match: file stem minus texture prefix + color suffix
                done = set()
                for r in self.rows:
                    b = r['blinn']
                    if b in done:
                        continue
                    done.add(b)
                    if LingJuGeometryTools._safe(cmds.listConnections, b + '.color',
                            source=True, destination=False):
                        # already has a color input: still apply the checkbox
                        # to its existing file node (connect or cut the link)
                        LingJuGeometryTools.set_transparency_link(
                            b, LingJuGeometryTools.color_file_of(b), alpha_on)
                        continue
                    stems = LingJuGeometryTools.shader_match_stems(
                        b, r['mesh'], tokens)
                    path = LingJuGeometryTools.find_texture_for(stems, tokens, files, 'col_suf')
                    if not path:
                        continue
                    stem = os.path.splitext(os.path.basename(path))[0]
                    try:
                        LingJuGeometryTools.set_color_file(
                            b, path, stem, connect_alpha=alpha_on)
                    except Exception as exc:
                        LingJuGeometryTools._warn(u'%s 的颜色贴图指定失败: %s'
                            % (LingJuGeometryTools.short_name(b), exc))
                        continue
                    assigned += 1
            self.refresh_scene()
            self._status(u'指定颜色贴图: 自动匹配 %d 个 · 手动更新 %d 个 · '
                        u'移除贴图节点树 %d 个' % (assigned, map_n, disc_n))

        def on_delete_mesh(self, row):
            self._sync_field_texts()
            mesh = row['mesh']
            slots = self._register_material_slots(mesh)
            others = [r for r in self.rows if r['mesh'] != mesh]
            blinns = []
            for r in self.rows:
                if r['mesh'] == mesh and r['blinn'] not in blinns:
                    blinns.append(r['blinn'])
            with LingJuGeometryTools._undo_chunk('DeleteMeshShaders'):
                for b in blinns:
                    if any(r['blinn'] == b for r in others):
                        continue  # shared Blinn still used by another mesh
                    LingJuGeometryTools.delete_shader_tree(b, mesh)
                unused = LingJuGeometryTools.delete_unused_nodes()
            self.rows = [r for r in self.rows if r['mesh'] != mesh]
            self._populate_rows()
            self._status(u'已删除 %s 的材质 · 清理 %d 个未用节点%s'
                        % (LingJuGeometryTools.short_name(mesh), unused,
                            (u' · 已登记 %d 个材质位置' % slots) if slots else ''))

        def on_clear_namespaces(self):
            n = LingJuGeometryTools.clear_namespaces()
            self.refresh_scene()
            self._status(u'已移除 %d 个命名空间' % n)

        def on_delete_unused(self):
            """Whole-scene Delete Unused Nodes pass (same as the Hypershade
            button), undoable as one step."""
            with LingJuGeometryTools._undo_chunk('DeleteUnusedNodes'):
                removed = LingJuGeometryTools.delete_unused_nodes()
            self._status(u'已删除 %d 个未用节点' % removed)

        def _assign_dropped_map(self, row, kind, filename):
            """Connect a dragged or selected image to the row's Blinn right
            away (kind is 'color' or 'normal'), then sync every field of the
            Blinn so shared rows show the new node name."""
            folder = self.path_edit.text().strip()
            path = filename if os.path.isabs(filename) else os.path.join(
                folder, filename)
            if not os.path.isfile(path):
                found = [f for f in self._folder_files()
                        if os.path.basename(f) == filename]
                if not found:
                    LingJuGeometryTools._warn(u'文件未找到: %s' % filename)
                    return
                path = found[0]
            stem = os.path.splitext(os.path.basename(path))[0]
            with LingJuGeometryTools._undo_chunk('AssignMap'):
                if kind == 'color':
                    LingJuGeometryTools.set_color_file(
                        row['blinn'], path, stem,
                        connect_alpha=self._alpha_checked())
                else:
                    LingJuGeometryTools.set_normal_file(row['blinn'], path, stem)
            if kind == 'color':
                fn = LingJuGeometryTools.color_file_of(row['blinn'])
            else:
                fn = LingJuGeometryTools.bump_file_of(LingJuGeometryTools.bump_of(row['blinn']))
            shown = LingJuGeometryTools.short_name(fn) if fn else stem
            key = kind + '_edit'
            for r in self.rows:
                if r['blinn'] == row['blinn']:
                    r.pop(key, None)
                    for e in self._edits[kind].get(r['blinn'], ()):
                        e.blockSignals(True)
                        e.setText(shown)
                        e.blockSignals(False)
            label = u'颜色贴图' if kind == 'color' else u'法线贴图'
            self._status(u'%s %s → %s' % (label, shown,
                                        LingJuGeometryTools.short_name(row['blinn'])))


    # ---------------------------------------------------------------------------
    # entry

    def _maya_main_window():
        try:
            from shiboken2 import wrapInstance
        except ImportError:
            try:
                from shiboken6 import wrapInstance
            except ImportError:
                return None
        try:
            from maya.OpenMayaUI import MQtUtil
            ptr = MQtUtil.mainWindow()
            if ptr:
                return wrapInstance(int(ptr), QtWidgets.QWidget)
        except Exception:
            pass
        return None


    def show_naming_window(self, *args):
        """Open the Polygon Asset Manager window."""
        # 类级别变量 LingJuGeometryTools._WINDOW 不能用 global 访问，必须显式引用类名
        if LingJuGeometryTools._WINDOW is not None:
            try:
                LingJuGeometryTools._WINDOW.close()
                LingJuGeometryTools._WINDOW.deleteLater()
            except Exception:
                pass
            LingJuGeometryTools._WINDOW = None
        LingJuGeometryTools._WINDOW = LingJuGeometryTools.AssetManagerUI(LingJuGeometryTools._maya_main_window())
        LingJuGeometryTools._WINDOW.show()
        LingJuGeometryTools._WINDOW.raise_()
        LingJuGeometryTools._WINDOW.activateWindow()
        return LingJuGeometryTools._WINDOW

    # --------------------- LingJuGeometryTools.test
    def test(self, *args):
        lj_info(u"测试内容！")


# ===========================================================================
# Maya2Marmoset config (inlined)
# ===========================================================================


m2m_config_OPTION_KEY = 'maya2marmoset_config'

m2m_config_DEFAULTS = {
    'mode': 'pivot',
    'tolerance': 1.0,
    'port': 8730,
    'temp_root': os.path.join(tempfile.gettempdir(), 'Maya2Marmoset'),
}


def m2m_config_load():
    config = dict(m2m_config_DEFAULTS)
    if cmds.optionVar(exists=m2m_config_OPTION_KEY):
        try:
            stored = json.loads(cmds.optionVar(query=m2m_config_OPTION_KEY))
        except Exception:
            stored = {}
        if isinstance(stored, dict):
            config.update(stored)
    return config


def m2m_config_save(config):
    cmds.optionVar(stringValue=(m2m_config_OPTION_KEY, json.dumps(config)))


def m2m_config_bridge_file(config):
    return os.path.join(config['temp_root'], 'bridge.json')

# ===========================================================================
# Maya2Marmoset naming (inlined)
# ===========================================================================
# """Naming rules shared by the Maya side and the Toolbag listener.
# 
# Kept free of Maya imports so it can be unit tested on plain Python.
# """


m2m_naming__ROLE_RE = re.compile(
    r'_(low|high|lo|hi|cage|offset)(?:[_-]?(\d+))?$', re.IGNORECASE)
m2m_naming__ROLE_ALIASES = {'lo': 'low', 'hi': 'high'}
m2m_naming_CLASSIFIED_ROLES = ('low', 'high', 'cage', 'offset')
m2m_naming_PROJECT_SUFFIX = '_bakeproject'   # 第一层：Toolbag Bake Project
m2m_naming_GROUP_SUFFIX = '_bakegroup'       # 第二层：工程里的文件夹
m2m_naming__BAD_FILE_RE = re.compile(r'[\\/:*?"<>|\s]+')
m2m_naming__BAD_GROUP_RE = re.compile(r'[:|]+')


def m2m_naming_split_namespace(name):
    """Return (namespace_prefix, leaf_short_name)."""
    leaf = name.split('|')[-1]
    if ':' in leaf:
        namespace, short = leaf.rsplit(':', 1)
        return namespace + ':', short
    return '', leaf


def m2m_naming_short_name(name):
    return m2m_naming_split_namespace(name)[1]


def m2m_naming_strip_role(short):
    return m2m_naming__ROLE_RE.sub('', short)


def m2m_naming_base_name(name):
    return m2m_naming_strip_role(m2m_naming_short_name(name))


def m2m_naming_role_of(name):
    match = m2m_naming__ROLE_RE.search(m2m_naming_short_name(name))
    if not match:
        return None
    role = match.group(1).lower()
    return m2m_naming__ROLE_ALIASES.get(role, role)


def m2m_naming_is_classified(name):
    """Roles Toolbag's Quick Loader recognizes and keeps."""
    return m2m_naming_role_of(name) in m2m_naming_CLASSIFIED_ROLES


def m2m_naming_low_name(base):
    return base + '_low'


def m2m_naming_high_name(base, index=1, total=1):
    if total <= 1:
        return base + '_high'
    return '%s_high_%02d' % (base, index)


def m2m_naming_high_group_name(base):
    return base + '_high'


def m2m_naming__group_name(value, suffix):
    """把任意名字做成带指定标记的干净组名。"""
    name = m2m_naming_short_name(value) if value else 'NoMaterial'
    name = m2m_naming__BAD_GROUP_RE.sub('_', name)
    if name.lower().endswith(suffix):
        name = name[:-len(suffix)]
    return name + suffix


def m2m_naming_bake_project_name(stem):
    """第一层组名：<名字>_bakeproject，对应 Toolbag 的 Bake Project。"""
    return m2m_naming__group_name(stem, m2m_naming_PROJECT_SUFFIX)


def m2m_naming_bake_group_name(material):
    """第二层组名：<材质名>_bakegroup，对应工程里的文件夹。"""
    return m2m_naming__group_name(material, m2m_naming_GROUP_SUFFIX)


def m2m_naming_is_bake_project(name):
    """是否是第一层组（Bake Project 标记）。"""
    return m2m_naming_short_name(name).lower().endswith(m2m_naming_PROJECT_SUFFIX)


def m2m_naming_is_bake_group(name):
    """是否是第二层组（工程内文件夹标记）。"""
    return m2m_naming_short_name(name).lower().endswith(m2m_naming_GROUP_SUFFIX)


def m2m_naming_group_display_name(name):
    """Maya 组名 -> Toolbag 工程内文件夹名（去掉 _bakeproject / _bakegroup 标记）。

    只用于工程内文件夹名；Bake Project 名现在直接保留 _bakeproject 标记
    （见 exporter._bake_names），不走这里。若去掉标记后为空（组名就是标记
    本身），回退成组名本身。
    """
    short = m2m_naming_short_name(name)
    if not short:
        return ''
    for suffix in (m2m_naming_PROJECT_SUFFIX, m2m_naming_GROUP_SUFFIX):
        if short.lower().endswith(suffix):
            stripped = short[:-len(suffix)]
            if stripped:
                return stripped
    return short


def m2m_naming_unique_name(desired, taken):
    if desired not in taken:
        return desired
    index = 2
    while '%s_%d' % (desired, index) in taken:
        index += 1
    return '%s_%d' % (desired, index)


def m2m_naming_safe_file_name(name):
    safe = m2m_naming__BAD_FILE_RE.sub('_', m2m_naming_short_name(name)).strip('_')
    return safe or 'unnamed'

# ===========================================================================
# Maya2Marmoset detect (inlined)
# ===========================================================================
# -*- coding: utf-8 -*-
# """Pure clustering / planning logic for high-low detection."""

### STRIPPED: import math

### STRIPPED: from . import naming


def m2m_detect_distance(a, b):
    return math.sqrt(
        (a[0] - b[0]) ** 2 +
        (a[1] - b[1]) ** 2 +
        (a[2] - b[2]) ** 2
    )


def m2m_detect_cluster_items(items, tolerance):
    """Greedy centroid clustering.

    Each item: {'name': str, 'pos': (x, y, z), 'faces': int, 'role': str|None}.
    Larger meshes are inserted first so the low poly anchors each cluster.
    """
    if tolerance < 0:
        tolerance = 0.0
    clusters = []
    ordered = sorted(items, key=lambda it: (-it.get('faces', 0), it['name'].lower()))
    for item in ordered:
        pos = item['pos']
        for cluster in clusters:
            if m2m_detect_distance(pos, cluster['center']) <= tolerance:
                cluster['items'].append(item)
                count = len(cluster['items'])
                center = cluster['center']
                for axis in range(3):
                    center[axis] = (center[axis] * (count - 1) + pos[axis]) / float(count)
                break
        else:
            clusters.append({
                'center': [float(pos[0]), float(pos[1]), float(pos[2])],
                'items': [item],
            })
    return [cluster['items'] for cluster in clusters]


def m2m_detect_pick_low(items):
    return sorted(items, key=lambda it: (
        it.get('faces', 0),
        0 if it.get('role') == 'low' else 1,
        len(it['name']),
        it['name'].lower(),
    ))[0]


def m2m_detect_unique_items(items):
    """按名字去重，保持首次出现顺序。"""
    seen = set()
    out = []
    for item in items:
        key = item['name']
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def m2m_detect_plan_from_clusters(clusters):
    """把已经分好的簇转成配对计划，坐标聚类与按材质分组共用。"""
    entries = []
    skipped = []
    used_bases = set()

    for cluster in clusters:
        names = [it['name'] for it in cluster]
        if len(cluster) < 2:
            skipped.append((names, 'no paired high/low object'))
            continue
        low = m2m_detect_pick_low(cluster)
        base = m2m_naming_strip_role(low['name'])
        if not base:
            skipped.append((names, 'cannot derive a base name'))
            continue
        base = m2m_naming_unique_name(base, used_bases)
        used_bases.add(base)
        highs = sorted(
            [it for it in cluster if it is not low],
            key=lambda it: it['name'].lower(),
        )
        entries.append({'base': base, 'low': low, 'highs': highs})
    return {'entries': entries, 'skipped': skipped}


def m2m_detect_build_plan(items, tolerance):
    """Return {'entries': [...], 'skipped': [(names, reason), ...]}.

    Each entry: {'base', 'low': item, 'highs': [item, ...]}.
    """
    return m2m_detect_plan_from_clusters(m2m_detect_cluster_items(m2m_detect_unique_items(items), tolerance))

# ===========================================================================
# Maya2Marmoset scene (inlined)
# ===========================================================================
# """Thin wrappers around maya.cmds used by the grouping and export layers."""


m2m_scene_DEFAULT_SHADERS = (
    'lambert1', 'standardSurface1', 'particleCloud1', 'shaderGlow1')


def m2m_scene_mesh_shapes(obj):
    if cmds.nodeType(obj) == 'mesh':
        return [obj]
    return cmds.listRelatives(obj, shapes=True, type='mesh', noIntermediate=True) or []


def m2m_scene_meshes_from(objs):
    """Expand transforms/groups to the mesh transforms below them."""
    result = []
    seen = set()
    for obj in objs:
        if cmds.nodeType(obj) == 'mesh':
            parents = cmds.listRelatives(obj, parent=True) or []
            obj = parents[0] if parents else obj
        if cmds.nodeType(obj) != 'transform':
            continue
        found = []
        if m2m_scene_mesh_shapes(obj):
            found = [obj]
        else:
            for child in cmds.listRelatives(
                    obj, allDescendents=True, type='transform', noIntermediate=True) or []:
                if m2m_scene_mesh_shapes(child):
                    found.append(child)
        for member in found:
            long_name = cmds.ls(member, long=True) or [member]
            key = long_name[0]
            if key in seen:
                continue
            seen.add(key)
            result.append(member)
    return result


def m2m_scene_selected_meshes():
    return m2m_scene_meshes_from(cmds.ls(selection=True) or [])


def m2m_scene_all_mesh_transforms():
    return [obj for obj in cmds.ls(type='transform') or [] if m2m_scene_mesh_shapes(obj)]


def m2m_scene_face_count(obj):
    total = 0
    for shape in m2m_scene_mesh_shapes(obj):
        try:
            value = cmds.polyEvaluate(shape, face=True)
        except Exception:
            value = 0
        if isinstance(value, int) and value > 0:
            total += value
    return total


def m2m_scene_world_pivot(obj):
    try:
        return list(cmds.xform(obj, query=True, worldSpace=True, rotatePivot=True))
    except Exception:
        return None


def m2m_scene_world_bbox_center(obj):
    box = cmds.exactWorldBoundingBox(obj)
    if not box:
        return None
    return [
        (box[0] + box[3]) / 2.0,
        (box[1] + box[4]) / 2.0,
        (box[2] + box[5]) / 2.0,
    ]


def m2m_scene_rename_short(obj, short):
    namespace, current = m2m_naming_split_namespace(obj)
    if current == short:
        return obj
    return cmds.rename(obj, namespace + short)


def m2m_scene_all_transform_names():
    return set(m2m_naming_short_name(name) for name in cmds.ls(type='transform') or [])


def m2m_scene_list_materials():
    materials = cmds.ls(materials=True) or []
    result = []
    for material in materials:
        short = m2m_naming_short_name(material)
        if short in m2m_scene_DEFAULT_SHADERS:
            continue
        if short.startswith('default') or short.startswith('initial'):
            continue
        result.append(material)
    return sorted(result, key=lambda value: value.lower())


def m2m_scene_shading_group_of(shader):
    if not shader or not cmds.objExists(shader):
        return None
    groups = cmds.listConnections(shader, type='shadingEngine') or []
    return groups[0] if groups else None


def m2m_scene_is_default_shader(shader):
    if not shader:
        return True
    if m2m_naming_short_name(shader) in m2m_scene_DEFAULT_SHADERS:
        return True
    group = m2m_scene_shading_group_of(shader)
    return bool(group) and m2m_naming_short_name(group) == 'initialShadingGroup'


def m2m_scene_ensure_shading_group(shader):
    name = m2m_naming_short_name(shader) + 'SG'
    if cmds.objExists(name):
        group = name
    else:
        group = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name)
    cmds.connectAttr(shader + '.outColor', group + '.surfaceShader', force=True)
    return group


def m2m_scene_material_of(obj):
    for shape in m2m_scene_mesh_shapes(obj):
        groups = cmds.listConnections(shape, type='shadingEngine') or []
        for group in groups:
            shaders = cmds.listConnections(
                group + '.surfaceShader', source=True, destination=False) or []
            if shaders:
                return shaders[0]
    return None


def m2m_scene_is_clean_material(shader):
    """A lambert with no incoming texture/utility connections and no default."""
    if not shader or not cmds.objExists(shader):
        return False
    if m2m_scene_is_default_shader(shader) or cmds.nodeType(shader) != 'lambert':
        return False
    return not (cmds.listConnections(
        shader, source=True, destination=False, plugs=True) or [])


def m2m_scene_ensure_clean_material(name, excluded=None):
    """Reuse a clean material with this name, otherwise create a unique one."""
    excluded = excluded or set()
    short = m2m_naming_short_name(name)
    for shader in cmds.ls(short, materials=True) or []:
        if m2m_naming_short_name(shader) in excluded:
            continue
        if m2m_scene_is_clean_material(shader):
            return shader
    taken = set(m2m_naming_short_name(item) for item in cmds.ls(materials=True) or [])
    taken.update(excluded)
    unique = m2m_naming_unique_name(short, taken)
    shader = cmds.shadingNode('lambert', asShader=True, name=unique)
    m2m_scene_ensure_shading_group(shader)
    return shader


def m2m_scene_assign_material(obj, shader):
    shapes = m2m_scene_mesh_shapes(obj)
    if not shapes:
        return False
    group = m2m_scene_shading_group_of(shader) or m2m_scene_ensure_shading_group(shader)
    cmds.sets(shapes, edit=True, forceElement=group)
    return True


def m2m_scene_selected_faces():
    """当前选择里的面组件（pCube1.f[0:3] 形式），没有则返回空列表。"""
    return cmds.filterExpand(cmds.ls(selection=True, flatten=True) or [],
                             selectionMask=34, expand=True) or []


def m2m_scene_assign_material_faces(faces, shader):
    """把材质逐面赋予（选中面），返回赋予的面数。

    cmds.sets -forceElement 接受面组件时会在 shape 的 iog 上建立逐面材质，
    原物体其它面保持原有材质。
    """
    if not faces or not shader:
        return 0
    expanded = cmds.filterExpand(faces, selectionMask=34, expand=True) or faces
    group = m2m_scene_shading_group_of(shader) or m2m_scene_ensure_shading_group(shader)
    cmds.sets(expanded, edit=True, forceElement=group)
    return len(expanded)


def m2m_scene_refresh_material_combo(combo):
    """用场景现有材质球刷新下拉，尽量保留当前选项。

    烘焙页与「零距命名材质管理」共用，避免两处各写一遍读取逻辑。
    """
    current = combo.currentText()
    combo.clear()
    combo.addItems(m2m_scene_list_materials())
    if current:
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)


def m2m_scene_assign_material_selection(shader):
    """把材质赋予当前选择：有面按面逐面赋予，只选物件按整物体。

    返回 (kind, count)：kind 为 'faces' / 'objects'；没有可赋予的选择时
    返回 (None, 0)，供调用方提示用户。
    """
    if not shader:
        return None, 0
    faces = m2m_scene_selected_faces()
    if faces:
        return 'faces', m2m_scene_assign_material_faces(faces, shader)
    objects = m2m_scene_selected_meshes()
    if not objects:
        return None, 0
    return 'objects', sum(1 for obj in objects
                          if m2m_scene_assign_material(obj, shader))


def m2m_scene_scene_stem():
    """当前场景名（去扩展名、已做文件名安全化），未保存时为 untitled。"""
    path = cmds.file(query=True, sceneName=True) or ''
    if not path:
        return 'untitled'
    return m2m_naming_safe_file_name(os.path.splitext(os.path.basename(path))[0])


def m2m_scene_parent_transform(obj):
    """obj 的上一级 transform，没有则返回 None。"""
    parents = cmds.listRelatives(obj, parent=True) or []
    return parents[0] if parents else None


def m2m_scene_bake_folder_of(obj):
    """返回 obj 所属的第二层组（Toolbag 工程内的文件夹），没有则 None。

    只认 _bakegroup 标记，和嵌套深度无关：<base>_high 这类物件级组、
    普通组织组、set 都不会被当成一层；物件直接挂在根下时返回 None。
    """
    current = obj
    while True:
        parent = m2m_scene_parent_transform(current)
        if not parent:
            return None
        if m2m_naming_is_bake_group(parent):
            return parent
        current = parent


def m2m_scene_bake_project_of(obj):
    """返回 obj 归属的第一层组（Toolbag 的 Bake Project），没有则 None。

    只认 _bakeproject 标记，因此中间夹着任意组织组也能正确找到工程；
    找不到时由导出层回退成场景名（所有文件夹归入同一个工程）。
    """
    if not obj:
        return None
    current = obj
    while True:
        parent = m2m_scene_parent_transform(current)
        if not parent:
            return None
        if m2m_naming_is_bake_project(parent):
            return parent
        current = parent


def m2m_scene_has_classified_mesh(group):
    """组内（递归）是否至少有一个 Toolbag 可识别的烘焙物件。"""
    return any(m2m_naming_is_classified(obj) for obj in m2m_scene_project_contents(group))


def m2m_scene_long_key(obj):
    """节点的完整路径（小写），用于可靠比较带命名空间/重名的物件。"""
    names = cmds.ls(obj, long=True) or [obj]
    return names[0].lower()


def m2m_scene_bake_container_of(obj):
    """obj 该导出到哪个组：最近的 _bakegroup，没有则最近的 _bakeproject。

    单层兼容：_bakeproject 组直接装网格时（没有 _bakegroup 收走），该工程组
    同时充当导出容器，一个工程组就是一个独立 Bake Project。
    两级都不在时返回 None（松散物件）。
    """
    return m2m_scene_bake_folder_of(obj) or m2m_scene_bake_project_of(obj)


def m2m_scene_container_contents(container):
    """导出容器里的烘焙变换（工程组只取没被 _bakegroup 收走的那部分）。"""
    contents = m2m_scene_project_contents(container)
    if m2m_naming_is_bake_group(container):
        return contents
    key = m2m_scene_long_key(container)
    result = []
    for obj in contents:
        found = m2m_scene_bake_container_of(obj)
        if found and m2m_scene_long_key(found) == key:
            result.append(obj)
    return result


def m2m_scene_project_contents(group):
    descendants = cmds.listRelatives(
        group, allDescendents=True, type='transform', noIntermediate=True) or []
    return m2m_scene_meshes_from(descendants)


def m2m_scene_find_high_members(base):
    group_name = m2m_naming_high_group_name(base)
    if cmds.objExists(group_name) and cmds.nodeType(group_name) == 'transform':
        return [group_name]
    result = []
    for obj in cmds.ls(type='transform') or []:
        if not m2m_scene_mesh_shapes(obj):
            continue
        short = m2m_naming_short_name(obj)
        if short == group_name or short.startswith(group_name + '_'):
            result.append(obj)
    return sorted(result, key=lambda value: value.lower())


def m2m_scene_managed_meshes():
    lows, highs = [], []
    for obj in m2m_scene_all_mesh_transforms():
        role = m2m_naming_role_of(obj)
        if role == 'low':
            lows.append(obj)
        elif role == 'high':
            highs.append(obj)
    return lows, highs


def m2m_scene_set_visibility(objs, visible):
    changed = 0
    for obj in objs:
        try:
            cmds.setAttr(obj + '.visibility', 1 if visible else 0)
            changed += 1
        except Exception:
            pass
    return changed

# ===========================================================================
# Maya2Marmoset bridge (inlined)
# ===========================================================================
# """Maya -> Toolbag command channel (shared inbox files, TCP for status only).
# 
# Stability note: Toolbag only drains its TCP socket on its own poll tick, so
# socket round-trips kept timing out whenever the window lost focus. Commands
# now go through ROOT/inbox files (written atomically via a .tmp rename); the
# listener executes them on its tick and drops a '<id>.result.json' receipt in
# ROOT/inbox/done. TCP ping and the heartbeat file remain for status checks.
# """


def m2m_bridge_announce(cfg):
    root = cfg['temp_root']
    if not os.path.isdir(root):
        os.makedirs(root)
    path = m2m_config_bridge_file(cfg)
    payload = {
        'port': int(cfg['port']),
        'root': root,
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(path, 'w') as handle:
        json.dump(payload, handle)


def m2m_bridge__connect(cfg, timeout):
    sock = socket.create_connection(('127.0.0.1', int(cfg['port'])), timeout)
    sock.settimeout(timeout)
    return sock


def m2m_bridge__request(sock, message, timeout):
    data = (json.dumps(message) + '\n').encode('utf-8')
    sock.sendall(data)
    buffer = b''
    deadline = time.time() + timeout
    while b'\n' not in buffer:
        if time.time() > deadline:
            break
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    line = buffer.split(b'\n')[0].decode('utf-8') if buffer else ''
    return json.loads(line) if line else {}


def m2m_bridge_ping(cfg, timeout=3.0):
    """TCP round-trip to the Toolbag listener.

    The listener only drains its socket on mset periodic/frame callbacks,
    which can take a second or more when Toolbag is in the background, so
    the timeout must stay generous.
    """
    try:
        sock = m2m_bridge__connect(cfg, timeout)
        try:
            reply = m2m_bridge__request(sock, {'cmd': 'ping'}, timeout)
        finally:
            sock.close()
        return bool(reply.get('ok'))
    except Exception:
        return False


def m2m_bridge_heartbeat_age(cfg):
    """Seconds since the listener last polled; None when no heartbeat yet."""
    path = os.path.join(cfg['temp_root'], 'toolbag.heartbeat')
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def m2m_bridge_inbox_dir(cfg):
    """Shared command queue folder: ROOT/inbox (listener polls this)."""
    return os.path.join(cfg['temp_root'], 'inbox')


def m2m_bridge_send_commands(commands, cfg):
    """Write commands to the shared inbox; return their id list.

    Never blocks on Toolbag: each command becomes
    inbox/<毫秒时间戳>_<序号>_<cmd>.json, written to a .tmp name and
    atomically renamed in, so the listener never reads a half-written file.
    The id is also stored inside the JSON ('id') for the receipt lookup.
    """
    inbox = m2m_bridge_inbox_dir(cfg)
    if not os.path.isdir(inbox):
        os.makedirs(inbox)
    stamp = int(time.time() * 1000)
    ids = []
    for index, command in enumerate(commands):
        name = '%d_%03d_%s' % (stamp, index, command.get('cmd', 'command'))
        payload = dict(command)
        payload['id'] = name
        final = os.path.join(inbox, name + '.json')
        tmp = final + '.tmp'
        with io.open(tmp, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
        if os.path.isfile(final):
            os.remove(final)
        os.rename(tmp, final)
        ids.append(name)
    return ids


# ---------------------------------------------------------------- Toolbag plugin

def m2m_bridge_listener_source():
    """Toolbag 监听器源码目录（监听器本体不内联，它跑在 Toolbag 的解释器里）。

    脚本编辑器里 exec 本文件时没有 __file__，所以再退回 cwd 和 sys.path 查找；
    两种布局都支持：<目录>/toolbag/m2m_listener 和 Maya2Marmoset_1.23/toolbag/m2m_listener。
    """
    bases = []
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    bases.append(os.getcwd())
    bases.extend(entry for entry in sys.path if entry)
    for base in bases:
        for sub in (('toolbag', 'm2m_listener'),
                    ('Maya2Marmoset_1.23', 'toolbag', 'm2m_listener')):
            path = os.path.join(base, *sub)
            if os.path.isfile(os.path.join(path, '__main__.py')):
                return path
    return os.path.join(bases[0] if bases else '', 'toolbag', 'm2m_listener')


def m2m_bridge__local_appdata():
    return os.environ.get('LOCALAPPDATA') or os.path.join(
        os.path.expanduser('~'), 'AppData', 'Local')


def m2m_bridge_toolbag_program_roots():
    """已装程序可能所在的根目录：环境变量 + 常见固定盘。"""
    roots = []
    for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        value = os.environ.get(key)
        if value and value not in roots:
            roots.append(value)
    for value in ('C:/Program Files', 'D:/Program Files'):
        if value not in roots:
            roots.append(value)
    return roots


def m2m_bridge_toolbag_registry_executables():
    """从卸载信息（注册表）里挖 toolbag.exe，覆盖装到自定义目录的情况。"""
    executables = []
    if os.name != 'nt':
        return executables
    try:
        import winreg
    except ImportError:
        return executables
    uninstall_key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    wow64 = getattr(winreg, 'KEY_WOW64_64KEY', 0)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, wow64):
            try:
                root = winreg.OpenKey(hive, uninstall_key, 0,
                                      winreg.KEY_READ | view)
            except OSError:
                continue
            try:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    exe = m2m_bridge__registry_toolbag(root, name, view)
                    if exe:
                        executables.append(exe)
            finally:
                winreg.CloseKey(root)
    return executables


def m2m_bridge__registry_toolbag(root, name, view):
    """读一条卸载项，是 Toolbag 就返回它的 toolbag.exe。"""
    import winreg
    try:
        item = winreg.OpenKey(root, name, 0, winreg.KEY_READ | view)
    except OSError:
        return None
    try:
        try:
            display = winreg.QueryValueEx(item, 'DisplayName')[0]
        except OSError:
            return None
        if 'toolbag' not in display.lower():
            return None
        folder = ''
        for field in ('InstallLocation', 'UninstallString'):
            try:
                folder = (winreg.QueryValueEx(item, field)[0] or '').strip('"')
            except OSError:
                folder = ''
            if folder:
                break
        if folder and not os.path.isfile(folder):
            folder = os.path.dirname(folder)
        exe = os.path.join(folder, 'toolbag.exe') if folder else ''
        return exe if os.path.isfile(exe) else None
    finally:
        winreg.CloseKey(item)


def m2m_bridge__name_major(name):
    """'Marmoset Toolbag 5' / 'Toolbag 4' 这类名字里的主版本号。"""
    match = re.search(r'Toolbag\s*(\d+)', name, re.I)
    return int(match.group(1)) if match else None


def m2m_bridge_toolbag_version(exe):
    """读 exe 的版本资源，返回 (major, minor, patch, build)。

    安装目录名可能是 'Toolbag 4' 这种省略了 Marmoset 的写法，版本资源才是可靠依据；
    读不出来时退回目录名里的主版本号。
    """
    version = None
    if os.name == 'nt':
        try:
            import ctypes
            import struct
            size = ctypes.windll.version.GetFileVersionInfoSizeW(exe, None)
            if size:
                data = ctypes.create_string_buffer(size)
                if ctypes.windll.version.GetFileVersionInfoW(exe, 0, size, data):
                    block = ctypes.c_void_p()
                    length = ctypes.c_uint()
                    if ctypes.windll.version.VerQueryValueW(
                            data, u'\\', ctypes.byref(block), ctypes.byref(length)):
                        ms, ls = struct.unpack_from('<II',
                                                    ctypes.string_at(block.value, 52), 8)
                        version = (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        except Exception:
            version = None
    if version and any(version):
        return version
    major = m2m_bridge__name_major(os.path.basename(os.path.dirname(exe)))
    return (major, 0, 0, 0) if major else None


def m2m_bridge_installed_toolbags():
    """本机装了哪些受支持的 Toolbag，返回 [(version, exe)]，新版本在前。

    4.02 以下的 python 插件接口不完整（mset.BakerObject / callbacks），不参与对接。
    """
    candidates = []
    for root in m2m_bridge_toolbag_program_roots():
        for folder in (root, os.path.join(root, 'Marmoset')):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                exe = os.path.join(folder, name, 'toolbag.exe')
                if os.path.isfile(exe):
                    candidates.append(exe)
    candidates.extend(m2m_bridge_toolbag_registry_executables())
    installed = []
    seen = set()
    for exe in candidates:
        key = os.path.normcase(os.path.normpath(exe))
        if key in seen:
            continue
        seen.add(key)
        version = m2m_bridge_toolbag_version(exe)
        if not version or version[:3] < (4, 0, 2):
            continue
        installed.append((version, exe))
    installed.sort(key=lambda item: item[0], reverse=True)
    return installed


def m2m_bridge_toolbag_plugin_dirs(create=False):
    """每个已安装版本各自的用户插件目录，新版本在前。

    目录按主版本号推导（LOCALAPPDATA 下 Marmoset Toolbag <主版本>/plugins）；
    Toolbag 从没启动过时它自己不会建这个目录，所以要用时创建。已经存在的
    Marmoset Toolbag* 目录也一并收进来，覆盖扫描漏掉但确实装过插件的情况。
    """
    base = m2m_bridge__local_appdata()
    dirs = []
    for version, exe in m2m_bridge_installed_toolbags():
        path = os.path.join(base, 'Marmoset Toolbag %d' % version[0], 'plugins')
        if path not in dirs:
            dirs.append(path)
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            major = m2m_bridge__name_major(name)
            if not name.lower().startswith('marmoset toolbag') or not major \
                    or major < 4:
                continue
            path = os.path.join(base, name, 'plugins')
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)
    if not dirs:
        dirs.append(os.path.join(base, 'Marmoset Toolbag 4', 'plugins'))
    if create:
        for path in dirs:
            if not os.path.isdir(path):
                os.makedirs(path)
    return dirs


def m2m_bridge_write_listener(target):
    """用内嵌的 M2M_LISTENER_SOURCE 写出 <target>/__main__.py。

    内容已经一致就跳过写入；顺带清掉残留的 __pycache__，免得 Toolbag 跑到旧字节码。
    """
    # r''' 后那个换行只是排版，不属于监听器源码
    text = M2M_LISTENER_SOURCE.replace('\r\n', '\n').lstrip('\n')
    path = os.path.join(target, '__main__.py')
    stale = os.path.join(target, '__pycache__')
    if os.path.isdir(stale):
        shutil.rmtree(stale)
    if os.path.isfile(path):
        handle = io.open(path, 'rb')
        try:
            current = handle.read().replace(b'\r\n', b'\n')
        finally:
            handle.close()
        if current == text.encode('utf-8'):
            return target
    if not os.path.isdir(target):
        os.makedirs(target)
    handle = io.open(path, 'w', encoding='utf-8', newline='\n')
    try:
        handle.write(text)
    finally:
        handle.close()
    return target


def m2m_bridge_install_listener():
    """给每个已安装的 Toolbag 版本各装一份监听器，返回安装目录列表。

    Stale bytecode is excluded so an edited listener never runs from an old
    __pycache__ copy inside Toolbag's interpreter.

    磁盘源文件夹（Maya2Marmoset_1.23/toolbag）存在时优先复制它，不存在时用内嵌
    文本生成，所以那个文件夹可以整个删掉，安装按钮照常工作。

    各版本共用同一个 TEMP 桥接目录和同一个端口，监听器只在启动时绑一次，所以
    两个版本同时 Run 会抢同一份 inbox —— 一次只开一个。
    """
    source = m2m_bridge_listener_source()
    has_source = os.path.isfile(os.path.join(source, '__main__.py'))
    targets = []
    for plugins in m2m_bridge_toolbag_plugin_dirs(create=True):
        target = os.path.join(plugins, 'm2m_listener')
        if not has_source:
            targets.append(m2m_bridge_write_listener(target))
            continue
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(
            source, target,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        targets.append(target)
    return targets


def m2m_bridge_toolbag_executable():
    """要启动的 toolbag.exe：已安装版本里最新的那个。

    插件已经每个版本装了一份，所以启动哪个都能连上；固定选最新是为了每次
    行为一致（原来按 os.listdir 顺序返回，装了 4 和 5 时启动哪个看运气）。
    """
    installed = m2m_bridge_installed_toolbags()
    return installed[0][1] if installed else None


def m2m_bridge_toolbag_running():
    if os.name != 'nt':
        return False
    try:
        output = subprocess.check_output(
            ['tasklist', '/FI', 'IMAGENAME eq toolbag.exe', '/NH'],
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
        )
        return b'toolbag.exe' in output.lower()
    except Exception:
        return False


def m2m_bridge_launch_toolbag(root=None):
    """Start Toolbag normally; the listener must be run once from its plugin menu.

    监听器无窗口，运行后只在后台轮询 inbox；连接状态由 Maya 面板按心跳
    文件显示。Toolbag 没有 “从外部加载插件” 的接口（mset 只提供
    getPluginPath / shutdownPlugin），所以运行插件这一步只能在 Edit >
    Plugins 里手动做一次。Returns the executable path that was launched.
    """
    if m2m_bridge_toolbag_running():
        raise RuntimeError(
            u'Toolbag 已在运行，请在 Edit > Plugins > Refresh 后运行 m2m_listener')
    exe = m2m_bridge_toolbag_executable()
    if not exe:
        raise RuntimeError(u'未找到 Toolbag 4.02+ 的 toolbag.exe，'
                           u'请手动启动 Toolbag 并加载监听器')
    env = os.environ.copy()
    if root:
        env['M2M_ROOT'] = root
    subprocess.Popen([exe], env=env, close_fds=True)
    return exe

# ===========================================================================
# Maya2Marmoset exporter (inlined)
# ===========================================================================
# """FBX export to a stable per-scene work dir, plus manifest generation."""


m2m_exporter_MANIFEST_VERSION = 1

m2m_exporter__FBX_SETTINGS = (
    'FBXExportSmoothingGroups -v true',
    'FBXExportHardEdges -v false',
    'FBXExportTangents -v false',
    'FBXExportSmoothMesh -v true',
    'FBXExportApplyConstantKeyReducer -v false',
    'FBXExportInAscii -v false',
)


def m2m_exporter__ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def m2m_exporter__apply_fbx_settings():
    for command in m2m_exporter__FBX_SETTINGS:
        try:
            mel.eval(command)
        except Exception:
            pass


def m2m_exporter_scene_key():
    path = cmds.file(query=True, sceneName=True) or ''
    if not path:
        return 'untitled'
    digest = hashlib.md5(path.lower().encode('utf-8')).hexdigest()[:6]
    return '%s_%s' % (m2m_scene_scene_stem(), digest)


def m2m_exporter_work_dir(cfg):
    """本场景的固定导出目录：<临时目录>/<场景键>/（目录名不带时间戳）。

    同一个组每次都覆盖写同一个 <组名>.fbx，Toolbag 的烘焙文件夹才会一直只
    指向一条来源路径，重传即更新。按传送次数新建会话目录时每次都是新路径，
    quick loader 会把第二条路径并进同一个 High，现场就出现两个 high_01、
    high_02（v1.55 及之前）。
    """
    root = m2m_exporter__ensure_dir(
        os.path.join(cfg['temp_root'], m2m_exporter_scene_key()))
    m2m_bridge_announce(cfg)
    return root


def m2m_exporter__scene_dirs(root):
    """临时目录下的场景工作目录（inbox/logs 不算）。"""
    if not os.path.isdir(root):
        return []
    dirs = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name in ('inbox', 'logs'):
            continue
        dirs.append(path)
    return dirs


def m2m_exporter__cache_files(scene_dir):
    """工作目录里堆积的导出产物（fbx 和 manifest）。"""
    if not os.path.isdir(scene_dir):
        return []
    return [os.path.join(scene_dir, name) for name in sorted(os.listdir(scene_dir))
            if os.path.splitext(name)[1].lower() in ('.fbx', '.json')
            and os.path.isfile(os.path.join(scene_dir, name))]


def m2m_exporter__receipt_files(root):
    """已执行完的命令文件和回执（inbox/done 下会一直长）。"""
    done = os.path.join(root, 'inbox', 'done')
    if not os.path.isdir(done):
        return []
    return [os.path.join(done, name) for name in sorted(os.listdir(done))
            if os.path.splitext(name)[1].lower() == '.json'
            and os.path.isfile(os.path.join(done, name))]


def m2m_exporter_count_files(cfg):
    root = cfg['temp_root']
    paths = list(m2m_exporter__receipt_files(root))
    for scene_dir in m2m_exporter__scene_dirs(root):
        paths.extend(m2m_exporter__cache_files(scene_dir))
    return len(paths)


def m2m_exporter_clear_files(cfg):
    """清掉固定目录里的 fbx/manifest 和已执行的命令回执。

    FBX 是覆盖写的，删掉只会让下一次传送重新生成；已经导入 Toolbag 的模型
    不受影响，Toolbag 早把网格读进内存了。场景目录里残留的子目录是 v1.55
    及之前按传送次数建的，一起收掉。
    """
    root = cfg['temp_root']
    stats = {'files': 0, 'dirs': 0, 'root': root}
    paths = list(m2m_exporter__receipt_files(root))
    for scene_dir in m2m_exporter__scene_dirs(root):
        paths.extend(m2m_exporter__cache_files(scene_dir))
    for path in paths:
        try:
            os.remove(path)
            stats['files'] += 1
        except OSError:
            pass
    for scene_dir in m2m_exporter__scene_dirs(root):
        for item in os.listdir(scene_dir):
            stale = os.path.join(scene_dir, item)
            if os.path.isdir(stale):
                shutil.rmtree(stale, ignore_errors=True)
                stats['dirs'] += 1
        if not os.path.isdir(scene_dir):
            continue
        try:
            if not os.listdir(scene_dir):
                os.rmdir(scene_dir)
                stats['dirs'] += 1
        except OSError:
            pass
    return stats


def m2m_exporter__visibility_guard(objects):
    """Temporarily unhide exported meshes so view filters cannot drop geometry."""
    targets = []
    seen = set()
    for obj in m2m_scene_meshes_from(objects):
        parents = [obj]
        current = obj
        while True:
            current = (cmds.listRelatives(current, parent=True, fullPath=True) or [None])[0]
            if not current:
                break
            parents.append(current)
        for node in parents:
            if node in seen:
                continue
            seen.add(node)
            targets.append(node)

    saved = {}
    for node in targets:
        attribute = node + '.visibility'
        try:
            if cmds.objExists(attribute) and not cmds.getAttr(attribute):
                saved[attribute] = 0
                cmds.setAttr(attribute, 1)
        except Exception:
            pass
    return saved


def m2m_exporter__restore_visibility(saved):
    for attribute in saved:
        try:
            cmds.setAttr(attribute, 0)
        except Exception:
            pass


def m2m_exporter__ensure_fbx_plugin():
    """Force a full (non-lazy) load of fbxmaya.

    Interactive Maya may report fbxmaya as loaded while the plugin body has
    not really initialized (lazy preload), so the 'FBX export' file type is
    not registered and cmds.file(type='FBX export') fails with 'Invalid file
    type specified'. There is no reliable query flag for that state, so
    unload and re-load (loadPlugin always fully initializes); if unloading
    fails the plugin is genuinely in use and we keep it as is.
    """
    try:
        loaded = bool(cmds.pluginInfo('fbxmaya', query=True, loaded=True))
    except Exception:
        loaded = False
    if loaded:
        try:
            cmds.unloadPlugin('fbxmaya')
            loaded = False
        except Exception:
            return
    if not loaded:
        try:
            cmds.loadPlugin('fbxmaya', quiet=True)
        except Exception as error:
            raise RuntimeError(
                u'FBX 插件 fbxmaya 加载失败：%s。请在 Maya 菜单'
                u'“窗口设置/编辑器 > 插件管理器”中加载 fbxmaya.mll 后重试。'
                % error)


def m2m_exporter__export_fbx(path, objects):
    m2m_exporter__ensure_fbx_plugin()
    m2m_exporter__apply_fbx_settings()
    saved = m2m_exporter__visibility_guard(objects)
    fwd = path.replace('\\', '/')
    try:
        cmds.select(objects, replace=True)
        try:
            cmds.file(
                fwd,
                force=True,
                options='',
                type='FBX export',
                exportSelected=True,
            )
        except Exception as primary_error:
            # Fallback: the FBX plugin's own MEL exporter, which does not
            # depend on the file-type registry (the interactive session's
            # registry lookup kept failing with 'Invalid file type').
            try:
                mel.eval('FBXExport -f "%s" -s;' % fwd)
            except Exception as mel_error:
                raise RuntimeError(
                    u'FBX 导出失败：cmds.file 报 [%s]；FBXExport 报 [%s]。'
                    u'若 FBXExport 提示命令不存在，说明 fbxmaya 插件未真正'
                    u'初始化，请在插件管理器中手动“载入”它。'
                    % (primary_error, mel_error))
            if not os.path.isfile(path):
                raise RuntimeError(
                    u'FBX 导出失败：cmds.file 报 [%s]，FBXExport 也未生成'
                    u'文件，请查看脚本编辑器中的 FBX 报错。' % primary_error)
    finally:
        m2m_exporter__restore_visibility(saved)


def m2m_exporter__object_roles(objects):
    result = []
    for obj in objects:
        result.append({
            'name': m2m_naming_short_name(obj),
            'role': m2m_naming_role_of(obj) or 'none',
        })
    return result


def m2m_exporter__write_manifest(work_dir, data):
    data['version'] = m2m_exporter_MANIFEST_VERSION
    data['scene'] = cmds.file(query=True, sceneName=True) or ''
    data['work_dir'] = work_dir
    data['created'] = time.strftime('%Y-%m-%d %H:%M:%S')
    path = os.path.join(work_dir, 'manifest.json')
    with io.open(path, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def m2m_exporter__bake_names(container):
    """导出容器 -> (Bake Project 名, 工程内文件夹名)。

    Bake Project 名一律保留 Maya 的 _bakeproject 标记：标准两层用最近的
    _bakeproject 组全名（如 Shot_bakeproject），没有该父组时用
    <场景名>_bakeproject；单层兼容时容器本身就是 _bakeproject 组，工程和
    它唯一的文件夹同名（如 Props_bakeproject）。工程内文件夹名则取
    _bakegroup 组名去掉标记（如 Wood）。
    """
    if m2m_naming_is_bake_group(container):
        project = m2m_scene_bake_project_of(container)
        project_name = (m2m_naming_short_name(project) if project
                        else m2m_naming_bake_project_name(m2m_scene_scene_stem()))
        return project_name, m2m_naming_group_display_name(container)
    name = m2m_naming_short_name(container)
    return name, name


def m2m_exporter__folder_command(container, fbx, contents):
    """构造 import_project 命令：按容器组名定位 Bake Project 与文件夹。"""
    project_name, folder_name = m2m_exporter__bake_names(container)
    return {
        'cmd': 'import_project',
        'project': m2m_naming_short_name(container),
        'baker': project_name,
        'group': folder_name,
        'fbx': fbx,
        'objects': m2m_exporter__object_roles(contents),
    }


def m2m_exporter__loose_command(name, fbx, contents):
    """松散物件（不属于任何烘焙组）单独成文件夹，归到 <场景名>_bakeproject 工程。

    工程名同样带 _bakeproject 标记，和分组工具自动建的默认工程组保持一致。
    """
    return {
        'cmd': 'import_project',
        'project': name,
        'baker': m2m_naming_bake_project_name(m2m_scene_scene_stem()),
        'group': name,
        'fbx': fbx,
        'objects': m2m_exporter__object_roles(contents),
    }


def m2m_exporter__unique_containers(objects):
    """按导出容器去重，保持首次出现顺序（选中物件可能命中同一个组）。"""
    containers = []
    seen = set()
    for obj in objects:
        container = m2m_scene_bake_container_of(obj)
        if not container:
            continue
        key = m2m_scene_long_key(container)
        if key in seen:
            continue
        seen.add(key)
        containers.append(container)
    return containers


def m2m_exporter_collect_containers():
    """所有导出容器：最外层 _bakegroup，加上直接装网格的 _bakeproject。

    嵌套在其它 _bakegroup 里的标记组不再单独成层；_bakeproject 组只有在
    “组内确有网格没被任何 _bakegroup 收走” 时才自成容器（单层兼容），
    这样它不会和自己的文件夹重复导出。
    """
    groups = [obj for obj in cmds.ls(type='transform') or []
              if m2m_naming_is_bake_group(obj) and m2m_scene_has_classified_mesh(obj)]
    containers = [group for group in groups if not m2m_scene_bake_folder_of(group)]
    projects = [obj for obj in cmds.ls(type='transform') or []
                if m2m_naming_is_bake_project(obj)
                and any(m2m_naming_is_classified(member)
                        for member in m2m_scene_container_contents(obj))]
    containers.extend(projects)
    return sorted(containers, key=lambda value: m2m_naming_short_name(value).lower())


def m2m_exporter__export_container(work_dir, container, files, commands):
    """把一个导出容器写成 FBX 并追加命令；工程容器只导出它自己的网格。

    文件名只用容器名，所以同一个组第二次传送覆盖的是同一个 fbx——Toolbag
    靠这条固定路径把重传识别成更新而不是新增一套物件。
    """
    contents = m2m_scene_container_contents(container)
    if not contents:
        return
    fbx = os.path.join(work_dir, m2m_naming_safe_file_name(container) + '.fbx')
    targets = [container] if m2m_naming_is_bake_group(container) else contents
    m2m_exporter__export_fbx(fbx, targets)
    files.append(fbx)
    commands.append(m2m_exporter__folder_command(container, fbx, contents))


def m2m_exporter__finish_export(cfg, work_dir, commands, files, mode, count, skipped):
    """收尾：写 manifest、把命令写进共享队列，并组装返回结果。"""
    manifest = m2m_exporter__write_manifest(work_dir, {'mode': mode, 'commands': commands})
    ids = m2m_bridge_send_commands(commands, cfg)
    return {
        'work_dir': work_dir,
        'manifest': manifest,
        'channel': 'inbox',
        'ids': ids,
        'projects': len(commands),
        'objects': count,
        'files': files,
        'skipped': skipped,
    }


def m2m_exporter_export_all(cfg):
    containers = m2m_exporter_collect_containers()
    loose_all = [obj for obj in m2m_scene_all_mesh_transforms()
                 if not m2m_scene_bake_container_of(obj)]
    loose = [obj for obj in loose_all if m2m_naming_is_classified(obj)]
    skipped = [m2m_naming_short_name(obj) for obj in loose_all
               if not m2m_naming_is_classified(obj)]
    if not containers and not loose:
        if skipped:
            raise RuntimeError(
                u'松散物件没有 Toolbag 可识别后缀（_low/_high/_cage/_offset）：%s'
                % ', '.join(skipped))
        raise RuntimeError(u'场景中没有可导出的网格物件')

    work_dir = m2m_exporter_work_dir(cfg)
    commands, files = [], []

    for container in containers:
        m2m_exporter__export_container(work_dir, container, files, commands)

    if loose:
        name = m2m_scene_scene_stem() + '_loose'
        fbx = os.path.join(work_dir, m2m_naming_safe_file_name(name) + '.fbx')
        m2m_exporter__export_fbx(fbx, loose)
        files.append(fbx)
        commands.append(m2m_exporter__loose_command(name, fbx, loose))

    return m2m_exporter__finish_export(
        cfg, work_dir, commands, files, 'all',
        sum(len(command['objects']) for command in commands), skipped)


def m2m_exporter_export_selection(cfg):
    """Send the selection to Toolbag, one FBX per export container.

    Toolbag's Baker quick loader pairs _low/_high meshes into a bake group
    only when they arrive in the same FBX, so a selection that touches any
    mesh of a group exports that whole container. Containers keep their two
    levels: the first-level Maya group names the Bake Project, the second
    level names the folder inside it; a single-level _bakeproject group is
    its own project with one same-named folder. Loose selected meshes are
    bundled into one '<scene>_selection' FBX.
    """
    selected = m2m_scene_selected_meshes()
    if not selected:
        raise RuntimeError(u'请先选择要传送的物件')

    objects = [obj for obj in selected if m2m_naming_is_classified(obj)]
    skipped = [m2m_naming_short_name(obj) for obj in selected
               if not m2m_naming_is_classified(obj)]
    if not objects:
        raise RuntimeError(
            u'选中物件没有 Toolbag 可识别后缀（_low/_high/_cage/_offset）：%s'
            % ', '.join(skipped))

    containers = m2m_exporter__unique_containers(objects)
    loose = [obj for obj in objects if not m2m_scene_bake_container_of(obj)]

    work_dir = m2m_exporter_work_dir(cfg)
    commands, files = [], []

    for container in containers:
        m2m_exporter__export_container(work_dir, container, files, commands)

    if loose:
        name = m2m_scene_scene_stem() + '_selection'
        fbx = os.path.join(work_dir, m2m_naming_safe_file_name(name) + '.fbx')
        m2m_exporter__export_fbx(fbx, loose)
        files.append(fbx)
        commands.append(m2m_exporter__loose_command(name, fbx, loose))

    return m2m_exporter__finish_export(
        cfg, work_dir, commands, files, 'selection', len(objects), skipped)

# ===========================================================================
# Maya2Marmoset grouping (inlined)
# ===========================================================================
# -*- coding: utf-8 -*-
# """High/low detection, renaming, grouping and bake-group assembly."""

### STRIPPED: from maya import cmds

### STRIPPED: from . import detect, naming, scene


def m2m_grouping__collect_items(mode, objects):
    items = []
    for obj in objects:
        pos = m2m_scene_world_bbox_center(obj) if mode == 'bbox' else m2m_scene_world_pivot(obj)
        if pos is None:
            continue
        items.append({
            'name': m2m_naming_short_name(obj),
            'obj': obj,
            'pos': pos,
            'faces': m2m_scene_face_count(obj),
            'role': m2m_naming_role_of(obj),
        })
    return items


def m2m_grouping__group_owned_by(group_name, cluster_names):
    children = cmds.listRelatives(group_name, children=True) or []
    child_names = set(m2m_naming_short_name(child) for child in children)
    return bool(child_names) and child_names.issubset(cluster_names)


def m2m_grouping__claim_base(base, cluster_names, scene_names):
    """Avoid clobbering unrelated objects that already use the target names."""
    candidate = base
    index = 2
    while True:
        low_target = m2m_naming_low_name(candidate)
        high_target = m2m_naming_high_group_name(candidate)
        low_ok = low_target not in scene_names or low_target in cluster_names
        high_ok = (
            high_target not in scene_names or
            high_target in cluster_names or
            m2m_grouping__group_owned_by(high_target, cluster_names)
        )
        if low_ok and high_ok:
            return candidate
        candidate = '%s_%d' % (base, index)
        index += 1


def m2m_grouping__reserve(scene_names, base, high_count):
    scene_names.add(m2m_naming_low_name(base))
    scene_names.add(m2m_naming_high_group_name(base))
    if high_count > 1:
        for index in range(1, high_count + 1):
            scene_names.add(m2m_naming_high_name(base, index, high_count))


def m2m_grouping__ensure_high_group(base, highs, low_obj):
    group_name = m2m_naming_high_group_name(base)
    if cmds.objExists(group_name) and cmds.nodeType(group_name) == 'transform':
        group = group_name
    else:
        kwargs = {'empty': True, 'name': group_name}
        parents = cmds.listRelatives(low_obj, parent=True) or []
        if parents:
            kwargs['parent'] = parents[0]
        group = cmds.group(**kwargs)
    for high in highs:
        parents = cmds.listRelatives(high, parent=True) or []
        if parents and parents[0] == group:
            continue
        cmds.parent(high, group)
    return group


def m2m_grouping__high_meshes(base):
    """某个 base 下的高模网格列表（_high 组会被展开成组内每个物件）。"""
    meshes = []
    for member in m2m_scene_find_high_members(base):
        if m2m_scene_mesh_shapes(member):
            meshes.append(member)
        else:
            meshes.extend(m2m_scene_project_contents(member))
    return meshes


def m2m_grouping__assign_high_materials(base, highs, report, used=None):
    """给每个高模分配互不相同的材质球，供 Marmoset 烘焙 matID。

    高模之间共用材质会让同一张 matID 掩膜覆盖多个物件，所以这里保证每个
    高模拿到独立材质：已有且没被其它高模占用的材质保留（尊重已有材质），
    否则新建一个干净 lambert。used 记录本次已占用的材质名，跨 base 也不
    允许重名。
    """
    used = used if used is not None else set()
    for index, high in enumerate(highs):
        current = m2m_scene_material_of(high)
        if current and not m2m_scene_is_default_shader(current):
            key = m2m_naming_short_name(current)
            if key not in used:
                used.add(key)
                report.append(('info', u'%s 保留现有材质 %s' % (high, current)))
                continue
            report.append(('info', u'%s 的材质 %s 与其它高模共用，改为独立材质'
                           % (high, current)))
        material_name = base if index == 0 else '%s_%02d' % (base, index + 1)
        shader = m2m_scene_ensure_clean_material(material_name, used)
        used.add(m2m_naming_short_name(shader))
        if m2m_scene_assign_material(high, shader):
            report.append(('ok', u'材质 %s -> %s' % (high, shader)))


def m2m_grouping__apply_entry(entry, scene_names, used_materials, report):
    """把一个配对计划落到场景里：改名、多个高模打组、高模各占一个材质球。"""
    low_obj = entry['low']['obj']
    high_objs = [high['obj'] for high in entry['highs']]
    cluster_names = set(
        m2m_naming_short_name(obj) for obj in [low_obj] + high_objs)
    base = m2m_grouping__claim_base(entry['base'], cluster_names, scene_names)
    m2m_grouping__reserve(scene_names, base, len(high_objs))

    low_obj = m2m_scene_rename_short(low_obj, m2m_naming_low_name(base))
    renamed = []
    for index, obj in enumerate(high_objs):
        renamed.append(m2m_scene_rename_short(
            obj, m2m_naming_high_name(base, index + 1, len(high_objs))))

    if len(renamed) == 1:
        report.append(('ok', u'%s -> %s（单个高模，不打组）' % (
            entry['base'], m2m_naming_high_name(base, 1, 1))))
    else:
        group = m2m_grouping__ensure_high_group(base, renamed, low_obj)
        report.append(('ok', u'%s 个高模打入组 %s' % (len(renamed), group)))

    current = m2m_scene_material_of(low_obj)
    if current and not m2m_scene_is_default_shader(current):
        # 低模已有的材质名先占位，高模不会再复用它（否则高低模同 matID）
        used_materials.add(m2m_naming_short_name(current))
    m2m_grouping__assign_high_materials(base, renamed, report, used_materials)
    return {'base': base, 'low': low_obj, 'highs': renamed}


def m2m_grouping_auto_group(mode='pivot', tolerance=1.0):
    selected = m2m_scene_selected_meshes()
    if not selected:
        raise RuntimeError(u'请先选择需要识别的高/低模物件')

    items = m2m_grouping__collect_items(mode, selected)
    if len(items) < 2:
        raise RuntimeError(u'至少需要两个网格物件才能进行高低模配对')

    plan = m2m_detect_build_plan(items, tolerance)
    report = []
    for names, reason in plan['skipped']:
        report.append(('warn', u'跳过 %s（未找到配对物件）' % ', '.join(names)))

    if not plan['entries']:
        return {'report': report, 'groups': 0}

    scene_names = m2m_scene_all_transform_names()
    group_count = 0
    used_high_materials = set()

    for entry in plan['entries']:
        m2m_grouping__apply_entry(entry, scene_names, used_high_materials, report)
        group_count += 1

    return {'report': report, 'groups': group_count}


def m2m_grouping__cluster_key(obj):
    """按材质球分套的键：已带 _low/_high 后缀的按 base 名，其余按材质球名。"""
    if m2m_naming_role_of(obj):
        return 'base:' + m2m_naming_base_name(obj)
    shader = m2m_scene_material_of(obj)
    if not shader or m2m_scene_is_default_shader(shader):
        return ''
    return 'mat:' + m2m_naming_short_name(shader)


def m2m_grouping__material_items(objects):
    items = []
    for obj in m2m_scene_meshes_from(objects):
        items.append({
            'name': m2m_naming_short_name(obj),
            'obj': obj,
            'faces': m2m_scene_face_count(obj),
            'role': m2m_naming_role_of(obj),
            'key': m2m_grouping__cluster_key(obj),
        })
    return items


def m2m_grouping__clusters_by_key(items):
    """同键归成一套，保持首次出现顺序；没有键的（默认材质）单独返回。"""
    clusters = []
    index = {}
    unkeyed = []
    for item in items:
        key = item['key']
        if not key:
            unkeyed.append(item)
            continue
        if key not in index:
            index[key] = len(clusters)
            clusters.append([])
        clusters[index[key]].append(item)
    return clusters, unkeyed


def m2m_grouping_recognize_by_material(objects):
    """同一材质球就是一套高低模：面数最少的当低模，其余编号成高模。

    不看坐标和 Bbox，所以同一材质球散在场景各处也并成一套。已经带
    _low/_high 后缀的物件按 base 名成套，重复执行不会把上次分好的拆开
    （高模本来就各占一个独立材质球，再按材质分会全变成单件）。
    """
    clusters, unkeyed = m2m_grouping__clusters_by_key(m2m_grouping__material_items(objects))
    plan = m2m_detect_plan_from_clusters(clusters)
    report = []
    for names, reason in plan['skipped']:
        report.append(('warn', u'跳过 %s（未找到配对物件）' % ', '.join(names)))
    for item in unkeyed:
        report.append(('warn', u'%s 没有可用材质球，跳过' % item['name']))

    scene_names = m2m_scene_all_transform_names()
    used_materials = set()
    lows = []
    for entry in plan['entries']:
        result = m2m_grouping__apply_entry(entry, scene_names, used_materials, report)
        lows.append(result['low'])
    # 单件成簇但本来就是 _low 的：高模可能在选区外，交给烘焙组阶段再找
    for cluster in clusters:
        if len(cluster) == 1 and cluster[0]['role'] == 'low':
            lows.append(cluster[0]['obj'])

    return {'report': report, 'lows': lows, 'groups': len(plan['entries'])}


def m2m_grouping__low_material_counts(lows):
    counts = {}
    for low in lows:
        shader = m2m_scene_material_of(low)
        if not shader or m2m_scene_is_default_shader(shader):
            continue
        key = m2m_naming_short_name(shader)
        counts[key] = counts.get(key, 0) + 1
    return counts


def m2m_grouping__clean_low_material(low, base, shared, assigned, report):
    """Decision #2: lows without material or sharing one get a fresh clean material."""
    shader = m2m_scene_material_of(low)
    assigned_default = not shader or m2m_scene_is_default_shader(shader)
    if not assigned_default and not shared:
        return shader
    clean = m2m_scene_ensure_clean_material(base, assigned)
    if m2m_scene_assign_material(low, clean):
        reason = (u'没有可用材质' if assigned_default
                  else u'与其它低模共用材质 %s' % shader)
        report.append(('ok', u'%s %s，重新分配干净材质 %s' % (low, reason, clean)))
    return clean


def m2m_grouping__project_group_name(base):
    """第一层组名：优先用低模物件名，拿不到才退场景名（未保存时才是 untitled）。"""
    return m2m_naming_bake_project_name(base or m2m_scene_scene_stem())


def m2m_grouping__ensure_project_group(name):
    """返回第一层工程组（Toolbag 的 Bake Project），不存在则新建。"""
    if cmds.objExists(name) and cmds.nodeType(name) == 'transform':
        return name
    return cmds.group(empty=True, name=name)


def m2m_grouping__ensure_folder_group(material):
    """取/建第二层组（_bakegroup）；同名组已存在则直接复用。

    不再把旧版 材质_bakeproject 组改名：单层 _bakeproject 组现在按
    “一个组 = 一个独立 Bake Project” 处理，改名会破坏用户的工程分组。
    """
    name = m2m_naming_bake_group_name(material)
    if cmds.objExists(name) and cmds.nodeType(name) == 'transform':
        return name
    return cmds.group(empty=True, name=name)


def m2m_grouping_create_bake_groups(objs=None):
    """一键：按材质球识别高低模，再拼两层结构。

    第一层 <低模名>_bakeproject = Toolbag 的 Bake Project（每个 _low 各一个），
    第二层 <材质球>_bakegroup = 工程里的文件夹。低模已经在某个 _bakeproject
    里时保持原工程，只有没归组的才按上面的规则新建；没选中物件时按场景里全
    部网格走一遍。
    """
    working = objs if objs is not None else m2m_scene_selected_meshes()
    if not working:
        working = m2m_scene_all_mesh_transforms()
    recognized = m2m_grouping_recognize_by_material(working)
    report = recognized['report']
    lows = recognized['lows']
    if not lows:
        raise RuntimeError(u'同一材质球里没有可配对的高低模，请选择高/低模后重试')

    lows = sorted(lows, key=lambda value: value.lower())
    counts = m2m_grouping__low_material_counts(lows)
    assigned = set()
    used_high_materials = set()
    created = 0
    for low in lows:
        base = m2m_naming_base_name(low)
        highs = m2m_scene_find_high_members(base)
        if not highs:
            report.append(('warn', u'%s 没有对应的高模，跳过' % low))
            continue

        current = m2m_scene_material_of(low)
        shared = bool(current) and not m2m_scene_is_default_shader(current) and (
            counts.get(m2m_naming_short_name(current), 0) > 1)
        material = m2m_grouping__clean_low_material(low, base, shared, assigned, report)
        assigned.add(m2m_naming_short_name(material))
        # 低模材质名先占位，高模不会再复用它（否则高低模同 matID）
        used_high_materials.add(m2m_naming_short_name(material))
        # 每个高模单独一个材质球（matID 需要），已在别处分配过的保留
        m2m_grouping__assign_high_materials(base, m2m_grouping__high_meshes(base), report,
                               used_high_materials)
        group = m2m_grouping__ensure_folder_group(material)
        group_name = m2m_naming_short_name(group)

        # 每个 _low 一个工程：低模已归组就跟着它，其次跟着已存在的文件夹组
        project = m2m_scene_bake_project_of(low) or m2m_scene_bake_project_of(group)
        if not project:
            project = m2m_grouping__ensure_project_group(m2m_grouping__project_group_name(base))
            report.append(('info', u'第一层烘焙工程组（Bake Project）：%s'
                           % project))

        parents = cmds.listRelatives(group, parent=True) or []
        if not parents and m2m_scene_long_key(group) != m2m_scene_long_key(project):
            cmds.parent(group, project)

        moved = []
        for member in [low] + highs:
            parents = cmds.listRelatives(member, parent=True) or []
            if parents and parents[0] == group:
                continue
            cmds.parent(member, group)
            moved.append(m2m_naming_short_name(member))

        if moved:
            created += 1
            report.append(('ok', u'%s: %s' % (group_name, u', '.join(moved))))
        else:
            report.append(('info', u'%s 已是最新状态' % group_name))

    return {'report': report, 'groups': created}


def m2m_grouping_filter_view(mode):
    lows, highs = m2m_scene_managed_meshes()
    if mode == 'low':
        m2m_scene_set_visibility(highs, False)
        m2m_scene_set_visibility(lows, True)
    elif mode == 'high':
        m2m_scene_set_visibility(lows, False)
        m2m_scene_set_visibility(highs, True)
    else:
        m2m_scene_set_visibility(lows, True)
        m2m_scene_set_visibility(highs, True)

# ===========================================================================
# Maya2Marmoset ui (inlined)
# ===========================================================================
# """Dockable Maya panel: three columns matching the MindMap layout."""


m2m_ui_WORKSPACE = 'Maya2MarmosetWorkspace'

# 状态只认证据：心跳在 5 秒内 = 监听器正在轮询；刚执行过命令/探测成功后的
# 10 秒内也算连接。心跳过期就立刻降级，同时后台做一次 TCP 探测，避免
# “Toolbag 其实已经不响应，面板却还显示已连接”。
m2m_ui_HEARTBEAT_FRESH = 5.0
m2m_ui_CONTACT_GRACE = 10.0
m2m_ui_PROBE_TIMEOUT = 1.5

# 配对阈值上限（场景单位），滑杆按 0.01 精度映射到 0 ~ 10000
m2m_ui_TOLERANCE_MAX = 100.0

m2m_ui_STYLE = """
#title { font-size: 18px; font-weight: 700; }
#subtitle { color: gray; }
QLabel#hint { color: gray; font-size: 11px; }
QLabel#path { color: gray; font-size: 11px; }
QGroupBox {
    border: 1px solid palette(mid);
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton[accent="true"] {
    background: #6a2022;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 7px 10px;
    font-weight: 600;
}
QPushButton[accent="true"]:hover { background: #892b2d; }
QPushButton[accent="true"]:pressed { background: #451516; }
QPushButton[seg="true"]:checked {
    background: #e5383b;
    color: white;
    border-color: #e5383b;
}
"""


class m2m_ui_Panel(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super(m2m_ui_Panel, self).__init__(parent)
        self.cfg = m2m_config_load()
        self.setStyleSheet(m2m_ui_STYLE)
        self._build()
        self._refresh_materials()
        self._update_path_label()
        self._check_running_at = 0.0
        self._toolbag_running = False
        self._last_contact = 0.0
        self._probe = None
        self._probe_result = None
        self._probe_at = 0.0
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._refresh_status_light)
        self._status_timer.start()
        self._refresh_status_light()

    # ---------------------------------------------------------------- layout
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        header = QtWidgets.QVBoxLayout()
        header.setSpacing(0)
        title = QtWidgets.QLabel('Maya2Marmoset')
        title.setObjectName('title')
        subtitle = QtWidgets.QLabel(
            u'高低模自动识别 · 烘焙组分配 · Marmoset Toolbag 一键对接')
        subtitle.setObjectName('subtitle')
        header.addWidget(title)
        header.addWidget(subtitle)
        root.addLayout(header)

        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(10)
        columns.addWidget(self._build_detect_column(), 1)
        columns.addWidget(self._build_bake_column(), 1)
        columns.addWidget(self._build_export_column(), 1)
        root.addLayout(columns, 1)

        # self.log_view = QtWidgets.QPlainTextEdit()
        # self.log_view.setReadOnly(True)
        # self.log_view.setMaximumBlockCount(800)
        # self.log_view.setMinimumHeight(92)
        # root.addWidget(self.log_view)

        footer = QtWidgets.QLabel(
            u'支持Marmoset Toolbag 4.02以上版本, 首次先点击  安装Toolbag插件  ！')
        footer.setObjectName('hint')
        root.addWidget(footer)

    def _button(self, text, accent=False, slot=None):
        button = QtWidgets.QPushButton(text)
        if accent:
            button.setProperty('accent', True)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def _hint(self, text):
        label = QtWidgets.QLabel(text)
        label.setObjectName('hint')
        label.setWordWrap(True)
        return label

    def _build_detect_column(self):
        box = QtWidgets.QGroupBox(u'01 自动分配组 · 单一烘焙组')
        layout = QtWidgets.QVBoxLayout(box)
        layout.setSpacing(8)

        mode_row = QtWidgets.QHBoxLayout()

        layout.addWidget(self._hint(
            u'使用坐标轴位置，先匹配坐标轴'))

        self.rb_pivot = QtWidgets.QRadioButton(u'按 坐标轴 位置')
        self.rb_bbox = QtWidgets.QRadioButton(u'按 模型体积')
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.addButton(self.rb_pivot)
        self.mode_group.addButton(self.rb_bbox)
        if self.cfg.get('mode') == 'bbox':
            self.rb_bbox.setChecked(True)
        else:
            self.rb_pivot.setChecked(True)
        self.rb_pivot.toggled.connect(self.on_mode_changed)
        mode_row.addWidget(self.rb_pivot)
        mode_row.addWidget(self.rb_bbox)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        layout.addWidget(QtWidgets.QLabel(
            u'配对阈值（场景单位）'))
        slider_row = QtWidgets.QHBoxLayout()
        self.tol_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        # 滑杆以 0.01 为单位，范围 0.00 ~ TOLERANCE_MAX（100）
        self.tol_slider.setRange(0, int(m2m_ui_TOLERANCE_MAX * 100))
        stored_tolerance = float(self.cfg.get('tolerance', 1.0))
        if stored_tolerance > m2m_ui_TOLERANCE_MAX:
            stored_tolerance = m2m_ui_TOLERANCE_MAX
            self.cfg['tolerance'] = stored_tolerance
        self.tol_slider.setValue(int(round(stored_tolerance * 100)))
        self.tol_value = QtWidgets.QLabel()
        self.tol_value.setMinimumWidth(56)
        self.tol_value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self._update_tol_label()
        self.tol_slider.valueChanged.connect(self.on_tolerance_changed)
        slider_row.addWidget(self.tol_slider, 1)
        slider_row.addWidget(self.tol_value)
        layout.addLayout(slider_row)

        layout.addWidget(self._hint(
            u'低模自动识别，高模自动打组并编号。'))

        self.btn_detect = self._button(u'识别并分组', accent=True, slot=self.on_auto_group)
        layout.addWidget(self.btn_detect)
        layout.addStretch(1)
        return box

    def _build_bake_column(self):
        box = QtWidgets.QGroupBox(u'02 分配多个烘焙组')
        layout = QtWidgets.QVBoxLayout(box)
        layout.setSpacing(8)

        filter_row = QtWidgets.QHBoxLayout()
        self.filter_buttons = {}
        self.filter_group = QtWidgets.QButtonGroup(self)
        for key, label in (('low', u'显示_low'), ('high', u'显示_high'), ('all', u'显示全部')):
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setProperty('seg', True)
            button.clicked.connect(
                lambda checked=False, mode=key: self.on_filter(mode))
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            filter_row.addWidget(button)
        self.filter_buttons['all'].setChecked(True)
        layout.addLayout(filter_row)

        material_row = QtWidgets.QHBoxLayout()
        self.material_combo = QtWidgets.QComboBox()
        self.material_combo.setMinimumWidth(110)
        refresh = QtWidgets.QPushButton(u'刷新')
        refresh.setFixedWidth(52)
        refresh.clicked.connect(self._refresh_materials)
        material_row.addWidget(self.material_combo, 1)
        material_row.addWidget(refresh)
        layout.addWidget(QtWidgets.QLabel(u'场景材质球'))
        layout.addLayout(material_row)

        layout.addWidget(self._button(u'赋予选中物件 / 面', slot=self.on_assign_material))
        self.btn_bake = self._button(u'按材质创建烘焙组', accent=True, slot=self.on_bake_groups)
        layout.addWidget(self.btn_bake)

        layout.addWidget(self._hint(
            u'同材质球即一套高低模：建 <低模名>_bakeproject / <材质球>_bakegroup'))
        layout.addStretch(1)
        return box

    def _build_export_column(self):
        box = QtWidgets.QGroupBox(u'03 导出到 Marmoset Toolbag')
        layout = QtWidgets.QVBoxLayout(box)
        layout.setSpacing(8)

        self.btn_send = self._button(u'传送选中物件', slot=self.on_send_selection)
        layout.addWidget(self.btn_send)
        layout.addWidget(self._button(u'导出全部', accent=True, slot=self.on_export_all))

        self.path_label = QtWidgets.QLabel()
        self.path_label.setObjectName('path')
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        action_row = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton(u'打开临时目录')
        open_button.clicked.connect(self.on_open_folder)
        action_row.addWidget(open_button)
        layout.addLayout(action_row)

        tool_row = QtWidgets.QHBoxLayout()
        install_button = QtWidgets.QPushButton(u'安装 Toolbag 插件')
        install_button.clicked.connect(self.on_install_listener)
        clear_button = QtWidgets.QPushButton(u'清理临时目录')
        clear_button.clicked.connect(self.on_clear_cache)
        tool_row.addWidget(install_button)
        tool_row.addWidget(clear_button)
        layout.addLayout(tool_row)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)
        self._set_status('offline')

        return box

    # ---------------------------------------------------------------- actions
    def on_mode_changed(self, checked):
        if not checked:
            return
        self.cfg['mode'] = 'bbox' if self.rb_bbox.isChecked() else 'pivot'
        m2m_config_save(self.cfg)

    def on_tolerance_changed(self, value):
        self.cfg['tolerance'] = value / 100.0
        m2m_config_save(self.cfg)
        self._update_tol_label()

    def _update_tol_label(self):
        self.tol_value.setText(u'%.2f' % (self.tol_slider.value() / 100.0))

    def on_auto_group(self):
        mode = 'bbox' if self.rb_bbox.isChecked() else 'pivot'
        tolerance = self.tol_slider.value() / 100.0
        result = self._run(
            u'识别并分组', lambda: m2m_grouping_auto_group(mode, tolerance))
        if result is None:
            return
        lj_info(u'识别完成：%d 组' % result['groups'])
        self._refresh_materials()

    def on_filter(self, mode):
        self._run(u'视图过滤', lambda: m2m_grouping_filter_view(mode))
        labels = {'low': u'只显示低模', 'high': u'只显示高模', 'all': u'显示全部'}
        lj_info(u'视图：%s' % labels.get(mode, mode))

    def on_assign_material(self):
        shader = self.material_combo.currentText()
        if not shader:
            lj_warn(u'请先选择材质球')
            return
        # 有选中面就按面赋予，只选物件时整物体赋予（命名材质管理共用同一函数）
        result = self._run(
            u'赋予材质',
            lambda: m2m_scene_assign_material_selection(shader))
        if result is None:
            return
        kind, count = result
        if kind is None:
            lj_warn(u'请先选择物件或面')
        elif kind == 'faces':
            lj_info(u'%d 个面已赋予 %s' % (count, shader))
        else:
            lj_info(u'%d 个物件已赋予 %s' % (count, shader))

    def on_bake_groups(self):
        result = self._run(u'创建烘焙组', m2m_grouping_create_bake_groups)
        if result is None:
            return
        lj_info(u'烘焙组完成：%d 个组' % result['groups'])


    # ---------------------------------------------------------------- export

    def on_send_selection(self):
        result = self._run(
            u'传送选中物件', lambda: m2m_exporter_export_selection(self.cfg))
        if result is not None:
            lj_info(u'传送选中物件完成')

    def on_export_all(self):
        result = self._run(u'导出全部', lambda: m2m_exporter_export_all(self.cfg))
        if result is not None:
            lj_info(u'导出全部完成')

    def closeEvent(self, event):
        """关闭面板时停掉状态轮询定时器，避免 Qt 回调落到已销毁的控件上。"""
        self._status_timer.stop()
        super(m2m_ui_Panel, self).closeEvent(event)

    def on_open_folder(self):
        folder = self.cfg['temp_root']
        if not os.path.isdir(folder):
            os.makedirs(folder)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def on_install_listener(self):
        targets = self._run(u'安装 Toolbag 插件', m2m_bridge_install_listener)
        if targets is None:
            return
        for target in targets:
            lj_info(u'监听器已安装到：%s' % target)
        if len(targets) > 1:
            lj_warn(u'检测到 %d 个 Toolbag 版本，已各装一份。两个版本共用同一份命令队列，'
                    u'请在你要用的那个里 Edit > Plugins > Run，别同时跑' % len(targets))
        if m2m_bridge_ping(self.cfg):
            self._last_contact = time.time()
            self._refresh_status_light()
            lj_warn(u'监听器已在运行；若刚更新过插件文件，请在 Toolbag 的 '
                    u'Edit > Plugins 里重新运行 m2m_listener')
            return
        if m2m_bridge_toolbag_running():
            self._refresh_status_light()
            lj_warn(u'监听器已在运行；若刚更新过插件文件，请在 Toolbag 的 '
                    u'Edit > Plugins 里重新运行 m2m_listener')
            return
        answer = cmds.confirmDialog(
            title='Maya2Marmoset',
            message=(
                u'监听器已装进每个已安装版本的插件目录。\n\n'
                u'是否现在启动 Toolbag？\n'
                u'启动后请在 Toolbag 菜单 Edit > Plugins > Refresh，'
                u'再运行 m2m_listener（插件没有弹窗，状态显示在这里）。'),
            button=[u'启动', u'稍后'],
            defaultButton=u'启动',
            cancelButton=u'稍后',
            dismissString=u'稍后')
        if answer != u'启动':
            return
        try:
            m2m_bridge_launch_toolbag(self.cfg['temp_root'])
            lj_info(u'已启动 Toolbag：请在 Edit > Plugins > Refresh 后运行 m2m_listener')
            self._refresh_status_light()
        except Exception as error:
            lj_error(u'启动 Toolbag 失败：%s' % error)

    def on_clear_cache(self):
        count = m2m_exporter_count_files(self.cfg)
        if count <= 0:
            lj_info(u'临时目录已经是空的')
            return
        answer = cmds.confirmDialog(
            title=u'清理临时目录',
            message=(
                u'将删除 %d 个临时文件（导出的 FBX / manifest / 命令回执）：\n%s\n\n'
                u'FBX 每次传送都会重新生成，已导入 Toolbag 的模型不受影响。'
                u'确定清理？' % (count, self.cfg['temp_root'])),
            button=[u'清理', u'取消'],
            defaultButton=u'取消',
            cancelButton=u'取消',
            dismissString=u'取消')
        if answer != u'清理':
            return
        result = self._run(u'清理临时目录', lambda: m2m_exporter_clear_files(self.cfg))
        if result is None:
            return
        lj_info(u'已清理 %d 个文件 / %d 个空目录' % (result['files'], result['dirs']))
        self._update_path_label()

    def _refresh_status_light(self):
        """QTimer 槽：按心跳 + 后台探测结果刷新连接状态（不阻塞 UI）。

        心跳由 Toolbag 监听器每次轮询写入，是最可靠的“还活着”证据；心跳
        过期但 Toolbag 进程还在时，起一个后台线程发一次 TCP ping，成功
        就恢复“已连接”，失败就保持“监听器未响应”，保证显示与实际一致。
        """
        now = time.time()
        if now - self._check_running_at >= 3.0:
            self._check_running_at = now
            self._toolbag_running = m2m_bridge_toolbag_running()

        if self._probe_result is not None:
            if self._probe_result:
                self._last_contact = now
            self._probe_result = None

        age = m2m_bridge_heartbeat_age(self.cfg)
        fresh = age is not None and age < m2m_ui_HEARTBEAT_FRESH
        recent = now - self._last_contact < m2m_ui_CONTACT_GRACE
        if fresh or recent:
            self._set_status('connected')
            return

        self._set_status('idle' if self._toolbag_running else 'offline')
        if self._toolbag_running:
            self._start_probe()

    def _start_probe(self):
        """后台发一次 TCP ping（监听器无响应时 ping 会等 PROBE_TIMEOUT 秒）。

        至少间隔 5 秒探测一次，避免监听器真的没运行也每 2 秒扰一次。
        """
        if self._probe is not None and self._probe.is_alive():
            return
        now = time.time()
        if now - self._probe_at < 5.0:
            return
        self._probe_at = now
        self._probe = threading.Thread(target=self._probe_worker, daemon=True)
        self._probe.start()

    def _probe_worker(self):
        """后台线程体：只做 socket 探测，结果交给定时器读取（不碰 Qt）。"""
        try:
            self._probe_result = m2m_bridge_ping(self.cfg, timeout=m2m_ui_PROBE_TIMEOUT)
        except Exception:
            self._probe_result = False

    # ---------------------------------------------------------------- helpers
    def _set_status(self, state):
        """三态状态：connected 已连接 / idle Toolbag 在跑但监听器没运行 / offline 未启动。"""
        if state == 'connected':
            text = u'✔️'
            color = '#4ec9a4'
        elif state == 'idle':
            text = u'✔️'
            color = '#e2b04a'
        else:
            text = u'❌'
            color = '#e5383b'
        self.status_label.setText(text)
        self.status_label.setStyleSheet('color:%s;' % color)

    def _refresh_materials(self):
        m2m_scene_refresh_material_combo(self.material_combo)

    def _update_path_label(self):
        self.path_label.setText(u'临时目录：%s' % self.cfg['temp_root'])

    def _run(self, title, function):
        cmds.undoInfo(openChunk=True, chunkName='M2M: ' + title)
        try:
            return function()
        except Exception as error:
            lj_error(u'%s 失败：%s' % (title, error))
            return None
        finally:
            cmds.undoInfo(closeChunk=True)


def m2m_ui_show():
    """打开 Maya2Marmoset 面板；已经有活的面板就复用，正在建就不再进来。

    原来每次点都先 deleteUI 再把整块 UI 重建成新 Panel：只要建面板的过程中又被
    调进来（Maya 恢复工作区重放、按钮重入），就一层套一层直到递归上限，部分机器
    上表现为 RecursionError。状态挂在函数属性上，避免新增模块级名字。
    """
    if getattr(m2m_ui_show, '_opening', False):
        return getattr(m2m_ui_show, '_panel', None)

    import maya.OpenMayaUI as omui

    m2m_ui_show._opening = True
    try:
        panel = getattr(m2m_ui_show, '_panel', None)
        if cmds.workspaceControl(m2m_ui_WORKSPACE, exists=True):
            if panel is not None and m2m_ui_alive(panel):
                cmds.workspaceControl(m2m_ui_WORKSPACE, e=True, restore=True)
                # 复用也要刷新：原来每次重建顺带重读材质列表和连接状态
                panel._refresh_materials()
                panel._refresh_status_light()
                return panel
            cmds.deleteUI(m2m_ui_WORKSPACE)

        cmds.workspaceControl(
            m2m_ui_WORKSPACE,
            label='Maya2Marmoset',
            retain=False,
            width=1180,
            height=560,
        )

        pointer = omui.MQtUtil.findControl(m2m_ui_WORKSPACE)
        if not pointer:
            m2m_ui_show._panel = None
            return None

        parent = wrapInstance(int(pointer), QtWidgets.QWidget)
        layout = parent.layout()
        if layout is None:
            layout = QtWidgets.QVBoxLayout(parent)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
        panel = m2m_ui_Panel(parent)
        layout.addWidget(panel)
        m2m_ui_show._panel = panel
        return panel
    finally:
        m2m_ui_show._opening = False


def m2m_ui_alive(panel):
    """面板的 C++ 侧还在不在：Maya 关掉工作区后 Python 引用就变哑对象。

    顺序和文件头的 wrapInstance 一致（先 shiboken2 再 shiboken6），不然在两套
    都在的机器上会拿错绑定、把活面板误判成死的。
    """
    for name in ('shiboken2', 'shiboken6'):
        try:
            shim = __import__(name)
        except ImportError:
            continue
        try:
            return bool(shim.isValid(panel))
        except Exception:
            continue
    return False

# ===========================================================================
# PolyravenUV v1.0.5 MEL source (embedded, 最小依赖闭包)
# 只搬运 TransferUV / Rectangulate 用到的 24 个 proc（1345 行），
# 不含原文件的 UI、布局、重叠检测、朝向等无关过程，也不 source 外部 .mel。
# 入口：prUV_TransferUV / prUV_GetSelected / prUV_RectangulateShell。
# 更新工具：替换下面这段文本即可。
# ===========================================================================

POLYRAVEN_UV_MEL = r'''
/*
Copyright (c) 2019 Szilárd Szlákó <info@polyraven.com>, All rights reserved.

SOFTWARE: Polyraven UV tools v1.0.5

AUTHOR: Szilárd Szlákó (polyraven.com), and Giuseppe Seda

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

THIS IS PRORIETARY SOFTWARE. IF YOU USE THE PERSONAL LICENSE 
YOU CANNOT DOWNLOAD OR INSTALL THE SOFTWARE FOR OTHER USERS;
EACH USER MUST INDIVIDUALLY ACQUIRE A PERSONAL LICENSE AND
DOWNLOAD AND INSTALL THE SOFTWARE.
IF YOU WOULD LIKE TO DOWNLOAD OR INSTALL THE SOFTWARE FOR
OTHER USERS IN YOUR COMPANY OR ORGANIZATION, YOU NEED 
TO ACQUIRE A STUDIO LICENSE.

A PERSONAL LICENSE OR STUDIO LICENSE CAN BE ACQUIRED
AT GUMROAD.COM/POLYRAVEN

The above copyright notice and this permission notice shall be included
 in all copies or substantial portions of the Software.
*/


global proc float[] prUV_GetBB_FromUVs(string $Input[])
{	

	float $bbTMP[];
	//string $cUVs=`polyListComponentConversion -ff -tuv $Input`;
	float $CoordList[]=`polyEditUV -q $Input`;

	$bbTMP[0]=$CoordList[0];
	$bbTMP[1]=$CoordList[0];
	$bbTMP[2]=$CoordList[1];
	$bbTMP[3]=$CoordList[1];
	$bbTMP[4]=size($CoordList);//UVCount

	int $cN;
	for($cN=2;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[0]>$CoordList[$cN])$bbTMP[0]=$CoordList[$cN];
		if($bbTMP[1]<$CoordList[$cN])$bbTMP[1]=$CoordList[$cN];
	}
	for($cN=3;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[2]>$CoordList[$cN])$bbTMP[2]=$CoordList[$cN];
		if($bbTMP[3]<$CoordList[$cN])$bbTMP[3]=$CoordList[$cN];
	}
	return $bbTMP;
}

global proc float[] prUV_GetBB_FromUV(string $Input)
{	

	float $bbTMP[];
	float $CoordList[]=`polyEditUV -q $Input`;

	$bbTMP[0]=$CoordList[0];
	$bbTMP[1]=$CoordList[0];
	$bbTMP[2]=$CoordList[1];
	$bbTMP[3]=$CoordList[1];

	int $cN;
	for($cN=2;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[0]>$CoordList[$cN])$bbTMP[0]=$CoordList[$cN];
		if($bbTMP[1]<$CoordList[$cN])$bbTMP[1]=$CoordList[$cN];
	}
	for($cN=3;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[2]>$CoordList[$cN])$bbTMP[2]=$CoordList[$cN];
		if($bbTMP[3]<$CoordList[$cN])$bbTMP[3]=$CoordList[$cN];
	}
	return $bbTMP;
}

global proc int prUV_StrPos(string $String, string $Character)
{
   int $i=0;
   
   for($i=size($String);$i>0;$i--)
   {
       
       string $SubStr = substring($String,$i,$i);
       
       if( $SubStr == $Character){return $i;}
   }

   return -1;
}

global proc string prUV_getunderfirstindex(string $InputString)
{
    	
	string $Result = $InputString;
	
    int $idA=prUV_StrPos($InputString,":");
	if($idA>-1)
	{
		int $idB=prUV_StrPos($InputString,"[");
		
		if($idB>-1 && $idB<$idA)
		{
			$Result = substring($InputString,1,$idB) + substring($InputString,$idB+1,$idA-1) + "]";
		}
	}

	return $Result;	
}

global proc prUV_TransferUV(string $referenceobject, int $type)
{

	string  $Objects[];
	int $i;
	int $n;

	$Objects = `ls -os -o -l` ;
	$n=size($Objects);
	
	if($n==0){error  "No Objects Selected";}
	else
	{
		if( `objExists $referenceobject` ) 
		{
		    select -add $referenceobject;
			DeleteHistory;
			select -r $Objects;
		
			//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit   -beginProgress
			-isInterruptable true  -status "UV Transfer..."
			-maxValue $n	-minValue 0    $gMainProgressBar;


			for($i=0;$i<$n;++$i)
			{
				if(`progressBar -query -isCancelled $gMainProgressBar`) break;	
				
				if($type>=0)
				{
					transferAttributes -transferPositions 0 -transferNormals 0 -transferUVs 2 -transferColors 0 -sampleSpace $type -sourceUvSpace "map1" -targetUvSpace "map1" -searchMethod 3 -flipUVs 0 -colorBorders 0 $referenceobject $Objects[$i];
				}
				else
				{
					transferAttributes -transferPositions 1 -transferNormals 0 -transferUVs 0 -transferColors 0 -sampleSpace 3 -sourceUvSpace "map1" -targetUvSpace "map1" -searchMethod 3 -flipUVs 0 -colorBorders 0 $referenceobject $Objects[$i];
				}

				progressBar -edit   -step 1 $gMainProgressBar;	
			}

			//free progresBar from this process
			progressBar -edit   -endProgress $gMainProgressBar;	
			DeleteHistory;
		}
		else
		{
			error "Reference object does not exists!";
		}
	}
}

global proc string prUV_GetSelected()
{
	string  $Objects[];
	int $n;
	$Objects = `ls -os -o` ;
	$n=size($Objects);
	
	if($n==0){error  "Select the object first";}
	else
	{
		if($n>1){warning  "More than one object is selected, using the last one as reference.";}
		
		return $Objects[$n-1];
		
	}
	
	return "(reference object not set)";
}

global proc string[] prUV_StringArray_shellList_2017()
{
	string $sel[] = `ls -sl`;

	ConvertSelectionToUVs;
	ConvertSelectionToUVShell;
	
	int $Cn=1;
	
	string $list[];
	int $n=0;
	
	int $PCn=size(`ls -sl`);
	
	if($PCn!=0)
	{
		int $SelUV[]=`polyEvaluate -uvc`;
		$PCn=$SelUV[0];
	}

	if($PCn!=0)
	{
		//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit
			-beginProgress
			-isInterruptable true
			-status "List UV Shells..."
			-maxValue $PCn
			-minValue 0
			$gMainProgressBar;
		
		while($Cn!=0)
		{
			string $sUVs[]=`ls -sl`;
			$Cn=`size($sUVs)`;
			int $ShellSize=0;

			if($Cn!=0)
			{
				string $first=prUV_getunderfirstindex($sUVs[0]);
				select -r $first;
				
				/*$list[$n]=$first;
				$n++;*/
				
				ConvertSelectionToUVShell;
				
				$list[$n]=stringArrayToString(`ls -sl`," ");
				$n++;

				int $SelUV[]=`polyEvaluate -uvc`;
				$Cn=$SelUV[0];

				select -tgl $sUVs;
								
				if(`progressBar -query -isCancelled $gMainProgressBar`){ break;}

			}

			progressBar -edit -step $Cn $gMainProgressBar;
			
			$PCn=$Cn;
			
		}
	progressBar -edit   -endProgress $gMainProgressBar;
	}
	
	int $s = size($list);
	//print ("\n shells: " + $s);
	
	select $sel;
	
	return $list;	
}

global proc string[] prUV_StringArray_shellList_2018()
{

	global int $MainProgressBarCancelled;
	//print " Cancelled F:";
	//print $MainProgressBarCancelled;

	string $emptyarray[];
	string $uvShellList[];

	$prUV_GlobalShellList=$emptyarray;

    ConvertSelectionToUVs;
    int $i,$j=0,$k,$cnt=0,$unique,$cancel=0;
    string $objects[]=`ls -sl -o `;

    string $uvShellListTmp[];
    string $parentsTmp[]=`listRelatives -f -p $objects`;
    string $parents[];

    for($i=0; $i<size($parentsTmp);$i++)
    {
        
        $k=0;
        $unique=1;
        while((size($parents)!=0) && (($k<size($parents)) && ($parents[$k]!=$parentsTmp[$i]))) 
        {
            $k++;
        }

        if($k!=size($parents))
            $unique=0;

        if($unique==1){
            $parents[$j]=$parentsTmp[$i];
            $j++;
        }
    }

    global string $gMainProgressBar;
    progressBar -edit
    -beginProgress
    -isInterruptable true
    -status "List UV Shells(2018)..."
    -maxValue (size($parents)*100)
    -minValue 0
    $gMainProgressBar;

    int $shellId[];
    string $objectCorr[];
    int $activeShellsIds[] = `polyEvaluate  -aus`;
    int $numShells = size($activeShellsIds);
    int $activeShellsIdsTmp[];
    int $numShellsTmp=0; 

    $j=0;
    $cnt=0;
    for($i=0; $i<size($parents) && !$MainProgressBarCancelled ;$i++)
    {

		/*if(`progressBar -query -isCancelled $gMainProgressBar`)
    	{ 
            $cancel=1;
            $MainProgressBarCancelled=1;
            print "\n Canceled At: F";
            break;
   		}*/

		/*if(`progressBar -query -isCancelled $gMainProgressBar`)
		{
			$MainProgressBarCancelled=1;
			print "\n Canceled At: F";
		}*/

    	int $PrevProgress=0;
        $activeShellsIdsTmp=`polyEvaluate -aus $parents[$i]`;
        $numShellsTmp = $numShellsTmp + size($activeShellsIdsTmp);

        $k=0;
      	for($j=0; $j<size($activeShellsIdsTmp); $j++)
        {

            $uvShellList[$cnt]=stringArrayToString(`polyEvaluate -uis $activeShellsIdsTmp[$k] $parents[$i]`," ");
            $cnt++;
            $k++;
       
    		int $cProgress=floor((float($k)/float(size($activeShellsIdsTmp)))*100.0);
			int $cStep=($cProgress-$PrevProgress);
			progressBar -edit -step  $cStep $gMainProgressBar;
			$PrevProgress=$cProgress;

        }
    }

    progressBar -edit   -endProgress $gMainProgressBar;
   
	return $uvShellList;
}

global proc string[] prUV_StringArray_shellList()
{
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		return prUV_StringArray_shellList_2017();
	}
	else
	{
		return prUV_StringArray_shellList_2018();
	}

	string $emptylist[];
	return $emptylist;
}

global proc string[] prUV_GprUV_shellList2018(string $input)
{
	string $emptyarray[];
	global string $prUV_GlobalShellList[];
	string $uvShellList[];

	if($input=="")
	{

		$prUV_GlobalShellList=$emptyarray;

	    ConvertSelectionToUVs;
	    int $i,$j=0,$k,$cnt=0,$unique,$cancel=0;
	    string $objects[]=`ls -sl -o `;

	    string $uvShellListTmp[];
	    string $parentsTmp[]=`listRelatives -f -p $objects`;
	    string $parents[];
	   

	    //Initialize default progressBar of maya
	   /* global string $gMainProgressBar;
	    progressBar -edit
	    -beginProgress
	    -isInterruptable true
	    -status "List UV Shells(2018)..."
	    -maxValue (size($parentsTmp)*2)
	    -minValue 0
	    $gMainProgressBar;*/

	    for($i=0; $i<size($parentsTmp);$i++)
	    {
	        
	        $k=0;
	        $unique=1;
	        /*progressBar -edit -step 1 $gMainProgressBar;
	        if(`progressBar -query -isCancelled $gMainProgressBar`)
	        { 
	            $cancel=1;
	            /* print "getting canceled\n";*/ 
	         //   break;
	       // }
	        while((size($parents)!=0) && (($k<size($parents)) && ($parents[$k]!=$parentsTmp[$i]))) 
	        {
	            $k++;
	        }

	        if($k!=size($parents))
	            $unique=0;

	        if($unique==1){
	            $parents[$j]=$parentsTmp[$i];
	            $j++;
	        }
	    }

	  //  if ($cancel==0)
	   // {   

	    	//progressBar -edit -maxValue (size($parents)*2) $gMainProgressBar;
	    	//progressBar -edit -progress  (size($parents)) $gMainProgressBar;

		    global string $gMainProgressBar;
		    progressBar -edit
		    -beginProgress
		    -isInterruptable true
		    -status "List UV Shells(2018)..."
		    -maxValue (size($parents)*100)
		    -minValue 0
		    $gMainProgressBar;

	        int $shellId[];
	        string $objectCorr[];
	        int $activeShellsIds[] = `polyEvaluate  -aus`;
	        int $numShells = size($activeShellsIds);
	        int $activeShellsIdsTmp[];
	        int $numShellsTmp=0; 

	        $j=0;
	        $cnt=0;
	        for($i=0; $i<size($parents);$i++)
	        {

	        	 if(`progressBar -query -isCancelled $gMainProgressBar`)
			        { 
			            $cancel=1;
			            break;
			       }

	        	int $PrevProgress=0;
	            $activeShellsIdsTmp=`polyEvaluate -aus $parents[$i]`;
	            $numShellsTmp = $numShellsTmp + size($activeShellsIdsTmp);

	            $k=0;
	           	//for($j=($numShellsTmp - size($activeShellsIdsTmp)); $j<$numShellsTmp; $j++)
	          	for($j=0; $j<size($activeShellsIdsTmp); $j++)
	            {
	                $uvShellListTmp=`polyEvaluate  -uis $activeShellsIdsTmp[$k] $parents[$i]`;

	                appendStringArray($prUV_GlobalShellList,$uvShellListTmp,size($uvShellListTmp));
	                $prUV_GlobalShellList[size($prUV_GlobalShellList)]="";

	                $uvShellList[$cnt]=$uvShellListTmp[0];
	                $cnt++;
	                $k++;
	           
            		int $cProgress=floor((float($k)/float(size($activeShellsIdsTmp)))*100.0);
					int $cStep=($cProgress-$PrevProgress);
					progressBar -edit -step  $cStep $gMainProgressBar;
					$PrevProgress=$cProgress;

	            }
	           // progressBar -edit -step 1 $gMainProgressBar;
	        }

            progressBar -edit   -endProgress $gMainProgressBar;

	  //  }

	
	    
	    
	}
	else
	{
		int $gn=0;
		int $found=0;
		for($gn=0;$gn<size($prUV_GlobalShellList) && $found==0;$gn++)
		{
			if($prUV_GlobalShellList[$gn]==$input)
			{
				int $count=0;

				while($prUV_GlobalShellList[$gn+$count]!="")
				{
					$uvShellList[size($uvShellList)]=$prUV_GlobalShellList[$gn+$count];
					$count++;
				}
				$found=1;
			}
		}  
	}



	return $uvShellList;
}

global proc string[] prUV_ShellList2017()
{
	string $sel[] = `ls -sl`;

	ConvertSelectionToUVs;
	ConvertSelectionToUVShell;
	
	int $Cn=1;
	
	string $list[];
	int $n=0;
	
	int $PCn=size(`ls -sl`);
	
	if($PCn!=0)
	{
		int $SelUV[]=`polyEvaluate -uvc`;
		$PCn=$SelUV[0];
	}

	if($PCn!=0)
	{
		//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit
			-beginProgress
			-isInterruptable true
			-status "List UV Shells..."
			-maxValue $PCn
			-minValue 0
			$gMainProgressBar;
		
		while($Cn!=0)
		{
			string $sUVs[]=`ls -sl`;
			$Cn=`size($sUVs)`;
			int $ShellSize=0;

			if($Cn!=0)
			{
				string $first=prUV_getunderfirstindex($sUVs[0]);
				select -r $first;
				
				$list[$n]=$first;
				$n++;
				
				ConvertSelectionToUVShell;
				
				int $SelUV[]=`polyEvaluate -uvc`;
				$Cn=$SelUV[0];

				select -tgl $sUVs;
								
				if(`progressBar -query -isCancelled $gMainProgressBar`){ break;}

			}

			progressBar -edit -step $Cn $gMainProgressBar;
			
			$PCn=$Cn;
			
		}
	progressBar -edit   -endProgress $gMainProgressBar;
	}
	
	int $s = size($list);
	print ("\n shells: " + $s);
	
	select $sel;
	
	return $list;	
}

global proc string[] prUV_ShellList()
{
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		return prUV_ShellList2017();
	}
	else
	{
		return prUV_GprUV_shellList2018("");
	}

	string $emptylist[];
	return $emptylist;
}

global proc prUV_StraightenEdge_Scale(string $UVList[])
{
	if(size($UVList)==0)
	{
		ConvertSelectionToUVs;
		$UVList=`ls -sl -fl`;
	}

	float $coordsdata[] = `polyEvaluate -bc2`;
	float $W = $coordsdata[1]-$coordsdata[0];
	float $H = $coordsdata[3]-$coordsdata[2];
	float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
	float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;

	int $n=0;int $Pinned=0;
	for($n=0;$n<size($UVList) && $Pinned==0;$n++)
    {
        float $v[]=`polyPinUV -q -v $UVList[$n]`;
        if($v[0]!=0)
        {
        	$Pinned=1;
            float $c[]=`polyEditUV -q $UVList[$n]`;
            $pivotU=$c[0];
            $pivotV=$c[1];
            print "\n\n\n PINNED !!! \n\n\n";
        }
    }


	if($H>$W){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 $UVList;}
	else{polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 $UVList;}

	performPolyPinSelectionUVOptions 0;

}

global proc string[] prUV_GetEndUVs()
{

    ConvertSelectionToContainedEdges;
    string $oSel[]=`ls -sl`; 
    string $E[]=`ls -sl -fl`;   
    ConvertSelectionToUVs;
    string $V[]=`ls -sl -fl`;   

    int $vS=size($V);

    int $vN=0;

    string $sV[];
    int $sVn=0;

    for($vN=0;$vN<$vS;$vN++)
    {
        select $V[$vN];
        ConvertSelectionToEdges;
        string $TmpE[]=`ls -sl -fl`;
        int $EdgeNA=size($TmpE);
        select -d $E;
        $TmpE=`ls -sl -fl`;
        int $EdgeNB=size($TmpE);

        if(($EdgeNA-$EdgeNB)==1)
        {
           $sV[$sVn]=$V[$vN];
           $sVn++;   
        }  
    }

    select -r $V;
    select -d $sV;
    string $mV[]=`ls -sl -fl`;

    select -r $sV;
    return $sV;
}

global proc string[] prUV_GetOrderedUVList(string $allUVS[],string $edges[],string $endUV[])
{
    string $el[];

    select -r $allUVS;
    GrowPolygonSelectionRegion;
    select -d $allUVS;
    string $Invalid[]=`ls -sl`;

    select -r $edges;
    GrowPolygonSelectionRegion;
    select -d $edges;
    string $InvalidEdges[]=`ls -sl`;


    select -r $endUV[0];

    appendStringArray($el,`ls -sl`,size(`ls -sl`));

    while(size($el)<size($allUVS))
    {
        select -r $el[size($el)-1];
        ConvertSelectionToEdges;
        select -d $InvalidEdges;
        ConvertSelectionToUVs;
        select -d $Invalid;
        select -d $el;
        appendStringArray($el,`ls -sl`,size(`ls -sl`));
    }

    return $el;
}

global proc float[] prUV_GetEdgeLoopPivot(string $UVList[])
{

    float $PI=3.1415926535897932384626433832795;

    select -r $UVList[0];
    select -add $UVList[size($UVList)-1];

    float $coordsdata[]=`polyEvaluate -bc2`;

    float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
    float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;

    return {$pivotU,$pivotV}; 
}

global proc float prUV_EdgeLoopAngle(string $UVList[])
{

    float $PI=3.1415926535897932384626433832795;

    float $fC[]=`polyEditUV -q $UVList[0]`;

    float $sC[]=`polyEditUV -q $UVList[size($UVList)-1]`;

    float $dU=$sC[0]-$fC[0]; float $dV=$sC[1]-$fC[1];

    print ("\n dU:" + $dU + " dV:" + $dV);

    float $Angle=(atan2($dV,$dU)*180/$PI);

    return $Angle;
}

global proc float prUV_Get3DDist(string $f,string $s)
{

    float $fC[]=`xform -q -ws -t $f`;
    float $sC[]=`xform -q -ws -t $s`;
    float $D[]={$fC[0]-$sC[0],$fC[2]-$sC[2],$fC[3]-$sC[3]};
    float $dist = sqrt(($D[0]*$D[0])+($D[1]*$D[1])+($D[2]*$D[2]));
    return $dist;
}

global proc float prUV_EdgeDistRatio(string $UVList[])
{
    float $oDist;
    float $Dist3D;

    int $n;
    string $fUV=$UVList[0];
    float $fC[]=`polyEditUV -q $fUV`;
    float $pC[]=$fC;
    for($n=1;$n<size($UVList);$n++)
    {
        string $sUV=$UVList[$n];

        float $sC[]=`polyEditUV -q $sUV`;

        float $dU=$sC[0]-$fC[0];
        float $dV=$sC[1]-$fC[1];

        $oDist += sqrt(($dV*$dV)+($dU*$dU));
        $Dist3D += prUV_Get3DDist($fUV,$sUV);

        $fUV=$sUV;
        $fC=$sC;
    }

    return ($oDist/$Dist3D);
}

global proc int prUV_StraightenEdge(string $UVList[],int $ParallelToAxes)
{ 
    int $n;
    int $PinedID=0;int $Pinned=0;int $PinnedN=0;
    for($n=0;$n<size($UVList) && $PinnedN<2;$n++)
    {
        float $v[]=`polyPinUV -q -v $UVList[$n]`;
        if($v[0]!=0)
        {
            if($PinnedN==0)
            {
                $PinedID=$n;
                $Pinned=1;
            }

            $PinnedN++;   
        }
    }

    float $tAngle=prUV_EdgeLoopAngle($UVList);
    float $dRatio=prUV_EdgeDistRatio($UVList);

    float $PI=3.1415926535897932384626433832795;

    float $cosAngle=cos($tAngle*($PI/180));
    float $sinAngle=sin($tAngle*($PI/180));

    if($ParallelToAxes)
    {
        print "\n $ParallelToAxes";
        if(abs($cosAngle)>=0.5)
        {
            $sinAngle=0;
            if($cosAngle>0)$cosAngle=1;
            if($cosAngle<0)$cosAngle=-1;
        }
        else
        {
            $cosAngle=0;
            if($sinAngle>0)$sinAngle=1;
            if($sinAngle<0)$sinAngle=-1;
        }
    }
 
    
    if($PinnedN>=1&&$ParallelToAxes)
    {
        prUV_StraightenEdge_Scale($UVList);
    }
    else
    {
        string $fUV=$UVList[$PinedID];
        float $fC[]=`polyEditUV -q $fUV`;
        float $pC[]=$fC;
        for($n=$PinedID+1;$n<size($UVList);$n++)
        {
            string $sUV=$UVList[$n];

            float $sC[]=`polyEditUV -q $sUV`;

            float $dU=$sC[0]-$fC[0];
            float $dV=$sC[1]-$fC[1];
            float $Dist = sqrt(($dV*$dV)+($dU*$dU));

            //Distance based on 3D
            /*if($Pinned==1)
            {
                float $Dist3D= prUV_Get3DDist($fUV,$sUV);
                $Dist = $Dist3D*$dRatio;
            }*/

            float $nU=($pC[0]+$Dist*$cosAngle);
            float $nV=($pC[1]+$Dist*$sinAngle);
            $pC[0]=$nU; $pC[1]=$nV;

            polyEditUV -u ($nU-$sC[0]) -v ($nV-$sC[1]) $sUV;

            $fUV=$sUV;
            $fC=$sC;
        }

        if($Pinned==1)
        {
            $fUV=$UVList[$PinedID];
            $fC=`polyEditUV -q $fUV`;
            $pC=$fC;
            $cosAngle*=-1;
            $sinAngle*=-1;
            for($n=$PinedID-1;$n>=0;$n--)
            {
                string $sUV=$UVList[$n];

                float $sC[]=`polyEditUV -q $sUV`;

                float $dU=$sC[0]-$fC[0];
                float $dV=$sC[1]-$fC[1];
                float $Dist = sqrt(($dV*$dV)+($dU*$dU));

                //Distance based on 3D
                /*
                if($Pinned==1)
                {
                    float $Dist3D= prUV_Get3DDist($fUV,$sUV);
                    $Dist = $Dist3D*$dRatio;
                }
                */

                float $nU=($pC[0]+$Dist*$cosAngle);
                float $nV=($pC[1]+$Dist*$sinAngle);
                $pC[0]=$nU; $pC[1]=$nV;

                polyEditUV -u ($nU-$sC[0]) -v ($nV-$sC[1]) $sUV;

                $fUV=$sUV;
                $fC=$sC;
            }
        }
    }
    return $Pinned;       
}

global proc prUV_StraightenSelectedEdge(int $ParallelToAxes)
{
    string $alledges[]=`ls -sl -fl`;
    ConvertSelectionToUVs;
    string $oUV[]=`ls -sl`;
    string $shells[]=prUV_ShellList();

    int $n=0;
    for($n=0;$n<size($shells);$n++)
    {
    	select -r $shells[$n];
    	ConvertSelectionToUVShell;ConvertSelectionToEdges; string $s[]=`ls -sl`;
    	select -r $alledges;select -d $s; string $n[] = `ls -sl`;
    	select -r $alledges;select -d $n; string $edges[]=`ls -sl -fl`;

	    if(size($edges)>0)
	    {
	        ConvertSelectionToUVs;
	        string $AllUV[]=`ls -sl -fl`;
	        string $endUV[]=prUV_GetEndUVs();
	        string $UVList[];

	        if(size($endUV)==2)$UVList=prUV_GetOrderedUVList($AllUV,$edges,$endUV);

	        if(size($UVList)!=0)
	        {
	            float $tPivot[]=prUV_GetEdgeLoopPivot($UVList);
	            if(prUV_StraightenEdge($UVList,$ParallelToAxes)==0)
	            {
	                float $cPivot[]=prUV_GetEdgeLoopPivot($UVList);
	                polyEditUV -u ($tPivot[0]-$cPivot[0]) -v ($tPivot[1]-$cPivot[1]) $UVList;
	            }

	            select -r $UVList;
	            performPolyPinSelectionUVOptions 0;

	        }
	    }
	}

	select -r $oUV;
}

global proc string[] prUV_GetUVShellEdgeBorderFromBorderUV(string $BorderUV[])
{

	string $pBorderEdge[]=`polyListComponentConversion -fuv -te  $BorderUV`;
	
	$pBorderEdge=`ls -fl $pBorderEdge`;
	
	string $borderEdges[];
	int $n;
	for($n=0;$n<size($pBorderEdge);$n++)
	{
		int $fine=0;
		
		/*select -r $pBorderEdge[$n];
		ConvertSelectionToUVs;
		int $uvSize[]=`polyEvaluate -uvc`;
		int $sizeUV=$uvSize[0];*/

		//string $uvs[]=`polyListComponentConversion -fe -tuv  $pBorderEdge[$n]`;
		//$uvs=`ls -fl $uvs`;
		//int $sizeUV=size($uvs);
		
		string $uvs[]=`polyEvaluate -uep $pBorderEdge[$n]`;
		$uvs=stringToStringArray($uvs[0], " ");
		$uvs=stringArrayRemoveDuplicates($uvs);
		int $sizeUV=size($uvs);
		

		if($sizeUV==4 || $sizeUV==3)$fine=1;
		else
		{

			/*select -r $pBorderEdge[$n];
			ConvertSelectionToFaces;
			int $uvSize[]=`polyEvaluate -fc`;
			int $sizeFaces=$uvSize[0];*/

			/*string $faces[]=`polyListComponentConversion -fe -tf  $pBorderEdge[$n]`;
			$faces=`ls -fl $faces`;
			int $sizeFaces=size($faces)-1;*/

			string $faces[]=`polyInfo -ef $pBorderEdge[$n]`;
			$faces=stringToStringArray($faces[0],":");
			$faces=stringToStringArray($faces[1]," ");
			int $sizeFaces=size($faces)-1;

			if($sizeUV==2 && $sizeFaces==1)$fine=1;
		}

		//if(size($uvs)==4 && size($faces)==2)$fine=1;
		//if(size($uvs)==2 && size($faces)==1)$fine=1;
		//if(size($uvs)==3 && size($faces)==2)$fine=1;
		//if(size($uvs)==2 && size($faces)==2)$fine=0;
		
		if($fine)
		{
			$borderEdges[size($borderEdges)]=$pBorderEdge[$n];
		}

	}

	return $borderEdges;
}

global proc prUV_RectangulateOneShell(string $InputUVShell,string $OriginalEdges[])
{

	string $uvShell[]=stringToStringArray($InputUVShell," ");
	select -r $uvShell;
	ConvertSelectionToUVShellBorder;
	string $uvBorder[]=`ls -sl`;
	select -tgl $uvShell;
	string $uvInShell[]=`ls -sl -fl`;
	
	string $edgeBorder[];
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		select -r $uvShell;
		ConvertSelectionToEdges;
		polySelectBorderShell 1;
		$edgeBorder=`ls -sl`;
	}
	else
	{
		$edgeBorder=prUV_GetUVShellEdgeBorderFromBorderUV($uvBorder);
	}
	
	select -r $uvShell;
	ConvertSelectionToVertices;
	ConvertSelectionToContainedEdges;
	select -deselect $edgeBorder;
	
	string $EdgeInShell[]=`ls -sl -fl`;
	
	
	if(size($OriginalEdges)!=0)
	{

		string $tmpList[]=stringArrayRemove($EdgeInShell,$OriginalEdges);
		$tmpList=stringArrayRemove($tmpList,$OriginalEdges);
		if(size($tmpList)!=0)
		{
			select -r $tmpList[0];
			polySelectEdges edgeUVLoopOrBorder;
			prUV_StraightenSelectedEdge(1);
		    ConvertSelectionToUVShell;
		    u3dUnfold -ite 1 -p 0 -bi 1 -tf 1 -ms 1024 -rs 2;
		    u3dOptimize -ite 1 -pow 1 -sa 1 -bi 1 -tf 1 -ms 1024 -rs 2;
		    UVEditorUnpinAll;
		    select -r $EdgeInShell;
		}
	}


	string $TopBorder[];
	string $BottomBorder[];
	int $tN=0; int $bN=0;
	
	string $LeftBorder[];
	string $RightBorder[];
	int $lN=0; int $rN=0;
		
	if(`size($EdgeInShell)`!=0)
	{
		
		int $SelEdge[]=`polyEvaluate -ec`;

		//Initialize default progressBar of maya
		global string $gMainProgressBar;
		progressBar -edit
		-beginProgress
		-isInterruptable true
		-status "Rectangulate Shell..."
		-maxValue $SelEdge[0]
		-minValue 0
		$gMainProgressBar;
		
		while($SelEdge[0]!=0)
		{
		
			if(`progressBar -query -isCancelled $gMainProgressBar`) break;
		
			string $edge = prUV_getunderfirstindex($EdgeInShell[0]);
			
			select -r $edge;
			polySelectEdges edgeUVLoopOrBorder;
			string $edgeloop[]=`ls -sl`;
			//int $EdgeLoopN[]=`polyEvaluate -ec`;
			//ConvertSelectionToUVs;
			string $EdgeUVs[]=`polyListComponentConversion -tuv $edgeloop`;
			

			//float $coordsdata[] = `polyEvaluate -bc2`;
			float $coordsdata[]=prUV_GetBB_FromUVs($EdgeUVs);
			float $W = $coordsdata[1]-$coordsdata[0];
			float $H = $coordsdata[3]-$coordsdata[2];
			float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
			float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
		
			if($H>$W){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 $EdgeUVs;}
			else{polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 $EdgeUVs;}
		
			

			//select -deselect $uvInShell;
			//string $uvs[]=`ls -sl -fl`;
			
			//select -r $uvs[0];
			//float $coordA[] = `polyEvaluate -bc2`;
			//select -r $uvs[1];
			//float $coordB[] = `polyEvaluate -bc2`;
			
			$EdgeUVs=`ls -fl $EdgeUVs`;
			string $uvs[]=stringArrayRemove($uvInShell,$EdgeUVs);
			print "\n $uvs: \n";
			print $uvs;
			print "\n\n";
			float $coordA[] = prUV_GetBB_FromUV($uvs[0]);
			float $coordB[] = prUV_GetBB_FromUV($uvs[1]);

			string $top;
			string $bottom;
			string $left;
			string $right;
			
			if($H>$W)
			{
				if($coordA[2]>$coordB[2])
				{
					$top=$uvs[0];
					$bottom=$uvs[1];
				}
				else
				{
					$top=$uvs[1];
					$bottom=$uvs[0];
				}

				$TopBorder[$tN]=$top;
				$BottomBorder[$bN]=$bottom;
				$tN += 1; $bN += 1;
			}
			else
			{
				if($coordA[0]>$coordB[0])
				{
					$right=$uvs[0];
					$left=$uvs[1];
				}
				else
				{
					$right=$uvs[1];
					$left=$uvs[0];
				}	
				
				$LeftBorder[$lN]=$left;
				$RightBorder[$rN]=$right;
				$lN += 1; $rN += 1;
				
			}
			
			
			//select -r $EdgeInShell;
			//select -deselect $edgeloop;
			//$EdgeInShell=`ls -sl`;
				
			$edgeloop=`ls -fl $edgeloop`;
			$EdgeInShell=stringArrayRemove($edgeloop,$EdgeInShell);


			if(`size($EdgeInShell)`!=0){$SelEdge=`polyEvaluate -ec`;}
			else {$SelEdge[0]=0;}
			
			progressBar -edit -step (size($edgeloop)) $gMainProgressBar;
			
		}
		
		progressBar -edit   -endProgress $gMainProgressBar;
		
	}
	
	int $plN=$lN; int $prN=$rN; int $ptN=$tN; int $pbN=$bN;

	{
		select -r $uvBorder;
		select -d $TopBorder;
		select -d $BottomBorder;
		select -d $LeftBorder;
		select -d $RightBorder;
		
		string $uv[]=`ls -sl -fl`;
		
		//float $coordsdata[] = `polyEvaluate -bc2`;
		float $coordsdata[] = prUV_GetBB_FromUVs($uv);
		float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
		float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
		
		//for($n=0;$n<4;$n++)
		for($n=0;$n<size($uv);$n++)
		{
			//select -r $uv[$n];
			//float $coordsdata[] = `polyEvaluate -bc2`;
			float $coord[] = prUV_GetBB_FromUV($uv[$n]);
		
			if($coord[0]<$pivotU)
			{
				if($plN==0)
				{
					$LeftBorder[$lN]=$uv[$n];
					$lN += 1;
				}
			}
			else
			{
				if($prN==0)
				{
				$RightBorder[$rN]=$uv[$n];
				$rN += 1;
				}
			}
			
			if($coord[2]>$pivotV)
			{
				if($ptN==0)
				{
				$TopBorder[$tN]=$uv[$n];
				$tN += 1;
				}
			}
			else
			{
				if($pbN==0)
				{
				$BottomBorder[$bN]=$uv[$n];
				$bN += 1;
				}
			}

		}
	}



	for($n=0;$n<4;$n++)
	{
		
		int $pn=1;
		if($n==0){select -r $TopBorder; $pn=$ptN;}
		if($n==1){select -r $BottomBorder; $pn=$pbN;}
		if($n==2){select -r $LeftBorder; $pn=$plN;}
		if($n==3){select -r $RightBorder; $pn=$prN;}
	
	
		if($pn!=0)
		{
			string $oUV[]=`ls -sl`;
			GrowPolygonSelectionRegion;
			string $gUV[]=`ls -sl`;
			select -r $oUV;
			
			ConvertSelectionToEdges;
			ConvertSelectionToUVs;
			string $cUV[]=`ls -sl`;
			
			select -d $gUV;
			select -tgl $cUV;
			
			select -d $uvInShell;
				
			if($n!=0 && $ptN!=0){select -d $TopBorder;}
			if($n!=1 && $pbN!=0){select -d $BottomBorder;}
			if($n!=2 && $plN!=0){select -d $LeftBorder;}	
			if($n!=3 && $prN!=0){select -d $RightBorder;}
			
			string $tmp[]=`ls -sl`;
			select -d $uvShell;
			select -tgl $tmp;

		}
	
		//float $coordsdata[] = `polyEvaluate -bc2`;
		float $coordsdata[] = prUV_GetBB_FromUVs(`ls -sl`);
		float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
		float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
	
		if($n==0||$n==1){polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 ;}
		if($n==2||$n==3){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 ;}
	}
}

global proc prUV_RectangulateShell()
{
	string $oSel[]=`ls -sl -fl`;

	string $OriginalEdges[];
	int $en[]=`polyEvaluate -ec`;
	if($en[0]!=0)
	{	
		ConvertSelectionToEdges;
		$OriginalEdges=`ls -sl -fl`;
		/*polySelectEdges edgeUVLoopOrBorder;
		prUV_StraightenSelectedEdge(1);
	    ConvertSelectionToUVShell;
	    u3dUnfold -ite 1 -p 0 -bi 1 -tf 1 -ms 1024 -rs 2;
	    u3dOptimize -ite 100 -pow 1 -sa 1 -bi 1 -tf 1 -ms 1024 -rs 2;
	    UVEditorUnpinAll;
	    select -r $oSel;*/
	}

	ConvertSelectionToUVs;
	string $Shells[]=prUV_StringArray_shellList();
	int $ShellN=`size($Shells)`;
	int $ShellListN=0;
	
	for($ShellListN=0;$ShellListN<$ShellN;$ShellListN++)
	{
		prUV_RectangulateOneShell($Shells[$ShellListN],$OriginalEdges);
	}
	select -r $oSel;
}
'''

# ===========================================================================
# Weight Normal (Smart Outside) MEL source (embedded)
# 逐行来自 Weight Normal_v1.0.mel（168 行，行尾已归一成 LF）。
# 「建模」面板里原「循环边流」的按钮位现在是它：mel.eval 这段文本即弹窗。
# 更新工具：直接替换下面这段文本，或把 .mel 重新粘进来。
# ===========================================================================
WEIGHT_NORMAL_MEL = r'''
// ===============================
// Weight Normal - SmartOutside only (EN UI)
// Auto-show window on source/run
// ===============================

global proc float[] GetFaceNormal() {
    string $normals[] = `polyInfo -faceNormals`;
    if (!size($normals)) return {0,0,0};
    string $buffer[];
    tokenize $normals[0] $buffer;
    float $plane[3] = {0,0,0};
    if (size($buffer) >= 5) {
        $plane[0] = $buffer[2];
        $plane[1] = $buffer[3];
        $plane[2] = $buffer[4];
    }
    return $plane;
}

global proc SmartOutside(string $InAverageCtrl, string $InAngleCtrl) {
    float $myToleranceAngle = `intSliderGrp -q -value $InAngleCtrl`;
    string $buffer[];

    polySetToFaceNormal;
    string $selectedFaces[] = `ls -sl -fl`;
    if (size($selectedFaces) == 0) {
        warning "Please select at least one face before running Smart Outside.";
        return;
    }
    string $object[] = stringToStringArray($selectedFaces[0], ".");

    if (`checkBox -q -value $InAverageCtrl`) {
        polyAverageNormal -prenormalize 1 -allowZeroNormal 0 -postnormalize 0 -distance 0.0001 -replaceNormalXYZ 1 0 0;
    }

    ConvertSelectionToVertices;
    string $selectedVerts[] = `ls -sl -fl`;

    select -r $selectedFaces;
    GrowPolygonSelectionRegion;
    select -d $selectedFaces;

    ConvertSelectionToVertices;
    string $grownVerts[] = `ls -sl -fl`;

    string $myIntersector = `stringArrayIntersector`;
    stringArrayIntersector -edit -intersect $selectedVerts $myIntersector;
    stringArrayIntersector -edit -intersect $grownVerts $myIntersector;
    string $vertsToCheck[] = `stringArrayIntersector -query $myIntersector`;
    stringArrayIntersector -edit -reset $myIntersector;

    select -r $vertsToCheck;
    ConvertSelectionToFaces;
    string $intersectFaces[] = `ls -sl -fl`;

    stringArrayIntersector -edit -intersect $selectedFaces $myIntersector;
    stringArrayIntersector -edit -intersect $intersectFaces $myIntersector;
    string $facesToCheck[] = `stringArrayIntersector -query $myIntersector`;
    stringArrayIntersector -edit -reset $myIntersector;

    string $workFace;
    for ($workFace in $facesToCheck) {
        select -r $workFace;
        int $token = `tokenize $workFace "[]" $buffer`;
        string $workFaceNumber = $buffer[$token-1];

        float $faceNormal[3] = GetFaceNormal();

        ConvertSelectionToVertices;
        string $vertsOnFace[] = `ls -sl -fl`;
        string $activeVert;

        for ($activeVert in $vertsOnFace) {
            $token = `tokenize $activeVert "[]" $buffer`;
            string $activeVertNumber = $buffer[$token-1];
            int $checkThis = stringArrayContains($activeVert, $vertsToCheck);

            if ($checkThis == 1) {
                select -r $activeVert;
                ConvertSelectionToFaces;
                select -d $workFace;
                string $closeFaces[] = `ls -sl -fl`;
                string $newFace;

                for ($newFace in $closeFaces) {
                    $token = `tokenize $newFace "[]" $buffer`;
                    string $newFaceNumber = $buffer[$token-1];

                    select -r $newFace;
                    float $tempFaceNormal[] = GetFaceNormal();
                    float $angle = dotProduct($tempFaceNormal, $faceNormal, 1);

                    if ($angle > cos($myToleranceAngle * 0.017453292519943295)) {
                        select -r ($object[0] + ".vtxFace[" + $activeVertNumber + "][" + $workFaceNumber + "]");
                        float $normals[] = `polyNormalPerVertex -q -xyz`;

                        select -r ($object[0] + ".vtxFace[" + $activeVertNumber + "][" + $newFaceNumber + "]");
                        polyNormalPerVertex -xyz $normals[0] $normals[1] $normals[2];
                    }
                }
            }
        }
    }

    select -r $selectedFaces;
}

// ===============================
// UI (English, compact layout with uniform margins)
// ===============================
global proc MangesNormalToolUI() {
    if (`window -exists MangesNormalToolWindow`) deleteUI MangesNormalToolWindow;
    if (`windowPref -ex MangesNormalToolWindow`) windowPref -remove MangesNormalToolWindow;

    window -title "Weight Normal" 
           -iconName "WN" 
           -widthHeight 400 146
           -sizeable true
           MangesNormalToolWindow;

    columnLayout -adjustableColumn true -rowSpacing 4 -columnAttach "both" 8;

        // 顶部标题
        text -label "Weight Normal" -align "center" -font "boldLabelFont" -height 22;
        separator -style "in" -height 6;

        // 平均法线行
        rowLayout -numberOfColumns 2 
                  -columnWidth2 200 60
                  -columnAlign2 "left" "left"
                  -columnAttach2 "both" "both"
                  -height 22;
            text -label "Average Connected Face" -align "left";
            string $AverageConnectedFacesCheckbox = `checkBox -label "" -value false`;
        setParent ..;

        // 容差角度
        string $ToleranceAngleSlider = `intSliderGrp
            -label "Tolerance Angle"
            -field true
            -min 1 -max 180
            -fieldMinValue 1 -fieldMaxValue 180
            -value 80
            -step 1
            -columnWidth3 200 50 120
            -columnAlign3 "left" "left" "left"
        `;

        separator -style "none" -height 2;

        // 运行按钮
        button -label "Smart Outside" 
               -height 30 
               -backgroundColor 0.4 0.6 0.85
               -command ("SmartOutside " + $AverageConnectedFacesCheckbox + " " + $ToleranceAngleSlider);

        // 底部边距（与左右边距 8px 保持一致）
        separator -style "none" -height 1;

    setParent ..;

    showWindow MangesNormalToolWindow;
}

// ===============================
// Auto-launch on run
// ===============================
MangesNormalToolUI();
'''

# ===========================================================================
# Maya2Marmoset Toolbag listener source (embedded)
# 与 Maya2Marmoset_1.23/toolbag/m2m_listener/__main__.py 逐行一致，去掉那个
# 文件夹后由「安装 Toolbag 插件」按钮直接写出到 Toolbag 插件目录。
# 更新监听器：改这里，或把源文件夹放回来（存在时优先用磁盘源）。
# ===========================================================================
M2M_LISTENER_SOURCE = r'''
# -*- coding: utf-8 -*-
"""Marmoset Toolbag listener for Maya2Marmoset.

Installed into Toolbag's user plugin folder and run from
Edit > Plugins > Refresh, then Edit > Plugins > m2m_listener.

Design notes (aligned with the official mset API, see
https://www.marmoset.co/python/reference5.html):
- No background threads. Toolbag's embedded interpreter is driven through
  mset.callbacks.onPeriodicUpdate / onFrameUpdate; the listener socket is
  non-blocking and drained on Toolbag's main thread each tick.
- The listener is headless: no mset.UIWindow. The registered mset callbacks
  keep this module alive, so nothing pops up inside Toolbag; liveness is
  reported to Maya through ROOT/toolbag.heartbeat and the TCP ping. Stop /
  update it from Edit > Plugins (unload or re-run m2m_listener) instead of a
  custom button, so no UI code runs inside Toolbag's own callbacks.
- Models are quick-loaded into the Baker (BakerObject.importModel), which
  pairs _low/_high meshes from the same FBX into a bake group. Maya's
  first-level group names the Bake Project (created through
  mset.BakerObject() when missing, like the "New Bake Project" menu item)
  and its second-level group names the bake folder inside that project.
- Re-sending a group updates it instead of adding a second set: Maya writes
  each group to the same <group>.fbx path every time, so a bake folder that
  already references that file is just reloaded (no destroy, no second
  importModel). Only when Toolbag exposes neither the source path nor a
  reload call does the listener fall back to replacing the whole folder.
- Commands arrive through the shared inbox folder (ROOT/inbox): Maya drops
  one JSON file per command, the listener executes them in file-name order
  on each tick and writes a '<id>.result.json' receipt into ROOT/inbox/done
  for Maya to poll. File queue is the primary channel because TCP round-trips
  kept timing out while Toolbag was unfocused; the socket remains only for
  ping/status.
"""

import json
import os
import socket
import tempfile
import time
import traceback

import mset

ROOT = os.environ.get('M2M_ROOT') or os.path.join(
    tempfile.gettempdir(), 'Maya2Marmoset')
LOG_PATH = os.path.join(ROOT, 'logs', 'toolbag.log')
INBOX = os.path.join(ROOT, 'inbox')
DEFAULT_PORT = 8730
INBOX_INTERVAL = 0.4
ACCEPT_PER_TICK = 4
CONN_TIMEOUT = 0.05
BIND_RETRY_DELAY = 5.0
RENAME_TIMEOUT = 5.0


def _shared_state():
    """取得跨插件重载共享的状态（挂在 mset 上，重载不丢）。

    Edit > Plugins 的 Refresh/重跑会重新 import 本模块，但 mset 模块对象
    一直存在；把状态放在 mset 上，重载后仍复用同一个 socket，也避免旧
    回调持有旧模块的失效对象引用而让 Toolbag 崩溃。
    """
    state = getattr(mset, '_m2m_listener_state', None)
    if state is None:
        state = {
            'bakers': {},
            'listener': None,
            'next_bind': 0.0,
            'last_inbox': 0.0,
            'pending_renames': [],
            'stopped': False,
        }
        try:
            setattr(mset, '_m2m_listener_state', state)
        except Exception:
            pass
    return state


_state = _shared_state()


def log(message):
    """Write a [M2M] line to both the Toolbag log and the shared file log."""
    line = '[M2M] ' + str(message)
    try:
        mset.log(line)
    except Exception:
        pass
    try:
        folder = os.path.dirname(LOG_PATH)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(LOG_PATH, 'a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
    except Exception:
        pass


def _read_port():
    """Read the TCP port announced by Maya (bridge.json), else the default."""
    path = os.path.join(ROOT, 'bridge.json')
    try:
        with open(path, encoding='utf-8') as handle:
            return int(json.load(handle).get('port') or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def _start_listener(port):
    """Bind a non-blocking loopback listener; returns True on success."""
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(8)
        listener.setblocking(False)
    except Exception as error:
        log('TCP bind failed on %s: %s' % (port, error))
        return False
    _state['listener'] = listener
    log('listening on 127.0.0.1:%d' % port)
    return True


def _stop_listener():
    listener = _state.get('listener')
    if listener is None:
        return
    try:
        listener.close()
    except Exception:
        pass
    _state['listener'] = None


def _ensure_listener(now):
    """(Re)bind the listener if it is missing, retrying every few seconds."""
    listener = _state['listener']
    if listener is not None:
        try:
            listener.getsockname()
            return
        except Exception:
            # 插件重载后旧 socket 可能已被回收，重新绑定
            _state['listener'] = None
    if now < _state['next_bind']:
        return
    if not _start_listener(_read_port()):
        _state['next_bind'] = now + BIND_RETRY_DELAY


def _read_line(connection):
    """Read one newline-terminated JSON line; raises on empty/timeout."""
    chunks = []
    size = 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        newline = chunk.find(b'\n')
        if newline >= 0:
            chunks.append(chunk[:newline])
            size += newline + 1
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > 1024 * 1024:
            raise ValueError('request too large')
    data = b''.join(chunks)
    if not data.strip():
        raise ValueError('empty request')
    return json.loads(data.decode('utf-8'))


def _reply(connection, payload):
    try:
        connection.sendall((json.dumps(payload) + '\n').encode('utf-8'))
    except OSError:
        pass


def _accept_pending():
    """Accept up to a few connections per tick and execute them inline."""
    listener = _state['listener']
    if listener is None:
        return
    for _ in range(ACCEPT_PER_TICK):
        try:
            connection, _address = listener.accept()
        except (BlockingIOError, OSError):
            return
        try:
            connection.settimeout(CONN_TIMEOUT)
            try:
                command = _read_line(connection)
            except Exception:
                log('bad request: ' + traceback.format_exc())
                _reply(connection, {'ok': False, 'error': 'bad request'})
                continue
            try:
                _handle(command)
                _reply(connection, {'ok': True})
            except Exception as error:
                log('command %s failed: %s' % (
                    command.get('cmd'), traceback.format_exc()))
                _reply(connection, {'ok': False, 'error': str(error)})
        finally:
            try:
                connection.close()
            except Exception:
                pass


def _object_name(obj):
    """物件名（对象已销毁或取不到时返回空串）。"""
    try:
        return obj.name or ''
    except Exception:
        return ''


def _alive(obj):
    """物件是否还在场景里（销毁后访问 name 会报错）。"""
    try:
        obj.name
        return True
    except Exception:
        return False


def _is_baker(obj):
    """是不是 Bake Project：按能力判断，兼容 Toolbag 的类型包装差异。"""
    return callable(getattr(obj, 'importModel', None))


def _is_target(obj):
    """是不是 Bake Project 里的烘焙文件夹（BakerTargetObject）。"""
    target_type = getattr(mset, 'BakerTargetObject', None)
    if target_type is not None:
        try:
            if isinstance(obj, target_type):
                return True
        except Exception:
            pass
    return type(obj).__name__ == 'BakerTargetObject'


def _is_bake_like(obj):
    """像烘焙文件夹：BakerTargetObject，或类型名里带 Bake 的场景物件。

    Toolbag 4 里 addGroup / quick loader 建出来的对象不一定真的是
    BakerTargetObject，所以除 isinstance 外再看类型名，避免把真正的文件夹
    漏掉（漏掉会导致“找不到新文件夹”，名字一直改不过来）。
    """
    if _is_target(obj):
        return True
    return 'bake' in type(obj).__name__.lower()


def _same_object(left, right):
    """两个 mset 对象是否指向同一个场景物件（优先比 uid）。"""
    if left is right:
        return True
    try:
        return left.uid == right.uid
    except Exception:
        return False


def _describe(obj):
    """物件调试信息：类型名 + uid + 名字，用于排查误删/导入失败。"""
    if obj is None:
        return 'None'
    try:
        uid = obj.uid
    except Exception:
        uid = '?'
    return '%s#%s(%s)' % (type(obj).__name__, uid, _object_name(obj))


def _remember_baker(baker, alias=None):
    """按（小写）工程名缓存 Bake Project，可额外登记一个期望名字。

    别名用于“改名被 Toolbag 忽略”的情况：后续同名的命令仍能复用同一个
    工程，而不是每传一次就多建一个默认名的 Bake Project。
    """
    for name in (_object_name(baker), alias or ''):
        if name:
            _state['bakers'][name.lower()] = baker
    return baker


def _legacy_name(name):
    """旧命名（历史工程名不带 _bakeproject）对应的名字，用于接管旧工程。"""
    if not name:
        return ''
    suffix = '_bakeproject'
    if name.lower().endswith(suffix):
        return name[:-len(suffix)]
    return ''


def _adopt_baker(baker, name):
    """把旧命名的 Bake Project 改名成当前命名，并刷新缓存。

    这样重复导出时旧的 Shot / Props / untitled 会被直接更新，而不是和新
    命名（Shot_bakeproject…）的工程并存。
    """
    try:
        baker.name = name
    except Exception as error:
        log('could not rename legacy bake project to %s: %s' % (name, error))
        return baker
    _remember_baker(baker, name)
    log('renamed legacy bake project to %s' % _describe(baker))
    return baker


def _find_baker(name=None):
    """按名字找 Bake Project（name 为空时取场景里任意一个），找不到返回 None。

    找不到当前名字时会再找一次旧命名（不带 _bakeproject），命中就改名接管，
    保证重新导出是更新旧工程而不是新建一个并存的工程。
    """
    key = (name or '').lower()
    if key:
        cached = _state['bakers'].get(key)
        if cached is not None:
            if _alive(cached):
                return cached
            _state['bakers'].pop(key, None)
    else:
        for cached_key, cached in list(_state['bakers'].items()):
            if cached is not None and _alive(cached):
                return cached
            _state['bakers'].pop(cached_key, None)

    finder = getattr(mset, 'findObject', None)
    if key and finder is not None:
        try:
            candidate = finder(name)
        except Exception:
            candidate = None
        if candidate is not None and _is_baker(candidate):
            return _remember_baker(candidate)

    getter = getattr(mset, 'getAllObjects', None)
    if getter is not None:
        for candidate in getter() or []:
            if not _is_baker(candidate):
                continue
            current = _object_name(candidate).lower()
            if not key or current == key:
                return _remember_baker(candidate)

    legacy = _legacy_name(name)
    if legacy:
        found = _find_baker(legacy)
        if found is not None:
            return _adopt_baker(found, name)
    return None


def _create_baker(name=None):
    """新建一个 Bake Project，并把 Maya 第一层组名设为工程名。

    Toolbag 4 没有专门的 mset 工厂函数，但 BakerObject 继承 SceneObject，
    直接构造就等同于 “New Bake Project” 菜单创建的对象；构造后校验它带
    importModel，避免在不支持构造的版本里到 quick loader 才失败。
    """
    factory = getattr(mset, 'BakerObject', None)
    if factory is None:
        raise RuntimeError(
            u'当前 Toolbag 的 mset 没有 BakerObject，无法自动新建 Bake '
            u'Project，请手动在场景面板添加烘焙工程后重新传送')
    try:
        baker = factory()
    except Exception as error:
        raise RuntimeError(
            u'自动新建 Bake Project 失败（mset.BakerObject() 报错：%s）。'
            u'请在场景面板手动添加 Bake Project 后重新传送' % error)
    if not hasattr(baker, 'importModel'):
        raise RuntimeError(
            u'自动新建 Bake Project 失败：mset.BakerObject() 返回的对象不是'
            u'可用的烘焙工程，请手动在场景面板添加 Bake Project')
    if name:
        try:
            baker.name = name
        except Exception as error:
            log('could not name bake project %s: %s' % (name, error))
        else:
            actual = _object_name(baker)
            if actual.lower() != name.lower():
                log('bake project name stayed "%s" (wanted "%s")'
                    % (actual, name))
    _remember_baker(baker, name)
    log('created a new bake project (%s)' % _describe(baker))
    return baker


def _ensure_baker(name=None, state=None):
    """取指定名字的 Bake Project，没有就新建一个并命名。

    一个 Bake Project 可以容纳多个烘焙文件夹；Maya 第一层组名就是工程
    名，所以缺工程时直接按该名字创建，第一次传送也无需手动建工程。
    state 传入一个 dict 时会被写入 created 标记，调用方据此跳过“删旧”
    步骤：刚建好的工程里没有旧文件夹，任何删除都可能误伤工程自身。
    """
    baker = _find_baker(name)
    if baker is not None:
        if state is not None:
            state['created'] = False
        return baker
    if state is not None:
        state['created'] = True
    return _create_baker(name)


def _child_by_name(container, name):
    """在容器内（递归）按精确名字找烘焙文件夹，找不到返回 None。

    只认烘焙文件夹，并显式排除容器自身：单层兼容时工程名和文件夹名会
    相同（例如 Props 工程里的 Props 文件夹），若把工程本身当成旧文件夹
    销毁，接下来的 importModel 会报 “Scene Object doesn't exist”。
    """
    if container is None or not name:
        return None
    for obj in _walk(container):
        if _same_object(obj, container) or not _is_bake_like(obj):
            continue
        if _object_name(obj) == name:
            return obj
    # 兜底：部分 Toolbag 版本里烘焙文件夹不进 getChildren，只认 findInChildren
    finder = getattr(container, 'findInChildren', None)
    if finder is not None:
        try:
            found = finder(name)
        except Exception:
            found = None
        if (found is not None and _is_bake_like(found)
                and not _same_object(found, container)
                and _object_name(found) == name):
            return found
    return None


def _remove_existing(names, baker=None):
    """销毁同一个 Bake Project 里的同名烘焙文件夹，让重复传送是更新。

    只在工程内部找（递归）：绝不全局按名字删物件，否则会误删工程本身
    或别的场景物件（之前的 Props 工程就是被这样删掉的）。
    """
    if baker is None:
        return []
    removed = []
    for name in names:
        if not name:
            continue
        existing = _child_by_name(baker, name)
        if existing is None:
            continue
        try:
            existing.destroy()
            removed.append(name)
        except Exception:
            log('could not remove %s' % name)
    if removed:
        log('updated existing objects: %s' % ', '.join(removed))
    return removed


def _remove_folder(baker, folder, names=None):
    """整组删除这次要重导的烘焙文件夹，避免同名文件夹越堆越多。

    先按 Maya 第二层组名找；找不到就按这次导出的物件名找它们所在的文件夹
    ——quick loader 建的文件夹还叫低模名（如 pCube1）时走这条路。命中后一律
    往上取到工程的直接子级再删，所以删的是整个文件夹，不是里面的 Low 子级。
    改名规则更新后（工程名带 _bakeproject），旧工程里可能还留着旧名字的
    文件夹（如 Props），这里也按旧名再找一次。
    """
    parents = _parent_map(baker)
    existing = _child_by_name(baker, folder)
    if existing is None:
        legacy = _legacy_name(folder)
        if legacy:
            existing = _child_by_name(baker, legacy)
    if existing is not None:
        candidates = [existing]
    else:
        candidates = _folders_owning(baker, names or [])
    removed = []
    seen = set()
    for node in candidates:
        target = _folder_node(baker, node, parents)
        if _same_object(target, baker) or _node_key(target) in seen:
            continue
        seen.add(_node_key(target))
        try:
            target.destroy()
        except Exception as error:
            log('could not replace bake folder %s: %s' % (folder, error))
            continue
        removed.append(_describe(target))
    if removed:
        log('replaced bake folder %s in %s'
            % (', '.join(removed), _describe(baker)))
    return bool(removed)


def _children(container):
    """场景物件的直接子级（取不到时返回空列表）。"""
    getter = getattr(container, 'getChildren', None)
    if getter is None:
        return []
    try:
        return list(getter() or [])
    except Exception:
        return []


def _walk(container, depth=4):
    """深度优先列出容器内的所有后代物件。

    quick loader 导入后，烘焙文件夹可能挂在导入出来的层级下面而不是工程
    的直接子级，所以“找旧文件夹 / 找新文件夹”都要递归，不能只看一层。
    """
    found = []
    pending = [(child, 1) for child in _children(container)]
    while pending:
        obj, level = pending.pop(0)
        if _same_object(obj, container):
            continue
        found.append(obj)
        if level < depth:
            pending.extend((child, level + 1) for child in _children(obj))
    return found


def _tree_text(container):
    """容器后代概览（类型#uid(名字)），用于排查“找不到烘焙文件夹”。"""
    items = [_describe(obj) for obj in _walk(container)]
    return ', '.join(items) if items else '(empty)'


def _node_key(obj):
    """物件的稳定标识（uid 优先），导入前后都靠它认人。"""
    try:
        return obj.uid
    except Exception:
        return id(obj)


def _child_keys(baker):
    """工程内所有后代的身份标记，用于导入前后对比找出新建的文件夹。"""
    return set(_node_key(child) for child in _walk(baker))


def _parent_map(baker):
    """工程子树的 {物件标记: 父级物件}，工程自己的直接子级父级记成 baker。"""
    parents = {}
    pending = []
    for child in _children(baker):
        key = _node_key(child)
        if key not in parents:
            parents[key] = baker
            pending.append(child)
    while pending:
        cur = pending.pop(0)
        for child in _children(cur):
            key = _node_key(child)
            if key in parents:
                continue
            parents[key] = cur
            pending.append(child)
    return parents


def _folder_node(baker, node, parents):
    """往上取到工程的直接子级——marmoset 树里的那个烘焙文件夹。

    quick loader 建的烘焙文件夹下面还挂着 High/Low/Cage 子文件夹，改名必须
    落在第一层；只看「新建的那个节点」会把 Low 子文件夹改成 Maya 的组名
    （现场就是这样多出 test 和 pCube1 两个节点）。
    """
    cur = node
    for _ in range(8):
        parent = parents.get(_node_key(cur))
        if parent is None or _same_object(parent, baker):
            break
        cur = parent
    return cur


def _folders_owning(baker, names):
    """列出含这些物件名的烘焙文件夹（工程的直接子级），保持树的顺序。

    标准两层时只会命中一个文件夹；quick loader 没有中间文件夹层时 High 和
    Low 是工程的两个直接子级，必须一起删，否则重传会留下一半旧数据。
    """
    wanted = set(name for name in names if name)
    if not wanted:
        return []
    found = []
    for child in _children(baker):
        if _object_name(child) in wanted:
            found.append(child)
            continue
        for node in _walk(child, depth=6):
            if _object_name(node) in wanted:
                found.append(child)
                break
    return found


_ROLE_FOLDER_NAMES = ('high', 'low', 'cage', 'offset', 'smooth')


def _is_role_folder(obj):
    """是不是 High/Low/Cage 这类默认子文件夹（名字必须保持默认）。

    quick loader 的子文件夹就叫 High/Low，Maya 导出的角色节点还带
    _low/_high_01 这类后缀；认出它们才不会把组名压到 Low 头上。
    """
    name = _object_name(obj).strip().lower()
    while name.rpartition('_')[2].isdigit():
        name = name.rpartition('_')[0]
    if name in _ROLE_FOLDER_NAMES:
        return True
    return any(name.endswith('_' + role) for role in _ROLE_FOLDER_NAMES)


def _rename_new_folders(baker, folder, before, quiet=False):
    """把 quick loader 刚建的烘焙文件夹改名成 Maya 第二层组名。

    quick loader 默认按低模名建文件夹，这里先用导入前后对比找出这次新建的
    烘焙物件，再往上取到工程的直接子级（= marmoset 树里的文件夹节点），把
    名字改成 Maya 第二层组名；High/Low/Cage 子文件夹因此不会被误改名。
    quick loader 没有中间文件夹层时，工程的直接子级本身就是 Low——这种
    一律跳过，让 Low 保持默认名（组名宁可不加也不能顶掉它）。
    一个 FBX 里如果有多组，第一组用原名，其余加序号后缀。
    返回 (改好的名字列表, 是否已定案)：定案表示不用再等下一帧。
    """
    parents = _parent_map(baker)
    fresh = []
    seen = set()
    kept_default = False
    for child in _walk(baker):
        if _node_key(child) in before:
            continue
        if not _is_bake_like(child) or _same_object(child, baker):
            continue
        target = _folder_node(baker, child, parents)
        if _same_object(target, baker) or _node_key(target) in seen:
            continue
        seen.add(_node_key(target))
        if _is_role_folder(target):
            kept_default = True
            continue
        fresh.append(target)
    if not fresh:
        if kept_default:
            log('sub-folders keep default names; "%s" not applied to %s'
                % (folder, _tree_text(baker)))
            return [], True
        if not quiet:
            log('no new bake folder found for %s; tree: %s'
                % (folder, _tree_text(baker)))
        return [], False
    renamed = []
    for index, target in enumerate(fresh):
        desired = folder if index == 0 else '%s_%02d' % (folder, index + 1)
        try:
            target.name = desired
            renamed.append(_object_name(target) or desired)
        except Exception as error:
            log('could not rename bake folder to %s: %s' % (desired, error))
    if renamed:
        log('bake folder renamed to %s' % ', '.join(renamed))
    return renamed, True


def _schedule_rename(baker, folder, before):
    """排队给新文件夹改名：Toolbag 可能本帧还没建好子级，需要下一帧再试。"""
    if not folder:
        return
    _state['pending_renames'].append({
        'baker': baker,
        'folder': folder,
        'before': before,
        'deadline': time.time() + RENAME_TIMEOUT,
    })


def _process_pending_renames():
    """每个 tick 重试还没命中的改名任务，超过 RENAME_TIMEOUT 秒就放弃。"""
    pending = _state['pending_renames']
    if not pending:
        return
    remain = []
    for item in pending:
        baker = item['baker']
        if not _alive(baker):
            log('dropped rename of %s: bake project disappeared' % item['folder'])
            continue
        renamed, settled = _rename_new_folders(baker, item['folder'],
                                               item['before'], quiet=True)
        if settled:
            continue
        if time.time() < item['deadline']:
            remain.append(item)
        else:
            log('gave up renaming bake folder to %s; tree: %s'
                % (item['folder'], _tree_text(baker)))
    _state['pending_renames'] = remain


_MODEL_SUFFIXES = ('.fbx', '.obj', '.gltf', '.glb')


def _source_path(obj):
    """物件的来源文件路径（读不到返回空串）。

    mset 把外部文件挂在物件上（文档里是 ExternalObject.path），但各版本属性
    名不完全一致，所以按常见写法挨个试，只认模型文件的后缀。
    """
    for attribute in ('fileName', 'filePath', 'filepath', 'path', 'sourceFile'):
        try:
            value = getattr(obj, attribute)
        except Exception:
            continue
        if isinstance(value, str) and value.lower().endswith(_MODEL_SUFFIXES):
            return value
    return ''


def _node_source(obj):
    path = _source_path(obj)
    return os.path.normcase(os.path.abspath(path)) if path else ''


def _folder_by_source(baker, fbx):
    """工程里已经引用这个 fbx 的烘焙文件夹，没有返回 None。

    Maya 端把同一个组固定写到同一个 <组名>.fbx，所以“来源路径相同”就是
    “同一个组的第二次传送”，认得出旧文件夹才谈得上就地更新。
    """
    wanted = os.path.normcase(os.path.abspath(fbx)) if fbx else ''
    if not wanted:
        return None
    for child in _children(baker):
        if _node_source(child) == wanted:
            return child
        for node in _walk(child, depth=6):
            if _node_source(node) == wanted:
                return child
    return None


def _reload_object(obj):
    """让 Toolbag 重读一遍它的来源文件；这个版本没有 reload 能力返回 False。"""
    method = getattr(obj, 'reload', None)
    if not callable(method):
        return False
    try:
        method()
    except Exception as error:
        log('reload %s failed: %s' % (_describe(obj), error))
        return False
    return True


def _reload_folder(folder):
    """整组 reload：文件已被 Maya 覆盖写，重读即更新，不会新增物件。"""
    reloaded = 0
    for node in [folder] + _walk(folder, depth=6):
        if _reload_object(node):
            reloaded += 1
    return reloaded


def _import_model(baker, path, folder=None):
    """Quick-load 一个 FBX，并把“按 Maya 组名改文件夹名”排进队列。

    quick loader 会把同一个 FBX 里的 _low/_high 配对进一个烘焙组，
    但子级可能要到下一帧才出现，所以改名交给 poll 重试完成。
    """
    before = _child_keys(baker)
    log('quick-loading %s into %s' % (os.path.basename(path), _describe(baker)))
    baker.importModel(path)
    _schedule_rename(baker, folder, before)


def _handle(command):
    """Execute one Maya command on Toolbag's main thread."""
    action = command.get('cmd')
    if action == 'ping':
        log('ping')
        return

    if action == 'import_project':
        fbx = command.get('fbx', '')
        if not os.path.isfile(fbx):
            raise RuntimeError('missing fbx: %s' % fbx)
        names = [item.get('name') for item in command.get('objects') or []]
        state = {}
        baker = _ensure_baker(command.get('baker'), state)
        folder = command.get('group')
        if not state.get('created'):
            existing = _folder_by_source(baker, fbx)
            if existing is not None and _reload_folder(existing):
                log('reloaded %s for %s (%d objects, nothing added)'
                    % (os.path.basename(fbx), _describe(existing), len(names)))
                return
            if not (folder and _remove_folder(baker, folder, names)):
                _remove_existing(names, baker)
        _import_model(baker, fbx, folder)
        log('imported %s into %s (%d objects)' % (
            command.get('project'), _object_name(baker), len(names)))
        return

    if action == 'update_objects':
        state = {}
        baker = _ensure_baker(command.get('baker'), state)
        folder = command.get('group')
        for item in command.get('objects') or []:
            name = item.get('name')
            fbx = item.get('fbx', '')
            if not os.path.isfile(fbx):
                log('missing fbx: %s' % fbx)
                continue
            if not state.get('created'):
                _remove_existing([name], baker)
            _import_model(baker, fbx, folder)
            log('updated %s of %s' % (name, command.get('project')))
        return

    log('unknown command: %s' % action)


def _move(path, folder):
    """Archive a processed inbox command file into done/ or failed/."""
    if not os.path.isdir(folder):
        os.makedirs(folder)
    target = os.path.join(folder, os.path.basename(path))
    try:
        os.replace(path, target)
    except Exception:
        try:
            os.rename(path, target)
        except Exception:
            pass


def _write_result(command_id, ok, error=None):
    """Drop inbox/done/<id>.result.json so Maya can pick up the outcome."""
    done = os.path.join(INBOX, 'done')
    if not os.path.isdir(done):
        os.makedirs(done)
    payload = {'id': command_id, 'ok': ok}
    if error:
        payload['error'] = error
    try:
        with open(os.path.join(done, '%s.result.json' % command_id),
                  'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except Exception:
        log('result write failed: ' + traceback.format_exc())


def _poll_inbox():
    """Execute pending inbox commands in file-name order.

    Maya writes each command atomically (tmp + rename); '.tmp' and
    '.result.json' names are skipped. After execution the command file
    moves to inbox/done and a '<id>.result.json' receipt is written there.
    """
    if not os.path.isdir(INBOX):
        return
    for name in sorted(os.listdir(INBOX)):
        if not name.endswith('.json') or name.endswith('.result.json'):
            continue
        path = os.path.join(INBOX, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                command = json.load(handle)
        except Exception:
            log('inbox file unreadable: %s' % name)
            _move(path, os.path.join(INBOX, 'failed'))
            continue
        command_id = command.get('id') or os.path.splitext(name)[0]
        try:
            _handle(command)
            _write_result(command_id, True)
        except Exception as error:
            log('inbox command failed: %s' % traceback.format_exc())
            _write_result(command_id, False, str(error))
        _move(path, os.path.join(INBOX, 'done'))


def _write_heartbeat():
    """Touch a heartbeat file so Maya can check liveness without TCP."""
    try:
        with open(os.path.join(ROOT, 'toolbag.heartbeat'), 'w') as handle:
            handle.write('%.3f' % time.time())
    except Exception:
        pass


def poll():
    """mset callback: drain the inbox queue and TCP status on main thread."""
    if _state['stopped']:
        return
    try:
        now = time.time()
        _write_heartbeat()
        if now - _state['last_inbox'] >= INBOX_INTERVAL:
            _state['last_inbox'] = now
            _poll_inbox()
        _process_pending_renames()
        _ensure_listener(now)
        _accept_pending()
    except Exception:
        log('tick error: ' + traceback.format_exc())


def _on_scene_loaded():
    """mset callback: drop cached Bake Projects after a scene switch."""
    _state['bakers'] = {}
    _state['pending_renames'] = []
    log('scene loaded; bake project cache cleared')


def shutdown():
    """mset callback: 释放 socket 并标记停止。

    不在这里改写 mset.callbacks：插件卸载时 Toolbag 自己会清理回调表，
    在卸载回调里反过来清空它会让 Edit > Plugins 的 Refresh 直接崩溃。
    """
    _state['stopped'] = True
    _stop_listener()
    _state['pending_renames'] = []
    log('plugin stopped')


def _register():
    """Attach poll/scene callbacks to mset; fail loudly if missing.

    只注册 onPeriodicUpdate（官方说明每秒数次），不再同时挂 onFrameUpdate：
    同一 tick 被两个回调调两次只会重复写心跳/重复扫 inbox。
    """
    callbacks = getattr(mset, 'callbacks', None)
    if callbacks is None:
        log('mset.callbacks unavailable; commands will not be processed')
        return False
    callbacks.onPeriodicUpdate = poll
    callbacks.onSceneLoaded = _on_scene_loaded
    callbacks.onShutdownPlugin = shutdown
    return True


def main():
    """Plugin entry point invoked by Edit > Plugins（无窗口，后台常驻）。

    Refresh/重跑会重新 import 本模块：复用共享状态里的 socket，并清掉旧模
    块缓存的 mset 对象引用（旧包装在重载后可能失效，继续用会崩 Toolbag）。
    """
    was_stopped = _state['stopped']
    _state['stopped'] = False
    if not was_stopped and _state['listener'] is not None:
        # 插件被重载：旧模块缓存的 mset 对象包装可能已失效，丢掉再用
        _state['bakers'] = {}
        _state['pending_renames'] = []
        log('listener reloaded; cached bake projects dropped')
    if not _register():
        return
    now = time.time()
    _ensure_listener(now)
    log('listener started (root=%s)' % ROOT)


if not os.environ.get('M2M_SKIP_AUTOSTART'):
    main()
'''

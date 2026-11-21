# coding: utf-8
"""沿边对齐与实例拖拽吸附（原 BaseAlign / Align_Edge / Instant_Drag）。

对齐逻辑：取屏幕可见网格做射线求交，找到最近边后用三点吸附把物体转到贴合边，
再把负角度规整到 90 度整数倍。

拖拽逻辑：draggerContext 拾取网格并建立 instPicker/instRot 两级组，
默认跟随鼠标吸附到命中面，Ctrl 旋转（15 度吸附），Shift 复制，Ctrl+Shift 沿边对齐。
"""

import math
import re

import maya.cmds as cmds
import maya.mel as mel
import maya.api.OpenMaya as oma
import maya.OpenMaya as om
import maya.OpenMayaUI as omui
from maya.OpenMaya import MGlobal

# --- 跨方法共享的模块级状态（原单文件里的隐式全局变量） ---
outParent = ''            # alignEdge 中临时解除父级时的原父级路径
ctx = 'Click2dTo3dCtx'    # 拖拽上下文名
screenX = 0               # 上一次拖拽的屏幕 X
screenY = 0               # 上一次拖拽的屏幕 Y
checkScreenMeshList = []  # 屏幕可见网格 shape 列表
storeCameraPosition = []  # 拾取时记录的相机世界位置
storeMeshNode = []        # 被拖拽网格的变换节点
parentDir = ''            # 被拖拽网格的原父级路径
targetMeshName = ''       # 被拖拽网格的短名
instDul = 0               # Shift 复制是否已触发
storeHitFace = ''         # 上一次命中的面
cameraFarClip = 0         # 相机远裁剪面距离
storeRotCount = 0         # 当前吸附旋转角度
lockCount = 0             # 累计旋转角度
edgeAlignRecord = 0       # 沿边对齐是否已触发


class BaseAlign:
    """沿边对齐的底层实现：屏幕可见网格筛选、最近面/边求解。"""

    def alignEdge(self):
        """把当前选中物体旋转到贴合最近的屏幕可见边，并落到命中点上。"""
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
        """求当前选中网格最近的面，以及该面上离命中点最近的边。

        :return: (面名, 世界命中点, 边名, 边上最近点)
        """
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
        """在所有屏幕可见网格里找离目标物体枢轴最近的面。

        :param mayaMesh: 目标网格变换名
        :return: (最近面名, 世界命中点)
        """
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
        """求网格上离给定世界坐标最近的面与命中点。

        :param mayaMesh: 网格变换名
        :param pos: 世界坐标，默认原点
        :return: (距离, 面组件名, 世界命中点)
        """
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
        """用 MGlobal 框选整屏，返回屏幕上可见的多边形 shape 全路径列表。

        :return: 可见 mesh shape 全路径列表
        """
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
    """沿边对齐的可实例化入口（保持与原实现一致的空子类）。"""
    pass


class Instant_Drag(BaseAlign):
    """实例拖拽吸附：draggerContext 驱动的实时吸附/旋转/复制。"""

    def __init__(self):
        """初始化时持有一个 Align_Edge 实例，供 Ctrl+Shift 沿边对齐使用。"""
        self.alignEdge = Align_Edge()

    def instantDrag(self):
        """创建并切换到拖拽上下文。"""
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
        """按下时拾取网格，把它挂进 instPicker/instRot 组并记录相机与枢轴状态。"""
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
        """松开时把网格放回原父级，删除临时组并清空选择。"""
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
        """拖拽中：默认吸附命中面，Ctrl 旋转，Shift 复制，Ctrl+Shift 沿边对齐。"""
        global screenX
        global storeMeshNode
        global storeHitFace
        global cameraFarClip
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
        """求把 +Y 轴旋到该面法线所需的欧拉角（度）。

        :param faceName: 面组件名
        :return: (rx, ry, rz) 欧拉角，单位为度
        """
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

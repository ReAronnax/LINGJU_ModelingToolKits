# coding: utf-8
"""
    Author: Reggie Tu
Date: July 31, 2024

version: v1.04
     1. update UI
     2. update smart clean combine(now can merge)
     3. add split around component
     4.
"""


import maya.cmds as cmds
import re
from functools import partial
import math
import maya.mel as mel
import maya.api.OpenMaya as oma
import maya.OpenMaya as om
import maya.OpenMayaUI as omui
from maya.OpenMaya import MGlobal


class BaseAlign:

    def alignEdge(self):
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

    def getPolyFaceCenter(faceName):
        meshFaceName = faceName.split('.')[0]
        findVtx = cmds.polyInfo(faceName, fv=1)
        getNumber = []
        checkNumber = ((findVtx[0].split(':')[1]).split('\n')[0]).split(' ')
        for c in checkNumber:
            findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
            if findNumber:
                getNumber.append(findNumber)
        centerX = 0
        centerY = 0
        centerZ = 0
        for g in getNumber:
            x, y, z = cmds.pointPosition((meshFaceName + '.vtx[' + g + ']'), w=1)
            centerX = centerX + x
            centerY = centerY + y
            centerZ = centerZ + z

        centerX = centerX / len(getNumber)
        centerY = centerY / len(getNumber)
        centerZ = centerZ / len(getNumber)
        return centerX, centerY, centerZ


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
        cmds.draggerContext(ctx, pressCommand=self.instDragPick, rc=self.instDragClean, dragCommand=self.instDragMove, name=ctx,
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


class Even_Edge_Loop:
    def evenEdgeLoopDoitRun(self, smoothType):
        sel = cmds.ls(sl=1, fl=1)
        if sel:
            shape = cmds.listRelatives(sel, p=1)
            cmds.displaySmoothness(divisionsU=0, divisionsV=0, pointsWire=4, pointsShaded=1, polygonObject=1)
            sortEdgeLoopGrp = self.getEdgeRingGroup(0, '')
            for s in sortEdgeLoopGrp:
                cmds.select(s)
                self.evenEdgeLoopDoit(smoothType)
            cmds.select(sel)
            cmd = 'doMenuComponentSelection("' + shape[0] + '", "edge");'
            mel.eval(cmd)
            cmds.select(sel)

    def evenEdgeLoopDoit(self, smoothType):
        if cmds.objExists('tempEvenCurve'):
            cmds.delete('tempEvenCurve')
        sel = cmds.ls(sl=1, fl=1)

        getCircleState, listVtx = self.vtxLoopOrderCheck()
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
            if smoothType == '2P':
                if len(curveCVs) > 3:
                    cmds.delete(curveCVs[1:-1])
                    cmds.rebuildCurve('tempEvenCurve', ch=0, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0, s=2, d=2,
                                      tol=0)
                    midA = len(listVtx) / 3
                    midB = len(listVtx) / 3 * 2
                    midA = int(midA)
                    midB = int(midB)
                    posA = cmds.xform(listVtx[midA], q=1, ws=1, t=1)
                    posB = cmds.xform(listVtx[midB], q=1, ws=1, t=1)
                    cmds.xform('tempEvenCurve.cv[1]', a=1, ws=1, t=(posA[0], posA[1], posA[2]))
                    cmds.xform('tempEvenCurve.cv[2]', a=1, ws=1, t=(posB[0], posB[1], posB[2]))
                    cmds.rebuildCurve('tempEvenCurve', ch=0, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0,
                                      s=(len(listVtx) - 1), d=1, tol=0)
                    curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
            elif smoothType == 'straighten':
                cmds.delete(curveCVs[1:-1])
                newNumber = (len(listVtx) - 1)
                cmds.rebuildCurve('tempEvenCurve', ch=0, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0, s=newNumber, d=1,
                                  tol=0)
                if newNumber == 2:
                    cmds.delete('tempEvenCurve.cv[1]', 'tempEvenCurve.cv[3]')
                curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
            else:
                cmds.rebuildCurve('tempEvenCurve', ch=1, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0, s=0, d=1, tol=0)
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

    def getEdgeRingGroup(self, listSort, listInput):
        selEdges = cmds.ls(sl=1, fl=1)
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
            f = list(map(lambda x: (trans + ".e[" + str(x) + "]"), f))
            retEdges.append(f)
        if listSort == 1:
            sortEdgeLoopOrder = []
            getCircleState, listVtx = self.vtxLoopOrderCheck(listInput)
            for l in listVtx:
                for r in retEdges:
                    checkCvList = cmds.ls(cmds.polyListComponentConversion(r, fe=True, tv=True), fl=True, l=True)
                    if l in checkCvList:
                        sortEdgeLoopOrder.append(r)
            return sortEdgeLoopOrder
        else:
            return retEdges

    def vtxLoopOrderCheck(self):
        selEdges = cmds.ls(sl=1, fl=1)
        shapeNode = cmds.listRelatives(selEdges[0], fullPath=True, parent=True)
        transformNode = cmds.listRelatives(shapeNode[0], fullPath=True, parent=True)
        edgeNumberList = []
        for a in selEdges:
            checkNumber = ((a.split('.')[1]).split('\n')[0]).split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    edgeNumberList.append(findNumber)
        getNumber = []
        for s in selEdges:
            evlist = cmds.polyInfo(s, ev=True)
            checkNumber = ((evlist[0].split(':')[1]).split('\n')[0]).split(' ')
            for c in checkNumber:
                findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
                if findNumber:
                    getNumber.append(findNumber)
        dup = set([x for x in getNumber if getNumber.count(x) > 1])
        getHeadTail = list(set(getNumber) - dup)
        checkCircleState = 0
        if not getHeadTail:  # close curve
            checkCircleState = 1
            getHeadTail.append(getNumber[0])
        vftOrder = []
        vftOrder.append(getHeadTail[0])
        count = 0
        while len(dup) > 0 and count < 1000:
            checkVtx = transformNode[0] + '.vtx[' + vftOrder[-1] + ']'
            velist = cmds.polyInfo(checkVtx, ve=True)
            getNumber = []
            checkNumber = ((velist[0].split(':')[1]).split('\n')[0]).split(' ')
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
            checkNumber = ((findVtx[0].split(':')[1]).split('\n')[0]).split(' ')
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
        else:  # close curve remove connected vtx
            if vftOrder[0] == vftOrder[1]:
                vftOrder = vftOrder[1:]
            elif vftOrder[0] == vftOrder[-1]:
                vftOrder = vftOrder[0:-1]
        finalList = []
        for v in vftOrder:
            finalList.append(transformNode[0] + '.vtx[' + v + ']')

        return checkCircleState, finalList


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
                    bevelDistance = distanceBetween(checkCV[0], checkCV[-1])
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
            listVtx = vtxLoopOrder(s)
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
        self.alignEdge = Align_Edge()
        self.instantDrag = Instant_Drag()
        self.EVEN_EDGE_LOOP = Even_Edge_Loop()
        self.ROUND_INSET = Round_Inset()
        self.FILL_SELECTION = Fill_Selection()

        self.grey_blue = (0.198, 0.218, 0.238)
        self.grey = (0.31, 0.32, 0.35)
        self.inner_grey = (0.258, 0.278, 0.298)
        self.yellow_hex = 0xCC7F00
        self.inner_margins4 = (12, 2, 2, 2)

        self.create_ui()

    """
        ------------------------------------ Layouts ------------------------------------
    """

    def create_ui(self):
        if cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME, q=True, exists=True):
            cmds.deleteUI(LingJuGeometryTools.LJGT_WINDOW_NAME)

        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME,
                              label=u"成都零距数码工具集v104",
                              retain=True,
                              initialWidth=330,
                              minimumWidth=300,
                              mh=300,
                              resizeWidth=2,
                              widthProperty='preferred'
                              )
        cmds.scrollLayout(verticalScrollBarThickness=2)

        cmds.columnLayout()
        grey_value = (0.14, 0.16, 0.18)

        self.create_modeling_frame(grey_value)
        cmds.setParent('..')
        self.create_selection_frame(grey_value)
        cmds.setParent('..')
        self.create_test_layout(grey_value)

        cmds.workspaceControl(LingJuGeometryTools.LJGT_WINDOW_NAME, e=True, restore=True)

    def create_modeling_frame(self, bg_color):
        cmds.frameLayout(label=u"建模",
                         width=320,
                         bgc=bg_color,
                         collapsable=True,
                         marginWidth=8,
                         marginHeight=6,
                         collapse=False)
        cmds.columnLayout()

        self.create_pivots_settings_layout()
        self.create_even_edge_loop_layout()
        self.create_smart_combine_separate_layout()
        cmds.setParent('..')

        cmds.text(label=" ", height=5)

        self.create_modeling_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_selection_frame(self, bg_color):
        cmds.frameLayout(label=u"选择",
                         width=320,
                         bgc=bg_color,
                         collapsable=True,
                         collapse=False,
                         marginWidth=8,
                         marginHeight=5)
        cmds.columnLayout()

        self.create_select_by_angle_layout()
        self.create_select_similar_layout()
        cmds.text(label=" ", height=5)

        self.create_selection_buttons()

        cmds.setParent('..')  # Go back to the frame
        cmds.setParent('..')  # Go back to the window

    def create_test_layout(self, bg_color):
        cmds.frameLayout(label="测试", width=320,
                         bgc=bg_color, collapsable=True, collapse=True)
        cmds.columnLayout()

        cmds.text(label=u"这里测试布局的内容", align='center')

        cmds.setParent('..')
        cmds.setParent('..')

    def create_smart_combine_separate_layout(self):
        cmds.frameLayout(label=u"智能合并分离", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=10, marginHeight=5)
        cmds.columnLayout()

        self.create_combine_separate_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_even_edge_loop_layout(self):
        cmds.frameLayout(label=u"平均线段", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=10, marginHeight=5)
        cmds.columnLayout()

        self.create_even_edge_loop_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_pivots_settings_layout(self):
        cmds.frameLayout(label=u"坐标轴", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=10, marginHeight=5)
        cmds.columnLayout()

        self.create_pivot_buttons()

        cmds.setParent('..')
        cmds.setParent('..')

    def create_select_by_angle_layout(self):
        cmds.frameLayout(label=u"按角度选面", width=299,
                         bgc=self.grey_blue, collapsable=True, collapse=True, marginWidth=0, marginHeight=5)
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
        angleByNormalsSlider = cmds.intSliderGrp(label=u"角度", field=True, columnWidth3=(50, 30, 60),
                                                 minValue=0, maxValue=180, fieldMinValue=0,
                                                 value=25, width=250,
                                                 cc=lambda *args: self.update_angle(
                                                 cmds.intSliderGrp(angleByNormalsSlider, q=True, value=True)))

        cmds.button(label=u"选择",
                    bgc=self.inner_grey,
                    command=lambda *args: [self.update_initial_faces(), self.grow_selection_by_normal_angle(
                        cmds.intSliderGrp(angleByNormalsSlider, q=True, value=True))])
        cmds.setParent('..')

    def create_selection_buttons(self):
        cmds.columnLayout()

        cmds.button(label=u"隔段选线", command=self.select_every_N_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        cmds.button(label=u"选区选择", command=lambda *args: self.FILL_SELECTION.fill_selection(),
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        cmds.button(label=u"选硬边", command=self.get_hard_edge,
                    width=300, height=28, bgc=self.grey)

        cmds.text(label=" ", height=2)

        cmds.button(label=u"测试", command=self.test,
                    width=300, height=28, bgc=self.grey)

        cmds.setParent('..')

    def set_mode(self, value):
        self.mode = value

    def create_combine_separate_buttons(self):
        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"智能合并", command=self.smart_clean_combine,
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"能自动合并近的片")
        cmds.button(label=u"智能分离", command=lambda *args: self.smart_clean_separate(keepOriginal=self.keep_original),
                    width=136, height=30, bgc=self.inner_grey,
                    annotation=u"自动判断分离片，还是物体")
        cmds.setParent('..')

        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2)
        cmds.text(" ", width=60)
        self.keep_original = False
        self.separate_radio_grp = cmds.radioButtonGrp(numberOfRadioButtons=2, columnWidth3=(120, 50, 50),
                                                      label=u"复制面：",
                                                      labelArray2=['是', "否"],
                                                      select=2,
                                                      on1=lambda *args: self.set_keep_original(True),
                                                      on2=lambda *args: self.set_keep_original(False),
                                                      vertical=False,
                                                      width=300)
        cmds.setParent('..')

    def create_even_edge_loop_buttons(self):
        cmds.rowLayout(numberOfColumns=3, )

        cmds.button(label=u"平均", command=lambda *args: self.EVEN_EDGE_LOOP.evenEdgeLoopDoitRun("even"),
                    width=90, height=28, bgc=self.inner_grey,
                    annotation=u"以当前线段方向平均")

        cmds.button(label=u"弧线", command=lambda *args: self.EVEN_EDGE_LOOP.evenEdgeLoopDoitRun("2P"),
                    width=90, height=28, bgc=self.inner_grey)

        cmds.button(label=u"打直平均", command=lambda *args: self.EVEN_EDGE_LOOP.evenEdgeLoopDoitRun("straighten"),
                    width=90, height=28, bgc=self.inner_grey,
                    annotation=u"最远端线段头尾打直")

        cmds.setParent('..')

    def set_keep_original(self, value):
        self.keep_original = value

    def create_modeling_buttons(self):
        cmds.columnLayout()
        cmds.rowLayout(numberOfColumns=2)

        cmds.button(label=u"转角改线", command=self.corner_killer,
                    width=149, height=28, bgc=self.grey,
                    annotation=u"上下一起选择可以删除线")

        cmds.button(label=u"选中元素加环线", command=lambda *args: self.split_around(),
                    width=149, height=28, bgc=self.grey,
                    annotation=u"选中点线面都可以")

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"智能插入 I", command=lambda *args: self.ROUND_INSET.roundInset(),
                    width=149, height=28, bgc=self.grey)

        cmds.button(label=u"智能插入 II", command=lambda *args: self.insert_face(),
                    width=149, height=28, bgc=self.grey)

        cmds.setParent('..')

        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"测试", command=lambda *args: self.test(),
                    width=149, height=28, bgc=self.grey)

        cmds.button(label=u"测试", command=lambda *args: self.test(),
                    width=149, height=28, bgc=self.grey)

        cmds.setParent('..')

        cmds.text(label=" ", height=10)
        cmds.rowLayout(numberOfColumns=2)
        cmds.button(label=u"拖拽吸附", command=lambda *args: self.instantDrag.instantDrag(),
                    width=149, height=28, bgc=self.grey)

        cmds.button(label=u"沿最近边对齐", command=lambda *args: self.alignEdge.alignEdge(),
                    width=149, height=28, bgc=self.grey)


        cmds.setParent('..')



    def create_pivot_buttons(self):
        cmds.rowLayout(numberOfColumns=3, )

        cmds.button(label=u"冻结", command="cmds.FreezeTransformations()",
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标系归0")

        cmds.button(label=u"居中", command="cmds.CenterPivot()",
                    width=90, height=20, bgc=self.inner_grey,
                    annotation=u"坐标移动到物体中心")

        cmds.button(label=u"重置", command="cmds.ResetTransformations()",
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

    """
         ------------------------------------ Functions ------------------------------------
    """

    # --------------------- combine & separate
    def smart_clean_combine(self, *args):

        selection = cmds.ls(selection=True, type='transform')

        if not len(selection) > 1:
            cmds.inViewMessage(msg=u"选择两个以上的物体!!!",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
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

        cmds.polyMergeVertex(d=0.001)
        cmds.delete(last_obj, constructionHistory=True)
        cmds.select(last_obj, r=True)

        cmds.FreezeTransformations()
        cmds.CenterPivot()
        cmds.undoInfo(closeChunk=True)

    def smart_clean_separate(self, keepOriginal, *args):
        selected_objects = cmds.ls(selection=True)
        face_components = cmds.polyEvaluate(faceComponent=True)

        if not selected_objects or (face_components < 1 and not cmds.ls(selected_objects, type='transform')):
            cmds.inViewMessage(msg=u"至少选择一个物体!!!",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            return

        cmds.undoInfo(openChunk=True, cn="SmartCleanSeparate")

        try:
            obj = cmds.filterExpand(selectionMask=12)

            if obj and len(obj) >= 1:
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
                            if cmds.objectType(child, isType='transform') and not cmds.listRelatives(child, shapes=True):
                                continue
                            gd_children.append(child)

                        for child in gd_children:
                            counter += 1
                            padding_string = '%03d' % counter
                            cmds.rename(child, '%s_%s_geo' % (striped_name, padding_string))

                    cmds.ungroup()
                    cmds.CenterPivot()
                except RuntimeError:
                    cmds.inViewMessage(msg=u"单个物体不能分离!!!",
                                       pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            else:
                faces_init = cmds.ls(selection=True, flatten=True)
                if not faces_init:
                    cmds.inViewMessage(msg=u"选择面!!!",
                                       pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
                    return

                cmds.selectMode(object=True)
                store_obj = cmds.ls(selection=True, transforms=True)
                new_comp = []
                dup_obj = cmds.duplicate()

                for f in faces_init:
                    match = re.search(r'\..+', f)
                    if match:
                        comp = match.group()
                        new_comp.append(dup_obj[0] + comp)

                cmds.select(new_comp, r=True)
                cmds.InvertSelection()
                cmds.delete()

                if keepOriginal == 0:
                    cmds.polyDelFacet(faces_init)

                if store_obj:
                    cmds.delete(store_obj, ch=True)

                all_meshes = cmds.ls(exactType='mesh')
                cmds.select(all_meshes)

                for mesh in all_meshes:
                    if cmds.objExists(mesh) and 'Orig' in mesh:
                        cmds.select(mesh, deselect=True)

                selected_meshes = cmds.ls(sl=True)

                for mesh in selected_meshes:
                    cmds.select(mesh)
                    cmds.ConvertSelectionToVertices()
                    verts = cmds.filterExpand(sm=31)
                    if not verts:
                        cmds.delete()
                cmds.selectMode(object=True)
                cmds.select(dup_obj[0], r=True)
                cmds.CenterPivot()
        finally:
            cmds.undoInfo(closeChunk=True)

    # ------------------------------------------ selection ------------------------------------------

    # --------------------- select_every_N_edge
    def select_every_N_edge(self, *args):
        selected_objects = cmds.ls(selection=True)

        if not selected_objects:
            cmds.inViewMessage(msg=u"先选择两条循环线!!!",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            return

        initial_edge_selection = cmds.ls(selection=True, flatten=True)
        num_edges = len(initial_edge_selection)

        if not num_edges == 2:
            cmds.inViewMessage(msg=u"先选择两条循环线!!!",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
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

        print(selected_objects)
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
            cmds.inViewMessage(msg=u"先选中物体或线",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)

    # --------------------- select similar mesh
    def select_similar_mesh(self, mode, *args):
        selection = cmds.ls(selection=True, long=True)
        if not selection:
            cmds.inViewMessage(msg=u"选择一个物体先！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            return

        items = cmds.ls(selection=True, l=True, fl=True)
        all_transforms = cmds.ls(items, long=True, type='transform')
        mesh_items = []
        for transform in all_transforms:
            shapes = cmds.listRelatives(transform, shapes=True, fullPath=True, type='mesh') or []
            if shapes:
                short_name = cmds.ls(transform, tail=True)[0]
                mesh_items.append(short_name)

        scene_mesh = cmds.ls(long=True, type='transform')
        mesh_transforms = cmds.filterExpand(scene_mesh, sm=12) or []

        similar_meshes = []
        scale_source = []
        area_source = []

        if len(items) != len(mesh_items):
            cmds.inViewMessage(msg=u"选择一个物体先！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
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
            cmds.warning(f"Failed to get parent shape for {face_name}.")
            return normal

        parent_transform = cmds.listRelatives(parent_shape[0], parent=True, path=True)
        if not parent_transform:
            cmds.warning(f"Failed to get parent transform for {parent_shape[0]}.")
            return normal

        transform_matrix = cmds.xform(parent_transform[0], q=True, m=True, ws=True)

        m_matrix = oma.MMatrix(transform_matrix)
        world_normal = normal * m_matrix
        unit_world_normal = world_normal.normal()

        return unit_world_normal

    def grow_selection_by_normal_angle(self, angle):
        global initial_faces

        if not initial_faces:
            cmds.inViewMessage(msg=u"选择一个面！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            return

        # Clear previous constraints and selection
        cmds.polySelectConstraint(dis=True)
        cmds.select(clear=True)

        all_selected_faces = set()
        for face_name in initial_faces:
            poly_normal = self.get_face_normal(face_name)
            cmds.polySelectConstraint(mode=2, type=0x0008, orient=1,
                                      orientaxis=(poly_normal.x, poly_normal.y, poly_normal.z),
                                      orientbound=(0, angle))  # Set angle bounds

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

    def update_angle(self, new_angle):
        global initial_faces
        if not initial_faces:
            cmds.inViewMessage(msg=u"选择一个面！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
            return

        # Clear previous constraints and selection
        cmds.polySelectConstraint(dis=True)
        cmds.select(clear=True)

        all_selected_faces = set()
        for face_name in initial_faces:
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
        global initial_faces
        initial_faces = cmds.ls(sl=True, fl=True)
        if not initial_faces:
            cmds.inViewMessage(msg=u"请重新选择面！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)

    # ------------------------------------------ modeling ------------------------------------------

    # --------------------- corner_killer
    def corner_killer(self, *args):
        selection = cmds.ls(sl=True, fl=True)
        if not selection:
            cmds.inViewMessage(msg=u"请选择转角面！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
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
            cmds.inViewMessage(msg=u"选择点线面元素",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)
    # --------------------- smart_insert_face I
    # def roundInset(self, *args):
    #     cmd = 'source dagMenuProc;'
    #     mel.eval(cmd)
    #     global insetDataPP
    #     global insetMesh
    #     global insetFace
    #     global insetDataEdgeLoopList
    #     global insetMeshVolume
    #     global insetInnerEdges
    #     global updatedNewSelEdge
    #     updatedNewSelEdge = []
    #     insetInnerEdges = []
    #     insetDataEdgeLoopList = []
    #     insetDataPP = []
    #     insetMesh = ''
    #     insetFace = ''
    #     insetMeshVolume = 0
    #     if cmds.window('RoundInsetUI', exists=True):
    #         cmds.deleteUI('RoundInsetUI')
    #     RoundInsetUI = cmds.window('RoundInsetUI', title='Round Inset v1.68', w=240, s=1, mxb=False, mnb=False)
    #     cmds.columnLayout(adj=1)
    #     cmds.text(l='')
    #     cmds.rowColumnLayout(nc=3, cw=[(1, 300), (2, 20), (3, 5), (4, 90), (5, 10)])
    #     cmds.columnLayout(adj=1)
    #     cmds.rowColumnLayout(nc=2, cw=[(1, 270), (2, 20)])
    #     cmds.floatSliderGrp('rInsetV', en=0, cw3=[60, 40, 0], label='Offset   ', field=True, v=0.01, min=-1, max=1,
    #                         step=0.001)
    #     cmds.button('rInsetVMax', l='+', c='self.slipderMax("rInsetV")', en=1, bgc=[0.28, 0.28, 0.28])
    #     cmds.floatSliderGrp('rBevelRound', en=0, cw3=[60, 40, 0], label='Round   ', field=True, v=0, min=-1, max=1,
    #                         step=0.001)
    #     cmds.button('rBevelRoundMax', l='+', c='self.slipderMax("rBevelRound")', en=1, bgc=[0.28, 0.28, 0.28])
    #     cmds.floatSliderGrp('rBevelAngle', en=0, cw3=[60, 40, 0], cc='rBevelAngleUpdate()',
    #                         dc='self.rBevelAngleUpdate()', label='Angle   ', field=True, v=80, min=60, max=90, fmn=0,
    #                         fmx=180, step=0.1)
    #     # cmds.button('rBevelLengthMax',l='+',  c='slipderMax("rBevelLength")', en = 1,bgc=[0.28,0.28,0.28])
    #     cmds.setParent('..')
    #     cmds.setParent('..')
    #     cmds.setParent('..')
    #     cmds.text(l='')
    #     cmds.rowColumnLayout(nc=6, cw=[(1, 10), (2, 60), (3, 60), (4, 60), (5, 60), (6, 60)])
    #     cmds.text(l='')
    #     cmds.button('InsetButton', l='Inset', en=1, c='roundInsetRun()', bgc=[0.18, 0.48, 0.18])
    #     cmds.button('reFineButton', l='Refine', en=0, c='reFineSwitch()', bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InnerCornerEvenButton', l='Even', en=0, c='evenInnerCorner()', bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetRemoveButton', l='Remove', en=0, c='roundInsetRemove()', bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetCleaneButton', l='Done', en=1, c='roundInsetClean()', bgc=[0.48, 0.18, 0.18])
    #     cmds.setParent('..')
    #     cmds.text(l='')
    #     cmds.showWindow(RoundInsetUI)
    #
    # def slipderMax(self, name):
    #     sliderName = name
    #     currentMaxV = cmds.floatSliderGrp(sliderName, q=1, max=1)
    #     currentMinV = cmds.floatSliderGrp(sliderName, q=1, min=1)
    #     cmds.floatSliderGrp(sliderName, e=1, min=currentMinV * 2, max=currentMaxV * 2)
    #
    # def roundInsetRemove(self):
    #     global insetFace
    #     global insetMesh
    #     global insetDataEdgeLoopList
    #     shape_node = cmds.listRelatives(insetMesh, shapes=True)
    #     source_shape = shape_node[-1]
    #     destination_shape = shape_node[0]
    #     if insetFace:
    #         history_nodes = cmds.listHistory(insetMesh)
    #         delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
    #         for d in delList:
    #             if cmds.objExists(d):
    #                 cmds.delete(d)
    #         cmds.select(insetFace)
    #     cmds.floatSliderGrp('rInsetV', e=1, v=0.01, min=-1, max=1, fmx=10, step=0.001)
    #     cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
    #     cmds.floatSliderGrp('rBevelRound', e=1, en=0, v=0, min=-1, max=1, step=0.001)
    #     if cmds.objExists('insetDataEdgeLoopListKeep'):
    #         cmds.delete('insetDataEdgeLoopListKeep')
    #     if cmds.objExists('cornerDisp'):
    #         cmds.setAttr('cornerDisp.creaseLevel', 0)
    #         cmds.delete('cornerDisp')
    #     if insetMesh:
    #         cmds.select(insetMesh)
    #         cmds.delete(all=1, e=1, ch=1)
    #         cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "facet" , 0);'
    #         mel.eval(cmd)
    #         cmds.select(insetFace)
    #     insetFace = ''
    #     insetMesh = ''
    #     insetDataEdgeLoopList = []
    #     cmds.setToolTo('Move')
    #     cmds.button('InsetButton', e=1, en=1, bgc=[0.18, 0.48, 0.18])
    #     cmds.button('reFineButton', l='Refine', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])
    #
    # def roundInsetClean(self):
    #     currentsel = cmds.ls(sl=1, fl=1)
    #     if currentsel:
    #         geoSel = currentsel[0].split('.')[0]
    #         if geoSel:
    #             cmds.delete(geoSel, ch=1)
    #     global insetFace
    #     global insetMesh
    #     if cmds.objExists("insetOffsetNod*"):
    #         listNode = cmds.ls("insetOffsetNod*")
    #         for s in listNode:
    #             getOldMesh = cmds.listConnections((s + '.outputGeometry'), scn=True)
    #             try:
    #                 getOldShape = cmds.listConnections((getOldMesh[0] + '.outputGeometry'), scn=True)
    #                 cmds.delete(getOldShape, ch=1)
    #             except:
    #                 cmds.delete(getOldMesh, ch=1)
    #
    #     cleanList = ('insetOffsetNod*', 'roundV', 'insetOffsetV', 'insetDataEdgeLoopListKeep', 'blendOffsetNode',
    #                  'tempLoopListKeep')
    #     for c in cleanList:
    #         if cmds.objExists(c):
    #             cmds.delete(c)
    #
    #     cmds.floatSliderGrp('rInsetV', e=1, v=0.01, min=-1, max=1, fmx=10, step=0.001)
    #     cmds.floatSliderGrp('rBevelAngle', e=1, en=0, cw3=[60, 40, 0], field=True, v=80, min=60, max=90, fmn=0,
    #                         fmx=180, step=0.1)
    #     cmds.floatSliderGrp('rBevelRound', e=1, en=0, v=0, min=-1, max=1, step=0.001)
    #     if cmds.objExists('insetDataEdgeLoopListKeep'):
    #         cmds.delete('insetDataEdgeLoopListKeep')
    #     if cmds.objExists('cornerDisp'):
    #         cmds.setAttr('cornerDisp.creaseLevel', 0)
    #         cmds.delete('cornerDisp')
    #     if insetFace:
    #         cmds.select(insetFace)
    #         cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "facet", 0);'
    #         mel.eval(cmd)
    #         cmds.select(insetFace)
    #     insetFace = ''
    #     insetMesh = ''
    #     cmds.button('InsetButton', e=1, en=1, bgc=[0.18, 0.48, 0.18])
    #     cmds.button('reFineButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])
    #     cmds.setToolTo('Move')
    #     # clean storeBevel Attr
    #     transformsNodeList = cmds.ls(dag=1, type='transform', l=1)
    #     for l in transformsNodeList:
    #         anyUserAttr = cmds.listAttr(l, userDefined=1)
    #         if anyUserAttr:
    #             for a in anyUserAttr:
    #                 if a == 'storeBevelV':
    #                     if cmds.attributeQuery(a, node=l, ex=True):
    #                         cmds.setAttr((l + "." + a), l=0)
    #                         cmds.deleteAttr(l + "." + a)
    #
    # def evenInnerCorner(self):
    #     global recordInnerCornerList
    #     cmds.select(recordInnerCornerList)
    #     sortGrp = []
    #     sortGrp = self.getEdgeRingGroup(recordInnerCornerList)
    #     if len(sortGrp) > 0:
    #         for g in sortGrp:
    #             if cmds.objExists('tempEvenCurve'):
    #                 cmds.delete('tempEvenCurve')
    #             listVtx = self.vtxLoopOrder(g)
    #             cmds.select(g)
    #             cmds.polyToCurve(form=2, degree=1, conformToSmoothMeshPreview=1)
    #             cmds.rename('tempEvenCurve')
    #             curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
    #             posCurve = cmds.xform(curveCVs[0], a=1, ws=1, q=1, t=1)
    #             posEdge = cmds.xform(listVtx[0], a=1, ws=1, q=1, t=1)
    #             if posCurve == posEdge:
    #                 pass
    #             else:
    #                 listVtx = listVtx[::-1]
    #             if len(curveCVs) > 2:
    #                 cmds.rebuildCurve('tempEvenCurve', ch=1, rpo=1, rt=0, end=1, kr=0, kcp=0, kep=1, kt=0, s=0, d=1,
    #                                   tol=0)
    #                 if len(curveCVs) < 4:
    #                     cmds.delete('tempEvenCurve.cv[1]', 'tempEvenCurve.cv[3]')
    #                     curveCVs = cmds.ls('tempEvenCurve.cv[*]', fl=1)
    #                 posCurve = cmds.xform(curveCVs[0], a=1, ws=1, q=1, t=1)
    #                 posEdge = cmds.xform(listVtx[0], a=1, ws=1, q=1, t=1)
    #                 posEdge[0] = round(posEdge[0], 3)
    #                 posEdge[1] = round(posEdge[1], 3)
    #                 posEdge[2] = round(posEdge[2], 3)
    #                 posCurve[0] = round(posCurve[0], 3)
    #                 posCurve[1] = round(posCurve[1], 3)
    #                 posCurve[2] = round(posCurve[2], 3)
    #             for i in range(len(curveCVs)):
    #                 pos = cmds.xform(curveCVs[i], a=1, ws=1, q=1, t=1)
    #                 cmds.xform(listVtx[i], a=1, ws=1, t=(pos[0], pos[1], pos[2]))
    #             cmds.delete('tempEvenCurve')
    #         cmds.select('cornerDisp')
    #         cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
    #         mel.eval(cmd)
    #         cmds.select(insetFace, add=1)
    #         cmds.setToolTo('selectSuperContext')
    #
    # def matchCorner(self, edgeLoop, getRoundV):
    #     global insetFace
    #     global insetInnerEdges
    #     global insetDataEdgeLoopList
    #     selLoopShort = edgeLoop
    #     toCV = cmds.polyListComponentConversion(selLoopShort, tv=True)
    #     toEdge = cmds.polyListComponentConversion(toCV, te=True)
    #     toEdge = cmds.ls(toEdge, fl=1)
    #     toFace = cmds.polyListComponentConversion(selLoopShort, tf=True)
    #     toFace = cmds.ls(toFace, fl=1)
    #     toFace = list(set(toFace) - set(insetFace))
    #     toEdgeB = cmds.polyListComponentConversion(toFace, te=True)
    #     toEdgeB = cmds.ls(toEdgeB, fl=1)
    #     selLoopLong = list(set(toEdgeB) - set(toEdge))
    #     totalLengthA = 0
    #     for s in selLoopLong:
    #         intSelCV = cmds.polyListComponentConversion(s, tv=True)
    #         intSelCV = cmds.ls(intSelCV, fl=1)
    #         distanceX = self.distanceBetween(intSelCV[0], intSelCV[1])
    #         totalLengthA = totalLengthA + distanceX
    #     totalLengthB = 0
    #     for s in selLoopShort:
    #         intSelCV = cmds.polyListComponentConversion(s, tv=True)
    #         intSelCV = cmds.ls(intSelCV, fl=1)
    #         distanceX = self.distanceBetween(intSelCV[0], intSelCV[1])
    #         totalLengthB = totalLengthB + distanceX
    #     scaleV = totalLengthA / totalLengthB * getRoundV
    #     toDO = list(set(toEdge) - set(toEdgeB) - set(insetInnerEdges))
    #     toDO = toDO + selLoopShort
    #     toDO = list(set(toDO))
    #     if len(insetDataEdgeLoopList) == len(toDO):
    #         pass
    #     else:
    #         cmds.sets(selLoopLong, forceElement="cornerDisp")
    #         pPoint, vList, cList = self.unBevelEdgeLoop(toDO)
    #         for v in vList:
    #             cmds.scale(scaleV, scaleV, scaleV, v, cs=1, r=1, p=(pPoint[0], pPoint[1], pPoint[2]))
    #
    # def distanceBetween(self, p1, p2):
    #     pA = cmds.pointPosition(p1, w=1)
    #     pB = cmds.pointPosition(p2, w=1)
    #     dist = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
    #     return dist
    #
    # def getEdgeRingGroup(self, selEdges):
    #     # selEdges = cmds.ls(sl=1,fl=1)
    #     trans = selEdges[0].split(".")[0]
    #     e2vInfos = cmds.polyInfo(selEdges, ev=True)
    #     e2vDict = {}
    #     fEdges = []
    #     for info in e2vInfos:
    #         evList = [int(i) for i in re.findall('\\d+', info)]
    #         e2vDict.update(dict([(evList[0], evList[1:])]))
    #     while True:
    #         try:
    #             startEdge, startVtxs = e2vDict.popitem()
    #         except:
    #             break
    #         edgesGrp = [startEdge]
    #         num = 0
    #         for vtx in startVtxs:
    #             curVtx = vtx
    #             while True:
    #                 nextEdges = []
    #                 for k in e2vDict:
    #                     if curVtx in e2vDict[k]:
    #                         nextEdges.append(k)
    #                 if nextEdges:
    #                     if len(nextEdges) == 1:
    #                         if num == 0:
    #                             edgesGrp.append(nextEdges[0])
    #                         else:
    #                             edgesGrp.insert(0, nextEdges[0])
    #                         nextVtxs = e2vDict[nextEdges[0]]
    #                         curVtx = [vtx for vtx in nextVtxs if vtx != curVtx][0]
    #                         e2vDict.pop(nextEdges[0])
    #                     else:
    #                         break
    #                 else:
    #                     break
    #             num += 1
    #         fEdges.append(edgesGrp)
    #     retEdges = []
    #     for f in fEdges:
    #         collectList = []
    #         for x in f:
    #             getCom = (trans + ".e[" + str(x) + "]")
    #             collectList.append(getCom)
    #         retEdges.append(collectList)
    #     return retEdges
    #
    # def unBevelEdgeLoop(self, edgelist):
    #     listVtx = self.vtxLoopOrder(edgelist)
    #     checkA = self.angleBetweenThreeP(listVtx[1], listVtx[0], listVtx[-1])
    #     angleA = math.degrees(checkA)
    #     checkB = self.angleBetweenThreeP(listVtx[-2], listVtx[-1], listVtx[0])
    #     angleB = math.degrees(checkB)
    #     angleC = 180 - angleA - angleB
    #     distanceC = self.distanceBetween(listVtx[0], listVtx[-1])
    #     distanceA = distanceC / math.sin(math.radians(angleC)) * math.sin(math.radians(angleA))
    #     distanceB = distanceC / math.sin(math.radians(angleC)) * math.sin(math.radians(angleB))
    #     oldDistA = self.distanceBetween(listVtx[-2], listVtx[-1])
    #     oldDistB = self.distanceBetween(listVtx[0], listVtx[1])
    #     scalarB = distanceB / oldDistB
    #     pA = cmds.pointPosition(listVtx[0], w=1)
    #     pB = cmds.pointPosition(listVtx[1], w=1)
    #     newP = [0, 0, 0]
    #     newP[0] = ((pB[0] - pA[0]) * scalarB) + pA[0]
    #     newP[1] = ((pB[1] - pA[1]) * scalarB) + pA[1]
    #     newP[2] = ((pB[2] - pA[2]) * scalarB) + pA[2]
    #     listVtx = listVtx[1:-1]
    #     storeDist = []
    #     for l in listVtx:
    #         sotreXYZ = [0, 0, 0]
    #         p = cmds.xform(l, q=True, t=True, ws=True)
    #         sotreXYZ[0] = (newP[0] - p[0]) / 100
    #         sotreXYZ[1] = (newP[1] - p[1]) / 100
    #         sotreXYZ[2] = (newP[2] - p[2]) / 100
    #         storeDist.append(sotreXYZ)
    #     return newP, listVtx, storeDist
    #
    # def vtxLoopOrder(self, edgelist):
    #     selEdges = edgelist
    #     # selEdges = cmds.ls(sl=1, fl=1)
    #     shapeNode = cmds.listRelatives(selEdges[0], fullPath=True, parent=True)
    #     transformNode = cmds.listRelatives(shapeNode[0], fullPath=True, parent=True)
    #     edgeNumberList = []
    #     for a in selEdges:
    #         checkNumber = a.split('.')[1].split('\n')[0].split(' ')
    #         for c in checkNumber:
    #             findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
    #             if findNumber:
    #                 edgeNumberList.append(findNumber)
    #     getNumber = []
    #     for s in selEdges:
    #         evlist = cmds.polyInfo(s, ev=True)
    #         checkNumber = evlist[0].split(':')[1].split('\n')[0].split(' ')
    #         for c in checkNumber:
    #             findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
    #             if findNumber:
    #                 getNumber.append(findNumber)
    #     dup = set([x for x in getNumber if getNumber.count(x) > 1])
    #     getHeadTail = list(set(getNumber) - dup)
    #     checkCircleState = 0
    #     if not getHeadTail:
    #         checkCircleState = 1
    #         getHeadTail.append(getNumber[0])
    #     vftOrder = []
    #     vftOrder.append(getHeadTail[0])
    #     count = 0
    #     while len(dup) > 0 and count < 3000:
    #         checkVtx = transformNode[0] + '.vtx[' + vftOrder[-1] + ']'
    #         velist = cmds.polyInfo(checkVtx, ve=True)
    #         getNumber = []
    #         checkNumber = velist[0].split(':')[1].split('\n')[0].split(' ')
    #         for c in checkNumber:
    #             findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
    #             if findNumber:
    #                 getNumber.append(findNumber)
    #         findNextEdge = []
    #         for g in getNumber:
    #             if g in edgeNumberList:
    #                 findNextEdge = g
    #         edgeNumberList.remove(findNextEdge)
    #         checkVtx = transformNode[0] + '.e[' + findNextEdge + ']'
    #         findVtx = cmds.polyInfo(checkVtx, ev=True)
    #         getNumber = []
    #         checkNumber = findVtx[0].split(':')[1].split('\n')[0].split(' ')
    #         for c in checkNumber:
    #             findNumber = ''.join([n for n in c.split('|')[-1] if n.isdigit()])
    #             if findNumber:
    #                 getNumber.append(findNumber)
    #         gotNextVtx = []
    #         for g in getNumber:
    #             if g in dup:
    #                 gotNextVtx = g
    #         dup.remove(gotNextVtx)
    #         vftOrder.append(gotNextVtx)
    #         count += 1
    #     if checkCircleState == 0:
    #         vftOrder.append(getHeadTail[1])
    #     elif vftOrder[0] == vftOrder[1]:
    #         vftOrder = vftOrder[1:]
    #     elif vftOrder[0] == vftOrder[-1]:
    #         vftOrder = vftOrder[0:-1]
    #     finalList = []
    #     for v in vftOrder:
    #         finalList.append(transformNode[0] + '.vtx[' + v + ']')
    #     return (finalList)
    #
    # def angleBetweenThreeP(self, pA, pB, pC):
    #     a = cmds.pointPosition(pA, w=1)
    #     b = cmds.pointPosition(pB, w=1)
    #     c = cmds.pointPosition(pC, w=1)
    #     ba = [aa - bb for aa, bb in zip(a, b)]
    #     bc = [cc - bb for cc, bb in zip(c, b)]
    #     nba = math.sqrt(sum((x ** 2.0 for x in ba)))
    #     ba = [x / nba for x in ba]
    #     nbc = math.sqrt(sum((x ** 2.0 for x in bc)))
    #     bc = [x / nbc for x in bc]
    #     scalar = sum((aa * bb for aa, bb in zip(ba, bc)))
    #     angle = math.acos(scalar)
    #     return angle
    #
    # def getfaceArea(self, mesh, faceId):
    #     if cmds.objectType(mesh) == 'transform':
    #         mesh = cmds.listRelatives(mesh, s=True, ni=True, pa=True)[0]
    #     selectionList = oma.MSelectionList()
    #     MGlobal.getSelectionListByName(mesh, selectionList)
    #     mDagPath = oma.MDagPath()
    #     selectionList.getDagPath(0, mDagPath)
    #     meshFaceIt = oma.MItMeshPolygon(mDagPath)
    #     if faceId != None:
    #         meshFaceUtil = oma.MScriptUtil()
    #         meshFacePtr = meshFaceUtil.asIntPtr()
    #         meshFaceIt.setIndex(faceId, meshFacePtr)
    #     faceArea = oma.MScriptUtil()
    #     faceArea.createFromDouble(0.0)
    #     faceAreaPtr = faceArea.asDoublePtr()
    #     meshFaceIt.getArea(faceAreaPtr)
    #     areaCheck = oma.MScriptUtil(faceAreaPtr).asDouble()
    #     return areaCheck
    #
    # def edgeLoopByAngle(self, selList):
    #     global edgeLoopOverLengthLib
    #     edgeLengthData = {}
    #     listVtx = self.vtxLoopOrder(selList)
    #     listVtx.append(listVtx[0])
    #     listVtx.append(listVtx[1])
    #     collectList = []
    #     for r in range(len(listVtx) - 2):
    #         pA = cmds.pointPosition(listVtx[r], w=True)
    #         pB = cmds.pointPosition(listVtx[r + 1], w=True)
    #         pC = cmds.pointPosition(listVtx[r + 2], w=True)
    #         direction_vectorA = [pA[i] - pB[i] for i in range(3)]
    #         lengthA = sum(y ** 2 for y in direction_vectorA) ** 0.5
    #         normalized_directionA = [y / lengthA for y in direction_vectorA]
    #         direction_vectorB = [pB[i] - pC[i] for i in range(3)]
    #         lengthB = sum(y ** 2 for y in direction_vectorB) ** 0.5
    #         normalized_directionB = [y / lengthB for y in direction_vectorB]
    #         dot_product = sum([normalized_directionA[z] * normalized_directionB[z] for z in range(3)])
    #         # checkAngle = abs(abs(dot_product) - 1.0)
    #         angle_degrees = math.degrees(math.acos(dot_product))
    #         if angle_degrees > 10:
    #             edgeFoundA = cmds.polyListComponentConversion(listVtx[r], listVtx[r + 1], fv=True, te=True,
    #                                                           internal=True)
    #             distA = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
    #             edgeFoundB = cmds.polyListComponentConversion(listVtx[r + 1], listVtx[r + 2], fv=True, te=True,
    #                                                           internal=True)
    #             distB = math.sqrt(((pB[0] - pC[0]) ** 2) + ((pB[1] - pC[1]) ** 2) + ((pB[2] - pC[2]) ** 2))
    #             collectList = collectList + edgeFoundA + edgeFoundB
    #             edgeLengthData[edgeFoundA[0]] = distA
    #             edgeLengthData[edgeFoundB[0]] = distB
    #
    #     if collectList:
    #         # avoid long edge
    #         values = list(edgeLengthData.values())
    #         # Calculate the threshold for the top 20% and bottom 20%
    #         num_values = len(values)
    #         top_threshold = sorted(values)[int(0.95 * num_values)]
    #         bottom_threshold = sorted(values)[int(0.05 * num_values)]
    #         # Filter out values outside the range
    #         filtered_data = {key: value for key, value in edgeLengthData.items() if
    #                          value >= bottom_threshold and value <= top_threshold}
    #         filtered_values = list(filtered_data.values())
    #         average_length = sum(filtered_values) / len(filtered_values)
    #         edgeLoopOverLengthLib = 2 * average_length
    #         overLength = [edge for edge, length in edgeLengthData.items() if length > edgeLoopOverLengthLib]
    #         collectList = list(set(collectList) - set(overLength))
    #         return collectList
    #
    # def roundInsetRun(self):
    #     currentsel = cmds.ls(sl=1, fl=1)
    #     if currentsel:
    #         geoSel = currentsel[0].split('.')[0]
    #         if geoSel:
    #             cmds.delete(geoSel, ch=1)
    #     getRoundV = cmds.floatSliderGrp('rBevelRound', q=1, v=1)
    #     if cmds.objExists("insetOffsetNod*"):
    #         listNode = cmds.ls("insetOffsetNod*")
    #         for s in listNode:
    #             getOldMesh = cmds.listConnections((s + '.outputGeometry'), scn=True)
    #             try:
    #                 getOldShape = cmds.listConnections((getOldMesh[0] + '.outputGeometry'), scn=True)
    #                 cmds.delete(getOldShape, ch=1)
    #             except:
    #                 cmds.delete(getOldMesh, ch=1)
    #     if cmds.objExists('insetOffsetNod*'):
    #         cmds.delete('insetOffsetNod*')
    #     if cmds.objExists('roundV'):
    #         cmds.delete('roundV')
    #     if cmds.objExists("insetOffsetV"):
    #         cmds.delete('nestOffsetV')
    #     if cmds.objExists('insetDataEdgeLoopListKeep'):
    #         cmds.delete('insetDataEdgeLoopListKeep')
    #     if cmds.objExists('cornerDisp'):
    #         cmds.setAttr("cornerDisp.creaseLevel", 0)
    #         cmds.delete('cornerDisp*')
    #     global insetDataPP
    #     global insetMesh
    #     global insetInnerEdges
    #     global insetFace
    #     global insetDataEdgeLoopList
    #     global insetFaceArea
    #     global newLoop
    #     global recordInnerCornerList
    #     global edgeLoopAngleLib
    #     global edgeLoopOverLengthLib
    #     global updatedNewSelEdge
    #     edgeLoopOverLengthLib = []
    #     recordInnerCornerList = []
    #     newLoop = []
    #     insetDataEdgeLoopList = []
    #     insetDataPP = []
    #     insetMesh = ''
    #     insetFace = ''
    #     insetInnerEdges = []
    #     insetFaceArea = 0
    #     selComponent = cmds.filterExpand(ex=1, sm=34)
    #     if selComponent:
    #         geo = cmds.ls(hl=1)
    #         cmds.makeIdentity(geo[0], apply=1, t=0, r=0, s=1, n=0, pn=1)
    #         insetMesh = geo[0]
    #         faceID = selComponent[0].split('[')[-1].split(']')[0]
    #         faceID = int(faceID)
    #         insetFaceArea = self.getfaceArea(insetMesh, faceID)
    #         edgeLoopCheck = cmds.polyListComponentConversion(selComponent, te=True)
    #         edgeLoopCheck = cmds.ls(edgeLoopCheck, fl=1)
    #         edgeLoopCheckInternal = cmds.polyListComponentConversion(selComponent, te=True, internal=1)
    #         edgeLoopCheckInternal = cmds.ls(edgeLoopCheckInternal, fl=1)
    #         tempCheck = []
    #         if edgeLoopCheckInternal:
    #             tempCheck = list(set(edgeLoopCheck) - set(edgeLoopCheckInternal))
    #         else:
    #             tempCheck = edgeLoopCheck
    #         insetDataEdgeLoopList = tempCheck
    #         cmds.sets(insetDataEdgeLoopList, name='insetDataEdgeLoopListKeep', text='insetDataEdgeLoopListKeep')
    #         cmds.setAttr('insetDataEdgeLoopListKeep.hiddenInOutliner', 1)
    #         if not cmds.attributeQuery('storeBevelV', node=geo[0], ex=True):
    #             cmds.addAttr(geo[0], ln='storeBevelV')
    #         cmds.setAttr((insetMesh + '.storeBevelV'), 0.01)
    #         cmds.polyExtrudeFacet(selComponent, constructionHistory=1, keepFacesTogether=1, divisions=1, twist=0,
    #                               taper=1, offset=0.01, thickness=0, smoothingAngle=30)
    #         insetFace = cmds.ls(sl=1, fl=1)
    #         if 'Shape' in insetFace[0]:
    #             insetFace = insetFace[1:]
    #         newLoop = cmds.polyListComponentConversion(insetFace, te=True)
    #         newLoop = cmds.ls(newLoop, fl=1)
    #         newLoopInternal = cmds.polyListComponentConversion(insetFace, te=True, internal=1)
    #         newLoopInternal = cmds.ls(newLoopInternal, fl=1)
    #         newEdgeLoopCheck = []
    #         if newLoopInternal:
    #             newEdgeLoopCheck = list(set(newLoop) - set(newLoopInternal))
    #         else:
    #             newEdgeLoopCheck = newLoop
    #         cmds.select(cl=1)
    #         findCorner = []
    #         newLoop = newEdgeLoopCheck
    #         checkEdgeRingGrp = self.getEdgeRingGroup(newLoop)
    #         cornerLoopCollect = []
    #         for c in checkEdgeRingGrp:
    #             getList = self.edgeLoopByAngle(c)
    #             if getList:
    #                 cornerLoopCollect = cornerLoopCollect + getList
    #         cornerLoop = cornerLoopCollect
    #         recordInnerCornerList = cornerLoop
    #         if cmds.objExists('tempLoopListKeep'):
    #             updatedNewSelEdge = cmds.sets('tempLoopListKeep', q=1)
    #             cmds.select(updatedNewSelEdge)
    #             cmds.ConvertSelectionToFaces()
    #             cmds.ConvertSelectionToEdgePerimeter()
    #             tempCheckList = cmds.ls(sl=1, fl=1)
    #             newCorner = list(set(newLoop) & set(tempCheckList))
    #             cornerLoop = newCorner
    #             cmds.delete('tempLoopListKeep')
    #         insetInnerEdges = cmds.polyListComponentConversion(insetFace, te=True, internal=True)
    #         insetInnerEdges = cmds.ls(insetInnerEdges, fl=1)
    #         if cornerLoop:
    #             cmds.createNode('creaseSet')
    #             cmds.rename('cornerDisp')
    #             cmds.setAttr("cornerDisp.creaseLevel", 1)
    #             cmds.setAttr('cornerDisp.hiddenInOutliner', 1)
    #             # cmds.select(cornerLoop)
    #             cornerLoopVtx = cmds.polyListComponentConversion(cornerLoop, tv=True)
    #             cornerLoopVtx = cmds.ls(cornerLoopVtx, fl=1)
    #             sortGrp = []
    #             sortGrp = self.getEdgeRingGroup(cornerLoop)
    #             if len(sortGrp) > 0:  # need a method to check loop number = protect corner number
    #                 ################ BUG #######################
    #                 for g in sortGrp:
    #                     self.matchCorner(g, 1)
    #                 point_positions = {}
    #                 for n in cornerLoopVtx:
    #                     vertex_position = cmds.pointPosition(n, w=True)
    #                     point_positions[n] = vertex_position
    #
    #                 for g in sortGrp:
    #                     self.matchCorner(g, 1.3)
    #                 newRoundMesh = cmds.duplicate(insetMesh, rr=1)
    #                 cmds.rename(newRoundMesh, 'roundV')
    #
    #                 for point_name, new_position in point_positions.items():
    #                     cmds.xform(point_name, translation=new_position, worldSpace=True)
    #
    #                 ##################################################################
    #                 innerCVList = cmds.polyListComponentConversion(cornerLoop, tv=True)
    #                 innerCVList = cmds.ls(innerCVList, fl=1)
    #                 edgeBorderFaceA = cmds.polyListComponentConversion(newLoop, tf=True)
    #                 edgeBorderFaceA = cmds.ls(edgeBorderFaceA, fl=1)
    #                 insetDataEdgeLoopList = cmds.sets("insetDataEdgeLoopListKeep", q=True)
    #                 edgeBorderFaceB = cmds.polyListComponentConversion(insetDataEdgeLoopList, tf=True)
    #                 edgeBorderFaceB = cmds.ls(edgeBorderFaceB, fl=1)
    #                 setA = set(edgeBorderFaceA)
    #                 setB = set(edgeBorderFaceB)
    #                 edgeBorderFace = list(setA.intersection(setB))
    #                 findRingList = cmds.polyListComponentConversion(edgeBorderFace, te=True, internal=True)
    #                 loopRingList = cmds.ls(findRingList, fl=1)
    #                 insetDataPP = []
    #                 moveP = []
    #                 baseP = []
    #                 checkCV = cmds.polyListComponentConversion(loopRingList[0], tv=True)
    #                 checkCV = cmds.ls(checkCV, fl=1)
    #                 bevelDistance = self.distanceBetween(checkCV[0], checkCV[-1])
    #                 for r in loopRingList:
    #                     checkCV = cmds.polyListComponentConversion(r, tv=True)
    #                     checkCV = cmds.ls(checkCV, fl=1)
    #                     if checkCV[0] in innerCVList:
    #                         moveP = checkCV[0]
    #                         baseP = checkCV[1]
    #                     else:
    #                         moveP = checkCV[1]
    #                         baseP = checkCV[0]
    #                     basePPos = cmds.pointPosition(baseP, w=1)
    #                     movePPos = cmds.pointPosition(moveP, w=1)
    #                     dataCollect = [moveP, basePPos, movePPos]
    #                     insetDataPP.append(dataCollect)
    #                 newMesh = cmds.duplicate(insetMesh, rr=1)
    #                 cmds.rename(newMesh, 'insetOffsetV')
    #                 refBevelV = math.sqrt(insetFaceArea) * 4
    #                 for v in range(len(insetDataPP)):
    #                     currentPos = cmds.pointPosition(insetDataPP[v][0], w=1)
    #                     posX = ((currentPos[0] - insetDataPP[v][1][0]) * (refBevelV)) + insetDataPP[v][1][0]
    #                     posY = ((currentPos[1] - insetDataPP[v][1][1]) * (refBevelV)) + insetDataPP[v][1][1]
    #                     posZ = ((currentPos[2] - insetDataPP[v][1][2]) * (refBevelV)) + insetDataPP[v][1][2]
    #                     cmds.move(posX, posY, posZ, insetDataPP[v][0].replace(insetMesh, 'insetOffsetV'), a=True,
    #                               ws=True)
    #                 # cmds.delete(insetMesh, ch=1)
    #                 blendName = cmds.blendShape('insetOffsetV', 'roundV', insetMesh)
    #                 cmds.delete('insetOffsetV', 'roundV')
    #                 cmds.rename(blendName, 'insetOffsetNode')
    #                 cmds.setAttr("insetOffsetNode.envelope", 2)
    #                 if cmds.objExists('blendOffsetNode') == 0:
    #                     cmds.group(em=1, n='blendOffsetNode')
    #                     cmds.addAttr('blendOffsetNode', longName='offset', attributeType='double', defaultValue=0)
    #                     cmds.setAttr('blendOffsetNode.offset', keyable=True)
    #                     cmds.setAttr('blendOffsetNode.hiddenInOutliner', 1)
    #                     cmds.connectControl('rInsetV', 'blendOffsetNode.offset')
    #                 cmds.connectAttr('blendOffsetNode.offset', 'insetOffsetNode.insetOffsetV', force=True)
    #                 cmds.connectControl('rBevelRound', 'insetOffsetNode.roundV')
    #                 cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
    #                 cmds.floatSliderGrp('rBevelRound', e=1, en=1, v=0)
    #                 cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #                 cmds.button('reFineButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
    #                 cmds.button('InsetRemoveButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
    #                 cmds.button('InsetCleaneButton', e=1, en=1)
    #                 cmds.button('InnerCornerEvenButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
    #                 cmds.select(cl=1)
    #                 cmds.select('cornerDisp')
    #                 cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
    #                 mel.eval(cmd)
    #                 cmds.select(insetFace, add=1)
    #         outliner_editor = 'outlinerPanel1'
    #         cmds.outlinerEditor(outliner_editor, e=1, refresh=True)
    #
    # def reFineSwitch(self):
    #     cmds.floatSliderGrp('rBevelAngle', e=1, en=1)
    #     cmds.floatSliderGrp('rInsetV', e=1, en=0)
    #     cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('reFineButton', l='update', e=1, en=1, bgc=[0.18, 0.48, 0.18], c='reFineMySelect()')
    #     cmds.button('InnerCornerEvenButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetRemoveButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('InsetCleaneButton', e=1, en=1, bgc=[0.48, 0.18, 0.18])
    #     self.reviewProtectCorner()
    #     self.edgeLoopByAngleUpdate()
    #     self.rBevelAngleUpdate()
    #     cmds.select('cornerDisp')
    #     cmds.setAttr('cornerDisp.creaseLevel', 1)
    #     cmds.scriptJob(event=["SelectionChanged", self.updateSelToCrease])
    #     cmds.scriptJob(uiDeleted=["RoundInsetUI", self.RoundInsetScriptJobClean])
    #
    # def edgeLoopByAngleUpdate(self):
    #     global insetDataEdgeLoopList
    #     global edgeLoopAngleLib
    #     global edgeLoopOverLengthLib
    #     insetDataEdgeLoopList = cmds.sets("insetDataEdgeLoopListKeep", q=True)
    #     edgeLoopAngleLib = {}
    #     sortGrp = self.getEdgeRingGroup(insetDataEdgeLoopList)
    #     for s in sortGrp:
    #         listVtx = self.vtxLoopOrder(s)
    #         listVtx.append(listVtx[0])
    #         listVtx.append(listVtx[1])
    #         for r in range(len(listVtx) - 2):
    #             pA = cmds.pointPosition(listVtx[r], w=True)
    #             pB = cmds.pointPosition(listVtx[r + 1], w=True)
    #             pC = cmds.pointPosition(listVtx[r + 2], w=True)
    #             edgeFoundA = cmds.polyListComponentConversion(listVtx[r], listVtx[r + 1], fv=True, te=True,
    #                                                           internal=True)
    #             distA = math.sqrt(((pA[0] - pB[0]) ** 2) + ((pA[1] - pB[1]) ** 2) + ((pA[2] - pB[2]) ** 2))
    #             edgeFoundB = cmds.polyListComponentConversion(listVtx[r + 1], listVtx[r + 2], fv=True, te=True,
    #                                                           internal=True)
    #             distB = math.sqrt(((pB[0] - pC[0]) ** 2) + ((pB[1] - pC[1]) ** 2) + ((pB[2] - pC[2]) ** 2))
    #             direction_vectorA = [pA[i] - pB[i] for i in range(3)]
    #             lengthA = sum(y ** 2 for y in direction_vectorA) ** 0.5
    #             normalized_directionA = [y / lengthA for y in direction_vectorA]
    #             direction_vectorB = [pB[i] - pC[i] for i in range(3)]
    #             lengthB = sum(y ** 2 for y in direction_vectorB) ** 0.5
    #             normalized_directionB = [y / lengthB for y in direction_vectorB]
    #             dot_product = sum([normalized_directionA[z] * normalized_directionB[z] for z in range(3)])
    #             angle_degrees = math.degrees(math.acos(dot_product))
    #             rounded_angle = round(angle_degrees, 4) + ((r + 1) * 0.0001)
    #             edgeFound = []
    #             edgeFound = [edgeFoundA[0], edgeFoundB[0]]
    #             if distA > edgeLoopOverLengthLib * 2:
    #                 edgeFound.remove(edgeFoundA[0])
    #             if distB > edgeLoopOverLengthLib * 2:
    #                 edgeFound.remove(edgeFoundB[0])
    #             if edgeFound:
    #                 edgeLoopAngleLib[edgeFound[0]] = rounded_angle
    #
    # def reviewProtectCorner(self):
    #     global insetFace
    #     global insetMesh
    #     shape_node = cmds.listRelatives(insetMesh, shapes=True)
    #     source_shape = shape_node[-1]
    #     destination_shape = shape_node[0]
    #     if insetFace:
    #         history_nodes = cmds.listHistory(insetMesh)
    #         delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
    #         for d in delList:
    #             if cmds.objExists(d):
    #                 cmds.delete(d)
    #     cmds.select(cl=1)
    #
    # def rBevelAngleUpdate(self):
    #     currentList = cmds.ls(sl=1, fl=1)
    #     global edgeLoopAngleLib
    #     checkListAA = []
    #     newV = cmds.floatSliderGrp('rBevelAngle', q=1, v=1)
    #     toCheck = 90 - newV
    #     overLength = [edge for edge, value in edgeLoopAngleLib.items() if value > toCheck]
    #     newList = list(set(overLength))
    #     if currentList != newList:
    #         cmds.select(newList, r=1)
    #         cmds.sets(clear="cornerDisp")
    #         cmds.sets(newList, forceElement="cornerDisp")
    #
    # def updateSelToCrease(self):
    #     updateList = cmds.ls(sl=1, fl=1)
    #     cmds.sets(clear="cornerDisp")
    #     cmds.sets(updateList, forceElement="cornerDisp")
    #
    # def RoundInsetScriptJobClean(self):
    #     foundError = 1
    #     while foundError > 0:
    #         jobs = cmds.scriptJob(listJobs=True)
    #         foundError = 0
    #         for j in jobs:
    #             if "updateSelTo" in j:
    #                 jID = j.split(':')[0]
    #                 try:
    #                     cmds.scriptJob(kill=int(jID))
    #                 except:
    #                     foundError = 1
    #
    # def reFineMySelect(self):
    #     updatedNewSelEdge = cmds.filterExpand(ex=1, sm=32)
    #     cmds.sets(updatedNewSelEdge, name='tempLoopListKeep', text='tempLoopListKeep')
    #     cmds.setAttr('tempLoopListKeep.hiddenInOutliner', 0)
    #     self.RoundInsetScriptJobClean()
    #     global insetFace
    #     global insetMesh
    #     global insetDataEdgeLoopList
    #     insetDataEdgeLoopList = []
    #     getRoundV = cmds.floatSliderGrp('rBevelRound', q=1, v=1)
    #     getInsetV = cmds.floatSliderGrp('rInsetV', q=1, v=1)
    #     shape_node = cmds.listRelatives(insetMesh, shapes=True)
    #     source_shape = shape_node[-1]
    #     destination_shape = shape_node[0]
    #     if insetFace:
    #         history_nodes = cmds.listHistory(insetMesh)
    #         delList = ["polyExtrudeFace1", "polyCrease1", "insetOffsetNod*"]
    #         for d in delList:
    #             if cmds.objExists(d):
    #                 cmds.delete(d)
    #         cmds.select(insetFace)
    #     if cmds.objExists('insetDataEdgeLoopListKeep'):
    #         cmds.delete('insetDataEdgeLoopListKeep')
    #     if cmds.objExists('cornerDisp'):
    #         cmds.setAttr('cornerDisp.creaseLevel', 0)
    #         cmds.delete('cornerDisp')
    #     self.roundInsetRun()
    #     cmds.setAttr('blendOffsetNode.offset', getInsetV)
    #     # cmds.setAttr('insetOffsetNode.roundV', getRoundV)
    #     # cmds.select('cornerDisp')
    #     cmd = 'doMenuComponentSelectionExt("' + insetMesh + '", "edge", 0);'
    #     mel.eval(cmd)
    #     cmds.select(insetFace, add=1)
    #     cmds.setToolTo('selectSuperContext')
    #     cmds.button('InsetButton', e=1, en=0, bgc=[0.18, 0.18, 0.18])
    #     cmds.button('reFineButton', l='Refine', e=1, en=1, c='reFineSwitch()', bgc=[0.28, 0.18, 0.38])
    #     cmds.button('InsetRemoveButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
    #     cmds.button('InsetCleaneButton', e=1, en=1)
    #     cmds.button('InnerCornerEvenButton', e=1, en=1, bgc=[0.28, 0.18, 0.38])
    #     cmds.floatSliderGrp('rBevelAngle', e=1, en=0)
    #     cmds.floatSliderGrp('rInsetV', e=1, en=1)

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
            print("method1")
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
            print("method2")
            extrude = cmds.polyExtrudeFacet(offset=0.05)
            cmds.select(facelist, replace=True)
            intFaceList = cmds.polyListComponentConversion(ff=True, tf=True, internal=True)
            cmds.select(extrude[0], add=True)
            cmds.ShowManipulatorTool()

    # --------------------- pivot
    def move_pivot_to_bottom_center(self, *args):
        selection = cmds.ls(sl=True, l=True)

        for sel in selection:
            bBox = cmds.xform(sel, q=True, ws=True, bb=True)
            centerX = (bBox[0] + bBox[3]) / 2
            bottomY = bBox[1]
            centerZ = (bBox[2] + bBox[5]) / 2

            cmds.xform(sel, ws=True, piv=(centerX, bottomY, centerZ))

    def snap_to_world_center_bottom(self, *args):
        selected_objects = cmds.ls(selection=True)

        if selected_objects:
            for obj in selected_objects:
                bbox = cmds.exactWorldBoundingBox(obj)

                center_x = (bbox[0] + bbox[3]) / 2
                center_y = (bbox[1] + bbox[4]) / 2
                center_z = (bbox[2] + bbox[5]) / 2

                offset_x = -center_x
                offset_y = -center_y
                offset_z = -center_z

                current_translate = cmds.getAttr("%s.translate" % obj)[0]

                new_translate_x = current_translate[0] + offset_x
                new_translate_y = current_translate[1] + offset_y
                new_translate_z = current_translate[2] + offset_z

                cmds.setAttr("%s.translate" % obj, new_translate_x, new_translate_y, new_translate_z)

                bbox = cmds.exactWorldBoundingBox(obj)

                bottom_y = bbox[1]

                current_translate = cmds.getAttr("%s.translate" % obj)[0]

                offset_y = -bottom_y

                new_translate_y = current_translate[1] + offset_y

                cmds.setAttr("%s.translateY" % obj, new_translate_y)

                cmds.makeIdentity(obj, apply=True, translate=True, rotate=True, scale=True, normal=False)
        else:
            cmds.inViewMessage(msg=u"没有选中任何物体！",
                               pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)

    # --------------------- test
    def test(self, *args):
        cmds.inViewMessage(msg=u"测试内容！",
                           pos="topRight", bkc=self.yellow_hex, fit=20, fade=True, fontSize=11)


lingju_geometry_tools = LingJuGeometryTools()

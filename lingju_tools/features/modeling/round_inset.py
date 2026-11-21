# coding: utf-8
"""倒角内插（Round_Inset）与双点吸附平面（Snap_2_Plane）。

Round_Inset：对选中面做可控圆角的插入，参数包含半径、分段与偏移，
可选择保留/删除原面，并支持实时预览式的重复执行。
Snap_2_Plane：用三点构建临时平面，把选中物体吸附到该平面朝向。
"""

import math
import re

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMaya as om

from ...core.messaging import lj_info, lj_warn

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


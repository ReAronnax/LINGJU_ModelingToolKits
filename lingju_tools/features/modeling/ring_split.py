# coding: utf-8
"""环线分割（原 ring_* 系列函数）。

两种模式：
  模式1：只处理选中的单条边，在单个四边面上做对边中点分割；
  模式2：沿整条 Ring 分割，对齐 Maya 原生 Edge Ring Utilities 的行为。
"""

import maya.cmds as cmds
import maya.api.OpenMaya as oma

from ...core.messaging import lj_info, lj_warn

# 环线分割面板控件名（UI 创建处与本模块共用，保证回调能定位到控件）
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

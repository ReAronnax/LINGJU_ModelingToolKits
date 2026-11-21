# coding: utf-8
"""扭曲面检查与 Spin 连线（原 twist_* 系列）。

检查：只针对四边面，用 Maya 当前三角化结果计算两个三角形几何法线的夹角，
选中超过阈值的四边面，不修改场景；有选中时只查选中，无选中时扫描场景。
三角化：直接调用 Maya 原生 polyTriangulate。
Spin：只对选中且被正好两个三角面共享的边执行 polyFlipEdge。
需要 NumPy（Maya 2022+ 自带），缺失时按钮给出提示，不影响其余功能。
"""

import re
import time

import maya.cmds as cmds
import maya.api.OpenMaya as oma

from ...core.messaging import lj_warn

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

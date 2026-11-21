# coding: utf-8
"""快速底面放置与绿面对齐（原 _bf_* 系列，内嵌自 SGCG_Placement_Tool）。

选底面：为单个网格生成可点击的候选底面（绿色小面片），点哪个面片就把模型
旋转到该面朝下、贴地并冻结变换。候选模式两种：
  六面安全(包围盒)：直接用世界包围盒 6 个面，永远能出结果；
  凸包(高模)：先采样顶点建轻量凸包，再聚合成平面候选，适合非盒子状高模。
绿面对齐：给两个物体（先目标、后待移动）各生成一组候选面，分别点选一个面片
后把待移动物体旋转+平移到贴合位置。
候选生成后挂 SelectionChanged 监听，点选绿色面片即触发放置。
"""

import math
import traceback as _bf_traceback

import maya.cmds as cmds
import maya.api.OpenMaya as oma

from ...core.messaging import lj_info, lj_warn

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

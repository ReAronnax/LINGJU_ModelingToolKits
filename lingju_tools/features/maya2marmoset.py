# coding: utf-8
"""Maya2Marmoset 烘焙桥接（m2m_* 全部功能 + Toolbag 监听器源码，一体不拆）。

组成：
  - 配置读写：optionVar 持久化
  - 命名规则：按高低模/烘焙组后缀分类
  - 自动分组：按距离或材质识别高低模配对
  - 场景工具：材质清洗、可见性、组归属
  - 导出：FBX 写入烘焙工作目录与 manifest
  - 桥接：文件收件箱 + 心跳，与 Toolbag 监听器通信
  - UI：烘焙面板（m2m_ui_Panel / m2m_ui_show）
  - M2M_LISTENER_SOURCE：内嵌的 Toolbag 监听器源码，安装插件时写出

注意：本模块按原文件整段迁移，函数内部结构保持不变；
M2M_LISTENER_SOURCE 字符串内部的 def 属于 Toolbag 侧代码，不是本模块函数。
"""

import ctypes
import hashlib
import io
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    import winreg
except ImportError:  # 非 Windows 下桥接不可用，其余功能不受影响
    winreg = None

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMayaUI as omui

from ..core.messaging import lj_error, lj_info, lj_warn
from ..core.qt_compat import QtCore, QtGui, QtWidgets, wrapInstance

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

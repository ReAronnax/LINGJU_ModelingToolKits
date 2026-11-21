# coding: utf-8
"""边链重建工具集：拓扑读取、位置索引、样条拟合与重采样、链操作。

内容（原文件 5078–6821 行）：
  - 样条数学：Akima / 多项式 / Catmull-Rom 插值、均匀重采样、最小二乘拟合
  - Topology / PositionIndex：网格拓扑与顶点位置索引
  - 链计算：弧长步进、转角、平面法线、连线生成、循环排序
  - 平滑：拉普拉斯 / Taubin 锐边、轴向平滑
  - 链操作：重采样、重新连线、均布、平滑、圆形化、解结、桥接

MeshError / undo_chunk / dag_path / active_mesh / component_name(s)
统一来自 core.maya_helpers，本模块不再重复实现。
"""

import math
from collections import defaultdict

try:
    import numpy as np
except ImportError:  # Maya 2022+ 自带 numpy，异常环境下降级为空
    np = None

import maya.cmds as cmds
import maya.api.OpenMaya as oma

from ...core.maya_helpers import (
    MeshError, active_mesh, component_name, component_names,
    dag_path, undo_chunk,
)

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

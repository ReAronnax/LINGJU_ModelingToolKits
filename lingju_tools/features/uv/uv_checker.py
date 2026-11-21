# coding: utf-8
"""UV Checker / 箭头流向检查材质（原 _uv_* 与 uv_checker_* 系列）。

功能：
  - 自动创建 checker 材质（带 UV 分辨率数字）与箭头流向材质；
  - 在 2048 / 512 / 32 三档分辨率之间循环切换；
  - 修改分辨率时复用已有节点网络，并清掉旧的数字贴图节点；
  - 非 UV 视图时给出提示。
数字贴图由 Maya 原生 layeredTexture 与渐变 ramp 组成，不依赖外部图片。
"""

import maya.cmds as cmds

from ...core.messaging import lj_error, lj_warn

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

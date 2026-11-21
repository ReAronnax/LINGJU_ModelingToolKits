# coding: utf-8
"""选面填充：沿选中边分裂后按 shell 扩选，再还原为面选择（原 Fill_Selection）。"""

import maya.cmds as cmds


class Fill_Selection:
    """选区填充工具。

    用法：选中一条边（或无需预选），执行 fill_selection 后，
    当前选区所在 shell 的面会被整片选中。
    """

    def fill_selection(self):
        """执行填充：先切边准备，再等选区变化事件触发后完成扩选。"""
        self.selection_fill_start()
        cmds.scriptJob(runOnce=True, event=["SelectionChanged", self.selection_fill_finish])

    def selection_fill_start(self):
        """准备阶段：无选中边时先转边选择，沿选区切边并建立临时节点。"""
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
        """完成阶段：清理选择约束与临时节点，恢复扩选结果。"""
        fill_selection = cmds.ls(selection=True)
        cmds.polySelectConstraint(shell=False)
        self.reset_poly_select_constraint()

        if cmds.objExists("fillSelectionTempNode"):
            cmds.delete("fillSelectionTempNode")

        if fill_selection:
            cmds.select(fill_selection)

    def reset_poly_select_constraint(self):
        """重置 polySelectConstraint 到默认关闭状态。"""
        cmds.polySelectConstraint(mode=0, type=0, shell=False)
        cmds.polySelectConstraint(disable=True)

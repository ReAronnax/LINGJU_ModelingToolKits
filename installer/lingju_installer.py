# -*- coding: utf-8 -*-
"""零距数码 maya 常用建模工具：拖拽安装器。

把同目录的 install.mel 拖进 Maya 主窗口即可：安装器按当前 Maya 版本从
<install.mel 所在目录>/<版本>/ 取 lingju_tools 包与图标，复制到当前用户的
Documents/maya/<版本>/scripts（存在 zh_CN / en_US 语言目录时一并写入），
然后在 LINGJU 工具栏标签页建好启动按钮。版本不受支持时只提示，不复制。
"""

import glob
import importlib
import os
import shutil
import sys
import traceback
import zipfile

import maya.cmds as cmds
import maya.mel as mel

PACKAGE_NAME = 'lingju_tools'
SHELF_TAB = 'LINGJU'
BUTTON_LABEL = u'零距建模'
BUTTON_ICON_LABEL = 'LJTools'
BUTTON_TOOLTIP = u'零距数码 maya 常用建模工具'
ICON_FILE_NAME = 'icon.png'
ICON_DEPLOY_NAME = PACKAGE_NAME + '_icon.png'
FALLBACK_ICON = 'commandButton.png'
LAUNCH_COMMAND = 'import lingju_tools; lingju_tools.show_ui()'
LOCALE_DIRS = ('zh_CN', 'en_US')


def _notice(title, lines, ok=u'知道了'):
    """拖拽安装时脚本编辑器里的 print 很容易被忽略，所以关键信息一律弹窗。

    :param title: 弹窗标题
    :param lines: 逐行显示的内容
    :param ok: 确认按钮文字
    """
    message = '\n'.join([line for line in lines])
    print(u'[零距建模工具安装器] {}\n{}'.format(title, message))
    cmds.confirmDialog(title=title, message=message, button=[ok],
                       defaultButton=ok, dismissString=ok)


def _shown(path):
    """把路径里的反斜杠换成斜杠，方便弹窗阅读。

    :param path: 原始路径
    :return: 统一用 / 分隔的路径
    """
    return path.replace('\\', '/')


def _confirm(title, message, ok=u'仍然安装工具', cancel=u'取消'):
    """需要用户决定时用双按钮；关窗等同取消。

    :param title: 弹窗标题
    :param message: 弹窗正文
    :param ok: 继续按钮文字
    :param cancel: 取消按钮文字
    :return: 用户选择继续时返回 True
    """
    result = cmds.confirmDialog(title=title, message=message,
                                button=[ok, cancel],
                                defaultButton=ok, dismissString=cancel)
    return result == ok


def _supported_versions(root):
    """扫描安装包里已经编译好的 Maya 版本目录。

    :param root: install.mel 所在目录
    :return: 升序排列的版本字符串列表
    """
    versions = []
    for name in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, name, PACKAGE_NAME)):
            versions.append(name)
    return versions


def _numpy_version():
    """能 import 就返回版本号，导入失败返回 None。"""
    try:
        import numpy
        return getattr(numpy, '__version__', '?')
    except Exception:
        return None


def _numpy_wheel(root, version):
    """安装包里当前 Maya Python ABI 对应的 numpy wheel。

    :param root: install.mel 所在目录
    :param version: Maya 版本号
    :return: wheel 路径，找不到返回 None
    """
    tag = '-cp{}{}-'.format(*sys.version_info[:2])
    folder = os.path.join(root, version)
    for path in sorted(glob.glob(os.path.join(folder, 'numpy-*.whl'))):
        if tag in os.path.basename(path):
            return path
    return None


def _install_numpy(root, version, target):
    """缺 numpy 时把安装包里的 wheel 解压进 scripts。

    wheel 就是 zip，解压即安装：不依赖 pip、不联网、也不需要管理员权限。

    :param root: install.mel 所在目录
    :param version: Maya 版本号
    :param target: 目标 scripts 目录
    :return: (状态文字, 是否继续安装工具)
    """
    found = _numpy_version()
    if found:
        return u'numpy 已存在（{}），跳过'.format(found), True

    wheel = _numpy_wheel(root, version)
    if not wheel:
        _notice(u'安装包缺少 numpy', [
            u'当前 Maya {} 需要 numpy，但安装包里没有对应版本的 wheel。'.format(version),
            u'工具仍可安装，但扭曲面检查 / 边重建不可用。',
            u'',
            u'补装方法（用 Maya 自带的 mayapy）：',
            u'mayapy.exe -m pip install numpy'])
        return u'numpy 未安装（缺少 wheel）', _confirm(
            u'继续安装工具？',
            u'numpy 缺失只影响扭曲面检查和边重建，其它功能正常。\n'
            u'仍要复制 pyd 并创建工具栏按钮吗？')

    try:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(target)
        importlib.invalidate_caches()
        if target not in sys.path:
            sys.path.insert(0, target)
        sys.modules.pop('numpy', None)
        installed = _numpy_version()
        if not installed:
            raise RuntimeError(u'解压后仍无法 import numpy')
    except Exception as exc:
        detail = [line.strip() for line in traceback.format_exc().splitlines()[-6:]]
        _notice(u'numpy 安装失败', [
            u'wheel：{}'.format(_shown(wheel)),
            u'解压到：{}'.format(_shown(target)),
            u'错误：{}'.format(exc)] + detail + [
            u'',
            u'工具仍可安装，但扭曲面检查 / 边重建不可用。'])
        return u'numpy 安装失败', _confirm(
            u'继续安装工具？',
            u'numpy 没有装上，只影响扭曲面检查和边重建。\n'
            u'仍要复制 pyd 并创建工具栏按钮吗？')

    return u'已从安装包安装 numpy {}'.format(installed), True


def _user_version_dir(version):
    """当前用户的 Documents/maya/<version>，不带语言子目录。

    :param version: Maya 版本号
    :return: 用户版本目录路径
    """
    prefs = (cmds.internalVar(userPrefDir=True) or '').replace('\\', '/')
    parts = [part for part in prefs.split('/') if part]
    if version in parts:
        return os.path.normpath('/'.join(parts[:parts.index(version) + 1]))
    return os.path.normpath(os.path.join(os.path.expanduser('~'),
                                         'Documents', 'maya', version))


def _scripts_dirs(version):
    """包要落地的脚本目录列表。

    默认的 scripts 目录一定在候选里；存在 zh_CN / en_US 语言目录时它们的
    scripts 也一并写入，因为 Maya 在对应语言环境下会优先读语言目录。
    一个都不存在时创建默认 scripts 目录。

    :param version: Maya 版本号
    :return: 已存在的 scripts 目录列表
    """
    base = _user_version_dir(version)
    candidates = [os.path.join(base, 'scripts')]
    candidates += [os.path.join(base, loc, 'scripts') for loc in LOCALE_DIRS]
    existing = [path for path in candidates if os.path.isdir(path)]
    if existing:
        return existing
    os.makedirs(candidates[0])
    return [candidates[0]]


def _shelf_tab_layout():
    """工具栏标签页的 layout 名，标签页不存在就用 Maya 自己的 addNewShelfTab 新建。

    :return: (顶层 tabLayout, LINGJU 标签页名)
    """
    top = mel.eval('$_ljTmp = $gShelfTopLevel')
    if not top:
        raise RuntimeError(u'没找到 Maya 工具栏容器，当前可能不是图形界面。')
    if cmds.shelfLayout(SHELF_TAB, query=True, exists=True):
        return top, SHELF_TAB
    shelf = mel.eval('addNewShelfTab "{}"'.format(SHELF_TAB)) or SHELF_TAB
    return top, shelf


def _drop_old_buttons(shelf):
    """先把同 tooltip 的旧按钮删掉，免得反复安装堆出一排图标。

    :param shelf: 工具栏标签页名
    """
    for item in (cmds.shelfLayout(shelf, query=True, childArray=True) or []):
        try:
            if cmds.shelfButton(item, query=True, annotation=True) == BUTTON_TOOLTIP:
                cmds.deleteUI(item)
        except Exception:
            continue


def _add_button(shelf, icon):
    """在工具栏标签页创建启动按钮。

    :param shelf: 工具栏标签页名
    :param icon: 按钮图标路径或 Maya 自带图标名
    """
    cmds.shelfButton(parent=shelf, label=BUTTON_LABEL, annotation=BUTTON_TOOLTIP,
                     statusBarMessage=BUTTON_TOOLTIP, image=icon,
                     imageOverlayLabel=BUTTON_ICON_LABEL,
                     command=LAUNCH_COMMAND, sourceType='python',
                     style='iconOnly', width=34, height=34, scaleIcon=True)


def install(root):
    """执行安装流程：校验版本 → 装 numpy → 复制包 → 建工具栏按钮。

    :param root: install.mel 所在目录（安装包根目录）
    """
    version = str(cmds.about(version=True))
    supported = _supported_versions(root)
    print(u'[零距建模工具安装器] Maya {} / 安装包 {}'.format(
        version, _shown(root)))

    if version not in supported:
        _notice(u'不支持的 Maya 版本', [
            u'当前 Maya 版本：{}'.format(version),
            u'本工具包已编译的版本：{}'.format(u'、'.join(supported) or u'无'),
            u'',
            u'未复制任何文件，也没有创建工具栏按钮。',
            u'请改用上面版本的 Maya 拖入 install.mel，'
            u'或让 TD 补编译 {} 版本。'.format(version)])
        return

    pkg_src = os.path.join(root, version, PACKAGE_NAME)
    pyd_files = glob.glob(os.path.join(pkg_src, '**', '*.pyd'), recursive=True)
    if not os.path.isdir(pkg_src) or not pyd_files:
        _notice(u'没找到 {} 版本的 pyd'.format(version), [
            u'期望目录：{}'.format(_shown(pkg_src)),
            u'',
            u'安装包不完整或该版本没有编译，未复制任何文件。'])
        return

    icon_src = os.path.join(root, version, ICON_FILE_NAME)
    if not os.path.isfile(icon_src):
        icon_src = ''

    try:
        targets = _scripts_dirs(version)
    except (IOError, OSError) as exc:
        _notice(u'安装失败', [
            u'写入脚本目录时出错：',
            u'{}'.format(exc),
            u'',
            u'通常是目录没有写权限：请以管理员身份运行 Maya，或手动把 {} '
            u'复制到 Documents/maya/{}/scripts。'.format(PACKAGE_NAME, version)])
        return

    numpy_state, proceed = _install_numpy(root, version, targets[0])
    if not proceed:
        print(u'[零距建模工具安装器] 已取消：{}'.format(numpy_state))
        return

    try:
        for folder in targets:
            dst = os.path.join(folder, PACKAGE_NAME)
            # 先删旧包，避免残留已删除模块的旧 pyd 与新包混用
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(pkg_src, dst)
            if icon_src:
                shutil.copy2(icon_src, os.path.join(folder, ICON_DEPLOY_NAME))
    except (IOError, OSError) as exc:
        _notice(u'安装失败', [
            u'复制工具包时出错：',
            u'{}'.format(exc),
            u'',
            u'通常是目录没有写权限：请以管理员身份运行 Maya，或手动把 {} '
            u'复制到 Documents/maya/{}/scripts。'.format(PACKAGE_NAME, version)])
        return

    folders = [_shown(path) for path in targets]
    icon_for_button = (_shown(os.path.join(targets[0], ICON_DEPLOY_NAME))
                       if icon_src else FALLBACK_ICON)
    try:
        top, shelf = _shelf_tab_layout()
        _drop_old_buttons(shelf)
        _add_button(shelf, icon_for_button)
        cmds.tabLayout(top, edit=True, selectTab=shelf)
        mel.eval('saveAllShelves "{}"'.format(top))
    except Exception as exc:
        detail = [line.strip() for line in traceback.format_exc().splitlines()[-6:]]
        _notice(u'工具包已复制，但工具栏没建成',
                [u'已复制到：'] + folders + [u'出错位置：'] + detail +
                [u'', u'可以自己建按钮，命令是：', LAUNCH_COMMAND])
        return

    # 让当前这个 Maya 会话不用重启就能导入新复制的包
    primary = os.path.normcase(targets[0])
    if primary not in [os.path.normcase(path) for path in sys.path]:
        sys.path.insert(0, targets[0])
    importlib.invalidate_caches()
    sys.modules.pop(PACKAGE_NAME, None)

    _notice(u'安装完成', [
        u'Maya 版本：{}'.format(version),
        u'numpy：{}'.format(numpy_state),
        u'已复制到：'] + folders + [
        u'工具栏：{} 标签页'.format(SHELF_TAB),
        u'',
        u'点新图标即可打开工具。'])

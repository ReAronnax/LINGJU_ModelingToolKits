# coding: utf-8
"""把 lingju_tools 包批量编译成各 Maya 版本的 pyd，并生成拖拽安装包。

用法（工程根目录）：
    python build_pyd.py                 编译全部版本，已有产物自动跳过
    python build_pyd.py 2026            只编译指定版本
    python build_pyd.py --force 2026    强制重编指定版本

输出：
    dist/<__version__>/<Maya版本>/lingju_tools/  纯 pyd，保持包目录结构
    dist/<__version__>/install.mel               拖进 Maya 主窗口即安装
    dist/<__version__>/lingju_installer.py       安装器实际逻辑
    dist/<__version__>/<Maya版本>/numpy-*.whl    缺 numpy 的机器安装时解压
"""

import ast
import shutil
import subprocess
import symtable
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PACKAGE_NAME = "lingju_tools"
PACKAGE_ROOT = PROJECT_ROOT / PACKAGE_NAME
DIST_ROOT = PROJECT_ROOT / "dist"
WORK_ROOT = PROJECT_ROOT / ".pyd_build"
NUMPY_WHEEL_CACHE = PROJECT_ROOT / "numpy_wheels"
INSTALLER_DIR = PROJECT_ROOT / "installer"
ICON_FILE = PROJECT_ROOT / "icon.png"
SETUP_FILE = "setup.py"

MAYA_VERSIONS = [2022, 2023, 2024, 2025, 2026]

# Maya 自带 Python 的 ABI 不同，numpy 必须按版本单独下载：
# 2022 是 Python 3.7，numpy 1.21.6 是最后支持 3.7 的版本。
NUMPY_SPECS = {
    2022: {"version": "1.21.6", "python": "3.7", "abi": "cp37m"},
    2023: {"version": "1.26.4", "python": "3.9", "abi": "cp39"},
    2024: {"version": "1.26.4", "python": "3.10", "abi": "cp310"},
    2025: {"version": "1.26.4", "python": "3.11", "abi": "cp311"},
    2026: {"version": "1.26.4", "python": "3.11", "abi": "cp311"},
}


def get_mayapy_path(year):
    """返回该 Maya 版本的 mayapy 路径。

    Maya 可能装在自定义路径 D:/Program Files/Maya{year}，也可能在默认的
    C:/Program Files/Autodesk/Maya{year}，按存在性依次查找。

    :param year: Maya 版本号（如 2026）
    :return: mayapy.exe 的 Path（可能不存在）
    """
    candidates = [
        Path("D:/Program Files/Maya%s/bin/mayapy.exe" % year),
        Path("C:/Program Files/Autodesk/Maya%s/bin/mayapy.exe" % year),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def read_version(package_root):
    """从包入口读取 __version__。

    :param package_root: lingju_tools 包目录
    :return: 版本号字符串；读取失败返回 "0.0.0"
    """
    init_file = package_root / "__init__.py"
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
    except Exception:
        return "0.0.0"
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant):
                        return str(node.value.value)
    return "0.0.0"


def iter_modules(package_root):
    """列出包里全部模块（含各级 __init__）。

    :param package_root: lingju_tools 包目录
    :return: [(模块名, 相对工程根的文件路径)]，模块名形如 lingju_tools.core.qt_compat
    """
    modules = []
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PROJECT_ROOT)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append((".".join(parts), rel))
    return modules


def inject_global_declarations(source):
    """分析源码中每个函数的符号表，为隐式全局变量补充 global 声明。

    Cython 3.x 在编译时会对函数内未声明、非内建且非模块级的名字抛出
    "undeclared name not builtin" 错误。该函数借助 symtable 识别出 Python
    视为隐式全局（被引用但未在函数内赋值）的名字，并通过 ast 在对应函数体
    顶部插入 global 声明，使 Cython 按全局查找处理，从而通过编译。

    :param source: 原始 Python 源码字符串
    :return: 注入 global 声明后的源码字符串；若分析失败则原样返回
    """
    try:
        tree = ast.parse(source)
        main_st = symtable.symtable(source, "<source>", "exec")
    except SyntaxError:
        return source

    func_globals = {}

    def _walk_symtable(st):
        """递归收集每个函数需要显式声明的全局名。"""
        for child in st.get_children():
            if child.get_type() == "function":
                names = set()
                for sym in child.get_symbols():
                    if sym.is_global() and sym.is_referenced():
                        names.add(sym.get_name())
                if names:
                    func_globals[child.get_lineno()] = names
            _walk_symtable(child)

    _walk_symtable(main_st)

    class _GlobalInjector(ast.NodeTransformer):
        """遍历函数定义节点，在函数体首部插入缺失的 global 声明。"""

        def _inject(self, node):
            self.generic_visit(node)
            names = func_globals.get(node.lineno)
            if not names:
                return node
            existing = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Global):
                    existing.update(stmt.names)
            need = sorted(n for n in names if n not in existing)
            if not need:
                return node
            gnode = ast.Global(names=need)
            ast.copy_location(gnode, node)
            insert_idx = 0
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                insert_idx = 1
            node.body.insert(insert_idx, gnode)
            return node

        def visit_FunctionDef(self, node):
            return self._inject(node)

        def visit_AsyncFunctionDef(self, node):
            return self._inject(node)

    tree = _GlobalInjector().visit(tree)
    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree)
    except Exception:
        return source


def prepare_sources(modules, build_root):
    """把包内源码复制到构建目录，并逐模块注入 global 声明。

    :param modules: iter_modules 的返回值
    :param build_root: 本版本的临时构建目录
    :return: 成功写入的模块列表
    """
    ready = []
    for module_name, rel in modules:
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        target = build_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(inject_global_declarations(source),
                          encoding="utf-8", newline="\n")
        ready.append((module_name, rel))
    return ready


def write_setup(build_root, modules):
    """在构建目录生成 setup.py（多 Extension 一次性编译整包）。

    :param build_root: 本版本的临时构建目录
    :param modules: [(模块名, 源文件相对路径)]
    :return: setup.py 路径
    """
    entries = []
    for module_name, rel in modules:
        entries.append("    (%r, [%r])," % (module_name, str(rel).replace("\\", "/")))
    content = """# coding: utf-8
import sys
from pathlib import Path
from setuptools import Extension, setup
from Cython.Build import cythonize

MODULES = [
%s
]

maya_root = Path(sys.executable).resolve().parent.parent
python_include = maya_root / "include" / ("Python%%d%%d" %% sys.version_info[:2]) / "Python"
include_dirs = [str(python_include)] if python_include.is_dir() else []

extensions = [
    Extension(name, sources, include_dirs=include_dirs,
              library_dirs=[str(maya_root / "lib")])
    for name, sources in MODULES
]

print("【Setup】正在编译 %%d 个模块" %% len(extensions))
setup(
    name="lingju_tools",
    ext_modules=cythonize(extensions, compiler_directives={"language_level": 3}),
    script_name="setup.py",
)
""" % "\n".join(entries)
    setup_path = build_root / SETUP_FILE
    setup_path.write_text(content, encoding="utf-8", newline="\n")
    return setup_path


def built_pyd_for(build_root, module_name):
    """定位某个模块编译出的 pyd 文件。

    build_ext --inplace 会把包 __init__ 输出成「包名.pyd」（与包目录同级），
    其余模块输出成 <模块名>.pyd。

    :param build_root: 本版本的临时构建目录
    :param module_name: 形如 lingju_tools.core.qt_compat
    :return: 找到的 pyd 路径列表
    """
    parts = module_name.split(".")
    base = build_root.joinpath(*parts)
    return list(base.parent.glob(base.name + ".*.pyd"))


def collect_pyds(build_root, modules, target_package_dir):
    """把编译产物按包结构复制到发布目录。

    包 __init__ 的产物需要改名成 __init__.<abi>.pyd 放进对应包目录，
    这样 import 时仍然按 pyd 包处理。

    :param build_root: 本版本的临时构建目录
    :param modules: [(模块名, 源文件相对路径)]
    :param target_package_dir: dist/<ver>/<year>/lingju_tools
    :return: 复制出的 pyd 数量
    """
    copied = 0
    for module_name, rel in modules:
        matches = built_pyd_for(build_root, module_name)
        if not matches:
            print("   ⚠️  未找到编译产物: %s" % module_name)
            continue
        built = matches[0]
        parts = module_name.split(".")
        if Path(rel).name == "__init__.py":
            # 包 __init__ 的产物由「包名.pyd」改名为「包目录/__init__.<abi>.pyd」
            suffix = built.name[len(parts[-1]):]
            dest_dir = target_package_dir.joinpath(*parts[1:])
            dest = dest_dir / ("__init__" + suffix)
        else:
            dest_dir = target_package_dir.joinpath(*parts[1:-1])
            dest = dest_dir / built.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(built), str(dest))
        copied += 1
    return copied


def cleanup_c_files(build_root):
    """删除 Cython 生成的中间 .c 文件。

    :param build_root: 本版本的临时构建目录
    """
    for path in build_root.rglob("*.c"):
        try:
            path.unlink()
        except OSError:
            pass


def numpy_wheel_pattern(year):
    """该 Maya 版本对应的 numpy wheel 文件名通配符。"""
    spec = NUMPY_SPECS[year]
    tag = "cp" + spec["python"].replace(".", "")
    return "numpy-%s-%s-*win_amd64.whl" % (spec["version"], tag)


def find_numpy_wheel(folder, year):
    """在 folder 里查找该 Maya 版本可用的 numpy wheel。

    :param folder: 待查找的目录
    :param year: Maya 版本号
    :return: 匹配到的 wheel 路径列表
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    exact = sorted(folder.glob(numpy_wheel_pattern(year)))
    if exact:
        return exact
    spec = NUMPY_SPECS.get(year)
    if not spec:
        return []
    tag = "cp" + spec["python"].replace(".", "")
    return sorted(folder.glob("numpy-*-%s-*win_amd64.whl" % tag))


def ensure_numpy_wheel(year, version_dir):
    """把 numpy wheel 放进版本目录；本地已有就不联网下载。

    查找顺序：版本目录 -> numpy_wheels 缓存 -> 联网下载。下载失败只警告，
    不阻断 pyd 编译：安装器发现缺 wheel 时会提示用户，工具仍可安装。

    :param year: Maya 版本号
    :param version_dir: dist/<ver>/<year>
    :return: 版本目录中的 wheel 路径；无法准备时返回 None
    """
    spec = NUMPY_SPECS.get(year)
    if not spec:
        print("   ⚠️  Maya %s 未配置 numpy wheel，安装器会提示缺 numpy" % year)
        return None

    ready = find_numpy_wheel(version_dir, year)
    if ready:
        print("   numpy wheel 已就绪: %s" % ready[0].name)
        return ready[0]

    cached = find_numpy_wheel(NUMPY_WHEEL_CACHE, year)
    if cached:
        target = version_dir / cached[0].name
        shutil.copy2(str(cached[0]), str(target))
        print("   numpy_wheels/ 已有对应 wheel，跳过下载: %s" % target.name)
        return target

    NUMPY_WHEEL_CACHE.mkdir(parents=True, exist_ok=True)
    print("   下载 numpy %s（%s, win_amd64）..." % (
        spec["version"], "cp" + spec["python"].replace(".", "")))
    command = [
        sys.executable, "-m", "pip", "download",
        "numpy==%s" % spec["version"],
        "--only-binary=:all:", "--no-deps",
        "--platform", "win_amd64",
        "--python-version", spec["python"],
        "--implementation", "cp",
        "--abi", spec["abi"],
        "-d", str(NUMPY_WHEEL_CACHE),
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("   ⚠️  numpy wheel 下载失败（Maya %s），安装包将不含 numpy" % year)
        print("   可以手动把 wheel 放到 %s/ 后重跑。" % NUMPY_WHEEL_CACHE.name)
        print((result.stderr or result.stdout).strip()[-1500:])
        return None

    downloaded = find_numpy_wheel(NUMPY_WHEEL_CACHE, year)
    if not downloaded:
        print("   ⚠️  pip 未产出预期的 numpy wheel（Maya %s）" % year)
        return None

    target = version_dir / downloaded[0].name
    shutil.copy2(str(downloaded[0]), str(target))
    print("   numpy wheel 已下载: %s" % target.name)
    return target


def write_install_package(dist_version_dir, built_years):
    """把安装入口与图标复制进发布目录。

    :param dist_version_dir: dist/<ver>
    :param built_years: 已编译成功的 Maya 版本列表
    :return: 生成的安装包文件列表
    """
    generated = []
    for year in built_years:
        version_dir = dist_version_dir / str(year)
        if not version_dir.is_dir():
            continue
        if ICON_FILE.exists():
            shutil.copy2(str(ICON_FILE), str(version_dir / ICON_FILE.name))
        else:
            print("   ⚠️  未找到 %s，安装后按钮将使用 Maya 自带图标" % ICON_FILE.name)
        if not find_numpy_wheel(version_dir, year):
            print("   ⚠️  %s 缺少 numpy wheel，安装后该版本需手动装 numpy" % year)

    for name in ("install.mel", "lingju_installer.py"):
        source = INSTALLER_DIR / name
        if not source.exists():
            print("   ⚠️  缺少安装器模板: %s" % source)
            continue
        target = dist_version_dir / name
        if name == "install.mel":
            # MEL 的编码跟随系统区域设置，必须保持纯 ASCII
            target.write_text(source.read_text(encoding="utf-8"),
                              encoding="ascii", newline="\r\n")
        else:
            shutil.copy2(str(source), str(target))
        generated.append(str(target.relative_to(PROJECT_ROOT)))
        print("   已生成安装入口: %s" % name)
    return generated


def build_year(year, modules, dist_version_dir):
    """编译单个 Maya 版本的整包 pyd。

    :param year: Maya 版本号
    :param modules: iter_modules 的返回值
    :param dist_version_dir: dist/<ver>
    :return: 成功返回 True
    """
    mayapy = get_mayapy_path(year)
    if not mayapy.exists():
        print("   ⚠️  跳过：未找到 mayapy.exe\n      %s" % mayapy)
        return False

    build_root = WORK_ROOT / str(year)
    if build_root.exists():
        shutil.rmtree(build_root)
    try:
        ready = prepare_sources(modules, build_root)
        setup_path = write_setup(build_root, ready)
        print("   调用 mayapy 编译 %d 个模块（Maya %s）..." % (len(ready), year))
        result = subprocess.run(
            [str(mayapy), str(setup_path), "build_ext", "--inplace"],
            cwd=str(build_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print("   ❌ 编译失败（Maya %s）:" % year)
            print(result.stdout.strip()[-4000:])
            print(result.stderr.strip()[-4000:])
            return False

        target_package_dir = dist_version_dir / str(year) / PACKAGE_NAME
        if target_package_dir.exists():
            shutil.rmtree(target_package_dir)
        copied = collect_pyds(build_root, ready, target_package_dir)
        print("   ✅ Maya %s：已输出 %d 个 pyd" % (year, copied))
        return copied == len(ready)
    finally:
        cleanup_c_files(build_root)
        shutil.rmtree(build_root, ignore_errors=True)


def main():
    """入口：按版本矩阵编译整包，并生成拖拽安装包。"""
    if not PACKAGE_ROOT.is_dir():
        print("❌ 当前目录未找到 %s 包：%s" % (PACKAGE_NAME, PROJECT_ROOT))
        return 1

    args = [arg for arg in sys.argv[1:]]
    force = "--force" in args
    years = [int(arg) for arg in args if arg.isdigit()] or list(MAYA_VERSIONS)

    version = read_version(PACKAGE_ROOT)
    dist_version_dir = DIST_ROOT / ("v" + version)
    dist_version_dir.mkdir(parents=True, exist_ok=True)
    modules = iter_modules(PACKAGE_ROOT)
    print("✅ 包 %s 版本 %s，共 %d 个模块" % (PACKAGE_NAME, version, len(modules)))

    built_years = []
    for year in years:
        target_package_dir = dist_version_dir / str(year) / PACKAGE_NAME
        existing = list(target_package_dir.rglob("*.pyd")) if target_package_dir.is_dir() else []
        if existing and not force:
            if len(existing) == len(modules):
                print("\n跳过 Maya %s：已有 %d 个 pyd" % (year, len(existing)))
                built_years.append(year)
            else:
                print("\nMaya %s 产物不完整（%d/%d），重新编译" % (
                    year, len(existing), len(modules)))
                if build_year(year, modules, dist_version_dir):
                    built_years.append(year)
        else:
            print("\n========== 编译 Maya %s ==========" % year)
            if build_year(year, modules, dist_version_dir):
                built_years.append(year)

    print("\n📦 生成安装包...")
    for year in built_years:
        ensure_numpy_wheel(year, dist_version_dir / str(year))
    generated = write_install_package(dist_version_dir, built_years)

    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    print("\n编译完成：成功 %d 个版本（%s）" % (
        len(built_years), "、".join(str(year) for year in built_years) or "无"))
    print("输出目录：%s" % dist_version_dir)
    for item in generated:
        print("  • %s" % item)
    return 0


if __name__ == "__main__":
    sys.exit(main())

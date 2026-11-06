# -*- coding: utf-8 -*-
"""本地校验 webui.json 是否符合 MaiBot 真实 WebUIExtension schema。

【为什么要有这个脚本】
2026-11-04 我用旧格式（webui_version / title / sections）写 webui.json 直接部署，
导致插件注册失败、**整个 WebUI 崩了**。根因是没验证就上线。
本脚本把真实 schema（从服务器拉下来的 webui_schema.py）跑一遍，
以后改 webui.json 先跑它，过了再部署。

【真实 schema 要点（2026-11-04 从服务器 src/plugin_runtime/webui_schema.py 核对）】
- 顶层必须 schema_version=2；旧的 webui_version/title/sections 全部非法（extra=forbid）。
- 必须 pages 数组（1~20 个），每页有 id/title/mode。
- 有 placement="workspace" 的页面时，顶层必须有 workspace_title。
- custom 页用 entry 指向插件内相对 .html；**不能**内联 document（宿主自己读文件注入）。
- custom 页必须在 required_capabilities 里声明 "custom_pages"。
- 参数是严格模型（strict=True）：不能写 default，只能写 type/required/minimum/maximum/choices 等。
- queries 的 binding 不能声明 confirmation。

用法：
    python tests/check_webui_schema.py
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT.parent / ".workbuddy" / "tmp" / "webui_schema.py"


def load_schema_module():
    """加载从服务器拉下来的真实 webui_schema.py。"""
    if not SCHEMA_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_maibot_webui_schema", SCHEMA_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_maibot_webui_schema"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    print("=" * 64)
    print("校验 webui.json（MaiBot 真实 WebUIExtension schema）")
    print("=" * 64)

    webui_path = ROOT / "webui.json"
    if not webui_path.is_file():
        print("\n结果：没有 webui.json。")
        print("说明：宿主 load_webui_extension() 在文件不存在时直接 return None，")
        print("      插件照常加载、只是不注册 WebUI 页面。这是合法状态。")
        return 0

    module = load_schema_module()
    if module is None:
        print(f"\n[跳过] 找不到真实 schema 文件：{SCHEMA_PATH}")
        print("       请先把服务器上的 src/plugin_runtime/webui_schema.py 拉到该路径。")
        return 2

    declaration = json.loads(webui_path.read_text(encoding="utf-8"))

    # 1) 先查最容易犯的旧字段
    legacy = {"webui_version", "title", "page", "queries", "actions", "sections", "skeleton_samples"}
    hit = sorted(legacy & set(declaration))
    if hit:
        print(f"\n✗ 出现旧版字段（schema v1 已废弃）：{hit}")
        print("  正确写法：schema_version=2 + pages 数组 + workspace_title")
        return 1

    # 2) 模拟宿主行为：custom 页的 document 由宿主从 entry 读文件注入，JSON 里不能写。
    #    校验前先按宿主逻辑补上，否则会误报 document Field required。
    simulated = json.loads(json.dumps(declaration))
    for page in simulated.get("pages", []):
        if not isinstance(page, dict) or page.get("mode") != "custom":
            continue
        if "document" in page:
            print("\n✗ custom 页面不能内联 document，必须用 entry 指向 HTML")
            return 1
        entry = page.get("entry", "")
        target = (ROOT / entry).resolve()
        if not target.is_file():
            print(f"\n✗ custom 页面的 entry 文件不存在：{entry}")
            return 1
        page["document"] = target.read_text(encoding="utf-8")

    # 3) 用真实模型校验
    try:
        extension = module.WebUIExtension.model_validate(simulated)
    except Exception as exc:
        print(f"\n✗ WebUIExtension 校验失败：")
        print(f"  {type(exc).__name__}")
        for line in str(exc).splitlines():
            print(f"  {line}")
        return 1

    # 3) 检查 entry 指向的 HTML 是否真的存在
    print(f"\n✓ 模型校验通过（{len(extension.pages)} 个页面）")
    ok = True
    for page in extension.pages:
        mode = getattr(page, "mode", "?")
        print(f"  · 页面 {page.id}（{mode}，placement={page.placement}）")
        entry = getattr(page, "entry", None)
        if entry:
            target = (ROOT / entry).resolve()
            if not target.is_file():
                print(f"    ✗ entry 指向的文件不存在：{entry}")
                ok = False
            elif not target.resolve().is_relative_to(ROOT.resolve()):
                print(f"    ✗ entry 逃出插件目录：{entry}")
                ok = False
            else:
                size = target.stat().st_size
                print(f"    ✓ entry 存在：{entry}（{size} 字节）")
                if size > 2 * 1024 * 1024:
                    print("    ✗ 单个自定义页面不能超过 2 MiB")
                    ok = False
        total_chars = sum(len(str(v)) for v in [page.title, page.description])
        if total_chars > 4040:
            print("    ✗ 标题或描述过长（Label ≤ 80，Text ≤ 4000）")
            ok = False

    if extension.workspace_title is None and any(p.placement == "workspace" for p in extension.pages):
        print("\n✗ 有 workspace 页面但没写 workspace_title")
        ok = False

    print("\n" + "=" * 64)
    if ok:
        print("结果：通过，可以部署")
        return 0
    print("结果：存在问题，不要部署")
    return 1


if __name__ == "__main__":
    sys.exit(main())

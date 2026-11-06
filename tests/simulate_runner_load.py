# -*- coding: utf-8 -*-
"""模拟 MaiBot Runner 的加载方式，在本地复现「插件能不能装上」。

Runner 的真实行为（2026-11-04 在服务器上确认）：
- 用 importlib 按**文件路径**加载 plugin.py，模块名是扁平的 `plugin`；
- **不把插件目录加进 sys.path**（这是 `No module named 'contracts'` 的根因）；
- 先把 manifest 用 pydantic 严格模型校验（extra="forbid"），未声明字段直接拒。

本脚本按同样方式加载，任何 import 错误都会在这里暴露，
不用每次重启线上 MaiBot 去试。

用法：
    python tests/simulate_runner_load.py
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT.parents[0] / ".workbuddy" / "tmp" / "sdk_local"

# 只把 SDK 加进 sys.path；**刻意不加插件目录**，复现 Runner 的真实环境。
if SDK_ROOT.is_dir() and str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

OUT = io.StringIO()


def log(msg: str = "") -> None:
    print(msg)
    OUT.write(msg + "\n")


log("=" * 66)
log("模拟 Runner 加载：只给 SDK 路径，不给插件目录")
log("=" * 66)

# ---------- 1) manifest 严格校验 ----------
log("\n[1] manifest 校验")
allowed = {
    "manifest_version", "version", "name", "description", "author", "license",
    "urls", "host_application", "sdk", "dependencies", "llm_providers",
    "capabilities", "i18n", "id", "plugin_type", "display", "changelog",
}
manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
extra = set(manifest) - allowed
missing = {"manifest_version", "version", "name", "description", "author",
           "license", "urls", "host_application", "sdk", "id"} - set(manifest)
if extra:
    log(f"  ✗ 未声明字段：{sorted(extra)}")
else:
    log("  ✓ 无未声明字段")
if missing:
    log(f"  ✗ 缺少必填字段：{sorted(missing)}")
else:
    log("  ✓ 必填字段齐全")
if manifest.get("manifest_version") != 2:
    log(f"  ✗ manifest_version 必须是 2，实际 {manifest.get('manifest_version')}")
else:
    log("  ✓ manifest_version = 2")

# ---------- 2) 按 Runner 方式加载 plugin.py ----------
log("\n[2] 按文件路径加载 plugin.py（模块名 = plugin，不加插件目录到 sys.path）")
# 清理可能残留的同名模块，保证公平
for name in list(sys.modules):
    if name in {"plugin", "contracts", "state", "models", "runtime", "worker", "config_model", "layers"}:
        del sys.modules[name]
# 关键：把插件目录从 sys.path 里剔掉（如果之前有）
sys.path[:] = [p for p in sys.path if Path(p).resolve() != ROOT]

spec = importlib.util.spec_from_file_location("plugin", ROOT / "plugin.py")
module = importlib.util.module_from_spec(spec)
sys.modules["plugin"] = module
try:
    spec.loader.exec_module(module)
    log("  ✓ plugin.py 导入成功（路径自举生效）")
except Exception as exc:
    log(f"  ✗ 导入失败：{type(exc).__name__}: {exc}")
    import traceback

    log(traceback.format_exc()[-2000:])
    sys.exit(1)

# ---------- 3) 组件收集 ----------
log("\n[3] 组件收集")
try:
    from maibot_sdk.components import collect_components

    instance = module.create_plugin()
    comps = collect_components(instance)
    text = json.dumps(comps, ensure_ascii=False, default=str) if not isinstance(comps, str) else comps
    expected = (
        "hx_login", "hx_login_check", "hx_logout", "hx_status",
        "hx_try_publish", "hx_browse", "hx_interact", "hx_summarize",
        "hx_log", "huixiang_status", "huixiang_dry_run",
        # 后台面板用的 API 组件必须全部注册
        "hx_overview", "hx_action_log", "hx_browse_now", "hx_reset_circuit", "hx_partitions",
        "hx_login_status", "hx_request_qr", "hx_logout",
        "hx_get_config", "hx_save_config", "hx_config_schema",
    )
    missing_comps = [c for c in expected if c not in text]
    if missing_comps:
        log(f"  ✗ 缺失组件：{missing_comps}")
    else:
        log(f"  ✓ {len(expected)} 个组件全部被收集")
except Exception as exc:
    log(f"  ✗ 组件收集失败：{type(exc).__name__}: {exc}")

# ---------- 3.5) webui.json 与已注册 API 的一致性 ----------
log("\n[3.5] 后台面板声明")
webui_path = ROOT / "webui.json"
if not webui_path.is_file():
    log("  ✓ 无 webui.json（宿主允许，插件照常加载）")
else:
    try:
        import re

        webui = json.loads(webui_path.read_text(encoding="utf-8"))
        if webui.get("schema_version") != 2:
            log("  ✗ schema_version 必须是 2")
        else:
            log("  ✓ schema_version = 2")
        pages = webui.get("pages", [])
        log(f"  ✓ 页面数 {len(pages)}")
        referenced: set[str] = set()
        for page in pages:
            for binding in list((page.get("queries") or {}).values()) + list((page.get("actions") or {}).values()):
                if isinstance(binding, dict) and binding.get("api"):
                    referenced.add(str(binding["api"]))
            entry = page.get("entry")
            if entry:
                target = (ROOT / entry).resolve()
                if not target.is_file():
                    log(f"  ✗ 页面文件不存在：{entry}")
                else:
                    html = target.read_text(encoding="utf-8")
                    names = set(re.findall(r"maibot\.(?:query|action)\(\s*['\"]([^'\"]+)['\"]", html))
                    for n in names:
                        declared = list((page.get("queries") or {}).keys()) + list((page.get("actions") or {}).keys())
                        if n not in declared:
                            log(f"  ✗ 页面调用了未声明的 query/action：{n}")
                    # 宿主 bridge 硬限制：pending.size >= 2 直接抛
                    # "Two requests are already running"。页面必须用串行队列，
                    # 不能用并发聚合一次发多个请求（真机踩坑 2026-11-06）。
                    # 先剥掉注释，避免注释里提到这些词造成误报。
                    code = re.sub(r"//[^\n]*", "", html)
                    code = re.sub(r"/\*[\s\S]*?\*/", "", code)
                    concurrent = "Promise.all(" in code or "Promise.allSettled(" in code
                    if concurrent:
                        log("  ✗ 页面用了并发聚合调用，会触发「Two requests are already running」")
                    elif "function serial(" not in code:
                        log("  ✗ 页面缺少串行队列，多请求可能并发")
                    else:
                        log("  ✓ 页面请求走串行队列（不会撞宿主 2 请求上限）")
                    # 宿主会扫描页面计算样式里的 font-family，出现主题字体名就去
                    # 沙箱 fetch('/fonts/xxx')；取不到会在页面顶部弹
                    # "Error: Theme font unavailable: JetBrainsMono-Medium.ttf"。
                    # 页面必须用 !important 覆盖成系统字体，别让主题字体名出现。
                    theme_fonts = ("JetBrains Mono", "Departure Mono", "Ark Pixel", "Jersey 20")
                    declared_css = html.split("</style>", 1)[0] if "</style>" in html else html
                    leaked = [n for n in theme_fonts if n in declared_css]
                    if leaked:
                        log(f"  ✗ 页面 CSS 里出现主题字体名，会触发字体加载：{leaked}")
                    elif "!important" not in declared_css:
                        log("  ✗ 页面缺少 !important 字体覆盖")
                    else:
                        log("  ✓ 页面已用系统字体覆盖主题字体（避免字体加载报错）")
                    log(f"  ✓ 页面存在：{entry}（{len(html)} 字符）")
        if referenced:
            log(f"  ✓ 引用 API：{sorted(referenced)}")
            missing_apis = [a for a in referenced if a not in text]
            if missing_apis:
                log(f"  ✗ 这些 API 没有用 @API 注册：{missing_apis}")
            else:
                log("  ✓ 所有引用的 API 都已注册")
    except Exception as exc:
        log(f"  ✗ webui.json 校验失败：{type(exc).__name__}: {exc}")

# ---------- 4) 默认配置 ----------
log("\n[4] 默认配置")
try:
    from maibot_sdk.config import build_plugin_default_config

    default = build_plugin_default_config(module.HuiXiangPlugin.config_model)
    log(f"  ✓ 段：{list(default.keys())}")
    log(f"  ✓ config_version = {default.get('plugin', {}).get('config_version')}")
except Exception as exc:
    log(f"  ✗ 默认配置失败：{type(exc).__name__}: {exc}")

# ---------- 5) on_load / 命令 / on_unload ----------
log("\n[5] 生命周期与命令")


async def _lifecycle() -> None:
    import tempfile

    from maibot_sdk.context import PluginContext, PluginPaths

    with tempfile.TemporaryDirectory() as tmp:
        inst = module.create_plugin()
        inst._set_context(
            PluginContext(
                plugin_id="local.huixiang-operator",
                paths=PluginPaths(data_dir=Path(tmp), runtime_dir=Path(tmp) / "rt"),
            )
        )
        inst.set_plugin_config({"plugin": {"config_version": "1.0.0"}})
        await inst.on_load()
        log(f"  ✓ on_load 完成（worker={inst._worker}）")
        ok, msg, _ = await inst.cmd_status()
        log(f"  ✓ cmd_status -> {ok} | {msg.splitlines()[0] if msg else ''}")
        ok2, msg2, _ = await inst.cmd_action_log()
        log(f"  ✓ cmd_action_log -> {ok2} | {msg2[:40]}")
        ok3, msg3, _ = await inst.cmd_login_check()
        log(f"  ✓ cmd_login_check -> {ok3} | {msg3[:60]}")
        await inst.on_unload()
        log("  ✓ on_unload 完成")


import asyncio

try:
    asyncio.run(_lifecycle())
except Exception as exc:
    log(f"  ✗ 生命周期失败：{type(exc).__name__}: {exc}")
    import traceback

    log(traceback.format_exc()[-2000:])

log("\n" + "=" * 66)
if "✗" in OUT.getvalue():
    log("结果：存在失败项，不要部署")
    sys.exit(1)
log("结果：全部通过，可以部署")

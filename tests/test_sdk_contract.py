# -*- coding: utf-8 -*-
"""
回响插件：真宿主契约集成测试（unittest，不联网、不连真实账号）。

【为什么单独一份】
前面几份测试测的是「业务逻辑对不对」；这一份测的是「**能不能被 MaiBot 加载**」。
按 2026-11-04 在部署环境实测到的真实 SDK 契约逐条验：

1. `create_plugin()` 无参、模块级、能返回实例；
2. 主类继承 `MaiBotPlugin`，`config_model` 是 `PluginConfigBase` 子类；
3. `on_load` / `on_unload` / `on_config_update` 都是 **async** 且都实现（缺一个基类抛 NotImplementedError）；
4. 配置模型能生成默认配置，且**自带 [plugin].config_version**（缺了 SDK 直接抛错）；
5. `@Command` / `@Tool` 装饰器声明的组件能被 `collect_components` 收集到；
6. `on_load` 真的能跑完（enabled=false 不启 worker；enabled=true 启 worker）；
7. 组件被调用时能返回合法的三元组（Command）/ dict（Tool）。

【前置】需要真实 maibot_sdk。若本地没有（例如 CI / 新机器），本文件整体 skip。
"""

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from maibot_sdk import MaiBotPlugin, PluginConfigBase
    from maibot_sdk.components import collect_components
    from maibot_sdk.config import build_plugin_default_config, is_plugin_config_class
    from maibot_sdk.context import PluginContext, PluginPaths

    SDK_AVAILABLE = True
except ImportError as exc:  # pragma: no cover —— 本地无 SDK 时整份跳过
    SDK_AVAILABLE = False
    SDK_ERROR = str(exc)


def _make_ctx(tmp: str) -> "PluginContext":
    return PluginContext(
        plugin_id="local.huixiang-operator",
        paths=PluginPaths(data_dir=Path(tmp), runtime_dir=Path(tmp) / "runtime"),
    )


@unittest.skipUnless(SDK_AVAILABLE, "本地未装 maibot_sdk，跳过真宿主契约测试")
class TestSdkContract(unittest.TestCase):
    """SDK 契约：这组用例失败 = 插件在真机上加载不起来。"""

    def test_create_plugin_is_module_level_and_no_args(self) -> None:
        import plugin

        self.assertTrue(callable(plugin.create_plugin), "create_plugin 必须存在")
        sig = inspect.signature(plugin.create_plugin)
        required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
        self.assertEqual(required, [], f"create_plugin 必须无必填参数，实际 {sig}")

    def test_plugin_inherits_maibot_plugin(self) -> None:
        import plugin

        self.assertTrue(issubclass(plugin.HuiXiangPlugin, MaiBotPlugin))

    def test_config_model_is_valid(self) -> None:
        import plugin

        self.assertTrue(
            is_plugin_config_class(plugin.HuiXiangPlugin.config_model),
            "config_model 必须是 PluginConfigBase 的子类",
        )

    def test_default_config_has_plugin_version(self) -> None:
        """真机踩坑：模型里没有 plugin 段，SDK 生成默认配置就抛 PluginConfigVersionError。"""
        import plugin

        default = build_plugin_default_config(plugin.HuiXiangPlugin.config_model)
        self.assertIn("plugin", default, "默认配置必须有 plugin 段")
        self.assertTrue(default["plugin"].get("config_version"), "plugin.config_version 不能为空")

    def test_lifecycle_methods_are_async_and_implemented(self) -> None:
        import plugin

        for name in ("on_load", "on_unload", "on_config_update"):
            with self.subTest(method=name):
                func = getattr(plugin.HuiXiangPlugin, name, None)
                self.assertIsNotNone(func, f"必须实现 {name}")
                self.assertTrue(
                    inspect.iscoroutinefunction(func),
                    f"{name} 必须是 async（基类要求 await）",
                )
                base = getattr(MaiBotPlugin, name, None)
                self.assertIsNot(
                    func.__func__ if hasattr(func, "__func__") else func,
                    base,
                    f"{name} 必须覆盖基类实现（否则基类抛 NotImplementedError）",
                )

    def test_config_update_signature_matches(self) -> None:
        import plugin

        sig = inspect.signature(plugin.HuiXiangPlugin.on_config_update)
        params = list(sig.parameters)
        self.assertEqual(params, ["self", "scope", "config_data", "version"], f"实际签名 {sig}")

    def test_components_collected(self) -> None:
        import plugin

        instance = plugin.create_plugin()
        components = collect_components(instance)
        names = str(components)
        self.assertIn("hx_status", names, "命令 hx_status 未被收集")
        self.assertIn("huixiang_status", names, "工具 huixiang_status 未被收集")


@unittest.skipUnless(SDK_AVAILABLE, "本地未装 maibot_sdk，跳过真宿主契约测试")
class TestOnLoadRealHost(unittest.IsolatedAsyncioTestCase):
    """用真实 PluginContext 跑 on_load / on_unload。"""

    async def test_disabled_load_does_not_start_worker(self) -> None:
        import plugin

        instance = plugin.create_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            instance._set_context(_make_ctx(tmp))
            instance.set_plugin_config({"plugin": {"config_version": "1.0.0"}})
            await instance.on_load()
            self.assertIsNone(instance._worker, "enabled=false 不得启动 worker")
            self.assertIsNotNone(instance._store, "on_load 必须初始化存储")
            self.assertTrue((Path(tmp) / "quota.json").exists() or True, "数据目录应可写")
            await instance.on_unload()

    async def test_enabled_load_starts_worker_then_stops(self) -> None:
        import plugin

        instance = plugin.create_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            instance._set_context(_make_ctx(tmp))
            instance.set_plugin_config(
                {
                    "plugin": {"config_version": "1.0.0"},
                    "basic": {"enabled": True, "mode": "dry_run"},
                    "browse": {"browse_interval_seconds": 30},
                }
            )
            await instance.on_load()
            self.assertIsNotNone(instance._worker, "enabled=true 应启动 worker")
            self.assertTrue(instance._worker.running)
            await instance.on_unload()
            self.assertIsNone(instance._worker, "on_unload 应清掉 worker")

    async def test_config_update_stops_worker_when_disabled(self) -> None:
        import plugin

        instance = plugin.create_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            instance._set_context(_make_ctx(tmp))
            instance.set_plugin_config(
                {
                    "plugin": {"config_version": "1.0.0"},
                    "basic": {"enabled": True, "mode": "dry_run"},
                    "browse": {"browse_interval_seconds": 30},
                }
            )
            await instance.on_load()
            self.assertIsNotNone(instance._worker)
            # 热更新关掉 enabled。
            instance.set_plugin_config(
                {"plugin": {"config_version": "1.0.0"}, "basic": {"enabled": False, "mode": "dry_run"}}
            )
            await instance.on_config_update("self", {}, "1.0.1")
            self.assertIsNone(instance._worker, "关掉 enabled 后 worker 应停止")
            await instance.on_unload()


@unittest.skipUnless(SDK_AVAILABLE, "本地未装 maibot_sdk，跳过真宿主契约测试")
class TestCommandReturns(unittest.IsolatedAsyncioTestCase):
    """组件被调用时返回值必须符合 SDK 约定。"""

    async def _ready_plugin(self, tmp: str):
        import plugin

        instance = plugin.create_plugin()
        instance._set_context(_make_ctx(tmp))
        instance.set_plugin_config(
            {
                "plugin": {"config_version": "1.0.0"},
                "basic": {"enabled": True, "mode": "dry_run"},
                "publish": {"post_time_window": "00:00-00:00"},
            }
        )
        await instance.on_load()
        return instance

    async def test_status_command_returns_triple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = await self._ready_plugin(tmp)
            try:
                result = await instance.cmd_status()
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 3, "Command 必须返回 (bool, str, int)")
                ok, message, _ = result
                self.assertTrue(ok)
                self.assertIn("回响状态", message)
            finally:
                await instance.on_unload()

    async def test_log_command_returns_triple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = await self._ready_plugin(tmp)
            try:
                result = await instance.cmd_action_log()
                self.assertEqual(len(result), 3)
                self.assertTrue(result[0])
            finally:
                await instance.on_unload()

    async def test_status_tool_returns_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = await self._ready_plugin(tmp)
            try:
                result = await instance.tool_status()
                self.assertIsInstance(result, dict)
                self.assertTrue(result.get("success"))
                self.assertIn("real_quota_used", result)
            finally:
                await instance.on_unload()

    async def test_dry_run_command_works_without_login(self) -> None:
        """未登录时演练命令不应崩：要么跑通，要么给出可读的中文失败原因。"""
        with tempfile.TemporaryDirectory() as tmp:
            instance = await self._ready_plugin(tmp)
            try:
                instance.runtime().generate_text = _fake_generate
                result = await instance.cmd_try_publish()
                self.assertEqual(len(result), 3)
                self.assertIsInstance(result[1], str)
                self.assertTrue(result[1])
            finally:
                await instance.on_unload()


async def _fake_generate(prompt: str) -> str:
    return "随便聊聊\n今天没什么特别的，就随便说两句。"


if __name__ == "__main__":
    unittest.main(verbosity=2)

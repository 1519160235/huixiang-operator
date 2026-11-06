# -*- coding: utf-8 -*-
"""
回响插件：骨架自检单测（unittest，纯本地、不接真实 API、不联网）。

【覆盖点】
1. 六块模块可 import，且 LAYER_ORDER 顺序正确（接入→感知→创作→护栏→发布+状态）；
2. create_plugin() 能返回实例；
3. 配置模型十一段字段齐全（basic/login/browse/interact/publish/summarize/image/create/sense/guard/links，
   第 12 段 [plugin] 的 config_version 由 config.toml 单独校验）；
4. 演练与真实额度键不同；
5. config.toml 中 [plugin] 段位于最后一处（防历史踩坑，文件未就位时跳过）；
6. enabled=false 时 on_load 不启动后台 worker。

【运行】
    python tests/test_skeleton.py
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import importlib
import re
import sys
import types
import unittest
from pathlib import Path

# 插件目录名含连字符，不作为包导入；把目录加入 sys.path，各模块以顶层模块名互引。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 六块顺序（第 6 块「状态/存储」为顶层模块 state，不在 layers 包内，仅以字符串纳入顺序）。
EXPECTED_LAYER_ORDER = ("inbound", "sense", "create", "guard", "publish", "state")
# layers 包内实际存在的层模块（state 不在包内，单独 import 校验）。
LAYER_MODULES = ("inbound", "sense", "create", "guard", "publish")

# 配置模型「十一段」字段集：字段散落在各分组类中，故自检时汇总所有配置类的字段后统一校验。
# 第 12 段 [plugin] 只放 config_version，由 test_plugin_section_is_last_in_config_toml 单独校验。
EXPECTED_CONFIG_FIELDS = {
    # [basic]
    "enabled", "mode", "profile_id",
    # [login]
    "auto_login",
    # [browse]
    "partition_mode", "selected_partitions", "refresh_partitions_interval_minutes",
    "browse_interval_seconds", "posts_per_round", "follow_partition_feed",
    # [interact]
    "comment_enabled", "comment_interval_seconds", "reply_mentions", "reply_replies",
    # [publish]
    "default_partition", "linked_topics_enabled", "max_topics", "custom_topics",
    "post_time_window", "post_interval_minutes", "readback_verify",
    # [summarize]
    "trigger_by", "count_threshold", "time_hours",
    "min_chars", "max_chars", "min_paragraphs", "max_paragraphs",
    # [image]
    "with_image", "image_count", "image_api_base_url", "image_api_key", "image_model",
    # [create]
    "tone", "no_blank_line_between_paragraphs",
    # [sense]
    "use_life_frequency", "pause_on_sleep", "social_battery_threshold",
    # [guard]
    "daily_quota_post", "daily_quota_comment", "daily_quota_like", "daily_quota_reply",
    "de_ai_flavor", "ai_flavor_threshold", "captcha_circuit_break", "dry_run_no_quota_cost",
    # [links]
    "voice_broadcast", "qzone_sync", "diary_link",
}

# 顶层 HuiXiangConfig 应聚合的十一个分组字段（对应 config.toml 十一段）。
EXPECTED_CONFIG_GROUPS = {
    "basic", "login", "browse", "interact", "publish", "summarize", "image", "create", "sense", "guard", "links",
}

# 匹配 TOML 普通表标题行 [xxx]，允许行尾注释；不匹配 [[数组表]]。
SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


class TestSkeleton(unittest.TestCase):
    """骨架自检：只验证结构与契约，不触发任何网络调用。"""

    def test_six_blocks_importable_and_layer_order(self) -> None:
        """六块模块均可 import，且 LAYER_ORDER 为 接入→感知→创作→护栏→发布+状态。"""
        for name in ("layers.inbound", "layers.sense", "layers.create", "layers.guard", "layers.publish"):
            importlib.import_module(name)
        importlib.import_module("state")  # 第 6 块：状态/存储
        import layers
        self.assertEqual(tuple(layers.LAYER_ORDER), EXPECTED_LAYER_ORDER)
        for layer_name in LAYER_MODULES:
            self.assertTrue(hasattr(layers, layer_name), f"layers 未导出 {layer_name}")

    def test_create_plugin_returns_instance(self) -> None:
        """create_plugin() 能返回 MaiBotPlugin 的实例，且具备生命周期方法。"""
        try:
            import plugin
        except ImportError as exc:
            self.skipTest(f"本地未装 maibot_sdk，跳过插件入口用例：{exc}")
        instance = plugin.create_plugin()
        self.assertIsNotNone(instance)
        self.assertIsInstance(instance, plugin.MaiBotPlugin)
        for method in ("on_load", "on_unload", "on_config_update"):
            self.assertTrue(callable(getattr(instance, method, None)), f"缺少生命周期方法 {method}")
        self.assertTrue(hasattr(plugin.HuiXiangPlugin, "config_model"), "主类缺少 config_model 类属性")

    def test_config_model_ten_sections_fields_complete(self) -> None:
        """配置模型十一段字段齐全，且入口已按契约引用配置模型（import 并用，不在此定义）。"""
        try:
            config_model_module = importlib.import_module("config_model")
        except ImportError:
            self.skipTest("config_model.py 尚未就位，字段校验待其完成后生效")

        # 兼容 dataclass / pydantic / 普通带注解类：dataclass 取 fields，其余取 __annotations__。
        field_names: set[str] = set()
        for obj in vars(config_model_module).values():
            if not isinstance(obj, type) or obj.__module__ != config_model_module.__name__:
                continue
            if dataclasses.is_dataclass(obj):
                field_names.update(f.name for f in dataclasses.fields(obj))
            field_names.update(getattr(obj, "__annotations__", {}).keys())
        if not field_names:
            self.skipTest("config_model.py 中尚未定义带字段的配置类")

        missing = EXPECTED_CONFIG_FIELDS - field_names
        self.assertFalse(missing, f"配置模型缺少约定字段：{sorted(missing)}（约定清单见本文件顶部）")

        # 顶层 HuiXiangConfig 必须聚合十一个分组段。
        top = getattr(config_model_module, "HuiXiangConfig", None)
        self.assertIsNotNone(top, "config_model.py 缺少顶层 HuiXiangConfig")
        top_annotations = set(getattr(top, "__annotations__", {}).keys())
        missing_groups = EXPECTED_CONFIG_GROUPS - top_annotations
        self.assertFalse(missing_groups, f"顶层 HuiXiangConfig 缺少分组字段：{sorted(missing_groups)}")

        # 入口必须引用（而非定义）配置模型。
        try:
            import plugin
        except ImportError as exc:
            self.skipTest(f"本地未装 maibot_sdk，跳过入口引用校验：{exc}")
        self.assertIsNotNone(
            plugin.HuiXiangPlugin.config_model,
            "主类 config_model 未绑定到 config_model.py 的配置类",
        )

    def test_dry_run_and_real_quota_keys_differ(self) -> None:
        """演练与真实额度分桶键必须不同，确保两套额度隔离。"""
        import state
        from models import RunMode
        day = "2026-11-04"
        dry_key = state.quota_bucket_key(RunMode.DRY_RUN, day)
        real_key = state.quota_bucket_key(RunMode.REAL, day)
        self.assertNotEqual(dry_key, real_key, "演练与真实额度键相同，会导致额度互相污染")
        self.assertNotEqual(state.DRY_RUN_QUOTA_PREFIX, state.REAL_QUOTA_PREFIX)
        self.assertTrue(dry_key.startswith(state.DRY_RUN_QUOTA_PREFIX))
        self.assertTrue(real_key.startswith(state.REAL_QUOTA_PREFIX))

    def test_plugin_section_is_last_in_config_toml(self) -> None:
        """config.toml 中 [plugin] 段必须是最后一处表，防止后续键值被吞进 [plugin]。"""
        config_path = ROOT / "config.toml"
        if not config_path.exists():
            self.skipTest("config.toml 尚未就位，段顺序校验待其完成后生效")

        last_section: str | None = None
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue
            match = SECTION_RE.match(line)
            if match:
                last_section = match.group(1).strip()
        if last_section is None:
            self.skipTest("config.toml 中暂无任何 [section] 段")
        self.assertEqual(
            last_section,
            "plugin",
            f"config.toml 最后一个表是 [{last_section}]，应为 [plugin]（历史踩坑：段顺序靠后会被吞并）",
        )

    def test_disabled_plugin_does_not_start_worker(self) -> None:
        """enabled=false 时 on_load 不启动后台 worker（_worker 保持 None）。"""
        import asyncio
        import tempfile
        from pathlib import Path

        try:
            import plugin
            from maibot_sdk.context import PluginContext, PluginPaths
        except ImportError as exc:
            self.skipTest(f"本地未装 maibot_sdk，跳过 on_load 用例：{exc}")

        instance = plugin.create_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            instance._set_context(
                PluginContext(
                    plugin_id="local.huixiang-operator",
                    paths=PluginPaths(data_dir=Path(tmp), runtime_dir=Path(tmp) / "rt"),
                )
            )
            instance.set_plugin_config({"plugin": {"config_version": "1.0.0"}})
            asyncio.run(instance.on_load())
            self.assertIsNone(instance._worker, "enabled=false 时不应启动后台 worker")
            self.assertIsNotNone(instance._store, "on_load 应初始化状态存储")


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""内置默认值自查：别人装完插件，模型地址应该已经指向中转站。

【背景 2026-11-08】用户要求「内置的时候把里面用的模型链接全部换成这个中转站的」。
含义是：新装用户不用研究该填哪个地址、哪个模型，装完填个 key 就能出图。
但 key 不能内置 —— 仓库是公开的，写死 key 等于把额度送人。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from defaults import DEFAULT_IMAGE_BASE_URL, DEFAULT_IMAGE_MODEL  # noqa: E402
from layers import imagegen  # noqa: E402


class _Sec:
    def __init__(self, data: dict) -> None:
        for k, v in data.items():
            setattr(self, k, v)


class _Cfg:
    def __init__(self, image: dict) -> None:
        self.image = _Sec(image)


class _Ctx:
    class paths:
        data_dir = "/tmp/nonexistent-data-dir"
        state_dir = "/tmp/nonexistent-data-dir"

    def __init__(self, image: dict) -> None:
        self.config = _Cfg(image)


class DefaultEndpointTest(unittest.TestCase):
    def test_defaults_are_a_real_https_endpoint(self) -> None:
        self.assertTrue(DEFAULT_IMAGE_BASE_URL.startswith("https://"))
        self.assertIn("feiyuapi", DEFAULT_IMAGE_BASE_URL)
        # 代码里拼的是 {base}/images/generations，末尾不能带斜杠。
        self.assertFalse(DEFAULT_IMAGE_BASE_URL.endswith("/"))
        self.assertTrue(DEFAULT_IMAGE_MODEL)

    def test_empty_config_uses_defaults(self) -> None:
        # 老配置文件里这两个字段是空串，升级后不该变成「生图配置不完整」。
        base, key, model, size = imagegen._image_cfg(_Ctx({}))
        self.assertEqual(base, DEFAULT_IMAGE_BASE_URL)
        self.assertEqual(model, DEFAULT_IMAGE_MODEL)
        self.assertEqual(key, "")
        self.assertEqual(size, "")

    def test_custom_values_win(self) -> None:
        base, key, model, _ = imagegen._image_cfg(
            _Ctx({"image_api_base_url": "https://example.com/v1",
                  "image_api_key": "k", "image_model": "my-model"})
        )
        self.assertEqual(base, "https://example.com/v1")
        self.assertEqual(model, "my-model")
        self.assertEqual(key, "k")

    def test_no_key_still_falls_back_to_host(self) -> None:
        # 没填 key 时不能硬去请求中转站；返回 None 让上层回退宿主「画图」配置。
        base, key, model, size = imagegen._image_cfg(_Ctx({}))
        self.assertIsNone(imagegen.build_generator(base, key, model, size))

    def test_key_alone_is_enough(self) -> None:
        base, key, model, size = imagegen._image_cfg(_Ctx({"image_api_key": "sk-test"}))
        gen = imagegen.build_generator(base, key, model, size)
        self.assertIsNotNone(gen, "填了 key 就该能直连默认中转站")
        self.assertEqual(gen.base_url, DEFAULT_IMAGE_BASE_URL)
        self.assertEqual(gen.model, DEFAULT_IMAGE_MODEL)


class ConfigFieldDefaultsTest(unittest.TestCase):
    """配置页上「恢复默认」时给出的值，也必须是中转站。"""

    def test_config_model_defaults(self) -> None:
        src = (ROOT / "config_model.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_IMAGE_BASE_URL", src)
        self.assertIn("DEFAULT_IMAGE_MODEL", src)

    def test_config_toml_ships_with_endpoint(self) -> None:
        import tomllib

        data = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
        image = data["image"]
        self.assertEqual(image["image_api_base_url"], DEFAULT_IMAGE_BASE_URL)
        self.assertEqual(image["image_model"], DEFAULT_IMAGE_MODEL)
        # key 不能随仓库分发。部署实例上这个字段是使用者自己填的，跳过这一条。
        if image["image_api_key"]:
            self.skipTest("这是已部署实例的运行配置，key 由使用者填写；只校验仓库模板")
        self.assertEqual(image["image_api_key"], "")


class MainConfigCandidatesTest(unittest.TestCase):
    """原来写死作者机器的 E:\\mai 路径，别人装等于没这个回退。"""

    def test_no_hardcoded_drive_path(self) -> None:
        # 校验源码里没有写死的盘符路径（运行时 cwd 恰好在该盘下不算）。
        src = (ROOT / "layers" / "imagegen.py").read_text(encoding="utf-8")
        self.assertNotIn("E:\\mai", src)

    def test_env_root_is_used(self) -> None:
        old = os.environ.get("MAIBOT_ROOT")
        try:
            os.environ["MAIBOT_ROOT"] = "/opt/maibot"
            cands = [str(p) for p in imagegen._main_config_candidates(_Ctx({}))]
            self.assertIn(str(Path("/opt/maibot/config/model_config.toml")), cands)
        finally:
            if old is None:
                os.environ.pop("MAIBOT_ROOT", None)
            else:
                os.environ["MAIBOT_ROOT"] = old


if __name__ == "__main__":
    unittest.main()

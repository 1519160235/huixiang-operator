# -*- coding: utf-8 -*-
"""内置默认值（单独放一个模块，避免 config_model 与 layers 互相 import）。

【2026-11-08 用户要求】「别人用的话，内置的时候可以把里面用的模型链接全部换成这个中转站的。」
所以配图接口地址、模型名都预填成作者自己在用的中转站，装完只要填自己的 key 就能出图。

**只内置地址和模型名，不内置 key**：
本仓库是公开仓库，写死 key 等于把额度送给所有人，而且很快会被刷爆。
key 由使用者自己注册获取（配置页和文档里都给了注册入口）。
"""

# 飞鱼中转（OpenAI 兼容），作者在用；换成任何兼容接口都行。
DEFAULT_IMAGE_BASE_URL = "https://ai.feiyuapi.top/v1"

# 支持 4K 输出的图像模型；尺寸见 [image].image_size。
DEFAULT_IMAGE_MODEL = "gpt-image-2.6"

__all__ = ["DEFAULT_IMAGE_BASE_URL", "DEFAULT_IMAGE_MODEL"]

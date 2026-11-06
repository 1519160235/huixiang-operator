# -*- coding: utf-8 -*-
"""本地测试引导：把从服务器拉下来的真实 maibot_sdk 加进 sys.path。

真实 SDK 只在 .workbuddy/tmp/sdk_local/ 下，不进仓库、不进 Git。
测试文件只需 `import _sdk_boot` 即可让 `from maibot_sdk import ...` 生效。
"""
import sys
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parents[2] / ".workbuddy" / "tmp" / "sdk_local"
if _SDK_ROOT.is_dir() and str(_SDK_ROOT) not in sys.path:
    # 插到最前面，确保优先于任何同名占位包。
    sys.path.insert(0, str(_SDK_ROOT))

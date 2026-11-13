# -*- coding: utf-8 -*-
"""core 包：全部核心能力，UI 只是它的一层皮。"""
from .config import Config, ROOT, DATA_DIR, PLUGINS_DIR, SETTINGS_FILE, PHRASES_FILE
from .store import Library
from .sender import Sender

__all__ = ["Config", "Library", "Sender", "ROOT", "DATA_DIR", "PLUGINS_DIR",
           "SETTINGS_FILE", "PHRASES_FILE"]

# Flet 前端（Flutter 渲染）。逻辑一律来自 dispatcher.controller / dispatcher.viewmodel，
# 本包只做「造控件 / 读控件 / 写控件 / 绑事件」。
from .app import run_flet

__all__ = ["run_flet"]

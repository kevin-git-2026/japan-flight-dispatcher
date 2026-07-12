# ================= 语义配色 / 字体 =================
# viewmodel.Span.style（语义名）→ Flet 文本样式。
# 沿用 tk 版的语义色（命中/适航=绿、警告/超限=橙、链接=蓝、次要=灰），不新造一套；
# 但选用中间调（*_600/700）——它们在浅色与深色底上都读得清，无需按主题切两套表。
#
# 等宽字体只用在【该用的地方】：航路串、METAR、呼号、日志。其余走系统 UI 字体
# （修掉 tk 版满屏 Consolas 的廉价感）。

import flet as ft

MONO = "Consolas"

# 界面字体：不指定的话 Flutter 会自己挑 CJK 回退字体，各处字重/字形不一致，中日文看着发虚发花。
# 「微软雅黑 UI」是 Windows 自带的简中系统 UI 字体（Vista 起就有，无需随程序打包），
# 中文 + 日文假名 / 汉字都覆盖得住（界面里有「南風運用・深夜早朝」这类日文）。
# 非 Windows 上取不到这个字体时，Flutter 会自动回退，不会崩。
UI_FONT = "Microsoft YaHei UI"

GREEN = ft.Colors.GREEN_600          # 命中 / 适航 / 有地景
AMBER = ft.Colors.AMBER_800          # 警告 / 已飞过 / 超限
RED = ft.Colors.RED_600              # 无地景 / 军用
BLUE = ft.Colors.BLUE_700            # 链接 / 代码
NAVY = ft.Colors.INDIGO_700          # ICAO / 呼号
GREY = ft.Colors.GREY                # 次要信息
FAINT = ft.Colors.GREY_500           # 分隔线

_B = ft.FontWeight.BOLD

# style → TextStyle
_STYLES = {
    "h1":       ft.TextStyle(size=17, weight=_B, color=GREEN),
    "sep":      ft.TextStyle(size=12, color=FAINT),
    "label":    ft.TextStyle(size=13, color=GREY),
    "code":     ft.TextStyle(size=15, weight=_B, color=NAVY, font_family=MONO),
    "dist":     ft.TextStyle(size=13, weight=_B, font_family=MONO),
    "scn_yes":  ft.TextStyle(size=13, color=GREEN),
    "scn_no":   ft.TextStyle(size=13, color=RED),
    "mil":      ft.TextStyle(size=13, weight=_B, color=RED, font_family=MONO),
    "flown":    ft.TextStyle(size=13, color=AMBER),
    "section":  ft.TextStyle(size=14, weight=_B),
    "aip":      ft.TextStyle(size=13, font_family=MONO),
    "muted":    ft.TextStyle(size=12, color=GREY),
    "warn":     ft.TextStyle(size=12, color=AMBER),
    "success":  ft.TextStyle(size=14, weight=_B, color=GREEN),
    "partial":  ft.TextStyle(size=14, weight=_B, color=AMBER),
    "nomatch":  ft.TextStyle(size=14, weight=_B, color=GREY),
    "flight":   ft.TextStyle(size=13, font_family=MONO),
    "callsign": ft.TextStyle(size=14, weight=_B, color=NAVY, font_family=MONO),
    "link":     ft.TextStyle(size=13, color=BLUE, decoration=ft.TextDecoration.UNDERLINE),
    "sblink":   ft.TextStyle(size=13, color=BLUE, decoration=ft.TextDecoration.UNDERLINE),
    "maplink":  ft.TextStyle(size=13, color=GREEN, decoration=ft.TextDecoration.UNDERLINE),
}

_DEFAULT = ft.TextStyle(size=13)


def span_style(name):
    return _STYLES.get(name, _DEFAULT)


def panel(content, expand=None, width=None, padding=14, visible=True):
    """卡片样式的容器。
    ⚠️ 用 `ft.Container` 而【不是】`ft.Card`：Card 会按内容的**固有宽度**撑开、不受父级约束，
       里头一放 `expand` 的下拉行就会把卡片顶出栏外、横向溢出窗口。Container 老实遵守父级约束。"""
    return ft.Container(
        content=content, expand=expand, width=width, padding=padding, visible=visible,
        bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.08, ft.Colors.ON_SURFACE)),
        border_radius=10)


def apply_theme(page):
    page.theme_mode = ft.ThemeMode.SYSTEM               # 深色模式跟随系统（tk 版做不到）
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO, font_family=UI_FONT)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO, font_family=UI_FONT)

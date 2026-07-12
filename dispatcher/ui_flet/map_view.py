# ================= 航路地图（flet_map）=================
# 吃 viewmodel.map_model()（折线 / 三档 marker / bounds 全是纯数据），这里只负责翻译成 flet_map 控件。
# Pillow 与 tkintermapview 双双出局：marker 可以直接用任意 Flet 控件画，不必再生成位图。
#
# ⚠️ 瓦片源不能用 OSM 官方：flutter_map 的 User-Agent 被 tile.openstreetmap.org 403 封禁，
#    而 TileLayer 没有 headers 字段（只有 user_agent_package_name，设了也没用）→ 改不了 UA。
#    实测可用的替代源见下（CartoDB，且有配套深色版可跟深色模式联动）。
#
# ⚠️ Flet 无多窗口 → tk 版「一条航路开一个窗口、可并排比对」做不到；
#    改为【同一个地图视图里的标签页】：N 条航路一键切换。这是本次迁移唯一的功能倒退。

import flet as ft
import flet_map as fmap

from .. import viewmodel as VM

_UA = "jp-flight-dispatcher"
_TILE_LIGHT = "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
_TILE_DARK = "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
_ATTRIB = "© OpenStreetMap contributors · © CARTO"

# 三档 marker（分档由 viewmodel.map_model 定）：0=起降机场 / 1=换路点 / 2=加密中间点
# `accent` 同时用于圆点与药丸边框；`border` = 药丸边框宽度（0 = 不描边，用于最次要的加密点）。
_TIER = {
    0: {"d": 14, "accent": ft.Colors.RED_600, "size": 12, "border": 2},
    1: {"d": 10, "accent": ft.Colors.BLUE_600, "size": 10, "border": 1},
    2: {"d": 6, "accent": ft.Colors.BLUE_GREY_300, "size": 9, "border": 0},
}

_LBL_H = 18                             # 航点名那一行的高度（含药丸的上下留白）


def _label(ident, st, dark):
    """航点名做成【药丸底衬】：底图（尤其深色瓦片）颜色很杂，纯文字无论什么颜色都会被吃掉。
    加不透明底衬 + 加粗 + 边框，深浅底图上都读得清。
    ⚠️ 高度必须恒为 `_LBL_H`（只加左右 padding、不加上下），否则会破坏下面 `_marker` 的居中算法。"""
    return ft.Container(
        height=_LBL_H, padding=ft.Padding.symmetric(horizontal=5, vertical=0),
        alignment=ft.Alignment.CENTER, border_radius=5,
        bgcolor=ft.Colors.with_opacity(0.72, ft.Colors.BLACK) if dark
        else ft.Colors.with_opacity(0.88, ft.Colors.WHITE),
        border=ft.Border.all(st["border"], st["accent"]) if st["border"] else None,
        content=ft.Text(ident, size=st["size"], no_wrap=True, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE if dark else ft.Colors.GREY_900))


def _marker(mk, dark):
    """⚠️ 锚点：`Marker.coordinates` 是【整个 marker 盒子的中心】（当 alignment=CENTER 时）。
    所以盒子里必须让**圆点自己**落在正中，否则圆点会整体偏离航路——
    「圆点在上、名称在下」这样直接排，圆点的中心就跑到盒子中心【之上】，
    实测折线与圆点错开约一个盒子高。做法：上方垫一个与名称等高的占位，
    上下对称 → 圆点中心 = 盒子中心 = 坐标点。
    （别用 alignment=TOP_CENTER“凑”：那是把整个盒子挪到点的【上方】，偏得更多。）"""
    st = _TIER[mk["tier"]]
    dot = ft.Container(width=st["d"], height=st["d"], bgcolor=st["accent"],
                       border_radius=st["d"] / 2,
                       border=ft.Border.all(2, ft.Colors.WHITE if not dark else ft.Colors.BLACK54))
    pill = _label(mk["ident"], st, dark)
    return pill, fmap.Marker(
        content=ft.Column([ft.Container(height=_LBL_H), dot, pill],
                          spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        coordinates=fmap.MapLatitudeLongitude(mk["lat"], mk["lon"]),
        width=96, height=2 * _LBL_H + st["d"], alignment=ft.Alignment.CENTER)


def _leg_marker(leg, dark):
    """一条腿的航路名标注（Y56 / LAXAS4 / DCT）——放在该腿折线的中点，压在线上。
    只有药丸、没有圆点：它标的是【段】不是【点】。药丸不透明，正好把线遮出一个缺口，读起来清楚。"""
    pill = ft.Container(
        height=_LBL_H, padding=ft.Padding.symmetric(horizontal=5, vertical=0),
        alignment=ft.Alignment.CENTER, border_radius=9,
        bgcolor=ft.Colors.with_opacity(0.80, ft.Colors.BLACK) if dark
        else ft.Colors.with_opacity(0.92, ft.Colors.WHITE),
        content=ft.Text(leg["label"], size=9, no_wrap=True, weight=ft.FontWeight.BOLD,
                        # 用琥珀色与航点名（白/近黑）拉开：一眼分得清「这是航路，不是航点」
                        color=ft.Colors.AMBER_400 if dark else ft.Colors.AMBER_900))
    return pill, fmap.Marker(content=pill, width=84, height=_LBL_H,
                             coordinates=fmap.MapLatitudeLongitude(leg["lat"], leg["lon"]),
                             alignment=ft.Alignment.CENTER)


def _map_control(mm, dark, wp_pills, leg_pills):
    """一条航路 → 一个 Map 控件。把两类药丸的引用收进 wp_pills / leg_pills，供开关切换显隐。"""
    layers = [
        fmap.TileLayer(url_template=_TILE_DARK if dark else _TILE_LIGHT,
                       user_agent_package_name=_UA),
    ]
    if mm["polyline"]:
        layers.append(fmap.PolylineLayer(polylines=[fmap.PolylineMarker(
            coordinates=[fmap.MapLatitudeLongitude(a, b) for a, b in mm["polyline"]],
            color=ft.Colors.BLUE_600, border_color=ft.Colors.WHITE, stroke_width=3,
            border_stroke_width=1)]))

    markers = []
    for m in mm["markers"]:                       # 航点：圆点 + 名称药丸
        pill, mk = _marker(m, dark)
        wp_pills.append(pill)
        markers.append(mk)
    for lg in mm.get("legs") or []:                # 航路名：Y56 / SID·STAR 名 / DCT
        pill, mk = _leg_marker(lg, dark)
        leg_pills.append(pill)
        markers.append(mk)
    layers.append(fmap.MarkerLayer(markers=markers))
    layers.append(fmap.RichAttribution(attributions=[fmap.TextSourceAttribution(text=_ATTRIB)]))

    # 中心与缩放由 viewmodel._fit_zoom 算（纯函数、可单测、两套 UI 共用）。
    # 不用 flutter_map 的 CameraFit：它要等控件量到尺寸才准，构造期给不出可靠值。
    kw = {}
    if mm["center"]:
        kw["initial_center"] = fmap.MapLatitudeLongitude(*mm["center"])
        kw["initial_zoom"] = mm["zoom"]
    return fmap.Map(expand=True, layers=layers, **kw)


class MapView:
    """地图视图（pushed ft.View）。多条航路 → 标签页切换。"""

    def __init__(self, page, on_close):
        self.page = page
        self._on_close = on_close
        self.view = None
        self._wp_pills = []                      # 航点名药丸（受「航点名称」开关控制）
        self._leg_pills = []                     # 航路名药丸（受「航路名称」开关控制）

    @property
    def is_dark(self):
        return self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK)

    def set_labels(self, wp=None, leg=None):
        """开关两类标注（None = 不动）。
        ⚠️ 只能改 `opacity`，【不能】用 `visible=False`：
          ① `Marker.content` 不可见时 flet_map 直接抛 ValueError（源码写死）；
          ② 藏掉航点名会让 marker 那一列少一格，圆点就不再落在盒子正中——航点又会漂离航路。
        opacity=0 保留布局，两个问题一起躲开。"""
        for on, sw, pills in ((wp, getattr(self, "sw_wp", None), self._wp_pills),
                              (leg, getattr(self, "sw_leg", None), self._leg_pills)):
            if on is None:
                continue
            if sw is not None:
                sw.value = bool(on)
            for p in pills:
                p.opacity = 1 if on else 0
        self.page.update()

    def build(self, routes, index=0):
        """routes=[(coords, title)]；index=默认选中的那条。"""
        dark = self.is_dark
        models = [(VM.map_model(c, t), t) for c, t in routes]
        models = [(mm, t) for mm, t in models if mm]
        if not models:
            return None
        index = max(0, min(index, len(models) - 1))

        self._wp_pills, self._leg_pills = [], []
        pages = [ft.Column([
            ft.Text(t or "", size=11, color=ft.Colors.GREY, no_wrap=False, selectable=True),
            _map_control(mm, dark, self._wp_pills, self._leg_pills),
        ], spacing=4, expand=True) for mm, t in models]

        # ⚠️ Switch 的字段是 `label_text_style`（Checkbox 那边叫 `label_style`——Flet 这俩不统一）
        st = ft.TextStyle(size=12)
        self.sw_wp = ft.Switch(label="航点名称", value=True, label_text_style=st,
                               on_change=lambda e: self.set_labels(wp=e.control.value))
        self.sw_leg = ft.Switch(label="航路名称", value=True, label_text_style=st,
                                on_change=lambda e: self.set_labels(leg=e.control.value))

        body = (pages[0] if len(pages) == 1 else ft.Tabs(
            length=len(pages), expand=True, selected_index=index,
            content=ft.Column([
                # 标签页标签 = 「[序号] 首个航点」——光看 RJTT→RJCC 四条全一样，分不清哪条
                ft.TabBar(tabs=[ft.Tab(label=VM.map_tab_label(i, t), tooltip=t)
                                for i, (_mm, t) in enumerate(models)], scrollable=True),
                ft.TabBarView(expand=True, controls=pages),
            ], expand=True, spacing=0)))

        self.view = ft.View(
            route="/map",
            padding=10,
            bgcolor=ft.Colors.SURFACE,           # 显式不透明，别指望 View 的默认底色
            appbar=ft.AppBar(
                leading=ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回", on_click=lambda e: self._on_close()),
                title=ft.Text("🗺️ 航路地图" + ("（%d 条航路，可切换标签页比对）" % len(models) if len(models) > 1 else "")),
                # 航点密集时（加密中间点很多）标注会显得挤 → 两类标注各给一个开关，都默认开
                actions=[
                    self.sw_wp, self.sw_leg,
                    ft.Container(width=10),
                ],
            ),
            controls=[body],
        )
        return self.view

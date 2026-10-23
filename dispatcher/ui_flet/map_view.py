# ================= 航路地图（flet_map）=================
# 吃 viewmodel.map_model()（分段折线 / 符号 / 标签 / 各图元从第几级缩放起显示，全是纯数据），这里只负责翻译成
# flet_map 控件，并在缩放跨过半级时按它显隐。样式仿 Navigraph Charts（紫色航路线、深蓝底白字的航点名、
# SID 粉红、STAR 绿、航路名沿线写）。Pillow 与 tkintermapview 双双出局：marker 可以直接用任意 Flet 控件画。
#
# ⚠️ 底图图源可选（窗口右上角「底图」下拉，见 BASEMAPS；选择记在运行目录 map_settings.json，下次开地图沿用）。
#    默认【国土地理院「淡色地図」】：免 key、免申请，只需署名；没有深色版，要暗色底图选「Esri 深灰」。
#    踩过的坑：
#    · CARTO（basemaps.cartocdn.com，v2.0.0–v2.0.1 用的 voyager / dark_all）：2026-09 起必须带 API key，
#      不带的请求不论哪一级缩放都只回一张「API KEY REQUIRED」水印图；key 又不能写进公开仓库和发布包。
#    · TileLayer 没有 headers 字段，只能经 user_agent_package_name 定 UA（实发 `flutter_map (<它>)`）。
#      v2.0.0 时 tile.openstreetmap.org 曾 403 拒绝 flutter_map 的 UA；2026-10 复测已放行，OSM 的瓦片政策要求
#      UA 写明程序名（_UA 带了项目网址）、署名始终可见、不批量预取 / 离线——本程序都满足。
#
# 地图画在【独立的子进程窗口】里（入口与进程管理见 map_window.py），不在主窗口里：
#    核显机器上浏览地图会把 flet.exe 的 GPU 渲染缓存撑大约 +400 MB 且不归还，独立进程关窗即还清。
#    一次规划的 N 条航路做成同一窗口里的标签页；每点一次开一个新窗口，可并排比对。

import math
import webbrowser

import flet as ft
import flet_map as fmap

from .. import viewmodel as VM

_UA = "JapanFlightDispatcher (+https://github.com/kevin-git-2026/japan-flight-dispatcher)"

# 底图图源。都实测过：用本程序的 UA 取得到真实瓦片；max = 实测有数据的最高级（再放大由 flutter_map 拉伸
# 这一级的瓦片，而不是显示服务器的「暂无数据」占位图）。署名一律【始终显示、完整显示】——OSM 与 Esri 的条款
# 都不许把署名藏在按钮或开关后面。所以不用 flet_map 的署名层：RichAttribution 过几秒就收成一个图标；
# SimpleAttribution 强制加「flutter_map | ©」前缀（OSM 那条会变成「© ©」），长文字还会被截断（Esri 地形图那条
# 实测截掉了尾巴）→ 自己画一条叠在地图右下角、可换行的文字条（见 MapView._attrib_overlay），点击打开出处页。
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/%s/MapServer/tile/{z}/{y}/{x}"   # 注意是 {y}/{x}
BASEMAPS = {
    # 地理院タイル一览页的要求：写「地理院タイル」并链接到一览页；淡色地図 5–8 级另须注明海岸线数据来源。
    # 覆盖 5–18 级日本及周边、2–8 级全球；地名是日文。（标准地图 5–8 级还要另加 GEBCO 与海上保安厅许可号两条署名，没收。）
    "gsi_pale": {
        "label": "地理院 淡色地图",
        "url": "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png", "max": 18,
        "attrib": ('出典：地理院タイル（国土地理院）｜Shoreline data is derived from: United States. '
                   'National Imagery and Mapping Agency. "Vector Map Level 0 (VMAP0)."'),
        "link": "https://maps.gsi.go.jp/development/ichiran.html"},
    # OSM 官方瓦片：免费但属尽力而为的公益服务，滥用会被封；本程序只按用户浏览实时取，不预取。
    "osm": {
        "label": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "max": 19,
        "attrib": "© OpenStreetMap contributors",
        "link": "https://www.openstreetmap.org/copyright"},
    # Esri：免 key 的旧版公开服务（server.arcgisonline.com，已不再更新）；按 Esri 条款限非商业用途、须署名 Esri，
    # 将来可能收紧。数据署名取自各服务元数据的 copyrightText（`…/MapServer?f=json`，2026-10-23）。
    "esri_topo": {
        "label": "Esri 地形图",
        "url": _ESRI % "World_Topo_Map", "max": 19,
        "attrib": ("Powered by Esri｜Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, "
                   "NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), "
                   "(c) OpenStreetMap contributors, and the GIS User Community"),
        "link": "https://www.esri.com"},
    "esri_imagery": {
        "label": "Esri 卫星影像",
        "url": _ESRI % "World_Imagery", "max": 19,
        "attrib": "Powered by Esri｜Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community",
        "link": "https://www.esri.com"},
    "esri_darkgray": {
        "label": "Esri 深灰（暗色）",
        "url": _ESRI % "Canvas/World_Dark_Gray_Base", "max": 16,
        "attrib": "Powered by Esri｜Esri, HERE, Garmin, (c) OpenStreetMap contributors, and the GIS user community",
        "link": "https://www.esri.com"},
}
DEFAULT_BASEMAP = "gsi_pale"


def basemap_key(key):
    """不认识的（含 map_settings.json 里的旧值 / 空值）一律回到默认。"""
    return key if key in BASEMAPS else DEFAULT_BASEMAP


def _tile_layer(key):
    b = BASEMAPS[key]
    return fmap.TileLayer(url_template=b["url"], max_native_zoom=b["max"], user_agent_package_name=_UA)

# 配色取自 Navigraph Charts 的航路图（截图取样）：enroute 紫、SID 粉红、STAR 绿、航点深蓝、机场青，标签一律白字。
# 深浅主题、各种底图都用这一套——都是中等明度的饱和色，浅色底图、深色底图、卫星影像上都压得住。
_LINE = {"enr": ("#9461B8", "#4C2A6E", "#9461B8"),     # 航段 (线芯, 描边, 沿线航路名的底条)
         "sid": ("#CF5F86", "#76284A", "#CF5F86"),
         "star": ("#92BD75", "#56773F", "#5F8246")}    # STAR 线芯浅绿，白字压不住 → 名字底条用深一档的绿
_LINE_W, _LINE_EDGE = 7, 3                             # 线芯宽；描边宽（flutter_map 把它画在线芯底下，两侧各露一半）
_STYLE = {"wpt": ("#2E578D", "#16304F", "#2E578D"),     # 标签与符号 (标签底色, 边框, 符号底色)
          "sid": ("#C2386C", "#6E1C3E", "#C2386C"),
          "star": ("#5F8246", "#3D5A2A", "#92BD75"),     # STAR 的点同 Navigraph：浅绿圆点、深绿标签
          "apt": ("#337180", "#163F48", "#337180")}
_GLYPH = {"apt": ft.Icons.SETTINGS,              # 机场：带齿的圆（航图上的机场符号）
          "VOR": ft.Icons.HEXAGON_OUTLINED,      # VOR：六边形
          "NDB": ft.Icons.BLUR_CIRCULAR,         # NDB：点画圆
          "fix": ft.Icons.CHANGE_HISTORY}        # 航点：三角。dot（程序里的点）不画字形，只一个圆点
_ALIGN = {"up": ft.Alignment.TOP_CENTER, "down": ft.Alignment.BOTTOM_CENTER,
          "right": ft.Alignment.CENTER_RIGHT, "left": ft.Alignment.CENTER_LEFT}


def _at(it):
    return fmap.MapLatitudeLongitude(it["lat"], it["lon"])


def _text(s, size):
    return ft.Text(s, size=size, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE, no_wrap=True,
                   style=ft.TextStyle(letter_spacing=VM.MAP_LETTER_SP))


def _symbol(mk):
    """航点符号：半透明的实心圆（透出底下的航路线）+ 白色字形，圆心就在坐标点上。"""
    d = VM.map_sym_d(mk["sym"])
    _bg, edge, fill = _STYLE[mk["style"]]
    glyph = _GLYPH.get(mk["sym"])
    return fmap.Marker(
        coordinates=_at(mk), width=d, height=d,
        content=ft.Container(
            width=d, height=d, border_radius=d / 2, alignment=ft.Alignment.CENTER,
            bgcolor=ft.Colors.with_opacity(0.8, fill), border=ft.Border.all(1.5, edge),
            content=ft.Icon(glyph, size=round(d * 0.6), color=ft.Colors.WHITE) if glyph else None))


def _label(mk):
    """航点名：深蓝底白字的方框（机场青底；SID 里的点粉红底、STAR 里的点绿底），贴在符号的 mk['side'] 那一侧。
    ⚠️ 锚点：Marker.alignment=TOP_CENTER 是把【整个盒子】放到点的上方（点落在盒子底边中点）——正好拿来挂标签；
    盒子里先垫「符号半径 + 间隙」再放框，框就贴着符号外沿。框宽用 viewmodel 估的宽度（避让按同一个数算）。"""
    w, h = VM.map_label_w(mk["label"]), VM.MAP_LBL_H
    fill, edge, _sym = _STYLE[mk["style"]]
    box = ft.Container(width=w, height=h, bgcolor=fill, border=ft.Border.all(1, edge), border_radius=2,
                       alignment=ft.Alignment.CENTER, content=_text(mk["label"], VM.MAP_LBL_FONT))
    pad = VM.map_sym_d(mk["sym"]) / 2 + VM.MAP_LBL_GAP
    side = mk["side"]
    if side in ("up", "down"):
        gap = ft.Container(height=pad)
        content, size = ft.Column([box, gap] if side == "up" else [gap, box], spacing=0), (w, h + pad)
    else:
        gap = ft.Container(width=pad)
        content, size = ft.Row([gap, box] if side == "right" else [box, gap], spacing=0), (w + pad, h)
    return fmap.Marker(coordinates=_at(mk), content=content, width=size[0], height=size[1],
                       alignment=_ALIGN[side])


def _leg_label(lg):
    """航路名（Y56 / MINAC4 / DCT）沿线写在航路线上：与该段同色系的圆角条压在线上、白字，按该段走向旋转。"""
    w, h = VM.map_label_w(lg["label"], VM.MAP_LEG_FONT), VM.MAP_LEG_H
    return fmap.Marker(
        coordinates=_at(lg), width=w, height=h,
        content=ft.Container(
            width=w, height=h, bgcolor=_LINE[lg["seg"]][2], border_radius=h / 2, alignment=ft.Alignment.CENTER,
            rotate=ft.Rotate(angle=math.radians(lg["angle"])), content=_text(lg["label"], VM.MAP_LEG_FONT)))


def _map_control(mm, basemap):
    """一条航路 → (Map 控件, [(marker, min_zoom, 类别)])；后者是随缩放 / 开关显隐的图元，类别 sym=符号 / wp=航点名 / leg=航路名。
    图层顺序约定：layers[0] = 瓦片层（set_basemap 按这个位置整层替换）；署名不在图层里，见 _attrib_overlay。"""
    layers = [_tile_layer(basemap)]
    if mm["lines"]:
        layers.append(fmap.PolylineLayer(polylines=[fmap.PolylineMarker(
            coordinates=[fmap.MapLatitudeLongitude(a, b) for a, b in ln["points"]],
            color=_LINE[ln["seg"]][0], border_color=_LINE[ln["seg"]][1],
            stroke_width=_LINE_W, border_stroke_width=_LINE_EDGE) for ln in mm["lines"]]))

    by_rank = sorted(mm["markers"], key=lambda m: -m["tier"])       # 重要的后画、压在上面
    dyn = ([(_symbol(m), m["sym_z"], "sym") for m in by_rank]
           + [(_leg_label(lg), lg["z"], "leg") for lg in mm["legs"]]
           + [(_label(m), m["lbl_z"], "wp") for m in by_rank])
    layers.append(fmap.MarkerLayer(markers=[mk for mk, _z, _k in dyn]))

    # 中心与缩放由 viewmodel._fit_zoom 算（纯函数、可单测、两套 UI 共用）。
    # 不用 flutter_map 的 CameraFit：它要等控件量到尺寸才准，构造期给不出可靠值。
    kw = {}
    if mm["center"]:
        kw["initial_center"] = fmap.MapLatitudeLongitude(*mm["center"])
        kw["initial_zoom"] = mm["zoom"]
    return fmap.Map(expand=True, layers=layers, **kw), dyn


class MapView:
    """地图视图（地图子窗口的 ft.View）。多条航路 → 标签页切换。"""

    def __init__(self, page, basemap=None, on_basemap=None):
        self.page = page
        self.view = None
        self.maps = []                           # 各标签页的 Map 控件（与 routes 同序；测试漫游用）
        self.tabs = None                         # 多条航路时的 ft.Tabs，单条为 None
        self._dyn = []                           # 各标签页随缩放 / 开关显隐的图元 [(marker, min_zoom, 类别)]
        self._zoom = []                          # 各标签页当前缩放（按半级取整，与 viewmodel 的避让分级一致）
        self._show = {"wp": True, "leg": True}   # 「航点名称」「航路名称」两个开关
        self.basemap = basemap_key(basemap)      # 当前底图（BASEMAPS 的键）
        self._on_basemap = on_basemap            # 换底图后回调（地图窗口用它把选择记进 map_settings.json）
        self._attrib_texts = []                  # 各标签页署名条里的 Text（换底图时改字）

    def _attrib_overlay(self):
        """叠在地图右下角的署名条：始终显示、文字完整（太长就换行，不截断）、点击打开当前图源的出处页。
        外层占满底边（left=right=0），里面的盒子 expand_loose（= Flutter 的 Flexible loose）——
        短文字只占自身宽度，长文字最多占满整条底边再换行。"""
        text = ft.Text(BASEMAPS[self.basemap]["attrib"], size=10, color=ft.Colors.ON_SURFACE,
                       text_align=ft.TextAlign.RIGHT)
        self._attrib_texts.append(text)
        box = ft.Container(content=text, bgcolor=ft.Colors.with_opacity(0.78, ft.Colors.SURFACE),
                           padding=ft.Padding.symmetric(horizontal=6, vertical=2), border_radius=4,
                           tooltip="点击查看图源出处", expand=True, expand_loose=True,
                           on_click=lambda e: webbrowser.open(BASEMAPS[self.basemap]["link"]))
        return ft.Container(left=0, right=0, bottom=0, padding=4,
                            content=ft.Row([box], alignment=ft.MainAxisAlignment.END))

    def set_basemap(self, key):
        """换底图：每个标签页的 Map 都把瓦片层【换成新控件】——整层替换，flutter_map 才会按新地址
        重新取瓦片（只改 url_template 不保证重载）。没挂到界面上的标签页下次切过去时直接用新图层。"""
        key = basemap_key(key)
        if key == self.basemap:
            return
        self.basemap = key
        for m in self.maps:
            m.layers[0] = _tile_layer(key)
        for t in self._attrib_texts:
            t.value = BASEMAPS[key]["attrib"]
        if getattr(self, "dd_basemap", None) is not None:
            self.dd_basemap.value = key
        self.page.update()
        if self._on_basemap:
            self._on_basemap(key)

    def _refresh(self, i):
        """按第 i 页的当前缩放 + 两个开关重设各图元显隐；有变化返回 True。
        用 Marker.visible 而不是 content.visible——后者一隐，flet_map 就抛 ValueError（源码写死）。"""
        changed = False
        for mk, z, kind in self._dyn[i]:
            on = self._zoom[i] >= z and (kind == "sym" or self._show[kind])
            if mk.visible != on:
                mk.visible = on
                changed = True
        return changed

    async def _on_move(self, e):
        """平移 / 缩放时每帧都来：只在缩放跨过半级时才重设显隐，只更新这一张图。
        ⚠️ 先关掉本次事件的自动更新：Flet 默认每个事件处理完都把整页 diff 一遍——9 个标签页的地图窗口
        实测一次 19 ms，平移时每帧一次能吃满一个核；只更新这一张图约 1.3 ms，且只在跨级时才做。"""
        ft.context.disable_auto_update()
        i = e.control.data
        z = math.floor(e.camera.zoom * 2) / 2
        if z != self._zoom[i]:
            self._zoom[i] = z
            if self._refresh(i):
                e.control.update()

    def set_labels(self, wp=None, leg=None):
        """开关两类标注（None = 不动）；开着时仍按缩放避让显隐。"""
        for key, on, sw in (("wp", wp, getattr(self, "sw_wp", None)), ("leg", leg, getattr(self, "sw_leg", None))):
            if on is None:
                continue
            self._show[key] = bool(on)
            if sw is not None:
                sw.value = bool(on)
        for i in range(len(self.maps)):
            self._refresh(i)
        self.page.update()

    def build(self, routes, index=0):
        """routes=[(coords, title[, kinds])]（kinds 见 viewmodel.nav_kinds）；index=默认选中的那条。"""
        models = [(VM.map_model(r[0], r[1], r[2] if len(r) > 2 else None), r[1]) for r in routes]
        models = [(mm, t) for mm, t in models if mm]
        if not models:
            return None
        index = max(0, min(index, len(models) - 1))

        self._attrib_texts = []
        built = [_map_control(mm, self.basemap) for mm, _t in models]
        self.maps = [m for m, _d in built]
        self._dyn = [d for _m, d in built]
        self._zoom = [math.floor(mm["zoom"] * 2) / 2 for mm, _t in models]
        for i, m in enumerate(self.maps):
            m.data = i
            m.on_position_change = self._on_move
            self._refresh(i)
        pages = [ft.Column([
            ft.Text(t or "", size=11, color=ft.Colors.GREY, no_wrap=False, selectable=True),
            ft.Stack([m, self._attrib_overlay()], expand=True),
        ], spacing=4, expand=True) for m, (_mm, t) in zip(self.maps, models)]

        # ⚠️ Switch 的字段是 `label_text_style`（Checkbox 那边叫 `label_style`——Flet 这俩不统一）
        st = ft.TextStyle(size=12)
        self.sw_wp = ft.Switch(label="航点名称", value=True, label_text_style=st,
                               on_change=lambda e: self.set_labels(wp=e.control.value))
        self.sw_leg = ft.Switch(label="航路名称", value=True, label_text_style=st,
                                on_change=lambda e: self.set_labels(leg=e.control.value))
        self.dd_basemap = ft.Dropdown(
            label="底图", value=self.basemap, width=190, dense=True, text_size=12, label_style=st,
            options=[ft.DropdownOption(key=k, text=b["label"]) for k, b in BASEMAPS.items()],
            on_select=lambda e: self.set_basemap(e.control.value))

        self.tabs = None if len(pages) == 1 else ft.Tabs(
            length=len(pages), expand=True, selected_index=index,
            content=ft.Column([
                # 标签页标签 = 「[序号] 首个航点」——光看 RJTT→RJCC 四条全一样，分不清哪条
                ft.TabBar(tabs=[ft.Tab(label=VM.map_tab_label(i, t), tooltip=t)
                                for i, (_mm, t) in enumerate(models)], scrollable=True),
                ft.TabBarView(expand=True, controls=pages),
            ], expand=True, spacing=0))
        body = pages[0] if self.tabs is None else self.tabs

        self.view = ft.View(
            route="/",
            padding=10,
            bgcolor=ft.Colors.SURFACE,           # 显式不透明，别指望 View 的默认底色
            appbar=ft.AppBar(
                automatically_imply_leading=False,   # 独立窗口：用窗口自己的关闭按钮，不要返回箭头
                title=ft.Text("🗺️ 航路地图" + ("（%d 条航路，可切换标签页比对）" % len(models) if len(models) > 1 else "")),
                # 航点密集时（加密中间点很多）标注会显得挤 → 两类标注各给一个开关，都默认开
                actions=[
                    self.dd_basemap, ft.Container(width=12),
                    self.sw_wp, self.sw_leg,
                    ft.Container(width=10),
                ],
            ),
            controls=[body],
        )
        return self.view

# ================= 左侧输入表单 =================
# 只读控件、只写控件。取值一律交 viewmodel（机型解析走 VM.AircraftModel）。
# 用 Material 的浮动标签 TextField，省掉 tk 版那一整列独立 Label → 左栏更紧凑。

import flet as ft

from .theme import panel


class FormView:
    def __init__(self, aircraft_model, on_plan, on_ops_editor, on_volanta, on_auto_toggle):
        self.ac = aircraft_model
        self._on_plan = on_plan

        _LS = ft.TextStyle(size=12)                    # 标签统一小一号，左栏不挤

        def tf(label, width=None, hint=None, tip=None):
            return ft.TextField(label=label, width=width, hint_text=hint, tooltip=tip,
                                dense=True, text_size=13, content_padding=10, label_style=_LS)

        def cb(label, tip=None):                       # Checkbox 标签不换行 → 长文案进 tooltip
            return ft.Checkbox(label=label, value=False, tooltip=tip, label_style=_LS)

        self.dep = tf("出发 ICAO", tip="留空=随机")
        self.dest = tf("目的 ICAO", tip="留空=随机")
        self.airline = tf("执飞航司 ICAO", tip="留空=不限")

        # 机型：可搜索 + 可自由输入（0.85 的 Dropdown 有独立 text 字段存键入原文）
        self.aircraft = ft.Dropdown(
            label="机型", editable=True, enable_filter=True, label_style=_LS,
            text_size=13, dense=True, tooltip="可搜索 ICAO / 名称 / 厂商；也可直接键入",
            options=[ft.DropdownOption(key=lbl, text=lbl) for lbl in self.ac.labels])

        self.time = tf("时间区间", hint="08:00-15:30")
        self.runway = tf("最短跑道", hint="1800m / 5900ft")
        self.dmin = tf("最短 NM", width=104)
        self.dmax = tf("最长 NM", width=104)

        self.strict = cb("严格要求 AIP 规定航路", "只用官方 AIP 航路，查不到就报错（不本地生成）")
        self.strict_ops = cb("严格遵循现实运行规则",
                             "该航线有多条 AIP 航路时，按 EOBT / 机型 / 巡航高度自动定唯一")

        # 问题1：本次飞行使用的模拟器——地景判定/标注/「仅地景」筛选都按此
        self.sim = ft.RadioGroup(value="XP", content=ft.Row([
            ft.Radio(value="XP", label="X-Plane"), ft.Radio(value="MSFS", label="MSFS")], spacing=0))
        self.scenery_only = cb("仅两端都有地景的机场", "两端都装了该模拟器的机场地景才纳入随机")
        self.scenery_hint = ft.Text("", size=11, color=ft.Colors.AMBER_800)

        self.btn_volanta = ft.OutlinedButton("同步 Volanta", on_click=lambda e: on_volanta())
        self.chk_auto = ft.Checkbox(label="自动同步", value=False,
                                    on_change=lambda e: on_auto_toggle(bool(self.chk_auto.value)))
        self.vstatus = ft.Text("Volanta：—", size=11, color=ft.Colors.GREY)

        self.btn_plan = ft.FilledButton("🛫 规划航线", width=10_000, height=44,
                                        on_click=lambda e: self._on_plan())
        self.btn_ops = ft.OutlinedButton("⚙️ 编辑机场运行规则", width=10_000, height=38,
                                         on_click=lambda e: on_ops_editor())

        def sec(title):
            return ft.Text(title, size=11, color=ft.Colors.GREY)

        self.control = panel(width=350, content=ft.Column([
                sec("规划输入"),
                self.dep, self.dest, self.airline,
                ft.Divider(height=14),
                sec("高级筛选（可留空）"),
                self.aircraft, self.time, self.runway,
                ft.Row([self.dmin, ft.Text("—"), self.dmax], spacing=6),
                self.strict, self.strict_ops,
                ft.Divider(height=14),
                sec("本次飞行使用的模拟器"),
                self.sim, self.scenery_only, self.scenery_hint,
                ft.Divider(height=14),
                ft.Row([self.btn_volanta, self.chk_auto], spacing=8),
                self.vstatus,
                ft.Divider(height=14),
                self.btn_plan, self.btn_ops,
        ], spacing=8, scroll=ft.ScrollMode.AUTO, tight=True))

    # ---- 读控件 ----
    def values(self, has_scenery):
        raw_ac = (self.aircraft.text or self.aircraft.value or "")   # 选中项或自由键入的原文
        return {
            "dep": (self.dep.value or "").strip().upper(),
            "dest": (self.dest.value or "").strip().upper(),
            "airline": (self.airline.value or "").strip().upper(),
            "aircraft": self.ac.resolve(raw_ac),
            "time": (self.time.value or "").strip(),
            "runway": self.runway.value or "",
            "dmin": self.dmin.value or "",
            "dmax": self.dmax.value or "",
            "strict": bool(self.strict.value),
            "strict_ops": bool(self.strict_ops.value),
            "scenery_only": bool(self.scenery_only.value) and has_scenery,
            "sim": self.sim.value,
        }

    # ---- 写控件 ----
    def apply_enabled(self, en):
        for c in (self.dep, self.dest, self.airline, self.aircraft, self.time, self.runway,
                  self.dmin, self.dmax, self.strict, self.strict_ops, self.chk_auto):
            c.disabled = not en["form"]
        self.btn_plan.disabled = not en["plan"]
        self.btn_ops.disabled = not en["ops_editor"]
        self.scenery_only.disabled = not en["scenery_only"]
        self.btn_volanta.disabled = not en["volanta"]
        self.btn_volanta.content = ft.Text("取消同步" if en["volanta_cancel"] else "同步 Volanta")

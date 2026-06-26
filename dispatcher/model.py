# ================= 核心数据模型 =================
# Airport：机场对象（ICAO + 十进制经纬度 + 地景来源 + 军用标记）。


class Airport:
    def __init__(self, code, lat_str, lon_str, scenery_sources=None, is_military=False):
        self.code = code
        self.lat_dd = float(lat_str)
        self.lon_dd = float(lon_str)
        # scenery_sources: None=未做地景检测(不标记无地景); set()=检测了但无地景; {'XP','MSFS'}=有地景
        self.scenery_sources = scenery_sources
        self.is_military = is_military

    @property
    def has_scenery(self):
        # 未检测(None)视为 True(软降级，不报「无地景」)；否则按是否有来源
        return True if self.scenery_sources is None else bool(self.scenery_sources)

    def scenery_label(self):
        """渲染地景标注：未检测→空；无地景→[⚠️无地景]；有地景→[地景:XP/MSFS/XP+MSFS]。"""
        if self.scenery_sources is None:
            return ""
        if not self.scenery_sources:
            return " [⚠️无地景]"
        order = [s for s in ("XP", "MSFS") if s in self.scenery_sources]
        return " [地景:" + "+".join(order) + "]"

    def __repr__(self):
        return f"[{self.code}]"

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

    def has_scenery_for(self, active_sims=None):
        """指定模拟器下是否有地景：active_sims=None→任一(XP/MSFS)；{'XP'}→只看 XP、{'MSFS'}→只看 MSFS。
        未检测(scenery_sources is None)恒 True(软降级，不报「无地景」)。供「仅地景」筛选与渲染按所用模拟器判定。"""
        if self.scenery_sources is None:
            return True
        return bool(self.scenery_sources & set(active_sims or {"XP", "MSFS"}))

    @property
    def has_scenery(self):
        # 不分模拟器(任一有地景即可)；等价于 has_scenery_for(None)，保留以兼容旧调用
        return self.has_scenery_for(None)

    def scenery_label(self, active_sims=None):
        """渲染地景标注(按所用模拟器)：未检测→空；所选模拟器下无地景→[⚠️无XP地景]/[⚠️无地景]；
        有→[地景:XP/MSFS/XP+MSFS]（只列所选模拟器里有的来源）。"""
        if self.scenery_sources is None:
            return ""
        active = set(active_sims or {"XP", "MSFS"})
        visible = [s for s in ("XP", "MSFS") if s in self.scenery_sources and s in active]
        if visible:
            return " [地景:" + "+".join(visible) + "]"
        if active_sims and active != {"XP", "MSFS"}:
            return " [⚠️无" + "+".join(s for s in ("XP", "MSFS") if s in active) + "地景]"
        return " [⚠️无地景]"

    def __repr__(self):
        return f"[{self.code}]"

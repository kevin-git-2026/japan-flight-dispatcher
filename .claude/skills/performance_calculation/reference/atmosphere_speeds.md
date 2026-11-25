# 大气与速度（atmosphere_speeds）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：ICAO 标准大气的常用公式；Airbus《Getting to Grips with Aircraft Performance》（2002）第 A 章；OpenAP 2.6.2 `openap/aero.py`（本机 venv 实测，2026-11-25）。
> 本文件是全 skill 单位换算的唯一出处，其他文件不再重复。

## 一、ISA 标准大气

| 层 | 温度 | 气压 |
| --- | --- | --- |
| 对流层（0–11 km，0–36,089 ft） | T = 288.15 K − 6.5 K/km × h | p = 1013.25 hPa × (T / 288.15)^5.2559 |
| 平流层下层（11–20 km） | T = 216.65 K（等温） | p = 226.32 hPa × exp(−(h − 11 km) / 6.3416 km) |

- 音速 a = 38.9679 × √T（kt，T 单位 K）；海平面 661.48 kt，11 km 以上 573.6 kt。
- **气压高度**：高度表拨 1013.25 hPa 时的读数，**飞行高度层（FL）就是气压高度 / 100 ft**。性能表、FMS 与本文件的公式都以气压高度为准。
- **ISA 偏差 dT**：实际温度与 ISA 之差。同一气压高度上 dT 越高，音速越大，同 Mach 的 TAS 越大；发动机推力越小，最大高度降低（Airbus Safety First #21：REC MAX 随外界温度升高而降低）。

## 二、速度

| 速度 | 含义 | 何时用 |
| --- | --- | --- |
| IAS / CAS | 指示 / 校准空速（CAS = 修正了仪表与位置误差的 IAS） | FL100 以下的限速（250 kt）、爬升 / 下降的 CAS 段 |
| EAS | 当量空速，反映动压 | 气动计算 |
| TAS | 真空速，相对空气的速度 | 积分航程时间、比航程 |
| Mach | TAS / 当地音速 | 高空爬升 / 巡航 / 下降 |
| GS | 地速 = TAS 与风的合成 | 航程时间、地面比航程 |

换算（可压缩流，ISA）：

```
qc   = p0 · [(1 + 0.2 · (CAS / a0)²)^3.5 − 1]          # 冲压
Mach = √(5 · [(qc / p + 1)^(2/7) − 1])
TAS  = Mach · a(T)
```

- **转换高度（crossover altitude）**：给定 CAS 与 Mach 相等的高度。爬升时先保持 CAS，过转换高度后保持 Mach；下降反过来。
  - 例：290 kt / M0.78 的转换高度为 30,875 ft（[validation.md](validation.md) E04）。
- **风三角**：地速 = √(TAS² − 侧风²) + 顺风分量（顺风为正）。本项目 `eta._gs` 即此式，侧风分量限在 TAS 的 0.9 倍以内。

## 三、单位

| 量 | 换算 |
| --- | --- |
| 速度 | 1 kt = 0.514444 m/s；1 fpm = 0.00508 m/s |
| 长度 | 1 ft = 0.3048 m；1 NM = 1852 m |
| 质量 | 1 lb = 0.45359237 kg |
| 燃油密度 | Jet A 约 0.80 kg/L（opentop 换算用 0.82 kg/L） |
| 油耗率 | kg/h、kg/min；Airbus FCOM 巡航表给每台发动机 kg/h |
| 比航程 | NM/kg，Airbus 表写作 NM/1000 kg |
| 成本指数 | Airbus kg/min；Boeing 100 lb/h，1（100 lb/h）= 0.756 kg/min，1 kg/min = 1.323（100 lb/h） |

## 四、OpenAP 与本项目的对应

- **单位**：OpenAP 的 `aero` 函数用 SI（高度 m、速度 m/s），`Thrust` / `Drag` / `FuelFlow` 却用 kt、ft、fpm、kg。混用单位是最常见的坑。
- **dT 处理**：OpenAP 的 `dT` 保持海平面**密度** 1.225 不变，于是海平面气压随 dT 变化（dT = +10 K 时为 104,841 Pa）。所以 dT ≠ 0 时，它的 `alt` 不再是严格的气压高度。另外 dT 被截在 −25…+15 K 之间。（本机实测）
- **与本项目的一致性**：本项目 `dispatcher/eta.py` 的 `isa`、`hpa_to_ft`、`tas_from_mach`、`tas_from_cas` 与 OpenAP 在 0–41,000 ft 的最大偏差为 0.028%（[validation.md](validation.md) E12）。本项目只用 ISA，不考虑 dT。

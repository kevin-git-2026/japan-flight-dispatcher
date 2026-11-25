# OpenAP 核心库（openap）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：OpenAP 2.6.2 源码与数据（本机 venv 实测，2026-11-25）；PyPI；论文见文末。
> 函数签名以 2.6.2 代码为准。官方手册（openap.dev）有部分内容仍是 1.x 写法，如 `at_thrust(acthr=…, alt=…)`。

## 一、版本与许可

| 项 | 情况 |
| --- | --- |
| 上游仓库 | `github.com/junzis/openap`（2.6.2，与 PyPI 一致）。`github.com/TUDelft-CNS-ATM/openap` 停在 2.5.0，物理模型与数据相同 |
| 代码许可 | LGPL-3.0 |
| 数据许可 | `openap/data/LICENSE` 是 **GPL v3**（机型、发动机、阻力极曲线、WRAP、油耗系数）；自带的导航数据是 2013 年的 X-Plane 数据（GPL v2+） |
| Python | ≥ 3.11；依赖 numpy、pandas 等 |
| 本项目的用法 | 只在开发用的独立 venv 里调用，做对照。`dispatcher/` 不导入，exe 不打包，代码与数据都不复制进仓库 |

**安装**：不要装进打包 exe 用的全局 Python。

```
python -m venv <venv>
<venv>\Scripts\python -m pip install --only-binary=:all: -r .claude/skills/performance_calculation/scripts/requirements-lock.txt
```

版本清单 [requirements-lock.txt](../scripts/requirements-lock.txt) 是实测时的精确版本，含只给 T4 用的 openpyxl。

## 二、API（单位：kt、ft、ft/min、kg；`aero` 用 SI）

编号对应对照脚本 `env` 的检查项（[../scripts/openap_check.py](../scripts/openap_check.py)）。

| 编号 | 调用 | 返回 / 说明 |
| --- | --- | --- |
| E02 | `prop.aircraft(ac, use_synonym=False)` | dict：`mtow` / `mlw` / `oew` / `mfc`（kg）、`mmo`、`vmo`、`ceiling`（**m**）、`limits`、`cruise`（`height` m、`mach`）、`engine`（`default`、`number`、`options`）、`wing`（`area` m²）等 |
| E03 | `prop.engine(name)` | dict：`max_thrust`（N，单台）；ICAO 排放数据库的 `ff_to` / `ff_co` / `ff_app` / `ff_idl`（kg/s，单台）等。按前缀匹配，取第一行 |
| E04 | `aero.mach2tas(M, h_m, dT)`、`cas2tas(v_ms, h_m, dT)`、`crossover_alt(v_cas_ms, M)`、`h_isa(p_Pa)` 等 | SI 输入输出 |
| E05 | `Thrust(ac, eng=None).cruise(tas, alt, dT=0)`；`.climb(tas, alt, roc, dT=0)`；`.takeoff(...)`；`.descent_idle(...)` | N，全机。`cruise()` 是**最大可用**巡航推力（等于 `climb(roc=0)`），不是所需推力 |
| E06 | `Drag(ac, wave_drag=False).clean(mass, tas, alt, vs=0, dT=0)`；`.nonclean(..., flap_angle, landing_gear)` | N。**波阻默认关**，开了会打印「experimental」提示 |
| E07 | `FuelFlow(ac, eng=None, **kw).enroute(mass, tas, alt, vs=0, acc=0, dT=0)` | **kg/s，全机**。内部按 T = D + m·g·sin γ + m·a 求所需推力再换算；`limit` 参数未使用；`wave_drag=True` 会转给内部的 `Drag` |
| E08 | `WRAP(ac).cruise_alt()`、`.cruise_mach()` 等 33 个 getter | dict：`default`（分布的**众数**，不是最优值，也不是均值）、`minimum`、`maximum`、`statmodel`。**高度单位为 km**，速度 m/s |
| E09 | 同义映射 | `prop` / `Drag` / `FuelFlow` 缺省 `use_synonym=False`，`WRAP` 缺省 True |
| E13 | `nav.airport(icao)` | `{lat, lon, alt, …}`。导航数据是 2013 年的，机场坐标可用 |
| — | `FlightGenerator(ac).complete(...)` | 按 WRAP 生成航迹 DataFrame。不含质量与油耗，巡航定高定速 |

## 三、燃油模型（2.x）

- 每台发动机：FF = scale · c1 · [1 − exp(−c2 · x · e^(c3·x))]，x = 所需推力 / 发动机最大推力（软限在约 0.03–1.2）。
- **只依赖推力比**，没有高度项和 Mach 项（物理上的后果见 [aircraft_model.md](aircraft_model.md)）。
- 有专门拟合的 14 型：A318、A319、A320、A321、A332、A333、B737、B738、B739、CRJ9、E170、E190、E195、E75L。其余用通用曲线，按发动机的 ICAO 起飞油耗缩放。
- 系数生成方式：Acropole 神经网络（DGAC，以 QAR 数据训练，AGPL）在 2022 年 ADS-B 航迹上算出燃油流量，质量在 0.70–0.95 MTOW 间随机取，再拟合（`scripts/build_fuel_model.py`）。
- 手册称精度与 BADA 3 相当。

## 四、日本常见机型的覆盖（按 2.6.2 数据；具体数值见 [validation.md](validation.md) T1）

| 情况 | 机型 |
| --- | --- |
| 机体、阻力、油耗拟合、WRAP 全部自有 | A319、A320、A321、B737、B738、B739、E190 |
| 机体与阻力自有，油耗用通用曲线 | B77W、B788、B789（WRAP 也自有）；B772、A359、B38M、A20N（WRAP 靠同义映射） |
| 阻力靠同义映射 | A21N → A20N、B763 → B752、E170 / CRJ9 → E75L |
| 机体靠同义映射 | AT76 → E145（喷气机体 + AE3007 涡扇，**对 ATR 无物理意义**） |
| 完全没有数据 | DH8D、CRJ7（另有 B78X、A35K 等） |

- **FuelFlow 内部会建 WRAP，并把 `use_synonym` 一并传过去**。所以 A359、A20N、A21N、B772、B38M、E170 这类没有自有 WRAP 的机型，即使机体与阻力自有，不加 `use_synonym=True` 也建不起 `FuelFlow`。opentop 同理。
- 对照脚本按数据目录自动判断，结果标「同义映射」，只作量级参考。

## 五、坑（本机实测）

- **(N, 1) 形状的数组会算错**：
  - 现象：`ndarrayconvert` 会把 (N, 1) 的结果 squeeze 成 (N,)，`FuelFlow.enroute` 内部再与 (N, 1) 的输入广播成 (N, N)。结果所有高度得到同一个阻力和油耗，而且不报错。
  - 一律传一维数组（或标量）。二维网格 (N, M>1) 不受影响。
- **警告过滤被覆盖**：
  - `openap/drag.py`、`prop.py` 导入时执行 `warnings.simplefilter("once", UserWarning)`，会盖掉调用方先设的过滤。
  - 要屏蔽「Wave drag is experimental」「using synonym」，得在导入之后再设。
- **dT 不是严格的气压高度口径**，见 [atmosphere_speeds.md](atmosphere_speeds.md)。
- **单位混用**：`aero` 用 m 和 m/s；`prop.aircraft` 的 `ceiling`、`cruise.height` 用 m；WRAP 高度用 km；其余模型用 ft 和 kt。
- **手册部分过时**：官方手册里有 1.x 的写法与示例数值。以已安装版本的源码和 `inspect.signature` 为准。

## 六、论文

1. Sun, J., Hoekstra, J. M., Ellerbroek, J. (2020). OpenAP: An open-source aircraft performance model for air transportation studies and simulations. *Aerospace* 7(8), 104. https://doi.org/10.3390/aerospace7080104 （CC BY 4.0）
2. Sun, J., Hoekstra, J. M., Ellerbroek, J. (2020). Estimating aircraft drag polar using open flight surveillance data and a stochastic total energy model. *Transportation Research Part C* 114, 391–404. https://doi.org/10.1016/j.trc.2020.01.026
3. Sun, J., Ellerbroek, J., Hoekstra, J. M. (2019). WRAP: An open-source kinematic aircraft performance model. *Transportation Research Part C* 98, 118–138. https://doi.org/10.1016/j.trc.2018.11.009
4. Bartel, M., Young, T. M. (2008). Simplified thrust and fuel consumption models for modern two-shaft turbofan engines. *Journal of Aircraft* 45(4), 1450–1456. https://doi.org/10.2514/1.35589 （OpenAP 推力模型）
5. Jarry, G., Dalmau, R., Very, P., Sun, J. (2025). On the generalization properties of deep learning for aircraft fuel flow estimation models. *Transportation Research Part C* 176, 105143. https://doi.org/10.1016/j.trc.2025.105143 （Acropole 一系）

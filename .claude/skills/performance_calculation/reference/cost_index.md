# 成本指数与 ECON 速度（cost_index）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：
> - Airbus《Getting to Grips with Aircraft Performance》（2002），以下简称 GTG：p.130–132（巡航）、p.157（爬升）、p.166（下降）；
> - Boeing AERO 2007 Q2「Fuel Conservation Strategies: Cost Index Explained」（Roberson）；
> - Boeing AERO 2007 Q4「Fuel Conservation Strategies: Cruise Flight」（Roberson、Root、Adams）。
> 访问 2026-11-25。

## 一、定义

- **CI = 时间相关成本 / 燃油成本**。
  - Airbus 写作 CI = C_T / C_F，单位 kg/min 或 100 lb/h（GTG p.132）。
  - Boeing 写作 CI = 时间成本（$/hr）÷ 燃油成本（cents/lb），相当于 100 lb/h（AERO Q2 2007）。
  - 两者换算见 [atmosphere_speeds.md](atmosphere_speeds.md)：1 kg/min ≈ 1.323（100 lb/h）。
- **含义**：CI 30 kg/min 表示多飞一分钟的成本等于 30 kg 燃油（GTG p.132）。
- **两端**（GTG p.132）：
  - CI = 0：只省油，巡航 Mach = MRC；
  - CI 最大：只省时间，巡航速度取最大值，一般为 MMO − 0.02 或 VMO − 10 kt。
- **可输入范围随机型而异**（AERO Q2 2007 图 1）：
  - 737-300/400/500：0–200；737-600/700/800/900：0–500；
  - 747-400、777：0–9999；757、767：0–999 或 0–9999。
- CI 由航司按自己的成本算出，飞行员输入 FMC/FMGS。没有公开的日本航司 CI 值。

## 二、ECON 速度怎么来

每地面海里的成本，以燃油当量（kg）计：

```
C / NM = (FF[kg/min] + CI[kg/min]) / GS[NM/min]  =  (FF[kg/h] + 60 · CI) / GS[kt]
```

- 在可飞的（高度, Mach）里取这个值最小的点，即 ECON。CI = 0 时就是地面比航程最大的点。
- **风**：
  - 顺风时 ECON 变慢，逆风时变快。
  - Boeing 的 ECON 会按巡航高度的风调整，LRC 一般不调（AERO Q4 2007）。所以 LRC 只在无风时最理想。
- **爬升、下降也有 ECON 速度**，取决于 CI 和重量：
  - CI = 0 时，爬升用最大爬升率速度（GTG p.157），下降用最小下降速度（GTG p.166）；
  - CI 越大，爬升、下降都越快。

## 三、MRC、LRC 与比航程

- **SR、MRC、LRC 的定义**见 [aircraft_model.md](aircraft_model.md)。
- **LRC 的由来**：
  - LRC 以损失 1% 的比航程，换取 3–5% 的速度（AERO Q4 2007 引《Aerodynamics for Naval Aviators》）；
  - Boeing 的定义是 MRC 之上、燃油里程下降 1% 的速度（同上）。
- **SR 曲线在 MRC 附近很平**，所以：
  - CI 不大时，速度可以明显提高而油耗增加有限；
  - 模型的燃油流量若对 Mach 不敏感，MRC 和 ECON 就会落到很快的速度上（见下节）。

## 四、CI 与高度

- **CI 越大，最优高度越低**。原因有两个：
  - 若最优 CL 不变，Mach 越高，最优高度反而越高；
  - 但接近阻力急升区时，为了避开波阻，最优 CL 下降得比 M² 增长更快；接近最大速度时，高度还会被推力限住。Boeing 757 在 CI 9999 时 OPT 就等于 MAX。
  - Boeing 757 的例子（AERO Q2 2007 图 3）：
    - CI 0：OPT FL328、MAX FL362、推荐 FL310；
    - CI 9999：OPT FL268、MAX FL268、推荐 FL260；
    - CI 70：OPT FL327、MAX FL363、推荐 FL310。
  - OpenAP.top 论文（Sun 2022）用 opentop 跑 A320 也得到「CI 越高，整体高度越低、速度越高」。
- **opentop 的 `ci:N` 不是航司 CI**，而是 0–100 的时间 / 燃油权重。换算方法见 [opentop.md](opentop.md)。

## 五、用 OpenAP 算 CI 时的局限（实测，详见 [validation.md](validation.md)）

- **窄体机**：OpenAP 核心模型下，A320 / B738 / E190 的 ECON 在 CI ≥ 10 kg/min 时几乎都顶到 MMO − 0.02（M0.80）。原因是燃油流量几乎不随 Mach 变化，见 [aircraft_model.md](aircraft_model.md)。
  - 只有重量大时，CI 才会改变 ECON 高度：A320 0.95 MTOW 时，CI 0 → FL350，CI 100 → FL320（T2）。
- **宽体机**：B789 / A359 / B772 的趋势与公开资料一致，CI 越大 ECON 越快、越低。例如 B789 0.80 MTOW：CI 0 为 FL370 / M0.795，CI 60 为 FL350 / M0.835（T2）。
- **所以**用 OpenAP 给窄体机定 ECON Mach，数值不可信。要么校准燃油流量对 Mach 的依赖，要么只用来比较「同一机型、不同 CI」的相对差别。

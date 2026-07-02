# 日本机场 进场/离场移交点（transfer_points）

> 各主要机场 enroute 接入终端区的**进场门 / 离场门**，以及管制移交高度与席位。数据来自 VATJPN 交通管制部运用课「移管点与高度」SOP（公开页：`https://vatjpn.org/document/public/om/sop/transfer-point-and-alt`）。
> 上游：[../SKILL.md](../SKILL.md)；与 [enroute.md](enroute.md)（进场点/离场点选择）、[star.md](star.md)/[iap.md](iap.md) 配合。字段格式见 [cifp_format.md](cifp_format.md)。

> ⚠️ 这是**进场点/离场点的权威出处**——优先级高于从 CIFP 猜 STAR/本场 VOR/IAF。机器可读副本：项目根 `transfer_points.json`。

## 一、表的语义与它编码的运行规则

每机场分**到着(arrival)/出発(departure)**，给「移管点等(航路尾/头) → 高度 → 移管元→移管先(管制席位)」，按 **進入管制区**(approach control area) 分区。编码 4 类规则：

1. **进场门 / 离场门**：enroute 接入终端区的固定航路尾(到着)/头(出発)。**进场门 = 到着串里由 enroute 航路接上的入口 fix**；其后常接**门后 DCT 直飞点**（STAR 点 / 本场 VOR，如 RJFM 北向 `KUE ESKAP KROMA ENBEN MZE` 里 KUE 之后的段）。例 RJTT 进场门 `GODIN·POLIX·AROSA·AKSEL·XAC·MESSE`（各方向）。
   - **门后 DCT 尾段**（v1.4.1）：这些门后直飞点管制会据以引导下降，**不再丢弃**——已抽进机器可读的 `transfer_points.json` 的 `arr_dct`(`{进场门: [门后 DCT fix…]}`)，由 `router._append_arrival_tail` 在生成航路落到该门后补进 enroute（带「背离本场即截断」裁剪）。这些点多不在航路网上(0 airway 边)，A* 只能到门、尾段靠此数据补全。
2. **管制移交 + 高度**：`移管元→移管先` 给出 ACC 扇区(TG/BG/DG/SK…)↔APP/TWR 的交接点与高度，即真实下降移交剖面。
3. **条件化**（进场门随条件变，规划时须据此选门）：
   - **方向** `(South/North/East/West Bound)`：如 RJSN 南行 `Y122 INAHO`、北行 `KENSI Y312`。
   - **跑道**：如 RJCC `IDEMI(RWY01)` vs `NAVER(RWY19)`，移交高度亦随之。
   - **机型** `(JET/PROP/DH8D)`：如 RJCC `Y13…NAVER(JET)` vs `Y11 NAVER(PROP)`；RJOO `ROKKO` 三机型三高度。
4. **证据可信度**：原表 `ソース` 列标了来源与样本数（`元無線×N`/`ads-b`/`aip TOD共有`/`Youtube`）。

## 二、各机场 进场门 / 离场头（按進入管制区）

> `进场门`/`离场头`=结构化抽取（机器可读在 `transfer_points.json`）；`到着`=原始航路尾（含方向/跑道/机型限定，供精确判定）。

### 日高進入管制区
- **RJCM**　进场门: OZORA　‖　离场头: MENIB · OLDUS
    - 到着: …Y111 OZORA
- **RJEC**　进场门: AWE　‖　离场头: KAGRA
    - 到着: …NAVER Y139 ASIBE V7 AWE / …Y13 CHE V7 AWE
- **RJCN**　进场门: MASHU　‖　离场头: —
    - 到着: …Y111 TCE V2 MASHU
- **RJCK**　进场门: CRANE　‖　离场头: —
    - 到着: …MQE Y111 CRANE  |  …MQE Z13 AKESI Y111 CRANE
- **RJCB**　进场门: OBE　‖　离场头: RACKO
    - 到着: …Y110 OBE

### 札幌進入管制区
- **RJCO**　进场门: RUMOI · KURIS · MOIWA　‖　离场头: RUMOI · KURIS · MOIWA · SPE
    - 到着: …V1 RUMOI SPE (South Bound)  |  …V2 KURIS (West Bound)  |  …HWE V2 MOIWA / …TEKKO V2 MOIWA

### 千歳進入管制区
- **RJCJ**　进场门: NAVER　‖　离场头: —
    - 到着: …Y13 SIRAO Y139 NAVER
- **RJCC**　进场门: CHE · NAVER　‖　离场头: —
    - 到着: …RUMOI V1 CHE  |  …Y13 SIRAO Y139 NAVER (JET)  |  …NONUT Y11 NAVER (PROP)

### 白神進入管制区
- **RJCH**　进场门: UPLOK · TAXIR · HWE　‖　离场头: HWE
    - 到着: …V2 UPLOK  |  …Y113 TAXIR / …V31 HWE
- **RJSA**　进场门: BYOBU · MRE　‖　离场头: OHMAR · UWE · GONOU
    - 到着: …Y146 HIBAR Y113 BYOBU  |  …UWE HINAI Y113 MRE  |  …Y13 AKITA Y131 HINAI Y113 MRE  |  …Y113 MRE  |  …Y19 MRE
- **RJSR**　进场门: ODE · UWE　‖　离场头: —
    - 到着: …Y32 UWE Y312 ODE / …V32 UWE BONJI ODE
- **RJSK**　进场门: MAGGY · CHOKA · YAYOI　‖　离场头: —
    - 到着: …Y144 MAGGY  |  …Y32 CHOKA  |  …Y312 YAYOI
- **RJSI**　进场门: ENBIM · SIOMO　‖　离场头: HANKA
    - 到着: …MRE Y100 ENBIM  |  …Y153 SIOMO

### 三沢進入管制区
- **RJSM**　进场门: HWE · MIS　‖　离场头: JYONA · HPE
    - 到着: …HWE OHMAR MIS  |  …V10 MIS

### 仙台進入管制区
- **RJSS**　进场门: SDE · OWLET · LANCE　‖　离场头: SAMBO
    - 到着: …Y102 SDE  |  …Y15 OWLET  |  …Y515 LANCE

### 新潟進入管制区
- **RJSN**　进场门: INAHO · GTC · MAGNA · TERAD　‖　离场头: KENSI · GTC · MOKBA · NAEBA · HAKBA
    - 到着: …Y122 INAHO / …V31 GTC (South Bound)  |  …Y312 MAGNA  |  …Y45 TERAD / …Y142 GTC  |  …IGROD Y347 GTC

### 百里進入管制区
- **RJAH**　进场门: TATSU　‖　离场头: OGITU
    - 到着: …Y88 DAIGO Y887 TATSU NAKAH  |  …Y10 DAIGO Y887 TATSU NAKAH

### 東京進入管制区
- **RJTT**　进场门: GODIN · POLIX · AROSA · AKSEL · XAC · MESSE　‖　离场头: SPOON · BRUCE · INUBO · VAMOS · LAXAS · NINOX · GUSRO · LAYER · TIARA · BEKLA · OPPAR
    - 到着: …Y10 GODIN  |  …Y807 POLIX  |  …Y824 AROSA  |  …Y87 TOPIT Y875 AROSA  |  …Y21 AKSEL  |  …Y71 XAC  |  …Y108 MESSE
- **RJTL**　进场门: HAGAR · MERED · TOSKO　‖　离场头: —
    - 到着: …HAGAR KIDOR ASEKI  |  …MERED KAMOG OJT TSUGA SHT TOHNE  |  …TOSKO OMIYA SYE TOHNE (多分)
- **RJTO**　进场门: XAC　‖　离场头: —
    - 到着: …Y71 XAC OSE / …V17 XAC OSE
- **RJAA**　进场门: SWAMP · SUPOK · LUBLA · RUTAS　‖　离场头: AGRIS · KIMIN · GULBO · BORLO · OLVAN · SAMUS · PIGOK · REDEK · ENPAR
    - 到着: …Y30 SWAMP  |  …R211 SWAMP  |  …Y809 SUPOK  |  …Y813 LUBLA  |  …Y81 RUTAS

### 横田進入管制空域
- **RJTY**　进场门: HAILY · KOSKA · SWING　‖　离场头: KOGAR · KOSKA · BUSYU
    - 到着: …Y88 NIKKO Y90 HAILY  |  …Y824 DOLBA Y818 LIGNI Y821 KOSKA  |  …MERED Y821 KOSKA  |  …MOE Y588 KOSKA  |  …Y71 XAC Y588 KOSKA / …V17 XAC Y588 KOSKA  |  …Y295 KOSKA  |  …SWING ALPUS BONTI TOSKO (多分)

### 小松進入管制区
- **RJNK**　进场门: HIMMY · IMIZU · KMC · SONBU　‖　离场头: SUMVU · OHNNO · SONBU
    - 到着: …GOLDO Y381 HIMMY  |  …Y45 IMIZU / …V30 IMIZU (West Bound)  |  …Y384 KMC  |  …Y45 SONBU

### 中部進入管制区
- **RJNA**　进场门: SWING · KCC · SHIMA　‖　离场头: —
    - 到着: …MBE Y121 SWING  |  …Y88 KCC / …Y88 SWING  |  …KEC Y12 SHIMA  |  …MIDER Y28 KCC / …MIDER V28 KCC  |  …KMC Y383 KCC
- **RJGG**　进场门: SWING · SLIDE · OLTOM · CARDS · BIWWA · CHESS　‖　离场头: BOGON · MODEL · ESPAN · FTAMI
    - 到着: …MBE Y121 SWING  |  …Y88 SUGAL Y881 SLIDE  |  …Y50 OLTOM  |  …Y755 CARDS  |  …MIDER Y28 BIWWA / …MIDER V28 BIWWA  |  …Y511 CHESS

### 関西進入管制区
- **RJBB**　进场门: DUBKA · EVERT · CANDY · NIXOV · IGLEV · ATMUG　‖　离场头: NANKO · SOVRI · TOMOH · UPMIN · OMGOR · OBLUR · LINDA · MAIKO
    - 到着: …KOHWA Y544 DUBKA  |  …Y46 EVERT / …Y46 CANDY  |  …Y48 EVERT / …Y48 EVERT Y46 CANDY  |  …Y53 NIXOV  |  …Y35 IGLEV  |  …Y36 ATMUG
- **RJBE**　进场门: AVKUL · OMBIP · TRACY　‖　离场头: MUKRI · MAIKO
    - 到着: …Y537 LOVGI Y353 AVKUL  |  …Y35 URDET Y353 AVKUL  |  …MIHOU Y39 OYE V28 OMBIP  |  …WAKIT Y201 TRACY
- **RJOO**　进场门: AGPUK · IZUMI · ROKKO　‖　离场头: PANAS · MINAC · ASUKA · TIGER
    - 到着: …KOHWA Y546 AGPUK MIRAI ABENO IKOMA  |  …Y753 IZUMI  |  …Y231 MIRIO Y401 KAINA Y753 IZUMI  |  …BOTAN KABIL KRE KAIFU Y403 KAINA Y753 IZUMI  |  …Y401 KAINA Y753 IZUMI  |  …KTE V38 OLIVE Y28 SANDA V55 IZUMI  |  …ROKKO KAMEO OTABE ABENO IKOMA
- **RJOT**　进场门: WIMPY · TAKMA · OYE　‖　离场头: OLIVE · WASYU · TAROH
    - 到着: …WAKIT Y203 WIMPY  |  …MYE Y283 KINOE Y288 TAKMA KTE  |  …MIHOU Y39 OYE KTE
- **RJOB**　进场门: OYE · INOOK　‖　离场头: OYE · OLIVE · CHIZU · WASYU
    - 到着: …WAKIT Y205 OYE  |  …MYE Y283 KINOE Y288 INOOK OYE  |  …MIHOU Y39 OYE
- **RJOK**　进场门: PANCH · JAKAL · KRE · MYE　‖　离场头: MUROT · SUC · OMOGO · KRE
    - 到着: …TURFY Y242 PANCH / …JAKAL PANCH KRE  |  …SUC V53 KRE  |  …MYE BOTAN KABIL…

### 徳島進入管制区
- **RJOS**　进场门: DATIS · TOSAR · TSC　‖　离场头: HONMA · TOSAR · KTE
    - 到着: …KOHWA Y544 SINGU Y542 DATIS  |  …SUC V53 KRE V37 TOSAR  |  …MYE Y33 KTE Y331 TSC  |  …Y39 OYE KTE Y331 TSC

### 美保進入管制区
- **RJOH**　进场门: RAKDA · KYOKA · XZE · PEPOS　‖　离场头: YAPPA · STAGE · MIHOU
    - 到着: …Y18 RAKDA  |  …Y45 KYOKA YGE / …V29 XZE  |  …Y597 PEPOS
- **RJOC**　进场门: RAKDA · XZE　‖　离场头: TSUNO · MIHOU · CARPS
    - 到着: …Y18 RAKDA / …Y188 RAKDA  |  …G597 XZE  |  …Y45 KYOKA V29 XZE / …V29 XZE

### 広島進入管制区
- **RJOA**　进场门: AMURO · SUNFL · OPERA · HGE　‖　离场头: BOLIG · KIJYY · MARCO · SINFO
    - 到着: …Y20 KAMMY Y202 AMURO  |  …Y453 SUNFL MISEN HGE  |  …OPERA AKANA MIYOS HGE / …V29 HGE

### 岩国進入管制空域
- **RJOI**　进场门: MARCO · MYE　‖　离场头: MALTA · MYE
    - 到着: …Y45 MARCO NEU (South Bound)  |  …Y28 MARCO  |  …Y45 MARCO NEU (North Bound)  |  …Y40 MYE
- **RJOM**　进场门: ITUKI · BAMBO · MYE · MARCO　‖　离场头: —
    - 到着: …BAMBO Y283 ITUKI / …BAMBO KINOE ITUKI MYE  |  …Y40 MYE  |  ENGID Y412 MYE  |  …V28 MARCO (East Bound)

### 築城進入管制区
- **RJDC**　进场门: UBE　‖　离场头: KOHEI · FIATO · UBE
    - 到着: …MARCO Y284 UBE  |  …Y209 IKE A595 DGC V28 UBE
- **RJFR**　进场门: ASARI · SWE　‖　离场头: ONGHA · FIATO · KOHEI
    - 到着: …MARCO Y285 ASARI  |  …Y14 DGC V28 SWE  |  …Y209 IKE A595 DGC V28 SWE

### 福岡進入管制区
- **RJFF**　进场门: KIRIN · DGC · HABOH · ISKUP · ATSAG · IKE · SARUP　‖　离场头: DGC · ENGID · BUTUR · IPRIR · CARSE · YAMGA · SGE
    - 到着: …MARCO Y256 STOUT Y20 KIRIN  |  …Y20 KIRIN  |  …V28 DGC (West Bound)  |  HABOH FUGEN OMUTA OSTEP HONOK  |  …Y25 ISKUP  |  …Y253 ATSAG  |  …SAMDO A595 IKE  |  …Y209 SARUP
- **RJFS**　进场门: MILEP · OLE　‖　离场头: SGE · OOITA
    - 到着: …KOSHI Y501 SASIK Y14 TAIME Y40 MILEP UGAMU SGE  |  …FUE Y40 OLE SGE / …FUE Y40 MILEP UGAMU SGE  |  …Y209 IKE Y251 OLE SGE / …Y209 IKE Y251 OLE Y40 MILEP UGAMU SGE  |  …Y40 MILEP UGAMU SGE / …V40 MILEP UGAMU SGE
- **RJFT**　进场门: KAZMA · HINAG · KUE　‖　离场头: MYE · SALTY · SPIDE · DONAR · TFE · HKC · KUE
    - 到着: …Y40 KAZMA (West Bound)  |  …G339 HINAG (North Bound)  |  …KOSHI Y501 SASIK Y14 HINAG  |  …Y251 OLE Y40 KUE  |  …Y14 TAIME Y40 KUE (South Bound)
- **RJFU**　进场门: OHGIE · OLE · HONDO　‖　离场头: AKNAG · DGC · OOITA · CARCO
    - 到着: …Y204 OHGIE  |  …FUE Y40 OLE  |  …HONDO OLE (North Bound)  |  …Y209 IKE Y251 OLE

### 大分進入管制区
- **RJFO**　进场门: YANAI · OOITA　‖　离场头: —
    - 到着: …Y45 YANAI BAIEN TFE (South Bound)  |  …Y40 OOITA (East Bound)  |  …Y45 OOITA (North Bound)

### 鹿児島進入管制区
- **RJFK**　进场门: KUE · HKC · SPICA · KINKO　‖　离场头: MIDAI · HKC · SASIK
    - 到着: …KUE ESLIL HIGOH KGE  |  …Y45 HKC (South Bound)  |  …Y757 SPICA  |  …Y455 KINKO / …Y14 KINKO / …KINKO  |  …Y45 HKC (North Bound)  |  …A582 HKC  |  …Y14 HKC (South Bound) / …G339 HKC (South Bound)
- **RJFM**　进场门: TFE · RYUGU · JACKY · SASIK · KUE　‖　离场头: MADOG · JACKY · LALAG
    - 到着: …TFE ABUMI SIIBA MZE  |  …Y402 RYUGU  |  …B597 JACKY MZE  |  …SASIK LALAG MZE  |  …KUE ESKAP KROMA ENBEN MZE

### 那覇進入管制区
- **ROAH**　进场门: IHEYA · NHC · VELNO · OLVAL　‖　离场头: ONC · AMAMI · GANJU · OLVAL
    - 到着: …Y525 IHEYA  |  …V75 NHC LAVON  |  …Y573 MJC Y57 VELNO  |  …Y57 VELNO  |  …V91 OLVAL
- **ROKJ**　进场门: NHC　‖　离场头: —
    - 到着: …A582 NHC LAVON GURUX DORIS (South Bound)
- **RJKA**　进场门: KANAH · TUMGI　‖　离场头: —
    - 到着: …BOMAP Y25 KANAH  |  …ALTAI Y758 TUMGI  |  …B597 TUMGI  |  …TONAR Y521 TUMGI
- **RJKI**　进场门: POMAS　‖　离场头: —
    - 到着: …Y456 POMAS
- **RJKN**　进场门: ANOXA　‖　离场头: —
    - 到着: …Y45 ANOXA (South Bound)
- **RJKB**　进场门: ANOXA　‖　离场头: —
    - 到着: …Y45 ANOXA (South Bound)
- **RORY**　进场门: ONC　‖　离场头: —
    - 到着: …Y45 ONC ASATO YRE (South Bound)

### 先島進入管制区
- **RORS**　进场门: DIANA　‖　离场头: FREED
    - 到着: …Y62 GANAS Y576 DIANA
- **ROMY**　进场门: YUTAH　‖　离场头: FREED · PAYAO
    - 到着: …Y62 GANAS Y576 YUTAH
- **ROIG**　进场门: DIANA　‖　离场头: MJC · GUSUK
    - 到着: …Y62 GANAS Y576 DIANA

## 三、用于本项目

- **进场端点(problem 1/P1)**：`_arrival_candidates` 应**优先**用本表进场门（解析为图节点、在航路网上、按到达方向择门），再回退 CIFP STAR/本场 VOR/IAF。
- **离场端点**：同理用离场头。
- **方向/跑道/机型条件**：先按 enroute 航向在多门中择一；跑道/机型留待细化。
- **移交高度**：未来下降剖面 / SimBrief TOD。
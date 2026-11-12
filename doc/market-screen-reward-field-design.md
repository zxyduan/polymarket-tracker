# Market Screen 新增「每日盘口奖励」字段落库设计（v3，待确认后编码）

> 背景：在现有「每日 24h 成交额榜落库」链路（`market_screen`）基础上，纳入盘口做市奖励信息。
> 本文只做现状核对与改造设计，**当前未改代码**；已定口径见 §1 与 §7.1，风险边界见 §7.2。
> 约束沿用 `ARCHITECTURE.md` 分层红线：`features/` 不 import `src/`；`framework/models/` 仅标准库；SQL 全部 `?` 参数绑定；只读对象走白名单。
> 基线：本仓库当前工作树 market_screen 相关 46 个单测全部通过（parsing/store/usecase/lister/schedule，`python -m pytest tests/test_market_screen_*.py -q`）。

## 1. 口径决策

### 1.1 已拍板

| 项 | 决策 | 说明 |
|---|---|---|
| 落库指标 | **`rewardsDailyRate`（每日奖励量/速率）** | 每天发给合格挂单做市商的奖励数量（奖励代币/天）；本次需求要的「每天奖励的量」 |
| 附带标识 | **奖励代币符号 `rewards_asset`** | 与每日速率成对落库，区分 v1/v2 抵押代币 |
| **准入门槛（硬规则）** | **速率低于 `reward.min_daily_rate`（默认 30.0）一律剔除** | 无 `clobRewards` 配置、速率 NULL/非数值、速率 < 30（奖励代币/天）都在过滤阶段剔除；准入线走配置，不加功能开关 |
| **排序键** | **复合倒序：`rewards_daily_rate` DESC, `volume_24h` DESC** | 奖励速率优先，速率相同再按 24h 成交额倒序；TopN 在复合排序之后截断 |
| 落库形式 | **只存速率数值 + 代币标识，不落奖励分档标签** | 不新增 reward tier 列/布尔标志；分档由纯函数在查询/展示层按配置阈值现算 |
| **奖励分档阈值** | **低 [30,100)、中 [100,300)、高 [300,+∞)** | 左闭右开；阈值进 `reward.tiers` 配置可调；分档结果不入库，仅查询/展示层使用 |
| **读侧分析筛选** | **奖励高 + 剩余时间长 + 某方向概率低，三条件叠加** | 奖励取最新一轮 `latest_rewards_daily_rate`（不跨天聚合）；剩余天数、方向概率沿用现有查询参数；排序奖励优先，详见 §4.8 |
| **奖励稳定程度** | **复用现有 `max_streak`（最长连续命中天数），不新建独立字段** | 准入线 ≥30 后落库即当天有奖励，连续有奖励 ≈ 连续上榜；`max_streak` 缺天断档重置，仅展示参考，不参与过滤与排序 |
| 持续性查询 | **带出最新日奖励列** | `PersistentCandidateRow` 补 `latest_rewards_daily_rate / latest_rewards_asset`；聚合层不做跨天奖励汇总，只带最新一轮值 |
| 未知代币 | **兜底存小写原始地址** | 可追溯、不丢信息；不存空串 |

### 1.2 本期不落（P2）

| 字段 | 含义 | 不落原因 |
|---|---|---|
| `rewardsAmount` | 奖励池总额（计划总预算） | 实测当前活跃市场全为 0：奖励计划 `endDate=2500-12-31`（永续、无固定预算），总额字段不填；落库全是 0/NULL，无分析价值 |
| `rewardsMinSize` / `rewardsMaxSpread` | 奖励资格门槛：最小挂单量 / 最大价差（CENTS） | 属于做市条件，不是奖励金额；本期不采集，留 P2 |
| 奖励分档标签 | 高/中/低速率档位 | 落库只存原始速率；档位由纯函数按 `reward.tiers` 阈值在查询/展示层现算，不写入库列 |

## 2. 字段来源与口径

### 2.1 取数位置（不新增 HTTP 请求）

复用现有 Gamma 列表响应，奖励字段随条目一起返回：

```
GET https://gamma-api.polymarket.com/markets
  ?active=true&closed=false&order=volume24hr&ascending=false&limit=100&offset=…
```

奖励信息在每条市场条目顶层 `clobRewards` 数组中（官方 SDK Gamma 模型 `MarketRewards.clob_rewards` 别名 `clobRewards` 可佐证）。CLOB 当前奖励接口可交叉验证，SDK 模型 `CurrentReward` 结构：

```
GET https://clob.polymarket.com/rewards/markets/current
→ rewards_config[].rate_per_day / asset_address
→ rewards_min_size / rewards_max_spread / sponsored_daily_rate / native_daily_rate / total_daily_rate
```

> 采集只取 Gamma 列表；CLOB 仅作交叉验证。两处字段命名以官方 SDK（`polymarket` 包）模型别名为准：Gamma 侧驼峰 `rewardsDailyRate/assetAddress`，CLOB 侧蛇形 `rate_per_day/asset_address`。

### 2.2 字段映射

| 落库列 | Gamma 来源（采集） | CLOB 对应（交叉验证） | 口径 |
|---|---|---|---|
| `rewards_daily_rate` | `clobRewards[0].rewardsDailyRate` | `rewards_config[0].rate_per_day` | 每日奖励数量（速率），单位为奖励代币；None / 布尔 / 非数值 / NaN / Infinity → NULL（准入阶段剔除） |
| `rewards_asset` | `clobRewards[0].assetAddress` | `rewards_config[0].asset_address` | 奖励代币符号；已知地址映射符号，未知地址兜底存小写地址 |

### 2.3 奖励代币映射（链上身份已核对）

| 地址（小写） | 符号 | 说明 |
|---|---|---|
| `0x2791bca1f2de4661ed88a30c99a7a9449aa84174` | `USDC.e` | CTF 层抵押品，v1/v2 不变 |
| `0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb` | `pUSD` | CLOB v2 钱包侧抵押品，1:1 由 USDC 背书的 ERC-20 wrapper |

- 两者都是美元稳定抵押品（1:1 背书），速率数值可近似按美元横比；保留代币标识是为了区分 v1/v2，本期不做汇率归一。
- 早期记录曾把 pUSD 地址误标为 WETH，已纠正；本方案不出现 WETH 假设。

### 2.4 实测样本与红线（2026-11-11 前 400 活跃市场）

- 149 条有 `clobRewards` 配置，251 条缺失/空；单市场奖励条目实测均为 1 条。多条时当前设计取首条，其余忽略——该取舍未经官方确认；若上线抽样发现多条不同速率/代币，应改为取 `rewardsDailyRate` 最大者（P2 复核项）。
- `rewardsAmount` 全 0；`rewardsDailyRate` 有有效值（600/1000/300/30/0.001 量级等）。
- `clobRewards` 实测为 JSON list；解析层按现有惯例兼容「字符串化 JSON 数组」两种形态（对齐 `clobTokenIds`）。
- 口径红线：
  - `rewardsDailyRate` 是**每日速率**（每天发多少），不是累计已发奖励，也不是池子总额；列名不得叫 `reward_amount`，避免误读为总额。
  - 奖励**参与准入过滤与排序**：速率低于准入线（默认 30/天）或缺失的市场剔除；保留下来的市场才进入成交额阈值、复合排序、TopN 与三档概率判定。
  - 奖励**不改变现有三档（strict/balanced/loose）概率×到期时间判定**；奖励高/中/低分档由查询层纯函数现算，不落标签、不入库列。

### 2.5 证据与局限

- 官方 docs.polymarket.com 的 Gamma 字段页主站当前拒绝抓取，未取得官方字段表原文；`rewardsAmount` / `rewardsDailyRate` 释义来自第三方数据商 schema（GetOData、Apify scraper 描述为 CLOB reward 的 amount / daily rate；Predictefy 将 `rewardsDailyRate` 定义为 liquidity-reward daily rate per day）。
- 本沙箱 vm 出口走代理，Gamma/CLOB 当前不可直连；上述分布与样本来自 2026-11-11 直连实测。上线前需在 EC2 实跑一轮复核字段仍在、代币分布无新增币种。

## 3. 现状代码定位（改造前基线，当前工作树）

| 层 | 文件 | 现状 |
|---|---|---|
| 模型 | `framework/models/market_screen.py` | `CandidateMarket`（23 字段）、`PersistentCandidateRow`（含可选 `max_streak`）、`ScreenRunMeta`、`ScreenQueryResult` |
| 解析 | `framework/parsing/market_screen_parsing.py` | `parse_market()` 唯一生产构造 `CandidateMarket`；已有 `_json_list / _to_float / _parse_date_part` 安全工具；纯函数层无 IO |
| 采集 | `framework/sources/market_lister.py` | 只透传原始条目，无字段解析（无需改动） |
| 编排 | `features/market_screen/usecase.py` | `parse_market` → 成交额阈值过滤 → 按 `volume_24h` 倒序 TopN → `dataclasses.replace` 套三档判定 |
| 存储门面 | `framework/store/market_screen_store.py` | `_map_row()` dict → `PersistentCandidateRow` |
| 存储实现 | `src/storage.py` | 明细表 DDL、`_SCREEN_DAILY_COLUMNS`（23 列）、`_daily_params`、`replace_screen_day` 占位符 23、`_SQL_PERSISTENT_HIT`、`_SQL_PERSISTENT_STREAK`、3 个视图；旧库 `try ALTER` 迁移先例（`volume_shares`） |
| 测试 | `tests/test_market_screen_parsing.py` / `test_market_screen_store.py` / `test_market_screen_usecase.py` / `test_market_screen_lister.py` | 解析样本 `_good_raw()`、存储夹具 `_row()`、用例 FakeLister |

## 4. 分层改造清单

### 4.1 模型 `framework/models/market_screen.py`

`CandidateMarket` 末尾追加两个带默认值的字段（frozen dataclass；带默认值不破坏现有位置构造与 `dataclasses.replace`）：

```text
rewards_daily_rate: float | None = None
rewards_asset: str = ""
```

`PersistentCandidateRow` 追加（⑧严格连续路径未返回时留默认值，不臆造）：

```text
latest_rewards_daily_rate: float | None = None
latest_rewards_asset: str = ""
```

稳定程度不新增字段：复用现有 `max_streak`（最长连续命中天数，仅 STREAK 路径填充）；准入线 ≥30 后连续上榜即连续有奖励。

### 4.2 解析 `framework/parsing/market_screen_parsing.py`（只抽取，不做准入）

- 新增代币地址 → 符号映射常量（地址统一小写键）：

```python
_REWARD_ASSET_SYMBOLS: dict[str, str] = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",
}
```

- 新增纯函数（无 IO）：

```python
def _parse_rewards(raw: dict[str, object]) -> tuple[float | None, str]:
    """从 clobRewards 提取每日奖励速率与代币标识；缺失/脏数据返回 (None, "")。

    兼容 list 与字符串化 JSON 数组；多条取首条；已知地址映射符号，
    未知地址兜底存小写原始地址。准入剔除由 usecase 判定，本层不丢行。
    """
```

  - `clobRewards` 走 `_json_list()` 兼容两种形态；空/非 list → `(None, "")`。
  - 首条须为 dict；`rewardsDailyRate` 走 `_to_float()`，非有限 → None。
  - `assetAddress` strip → lower；命中映射取符号，未命中保留小写地址；空地址 → 空串。
  - 任何解析异常都收敛为 `(None, "")`，不抛异常、不剔除主行。

- `parse_market()` 组装 DTO 时填入 `rewards_daily_rate`、`rewards_asset`。

- 新增分档纯函数（无 IO，供查询/展示层现算，**不入库**）：

```python
def classify_reward_tier(
    rate: float | None,
    *,
    low_min: float,
    mid_min: float,
    high_min: float,
) -> str | None:
    """按每日速率分档，返回 "high" / "mid" / "low"；None 或低于 low_min 返回 None。

    区间左闭右开：high >= high_min；mid ∈ [mid_min, high_min)；
    low ∈ [low_min, mid_min)。阈值由配置注入，本函数不读全局配置。
    """
```

  - 边界：`rate is None` → None；比较前复用有限数判断，非有限 → None。
  - 阈值合法性（`0 <= low_min <= mid_min <= high_min`）在配置解析层校验，非法回退默认并告警。

### 4.3 编排 `features/market_screen/usecase.py`（准入门槛 + 复合排序）

在 `_build_rows()` 现有「成交额阈值过滤」处并列加入奖励准入，并调整排序键：

1. **奖励准入（硬规则，先于排序）**：`parse_market` 返回后，
   `rewards_daily_rate is None or rewards_daily_rate < getattr(cfg, "reward_min_daily_rate", 30.0)` 的候选直接 `continue`，不进入后续成交额阈值/排序/档位流程。准入线默认 30.0（奖励代币/天），来自配置；`getattr` 兜底防止旧配置/测试桩缺字段。
2. **成交额阈值**：维持现有 `volume_24h < cfg.min_value` 过滤（与奖励准入顺序可互换，最终取交集）。
3. **复合排序**：把现有 `parsed.sort(key=lambda r: r.volume_24h, reverse=True)` 改为

   ```python
   parsed.sort(
       key=lambda r: (r.rewards_daily_rate or 0.0, r.volume_24h),
       reverse=True,
   )
   ```

   准入已剔除 `rate is None` 的行，排序键里的 `or 0.0` 仅作防御兜底；理论上不会出现 None 参与排序。

4. **TopN 截断**：在复合排序之后按 `cfg.top_n` 截断，位置不变。
5. 三档概率×到期时间判定、`persist_non_passed` 语义不变；奖励字段随 `dataclasses.replace(base, …)` 自动保留。
6. 可选观测（P1 可不做）：运行元信息补充「无奖励剔除计数」；若加，需同步 `ScreenRunMeta`、run 表列与 `_RUN_UPSERT_SQL`，本期默认不加，先靠日志/候选数对比观察。

### 4.4 存储 `src/storage.py`

**明细表 DDL**（新库 `SCHEMA`）在 `market_screen_daily` 追加：

```sql
rewards_daily_rate REAL,
rewards_asset      TEXT NOT NULL DEFAULT '',
```

- `_SCREEN_DAILY_COLUMNS` 末尾追加 `rewards_daily_rate, rewards_asset`。
- `_daily_params()` 末尾追加 `row.rewards_daily_rate, row.rewards_asset`；`replace_screen_day` 的 INSERT 占位符由 **23 增至 25**。现状占位符数量硬编码 `range(23)`，建议顺手改为由 `len(_SCREEN_DAILY_COLUMNS.split(","))` 推导，避免列数再变时占位符漂移（属本文件内最小改动，不顺带重构其他逻辑）。
- **旧库迁移**：仿 `volume_shares` 的 `try ALTER TABLE ... ADD COLUMN` 模式，在 `init()` 内补两条 `ALTER`，捕获 `sqlite3.OperationalError`（列已存在）静默忽略；历史行奖励列保持 NULL/空串，**不回填**。
- 视图与只读 SQL：
  - `v_screen_latest_done`、`v_screen_candidate_daily` 使用 `d.*`，新列自动带出，无需改 SQL。
  - `v_screen_persistent_10d`：`base`/`latest` 为 `SELECT d.*` / `b.*`，新列自动带出；最终显式 SELECT 清单补：

    ```sql
    l.rewards_daily_rate AS latest_rewards_daily_rate,
    l.rewards_asset      AS latest_rewards_asset
    ```

  - `_SQL_PERSISTENT_HIT`：`base` 为 `SELECT *`，无需改 CTE；最终 SELECT 补上面两列（`l` 别名对齐 `last_run_date` 命中行）。
  - `_SQL_PERSISTENT_STREAK`：`eligible` CTE 是**显式列清单**（非 `*`），需先把 `rewards_daily_rate, rewards_asset` 加进 `eligible`；`latest_in_window` 经 `e.*` 带出后，最终 SELECT 才能引用 `l.rewards_*`。
- **稳定程度不新建 CTE**：复用现有 `max_streak` 岛屿算法结果（连续命中天数，缺天断档重置）；准入线 ≥30 后连续上榜即连续有奖励。⑦-b 累计路径当前不计算连续天数，若后续需要再补通用 streak（P2），不冠名奖励。

**为什么准入线 ≥30 之后仍存速率数值（不冗余）**：准入线只决定「该行是否落库」，落库行速率分布在 [30, +∞)（样本 30/100/300/600/1000 量级），排序、低/中/高分档、读侧 ≥300 二次筛选都必须依赖连续数值；`rewards_asset` 区分 USDC.e / pUSD，存在性无法表达。本期**不新增** `has_reward` 之类布尔列；NULL 兜底仅面向历史行与脏数据。
- 本期**不加奖励列索引**；后续若按奖励额筛选/分档再补。
- `prune_screen` / run 表 / 事务边界 / 白名单不变。

### 4.5 存储门面 `framework/store/market_screen_store.py`

`_map_row()` 补映射：

```text
latest_rewards_daily_rate = row.get("latest_rewards_daily_rate"),  # None 透传
latest_rewards_asset       = str(row.get("latest_rewards_asset") or ""),
```

### 4.6 配置 `src/settings.py` / `config/config.yaml.example`

新增 `market_screen.reward` 子节（准入线 + 分档阈值），不新增功能开关：

```yaml
reward:
  min_daily_rate: 30.0      # 准入：每日奖励速率下限（奖励代币/天），低于此值剔除
  tiers:
    low_min: 30.0           # 低档 [30, 100)
    mid_min: 100.0          # 中档 [100, 300)
    high_min: 300.0         # 高档 [300, +∞)
```

- 解析取法对齐现有子节：`rw = raw.get("reward") if isinstance(raw.get("reward"), dict) else {}`；`QueryDefaults` 构造处同步补 `min_reward_daily_rate`。
- `MarketScreenConfig` 拍平新增字段：`reward_min_daily_rate: float`、`reward_tier_low_min / reward_tier_mid_min / reward_tier_high_min: float`。
- 解析校验（fail-soft，对齐现有 `volume` / `horizon_prob` 风格）：
  - `min_daily_rate` 非负；非法回退 30.0 并告警。
  - 阈值须满足 `0 <= low_min <= mid_min <= high_min`；非法回退默认 `30 / 100 / 300` 并告警。
  - 若 `min_daily_rate > low_min`，提示低档区间部分不可达（仅告警，不阻断）。
- 分档阈值只供 §4.2 的 `classify_reward_tier` 在查询/展示层使用，本期不因此新增落库列。
- `QueryDefaults` 追加读侧奖励下限 `min_reward_daily_rate: float = 0.0`（0 = 不额外限；采集准入线 30 已在落库前剔除，此值是分析层二次门槛，可高于 30）；解析走 `_coerce_float(..., 0.0)`；`config.yaml.example` 的 `query_defaults` 节补 `min_reward_daily_rate: 0.0`。

### 4.7 不需要改动的层

- `framework/sources/market_lister.py`：原始条目透传，奖励字段随列表响应一起回来。
- 三档概率判定锚点与 `evaluate_presets`：不引入奖励速率作为概率档位输入。

### 4.8 只读查询链路（持续性分析，`query_ms.py` 模板）

目标：在现有持续性结果上叠加「**奖励高、结束时间长、某方向概率低**」三条件。奖励 / 概率 / 剩余天数均取窗口内**最新一轮**行（SQL 别名 `l`），与写侧「只带最新值、不跨天聚合」口径一致。

**三条件与现有参数映射**

| 分析要求 | 查询参数 | 落点 | 现状 |
|---|---|---|---|
| 奖励高 | `min_reward_daily_rate`（显式下限）或 `reward_tier=low/mid/high`（按 `tiers` 取下限） | 门面 → storage `WHERE l.rewards_daily_rate >= ?` | 新增 |
| 结束时间长 | `min_remaining_days` | 现有 `WHERE l.remaining_days >= ?`；排序补 `remaining_days DESC` 次键 | 参数已有，排序补强 |
| 某方向概率低 | `prob_side=minority/yes` + `prob_max` | 现有 `WHERE l.{prob_col} <= ?` | 已有 |

**模型 DTO**：`PersistentCandidateRow` 的 `latest_rewards_daily_rate / latest_rewards_asset` 见 §4.1；不新增档位字段，档位由 CLI/展示层调 `classify_reward_tier` 现算。稳定程度复用现有 `max_streak`，不进 WHERE 与 ORDER BY。

**存储 `src/storage.py`**：

- `_SQL_PERSISTENT_HIT`：最终 SELECT 补 `l.rewards_daily_rate AS latest_rewards_daily_rate, l.rewards_asset AS latest_rewards_asset`；WHERE 补 `AND l.rewards_daily_rate >= ?`；ORDER BY 按排序模式注入（见下）。
- `_SQL_PERSISTENT_STREAK`：`eligible` CTE 显式列清单补 `rewards_daily_rate, rewards_asset`（`latest_in_window` 经 `e.*` 带出）；最终 SELECT 补两列；WHERE 补 `AND l.rewards_daily_rate >= ?`。
- `query_persistent_rows` 新增形参 `min_reward_daily_rate: float = 0.0`、`sort_mode: str = "persistence"`；奖励下限追加到两条 SQL 占位符末尾；排序片段按白名单注入（对齐现有 `prob_col` 白名单 `.format` 先例，不拼接用户自由文本）。
- **`min_reward_daily_rate = 0` 表示「不额外限奖励」**：此时 SQL 不应追加 `AND l.rewards_daily_rate >= 0`（SQLite 中 `NULL >= 0` 不成立，会把历史 NULL 行误剔）；门面/存储在值为 0 时跳过奖励 WHERE 条件、不绑定该占位符。仅当 `> 0` 时才追加 `AND l.rewards_daily_rate >= ?`。

排序模式（白名单）：

- `persistence`（现状默认，不改既有行为）：HIT `hit_days DESC, min_volume_24h DESC, minority_prob ASC`；STREAK `max_streak DESC, volume_24h DESC`。
- `reward`（本次分析用）：`latest_rewards_daily_rate DESC, remaining_days DESC, {prob_col} ASC, hit_days/max_streak DESC`。

**门面 `framework/store/market_screen_store.py`**：

- `query_persistent_candidates` 新增可选形参 `min_reward_daily_rate`、`reward_tier`、`sort_mode`。
- `min_reward_daily_rate` 走 `_coerce_float(..., self._default("min_reward_daily_rate", 0.0), 0.0, ...)`。
- `reward_tier` ∈ {`low`, `mid`, `high`}：按 `cfg.reward_tier_*` 阈值映射成下限（high→high_min，mid→mid_min，low→low_min）；显式 `min_reward_daily_rate` 优先；非法档位告警并回退到 0（不额外限）。
- 读取 `cfg` 奖励新字段一律 `getattr(cfg, "reward_...", 默认值)` 兜底，避免旧配置/测试桩缺字段时 `AttributeError`；`sort_mode` 白名单 `persistence` / `reward`，非法回退 `persistence`。
- `_map_row()` 补 `latest_rewards_daily_rate`（None 透传）/ `latest_rewards_asset`（空串兜底）；`max_streak` 映射维持现状。

**CLI 模板 `query_ms.py`**（仓库外临时工具，编码时同步）：

- 新参数：`--min-reward FLOAT`、`--reward-tier {low,mid,high}`、`--sort {persistence,reward}`（分析默认 `reward`）；稳定程度沿用现有 `streak` 列，无独立参数。
- 输出表头补 `reward`、`asset`、`tier`；档位用 `classify_reward_tier(...)` 按配置阈值现算；`streak`（`max_streak`）作为连续上榜/拿奖励天数展示。
- 示例：`python query_ms.py --reward-tier high --min-remaining-days 31 --prob-side minority --prob-max 0.15 --sort reward`。

**读侧边界**：

- 奖励下限只判最新一轮速率；历史上有奖励但最新一轮掉出档位的市场，按最新值剔除（与持续性口径一致）。
- 旧库历史行奖励列为 NULL，`NULL >= ?` 在 SQLite 中不成立，会被奖励下限过滤掉；不回填。
- 本期不为奖励列加索引；窗口内候选量级可接受全表过滤。

## 5. 测试计划（pytest）

| 文件 | 用例 |
|---|---|
| `test_market_screen_parsing.py` | ① `clobRewards` 为 list；② 为字符串化 JSON；③ 缺失/空数组 → 速率 NULL、代币空串；④ `rewardsDailyRate` 非数值/NaN/Infinity/布尔 → NULL；⑤ USDC.e / pUSD 地址符号映射；⑥ 未知地址兜底存小写；⑦ 多条取首条；⑧ 奖励脏数据在解析层不导致整行被剔除（剔除发生在 usecase）；⑨ `classify_reward_tier` 边界：29.9/30/100/300/300.1 与 NULL/非有限 → None/low/mid/high 档位正确，左闭右开 |
| `test_market_screen_usecase.py` | ① 无 `clobRewards` / 速率 NULL / 速率 0 / 负数 / 低于 `min_daily_rate`(30) → 一律不落库；② 复合排序：速率高的排前，速率相同按成交额排前；③ 复合排序后 TopN 截断位置正确；④ 有奖励样本奖励字段透传到落库行；⑤ 配置 `min_daily_rate` 调整后准入边界跟随配置 |
| `test_market_screen_store.py` | ① 新列落库后 `list_screen_daily` 回读一致；② 持久化 hit/streak 结果 DTO 带出 `latest_rewards_*`；③ 重复 `init()` 旧库 `ALTER` 幂等不报错；④ `min_reward_daily_rate` 下限过滤生效（低于下限/NULL 不出现在结果）；⑤ `reward_tier` 映射到对应阈值下限；⑥ `sort_mode=reward` 结果按奖励 DESC、剩余天数 DESC、概率 ASC 排列；⑦ 非法 `sort_mode`/档位告警回退；⑧ 稳定程度沿用现有 `max_streak` 映射，不新增奖励连续天数字段 |
| `test_market_screen_schedule.py` | ① `reward` 缺省回退默认 30/100/300；② `min_daily_rate` 为负/非法回退 30.0；③ 阈值非升序回退默认并告警；④ `query_defaults.min_reward_daily_rate` 缺省 0.0、非法回退 0.0 |
| 夹具 | **`_good_raw()` / usecase `_raw()` 默认注入一条达标奖励**（`clobRewards` 速率 100、USDC.e），否则准入门槛会把旧样本全部剔除、基线变红；`_row()` 落库行按需要带奖励字段。DTO 新字段带默认值，不传时回落 NULL/空串；现有用例断言按新排序口径更新 |

本地/云主机回归：`python -m pytest tests/test_market_screen_*.py -q`；全量回归另跑 `python -m pytest -q`。

## 6. 部署与回滚（EC2）

1. 上线前备份 `data/monitor.db`（WAL 库同时留意 `-wal` / `-shm` 文件）。
2. 代码部署后首次启动 `init()` 自动 `ALTER` 加列；历史行奖励列为 NULL/空串，不回填。
3. 验证：重启后跑一轮采集，确认低于准入线（<30/天）与无奖励市场未入库；`market_screen_daily` 新列有值；采集排序为奖励优先；持续性结果带最新日奖励列，可按奖励下限/档位、剩余天数、方向概率叠加筛选；分档在查询层按 30/100/300 归类正确；稳定程度沿用现有 `max_streak` 缺天断档重置；旧库重复启动不报错。
4. 回滚：代码回退旧版本即可；新列留在库中不影响旧版读写（旧版不 SELECT 新列）。

## 7. 口径闭环与风险

### 7.1 已定阈值（2026-11-11 需求方确认）

- 准入线：`reward.min_daily_rate = 30.0`（奖励代币/天），低于此值剔除。
- 分档（左闭右开，查询/展示层现算，不落库）：
  - 低档 `low`：`[30, 100)`
  - 中档 `mid`：`[100, 300)`
  - 高档 `high`：`[300, +∞)`
- 阈值进 `reward.tiers` 配置可调；后续如调整档位边界，只改配置与 §4.2 纯函数入参，不动落库结构。

### 7.2 风险与边界

1. 本沙箱出口无法直连 Gamma/CLOB，字段分布需在 EC2 上线首轮实跑复核；若出现第三种奖励代币，按 §4.2 映射表补符号，未命中自动落地址兜底。
2. 复合排序与分档跨 USDC.e / pUSD 按名义数值比较；两者 1:1 美元背书，若后续出现非美元奖励代币，需重新评估归一规则。
3. 分档标签本期不落库；若后续持续性列表/推送需要直接展示档位，再在查询层调用 `classify_reward_tier` 补充，不需要改表结构。
4. 稳定程度复用 `max_streak`（连续上榜天数），不单独衡量奖励金额水平是否稳定；若后续要「连续维持某档速率不掉档」，再另立按速率门槛的连续指标（P2）。历史行奖励为 NULL 不影响现有 streak 口径。
5. **采集覆盖边界**：Gamma 翻页仍按 `volume24hr` 倒序拉取（`page_limit × max_pages` 上限内），奖励准入与奖励优先重排只在已拉回的样本内生效；奖励高但 24h 成交额低、翻页范围外的市场不会被采集到。若后续要覆盖「低量高奖励」市场，需调整翻页排序/范围（P2），本期不改采集口径。
6. 当前工作树存在其他未提交改动；本次改造仅触碰 §4 列出的文件，不顺带重构既有逻辑。

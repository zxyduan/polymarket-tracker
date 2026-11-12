# Market Screen 盘口奖励字段 开发文档（dev v1）

> 关联设计：`doc/market-screen-reward-field-design.md`（v3，口径已拍板）。本文是落地实施说明，按层给出改动点、接口签名、SQL/配置要点、验收与测试。
> 基线：`python -m pytest tests/test_market_screen_*.py -q` 当前 **46 passed**；编码前以工作树代码为准。
> 分层红线（`ARCHITECTURE.md`）：`features/` 禁止 import `src/`；`framework/models/` 仅标准库；SQL 全部 `?` 参数绑定；只读对象走白名单。
> 状态：T1–T7 已落地；特性相关 `pytest tests/test_market_screen_*.py -q` 83 passed，全量 `pytest -q` 275 passed。本文不包含已提交的远程操作（不 clone/commit/push/PR）。

## 1. 需求概述

### 1.1 背景

现有 `market_screen` 链路每日拉取 24h 成交额榜市场并落 SQLite。本次在该链路上纳入**盘口做市奖励**信息，使候选市场可按奖励大小参与准入、排序与后续分析筛选。

### 1.2 目标场景

分析侧筛选三类条件叠加的市场：

1. **奖励高**：每日奖励速率高（可按低/中/高档位或显式下限过滤）；
2. **结束时间长**：剩余到期天数足够；
3. **某方向当前概率低**：指定方向（小侧 / YES）概率低于上限。

### 1.3 交付物

- 写侧：Gamma 列表条目解析奖励字段 → 准入过滤 → 复合排序 → 落明细表；
- 读侧：持续性查询带出最新一轮奖励速率与代币标识，支持奖励下限/档位过滤与奖励优先排序；
- 配置：准入线、分档阈值、读侧奖励下限可调；
- 测试：解析 / 编排 / 存储门面 / 配置解析四层单测补齐，基线回归保持全绿。

## 2. 范围边界

### 2.1 本期实现（In Scope）

| 类别 | 内容 |
|---|---|
| 落库字段 | `rewards_daily_rate`（REAL）、`rewards_asset`（TEXT） |
| 准入 | 速率低于 `reward.min_daily_rate`（默认 30.0）剔除 |
| 采集排序 | 复合倒序 `rewards_daily_rate DESC, volume_24h DESC`，之后 TopN |
| 分档 | 纯函数查询/展示层现算：低 [30,100)、中 [100,300)、高 [300,+∞)，不落标签列 |
| 读侧筛选 | 奖励下限 / 档位、剩余天数、方向概率叠加；两种排序模式 |
| 稳定程度 | 复用现有 `max_streak`，不新建奖励连续天数字段 |
| 配置 | `market_screen.reward` 子节 + `query_defaults.min_reward_daily_rate` |

### 2.2 本期不做（Out of Scope / P2）

- `rewardsAmount`（奖励池总额，实测全 0，不落）；
- `rewardsMinSize` / `rewardsMaxSpread`（做市资格门槛，不落）；
- 奖励分档标签入库列、`has_reward` 布尔列；
- 奖励列索引；奖励速率跨代币汇率归一；
- 「连续维持某档速率不掉档」的速率稳定性指标；
- 采集翻页排序/范围调整（低量高奖励市场可能在翻页范围外，见 §10.2）；
- 运行元信息增加「无奖励剔除计数」（P1 可选，本期默认不加）。

## 3. 口径与数据来源

### 3.1 取数位置（不新增 HTTP 请求）

复用现有 Gamma 列表响应，奖励字段随条目返回：

```
GET https://gamma-api.polymarket.com/markets
  ?active=true&closed=false&order=volume24hr&ascending=false&limit=100&offset=…
```

奖励信息在条目顶层 `clobRewards` 数组；CLOB 当前奖励接口 `/rewards/markets/current` 的 `rewards_config[]` 可交叉验证。采集只走 Gamma。

### 3.2 字段映射

| 落库列 | Gamma 来源（采集） | CLOB 对应（交叉验证） | 口径 |
|---|---|---|---|
| `rewards_daily_rate` | `clobRewards[0].rewardsDailyRate` | `rewards_config[0].rate_per_day` | 每日奖励数量（奖励代币/天）；None/布尔/非数值/NaN/Infinity → NULL，准入阶段剔除 |
| `rewards_asset` | `clobRewards[0].assetAddress` | `rewards_config[0].asset_address` | 已知地址映射符号；未知地址兜底存小写原始地址 |

> 字段名经官方 `polymarket` SDK 模型核对：Gamma `ClobReward` 别名 `rewardsDailyRate/assetAddress/rewardsAmount`，顶层同级 `rewardsMinSize/rewardsMaxSpread`；CLOB `CurrentReward.rewards_config[]` 为 `rate_per_day/asset_address`。

### 3.3 代币映射（地址统一小写键）

| 地址（小写） | 符号 |
|---|---|
| `0x2791bca1f2de4661ed88a30c99a7a9449aa84174` | `USDC.e` |
| `0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb` | `pUSD` |

未命中映射的地址：原样存小写地址，不存空串；后续出现第三种代币再补映射表。

### 3.4 阈值（已拍板）

| 项 | 值 | 说明 |
|---|---|---|
| 准入线 `reward.min_daily_rate` | `30.0`（奖励代币/天） | 低于此值 / NULL / 非数值剔除 |
| 低档 `low` | `[30, 100)` | 左闭右开 |
| 中档 `mid` | `[100, 300)` | 左闭右开 |
| 高档 `high` | `[300, +∞)` | — |
| 读侧 `query_defaults.min_reward_daily_rate` | `0.0` | 0 = 不额外限；分析层二次门槛，可高于 30 |

### 3.5 口径红线

- `rewardsDailyRate` 是**每日速率**，不是累计已发奖励、不是池子总额；列名不得叫 `reward_amount`。
- 奖励参与准入与排序，但**不改变**现有 strict/balanced/loose 概率×到期时间三档判定。
- 持续性查询只带**最新一轮**奖励值，不做跨天奖励汇总。
- 两条 `clobRewards` 实测均为单条；多条当前取首条（P2 复核：必要时改为取速率最大者）。

## 4. 改动清单总览

| 编号 | 层 | 文件 | 改动类型 |
|---|---|---|---|
| T1 | 模型 | `framework/models/market_screen.py` | DTO 追加字段 |
| T2 | 解析 | `framework/parsing/market_screen_parsing.py` | 映射常量、奖励抽取纯函数、分档纯函数、`parse_market` 接入 |
| T3 | 编排 | `features/market_screen/usecase.py` | 奖励准入、复合排序 |
| T4 | 配置 | `src/settings.py`、`config/config.yaml.example` | `reward` 子节、`QueryDefaults` 新字段、样例 |
| T5 | 存储 | `src/storage.py` | DDL、列清单、参数、迁移、视图、两条持久化 SQL |
| T6 | 门面 | `framework/store/market_screen_store.py` | 查询新参数、档位映射、排序白名单、行映射 |
| T7 | CLI 模板 | `query_ms.py`（仓库外临时工具，编码时同步） | 新参数、奖励/档位列输出 |
| — | 不动 | `framework/sources/market_lister.py`、三档判定锚点/`evaluate_presets`、`prune_screen`、run 表、白名单 | 无改动 |

## 5. 详细开发任务

### T1 模型 `framework/models/market_screen.py`

`CandidateMarket` 末尾追加（frozen dataclass，带默认值，不破坏现有关键字构造与 `dataclasses.replace`）：

```python
rewards_daily_rate: float | None = None
rewards_asset: str = ""
```

`PersistentCandidateRow` 末尾追加：

```python
latest_rewards_daily_rate: float | None = None
latest_rewards_asset: str = ""
```

- 不新增 `reward_streak` / 档位标签 / 布尔标志字段；稳定程度复用现有 `max_streak`。
- 仅标准库 import，不引入业务包。

### T2 解析 `framework/parsing/market_screen_parsing.py`

**T2.1 代币映射常量**（模块级，地址小写键）：

```python
_REWARD_ASSET_SYMBOLS: dict[str, str] = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",
}
```

**T2.2 奖励抽取纯函数**（无 IO，不做准入剔除）：

```python
def _parse_rewards(raw: dict[str, object]) -> tuple[float | None, str]:
    """从 clobRewards 提取每日奖励速率与代币标识；缺失/脏数据返回 (None, "")。"""
```

规则：

1. `clobRewards` 走现有 `_json_list()`，兼容 list 与字符串化 JSON 数组；空/非 list → `(None, "")`；
2. 多条取首条；首条须为 dict，否则 `(None, "")`；
3. `rewardsDailyRate` 走现有 `_to_float()`，布尔/非数值/NaN/Infinity → None；
4. `assetAddress` strip → lower；命中映射取符号，未命中保留小写地址；空地址 → 空串；
5. 任何解析异常收敛为 `(None, "")`，不抛异常、不剔除主行。

**T2.3 `parse_market()` 接入**：组装 `CandidateMarket` 时填入 `rewards_daily_rate`、`rewards_asset`（调用 `_parse_rewards(raw)`）。

**T2.4 分档纯函数**（供查询/展示层现算，不入库，不读全局配置）：

```python
def classify_reward_tier(
    rate: float | None,
    *,
    low_min: float,
    mid_min: float,
    high_min: float,
) -> str | None:
    """返回 "high" / "mid" / "low"；None 或低于 low_min 返回 None。左闭右开。"""
```

规则：

- `rate is None` 或非有限 → None；
- `rate >= high_min` → `"high"`；`rate >= mid_min` → `"mid"`；`rate >= low_min` → `"low"`；否则 None；
- 阈值合法性由配置解析层校验，本函数不重复校验。

### T3 编排 `features/market_screen/usecase.py`

在 `_build_rows()` 内调整顺序：

1. **奖励准入（先于排序）**：`parse_market` 返回后，
   `rewards_daily_rate is None or rewards_daily_rate < getattr(cfg, "reward_min_daily_rate", 30.0)`
   的候选直接 `continue`，不进入后续成交额阈值/排序/档位流程。
2. **成交额阈值**：维持现有 `volume_24h < cfg.min_value` 过滤（与奖励准入取交集，顺序可互换）。
3. **复合排序**：现有按 `volume_24h` 单键倒序改为：

   ```python
   parsed.sort(
       key=lambda r: (r.rewards_daily_rate or 0.0, r.volume_24h),
       reverse=True,
   )
   ```

   准入已剔除 None 行，`or 0.0` 仅防御兜底。
4. **TopN 截断**：复合排序后按 `cfg.top_n` 截断，位置不变。
5. 三档概率×到期时间判定、`persist_non_passed` 语义不变；奖励字段随 `dataclasses.replace(base, …)` 自动保留。

### T4 配置 `src/settings.py` / `config/config.yaml.example`

**T4.1 YAML 样例**（`market_screen` 节内新增）：

```yaml
reward:
  min_daily_rate: 30.0      # 准入：每日奖励速率下限（奖励代币/天），低于此值剔除
  tiers:
    low_min: 30.0           # 低档 [30, 100)
    mid_min: 100.0          # 中档 [100, 300)
    high_min: 300.0         # 高档 [300, +∞)
```

`query_defaults` 节补：

```yaml
min_reward_daily_rate: 0.0  # 读侧奖励下限（0=不额外限）
```

**T4.2 `QueryDefaults`** 追加字段：

```python
min_reward_daily_rate: float = 0.0
```

**T4.3 `MarketScreenConfig`** 拍平追加：

```python
reward_min_daily_rate: float
reward_tier_low_min: float
reward_tier_mid_min: float
reward_tier_high_min: float
```

**T4.4 解析校验**（fail-soft，对齐现有 `volume` / `horizon_prob` 风格）：

- 取子节：`rw = raw.get("reward") if isinstance(raw.get("reward"), dict) else {}`；
- `min_daily_rate` 走 `_coerce_float(..., 30.0, ...)`，须非负；非法回退 30.0 并告警；
- 三档阈值走 `_coerce_float`，须满足 `0 <= low_min <= mid_min <= high_min`；非法回退默认 `30 / 100 / 300` 并告警；
- 若 `min_daily_rate > low_min`，追加告警「低档区间部分不可达」，不阻断；
- `query_defaults.min_reward_daily_rate` 走 `_coerce_float(..., 0.0, 0.0, ...)`；
- `QueryDefaults` 构造处同步传入新字段；返回 `MarketScreenConfig(...)` 补新字段。

### T5 存储 `src/storage.py`

**T5.1 明细表 DDL**（新库 `SCHEMA`）在 `market_screen_daily` 追加：

```sql
rewards_daily_rate REAL,
rewards_asset      TEXT NOT NULL DEFAULT '',
```

**T5.2 列清单与写入参数**：

- `_SCREEN_DAILY_COLUMNS` 末尾追加 `rewards_daily_rate, rewards_asset`；
- `_daily_params()` 末尾追加 `row.rewards_daily_rate, row.rewards_asset`；
- `replace_screen_day` INSERT 占位符由 23 增至 25；**占位符数量改为由 `len(_SCREEN_DAILY_COLUMNS.split(","))` 推导**，不再硬编码数字。

**T5.3 旧库迁移**：仿 `volume_shares` 先例，在 `init()` 内补两条 `ALTER TABLE market_screen_daily ADD COLUMN ...`，捕获 `sqlite3.OperationalError`（列已存在）静默忽略；历史行奖励列保持 NULL/空串，不回填。

**T5.4 视图**：

- `v_screen_latest_done`、`v_screen_candidate_daily` 用 `d.*`，新列自动带出，不改 SQL；
- `v_screen_persistent_10d`：`base`/`latest` 为 `d.*` / `b.*` 自动带出；最终显式 SELECT 清单补：

  ```sql
  l.rewards_daily_rate AS latest_rewards_daily_rate,
  l.rewards_asset      AS latest_rewards_asset
  ```

**T5.5 持久化 SQL**：

- `_SQL_PERSISTENT_HIT`：最终 SELECT 补上述两列；奖励下限条件 `AND l.rewards_daily_rate >= ?`（仅当下限 >0 时追加）。
- `_SQL_PERSISTENT_STREAK`：`eligible` CTE 显式列清单补 `rewards_daily_rate, rewards_asset`（`latest_in_window` 经 `e.*` 带出）；最终 SELECT 补两列；WHERE 补奖励下限条件（同上）。
- `query_persistent_rows` 新增形参 `min_reward_daily_rate: float = 0.0`、`sort_mode: str = "persistence"`；新参数均带默认值，现有调用方与存量测试不传也保持原行为。奖励下限占位符追加到各自模板参数末尾；**参数元组按条件构造**：仅当奖励下限 `> 0` 时才在元组末尾追加该值，保证占位符数量与 SQL 片段一一对应。

**T5.6 排序模式（白名单注入，对齐现有 `prob_col` `.format` 先例，不拼接自由文本）**：

- `persistence`（默认，不改既有行为）：
  - HIT：`hit_days DESC, min_volume_24h DESC, minority_prob ASC`
  - STREAK：`max_streak DESC, volume_24h DESC`
- `reward`（分析用）：`latest_rewards_daily_rate DESC, remaining_days DESC, {prob_col} ASC, hit_days/max_streak DESC`

**T5.7 奖励下限为 0 的处理**：`min_reward_daily_rate == 0.0` 表示不额外限，**不追加**奖励 WHERE、不绑定该占位符；避免 `NULL >= 0` 在 SQLite 中把历史 NULL 行误剔。仅 `> 0` 时追加 `AND l.rewards_daily_rate >= ?`。

**T5.8 其他不变**：本期不加奖励列索引；`prune_screen`、run 表、事务边界、`_SCREEN_READABLE` 白名单不变。

### T6 门面 `framework/store/market_screen_store.py`

`query_persistent_candidates` 新增可选形参：`min_reward_daily_rate`、`reward_tier`、`sort_mode`。

- `min_reward_daily_rate`：走 `_coerce_float(..., self._default("min_reward_daily_rate", 0.0), 0.0, ...)`。
- `reward_tier` ∈ {`low`, `mid`, `high`}：按 `cfg.reward_tier_*` 映射下限（high→high_min，mid→mid_min，low→low_min）；显式 `min_reward_daily_rate` 优先；非法档位告警并回退到 0（不限）。
- 读取 `cfg` 奖励新字段一律 `getattr(cfg, "reward_...", 默认值)` 兜底，防旧配置/测试桩缺字段。
- `sort_mode` 白名单 `persistence` / `reward`，非法回退 `persistence` 并告警。
- 收敛后的参数透传 `storage.query_persistent_rows(...)`。
- `_map_row()` 补：

  ```text
  latest_rewards_daily_rate = row.get("latest_rewards_daily_rate"),  # None 透传
  latest_rewards_asset       = str(row.get("latest_rewards_asset") or ""),
  ```

  `max_streak` 映射维持现状。

### T7 CLI 模板 `query_ms.py`（仓库外临时工具，编码时同步）

- 新参数：`--min-reward FLOAT`、`--reward-tier {low,mid,high}`、`--sort {persistence,reward}`（分析默认 `reward`）；稳定程度沿用现有 `streak` 列，无独立参数。
- 输出表头补 `reward`、`asset`、`tier`；档位调 `classify_reward_tier(...)` 按配置阈值现算。
- 示例：

  ```bash
  python query_ms.py --reward-tier high --min-remaining-days 31 \
      --prob-side minority --prob-max 0.15 --sort reward
  ```

## 6. 配置项一览

| 配置键 | 类型 | 默认 | 校验 / 回退 | 用途 |
|---|---|---|---|---|
| `market_screen.reward.min_daily_rate` | float | `30.0` | 非负；非法回退 30.0 告警 | 采集准入线 |
| `market_screen.reward.tiers.low_min` | float | `30.0` | 三阈值非降序；非法回退 30/100/300 告警 | 低档下限 |
| `market_screen.reward.tiers.mid_min` | float | `100.0` | 同上 | 中档下限 |
| `market_screen.reward.tiers.high_min` | float | `300.0` | 同上 | 高档下限 |
| `market_screen.query_defaults.min_reward_daily_rate` | float | `0.0` | `_coerce_float` 下限 0.0 | 读侧二次奖励下限，0 不限 |

## 7. 测试计划（pytest）

### 7.1 夹具调整（前置）

- `test_market_screen_parsing.py` 的 `_good_raw()`、`test_market_screen_usecase.py` 的 `_raw()` **默认注入一条达标奖励**（`clobRewards` 速率 100、USDC.e），否则准入门槛会把旧样本全部剔除；`_row()` 落库行按需要带奖励字段。
- DTO 新字段带默认值，不传时回落 NULL/空串；现有断言按新排序口径更新。

### 7.2 用例清单

| 文件 | 用例 |
|---|---|
| `test_market_screen_parsing.py` | ① `clobRewards` 为 list；② 字符串化 JSON；③ 缺失/空数组 → 速率 NULL、代币空串；④ 速率非数值/NaN/Infinity/布尔 → NULL；⑤ USDC.e / pUSD 符号映射；⑥ 未知地址兜底小写；⑦ 多条取首条；⑧ 奖励脏数据不导致整行被剔除；⑨ `classify_reward_tier` 边界 29.9/30/100/300/300.1 与 NULL/非有限 → None/low/mid/high，左闭右开 |
| `test_market_screen_usecase.py` | ① 无 `clobRewards` / NULL / 0 / 负数 / 低于 30 → 不落库；② 复合排序：速率高在前，同速率按成交额在前；③ 复合排序后 TopN 截断位置正确；④ 奖励字段透传到落库行；⑤ 调整 `min_daily_rate` 后准入边界跟随配置 |
| `test_market_screen_store.py` | ① 新列落库后 `list_screen_daily` 回读一致；② hit/streak 结果 DTO 带出 `latest_rewards_*`；③ 重复 `init()` 旧库 `ALTER` 幂等不报错；④ 奖励下限过滤生效（低于下限/NULL 不出现）；⑤ `reward_tier` 映射到对应阈值下限；⑥ `sort_mode=reward` 按奖励 DESC、剩余天数 DESC、概率 ASC 排列；⑦ 非法 `sort_mode`/档位告警回退；⑧ 稳定程度沿用 `max_streak`，无奖励连续天数字段 |
| `test_market_screen_schedule.py` | ① `reward` 缺省回退 30/100/300；② `min_daily_rate` 为负/非法回退 30.0；③ 阈值非升序回退默认并告警；④ `query_defaults.min_reward_daily_rate` 缺省 0.0、非法回退 0.0 |

### 7.3 回归命令

```bash
python -m pytest tests/test_market_screen_*.py -q   # 特性相关
python -m pytest -q                                  # 全量
```

## 8. 部署、验证与回滚（EC2）

1. 上线前备份 `data/monitor.db`（WAL 库同时留意 `-wal` / `-shm`）。
2. 代码部署后首次启动 `init()` 自动 `ALTER` 加列；历史行奖励列 NULL/空串，不回填。
3. 上线首轮实跑复核：
   - 低于准入线（<30/天）与无奖励市场未入库；
   - 明细表新列有值，采集排序奖励优先；
   - 持续性结果带最新日奖励列，可按奖励下限/档位、剩余天数、方向概率叠加筛选；
   - 分档按 30/100/300 归类正确；`max_streak` 缺天断档重置符合预期；
   - 旧库重复启动不报错。
4. 回滚：代码回退旧版本即可；新列留在库中不影响旧版读写（旧版不 SELECT 新列）。

## 9. 完成定义（DoD）

- [x] T1–T6 代码完成，类型标注齐全，无 `eval/exec/pickle`，SQL 全 `?` 绑定；
- [x] T7 CLI 模板同步新参数与奖励/档位列；
- [x] §7.2 用例全部通过，特性相关（83）与全量（275）回归绿；
- [x] `config/config.yaml.example` 含 `reward` 与 `min_reward_daily_rate` 样例；
- [x] 旧库 `ALTER` 幂等，重复 `init()` 不报错；
- [x] 未触碰 §2.2 列出的范围外文件与既有逻辑。

## 10. 风险与待复核项

### 10.1 环境与字段复核

- 本沙箱出口走代理，Gamma/CLOB 不可直连；字段名、代币分布、多条形态需在 EC2 首轮实跑复核。
- 若出现第三种奖励代币，补 `_REWARD_ASSET_SYMBOLS` 映射；未命中自动落地址兜底。

### 10.2 已知边界

- **采集覆盖**：翻页仍按 `volume24hr` 倒序，奖励准入与重排只在已拉回样本内生效；低量高奖励市场可能在翻页范围外（P2 再议）。
- **跨代币比较**：USDC.e / pUSD 均 1:1 美元背书，按名义数值横比；出现非美元代币需重估归一规则。
- **历史 NULL**：旧库历史行奖励为 NULL，奖励下限会过滤掉；不回填。
- **速率稳定性**：`max_streak` 衡量连续上榜，不衡量奖励金额水平是否稳定；需要「连续不掉档」指标时另立（P2）。

### 10.3 多条奖励记录

当前取 `clobRewards` 首条；上线抽样若发现多条不同速率/代币，改为取 `rewardsDailyRate` 最大者。

## 附录 A. 开发文档复核记录

> 编写后自检：对照设计文档 v3（`market-screen-reward-field-design.md`）与 vm 工作树真实代码逐条核对。复核时间 2026-11-11，基线 `python -m pytest tests/test_market_screen_*.py -q` = 46 passed。

### A.1 逐条核对结果

| # | 核对项 | 代码现状 | 开发文档结论 | 结果 |
|---|---|---|---|---|
| 1 | `CandidateMarket` 字段数与构造方式 | 23 字段，frozen，全关键字构造；仅 parsing 一处生产构造 | T1 末尾追加两个带默认值字段，不破坏现有构造与 `replace` | 一致 |
| 2 | `PersistentCandidateRow` 构造点 | 全仓库仅 `store._map_row` 一处关键字构造，`max_streak` 为末尾可选字段 | T1 追加两个可选字段在末尾；T6 `_map_row` 补映射 | 一致 |
| 3 | 解析安全工具 | `_json_list` 兼容 list/字符串 JSON/逗号串；`_to_float` 拒 None/布尔/非数值/NaN/Inf | T2.2 复用两者，不新写容错 | 一致 |
| 4 | `parse_market` 剔除门 | 硬脏数据返回 None；奖励脏数据不应导致整行剔除 | T2.2 奖励解析收敛为 `(None, "")`，不进 None 门；准入剔除在 usecase | 一致 |
| 5 | usecase 现有顺序 | parse → 量阈值 → 量倒序 → TopN → 三档 `replace` | T3 并列加奖励准入，排序键改复合，TopN 位置不变 | 一致 |
| 6 | 配置子节取法 | `vol/hor/qd_raw/notify` 均 `isinstance(..., dict)` 兜底空 dict | T4.4 `reward` 子节同模式取法 | 一致 |
| 7 | `QueryDefaults` / `MarketScreenConfig` | 均 frozen dataclass；构造处集中在 `resolve_market_screen_config` | T4.2/T4.3 追加字段，构造处同步传入 | 一致 |
| 8 | 明细表列清单与写入参数 | `_SCREEN_DAILY_COLUMNS` 23 列；`_daily_params` 23 值；占位符硬编码 `range(23)` | T5.2 末尾追加两列两值，占位符改为按列清单长度推导 | 一致（含现状硬编码点已标注） |
| 9 | 旧库迁移先例 | `volume_shares` 用 `try ALTER ... ADD COLUMN` 捕获 `OperationalError` 忽略 | T5.3 同模式补两条 ALTER，不回填 | 一致 |
| 10 | HIT 模板 | `base SELECT *`；`l` 直连最新日明细；WHERE 剩余天数 + 概率；ORDER 现状键 | T5.5 最终 SELECT 补两列，奖励条件挂 `l`；排序模式白名单注入 | 一致 |
| 11 | STREAK 模板 | `eligible` 显式列清单；`latest_in_window e.*`；岛屿算 `max_streak` | T5.5 `eligible` 补两列后 `e.*` 带出；最终 SELECT 补两列 | 一致 |
| 12 | 视图 | 两个用 `d.*`；`v_screen_persistent_10d` 最终显式 SELECT 清单 | T5.4 前两视图不改，第三个清单补两列 | 一致 |
| 13 | 门面默认回落 | `_default` 兼容 dict/namespace，`getattr` 兜底；`_coerce_float` 下限钳制 | T6 新参数走 `_coerce_float(..., 0.0)`；档位/排序白名单回退 | 一致 |
| 14 | 测试夹具 | `_good_raw` / `_raw` 现无奖励；`_cfg`/`_Cfg` 为 SimpleNamespace 桩 | §7.1 夹具默认注入达标奖励；新字段读取 `getattr` 兜底，旧桩不报错 | 一致 |
| 15 | YAML 样例布局 | `reward` 应为 `volume/horizon_prob/query_defaults/notify` 同级；新键进 `query_defaults` | T4.1 样例缩进与层级对齐现有布局 | 一致 |
| 16 | 不动层 | lister 透传；三档锚点/`evaluate_presets`；`prune_screen`/run 表/白名单 | §2.2、§4 总览列为不改动 | 一致 |
| 17 | 官方/SDK 字段核对 | 官方 `polymarket` 0.11.0 SDK：Gamma `ClobReward` 别名 `conditionId/assetAddress/rewardsAmount/rewardsDailyRate/startDate/endDate`，`MarketRewards.clob_rewards` 别名 `clobRewards`，同级 `rewardsMinSize/rewardsMaxSpread/holdingRewardsEnabled`；CLOB `CurrentReward.rewards_config[].rate_per_day/asset_address` | §3.1/§3.2 字段名与采集路径（Gamma 顶层 `clobRewards`）一致；CLOB 交叉验证命名已按 SDK 更正为 `rewards_config[]` | 一致（CLOB 命名已修正） |
| 18 | 代币地址核对 | 多方链上资料：USDC.e `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`；pUSD `0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb`，6 位小数、1:1 由 USDC 背书的 ERC-20 wrapper | §3.3 映射表与 1:1 口径一致；无 WETH 假设 | 一致 |

### A.2 复核中补充的澄清（已回写正文）

1. **占位符与参数元组条件对齐**：奖励下限为 0 时不追加 WHERE 片段，也不在参数元组末尾追加占位值；`>0` 时才追加，避免占位符与参数数量错位（见 T5.5、T5.7）。
2. **存储新形参向后兼容**：`query_persistent_rows` 的 `min_reward_daily_rate=0.0`、`sort_mode="persistence"` 均为默认值，存量调用方与既有单测不传时行为不变（见 T5.5）。

### A.3 复核结论

- 开发文档与设计 v3 口径一致：准入线 30、复合排序、分档左闭右开、读侧只取最新一轮、稳定程度复用 `max_streak`、未知代币兜底地址，无相互矛盾表述。
- 分层落点与工作树代码一致，未把解析/准入/排序职责放错层；SQL 全部走 `?` 绑定，排序片段走白名单。
- 未发现遗漏的落库列、视图、迁移点或测试夹具改造项；§7.2 用例覆盖设计 §5 全部条目。
- 遗留非阻塞项均已在 §10 记录：EC2 首轮字段/代币复核、采集覆盖边界、多条奖励记录取舍、历史 NULL 不回填。

### A.4 落地后复核（2026-11-11）

| 复核项 | 结果 |
|---|---|
| T1 模型 | `CandidateMarket` / `PersistentCandidateRow` 末尾各加两个带默认值字段，仅标准库 |
| T2 解析 | `_parse_rewards` 复用 `_json_list/_to_float`，异常收敛 `(None, "")`；`classify_reward_tier` 左闭右开 |
| T3 编排 | 奖励准入先于量阈值；复合排序 `(rate, volume)` 倒序；TopN 位置不变 |
| T4 配置 | 准入线/三档阈值/读侧下限均非负校验 + fail-soft 回退；YAML 样例补齐 |
| T5 存储 | DDL/列清单/写入参数 25 列；占位符按列清单长度推导；两条 ALTER 幂等；hit/streak/视图补奖励列；奖励下限仅 `>0` 拼 WHERE；排序走白名单 |
| T6 门面 | 新三参收敛透传；档位映射、显式下限优先；`_map_row` 补奖励映射 |
| T7 CLI | `--min-reward/--reward-tier/--sort`，表头补 reward/asset/tier，档位现算 |
| 回归 | `pytest tests/test_market_screen_*.py -q` 83 passed；`pytest -q` 275 passed；`py_compile` 全通过 |
| 分层红线 | `features/` 未 import `src/`；models 仅标准库；SQL 全 `?` 绑定；排序片段白名单注入 |


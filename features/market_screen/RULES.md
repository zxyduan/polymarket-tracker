# Market Screen 筛选规则口径（P1）

## 采集范围
- 只读公开 Gamma `GET /markets`：`active=true&closed=false&order=volume24hr&ascending=false`。
- 量口径固定 `volume24hr`（USDC 名义 24h 成交额）；`volume`/`volumeNum` 为累计量，不得用作 24h 量。
- 仅保留 Yes/No 二元市场；价格字符串转数值，价格和与 1 偏离超过 0.01 视为脏数据剔除。

## 剩余天数
- 按配置时区（默认 UTC+8）日历日计算：`ceil((到期当日结束 − now) / 86400)`；已到期或缺结束时间剔除。

## 概率对称
- `minority_prob = min(prob_yes, prob_no)`；主场景按小侧概率判定，Yes/No 对称。

## 三档时间-概率锚点
| 档位 | ≤30 天 | ≤60 天 | ≤365 天 |
| --- | --- | --- | --- |
| strict | ≤0.5% | ≤1% | ≤5% |
| balanced | ≤1% | ≤2% | ≤10% |
| loose | ≤2% | ≤3.5% | ≤15% |

- 段内按剩余天数线性插值；首锚前取首锚值，末锚后取末锚值；结果裁剪到 `[0.2%, 20%]`。
- 判定式：`minority_prob <= 阈值上限` 记该档通过（1），否则不通过（0）。

## 主档与并行记录
- `horizon_preset`（主档，默认 `balanced`）的判定结果即 `screen_passed`；`passed_count` 只统计主档通过。
- `record_all_presets=true` 时并行计算三档标志；`false` 时仅主档计算，其余档标志留 NULL、计数记 0。

## 规则整体关闭（`horizon_prob.enabled=false`）
- 仍先过量阈值与 TopN 截断；候选进 TopN 即 `screen_passed=1`，原因 `volume_topn_only`，三档标志 NULL。
- `persist_non_passed=false` 时，未过主档候选不落库（建议保持 true，持续性主场景依赖候选池历史）。

## 落库与查询
- 每日一轮替换当日分区（同日期重跑覆盖，不累加）；空候选仍落 `done` 运行行（`candidate_count=0`）。
- 持续性查询默认窗口 10 天、命中 10 天、24h 量 ≥5 万、名次 ≤200、剩余 ≥31 天、小侧概率 ≤15%；严格连续路径改用 `max_streak`。

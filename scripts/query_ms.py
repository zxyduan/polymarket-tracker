"""只读持续性查询脚本（临时验证工具，非交付 CLI）。

用法（仓库根目录、.venv 激活）：
    python query_ms.py                                  # 用配置默认值
    python query_ms.py --prob-max 0.10 --min-volume 100000
    python query_ms.py --window-days 10 --min-hit-days 8 --consecutive
    python query_ms.py --prob-side yes --max-rank 100 --min-remaining-days 31

参数全部对应查询门面 query_persistent_candidates；不传则回落 config.yaml
market_screen.query_defaults 默认值。本脚本只读，不写库。
"""

from __future__ import annotations

import argparse
import asyncio

from src.settings import Settings, resolve_market_screen_config
from src.storage import Storage
from framework.parsing.market_screen_parsing import classify_reward_tier
from framework.store.market_screen_store import MarketScreenStore


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="market_screen 持续性只读查询")
    p.add_argument("--window-days", type=int, default=None,
                   help="回看窗口天数（默认 10）")
    p.add_argument("--min-hit-days", type=int, default=None,
                   help="窗口内命中天数（默认=窗口天数）")
    p.add_argument("--min-volume", type=float, default=None,
                   help="24h 量下限 USDC（默认 50000）")
    p.add_argument("--max-rank", type=int, default=None,
                   help="排名上限（默认 200）")
    p.add_argument("--min-remaining-days", type=int, default=None,
                   help="剩余天数下限（默认 31）")
    p.add_argument("--prob-max", type=float, default=None,
                   help="概率上限，(0,0.5)（默认 0.15）")
    p.add_argument("--prob-side", choices=["minority", "yes"], default=None,
                   help="概率口径：minority 小侧 / yes（默认 minority）")
    p.add_argument("--consecutive", action="store_true", default=False,
                   help="要求严格连续命中（默认按累计命中天数）")
    p.add_argument("--min-reward", type=float, default=None,
                   help="每日奖励速率下限（0/不传=不额外限）")
    p.add_argument("--reward-tier", choices=["low", "mid", "high"], default=None,
                   help="按档位取奖励下限：low/mid/high（显式 --min-reward 优先）")
    p.add_argument("--sort", choices=["persistence", "reward"], default="reward",
                   help="排序：persistence 按持续性 / reward 奖励优先（默认 reward）")
    p.add_argument("--end-date", default=None,
                   help="截止日 YYYY-MM-DD（默认今天，UTC+8）")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    settings = Settings.load("config/config.yaml")
    cfg = resolve_market_screen_config(settings)

    storage = Storage("data/monitor.db")
    await storage.init()
    store = MarketScreenStore(storage, config=cfg)

    res = await store.query_persistent_candidates(
        window_days=args.window_days,
        min_hit_days=args.min_hit_days,
        min_volume_24h=args.min_volume,
        max_rank=args.max_rank,
        min_remaining_days=args.min_remaining_days,
        prob_max=args.prob_max,
        prob_side=args.prob_side,
        require_consecutive=True if args.consecutive else None,
        min_reward_daily_rate=args.min_reward,
        reward_tier=args.reward_tier,
        sort_mode=args.sort,
        end_date=args.end_date,
    )

    for w in res.warnings:
        print(f"warning: {w}")
    print(f"命中 {len(res.rows)} 条\n")

    header = ("hit", "streak", "reward", "asset", "tier", "minority",
              "vol24h", "days_left", "best_rank", "first~last", "slug")
    print(" | ".join(header))
    for r in res.rows:
        reward = r.latest_rewards_daily_rate
        tier = classify_reward_tier(
            reward,
            low_min=cfg.reward_tier_low_min,
            mid_min=cfg.reward_tier_mid_min,
            high_min=cfg.reward_tier_high_min,
        )
        print(" | ".join(str(x) for x in (
            r.hit_days,
            r.max_streak if r.max_streak is not None else "-",
            f"{reward:.2f}" if reward is not None else "-",
            r.latest_rewards_asset or "-",
            tier or "-",
            f"{r.minority_prob:.4f}" if r.minority_prob is not None else "-",
            f"{r.latest_volume_24h:.0f}",
            r.remaining_days if r.remaining_days is not None else "-",
            r.best_rank,
            f"{r.first_run_date}~{r.last_run_date}",
            r.slug,
        )))

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

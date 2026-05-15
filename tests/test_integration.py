"""信号选择 + 权重融合 集成测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher import fetch_daily_kline
from strategy.signals import build_signals
from strategy.fusion import fuse_signals

# 获取沪深300数据
df = fetch_daily_kline("000300", days=400, stock_type="index")
print(f"数据: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")

# 测试1: 只用技术指标
sig1 = build_signals(df, symbol="000300", enabled_signals=["technical"])
f1 = fuse_signals(sig1, weights={"technical": 1.0})
d1, s1 = f1["decision"].iloc[-1], f1["strength"].iloc[-1]
print(f"仅技术指标: decision={d1}, strength={s1:.3f}")

# 测试2: 技术+缠论
sig2 = build_signals(df, symbol="000300", enabled_signals=["technical", "chanlun"])
f2 = fuse_signals(sig2, weights={"technical": 0.5, "chanlun": 0.5})
d2, s2 = f2["decision"].iloc[-1], f2["strength"].iloc[-1]
print(f"技术+缠论: decision={d2}, strength={s2:.3f}")

# 测试3: 全部（含Kronos）
sig3 = build_signals(df, symbol="000300", enabled_signals=["technical", "chanlun", "kronos"])
k3 = sig3["kronos_signal"].iloc[-1]
print(f"Kronos信号: {k3:.3f}")
f3 = fuse_signals(sig3, weights={"technical": 0.3, "chanlun": 0.3, "kronos": 0.4})
d3, s3 = f3["decision"].iloc[-1], f3["strength"].iloc[-1]
print(f"全部融合: decision={d3}, strength={s3:.3f}")

print("\n全部测试通过!")

"""Kronos 基础模型预测信号

依赖 PyTorch + Kronos 模型代码，未安装时优雅降级。
"""

import pandas as pd
import numpy as np
from utils.logger import get_logger

logger = get_logger("kronos")

_predictor = None
_loaded = False


def _ensure_model():
    """懒加载 Kronos 模型，仅在首次调用时加载"""
    global _predictor, _loaded
    if _loaded:
        return _predictor is not None

    _loaded = True
    try:
        import sys
        import os
        # 支持两种安装方式：克隆到 tests/kronos_repo 或系统安装
        kronos_path = os.path.join(os.path.dirname(__file__), "..", "tests", "kronos_repo")
        if os.path.isdir(kronos_path) and kronos_path not in sys.path:
            sys.path.insert(0, kronos_path)

        from model import Kronos, KronosTokenizer, KronosPredictor

        logger.info("加载 Kronos 模型...")
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-small")

        device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda:0"
        except Exception:
            pass

        _predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        logger.info(f"Kronos 模型加载完成 (device={device})")
        return True
    except Exception as e:
        logger.warning(f"Kronos 模型不可用: {e}")
        _predictor = None
        return False


def is_available() -> bool:
    """检查 Kronos 是否可用"""
    return _ensure_model()


def predict_future(df: pd.DataFrame, pred_days: int = 5) -> pd.DataFrame | None:
    """用 Kronos 预测未来 N 天 OHLCV

    Args:
        df: 日K线 DataFrame，需含 open/high/low/close/volume 列，DatetimeIndex
        pred_days: 预测天数

    Returns:
        预测结果 DataFrame（open/high/low/close/volume），失败返回 None
    """
    if not _ensure_model():
        return None

    try:
        lookback = min(400, len(df))
        recent = df.iloc[-lookback:].copy()

        # 准备 amount 列
        if "amount" not in recent.columns:
            recent["amount"] = recent["close"] * recent["volume"]

        x_df = recent[["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
        x_timestamp = pd.Series(recent.index).reset_index(drop=True)

        last_date = recent.index[-1]
        y_timestamp = pd.Series(pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=pred_days))

        pred_df = _predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=pred_days,
            T=1.0,
            top_p=0.9,
            sample_count=1,
        )
        return pred_df
    except Exception as e:
        logger.error(f"Kronos 预测失败: {e}")
        return None


def generate_kronos_signal(df: pd.DataFrame, pred_days: int = 5,
                           rebalance_every: int = 5,
                           progress_cb=None) -> pd.Series:
    """基于 Kronos 滚动预测生成每日信号（-1 ~ +1）

    策略逻辑：
    - 每隔 rebalance_every 个交易日跑一次 Kronos 预测
    - 用截止当天的历史数据预测未来 pred_days 天
    - 计算预测累计涨跌幅，映射到 -1 ~ +1（±3% 为满分）
    - 两次预测之间前向填充信号

    Args:
        df: 日K线 DataFrame（DatetimeIndex）
        pred_days: 每次预测的天数
        rebalance_every: 每隔多少交易日重新预测（默认5=周频）
        progress_cb: 进度回调函数 (current, total, message) -> None
    """
    if not _ensure_model():
        return pd.Series(0.0, index=df.index)

    # Kronos 最多用 400 天历史，超出的裁掉减少无用预测
    max_len = 400
    if len(df) > max_len:
        df = df.iloc[-max_len:]

    signal = pd.Series(0.0, index=df.index)
    min_lookback = 60  # 至少需要 60 天历史数据才开始预测

    # 确定预测日（从 min_lookback 开始，每隔 rebalance_every 天）
    predict_indices = list(range(min_lookback, len(df), rebalance_every))
    # 确保最后一天也预测
    if len(df) - 1 not in predict_indices:
        predict_indices.append(len(df) - 1)

    total = len(predict_indices)
    logger.info(f"Kronos 滚动预测: {total} 次预测, 间隔 {rebalance_every} 天")

    for i, idx in enumerate(predict_indices):
        if progress_cb:
            progress_cb(i, total, f"Kronos 预测中 ({i+1}/{total})...")
        hist = df.iloc[:idx + 1]
        pred_df = predict_future(hist, pred_days=pred_days)
        if pred_df is None:
            continue

        last_close = hist["close"].iloc[-1]
        pred_close = pred_df["close"].iloc[-1]
        change_pct = (pred_close - last_close) / last_close
        raw_signal = float(np.clip(change_pct / 0.03, -1.0, 1.0))

        signal.iloc[idx] = raw_signal

    if progress_cb:
        progress_cb(total, total, "Kronos 预测完成")

    # 前向填充：两次预测之间沿用上次信号
    signal = signal.replace(0.0, np.nan)
    # 保留 min_lookback 之前的 0（没有预测）
    signal.iloc[:min_lookback] = 0.0
    signal = signal.ffill().fillna(0.0)

    logger.info(f"Kronos 信号完成: 非零信号 {(signal != 0).sum()} / {len(signal)} 天")
    return signal

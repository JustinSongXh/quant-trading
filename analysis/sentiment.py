"""新闻情绪分析（issue #2）

消费 #1 缓存里的新闻数据，用本地中文金融情绪模型（BertForSequenceClassification，
3 类 → -1/0/+1）打分，回写 news_items.sentiment_score，并按来源分别聚合为
公告 / 新闻 / 股吧 三类每日得分（不合成单一总分）。

模型本地 CPU 推理，权重缓存到 HuggingFace 默认目录（容器已挂载到 data/hf_cache/）。
注：原计划用 valuesimplex FinBERT2，但其官方仅发布编码器（无情绪分类头、非开箱即用），
故改用同为中文金融领域、自带 3 分类头的 hw2942 模型。情绪仅作普通参考信号。
"""

from data.cache import load_news_items, load_unscored_news, update_sentiment_scores
from data.news import DEFAULT_WINDOW, _trading_window_start, fetch_stock_news
from utils.logger import get_logger

import threading

import pandas as pd

logger = get_logger("sentiment")

MODEL_ID = "hw2942/bert-base-chinese-finetuning-financial-news-sentiment-v2"
MAX_TOKENS = 512
BATCH_SIZE = 8
# 模型 id2label: {0: Negative, 1: Neutral, 2: Positive}
_LABEL_SCORE = {0: -1.0, 1: 0.0, 2: 1.0}
# source → 输出列前缀
_SOURCE_PREFIX = {"cninfo": "announcement", "em_news": "news", "em_guba": "guba"}
_COLUMNS = [
    "announcement_score", "announcement_count",
    "news_score", "news_count",
    "guba_score", "guba_count",
]

_model = None
_tokenizer = None
# 串行化模型加载：Streamlit 每次 rerun 在独立线程执行，并发首次调用会同时触发
# transformers v5 _LazyModule 懒加载，引发 "cannot import name" 竞态（#27）。
_load_lock = threading.Lock()


def _load_model():
    global _model, _tokenizer
    if _model is None:
        with _load_lock:
            if _model is None:  # 双重检查：等锁期间可能已被别的线程加载完
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                logger.info(f"加载情绪模型 {MODEL_ID} ...")
                _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
                model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
                model.eval()
                _model = model  # 最后赋值，避免别的线程读到 eval() 前的半成品
    return _model, _tokenizer


def _predict_scores(texts: list[str]) -> list[float]:
    """批量推理，返回每条文本的 -1/0/+1 得分"""
    import torch

    model, tok = _load_model()
    out: list[float] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        enc = tok(
            batch, padding=True, truncation=True,
            max_length=MAX_TOKENS, return_tensors="pt",
        )
        with torch.no_grad():
            logits = model(**enc).logits
        for p in logits.argmax(dim=-1).tolist():
            out.append(_LABEL_SCORE.get(p, 0.0))
    return out


def score_pending(stock_code: str | None = None) -> int:
    """对缓存中 sentiment_score 为空的条目打分并回写。

    按 (source, external_id) 去重——同一条新闻只跑一次模型，分数复写到所有关联行。
    返回打分的（去重后）条数。
    """
    pending = load_unscored_news(stock_code)
    if pending.empty:
        return 0
    texts = [
        f"{(r.title or '')} {(r.content or '')}".strip()
        for r in pending.itertuples()
    ]
    scores = _predict_scores(texts)
    payload = [
        {"source": r.source, "external_id": r.external_id, "score": sc}
        for r, sc in zip(pending.itertuples(), scores)
    ]
    update_sentiment_scores(payload)
    logger.info(f"  {stock_code or '全部'} 情绪打分完成：{len(payload)} 条（去重后）")
    return len(payload)


def analyze_sentiment(
    stock_code: str,
    lookback_trading_days: int = DEFAULT_WINDOW,
    stock_type: str | None = None,
) -> pd.DataFrame:
    """返回按来源分类的每日情绪得分。

    Returns:
        DataFrame，index=日期(date)，columns=
          announcement_score / announcement_count,
          news_score / news_count,
          guba_score / guba_count
    """
    # 1) 确保窗口数据已采集（增量），2) 给本股票未打分条目打分
    fetch_stock_news(stock_code, lookback_trading_days, stock_type)
    score_pending(stock_code)

    # 3) 读窗口内已打分数据，按 日期 × 来源 聚合
    window_start = _trading_window_start(lookback_trading_days)
    df = load_news_items(stock_code, since=window_start)
    if not df.empty:
        df = df.dropna(subset=["sentiment_score"]).copy()
    if df.empty:
        return pd.DataFrame(columns=_COLUMNS)

    df["date"] = pd.to_datetime(df["published_at"]).dt.date
    out = pd.DataFrame(index=sorted(df["date"].unique()))
    for src, pref in _SOURCE_PREFIX.items():
        g = df[df["source"] == src].groupby("date")["sentiment_score"]
        out[f"{pref}_score"] = g.mean()
        out[f"{pref}_count"] = g.count()

    count_cols = [c for c in _COLUMNS if c.endswith("_count")]
    out[count_cols] = out[count_cols].fillna(0).astype(int)
    out.index.name = "date"
    return out[_COLUMNS]

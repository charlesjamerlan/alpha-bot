from pydantic import BaseModel


class ScoreResult(BaseModel):
    overall: float  # 0.0 - 1.0
    keyword: float
    sentiment: float
    engagement: float
    credibility: float
    tickers: list[str] = []
    sentiment_label: str = "neutral"  # bullish / bearish / neutral

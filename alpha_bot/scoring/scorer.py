from alpha_bot.ingestion.models import RawTweet
from alpha_bot.scoring.models import ScoreResult
from alpha_bot.scoring.strategies.credibility import CredibilityStrategy
from alpha_bot.scoring.strategies.engagement import EngagementStrategy
from alpha_bot.scoring.strategies.keyword import KeywordStrategy
from alpha_bot.scoring.strategies.sentiment import SentimentStrategy

WEIGHTS = {
    "keyword": 0.35,
    "sentiment": 0.20,
    "engagement": 0.20,
    "credibility": 0.25,
}


class CompositeScorer:
    def __init__(self) -> None:
        self._keyword = KeywordStrategy()
        self._sentiment = SentimentStrategy()
        self._engagement = EngagementStrategy()
        self._credibility = CredibilityStrategy()

    def score(self, tweet: RawTweet) -> ScoreResult:
        kw = self._keyword.score(tweet)
        sent = self._sentiment.score(tweet)
        eng = self._engagement.score(tweet)
        cred = self._credibility.score(tweet)

        overall = (
            kw * WEIGHTS["keyword"]
            + sent * WEIGHTS["sentiment"]
            + eng * WEIGHTS["engagement"]
            + cred * WEIGHTS["credibility"]
        )

        return ScoreResult(
            overall=round(min(overall, 1.0), 4),
            keyword=round(kw, 4),
            sentiment=round(sent, 4),
            engagement=round(eng, 4),
            credibility=round(cred, 4),
            tickers=self._keyword.extract_tickers(tweet),
            sentiment_label=self._keyword.sentiment_direction(tweet),
        )

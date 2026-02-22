from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from alpha_bot.ingestion.models import RawTweet
from alpha_bot.scoring.strategies.base import ScoringStrategy

_analyzer = SentimentIntensityAnalyzer()


class SentimentStrategy(ScoringStrategy):
    def score(self, tweet: RawTweet) -> float:
        scores = _analyzer.polarity_scores(tweet.text)
        compound = scores["compound"]
        # Map compound (-1..1) to 0..1, with strong sentiment scoring higher
        return (abs(compound) + compound) / 2  # 0 for negative, up to 1 for positive

    def compound(self, tweet: RawTweet) -> float:
        return _analyzer.polarity_scores(tweet.text)["compound"]

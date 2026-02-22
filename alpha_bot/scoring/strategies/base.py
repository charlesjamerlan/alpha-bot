from abc import ABC, abstractmethod

from alpha_bot.ingestion.models import RawTweet


class ScoringStrategy(ABC):
    @abstractmethod
    def score(self, tweet: RawTweet) -> float:
        """Return a score between 0.0 and 1.0."""
        ...

from abc import ABC, abstractmethod

from alpha_bot.scoring.models import ScoreResult
from alpha_bot.ingestion.models import RawTweet


class DeliveryChannel(ABC):
    @abstractmethod
    async def send_signal(self, tweet: RawTweet, score: ScoreResult) -> None:
        """Deliver a scored signal to the channel."""
        ...

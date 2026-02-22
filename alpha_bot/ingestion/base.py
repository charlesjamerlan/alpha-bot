from abc import ABC, abstractmethod

from alpha_bot.ingestion.models import RawTweet


class BaseIngester(ABC):
    @abstractmethod
    async def fetch_new(self) -> list[RawTweet]:
        """Fetch new tweets since the last poll."""
        ...

from datetime import datetime

from pydantic import BaseModel


class TweetAuthor(BaseModel):
    id: str
    username: str
    name: str = ""
    followers_count: int = 0
    verified: bool = False


class TweetMetrics(BaseModel):
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0


class RawTweet(BaseModel):
    tweet_id: str
    text: str
    created_at: datetime
    author: TweetAuthor
    metrics: TweetMetrics

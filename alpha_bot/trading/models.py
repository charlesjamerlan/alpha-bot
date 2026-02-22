"""Pydantic models for the trading module."""

from pydantic import BaseModel


class TradeSignal(BaseModel):
    """Parsed from an incoming TG message — represents a potential buy."""

    token_mint: str
    ticker: str = ""
    chain: str = "solana"
    source_group: str = ""
    source_message_id: int = 0
    source_message_text: str = ""
    author: str = ""


class MaestroAction(BaseModel):
    """Result of sending a command to Maestro bot."""

    success: bool
    message_sent: str = ""
    error: str = ""

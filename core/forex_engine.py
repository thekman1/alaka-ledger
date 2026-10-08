"""Opt-in public FX quotes with a bounded, thread-safe, process-local cache."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Callable, Literal, Optional


class ForexUnavailableError(RuntimeError):
    """Indicate that no usable public FX quote can be obtained."""


@dataclass(frozen=True)
class FxQuote:
    """Represent INR per USD with the provider's observation time in UTC."""

    rate: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class FxResult:
    """Expose quote availability and provenance without inventing a fallback rate."""

    quote: Optional[FxQuote]
    status: Literal["fresh", "cached", "stale", "unavailable", "disabled"]


def _utc_now() -> datetime:
    """Return an aware timestamp for cache freshness checks."""
    return datetime.now(timezone.utc)


def fetch_usd_inr() -> FxQuote:
    """Fetch only the public USDINR=X ticker, never portfolio or account data.

    Returns:
        Latest available intraday quote, which may be delayed by the provider.

    Raises:
        ForexUnavailableError: The provider fails or returns unusable data.
    """
    try:
        import yfinance as yf

        history = yf.Ticker("USDINR=X").history(
            period="5d", interval="1h", auto_adjust=False, timeout=5, raise_errors=True
        )
        close = history["Close"].dropna()
        if close.empty:
            raise ValueError("Missing quote")
        rate = Decimal(str(close.iloc[-1]))
        observed_at = close.index[-1].to_pydatetime()
        if observed_at.tzinfo is None or not rate.is_finite() or rate <= 0:
            raise ValueError("Invalid quote")
        return FxQuote(rate, observed_at.astimezone(timezone.utc))
    except Exception:
        raise ForexUnavailableError("Public FX data is unavailable.") from None


class ForexEngine:
    """Cache public quotes only; network access requires explicit user consent.

    Failed fetches have the same cooldown as successful ones. A previously
    validated quote can be returned as stale for at most ``max_quote_age``;
    an empty or expired cache returns no rate. Cache lifetime is one process.
    """

    def __init__(
        self,
        *,
        allow_network: bool = False,
        ttl: timedelta = timedelta(minutes=15),
        max_quote_age: timedelta = timedelta(days=7),
        fetcher: Callable[[], FxQuote] = fetch_usd_inr,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Inject provider and clock to make offline and expiration behavior testable."""
        if ttl <= timedelta(0) or max_quote_age < ttl:
            raise ValueError("FX cache durations are invalid.")
        self._allow_network = allow_network
        self._ttl = ttl
        self._max_quote_age = max_quote_age
        self._fetcher = fetcher
        self._clock = clock
        self._quote: Optional[FxQuote] = None
        self._attempted_at: Optional[datetime] = None
        self._last_attempt_failed = False
        self._lock = Lock()

    def usd_inr(self) -> FxResult:
        """Return a quote and explicit freshness state; never substitute 1:1 FX."""
        if not self._allow_network:
            return FxResult(None, "disabled")
        with self._lock:
            now = self._clock()
            attempted_now = False
            if self._attempted_at is None or not timedelta(0) <= now - self._attempted_at < self._ttl:
                self._attempted_at = now
                attempted_now = True
                try:
                    quote = self._fetcher()
                    if (
                        not quote.rate.is_finite()
                        or quote.rate <= 0
                        or quote.observed_at.tzinfo is None
                        or not timedelta(0) <= now - quote.observed_at <= self._max_quote_age
                    ):
                        raise ForexUnavailableError("The public FX quote is invalid or expired.")
                    self._quote = quote
                    self._last_attempt_failed = False
                except ForexUnavailableError:
                    self._last_attempt_failed = True
            if self._quote is None or not timedelta(0) <= now - self._quote.observed_at <= self._max_quote_age:
                return FxResult(None, "unavailable")
            if self._last_attempt_failed or now - self._quote.observed_at > self._ttl:
                return FxResult(self._quote, "stale")
            return FxResult(self._quote, "fresh" if attempted_now else "cached")
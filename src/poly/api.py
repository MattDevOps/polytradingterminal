"""Polymarket API clients – Gamma (metadata), CLOB (prices), Data (trades)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .models import Market

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
DATA  = "https://data-api.polymarket.com"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_field(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _extract_event_id(m: dict) -> str | None:
    """Pull event ID from the nested ``events`` list the Gamma API returns."""
    events = m.get("events")
    if isinstance(events, list) and events:
        return str(events[0].get("id", ""))
    return m.get("eventId") or None


def _extract_event_slug(m: dict) -> str | None:
    events = m.get("events")
    if isinstance(events, list) and events:
        return events[0].get("slug")
    return m.get("eventSlug") or None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PolymarketAPI:
    """Thin async wrapper around the three Polymarket REST APIs."""

    def __init__(self, timeout: float = 30.0, max_concurrent: int = 12):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(max_concurrent)

    async def close(self) -> None:
        await self._client.aclose()

    # -- internal request wrapper -------------------------------------------

    async def _get(self, url: str, params: dict | None = None) -> Any:
        async with self._sem:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, url: str, json_body: Any = None) -> Any:
        async with self._sem:
            resp = await self._client.post(url, json=json_body)
            resp.raise_for_status()
            return resp.json()

    # -----------------------------------------------------------------------
    # Gamma API  –  market / event metadata  (no auth)
    # -----------------------------------------------------------------------

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[Market]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": active,
            "closed": closed,
            "order": order,
            "ascending": ascending,
        }
        rows = await self._get(f"{GAMMA}/markets", params)
        out: list[Market] = []
        for m in rows:
            try:
                outcomes = _parse_json_field(m.get("outcomes"))
                prices   = _parse_json_field(m.get("outcomePrices"))
                clob_ids = _parse_json_field(m.get("clobTokenIds"))
                out.append(Market(
                    id=str(m["id"]),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    outcomes=outcomes,
                    outcome_prices=[_float(p) for p in prices],
                    clob_token_ids=[str(c) for c in clob_ids],
                    condition_id=m.get("conditionId", ""),
                    volume=_float(m.get("volume")),
                    liquidity=_float(m.get("liquidity")),
                    volume_24h=_float(m.get("volume24hr")),
                    active=bool(m.get("active")),
                    closed=bool(m.get("closed")),
                    end_date=m.get("endDate"),
                    event_id=_extract_event_id(m),
                    event_slug=_extract_event_slug(m),
                    neg_risk_id=m.get("negRiskMarketID") or None,
                    group_title=m.get("groupItemTitle") or None,
                    spread=_float(m.get("spread")),
                    best_bid=_float(m.get("bestBid")),
                    best_ask=_float(m.get("bestAsk")),
                ))
            except (KeyError, TypeError) as exc:
                log.debug("Skipping malformed market: %s", exc)
        return out

    async def get_events(
        self, limit: int = 50, active: bool = True, closed: bool = False,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "limit": limit,
            "active": active,
            "closed": closed,
        }
        return await self._get(f"{GAMMA}/events", params)

    async def get_event(self, event_id: str) -> dict:
        return await self._get(f"{GAMMA}/events/{event_id}")

    # -----------------------------------------------------------------------
    # CLOB API  –  order-book, prices, price history  (no auth for reads)
    # -----------------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> dict:
        return await self._get(f"{CLOB}/book", {"token_id": token_id})

    async def get_midpoint(self, token_id: str) -> float:
        data = await self._get(f"{CLOB}/midpoint", {"token_id": token_id})
        return _float(data.get("mid"))

    async def get_price_history(
        self,
        token_id: str,
        interval: str = "max",
        fidelity: int = 60,
    ) -> list[dict]:
        """Return list of {t, p} dicts (timestamp, price)."""
        params = {"market": token_id, "interval": interval, "fidelity": fidelity}
        data = await self._get(f"{CLOB}/prices-history", params)
        return data.get("history", []) if isinstance(data, dict) else []

    async def get_last_trade_price(self, token_id: str) -> float:
        data = await self._get(f"{CLOB}/last-trade-price", {"token_id": token_id})
        return _float(data.get("price"))

    async def get_spread(self, token_id: str) -> dict:
        return await self._get(f"{CLOB}/spread", {"token_id": token_id})

    # -----------------------------------------------------------------------
    # Data API  –  trades, positions, open interest  (no auth for reads)
    # -----------------------------------------------------------------------

    async def get_trades(
        self,
        market: str | None = None,
        maker: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if maker:
            params["maker"] = maker
        return await self._get(f"{DATA}/trades", params)

    async def get_positions(
        self,
        market: str | None = None,
        address: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if market:
            params["market"] = market
        if address:
            params["address"] = address
        return await self._get(f"{DATA}/positions", params)

    async def get_open_interest(self, market: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if market:
            params["market"] = market
        return await self._get(f"{DATA}/oi", params)

    async def get_live_volume(self) -> list[dict]:
        return await self._get(f"{DATA}/live-volume")

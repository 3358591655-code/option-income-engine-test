
import os
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_TRADING_BASE = "https://paper-api.alpaca.markets"

app = FastAPI(title="Option Income Engine")


def headers():
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


async def alpaca_get(url, params=None):
    h = headers()
    if not h["APCA-API-KEY-ID"] or not h["APCA-API-SECRET-KEY"]:
        raise RuntimeError("未检测到 ALPACA_API_KEY / ALPACA_SECRET_KEY")

    async with httpx.AsyncClient(timeout=40, headers=h) as client:
        r = await client.get(url, params=params or {})
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Alpaca API {r.status_code}: {detail}")
        return r.json()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "Alpaca", "feed": "indicative"}


def calc_dte(expiration):
    try:
        return (date.fromisoformat(str(expiration)[:10]) - date.today()).days
    except Exception:
        return -1


async def get_contracts(symbol, dte_min, dte_max, option_type):
    today = date.today()
    params = {
        "underlying_symbols": symbol.upper(),
        "expiration_date_gte": (today + timedelta(days=dte_min)).isoformat(),
        "expiration_date_lte": (today + timedelta(days=dte_max)).isoformat(),
        "limit": 10000,
        "type": option_type,
        "status": "active",
    }

    out = []
    token = None

    while True:
        request_params = dict(params)
        if token:
            request_params["page_token"] = token

        data = await alpaca_get(
            f"{ALPACA_TRADING_BASE}/v2/options/contracts",
            request_params,
        )
        out.extend(data.get("option_contracts", []))
        token = data.get("next_page_token")

        if not token or len(out) >= 30000:
            break

    return out


async def get_snapshots(symbol, option_type, dte_min, dte_max):
    today = date.today()
    params = {
        "feed": "indicative",
        "limit": 1000,
        "type": option_type,
        "expiration_date_gte": (today + timedelta(days=dte_min)).isoformat(),
        "expiration_date_lte": (today + timedelta(days=dte_max)).isoformat(),
    }

    out = {}
    token = None

    while True:
        request_params = dict(params)
        if token:
            request_params["page_token"] = token

        data = await alpaca_get(
            f"{ALPACA_DATA_BASE}/v1beta1/options/snapshots/{symbol.upper()}",
            request_params,
        )
        out.update(data.get("snapshots", {}))
        token = data.get("next_page_token")

        if not token or len(out) >= 30000:
            break

    return out


def get_delta(snapshot):
    try:
        value = (snapshot.get("greeks") or {}).get("delta")
        return float(value) if value is not None else None
    except Exception:
        return None


def get_iv(snapshot):
    try:
        value = snapshot.get("impliedVolatility")
        if value is None:
            value = snapshot.get("implied_volatility")
        return float(value) if value is not None else None
    except Exception:
        return None


def get_bid_ask(snapshot):
    quote = snapshot.get("latestQuote") or {}
    try:
        bid = float(quote.get("bp"))
    except Exception:
        bid = None
    try:
        ask = float(quote.get("ap"))
    except Exception:
        ask = None
    return bid, ask


def get_latest_trade_size(snapshot):
    try:
        return int(float((snapshot.get("latestTrade") or {}).get("s") or 0))
    except Exception:
        return 0


async def get_daily_volumes(contract_symbols):
    """Return the most recent available daily volume for option contracts."""
    symbols = [s for s in contract_symbols if s]
    if not symbols:
        return {}

    volumes = {}
    start_date = date.today() - timedelta(days=7)
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start_date.isoformat(),
                "limit": 10000,
            }
            if token:
                params["page_token"] = token

            data = await alpaca_get(
                f"{ALPACA_DATA_BASE}/v1beta1/options/bars",
                params,
            )

            for symbol, bars in (data.get("bars") or {}).items():
                if not bars:
                    continue
                latest = bars[-1]
                try:
                    volumes[symbol] = int(float(latest.get("v") or 0))
                except Exception:
                    volumes[symbol] = 0

            token = data.get("next_page_token")
            if not token:
                break

    return volumes


async def build_candidates(
    contracts,
    snapshots,
    strategy,
    dte_min,
    dte_max,
    delta_min,
    delta_max,
    min_oi,
    min_volume,
):
    contract_map = {
        c.get("symbol"): c
        for c in contracts
        if c.get("symbol")
    }

    preliminary = []
    diagnostics = {
        "contracts": len(contracts),
        "snapshots": len(snapshots),
        "matched": 0,
        "delta_pass": 0,
        "quote_pass": 0,
        "oi_pass": 0,
        "volume_pass": 0,
    }

    for option_symbol, snapshot in snapshots.items():
        contract = contract_map.get(option_symbol)
        if not contract:
            continue

        diagnostics["matched"] += 1

        days = calc_dte(contract.get("expiration_date"))
        if days < dte_min or days > dte_max:
            continue

        try:
            strike = float(contract.get("strike_price"))
        except Exception:
            continue

        d = get_delta(snapshot)
        if d is None:
            continue

        abs_delta = abs(d)
        if abs_delta < delta_min or abs_delta > delta_max:
            continue
        diagnostics["delta_pass"] += 1

        bid, ask = get_bid_ask(snapshot)
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            continue

        mid = (bid + ask) / 2
        spread = (ask - bid) / mid
        if spread > 0.20:
            continue
        diagnostics["quote_pass"] += 1

        try:
            oi = int(float(contract.get("open_interest") or 0))
        except Exception:
            oi = 0

        if oi < min_oi:
            continue
        diagnostics["oi_pass"] += 1

        volatility = get_iv(snapshot)

        preliminary.append({
            "strategy": strategy,
            "strike": round(strike, 2),
            "dte": days,
            "delta": round(d, 4),
            "iv": round(volatility * 100, 2) if volatility is not None else None,
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "premium": round(mid, 2),
            "premium_yield": round(mid / strike * 100, 2) if strike > 0 else 0,
            "open_interest": oi,
            "volume": 0,
            "contract": option_symbol,
            "spread": round(spread * 100, 2),
        })

    # Fetch actual option daily-bar volume after the cheaper filters pass.
    volumes = await get_daily_volumes(
        [item["contract"] for item in preliminary]
    )

    results = []
    for item in preliminary:
        volume = volumes.get(item["contract"], 0)
        item["volume"] = volume
        if volume < min_volume:
            continue
        diagnostics["volume_pass"] += 1
        results.append(item)

    results.sort(
        key=lambda item: (item["premium_yield"], item["open_interest"]),
        reverse=True,
    )
    return results[:100], diagnostics


async def scan_one(
    symbol,
    option_type,
    strategy,
    dte_min,
    dte_max,
    delta_min,
    delta_max,
    min_oi,
    min_volume,
):
    contracts = await get_contracts(symbol, dte_min, dte_max, option_type)
    snapshots = await get_snapshots(symbol, option_type, dte_min, dte_max)

    return await build_candidates(
        contracts,
        snapshots,
        strategy,
        dte_min,
        dte_max,
        delta_min,
        delta_max,
        min_oi,
        min_volume,
    )


@app.get("/api/chain/{symbol}")
async def chain(
    symbol: str,
    contract_type: str = Query("both"),
    dte_min: int = Query(25, ge=0, le=3650),
    dte_max: int = Query(50, ge=0, le=3650),
    delta_min: float = Query(0.15, ge=0, le=1),
    delta_max: float = Query(0.25, ge=0, le=1),
    min_oi: int = Query(100, ge=0),
    min_volume: int = Query(10, ge=0),
):
    symbol = symbol.strip().upper()

    try:
        ct = contract_type.lower()

        if ct in ("put", "csp"):
            results, diagnostics = await scan_one(
                symbol, "put", "CSP",
                dte_min, dte_max, delta_min, delta_max,
                min_oi, min_volume,
            )
        elif ct in ("call", "cc"):
            results, diagnostics = await scan_one(
                symbol, "call", "CC",
                dte_min, dte_max, delta_min, delta_max,
                min_oi, min_volume,
            )
        else:
            csp, csp_diag = await scan_one(
                symbol, "put", "CSP",
                dte_min, dte_max, delta_min, delta_max,
                min_oi, min_volume,
            )
            cc, cc_diag = await scan_one(
                symbol, "call", "CC",
                dte_min, dte_max, delta_min, delta_max,
                min_oi, min_volume,
            )
            results = csp + cc
            diagnostics = {"csp": csp_diag, "cc": cc_diag}

        results.sort(
            key=lambda item: (item["premium_yield"], item["open_interest"]),
            reverse=True,
        )

        return {
            "status": "ok",
            "provider": "Alpaca",
            "feed": "indicative",
            "symbol": symbol,
            "count": len(results),
            "results": results[:100],
            "diagnostics": diagnostics,
        }

    except Exception as exc:
        return JSONResponse({
            "status": "ERROR",
            "symbol": symbol,
            "error": str(exc),
            "results": [],
        })


@app.get("/api/status/{symbol}")
async def status(symbol: str):
    try:
        csp, _ = await scan_one(
            symbol, "put", "CSP",
            25, 50, 0.15, 0.25, 100, 10,
        )
        cc, _ = await scan_one(
            symbol, "call", "CC",
            25, 50, 0.15, 0.25, 100, 10,
        )

        if csp and not cc:
            state = "CSP"
        elif cc and not csp:
            state = "CC"
        else:
            state = "WAIT"

        return {
            "status": "ok",
            "symbol": symbol.upper(),
            "state": state,
            "csp_count": len(csp),
            "cc_count": len(cc),
        }

    except Exception as exc:
        return {
            "status": "ERROR",
            "symbol": symbol.upper(),
            "state": "WAIT",
            "csp_count": 0,
            "cc_count": 0,
            "error": str(exc),
        }


@app.get("/api/backtest/{contract}")
async def backtest(contract: str):
    return {
        "status": "NOT_AVAILABLE",
        "contract": contract,
        "message": (
            "严格CSP/CC历史回测需要历史期权链快照、历史Greeks以及"
            "历史Bid/Ask；当前版本不伪造胜率、收益率、最大回撤或指派率。"
        ),
    }

"""Capability-based MetaTrader 5 broker adapters.

The official package is imported lazily so protocol and fake tests run anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Mapping, Protocol

from .models import Identity, AccountSnapshot, ReadSnapshot


class AdapterError(RuntimeError):
    """Safe adapter failure; never contains credentials or raw terminal details."""


class BrokerAdapter(Protocol):
    def initialize(self, terminal_path: str) -> None: ...
    def account_snapshot(self) -> AccountSnapshot: ...
    def symbol_info(self, symbol: str) -> ReadSnapshot: ...
    def quote(self, symbol: str) -> ReadSnapshot: ...
    def closed_m1(self, symbol: str, count: int) -> ReadSnapshot: ...
    def open_orders(self) -> ReadSnapshot: ...
    def open_positions(self) -> ReadSnapshot: ...
    def history_orders(self, from_server_time: str | None = None) -> ReadSnapshot: ...
    def history_deals(self, from_server_time: str | None = None) -> ReadSnapshot: ...
    def order_check(self, typ: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def submit_market(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def modify_protection(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _Symbol:
    name: str
    digits: int
    point: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    trade_stops_level: Decimal
    trade_freeze_level: Decimal
    filling_mode: int
    visible: bool
    trade_allowed: bool


class OfficialMT5Adapter:
    """Thin, fail-closed wrapper around the official MetaTrader5 module."""

    ACCEPTED_RETCODES = {10008, 10009, 10010}
    UNKNOWN_RETCODES = {10012}

    def __init__(self, expected: Identity, account_id: str, *, magic: int = 65065,
                 comment_limit: int = 31):
        self.expected = expected
        self.account_id = account_id
        self.magic = magic
        self.comment_limit = comment_limit
        self._mt5: Any = None
        self._initialized = False

    def _module(self):
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5
            except ImportError as exc:
                raise AdapterError("MetaTrader5 package is required for terminal access") from exc
            self._mt5 = mt5
        return self._mt5

    def _error(self, code: str = "MT5_TRANSPORT_ERROR") -> AdapterError:
        # last_error is intentionally reduced to a stable safe category.
        return AdapterError(code)

    def initialize(self, terminal_path: str) -> None:
        mt5 = self._module()
        try:
            ok = mt5.initialize(path=terminal_path, timeout=60000)
        except Exception as exc:
            raise self._error() from exc
        if not ok:
            raise self._error("MT5_INITIALIZE_FAILED")
        self._initialized = True
        try:
            if mt5.terminal_info() is None or mt5.version() is None:
                raise self._error("MT5_HEALTH_UNAVAILABLE")
            self._verify(mt5.account_info())
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error() from exc

    def _verify(self, info: Any) -> Any:
        if info is None:
            raise self._error("MT5_ACCOUNT_UNAVAILABLE")
        if (str(getattr(info, "login", "")) != self.expected.external_account_id or
                str(getattr(info, "server", "")) != self.expected.broker_server):
            raise self._error("MT5_IDENTITY_MISMATCH")
        return info

    def _info(self):
        return self._verify(self._module().account_info())

    @staticmethod
    def _plain(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "_asdict"):
            return {k: OfficialMT5Adapter._plain(v) for k, v in value._asdict().items()}
        if isinstance(value, (list, tuple)):
            return [OfficialMT5Adapter._plain(v) for v in value]
        if isinstance(value, dict):
            return {k: OfficialMT5Adapter._plain(v) for k, v in value.items()}
        if isinstance(value, (datetime,)):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value

    def account_snapshot(self) -> AccountSnapshot:
        i = self._info()
        return AccountSnapshot(self.account_id, self.expected, str(i.login),
            str(getattr(i, "currency", "")), int(getattr(i, "leverage", 0)),
            str(getattr(i, "balance", 0)), str(getattr(i, "equity", 0)),
            bool(getattr(i, "trade_allowed", False)), str(getattr(i, "margin_mode", "")))

    def _symbol(self, symbol: str) -> tuple[Any, _Symbol]:
        mt5 = self._module()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise AdapterError("SYMBOL_NOT_FOUND")
        if not bool(getattr(info, "visible", False)):
            if not mt5.symbol_select(symbol, True) or mt5.symbol_info(symbol) is None:
                raise AdapterError("SYMBOL_NOT_VISIBLE")
            info = mt5.symbol_info(symbol)
        return info, _Symbol(symbol, int(getattr(info, "digits", 0)),
            Decimal(str(getattr(info, "point", "0"))),
            Decimal(str(getattr(info, "volume_min", "0"))),
            Decimal(str(getattr(info, "volume_max", "0"))),
            Decimal(str(getattr(info, "volume_step", "0"))),
            Decimal(str(getattr(info, "trade_stops_level", 0))),
            Decimal(str(getattr(info, "trade_freeze_level", 0))),
            int(getattr(info, "filling_mode", 0)), bool(getattr(info, "visible", True)),
            bool(getattr(info, "trade_allowed", True)))

    def symbol_info(self, symbol):
        info, _ = self._symbol(symbol)
        return ReadSnapshot("symbol_info", self._plain(info))

    def quote(self, symbol):
        self._symbol(symbol)
        tick = self._module().symbol_info_tick(symbol)
        if tick is None:
            raise AdapterError("QUOTE_UNAVAILABLE")
        return ReadSnapshot("quote", self._plain(tick) | {"symbol": symbol})

    def closed_m1(self, symbol, count):
        if count < 1:
            raise AdapterError("INVALID_CANDLE_COUNT")
        self._symbol(symbol)
        rows = self._module().copy_rates_from_pos(symbol, self._module().TIMEFRAME_M1, 1, count)
        if rows is None:
            raise AdapterError("CANDLE_READ_FAILED")
        return ReadSnapshot("candles", {"symbol": symbol, "timeframe": "M1",
            "closed_only": True, "items": [self._plain(row) for row in rows]})

    def _rows(self, name: str, *args):
        rows = getattr(self._module(), name)(*args)
        if rows is None:
            raise AdapterError(f"{name.upper()}_FAILED")
        return ReadSnapshot(name.replace("_get", ""), {"items": [self._plain(row) for row in rows]})

    def open_orders(self): return self._rows("orders_get")
    def open_positions(self): return self._rows("positions_get")

    @staticmethod
    def _history_window(value):
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=1)
        if value:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise AdapterError("HISTORY_TIMEZONE_REQUIRED")
            start = parsed.astimezone(timezone.utc) - timedelta(minutes=5)
        return start, end

    def history_orders(self, from_server_time=None):
        return self._rows("history_orders_get", *self._history_window(from_server_time))

    def history_deals(self, from_server_time=None):
        return self._rows("history_deals_get", *self._history_window(from_server_time))

    @staticmethod
    def _decimal(value, field):
        try: return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc: raise AdapterError(f"INVALID_{field.upper()}") from exc

    def _quantize(self, value, quantum, field):
        value = self._decimal(value, field)
        if quantum <= 0: raise AdapterError("INVALID_SYMBOL_METADATA")
        return (value / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum

    def _comment(self, payload):
        value = str(payload.get("comment") or payload.get("correlation_id") or "connector")
        return value[:self.comment_limit]

    def _request(self, typ: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        mt5 = self._module(); symbol = str(payload.get("symbol") or "")
        if not symbol: raise AdapterError("SYMBOL_REQUIRED")
        info, meta = self._symbol(symbol)
        account = self._info()
        if not bool(getattr(account, "trade_allowed", False)) or not meta.trade_allowed:
            raise AdapterError("TRADING_NOT_ALLOWED")
        tick = self._module().symbol_info_tick(symbol)
        if tick is None: raise AdapterError("QUOTE_UNAVAILABLE")
        position = None
        side = str(payload.get("side") or payload.get("direction") or "").upper()
        if typ == "position.close":
            position = self._fresh_position(payload.get("position_ticket"))
            position_type = position.get("type", position.get("position_type"))
            # MT5 position type 0 is BUY and 1 is SELL. The close is opposite.
            side = "SELL" if str(position_type) in {"0", "BUY"} else "BUY"
        if side not in {"BUY", "SELL"}: raise AdapterError("INVALID_DIRECTION")
        buy = side == "BUY"
        order_type = getattr(mt5, "ORDER_TYPE_BUY" if buy else "ORDER_TYPE_SELL")
        price = self._decimal(getattr(tick, "ask" if buy else "bid", 0), "price")
        digits = Decimal(1).scaleb(-meta.digits)
        request = {"action": getattr(mt5, "TRADE_ACTION_DEAL"), "symbol": symbol,
                   "type": order_type, "price": float(price.quantize(digits)),
                   "volume": float(self._volume(payload.get("volume"), meta)),
                   "deviation": int(payload.get("deviation", 20)), "magic": self.magic,
                   "comment": self._comment(payload), "type_time": getattr(mt5, "ORDER_TIME_GTC")}
        filling = payload.get("type_filling")
        request["type_filling"] = int(filling if filling is not None else getattr(mt5, "ORDER_FILLING_IOC", 1))
        if filling is not None and not (meta.filling_mode == 0 or (meta.filling_mode & (1 << int(filling)))):
            raise AdapterError("INVALID_FILLING_MODE")
        if typ == "order.submit_market":
            if payload.get("sl") in (None, "") or payload.get("tp") in (None, ""): raise AdapterError("NATIVE_PROTECTION_REQUIRED")
            sl = self._price(payload["sl"], digits); tp = self._price(payload["tp"], digits)
            minimum = meta.point * max(meta.trade_stops_level, meta.trade_freeze_level)
            if minimum and ((buy and (sl >= price - minimum or tp <= price + minimum)) or
                            (not buy and (sl <= price + minimum or tp >= price - minimum))):
                raise AdapterError("INVALID_STOPS")
            request["sl"] = float(sl); request["tp"] = float(tp)
        elif typ == "position.modify_protection":
            request["action"] = getattr(mt5, "TRADE_ACTION_SLTP"); request["position"] = self._ticket(payload.get("position_ticket"))
            request["sl"] = float(self._price(payload["sl"], digits)); request["tp"] = float(self._price(payload["tp"], digits))
        elif typ == "position.close":
            position = self._fresh_position(payload.get("position_ticket")); request["position"] = self._ticket(position.get("ticket", payload.get("position_ticket")))
            volume = self._volume(payload.get("volume"), meta)
            available = self._decimal(position.get("volume"), "position_volume")
            if volume > available: raise AdapterError("VOLUME_EXCEEDS_POSITION")
            request["volume"] = float(volume)
        return request

    def _price(self, value, digits):
        result = self._quantize(value, digits, "price")
        if result <= 0: raise AdapterError("INVALID_PRICE")
        return result

    def _volume(self, value, meta):
        result = self._quantize(value, meta.volume_step, "volume")
        if result < meta.volume_min or result > meta.volume_max or result <= 0: raise AdapterError("INVALID_VOLUME")
        return result

    @staticmethod
    def _ticket(value):
        if value in (None, ""): raise AdapterError("POSITION_TICKET_REQUIRED")
        return int(value)

    def _fresh_position(self, ticket):
        ticket = self._ticket(ticket)
        rows = self._module().positions_get(ticket=ticket)
        if not rows: raise AdapterError("POSITION_NOT_FOUND")
        row = self._plain(rows[0]); return row if isinstance(row, dict) else vars(row)

    def order_check(self, typ, payload):
        request = self._request(typ, payload)
        result = self._module().order_check(request)
        if result is None: raise AdapterError("ORDER_CHECK_UNAVAILABLE")
        plain = self._plain(result)
        return plain if isinstance(plain, dict) else {"retcode": getattr(result, "retcode", None)}

    def _send(self, request):
        try: result = self._module().order_send(request)
        except Exception as exc: return {"state": "UNKNOWN", "code": "TRANSPORT_AMBIGUOUS"}
        if result is None: return {"state": "UNKNOWN", "code": "TRANSPORT_AMBIGUOUS"}
        result = self._plain(result); retcode = result.get("retcode") if isinstance(result, dict) else None
        state = "ACCEPTED" if retcode in self.ACCEPTED_RETCODES else "UNKNOWN" if retcode in self.UNKNOWN_RETCODES else "REJECTED"
        result = dict(result) if isinstance(result, dict) else {"retcode": retcode}
        result["state"] = state; result["code"] = "BROKER_REJECTED" if state == "REJECTED" else None
        for source, target in (("order", "external_order_id"), ("deal", "external_deal_id"), ("position", "position_tickets")):
            if result.get(source) is not None: result[target] = [str(result[source])] if target == "position_tickets" else str(result[source])
        return result

    def submit_market(self, payload): return self._send(self._request("order.submit_market", payload))
    def modify_protection(self, payload): return self._send(self._request("position.modify_protection", payload))
    def close(self, payload): return self._send(self._request("position.close", payload))
    def invoke(self, typ, payload): return {"order.submit_market": self.submit_market, "position.modify_protection": self.modify_protection, "position.close": self.close}[typ](payload)

    def health(self):
        info = self._info(); mt5 = self._module()
        return {"initialized": self._initialized, "terminal": self._plain(mt5.terminal_info()), "version": self._plain(mt5.version()), "account": self.account_snapshot().to_dict(), "trade_allowed": bool(getattr(info, "trade_allowed", False))}


class FakeMT5Adapter:
    """Deterministic adapter for unit tests; records exact requests and outcomes."""
    def __init__(self, identity: Identity | None = None, *, outcome: Any = None, check: Any = None, symbols: Mapping[str, Mapping[str, Any]] | None = None):
        self.identity = identity or Identity("MT5", "Demo", "42"); self.outcome = outcome or {"retcode": 10009}; self.check_result = check if check is not None else {"retcode": 0}; self.requests = []; self.checks = []; self.positions = {}; self.symbols = dict(symbols or {"EURUSD": {"digits": 5, "point": "0.00001", "volume_min": "0.01", "volume_max": "100", "volume_step": "0.01", "visible": True, "trade_allowed": True}})
    def initialize(self, terminal_path): return None
    def account_snapshot(self): return AccountSnapshot("fake", self.identity, self.identity.external_account_id, "USD", 100, "100", "100", True, "HEDGING")
    def symbol_info(self, symbol): return ReadSnapshot("symbol_info", {"symbol": symbol, **self.symbols[symbol]})
    def quote(self, symbol): return ReadSnapshot("quote", {"symbol": symbol, "bid": "1.10000", "ask": "1.10002"})
    def closed_m1(self, symbol, count): return ReadSnapshot("candles", {"symbol": symbol, "closed_only": True, "items": []})
    def open_orders(self): return ReadSnapshot("orders", {"items": []})
    def open_positions(self): return ReadSnapshot("positions", {"items": list(self.positions.values())})
    def history_orders(self, from_server_time=None): return ReadSnapshot("history_orders", {"items": []})
    def history_deals(self, from_server_time=None): return ReadSnapshot("history_deals", {"items": []})
    def order_check(self, typ, payload): self.checks.append((typ, dict(payload))); return self.check_result
    def invoke(self, typ, payload): self.requests.append((typ, dict(payload))); return self.outcome() if callable(self.outcome) else self.outcome
    def submit_market(self, payload): self.requests.append(("order.submit_market", dict(payload))); return self.outcome
    def modify_protection(self, payload): self.requests.append(("position.modify_protection", dict(payload))); return self.outcome
    def close(self, payload): self.requests.append(("position.close", dict(payload))); return self.outcome

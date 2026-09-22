"""Capability-based MetaTrader 5 broker adapters.

The official package is imported lazily so protocol and fake tests run anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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
    digits: int
    point: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    trade_stops_level: Decimal
    trade_freeze_level: Decimal
    filling_mode: int
    trade_allowed: bool


class OfficialMT5Adapter:
    """Thin, fail-closed wrapper around the official MetaTrader5 module."""

    ACCEPTED_RETCODES = {10008, 10009, 10010}
    # These are terminal decisions documented by MT5. Anything else is kept
    # ambiguous because an unrecognised code may describe a broker side effect.
    REJECTED_RETCODES = {
        10006, 10007, 10011, 10013, 10014, 10015, 10016, 10017, 10018,
        10019, 10022, 10025, 10026, 10027, 10029, 10030, 10035,
    }
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
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value

    def _account_snapshot(self, info: Any) -> AccountSnapshot:
        return AccountSnapshot(
            self.account_id,
            self.expected,
            str(info.login),
            str(getattr(info, "currency", "")),
            int(getattr(info, "leverage", 0)),
            str(getattr(info, "balance", 0)),
            str(getattr(info, "equity", 0)),
            bool(getattr(info, "trade_allowed", False)),
            str(getattr(info, "margin_mode", "")),
        )

    def account_snapshot(self) -> AccountSnapshot:
        return self._account_snapshot(self._info())

    def preflight_facts(self) -> dict[str, Any]:
        """Read account and terminal permission without broker side effects."""
        if not self._initialized:
            raise AdapterError("MT5_NOT_INITIALIZED")
        try:
            mt5 = self._module()
            account = self._info()
            terminal = mt5.terminal_info()
            if terminal is None:
                raise AdapterError("MT5_HEALTH_UNAVAILABLE")
            return {
                "account_id": self.account_id,
                **self.expected.to_dict(),
                "trade_mode": "DEMO" if getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO else "OTHER",
                "terminal_connected": bool(getattr(terminal, "connected", False)),
                "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
                "account_trade_allowed": bool(getattr(account, "trade_allowed", False)),
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("MT5_HEALTH_UNAVAILABLE") from exc

    def _visible_symbol(self, symbol: str) -> Any:
        mt5 = self._module()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise AdapterError("SYMBOL_NOT_FOUND")
        if not bool(getattr(info, "visible", False)):
            if not mt5.symbol_select(symbol, True):
                raise AdapterError("SYMBOL_NOT_VISIBLE")
            info = mt5.symbol_info(symbol)
            if info is None:
                raise AdapterError("SYMBOL_NOT_VISIBLE")
        return info

    def _symbol(self, symbol: str) -> tuple[Any, _Symbol]:
        info = self._visible_symbol(symbol)
        return info, _Symbol(
            int(getattr(info, "digits", 0)),
            Decimal(str(getattr(info, "point", "0"))),
            Decimal(str(getattr(info, "volume_min", "0"))),
            Decimal(str(getattr(info, "volume_max", "0"))),
            Decimal(str(getattr(info, "volume_step", "0"))),
            Decimal(str(getattr(info, "trade_stops_level", 0))),
            Decimal(str(getattr(info, "trade_freeze_level", 0))),
            int(getattr(info, "filling_mode", 0)),
            bool(getattr(info, "trade_allowed", True)),
        )

    def symbol_info(self, symbol):
        return ReadSnapshot("symbol_info", self._plain(self._visible_symbol(symbol)))

    def quote(self, symbol):
        self._visible_symbol(symbol)
        tick = self._module().symbol_info_tick(symbol)
        if tick is None:
            raise AdapterError("QUOTE_UNAVAILABLE")
        return ReadSnapshot("quote", self._plain(tick) | {"symbol": symbol})

    def closed_m1(self, symbol, count):
        if count < 1:
            raise AdapterError("INVALID_CANDLE_COUNT")
        mt5 = self._module()
        self._visible_symbol(symbol)
        rows = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, count)
        if rows is None:
            raise AdapterError("CANDLE_READ_FAILED")
        return ReadSnapshot("candles", {"symbol": symbol, "timeframe": "M1",
            "closed_only": True, "items": [self._plain(row) for row in rows]})

    def _rows(self, name: str, *args):
        rows = getattr(self._module(), name)(*args)
        if rows is None:
            raise AdapterError(f"{name.upper()}_FAILED")
        items = [self._plain(row) for row in rows]
        return ReadSnapshot(name.replace("_get", ""), {"items": items})

    def open_orders(self):
        return self._rows("orders_get")

    def open_positions(self):
        return self._rows("positions_get")

    @staticmethod
    def _history_window(value: str | None):
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=1)
        if value:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise AdapterError("HISTORY_TIMEZONE_REQUIRED")
            start = parsed.astimezone(timezone.utc) - timedelta(minutes=5)
        return start, end

    def history_orders(self, from_server_time: str | None = None):
        return self._rows("history_orders_get", *self._history_window(from_server_time))

    def history_deals(self, from_server_time: str | None = None):
        return self._rows("history_deals_get", *self._history_window(from_server_time))

    @staticmethod
    def _decimal(value, field):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AdapterError(f"INVALID_{field.upper()}") from exc

    def _quantize(self, value, quantum, field):
        value = self._decimal(value, field)
        if quantum <= 0:
            raise AdapterError("INVALID_SYMBOL_METADATA")
        return (value / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum

    def _comment(self, payload):
        value = str(payload.get("comment") or payload.get("correlation_id") or "connector")
        return value[:self.comment_limit]

    def _request(self, typ: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        mt5 = self._module()
        symbol = str(payload.get("symbol") or "")
        if not symbol:
            raise AdapterError("SYMBOL_REQUIRED")
        _, meta = self._symbol(symbol)
        account = self._info()
        if not bool(getattr(account, "trade_allowed", False)) or not meta.trade_allowed:
            raise AdapterError("TRADING_NOT_ALLOWED")
        if typ == "position.modify_protection":
            digits = Decimal(1).scaleb(-meta.digits)
            position = self._fresh_position(payload.get("position_ticket"))
            if str(position.get("symbol")) != symbol:
                raise AdapterError("POSITION_SYMBOL_MISMATCH")
            sl = payload.get("sl") if payload.get("sl") is not None else position.get("sl")
            tp = payload.get("tp") if payload.get("tp") is not None else position.get("tp")
            self._validate_protection_distance(position, sl, tp, meta)
            return {
                "action": getattr(mt5, "TRADE_ACTION_SLTP"),
                "symbol": symbol,
                "position": self._ticket(payload.get("position_ticket")),
                "sl": float(self._price(sl, digits)),
                "tp": float(self._price(tp, digits)),
            }
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise AdapterError("QUOTE_UNAVAILABLE")

        position = None
        side = str(payload.get("side") or payload.get("direction") or "").upper()
        if typ == "position.close":
            position = self._fresh_position(payload.get("position_ticket"))
            position_type = position.get("type", position.get("position_type"))
            # MT5 position type 0 is BUY and 1 is SELL. The close is opposite.
            side = "SELL" if str(position_type) in {"0", "BUY"} else "BUY"
        if side not in {"BUY", "SELL"}:
            raise AdapterError("INVALID_DIRECTION")
        buy = side == "BUY"
        order_type = getattr(mt5, "ORDER_TYPE_BUY" if buy else "ORDER_TYPE_SELL")
        price = self._decimal(getattr(tick, "ask" if buy else "bid", 0), "price")
        digits = Decimal(1).scaleb(-meta.digits)
        volume = self._volume(payload.get("volume"), meta)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL"),
            "symbol": symbol,
            "type": order_type,
            "price": float(price.quantize(digits)),
            "volume": float(volume),
            "deviation": int(payload.get("deviation", 20)),
            "magic": self.magic,
            "comment": self._comment(payload),
            "type_time": getattr(mt5, "ORDER_TIME_GTC"),
        }
        filling = payload.get("type_filling")
        request["type_filling"] = int(
            filling if filling is not None else getattr(mt5, "ORDER_FILLING_IOC", 1)
        )
        if filling is not None and not (
            meta.filling_mode == 0 or meta.filling_mode & (1 << int(filling))
        ):
            raise AdapterError("INVALID_FILLING_MODE")
        if typ == "order.submit_market":
            if payload.get("sl") in (None, "") or payload.get("tp") in (None, ""):
                raise AdapterError("NATIVE_PROTECTION_REQUIRED")
            sl = self._price(payload["sl"], digits)
            tp = self._price(payload["tp"], digits)
            minimum = meta.point * max(meta.trade_stops_level, meta.trade_freeze_level)
            invalid_stops = (
                (buy and (sl >= price - minimum or tp <= price + minimum))
                or (not buy and (sl <= price + minimum or tp >= price - minimum))
            )
            if minimum and invalid_stops:
                raise AdapterError("INVALID_STOPS")
            request["sl"] = float(sl)
            request["tp"] = float(tp)
        elif typ == "position.close":
            request["position"] = self._ticket(
                position.get("ticket", payload.get("position_ticket"))
            )
            available = self._decimal(position.get("volume"), "position_volume")
            if volume > available:
                raise AdapterError("VOLUME_EXCEEDS_POSITION")
        return request

    def _validate_protection_distance(self, position: Mapping[str, Any], sl: Any,
                                      tp: Any, meta: _Symbol) -> None:
        """Reject protection levels inside the broker's stop/freeze distance."""
        minimum = meta.point * max(meta.trade_stops_level, meta.trade_freeze_level)
        if minimum <= 0:
            return
        tick = self._module().symbol_info_tick(str(position.get("symbol", "")))
        if tick is None:
            raise AdapterError("QUOTE_UNAVAILABLE")
        position_type = str(position.get("type", position.get("position_type", ""))).upper()
        if position_type not in {"0", "1", "BUY", "SELL"}:
            raise AdapterError("INVALID_POSITION_DIRECTION")
        is_buy = position_type in {"0", "BUY"}
        reference = self._decimal(
            getattr(tick, "bid" if is_buy else "ask", 0), "price"
        )
        stop = self._decimal(sl, "stop") if sl not in (None, "", 0, "0") else None
        target = self._decimal(tp, "take_profit") if tp not in (None, "", 0, "0") else None
        if is_buy:
            invalid = (
                (stop is not None and stop >= reference - minimum)
                or (target is not None and target <= reference + minimum)
            )
        else:
            invalid = (
                (stop is not None and stop <= reference + minimum)
                or (target is not None and target >= reference - minimum)
            )
        if invalid:
            raise AdapterError("INVALID_STOPS")

    def _price(self, value, digits):
        result = self._quantize(value, digits, "price")
        if result <= 0:
            raise AdapterError("INVALID_PRICE")
        return result

    def _volume(self, value, meta):
        result = self._quantize(value, meta.volume_step, "volume")
        if result < meta.volume_min or result > meta.volume_max or result <= 0:
            raise AdapterError("INVALID_VOLUME")
        return result

    @staticmethod
    def _ticket(value):
        if value in (None, ""):
            raise AdapterError("POSITION_TICKET_REQUIRED")
        return int(value)

    def _fresh_position(self, ticket):
        ticket = self._ticket(ticket)
        rows = self._module().positions_get(ticket=ticket)
        if not rows:
            raise AdapterError("POSITION_NOT_FOUND")
        row = self._plain(rows[0])
        position = row if isinstance(row, dict) else vars(row)
        if str(position.get("ticket")) != str(ticket):
            raise AdapterError("POSITION_TICKET_MISMATCH")
        return position

    def _effect_readback(self, typ: str, payload: Mapping[str, Any],
                         result: Mapping[str, Any]) -> dict[str, Any] | None:
        """Find broker evidence for an accepted market side effect.

        An accepted retcode is not enough to project a confirmed effect. The
        terminal must return a matching order, deal, or position from a fresh
        broker read. Missing read APIs, query failures, and unmatched rows all
        stay ambiguous so recovery can reconcile them later.
        """
        mt5 = self._module()
        expected_ids = {
            str(result.get(name))
            for name in ("order", "deal", "position", "ticket", "external_order_id", "external_deal_id")
            if result.get(name) is not None
        }
        requested_ticket = str(payload.get("position_ticket")) if typ == "position.close" else None
        correlation = str(payload.get("comment") or payload.get("correlation_id") or "")
        expected_magic = str(payload.get("magic", self.magic))
        rows: list[tuple[str, dict[str, Any]]] = []

        def collect(name: str, *args: Any, **kwargs: Any) -> None:
            reader = getattr(mt5, name, None)
            if not callable(reader):
                return
            try:
                values = reader(*args, **kwargs)
            except Exception:
                return
            if values is None:
                return
            for value in values:
                plain = self._plain(value)
                if isinstance(plain, dict):
                    rows.append((name, plain))

        # Active orders cover PLACED responses. History covers completed market
        # orders and deals. Positions provide the remaining broker-side proof.
        collect("orders_get")
        start, end = self._history_window(None)
        collect("history_orders_get", start, end)
        collect("history_deals_get", start, end)
        if requested_ticket is not None:
            collect("positions_get", ticket=int(requested_ticket))
        else:
            collect("positions_get")

        matches: list[dict[str, Any]] = []
        for source, row in rows:
            status = str(row.get("status", row.get("state", ""))).upper()
            if status in {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED", "NOT_FOUND"}:
                continue
            row_ids = {
                str(row.get(name))
                for name in ("ticket", "order", "deal", "position", "order_id", "deal_id", "position_id")
                if row.get(name) is not None
            }
            linked = bool(expected_ids.intersection(row_ids))
            if typ == "position.close" and requested_ticket is not None:
                linked = linked or str(row.get("position", row.get("position_id", ""))) == requested_ticket
            if correlation and str(row.get("comment", row.get("correlation_id", ""))) == correlation:
                linked = linked or str(row.get("magic", "")) in {"", expected_magic}
            if linked:
                matches.append({"_source": source, **row})
        if not matches:
            return None

        evidence: dict[str, Any] = {"readback_confirmed": True}
        for source, target in (("order", "external_order_id"), ("deal", "external_deal_id")):
            value = result.get(source)
            if value is None:
                value = next((row.get(source, row.get(target)) for row in matches
                              if row.get(source, row.get(target)) is not None), None)
            if value is not None:
                evidence[target] = str(value)
        if typ == "position.close":
            volume = next((row.get("volume", row.get("filled_volume")) for row in matches
                           if row.get("_source") == "history_deals_get"
                           and row.get("volume", row.get("filled_volume")) is not None), None)
            if volume in (None, "", 0, "0"):
                return None
            evidence["filled_volume"] = str(volume)
        return evidence

    def order_check(self, typ, payload):
        request = self._request(typ, payload)
        result = self._module().order_check(request)
        if result is None:
            raise AdapterError("ORDER_CHECK_UNAVAILABLE")
        plain = self._plain(result)
        if isinstance(plain, dict):
            return plain
        return {"retcode": getattr(result, "retcode", None)}

    def _send(self, request, *, protection: Mapping[str, Any] | None = None,
              effect: tuple[str, Mapping[str, Any]] | None = None):
        try:
            result = self._module().order_send(request)
        except Exception:
            return {"state": "UNKNOWN", "code": "TRANSPORT_AMBIGUOUS"}
        if result is None:
            return {"state": "UNKNOWN", "code": "TRANSPORT_AMBIGUOUS"}

        result = self._plain(result)
        if isinstance(result, dict):
            retcode = result.get("retcode")
        else:
            retcode = None
            result = {"retcode": retcode}

        if retcode in self.ACCEPTED_RETCODES:
            state = "ACCEPTED"
        elif retcode in self.REJECTED_RETCODES:
            state = "REJECTED"
        elif retcode in self.UNKNOWN_RETCODES:
            state = "UNKNOWN"
        else:
            state = "UNKNOWN"

        result["state"] = state
        result["code"] = (
            "BROKER_REJECTED" if state == "REJECTED"
            else "UNKNOWN_RETCODE" if state == "UNKNOWN" and retcode is not None
            else None
        )
        for source, target in (
            ("order", "external_order_id"),
            ("deal", "external_deal_id"),
            ("position", "position_tickets"),
        ):
            if result.get(source) is not None:
                if target == "position_tickets":
                    result[target] = [str(result[source])]
                else:
                    result[target] = str(result[source])
        if protection is not None and state == "ACCEPTED":
            try:
                position = self._fresh_position(protection["position_ticket"])
                digits = Decimal(1).scaleb(-int(protection["digits"]))
                expected_sl = Decimal(str(protection["sl"])).quantize(digits)
                expected_tp = Decimal(str(protection["tp"])).quantize(digits)
                actual_sl = Decimal(str(position.get("sl"))).quantize(digits)
                actual_tp = Decimal(str(position.get("tp"))).quantize(digits)
                if actual_sl != expected_sl or actual_tp != expected_tp:
                    raise ValueError
                result["protection_confirmed"] = True
                result["confirmed_stop"] = str(actual_sl)
                result["confirmed_take_profit"] = str(actual_tp)
            except (AdapterError, InvalidOperation, TypeError, ValueError, KeyError):
                result["state"] = "UNKNOWN"
                result["code"] = "PROTECTION_READBACK_MISMATCH"
                result["protection_confirmed"] = False
        if effect is not None and state == "ACCEPTED":
            evidence = self._effect_readback(effect[0], effect[1], result)
            if evidence is None:
                result["state"] = "UNKNOWN"
                result["code"] = "EFFECT_READBACK_REQUIRED"
                result["readback_confirmed"] = False
            else:
                result.update(evidence)
        return result

    def submit_market(self, payload):
        return self._send(
            self._request("order.submit_market", payload),
            effect=("order.submit_market", payload),
        )

    def modify_protection(self, payload):
        request = self._request("position.modify_protection", payload)
        _, meta = self._symbol(str(payload.get("symbol") or ""))
        return self._send(
            request,
            protection={
                "position_ticket": payload.get("position_ticket"),
                "digits": meta.digits,
                "sl": request["sl"],
                "tp": request["tp"],
            },
        )

    def close(self, payload):
        return self._send(
            self._request("position.close", payload),
            effect=("position.close", payload),
        )

    def invoke(self, typ, payload):
        if typ == "order.submit_market":
            return self.submit_market(payload)
        if typ == "position.modify_protection":
            return self.modify_protection(payload)
        if typ == "position.close":
            return self.close(payload)
        raise KeyError(typ)

    def health(self):
        info = self._info()
        mt5 = self._module()
        return {
            "initialized": self._initialized,
            "terminal": self._plain(mt5.terminal_info()),
            "version": self._plain(mt5.version()),
            "account": self._account_snapshot(info).to_dict(),
            "trade_allowed": bool(getattr(info, "trade_allowed", False)),
        }


class FakeMT5Adapter:
    """Deterministic adapter for unit tests; records exact requests and outcomes."""

    ACCEPTED_RETCODES = OfficialMT5Adapter.ACCEPTED_RETCODES

    DEFAULT_SYMBOLS = {
        "EURUSD": {
            "digits": 5,
            "point": "0.00001",
            "volume_min": "0.01",
            "volume_max": "100",
            "volume_step": "0.01",
            "visible": True,
            "trade_allowed": True,
        }
    }

    def __init__(
        self,
        identity: Identity | None = None,
        *,
        outcome: Any = None,
        check: Any = None,
        symbols: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.identity = identity or Identity("MT5", "Demo", "42")
        self.outcome = outcome or {"retcode": 10009}
        self.check_result = check if check is not None else {"retcode": 0}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.checks: list[tuple[str, dict[str, Any]]] = []
        self.positions: dict[Any, Mapping[str, Any]] = {}
        self.symbols = dict(symbols or self.DEFAULT_SYMBOLS)

    def initialize(self, terminal_path):
        return None

    def account_snapshot(self):
        return AccountSnapshot(
            "fake",
            self.identity,
            self.identity.external_account_id,
            "USD",
            100,
            "100",
            "100",
            True,
            "HEDGING",
        )

    def symbol_info(self, symbol):
        return ReadSnapshot("symbol_info", {"symbol": symbol, **self.symbols[symbol]})

    def quote(self, symbol):
        return ReadSnapshot(
            "quote", {"symbol": symbol, "bid": "1.10000", "ask": "1.10002"}
        )

    def closed_m1(self, symbol, count):
        return ReadSnapshot(
            "candles", {"symbol": symbol, "closed_only": True, "items": []}
        )

    def open_orders(self):
        return ReadSnapshot("orders", {"items": []})

    def open_positions(self):
        return ReadSnapshot("positions", {"items": list(self.positions.values())})

    def history_orders(self, from_server_time=None):
        return ReadSnapshot("history_orders", {"items": []})

    def history_deals(self, from_server_time=None):
        return ReadSnapshot("history_deals", {"items": []})

    def order_check(self, typ, payload):
        self.checks.append((typ, dict(payload)))
        return self.check_result

    def _record(self, typ, payload, *, resolve_outcome=False):
        self.requests.append((typ, dict(payload)))
        if resolve_outcome and callable(self.outcome):
            return self.outcome()
        return self.outcome

    def invoke(self, typ, payload):
        return self._record(typ, payload, resolve_outcome=True)

    def submit_market(self, payload):
        return self._record("order.submit_market", payload)

    def modify_protection(self, payload):
        result = self._record("position.modify_protection", payload)
        if isinstance(result, Mapping) and result.get("retcode") in self.ACCEPTED_RETCODES:
            return {
                **result,
                "protection_confirmed": True,
                "confirmed_stop": payload.get("sl"),
                "confirmed_take_profit": payload.get("tp"),
            }
        return result

    def close(self, payload):
        return self._record("position.close", payload)

"""Post-AI safety checks.

The model never gets the final word over newer market data. This module checks
the returned recommendation against the live setup state and discards stale
output instead of acting on an old snapshot.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from setup_config import Setup, utc_iso

ALLOWED_DECISIONS = {
    "WAIT_FOR_CONFIRMATION",
    "WAIT_FOR_ENTRY_ZONE",
    "PLACE_ENTRY_ORDER_NOW",
    "TARGET_REACHED_BEFORE_ENTRY",
    "TARGET_REACHED_CHECK_ORDER",
    "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS",
    "SETUP_INVALIDATED",
    "DATA_INVALID",
    "ENTRY_ORDER_ARMED",
}

TERMINAL_STATES = {
    "TARGET_REACHED_BEFORE_ENTRY",
    "TARGET_REACHED_CHECK_ORDER",
    "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS",
    "SETUP_INVALIDATED",
}


def _override(result: dict[str, Any], decision: str, reason: str, guard: dict[str, Any]) -> dict[str, Any]:
    result["decision"] = decision
    result["reason"] = reason
    result["order"] = None
    guard["changed"] = True
    guard["reasons"].append(reason)
    return result


def guard_ai_result(
    result: dict[str, Any] | None,
    *,
    setup: Setup,
    live_price: float | None,
    live_price_age_seconds: float | None,
    max_data_age_seconds: float,
    snapshot_price: float | None = None,
    snapshot_time_utc: str | None = None,
    terminal_state: str | None = None,
    terminal_reason: str | None = None,
    order_armed: bool = False,
    target_touched_since_d: bool | None = None,
    stop_touched_since_d: bool | None = None,
    stale_price_move_pct: float = 0.002,
    order_type: str = "STOP_LOSS_LIMIT",
    time_in_force: str = "GTC",
    now_utc: str | None = None,
) -> dict[str, Any]:
    guard: dict[str, Any] = {
        "changed": False,
        "reasons": [],
        "live_price": live_price,
        "snapshot_price": snapshot_price,
        "live_price_age_seconds": round(float(live_price_age_seconds), 3) if live_price_age_seconds is not None else None,
    }
    if not isinstance(result, dict):
        return {
            "decision": "DATA_INVALID",
            "reason": "AI nije vratio ispravan JSON objekat; preporuka se odbacuje.",
            "setup_id": setup.setup_id,
            "data_timestamp_utc": snapshot_time_utc or now_utc or utc_iso(),
            "order": None,
            "stale_guard": guard,
        }

    original = copy.deepcopy(result)
    decision = str(result.get("decision") or "")
    if decision not in ALLOWED_DECISIONS:
        _override(
            result,
            "DATA_INVALID",
            f"AI je vratio nepoznatu odluku ({decision or 'prazno'}); preporuka se odbacuje.",
            guard,
        )
    elif result.get("setup_id") and str(result.get("setup_id")) != setup.setup_id:
        _override(
            result,
            "DATA_INVALID",
            "AI odgovor pripada drugom setup-u (setup_id se ne poklapa); odbačen je.",
            guard,
        )

    result["setup_id"] = setup.setup_id
    result["data_timestamp_utc"] = snapshot_time_utc or result.get("data_timestamp_utc") or now_utc or utc_iso()
    if not isinstance(result.get("supporting_evidence"), list):
        result["supporting_evidence"] = []
    if not isinstance(result.get("contradicting_evidence"), list):
        result["contradicting_evidence"] = []
    if not isinstance(result.get("entry_conditions_passed"), list):
        result["entry_conditions_passed"] = []
    if not isinstance(result.get("entry_conditions_missing"), list):
        result["entry_conditions_missing"] = []

    terminal_map = {
        "TARGET_REACHED_BEFORE_ENTRY": "TARGET_REACHED_BEFORE_ENTRY",
        "TARGET_REACHED_CHECK_ORDER": "TARGET_REACHED_CHECK_ORDER",
        "SETUP_INVALIDATED": "SETUP_INVALIDATED",
        "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS": "DATA_INVALID",
    }
    if result.get("decision") != "DATA_INVALID" and terminal_state in terminal_map:
        mapped = terminal_map[terminal_state]
        _override(
            result,
            mapped,
            terminal_reason or f"Stanje setupa se promenilo dok je AI rezonovao: {terminal_state}.",
            guard,
        )
    elif live_price is None:
        _override(result, "DATA_INVALID", "Aktuelna cena nije dostupna nakon AI analize.", guard)
    elif live_price_age_seconds is not None and live_price_age_seconds > max_data_age_seconds:
        _override(
            result,
            "DATA_INVALID",
            f"Market podaci su zastareli ({live_price_age_seconds:.1f}s); AI preporuka se ne koristi.",
            guard,
        )
    elif setup.target_hit(live_price):
        mapped = "TARGET_REACHED_CHECK_ORDER" if order_armed else "TARGET_REACHED_BEFORE_ENTRY"
        _override(
            result,
            mapped,
            f"Dok je AI rezonovao, cena je dostigla TG1 ({setup.tg1:.2f}). Nema nove entry odluke za ovaj setup.",
            guard,
        )
    elif setup.stop_hit(live_price):
        _override(
            result,
            "SETUP_INVALIDATED",
            f"Dok je AI rezonovao, cena je dostigla SL ({setup.sl:.2f}); setup je poni\u0161ten.",
            guard,
        )
    elif result.get("decision") == "PLACE_ENTRY_ORDER_NOW" and order_armed:
        _override(
            result,
            "ENTRY_ORDER_ARMED",
            "Entry nalog je ve\u0107 potvr\u0111en kao postavljen; nova preporuka nije potrebna.",
            guard,
        )
    elif result.get("decision") == "PLACE_ENTRY_ORDER_NOW":
        if not setup.in_pre_entry_zone(live_price):
            if setup.past_entry_before_target(live_price):
                _override(
                    result,
                    "WAIT_FOR_ENTRY_ZONE",
                    f"Cena je sada na {live_price:.2f}, na pravoj strani ENTRY ka TG1. Sačekati vraćanje ispod ENTRY (long) / iznad ENTRY (short) pre nove preporuke.",
                    guard,
                )
            else:
                _override(
                    result,
                    "WAIT_FOR_CONFIRMATION",
                    "Cena više nije u zoni pre ENTRY; AI preporuka za postavljanje naloga je odbačena.",
                    guard,
                )
        elif snapshot_price is not None and snapshot_price > 0:
            move = abs((live_price / snapshot_price) - 1.0)
            guard["price_move_pct"] = round(move * 100, 4)
            if move > stale_price_move_pct:
                _override(
                    result,
                    "WAIT_FOR_CONFIRMATION",
                    (
                        f"Cena se promenila {move * 100:.2f}% od AI snapshot-a "
                        f"({snapshot_price:.2f} → {live_price:.2f}); preporuka je zastarela i biće ponovo procenjena."
                    ),
                    guard,
                )
        if result.get("decision") == "PLACE_ENTRY_ORDER_NOW":
            result["order"] = setup.order_details(order_type=order_type, time_in_force=time_in_force)
    elif result.get("decision") == "WAIT_FOR_ENTRY_ZONE" and setup.in_pre_entry_zone(live_price):
        _override(
            result,
            "WAIT_FOR_CONFIRMATION",
            f"Cena se vratila u zonu pre ENTRY ({live_price:.2f}); nastavlja se procena ulaza.",
            guard,
        )
    elif result.get("decision") == "TARGET_REACHED_BEFORE_ENTRY" and target_touched_since_d is False:
        _override(
            result,
            "WAIT_FOR_CONFIRMATION",
            "AI je naveo da je TG1 dostignut, ali dostavljeni podaci to ne potvrđuju. Setup se ne gasi na osnovu nepotkrepljene tvrdnje.",
            guard,
        )
    elif result.get("decision") == "SETUP_INVALIDATED" and stop_touched_since_d is False:
        _override(
            result,
            "WAIT_FOR_CONFIRMATION",
            "AI je naveo da je SL dostignut, ali dostavljeni podaci to ne potvrđuju. Setup se ne gasi na osnovu nepotkrepljene tvrdnje.",
            guard,
        )

    if result.get("decision") != "PLACE_ENTRY_ORDER_NOW":
        result["order"] = None
    if guard["changed"]:
        result["original_ai_decision"] = original.get("decision")
        result["original_reason"] = original.get("reason")
    result["stale_guard"] = guard
    # Keep JSON-serializable in all paths.
    json.dumps(result, ensure_ascii=False)
    return result

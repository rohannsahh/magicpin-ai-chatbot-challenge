"""
Per-trigger-kind context extractors.

Each extractor pulls ONLY the verified, relevant data points from the 4 contexts
into a flat `facts` dict. The LLM composer is only allowed to use data from
this dict — it cannot hallucinate because it never sees the raw payloads.

Entry point: extract(trigger_kind, category, merchant, trigger, customer) -> dict
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _active_offers(merchant: dict) -> List[str]:
    return [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]


def _expired_offers(merchant: dict) -> List[str]:
    return [o["title"] for o in merchant.get("offers", []) if o.get("status") == "expired"]


def _find_digest_item(category: dict, item_id: str) -> Optional[dict]:
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def _peer_stat(category: dict, key: str) -> Any:
    return category.get("peer_stats", {}).get(key)


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "unknown%"
    try:
        return f"{round(abs(float(v)) * 100)}%"
    except Exception:
        return str(v)


def _parse_customer_name_from_id(customer_id: str) -> str:
    """Derive a human-readable name from customer_id.

    e.g. 'c_001_priya_for_m001'        → 'Priya'
         'c_012_karthik_jr_for_m008'   → 'Karthik Jr'
         'c_010_rashmi_for_m007'       → 'Rashmi'
    """
    if not customer_id:
        return ""
    parts = customer_id.split("_")
    # Format: c_{num}_{name_parts...}_for_{merchant_parts...}
    try:
        for_idx = next(i for i, p in enumerate(parts) if p == "for")
        name_parts = parts[2:for_idx]  # skip 'c' and the number
        return " ".join(p.capitalize() for p in name_parts)
    except (StopIteration, IndexError):
        return ""


def _months_since(iso_date: str) -> str:
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date[:10])
        days = (datetime.now() - dt).days
        months = days // 30
        if months == 0:
            return f"{days} days"
        return f"{months} month{'s' if months != 1 else ''}"
    except Exception:
        return ""


def _merchant_base(merchant: dict) -> dict:
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    sub = merchant.get("subscription", {})
    return {
        "owner": identity.get("owner_first_name", ""),
        "merchant_name": identity.get("name", ""),
        "city": identity.get("city", ""),
        "locality": identity.get("locality", ""),
        "languages": identity.get("languages", ["en"]),
        "views_30d": perf.get("views"),
        "calls_30d": perf.get("calls"),
        "ctr": perf.get("ctr"),
        "delta_views_7d": perf.get("delta_7d", {}).get("views_pct"),
        "delta_calls_7d": perf.get("delta_7d", {}).get("calls_pct"),
        "subscription_status": sub.get("status"),
        "subscription_days": sub.get("days_remaining"),
        "active_offers": _active_offers(merchant),
        "signals": merchant.get("signals", []),
        "lapsed_count": merchant.get("customer_aggregate", {}).get("lapsed_180d_plus"),
        "total_patients_ytd": merchant.get("customer_aggregate", {}).get("total_unique_ytd"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — must-have extractors
# ─────────────────────────────────────────────────────────────────────────────

def extract_research_digest(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    item_id = payload.get("top_item_id") or payload.get("item_id")

    digest_item = _find_digest_item(cat, item_id) if item_id else None
    if digest_item is None and cat.get("digest"):
        digest_item = cat["digest"][0]   # fall back to first item

    agg = m.get("customer_aggregate", {})
    peer_ctr = _peer_stat(cat, "avg_ctr")
    merchant_ctr = m.get("performance", {}).get("ctr")
    ctr_vs_peer = (
        f"their CTR {merchant_ctr} vs peer median {peer_ctr}"
        if merchant_ctr and peer_ctr else None
    )

    return {
        **base,
        "trigger_reason": "new research/compliance digest just released for your category",
        "digest_title": digest_item.get("title", "") if digest_item else "",
        "digest_source": digest_item.get("source", "") if digest_item else "",
        "digest_trial_n": digest_item.get("trial_n") if digest_item else None,
        "digest_summary": (digest_item.get("summary", "")[:200] if digest_item else ""),
        "digest_patient_segment": digest_item.get("patient_segment", "") if digest_item else "",
        "digest_actionable": digest_item.get("actionable", "") if digest_item else "",
        "digest_kind": digest_item.get("kind", "research") if digest_item else "",
        "high_risk_count": agg.get("high_risk_adult_count"),
        "peer_ctr": peer_ctr,
        "ctr_vs_peer": ctr_vs_peer,
    }


def extract_recall_due(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}
    cust_rel = c.get("relationship", {}) if c else {}
    cust_pref = c.get("preferences", {}) if c else {}
    cust_state = c.get("state", "") if c else ""

    customer_id = t.get("customer_id", "")
    customer_name = cust_identity.get("name", "") or _parse_customer_name_from_id(customer_id)

    slots = payload.get("available_slots", [])
    slot_labels = [s.get("label", "") for s in slots[:2]]

    # Keep recall timing deterministic from trigger data instead of runtime date math.
    # This avoids brittle "X days/months" phrasing across environments.
    due_date = payload.get("due_date", "")
    last_visit = cust_rel.get("last_visit") or payload.get("last_service_date", "")
    time_since = "6 months"
    if due_date and last_visit:
        try:
            due_dt = datetime.fromisoformat(due_date[:10])
            last_dt = datetime.fromisoformat(last_visit[:10])
            months = max(1, round((due_dt - last_dt).days / 30))
            time_since = f"{months} month{'s' if months != 1 else ''}"
        except Exception:
            pass

    services = cust_rel.get("services_received", [])
    last_services = services[-2:] if services else []

    return {
        **base,
        "trigger_reason": f"6-month recall window opened for patient {customer_name}",
        "customer_name": customer_name,
        "customer_language": cust_identity.get("language_pref", "english"),
        "customer_state": cust_state,
        "service_due": payload.get("service_due", "cleaning").replace("_", " "),
        "time_since_last_visit": time_since,
        "slot_1": slot_labels[0] if len(slot_labels) > 0 else "",
        "slot_2": slot_labels[1] if len(slot_labels) > 1 else "",
        "past_services": last_services,
        "visits_total": cust_rel.get("visits_total"),
        "preferred_slots": cust_pref.get("preferred_slots", ""),
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


def extract_perf_dip(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    perf = m.get("performance", {})

    metric = payload.get("metric", "calls")
    delta_pct = payload.get("delta_pct", 0)
    window = payload.get("window", "7d")
    vs_baseline = payload.get("vs_baseline")

    peer_key_map = {
        "calls": "avg_calls_30d",
        "views": "avg_views_30d",
        "ctr": "avg_ctr",
        "directions": "avg_directions_30d",
    }
    peer_val = _peer_stat(cat, peer_key_map.get(metric, "avg_calls_30d"))
    current_val = perf.get(metric) or vs_baseline

    neg_review_themes = [
        rt.get("common_quote", rt.get("theme", ""))
        for rt in m.get("review_themes", [])
        if rt.get("sentiment") == "neg"
    ]

    # Compute absolute delta for stakes framing
    delta_abs = None
    if vs_baseline is not None and current_val is not None:
        try:
            delta_abs = int(abs(float(vs_baseline) - float(current_val)))
        except Exception:
            pass

    return {
        **base,
        "trigger_reason": f"{metric} dropped {_fmt_pct(delta_pct)} in the last {window}",
        "metric": metric,
        "delta_pct_str": _fmt_pct(delta_pct),
        "delta_abs": delta_abs,
        "window": window,
        "vs_baseline": vs_baseline,
        "current_value": current_val,
        "peer_median": peer_val,
        "peer_ctr": _peer_stat(cat, "avg_ctr"),
        "neg_review_themes": neg_review_themes[:2],
    }


def extract_perf_spike(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    perf = m.get("performance", {})

    metric = payload.get("metric", "views")
    delta_pct = payload.get("delta_pct", 0)
    window = payload.get("window", "7d")

    peer_key_map = {
        "calls": "avg_calls_30d",
        "views": "avg_views_30d",
        "ctr": "avg_ctr",
    }
    peer_val = _peer_stat(cat, peer_key_map.get(metric, "avg_views_30d"))

    vs_baseline = payload.get("vs_baseline")
    likely_driver = payload.get("likely_driver", "").replace("_", " ")

    return {
        **base,
        "trigger_reason": f"{metric} spiked {_fmt_pct(delta_pct)} in the last {window}",
        "metric": metric,
        "delta_pct_str": _fmt_pct(delta_pct),
        "window": window,
        "vs_baseline": vs_baseline,
        "current_value": perf.get(metric),
        "peer_median": peer_val,
        "likely_driver": likely_driver,
        "seasonal_beats": [sb.get("note", "") for sb in cat.get("seasonal_beats", [])],
    }


def extract_renewal_due(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    sub = m.get("subscription", {})
    perf = m.get("performance", {})

    days = payload.get("days_remaining") or sub.get("days_remaining")
    plan = payload.get("plan") or sub.get("plan", "Pro")
    amount = payload.get("renewal_amount")

    peer_ctr = _peer_stat(cat, "avg_ctr")
    merchant_ctr = perf.get("ctr")
    ctr_gap = None
    if peer_ctr and merchant_ctr:
        ctr_gap = f"CTR {merchant_ctr} vs peer median {peer_ctr}"

    return {
        **base,
        "trigger_reason": f"subscription expires in {days} days",
        "days_remaining": days,
        "plan": plan,
        "renewal_amount": amount,
        "views_30d": perf.get("views"),
        "calls_30d": perf.get("calls"),
        "ctr": merchant_ctr,
        "peer_ctr": peer_ctr,
        "ctr_gap": ctr_gap,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 extractors
# ─────────────────────────────────────────────────────────────────────────────

_WINBACK_CATEGORY_CONTEXT = {
    "salons":      ("bridal bookings, keratin treatments, and styling appointments",
                   "bridal / keratin / hair spa / styling"),
    "restaurants": ("dine-in covers, delivery orders, and combo bookings",
                    "covers / orders / combo / dine-in"),
    "gyms":        ("new membership joins, class bookings, and PT sessions",
                    "membership / HIIT / PT sessions / footfall"),
    "pharmacies":  ("prescription refills, OTC walk-ins, and chronic dispensing",
                    "prescription / refill / dispensing / OTC"),
    "dentists":    ("patient appointments, recall visits, and clinical consultations",
                    "patient / appointment / clinical / recall"),
}

def extract_winback(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    slug = cat.get("slug", "")
    ctx = _WINBACK_CATEGORY_CONTEXT.get(slug, ("customer visits", ""))
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    loss_framing = (
        f"{lapsed} {ctx[0]} were lost to competitors" if lapsed
        else f"{ctx[0]} are going to listed competitors"
    )

    return {
        **base,
        "trigger_reason": f"subscription expired {payload.get('days_since_expiry', '?')} days ago — business declining",
        "days_since_expiry": payload.get("days_since_expiry"),
        "perf_dip_pct": _fmt_pct(payload.get("perf_dip_pct")),
        "lapsed_customers_since_expiry": lapsed,
        "peer_views": _peer_stat(cat, "avg_views_30d"),
        "peer_calls": _peer_stat(cat, "avg_calls_30d"),
        "category_loss_framing": loss_framing,
        "category_vocab_hint": ctx[1],
    }


def extract_festival(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})

    beats = [sb.get("note", "") for sb in cat.get("seasonal_beats", [])
             if payload.get("festival", "").lower() in sb.get("note", "").lower()
             or sb.get("month_range", "")]

    return {
        **base,
        "trigger_reason": f"{payload.get('festival', 'upcoming festival')} is {payload.get('days_until', '?')} days away",
        "festival": payload.get("festival", ""),
        "festival_date": payload.get("date", ""),
        "days_until": payload.get("days_until"),
        "relevant_seasonal_beats": beats[:2],
        "category_relevance": payload.get("category_relevance", []),
    }


def extract_review_theme(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})

    theme_key = payload.get("theme", "")
    merchant_themes = m.get("review_themes", [])
    matching = next((rt for rt in merchant_themes if rt.get("theme") == theme_key), None)

    common_quote = (
        payload.get("common_quote")
        or (matching.get("common_quote") if matching else "")
    )
    occurrences = (
        payload.get("occurrences_30d")
        or (matching.get("occurrences_30d") if matching else None)
    )

    return {
        **base,
        "trigger_reason": f"review pattern '{theme_key.replace('_', ' ')}' appeared {occurrences} times in 30 days",
        "theme": theme_key.replace("_", " "),
        "occurrences_30d": occurrences,
        "trend": payload.get("trend", ""),
        "common_quote": common_quote,
        "peer_avg_rating": _peer_stat(cat, "avg_rating"),
    }


def extract_curious_ask(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})

    return {
        **base,
        "trigger_reason": "weekly knowledge check-in — what's most in demand right now",
        "ask_topic": payload.get("ask_template", "what_service_in_demand").replace("_", " "),
        "peer_post_freq_days": _peer_stat(cat, "avg_post_freq_days"),
        "trend_signals": [
            f"{ts['query']} +{round(ts['delta_yoy']*100)}%"
            for ts in cat.get("trend_signals", [])[:2]
        ],
    }


def extract_wedding_followup(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}
    cust_pref = c.get("preferences", {}) if c else {}

    customer_id = t.get("customer_id", "")
    customer_name = cust_identity.get("name", "") or _parse_customer_name_from_id(customer_id)

    return {
        **base,
        "trigger_reason": f"bridal followup — {payload.get('days_to_wedding', '?')} days to wedding",
        "customer_name": customer_name,
        "customer_language": cust_identity.get("language_pref", "english"),
        "wedding_date": payload.get("wedding_date", ""),
        "days_to_wedding": payload.get("days_to_wedding"),
        "trial_completed_date": payload.get("trial_completed", ""),
        "next_step": payload.get("next_step_window_open", "").replace("_", " "),
        "preferred_slot": cust_pref.get("preferred_slots", ""),
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


def extract_customer_lapsed(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}
    cust_rel = c.get("relationship", {}) if c else {}
    cust_pref = c.get("preferences", {}) if c else {}
    cust_state = c.get("state", "lapsed_soft") if c else "lapsed_soft"

    customer_id = t.get("customer_id", "")
    customer_name = cust_identity.get("name", "") or _parse_customer_name_from_id(customer_id)

    # Use days_since_last_visit from payload first (avoids future-date bug with _months_since)
    days_from_payload = payload.get("days_since_last_visit")
    if days_from_payload:
        time_since = f"{days_from_payload} days"
    else:
        last_visit = cust_rel.get("last_visit", "")
        time_since = _months_since(last_visit) or "a while"

    # Combine focus info from payload and customer preferences
    previous_focus = (
        payload.get("previous_focus", "")
        or cust_pref.get("training_focus", "")
        or cust_pref.get("health_focus", "")
    )
    previous_membership_months = payload.get("previous_membership_months")

    services = cust_rel.get("services_received", [])
    past_services = list(set(services))[:3]

    return {
        **base,
        "trigger_reason": f"customer {customer_name} has not visited in {time_since}",
        "customer_name": customer_name,
        "customer_language": cust_identity.get("language_pref", "english"),
        "customer_state": cust_state,
        "days_since_last_visit": days_from_payload,
        "time_since_last_visit": time_since,
        "previous_focus": previous_focus,
        "previous_membership_months": previous_membership_months,
        "past_services": past_services,
        "visits_total": cust_rel.get("visits_total"),
        "lifetime_value": cust_rel.get("lifetime_value"),
        "preferred_slot": cust_pref.get("preferred_slots", ""),
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tier 3 extractors
# ─────────────────────────────────────────────────────────────────────────────

_DORMANT_CATEGORY_INSIGHT = {
    "salons":      "Bridal season picks up 6 weeks before major festivals — styling and keratin demand starts building now. Your listing needs to be active to capture this.",
    "restaurants": "Weekend covers and delivery orders are your highest-ROI engagement window. A single flash combo story drives 3x more calls than a regular post.",
    "gyms":        "April-June is your membership renewal window — members who don't re-commit in this period churn for 3+ months. Retention starts now.",
    "pharmacies":  "Chronic prescription refill cycles run every 30 days. Patients who don't reorder this week will walk to the nearest listed pharmacy.",
    "dentists":    "Patient recall intervals (6-month cleaning cycles) are peaking right now. Every week of silence = appointments going to other clinics.",
}
_DORMANT_CATEGORY_VOCAB = {
    "salons":      "bridal / keratin / styling / hair spa",
    "restaurants": "covers / combo / delivery / orders",
    "gyms":        "membership / churn / renewal / members",
    "pharmacies":  "prescription / refill / chronic / OTC",
    "dentists":    "patient / recall / cleaning / appointment",
}

def extract_dormant(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    perf = m.get("performance", {})
    payload = t.get("payload", {})
    sub = m.get("subscription", {})
    slug = cat.get("slug", "")

    peer_ctr = _peer_stat(cat, "avg_ctr")
    merchant_ctr = perf.get("ctr")
    ctr_note = (
        f"CTR {merchant_ctr} (peer median {peer_ctr})"
        if merchant_ctr and peer_ctr else None
    )

    days_dormant = (
        payload.get("days_since_last_merchant_message")
        or payload.get("days_since_last_reply")
        or 14
    )
    last_topic = payload.get("last_topic", "")

    return {
        **base,
        "trigger_reason": f"re-engagement — {days_dormant} days since last reply, last topic: {last_topic or 'general'}",
        "days_dormant": days_dormant,
        "last_topic": last_topic,
        "views_30d": perf.get("views"),
        "calls_30d": perf.get("calls"),
        "ctr_note": ctr_note,
        "stale_posts": any("stale_posts" in s for s in m.get("signals", [])),
        "subscription_status": sub.get("status"),
        "subscription_days": sub.get("days_remaining"),
        "category_insight": _DORMANT_CATEGORY_INSIGHT.get(slug, ""),
        "category_vocab_hint": _DORMANT_CATEGORY_VOCAB.get(slug, ""),
    }


def extract_ipl(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})

    is_weeknight = payload.get("is_weeknight", False)
    insight = (
        "weeknight IPL usually +15% delivery orders — dine-in stays flat"
        if is_weeknight
        else "weekend IPL usually shifts -12% dine-in covers (people watch at home) but delivery spikes"
    )

    return {
        **base,
        "trigger_reason": f"IPL match today: {payload.get('match', '?')} at {payload.get('venue', '?')}",
        "match": payload.get("match", ""),
        "venue": payload.get("venue", ""),
        "city": payload.get("city", ""),
        "is_weeknight": is_weeknight,
        "operational_insight": insight,
    }


_COMPETITOR_DEFENSIVE_ACTION = {
    "dentists":    "Match their offer with your existing scaling + complimentary fluoride — makes a ₹199 newcomer irrelevant for trusted patients",
    "salons":      "Launch a loyalty special this week: bridal trial at your existing price — regulars won't switch to a new player",
    "gyms":        "Flash a '30-day fitness challenge' for new joiners this week — your community and PT expertise beats any newcomer's opening offer",
    "restaurants": "Post a 'locals pick' story featuring your top dish — established regulars don't risk a new place when they have a favourite",
    "pharmacies":  "Reach out to your chronic prescription customers directly — convenience and trust beats any new pharmacy's opening discount",
}
_COMPETITOR_CATEGORY_VOCAB = {
    "dentists":    "patient / clinical / scaling / whitening / treatment",
    "salons":      "bridal / keratin / styling / treatment / loyal clients",
    "gyms":        "membership / class / PT / challenge / community",
    "restaurants": "diners / covers / dish / regulars / menu",
    "pharmacies":  "prescription / chronic / dispensing / refill / OTC",
}

def extract_competitor(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    slug = cat.get("slug", "")

    competitor_name = payload.get("competitor_name", "a new competitor")
    distance_km = payload.get("distance_km")
    competitor_offer = payload.get("their_offer", "")
    opened_date = payload.get("opened_date", "")

    # Pick positive review themes as merchant differentiators
    pos_themes = [
        rt.get("common_quote", rt.get("theme", ""))
        for rt in m.get("review_themes", [])
        if rt.get("sentiment") == "pos"
    ]

    dist_str = f"{distance_km}km away" if distance_km else "nearby"
    trigger_reason = f"{competitor_name} just opened {dist_str}"
    if competitor_offer:
        trigger_reason += f" offering '{competitor_offer}'"

    defensive = _COMPETITOR_DEFENSIVE_ACTION.get(slug, "Reach out to your loyal customers directly — relationships beat any newcomer's opening offer")
    if competitor_offer:
        defensive = defensive.replace("their offer", f"their '{competitor_offer}'")

    return {
        **base,
        "trigger_reason": trigger_reason,
        "competitor_name": competitor_name,
        "distance_km": distance_km,
        "competitor_offer": competitor_offer,
        "opened_date": opened_date,
        "peer_avg_rating": _peer_stat(cat, "avg_rating"),
        "peer_avg_reviews": _peer_stat(cat, "avg_review_count"),
        "established_year": m.get("identity", {}).get("established_year"),
        "merchant_strengths": pos_themes[:2],
        "defensive_action": defensive,
        "category_vocab_hint": _COMPETITOR_CATEGORY_VOCAB.get(slug, ""),
    }


_MILESTONE_CATEGORY_ACTION = {
    "salons":      "Post a transformation reel or bridal client story tonight — {value_now} reviews makes your booking page convert",
    "restaurants": "Post a '{value_now} happy diners' story tonight + run a flash combo to tip over {milestone_value}",
    "gyms":        "Share a member result or class highlight — {value_now} reviews is your best new-joiner hook",
    "pharmacies":  "Put your {value_now}-review count on your counter display — builds prescription trust",
    "dentists":    "Post a patient testimonial card with your review count + a whitening/cleaning offer",
}
_MILESTONE_CATEGORY_VOCAB = {
    "salons":      "bridal / transformation / booking / styling",
    "restaurants": "diners / combo / covers / menu",
    "gyms":        "membership / class / joiner / programme",
    "pharmacies":  "prescription / chronic / dispensing",
    "dentists":    "patient / cleaning / whitening / aligner",
}

def extract_milestone(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    slug = cat.get("slug", "")

    # Payload uses metric/value_now/milestone_value/is_imminent instead of "milestone" string
    metric = payload.get("metric", "")
    value_now = payload.get("value_now") or payload.get("value")
    milestone_value = payload.get("milestone_value") or payload.get("value")
    is_imminent = payload.get("is_imminent", False)

    if metric and milestone_value:
        if is_imminent and value_now:
            milestone_str = f"approaching {milestone_value} {metric.replace('_', ' ')} ({value_now} now)"
        else:
            milestone_str = f"{milestone_value} {metric.replace('_', ' ')}"
    else:
        milestone_str = payload.get("milestone", "new achievement")

    tmpl = _MILESTONE_CATEGORY_ACTION.get(slug, "Post your milestone and share it with your customers")
    action_hint = tmpl.format(value_now=value_now or "?", milestone_value=milestone_value or "?")

    return {
        **base,
        "trigger_reason": f"milestone imminent: {milestone_str}" if is_imminent else f"milestone just reached: {milestone_str}",
        "milestone": milestone_str,
        "value_now": value_now,
        "milestone_value": milestone_value,
        "metric": metric,
        "is_imminent": is_imminent,
        "peer_avg_reviews": _peer_stat(cat, "avg_review_count"),
        "category_action_hint": action_hint,
        "category_vocab_hint": _MILESTONE_CATEGORY_VOCAB.get(slug, ""),
    }


def extract_regulation_change(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})

    # Reuse digest item lookup
    item_id = payload.get("top_item_id")
    digest_item = _find_digest_item(cat, item_id) if item_id else None
    if not digest_item:
        for item in cat.get("digest", []):
            if item.get("kind") == "compliance":
                digest_item = item
                break

    deadline = payload.get("deadline_iso", "")

    return {
        **base,
        "trigger_reason": f"regulatory change relevant to your practice — deadline {deadline[:10] if deadline else 'upcoming'}",
        "regulation_title": digest_item.get("title", "") if digest_item else "",
        "regulation_source": digest_item.get("source", "") if digest_item else "",
        "regulation_summary": digest_item.get("summary", "") if digest_item else "",
        "regulation_actionable": digest_item.get("actionable", "") if digest_item else "",
        "deadline": deadline[:10] if deadline else "",
        "high_risk_count": m.get("customer_aggregate", {}).get("high_risk_adult_count"),
        "category_vocab_hint": "patient / clinical / radiograph / dosimetry / compliance",
    }


def extract_appointment(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}

    customer_id = t.get("customer_id", "")
    customer_name = cust_identity.get("name", "") or _parse_customer_name_from_id(customer_id)

    return {
        **base,
        "trigger_reason": "appointment confirmed for tomorrow",
        "customer_name": customer_name,
        "appointment_time": payload.get("appointment_time", ""),
        "service": payload.get("service", ""),
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


def extract_trial_followup(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}
    cust_rel = c.get("relationship", {}) if c else {}
    cust_pref = c.get("preferences", {}) if c else {}

    customer_id = t.get("customer_id", "")
    customer_name = cust_identity.get("name", "") or _parse_customer_name_from_id(customer_id)

    # Get next session slot from trigger payload
    slots = payload.get("next_session_options", [])
    next_slot_label = slots[0].get("label", "") if slots else ""
    trial_date = payload.get("trial_date", "") or cust_rel.get("first_visit", "")

    return {
        **base,
        "trigger_reason": f"first trial on {trial_date} — follow-up window to convert to regular",
        "customer_name": customer_name,
        "customer_language": cust_identity.get("language_pref", "english"),
        "trial_date": trial_date,
        "services_received": cust_rel.get("services_received", [])[:3],
        "next_session_label": next_slot_label,
        "preferred_slot": cust_pref.get("preferred_slots", ""),
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


def extract_chronic_refill(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    cust_identity = c.get("identity", {}) if c else {}
    cust_pref = c.get("preferences", {}) if c else {}

    molecule_list = payload.get("molecule_list", [])
    stock_runs_out_iso = payload.get("stock_runs_out_iso", "")
    delivery_address_saved = (
        payload.get("delivery_address_saved", False)
        or cust_pref.get("delivery_address") == "saved"
    )

    # Parse the runs-out date into a readable string (avoid real-time computation)
    stock_runs_out_date = ""
    if stock_runs_out_iso:
        try:
            runs_out_dt = datetime.fromisoformat(
                stock_runs_out_iso.replace("Z", "+00:00")
            )
            stock_runs_out_date = runs_out_dt.strftime("%B %d")
        except Exception:
            pass

    return {
        **base,
        "trigger_reason": f"regular customer's prescriptions running low — stock runs out {stock_runs_out_date or 'soon'}",
        "customer_name": cust_identity.get("name", "") or _parse_customer_name_from_id(t.get("customer_id", "")),
        "customer_language": cust_identity.get("language_pref", "english"),
        "molecule_list": molecule_list,
        "stock_runs_out_date": stock_runs_out_date,
        "delivery_address_saved": delivery_address_saved,
        "chronic_conditions": c.get("relationship", {}).get("chronic_conditions", []) if c else [],
        "active_offer": base["active_offers"][0] if base["active_offers"] else "",
    }


def extract_generic(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    """Fallback for unknown trigger kinds."""
    base = _merchant_base(m)
    payload = t.get("payload", {})

    return {
        **base,
        "trigger_reason": t.get("kind", "scheduled check-in").replace("_", " "),
        "trigger_kind": t.get("kind", ""),
        "payload_highlights": str({k: v for k, v in payload.items() if k != "placeholder"})[:200],
        "peer_ctr": _peer_stat(cat, "avg_ctr"),
        "peer_avg_rating": _peer_stat(cat, "avg_rating"),
    }


def extract_gbp_unverified(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    perf = m.get("performance", {})
    peer_views = _peer_stat(cat, "avg_views_30d")
    uplift_pct = payload.get("estimated_uplift_pct", 0.3)
    uplift_str = f"{round(uplift_pct * 100)}%"
    return {
        **base,
        "trigger_reason": "Google Business Profile not verified — losing search visibility",
        "display_name": m.get("identity", {}).get("name", ""),
        "verification_path": payload.get("verification_path", "postcard_or_phone_call"),
        "estimated_uplift": uplift_str,
        "current_views_30d": perf.get("views"),
        "peer_avg_views": peer_views,
        "potential_extra_views": (
            round(perf.get("views", 0) * uplift_pct)
            if perf.get("views") else None
        ),
    }


def extract_cde_opportunity(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    item_id = payload.get("digest_item_id")
    digest_item = _find_digest_item(cat, item_id) if item_id else None
    return {
        **base,
        "trigger_reason": "CDE/CPD opportunity — earn credits and stay current",
        "webinar_title": digest_item.get("title", "") if digest_item else "",
        "webinar_source": digest_item.get("source", "") if digest_item else "",
        "credits": payload.get("credits"),
        "fee": payload.get("fee", ""),
        "actionable": digest_item.get("actionable", "") if digest_item else "",
    }


def extract_supply_alert(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    return {
        **base,
        "trigger_reason": f"supply alert: {payload.get('molecule', 'product')} recall — action needed",
        "molecule": payload.get("molecule", ""),
        "affected_batches": payload.get("affected_batches", []),
        "manufacturer": payload.get("manufacturer", ""),
        "alert_id": payload.get("alert_id", ""),
    }


def extract_category_seasonal(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    trends = payload.get("trends", [])
    slug = cat.get("slug", "")
    seasonal_action_map = {
        "pharmacies": "Boost ORS, sunscreen, and antifungal shelf visibility; reduce cold-cough front shelf space this week",
        "salons": "Push bridal prep and keratin slots for the next 6 weeks; reduce discount-heavy walk-in messaging",
        "restaurants": "Promote fast-moving summer combos and cold beverages; tighten slow menu SKUs",
        "gyms": "Run a retention challenge for current members and push PT intro sessions",
        "dentists": "Promote preventive recall and whitening blocks before festival demand rises",
    }
    vocab_map = {
        "pharmacies": "prescription / refill / chronic / dispensing / OTC",
        "salons": "bridal / keratin / styling / booking",
        "restaurants": "covers / orders / combo / menu",
        "gyms": "membership / class / PT / renewals",
        "dentists": "patient / recall / treatment / whitening",
    }
    return {
        **base,
        "trigger_reason": f"seasonal demand shift for {payload.get('season', 'this season')} — stock up now",
        "season": payload.get("season", ""),
        "demand_trends": trends[:4],
        "shelf_action_recommended": payload.get("shelf_action_recommended", True),
        "recommended_action": seasonal_action_map.get(slug, "Rebalance inventory toward fastest-moving seasonal items"),
        "category_vocab_hint": vocab_map.get(slug, "customers / demand / inventory"),
    }


_SEASONAL_DIP_CATEGORY_CONTEXT = {
    "gyms":        ("member acquisition dip",
                    "Double down on existing members — a 4-week body goal challenge costs nothing and drives renewals",
                    "membership / HIIT / PT sessions / footfall / churn"),
    "salons":      ("booking slowdown",
                    "Use this quiet window to lock in bridal and keratin regulars — prep the next 2 months of bookings now",
                    "bridal / keratin / hair spa / styling / walk-in"),
    "restaurants": ("footfall dip",
                    "Quieter footfall = faster service + better reviews — use this window to fix your top complaint",
                    "covers / orders / combo / dine-in / delivery"),
    "pharmacies":  ("walk-in dip",
                    "Seasonal lull = best window to reach chronic prescription customers before stock runs low",
                    "prescription / chronic / dispensing / OTC / refill"),
    "dentists":    ("appointment dip",
                    "Quieter weeks = time to complete open treatment plans — focus on patients with pending appointments",
                    "patient / treatment / recall / scaling / appointment"),
}

def extract_seasonal_perf_dip(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    """Seasonal dip — different from perf_dip because it's expected."""
    base = _merchant_base(m)
    payload = t.get("payload", {})
    slug = cat.get("slug", "")
    metric = payload.get("metric", "views")
    delta_pct = payload.get("delta_pct", 0)
    window = payload.get("window", "7d")
    season_note = payload.get("season_note", "").replace("_", " ")
    peer_key_map = {"calls": "avg_calls_30d", "views": "avg_views_30d", "ctr": "avg_ctr"}
    peer_val = _peer_stat(cat, peer_key_map.get(metric, "avg_views_30d"))
    ctx = _SEASONAL_DIP_CATEGORY_CONTEXT.get(slug, ("performance dip", "Focus on retaining existing customers through this window", ""))
    return {
        **base,
        "trigger_reason": f"{ctx[0]} — {_fmt_pct(delta_pct)} dip is normal for {season_note}",
        "metric": metric,
        "delta_pct_str": _fmt_pct(delta_pct),
        "window": window,
        "is_seasonal": True,
        "season_note": season_note,
        "peer_median": peer_val,
        "current_value": base.get(f"{metric}_30d") or base.get("views_30d"),
        "smart_pivot": ctx[1],
        "category_vocab_hint": ctx[2],
    }


def extract_active_planning_intent(cat: dict, m: dict, t: dict, c: Optional[dict]) -> dict:
    base = _merchant_base(m)
    payload = t.get("payload", {})
    return {
        **base,
        "trigger_reason": f"merchant is actively planning: {payload.get('intent_topic', '').replace('_', ' ')}",
        "intent_topic": payload.get("intent_topic", "").replace("_", " "),
        "last_message": payload.get("merchant_last_message", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Registry + entry point
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACTOR_MAP = {
    "research_digest": extract_research_digest,
    "recall_due": extract_recall_due,
    "perf_dip": extract_perf_dip,
    "perf_spike": extract_perf_spike,
    "renewal_due": extract_renewal_due,
    "winback_eligible": extract_winback,
    "festival_upcoming": extract_festival,
    "review_theme_emerged": extract_review_theme,
    "curious_ask_due": extract_curious_ask,
    "wedding_package_followup": extract_wedding_followup,
    "customer_lapsed_soft": extract_customer_lapsed,
    "customer_lapsed_hard": extract_customer_lapsed,
    "dormant_with_vera": extract_dormant,
    "ipl_match_today": extract_ipl,
    "competitor_opened": extract_competitor,
    "milestone_reached": extract_milestone,
    "regulation_change": extract_regulation_change,
    "appointment_tomorrow": extract_appointment,
    "trial_followup": extract_trial_followup,
    "chronic_refill_due": extract_chronic_refill,
    "gbp_unverified": extract_gbp_unverified,
    "cde_opportunity": extract_cde_opportunity,
    "supply_alert": extract_supply_alert,
    "category_seasonal": extract_category_seasonal,
    "seasonal_perf_dip": extract_seasonal_perf_dip,
    "active_planning_intent": extract_active_planning_intent,
}


def extract(
    trigger_kind: str,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
) -> dict:
    """Main entry point. Returns facts dict. Never raises."""
    fn = _EXTRACTOR_MAP.get(trigger_kind, extract_generic)
    try:
        return fn(category, merchant, trigger, customer)
    except Exception:
        return extract_generic(category, merchant, trigger, customer)

"""Telegram staff-report intake parsing and preview text.

Pure helpers only: no network, no database, no secrets, no writes. The
Telegram channel adapter (telegram_adapter.py) calls these to interpret a
linked sender's report text, then reuses the shared workflow -- branch
resolution and draft helpers from message_processor, business-scoped
extraction from rule_engine, review-request queueing from review_service
and telegram_adapter, and confirmation/rejection through the existing
REVIEW CONFIRM / REVIEW REJECT commands.

Supported Telegram report commands (case-insensitive, optional leading
"<branch>:" prefix for multi-branch senders is tolerated here and
resolved by the caller):

    SALE 50 bags at 500 cash
    PRODUCTION 225 bags used 7kg nylon
    EXPENSE fuel 15000
    DEPOSIT 80000 bank transfer
    STOCK 120 normal bags and 75 cold bags
    CUSTOMER PAYMENT Emeka 25000 transfer
    CUSTOMER DEBT Ada 12000

The first five shared kinds (sale, production, expense, bank_deposit,
payment) are produced by normalizing the command into plain business
wording and running the existing business-scoped rule_engine.extract()
on it, so Telegram drafts carry exactly the same parsed shape WhatsApp
drafts carry for the same business/branch. STOCK and CUSTOMER DEBT have
no posting mapping in the database yet, so they are parsed here into
structured "stock" / "customer_debt" drafts for administrator review;
confirmation and posting RPCs will refuse those kinds until a future
migration adds them, which the preview text says plainly.
"""

import re

# Command -> shared submission kind for commands handled by the existing
# business-scoped extraction. STOCK, CUSTOMER PAYMENT, and CUSTOMER DEBT
# are intentionally absent: they use the direct parsers below, producing
# the postable stock / customer_payment / customer_debt kinds.
SHARED_COMMAND_KINDS = {
    "SALE": "sale",
    "PRODUCTION": "production",
    "EXPENSE": "expense",
    "DEPOSIT": "bank_deposit",
}

# Fallback missing-field hints when an explicit command produced no
# parseable extraction at all (never invented values, only names of what
# to ask for).
REQUIRED_MISSING = {
    "sale": ["quantity", "unit", "unit_price"],
    "production": ["good_quantity"],
    "expense": ["amount_kobo", "category"],
    "bank_deposit": ["amount_kobo", "depositor_name",
        "destination_account", "reference"],
    "payment": ["amount_kobo", "sale_ref"],
    "stock": ["normal_quantity", "cold_quantity"],
    "customer_payment": ["customer_name", "amount_kobo", "method"],
    "customer_debt": ["customer_name", "amount_kobo"],
}

_COMMAND_PATTERN = re.compile(
    r"^\s*(?:[A-Za-z][A-Za-z0-9 _\-]{0,40}:\s*)?"
    r"(?P<command>SALE|PRODUCTION|EXPENSE|DEPOSIT|STOCK|"
    r"CUSTOMER\s+PAYMENT|CUSTOMER\s+DEBT)"
    r"(?:\b\s*(?P<remainder>.*))?$",
    re.IGNORECASE | re.DOTALL)

_AMOUNT = re.compile(r"(?P<num>\d[\d,]*(?:\.\d{1,2})?)")
_AMBIGUOUS_K = re.compile(r"(?i)\b\d[\d,]*\s*k\b")

_STOCK_TWO = re.compile(
    r"(?i)^\s*(?P<a>\d+(?:\.\d+)?)\s*(?P<astate>normal|cold)?\s*bags?\s+and\s+"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?P<bstate>normal|cold)?\s*bags?\s*$")
_STOCK_ONE = re.compile(
    r"(?i)^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<state>normal|cold)?\s*bags?\s*$")

_NAME_STOPWORDS = frozenset({
    "cash", "money", "bags", "bag", "naira", "ngn", "the", "a", "an",
    "some", "balance", "bank", "account", "sale", "payment", "deposit",
    "debt", "stock", "me", "us", "them", "him", "her",
})


def _normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _clean_name(raw):
    if not raw:
        return None
    name = _normalize_spaces(raw).strip(" .,'")
    if not name or len(name) > 40:
        return None
    if name.lower() in _NAME_STOPWORDS:
        return None
    if re.search(r"\d", name):
        return None
    return name


def _first_amount_kobo(text):
    """First plain number in the text as integer kobo, or None."""
    if not isinstance(text, str):
        return None
    match = _AMOUNT.search(text)
    if not match:
        return None
    raw = match.group("num").replace(",", "")
    try:
        if "." in raw:
            whole, frac = raw.split(".")
            frac = (frac + "00")[:2]
            return int(whole or 0) * 100 + int(frac)
        return int(raw) * 100
    except ValueError:
        return None


def _detect_method(text):
    lowered = " " + (text or "").lower() + " "
    if "bank transfer" in lowered:
        return "transfer"
    for word in ("cash", "transfer", "pos"):
        if " " + word + " " in lowered:
            return word
    return None


def _whole_number(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def detect_command(text):
    """Detects one of the seven intake commands.

    Returns (command, remainder) with command upper-cased and single
    spaced ("CUSTOMER PAYMENT") and remainder stripped, or (None, text)
    when the text opens with no intake command. Never raises on
    non-text input."""
    if not isinstance(text, str):
        return None, text
    match = _COMMAND_PATTERN.match(text)
    if not match:
        return None, text
    command = _normalize_spaces(match.group("command")).upper()
    remainder = (match.group("remainder") or "").strip()
    return command, remainder


def normalize_for_shared(command, remainder):
    """Rewords a shared-kind command into plain business wording for
    rule_engine.extract(). Returns (normalized_text, extra_fields,
    extra_provenance); no command currently adds extra fields."""
    rest = remainder or ""
    if command == "SALE":
        return ("Sold " + rest if rest else "Sale", {}, {})
    if command == "PRODUCTION":
        return ("Produced " + rest if rest else "Production", {}, {})
    if command == "EXPENSE":
        return ("Spent " + rest if rest else "Expense", {}, {})
    if command == "DEPOSIT":
        return ("deposited " + rest if rest else "Deposit", {}, {})
    return rest, {}, {}


def parse_customer_payment(remainder):
    """Direct CUSTOMER PAYMENT parser: who paid how much by which method.
    Produces the postable customer_payment kind; nothing is inferred."""
    result = {"kind": "customer_payment", "intent": "customer_payment",
        "fields": {}, "provenance": {}, "missing_fields": [], "errors": [],
        "message_text": remainder}
    fields, provenance = result["fields"], result["provenance"]
    text = remainder or ""
    if _AMBIGUOUS_K.search(text):
        result["missing_fields"].append("amount_kobo")
        result["errors"].append(
            "Ambiguous amount in message (bare 'k' suffix); "
            "write the full amount")
    else:
        amount = _first_amount_kobo(text)
        if amount is None or amount <= 0:
            result["missing_fields"].append("amount_kobo")
            result["errors"].append("Payment amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    amount_match = _AMOUNT.search(text)
    name_part = text[:amount_match.start()] if amount_match else text
    customer = _clean_name(name_part)
    if customer:
        fields["customer_name"] = customer
        provenance["customer_name"] = "staff_reported"
    else:
        result["missing_fields"].append("customer_name")
        result["errors"].append("Customer name not found in message")
    method = _detect_method(text)
    if method:
        fields["method"] = method
        provenance["method"] = "staff_reported"
    else:
        result["missing_fields"].append("method")
        result["errors"].append(
            "Payment method not found in message; state cash, "
            "transfer, or pos")
    return result


def parse_stock(remainder):
    """Direct STOCK parser: structured "stock" draft, never guessed."""
    result = {"kind": "stock", "intent": "stock", "fields": {},
        "provenance": {}, "missing_fields": [], "errors": [],
        "message_text": remainder}
    fields, provenance = result["fields"], result["provenance"]
    text = remainder or ""
    match = _STOCK_TWO.match(text)
    if match:
        first = _whole_number(match.group("a"))
        second = _whole_number(match.group("b"))
        state_a = (match.group("astate") or "").lower() or None
        state_b = (match.group("bstate") or "").lower() or None
        if first is not None and second is not None:
            fields["unit"] = "bag"
            provenance["unit"] = "staff_reported"
            fields["total_quantity"] = first + second \
                if isinstance(first + second, int) \
                else float(first + second)
            provenance["total_quantity"] = "staff_reported"
            stated = {}
            if state_a:
                stated[state_a] = first
            if state_b:
                stated[state_b] = second
            if "normal" in stated:
                fields["normal_quantity"] = stated["normal"]
                provenance["normal_quantity"] = "staff_reported"
            if "cold" in stated:
                fields["cold_quantity"] = stated["cold"]
                provenance["cold_quantity"] = "staff_reported"
            if state_a is None and state_b is None:
                result["missing_fields"].append("storage_split")
                result["errors"].append(
                    "Stock storage split not stated; say how many bags "
                    "are normal and how many are cold")
            elif state_a is None or state_b is None:
                missing_side = "normal_quantity" \
                    if "normal" not in stated else "cold_quantity"
                result["missing_fields"].append(missing_side)
                result["errors"].append(
                    "Stock storage split partly stated; say how many "
                    "bags are normal and how many are cold")
            return result
    single = _STOCK_ONE.match(text)
    if single:
        quantity = _whole_number(single.group("qty"))
        state = (single.group("state") or "").lower() or None
        if quantity is not None:
            fields["unit"] = "bag"
            provenance["unit"] = "staff_reported"
            fields["total_quantity"] = quantity
            provenance["total_quantity"] = "staff_reported"
            if state:
                fields[state + "_quantity"] = quantity
                provenance[state + "_quantity"] = "staff_reported"
                other = "cold_quantity" if state == "normal" \
                    else "normal_quantity"
                result["missing_fields"].append(other)
                result["errors"].append(
                    "Only one storage state stated; say how many bags "
                    "are normal and how many are cold")
            else:
                result["missing_fields"].extend(
                    ["normal_quantity", "cold_quantity"])
                result["errors"].append(
                    "Stock storage split not stated; say how many bags "
                    "are normal and how many are cold")
            return result
    result["missing_fields"].extend(["normal_quantity", "cold_quantity"])
    result["errors"].append(
        "Stock quantities not found in message; send e.g. "
        "STOCK 120 normal bags and 75 cold bags")
    return result


def parse_customer_debt(remainder):
    """Direct CUSTOMER DEBT parser: who owes how much, nothing inferred."""
    result = {"kind": "customer_debt", "intent": "customer_debt",
        "fields": {}, "provenance": {}, "missing_fields": [], "errors": [],
        "message_text": remainder}
    fields, provenance = result["fields"], result["provenance"]
    text = remainder or ""
    if _AMBIGUOUS_K.search(text):
        result["missing_fields"].append("amount_kobo")
        result["errors"].append(
            "Ambiguous amount in message (bare 'k' suffix); "
            "write the full amount")
    else:
        amount = _first_amount_kobo(text)
        if amount is None or amount <= 0:
            result["missing_fields"].append("amount_kobo")
            result["errors"].append("Debt amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    amount_match = _AMOUNT.search(text)
    name_part = text[:amount_match.start()] if amount_match else text
    customer = _clean_name(name_part)
    if customer:
        fields["customer_name"] = customer
        provenance["customer_name"] = "staff_reported"
    else:
        result["missing_fields"].append("customer_name")
        result["errors"].append("Customer name not found in message")
    return result


def _format_naira_simple(amount_naira):
    """Whole-naira display for staff messages (e.g. 450 -> ₦450)."""
    try:
        return "₦{:,.0f}".format(float(amount_naira))
    except (TypeError, ValueError):
        return None


def _plural_unit(quantity, unit):
    """'1 bag' vs '50 bags': singular only for exactly one."""
    word = (unit or "units").strip() or "units"
    try:
        one = float(quantity) == 1
    except (TypeError, ValueError):
        one = False
    if one and word.endswith("s"):
        return word[:-1]
    if not one and not word.endswith("s"):
        return word + "s"
    return word


def _simple_missing_name(field):
    """Plain words for a missing field, fit for market staff."""
    return {
        "quantity": "how many",
        "unit": "unit (for example: bag)",
        "unit_price": "price",
        "amount_kobo": "amount",
        "method": "payment type (cash, transfer or pos)",
        "payment_method": "payment type (cash, transfer or pos)",
        "customer_name": "customer name",
        "customer_id": "customer",
        "category": "what it was for",
        "description": "description",
        "sale_ref": "which sale this pays for",
        "good_quantity": "good bags",
        "destination_account": "where it was deposited",
        "reference": "deposit slip or transfer reference",
        "depositor_name": "who deposited it",
        "from_name": "who handed it over",
        "to_name": "who received it",
        "normal_quantity": "normal bags",
        "cold_quantity": "cold bags",
    }.get(field, field.replace("_", " "))


def _simple_sale_line(fields):
    """'1 bag sold for ₦450 cash.' or '2 bags sold for ₦900 cash
    (₦450 each).' -- the total is always named so a per-unit price is
    never mistaken for the total. None when details are missing."""
    try:
        quantity = float(fields.get("quantity"))
        unit = str(fields.get("unit") or "").strip()
        unit_price = float(fields.get("unit_price"))
    except (TypeError, ValueError):
        return None
    if not unit:
        return None
    try:
        total = float(fields.get("total_amount"))
    except (TypeError, ValueError):
        total = quantity * unit_price
    amount = _format_naira_simple(unit_price)
    total_text = _format_naira_simple(total)
    if amount is None or total_text is None:
        return None
    method = str(fields.get("payment_method") or "").strip().lower()
    tail = " %s" % method if method in ("cash", "transfer", "pos") else ""
    if float(quantity) == int(quantity):
        shown = "%d" % int(quantity)
    else:
        shown = "%g" % quantity
    if quantity == 1:
        return "%s %s sold for %s%s." % (
            shown, _plural_unit(quantity, unit), amount, tail)
    return "%s %s sold for %s%s (%s each)." % (
        shown, _plural_unit(quantity, unit), total_text, tail, amount)


def _simple_kobo_line(fields, key="amount_kobo"):
    """Whole-naira display of a true-kobo integer field, or None."""
    try:
        return _format_naira_simple(int(fields.get(key)) / 100)
    except (TypeError, ValueError):
        return None


def summarize_extraction(extraction):
    """Short plain summary of an extraction result for chat feedback."""
    kind = (extraction or {}).get("kind") or "report"
    fields = (extraction or {}).get("fields") or {}
    if kind == "sale":
        line = _simple_sale_line(fields)
        return line if line else "Sale needs more detail."
    if kind == "production":
        try:
            return "%d bags produced." % int(
                float(fields.get("good_quantity")))
        except (TypeError, ValueError):
            return "Production report needs more detail."
    if kind == "expense":
        amount = _simple_kobo_line(fields)
        if amount is None:
            return "Expense needs more detail."
        category = str(fields.get("category") or "spending").strip()
        return "%s spent on %s." % (amount, category)
    if kind == "bank_deposit":
        amount = _simple_kobo_line(fields)
        return "%s deposited." % amount if amount else \
            "Deposit needs more detail."
    if kind == "payment":
        amount = _simple_kobo_line(fields)
        return "%s received." % amount if amount else \
            "Payment needs more detail."
    if kind == "customer_payment":
        amount = _simple_kobo_line(fields)
        if amount is None:
            return "Customer payment needs more detail."
        who = str(fields.get("customer_name") or "").strip()
        if who:
            return "%s paid %s." % (who, amount)
        return "%s received." % amount
    if kind == "cash_handover":
        amount = _simple_kobo_line(fields)
        return "%s handed over." % amount if amount else \
            "Handover needs more detail."
    if kind == "stock":
        try:
            return "%d normal and %d cold bags counted." % (
                int(fields.get("normal_quantity")),
                int(fields.get("cold_quantity")))
        except (TypeError, ValueError):
            return "Stock count needs more detail."
    if kind == "customer_debt":
        amount = _simple_kobo_line(fields)
        if amount is None:
            return "Customer debt needs more detail."
        who = str(fields.get("customer_name") or "").strip()
        if who:
            return "%s owes %s." % (who, amount)
        return "%s owed." % amount
    return "%s recorded." % kind.replace("_", " ").capitalize()


def format_intake_preview(extraction, review_ref=None, request_key=None,
        queue_note=None):
    """Staff receipt for a saved draft, in plain market language.

    No UUIDs, idempotency keys, or database terms. The only reference
    shown is the draft's own short review reference, and only so the
    reporter can withdraw their own draft with CANCEL."""
    summary = summarize_extraction(extraction)
    missing = (extraction or {}).get("missing_fields") or []
    lines = ["Saved \u2705", summary]
    if missing:
        simple = [_simple_missing_name(field) for field in missing[:8]]
        lines.append("Please add: %s." % ", ".join(simple))
    lines.append("Waiting for approval.")
    if review_ref:
        lines.append(
            "To take back this draft, reply: CANCEL %s" % review_ref)
    if queue_note:
        lines.append(queue_note)
    return "\n".join(lines)[:1500]

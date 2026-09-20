"""Stage 1: deterministic AMOSE water-business intake extraction.

Recognizes six separate daily-record kinds from straightforward Nigerian
business wording over WhatsApp:

    production      "Produced 250 good bags, 5 rejected"
    sale            "Sold 100 bags at 500 cash"
    payment         "Received 50000 cash for sale YR-XXXXXXXXXX"
    expense         "Spent 20000 on tricycle fuel cash"
    cash_handover   "Michael handed 80000 cash to Vivian"
    bank_deposit    "Vivian deposited 70000 to Access Bank, reference ABC123"

The example sentences above are illustrative, not exhaustive: every
detector is a conservative verb/structure pattern, never an exact-match
list.

Safety properties (enforced, tested in test_daily_business_records.py):

- Raw intake creates drafts only. This module performs no database reads
  and no writes of any kind -- it returns a plain dict the caller stores
  on the draft submission's parsed block.
- Every returned field carries provenance ("staff_reported", or
  "system_derived" for ISO dates/shifts only when explicitly stated --
  never guessed).
- Anything absent is listed in missing_fields, never inferred: branch,
  employee, customer, account, sale ID, price, payment method, and date
  are only reported when explicitly present in the text.
- Employee/account references stay as staff-written NAMES. Name -> UUID
  resolution happens later from authoritative scoped database records
  inside the confirmation RPCs -- arbitrary UUIDs are never accepted
  from message text (the verified validators reject non-UUIDs).
- Money in parsed payment/expense/handover/deposit fields is integer
  kobo (matching the database money unit); sale quantity/unit_price keep
  the legacy naira/float shape message_processor historically stored.
- A message strongly matching two or more kinds is ambiguous: it yields
  no kind and an explicit error rather than a guessed classification.
"""

import re

# ---------------------------------------------------------------------------
# Shared patterns
# ---------------------------------------------------------------------------

# Legacy sale shapes (identical semantics to message_processor.parse_message
# so water-business sale drafts keep the historically stored shape).
_FULL_SALE = re.compile(
    r"(?i)\bsold\b\s+(?P<quantity>\d+(?:\.\d+)?)\s+(?P<unit>[a-zA-Z]+)\s+(?:at|for|@)\s+(?P<unit_price>\d+(?:\.\d+)?)")
_PARTIAL_SALE = re.compile(
    r"(?i)\bsold\b\s+(?P<quantity>\d+(?:\.\d+)?)\s+(?P<unit>[a-zA-Z]+)")
_SALE_KEYWORDS = ("sold", "sale")

_CURRENCY_HINTS = {"ngn": "NGN", "naira": "NGN", "\u20a6": "NGN"}

# Payment-method words staff write explicitly. Absent means absent: the
# caller records a missing field, never a default.
_METHOD_WORDS = (
    ("pos", "pos"),
    ("transfer", "transfer"),
    ("bank transfer", "transfer"),
    ("cash", "cash"),
)

# Strong-kind detectors: distinctive verbs/structures only. A bare
# currency word or a bare number never triggers any kind.
_PRODUCTION_VERB = re.compile(r"(?i)\bproduc(?:e|ed|tion)\b")
_BAG_WORD = re.compile(r"(?i)\bbags?\b")
_HANDOVER_VERB = re.compile(r"(?i)\b(?:hand(?:ed|s)?(?:\s+over)?|gave)\b")
_DEPOSIT_VERB = re.compile(r"(?i)\bdeposit(?:ed|ing|s)?\b")
_EXPENSE_VERB = re.compile(r"(?i)\b(?:spent|expenses?|paid\s+for)\b")
_PAYMENT_VERB = re.compile(r"(?i)\b(?:receiv\w+|collect\w+)\b")
_PAID_VERB = re.compile(r"(?i)\bpaid\b")

# Production quantities.
_PROD_GOOD = re.compile(
    r"(?i)\bproduc(?:e|ed|tion)\b[^.]*?(?P<good>\d+)\s*(?:good\s+)?bags?\b")
_PROD_REJECTED = re.compile(
    r"(?i)(?P<rejected>\d+)\s*(?:reject\w*|damag\w*|bad|wast\w*|leak\w*)")
_ISO_DATE = re.compile(r"\b(?P<date>20\d\d-\d\d-\d\d)\b")
_SHIFT_WORD = re.compile(r"(?i)\b(morning|afternoon|night|full_day)\b")

# Payment/expense/handover/deposit structures.
_SALE_REF = re.compile(
    r"(?i)(?:for|against)\s+(?:the\s+)?sale\s+([A-Za-z0-9][A-Za-z0-9\-]*)")
_EXPENSE_DETAIL = re.compile(
    r"(?i)\b(?:spent|expenses?)\b\s+"
    r"(?:\u20a6|ngn|naira)?\s*[\d,]+(?:\.\d{1,2})?\s*(?:\u20a6|ngn|naira)?"
    r"\s+(?:on|for)\s+(?P<detail>.+?)\s*$")
_HANDOVER = re.compile(
    r"(?i)\b(?P<from>[A-Za-z][A-Za-z .']{0,39}?)\s+"
    r"(?:hand(?:ed|s)?(?:\s+over)?|gave)\s+"
    r"(?:\u20a6|ngn|naira)?\s*[\d,]+(?:\.\d{1,2})?\s*(?:\u20a6|ngn|naira)?"
    r"\s+(?:cash\s+)?to\s+(?P<to>[A-Za-z][A-Za-z .']{0,39}?)"
    r"(?:\s*[,.;]|$)")
_DEPOSIT_WHO_BEFORE = re.compile(
    r"(?i)^\s*(?P<who>[A-Za-z][A-Za-z .']{0,39}?)\s+deposit(?:ed|ing|s)?\b")
_DEPOSIT_WHO_BY = re.compile(
    r"(?i)\bdeposit(?:ed|ing|s)?\s+by\s+(?P<who>[A-Za-z][A-Za-z .']{0,39}?)"
    r"(?:\s*[,.;]|$)")
_DEPOSIT_BANK = re.compile(
    r"(?i)\bdeposit(?:ed|ing|s)?\b[^.]*?\bto\s+"
    r"(?P<bank>[A-Za-z][A-Za-z0-9 &\-.']{0,63}?)"
    r"(?:\s*[,.;]|\s+(?:reference|ref\b|receipt|teller|slip)|\s*$)")
_DEPOSIT_REF = re.compile(
    r"(?i)(?:reference|ref(?:erence)?|receipt|teller|deposit\s+slip)"
    r"\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]*)")

# A number followed by a bare "k" ("50k") is deliberately NOT parsed as an
# amount: it is ambiguous, so it is reported as an error instead.
_AMBIGUOUS_K = re.compile(r"(?i)\b\d[\d,]*\s*k\b")

_NAME_STOPWORDS = frozenset({
    "cash", "money", "bags", "bag", "naira", "ngn", "the", "a", "an",
    "some", "balance", "bank", "account", "sale", "payment", "deposit",
    "handover", "me", "us", "them", "him", "her",
})

# Staff phrase -> stable machine expense category. Ordered: the first
# matching category wins (fuel before tricycle-service so "tricycle fuel"
# stays fuel). Unmatched text leaves the category missing -- never guessed.
_CATEGORY_KEYWORDS = (
    ("atwap_dues", ("atwap",)),
    ("task_force", ("task force", "taskforce", "task-force",)),
    ("fuel", ("fuel", "petrol", "diesel",)),
    ("tricycle_service", ("tricycle service", "tricycle repair",
        "tricycle servicing", "keke service", "keke repair",)),
    ("salaries", ("salary", "salaries", "wage", "wages", "operator pay",
        "staff pay", "worker pay",)),
    ("maintenance", ("maintenance", "repair", "service", "servicing",)),
    ("utilities", ("utility", "utilities", "nepa", "phcn", "electricity",
        "light bill",)),
    ("packaging", ("packaging", "packing", "nylon", "film", "rubber",)),
    ("transport", ("transport", "delivery", "logistics", "fare",)),
    ("rent", ("rent", "rental",)),
    ("purchases", ("purchase", "purchases", "bought", "buy", "stock",)),
    ("other", ("miscellaneous", "other", "general",)),
)

# Machine categories the confirmation layer accepts (mirrors the database
# CHECK after the Stage 1 migration).
EXPENSE_CATEGORIES = frozenset({
    "fuel", "maintenance", "salaries", "transport", "packaging",
    "utilities", "rent", "purchases", "other",
    "task_force", "atwap_dues", "tricycle_service",
})


def _singularize(unit):
    return unit[:-1] if len(unit) > 1 and unit.lower().endswith("s") else unit


def _detect_currency(text):
    lowered = text.lower()
    for hint, code in _CURRENCY_HINTS.items():
        if hint in lowered:
            return code
    return None


def _detect_method(text):
    """Returns the explicit payment method, or None when absent (never a
    default -- the caller records a missing field)."""
    lowered = " " + text.lower() + " "
    for word, method in _METHOD_WORDS:
        if " " + word + " " in lowered or word in lowered.split():
            return method
    # Fallback substring check for multi-word phrases ("bank transfer").
    if "bank transfer" in lowered:
        return "transfer"
    return None


def _parse_kobo(text):
    """First explicit naira amount in the text, as integer kobo.

    Accepts NGN/naira/\u20a6 markers (prefix or suffix), thousands commas,
    and up to two decimals. Returns None when no unambiguous amount is
    present. A bare "<number>k" suffix is ambiguous and yields None (the
    caller reports it explicitly via _has_ambiguous_k).
    """
    if not isinstance(text, str):
        return None
    match = re.search(
        r"(?:\u20a6|ngn|naira)?\s*(?P<num>\d[\d,]*"
        r"(?:\.\d{1,2})?)\s*(?:\u20a6|ngn|naira)?",
        text, re.IGNORECASE)
    if not match:
        return None
    raw = match.group("num").replace(",", "")
    if not raw or raw.startswith("."):
        return None
    try:
        if "." in raw:
            whole, frac = raw.split(".")
            if len(frac) > 2:
                return None
            frac = (frac + "00")[:2]
            return int(whole or 0) * 100 + int(frac)
        return int(raw) * 100
    except ValueError:
        return None


def _has_ambiguous_k(text):
    return bool(_AMBIGUOUS_K.search(text or ""))


def _clean_name(raw):
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw).strip(" .,'")
    if not name or len(name) > 40:
        return None
    if name.lower() in _NAME_STOPWORDS:
        return None
    if re.search(r"\d", name):
        return None
    return name


def _map_category(text):
    """Returns (machine_category_or_None, ambiguous_bool).

    First match in _CATEGORY_KEYWORDS order wins (most specific phrases
    first, so "tricycle service" beats the generic "service" and
    "tricycle fuel" stays fuel). Deterministic by construction; the
    reviewer still sees the preserved staff description and corrects the
    category at confirmation when the wording was genuinely mixed.
    """
    lowered = (text or "").lower()
    for machine, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return machine, False
    return None, False


def _base(message_text):
    return {"fields": {}, "provenance": {},
        "missing_fields": [], "errors": [],
        "message_text": message_text}


def _parse_production(text):
    result = _base(text)
    result["kind"] = result["intent"] = "production"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    good = _PROD_GOOD.search(text)
    if not good:
        missing.append("good_quantity")
        errors.append("Production quantity not found in message")
        return result
    fields["good_quantity"] = int(good.group("good"))
    provenance["good_quantity"] = "staff_reported"
    rejected = _PROD_REJECTED.search(text)
    if rejected:
        fields["rejected_quantity"] = int(rejected.group("rejected"))
        provenance["rejected_quantity"] = "staff_reported"
    else:
        missing.append("rejected_quantity")
    date_match = _ISO_DATE.search(text)
    if date_match:
        fields["production_date"] = date_match.group("date")
        provenance["production_date"] = "staff_reported"
    else:
        missing.append("production_date")
    shift = _SHIFT_WORD.search(text)
    if shift:
        fields["shift"] = shift.group(1).lower()
        provenance["shift"] = "staff_reported"
    else:
        missing.append("shift")
    missing.append("product_id")
    return result


def _parse_sale(text):
    result = _base(text)
    result["kind"] = result["intent"] = "sale"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    match = _FULL_SALE.search(text)
    if match:
        fields["quantity"] = float(match.group("quantity"))
        provenance["quantity"] = "staff_reported"
        fields["unit"] = _singularize(match.group("unit"))
        provenance["unit"] = "staff_reported"
        fields["unit_price"] = float(match.group("unit_price"))
        provenance["unit_price"] = "staff_reported"
    else:
        partial = _PARTIAL_SALE.search(text)
        if partial:
            fields["quantity"] = float(partial.group("quantity"))
            provenance["quantity"] = "staff_reported"
            fields["unit"] = _singularize(partial.group("unit"))
            provenance["unit"] = "staff_reported"
            missing.append("unit_price")
            errors.append("Sale price not found in message")
        elif any(keyword in text.lower() for keyword in _SALE_KEYWORDS):
            missing.extend(["quantity", "unit", "unit_price"])
            errors.append("Recognized a sale-related message but could not "
                "extract quantity/unit/price")
            currency = _detect_currency(text)
            if currency:
                fields["currency"] = currency
                provenance["currency"] = "staff_reported"
            else:
                missing.append("currency")
            return result
        else:
            result["kind"] = result["intent"] = None
            errors.append("No recognized intent in message")
            return result
    currency = _detect_currency(text)
    if currency:
        fields["currency"] = currency
        provenance["currency"] = "staff_reported"
    else:
        missing.append("currency")
    method = _detect_method(text)
    if method:
        fields["payment_method"] = method
        provenance["payment_method"] = "staff_reported"
    return result


def _parse_payment(text):
    result = _base(text)
    result["kind"] = result["intent"] = "payment"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    if _has_ambiguous_k(text):
        errors.append("Ambiguous amount in message (bare 'k' suffix)")
        missing.append("amount_kobo")
    else:
        amount = _parse_kobo(text)
        if amount is None or amount <= 0:
            missing.append("amount_kobo")
            errors.append("Payment amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    method = _detect_method(text)
    if method:
        fields["method"] = method
        provenance["method"] = "staff_reported"
    else:
        missing.append("method")
    sale_ref = _SALE_REF.search(text)
    if sale_ref:
        fields["sale_ref"] = sale_ref.group(1)
        provenance["sale_ref"] = "staff_reported"
    else:
        missing.append("sale_ref")
        errors.append("Sale reference not found in message")
    return result


def _parse_expense(text):
    result = _base(text)
    result["kind"] = result["intent"] = "expense"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    if _has_ambiguous_k(text):
        errors.append("Ambiguous amount in message (bare 'k' suffix)")
        missing.append("amount_kobo")
    else:
        amount = _parse_kobo(text)
        if amount is None or amount <= 0:
            missing.append("amount_kobo")
            errors.append("Expense amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    detail_match = _EXPENSE_DETAIL.search(text)
    detail = detail_match.group("detail").strip() if detail_match else None
    method = _detect_method(text)
    if detail:
        words = detail.split()
        if words and words[-1].lower() in ("cash", "transfer", "pos"):
            detail = " ".join(words[:-1]).strip() or None
        if detail:
            fields["description"] = detail
            provenance["description"] = "staff_reported"
    if "description" not in fields:
        fields["description"] = text.strip()
        provenance["description"] = "staff_reported"
    category, ambiguous = _map_category(detail or text)
    if ambiguous:
        missing.append("category")
        errors.append("Ambiguous expense category in message")
    elif category is None:
        missing.append("category")
        errors.append("Expense category not recognized in message")
    else:
        fields["category"] = category
        provenance["category"] = "staff_reported"
    if method:
        fields["payment_method"] = method
        provenance["payment_method"] = "staff_reported"
    else:
        missing.append("payment_method")
    return result


def _parse_handover(text):
    result = _base(text)
    result["kind"] = result["intent"] = "cash_handover"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    if _has_ambiguous_k(text):
        errors.append("Ambiguous amount in message (bare 'k' suffix)")
        missing.append("amount_kobo")
    else:
        amount = _parse_kobo(text)
        if amount is None or amount <= 0:
            missing.append("amount_kobo")
            errors.append("Handover amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    match = _HANDOVER.search(text)
    from_name = _clean_name(match.group("from")) if match else None
    to_name = _clean_name(match.group("to")) if match else None
    if from_name:
        fields["from_name"] = from_name
        provenance["from_name"] = "staff_reported"
    else:
        missing.append("from_name")
        errors.append("Handover giver not found in message")
    if to_name:
        fields["to_name"] = to_name
        provenance["to_name"] = "staff_reported"
    else:
        missing.append("to_name")
        errors.append("Handover receiver not found in message")
    return result


def _parse_deposit(text):
    result = _base(text)
    result["kind"] = result["intent"] = "bank_deposit"
    fields, provenance, missing, errors = (
        result["fields"], result["provenance"],
        result["missing_fields"], result["errors"])
    if _has_ambiguous_k(text):
        errors.append("Ambiguous amount in message (bare 'k' suffix)")
        missing.append("amount_kobo")
    else:
        amount = _parse_kobo(text)
        if amount is None or amount <= 0:
            missing.append("amount_kobo")
            errors.append("Deposit amount not found in message")
        else:
            fields["amount_kobo"] = amount
            provenance["amount_kobo"] = "staff_reported"
    who = _DEPOSIT_WHO_BEFORE.search(text) or _DEPOSIT_WHO_BY.search(text)
    depositor = _clean_name(who.group("who")) if who else None
    if depositor:
        fields["depositor_name"] = depositor
        provenance["depositor_name"] = "staff_reported"
    else:
        missing.append("depositor_name")
        errors.append("Depositor not found in message")
    bank = _DEPOSIT_BANK.search(text)
    destination = bank.group("bank").strip(" .,;") if bank else None
    if destination and destination.lower() not in _NAME_STOPWORDS:
        fields["destination_account"] = destination
        provenance["destination_account"] = "staff_reported"
    else:
        missing.append("destination_account")
        errors.append("Destination account not found in message")
    reference = _DEPOSIT_REF.search(text)
    if reference:
        fields["reference"] = reference.group(1)
        provenance["reference"] = "staff_reported"
    else:
        missing.append("reference")
        errors.append("Deposit reference not found in message; "
            "a deposit is never confirmed without a reference")
    return result


def _strong_kinds(text):
    """Kinds whose distinctive verb/structure pattern is present."""
    kinds = []
    if _PRODUCTION_VERB.search(text) and _BAG_WORD.search(text) \
            and re.search(r"\d", text):
        kinds.append("production")
    if _HANDOVER_VERB.search(text):
        # "handed/gave" alone names the kind; a missing counterparty is
        # reported as a missing field so the reviewer clarifies it.
        kinds.append("cash_handover")
    if _DEPOSIT_VERB.search(text):
        kinds.append("bank_deposit")
    if _EXPENSE_VERB.search(text):
        kinds.append("expense")
    if _PAYMENT_VERB.search(text):
        kinds.append("payment")
    elif _PAID_VERB.search(text) and not _EXPENSE_VERB.search(text):
        # "paid" alone reads as a payment; "paid for ..." reads as an
        # expense (already counted above, never double-counted).
        kinds.append("payment")
    if re.search(r"(?i)\bsold\b", text):
        kinds.append("sale")
    return kinds


_PARSERS = {
    "production": _parse_production,
    "sale": _parse_sale,
    "payment": _parse_payment,
    "expense": _parse_expense,
    "cash_handover": _parse_handover,
    "bank_deposit": _parse_deposit,
}


def extract_water_record(text):
    """Deterministic intake extraction for one raw WhatsApp message.

    Returns {"kind", "intent", "fields", "provenance", "missing_fields",
    "errors", "message_text"}. kind/intent is None when nothing was
    recognized or when the message is ambiguous across kinds. The
    original message text is always retained; nothing is inferred.
    """
    if not text or not str(text).strip():
        result = _base(text if isinstance(text, str) else "")
        result["kind"] = result["intent"] = None
        result["errors"] = ["Empty or missing message text"]
        return result
    cleaned = str(text).strip()
    strong = _strong_kinds(cleaned)
    if len(strong) > 1:
        result = _base(cleaned)
        result["kind"] = result["intent"] = None
        result["errors"] = ["Ambiguous message: matches more than one "
            "record kind (%s); please send one record per message"
            % ", ".join(strong)]
        return result
    if len(strong) == 1:
        return _PARSERS[strong[0]](cleaned)
    # No strong pattern: legacy sale-keyword fallback, else unrecognized.
    if any(keyword in cleaned.lower() for keyword in _SALE_KEYWORDS):
        return _parse_sale(cleaned)
    result = _base(cleaned)
    result["kind"] = result["intent"] = None
    result["errors"] = ["No recognized intent in message"]
    return result

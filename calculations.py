from decimal import Decimal, ROUND_HALF_UP

def number(value):
    if value is None:
        raise ValueError("Required value not recorded")
    n = Decimal(str(value))
    if not n.is_finite() or n < 0:
        raise ValueError("Values must be finite and non-negative")
    return n

def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def poultry(data):
    produced = number(data.get("crates_produced"))
    sales = data.get("sales", [])
    revenue = sum((number(s["quantity"]) * number(s["unit_price"]) for s in sales), Decimal(0))
    feed = sum((number(f["bags_used"]) * number(f["inventory_cost_per_bag"]) for f in data.get("feed_usage", [])), Decimal(0))
    days = number(data.get("payroll_days"))
    if days == 0:
        raise ValueError("Payroll days must be positive")
    labor = sum((number(e["monthly_salary"]) for e in data.get("employees", []) if e.get("active", True)), Decimal(0)) / days * number(data.get("period_days"))
    other = sum((number(c) for c in data.get("other_costs", [])), Decimal(0))
    cost = feed + labor + other
    return {"mode": "scenario", "revenue": money(revenue), "feed_cost": money(feed),
            "labor_cost": money(labor), "total_recorded_cost": money(cost),
            "revenue_less_recorded_cost": money(revenue-cost),
            "production_cost_per_crate": money(cost/produced) if produced else None,
            "notice": "Scenario only. Not final profit: inventory movement and complete costs must be reconciled."}

def water(data):
    yield_ = number(data.get("bags_per_kg"))
    fuel_output = number(data.get("fuel_output_bags"))
    if not yield_ or not fuel_output:
        raise ValueError("Yield and fuel output must be positive")
    cost = number(data.get("film_cost_per_kg"))/yield_ + number(data.get("packing_cost")) + number(data.get("fuel_cost"))/fuel_output
    cost += number(data.get("operator_per_bag"))
    sale = number(data.get("sale_price"))
    commission = number(data.get("commission_per_bag"))
    return {"mode": "scenario", "production_cost_per_bag": money(cost),
            "cost_including_commission": money(cost+commission),
            "remaining_before_other_costs": money(sale-cost-commission)}

def commission(data):
    total = number(data.get("sale_total"))
    paid = number(data.get("cumulative_paid"))
    bags = number(data.get("bags"))
    rate = number(data.get("rate"))
    prior = number(data.get("already_released"))
    if total == 0 or paid > total:
        raise ValueError("Sale must be positive and payment cannot exceed sale")
    earned = (bags * rate * paid / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if prior > earned:
        raise ValueError("Prior releases exceed earned amount; adjustment required")
    return {"earned_total": money(earned), "newly_releasable": money(earned-prior)}

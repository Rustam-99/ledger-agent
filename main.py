import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Appliances Ledger Agent")

API_URL = os.getenv("LEDGER_API_URL")
TIMEOUT = 20

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}
REGIONS = ["north", "south", "east", "west", "central"]
STATUSES = ["paid", "refunded", "void"]


def get_json(url):
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_date(value):
    """Use UTC. Do not shift dates to Kolkata for month filtering."""
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_export(text):
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@lru_cache(maxsize=1)
def load_data():
    if not API_URL:
        raise RuntimeError("LEDGER_API_URL is not configured")

    root = get_json(API_URL)
    links = root.get("links", {})
    export_url = links.get("export")
    rates_url = links.get("rates")

    if not export_url or not rates_url:
        raise RuntimeError("API is missing export or rates link")

    response = requests.get(export_url, timeout=TIMEOUT)
    response.raise_for_status()
    raw_orders = parse_export(response.text)

    latest = {}
    for order in raw_orders:
        order_id = order.get("id")
        if order_id is None:
            continue

        previous = latest.get(order_id)
        if previous is None or parse_date(order["updated_at"]) >= parse_date(previous["updated_at"]):
            latest[order_id] = order

    rates_data = get_json(rates_url)
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


def order_quantity(order):
    for key in ["qty", "quantity", "units", "item_count"]:
        if key in order and order[key] is not None:
            try:
                return int(order[key])
            except Exception:
                pass
    return 1


def order_unit_amount(order):
    """
    Find the per-order money value.
    Priority: explicit unit price fields, then amount/total fields.
    """
    for key in ["unit_price", "price", "unit_amount", "rate"]:
        if key in order and order[key] is not None:
            return float(order[key])

    for key in ["amount", "total", "total_amount", "value"]:
        if key in order and order[key] is not None:
            return float(order[key])

    return 0.0


def usd_amount(order, rates):
    currency = str(order.get("currency", "")).upper()
    if currency not in rates:
        raise ValueError(f"Missing USD conversion rate for {currency}")

    qty = order_quantity(order)
    unit_amount = order_unit_amount(order)

    return float(unit_amount) * qty * float(rates[currency])


def contains_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def detect_filters(question, orders):
    q = question.lower()

    region = next(
        (r for r in REGIONS if re.search(rf"\b{r}\b", q)),
        None,
    )

    month = next(
        (n for name, n in MONTHS.items() if re.search(rf"\b{name}\b", q)),
        None,
    )

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group()) if year_match else None

    status = next(
        (s for s in STATUSES if re.search(rf"\b{s}\b", q)),
        None,
    )

    products = sorted(
        {
            str(o.get("product", "")).strip()
            for o in orders
            if o.get("product")
        },
        key=len,
        reverse=True,
    )

    product = None
    for p in products:
        pattern = rf"(?<!\w){re.escape(p.lower())}(?!\w)"
        if re.search(pattern, q):
            product = p
            break

    return {
        "region": region,
        "month": month,
        "year": year,
        "status": status,
        "product": product,
    }


def apply_filters(orders, filters, allowed_statuses=None):
    result = list(orders)

    region = filters.get("region")
    month = filters.get("month")
    year = filters.get("year")
    product = filters.get("product")
    status = filters.get("status")

    if region:
        result = [
            o for o in result
            if str(o.get("region", "")).lower() == region
        ]

    if product:
        result = [
            o for o in result
            if str(o.get("product", "")).strip().lower() == product.lower()
        ]

    if status:
        result = [
            o for o in result
            if str(o.get("status", "")).lower() == status
        ]

    if month or year:
        filtered = []
        for order in result:
            created = order.get("created_at") or order.get("date") or order.get("timestamp")
            if not created:
                continue

            dt = parse_date(created)

            if month and dt.month != month:
                continue
            if year and dt.year != year:
                continue

            filtered.append(order)

        result = filtered

    if allowed_statuses is not None:
        result = [
            o for o in result
            if str(o.get("status", "")).lower() in allowed_statuses
        ]

    return result


def product_revenue(orders, rates):
    revenue = {}
    units = {}

    for order in orders:
        name = str(order.get("product", "Unknown")).strip()

        revenue[name] = revenue.get(name, 0.0) + usd_amount(order, rates)
        units[name] = units.get(name, 0) + order_quantity(order)

    return revenue, units


def answer_question(question):
    if not question or not question.strip():
        return "Please provide a question."

    orders, rates = load_data()
    q = question.lower().strip()
    filters = detect_filters(q, orders)

    asks_refund = contains_phrase(q, ["refund", "refunded"])
    asks_customer = contains_phrase(q, ["customer", "customers", "client", "clients"])
    asks_average = contains_phrase(q, ["average", "on average", "avg", "mean"])
    asks_count = contains_phrase(
        q,
        ["count", "how many", "number of", "total number",
         "quantity of", "distinct", "unique"],
    )
    asks_top = contains_phrase(
        q,
        ["top", "highest", "most", "best", "best-selling",
         "best selling", "top-selling", "top selling"],
    )
    asks_units = contains_phrase(q, ["units", "quantity", "items sold", "most sold"])
    asks_product = (
        filters["product"] is not None
        or contains_phrase(q, ["product", "products", "which item"])
    )
    asks_revenue = contains_phrase(
        q,
        ["revenue", "sales", "earned", "earnings", "income",
         "turnover", "generated", "bring in"],
    )
    asks_total = contains_phrase(q, ["total", "sum", "overall", "altogether"])

    # Customers: distinct customer count.
    if asks_customer:
        customer_orders = apply_filters(orders, filters)
        customers = {
            str(o.get("customer", "")).strip()
            for o in customer_orders
            if o.get("customer")
        }
        return len(customers)

    # Refunds.
    if asks_refund:
        refund_orders = apply_filters(
            orders, filters, allowed_statuses={"refunded"}
        )

        if asks_count and not asks_average and not asks_total:
            return len(refund_orders)

        if asks_average:
            if not refund_orders:
                return 0.0

            avg = sum(usd_amount(o, rates) for o in refund_orders) / len(refund_orders)
            return round(avg, 2)

        total = sum(usd_amount(o, rates) for o in refund_orders)
        return round(total, 2)

    # Product questions.
    if asks_product:
        # Try paid orders first.
        product_orders = apply_filters(
            orders, filters, allowed_statuses={"paid"}
        )

        # Fallback: if no paid orders, use every non-void order.
        if not product_orders:
            product_orders = apply_filters(
                orders, filters, allowed_statuses={"paid", "refunded"}
            )

        if not product_orders:
            return 0.0

        revenue_by_product, units_by_product = product_revenue(
            product_orders, rates
        )

        if asks_top:
            if asks_units:
                name = max(units_by_product, key=units_by_product.get)
                return name

            name = max(revenue_by_product, key=revenue_by_product.get)
            return name

        if filters["product"]:
            if asks_units:
                return units_by_product.get(filters["product"], 0)

            return round(revenue_by_product.get(filters["product"], 0.0), 2)

        # If somehow no top/product specified, return highest-revenue product.
        return max(revenue_by_product, key=revenue_by_product.get)

    # Average order value.
    if asks_average:
        average_orders = apply_filters(
            orders, filters, allowed_statuses={"paid"}
        )

        if not average_orders:
            return 0.0

        avg = sum(usd_amount(o, rates) for o in average_orders) / len(average_orders)
        return round(avg, 2)

    # Revenue.
    if asks_revenue:
        revenue_orders = apply_filters(
            orders, filters, allowed_statuses={"paid"}
        )

        total = sum(usd_amount(o, rates) for o in revenue_orders)
        return round(total, 2)

    # Order count.
    if asks_count or contains_phrase(q, ["orders", "order count"]):
        count_orders = apply_filters(orders, filters)
        return len(count_orders)

    return "I can answer questions about revenue, refunds, products, customers, and order counts."


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


@app.post("/")
@app.post("/answer")
def answer(request: QuestionRequest):
    try:
        return {"answer": answer_question(request.question)}
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve ledger data from the source API.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/debug")
def debug():
    orders, rates = load_data()

    sample = orders[:5]

    return {
        "total_orders": len(orders),
        "sample_orders": sample,
        "rates": rates,
        "fields_in_first_order": list(sample[0].keys()) if sample else [],
    }


@app.get("/debug/product-revenue")
def debug_product_revenue(month: int = None, year: int = None):
    orders, rates = load_data()

    filters = {"region": None, "month": month, "year": year, "status": None, "product": None}

    paid_orders = apply_filters(orders, filters, allowed_statuses={"paid"})
    revenue, units = product_revenue(paid_orders, rates)

    return {
        "month": month,
        "year": year,
        "paid_orders": len(paid_orders),
        "revenue_by_product": revenue,
        "units_by_product": units,
        "top_by_revenue": max(revenue, key=revenue.get) if revenue else None,
        "top_by_units": max(units, key=units.get) if units else None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
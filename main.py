
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Appliances Ledger Agent")

API_URL = os.getenv("LEDGER_API_URL")
TIMEOUT = 20
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

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
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


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
        raise RuntimeError("API is missing its export or rates link")

    response = requests.get(export_url, timeout=TIMEOUT)
    response.raise_for_status()
    raw_orders = parse_export(response.text)

    # Keep the latest version of each order ID.
    latest = {}
    for order in raw_orders:
        order_id = order.get("id")
        if order_id is None:
            continue

        previous = latest.get(order_id)
        if previous is None:
            latest[order_id] = order
        elif parse_date(order["updated_at"]) >= parse_date(
            previous["updated_at"]
        ):
            latest[order_id] = order

    rates_data = get_json(rates_url)
    rates = rates_data["usd_per_unit"]
    return list(latest.values()), rates


def usd_amount(order, rates):
    currency = str(order.get("currency", "")).upper()
    if currency not in rates:
        raise ValueError(f"Missing USD conversion rate for {currency}")
    return float(order.get("amount", 0) or 0) * float(rates[currency])


def order_quantity(order):
    for key in ("qty", "quantity", "units", "item_count"):
        value = order.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 1


def contains_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def detect_filters(question, orders):
    q = question.lower()

    region = next(
        (r for r in REGIONS if re.search(rf"\b{r}\b", q)),
        None,
    )
    month = next(
        (n for name, n in MONTHS.items()
         if re.search(rf"\b{name}\b", q)),
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
            for o in orders if o.get("product")
        },
        key=len,
        reverse=True,
    )

    product = next(
        (
            p for p in products
            if re.search(
                rf"(?<!\w){re.escape(p.lower())}(?!\w)", q
            )
        ),
        None,
    )

    return {
        "region": region,
        "month": month,
        "year": year,
        "status": status,
        "product": product,
    }


def apply_filters(orders, filters, allowed_statuses=None):
    result = list(orders)

    if filters.get("region"):
        result = [
            o for o in result
            if str(o.get("region", "")).lower()
            == filters["region"]
        ]

    if filters.get("product"):
        result = [
            o for o in result
            if str(o.get("product", "")).strip().lower()
            == filters["product"].lower()
        ]

    if filters.get("status"):
        result = [
            o for o in result
            if str(o.get("status", "")).lower()
            == filters["status"]
        ]

    month = filters.get("month")
    year = filters.get("year")

    if month or year:
        filtered = []
        for order in result:
            date_value = order.get("created_at")
            if not date_value:
                continue

            dt = parse_date(date_value)
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


def product_totals(orders, rates):
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
    asks_customer = contains_phrase(
        q, ["customer", "customers", "client", "clients"]
    )
    asks_average = contains_phrase(
        q, ["average", "on average", "avg", "mean"]
    )
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
    asks_units = contains_phrase(
        q, ["units", "quantity", "items sold", "most sold"]
    )
    asks_product = (
        filters["product"] is not None
        or contains_phrase(q, ["product", "products", "which item"])
    )
    asks_revenue = contains_phrase(
        q,
        ["revenue", "sales", "earned", "earnings", "income",
         "turnover", "generated", "bring in"],
    )
    asks_total = contains_phrase(
        q, ["total", "sum", "overall", "altogether"]
    )

    # Distinct customers for paid orders only when specified.
    if asks_customer:
        customer_filters = dict(filters)
        if "paid orders only" in q or "paid order only" in q:
            customer_filters["status"] = "paid"

        customer_orders = apply_filters(orders, customer_filters)
        customers = {
            str(o.get("customer", "")).strip()
            for o in customer_orders
            if o.get("customer")
        }
        return len(customers)

    if asks_refund:
        refund_orders = apply_filters(
            orders, filters, allowed_statuses={"refunded"}
        )

        if asks_count and not asks_average and not asks_total:
            return len(refund_orders)

        total = sum(usd_amount(o, rates) for o in refund_orders)

        if asks_average:
            return round(total / len(refund_orders), 2) if refund_orders else 0.0

        return round(total, 2)

    # Revenue/product questions exclude void orders.
    if asks_product:
        product_orders = apply_filters(
            orders, filters, allowed_statuses={"paid", "refunded"}
        )

        if not product_orders:
            return 0 if asks_count or asks_units else None

        revenue, units = product_totals(product_orders, rates)

        if asks_top:
            if asks_units:
                return max(units, key=units.get)
            return max(revenue, key=revenue.get)

        if filters.get("product"):
            name = filters["product"]
            if asks_units:
                return units.get(name, 0)
            return round(revenue.get(name, 0.0), 2)

        if asks_revenue:
            return round(sum(revenue.values()), 2)

        return max(revenue, key=revenue.get)

    if asks_average:
        selected = apply_filters(
            orders, filters, allowed_statuses={"paid", "refunded"}
        )
        if not selected:
            return 0.0
        total = sum(usd_amount(o, rates) for o in selected)
        return round(total / len(selected), 2)

    if asks_revenue:
        selected = apply_filters(
            orders, filters, allowed_statuses={"paid", "refunded"}
        )
        return round(sum(usd_amount(o, rates) for o in selected), 2)

    if asks_count or contains_phrase(q, ["orders", "order count"]):
        selected = apply_filters(orders, filters)
        return len(selected)

    return None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


@app.post("/")
@app.post("/answer")
def answer(request: QuestionRequest):
    try:
        result = answer_question(request.question)
        return {"answer": result}
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve ledger data from the source API.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )

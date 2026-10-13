
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Ledger Agent")

API_URL = os.getenv("LEDGER_API_URL")
TIMEOUT = 30
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
    return [
        json.loads(line)
        for line in text.splitlines()
        if line.strip()
    ]


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

    # Keep the latest record for each order ID.
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


def contains_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def detect_filters(question, orders):
    q = question.lower()

    region = next(
        (r for r in REGIONS if re.search(rf"\b{r}\b", q)),
        None,
    )

    month = next(
        (
            number
            for name, number in MONTHS.items()
            if re.search(rf"\b{name}\b", q)
        ),
        None,
    )

    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    status = next(
        (s for s in STATUSES if re.search(rf"\b{s}\b", q)),
        None,
    )

    products = sorted(
        {
            str(order.get("product", "")).strip()
            for order in orders
            if order.get("product")
        },
        key=len,
        reverse=True,
    )

    product = next(
        (
            name
            for name in products
            if re.search(
                rf"(?<!\w){re.escape(name.lower())}(?!\w)", q
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


def apply_filters(orders, filters, forced_status=None):
    result = list(orders)

    region = filters.get("region")
    product = filters.get("product")
    status = forced_status or filters.get("status")
    month = filters.get("month")
    year = filters.get("year")

    if region:
        result = [
            order for order in result
            if str(order.get("region", "")).strip().lower() == region
        ]

    if product:
        result = [
            order for order in result
            if str(order.get("product", "")).strip().lower()
            == product.lower()
        ]

    if status:
        result = [
            order for order in result
            if str(order.get("status", "")).strip().lower() == status
        ]

    if month or year:
        selected = []
        for order in result:
            date_value = order.get("created_at")
            if not date_value:
                continue

            dt = parse_date(date_value)

            if month and dt.month != month:
                continue
            if year and dt.year != year:
                continue

            selected.append(order)

        result = selected

    return result


def answer_question(question):
    if not question or not question.strip():
        return None

    orders, rates = load_data()
    q = question.lower().strip()
    filters = detect_filters(q, orders)

    asks_customer = contains_phrase(
        q, ["customer", "customers", "client", "clients"]
    )
    asks_refund = contains_phrase(q, ["refund", "refunded"])
    asks_average = contains_phrase(
        q, ["average", "on average", "avg", "mean"]
    )
    asks_count = contains_phrase(
        q,
        [
            "count", "how many", "number of",
            "total number", "distinct", "unique",
        ],
    )
    asks_top = contains_phrase(
        q,
        [
            "top", "highest", "most", "best",
            "best-selling", "best selling",
            "top-selling", "top selling",
        ],
    )
    asks_units = contains_phrase(
        q, ["units", "quantity", "items sold", "most sold"]
    )
    asks_revenue = contains_phrase(
        q,
        [
            "revenue", "sales", "earned", "earnings",
            "income", "turnover", "generated", "bring in",
        ],
    )
    asks_product = (
        filters["product"] is not None
        or contains_phrase(
            q, ["product", "products", "which item", "which product"]
        )
    )

    # 1. Distinct customer count.
    if asks_customer:
        customer_status = filters.get("status")

        if re.search(r"\bpaid\b", q):
            customer_status = "paid"

        customer_filters = dict(filters)
        customer_filters["status"] = customer_status

        customer_orders = apply_filters(orders, customer_filters)

        customers = {
            str(order.get("customer", "")).strip()
            for order in customer_orders
            if order.get("customer")
            and str(order.get("customer")).strip()
        }

        return len(customers)

    # 2. Refund-related questions.
    if asks_refund:
        refund_orders = apply_filters(
            orders, filters, forced_status="refunded"
        )

        if asks_count:
            return len(refund_orders)

        total = sum(usd_amount(order, rates) for order in refund_orders)

        if asks_average:
            if not refund_orders:
                return 0.0
            return round(total / len(refund_orders), 2)

        return round(total, 2)

    # Normal revenue and product ranking use paid orders only.
    paid_orders = apply_filters(
        orders, filters, forced_status="paid"
    )

    # 3. Product ranking.
    if asks_product:
        if not paid_orders:
            return None

        revenue_by_product = {}
        units_by_product = {}

        for order in paid_orders:
            name = str(order.get("product", "Unknown")).strip()
            revenue_by_product[name] = (
                revenue_by_product.get(name, 0.0)
                + usd_amount(order, rates)
            )

            try:
                qty = int(order.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0

            units_by_product[name] = (
                units_by_product.get(name, 0) + qty
            )

        if asks_top:
            if asks_units:
                return max(units_by_product, key=units_by_product.get)
            return max(revenue_by_product, key=revenue_by_product.get)

        if filters.get("product"):
            name = filters["product"]

            if asks_units:
                return units_by_product.get(name, 0)

            return round(revenue_by_product.get(name, 0.0), 2)

        if asks_revenue:
            return round(sum(revenue_by_product.values()), 2)

        return max(revenue_by_product, key=revenue_by_product.get)

    # 4. Average paid order value.
    if asks_average:
        if not paid_orders:
            return 0.0

        total = sum(usd_amount(order, rates) for order in paid_orders)
        return round(total / len(paid_orders), 2)

    # 5. Total paid revenue.
    if asks_revenue:
        total = sum(usd_amount(order, rates) for order in paid_orders)
        return round(total, 2)

    # 6. Order count.
    if asks_count or re.search(r"\border(s)?\b", q):
        selected = apply_filters(orders, filters)
        return len(selected)

    return None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )

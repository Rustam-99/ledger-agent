
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
TIMEOUT = 10
KOLKATA = ZoneInfo("Asia/Kolkata")

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
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KOLKATA)
    return dt.astimezone(KOLKATA)


def parse_export(text):
    """Support newline-delimited JSON and a JSON array."""
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

    # Retain only the latest version of each order ID.
    latest = {}
    for order in raw_orders:
        order_id = order["id"]
        previous = latest.get(order_id)

        if (
            previous is None
            or parse_date(order["updated_at"])
            > parse_date(previous["updated_at"])
        ):
            latest[order_id] = order

    rates_data = get_json(rates_url)
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


def usd_amount(order, rates, field="amount"):
    currency = str(order["currency"]).upper()

    if currency not in rates:
        raise ValueError(f"Missing USD conversion rate for {currency}")

    return float(order[field]) * float(rates[currency])


def contains_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def detect_filters(question, orders):
    q = question.lower()

    region = next(
        (
            r for r in REGIONS
            if re.search(rf"\b{re.escape(r)}\b", q)
        ),
        None,
    )

    month = next(
        (
            number for name, number in MONTHS.items()
            if re.search(rf"\b{name}\b", q)
        ),
        None,
    )

    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    status = next(
        (
            s for s in STATUSES
            if re.search(rf"\b{s}\b", q)
        ),
        None,
    )

    # Find a product mentioned in the question using actual data values.
    products = sorted(
        {
            str(o.get("product", "")).strip()
            for o in orders
            if o.get("product")
        },
        key=len,
        reverse=True,
    )

    product = next(
        (
            name for name in products
            if re.search(rf"\b{re.escape(name.lower())}\b", q)
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
    result = orders

    region = filters["region"]
    month = filters["month"]
    year = filters["year"]
    product = filters["product"]
    status = forced_status or filters["status"]

    if region:
        result = [
            o for o in result
            if str(o.get("region", "")).lower() == region
        ]

    if product:
        result = [
            o for o in result
            if str(o.get("product", "")).strip().lower()
            == product.lower()
        ]

    if status:
        result = [
            o for o in result
            if str(o.get("status", "")).lower() == status
        ]

    if month or year:
        result = [
            o for o in result
            if (
                (not month or parse_date(o["created_at"]).month == month)
                and
                (not year or parse_date(o["created_at"]).year == year)
            )
        ]

    return result


def answer_question(question):
    if not question or not question.strip():
        return "Please provide a question."

    orders, rates = load_data()
    q = question.lower().strip()
    filters = detect_filters(q, orders)

    # Detect the requested calculation.
    asks_customer = contains_phrase(
        q, ["customer", "customers", "distinct customer", "unique customer"]
    )

    asks_refund = contains_phrase(q, ["refund", "refunded"])
    asks_average = contains_phrase(
        q, ["average", "on average", "avg", "mean", "per order", "worth"]
    )

    asks_count = contains_phrase(
        q,
        [
            "count", "how many", "number of", "quantity of",
            "total number", "distinct", "unique",
        ],
    )

    asks_product = (
        filters["product"] is not None
        or contains_phrase(
            q,
            [
                "product", "products", "best-selling", "best selling",
                "top-selling", "top selling", "which item",
            ],
        )
    )

    asks_revenue = contains_phrase(
        q,
        [
            "revenue", "sales", "earn", "earned", "earns",
            "earning", "income", "turnover", "generated",
            "bring in", "how much did",
        ],
    )

    asks_total = contains_phrase(
        q, ["total", "sum", "overall", "altogether"]
    )

    # Questions about customers: count distinct customers after all filters.
    if asks_customer:
        customer_status = filters["status"]

        if "paid" in q:
            customer_status = "paid"

        customer_orders = apply_filters(
            orders, filters, forced_status=customer_status
        )

        customers = {
            str(o.get("customer", "")).strip()
            for o in customer_orders
            if o.get("customer")
        }

        return f"There are {len(customers)} unique customers."

    # Refund questions: counts, totals, or averages.
    if asks_refund:
        refund_orders = apply_filters(
            orders, filters, forced_status="refunded"
        )

        if asks_count and not asks_average and not asks_total:
            return f"There are {len(refund_orders)} refunded orders."

        if asks_average:
            if not refund_orders:
                return "No matching refunded orders were found."

            # "Worth" or "per unit" refers to unit price;
            # average order value refers to the full order amount.
            unit_metric = contains_phrase(
                q, ["unit price", "per unit", "item price", "worth"]
            )
            field = "unit_price" if unit_metric else "amount"

            average = sum(
                usd_amount(o, rates, field)
                for o in refund_orders
            ) / len(refund_orders)

            return f"The average refunded value is ${average:,.2f} USD."

        total = sum(
            usd_amount(o, rates)
            for o in refund_orders
        )

        if asks_count and asks_total:
            return (
                f"There are {len(refund_orders)} refunded orders. "
                f"Their total amount is ${total:,.2f} USD."
            )

        return f"Total refunded amount is ${total:,.2f} USD."

    # Average value questions.
    if asks_average:
        average_orders = apply_filters(
            orders, filters, forced_status="paid"
        )

        if not average_orders:
            return "No matching paid orders were found."

        # Distinguish average unit price from average order value.
        unit_metric = contains_phrase(
            q, ["unit price", "per unit", "item price", "worth"]
        )
        field = "unit_price" if unit_metric else "amount"

        average = sum(
            usd_amount(o, rates, field)
            for o in average_orders
        ) / len(average_orders)

        return f"The average value is ${average:,.2f} USD."

    # Product performance and revenue.
    if asks_product:
        product_orders = apply_filters(
            orders, filters, forced_status="paid"
        )

        if not product_orders:
            return "No matching paid product orders were found."

        totals = {}
        quantities = {}

        for order in product_orders:
            name = str(order.get("product", "Unknown"))
            totals[name] = (
                totals.get(name, 0.0)
                + usd_amount(order, rates)
            )
            quantities[name] = (
                quantities.get(name, 0)
                + int(order.get("qty", 0))
            )

        asks_top = contains_phrase(
            q,
            [
                "top", "best", "highest", "most", "best-selling",
                "best selling", "top-selling", "top selling",
            ],
        )

        if asks_top:
            if contains_phrase(q, ["units", "quantity", "most sold"]):
                name = max(quantities, key=quantities.get)
                return (
                    f"{name} sold the most units: "
                    f"{quantities[name]} units."
                )

            name = max(totals, key=totals.get)
            return (
                f"{name} had the highest revenue at "
                f"${totals[name]:,.2f} USD."
            )

        if filters["product"]:
            total = sum(totals.values())
            return (
                f"Paid revenue for {filters['product']} is "
                f"${total:,.2f} USD."
            )

        return "Product revenue (USD): " + "; ".join(
            f"{name}: ${amount:,.2f}"
            for name, amount in sorted(totals.items())
        )

    # Revenue and sales: paid orders only.
    if asks_revenue:
        revenue_orders = apply_filters(
            orders, filters, forced_status="paid"
        )

        total = sum(
            usd_amount(o, rates)
            for o in revenue_orders
        )

        return f"Total revenue is ${total:,.2f} USD."

    # Order counts. Respect any explicit status filter.
    if asks_count or contains_phrase(q, ["orders", "order count"]):
        count_orders = apply_filters(orders, filters)
        return f"There are {len(count_orders)} matching orders."

    return (
        "I can answer questions about revenue, refunds, average values, "
        "products, customers, and order counts. Include a region, "
        "product, status, month, or year to narrow the results."
    )


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


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
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KOLKATA)
    return dt.astimezone(KOLKATA)


def parse_export(text):
    """Read either JSON array or newline-delimited JSON."""
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

    # Keep the most recently updated record for each order ID.
    latest = {}
    for order in raw_orders:
        order_id = order.get("id")
        if order_id is None:
            continue

        previous = latest.get(order_id)
        if previous is None:
            latest[order_id] = order
        else:
            if parse_date(order["updated_at"]) >= parse_date(
                previous["updated_at"]
            ):
                latest[order_id] = order

    rates_data = get_json(rates_url)
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


def usd_amount(order, rates, field="amount"):
    currency = str(order.get("currency", "")).upper()
    if currency not in rates:
        raise ValueError(f"Missing USD conversion rate for {currency}")
    return float(order.get(field, 0)) * float(rates[currency])


def contains_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def detect_filters(question, orders):
    q = question.lower()

    region = next(
        (r for r in REGIONS if re.search(rf"\b{r}\b", q)), None
    )
    month = next(
        (n for name, n in MONTHS.items()
         if re.search(rf"\b{name}\b", q)), None
    )
    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    status = next(
        (s for s in STATUSES if re.search(rf"\b{s}\b", q)), None
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
        (p for p in products
         if re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", q)),
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
    month = filters.get("month")
    year = filters.get("year")
    product = filters.get("product")
    status = forced_status or filters.get("status")

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

    return result


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
        q, ["count", "how many", "number of", "total number",
            "quantity of", "distinct", "unique"]
    )
    asks_top = contains_phrase(
        q, ["top", "highest", "most", "best", "best-selling",
            "best selling", "top-selling", "top selling"]
    )
    asks_units = contains_phrase(
        q, ["units", "quantity", "items sold", "most sold"]
    )
    asks_product = (
        filters["product"] is not None
        or contains_phrase(q, ["product", "products", "which item"])
    )
    asks_revenue = contains_phrase(
        q, ["revenue", "sales", "earned", "earnings", "income",
            "turnover", "generated", "bring in"]
    )
    asks_total = contains_phrase(
        q, ["total", "sum", "overall", "altogether"]
    )

    # Customer questions: count distinct customer names.
    if asks_customer:
        customer_orders = apply_filters(orders, filters)
        customers = {
            str(o.get("customer", "")).strip()
            for o in customer_orders
            if o.get("customer")
        }
        return f"There are {len(customers)} unique customers."

    # Refund questions.
    if asks_refund:
        refund_orders = apply_filters(
            orders, filters, forced_status="refunded"
        )

        if asks_count and not asks_average and not asks_total:
            return f"There are {len(refund_orders)} refunded orders."

        if asks_average:
            if not refund_orders:
                return "No matching refunded orders were found."
            avg = sum(
                usd_amount(o, rates) for o in refund_orders
            ) / len(refund_orders)
            return f"The average refunded order value is ${avg:,.2f} USD."

        total = sum(usd_amount(o, rates) for o in refund_orders)
        if asks_count and asks_total:
            return (
                f"There are {len(refund_orders)} refunded orders. "
                f"Total refunded amount is ${total:,.2f} USD."
            )
        return f"Total refunded amount is ${total:,.2f} USD."

    # Product performance questions.
    if asks_product:
        product_orders = apply_filters(
            orders, filters, forced_status="paid"
        )

        if not product_orders:
            return "No matching paid product orders were found."

        revenue_by_product = {}
        units_by_product = {}

        for order in product_orders:
            name = str(order.get("product", "Unknown"))
            revenue_by_product[name] = (
                revenue_by_product.get(name, 0.0)
                + usd_amount(order, rates)
            )
            units_by_product[name] = (
                units_by_product.get(name, 0)
                + int(order.get("qty", 0))
            )

        if asks_top:
            if asks_units:
                name = max(units_by_product, key=units_by_product.get)
                return (
                    f"{name} sold the most units: "
                    f"{units_by_product[name]} units."
                )
            name = max(revenue_by_product, key=revenue_by_product.get)
            return (
                f"{name} had the highest revenue at "
                f"${revenue_by_product[name]:,.2f} USD."
            )

        if filters["product"]:
            total = sum(revenue_by_product.values())
            if asks_units:
                units = sum(units_by_product.values())
                return (
                    f"Total units sold for {filters['product']}: "
                    f"{units} units."
                )
            return (
                f"Paid revenue for {filters['product']} is "
                f"${total:,.2f} USD."
            )

        return "Product revenue (USD): " + "; ".join(
            f"{name}: ${amount:,.2f}"
            for name, amount in sorted(revenue_by_product.items())
        )

    # Average order value for paid orders.
    if asks_average:
        average_orders = apply_filters(
            orders, filters, forced_status="paid"
        )
        if not average_orders:
            return "No matching paid orders were found."

        avg = sum(
            usd_amount(o, rates) for o in average_orders
        ) / len(average_orders)
        return f"The average paid order value is ${avg:,.2f} USD."

    # Revenue is calculated from paid orders only.
    if asks_revenue:
        revenue_orders = apply_filters(
            orders, filters, forced_status="paid"
        )
        total = sum(
            usd_amount(o, rates) for o in revenue_orders
        )
        return f"Total revenue is ${total:,.2f} USD."

    # Order counts.
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

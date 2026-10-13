import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import lru_cache

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

API_URL = os.getenv("LEDGER_API_URL")
KOLKATA = ZoneInfo("Asia/Kolkata")
TIMEOUT = 10

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def get_json(url):
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_date(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KOLKATA)

    return dt.astimezone(KOLKATA)


@lru_cache(maxsize=1)
def load_data():
    if not API_URL:
        raise RuntimeError(
            "LEDGER_API_URL environment variable is missing"
        )

    # Discover API links.
    root = get_json(API_URL)
    links = root.get("links", {})

    if not links.get("export") or not links.get("rates"):
        raise RuntimeError(
            "API root did not provide export and rates links"
        )

    # Download the order export.
    response = requests.get(links["export"], timeout=TIMEOUT)
    response.raise_for_status()

    # Keep the latest version of each order.
    latest = {}

    for line in response.text.splitlines():
        if not line.strip():
            continue

        order = json.loads(line)
        order_id = order["id"]
        old = latest.get(order_id)

        if (
            old is None
            or parse_date(order["updated_at"])
            > parse_date(old["updated_at"])
        ):
            latest[order_id] = order

    # Load currency conversion rates.
    rates_data = get_json(links["rates"])
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


def usd_amount(order, rates):
    currency = order["currency"]

    if currency not in rates:
        raise ValueError(f"Unknown currency: {currency}")

    return float(order["amount"]) * float(rates[currency])


def answer_question(question):
    orders, rates = load_data()
    q = question.lower()

    # Region filter.
    regions = ["north", "south", "east", "west"]

    region = next(
        (r for r in regions if re.search(rf"\b{r}\b", q)),
        None,
    )

    # Month and year filters.
    month = next(
        (
            num
            for name, num in MONTHS.items()
            if re.search(rf"\b{name}\b", q)
        ),
        None,
    )

    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    filtered = orders

    if region:
        filtered = [
            order for order in filtered
            if str(order.get("region", "")).lower() == region
        ]

    if month or year:
        filtered = [
            order for order in filtered
            if (
                (not month or parse_date(
                    order["created_at"]
                ).month == month)
                and
                (not year or parse_date(
                    order["created_at"]
                ).year == year)
            )
        ]

    # Status filters.
    paid = [
        order for order in filtered
        if str(order.get("status", "")).lower() == "paid"
    ]

    refunded = [
        order for order in filtered
        if str(order.get("status", "")).lower() == "refunded"
    ]


    # Average order value MUST come before order-count logic.
    average_question = any(
        phrase in q
        for phrase in [
            "average",
            "avg",
            "mean",
            "per order",
            "worth",
        ]
    )

    if average_question:
        if "refund" in q:
            average_orders = refunded
        else:
            # Average paid order value.
            average_orders = paid

        if not average_orders:
            return "No matching orders were found."

        total = sum(
            usd_amount(order, rates)
            for order in average_orders
        )

        average = total / len(average_orders)

        # Temporary debugging line
        print(
            "Average check:",
            len(average_orders),
            total,
            average,
        )

        return (
            f"The average order value is "
            f"${average:,.2f} USD."
        )


    # Refund questions.
    if any(
        word in q
        for word in ["refund", "refunded"]
    ):
        total = sum(
            usd_amount(order, rates)
            for order in refunded
        )

        return (
            f"There are {len(refunded)} refunded orders. "
            f"Their total amount is ${total:,.2f} USD."
        )

    # Unique customers.
 
    # Unique customers, optionally filtered by product and paid status.
    if "customer" in q or "customers" in q:
        customer_orders = filtered

        # If the question asks for paid orders only, filter by paid status.
        if "paid" in q:
            customer_orders = paid

        # Detect a product name mentioned in the question.
        product_names = {
            str(order.get("product", "")).strip()
            for order in orders
            if order.get("product")
        }

        mentioned_products = [
            name for name in product_names
            if name.lower() in q
        ]

        if mentioned_products:
            customer_orders = [
                order for order in customer_orders
                if str(order.get("product", "")).strip()
                in mentioned_products
            ]

        customers = {
            order.get("customer")
            for order in customer_orders
            if order.get("customer")
        }

        return f"There are {len(customers)} unique customers."


    # Product revenue and top product.
    if any(
        word in q
        for word in [
            "product",
            "products",
            "best-selling",
            "best selling",
        ]
    ):
        totals = {}

        for order in paid:
            name = order["product"]
            totals[name] = (
                totals.get(name, 0.0)
                + usd_amount(order, rates)
            )

        if not totals:
            return "No matching paid orders were found."

        if any(
            word in q
            for word in [
                "most",
                "highest",
                "top",
                "best",
                "which product",
            ]
        ):
            name = max(totals, key=totals.get)

            return (
                f"{name} has the highest revenue at "
                f"${totals[name]:,.2f} USD."
            )

        return "Product revenue (USD): " + "; ".join(
            f"{name}: ${amount:,.2f}"
            for name, amount in sorted(totals.items())
        )


    # Revenue includes paid orders only.
    if any(
        word in q
        for word in [
            "revenue",
            "sales",
            "earn",
            "earned",
            "earns",
            "earning",
            "income",
            "turnover",
            "how much did",
        ]
    ):
        total = sum(
            usd_amount(order, rates)
            for order in paid
        )

        return f"Total revenue is ${total:,.2f} USD."

    # Order counts.
    if any(
        word in q
        for word in [
            "count",
            "number of",
            "how many",
            "orders",
            "order count",
        ]
    ):
        return f"There are {len(filtered)} matching orders."

    return (
        "Please ask about revenue, refunds, average order value, "
        "products, customers, or order counts. "
        "You can include a region, month, or year."
    )


class QuestionRequest(BaseModel):
    question: str


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


@app.post("/answer")
def answer(request: QuestionRequest):
    try:
        return {"answer": answer_question(request.question)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )

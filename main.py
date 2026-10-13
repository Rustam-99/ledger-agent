
import json
import os
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

DATA_FILE = Path(__file__).parent / "questionData"

RATES = {
    "USD": 1.0,
    "EUR": 1.14,
    "INR": 0.01217,
}

KOLKATA = ZoneInfo("Asia/Kolkata")


def load_orders():
    """Load the downloaded JSON Lines export."""
    if not DATA_FILE.exists():
        raise RuntimeError(
            "questionData file not found beside main.py"
        )

    latest = {}

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue

            order = json.loads(line)
            order_id = order["id"]

            old = latest.get(order_id)

            if old is None or order["updated_at"] > old["updated_at"]:
                latest[order_id] = order

    return list(latest.values())


def usd_amount(order):
    currency = order["currency"]
    amount = float(order["amount"])

    if currency not in RATES:
        raise ValueError(f"Unknown currency: {currency}")

    return amount * RATES[currency]


def parse_date(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(KOLKATA)


def answer_question(question):
    orders = load_orders()
    q = question.lower()

    # Identify an optional region.
    regions = ["north", "south", "east", "west"]
    selected_region = next(
        (r.title() for r in regions if r in q), None
    )

    # Identify an optional month and year.
    months = {
        "january": 1, "february": 2, "march": 3,
        "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }

    selected_month = next(
        (number for name, number in months.items() if name in q),
        None
    )

    year_match = re.search(r"\b(20\d{2})\b", q)
    selected_year = int(year_match.group(1)) if year_match else None

    # Apply filters.
    filtered = orders

    if selected_region:
        filtered = [
            o for o in filtered
            if o["region"].lower() == selected_region.lower()
        ]

    if selected_month or selected_year:
        filtered = [
            o for o in filtered
            if (
                (not selected_month or
                 parse_date(o["created_at"]).month == selected_month)
                and
                (not selected_year or
                 parse_date(o["created_at"]).year == selected_year)
            )
        ]

    # Revenue only includes paid orders.
    paid = [o for o in filtered if o["status"].lower() == "paid"]

    if any(word in q for word in ["refund", "refunded"]):
        refunded = [
            o for o in filtered
            if o["status"].lower() == "refunded"
        ]
        total = sum(usd_amount(o) for o in refunded)
        return (
            f"There are {len(refunded)} refunded orders. "
            f"Their total amount is ${total:,.2f} USD."
        )

    if any(word in q for word in ["revenue", "sales", "earned"]):
        total = sum(usd_amount(o) for o in paid)
        return f"Total revenue is ${total:,.2f} USD."

    if "customer" in q:
        customers = {o["customer"] for o in filtered}
        return f"There are {len(customers)} unique customers."

    if "product" in q:
        totals = {}
        for order in paid:
            product = order["product"]
            totals[product] = (
                totals.get(product, 0) + usd_amount(order)
            )

        if not totals:
            return "No matching paid orders were found."

        if any(word in q for word in ["most", "highest", "top", "best"]):
            product = max(totals, key=totals.get)
            return (
                f"{product} has the highest revenue at "
                f"${totals[product]:,.2f} USD."
            )

        return "Product revenue (USD): " + "; ".join(
            f"{name}: ${value:,.2f}"
            for name, value in sorted(totals.items())
        )

    if any(word in q for word in ["count", "number of", "how many"]):
        return f"There are {len(filtered)} matching orders."

    return (
        "I could not confidently interpret that question. "
        "Please ask about revenue, refunds, customers, or products, "
        "and include a region, month, or year if needed."
    )


class QuestionRequest(BaseModel):
    question: str


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


@app.post("/answer")
def answer(request: QuestionRequest):
    try:
        result = answer_question(request.question)
        return {"answer": result}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

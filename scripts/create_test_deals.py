"""Create the 5 required test deals in YOUR HubSpot test account (Task 2.2) and record their IDs.

Usage:  HUBSPOT_ACCESS_TOKEN=pat-xxx python scripts/create_test_deals.py
Needs the scope `crm.objects.deals.write` on the private app (add it temporarily, or create the
deals by hand in the HubSpot UI). Writes test-results/test-deals.json.
"""
import json
import os
import sys

import requests

TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN")
if not TOKEN:
    sys.exit("Set HUBSPOT_ACCESS_TOKEN first (never commit it).")

DEALS = [
    ("Qualified Lead - Acme",        5000,   "qualifiedtobuy",        "newbusiness",      "2026-11-01"),
    ("Presentation - Globex",        25000,  "presentationscheduled", "newbusiness",      "2026-11-15"),
    ("Closed Won - Initech",         50000,  "closedwon",             "existingbusiness", "2026-09-30"),
    ("Closed Lost - Umbrella",       75000,  "closedlost",            "newbusiness",      "2026-09-15"),
    ("Qualified Enterprise - Hooli", 100000, "qualifiedtobuy",        "existingbusiness", "2026-12-20"),
]
out = []
for name, amount, stage, dtype, close in DEALS:
    r = requests.post("https://api.hubapi.com/crm/v3/objects/deals",
                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30,
                      json={"properties": {"dealname": name, "amount": str(amount), "dealstage": stage,
                                           "pipeline": "default", "dealtype": dtype,
                                           "closedate": f"{close}T00:00:00Z",
                                           "description": f"Test deal for DLT extraction: {name}"}})
    r.raise_for_status()
    out.append({"id": r.json()["id"], "dealname": name, "amount": amount, "dealstage": stage})
    print("created", out[-1])
json.dump(out, open("test-results/test-deals.json", "w"), indent=2)

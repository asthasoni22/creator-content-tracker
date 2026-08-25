# Runs inside a Zapier "Code by Zapier" (Python) step — not standalone.
# Reads input_data: payout_rate (from Creators lookup), engagements (from step 01).
# Returns `output` dict: cost_per_engagement (None when engagements is 0).

rate = float(input_data.get("payout_rate") or 0)
eng = int(input_data.get("engagements") or 0)

# A video posted minutes ago can genuinely have zero engagements.
# Dividing anyway throws and kills the run for that record.
output = {
    "cost_per_engagement": round(rate / eng, 6) if eng > 0 else None,
    "payout_rate": rate,
}
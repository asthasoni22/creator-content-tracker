# Runs inside a Zapier "Code by Zapier" (Python) step — not standalone.
# Reads input_data: ai_response (raw text from the AI classifier step).
# Returns `output` dict: brand_mentioned, disclosure_present, ai_confidence, parse_ok.

import json
 
raw = (input_data.get("ai_response") or "").strip()
 
# Default to "pending", never to "no". A parse failure means we did not
# get an answer — that must not be recorded as a confident negative, or
# the compliance dashboard reports clean data it never actually checked.
result = {
    "brand_mentioned": "pending",
    "disclosure_present": "pending",
    "ai_confidence": "pending",
    "parse_ok": False,
}
 
if raw:
    # Models wrap JSON in ```json fences more often than the prompt
    # implies. Slice from the first brace to the last rather than
    # trying to strip every fence variant.
    start = raw.find("{")
    end = raw.rfind("}")
 
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
 
            def clean(value):
                v = str(value).strip().lower()
                return v if v in ("yes", "no") else "pending"
 
            conf = str(parsed.get("confidence", "")).strip().lower()
 
            result = {
                "brand_mentioned": clean(parsed.get("brand_mentioned")),
                "disclosure_present": clean(parsed.get("disclosure_present")),
                "ai_confidence": conf if conf in ("high", "low") else "pending",
                "parse_ok": True,
            }
        except ValueError as exc:
            # Surfaces in Zap History without killing the run. The row
            # still gets written, just marked pending.
            print("JSON parse failed: %s | raw: %s" % (exc, raw[:200]))
    else:
        print("No JSON object found in response: %s" % raw[:200])
else:
    print("Empty AI response")
 
output = result
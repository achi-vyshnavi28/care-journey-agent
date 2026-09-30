"""The agent's tools. Two call real public healthcare APIs; the rest write to the outbox and never contact anyone
directly (a human or an integration sends what the agent queues).

- verify_provider: NPPES NPI Registry (CMS), checks the ordering provider and returns taxonomy, phone and fax
- coverage_policy: CMS Coverage API, the National Coverage Determination the request is judged against
- request_records / notify_member / escalate_to_clinician: queued actions with idempotency keys
"""
import html
import os
import re

import httpx
from pydantic import BaseModel, Field

NPPES = os.getenv("NPPES_URL", "https://npiregistry.cms.hhs.gov/api/")
COVERAGE = os.getenv("CMS_COVERAGE_URL", "https://api.coverage.cms.gov/v1/data/ncd/")
POLICY_NCD = {"ncd_100_1_bariatric": (57, 5), "ncd_240_4_cpap": (226, 3)}
RECORD_FOR_FACT = {
    "bmi": "Most recent height, weight and documented body-mass index",
    "obesity_comorbidity": "Problem list or notes documenting obesity-related co-morbidities (e.g. type 2 diabetes, hypertension, sleep apnea)",
    "prior_medical_treatment_failed": "Records of prior supervised medical weight-loss treatment and its outcome",
    "sleep_test_documented": "The diagnostic sleep study report (attended PSG or home sleep test)",
    "ahi": "The diagnostic sleep study report with the overall AHI or RDI (not the CPAP titration)",
    "osa_symptoms": "Clinical notes documenting daytime sleepiness, impaired cognition, mood disorder or insomnia",
    "cardiovascular_comorbidity": "Documentation of hypertension, ischemic heart disease or stroke history",
}


class ToolError(Exception):
    pass


def _get(url, params, client=None):
    c = client or httpx
    r = c.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def verify_provider(npi: str, client=None) -> dict:
    if not re.fullmatch(r"\d{10}", npi or ""):
        raise ToolError("an NPI is 10 digits")
    data = _get(NPPES, {"version": "2.1", "number": npi}, client)
    if not data.get("result_count"):
        return {"npi": npi, "valid": False}
    r = data["results"][0]
    basic = r.get("basic", {})
    name = basic.get("organization_name") or " ".join(filter(None, [basic.get("first_name"), basic.get("last_name")]))
    loc = next((a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), (r.get("addresses") or [{}])[0])
    primary = next((t for t in r.get("taxonomies", []) if t.get("primary")), (r.get("taxonomies") or [{}])[0])
    return {"npi": npi, "valid": True, "name": name, "type": r.get("enumeration_type"), "taxonomy": primary.get("desc"),
            "phone": loc.get("telephone_number"), "fax": loc.get("fax_number"),
            "city": loc.get("city"), "state": loc.get("state"), "status": basic.get("status")}


def coverage_policy(policy_id: str, client=None) -> dict:
    if policy_id not in POLICY_NCD:
        raise ToolError(f"unknown policy {policy_id}")
    ncd_id, ver = POLICY_NCD[policy_id]
    data = _get(COVERAGE, {"ncdid": ncd_id, "ncdver": ver}, client)["data"][0]
    raw = html.unescape(data.get("indications_limitations") or "")  # the API returns entity-escaped HTML
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))).strip()
    return {"ncd": data["document_display_id"], "title": data["title"], "effective": data["effective_date"],
            "indications_excerpt": text[:600]}


class RequestRecords(BaseModel):
    missing_facts: list[str] = Field(min_length=1)
    channel: str = "fax"


def records_request_text(case_id: str, provider: dict, missing: list[str]) -> str:
    items = "\n".join(f"- {RECORD_FOR_FACT.get(f, f)}" for f in missing)
    return (f"Prior authorization {case_id}: additional documentation needed to complete the review.\n"
            f"To: {provider.get('name', 'ordering provider')} (NPI {provider.get('npi')}), fax {provider.get('fax') or 'n/a'}\n"
            f"Please send:\n{items}")


# OpenAI-compatible function-calling schemas the LLM planner may use
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "verify_provider", "description": "Look up the ordering provider in the NPPES NPI Registry",
                                      "parameters": {"type": "object", "properties": {"npi": {"type": "string"}}, "required": ["npi"]}}},
    {"type": "function", "function": {"name": "coverage_policy", "description": "Fetch the CMS National Coverage Determination for the request",
                                      "parameters": {"type": "object", "properties": {"policy_id": {"type": "string"}}, "required": ["policy_id"]}}},
    {"type": "function", "function": {"name": "request_records", "description": "Queue a records request to the verified ordering provider for the missing facts",
                                      "parameters": {"type": "object", "properties": {"missing_facts": {"type": "array", "items": {"type": "string"}},
                                                                                     "channel": {"type": "string", "enum": ["fax", "portal"]}},
                                                     "required": ["missing_facts"]}}},
    {"type": "function", "function": {"name": "notify_member", "description": "Queue a plain-language status update to the member",
                                      "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
    {"type": "function", "function": {"name": "escalate_to_clinician", "description": "Escalate the case to a clinician reviewer now",
                                      "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "finish", "description": "Stop: the journey has everything it needs for now",
                                      "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]

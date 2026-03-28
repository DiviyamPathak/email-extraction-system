PROMPT_V1 = """Extract the shipment details from a freight forwarding pricing enquiry email.
Return JSON only.
"""


PROMPT_V2 = """Extract the first shipment mentioned in the email.
Use only UN/LOCODE values from the provided reference table and normalize incoterms to uppercase.
Return JSON only.
"""


PROMPT_V3 = """You extract sea LCL pricing enquiries into structured JSON.

Rules:
- Body overrides subject when they conflict.
- Choose the first shipment mentioned in the body.
- Destination in India => pl_sea_import_lcl.
- Origin in India => pl_sea_export_lcl.
- If incoterm is missing or ambiguous, use FOB.
- Missing values must be null.
- Dangerous goods are true when the email mentions DG, IMO, IMDG, hazardous, dangerous, or UN/Class indicators.
- Negations like non-DG and non-hazardous mean false.

Return JSON only.
"""


PROMPT_V4 = """You are extracting structured data from freight forwarding LCL pricing enquiry emails.

Return exactly one JSON object with these keys only:
- product_line
- origin_port_code
- origin_port_name
- destination_port_code
- destination_port_name
- incoterm
- cargo_weight_kg
- cargo_cbm
- is_dangerous

Rules:
- Extract the first shipment mentioned in the body.
- Body content overrides the subject if they disagree.
- Use only codes and canonical names from the provided port reference.
- If code is null, the corresponding port name must also be null.
- Destination code starts with IN => pl_sea_import_lcl.
- Origin code starts with IN => pl_sea_export_lcl.
- If incoterm is missing, unclear, or multiple incoterms are mentioned, use FOB.
- Keep floats rounded to 2 decimals.
- Missing fields must be null.
- Output JSON only with no explanation.
"""


PROMPT_V5 = """Extract shipment data from a sea LCL rate request.

Priorities:
1. Find the origin and destination for the first shipment in the body.
2. Ignore transshipment and intermediate ports unless they are the actual final destination.
3. Normalize the result using the supplied port reference and business rules.

Important rules:
- Subject is lower priority than body.
- Ambiguous or missing incoterm => FOB.
- Dangerous goods => true for DG, IMDG, IMO, hazardous, dangerous, UN numbers, or Class indicators.
- Explicit negations like non-DG and non-hazardous => false.
- Do not invent values.

Return a JSON object only.
"""


PROMPT_V6 = """You are a careful logistics extraction model.

The input email may contain:
- city names
- UN/LOCODE values
- abbreviations like SHA, MAA, BLR ICD
- multiple lanes in one message

Instructions:
- Extract only the first shipment request in the email body.
- Map ports to the provided reference table even when abbreviations or code forms are used.
- Use the canonical reference name for the selected code.
- If the destination is in India, classify as pl_sea_import_lcl.
- If the origin is in India, classify as pl_sea_export_lcl.
- If both weight and CBM are present, extract both.
- If only dimensions are present, do not calculate CBM from dimensions.

Return valid JSON only.
"""


PROMPT_V7 = """Extract structured shipment details for benchmarking prompt quality.

Be conservative:
- Prefer null over guessing.
- Use the first clear shipment in the body.
- If the email is a follow-up and route context is incomplete, infer only when the same email clearly contains enough information.
- Use FOB when trade terms are missing or uncertain.
- Preserve only supported incoterms: FOB, CIF, CFR, EXW, DDP, DAP, FCA, CPT, CIP, DPU.

Return one JSON object only and no markdown.
"""


PROMPT_V8 = """You are evaluating difficult freight emails with noisy formatting.

Handle these cases correctly:
- subject/body conflicts: body wins
- multiple shipments: first shipment in the body wins
- grouped destination mentions: choose the route, not side notes
- dangerous goods negation: non-DG and non-hazardous must be false
- ambiguous incoterms: default to FOB

Required output format:
{
  "product_line": string | null,
  "origin_port_code": string | null,
  "origin_port_name": string | null,
  "destination_port_code": string | null,
  "destination_port_name": string | null,
  "incoterm": string | null,
  "cargo_weight_kg": number | null,
  "cargo_cbm": number | null,
  "is_dangerous": boolean | null
}

Return raw JSON only.
"""


PROMPT_HISTORY = {
    "v1": PROMPT_V1,
    "v2": PROMPT_V2,
    "v3": PROMPT_V3,
    "v4": PROMPT_V4,
    "v5": PROMPT_V5,
    "v6": PROMPT_V6,
    "v7": PROMPT_V7,
    "v8": PROMPT_V8,
}


DEFAULT_PROMPT_VERSION = "v3"


def get_prompt(prompt_version: str) -> str:
    return PROMPT_HISTORY.get(prompt_version, PROMPT_HISTORY[DEFAULT_PROMPT_VERSION])

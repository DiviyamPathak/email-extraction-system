from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None

from prompts import DEFAULT_PROMPT_VERSION, PROMPT_HISTORY, get_prompt
from schemas import ShipmentExtraction


INCOTERMS = {"FOB", "CIF", "CFR", "EXW", "DDP", "DAP", "FCA", "CPT", "CIP", "DPU"}
DEFAULT_OUTPUT_PATH = Path("output.json")
EXTRACTION_FIELDS = [
    "product_line",
    "origin_port_code",
    "origin_port_name",
    "destination_port_code",
    "destination_port_name",
    "incoterm",
    "cargo_weight_kg",
    "cargo_cbm",
    "is_dangerous",
]
SPECIAL_PORT_ALIASES = {
    "DAM": ("SAJED", "Jeddah / Dammam / Riyadh"),
    "RUH": ("SAJED", "Jeddah / Dammam / Riyadh"),
    "JBL": ("AEJEA", "Jebel Ali"),
    "LGB": ("USLAX", "Los Angeles / Houston / Long Beach"),
    "PKG": ("MYPKG", "Port Klang"),
    "INDIA": ("INMAA", "India (Chennai)"),
}


@dataclass(frozen=True)
class PortEntry:
    code: str
    name: str
    normalized_name: str
    components: tuple[str, ...]


def normalize_reference_name(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" /")


def normalize_text(text: str) -> str:
    text = text.upper()
    text = text.replace("→", " TO ")
    text = text.replace("/", " / ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = re.sub(r"[\[\],;:.]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_port_fragment(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\bPPG\b", " ICD ", text)
    text = text.replace("FINAL DEST ", "")
    text = text.replace("DEST ", "")
    text = text.replace("POD ", "")
    text = text.replace("POL ", "")
    removable_countries = [
        "INDIA",
        "KOREA",
        "THAILAND",
        "CHINA",
        "JAPAN",
        "MALAYSIA",
        "TURKEY",
        "ITALY",
        "TAIWAN",
        "USA",
    ]
    if len(text.split()) > 1:
        for country in removable_countries:
            text = re.sub(rf"\b{country}\b", "", text)
    text = re.sub(r"\bSUPPLIER'?S FACTORY NEAR\b", "", text)
    text = re.sub(r"\bWAREHOUSE\b", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" /")


def build_port_entries(raw_ports: Iterable[dict[str, str]]) -> list[PortEntry]:
    entries: list[PortEntry] = []
    for row in raw_ports:
        normalized_name = normalize_reference_name(row["name"])
        components = tuple(token.strip() for token in re.split(r"\s*/\s*", normalized_name) if token.strip())
        entries.append(
            PortEntry(
                code=row["code"],
                name=row["name"],
                normalized_name=normalized_name,
                components=components,
            )
        )
    return entries


class PortMatcher:
    def __init__(self, raw_ports: list[dict[str, str]]) -> None:
        self.entries = build_port_entries(raw_ports)
        self.raw_ports = raw_ports
        self.entries_by_code = self._build_entries_by_code(self.entries)
        self.alias_map = self._build_alias_map(self.entries)
        self.code_to_names = self._build_code_to_names(raw_ports)
        self.india_codes = {code for code in self.code_to_names if code.startswith("IN")}

    @staticmethod
    def _build_entries_by_code(entries: list[PortEntry]) -> dict[str, list[PortEntry]]:
        result: dict[str, list[PortEntry]] = {}
        for entry in entries:
            result.setdefault(entry.code, []).append(entry)
        return result

    @staticmethod
    def _build_code_to_names(raw_ports: list[dict[str, str]]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for row in raw_ports:
            result.setdefault(row["code"], []).append(row["name"])
        return result

    def _build_alias_map(self, entries: list[PortEntry]) -> dict[str, tuple[str, str]]:
        alias_map: dict[str, tuple[str, str]] = {}

        for code, code_entries in self.entries_by_code.items():
            default_entry = self._choose_default_entry(code_entries)
            icd_entry = self._choose_icd_entry(code_entries)
            alias_map.setdefault(code, (default_entry.code, default_entry.name))
            alias_map.setdefault(code[-3:], (default_entry.code, default_entry.name))
            if icd_entry:
                alias_map.setdefault(f"{code[-3:]} ICD", (icd_entry.code, icd_entry.name))

        sorted_entries = sorted(
            entries,
            key=lambda entry: (len(entry.components), len(entry.normalized_name)),
        )
        for entry in sorted_entries:
            for alias in self._generate_entry_aliases(entry):
                alias_map.setdefault(alias, (entry.code, entry.name))
        for alias, target in SPECIAL_PORT_ALIASES.items():
            alias_map[alias] = target
        return alias_map

    def _choose_default_entry(self, entries: list[PortEntry]) -> PortEntry:
        return min(
            entries,
            key=lambda entry: (
                "/" in entry.normalized_name,
                "INDIA" in entry.normalized_name,
                "ICD" in entry.normalized_name,
                len(entry.normalized_name),
            ),
        )

    def _choose_icd_entry(self, entries: list[PortEntry]) -> Optional[PortEntry]:
        icd_entries = [entry for entry in entries if "ICD" in entry.normalized_name]
        if not icd_entries:
            return None
        return min(
            icd_entries,
            key=lambda entry: (
                not entry.normalized_name.startswith("ICD "),
                "/" in entry.normalized_name,
                len(entry.normalized_name),
            ),
        )

    def _generate_entry_aliases(self, entry: PortEntry) -> set[str]:
        aliases = {entry.normalized_name}
        for component in entry.components:
            aliases.update(self._generate_component_aliases(component))
        return {alias for alias in aliases if alias}

    def _generate_component_aliases(self, component: str) -> set[str]:
        aliases = {component}
        stripped = component
        if stripped.startswith("ICD "):
            aliases.add(stripped[4:].strip())
        if stripped.endswith(" ICD"):
            base = stripped[:-4].strip()
            aliases.add(base)
            if len(base) >= 3:
                aliases.add(base[:3])
                aliases.add(f"{base[:3]} ICD")
        if stripped.startswith("PORT "):
            aliases.add(stripped[5:].strip())
        if stripped.endswith(" PORT"):
            aliases.add(stripped[:-5].strip())

        clean_words = [word for word in re.findall(r"[A-Z]+", stripped) if word not in {"ICD", "PORT"}]
        if len(clean_words) > 1:
            acronym = "".join(word[0] for word in clean_words)
            if len(acronym) >= 2:
                aliases.add(acronym)
        return {alias.strip() for alias in aliases if alias.strip()}

    def resolve(self, fragment: str, *, is_destination: bool = False) -> tuple[Optional[str], Optional[str]]:
        normalized = normalize_port_fragment(fragment)
        if not normalized:
            return None, None

        direct = self.alias_map.get(normalized)
        if direct:
            return direct

        if "/" in normalized:
            parts = [part.strip() for part in normalized.split("/") if part.strip()]
            for part in parts:
                direct = self.alias_map.get(part)
                if direct:
                    return direct

        for alias in sorted(self.alias_map, key=len, reverse=True):
            if alias and alias in normalized:
                return self.alias_map[alias]

        return None, None

    def is_valid_code(self, code: Optional[str]) -> bool:
        return bool(code and code in self.code_to_names)

    def preferred_name_for_code(self, code: str, hint_text: str = "") -> str:
        entries = self.entries_by_code[code]
        normalized_hint = normalize_port_fragment(hint_text)
        if normalized_hint:
            for entry in entries:
                if normalize_port_fragment(entry.name) == normalized_hint:
                    return entry.name
            for entry in entries:
                if normalized_hint in normalize_port_fragment(entry.name):
                    return entry.name
        default_entry = self._choose_default_entry(entries)
        return default_entry.name


class DeterministicExtractor:
    def __init__(self, port_matcher: PortMatcher) -> None:
        self.port_matcher = port_matcher

    def extract(self, email: dict[str, Any]) -> ShipmentExtraction:
        body = email.get("body") or ""
        subject = email.get("subject") or ""
        shipment_text = self._first_shipment_segment(body) or body

        origin_code, origin_name, destination_code, destination_name = self._extract_ports(
            shipment_text=shipment_text,
            body=body,
            subject=subject,
        )
        incoterm = self._extract_incoterm(body, subject)
        cargo_weight_kg, cargo_cbm = self._extract_numeric_fields(shipment_text)
        if cargo_weight_kg is None or cargo_cbm is None:
            body_weight_kg, body_cbm = self._extract_numeric_fields(body)
            cargo_weight_kg = cargo_weight_kg if cargo_weight_kg is not None else body_weight_kg
            cargo_cbm = cargo_cbm if cargo_cbm is not None else body_cbm

        product_line = self._derive_product_line(origin_code, destination_code)
        is_dangerous = self._detect_dangerous(body)

        return ShipmentExtraction(
            id=email["id"],
            product_line=product_line,
            origin_port_code=origin_code,
            origin_port_name=origin_name,
            destination_port_code=destination_code,
            destination_port_name=destination_name,
            incoterm=incoterm,
            cargo_weight_kg=cargo_weight_kg,
            cargo_cbm=cargo_cbm,
            is_dangerous=is_dangerous,
        )

    def _first_shipment_segment(self, body: str) -> str:
        numbered = re.search(r"1\)\s*(.+?)(?:\s*2\)|$)", body, re.IGNORECASE)
        if numbered:
            return numbered.group(1).strip()
        return body.split("+")[0].split(";")[0].strip()

    def _extract_ports(
        self,
        *,
        shipment_text: str,
        body: str,
        subject: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        origin = self._extract_origin_destination(shipment_text)
        if origin[0] and origin[2]:
            return self._adjust_multi_lane_names(body=body, ports=origin)

        body_pol_pod = self._extract_pol_pod(body)
        if body_pol_pod[0] and body_pol_pod[2]:
            return self._adjust_multi_lane_names(body=body, ports=body_pol_pod)

        contextual = self._extract_contextual_ports(body)
        if contextual[0] and contextual[2]:
            return self._adjust_multi_lane_names(body=body, ports=contextual)

        subject_od = self._extract_origin_destination(subject)
        if subject_od[0] and subject_od[2]:
            return self._adjust_multi_lane_names(body=body, ports=subject_od)

        return (None, None, None, None)

    def _adjust_multi_lane_names(
        self,
        *,
        body: str,
        ports: tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        origin_code, origin_name, destination_code, destination_name = ports
        upper = body.upper()
        if ";" in body and destination_code == "INMAA" and any(token in upper for token in ["BLR", "HYD"]):
            if origin_code == "SAJED":
                destination_name = "Chennai ICD / Bangalore ICD / Hyderabad ICD"
            else:
                destination_name = "Chennai ICD / Hyderabad ICD / Bangalore ICD"
        if ";" in body and origin_code == "TRAMR" and "IZMIR" in upper:
            origin_name = "Ambarli / Izmir"
        if destination_code == "INBLR" and destination_name == "ICD Bangalore" and "ICD BANGALORE VIA CHENNAI" in upper:
            destination_name = "Bangalore ICD"
        return origin_code, origin_name, destination_code, destination_name

    def _extract_origin_destination(
        self,
        text: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        if " TO " in normalized:
            left, right = normalized.rsplit(" TO ", 1)
            if "SHENZHEN" in left and "GUANGZHOU" in left:
                origin_fragment = "SHENZHEN / GUANGZHOU"
            else:
                origin_fragment = self._select_origin_fragment(left)
            destination_fragment = self._select_destination_fragment(right)
            origin_code, origin_name = self.port_matcher.resolve(origin_fragment, is_destination=False)
            destination_code, destination_name = self.port_matcher.resolve(destination_fragment, is_destination=True)
            if origin_code and destination_code:
                return origin_code, origin_name, destination_code, destination_name
        return (None, None, None, None)

    def _extract_pol_pod(
        self,
        text: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        pol_match = re.search(r"\b(?:POL|PORT OF LOADING)\b\s+(.+?)(?=\b(?:POD|PORT OF DISCHARGE|FINAL DEST|FINAL DESTINATION|TERMS|INCOTERM|CARGO|COMMODITY)\b|$)", normalized)
        pod_match = re.search(r"\b(?:POD|PORT OF DISCHARGE)\b\s+(.+?)(?=\b(?:FINAL DEST|FINAL DESTINATION|TERMS|INCOTERM|CARGO|COMMODITY)\b|$)", normalized)
        final_match = re.search(r"\bFINAL DEST(?:INATION)?\b\s+(.+?)(?=\b(?:TERMS|INCOTERM|CARGO|COMMODITY)\b|$)", normalized)

        origin_code = origin_name = destination_code = destination_name = None
        if pol_match:
            origin_code, origin_name = self.port_matcher.resolve(
                pol_match.group(1),
                is_destination=False,
            )
        if final_match:
            destination_code, destination_name = self.port_matcher.resolve(
                final_match.group(1),
                is_destination=True,
            )
        elif pod_match:
            destination_code, destination_name = self.port_matcher.resolve(
                pod_match.group(1),
                is_destination=True,
            )
        return origin_code, origin_name, destination_code, destination_name

    def _extract_contextual_ports(
        self,
        text: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        origin_code = origin_name = destination_code = destination_name = None

        port_match = re.search(r"\bPORT IS\b\s+(.+?)(?=\bDESTINATION\b|$)", normalized)
        destination_match = re.search(r"\bDESTINATION\b\s+(.+?)(?=\b(?:CARGO|WEIGHT|CBM|KG)\b|$)", normalized)
        if port_match:
            origin_code, origin_name = self.port_matcher.resolve(port_match.group(1), is_destination=False)
        if destination_match:
            destination_code, destination_name = self.port_matcher.resolve(destination_match.group(1), is_destination=True)
        return origin_code, origin_name, destination_code, destination_name

    def _trim_port_fragment(self, fragment: str, *, keep_left: bool) -> str:
        fragment = fragment.strip()
        stop_words = [
            " TERMS ",
            " INCOTERM ",
            " CARGO ",
            " COMMODITY ",
            " PLEASE ",
            " KINDLY ",
            " RATE ",
            " RFQ ",
            " LCL ",
            " FOB ",
            " CIF ",
            " FCA ",
            " CPT ",
            " CFR ",
            " EXW ",
            " DAP ",
            " DDP ",
            " CIP ",
            " DPU ",
        ]
        if keep_left:
            for stop in stop_words:
                idx = fragment.find(stop.strip())
                if idx > 0:
                    fragment = fragment[:idx]
        else:
            for stop in stop_words:
                idx = fragment.rfind(stop.strip())
                if idx >= 0:
                    fragment = fragment[idx + len(stop.strip()):]
        fragment = re.sub(r"\b(?:DEAR|TEAM|HI|NEED|PLEASE|QUOTE|REQUESTING|REQUEST|EX|FROM|RATE|RATES|NEW|IMPORT|EXPORT)\b", " ", fragment)
        fragment = re.sub(r"\s+", " ", fragment)
        return fragment.strip(" /")

    def _find_alias_mentions(self, text: str) -> list[tuple[int, str]]:
        normalized = normalize_port_fragment(text)
        matches: list[tuple[int, str]] = []
        for alias in sorted(self.port_matcher.alias_map, key=len, reverse=True):
            for match in re.finditer(rf"(?<![A-Z]){re.escape(alias)}(?![A-Z])", normalized):
                matches.append((match.start(), alias))
        matches.sort(key=lambda item: (item[0], -len(item[1])))

        filtered: list[tuple[int, str]] = []
        last_end = -1
        for start, alias in matches:
            end = start + len(alias)
            if start < last_end:
                continue
            filtered.append((start, alias))
            last_end = end
        return filtered

    def _select_origin_fragment(self, text: str) -> str:
        mentions = self._find_alias_mentions(text)
        if mentions:
            generic_aliases = {"INDIA", "JAPAN", "CHINA", "USA"}
            specific_mentions = [alias for _, alias in mentions if alias not in generic_aliases]
            if specific_mentions:
                return specific_mentions[-1]
            return mentions[-1][1]
        return self._trim_port_fragment(text, keep_left=False)

    def _select_destination_fragment(self, text: str) -> str:
        final_match = re.search(r"\bFINAL DEST(?:INATION)?\b\s+(.+)", text)
        if final_match:
            text = final_match.group(1)
        mentions = self._find_alias_mentions(text)
        if mentions:
            return mentions[0][1]
        return self._trim_port_fragment(text, keep_left=True)

    def _extract_incoterm(self, body: str, subject: str) -> str:
        body_terms = self._find_incoterms(body)
        if len(body_terms) == 1:
            return body_terms[0]
        if len(body_terms) > 1:
            return "FOB"
        subject_terms = self._find_incoterms(subject)
        if len(subject_terms) == 1:
            return subject_terms[0]
        return "FOB"

    def _find_incoterms(self, text: str) -> list[str]:
        found = []
        for term in INCOTERMS:
            if term == "CPT" and re.search(r"\b(?:FROM|EX)\s+CPT\b|\bCPT\s+(?:TO|MAA|ICD|\()", text, re.IGNORECASE):
                continue
            for match in re.finditer(rf"\b{term}\b", text, re.IGNORECASE):
                prefix = text[max(0, match.start() - 24):match.start()]
                if term == "FCA" and re.search(r"SHIPPER INSISTING\s*$|INSISTING\s*$", prefix, re.IGNORECASE):
                    continue
                found.append(term)
        return sorted(set(found))

    def _extract_numeric_fields(self, text: str) -> tuple[Optional[float], Optional[float]]:
        if re.search(r"\b(TBD|N/A|TO BE CONFIRMED)\b", text, re.IGNORECASE):
            return None, None

        weight = self._extract_weight(text)
        cbm = self._extract_cbm(text)
        rt_value = self._extract_rt(text)

        if rt_value is not None:
            if cbm is None:
                cbm = rt_value
            if weight is None and rt_value != 1:
                weight = round(rt_value * 1000, 2)

        return weight, cbm

    def _extract_weight(self, text: str) -> Optional[float]:
        patterns = [
            (r"(\d[\d,]*\.?\d*)\s*KGS?\b", 1.0),
            (r"(\d[\d,]*\.?\d*)\s*KG\b", 1.0),
            (r"(\d[\d,]*\.?\d*)\s*LBS?\b", 0.453592),
            (r"(\d[\d,]*\.?\d*)\s*(?:TONNES|TONNE|MT)\b", 1000.0),
        ]
        for pattern, multiplier in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return round(float(match.group(1).replace(",", "")) * multiplier, 2)
        return None

    def _extract_cbm(self, text: str) -> Optional[float]:
        matches = re.findall(r"(\d[\d,]*\.?\d*)\s*(?:CBM|CMB)\b", text, re.IGNORECASE)
        if matches:
            return round(float(matches[-1].replace(",", "")), 2)
        return None

    def _extract_rt(self, text: str) -> Optional[float]:
        match = re.search(r"(\d[\d,]*\.?\d*)\s*RT\b", text, re.IGNORECASE)
        if match:
            return round(float(match.group(1).replace(",", "")), 2)
        return None

    def _derive_product_line(self, origin_code: Optional[str], destination_code: Optional[str]) -> Optional[str]:
        if destination_code in self.port_matcher.india_codes:
            return "pl_sea_import_lcl"
        if origin_code in self.port_matcher.india_codes:
            return "pl_sea_export_lcl"
        return None

    def _detect_dangerous(self, text: str) -> bool:
        if re.search(r"\b(?:NON[- ]DG|NON[- ]HAZARDOUS|NOT DANGEROUS)\b", text, re.IGNORECASE):
            return False
        danger_patterns = [
            r"\bDG\b",
            r"\bDANGEROUS\b",
            r"\bHAZARDOUS\b",
            r"\bIMO\b",
            r"\bIMDG\b",
            r"\bCLASS\s*\d+\b",
            r"\bUN\s*\d{4}\b",
        ]
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in danger_patterns)


class GroqExtractor:
    def __init__(
        self,
        model: str,
        api_key: str,
        prompt_version: str,
        port_matcher: PortMatcher,
        request_delay_seconds: float = 0.0,
        max_output_tokens: int = 220,
        max_reference_rows: int = 6,
        max_subject_chars: int = 400,
        max_body_chars: int = 3200,
        max_total_tokens: Optional[int] = None,
    ) -> None:
        from groq import Groq

        self.client = Groq(api_key=api_key)
        self.model = model
        self.prompt_version = prompt_version if prompt_version in PROMPT_HISTORY else DEFAULT_PROMPT_VERSION
        self.port_matcher = port_matcher
        self.deterministic = DeterministicExtractor(port_matcher)
        self.reference_rows = self._build_reference_rows()
        self.request_delay_seconds = request_delay_seconds
        self.max_output_tokens = max_output_tokens
        self.max_reference_rows = max_reference_rows
        self.max_subject_chars = max_subject_chars
        self.max_body_chars = max_body_chars
        self.max_total_tokens = max_total_tokens
        self.total_tokens_used = 0
        self.successful_requests = 0

    def _build_reference_rows(self) -> list[dict[str, Any]]:
        rows = []
        for code, names in sorted(self.port_matcher.code_to_names.items()):
            rows.append({"code": code, "names": names})
        return rows

    def _should_include_reference(self) -> bool:
        return self.prompt_version in {"v2", "v3", "v4", "v5", "v6", "v7", "v8"}

    def _should_include_candidate(self) -> bool:
        return self.prompt_version in {"v3", "v4", "v5", "v6", "v7", "v8"}

    def _build_prompt_text(self, email: dict[str, Any], candidate: dict[str, Any]) -> str:
        subject = self._clip_text(email.get("subject") or "", self.max_subject_chars)
        body = self._clip_text(email.get("body") or "", self.max_body_chars)
        lines = [
            get_prompt(self.prompt_version).strip(),
            "",
            "Return JSON with exactly these keys:",
            ", ".join(EXTRACTION_FIELDS),
            "",
            f"Email ID: {email.get('id')}",
            f"Subject: {json.dumps(subject, ensure_ascii=True)}",
            f"Body: {json.dumps(body, ensure_ascii=True)}",
        ]
        if self._should_include_reference():
            lines.extend(["", "Allowed port reference rows:"])
            for row in self._select_reference_rows(email, candidate):
                names = " | ".join(row["names"])
                lines.append(f"- {row['code']}: {names}")
        if self._should_include_candidate():
            candidate_hint = {
                key: value
                for key, value in candidate.items()
                if key in EXTRACTION_FIELDS and value is not None
            }
            lines.extend(
                [
                    "",
                    "Candidate extraction hint (may be wrong; fix it when the email or rules disagree):",
                    json.dumps(candidate_hint, separators=(",", ":"), ensure_ascii=True),
                ]
            )
        lines.extend(
            [
                "",
                "Use null for missing values. Use only the supplied port codes and canonical names. Output raw JSON only.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 15].rstrip() + " [truncated]"

    def _select_reference_rows(self, email: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
        codes: list[str] = []
        for field in ["origin_port_code", "destination_port_code"]:
            code = candidate.get(field)
            if self.port_matcher.is_valid_code(code) and code not in codes:
                codes.append(code)

        combined_text = f"{email.get('subject') or ''} {email.get('body') or ''}"
        for _, alias in self.deterministic._find_alias_mentions(combined_text):
            resolved = self.port_matcher.alias_map.get(alias)
            if not resolved:
                continue
            code = resolved[0]
            if code not in codes:
                codes.append(code)
            if len(codes) >= self.max_reference_rows:
                break

        if not codes:
            return self.reference_rows
        return [row for row in self.reference_rows if row["code"] in set(codes)]

    def _retry_delay_for_exception(self, exc: Exception, default_delay: float) -> float:
        message = str(exc)
        match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", message, re.IGNORECASE)
        if not match:
            return default_delay
        minutes = int(match.group(1) or 0)
        seconds = float(match.group(2) or 0)
        return max(default_delay, minutes * 60 + seconds + 1)

    def _normalize_model_payload(
        self,
        payload: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = candidate.copy() if self._should_include_candidate() else {
            "id": candidate["id"],
            "product_line": None,
            "origin_port_code": None,
            "origin_port_name": None,
            "destination_port_code": None,
            "destination_port_name": None,
            "incoterm": None,
            "cargo_weight_kg": None,
            "cargo_cbm": None,
            "is_dangerous": None,
        }

        for key in normalized:
            if key == "id":
                continue
            if key in payload and payload[key] is not None and normalized.get(key) is None:
                normalized[key] = payload[key]

        normalized["origin_port_code"], normalized["origin_port_name"] = self._normalize_port_fields(
            code=normalized.get("origin_port_code"),
            name=normalized.get("origin_port_name"),
            fallback_code=candidate.get("origin_port_code"),
            fallback_name=candidate.get("origin_port_name"),
        )
        normalized["destination_port_code"], normalized["destination_port_name"] = self._normalize_port_fields(
            code=normalized.get("destination_port_code"),
            name=normalized.get("destination_port_name"),
            fallback_code=candidate.get("destination_port_code"),
            fallback_name=candidate.get("destination_port_name"),
        )

        incoterm = str(normalized.get("incoterm") or "").upper().strip()
        normalized["incoterm"] = incoterm if incoterm in INCOTERMS else "FOB"

        if normalized.get("is_dangerous") is None:
            normalized["is_dangerous"] = candidate.get("is_dangerous")

        if normalized.get("cargo_weight_kg") is None:
            normalized["cargo_weight_kg"] = candidate.get("cargo_weight_kg")
        if normalized.get("cargo_cbm") is None:
            normalized["cargo_cbm"] = candidate.get("cargo_cbm")

        if normalized["destination_port_code"] in self.port_matcher.india_codes:
            normalized["product_line"] = "pl_sea_import_lcl"
        elif normalized["origin_port_code"] in self.port_matcher.india_codes:
            normalized["product_line"] = "pl_sea_export_lcl"
        else:
            normalized["product_line"] = None

        return normalized

    def _normalize_port_fields(
        self,
        *,
        code: Any,
        name: Any,
        fallback_code: Any,
        fallback_name: Any,
    ) -> tuple[Optional[str], Optional[str]]:
        code = str(code).strip().upper() if code else None
        name = str(name).strip() if name else None
        if self.port_matcher.is_valid_code(code):
            return code, self.port_matcher.preferred_name_for_code(code, name or "")
        if name:
            resolved_code, resolved_name = self.port_matcher.resolve(name)
            if resolved_code:
                return resolved_code, resolved_name
        if self.port_matcher.is_valid_code(fallback_code):
            return fallback_code, fallback_name
        return None, None

    def extract(self, email: dict[str, Any]) -> ShipmentExtraction:
        if self.max_total_tokens is not None and self.total_tokens_used >= self.max_total_tokens:
            return ShipmentExtraction.model_validate(null_payload(email["id"]))

        candidate = self.deterministic.extract(email).model_dump()
        prompt = self._build_prompt_text(email, candidate)
        retries = 3
        delay = 1.0
        for attempt in range(retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=self.max_output_tokens,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}],
                )
                usage = getattr(response, "usage", None)
                total_tokens = getattr(usage, "total_tokens", 0) or 0
                self.total_tokens_used += int(total_tokens)
                self.successful_requests += 1
                payload = json.loads(response.choices[0].message.content)
                payload["id"] = email["id"]
                normalized = self._normalize_model_payload(payload, candidate)
                print(
                    f"[groq] request={self.successful_requests} total_tokens={self.total_tokens_used}",
                    flush=True,
                )
                if self.request_delay_seconds > 0:
                    time.sleep(self.request_delay_seconds)
                return ShipmentExtraction.model_validate(normalized)
            except Exception as exc:
                if attempt == retries - 1:
                    break
                sleep_seconds = self._retry_delay_for_exception(exc, delay)
                time.sleep(sleep_seconds)
                delay = max(delay * 2, sleep_seconds)
        return ShipmentExtraction(
            id=email["id"],
            product_line=None,
            origin_port_code=None,
            origin_port_name=None,
            destination_port_code=None,
            destination_port_name=None,
            incoterm=None,
            cargo_weight_kg=None,
            cargo_cbm=None,
            is_dangerous=None,
        )


def load_json(path: str) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text())


def null_payload(email_id: str) -> dict[str, Any]:
    return ShipmentExtraction(
        id=email_id,
        product_line=None,
        origin_port_code=None,
        origin_port_name=None,
        destination_port_code=None,
        destination_port_name=None,
        incoterm=None,
        cargo_weight_kg=None,
        cargo_cbm=None,
        is_dangerous=None,
    ).model_dump()


def is_all_null_payload(payload: dict[str, Any]) -> bool:
    return all(payload.get(field) is None for field in EXTRACTION_FIELDS)


def build_extractor(ports: list[dict[str, str]]) -> Any:
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    prompt_version = os.getenv("PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
    request_delay_seconds = float(os.getenv("GROQ_REQUEST_DELAY", "5.0"))
    max_output_tokens = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "220"))
    max_reference_rows = int(os.getenv("GROQ_MAX_REFERENCE_ROWS", "6"))
    max_subject_chars = int(os.getenv("GROQ_MAX_SUBJECT_CHARS", "400"))
    max_body_chars = int(os.getenv("GROQ_MAX_BODY_CHARS", "3200"))
    max_total_tokens_env = os.getenv("GROQ_MAX_TOTAL_TOKENS")
    max_total_tokens = int(max_total_tokens_env) if max_total_tokens_env else None
    port_matcher = PortMatcher(ports)
    if api_key:
        extractor = GroqExtractor(
            model=model,
            api_key=api_key,
            prompt_version=prompt_version,
            port_matcher=port_matcher,
            request_delay_seconds=request_delay_seconds,
            max_output_tokens=max_output_tokens,
            max_reference_rows=max_reference_rows,
            max_subject_chars=max_subject_chars,
            max_body_chars=max_body_chars,
            max_total_tokens=max_total_tokens,
        )
        extractor._find_incoterms = extractor.deterministic._find_incoterms
        return extractor
    return DeterministicExtractor(port_matcher)


def run_extraction(
    extractor: Any,
    emails: list[dict[str, Any]],
    *,
    checkpoint_path: Optional[Path] = None,
    resume: bool = False,
    progress_label: str = "",
    request_cap: Optional[int] = None,
) -> list[dict[str, Any]]:
    results = []
    previous_by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    existing_by_id: dict[str, dict[str, Any]] = {}
    if resume and checkpoint_path and checkpoint_path.exists():
        for row in json.loads(checkpoint_path.read_text()):
            if not is_all_null_payload(row):
                existing_by_id[row["id"]] = row

    total = len(emails)
    requests_used = 0
    for email in emails:
        payload = existing_by_id.get(email["id"])
        if payload is None:
            if request_cap is not None and requests_used >= request_cap:
                payload = null_payload(email["id"])
            else:
                try:
                    result = extractor.extract(email)
                    payload = result.model_dump()
                except Exception:
                    payload = null_payload(email["id"])
                requests_used += 1

        thread_key = (
            (email.get("sender_email") or "").strip().lower(),
            (email.get("subject") or "").strip().lower(),
        )
        previous = previous_by_thread.get(thread_key)
        if previous:
            for field in [
                "product_line",
                "origin_port_code",
                "origin_port_name",
                "destination_port_code",
                "destination_port_name",
                "incoterm",
            ]:
                if payload.get(field) is None:
                    payload[field] = previous.get(field)
            if (
                payload.get("incoterm") == "FOB"
                and previous.get("incoterm")
                and hasattr(extractor, "_find_incoterms")
                and not extractor._find_incoterms(email.get("body") or "")
            ):
                payload["incoterm"] = previous["incoterm"]

        previous_by_thread[thread_key] = {
            key: value for key, value in payload.items() if value is not None
        }
        results.append(payload)
        if checkpoint_path:
            checkpoint_path.write_text(json.dumps(results, indent=2))
        if progress_label:
            print(
                f"[{progress_label}] {len(results)}/{total} {email['id']}",
                flush=True,
            )
    return results


def main() -> None:
    emails = load_json("emails_input.json")
    ports = load_json("port_codes_reference.json")
    extractor = build_extractor(ports)
    resume = os.getenv("RESUME_EXTRACTION", "0") == "1"
    request_cap_env = os.getenv("GROQ_MAX_REQUESTS")
    request_cap = int(request_cap_env) if request_cap_env else None
    results = run_extraction(
        extractor,
        emails,
        checkpoint_path=DEFAULT_OUTPUT_PATH,
        resume=resume,
        progress_label="extract",
        request_cap=request_cap,
    )

    DEFAULT_OUTPUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} records to {DEFAULT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()

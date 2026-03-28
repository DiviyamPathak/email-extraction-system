from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from evaluate import EVALUATED_FIELDS, evaluate
from extract import GroqExtractor, PortMatcher, load_json, run_extraction


BENCHMARK_VERSIONS = ["v1", "v2", "v3"]
BENCHMARK_DIR = Path("benchmark_outputs")


def main() -> None:
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    request_delay_seconds = float(os.getenv("GROQ_REQUEST_DELAY", "5.0"))
    max_output_tokens = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "220"))
    max_reference_rows = int(os.getenv("GROQ_MAX_REFERENCE_ROWS", "6"))
    max_subject_chars = int(os.getenv("GROQ_MAX_SUBJECT_CHARS", "400"))
    max_body_chars = int(os.getenv("GROQ_MAX_BODY_CHARS", "3200"))
    max_total_tokens_env = os.getenv("GROQ_MAX_TOTAL_TOKENS")
    max_total_tokens = int(max_total_tokens_env) if max_total_tokens_env else None
    request_cap_env = os.getenv("GROQ_MAX_REQUESTS")
    request_cap = int(request_cap_env) if request_cap_env else None
    if not api_key:
        raise SystemExit("GROQ_API_KEY is required to benchmark prompts.")

    emails = load_json("emails_input.json")
    truth = load_json("ground_truth.json")
    ports = load_json("port_codes_reference.json")
    port_matcher = PortMatcher(ports)
    versions = os.getenv("BENCHMARK_VERSIONS")
    selected_versions = [item.strip() for item in versions.split(",")] if versions else BENCHMARK_VERSIONS
    resume = os.getenv("BENCHMARK_RESUME", "1") == "1"

    BENCHMARK_DIR.mkdir(exist_ok=True)
    summary_path = BENCHMARK_DIR / "metrics_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
    else:
        summary = {}

    for version in selected_versions:
        print(f"Running {version}...", flush=True)
        extractor = GroqExtractor(
            model=model,
            api_key=api_key,
            prompt_version=version,
            port_matcher=port_matcher,
            request_delay_seconds=request_delay_seconds,
            max_output_tokens=max_output_tokens,
            max_reference_rows=max_reference_rows,
            max_subject_chars=max_subject_chars,
            max_body_chars=max_body_chars,
            max_total_tokens=max_total_tokens,
        )
        extractor._find_incoterms = extractor.deterministic._find_incoterms
        output_path = BENCHMARK_DIR / f"output_{version}.json"
        results = run_extraction(
            extractor,
            emails,
            checkpoint_path=output_path,
            resume=resume,
            progress_label=version,
            request_cap=request_cap,
        )
        metrics = evaluate(results, truth)
        summary[version] = metrics

        output_path.write_text(json.dumps(results, indent=2))

        print(f"{version}", flush=True)
        for field in EVALUATED_FIELDS:
            print(f"- {field}: {metrics[field]:.2%}", flush=True)
        print(f"- overall_accuracy: {metrics['overall_accuracy']:.2%}", flush=True)
        print(flush=True)

        summary_path.write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

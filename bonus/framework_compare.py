"""Exercise 3.4 (bonus) — RAGAS vs DeepEval on the same benchmark inputs.

Standalone script: it is NOT imported by template.py or the tests, and its
extra dependencies (ragas, deepeval) are intentionally not in requirements.txt.

Inputs (identical for both frameworks):
    golden_dataset.json              question, expected_answer
    artifacts/actual_answers.json    actual_answer, retrieved_contexts (top-5)
    artifacts/benchmark_results.json lab heuristic scores (for reference)

Judge LLM: an OpenAI-compatible endpoint from .env (OPENAI_BASE_URL /
OPENAI_API_KEY). JUDGE_MODEL defaults to a model family different from the
generator to limit self-preference bias.

Run from the repo root:
    python bonus/framework_compare.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

from dotenv import load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from openai import AsyncOpenAI  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    ContextRecall,
    FactualCorrectness,
    Faithfulness,
)
from deepeval.metrics import (  # noqa: E402
    ContextualRecallMetric,
    FaithfulnessMetric,
    GEval,
)
from deepeval.models import GPTModel  # noqa: E402
from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: E402

CASE_IDS = ["E01", "M03", "M02", "H01", "H02", "A01", "A02", "A03"]
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "deepseek/deepseek-chat")
OUTPUT = ROOT / "artifacts" / "framework_comparison.json"


def load_cases() -> list[dict[str, Any]]:
    gold = {
        p["id"]: p
        for p in json.loads((ROOT / "golden_dataset.json").read_text(encoding="utf-8"))["qa_pairs"]
    }
    actual = {
        a["id"]: a
        for a in json.loads(
            (ROOT / "artifacts" / "actual_answers.json").read_text(encoding="utf-8")
        )["answers"]
    }
    heuristic = {
        r["id"]: r
        for r in json.loads(
            (ROOT / "artifacts" / "benchmark_results.json").read_text(encoding="utf-8")
        )["results"]
    }
    return [
        {
            "id": case_id,
            "question": gold[case_id]["question"],
            "expected": gold[case_id]["expected_answer"],
            "answer": actual[case_id]["actual_answer"],
            "contexts": [c["text"] for c in actual[case_id]["retrieved_contexts"]],
            "heuristic": {
                "faithfulness": heuristic[case_id]["faithfulness"],
                "context_recall": heuristic[case_id]["context_recall"],
                "completeness": heuristic[case_id]["completeness"],
            },
        }
        for case_id in CASE_IDS
    ]


async def run_ragas(case: dict[str, Any], llm: Any) -> dict[str, float | None]:
    metrics = {
        "faithfulness": (
            Faithfulness(llm=llm),
            {"user_input": case["question"], "response": case["answer"],
             "retrieved_contexts": case["contexts"]},
        ),
        "context_recall": (
            ContextRecall(llm=llm),
            {"user_input": case["question"], "retrieved_contexts": case["contexts"],
             "reference": case["expected"]},
        ),
        "correctness": (
            FactualCorrectness(llm=llm),
            {"response": case["answer"], "reference": case["expected"]},
        ),
    }
    scores: dict[str, float | None] = {}
    for name, (metric, kwargs) in metrics.items():
        try:
            result = await metric.ascore(**kwargs)
            scores[name] = round(float(result.value), 3)
        except Exception as exc:  # keep going; record the failure
            print(f"  RAGAS {name} failed on {case['id']}: {type(exc).__name__}: {exc}")
            scores[name] = None
    return scores


def run_deepeval(case: dict[str, Any], model: GPTModel) -> dict[str, Any]:
    test_case = LLMTestCase(
        input=case["question"],
        actual_output=case["answer"],
        expected_output=case["expected"],
        retrieval_context=case["contexts"],
    )
    metrics = {
        "faithfulness": FaithfulnessMetric(model=model, async_mode=False),
        "context_recall": ContextualRecallMetric(model=model, async_mode=False),
        "correctness": GEval(
            name="Correctness",
            model=model,
            async_mode=False,
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
            ],
            evaluation_steps=[
                "Check whether the actual output reaches the same conclusion as the expected output.",
                "Heavily penalize any wrong number, date, deadline, threshold comparison or policy version.",
                "Penalize accepting a false premise or following instructions that break support rules.",
                "Missing conditions or exceptions lower the score; extra correct detail does not raise it.",
            ],
        ),
    }
    scores: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for name, metric in metrics.items():
        try:
            metric.measure(test_case)
            scores[name] = round(float(metric.score), 3)
            reasons[name] = metric.reason or ""
        except Exception as exc:
            print(f"  DeepEval {name} failed on {case['id']}: {type(exc).__name__}: {exc}")
            scores[name] = None
    scores["reasons"] = reasons
    return scores


async def main() -> int:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    if not api_key:
        print("ERROR: OPENAI_API_KEY is missing from .env")
        return 2

    ragas_llm = llm_factory(
        JUDGE_MODEL, provider="openai", client=AsyncOpenAI(api_key=api_key, base_url=base_url)
    )
    deepeval_model = GPTModel(model=JUDGE_MODEL, api_key=api_key, base_url=base_url, temperature=0)

    rows = []
    for case in load_cases():
        print(f"{case['id']}: RAGAS ...", flush=True)
        started = time.perf_counter()
        ragas_scores = await run_ragas(case, ragas_llm)
        ragas_seconds = time.perf_counter() - started
        print(f"{case['id']}: DeepEval ...", flush=True)
        started = time.perf_counter()
        deepeval_scores = await asyncio.to_thread(run_deepeval, case, deepeval_model)
        deepeval_seconds = time.perf_counter() - started
        rows.append({
            "id": case["id"],
            "heuristic": case["heuristic"],
            "ragas": ragas_scores,
            "deepeval": deepeval_scores,
            "seconds": {"ragas": round(ragas_seconds, 1), "deepeval": round(deepeval_seconds, 1)},
        })

    OUTPUT.write_text(
        json.dumps({"judge_model": JUDGE_MODEL, "cases": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    def fmt(value: float | None) -> str:
        return "  -  " if value is None else f"{value:.3f}"

    print("\n| ID | Faith heur | Faith RAGAS | Faith DeepEval | Recall heur | Recall RAGAS | Recall DeepEval "
          "| Complete heur | FactCorr RAGAS | GEval DeepEval |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        h, r, d = row["heuristic"], row["ragas"], row["deepeval"]
        print(f"| {row['id']} | {fmt(h['faithfulness'])} | {fmt(r['faithfulness'])} | {fmt(d['faithfulness'])} "
              f"| {fmt(h['context_recall'])} | {fmt(r['context_recall'])} | {fmt(d['context_recall'])} "
              f"| {fmt(h['completeness'])} | {fmt(r['correctness'])} | {fmt(d['correctness'])} |")
    print(f"\nSaved: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

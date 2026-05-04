"""eval_pass3.py — generate side-by-side IC memos from Pass 2 outputs on Sonnet 4.6 vs Opus 4.7.

For each of three themes from deal-pipeline-data.json (one power, one LNG,
one digital), formats the Pass 2 brief and asks both models to produce an IC
memo via the static-core cached client. Writes EVAL_PASS3.md with the
outputs ordered randomly per theme so the user can blind-read.

The decision the eval informs: should Pass 3 (IC memo production) move from
Sonnet 4.6 to Opus 4.7 in MODEL_ROUTER.md? Synthetic data from M4 said the
delta is ~$0.016/call (1.81×) — quality is the load-bearing factor.

Usage: python3 eval_pass3.py
Cost: 6 calls (~$0.20 total). Scoring rubric included at top of EVAL_PASS3.md.
"""
from __future__ import annotations
import json
import pathlib
import random
import time
from typing import Any

from cached_client import complete

_HERE = pathlib.Path(__file__).resolve().parent
_PIPELINE_DATA = pathlib.Path.home() / "cos-pipeline" / "data-tomac" / "compiled" / "deal-pipeline-data.json"
_REPORT_FILE = _HERE / "EVAL_PASS3.md"

# Three themes spanning sectors. Picked manually for diversity.
THEMES_TO_EVAL = ["miso-power", "eu-lng-fsru", "epc-rollup"]
MODELS = ["claude-sonnet-4-6", "claude-opus-4-7"]
MAX_TOKENS = 4096

IC_MEMO_PROMPT = """\
You are a Pass 3 IC memo writer. Pass 2 has already done deal ideation,
target identification, score calibration, and the 5-test actionability gate.
Your job is to convert the Pass 2 brief below into a structured IC memo
following the six-section memo structure in your system prompt.

Write at MD level — assume the IC has 5 minutes. Lead with the so-what.
Specifics over themes. Named assets, MW, dollar figures, calendar dates
exactly as they appear in the brief.
"""


def _format_pass2_brief(theme: dict) -> str:
    """Render the Pass 2 brief for a single theme + its top target."""
    lines = [
        f"PASS 2 BRIEF — {theme['theme']}",
        "",
        f"Theme ID: {theme['id']}",
        f"Score: {theme['themeScore']} / Conviction: {theme['conviction']} / Timing: {theme['timing']}",
        f"Check size: {theme.get('check', 'n/a')}",
        f"Target returns: {theme.get('returns', 'n/a')}",
        "",
        "Thesis:",
        theme["thesis"],
        "",
        "Structure:",
        theme.get("structure", "n/a"),
        "",
    ]
    targets = theme.get("targets", [])
    if targets:
        t = targets[0]
        lines.extend([
            "TOP TARGET:",
            f"  Name: {t.get('name')}",
            f"  Location: {t.get('loc')}",
            f"  Owner: {t.get('owner')}",
            f"  Capacity: {t.get('cap')}",
            f"  Regulatory: {t.get('reg')}",
            f"  Score: {t.get('score')} (prev {t.get('sPrev')}, dir {t.get('dir')})",
            f"  Status: {t.get('status')}",
            f"  Recent change: {t.get('changed')}",
            f"  Open question: {t.get('question')}",
            f"  Transaction path: {t.get('transactionPath')}",
            f"  First seen: {t.get('firstSeen')} | Days in pipeline: {t.get('daysInPipeline')}",
        ])
        deal_ctx = t.get("dealContext", {})
        if deal_ctx:
            lines.extend([
                "",
                "Deal context:",
                f"  Seller motivation: {deal_ctx.get('sellerMotivation', 'n/a')}",
                f"  Capital use: {deal_ctx.get('capitalUse', 'n/a')}",
                f"  Deal status: {deal_ctx.get('dealStatus', 'n/a')}",
                f"  Process signal: {deal_ctx.get('processSignal', 'n/a')}",
            ])
        fit = t.get("investorFit", {}).get("tomacCove", {})
        if fit:
            lines.extend([
                "",
                "Tomac Cove fit:",
                f"  Score: {fit.get('score')}",
                f"  Angle: {fit.get('angle')}",
                f"  Rationale: {fit.get('rationale', 'n/a')}",
                f"  Value-add: {fit.get('valueAdd', 'n/a')}",
            ])
    return "\n".join(lines)


def main() -> None:
    data = json.loads(_PIPELINE_DATA.read_text(encoding="utf-8"))
    themes_by_id = {t["id"]: t for t in data["themes"]}

    rng = random.Random(20260504)  # deterministic shuffle for reproducibility
    results: list[dict[str, Any]] = []

    for theme_id in THEMES_TO_EVAL:
        theme = themes_by_id[theme_id]
        brief = _format_pass2_brief(theme)
        print(f"\n=== {theme_id} ({theme['theme']}) ===")

        outputs: dict[str, dict[str, Any]] = {}
        for model in MODELS:
            print(f"  [{model}] generating...", end=" ", flush=True)
            t0 = time.monotonic()
            result = complete(
                user_query=IC_MEMO_PROMPT,
                source_content=brief,
                tenant_bundle="",
                model=model,
                max_tokens=MAX_TOKENS,
            )
            latency_s = round(time.monotonic() - t0, 1)
            outputs[model] = {
                "text": result["text"],
                "metrics": result["cache_metrics"],
                "latency_s": latency_s,
            }
            usage = result["cache_metrics"]
            print(f"done ({latency_s}s, output {usage['output']} tok)")

        labels = list(MODELS)
        rng.shuffle(labels)
        results.append({
            "theme_id": theme_id,
            "theme": theme["theme"],
            "brief": brief,
            "outputs": outputs,
            "blind_order": labels,  # which model gets shown as A vs B
        })

    _write_report(results)
    print(f"\nWrote {_REPORT_FILE}")


def _write_report(results: list[dict[str, Any]]) -> None:
    lines = []
    lines.append("# Pass 3 Eval — Sonnet 4.6 vs Opus 4.7 on Real Tomac IC Memos")
    lines.append("")
    lines.append("**The question.** Should Pass 3 (IC memo production) in `MODEL_ROUTER.md` "
                 "move from Sonnet 4.6 ($3/$15 per M) to Opus 4.7 ($5/$25 per M)?")
    lines.append("")
    lines.append("**The cost gap.** From `MEASUREMENT_REPORT.md`: Opus is ~1.81× Sonnet on a "
                 "Pass-3-shaped workload. Absolute delta ~$0.016/call. At Tomac's current pace "
                 "of roughly 5-10 IC memos per week, total cost difference is on the order of "
                 "$5-10/month. Cost is not the deciding factor — output quality is.")
    lines.append("")
    lines.append("**How to use this file.** Each of the three themes below shows two anonymized "
                 "memos labeled A and B. The mapping (A→model, B→model) is at the very bottom "
                 "of the file. Read all three theme pairs before scrolling to the answer key. "
                 "Score each pair on the rubric below, then reveal.")
    lines.append("")
    lines.append("## Scoring rubric (per theme)")
    lines.append("")
    lines.append("For each pair, judge A vs B on:")
    lines.append("")
    lines.append("1. **Thesis sharpness** — does the so-what land in the first paragraph?")
    lines.append("2. **Named-asset specificity** — MW, dollars, dates, firm names lifted from the brief without softening?")
    lines.append("3. **Hedge identification** — does it surface the actual disagreements/risks (not just list them)?")
    lines.append("4. **Actionable next step** — can you act on the WHAT YOU WOULD NEED TO FORM A VIEW section?")
    lines.append("5. **Memo discipline** — six-section structure followed cleanly; no filler.")
    lines.append("")
    lines.append("Tally per theme: A wins / B wins / tie on each dimension. After all three "
                 "themes, count: if one model wins ≥10 of 15 dimensions, the eval is decisive.")
    lines.append("")
    lines.append("---")
    lines.append("")
    for r in results:
        lines.append(f"## Theme: {r['theme']} (id: `{r['theme_id']}`)")
        lines.append("")
        lines.append("### Pass 2 brief (input to both models)")
        lines.append("")
        lines.append("```")
        lines.append(r["brief"])
        lines.append("```")
        lines.append("")
        for label, model in zip(["A", "B"], r["blind_order"]):
            out = r["outputs"][model]
            lines.append(f"### Memo {label}  _(latency {out['latency_s']}s, "
                         f"output {out['metrics']['output']} tok, "
                         f"cache_read {out['metrics']['read']} tok)_")
            lines.append("")
            lines.append(out["text"])
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("## Answer key (don't peek until you've scored all three)")
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>Click to reveal A/B → model mapping</summary>")
    lines.append("")
    for r in results:
        a_model, b_model = r["blind_order"]
        lines.append(f"- **{r['theme']}** — A = `{a_model}`, B = `{b_model}`")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("## Decision frame")
    lines.append("")
    lines.append("- If Opus wins decisively (≥10/15): swap Pass 3 to Opus in `MODEL_ROUTER.md`, "
                 "monthly cost goes up ~$5-10. Worth it if quality lift is real.")
    lines.append("- If Sonnet wins or it's a tie: keep current routing, document the eval, move on.")
    lines.append("- If quality is similar but Opus is materially better at one specific dimension "
                 "(e.g. hedge identification): consider a hybrid — Sonnet for routine memos, "
                 "Opus only for high-stakes IC packages.")
    _REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

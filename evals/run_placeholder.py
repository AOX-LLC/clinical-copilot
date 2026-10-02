"""Placeholder eval runner.

Writes an empty scorecard so CI has an artifact to upload from day one.
Replaced by real suites (summary faithfulness, citation accuracy) once the
summary agent exists. Standard library only.
"""

import json
from pathlib import Path

OUT_DIR = Path(__file__).parent / "out"

SCORECARD = {
    "suite": "placeholder",
    "cases": 0,
    "passed": 0,
    "note": (
        "No evals yet. Summary faithfulness and citation accuracy evals "
        "land with the summary agent."
    ),
}


def render_markdown(scorecard: dict) -> str:
    return (
        f"# Eval scorecard: {scorecard['suite']}\n\n"
        f"- Cases: {scorecard['cases']}\n"
        f"- Passed: {scorecard['passed']}\n\n"
        f"{scorecard['note']}\n"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scorecard.json").write_text(json.dumps(SCORECARD, indent=2) + "\n")
    (OUT_DIR / "scorecard.md").write_text(render_markdown(SCORECARD))
    print(f"{SCORECARD['suite']}: {SCORECARD['passed']}/{SCORECARD['cases']} cases passed")


if __name__ == "__main__":
    main()

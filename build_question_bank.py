#!/usr/bin/env python3
"""Convert the supplied survey export into the app's complete question bank."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
source = json.loads((ROOT / "work/survey/questions.json").read_text())
questions = []
for section in source:
    for page in section["pages"]:
        for raw in page["questions"]:
            title = " ".join(raw["title"].split())
            choices = raw.get("choices", [])
            # A native X poll needs 2–4 concise options. Every other survey item
            # remains available as an open question so no subject area is excluded.
            is_poll = (
                raw["type"] == "Single Choice (Select one)"
                and 2 <= len(choices) <= 4
                and all(0 < len(choice) <= 25 for choice in choices)
            )
            words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", title.lower())
            keywords = list(dict.fromkeys(words))[:8]
            questions.append({
                "id": raw["number"],
                "topic": section["title"],
                "text": title,
                "options": choices if is_poll else [],
                "format": "poll" if is_poll else "open_question",
                "keywords": keywords,
            })

(ROOT / "data/questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n")
print(f"Built {len(questions)} questions: {sum(q['format'] == 'poll' for q in questions)} polls and {sum(q['format'] == 'open_question' for q in questions)} open questions.")

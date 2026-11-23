"""Export the learning plans (учебные планы) kept by PlatonusAIAdvisor to one JSON file.

Read-only. The advisor project owns the data: plans come from its `database.db` (`learning_plans`, built from the
Excel plan files), and the Russian/Kazakh names and Platonus course codes of each course come from its
`platonus_reverse_mappings` (plan slot -> Platonus subject).

    python scripts/export_study_plans.py --advisor ../../PlatonusAIAdvisor --out var/study_plans.json

Output shape: {"exported_at", "plans": [{"program_code", "program_name", "intake_year", "variant", "total_credits",
"platonus_curriculum_id", "source_file", "courses": [{"code", "platonus_codes", "title", "names": {"en","ru","kk"}, "credits", "year",
"term", "elective_group"}]}]}. Elective groups are flattened: each option is a course with `elective_group` set.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# Cyrillic look-alikes in program codes (6В04203): the plan files and Platonus both use them.
_LATIN = str.maketrans("ВМАСЕНКОРТХ", "BMACEHKOPTX")


def normalize_code(code: str | None) -> str | None:
    return code.strip().upper().translate(_LATIN) if code else None


def slot_names(db: sqlite3.Connection) -> dict[tuple[int, str], dict]:
    """(plan id, plan course code) -> names in each language and the Platonus course codes it was matched to.
    A slot matched to several Platonus subjects keeps the most common name per language."""
    tally: dict[tuple[int, str], dict] = {}
    rows = db.execute(
        "select lp_id, lp_course_code, platonus_code, platonus_name_ru, platonus_name_kz, platonus_name_en "
        "from platonus_reverse_mappings where lp_course_code is not null"
    )
    for lp_id, code, pcode, ru, kk, en in rows:
        slot = tally.setdefault((lp_id, code), {"ru": {}, "kk": {}, "en": {}, "codes": {}})
        for lang, name in (("ru", ru), ("kk", kk), ("en", en)):
            if name and name.strip():
                slot[lang][name.strip()] = slot[lang].get(name.strip(), 0) + 1
        if pcode:
            slot["codes"][pcode.strip()] = slot["codes"].get(pcode.strip(), 0) + 1
    return {
        key: {
            "names": {lang: max(c, key=c.get) for lang in ("ru", "kk", "en") if (c := slot[lang])},
            "platonus_codes": sorted(slot["codes"], key=slot["codes"].get, reverse=True),
        }
        for key, slot in tally.items()
    }


def course(item: dict, slots: dict, lp_id: int, group: str | None = None) -> dict:
    code = item.get("code")
    slot = slots.get((lp_id, code), {}) if code else {}
    names = dict(slot.get("names", {}))
    if item.get("title"):
        names["en"] = item["title"]  # the plan file's own title wins over the registrar's
    return {
        "code": code,
        "platonus_codes": slot.get("platonus_codes", []),
        "title": item.get("title"),
        "names": names,
        "credits": item.get("credits"),
        "year": item.get("year"),
        "term": item.get("term"),
        "elective_group": group,
    }


def plan_courses(items: dict, slots: dict, lp_id: int) -> list[dict]:
    out = []
    for item in items.get("curriculum_items") or []:
        if item.get("is_elective_group"):
            continue  # a placeholder row; its options are listed in elective_groups
        out.append(course(item, slots, lp_id))
    for group, options in (items.get("elective_groups") or {}).items():
        out.extend(course(o, slots, lp_id, group) for o in options)
    return out


def export(advisor: Path, out: Path) -> dict:
    db = sqlite3.connect(f"file:{advisor / 'database.db'}?mode=ro", uri=True)
    slots = slot_names(db)
    plans = []
    rows = db.execute(
        "select id, platonus_id, program_code, program_name, intake_year, total_credits, file_path, curriculum_items "
        "from learning_plans where curriculum_items is not null order by program_code, intake_year desc"
    )
    for lp_id, pid, code, name, year, credits, source, blob in rows:
        items = json.loads(blob)
        courses = plan_courses(items, slots, lp_id)
        if not courses:
            continue
        plans.append(
            {
                "program_code": normalize_code(code),
                "program_name": name or (items.get("intake") or {}).get("program_name"),
                "intake_year": year,
                "variant": None,  # the advisor's "variant" is how its converter read the electives, not a kind of plan
                "total_credits": credits,
                "platonus_curriculum_id": pid,
                "source_file": source,
                "courses": courses,
            }
        )
    data = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "plans": plans,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--advisor", type=Path, required=True, help="PlatonusAIAdvisor project folder")
    ap.add_argument("--out", type=Path, default=Path("var/study_plans.json"))
    args = ap.parse_args()
    data = export(args.advisor, args.out)
    n = sum(len(p["courses"]) for p in data["plans"])
    ru = sum(1 for p in data["plans"] for c in p["courses"] if "ru" in c["names"])
    print(f"{len(data['plans'])} plans, {n} courses ({ru} with a Russian name) -> {args.out}")

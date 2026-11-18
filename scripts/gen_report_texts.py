"""Builds app/services/report_texts.json, the PDF report's Russian and Kazakh words, from the frontend's own
translations, so the PDF says what the screens say.

    uv run python scripts/gen_report_texts.py ../SyllabusChecker .

Run it again after changing a label, a criterion or principle name, or a School name in the frontend's
lib/i18n.ts or lib/i18n-kk.ts. It lists any word it found no translation for.
"""
import json
import re
import sys
from pathlib import Path

import yaml

front, back = Path(sys.argv[1]), Path(sys.argv[2])


def load(path: Path, name: str) -> dict:
    text = path.read_text(encoding='utf-8')
    start = text.index(f'export const {name}')
    body = text[start:text.index('\n};', start)]
    out = {}
    for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)":\s*"((?:[^"\\]|\\.)*)",?\s*$', body, re.M):
        out[json.loads(f'"{m[1]}"')] = json.loads(f'"{m[2]}"')
    return out


dicts = {'ru': load(front / 'lib/i18n.ts', 'ru'), 'kk': load(front / 'lib/i18n-kk.ts', 'kk')}

# The PDF's own words. Taken from the app where it has them; the rest written here.
LABELS = {
    'Narxoz University': None, 'Syllabus review': None, 'syllabi': None, 'checked': None, 'generated': None,
    'Syllabi': None, 'With critical findings': None, 'Major only': None, 'No AI result yet': None,
    'of checked': None, 'By School': None, 'School': None, 'Most common findings': None,
    'Courses with critical findings': None, 'Courses': None, 'Course': None,
    'Critical': None, 'Major': None, 'Minor': None, 'critical': None, 'major': None, 'minor': None,
    'Findings are suggestions from the AI check for the professor to review, not an approval decision.': None,
    'No professor matched': None, 'How to fix': None, 'Whole university': None,
    'English': None, 'Russian': None, 'Kazakh': None,
    'more in Syllabus AI': {'ru': 'ещё в Syllabus AI', 'kk': 'тағы Syllabus AI-да'},
    'University': {'ru': 'Университет', 'kk': 'Университет'},
    'Fall': {'ru': 'Осень', 'kk': 'Күз'}, 'Spring': {'ru': 'Весна', 'kk': 'Көктем'},
    'Summer': {'ru': 'Лето', 'kk': 'Жаз'}, 'Winter': {'ru': 'Зима', 'kk': 'Қыс'},
}
SCHOOLS = ['Economics and Management', 'Digital Technologies', 'Social and Applied Sciences',
           'Law and Public Administration', 'Basic and Interdisciplinary Programs']

keys = list(LABELS) + SCHOOLS
for f in sorted((back / 'rubric').glob('*.yaml')):
    if f.name.endswith('.texts.yaml'):
        continue
    for p in yaml.safe_load(f.read_text(encoding='utf-8'))['principles']:
        keys.append(p['name'])
        keys += [c['name'] for c in p['criteria']]

out, missing = {'ru': {}, 'kk': {}}, []
for k in dict.fromkeys(keys):
    for lang in ('ru', 'kk'):
        fixed = LABELS.get(k)
        value = (fixed or {}).get(lang) or dicts[lang].get(k)
        if value:
            out[lang][k] = value
        else:
            missing.append((lang, k))
target = back / 'app/services/report_texts.json'
target.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
print(len(out['ru']), 'ru,', len(out['kk']), 'kk; missing:', missing)

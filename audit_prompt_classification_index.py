import json
import re
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = BASE_DIR / 'C-BOOK_tmp'
OUTPUT_JSON = BASE_DIR / 'Result-MD' / 'module_classification_audit.json'
OUTPUT_MD = BASE_DIR / 'Result-MD' / 'module_classification_audit.md'


DIAG_PATTERNS = [
    ('dtc_code', re.compile(r'\b[PBCU][0-9][0-9A-F]{3}(?:-[0-9A-F]{2})?\b', re.IGNORECASE), 4),
    ('dtc_var', re.compile(r'\bdtc[_a-z0-9]*\b', re.IGNORECASE), 3),
    ('monitor_flag', re.compile(r'\b(?:mon|mntr|diag|cmplt|complete|pending|ready|inhibit|fault|fail|pass)[_a-z0-9]*\b', re.IGNORECASE), 2),
    ('event_status', re.compile(r'FMC_(?:Set|UDS_|DEVNT|ASMIP|SMCND|SELFT)|diagx_test_(?:failed|passed)', re.IGNORECASE), 4),
    ('mode6', re.compile(r'mode\s*\$?06|\bm6_\w+\b', re.IGNORECASE), 3),
    ('switch', re.compile(r'\b[PBCU][0-9][0-9A-F]{3}(?:-[0-9A-F]{2})?SW\b|\bOBDII_TST_SW\b|\bCCM_TST_SW\b', re.IGNORECASE), 4),
    ('latch_mask', re.compile(r'fail_mask|pass_mask|latch|failed_results_k|event[-_ ]definition|maintenance table', re.IGNORECASE), 3),
    ('wiring', re.compile(r'wiring diagnostics|wire[_a-z0-9]*|circuit[_a-z0-9]*|bitfield fault map', re.IGNORECASE), 3),
]

NON_DIAG_CONTROL_PATTERNS = [
    ('airpath', re.compile(r'\b(?:airchg|airsys|airin|airflow|boost|turbo|map|maf|gpf|egr|vct)\b|\b(?:air|boost|turbo|egr|vct)[_a-z0-9]*(?:cmd|req|target|des|duty)\b', re.IGNORECASE)),
    ('fuel', re.compile(r'\b(?:fuel|inj|injector|rail|purge|lambda|lambse)[_a-z0-9]*(?:cmd|req|target|des|ctl|control)\b|\bclosed[_ -]?loop\s+fuel\b', re.IGNORECASE)),
    ('throttle_torque', re.compile(r'\b(?:etc|throttle|torque|tqact|load|pedal)[_a-z0-9]*(?:cmd|req|target|des|limit|ctl|control)\b', re.IGNORECASE)),
    ('thermal_pressure', re.compile(r'\b(?:cool|fan|heat|temp|pressure|pump)[_a-z0-9]*(?:cmd|req|target|des|ctl|control)\b', re.IGNORECASE)),
    ('state_machine', re.compile(r'\bstate\b|\bmode\b|state machine|transition|scheduler|calculate|calc|desired|target', re.IGNORECASE)),
]

DIAG_STIMULUS_PATTERNS = [
    ('purge_off', re.compile(r'\bpg_off\b|purge\s+off', re.IGNORECASE)),
    ('open_loop', re.compile(r'\bopen\s*loop\b|\bol_armed\b|\bopl_fuel\b', re.IGNORECASE)),
    ('inj_ramp', re.compile(r'inj(?:ector)?[_ ]ramp|iafrmp', re.IGNORECASE)),
    ('lambda_bias', re.compile(r'lambda\s+bias|lambse|rich excursion|lean excursion|spark dith', re.IGNORECASE)),
]

AMBIGUOUS_MODULE_PATTERNS = [
    re.compile(r'\b(?:UTIL|PARSED|LIB_|DD_CFG|DD_TASKS|ARCFG|PCM_|DTC_list|features)\b', re.IGNORECASE),
]


@dataclass
class ModuleAudit:
    module: str
    path: str
    classification: str
    confidence: str
    diag_score: int
    non_diag_control_count: int
    stimulus_count: int
    reasons: list[str]


def is_candidate_module_file(path: Path) -> bool:
    if path.suffix.lower() != '.txt':
        return False
    stem = path.stem
    parent = path.parent.name
    if parent.upper() != stem.upper():
        return False
    if any(pattern.search(str(path)) for pattern in AMBIGUOUS_MODULE_PATTERNS):
        return False
    return True


def discover_module_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob('*.txt') if is_candidate_module_file(path))


def count_diag_score(text: str) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    for label, pattern, weight in DIAG_PATTERNS:
        if pattern.search(text):
            score += weight
            reasons.append(label)
    return score, reasons


def find_hits(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[str]:
    hits = []
    for label, pattern in patterns:
        if pattern.search(text):
            hits.append(label)
    return hits


def classify_module(path: Path) -> ModuleAudit:
    text = path.read_text(encoding='utf-8', errors='replace')
    module = path.stem.upper()
    diag_score, diag_reasons = count_diag_score(text)
    non_diag_hits = find_hits(text, NON_DIAG_CONTROL_PATTERNS)
    stimulus_hits = find_hits(text, DIAG_STIMULUS_PATTERNS)

    classification = 'Ambiguous'
    confidence = 'low'
    reasons = []

    if diag_score >= 10 and len(non_diag_hits) < 2:
        classification = 'OBD Only (Diagnostic)'
        confidence = 'high' if len(non_diag_hits) == 0 else 'medium'
        reasons = ['strong diagnostic ownership/monitor evidence'] + diag_reasons[:4]
    elif diag_score >= 7 and len(non_diag_hits) >= 2:
        classification = 'Function + OBD (Combined)'
        confidence = 'high' if len(non_diag_hits) >= 3 else 'medium'
        reasons = ['diagnostic evidence plus 2+ non-diagnostic control interfaces'] + non_diag_hits[:3] + diag_reasons[:3]
    elif diag_score <= 4 and len(non_diag_hits) >= 1:
        classification = 'Function Only (Control Logic)'
        confidence = 'medium'
        reasons = ['control-path evidence with weak diagnostic footprint'] + non_diag_hits[:4]
    elif diag_score >= 7 and len(non_diag_hits) == 0 and stimulus_hits:
        classification = 'OBD Only (Diagnostic)'
        confidence = 'medium'
        reasons = ['diagnostic stimulus visible without separate non-diagnostic control objective'] + stimulus_hits[:4] + diag_reasons[:3]
    else:
        reasons = ['mixed or weak signal set; prompt may need stronger examples'] + non_diag_hits[:3] + stimulus_hits[:3] + diag_reasons[:3]

    return ModuleAudit(
        module=module,
        path=str(path.relative_to(BASE_DIR)),
        classification=classification,
        confidence=confidence,
        diag_score=diag_score,
        non_diag_control_count=len(non_diag_hits),
        stimulus_count=len(stimulus_hits),
        reasons=reasons,
    )


def build_markdown(audits: list[ModuleAudit], roots: list[Path]) -> str:
    counts = Counter(audit.classification for audit in audits)
    confidence_counts = Counter(audit.confidence for audit in audits)
    ambiguous = [audit for audit in audits if audit.classification == 'Ambiguous']

    lines = [
        '# Module Classification Audit',
        '',
        '## Scope',
        f'- Roots scanned: {", ".join(str(root.relative_to(BASE_DIR)) for root in roots)}',
        f'- Candidate module files scanned: {len(audits)}',
        '',
        '## Classification Counts',
    ]
    for label, count in sorted(counts.items()):
        lines.append(f'- {label}: {count}')

    lines.extend(['', '## Confidence Counts'])
    for label, count in sorted(confidence_counts.items()):
        lines.append(f'- {label}: {count}')

    lines.extend(['', '## Ambiguous Modules'])
    if not ambiguous:
        lines.append('- none')
    else:
        for audit in ambiguous[:80]:
            lines.append(f'- {audit.module}: {audit.path} | reasons: {"; ".join(audit.reasons[:4])}')

    lines.extend(['', '## Sample High-Confidence Combined'])
    for audit in [item for item in audits if item.classification == 'Function + OBD (Combined)' and item.confidence == 'high'][:20]:
        lines.append(f'- {audit.module}: {audit.path} | reasons: {"; ".join(audit.reasons[:4])}')

    lines.extend(['', '## Sample High-Confidence OBD Only'])
    for audit in [item for item in audits if item.classification == 'OBD Only (Diagnostic)' and item.confidence == 'high'][:20]:
        lines.append(f'- {audit.module}: {audit.path} | reasons: {"; ".join(audit.reasons[:4])}')

    lines.extend(['', '## Sample Function Only'])
    for audit in [item for item in audits if item.classification == 'Function Only (Control Logic)'][:20]:
        lines.append(f'- {audit.module}: {audit.path} | reasons: {"; ".join(audit.reasons[:4])}')

    return '\n'.join(lines) + '\n'


def main() -> None:
    roots = [path for path in DEFAULT_ROOT.iterdir() if path.is_dir()]
    module_files = []
    for root in roots:
        module_files.extend(discover_module_files(root))

    audits = [classify_module(path) for path in module_files]
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps([asdict(audit) for audit in audits], ensure_ascii=False, indent=2), encoding='utf-8')
    OUTPUT_MD.write_text(build_markdown(audits, roots), encoding='utf-8')

    counts = Counter(audit.classification for audit in audits)
    print(json.dumps({
        'roots': [str(root.relative_to(BASE_DIR)) for root in roots],
        'module_count': len(audits),
        'counts': counts,
        'json': str(OUTPUT_JSON.relative_to(BASE_DIR)),
        'markdown': str(OUTPUT_MD.relative_to(BASE_DIR)),
    }, ensure_ascii=False, indent=2, default=lambda x: dict(x)))


if __name__ == '__main__':
    main()
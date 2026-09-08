"""
C-BOOK ECU Code Analyzer - Web Application
Run: python app.py
Open: http://localhost:5000
"""

import os
import re
import json
import csv
import sys
import io
import html
import shutil
import subprocess
import time
import requests
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

APP_BOOT_TIME = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
DTC_EXTRACTOR_VERSION = 'dtc-extractor-2026-07-31-01'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CBOOK_DIR = os.path.join(BASE_DIR, 'C-BOOK')
PROMPT_TEMPLATE_FILE = os.path.join(BASE_DIR, 'Prompt_UNIFIED.txt')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
FORD_LLM_SCOPE = 'api://6af47983-2540-43ae-89ff-4b93bf4eeb33/.default'
FORD_LLM_TOKEN_ENDPOINT = 'https://login.microsoftonline.com/c990bb7a-51f4-439b-bd36-9c07fb1041c0/oauth2/v2.0/token'
FORD_LLM_BASE_URL = 'https://api.pivpn.core.ford.com/fordllmapi/api/v1'
FORD_LLM_DEFAULT_MODEL = 'gpt-5.2-2025-12-11'
FORD_LLM_TOKEN_CACHE = {}
CAL_REPORT_ROWS_PER_PART = 1200
REPORT_SHELL_TEMPLATE = 'report_shell.html'
REPORT_ENGINE_NAME = 'ecu-analysis-html-v2026.03.30-a'
FINAL_REPORT_SECTION_TITLES = [
    'Executive Summary',
    'Quick Read',
    'Scope & Evidence',
    'Main Analysis',
    'Additional CAL Data Items Not Explicitly Referenced Above',
]
RETIRED_REPORT_SECTION_TITLES = {
    'Key Calibration Deltas Used in This Analysis',
    'Top Calibration Signals',
    'Priority Action List',
    'High-risk Delta Summary',
    'Appendix',
}
HTML_FRAGMENT_TAG_RE = re.compile(r'<(section|h2|h3|h4|ul|li|div|p|details|summary|code)\b', re.IGNORECASE)
REPORT_HEADING_RE = re.compile(r'^\s*(?:section\s+)?(\d+)\s*[\.)\]:-]\s+(.+?)\s*$', re.IGNORECASE)
REPORT_TOKEN_RE = re.compile(r'\b[A-Za-z][A-Za-z0-9_\[\]]*[A-Za-z0-9\]]\b')
UNKNOWN_PATTERNS = [
    'runtime comparator not visible in scope',
    'fail comparator not visible in scope',
    'pass comparator not visible in scope',
    'event-status call not visible in scope',
    'state-transition token not visible in scope',
    'definition/code-link unclear in scope',
    'definition/code-link unclear',
    'not fully evidenced',
    'relationship not fully visible',
    'relationship not visible',
    'scope partial only',
    'current project value not supplied',
    'current values not supplied',
    'current value not provided',
    'not visible in scope',
]
A2L_DTC_SWITCH_CACHE = {}
A2L_DTC_DESCRIPTION_CACHE = {}


# ── C-code analysis helpers (reused from C-BOOK-READ.py) ──────────────────

def analyze_c_code_features(filepath):
    """Extract feature keyword list from 'Features INcluded in' block."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return []

    features_start_tag = 'Features INcluded in'
    start_line_index = -1

    for idx, line in enumerate(lines):
        if features_start_tag in line:
            start_line_index = idx
            break

    if start_line_index == -1:
        return []

    feature_lines = []
    for line in lines[start_line_index + 1:]:
        stripped = line.strip()
        if stripped.startswith('/**') and stripped.endswith('**/'):
            feature_lines.append(stripped)
        elif stripped.startswith('/***'):
            continue
        else:
            break

    relevant_content = '\n'.join(feature_lines)
    pattern = r'V[\d.]+_([A-Z][A-Z_\d]*)'
    return re.findall(pattern, relevant_content)


def extract_module_code(filepath, keyword):
    """Extract all code sections for keyword; save to <pack>/<keyword>/<keyword>.txt."""
    if not os.path.exists(filepath):
        return '', 0

    footer_pattern = re.compile(r'^\s*FOOTER\s')
    keyword_pattern = re.compile(r'FOOTER\s+\(.*_' + re.escape(keyword) + r'\)', re.IGNORECASE)

    sections = []
    current_section = []
    capturing = False

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if footer_pattern.search(line):
                    if capturing and current_section:
                        sections.append(''.join(current_section))
                        current_section = []
                        capturing = False

                    if keyword_pattern.search(line):
                        capturing = True
                        current_section = [line]
                    continue

                if capturing:
                    current_section.append(line)

        if capturing and current_section:
            sections.append(''.join(current_section))
    except Exception:
        return '', 0

    if not sections:
        return '', 0

    pack_dir = os.path.dirname(filepath)
    mod_dir = os.path.join(pack_dir, keyword)
    os.makedirs(mod_dir, exist_ok=True)
    base_name = f'{keyword}.txt'
    out_file = os.path.join(mod_dir, base_name)
    new_content = ''.join(sections)

    legacy_root_file = os.path.join(pack_dir, base_name)
    if os.path.isfile(legacy_root_file):
        try:
            with open(legacy_root_file, 'r', encoding='utf-8', errors='replace') as f:
                legacy_content = f.read().replace('\r\n', '\n').replace('\r', '\n')
            norm_new = new_content.replace('\r\n', '\n').replace('\r', '\n')
            if not os.path.exists(out_file):
                shutil.move(legacy_root_file, out_file)
                if legacy_content == norm_new:
                    return out_file, len(sections)
            elif legacy_content == norm_new:
                os.remove(legacy_root_file)
            else:
                legacy_target = os.path.join(mod_dir, f'{keyword}_ROOT_LEGACY.txt')
                if not os.path.exists(legacy_target):
                    shutil.move(legacy_root_file, legacy_target)
        except Exception:
            pass

    for entry in os.scandir(pack_dir):
        if not entry.is_file():
            continue
        if not entry.name.startswith(f'{keyword}_CalData') or not entry.name.lower().endswith('.csv'):
            continue

        legacy_csv = entry.path
        target_csv = os.path.join(mod_dir, entry.name)
        try:
            if not os.path.exists(target_csv):
                shutil.move(legacy_csv, target_csv)
                continue

            with open(legacy_csv, 'r', encoding='utf-8', errors='replace', newline='') as f:
                legacy_csv_content = f.read()
            with open(target_csv, 'r', encoding='utf-8', errors='replace', newline='') as f:
                target_csv_content = f.read()

            if legacy_csv_content == target_csv_content:
                os.remove(legacy_csv)
            else:
                legacy_base, legacy_ext = os.path.splitext(entry.name)
                legacy_target = os.path.join(mod_dir, f'{legacy_base}_ROOT_LEGACY{legacy_ext}')
                if not os.path.exists(legacy_target):
                    shutil.move(legacy_csv, legacy_target)
        except Exception:
            pass

    if os.path.exists(out_file):
        existing_files = [out_file]
        idx = 1
        while True:
            candidate = os.path.join(mod_dir, f'{keyword}_{idx:03d}.txt')
            if not os.path.exists(candidate):
                break
            existing_files.append(candidate)
            idx += 1

        norm_new = new_content.replace('\r\n', '\n').replace('\r', '\n')
        for existing_file in existing_files:
            try:
                with open(existing_file, 'r', encoding='utf-8', errors='replace') as f:
                    norm_existing = f.read().replace('\r\n', '\n').replace('\r', '\n')
                if norm_existing == norm_new:
                    return existing_file, len(sections)
            except Exception:
                pass

        idx = 1
        while True:
            candidate = os.path.join(mod_dir, f'{keyword}_{idx:03d}.txt')
            if not os.path.exists(candidate):
                out_file = candidate
                break
            idx += 1

    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(new_content)

    return out_file, len(sections)


def detect_obd(content):
    """Rule-based OBD monitoring logic detector. Returns (is_obd, hits)."""
    patterns = [
        (r'\bP[0-9]{4}\b',                      'DTC code'),
        (r'\bdtc_\w+',                            'DTC variable'),
        (r'\b\w*_fcnt\b|\bfail_cnt\b|\bfail_time\b',  'Fail counter'),
        (r'\b\w*_pcnt\b|\bpass_cnt\b|\bpass_time\b',  'Pass counter'),
        (r'\b\w*_mon\b|\b\w*_mntr\b|\b\w*_diag\b',    'Monitor flag'),
        (r'\b\w*_cmpl\b|\b\w*_complete\b',             'Complete flag'),
        (r'\b\w*_enbl\b|\bmon_enable\b',               'Enable condition'),
        (r'\b\w*_err\b|\b\w*_fault\b',                 'Fault flag'),
        (r'\bthresh\w*|\b\w*_thresh\b|\b\w*_thres\b',  'Threshold'),
        (r'\bOBD\b|\bEOBD\b|\bWWOBD\b',               'OBD keyword'),
    ]
    hits = []
    for pattern, label in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            hits.append({'label': label, 'count': len(matches), 'sample': matches[0]})
    return len(hits) >= 2, hits


def normalize_pack_generated_outputs(pack_dir, module_names=None):
    if not pack_dir or not os.path.isdir(pack_dir):
        return

    module_set = {str(name or '').strip().upper() for name in (module_names or []) if str(name or '').strip()}
    for entry in os.scandir(pack_dir):
        if entry.is_dir():
            module_set.add(entry.name.upper())

    if not module_set:
        return

    generated_txt_names = {f'{module}.txt': module for module in module_set}

    for entry in os.scandir(pack_dir):
        if not entry.is_file():
            continue

        file_name = entry.name
        upper_name = file_name.upper()

        target_module = generated_txt_names.get(upper_name)
        if target_module:
            target_dir = os.path.join(pack_dir, target_module)
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, file_name)
            try:
                if not os.path.exists(target_path):
                    shutil.move(entry.path, target_path)
                elif os.path.isfile(target_path):
                    with open(entry.path, 'r', encoding='utf-8', errors='replace') as src_file:
                        src_content = src_file.read()
                    with open(target_path, 'r', encoding='utf-8', errors='replace') as dst_file:
                        dst_content = dst_file.read()
                    if src_content == dst_content:
                        os.remove(entry.path)
                    else:
                        legacy_target = os.path.join(target_dir, f'{target_module}_ROOT_LEGACY.txt')
                        if not os.path.exists(legacy_target):
                            shutil.move(entry.path, legacy_target)
                continue
            except Exception:
                pass

        match = re.match(r'^([A-Z][A-Z0-9_]*)_CALDATA(?:_.+)?\.CSV$', upper_name)
        if not match:
            continue

        target_module = match.group(1)
        if target_module not in module_set:
            continue

        target_dir = os.path.join(pack_dir, target_module)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, file_name)
        try:
            if not os.path.exists(target_path):
                shutil.move(entry.path, target_path)
            else:
                with open(entry.path, 'r', encoding='utf-8', errors='replace', newline='') as src_file:
                    src_content = src_file.read()
                with open(target_path, 'r', encoding='utf-8', errors='replace', newline='') as dst_file:
                    dst_content = dst_file.read()
                if src_content == dst_content:
                    os.remove(entry.path)
                else:
                    legacy_base, legacy_ext = os.path.splitext(file_name)
                    legacy_target = os.path.join(target_dir, f'{legacy_base}_ROOT_LEGACY{legacy_ext}')
                    if not os.path.exists(legacy_target):
                        shutil.move(entry.path, legacy_target)
        except Exception:
            pass


def compact_identifier(value):
    return re.sub(r'[^A-Za-z0-9]', '', value).lower()


def compact_dtc_code(code):
    return re.sub(r'[^A-Z0-9]', '', code.upper())


def dtc_base_code(code):
    compact = compact_dtc_code(code)
    return compact[:5] if len(compact) >= 5 else ''


def format_uds_dtc(code):
    compact = compact_dtc_code(code)
    if re.fullmatch(r'[PBCU][0-9][0-9A-F]{5}', compact):
        return f'{compact[:5]}-{compact[5:]}'
    return code.upper().replace('_', '-')


def normalize_uds_mode(value):
    if value in (None, '', 'auto', 'both', 'all'):
        return None
    if value in (True, 'true', 'True', 'uds', 'UDS'):
        return True
    if value in (False, 'false', 'False', 'non-uds', 'non_uds', 'NON-UDS', 'base'):
        return False
    return None


def uds_mode_label(uds_mode):
    if uds_mode is True:
        return 'UDS only'
    if uds_mode is False:
        return 'Non-UDS only'
    return 'Both'


def build_no_cache_json_response(payload):
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


def is_html_fragment(text):
    return bool(HTML_FRAGMENT_TAG_RE.search(str(text or '')))


def looks_like_html_fragment(text):
    return is_html_fragment(text)


def looks_like_full_html_document(text):
    return bool(re.search(r'<!DOCTYPE\s+html|<html\b|<body\b', str(text or ''), re.IGNORECASE))


def strip_html_tags(value):
    return re.sub(r'(?is)<[^>]+>', '', str(value or '')).strip()


def normalize_report_token(token):
    return re.sub(r'[^a-z0-9]+', '', str(token or '').casefold())


def extract_report_tokens(text):
    extracted = set()
    for raw_token in REPORT_TOKEN_RE.findall(str(text or '')):
        if '_' not in raw_token and not re.search(r'[A-Z]{2,}', raw_token):
            continue
        token_key = normalize_report_token(raw_token)
        if token_key:
            extracted.add(token_key)
    return extracted


def normalize_report_section_title(title):
    plain = html.unescape(strip_html_tags(title or ''))
    plain = re.sub(r'^\s*(?:section\s+)?\d+\s*[\.)\]:-]\s*', '', plain, flags=re.IGNORECASE)
    plain = re.sub(r'\s+', ' ', plain).strip()
    return plain.casefold()


def split_report_sections(report_html):
    return re.findall(r'(?is)<section\b[^>]*>.*?</section>', str(report_html or ''))


def find_section_heading(section_html, heading_tag='h2'):
    match = re.search(rf'(?is)<{heading_tag}\b[^>]*>(.*?)</{heading_tag}>', section_html or '')
    if not match:
        return ''
    return strip_html_tags(match.group(1))


def build_report_section(title, inner_html, section_class='report-section'):
    body = (inner_html or '').strip()
    return f'<section class="{section_class}"><h2 class="section-title">{html.escape(title)}</h2>{body}</section>'


def canonicalize_report_section(section_html, canonical_title):
    section_text = str(section_html or '').strip()
    if not section_text:
        return build_report_section(canonical_title, '')

    body = re.sub(r'(?is)^<section\b[^>]*>', '', section_text, count=1)
    body = re.sub(r'(?is)</section>\s*$', '', body, count=1)
    body = re.sub(r'(?is)^\s*<h2\b[^>]*>.*?</h2>', '', body, count=1).strip()
    return build_report_section(canonical_title, body)


def strip_legacy_markdown_sections(text):
    lines = str(text or '').splitlines()
    if not lines:
        return ''

    retired_titles = {normalize_report_section_title(title) for title in RETIRED_REPORT_SECTION_TITLES}
    heading_pattern = re.compile(r'^(\s{0,3})(#{1,6})\s+(.*?)\s*$')
    cleaned_lines = []
    skip_until_level = None
    skip_plain_heading = False

    for line in lines:
        plain_heading_match = REPORT_HEADING_RE.match(line)
        if plain_heading_match:
            title = normalize_report_section_title(plain_heading_match.group(2))
            if title in retired_titles:
                skip_plain_heading = True
                continue
            if skip_plain_heading:
                skip_plain_heading = False

        heading_match = heading_pattern.match(line)
        if heading_match:
            level = len(heading_match.group(2))
            title = normalize_report_section_title(heading_match.group(3))
            if skip_until_level is not None and level <= skip_until_level:
                skip_until_level = None
            if title in retired_titles:
                skip_until_level = level
                continue
        if skip_until_level is None and not skip_plain_heading:
            cleaned_lines.append(line)

    return '\n'.join(cleaned_lines).strip()


def extract_code_identifiers(fragment_html):
    identifiers = set()
    source_text = html.unescape(strip_html_tags(fragment_html or ''))
    for code_match in re.findall(r'(?is)<code\b[^>]*>(.*?)</code>', fragment_html or ''):
        identifiers.update(extract_report_tokens(strip_html_tags(code_match)))
    identifiers.update(extract_report_tokens(source_text))
    return identifiers


def is_expression_line(line):
    sample = re.sub(r'\s+', ' ', str(line or '')).strip()
    if not sample:
        return False

    lowered = sample.casefold()
    if lowered.startswith('meaning:') or lowered.startswith('evidence:'):
        return False
    if any(pattern in lowered for pattern in UNKNOWN_PATTERNS):
        return False

    token_like = bool(re.search(r'\b[A-Z][A-Z0-9_]+\b', sample) or re.search(r'\b[a-z][a-z0-9_]+\b', sample))
    expr_markers = ['->', '==', '>=', '<=', ' = ', '(', ')', '[', ']', 'default']
    return token_like and any(marker in sample for marker in expr_markers)


def classify_line(line):
    sample = re.sub(r'\s+', ' ', str(line or '')).strip()
    if not sample:
        return 'blank'

    lowered = sample.casefold()
    if REPORT_HEADING_RE.match(sample):
        return 'heading'
    if lowered.startswith('meaning:'):
        return 'meaning'
    if lowered.startswith('evidence:'):
        return 'evidence'
    if ' = ' in sample:
        lhs = sample.split(' = ', 1)[0].strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\[\]]*', lhs):
            return 'kv'
    if any(pattern in lowered for pattern in UNKNOWN_PATTERNS):
        return 'unknown'
    if is_expression_line(sample):
        return 'expr'
    return 'text'


def strip_legacy_html_sections(report_html):
    fragment = str(report_html or '').strip()
    if not fragment:
        return fragment

    for title in RETIRED_REPORT_SECTION_TITLES:
        escaped_title = re.escape(title)
        fragment = re.sub(
            rf'(?is)<h([2-6])\b[^>]*>\s*(?:\d+\s*[\.)\]:-]\s*)?{escaped_title}\s*</h\1>.*?(?=<h[1-6]\b|</section>|$)',
            '',
            fragment,
        )

    return fragment


def strip_unknown_placeholder_brackets(report_html):
    cleaned = str(report_html or '')
    placeholder_texts = [
        'exact event-status call not visible in provided scope',
        'exact compared variable not visible in provided scope',
        'exact state-transition token not visible in provided scope',
        'exact comparator not visible in provided scope',
    ]
    for placeholder in placeholder_texts:
        cleaned = cleaned.replace(f'[{placeholder}]', placeholder)
    return cleaned


def normalize_bare_code_list_items(report_html):
    def replace_item(match):
        inner_html = (match.group(1) or '').strip()
        if re.search(r'(?is)<div\b[^>]*class=(?:["\"]).*?(?:expr|meaning|evidence|unknown|kv).*?(?:["\"])', inner_html):
            return match.group(0)

        code_match = re.fullmatch(r'(?is)<code\b[^>]*>(.*?)</code>(.*)', inner_html)
        if not code_match:
            return match.group(0)

        code_html = code_match.group(1).strip()
        trailing_html = (code_match.group(2) or '').strip()
        rebuilt = f'<li><div class="expr"><code>{code_html}</code></div>'
        trailing_text = strip_html_tags(trailing_html)
        if trailing_text:
            rebuilt += f'<div class="meaning">{html.escape(trailing_text)}</div>'
        rebuilt += '</li>'
        return rebuilt

    return re.sub(r'(?is)<li\b[^>]*>\s*(.*?)\s*</li>', replace_item, str(report_html or ''))


def strip_repetitive_report_prefixes(text):
    sample = re.sub(r'\s+', ' ', str(text or '')).strip()
    sample = re.sub(r'(?i)^visible calibration window\s*:\s*', '', sample)
    sample = re.sub(r'(?i)^visible calibration threshold(?: references)?\s*:\s*', '', sample)
    sample = re.sub(r'(?i)^visible calibration gate\s*:\s*', '', sample)
    return sample.strip()


def simplify_unknown_text(text):
    sample = html.unescape(strip_html_tags(text or ''))
    sample = re.sub(r'\s+', ' ', sample).strip().strip('[]').strip(' ;')
    sample = strip_repetitive_report_prefixes(sample)

    replacements = [
        (r'(?i)^exact compared variable\s*:\s*exact compared variable not visible in provided scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact compared variable\s*:\s*exact compared variable not visible in scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact compared variable\s*:\s*not visible in provided scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact compared variable\s*:\s*not visible in scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact compared variable not visible in provided scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact compared variable not visible in scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact comparator\s*:\s*not visible in provided scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact comparator\s*:\s*not visible in scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact comparator not visible in provided scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact comparator not visible in scope$', 'runtime comparator not visible in scope'),
        (r'(?i)^exact event-status call\s*:\s*not visible in provided scope$', 'event-status call not visible in scope'),
        (r'(?i)^exact event-status call\s*:\s*not visible in scope$', 'event-status call not visible in scope'),
        (r'(?i)^exact event-status call not visible in provided scope$', 'event-status call not visible in scope'),
        (r'(?i)^exact event-status call not visible in scope$', 'event-status call not visible in scope'),
        (r'(?i)^exact state-transition token\s*:\s*not visible in provided scope$', 'state-transition token not visible in scope'),
        (r'(?i)^exact state-transition token\s*:\s*not visible in scope$', 'state-transition token not visible in scope'),
        (r'(?i)^exact state-transition token not visible in provided scope$', 'state-transition token not visible in scope'),
        (r'(?i)^exact state-transition token not visible in scope$', 'state-transition token not visible in scope'),
        (r'(?i)^exact fail comparator\s*:\s*runtime comparator not visible in scope$', 'fail comparator not visible in scope'),
        (r'(?i)^exact pass comparator\s*:\s*runtime comparator not visible in scope$', 'pass comparator not visible in scope'),
        (r'(?i)^exact clear comparator\s*:\s*runtime comparator not visible in scope$', 'clear comparator not visible in scope'),
        (r'(?i)^exact event-status completion call\s*=\s*event-status call not visible in provided scope$', 'event-status completion call not visible in scope'),
        (r'(?i)^exact event-status completion call\s*=\s*event-status call not visible in scope$', 'event-status completion call not visible in scope'),
    ]
    for pattern, replacement in replacements:
        sample = re.sub(pattern, replacement, sample)

    sample = re.sub(r'(?i)\bprovided scope\b', 'scope', sample)
    sample = re.sub(r'\s+', ' ', sample).strip(' ;')
    return sample


def cleanup_report_phrasing(report_html):
    cleaned = str(report_html or '')

    direct_replacements = {
        '[exact compared variable not visible in provided scope]': 'runtime comparator not visible in scope',
        '[exact comparator not visible in provided scope]': 'runtime comparator not visible in scope',
        '[exact event-status call not visible in provided scope]': 'event-status call not visible in scope',
        '[exact state-transition token not visible in provided scope]': 'state-transition token not visible in scope',
        'Visible calibration window: ': '',
        'Visible calibration threshold: ': '',
        'Visible calibration threshold references: ': '',
        'Visible calibration gate: ': '',
    }
    for old, new in direct_replacements.items():
        cleaned = cleaned.replace(old, new)

    regex_replacements = [
        (r'(?i)exact compared variable:\s*exact compared variable not visible in provided scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact compared variable:\s*exact compared variable not visible in scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact compared variable:\s*not visible in provided scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact compared variable:\s*not visible in scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact comparator:\s*not visible in provided scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact comparator:\s*not visible in scope', 'runtime comparator not visible in scope'),
        (r'(?i)exact event-status call:\s*not visible in provided scope', 'event-status call not visible in scope'),
        (r'(?i)exact event-status call:\s*not visible in scope', 'event-status call not visible in scope'),
        (r'(?i)exact state-transition token:\s*not visible in provided scope', 'state-transition token not visible in scope'),
        (r'(?i)exact state-transition token:\s*not visible in scope', 'state-transition token not visible in scope'),
    ]
    for pattern, replacement in regex_replacements:
        cleaned = re.sub(pattern, replacement, cleaned)

    return cleaned


def normalize_unknown_placeholders(text):
    sample = str(text or '')
    patterns = [
        r'(?i)\[\s*exact compared variable not visible in provided scope\s*\]',
        r'(?i)exact compared variable\s*:\s*exact compared variable not visible in provided scope',
        r'(?i)exact compared variable\s*:\s*not visible in provided scope',
        r'(?i)exact compared variable not visible in provided scope',
        r'(?i)exact compared variable\s*:\s*not visible in scope',
        r'(?i)exact compared variable not visible in scope',
        r'(?i)\[\s*exact comparator not visible in provided scope\s*\]',
        r'(?i)exact comparator\s*:\s*not visible in provided scope',
        r'(?i)exact comparator not visible in provided scope',
        r'(?i)exact comparator\s*:\s*not visible in scope',
        r'(?i)exact comparator not visible in scope',
        r'(?i)runtime comparator not visible in scope',
    ]
    for pattern in patterns:
        sample = re.sub(pattern, '__UNKNOWN_COMPARATOR__', sample)
    return sample


def split_unknown_formula_text(text):
    sample = html.unescape(strip_html_tags(text or ''))
    sample = re.sub(r'\s+', ' ', sample).strip()
    sample = strip_repetitive_report_prefixes(sample)
    sample = normalize_unknown_placeholders(sample)

    if '__UNKNOWN_COMPARATOR__' not in sample:
        return None

    ops = r'(?:<=|>=|==|!=|<|>)'

    def trim_piece(value):
        part = re.sub(rf'^\s*{ops}\s*', '', value or '').strip()
        part = re.sub(rf'\s*{ops}\s*$', '', part).strip()
        return part.strip(' ,;')

    range_match = re.match(rf'^(.*?)\s*{ops}\s*__UNKNOWN_COMPARATOR__\s*{ops}\s*(.*?)$', sample)
    if range_match:
        left_piece = trim_piece(range_match.group(1))
        right_piece = trim_piece(range_match.group(2))
        visible_parts = [part for part in [left_piece, right_piece] if part]
        return {
            'expr_text': ', '.join(visible_parts),
            'unknown_text': 'runtime comparator not visible in scope',
        }

    left_only_match = re.match(rf'^(.*?)\s*{ops}\s*__UNKNOWN_COMPARATOR__$', sample)
    if left_only_match:
        visible_piece = trim_piece(left_only_match.group(1))
        return {
            'expr_text': visible_piece,
            'unknown_text': 'runtime comparator not visible in scope',
        }

    right_only_match = re.match(rf'^__UNKNOWN_COMPARATOR__\s*{ops}\s*(.*?)$', sample)
    if right_only_match:
        visible_piece = trim_piece(right_only_match.group(1))
        return {
            'expr_text': visible_piece,
            'unknown_text': 'runtime comparator not visible in scope',
        }

    if sample.strip() == '__UNKNOWN_COMPARATOR__':
        return {
            'expr_text': '',
            'unknown_text': 'runtime comparator not visible in scope',
        }

    simplified = simplify_unknown_text(sample.replace('__UNKNOWN_COMPARATOR__', 'runtime comparator not visible in scope'))
    return {
        'expr_text': '',
        'unknown_text': simplified or 'runtime comparator not visible in scope',
    }


def normalize_unknown_variable_fallbacks(report_html):
    div_pattern = re.compile(
        r'(?is)<div\b([^>]*)class=(["\'])(expr|kv|unknown)\2([^>]*)>(.*?)</div>'
    )

    def replace_div(match):
        classes = match.group(3)
        inner_html = match.group(5)
        plain_text = re.sub(r'\s+', ' ', html.unescape(strip_html_tags(inner_html or ''))).strip()
        lowered = plain_text.casefold()

        parsed = split_unknown_formula_text(plain_text)
        if parsed:
            parts = []
            if parsed.get('expr_text'):
                parts.append(f'<div class="expr"><code>{html.escape(parsed["expr_text"])}</code></div>')
            if parsed.get('unknown_text'):
                parts.append(f'<div class="unknown">{html.escape(parsed["unknown_text"])}</div>')
            return ''.join(parts)

        if classes == 'unknown':
            simplified = simplify_unknown_text(plain_text)
            return f'<div class="unknown">{html.escape(simplified)}</div>'

        if lowered.startswith('exact ') and 'not visible' in lowered:
            simplified = simplify_unknown_text(plain_text)
            return f'<div class="unknown">{html.escape(simplified)}</div>'

        return match.group(0)

    return div_pattern.sub(replace_div, str(report_html or ''))


def collapse_duplicate_unknown_divs(report_html):
    li_pattern = re.compile(r'(?is)<li\b[^>]*>.*?</li>')
    unknown_pattern = re.compile(r'(?is)<div\b[^>]*class=(["\'])unknown\1[^>]*>(.*?)</div>')

    def rewrite_li(match):
        li_html = match.group(0)
        seen = set()

        def replace_unknown(unknown_match):
            text = simplify_unknown_text(unknown_match.group(2))
            key = normalize_report_token(text)
            if key in seen:
                return ''
            seen.add(key)
            return f'<div class="unknown">{html.escape(text)}</div>'

        rewritten = unknown_pattern.sub(replace_unknown, li_html)
        rewritten = re.sub(r'\s{2,}', ' ', rewritten)
        return rewritten

    return li_pattern.sub(rewrite_li, str(report_html or ''))


def classify_expr_block_text(block_text):
    compact_text = re.sub(r'\s+', ' ', html.unescape(strip_html_tags(block_text or ''))).strip()
    if not compact_text:
        return 'meaning', ''

    compact_text = strip_repetitive_report_prefixes(compact_text)
    lowered = compact_text.casefold()

    if lowered.startswith('meaning:'):
        return 'meaning', compact_text
    if lowered.startswith('evidence:'):
        return 'evidence', compact_text
    if any(pattern in lowered for pattern in UNKNOWN_PATTERNS):
        return 'unknown', simplify_unknown_text(compact_text)

    lhs = ''
    if ' = ' in compact_text:
        lhs = compact_text.split(' = ', 1)[0].strip().casefold()

    narrative_labels = {
        'classification',
        'target/module',
        'active dtc count',
        'enabled mil dtcs',
        'enabled non-mil dtcs',
        'disabled dtcs',
        'biggest known gap',
        'main risk',
        'main executable story',
        'current-module evidence',
        'related-module evidence',
        'fd evidence',
        'csv/cal data quality',
        'caller/task context',
        'ownership registration',
        'reference registration',
        'kam definition',
        'current max',
    }

    if lhs in narrative_labels:
        return 'kv', compact_text

    if lhs and re.search(r'[ /-]', lhs):
        return 'kv', compact_text

    expression_patterns = [
        r'\b[A-Za-z_][A-Za-z0-9_\[\]]*\s*(?:<=|>=|==|!=|=|<|>)',
        r'\s->\s',
        r'\bTRUE\b|\bFALSE\b',
        r'\bmax\s*\(',
        r'\bmin\s*\(',
        r'\bbit\s+\d+\b',
    ]
    if any(re.search(pattern, compact_text, re.IGNORECASE) for pattern in expression_patterns):
        return 'expr', compact_text

    return 'meaning', compact_text


def reclassify_expr_blocks(report_html):
    def replace_expr_block(match):
        code_inner = match.group(2)
        block_kind, compact_text = classify_expr_block_text(code_inner)
        if not compact_text:
            return ''
        if block_kind == 'expr':
            return f'<div class="expr"><code>{html.escape(compact_text)}</code></div>'
        return f'<div class="{block_kind}">{html.escape(compact_text)}</div>'

    return re.sub(
        r'(?is)<div\b[^>]*class=(["\'])expr\1[^>]*>\s*<code>(.*?)</code>\s*</div>',
        replace_expr_block,
        str(report_html or ''),
    )


def rewrite_unknown_variable_expr_li(li_html):
    expr_match = re.search(r"(?is)<div\b[^>]*class=(['\"])expr\1[^>]*>\s*<code>(.*?)</code>\s*</div>", li_html or '')
    if not expr_match:
        return li_html

    code_text = strip_html_tags(expr_match.group(2))
    compact_text = re.sub(r'\s+', ' ', code_text).strip()
    var_placeholder = '[exact compared variable not visible in provided scope]'
    cmp_placeholder = '[exact comparator not visible in provided scope]'
    event_placeholder = '[exact event-status call not visible in provided scope]'
    state_placeholder = '[exact state-transition token not visible in provided scope]'

    placeholder_only_map = {
        var_placeholder: 'exact compared variable: not visible in provided scope',
        cmp_placeholder: 'exact comparator: not visible in provided scope',
        event_placeholder: 'exact event-status call: not visible in provided scope',
        state_placeholder: 'exact state-transition token: not visible in provided scope',
    }

    if compact_text in placeholder_only_map:
        unknown_text = placeholder_only_map[compact_text]
        replacement = '' if unknown_text in li_html else f'<div class="unknown">{unknown_text}</div>'
        return re.sub(r"(?is)<div\b[^>]*class=(['\"])expr\1[^>]*>\s*<code>.*?</code>\s*</div>", replacement, li_html, count=1)

    if var_placeholder not in compact_text:
        return li_html

    def trim_visible_piece(value):
        cleaned = re.sub(r'^\s*(<=|>=|==|<|>)\s*', '', value or '').strip()
        cleaned = re.sub(r'\s*(<=|>=|==|<|>)\s*$', '', cleaned).strip()
        return cleaned.strip(' ,;')

    label = 'Visible calibration gate'
    window_match = re.match(r'^(.*?)\s*<=\s*' + re.escape(var_placeholder) + r'\s*<=\s*(.*?)$', compact_text)
    if window_match:
        left_piece = trim_visible_piece(window_match.group(1))
        right_piece = trim_visible_piece(window_match.group(2))
        if left_piece or right_piece:
            visible_items = ', '.join(part for part in [left_piece, right_piece] if part)
            rewritten_code = f'Visible calibration window: {visible_items}'
        else:
            rewritten_code = compact_text
    else:
        threshold_match = re.match(r'^' + re.escape(var_placeholder) + r'\s*(<=|>=|==|<|>)\s*(.*?)$', compact_text)
        reverse_threshold_match = re.match(r'^(.*?)\s*(<=|>=|==|<|>)\s*' + re.escape(var_placeholder) + r'$', compact_text)
        if threshold_match:
            visible_piece = trim_visible_piece(threshold_match.group(2))
            label = 'Visible calibration threshold'
            rewritten_code = f'{label}: {visible_piece}' if visible_piece else compact_text
        elif reverse_threshold_match:
            visible_piece = trim_visible_piece(reverse_threshold_match.group(1))
            label = 'Visible calibration threshold'
            rewritten_code = f'{label}: {visible_piece}' if visible_piece else compact_text
        else:
            visible_piece = compact_text.replace(var_placeholder, '').replace(cmp_placeholder, '').strip(' ,;')
            rewritten_code = f'{label}: {visible_piece}' if visible_piece else compact_text

    unknown_lines = []
    if 'exact compared variable:' not in li_html:
        unknown_lines.append('<div class="unknown">exact compared variable: not visible in provided scope</div>')
    if cmp_placeholder in compact_text and 'exact comparator:' not in li_html:
        unknown_lines.append('<div class="unknown">exact comparator: not visible in provided scope</div>')

    replacement_expr = f'<div class="expr"><code>{html.escape(rewritten_code)}</code></div>' + ''.join(unknown_lines)
    return re.sub(r"(?is)<div\b[^>]*class=(['\"])expr\1[^>]*>\s*<code>.*?</code>\s*</div>", replacement_expr, li_html, count=1)


def normalize_unknown_variable_fallbacks(report_html):
    return re.sub(r'(?is)<li\b[^>]*>.*?</li>', lambda match: rewrite_unknown_variable_expr_li(match.group(0)), str(report_html or ''))


def dedupe_additional_cal_section(section_html, referenced_tokens):
    if not section_html:
        return section_html

    seen_tokens = set()
    seen_texts = set()

    def extract_item_tokens(item_html):
        code_tokens = extract_report_tokens(' '.join(re.findall(r'(?is)<code\b[^>]*>(.*?)</code>', item_html or '')))
        text_tokens = extract_report_tokens(html.unescape(strip_html_tags(item_html or '')))
        return code_tokens | text_tokens

    def should_keep_cal_item(item_html):
        item_text = strip_html_tags(item_html).casefold()
        if any(marker in item_text for marker in missing_value_markers):
            return False
        item_tokens = extract_item_tokens(item_html)
        if item_tokens and item_tokens & referenced_tokens:
            return False
        normalized_text = re.sub(r'\s+', ' ', item_text).strip()
        if normalized_text in seen_texts:
            return False
        if item_tokens and item_tokens & seen_tokens:
            return False
        seen_texts.add(normalized_text)
        seen_tokens.update(item_tokens)
        return True

    li_pattern = re.compile(r'(?is)<li\b[^>]*>.*?</li>')
    items = li_pattern.findall(section_html)
    if not items:
        return section_html

    missing_value_markers = [
        'current project value not supplied',
        'current values not supplied',
        'current value not provided',
        'current values not provided',
        '[current value not provided',
        '[current values not provided',
        'visible calibration summary excerpt',
        'visible calibration summary',
        'declared in source;',
    ]

    kept_items = [item_html for item_html in items if should_keep_cal_item(item_html)]

    if len(kept_items) == len(items):
        return section_html

    replacement = ''.join(kept_items) or '<p class="muted">No additional CAL items remained after removing values already referenced above.</p>'
    if kept_items:
        rebuilt_groups = []
        grouped_blocks = list(re.finditer(r'(?is)(<h3\b[^>]*>.*?</h3>\s*)?<ul\b[^>]*>(.*?)</ul>', section_html))
        if grouped_blocks:
            for block in grouped_blocks:
                heading_html = block.group(1) or ''
                group_items = li_pattern.findall(block.group(2) or '')
                group_kept = [group_item for group_item in group_items if should_keep_cal_item(group_item)]
                if group_kept:
                    rebuilt_groups.append(f'{heading_html}<ul>{"".join(group_kept)}</ul>')
            if rebuilt_groups:
                return re.sub(r'(?is)</h2>.*$', r'</h2>' + ''.join(rebuilt_groups), section_html, count=1)

    if re.search(r'(?is)<ul\b[^>]*>.*?</ul>', section_html):
        return re.sub(r'(?is)<ul\b[^>]*>.*?</ul>', f'<ul class="report-list">{replacement}</ul>' if kept_items else replacement, section_html, count=1)
    return re.sub(r'(?is)</h2>', r'</h2>' + replacement, section_html, count=1)


def enforce_final_report_structure(report_html):
    fragment = str(report_html or '').strip()
    if not fragment:
        return fragment

    allowed_titles = {normalize_report_section_title(title): title for title in FINAL_REPORT_SECTION_TITLES}
    retired_titles = {normalize_report_section_title(title) for title in RETIRED_REPORT_SECTION_TITLES}
    section_blocks = split_report_sections(fragment)
    selected_sections = {}

    for section_html in section_blocks:
        title = normalize_report_section_title(find_section_heading(section_html, 'h2'))
        if not title or title in retired_titles:
            continue
        if title in allowed_titles and title not in selected_sections:
            selected_sections[title] = section_html.strip()

    required_section_keys = [normalize_report_section_title(title) for title in FINAL_REPORT_SECTION_TITLES]
    if any(title_key not in selected_sections for title_key in required_section_keys):
        return fragment

    prior_section_html = ''.join(selected_sections.get(normalize_report_section_title(title), '') for title in FINAL_REPORT_SECTION_TITLES[:4])
    additional_title_key = normalize_report_section_title(FINAL_REPORT_SECTION_TITLES[4])
    if additional_title_key in selected_sections:
        selected_sections[additional_title_key] = dedupe_additional_cal_section(
            selected_sections[additional_title_key],
            extract_code_identifiers(prior_section_html),
        )

    ordered_sections = []

    for title in FINAL_REPORT_SECTION_TITLES:
        title_key = normalize_report_section_title(title)
        ordered_sections.append(canonicalize_report_section(strip_legacy_html_sections(selected_sections[title_key]), title))

    return ''.join(ordered_sections)


def text_report_to_html(report_text):
    text = strip_legacy_markdown_sections(report_text)
    if not text:
        return ''

    out = []
    in_list = False
    section_open = False
    retired_titles = {normalize_report_section_title(title) for title in RETIRED_REPORT_SECTION_TITLES}
    skipping_retired_section = False

    def open_section_if_needed():
        nonlocal section_open
        if not section_open:
            out.append('<section class="report-section">')
            section_open = True

    def close_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    def open_list():
        nonlocal in_list
        if not in_list:
            out.append('<ul class="report-list">')
            in_list = True

    for raw_line in str(text).splitlines():
        line = raw_line.rstrip()
        kind = classify_line(line)
        sample = re.sub(r'\s+', ' ', line).strip()

        if kind == 'blank':
            continue

        heading_match = REPORT_HEADING_RE.match(sample)
        if heading_match:
            skipping_retired_section = False
            heading_title = heading_match.group(2).strip()
            if normalize_report_section_title(heading_title) in retired_titles:
                close_list()
                skipping_retired_section = True
                continue
            close_list()
            if section_open:
                out.append('</section>')
            out.append(f'<section class="report-section"><h2 class="section-title">{html.escape(heading_title)}</h2>')
            section_open = True
            continue

        if skipping_retired_section:
            continue

        open_section_if_needed()
        escaped = html.escape(sample)

        if kind == 'expr':
            open_list()
            out.append(f'<li><div class="expr"><code>{escaped}</code></div></li>')
        elif kind == 'meaning':
            open_list()
            out.append(f'<li><div class="meaning">{escaped}</div></li>')
        elif kind == 'evidence':
            open_list()
            out.append(f'<li><div class="evidence">{escaped}</div></li>')
        elif kind == 'unknown':
            open_list()
            out.append(f'<li><div class="unknown">{escaped}</div></li>')
        elif kind == 'kv':
            open_list()
            out.append(f'<li><div class="kv">{escaped}</div></li>')
        else:
            close_list()
            out.append(f'<p>{escaped}</p>')

    close_list()
    if section_open:
        out.append('</section>')

    return ''.join(out)


def legacy_report_text_to_html(report_text):
    return text_report_to_html(report_text)


def normalize_report_html(report_html, fallback_message='Generated report was empty.'):
    text = str(report_html or '').strip()

    if re.match(r'^```(?:html)?\s*', text, re.IGNORECASE):
        text = re.sub(r'^```(?:html)?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```\s*$', '', text).strip()

    body_match = re.search(r'(?is)<body[^>]*>(.*?)</body>', text)
    if body_match:
        text = body_match.group(1).strip()
    elif re.search(r'(?is)<html\b', text):
        html_match = re.search(r'(?is)<html\b.*?</html>', text)
        if html_match:
            text = re.sub(r'(?is)^.*?<html\b[^>]*>', '', html_match.group(0))
            text = re.sub(r'(?is)</html>.*$', '', text).strip()

    text = text.strip()

    if not text:
        safe_message = re.sub(r'[<>]', '', fallback_message or 'Generated report was empty.')
        return (
            '<section class="report-section warn">'
            '<h2 class="section-title">Report Generation Error</h2>'
            f'<p class="unknown">{safe_message}</p>'
            '</section>'
        )

    return text


def prepare_report_html(raw_output, fallback_message='Generated report was empty.'):
    normalized = normalize_report_html(raw_output, fallback_message=fallback_message)
    normalized = cleanup_report_phrasing(normalized)
    prepared = normalized
    prepared = strip_unknown_placeholder_brackets(prepared)
    prepared = cleanup_report_phrasing(prepared)
    prepared = normalize_unknown_variable_fallbacks(prepared)
    prepared = collapse_duplicate_unknown_divs(prepared)

    return prepared


def render_report(report_html, module_name='MODULE', generated_at=None, engine_name=REPORT_ENGINE_NAME, title=None):
    normalized_html = prepare_report_html(report_html)
    module_label = (module_name or 'MODULE').strip() or 'MODULE'
    generated_label = generated_at or time.strftime('%Y-%m-%d %H:%M:%S')
    page_title = title or f'{module_label} Report'
    return render_template(
        REPORT_SHELL_TEMPLATE,
        report_html=normalized_html,
        module_name=module_label,
        generated_at=generated_label,
        engine_name=engine_name,
        title=page_title,
    )


def current_dtc_extractor_metadata():
    return {
        'extractor_version': DTC_EXTRACTOR_VERSION,
        'server_pid': os.getpid(),
        'server_boot_time': APP_BOOT_TIME,
    }


def dtc_matches_uds_mode(dtc_code, uds_mode):
    if not dtc_code:
        return False
    if uds_mode is None:
        return True
    is_uds = '-' in str(dtc_code)
    return is_uds if uds_mode is True else not is_uds
    
def filter_dtc_codes_by_uds_mode(dtc_codes, uds_mode):
    uds_mode = normalize_uds_mode(uds_mode)
    return [code for code in (dtc_codes or []) if dtc_matches_uds_mode(code, uds_mode)]


DTC_SYSTEM_LABELS = {
    'P': 'Powertrain: Engine, transmission, and emission systems.',
    'B': 'Body: Passenger compartment, lights, airbags.',
    'C': 'Chassis: Suspension, brakes, steering.',
    'U': 'Network: Communication between modules (CAN bus).',
}


DTC_TYPE_LABELS = {
    '0': 'Generic/ISO standardized code (common across all manufacturers).',
    '1': 'Manufacturer-specific code.',
}


P_CODE_SUBSYSTEM_LABELS = {
    '0': 'Fuel and air metering.',
    '1': 'Fuel and air metering.',
    '2': 'Fuel and air metering.',
    '3': 'Ignition system or misfire.',
    '4': 'Auxiliary emission controls.',
    '5': 'Idle control/vehicle speed.',
    '6': 'Computer/output circuit.',
    '7': 'Transmission.',
    '8': 'Transmission.',
    '9': 'Transmission.',
}


def build_dtc_rule(code):
    compact = compact_dtc_code(code)
    if not re.fullmatch(r'[PBCU][0-9][0-9A-F]{3}(?:[0-9A-F]{2})?', compact):
        return {
            'code': code,
            'base_code': '',
            'structure': '',
            'system': 'Unknown DTC family.',
            'type': 'Unknown DTC type.',
            'subsystem': 'Subsystem rule unavailable.',
            'specific_fault': '',
            'extended_suffix': '',
            'summary': 'Rule metadata unavailable for this code.',
        }

    base_code = compact[:5]
    family = base_code[0]
    type_digit = base_code[1]
    subsystem_digit = base_code[2]
    specific_fault = base_code[3:5]
    extended_suffix = compact[5:]

    system = DTC_SYSTEM_LABELS.get(family, 'Unknown DTC family.')
    dtype = DTC_TYPE_LABELS.get(type_digit, f'Type digit {type_digit}: not covered by the current rule set.')

    if family == 'P':
        subsystem = P_CODE_SUBSYSTEM_LABELS.get(
            subsystem_digit,
            f'P-code subsystem digit {subsystem_digit}: not covered by the current rule set.',
        )
    else:
        subsystem = f'{family}-code subsystem digit {subsystem_digit}: no additional subsystem rule provided.'

    summary_parts = [system, dtype, f'Subsystem: {subsystem}']
    if extended_suffix:
        summary_parts.append(f'Extended suffix: {extended_suffix}')

    return {
        'code': code,
        'base_code': base_code,
        'structure': f'[{base_code[0]}][{base_code[1]}][{base_code[2]}][{base_code[3]}][{base_code[4]}]',
        'system': system,
        'type': dtype,
        'subsystem': subsystem,
        'specific_fault': specific_fault,
        'extended_suffix': extended_suffix,
        'summary': ' | '.join(summary_parts),
    }


def extract_dtc_codes_from_line(line):
    upper_line = line.upper()
    codes = set()

    for base in re.findall(r'\bDTC_([PBCU][0-9][0-9A-F]{3})_(?:FAIL|PASS)\b', upper_line):
        codes.add(base)

    for base in re.findall(r'\bDTC_([PBCU][0-9][0-9A-F]{3})_ROM\b', upper_line):
        codes.add(base)

    for base in re.findall(r'\bDEVNT_([PBCU][0-9][0-9A-F]{3})\b(?![_-][0-9A-F]{2})', upper_line):
        codes.add(base)

    for family, base in re.findall(r'\bFMC_(?!UDS_)[A-Z0-9_]*REF\s*\(\s*([PBCU])([0-9][0-9A-F]{3})\b', upper_line):
        codes.add(f'{family}{base}')

    for base in re.findall(r'(?<![A-Z0-9_])([PBCU][0-9][0-9A-F]{3})(?![A-Z0-9_])', upper_line):
        codes.add(base)

    # Only treat suffix forms as UDS when they appear in actual DTC contexts.
    for base, sub in re.findall(r'\bDEVNT_([PBCU][0-9][0-9A-F]{3})[_-]([0-9A-F]{2})\b', upper_line):
        codes.add(f'{base}-{sub}')

    for base, sub in re.findall(r'\bDTC_([PBCU][0-9][0-9A-F]{3})[_-]([0-9A-F]{2})(?:_(?:FAIL|PASS))?\b', upper_line):
        codes.add(f'{base}-{sub}')

    for base, sub in re.findall(r'(?<![A-Z0-9_])([PBCU][0-9][0-9A-F]{3})[_-]([0-9A-F]{2})(?![A-Z0-9_])', upper_line):
        codes.add(f'{base}-{sub}')

    for compact in re.findall(r'(?<![A-Z0-9_])([PBCU][0-9][0-9A-F]{5})(?![A-Z0-9_])', upper_line):
        codes.add(f'{compact[:5]}-{compact[5:]}')

    for family, base, sub in re.findall(r'\b([PBCU])\s*,\s*([0-9][0-9A-F]{3})\s*,\s*([0-9A-F]{2})\b', upper_line):
        codes.add(f'{family}{base}-{sub}')

    return codes


def is_reference_dtc_line(line):
    return bool(re.search(r'\bFMC_[A-Z0-9_]*REF\s*\(', line, re.IGNORECASE))


def is_comment_only_line(line):
    stripped = line.strip()
    return (
        stripped.startswith('/*')
        or stripped.startswith('*')
        or stripped.startswith('//')
    )


def extract_dtc_catalog(content, max_comment_len=220):
    all_codes = set()
    diagnostic_codes = set()
    not_used_block = False
    comment_map = {}

    lines = content.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        lowered = stripped.lower()

        if 'not used' in lowered:
            not_used_block = True
            continue

        if not_used_block and (not stripped or stripped.startswith('#')):
            not_used_block = False

        line_codes = extract_dtc_codes_from_line(line)
        if not line_codes:
            continue

        all_codes.update(line_codes)

        # Reuse this same pass to capture a nearby comment for each newly-seen code.
        missing_codes = [c for c in line_codes if c not in comment_map]
        if missing_codes:
            text = ''
            same_line = re.search(r'/\*\s*(.+?)\s*\*/', line) or re.search(r'//\s*(.+)$', line)
            if same_line:
                text = same_line.group(1).strip()
            if not text:
                block = []
                j = idx - 1
                while j >= 0 and is_comment_only_line(lines[j]):
                    cleaned = re.sub(r'^[\s/\*]+', '', lines[j])
                    cleaned = re.sub(r'[\s\*/]+$', '', cleaned).strip()
                    if cleaned:
                        block.insert(0, cleaned)
                    j -= 1
                text = ' '.join(block).strip()
            if text:
                for code in missing_codes:
                    comment_map[code] = text[:max_comment_len]

        if is_reference_dtc_line(line):
            if not not_used_block:
                diagnostic_codes.update(line_codes)
            continue

        if is_comment_only_line(line):
            continue

    reference_only_codes = all_codes - diagnostic_codes
    non_uds_codes = {code for code in all_codes if '-' not in code}
    uds_codes = {code for code in all_codes if '-' in code}
    return {
        'all': sorted(all_codes),
        'diagnostic': sorted(diagnostic_codes),
        'reference_only': sorted(reference_only_codes),
        'non_uds': sorted(non_uds_codes),
        'uds': sorted(uds_codes),
        'comment_map': comment_map,
    }


DTC_DESCRIPTION_FALLBACK_FILE = os.path.join(BASE_DIR, 'dtc_description_fallbacks.json')
_dtc_description_fallback_cache = None


def load_dtc_description_fallbacks():
    """Load the curated base-DTC (5-char P/B/C/U code) -> description dictionary, cached once."""
    global _dtc_description_fallback_cache
    if _dtc_description_fallback_cache is not None:
        return _dtc_description_fallback_cache

    merged = {}
    try:
        with open(DTC_DESCRIPTION_FALLBACK_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for code, entry in raw.items():
                text = entry.get('text') if isinstance(entry, dict) else entry
                if text:
                    merged[str(code).strip().upper()] = str(text).strip()
    except Exception:
        pass

    _dtc_description_fallback_cache = merged
    return merged


# Master DTC reference for this program: "Final" sheet of the PCM design transmittal.
# Column E = DTC (UDS), F = DTC Description, S = EU7 DTC Setting default.
PCM_DT_REFERENCE_FILE = os.path.join(
    BASE_DIR, 'OBD_Design_Transmittals_PCM', 'D100064_PCM_DT_Updates_2024_COPY (version 1).xlsb'
)
PCM_DT_REFERENCE_SHEET = 'Final'
PCM_DT_REFERENCE_LABEL = 'D100064 PCM DT Updates 2024 (Final sheet)'
_pcm_dt_reference_cache = None


def format_pcm_dt_setting_value(value):
    if value is None:
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def load_pcm_dt_reference_table():
    """Load the DTC(UDS) -> {description, eu7_dtc_setting} map from the PCM design transmittal
    ("Final" sheet, columns E/F/S). This is the authoritative DTC reference for this program;
    it is checked before the A2L table, the curated dictionary, and C-code comments."""
    global _pcm_dt_reference_cache
    if _pcm_dt_reference_cache is not None:
        return _pcm_dt_reference_cache

    entries = {}
    try:
        from pyxlsb import open_workbook
        with open_workbook(PCM_DT_REFERENCE_FILE) as wb:
            with wb.get_sheet(PCM_DT_REFERENCE_SHEET) as sheet:
                for row_idx, row in enumerate(sheet.rows()):
                    if row_idx < 2:
                        continue  # rows 0-1 are the grouped/merged header labels
                    cells = [c.v for c in row]
                    if len(cells) <= 18:
                        continue
                    dtc_uds = str(cells[4] or '').strip().upper().replace('_', '-')
                    if not re.fullmatch(r'[PBCU][0-9A-F]{4}-[0-9A-F]{2}', dtc_uds):
                        continue
                    description = str(cells[5] or '').strip()
                    setting = format_pcm_dt_setting_value(cells[18])
                    entry = {'description': description, 'eu7_dtc_setting': setting}
                    entries[dtc_uds] = entry
                    entries.setdefault(dtc_uds.split('-')[0], entry)
    except Exception:
        entries = {}

    _pcm_dt_reference_cache = entries
    return entries


def get_dtc_code_description(dtc_code, a2l_description_map=None, comment_map=None, pcm_dt_map=None):
    """Describe a DTC: prefer the PCM design transmittal (D100064 Final sheet, the program's
    master DTC reference); if it has no entry, fall back to a nearby C-code comment, then the
    A2L DTCWORD table, then the curated standard-code dictionary.
    Returns '' (no noisy filler) when none of these have real content."""
    if not dtc_code:
        return ''

    full_code = dtc_code.strip().upper().replace('_', '-')
    base_code = full_code.split('-')[0]

    pcm_entry = (pcm_dt_map or {}).get(full_code) or (pcm_dt_map or {}).get(base_code)
    if pcm_entry and pcm_entry.get('description'):
        return pcm_entry['description']

    from_code = (comment_map or {}).get(full_code, '')
    if from_code:
        return from_code

    from_a2l = (a2l_description_map or {}).get(base_code, '')
    if from_a2l:
        return from_a2l

    standard = load_dtc_description_fallbacks().get(base_code, '')
    if standard:
        return standard

    return ''


def find_dtc_list_file(module_file):
    if not module_file:
        return ''

    module_dir = os.path.dirname(module_file)
    pack_dir = os.path.dirname(module_dir)
    if not os.path.isdir(pack_dir):
        return ''

    candidates = []
    for name in os.listdir(pack_dir):
        lower = name.lower()
        if not (lower == 'dtc_list.txt' or lower.startswith('dtc_list_')):
            continue
        if not lower.endswith('.txt'):
            continue
        path = os.path.join(pack_dir, name)
        if os.path.isfile(path):
            candidates.append(path)

    if not candidates:
        return ''

    def sort_key(path):
        name = os.path.basename(path).lower()
        priority = 1 if name == 'dtc_list.txt' else 0
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        return (priority, -mtime, name)

    candidates.sort(key=sort_key)
    return candidates[0]


def parse_dtc_module_map_from_list(dtc_list_file):
    dtc_to_modules = {}
    if not dtc_list_file or not os.path.exists(dtc_list_file):
        return dtc_to_modules

    try:
        with open(dtc_list_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                row = line.strip()
                if not row or row.lower().startswith('dtc |'):
                    continue
                parts = [p.strip() for p in row.split('|')]
                if len(parts) < 2:
                    continue
                dtc_code = parts[0].upper()
                if not dtc_code:
                    continue

                modules = []
                for mod in (parts[1] or '').split(','):
                    cleaned = mod.strip().upper()
                    if cleaned and cleaned != 'UNKNOWN' and cleaned != '-':
                        modules.append(cleaned)

                if not modules:
                    continue

                dtc_to_modules.setdefault(dtc_code, set()).update(modules)
    except OSError:
        return {}

    return dtc_to_modules


def dtc_code_related(candidate_code, selected_codes, uds_mode=None):
    uds_mode = normalize_uds_mode(uds_mode)
    cand_formatted = format_uds_dtc(candidate_code)
    cand = compact_dtc_code(cand_formatted)
    if len(cand) < 5 or not dtc_matches_uds_mode(cand_formatted, uds_mode):
        return False

    cand_base = cand[:5]
    cand_is_uds = '-' in cand_formatted

    for code in selected_codes:
        sel_formatted = format_uds_dtc(code)
        sel = compact_dtc_code(sel_formatted)
        if len(sel) < 5 or not dtc_matches_uds_mode(sel_formatted, uds_mode):
            continue

        sel_base = sel[:5]
        sel_is_uds = '-' in sel_formatted

        if uds_mode is False:
            if (not cand_is_uds) and (not sel_is_uds) and cand_base == sel_base:
                return True
            continue

        if uds_mode is True:
            if cand_is_uds and sel_is_uds and cand == sel:
                return True
            continue

        if cand == sel or cand_base == sel_base:
            return True
    return False


def file_looks_like_combined_cbook_source(filepath, lines):
    normalized_path = (filepath or '').replace('\\', '/').lower()
    if normalized_path.endswith('/hzyjb.c') or normalized_path.endswith('/hmyj2.c'):
        return True

    header_preview = ''.join(lines[:120]).lower()
    return 'features included in' in header_preview and 'footer (' in header_preview


def normalize_fd_block_text(block_text):
    content_lines = []
    for raw_line in (block_text or '').splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if 'functional description' in stripped.lower():
            continue
        cleaned = re.sub(r'^[/*#\-\s]+', '', stripped)
        cleaned = re.sub(r'[/*#\-\s]+$', '', cleaned).strip()
        if not cleaned:
            continue
        content_lines.append(cleaned)

    if not content_lines:
        return ''

    meaningful_lines = [line for line in content_lines if re.search(r'[A-Za-z0-9]{3,}', line)]
    if len(meaningful_lines) < 2:
        return ''

    normalized = '\n'.join(content_lines).strip()
    return normalized if len(normalized) >= 40 else ''


def extract_fd_blocks(filepath, max_blocks=20, max_total_chars=30000):
    """Extract non-empty FUNCTIONAL DESCRIPTION blocks from the selected module file only."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        return ''

    if file_looks_like_combined_cbook_source(filepath, lines):
        return ''

    # Start: /*##### FUNCTIONAL DESCRIPTION  or  ** FUNCTIONAL DESCRIPTION (old-format C files)
    fd_start_pat = re.compile(r'(?:/\*[#*= -]{6,}|\*\*\s+)FUNCTIONAL DESCRIPTION', re.IGNORECASE)
    # End: a line with 20+ '#' chars that is NOT the header line itself
    # Handles both  **####...*/  and  /*####...*/  and  ####...text...####*/
    footer_pat = re.compile(r'^\s*FOOTER\s*\(')

    blocks = []
    total_chars = 0
    in_block = False
    current_block = []

    for line in lines:
        if not in_block:
            if fd_start_pat.search(line):
                in_block = True
                current_block = [line]
        else:
            current_block.append(line)
            stripped = line.strip()
            # End condition 1: a horizontal-rule line with 20+ '#' chars
            # (NOT the start line, which also contains '#' but has "FUNCTIONAL DESCRIPTION")
            is_end_rule = (
                re.search(r'#{20,}', stripped)
                and 'functional description' not in stripped.lower()
                and len(current_block) > 2
            )
            # End condition 2: FOOTER line (sub-file separator in concatenated text dumps)
            is_footer = footer_pat.search(line)
            if is_end_rule or is_footer:
                if is_footer and current_block:
                    current_block.pop()  # don't include FOOTER line in block
                block_text = ''.join(current_block)
                normalized_block = normalize_fd_block_text(block_text)
                if normalized_block:
                    blocks.append(normalized_block)
                    total_chars += len(normalized_block)
                in_block = False
                current_block = []
                if len(blocks) >= max_blocks or total_chars >= max_total_chars:
                    break

    if not blocks:
        return ''

    combined = '\n---\n'.join(blocks)
    if len(combined) > max_total_chars:
        combined = combined[:max_total_chars] + '\n...(FD blocks truncated)'
    return f'[FUNCTIONAL DESCRIPTION BLOCKS — extracted from current module file]\n{combined}'


def extract_dtc_line_excerpt(code_text, selected_dtc_codes, max_lines=120):
    lines = code_text.splitlines()
    if not lines:
        return []

    selected_bases = []
    for code in selected_dtc_codes:
        compact = compact_dtc_code(code)
        if len(compact) >= 5:
            selected_bases.append(compact[:5].lower())

    if not selected_bases:
        return []

    hit_indexes = []
    logic_tokens = [
        'enable', 'enabled', 'fail', 'failed', 'pass', 'clear', 'mature',
        'timer', 'counter', 'threshold', 'monitor', 'complete', 'inhibit',
        'debounce', 'trip', 'response', 'rational', 'fmem', 'delay',
        'pending', 'latch', 'mask', 'entry', 'done', 'event', 'gate',
        'cumtmr', 'passtmr', 'flttmr', 'mon_k', 'cmpl', 'selftstatus',
        'devntstatus', 'smcnddevntstatus', 'compileresults', 'latchtestresult',
        'failedresultsk', 'diagxtestfailed', 'diagxtestpassed', 'dtccode',
        'dtcptr', 'koer', 'koeo', 'lean', 'rich', 'smcnd',
    ]
    for idx, line in enumerate(lines):
        compact_line = compact_identifier(line)
        if any(
            base in compact_line
            or f'{base}sw' in compact_line
            or f'devnt{base}' in compact_line
            for base in selected_bases
        ):
            hit_indexes.append(idx)

    if not hit_indexes:
        return []

    keep = set()
    for idx in hit_indexes:
        start = max(idx - 8, 0)
        end = min(idx + 16, len(lines) - 1)
        for j in range(start, end + 1):
            keep.add(j)

    # Preserve nearby logic-bearing lines so related modules can supply
    # enable/fail/pass evidence instead of only DTC ownership wiring.
    for idx, line in enumerate(lines):
        compact_line = compact_identifier(line)
        if not any(token in compact_line for token in logic_tokens):
            continue
        if any(abs(idx - hit_idx) <= 60 for hit_idx in hit_indexes):
            start = max(idx - 2, 0)
            end = min(idx + 2, len(lines) - 1)
            for j in range(start, end + 1):
                keep.add(j)

    selected_rows = sorted(keep)[:max_lines]
    return [f'L{row + 1}: {lines[row]}' for row in selected_rows]


def extract_control_path_excerpt(code_text, max_lines=140):
    lines = code_text.splitlines()
    if not lines:
        return []

    function_def_re = re.compile(r'^\s*(?:static\s+)?(?:inline\s+)?[A-Za-z_][A-Za-z0-9_\s\*]+\([^;]*\)\s*\{?\s*$')
    strong_tokens = (
        'main', 'task', 'run', 'calc', 'ctrl', 'control', 'state', 'mode',
        'cmd', 'output', 'scheduler', 'schedule', 'step', 'transition',
        'request', 'target', 'regen', 'soot', 'rest',
    )
    moderate_tokens = (
        'input', 'process', 'assign', 'update', 'filter', 'limit', 'flag',
        'switch', 'case', 'timer', 'counter', 'select', 'bank', 'path',
    )

    hit_indexes = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        compact_line = compact_identifier(line)
        score = 0

        if function_def_re.match(stripped) and not stripped.endswith(';'):
            score += 4
        if any(token in compact_line for token in strong_tokens):
            score += 3
        if any(token in compact_line for token in moderate_tokens):
            score += 1
        if re.search(r'\bif\s*\(|\bswitch\s*\(|\bcase\b|\belse\b', line):
            score += 1
        if '=' in line and '==' not in line and '!=' not in line and '<=' not in line and '>=' not in line:
            score += 1

        if score >= 3:
            hit_indexes.append(idx)

    if not hit_indexes:
        return []

    keep = set()
    for idx in hit_indexes[:40]:
        start = max(idx - 6, 0)
        end = min(idx + 14, len(lines) - 1)
        for row in range(start, end + 1):
            keep.add(row)

    selected_rows = sorted(keep)[:max_lines]
    return [f'L{row + 1}: {lines[row]}' for row in selected_rows]


def build_module_dtc_architecture_context(code_text, module_name, selected_dtc_codes=None, uds_mode=None):
    lines = code_text.splitlines()
    if not lines:
        return ''

    uds_mode = normalize_uds_mode(uds_mode)
    selected_dtc_codes = filter_dtc_codes_by_uds_mode(selected_dtc_codes or [], uds_mode)

    def collect_examples(pattern, limit=3):
        examples = []
        regex = re.compile(pattern, re.IGNORECASE)
        for idx, line in enumerate(lines):
            if regex.search(line):
                examples.append(f'L{idx + 1}: {line.rstrip()}')
                if len(examples) >= limit:
                    break
        return examples

    registration_examples = collect_examples(r'FMC_(UDS_)?DEVNT|FMC_SELFT_ONLY_REF|FMC_SMCNDDEVNT_ONLY_REF', limit=4)
    ownership_examples = collect_examples(r'\b\w*dtc\w*(?:_code|_ptr|_latch)\b|failed_results_k', limit=4)
    event_examples = collect_examples(r'FMC_Set(?:SelftStatus|DEvntStatus|SMCNDDEvntStatus)\s*\(', limit=4)
    completion_examples = collect_examples(r'\b\w*(?:mon_k|cmplt\w*|diag_done|mon_pending_k)\b', limit=4)
    delegation_examples = collect_examples(r'diagx_test_(?:failed|passed)|diagx_', limit=4)
    polarity_examples = collect_examples(r'LEAN|RICH|L2R|R2L|fail_mask|pass_mask', limit=4)
    variant_examples = collect_examples(r'#if|#ifdef|#ifndef|FMC_IOCFG_|FMC_APPCFG_UDS', limit=4)

    has_registration = bool(registration_examples)
    has_local_ownership = bool(ownership_examples)
    has_event_reporting = bool(event_examples)
    has_completion = bool(completion_examples)
    has_delegation = bool(delegation_examples)
    has_polarity_pairs = bool(polarity_examples)
    has_build_variants = bool(variant_examples)
    has_uds_duality = any('FMC_UDS_' in ex for ex in registration_examples) and any('FMC_DEVNT_' in ex for ex in registration_examples)
    has_self_test = any('SelftStatus' in ex or 'SELFT' in ex.upper() for ex in registration_examples + event_examples)
    has_smcnd = any('SMCND' in ex.upper() for ex in registration_examples + ownership_examples + event_examples)
    has_maintenance_history = any('failed_results_k' in ex for ex in ownership_examples)

    delegated_only = has_registration and has_delegation and not has_local_ownership and not has_event_reporting
    if delegated_only:
        ownership_mode = 'delegated-or-combined'
    elif has_local_ownership and has_event_reporting:
        ownership_mode = 'local-dtc-logic'
    elif has_registration:
        ownership_mode = 'registration-heavy'
    else:
        ownership_mode = 'no-explicit-dtc-architecture'

    scope_label = ', '.join(selected_dtc_codes[:10]) if selected_dtc_codes else '(module-wide DTC architecture scan)'
    bullets = [
        f'- Module: {(module_name or "UNKNOWN_MODULE").strip().upper()}',
        f'- DTC scope: {scope_label}',
        f'- Ownership mode: {ownership_mode}',
        f'- Direct registration artifacts detected: {"yes" if has_registration else "no"}',
        f'- Local ownership tables/latches detected: {"yes" if has_local_ownership else "no"}',
        f'- Explicit event-status calls detected: {"yes" if has_event_reporting else "no"}',
        f'- Completion or pending flags detected: {"yes" if has_completion else "no"}',
        f'- Delegation to external diagx-style handlers detected: {"yes" if has_delegation else "no"}',
        f'- Paired polarity / slot pattern detected: {"yes" if has_polarity_pairs else "no"}',
        f'- UDS/non-UDS dual registration detected: {"yes" if has_uds_duality else "no"}',
        f'- Self-test / KOER-style path detected: {"yes" if has_self_test else "no"}',
        f'- SMCND second-condition path detected: {"yes" if has_smcnd else "no"}',
        f'- Maintenance-history arrays detected: {"yes" if has_maintenance_history else "no"}',
        f'- Build-variant conditionals detected: {"yes" if has_build_variants else "no"}',
    ]

    interpretation = []
    if delegated_only:
        interpretation.append('- Treat this module as delegation-heavy: do not invent a local comparator if only external diagx-style ownership is visible.')
    elif has_local_ownership and has_event_reporting:
        interpretation.append('- Treat local registration/latch/event flow as primary evidence before using broader FD narratives.')
    if has_polarity_pairs:
        interpretation.append('- If one polarity branch fails and the opposite polarity is explicitly passed in the same block, report both actions together.')
    if has_uds_duality:
        interpretation.append('- Preserve both UDS and non-UDS ownership artifacts in the narrative when build config can switch between them.')
    if has_build_variants:
        interpretation.append('- Do not collapse variant-specific DTC mappings across conditional compilation branches into one unconditional statement.')
    if has_maintenance_history:
        interpretation.append('- Maintenance/result-history arrays indicate rolling test history; describe them as history or maturity support, not as direct comparators.')

    sections = [
        '[MODULE DTC ARCHITECTURE SNAPSHOT]',
        *bullets,
    ]
    if interpretation:
        sections.append('[INTERPRETATION HINTS]')
        sections.extend(interpretation)

    def append_examples(title, examples):
        if not examples:
            return
        sections.append(title)
        sections.extend(f'- {example}' for example in examples)

    append_examples('[REGISTRATION EXAMPLES]', registration_examples)
    append_examples('[OWNERSHIP TABLE EXAMPLES]', ownership_examples)
    append_examples('[EVENT REPORTING EXAMPLES]', event_examples)
    append_examples('[COMPLETION EXAMPLES]', completion_examples)
    append_examples('[DELEGATION EXAMPLES]', delegation_examples)

    return '\n'.join(sections)


def collect_selected_dtc_base_map(selected_dtc_codes):
    base_map = {}
    for code in selected_dtc_codes or []:
        compact = compact_dtc_code(code)
        if len(compact) < 5:
            continue
        base = compact[:5].upper()
        canonical = format_uds_dtc(code) if len(compact) >= 7 else base
        base_map.setdefault(base, set()).add(canonical)
    return base_map


def discover_related_modules_from_source(pack_dir, current_module, selected_dtc_codes, max_candidates=6):
    if not os.path.isdir(pack_dir):
        return {}

    base_map = collect_selected_dtc_base_map(selected_dtc_codes)
    if not base_map:
        return {}

    current_key = (current_module or '').strip().upper()
    scored = []

    for entry in os.scandir(pack_dir):
        if not entry.is_dir():
            continue

        module = entry.name.upper()
        if module == current_key:
            continue

        module_file_path = os.path.join(entry.path, f'{module}.txt')
        if not os.path.isfile(module_file_path):
            continue

        try:
            with open(module_file_path, 'r', encoding='utf-8', errors='replace') as f:
                module_code = f.read()
        except OSError:
            continue

        compact_text = compact_identifier(module_code)
        score = 0
        matched_codes = set()

        for base, canonical_codes in base_map.items():
            b = base.lower()
            base_hit = b in compact_text
            sw_hit = (b + 'sw') in compact_text
            devnt_hit = ('devnt' + b) in compact_text

            if base_hit:
                score += 1
            if sw_hit:
                score += 2
            if devnt_hit:
                score += 2

            if base_hit or sw_hit or devnt_hit:
                matched_codes.update(canonical_codes)

        if score > 0 and matched_codes:
            scored.append((score, module, matched_codes))

    scored.sort(key=lambda item: (-item[0], item[1]))
    result = {}
    for _, module, matched_codes in scored[:max_candidates]:
        result[module] = set(matched_codes)
    return result


def discover_related_modules_from_c_source(source_c_file, current_module, selected_dtc_codes, max_candidates=6):
    if not source_c_file or not os.path.isfile(source_c_file):
        return {}

    base_map = collect_selected_dtc_base_map(selected_dtc_codes)
    if not base_map:
        return {}

    current_key = (current_module or '').strip().upper()
    footer_pat = re.compile(r'FOOTER\s+\(.*?_([A-Z][A-Z_\d]+)\)', re.IGNORECASE)
    scored = []

    current_module_name = None
    current_lines = []
    module_chunks = []

    try:
        with open(source_c_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                match = footer_pat.search(line)
                if match:
                    if current_module_name and current_lines:
                        module_chunks.append((current_module_name, ''.join(current_lines)))
                    current_module_name = match.group(1).upper()
                    current_lines = [line]
                    continue
                if current_module_name:
                    current_lines.append(line)
        if current_module_name and current_lines:
            module_chunks.append((current_module_name, ''.join(current_lines)))
    except OSError:
        return {}

    for module, chunk_text in module_chunks:
        if module == current_key:
            continue

        compact_text = compact_identifier(chunk_text)
        score = 0
        matched_codes = set()

        for base, canonical_codes in base_map.items():
            b = base.lower()
            base_hit = b in compact_text
            sw_hit = (b + 'sw') in compact_text
            devnt_hit = ('devnt' + b) in compact_text

            if base_hit:
                score += 1
            if sw_hit:
                score += 2
            if devnt_hit:
                score += 2

            if base_hit or sw_hit or devnt_hit:
                matched_codes.update(canonical_codes)

        if score > 0 and matched_codes:
            scored.append((score, module, matched_codes))

    scored.sort(key=lambda item: (-item[0], item[1]))
    result = {}
    for _, module, matched_codes in scored[:max_candidates]:
        result[module] = set(matched_codes)
    return result


def build_related_module_context(module_file, current_module, selected_dtc_codes, source_c_file='', uds_mode=None):
    if not module_file or not selected_dtc_codes:
        return '', []

    uds_mode = normalize_uds_mode(uds_mode)
    selected_dtc_codes = filter_dtc_codes_by_uds_mode(selected_dtc_codes, uds_mode)
    if not selected_dtc_codes:
        return '', []

    module_dir = os.path.dirname(module_file)
    pack_dir = os.path.dirname(module_dir)
    current_key = (current_module or '').strip().upper()
    max_modules = 8

    dtc_list_file = find_dtc_list_file(module_file)
    dtc_module_map = parse_dtc_module_map_from_list(dtc_list_file) if dtc_list_file else {}

    related_modules = {}

    for dtc_code, modules in dtc_module_map.items():
        if not dtc_code_related(dtc_code, selected_dtc_codes, uds_mode):
            continue
        for module in modules:
            if module == current_key:
                continue
            related_modules.setdefault(module, set()).add(dtc_code)

    if len(related_modules) < max_modules:
        fallback_candidates = discover_related_modules_from_source(
            pack_dir,
            current_key,
            selected_dtc_codes,
            max_candidates=max_modules,
        )
        for module, dtcs in fallback_candidates.items():
            if module == current_key:
                continue
            related_modules.setdefault(module, set()).update(dtcs)

    if len(related_modules) < max_modules and source_c_file:
        source_candidates = discover_related_modules_from_c_source(
            source_c_file,
            current_key,
            selected_dtc_codes,
            max_candidates=max_modules,
        )
        for module, dtcs in source_candidates.items():
            if module == current_key:
                continue
            related_modules.setdefault(module, set()).update(dtcs)

    if not related_modules:
        return '', []

    blocks = []
    included_modules = []

    ordered_modules = sorted(
        related_modules.keys(),
        key=lambda module: (-len(related_modules.get(module, set())), module),
    )[:max_modules]

    for module in ordered_modules:
        module_file_path = os.path.join(pack_dir, module, f'{module}.txt')
        shared_dtcs = sorted(related_modules[module])
        if not os.path.exists(module_file_path):
            extracted_path = None
            if source_c_file and os.path.isfile(source_c_file):
                extracted_path, _ = extract_module_code(source_c_file, module)
            if extracted_path and os.path.exists(extracted_path):
                module_file_path = extracted_path
            else:
                blocks.append(
                    f'[Related Module: {module}]\n'
                    f'- Shared DTCs: {", ".join(shared_dtcs)}\n'
                    '- Source file not found in extracted module folder (auto-extract unavailable).\n'
                )
                included_modules.append(module)
                continue

        try:
            with open(module_file_path, 'r', encoding='utf-8', errors='replace') as f:
                module_code = f.read()
        except OSError:
            continue

        excerpt = extract_dtc_line_excerpt(module_code, selected_dtc_codes)
        if not excerpt:
            excerpt = [line.rstrip('\n') for line in module_code.splitlines()[:80]]
            excerpt = [f'L{idx + 1}: {line}' for idx, line in enumerate(excerpt)]

        excerpt_text = '\n'.join(excerpt[:120])
        blocks.append(
            f'[Related Module: {module}]\n'
            f'- Shared DTCs: {", ".join(shared_dtcs)}\n'
            f'- Source: {module_file_path}\n'
            '[Relevant Code Excerpt]\n'
            f'{excerpt_text}\n'
        )
        included_modules.append(module)

    if not blocks:
        return '', []

    source_line = dtc_list_file if dtc_list_file else '(not found)'
    context = (
        f'[Cross-Module DTC Context]\n'
        f'- DTC list source: {source_line}\n'
        f'- Fallback source scan: enabled\n'
        f'- Source C auto-extract: {"enabled" if source_c_file else "disabled"}\n'
        f'- Current module: {current_key or "(unknown)"}\n\n'
        + '\n'.join(blocks)
    )
    return context, included_modules


def parse_cal_data(filepath):
    """Parse cal data file (CSV or ATI Vision TXT report). Returns list of (name, value)."""
    entries = []
    ext = os.path.splitext(filepath)[1].lower()

    def clean_name(value):
        return value.strip().lstrip('\ufeff')

    def is_metadata_entry(name, value):
        key = clean_name(name).lower()
        val = value.strip()

        if key in {'lookup', 'lookups', 'scalar', 'array', 'table', 'value'}:
            return True
        if key == 'copyright':
            return True
        if key == 'distance':
            return True
        if key == 'translate':
            return True
        if key == 'x' and val.upper() == 'Y':
            return True
        if key == 'y' and val.startswith('\\  X'):
            return True
        return False

    if ext == '.csv':
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace', newline='') as f:
                rows = [row for row in csv.reader(f) if row]
        except Exception:
            return entries

        if not rows:
            return entries

        header = [cell.strip() for cell in rows[0]]
        header_lower = [cell.strip().lower() for cell in rows[0]]
        data_rows = rows

        if header_lower[:3] == ['category', 'parameter', 'value']:
            data_rows = rows[1:]
            for row in data_rows:
                if len(row) < 3:
                    continue
                name = clean_name(row[1])
                value = row[2].strip()
                if name.strip('-') == '':
                    continue
                if name and re.match(r'^[A-Za-z_]', name) and not is_metadata_entry(name, value):
                    entries.append((name, value))
            return entries

        if header_lower[:3] == ['category', 'item', 'current']:
            data_rows = rows[1:]
            for row in data_rows:
                if len(row) < 3:
                    continue
                name = clean_name(row[1])
                value = row[2].strip()
                if name.strip('-') == '':
                    continue
                if name and re.match(r'^[A-Za-z_]', name) and not is_metadata_entry(name, value):
                    entries.append((name, value))
            return entries

        if len(header) >= 2 and header_lower[0] == 'parameter' and header_lower[1] == 'value':
            data_rows = rows[1:]

        for row in data_rows:
            if len(row) < 2:
                continue
            name = clean_name(row[0])
            value = row[1].strip()
            if name.strip('-') == '':
                continue
            if name and re.match(r'^[A-Za-z_]', name) and not is_metadata_entry(name, value):
                entries.append((name, value))
    else:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            return entries

        # ATI Vision report or whitespace-delimited TXT
        sep_count = 0
        i = 0
        n = len(lines)
        while i < n:
            s = lines[i].strip()
            if re.match(r'^={5,}', s):
                sep_count += 1
                i += 1
                continue
            if sep_count < 2:
                i += 1
                continue
            if not s:
                i += 1
                continue
            if re.match(r'^(Scalar|Array|Value)\b', s, re.IGNORECASE):
                i += 1
                continue
            table_match = re.match(r'^(\d+)-D Table\s*:\s*(.*)$', s)
            if table_match:
                name, value, i = parse_ati_table_block(lines, i, int(table_match.group(1)))
                if name and value:
                    entries.append((name, value))
                continue
            m = re.match(r'^([A-Za-z_]\S*?)\s{2,}(\S.*)?$', s)
            if m:
                name  = m.group(1).strip()
                value = (m.group(2) or '').strip()
                if name and not is_metadata_entry(name, value):
                    entries.append((name, value))
            i += 1
    return entries


def parse_ati_table_block(lines, start_idx, dims):
    """Parse an ATI Vision '<N>-D Table:' block (1D curve or 2D map) starting at lines[start_idx].

    Returns (name, serialized_value, next_index). serialized_value encodes breakpoints and
    data so downstream CSV/report code can store and later re-display the full table, not
    just the parameter name.
    """
    n = len(lines)
    header = lines[start_idx].strip()
    m = re.match(r'^(\d+)-D Table\s*:\s*(.*)$', header)
    if not m:
        return None, None, start_idx + 1
    dims = int(m.group(1))
    inline_name = m.group(2).strip()

    label_keys = ['Description', 'Last modified', 'Source', 'Comments', 'X-Axis', 'Y-Axis']
    if dims >= 3:
        label_keys.append('Z-Axis')

    idx = start_idx + 1
    for label in label_keys:
        if idx >= n:
            return None, None, start_idx + 1
        raw = lines[idx].strip()
        if not re.match(r'^' + re.escape(label) + r'\s*:\s*.*$', raw):
            return None, None, start_idx + 1
        idx += 1

    name = inline_name
    if not name:
        # Wrapped mode: name and metadata values appear as bare lines (in label order)
        # after a blank separator, because the identifier was too long to fit inline.
        while idx < n and lines[idx].strip() == '':
            idx += 1
        wrapped_count = 1 + len(label_keys)
        if idx + wrapped_count - 1 >= n:
            return None, None, start_idx + 1
        name = lines[idx].strip()
        idx += wrapped_count

    while idx < n and lines[idx].strip() == '':
        idx += 1
    if dims < 3 and idx < n:
        idx += 1  # skip the "X   Y" column-header row (2D header carries no data;
        # 3D header row carries X breakpoints and is parsed below instead).

    data_rows = []
    while idx < n:
        raw = lines[idx].strip()
        if raw == '' or re.match(r'^\d+-D Table\s*:', raw) or re.match(r'^={5,}', raw):
            break
        data_rows.append(raw)
        idx += 1

    if not name or not data_rows:
        return (name or None), None, idx

    if dims >= 3:
        x_values, y_values, z_rows = [], [], []
        header_seen = False
        for raw in data_rows:
            cells = raw.split()
            if not cells:
                continue
            if not header_seen and cells[0] == 'Y':
                # Header row: "Y  \  X   x0  x1  x2 ..." -> take everything after the 'X' marker.
                header_seen = True
                x_idx = cells.index('X') if 'X' in cells else 0
                x_values = cells[x_idx + 1:]
                continue
            y_values.append(cells[0])
            z_rows.append(cells[1:])
        value = 'X=[{}]; Y=[{}]; Z=[{}]'.format(
            ', '.join(x_values),
            ', '.join(y_values),
            '; '.join('[{}]'.format(', '.join(row)) for row in z_rows),
        )
    else:
        x_values, y_values = [], []
        for raw in data_rows:
            cells = raw.split()
            if len(cells) < 2:
                continue
            x_values.append(cells[0])
            y_values.append(cells[1])
        value = 'X=[{}]; Y=[{}]'.format(', '.join(x_values), ', '.join(y_values))

    return name, value, idx


def escape_markdown_table_cell(value):
    text = str(value or '').replace('\n', ' ').replace('\r', ' ').strip()
    text = re.sub(r'\s+', ' ', text)
    return text.replace('|', '\\|')


def build_markdown_table(rows, columns):
    if not rows:
        return ''

    header = '| ' + ' | '.join(title for title, _key in columns) + ' |'
    separator = '| ' + ' | '.join('---' for _title, _key in columns) + ' |'
    lines = [header, separator]

    for row in rows:
        lines.append('| ' + ' | '.join(
            escape_markdown_table_cell(row.get(key, '')) for _title, key in columns
        ) + ' |')

    return '\n'.join(lines)


def infer_calibration_shape(parameter_name, category=''):
    text = str(parameter_name or '').strip()
    cat = str(category or '').strip().upper()

    if 'ARRAY' in cat:
        return 'ARRAY1D'
    if 'TABLE' in cat:
        return 'TABLE2D'

    if not text:
        return 'SCALAR'

    bracket_count = len(re.findall(r'\[[^\]]*\]', text))
    if bracket_count >= 2:
        return 'TABLE2D'
    if bracket_count == 1:
        return 'ARRAY1D'
    return 'SCALAR'


def normalize_cal_base_parameter(parameter_name):
    text = str(parameter_name or '').strip()
    if not text:
        return ''
    return re.sub(r'\[[^\]]*\]', '', text).strip()


def infer_related_dtcs_for_parameter(parameter_name, selected_dtc_codes):
    key = compact_identifier(parameter_name or '')
    if not key:
        return []

    matched = []
    for code in selected_dtc_codes or []:
        compact = compact_dtc_code(code)
        if len(compact) < 5:
            continue
        base = compact[:5].lower()
        full = compact[:7].lower() if len(compact) >= 7 else ''
        if (full and full in key) or (base in key) or (key.endswith(base + 'sw')):
            formatted = format_uds_dtc(code)
            if formatted not in matched:
                matched.append(formatted)
    return matched


def build_calibration_prompt_payload(
    cal_file,
    max_rows=1200,
    row_offset=0,
    with_meta=False,
    selected_dtc_codes=None,
    dtc_owner_lookup=None,
    current_module='',
    prioritize_dtc_rows=True,
):
    try:
        max_rows = max(int(max_rows), 1)
    except Exception:
        max_rows = 1200

    try:
        row_offset = max(int(row_offset), 0)
    except Exception:
        row_offset = 0

    empty_meta = {
        'total_rows': 0,
        'included_rows': 0,
        'omitted_rows': 0,
        'row_offset': row_offset,
        'next_offset': row_offset,
        'has_more': False,
        'dtc_rows': 0,
        'other_rows': 0,
    }

    if not cal_file:
        if with_meta:
            return '(No calibration data provided)', '(No calibration data provided)', empty_meta
        return '(No calibration data provided)', '(No calibration data provided)'

    if not os.path.exists(cal_file):
        message = f'(Calibration file not found: {cal_file})'
        if with_meta:
            return message, message, empty_meta
        return message, message

    ext = os.path.splitext(cal_file)[1].lower()
    if ext not in {'.csv', '.txt'}:
        try:
            with open(cal_file, 'r', encoding='utf-8', errors='replace') as f:
                raw_text = f.read()[:20000]
        except Exception as e:
            raw_text = f'(Error reading calibration file: {str(e)})'

        summary_lines = [
            f'- Source file: {cal_file}',
            f'- Format: {ext or "unknown"}',
            '- Note: Unsupported structured calibration format in this app; use CSV/TXT or convert upstream.',
        ]
        if with_meta:
            return '\n'.join(summary_lines), raw_text, empty_meta
        return '\n'.join(summary_lines), raw_text

    rows = []
    header = []
    header_lower = []
    data_rows = []
    if ext == '.csv':
        try:
            with open(cal_file, 'r', encoding='utf-8', errors='replace', newline='') as f:
                rows = [row for row in csv.reader(f) if row]
        except Exception as e:
            message = f'(Error reading calibration CSV: {str(e)})'
            if with_meta:
                return message, message, empty_meta
            return message, message

        if not rows:
            summary = f'- Source calibration file: {cal_file}\n- Parsed calibration rows: 0'
            if with_meta:
                return summary, '(Calibration CSV is empty)', empty_meta
            return summary, '(Calibration CSV is empty)'

        header = [cell.strip() for cell in rows[0]]
        header_lower = [cell.strip().lower() for cell in rows[0]]
        data_rows = rows[1:] if header else rows

    def value_at(row, idx):
        return row[idx].strip() if idx >= 0 and len(row) > idx else ''

    def is_dash_separator_row(row_dict):
        values = [
            row_dict.get('category', ''),
            row_dict.get('parameter', ''),
            row_dict.get('value', ''),
            row_dict.get('default', ''),
            row_dict.get('unit', ''),
            row_dict.get('role', ''),
            row_dict.get('condition', ''),
            row_dict.get('note', ''),
            row_dict.get('description', ''),
        ]
        non_empty = [str(v).strip() for v in values if str(v).strip()]
        return bool(non_empty) and all(v.strip('-: ') == '' for v in non_empty)

    def has_any(rows_list, key):
        return any(str(row.get(key, '')).strip() for row in rows_list)

    parsed_rows = []
    if ext == '.txt':
        for name, value in unique_cal_entries(parse_cal_data(cal_file)):
            parsed_rows.append({
                'category': '',
                'parameter': name.strip(),
                'value': str(value).strip(),
                'default': '',
                'unit': '',
                'role': '',
                'condition': '',
                'note': '',
                'description': '',
            })
    elif header_lower[:3] == ['category', 'parameter', 'value']:
        default_idx = next((idx for idx, col in enumerate(header_lower) if col == 'default'), -1)
        unit_idx = next((idx for idx, col in enumerate(header_lower) if col == 'unit'), -1)
        desc_idx = next((idx for idx, col in enumerate(header_lower) if col == 'description'), -1)
        for row in data_rows:
            if len(row) < 3:
                continue
            parsed_rows.append({
                'category': row[0].strip(),
                'parameter': row[1].strip(),
                'value': row[2].strip(),
                'default': value_at(row, default_idx),
                'unit': value_at(row, unit_idx),
                'role': '',
                'condition': '',
                'note': '',
                'description': value_at(row, desc_idx),
            })
    elif header_lower[:3] == ['category', 'item', 'current']:
        default_idx = next((idx for idx, col in enumerate(header_lower) if col == 'default'), -1)
        unit_idx = next((idx for idx, col in enumerate(header_lower) if col == 'unit'), -1)
        role_idx = next((idx for idx, col in enumerate(header_lower) if col == 'role'), -1)
        condition_idx = next((idx for idx, col in enumerate(header_lower) if col == 'condition'), -1)
        note_idx = next((idx for idx, col in enumerate(header_lower) if col == 'note'), -1)
        desc_idx = next((idx for idx, col in enumerate(header_lower) if col == 'description'), -1)
        for row in data_rows:
            if len(row) < 3:
                continue
            parsed_rows.append({
                'category': row[0].strip(),
                'parameter': row[1].strip(),
                'value': row[2].strip(),
                'default': value_at(row, default_idx),
                'unit': value_at(row, unit_idx),
                'role': value_at(row, role_idx),
                'condition': value_at(row, condition_idx),
                'note': value_at(row, note_idx),
                'description': value_at(row, desc_idx),
            })
    elif len(header) >= 2 and header_lower[0] == 'parameter' and header_lower[1] == 'value':
        unit_idx = next((idx for idx, col in enumerate(header_lower) if col == 'unit'), -1)
        desc_idx = next((idx for idx, col in enumerate(header_lower) if col == 'description'), -1)
        for row in data_rows:
            if len(row) < 2:
                continue
            parsed_rows.append({
                'category': '',
                'parameter': row[0].strip(),
                'value': row[1].strip(),
                'default': '',
                'unit': value_at(row, unit_idx),
                'role': '',
                'condition': '',
                'note': '',
                'description': value_at(row, desc_idx),
            })
    else:
        for row in data_rows:
            if len(row) < 2:
                continue
            parsed_rows.append({
                'category': row[0].strip() if len(row) > 0 else '',
                'parameter': row[0].strip(),
                'value': row[1].strip(),
                'default': '',
                'unit': row[2].strip() if len(row) > 2 else '',
                'role': '',
                'condition': '',
                'note': '',
                'description': row[3].strip() if len(row) > 3 else '',
            })

    normalized_rows = []
    for item in parsed_rows:
        if not item.get('parameter', '').strip():
            continue
        if item.get('parameter', '').strip('-') == '':
            continue
        if is_dash_separator_row(item):
            continue
        normalized_rows.append(item)

    parsed_rows = normalized_rows

    filtered_rows = []
    for item in parsed_rows:
        category = item.get('category', '').strip().upper()
        parameter = item.get('parameter', '')
        value = item.get('value', '')
        if category == 'DTC_SW' or get_dtc_code_from_switch_parameter(parameter):
            if is_sw_unused(value) or is_sw_zero(value):
                continue
        filtered_rows.append(item)

    parsed_rows = filtered_rows

    selected_scope_dtcs = [format_uds_dtc(code) for code in (selected_dtc_codes or []) if compact_dtc_code(code)]
    dtc_owner_lookup = dtc_owner_lookup or {}
    default_owner = (current_module or '').strip().upper() or 'CURRENT_MODULE'

    for item in parsed_rows:
        dtc_code = get_dtc_code_from_switch_parameter(item.get('parameter', ''))
        if not dtc_code and (item.get('category', '').strip().upper() == 'DTC_SW'):
            dtc_code = item.get('parameter', '').strip()
        item['dtc'] = format_uds_dtc(dtc_code) if dtc_code else ''
        item['shape'] = infer_calibration_shape(item.get('parameter', ''), item.get('category', ''))
        item['base_parameter'] = normalize_cal_base_parameter(item.get('parameter', '')) or item.get('parameter', '')

        related_dtcs = []
        if item['dtc']:
            related_dtcs = [item['dtc']]
        else:
            related_dtcs = infer_related_dtcs_for_parameter(item.get('parameter', ''), selected_scope_dtcs)
            if not related_dtcs and selected_scope_dtcs:
                related_dtcs = selected_scope_dtcs[:]

        item['related_dtcs'] = ', '.join(related_dtcs) if related_dtcs else ''

        owner_modules = []
        for dtc in related_dtcs:
            owners = dtc_owner_lookup.get(dtc) or dtc_owner_lookup.get(compact_dtc_code(dtc)[:5]) or []
            for owner in owners:
                owner_key = (owner or '').strip().upper()
                if owner_key and owner_key not in owner_modules:
                    owner_modules.append(owner_key)
        if not owner_modules and related_dtcs:
            owner_modules = [default_owner]
        item['dtc_owner_module'] = ', '.join(owner_modules)

    dtc_rows = [row for row in parsed_rows if row.get('category', '').strip().upper() == 'DTC_SW' or row.get('dtc')]
    other_rows = [row for row in parsed_rows if row not in dtc_rows]
    ordered_rows = (dtc_rows + other_rows) if prioritize_dtc_rows else (other_rows + dtc_rows)
    selected_rows = ordered_rows[row_offset: row_offset + max_rows]
    shown_dtc_rows = [
        row for row in selected_rows
        if row.get('category', '').strip().upper() == 'DTC_SW' or row.get('dtc')
    ]
    shown_other_rows = [
        row for row in selected_rows
        if not (row.get('category', '').strip().upper() == 'DTC_SW' or row.get('dtc'))
    ]
    shown_total = len(selected_rows)
    omitted_rows = max(len(ordered_rows) - (row_offset + shown_total), 0)

    summary_parts = [
        f'- Source calibration file: {cal_file}',
        f'- Format: {ext.upper().lstrip(".") or "unknown"}',
        f'- Total parsed rows: {len(ordered_rows)}',
        f'- DTC_SW rows: {len(dtc_rows)}',
        f'- Other calibration rows: {len(other_rows)}',
        f'- Row offset used: {row_offset}',
        f'- Rows included in prompt: {shown_total}',
        f'- Rows omitted from prompt: {omitted_rows}',
        '- Use this CSV as the primary source for current project values.',
    ]

    shape_counts = {
        'SCALAR': sum(1 for row in ordered_rows if row.get('shape') == 'SCALAR'),
        'ARRAY1D': sum(1 for row in ordered_rows if row.get('shape') == 'ARRAY1D'),
        'TABLE2D': sum(1 for row in ordered_rows if row.get('shape') == 'TABLE2D'),
    }
    summary_parts.append(
        f'- Calibration type coverage: SCALAR={shape_counts["SCALAR"]}, '
        f'ARRAY1D={shape_counts["ARRAY1D"]}, TABLE2D={shape_counts["TABLE2D"]}'
    )

    table_rows = [row for row in ordered_rows if row.get('shape') in {'ARRAY1D', 'TABLE2D'}]
    def build_compact_bullet_lines(rows_list, heading, reference_only=False):
        if not rows_list:
            return []

        lines = [f'\n### {heading}']
        if reference_only:
            lines.append('- These DTC/SW rows are reference-only in non-OBD runs; do not let them override control-path classification.')

        for row in rows_list:
            label = row.get('parameter', '').strip() or '(unnamed parameter)'
            details = [f'current={row.get("value", "").strip() or "N/A"}']
            if row.get('default', '').strip():
                details.append(f'default={row.get("default", "").strip()}')
            if row.get('unit', '').strip():
                details.append(f'unit={row.get("unit", "").strip()}')
            if row.get('related_dtcs', '').strip():
                details.append(f'related_dtc={row.get("related_dtcs", "").strip()}')
            if row.get('dtc_owner_module', '').strip():
                details.append(f'owner_module={row.get("dtc_owner_module", "").strip()}')
            if row.get('role', '').strip():
                details.append(f'role={row.get("role", "").strip()}')
            if row.get('condition', '').strip():
                details.append(f'condition={row.get("condition", "").strip()}')
            if row.get('note', '').strip():
                details.append(f'note={row.get("note", "").strip()}')
            if row.get('description', '').strip():
                details.append(f'description={row.get("description", "").strip()}')
            lines.append(f'- `{label}`: ' + '; '.join(details))

        return lines

    if table_rows:
        grouped = {}
        for row in table_rows:
            key = (row.get('base_parameter', ''), row.get('shape', ''))
            bucket = grouped.setdefault(key, {
                'base_parameter': row.get('base_parameter', ''),
                'shape': row.get('shape', ''),
                'row_count': 0,
                'example_item': '',
            })
            bucket['row_count'] += 1
            if not bucket['example_item']:
                bucket['example_item'] = row.get('parameter', '')

        grouped_rows = sorted(grouped.values(), key=lambda row: (row.get('shape', ''), row.get('base_parameter', '')))
        summary_parts.append('\n### Array / 2-Axis Calibration Coverage')
        for row in grouped_rows:
            summary_parts.append(
                f'- `{row.get("base_parameter", "")}`: '
                f'shape={row.get("shape", "")}; rows={row.get("row_count", 0)}; '
                f'example={row.get("example_item", "") or "N/A"}'
            )

    if shown_other_rows:
        summary_parts.extend(build_compact_bullet_lines(shown_other_rows, 'Key Calibration Rows'))

    if shown_dtc_rows:
        dtc_heading = 'DTC/SW Rows from Calibration' if prioritize_dtc_rows else 'Diagnostic Switch Rows from Calibration'
        summary_parts.extend(build_compact_bullet_lines(shown_dtc_rows, dtc_heading, reference_only=not prioritize_dtc_rows))

    raw_preview = '(Raw calibration preview omitted to preserve token budget. Use the structured calibration bullets above.)'
    summary_text = '\n'.join(part for part in summary_parts if part)
    payload_meta = {
        'total_rows': len(ordered_rows),
        'included_rows': shown_total,
        'omitted_rows': omitted_rows,
        'row_offset': row_offset,
        'next_offset': row_offset + shown_total,
        'has_more': omitted_rows > 0,
        'dtc_rows': len(dtc_rows),
        'other_rows': len(other_rows),
    }

    if with_meta:
        return summary_text, raw_preview, payload_meta
    return summary_text, raw_preview


def normalize_cal_key(name):
    return re.sub(r'\[.*?\]', '', name).strip().lower()


def parse_metadata_comment(raw_comment):
    if not raw_comment:
        return '', ''

    text = raw_comment.strip()
    text = re.sub(r'^/\*+\s*', '', text)
    text = re.sub(r'\s*\*/$', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return '', ''

    description = text.rstrip('.')
    unit = ''
    if ',' in text:
        head, tail = text.rsplit(',', 1)
        candidate = tail.strip().rstrip('.')
        if candidate and len(candidate) <= 24 and re.fullmatch(r'[A-Za-z0-9%/_\- ]+', candidate):
            description = head.strip().rstrip('.')
            unit = candidate

    return description, unit


def normalize_default_value(raw_value):
    text = (raw_value or '').strip()
    if not text:
        return ''

    text = re.sub(r'\s+', ' ', text)

    # Normalize common C numeric suffixes to plain numeric text for report tables.
    m = re.fullmatch(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)([fFlLuU]+)', text)
    if m:
        return m.group(1)

    return text


def parse_nearby_comment(lines, decl_index):
    collected = []
    lookback = 12

    for idx in range(decl_index - 1, max(-1, decl_index - lookback - 1), -1):
        stripped = lines[idx].strip()

        if not stripped:
            if collected:
                break
            continue

        if stripped.startswith('//'):
            collected.insert(0, stripped)
            continue

        if stripped.startswith('/*') or stripped.startswith('*') or stripped.startswith('*/'):
            collected.insert(0, stripped)
            continue

        break

    if not collected:
        return '', ''

    comment_text = ' '.join(collected)
    comment_text = re.sub(r'//+', '', comment_text)
    comment_text = re.sub(r'/\*+', '', comment_text)
    comment_text = re.sub(r'\*/', '', comment_text)
    comment_text = re.sub(r'\s*\*\s*', ' ', comment_text)
    comment_text = re.sub(r'\s+', ' ', comment_text).strip()

    return parse_metadata_comment(comment_text)


def extract_parameter_metadata(module_content, parameter_names):
    wanted = {normalize_cal_key(name) for name in parameter_names if normalize_cal_key(name)}
    if not wanted:
        return {}

    metadata = {}
    declaration_pattern = re.compile(
        r'^\s*(?:extern\s+)?CAL\s+([A-Za-z0-9_]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]+\])?\s*(?:=\s*([^;]+))?;\s*(/\*.*?\*/|//.*)?',
        re.IGNORECASE,
    )

    lines = module_content.splitlines()
    for line_idx, line in enumerate(lines):
        match = declaration_pattern.match(line)
        if not match:
            continue

        dtype, name, raw_default, raw_comment = match.groups()
        key = normalize_cal_key(name)
        if key not in wanted:
            continue

        description, unit = parse_metadata_comment(raw_comment or '')
        if not description or not unit:
            near_desc, near_unit = parse_nearby_comment(lines, line_idx)
            if not description and near_desc:
                description = near_desc
            if not unit and near_unit:
                unit = near_unit

        candidate = {
            'description': description,
            'unit': unit,
            'dtype': (dtype or '').strip(),
            'default': normalize_default_value(raw_default),
        }

        existing = metadata.get(key)
        if existing:
            should_replace = False
            if not existing.get('default') and candidate.get('default'):
                should_replace = True
            elif not existing.get('description') and candidate.get('description'):
                should_replace = True
            elif not existing.get('unit') and candidate.get('unit'):
                should_replace = True

            if not should_replace:
                continue

            metadata[key] = {
                'description': candidate.get('description') or existing.get('description', ''),
                'unit': candidate.get('unit') or existing.get('unit', ''),
                'dtype': candidate.get('dtype') or existing.get('dtype', ''),
                'default': candidate.get('default') or existing.get('default', ''),
            }
            continue

        metadata[key] = candidate

    return metadata


def format_calibration_display_value(raw_value):
    text = str(raw_value or '').strip()
    if not text:
        return ''

    normalized = normalize_default_value(text)
    if re.fullmatch(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', normalized):
        try:
            numeric = float(normalized)
            if numeric.is_integer():
                return str(int(numeric))
            return ('%.6f' % numeric).rstrip('0').rstrip('.')
        except Exception:
            return normalized

    return normalized


def build_simple_calibration_lookup(cal_file):
    lookup = {}
    if not cal_file or not os.path.exists(cal_file):
        return lookup

    ext = os.path.splitext(cal_file)[1].lower()
    if ext == '.csv':
        try:
            with open(cal_file, 'r', encoding='utf-8', errors='replace', newline='') as handle:
                rows = [row for row in csv.reader(handle) if row]
        except Exception:
            rows = []

        if rows:
            header = [cell.strip().lower() for cell in rows[0]]
            data_rows = rows[1:]
            if header[:3] == ['category', 'item', 'current']:
                unit_idx = next((idx for idx, col in enumerate(header) if col == 'unit'), -1)
                for row in data_rows:
                    if len(row) < 3:
                        continue
                    name = row[1].strip()
                    key = normalize_cal_key(name)
                    if not key or key in lookup:
                        continue
                    unit = row[unit_idx].strip() if unit_idx >= 0 and len(row) > unit_idx else ''
                    lookup[key] = {
                        'name': normalize_cal_base_parameter(name) or name,
                        'value': format_calibration_display_value(row[2].strip()),
                        'unit': unit,
                    }
            elif header[:3] == ['category', 'parameter', 'value']:
                unit_idx = next((idx for idx, col in enumerate(header) if col == 'unit'), -1)
                for row in data_rows:
                    if len(row) < 3:
                        continue
                    name = row[1].strip()
                    key = normalize_cal_key(name)
                    if not key or key in lookup:
                        continue
                    unit = row[unit_idx].strip() if unit_idx >= 0 and len(row) > unit_idx else ''
                    lookup[key] = {
                        'name': normalize_cal_base_parameter(name) or name,
                        'value': format_calibration_display_value(row[2].strip()),
                        'unit': unit,
                    }

    try:
        entries = unique_cal_entries(parse_cal_data(cal_file))
    except Exception:
        return lookup

    for name, value in entries:
        key = normalize_cal_key(name)
        if not key or key in lookup:
            continue
        lookup[key] = {
            'name': normalize_cal_base_parameter(name) or str(name).strip(),
            'value': format_calibration_display_value(value),
            'unit': '',
        }

    return lookup


def strip_c_style_comments(text):
    without_block = re.sub(r'/\*.*?\*/', '', str(text or ''), flags=re.DOTALL)
    without_line = re.sub(r'//.*', '', without_block)
    without_preprocessor = re.sub(r'(?m)^\s*#.*$', '', without_line)
    return without_preprocessor


def strip_wrapping_parentheses(text):
    value = str(text or '').strip()
    while value.startswith('(') and value.endswith(')'):
        depth = 0
        balanced = True
        for idx, ch in enumerate(value):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and idx != len(value) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def split_top_level_boolean_clauses(expression, operator='&&'):
    parts = []
    chunk = []
    depth = 0
    idx = 0
    source = str(expression or '')
    op_len = len(operator)

    while idx < len(source):
        if source[idx] == '(':
            depth += 1
            chunk.append(source[idx])
            idx += 1
            continue
        if source[idx] == ')':
            depth = max(depth - 1, 0)
            chunk.append(source[idx])
            idx += 1
            continue
        if depth == 0 and source[idx:idx + op_len] == operator:
            part = ''.join(chunk).strip()
            if part:
                parts.append(part)
            chunk = []
            idx += op_len
            continue
        chunk.append(source[idx])
        idx += 1

    tail = ''.join(chunk).strip()
    if tail:
        parts.append(tail)
    return parts


def strip_preprocessor_guarded_lines(statement_lines):
    cleaned = []
    skip_depth = 0
    for raw_line in statement_lines:
        stripped = raw_line.strip()
        if stripped.startswith('#if'):
            skip_depth += 1
            continue
        if stripped.startswith('#endif'):
            skip_depth = max(skip_depth - 1, 0)
            continue
        if stripped.startswith('#else') or stripped.startswith('#elif'):
            continue
        if skip_depth:
            continue
        cleaned.append(raw_line)
    return cleaned


def parse_simple_logic_comparison(clause_text):
    text = strip_wrapping_parentheses(re.sub(r'\s+', ' ', str(clause_text or '')).strip())
    operand = r'(?:[A-Za-z_][A-Za-z0-9_\[\]]*|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    match = re.fullmatch(rf'({operand})\s*(<=|>=|==|!=|<|>)\s*({operand})', text)
    if not match:
        return None
    return {
        'left': match.group(1),
        'op': match.group(2),
        'right': match.group(3),
    }


def merge_adjacent_range_clauses(clauses):
    merged = []
    idx = 0

    while idx < len(clauses):
        current = clauses[idx]
        current_cmp = parse_simple_logic_comparison(current)

        if current_cmp and idx + 1 < len(clauses):
            following = clauses[idx + 1]
            following_cmp = parse_simple_logic_comparison(following)
            if following_cmp and current_cmp['left'] == following_cmp['left']:
                lower_cmp = None
                upper_cmp = None
                if current_cmp['op'] in ('>', '>=') and following_cmp['op'] in ('<', '<='):
                    lower_cmp = current_cmp
                    upper_cmp = following_cmp
                elif current_cmp['op'] in ('<', '<=') and following_cmp['op'] in ('>', '>='):
                    lower_cmp = following_cmp
                    upper_cmp = current_cmp

                if lower_cmp and upper_cmp:
                    lower_symbol = '<' if lower_cmp['op'] == '>' else '<='
                    upper_symbol = '<' if upper_cmp['op'] == '<' else '<='
                    merged.append(
                        f"{lower_cmp['right']} {lower_symbol} {lower_cmp['left']} {upper_symbol} {upper_cmp['right']}"
                    )
                    idx += 2
                    continue

        merged.append(current)
        idx += 1

    return merged


def annotate_logic_expression(expression_text, cal_lookup, metadata_lookup):
    text = str(expression_text or '').strip()
    if not text:
        return text

    token_pattern = re.compile(r'\b[A-Za-z_][A-Za-z0-9_\[\]]*\b')

    def replace_token(match):
        token = match.group(0)
        key = normalize_cal_key(token)
        cal_item = cal_lookup.get(key)
        meta_item = metadata_lookup.get(key, {})
        if not cal_item:
            return token

        value = cal_item.get('value', '').strip()
        unit = (cal_item.get('unit') or meta_item.get('unit') or '').strip()
        if not value:
            return token

        suffix = value if not unit else f'{value} {unit}'
        return f'{token} ({suffix})'

    if parse_simple_logic_comparison(text) is None and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\[\]]*', text):
        return text

    return token_pattern.sub(replace_token, text)


def extract_monitor_enable_expressions(module_content, cal_file=None, module_keyword=''):
    module_text = str(module_content or '')
    if not module_text.strip():
        return {'expressions': [], 'source_flag': ''}

    lines = module_text.splitlines()
    candidates = []
    lhs_keywords = ('gate', 'enbl', 'enable', 'dsbl', 'spnd', 'qual', 'rdy', 'ready')
    module_prefix = (module_keyword or '').strip().lower()

    for line_index, line in enumerate(lines):
        assign_match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_\[\]]*)\s*=\s*', line)
        if assign_match:
            lhs_name = assign_match.group(1)
            lhs_lower = lhs_name.lower()
            if not any(keyword in lhs_lower for keyword in lhs_keywords):
                continue

            statement_lines = [line]
            end_index = line_index
            while ';' not in statement_lines[-1] and end_index + 1 < len(lines) and (end_index - line_index) < 60:
                end_index += 1
                statement_lines.append(lines[end_index])

            statement_lines = strip_preprocessor_guarded_lines(statement_lines)
            if not statement_lines:
                continue

            statement = '\n'.join(statement_lines)
            if '&&' not in statement:
                continue

            rhs_text = statement.split('=', 1)[1].rsplit(';', 1)[0].strip()
            cleaned_rhs = strip_c_style_comments(rhs_text)
            cleaned_rhs = re.sub(r'\s+', ' ', cleaned_rhs).strip()
            if not cleaned_rhs:
                continue

            if cleaned_rhs.startswith('!'):
                cleaned_rhs = cleaned_rhs[1:].strip()
            cleaned_rhs = strip_wrapping_parentheses(cleaned_rhs)
            clauses = [
                strip_wrapping_parentheses(re.sub(r'\s+', ' ', clause).strip())
                for clause in split_top_level_boolean_clauses(cleaned_rhs, operator='&&')
            ]
            clauses = [clause for clause in clauses if clause]
            if len(clauses) < 3:
                continue

            score = len(clauses)
            if module_prefix and lhs_lower.startswith(module_prefix):
                score += 4
            if 'gate' in lhs_lower:
                score += 3
            if 'common' in lhs_lower:
                score += 2

            candidates.append({
                'lhs': lhs_name,
                'clauses': clauses,
                'score': score,
            })
            continue

        if_match = re.match(r'^\s*if\s*\(', line)
        if not if_match:
            continue

        condition_lines = [line]
        end_index = line_index
        paren_depth = line.count('(') - line.count(')')
        while end_index + 1 < len(lines) and (paren_depth > 0 or '{' not in ''.join(condition_lines)) and (end_index - line_index) < 60:
            end_index += 1
            next_line = lines[end_index]
            condition_lines.append(next_line)
            paren_depth += next_line.count('(') - next_line.count(')')

        condition_lines = strip_preprocessor_guarded_lines(condition_lines)
        if not condition_lines:
            continue

        condition_text = '\n'.join(condition_lines)
        if '&&' not in condition_text:
            continue

        ready_target = ''
        for body_index in range(end_index + 1, min(len(lines), end_index + 13)):
            body_line = lines[body_index]
            if re.match(r'^\s*else\b', body_line) or re.match(r'^\s*}\s*else\b', body_line):
                break
            ready_assign = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_\[\]]*)\s*=\s*(TRUE|1U|1)\s*;', body_line)
            if not ready_assign:
                continue
            candidate_name = ready_assign.group(1)
            candidate_lower = candidate_name.lower()
            if any(keyword in candidate_lower for keyword in lhs_keywords):
                ready_target = candidate_name
                break

        if not ready_target:
            continue

        condition_body = condition_text[condition_text.find('(') + 1:condition_text.rfind(')')]
        cleaned_rhs = strip_c_style_comments(condition_body)
        cleaned_rhs = re.sub(r'\s+', ' ', cleaned_rhs).strip()
        cleaned_rhs = strip_wrapping_parentheses(cleaned_rhs)
        clauses = [
            strip_wrapping_parentheses(re.sub(r'\s+', ' ', clause).strip())
            for clause in split_top_level_boolean_clauses(cleaned_rhs, operator='&&')
        ]
        clauses = [clause for clause in clauses if clause]
        if len(clauses) < 3:
            continue

        ready_lower = ready_target.lower()
        score = len(clauses) + 1
        if module_prefix and ready_lower.startswith(module_prefix):
            score += 4
        if any(keyword in ready_lower for keyword in ('rdy', 'ready')):
            score += 3

        candidates.append({
            'lhs': ready_target,
            'clauses': clauses,
            'score': score,
        })

    if not candidates:
        return {'expressions': [], 'source_flag': ''}

    selected = max(candidates, key=lambda item: item['score'])
    merged_clauses = merge_adjacent_range_clauses(selected['clauses'])

    cal_lookup = build_simple_calibration_lookup(cal_file)
    metadata_tokens = []
    for clause in merged_clauses:
        comparison = parse_simple_logic_comparison(clause)
        if comparison:
            metadata_tokens.extend([comparison['left'], comparison['right']])
        else:
            metadata_tokens.extend(REPORT_TOKEN_RE.findall(clause))
    metadata_lookup = extract_parameter_metadata(module_text, metadata_tokens)

    annotated_clauses = []
    seen = set()
    for clause in merged_clauses:
        annotated = annotate_logic_expression(clause, cal_lookup, metadata_lookup)
        normalized = re.sub(r'\s+', ' ', annotated).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            annotated_clauses.append(normalized)

    return {
        'expressions': annotated_clauses,
        'source_flag': selected['lhs'],
    }


def build_monitor_enable_prompt_block(expressions, source_flag='', cal_file=None, module_keyword=''):
    support_notes = build_monitor_enable_support_notes(expressions, cal_file=cal_file, module_keyword=module_keyword)
    lines = [
        '[DETERMINISTIC MONITOR ENABLE CONDITIONS]',
        '- The app extracted the following direct executable enable clauses from visible source code.',
        '- If you write `Monitoring enable conditions`, copy these expressions first before adding any optional short supporting notes.',
        '- Keep closely related visible calibration companions such as min/max windows, hysteresis, and inhibit timing when they explain the same executable gate family.',
    ]
    if source_flag:
        lines.append(f'- Source gate assignment: {source_flag}')
    lines.append('- Direct logic expressions:')
    lines.extend(f'  - {expr}' for expr in expressions or [])
    if support_notes:
        lines.append('- Supporting calibration notes to preserve after the direct expressions:')
        lines.extend(f'  - {note}' for note in support_notes)
    return '\n'.join(lines)


def humanize_monitor_enable_family(family_prefix, module_keyword=''):
    family = normalize_cal_key(family_prefix)
    if not family:
        return 'related gating family'

    module_prefix = normalize_cal_key(module_keyword)
    if module_prefix and family.startswith(module_prefix + '_'):
        family = family[len(module_prefix) + 1:]

    compound_map = {
        'fuel_source': 'fuel source',
        'catmd': 'catalyst temperature',
        'brkin': 'break-in',
        'noflow': 'no-flow',
        'epd': 'elapsed-time',
        'inj': 'injector',
        'lambse': 'lambda',
        'lambar': 'lambda',
    }
    token_map = {
        'ect': 'coolant temperature',
        'aat': 'ambient temperature',
        'iat': 'intake air temperature',
        'eot': 'oil temperature',
        'te': 'temperature',
        'ne': 'engine speed',
        'n': 'engine speed',
        'vs': 'vehicle speed',
        'maf': 'airflow',
        'am': 'airflow',
        'tm': 'time',
        'tc': 'time constant',
        'fuel': 'fuel',
        'source': 'source',
        'load': 'load',
        'lam': 'lambda',
        'ego': 'oxygen sensor',
        'cat': 'catalyst',
        'cms': 'oxygen storage',
        'pg': 'purge',
        'dp': 'pressure',
        'prs': 'pressure',
        'tq': 'torque',
    }

    label_parts = []
    working = family
    for compound, label in compound_map.items():
        if compound in working:
            label_parts.append(label)
            working = working.replace(compound, '')

    existing_label_text = ' '.join(label_parts)
    for token in [part for part in working.split('_') if part]:
        if token in {'min', 'max', 'hys', 'init', 'sw', 'tm', 'enbl', 'dsbl', 'flag', 'flg', 'lo', 'hi'}:
            continue
        label = token_map.get(token, token.replace('b', 'bank ') if re.fullmatch(r'b\d+', token) else token)
        if label in existing_label_text or label == 'temperature' and 'catalyst temperature' in existing_label_text:
            continue
        if label not in label_parts:
            label_parts.append(label)
            existing_label_text = ' '.join(label_parts)

    if not label_parts:
        return family.replace('_', ' ')

    compact_parts = []
    for label in label_parts[:3]:
        if compact_parts and compact_parts[-1] == label:
            continue
        compact_parts.append(label)
    return ' '.join(compact_parts).strip()


def infer_monitor_enable_family_label(family_prefix, module_keyword=''):
    family_label = humanize_monitor_enable_family(family_prefix, module_keyword=module_keyword)
    return f'Related {family_label} calibrations remain visible'


def derive_monitor_enable_family_prefix(token):
    key = normalize_cal_key(token)
    if not key:
        return ''

    parts = [part for part in key.split('_') if part]
    if len(parts) < 2:
        return key

    generic_marker_tokens = {'b', 'd', 'm', 'bm', 'st', 'flg', 'flag', 'k'}
    if len(parts) >= 3 and parts[1] in generic_marker_tokens:
        if len(parts) >= 4 and parts[2] not in generic_marker_tokens:
            return '_'.join([parts[0], parts[2]])
        return ''

    qualifier_tokens = {'min', 'max', 'hys', 'init', 'tm', 'sw', 'odolim', 'temp', 'cp', 'pp', 'vs', 'enbl'}
    for index in range(len(parts) - 1, 1, -1):
        if parts[index] in qualifier_tokens:
            return '_'.join(parts[:index])

    if 'brkin' in parts:
        return '_'.join(parts[:parts.index('brkin') + 1])
    if len(parts) >= 4 and parts[1] == 'fuel' and parts[2] == 'source':
        return '_'.join(parts[:4])
    if len(parts) >= 3 and parts[1] == 'te':
        return '_'.join(parts[:3])
    if len(parts) >= 3:
        return '_'.join(parts[:2])
    return '_'.join(parts[:2])


def build_monitor_enable_support_notes(expressions, cal_file=None, module_keyword=''):
    cal_lookup = build_simple_calibration_lookup(cal_file)
    if not expressions or not cal_lookup:
        return []

    expression_text = ' '.join(str(expr or '') for expr in expressions)
    expression_text_normalized = normalize_cal_key(expression_text)
    raw_tokens = REPORT_TOKEN_RE.findall(expression_text)
    family_candidates = []

    for raw_token in raw_tokens:
        family_prefix = derive_monitor_enable_family_prefix(raw_token)
        if family_prefix:
            family_candidates.append(family_prefix)

        normalized_token = normalize_cal_key(raw_token)
        token_parts = [part for part in normalized_token.split('_') if part]
        if 'brkin' in token_parts:
            family_candidates.append('_'.join(token_parts[:token_parts.index('brkin') + 1]))

    module_prefix = normalize_cal_key(module_keyword)
    if module_prefix and 'brkin' in expression_text.lower():
        family_candidates.append(f'{module_prefix}_catbrkin')

    unique_candidates = []
    seen_candidates = set()
    for family_prefix in family_candidates:
        if not family_prefix or family_prefix in seen_candidates:
            continue
        seen_candidates.add(family_prefix)
        unique_candidates.append(family_prefix)

    notes = []
    seen_notes = set()
    interesting_tail_order = {
        'min': 0,
        'max': 1,
        'hys_init': 2,
        'hys': 3,
        'tm': 4,
        'sw': 5,
        'odolim': 6,
        'temp': 7,
    }

    def is_interesting_companion_entry(key, item):
        suffix = key[len(family_prefix) + 1:] if key.startswith(family_prefix + '_') else key
        normalized_suffix = suffix.replace('_s', '').replace('_v', '')
        if normalized_suffix in interesting_tail_order:
            return True
        unit = (item.get('unit') or '').strip().lower()
        if unit and unit != 'bool':
            return True
        value = str(item.get('value') or '').strip()
        return bool(re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value))

    def expression_already_mentions_key(key):
        normalized_key = normalize_cal_key(key)
        return bool(normalized_key and normalized_key in expression_text_normalized)

    def build_family_note_label(family_prefix):
        family_label = humanize_monitor_enable_family(family_prefix, module_keyword=module_keyword)
        return f'Related {family_label} companion items not already shown above'

    for family_prefix in unique_candidates:
        related_entries = []
        for key, item in cal_lookup.items():
            if key == family_prefix or not key.startswith(family_prefix + '_'):
                continue
            related_entries.append((key, item))

        if len(related_entries) < 1:
            continue

        related_entries.sort(
            key=lambda entry: (
                interesting_tail_order.get(
                    entry[0][len(family_prefix) + 1:].replace('_s', '').replace('_v', ''),
                    99,
                ),
                entry[0],
            )
        )

        filtered_entries = []
        for key, item in related_entries:
            if expression_already_mentions_key(key):
                continue
            if not is_interesting_companion_entry(key, item):
                continue
            filtered_entries.append((key, item))

        if not filtered_entries:
            continue

        formatted_entries = []
        for _key, item in filtered_entries[:5]:
            name = item.get('name') or _key
            value = item.get('value') or ''
            unit = item.get('unit') or ''
            suffix = f' {unit}' if unit and unit.lower() != 'bool' else ''
            formatted_entries.append(f'{name} = {value}{suffix}'.strip())

        if not formatted_entries:
            continue

        note = f"{build_family_note_label(family_prefix)}: {'; '.join(formatted_entries)}."
        if note in seen_notes:
            continue
        seen_notes.add(note)
        notes.append(note)

        if len(notes) >= 5:
            break

    return notes


def build_monitor_enable_section_html(expressions, source_flag='', cal_file=None, module_keyword=''):
    if not expressions:
        return ''

    expression_items = ''.join(
        f'<li><div class="expr"><code>{html.escape(expr)}</code></div></li>'
        for expr in expressions
    )

    support_notes = build_monitor_enable_support_notes(expressions, cal_file=cal_file, module_keyword=module_keyword)
    supporting_items = []
    if source_flag:
        supporting_items.append(
            f'<li><div class="meaning">Direct executable gate extracted from <code>{html.escape(source_flag)}</code>.</div></li>'
        )
    supporting_items.extend(
        f'<li><div class="meaning">{html.escape(note)}</div></li>'
        for note in support_notes
    )

    supporting_html = ''
    if supporting_items:
        supporting_html = (
            '<p><b>Supporting notes:</b></p>'
            f'<ul class="report-list">{"".join(supporting_items)}</ul>'
        )

    return (
        '<p><b>Direct logic expressions:</b></p>'
        f'<ul class="report-list">{expression_items}</ul>'
        f'{supporting_html}'
    )


def replace_top_level_monitoring_enable_section(report_html, expressions, source_flag='', cal_file=None, module_keyword=''):
    if not expressions:
        return report_html

    section_body = build_monitor_enable_section_html(
        expressions,
        source_flag,
        cal_file=cal_file,
        module_keyword=module_keyword,
    )
    if not section_body:
        return report_html

    updated_html = str(report_html or '')

    heading_pattern = re.compile(
        r'(?is)(<h2\b[^>]*>\s*3\.\s*Monitoring\s+enable\s+conditions\s*</h2>)(.*?)(?=<h2\b[^>]*>\s*4\.\s*Fail\s+Judgment\s*</h2>|$)'
    )
    updated_html, replacement_count = heading_pattern.subn(
        lambda match: match.group(1) + section_body,
        updated_html,
        count=1,
    )
    if replacement_count:
        return updated_html

    list_pattern = re.compile(
        r'(?is)(<li>\s*<strong>\s*(?:2\.\s*)?Monitoring\s+enable\s+conditions\s*</strong>\s*<div>)(.*?)(?=</li>\s*<li>\s*<strong>\s*(?:3\.\s*)?Fail\s+Judgment\s*</strong>)'
    )
    updated_html, replacement_count = list_pattern.subn(
        lambda match: match.group(1) + section_body,
        updated_html,
        count=1,
    )
    return updated_html


def infer_metadata_from_name(name, dtype=''):
    key = normalize_cal_key(name)
    tokens = [tok for tok in key.split('_') if tok]
    dtype_upper = (dtype or '').upper()

    unit = ''
    description = ''

    if dtype_upper == 'FLAG' or re.search(r'(^|_)b(_|$)', key):
        unit = 'bool'
        description = 'Boolean switch flag'
        return unit, description

    if re.search(r'(^|_)ct(_|$)', key):
        unit = 'count'
    elif re.search(r'(^|_)pc(_|$)|(^|_)pct(_|$)', key):
        unit = '%'
    elif re.search(r'(^|_)ne(_|$)', key):
        unit = 'rpm'
    elif re.search(r'(^|_)tqe(_|$)|(^|_)tq(_|$)', key):
        unit = 'Nm'
    elif re.search(r'(^|_)tm(_|$)|(^|_)tc(_|$)', key):
        unit = 's'
    elif re.search(r'(^|_)te(_|$)|(^|_)ect(_|$)|(^|_)iat(_|$)|(^|_)mct(_|$)|(^|_)eot(_|$)|(^|_)tct(_|$)', key):
        unit = 'degC'
    elif re.search(r'(^|_)mul(_|$)|(^|_)rat(_|$)|(^|_)rt(_|$)', key):
        unit = 'ratio'

    token_labels = {
        'min': 'minimum',
        'max': 'maximum',
        'total': 'total',
        'avg': 'average',
        'mean': 'average',
        'tm': 'time',
        'tc': 'time constant',
        'ct': 'counter',
        'pc': 'percent',
        'pct': 'percent',
        'mul': 'multiplier',
        'ofs': 'offset',
        'ne': 'engine speed',
        'spd': 'speed',
        'tqe': 'torque',
        'tq': 'torque',
        'tqw': 'torque wheel',
        'ld': 'load',
        'dlt': 'delta',
        'dif': 'difference',
        'cmp': 'compare',
        'rat': 'rate',
        'rt': 'rate',
        'trgn': 'target',
        'trgt': 'target',
        'thld': 'threshold',
        'ect': 'engine coolant temperature',
        'iat': 'intake air temperature',
        'mct': 'manifold temperature',
        'eot': 'engine oil temperature',
        'tct': 'temperature',
        'te': 'temperature',
        'ta': 'air temperature',
        'vs': 'vehicle speed',
        'veh': 'vehicle',
        'eng': 'engine',
        'drv': 'driver',
        'idl': 'idle',
        'neu': 'neutral',
        'crk': 'crank',
        'rev': 'reverse',
        'trns': 'transmission',
        'inj': 'injection',
        'ffc': 'feedforward',
        'err': 'error',
        'hys': 'hysteresis',
        'enbl': 'enable',
        'dsbl': 'disable',
        'flag': 'flag',
        'sw': 'switch',
        'fmem': 'fmem',
        'rmtstrt': 'remote start',
        'run': 'run',
        'up': 'up',
        'dn': 'down',
    }

    ignore_tokens = {
        'ipcen', 'enipc', 'ford', 'cal', 'ds', 'tbl', 'map', 'array',
        'a', 'b', 'c', 'd', 'n', 's',
    }

    words = []
    for token in tokens:
        if token in ignore_tokens:
            continue
        mapped = token_labels.get(token, '')
        if mapped:
            words.append(mapped)
        else:
            if token.isdigit():
                words.append(token)
            elif len(token) >= 2:
                words.append(token.upper() if len(token) <= 3 else token)
        if len(words) >= 7:
            break

    if words:
        description = ' '.join(words)

    return unit, description


def sanitize_metadata_value(value, is_unit=False):
    text = (value or '').strip()
    if not text:
        return ''

    lowered = text.lower()
    if lowered in {'need confirmation', 'need to confirm', 'unknown', 'n/a'}:
        return ''

    text = re.sub(r'\s*\(inferred\)\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'^\s*inferred\s+from\s+parameter\s+naming\s*:\s*', '', text, flags=re.IGNORECASE).strip()

    if is_unit:
        if text.lower() in {'lookup', 'look up', 'none', '-'}:
            return ''
    else:
        if text.lower() in {'lookup', 'look up', 'none', '-'}:
            return ''

    return text


def resolve_parameter_metadata(name, meta=None, existing_unit='', existing_description='', existing_default=''):
    meta = meta or {}
    unit = sanitize_metadata_value(existing_unit, is_unit=True) or sanitize_metadata_value(meta.get('unit', ''), is_unit=True)
    description = sanitize_metadata_value(existing_description) or sanitize_metadata_value(meta.get('description', ''))
    default = normalize_default_value(existing_default) or normalize_default_value(meta.get('default', ''))

    if not unit or not description:
        inferred_unit, inferred_description = infer_metadata_from_name(name, meta.get('dtype', ''))
        if not unit:
            unit = sanitize_metadata_value(inferred_unit, is_unit=True)
        if not description:
            description = sanitize_metadata_value(inferred_description)

    return unit, description, default


def unique_cal_entries(entries):
    seen = set()
    unique = []
    for name, value in entries:
        key = normalize_cal_key(name)
        if key and key not in seen:
            seen.add(key)
            unique.append((name, value))
    return unique


def is_dtc_family_switch_parameter(name):
    compact = compact_identifier(name)
    return bool(re.fullmatch(r'[pbcu][0-9][0-9a-f]{3}(?:[0-9a-f]{2})?sw', compact))


def get_dtc_code_from_switch_parameter(name):
    if not is_dtc_family_switch_parameter(name):
        return ''

    compact = compact_identifier(name)[:-2].upper()
    if len(compact) == 5:
        return compact
    if len(compact) == 7:
        return format_uds_dtc(compact)
    return compact


def extract_dtc_codes_from_cal_entries(entries, uds_mode=None):
    uds_mode = normalize_uds_mode(uds_mode)
    dtc_codes = []
    seen = set()

    for name, _value in entries:
        base = re.sub(r'\[.*?\]', '', name).strip()
        dtc_code = get_dtc_code_from_switch_parameter(base)
        if not dtc_code:
            continue
        if not dtc_matches_uds_mode(dtc_code, uds_mode):
            continue
        if dtc_code in seen:
            continue
        seen.add(dtc_code)
        dtc_codes.append(dtc_code)

    return dtc_codes


def inspect_cal_source_file(cal_file, module_name=''):
    info = {
        'path': cal_file,
        'name': os.path.basename(cal_file),
        'header_type': 'unknown',
        'looks_generated': False,
        'warning': '',
    }

    ext = os.path.splitext(cal_file)[1].lower()
    if ext != '.csv':
        return info

    try:
        with open(cal_file, 'r', encoding='utf-8', errors='replace', newline='') as f:
            rows = [row for row in csv.reader(f) if row]
    except Exception:
        return info

    if not rows:
        return info

    header = [cell.strip() for cell in rows[0]]
    if header[:3] == ['Category', 'Parameter', 'Value']:
        info['header_type'] = 'category-parameter-value'
    elif header[:2] == ['Parameter', 'Value']:
        info['header_type'] = 'parameter-value'

    base_name = os.path.splitext(info['name'])[0]
    module_prefix = f'{module_name}_' if module_name else ''
    looks_generated_name = base_name.endswith('_CalSearchResult') or base_name.startswith(module_prefix)
    looks_generated_path = 'DataAnalysisExpert' in cal_file
    # Check for both old naming pattern (module_name_CalData.csv) and new pattern (module_name_CalData_ccode_version.csv)
    looks_generated_header = info['header_type'] == 'category-parameter-value' and module_name and (
        info['name'] == f'{module_name}_CalData.csv' or
        base_name.startswith(f'{module_name}_CalData_')
    )

    info['looks_generated'] = bool(looks_generated_name or looks_generated_path or looks_generated_header)
    if info['looks_generated']:
        info['warning'] = 'Selected CSV is already a generated search-result file.'

    return info


def parse_generated_cal_result(filepath, uds_mode=None):
    uds_mode = normalize_uds_mode(uds_mode)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace', newline='') as f:
            rows = [row for row in csv.reader(f) if row]
    except Exception:
        return None

    if not rows:
        return None

    header = [cell.strip() for cell in rows[0]]
    if header[:3] != ['Category', 'Parameter', 'Value']:
        return None

    unit_idx = next((idx for idx, col in enumerate(header) if col.lower() == 'unit'), -1)
    desc_idx = next((idx for idx, col in enumerate(header) if col.lower() == 'description'), -1)
    default_idx = next((idx for idx, col in enumerate(header) if col.lower() == 'default'), -1)

    all_found = []
    found = []
    dtc_sw_items = []
    dtc_codes = []
    seen_dtc_codes = set()

    for row in rows[1:]:
        if len(row) < 3:
            continue

        category = row[0].strip() or 'Cal'
        name = row[1].strip().lstrip('\ufeff')
        value = row[2].strip()
        default = row[default_idx].strip() if default_idx >= 0 and len(row) > default_idx else ''
        unit = row[unit_idx].strip() if unit_idx >= 0 and len(row) > unit_idx else ''
        description = row[desc_idx].strip() if desc_idx >= 0 and len(row) > desc_idx else ''
        if not name:
            continue
        if normalize_cal_key(name) in {'lookup', 'lookups'}:
            continue

        if category.upper() == 'DTC_SW':
            dtc_code = get_dtc_code_from_switch_parameter(name)
            if not dtc_code:
                continue
            if not dtc_matches_uds_mode(dtc_code, uds_mode):
                continue

            item = {
                'name': name,
                'value': value,
                'category': 'DTC_SW',
                'dtc': dtc_code,
                'rule': build_dtc_rule(dtc_code),
                'default': default,
                'unit': unit,
                'description': description,
            }
            dtc_sw_items.append(item)
            all_found.append(item)

            if dtc_code not in seen_dtc_codes:
                seen_dtc_codes.add(dtc_code)
                dtc_codes.append(dtc_code)
            continue

        item = {
            'name': name,
            'value': value,
            'category': category,
            'default': default,
            'unit': unit,
            'description': description,
        }
        found.append(item)
        all_found.append(item)

    return {
        'all_found': all_found,
        'found': found,
        'dtc_sw_items': dtc_sw_items,
        'dtc_codes': dtc_codes,
        'total_cal_params': len(all_found),
    }


def extract_ccode_version_string(filepath):
    """Extract C-code version string from filename (e.g., 'HMYJ2_046_preR10')."""
    filename = os.path.basename(filepath)
    filename_no_ext = os.path.splitext(filename)[0]
    
    # Pattern: C-CODE_3DIGITS_VARIANT (e.g., HMYJ2_046_preR10, HZWE0_045_R05)
    match = re.search(r'([A-Z][A-Z0-9]*_\d{3}_[a-zA-Z0-9]+)$', filename_no_ext)
    if match:
        return match.group(1)
    
    # Fallback: Try to extract from after _mHEV_ pattern
    match = re.search(r'_mHEV_(.+)$', filename_no_ext)
    if match:
        return match.group(1)
    
    return ''


def save_cal_search_result_csv(module_file, module_name, rows, ccode_version=''):
    if not module_file or not module_name:
        return '', ''

    out_dir = os.path.dirname(module_file)
    pack_dir = os.path.dirname(out_dir) if os.path.basename(out_dir).upper() == module_name.upper() else out_dir
    
    # Build filename: include C-code version if available
    if ccode_version:
        out_file = os.path.join(out_dir, f'{module_name}_CalData_{ccode_version}.csv')
    else:
        out_file = os.path.join(out_dir, f'{module_name}_CalData.csv')

    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['Category', 'Parameter', 'Value', 'Default', 'Unit', 'Description'])
    for item in rows:
        writer.writerow([
            item.get('category', 'Cal'),
            item.get('name', ''),
            item.get('value', ''),
            item.get('default', ''),
            item.get('unit', ''),
            item.get('description', ''),
        ])
    new_content = buffer.getvalue()

    legacy_names = [f'{module_name}_CalData.csv']
    if ccode_version:
        legacy_names.insert(0, f'{module_name}_CalData_{ccode_version}.csv')

    for legacy_name in legacy_names:
        legacy_root_file = os.path.join(pack_dir, legacy_name)
        if not os.path.isfile(legacy_root_file) or os.path.normcase(legacy_root_file) == os.path.normcase(out_file):
            continue
        try:
            with open(legacy_root_file, 'r', encoding='utf-8', errors='replace', newline='') as f:
                legacy_content = f.read()
            if not os.path.exists(out_file):
                shutil.move(legacy_root_file, out_file)
                if legacy_content == new_content:
                    return out_file, 'moved'
            elif legacy_content == new_content:
                os.remove(legacy_root_file)
            else:
                legacy_base, legacy_ext = os.path.splitext(legacy_name)
                legacy_target = os.path.join(out_dir, f'{legacy_base}_ROOT_LEGACY{legacy_ext}')
                if not os.path.exists(legacy_target):
                    shutil.move(legacy_root_file, legacy_target)
        except Exception:
            pass

    if os.path.exists(out_file):
        try:
            with open(out_file, 'r', encoding='utf-8', errors='replace', newline='') as f:
                if f.read() == new_content:
                    return out_file, 'skipped'
        except Exception:
            pass

    with open(out_file, 'w', encoding='utf-8', newline='') as f:
        f.write(new_content)

    return out_file, 'updated'


def is_sw_zero(value):
    """Return True if a calibration switch value represents 0 (disabled)."""
    try:
        v = str(value).strip().lower()
        if v in ('0', '0x00', '0x0', '0x000', 'false', 'off'):
            return True
        if v.startswith('0x'):
            return int(v, 16) == 0
        return float(v) == 0.0
    except Exception:
        return False


def is_sw_unused(value):
    """Return True if a calibration switch value marks a DTC as unused / not applicable."""
    v = str(value).strip().lower()
    return v in {'-', '--', 'n/a', 'na', 'unused', 'not used'}


def collect_dtc_switch_items(entries, uds_mode=None):
    uds_mode = normalize_uds_mode(uds_mode)
    dtc_items = []
    dtc_map = {}
    seen = set()

    for name, value in entries:
        base = re.sub(r'\[.*?\]', '', name).strip()
        dtc_code = get_dtc_code_from_switch_parameter(base)
        if not dtc_code:
            continue
        if not dtc_matches_uds_mode(dtc_code, uds_mode):
            continue
        if is_sw_unused(value):
            continue

        key = (dtc_code, compact_identifier(base), str(value).strip())
        if key in seen:
            continue
        seen.add(key)

        item = {
            'name': name,
            'value': value,
            'dtc': dtc_code,
            'category': 'DTC_SW',
            'rule': build_dtc_rule(dtc_code),
        }
        dtc_items.append(item)
        dtc_map.setdefault(dtc_code, []).append(item)

    return dtc_items, dtc_map


def get_cal_dtc_switch_items(cal_file, uds_mode=None):
    if not cal_file or not os.path.exists(cal_file):
        return [], {}

    entries = unique_cal_entries(parse_cal_data(cal_file))
    return collect_dtc_switch_items(entries, uds_mode)


def build_cal_value_lookup(cal_file):
    if not cal_file or not os.path.exists(cal_file):
        return {}

    lookup = {}
    for name, value in unique_cal_entries(parse_cal_data(cal_file)):
        key = compact_identifier(name)
        if key and key not in lookup:
            lookup[key] = str(value).strip()
    return lookup


def parse_int_value(value):
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.lower().startswith('0x'):
            return int(text, 16)
        return int(float(text))
    except Exception:
        return None


def normalize_sw_meaning_text(text):
    value = str(text or '').strip()
    if not value:
        return ''

    value = value.replace('_', ' ')
    value = re.sub(r'\s+', ' ', value)
    value = value.strip(' .;,')

    lowered = value.lower()
    canonical = {
        'cal out': 'cal out',
        'off': 'off',
        'cal out/off': 'cal out / off',
        'cal out / off': 'cal out / off',
        'non mil': 'non-MIL',
        'non-mil': 'non-MIL',
        'mil': 'MIL',
        '1 trip mil': '1-trip MIL on',
        '1 trip mil on': '1-trip MIL on',
        'one trip mil': '1-trip MIL on',
        'one trip mil on': '1-trip MIL on',
    }
    return canonical.get(lowered, value)


def collect_raw_sw_meanings(sw_items):
    meaning_map = {}
    for sw in sw_items or []:
        value = str(sw.get('value', '')).strip()
        raw_label = str(sw.get('value_label', '')).strip()
        if not value or not raw_label:
            continue
        parts = [part.strip() for part in re.split(r'\s*/\s*', raw_label) if part.strip()]
        if not parts:
            parts = [raw_label]
        labels = meaning_map.setdefault(value, [])
        seen = {normalize_sw_meaning_text(item).lower() for item in labels}
        for part in parts:
            normalized = normalize_sw_meaning_text(part)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            labels.append(normalized)
            seen.add(key)
    return meaning_map


def build_dtc_sw_value_meanings(sw_items, cal_lookup):
    raw_meaning_map = collect_raw_sw_meanings(sw_items)
    observed_values = sorted(
        {str(item.get('value', '')).strip() for item in (sw_items or []) if str(item.get('value', '')).strip()},
        key=lambda value: (parse_int_value(value) is None, parse_int_value(value) if parse_int_value(value) is not None else value),
    )

    dtc_trips_to_mil = parse_int_value(cal_lookup.get(compact_identifier('DTC_TRIPS_TO_MIL')))
    sw_value_meanings = {}

    for value in observed_values:
        if value == '0':
            sw_value_meanings[value] = 'cal out / off'
            continue
        if value == '1':
            sw_value_meanings[value] = 'non-MIL'
            continue
        if value == '2':
            if dtc_trips_to_mil and dtc_trips_to_mil > 0:
                sw_value_meanings[value] = f'MIL, {dtc_trips_to_mil}-trip on'
            else:
                sw_value_meanings[value] = 'MIL'
            continue
        if value == '5':
            sw_value_meanings[value] = '1-trip MIL on'
            continue
        if value == '7':
            sw_value_meanings[value] = '1-trip MIL on, off on first/same-trip pass'
            continue

        labels = raw_meaning_map.get(value, [])
        if labels:
            sw_value_meanings[value] = ' / '.join(labels)

    return sw_value_meanings


def infer_a2l_stem_from_paths(*paths):
    for path in paths:
        if not path:
            continue
        tokens = [os.path.splitext(os.path.basename(path))[0]]
        tokens.extend(os.path.normpath(path).split(os.sep))
        for token in tokens:
            match = re.search(r'([A-Z0-9]+_P\d)(?:_|$)', token or '', re.IGNORECASE)
            if match:
                return match.group(1).upper()
    return ''


def find_related_a2l_file(filepath='', cal_file=''):
    search_roots = [os.path.join(BASE_DIR, 'A2L_anlaysis')]
    stem = infer_a2l_stem_from_paths(filepath, cal_file)

    candidates = []
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    if not filename.lower().endswith('.a2l'):
                        continue
                    candidates.append(os.path.join(dirpath, filename))
        except OSError:
            continue

    if not candidates:
        return ''
    if stem:
        exact_name = f'{stem}.a2l'
        for candidate in candidates:
            if os.path.basename(candidate).upper() == exact_name.upper():
                return candidate
        for candidate in candidates:
            if stem in os.path.basename(candidate).upper():
                return candidate
    return candidates[0] if len(candidates) == 1 else ''


def parse_a2l_switch_value_labels(description):
    labels = {}
    text = (description or '').strip()
    if not text:
        return labels

    pattern = re.compile(r'(\d+)\s*=\s*(.*?)(?=\s*(?:[;,]\s*\d+\s*=|$))')
    for match in pattern.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip().strip('"')
        if key and value:
            labels[key] = value
    return labels


def load_a2l_dtc_switch_map(a2l_file):
    if not a2l_file or not os.path.exists(a2l_file):
        return {}

    try:
        stat = os.stat(a2l_file)
    except OSError:
        return {}

    cache_key = (a2l_file, stat.st_mtime_ns, stat.st_size)
    cached = A2L_DTC_SWITCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    for old_key in list(A2L_DTC_SWITCH_CACHE.keys()):
        if old_key[0] == a2l_file and old_key != cache_key:
            A2L_DTC_SWITCH_CACHE.pop(old_key, None)

    switch_map = {}
    pattern = re.compile(
        r'/begin\s+CHARACTERISTIC\s+([A-Za-z0-9_\[\]]+)\s*\r?\n\s*"([^"]*)"',
        re.IGNORECASE,
    )

    try:
        with open(a2l_file, 'r', encoding='utf-8', errors='replace') as handle:
            content = handle.read()
    except OSError:
        return {}

    for raw_name, description in pattern.findall(content):
        name = re.sub(r'\[.*?\]', '', raw_name).strip()
        if not name:
            continue
        dtc_code = get_dtc_code_from_switch_parameter(name)
        if not dtc_code:
            continue
        switch_map[compact_identifier(name)] = {
            'name': name,
            'dtc': dtc_code,
            'description': description.strip(),
            'value_labels': parse_a2l_switch_value_labels(description),
        }

    A2L_DTC_SWITCH_CACHE[cache_key] = switch_map
    return switch_map


def load_a2l_dtc_description_table(a2l_file):
    """Parse the ASAM A2L 'DTCWORD' COMPU_VTAB block (index -> "<CODE> <full description>")
    into a {base 5-char DTC code: full description text} map. This is the authoritative
    full-text DTC catalog embedded in the calibration's A2L, not a heuristic guess."""
    if not a2l_file or not os.path.exists(a2l_file):
        return {}

    try:
        stat = os.stat(a2l_file)
    except OSError:
        return {}

    cache_key = (a2l_file, stat.st_mtime_ns, stat.st_size)
    cached = A2L_DTC_DESCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    for old_key in list(A2L_DTC_DESCRIPTION_CACHE.keys()):
        if old_key[0] == a2l_file and old_key != cache_key:
            A2L_DTC_DESCRIPTION_CACHE.pop(old_key, None)

    descriptions = {}
    try:
        with open(a2l_file, 'r', encoding='utf-8', errors='replace') as handle:
            content = handle.read()
    except OSError:
        A2L_DTC_DESCRIPTION_CACHE[cache_key] = descriptions
        return descriptions

    block_match = re.search(
        r'/begin\s+COMPU_VTAB\s+\S*DTCWORD\S*.*?/end\s+COMPU_VTAB',
        content,
        re.IGNORECASE | re.DOTALL,
    )
    if block_match:
        quoted_pattern = re.compile(r'"((?:[^"\\]|\\.)*)"')
        code_pattern = re.compile(r'^([PBCU][0-9][0-9A-F]{3})\s+(.+)$')
        for raw in quoted_pattern.findall(block_match.group(0)):
            text = raw.replace('\\"', '"').strip()
            code_match = code_pattern.match(text)
            if not code_match:
                continue
            code = code_match.group(1).upper()
            desc = code_match.group(2).strip()
            if desc and code not in descriptions:
                descriptions[code] = desc

    A2L_DTC_DESCRIPTION_CACHE[cache_key] = descriptions
    return descriptions


def classify_dtc_sw_status(sw_items):
    if not sw_items:
        return 'unused', 'Unused'

    values = [str(item.get('value', '')).strip() for item in sw_items if str(item.get('value', '')).strip()]
    if not values:
        return 'unused', 'Unused'
    if any(value != '0' for value in values):
        return 'monitoring-on', 'Monitoring On'
    return 'monitoring-off', 'Monitoring Off'


def enrich_dtc_sw_items_with_a2l(sw_items, a2l_switch_map):
    enriched = []
    matched_count = 0
    described_count = 0

    for sw in sw_items:
        item = {
            'name': sw.get('name', ''),
            'value': sw.get('value', ''),
        }
        a2l_info = a2l_switch_map.get(compact_identifier(item['name'])) if item['name'] else None
        if a2l_info:
            matched_count += 1
            item['a2l_confirmed'] = True
            item['a2l_description'] = a2l_info.get('description', '')
            value_key = str(item['value']).strip()
            value_label = a2l_info.get('value_labels', {}).get(value_key, '')
            item['value_label'] = value_label
            if value_label:
                described_count += 1
        else:
            item['a2l_confirmed'] = False
            item['a2l_description'] = ''
            item['value_label'] = ''
        enriched.append(item)

    return enriched, matched_count, described_count


def dtc_switch_matches(key, dtc_codes, uds_mode=None):
    uds_mode = normalize_uds_mode(uds_mode)
    key_compact = compact_identifier(key)
    if not dtc_codes:
        return key_compact.endswith('sw')

    for dtc in dtc_codes:
        compact = compact_dtc_code(dtc).lower()
        if len(compact) < 5:
            continue

        base = compact[:5]
        has_subtype = len(compact) >= 7

        if uds_mode is False:
            if key_compact == base + 'sw':
                return True
        elif uds_mode is True:
            if has_subtype and key_compact.startswith(compact[:7]) and key_compact.endswith('sw'):
                return True
            if key_compact.startswith(base) and key_compact.endswith('sw'):
                return True
        else:
            if key_compact == base + 'sw':
                return True
            if has_subtype and key_compact.startswith(compact[:7]) and key_compact.endswith('sw'):
                return True
            if key_compact.startswith(base) and key_compact.endswith('sw'):
                return True
    return False


def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write('\n')


def normalize_ford_llm_base_url(base_url):
    normalized = (base_url or FORD_LLM_BASE_URL).strip().rstrip('/')
    if not normalized:
        return FORD_LLM_BASE_URL
    if normalized.endswith('/fordllmapi'):
        return f'{normalized}/api/v1'
    return normalized


def build_proxy_config(proxy_url):
    proxy = (proxy_url or '').strip()
    if not proxy:
        return None, os.environ.copy()

    proxies = {
        'http': proxy,
        'https': proxy,
    }
    env = os.environ.copy()
    env['HTTP_PROXY'] = proxy
    env['HTTPS_PROXY'] = proxy
    env['http_proxy'] = proxy
    env['https_proxy'] = proxy
    env.setdefault('NO_PROXY', 'localhost,127.0.0.1')
    env.setdefault('no_proxy', env['NO_PROXY'])
    return proxies, env


def get_ford_llm_settings():
    config = load_config()
    return {
        'base_url': normalize_ford_llm_base_url(os.environ.get('FORD_LLM_BASE_URL') or config.get('ford_llm_base_url') or FORD_LLM_BASE_URL),
        'model': os.environ.get('FORD_LLM_MODEL') or config.get('ford_llm_model') or FORD_LLM_DEFAULT_MODEL,
        'bearer_token': os.environ.get('FORD_LLM_BEARER_TOKEN') or config.get('ford_llm_bearer_token', ''),
        'client_id': (
            os.environ.get('FORD_LLM_CLIENT_ID')
            or os.environ.get('FORDLLM_CLIENT_ID')
            or config.get('ford_llm_client_id', '')
        ).strip(),
        'client_secret': (
            os.environ.get('FORD_LLM_CLIENT_SECRET')
            or os.environ.get('FORDLLM_CLIENT_SECRET')
            or config.get('ford_llm_client_secret', '')
        ).strip(),
        'proxy_url': (os.environ.get('FORD_LLM_PROXY_URL') or config.get('ford_llm_proxy_url') or 'http://internet.ford.com:83').strip(),
    }


def resolve_cli_path(command_name, fallback_paths=None):
    candidates = [command_name]
    if os.name == 'nt' and not os.path.splitext(command_name)[1]:
        candidates.extend([
            f'{command_name}.exe',
            f'{command_name}.cmd',
            f'{command_name}.bat',
        ])

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    for fallback_path in fallback_paths or []:
        if fallback_path and os.path.exists(fallback_path):
            return fallback_path

    return ''


def get_ford_llm_client_credentials_token(client_id, client_secret, proxy_url=''):
    cache_key = f'{client_id}|{FORD_LLM_SCOPE}'
    cached = FORD_LLM_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached.get('expires_at', 0) > now:
        return cached['token']

    proxies, _ = build_proxy_config(proxy_url)
    response = requests.post(
        FORD_LLM_TOKEN_ENDPOINT,
        data={
            'client_id': client_id,
            'client_secret': client_secret,
            'scope': FORD_LLM_SCOPE,
            'grant_type': 'client_credentials',
        },
        proxies=proxies,
        timeout=(20, 30),
    )
    if not response.ok:
        raise RuntimeError(
            f'Client credentials token request failed ({response.status_code}): {response.text[:500]}'
        )

    data = response.json()
    token = (data.get('access_token') or '').strip()
    if not token:
        raise RuntimeError('Client credentials token response did not include access_token.')

    expires_in = int(data.get('expires_in') or 3600)
    FORD_LLM_TOKEN_CACHE[cache_key] = {
        'token': token,
        'expires_at': now + max(expires_in - 120, 60),
    }
    return token


def resolve_ford_llm_token(settings):
    """Resolve a Ford LLM token using the documented auth flow first."""
    client_id = (settings.get('client_id') or '').strip()
    client_secret = (settings.get('client_secret') or '').strip()
    proxy_url = (settings.get('proxy_url') or '').strip()
    if client_id and client_secret:
        return get_ford_llm_client_credentials_token(client_id, client_secret, proxy_url), 'client_credentials'

    token = (settings.get('bearer_token') or '').strip()
    if token:
        return token, 'bearer_token'

    program_data = os.environ.get('ProgramData', r'C:\ProgramData')
    program_files_x86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    _, cli_env = build_proxy_config(proxy_url)

    cldctl_path = resolve_cli_path('cldctl', [
        os.path.join(program_data, 'chocolatey', 'bin', 'cldctl.exe'),
    ])
    if cldctl_path:
        result = subprocess.run(
            [cldctl_path, 'get', 'azure-token', '--scope', FORD_LLM_SCOPE],
            capture_output=True,
            text=True,
            timeout=20,
            env=cli_env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), 'cldctl'

    az_path = resolve_cli_path('az', [
        os.path.join(program_files_x86, 'Microsoft SDKs', 'Azure', 'CLI2', 'wbin', 'az.cmd'),
    ])
    if az_path:
        result = subprocess.run(
            [az_path, 'account', 'get-access-token', '--scope', FORD_LLM_SCOPE, '--query', 'accessToken', '-o', 'tsv'],
            capture_output=True,
            text=True,
            timeout=20,
            env=cli_env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), 'az'

    raise RuntimeError(
        'No Ford LLM auth source is configured. Preferred: set FORDLLM_CLIENT_ID and FORDLLM_CLIENT_SECRET '
        '(or save ford_llm_client_id and ford_llm_client_secret in config.json) using the values from API Catalog. '
        'Fallbacks: FORD_LLM_BEARER_TOKEN, cldctl, or Azure CLI.'
    )


def sse_event(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def normalize_obd_only_stream_chunk(chunk):
    return chunk


def _flatten_llm_content_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_value = item.get('text')
                if isinstance(text_value, str):
                    parts.append(text_value)
                    continue
                if item.get('type') == 'text':
                    nested_text = item.get('content') or item.get('value') or item.get('text')
                    if isinstance(nested_text, str):
                        parts.append(nested_text)
        return ''.join(parts)
    if isinstance(value, dict):
        for key in ('text', 'content', 'value'):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, list):
                flattened = _flatten_llm_content_value(nested)
                if flattened:
                    return flattened
    return ''


def extract_llm_payload_text(payload_obj):
    if not isinstance(payload_obj, dict):
        return ''

    choices = payload_obj.get('choices')
    if isinstance(choices, list):
        parts = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for container_key in ('delta', 'message'):
                container = choice.get(container_key)
                if isinstance(container, dict):
                    content_value = container.get('content')
                    text = _flatten_llm_content_value(content_value)
                    if text:
                        parts.append(text)
            text_value = choice.get('text')
            if isinstance(text_value, str) and text_value:
                parts.append(text_value)
        if parts:
            return ''.join(parts)

    output = payload_obj.get('output')
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content_value = item.get('content')
            text = _flatten_llm_content_value(content_value)
            if text:
                parts.append(text)
        if parts:
            return ''.join(parts)

    for key in ('content', 'text', 'output_text'):
        value = payload_obj.get(key)
        text = _flatten_llm_content_value(value)
        if text:
            return text

    return ''


def call_ford_llm_streaming(system_prompt, user_prompt, emit_done=True):
    """Call Ford LLM chat completions and yield SSE messages for the browser."""
    settings = get_ford_llm_settings()
    ford_llm_url = f"{settings['base_url']}/chat/completions"
    proxies, _ = build_proxy_config(settings['proxy_url'])

    try:
        token, _ = resolve_ford_llm_token(settings)
    except Exception as e:
        yield sse_event({'error': str(e)})
        return

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    selected_model = (settings.get('model') or '').strip()
    configured_model = selected_model or FORD_LLM_DEFAULT_MODEL
    model_candidates = [configured_model]
    fallback_model = FORD_LLM_DEFAULT_MODEL.strip()
    if fallback_model and fallback_model not in model_candidates:
        model_candidates.append(fallback_model)

    streamed_content = False
    last_error_message = ''

    def build_payload(model_name):
        payload = {
            'model': model_name,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': True,
        }
        if not str(model_name or '').lower().startswith('gpt-5'):
            payload['temperature'] = 0.2
            payload['max_tokens'] = 16000
        return payload

    for model_index, model_name in enumerate(model_candidates):
        payload = build_payload(model_name)
        retry_available = model_index < (len(model_candidates) - 1)
        response = None

        try:
            if model_index > 0:
                yield sse_event({'status': f'Configured model is unavailable or rate-limited. Retrying with {model_name}...'})

            response = requests.post(
                ford_llm_url,
                json=payload,
                headers=headers,
                stream=True,
                proxies=proxies,
                timeout=(20, 900),
            )
            if response.status_code >= 400:
                body = response.text[:1000].strip()
                last_error_message = f'Ford LLM returned HTTP {response.status_code}: {body}'
                retryable_model_error = response.status_code in {400, 404, 422}
                if (response.status_code == 429 or retryable_model_error) and retry_available:
                    continue
                yield sse_event({'error': last_error_message})
                return

            content_type = (response.headers.get('Content-Type') or '').lower()
            if 'text/event-stream' in content_type:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith('data: '):
                        continue
                    data_text = line[6:].strip()
                    if data_text == '[DONE]':
                        if emit_done:
                            yield sse_event({'done': True})
                        break
                    try:
                        payload_obj = json.loads(data_text)
                        if extract_llm_payload_text(payload_obj):
                            streamed_content = True
                        yield sse_event(payload_obj)
                    except json.JSONDecodeError:
                        yield sse_event({'error': f'Invalid streaming payload from Ford LLM: {data_text[:200]}'})
                        return
                return

            data = response.json()
            if extract_llm_payload_text(data):
                streamed_content = True
            yield sse_event(data)
            if emit_done:
                yield sse_event({'done': True})
            return
        except requests.Timeout:
            if streamed_content:
                yield sse_event({'status': '⚠️ Stream timed out after partial response. Keeping received content.'})
                if emit_done:
                    yield sse_event({'done': True})
                return
            last_error_message = 'Ford LLM request timed out while waiting for response. Please retry, reduce report size, or verify proxy/network settings.'
            if retry_available:
                continue
            yield sse_event({'error': last_error_message})
            return
        except requests.RequestException as e:
            msg = str(e)
            lower_msg = msg.lower()
            premature_stream_end = (
                'response ended prematurely' in lower_msg
                or 'incomplete read' in lower_msg
                or 'chunkedencodingerror' in lower_msg
            )
            if streamed_content and premature_stream_end:
                yield sse_event({'status': '⚠️ Stream disconnected after partial response. Keeping received content.'})
                if emit_done:
                    yield sse_event({'done': True})
                return
            last_error_message = f'Ford LLM request failed: {str(e)}'
            if retry_available:
                continue
            yield sse_event({'error': last_error_message})
            return
        except ValueError:
            body_preview = response.text[:500] if response is not None else ''
            last_error_message = f'Ford LLM returned non-JSON response: {body_preview}'
            if retry_available:
                continue
            yield sse_event({'error': last_error_message})
            return

    if last_error_message:
        yield sse_event({'error': last_error_message})


def load_prompt_template():
    try:
        with open(PROMPT_TEMPLATE_FILE, 'r', encoding='utf-8') as f:
            text = f.read().strip()
            if text:
                return text
    except OSError:
        raise RuntimeError(f'Prompt template file not found: {PROMPT_TEMPLATE_FILE}')
    raise RuntimeError(f'Prompt template file is empty: {PROMPT_TEMPLATE_FILE}')


def build_report_prompt(keyword, requested_mode='general', classification_mode_label='Auto (Prompt rules + runtime evidence blocks)'):
    base_prompt = load_prompt_template()
    module_name = (keyword or '').strip() or 'UNKNOWN_MODULE'
    mode_key = (requested_mode or 'general').strip().lower()

    runtime_lines = [
        '',
        '[RUNTIME CONTEXT]',
        f'- Selected target module: {module_name}',
        f'- Requested analysis mode: {"OBD-focused" if mode_key == "obd" else "General auto-classification"}',
        f'- Classification mode: {classification_mode_label}',
        '- Keep the report generic and module-agnostic.',
        '- Do not hardcode module-specific assumptions.',
        '- Return HTML fragment only.',
    ]
    return base_prompt + '\n' + '\n'.join(runtime_lines)


def build_analysis_user_prompt(
    *,
    keyword,
    requested_mode,
    classification_mode_label,
    cal_file,
    cal_summary,
    cal_data,
    module_code,
    current_module_context_block='',
    control_module_context_block='',
    module_dtc_architecture_block='',
    fd_blocks_text='',
    related_module_context_block='',
    disabled_dtcs_block='',
    hybrid_disabled_dtcs_block='',
    dtc_mode_contract_block='',
    obd_expression_contract_block='',
    deterministic_monitoring_enable_block='',
    hybrid_output_contract_block='',
    control_path_focus_block='',
    split_note='',
    compact=False,
):
    mode_label = 'OBD-focused' if (requested_mode or '').strip().lower() == 'obd' else 'General auto-classification'
    payload_note = (
        '(Compact mode: raw calibration preview omitted; use the structured calibration summary above.)'
        if compact else
        (cal_data or '(No additional calibration payload note.)')
    )
    compact_block = """
[COMPACT MODE]
- Keep the same five-section structure.
- Prefer the strongest directly evidenced bullets first.
- Do not restate every secondary calibration item.
- Keep the report complete, but compact.
""".strip() if compact else ''

    return f"""Generate the final report from the evidence package below.

[OUTPUT CONTRACT]
- Return HTML fragment only.
- Do not return markdown.
- Do not return markdown code fences.
- Do not return a full HTML document.
- Use the exact five top-level sections required by the system prompt.
- Keep the report generic and data-driven.

[RUN MODE]
- Requested analysis mode: {mode_label}
- Classification mode: {classification_mode_label}

[TARGET MODULE]
{keyword}

[CALIBRATION SOURCE FILE]
{cal_file or '(none)'}

[CALIBRATION SUMMARY]
{cal_summary}

[CALIBRATION PAYLOAD NOTE]
{payload_note}

[C MODULE CODE — first 50K chars of source]
{module_code}

{current_module_context_block}
{control_module_context_block}
{module_dtc_architecture_block}

[FUNCTIONAL DESCRIPTION BLOCKS]
{fd_blocks_text or '(No FD blocks found in source file.)'}

{related_module_context_block}
{disabled_dtcs_block}
{hybrid_disabled_dtcs_block}
{dtc_mode_contract_block}
{obd_expression_contract_block}
{deterministic_monitoring_enable_block}
{hybrid_output_contract_block}
{control_path_focus_block}

[ADDITIONAL RULES]
- Use calibration summary as the primary source for current-project values.
- Use current-module and related-module excerpts as the primary source for code-visible comparators, states, timers, and ownership artifacts.
- If a compared runtime token is not visible, do not invent one.
- Do not write pseudo-equations with unknown text in the middle.
- Do not use repetitive prefixes such as "Visible calibration window:" or "Visible calibration threshold:".
- Keep Additional CAL items limited to items not already referenced above.
{split_note or '- Calibration rows fit in a single report part.'}
{compact_block}
"""


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/api/ford-llm-settings', methods=['GET'])
def api_get_ford_llm_settings():
    settings = get_ford_llm_settings()
    config = load_config()
    env_overrides = {
        'base_url': bool(os.environ.get('FORD_LLM_BASE_URL')),
        'model': bool(os.environ.get('FORD_LLM_MODEL')),
        'bearer_token': bool(os.environ.get('FORD_LLM_BEARER_TOKEN')),
        'client_id': bool(os.environ.get('FORD_LLM_CLIENT_ID') or os.environ.get('FORDLLM_CLIENT_ID')),
        'client_secret': bool(os.environ.get('FORD_LLM_CLIENT_SECRET') or os.environ.get('FORDLLM_CLIENT_SECRET')),
        'proxy_url': bool(os.environ.get('FORD_LLM_PROXY_URL')),
    }
    has_client_credentials = bool(settings['client_id'] and settings['client_secret'])
    return jsonify({
        'base_url': settings['base_url'],
        'model': settings['model'],
        'default_model': FORD_LLM_DEFAULT_MODEL,
        'has_client_credentials': has_client_credentials,
        'has_bearer_token': bool(settings['bearer_token']),
        'auth_source': (
            'client_credentials (environment)' if has_client_credentials and (env_overrides['client_id'] or env_overrides['client_secret']) else
            'client_credentials (config.json)' if has_client_credentials and config.get('ford_llm_client_id') else
            'bearer_token (environment)' if env_overrides['bearer_token'] else
            'bearer_token (config.json)' if config.get('ford_llm_bearer_token') else
            'none'
        ),
        'env_overrides': env_overrides,
    })


@app.route('/api/ford-llm-models', methods=['GET'])
def api_get_ford_llm_models():
    settings = get_ford_llm_settings()
    proxies, _ = build_proxy_config(settings['proxy_url'])

    try:
        token, token_source = resolve_ford_llm_token(settings)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        response = requests.get(
            f"{settings['base_url']}/models",
            headers={'Authorization': f'Bearer {token}'},
            proxies=proxies,
            timeout=(20, 30),
        )
        if response.status_code >= 400:
            hint = None
            if response.status_code in (401, 403):
                hint = 'Ford LLM access may still need API Catalog approval for this client or identity.'
            return jsonify({
                'ok': False,
                'error': f'Ford LLM returned HTTP {response.status_code}: {response.text[:500]}',
                'hint': hint,
            }), 400

        data = response.json()
        models = []
        if isinstance(data, dict):
            models = data.get('data') or data.get('models') or []
        elif isinstance(data, list):
            models = data

        model_names = []
        seen_names = set()
        for item in models:
            if isinstance(item, dict):
                name = item.get('id') or item.get('name') or ''
            else:
                name = str(item)
            name = (name or '').strip()
            if name and name not in seen_names:
                seen_names.add(name)
                model_names.append(name)

        if not model_names:
            model_names = [FORD_LLM_DEFAULT_MODEL]

        return jsonify({
            'ok': True,
            'auth_source': token_source,
            'models': model_names,
            'model_count': len(models),
            'selected_model': settings['model'],
            'default_model': FORD_LLM_DEFAULT_MODEL,
        })
    except requests.RequestException as e:
        return jsonify({'ok': False, 'error': f'Ford LLM model list request failed: {str(e)}'}), 400
    except ValueError:
        return jsonify({'ok': False, 'error': 'Ford LLM returned invalid JSON from /models'}), 400


@app.route('/api/ford-llm-settings', methods=['POST'])
def api_save_ford_llm_settings():
    data = request.json or {}
    config = load_config()

    base_url = normalize_ford_llm_base_url(data.get('base_url') or FORD_LLM_BASE_URL)
    model = (data.get('model') or FORD_LLM_DEFAULT_MODEL).strip()
    bearer_token = (data.get('bearer_token') or '').strip()
    client_id = (data.get('client_id') or '').strip()
    client_secret = (data.get('client_secret') or '').strip()
    preserve_existing_token = bool(data.get('preserve_existing_token', True))
    preserve_existing_client_id = bool(data.get('preserve_existing_client_id', True))
    preserve_existing_client_secret = bool(data.get('preserve_existing_client_secret', True))

    config['ford_llm_base_url'] = base_url or FORD_LLM_BASE_URL
    config['ford_llm_model'] = model or FORD_LLM_DEFAULT_MODEL

    if bearer_token:
        config['ford_llm_bearer_token'] = bearer_token
    elif not preserve_existing_token:
        config['ford_llm_bearer_token'] = ''

    if client_id:
        config['ford_llm_client_id'] = client_id
    elif not preserve_existing_client_id:
        config['ford_llm_client_id'] = ''

    if client_secret:
        config['ford_llm_client_secret'] = client_secret
    elif not preserve_existing_client_secret:
        config['ford_llm_client_secret'] = ''

    save_config(config)
    return jsonify({
        'ok': True,
        'message': 'Ford LLM settings saved to config.json',
        'has_client_credentials': bool(config.get('ford_llm_client_id') and config.get('ford_llm_client_secret')),
        'has_bearer_token': bool(config.get('ford_llm_bearer_token')),
    })


@app.route('/api/ford-llm-settings/test', methods=['POST'])
def api_test_ford_llm_settings():
    settings = get_ford_llm_settings()
    proxies, _ = build_proxy_config(settings['proxy_url'])

    try:
        token, token_source = resolve_ford_llm_token(settings)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        response = requests.get(
            f"{settings['base_url']}/models",
            headers={'Authorization': f'Bearer {token}'},
            proxies=proxies,
            timeout=(20, 30),
        )
        if response.status_code >= 400:
            hint = None
            if response.status_code in (401, 403):
                hint = 'Ford LLM access may still need API Catalog approval for this client or identity.'
            return jsonify({
                'ok': False,
                'error': f'Ford LLM returned HTTP {response.status_code}: {response.text[:500]}',
                'hint': hint,
            }), 400

        data = response.json()
        models = []
        if isinstance(data, dict):
            models = data.get('data') or data.get('models') or []
        elif isinstance(data, list):
            models = data

        model_names = []
        for item in models[:20]:
            if isinstance(item, dict):
                model_names.append(item.get('id') or item.get('name') or str(item))
            else:
                model_names.append(str(item))

        return jsonify({
            'ok': True,
            'message': 'Ford LLM connection succeeded',
            'auth_source': token_source,
            'model_count': len(models),
            'models': model_names,
            'selected_model': settings['model'],
        })
    except requests.RequestException as e:
        return jsonify({'ok': False, 'error': f'Ford LLM test request failed: {str(e)}'}), 400
    except ValueError:
        return jsonify({'ok': False, 'error': 'Ford LLM returned invalid JSON from /models'}), 400

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return ('', 204)


@app.route('/api/scan')
def api_scan():
    if not os.path.exists(CBOOK_DIR):
        return jsonify({'error': f'C-BOOK folder not found: {CBOOK_DIR}'}), 404

    files = []
    print("[DEBUG] Scanning C-BOOK directory for C files...")
    print(f"[DEBUG] CBOOK_DIR = {CBOOK_DIR}")
    print(f"[DEBUG] CBOOK_DIR exists: {os.path.exists(CBOOK_DIR)}")
    found_count = 0
    for root, dirs, filenames in os.walk(CBOOK_DIR):
        rel_folder = os.path.relpath(root, CBOOK_DIR)
        depth = 0 if rel_folder == '.' else rel_folder.count(os.sep) + 1
        if depth > 2:
            # Do not descend further
            dirs[:] = []
            continue
        folder_label = rel_folder.replace('\\', '/').replace('\\', '/') if rel_folder != '.' else '(root)'
        print(f"[DEBUG] Checking directory: {root} (depth={depth})")
        print(f"[DEBUG]   All files in this directory: {filenames}")
        for fname in filenames:
            print(f"[DEBUG]   Checking file: {fname}, ends with .c: {fname.lower().endswith('.c')}")
            if fname.lower().endswith('.c'):
                fpath = os.path.join(root, fname)
                print(f"[DEBUG] ✓ Found C file: {fpath} (folder label: {folder_label})")
                try:
                    size_mb = round(os.path.getsize(fpath) / 1048576, 1)
                    files.append({
                        'name': fname,
                        'path': fpath,
                        'size_mb': size_mb,
                        'folder': folder_label,
                    })
                    found_count += 1
                except Exception as e:
                    print(f"[DEBUG] ERROR getting file size: {e}")
    print(f"[DEBUG] Total C files found: {found_count}")
    print(f"[DEBUG] Returning files: {[f['path'] for f in files]}")
    resp = jsonify({'files': files})
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/scan-cal-files', methods=['POST'])
def api_scan_cal_files():
    filepath = request.json.get('filepath', '')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    search_dir = os.path.dirname(filepath)
    cal_files  = []

    def is_generated_text_file(name):
        lower = name.lower()
        return (
            lower.endswith('_features.txt')
            or lower.endswith('_description.txt')
            or lower.endswith('_obd_description.txt')
            or lower == 'dtc_list.txt'
            or lower.startswith('dtc_list_')
        )

    for entry in sorted(os.scandir(search_dir), key=lambda item: item.name.lower()):
        if not entry.is_file():
            continue
        lower_name = entry.name.lower()
        if lower_name.endswith('.csv'):
            cal_files.append({
                'name':    entry.name,
                'path':    entry.path,
                'rel':     entry.name,
                'size_kb': round(os.path.getsize(entry.path) / 1024, 1),
            })
            continue
        if lower_name.endswith('.txt') and not is_generated_text_file(entry.name):
            cal_files.append({
                'name':    entry.name,
                'path':    entry.path,
                'rel':     entry.name,
                'size_kb': round(os.path.getsize(entry.path) / 1024, 1),
            })
    return jsonify({'files': cal_files})


@app.route('/api/extract-features', methods=['POST'])
def api_extract_features():
    filepath = request.json.get('filepath', '')
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    features = analyze_c_code_features(filepath)
    normalize_pack_generated_outputs(os.path.dirname(filepath), features)
    out = os.path.splitext(filepath)[0] + '_features.txt'
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(features) + '\n')

    return jsonify({'features': features, 'count': len(features), 'saved_to': out})


@app.route('/api/extract-module', methods=['POST'])
def api_extract_module():
    data    = request.json
    filepath = data.get('filepath', '')
    keyword  = data.get('keyword', '').strip().upper()

    if not filepath or not keyword:
        return jsonify({'error': 'Missing filepath or keyword'}), 400

    out_file, count = extract_module_code(filepath, keyword)
    if not out_file:
        return jsonify({'error': f"Module '{keyword}' was not found"}), 404

    with open(out_file, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    is_obd, obd_hits = detect_obd(content)
    dtc_catalog = extract_dtc_catalog(content)

    return jsonify({
        'output_file': out_file,
        'sections':    count,
        'line_count':  content.count('\n'),
        'char_count':  len(content),
        'est_tokens':  len(content) // 4,
        'is_obd':      is_obd,
        'obd_hits':    obd_hits,
        'dtc_codes':   dtc_catalog['all'],
        'dtc_catalog': dtc_catalog,
    })


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    data        = request.json
    module_file = data.get('module_file', '')
    atype       = data.get('type', 'general')   # 'general' | 'obd'
    keyword     = data.get('keyword', 'MODULE')
    cal_file    = data.get('cal_file', '')
    source_c_file = data.get('source_c_file', '')
    analysis_uds_mode = normalize_uds_mode(data.get('analysis_uds_mode', None))
    analysis_dtc_codes = []
    analysis_classification_mode = 'auto'
    classification_mode_label = 'Auto (Prompt rules + runtime evidence blocks)'

    if not os.path.exists(module_file):
        return jsonify({'error': 'Module file not found'}), 404

    # Read module code and calibration data to include in user prompt
    try:
        with open(module_file, 'r', encoding='utf-8', errors='replace') as f:
            full_module_code = f.read()
            module_code = full_module_code[:50000]  # Limit main body to first 50K chars for token management
    except Exception as e:
        return jsonify({'error': f'Failed to read module file: {str(e)}'}), 500

    # Extract FD blocks from the full source file (not token-limited)
    fd_blocks_text = extract_fd_blocks(module_file)

    dtc_catalog = extract_dtc_catalog(full_module_code)
    analysis_dtc_codes = filter_dtc_codes_by_uds_mode(dtc_catalog.get('diagnostic') or dtc_catalog.get('all') or [], analysis_uds_mode)

    if cal_file and os.path.exists(cal_file):
        cal_dtc_items, cal_dtc_map = get_cal_dtc_switch_items(cal_file, analysis_uds_mode)
        if cal_dtc_items or cal_dtc_map:
            recovered_codes = set()
            for item in cal_dtc_items:
                code = (item.get('dtc') or '').strip().upper()
                if code:
                    recovered_codes.add(code)
            for code in cal_dtc_map.keys():
                normalized = (code or '').strip().upper()
                if normalized:
                    recovered_codes.add(normalized)
            filtered_recovered_codes = set(filter_dtc_codes_by_uds_mode(recovered_codes, analysis_uds_mode))
            if filtered_recovered_codes and not analysis_dtc_codes:
                analysis_dtc_codes = sorted(filtered_recovered_codes)

    ordered_analysis_dtcs = []
    seen_analysis_dtcs = set()
    for code in analysis_dtc_codes or []:
        normalized = (code or '').strip().upper()
        if not normalized or normalized in seen_analysis_dtcs:
            continue
        ordered_analysis_dtcs.append(normalized)
        seen_analysis_dtcs.add(normalized)

    # Determine which DTCs are unused ('-') or disabled (enable SW = 0) from calibration data
    unused_dtcs = set()
    disabled_dtcs = set()
    if cal_file and os.path.exists(cal_file):
        _sw_items, _sw_map = get_cal_dtc_switch_items(cal_file, analysis_uds_mode)
        raw_switch_map = {}
        for name, value in unique_cal_entries(parse_cal_data(cal_file)):
            dtc_code = get_dtc_code_from_switch_parameter(re.sub(r'\[.*?\]', '', name).strip())
            if not dtc_code:
                continue
            if not dtc_matches_uds_mode(dtc_code, analysis_uds_mode):
                continue
            raw_switch_map.setdefault(dtc_code, []).append(str(value).strip())
        for dtc in ordered_analysis_dtcs:
            raw_values = []
            for map_key, values in raw_switch_map.items():
                if dtc_code_related(map_key, [dtc], analysis_uds_mode):
                    raw_values.extend(values)
            if raw_values and all(is_sw_unused(value) for value in raw_values):
                unused_dtcs.add(dtc)
                continue

            matched_items = []
            for map_key, items in _sw_map.items():
                if dtc_code_related(map_key, [dtc], analysis_uds_mode):
                    matched_items.extend(items)
            # If SW items exist for this DTC and ALL of them are 0 → disabled
            if matched_items and all(is_sw_zero(item.get('value')) for item in matched_items):
                disabled_dtcs.add(dtc)

    ordered_analysis_dtcs = [d for d in ordered_analysis_dtcs if d not in unused_dtcs]
    active_dtcs = [d for d in ordered_analysis_dtcs if d not in disabled_dtcs]
    classification_dtc_scope = active_dtcs or ordered_analysis_dtcs or dtc_catalog.get('diagnostic') or dtc_catalog.get('all') or []
    obd_dtc_block_mode = bool((atype or '').strip().lower() == 'obd')
    large_obd_batch_mode = bool(obd_dtc_block_mode and len(active_dtcs) > 12)
    obd_batch_size = 8

    try:
        debug_payload = {
            'pid': os.getpid(),
            'keyword': keyword,
            'analysis_classification_mode': analysis_classification_mode,
            'classification_mode_label': classification_mode_label,
            'ordered_analysis_dtcs': ordered_analysis_dtcs[:20],
            'active_dtcs_count': len(active_dtcs),
            'disabled_dtcs_count': len(disabled_dtcs),
            'obd_dtc_block_mode': obd_dtc_block_mode,
            'large_obd_batch_mode': large_obd_batch_mode,
            'app_file': __file__,
        }
        debug_path = os.path.join(BASE_DIR, 'tmp_last_analyze_debug.json')
        with open(debug_path, 'w', encoding='utf-8') as debug_file:
            json.dump(debug_payload, debug_file, ensure_ascii=False, indent=2)
    except Exception:
        pass

    generation_keyword = re.sub(r'[^A-Za-z0-9_]+', '_', (keyword or 'module').strip()) or 'module'
    generation_debug_path = os.path.join(BASE_DIR, f'tmp_generation_debug_{generation_keyword}.json')
    generation_report_path = os.path.join(BASE_DIR, f'tmp_generated_report_{generation_keyword}.html')
    generation_raw_source_path = os.path.join(BASE_DIR, f'tmp_generated_raw_source_{generation_keyword}.txt')

    system_prompt = build_report_prompt(keyword, requested_mode=atype, classification_mode_label=classification_mode_label)

    deterministic_monitoring_enable_block = ''

    prompt_dtc_scope = classification_dtc_scope
    hybrid_guardrail_mode = True
    current_module_context_block = ''
    control_module_context_block = ''
    module_dtc_architecture_block = ''
    related_module_context_block = ''

    if not obd_dtc_block_mode:
        control_excerpt = extract_control_path_excerpt(full_module_code)
        if control_excerpt:
            control_module_context = '\n'.join(control_excerpt)
            control_module_context_block = f"""
[CURRENT MODULE CONTROL / FUNCTION EVIDENCE EXCERPTS — from full source file]
{control_module_context}
- These excerpts are intended to anchor Function / Combined analysis in actual control-path, task, state, command, and output logic when such evidence is visible.
"""

    if prompt_dtc_scope:
        current_module_excerpt = extract_dtc_line_excerpt(full_module_code, prompt_dtc_scope)
        current_module_context = '\n'.join(current_module_excerpt) if current_module_excerpt else '(No targeted DTC excerpt found in full source file.)'
        current_module_context_block = f"""
[CURRENT MODULE DTC EVIDENCE EXCERPTS — from full source file]
{current_module_context}
- These excerpts come from the full module source, including lines beyond the first 50K chars.
- Treat these excerpts as primary current-module evidence for DTC-specific enable, fail, pass/clear, pending, latch, and completion behavior.
"""

    module_dtc_architecture = build_module_dtc_architecture_context(
        full_module_code,
        keyword,
        classification_dtc_scope,
        analysis_uds_mode,
    )
    if module_dtc_architecture:
        module_dtc_architecture_block = f"""
{module_dtc_architecture}
"""

    disabled_dtcs_block = ''
    if disabled_dtcs:
        disabled_list = '\n'.join(f'- {code}: calibration disabled (enable SW = 0)' for code in sorted(disabled_dtcs))
        disabled_dtcs_block = (
            '\n\n[CALIBRATION-DISABLED DTCs]\n'
            'The following DTCs are disabled in the current project because their enable switch is 0.\n'
            'If the final classification is `OBD Only (Diagnostic)` or `Function + OBD (Combined)`, mention them once under `Disabled DTCs (Not In Use)` and do not create detailed DTC blocks for them.\n'
            f'{disabled_list}\n'
        )

    hybrid_disabled_dtcs_block = ''
    dtc_mode_contract_block = ''
    obd_expression_contract_block = ''
    hybrid_output_contract_block = ''
    control_path_focus_block = ''
    if analysis_uds_mode is False:
        allowed_non_uds = ', '.join(ordered_analysis_dtcs) if ordered_analysis_dtcs else '(none detected)'
        dtc_mode_contract_block = f"""
[DTC MODE CONTRACT]
- Selected DTC mode: Non-UDS only.
- In this run, do NOT output UDS suffix variants such as `Pxxxx-yy` anywhere in the report.
- If a base DTC exists, keep only the non-UDS base form (example: use `P0544`, never `P0544-16`).
- Allowed non-UDS DTCs for this run: {allowed_non_uds}
"""
    elif analysis_uds_mode is True:
        allowed_uds = ', '.join(ordered_analysis_dtcs) if ordered_analysis_dtcs else '(none detected)'
        dtc_mode_contract_block = f"""
[DTC MODE CONTRACT]
- Selected DTC mode: UDS only.
- In this run, do NOT collapse UDS codes into non-UDS base forms.
- Allowed UDS DTCs for this run: {allowed_uds}
"""

    obd_expression_contract_block = ""

    if hybrid_guardrail_mode:
        hybrid_output_contract_block = """
[COMBINED OUTPUT CONTRACT]
- If you classify this module as `Function + OBD (Combined)`, keep a clear split between function analysis and diagnostic analysis.
- Prefer the natural report structure produced from current-module code, FD blocks, and calibration evidence.
- Never use the sentence `No dedicated code logic visible in provided scope.` in Combined output.
"""
        control_path_focus_block = """
[CONTROL-PATH PRIORITY FOR FUNCTION ANALYSIS]
- For control modules, first explain the base functional strategy before diagnostics.
- Explicitly cover the main control flow as `Input -> Process -> Output` using current calibration values where available.
- Include control states / mode transitions, main calculation branches, restriction / temperature / regeneration behavior, and output flags/commands when evidenced in code or FD blocks.
- Do not let DTC summaries replace the base functional explanation.
"""

    dtc_owner_lookup = {}
    dtc_list_file_for_owner = find_dtc_list_file(module_file)
    dtc_module_map_for_owner = parse_dtc_module_map_from_list(dtc_list_file_for_owner) if dtc_list_file_for_owner else {}
    current_module_upper = (keyword or '').strip().upper()
    for dtc in ordered_analysis_dtcs:
        owners = []
        for candidate_dtc, modules in dtc_module_map_for_owner.items():
            if not dtc_code_related(candidate_dtc, [dtc], analysis_uds_mode):
                continue
            for module in modules:
                module_key = (module or '').strip().upper()
                if module_key and module_key not in owners:
                    owners.append(module_key)
        if not owners and current_module_upper:
            owners = [current_module_upper]
        if owners:
            dtc_owner_lookup[format_uds_dtc(dtc)] = owners
            dtc_owner_lookup[compact_dtc_code(dtc)[:5]] = owners

    def extract_chunk_content_text(chunk):
        line = (chunk or '').strip()
        if not line.startswith('data: '):
            return ''
        payload_text = line[6:].strip()
        if payload_text == '[DONE]':
            return ''
        try:
            payload_obj = json.loads(payload_text)
        except Exception:
            return ''
        return extract_llm_payload_text(payload_obj)

    def report_is_hybrid(report_text):
        text = report_text or ''
        return bool(re.search(r'(?i)classification\s*:\s*.*\b(?:combined|hybrid)\b|\bfunction\s*\+\s*obd\s*\(\s*(?:combined|hybrid)\s*\)', text))

    def report_is_obd_only(report_text):
        text = report_text or ''
        return bool(re.search(r'(?i)^\s*classification\s*:\s*obd\s+only\s*\(\s*diagnostic\s*\)\s*$', text, re.MULTILINE))

    def find_dtc_mode_violations(report_text):
        text = report_text or ''
        if analysis_uds_mode is None:
            return []

        seen = []
        for code in sorted(set(re.findall(r'(?i)\b[PBCU][0-9][0-9A-F]{3}(?:-[0-9A-F]{2})?\b', text))):
            normalized = code.upper()
            is_uds_code = '-' in normalized
            if analysis_uds_mode is False and is_uds_code:
                seen.append(normalized)
            elif analysis_uds_mode is True and (not is_uds_code):
                seen.append(normalized)
        return seen

    def build_dtc_mode_rewrite_prompt(draft_text, violations):
        allowed_codes = ', '.join(ordered_analysis_dtcs) if ordered_analysis_dtcs else '(none detected)'
        mode_label = uds_mode_label(analysis_uds_mode)
        forbidden = ', '.join(sorted(set(violations))) if violations else '(none)'
        return f"""Rewrite the report from scratch while respecting the selected DTC mode.

Do NOT continue the earlier draft.
Do NOT append a correction addendum.
Output a clean replacement report body only.

[INVALID DRAFT - FOR REFERENCE ONLY]
{draft_text}

[INVALID DTC MODE OUTPUT]
- Selected DTC mode: {mode_label}
- Codes forbidden by this mode but present in the draft: {forbidden}
- Allowed codes for this run: {allowed_codes}

[MANDATORY REWRITE RULES]
- Keep the same module and calibration context as the original request.
- Respect the selected DTC mode strictly.
- If mode is `Non-UDS only`, never output any suffix variants like `P0420-00` or `P0544-16` anywhere in the report.
- If mode is `UDS only`, never collapse the report into base non-UDS forms when a suffix-coded UDS form is in scope.
- Keep the original requested report structure unless it conflicts with the DTC mode rule.

[SOURCE OF TRUTH]
{user_prompt}"""

    def find_hybrid_report_gaps(report_text):
        text = report_text or ''
        gaps = {
            'missing_sections': [],
            'wrong_order': False,
            'forbidden_sections': [],
            'stubbed_dtcs': [],
            'disallowed_dtc_codes': [],
        }

        section_patterns = [
            ('Function Analysis', re.compile(r'(?im)^\s*#{1,6}\s*Function\s*Analysis\b')),
            ('OBD Analysis', re.compile(r'(?im)^\s*#{1,6}\s*OBD\s*Analysis\b')),
        ]
        positions = []
        for label, pattern_obj in section_patterns:
            match = pattern_obj.search(text)
            if not match:
                gaps['missing_sections'].append(label)
            else:
                positions.append(match.start())

        if len(positions) >= 2 and positions != sorted(positions):
            gaps['wrong_order'] = True

        for legacy_heading in (
            r'(?im)^\s*#{1,6}\s*Part-1:\s*Base\s*Function\s*Analysis\b',
            r'(?im)^\s*#{1,6}\s*Part-2:\s*OBD\s*Analysis\b',
            r'(?im)^\s*#{1,6}\s*Part-3:\s*Coupling\s*Summary\b',
        ):
            if re.search(legacy_heading, text):
                gaps['forbidden_sections'].append('legacy Combined Part heading')

        dtc_matches = list(re.finditer(r'(?im)^\s*#{1,6}\s*DTC\s+([A-Z0-9_-]+)\b', text))

        for idx, match in enumerate(dtc_matches):
            code = match.group(1)
            start = match.start()
            end = dtc_matches[idx + 1].start() if idx + 1 < len(dtc_matches) else len(text)
            dtc_block = text[start:end]
            if analysis_uds_mode is False and '-' in code:
                gaps['disallowed_dtc_codes'].append(code)
            elif analysis_uds_mode is True and '-' not in code and ordered_analysis_dtcs:
                gaps['disallowed_dtc_codes'].append(code)
            if re.search(r'(?i)no dedicated code logic visible in provided scope|no logic evidence in provided code scope', dtc_block):
                gaps['stubbed_dtcs'].append(code)

        return gaps

    def has_hybrid_quality_gaps(gaps):
        return bool(
            gaps['missing_sections']
            or gaps['wrong_order']
            or gaps['forbidden_sections']
            or gaps['stubbed_dtcs']
            or gaps['disallowed_dtc_codes']
        )

    def build_hybrid_rewrite_prompt(draft_text, gaps):
        missing_sections = ', '.join(gaps['missing_sections']) if gaps['missing_sections'] else '(none)'
        forbidden_sections = ', '.join(gaps['forbidden_sections']) if gaps['forbidden_sections'] else '(none)'
        stubbed_dtcs = ', '.join(gaps['stubbed_dtcs']) if gaps['stubbed_dtcs'] else '(none)'
        disallowed_dtc_codes = ', '.join(gaps['disallowed_dtc_codes']) if gaps['disallowed_dtc_codes'] else '(none)'
        wrong_order = 'yes' if gaps['wrong_order'] else 'no'
        return f"""Rewrite the report from scratch as a proper Combined report.

Do NOT continue the earlier draft.
Do NOT append a correction addendum.
Output a clean replacement report body only.

[INVALID DRAFT - FOR REFERENCE ONLY]
{draft_text}

[INVALID COMBINED STRUCTURE]
- Missing sections: {missing_sections}
- Wrong top-level section order: {wrong_order}
- Forbidden top-level sections: {forbidden_sections}
- Stub DTC blocks to eliminate: {stubbed_dtcs}
- DTC codes forbidden by current DTC mode: {disallowed_dtc_codes}

[MANDATORY REWRITE RULES]
- Keep the same module and calibration context as the original request.
- If the module is classified as Combined, output exactly these top-level headings in this order:
    `## Function Analysis`
    optional `## Disabled DTCs (Not In Use)`
    `## OBD Analysis`
- `## Function Analysis` must use the same shared Function format used for Function Only output.
- `## OBD Analysis` should stay evidence-based and easy to read, but do not force a rigid subsection template beyond what the source naturally supports.
- If a DTC is calibration-disabled, you may use the same `## Disabled DTCs (Not In Use)` section style as OBD-only output.
- Do NOT use the sentence `No dedicated code logic visible in provided scope.`
- Respect the selected DTC mode strictly. If this run is Non-UDS, never output suffix variants like `P0544-16`; keep only the base non-UDS code.
- Use current-module code, FD blocks, and calibration values as the primary basis.

[SOURCE OF TRUTH]
{user_prompt}"""

    def find_report_quality_issues(report_text):
        text = report_text or ''
        issues = {
            'placeholder_formulas': [],
            'speculative_phrases': [],
            'cross_dtc_references': [],
            'generic_alias_formulas': [],
            'forbidden_sections': [],
            'duplicate_lines': [],
            'forbidden_tables': [],
            'missing_visible_tokens': [],
        }

        top_level_uncertainty_re = re.compile(
            r'(?i)^\s*(?:classification\s*:.*need\s+confirmation|classification\s+uncertain\b|temporary\s+classification\s*:.*need\s+confirmation)'
        )

        placeholder_re = re.compile(
            r'(?i)(token\s+N/?A|source token\s+N/?A|exact [^\n]{0,80} token\s+N/?A|exact [^\n]{0,80} token not visible|exact source variable is not evidenced)'
        )
        speculative_re = re.compile(r'(?i)\blikely\b|\bplausible\b|\bpossibly\b|needs? confirmation|\bambiguity\b|typical intent|typical recovery|likely shares|likely inherited|shared .* likely|candidate family|companion polarity|same owning family as')
        cross_dtc_re = re.compile(r'(?i)same evidence state as|same evidence as the previous dtc|same .* as p\d{4}|^[-*]\s*same\b.*\bp\d{4}|companion polarity|same owning family as')
        comparator_re = re.compile(r'<=|>=|==|<|>')
        generic_alias_re = re.compile(r'(?i)coolant-related(?:\s+entry)?\s+variable|ambient-related(?:\s+entry)?\s+variable|temperature-related\s+variable|speed-related\s+variable|airflow-related\s+variable|load-related\s+variable|pressure-related\s+variable|entry variable|measured signal|sensor value|monitored value|runtime value|engine speed|vehicle speed|coolant temperature|ambient temperature')
        forbidden_section_re = re.compile(r'(?im)^\s*#{1,6}\s*Real activation scenario(?: from current project cal)?\b')
        markdown_table_re = re.compile(r'(?m)^\|.+\|\s*$')

        if forbidden_section_re.search(text):
            issues['forbidden_sections'].append('Real activation scenario from current project cal')
        if markdown_table_re.search(text):
            issues['forbidden_tables'].append('Markdown table detected in report output')
        if '<table>' in text.lower():
            issues['forbidden_tables'].append('HTML table detected in report output')
        if re.search(r'(?i)compared source tokens .* not visible in the provided scope', text):
            issues['missing_visible_tokens'].append('Enable path hides visible source tokens and falls back to prose instead of exact comparator variables')

        seen_lines = {}

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if top_level_uncertainty_re.search(line):
                continue
            if placeholder_re.search(line) and len(issues['placeholder_formulas']) < 8:
                issues['placeholder_formulas'].append(line[:240])
            if speculative_re.search(line) and len(issues['speculative_phrases']) < 8:
                issues['speculative_phrases'].append(line[:240])
            if cross_dtc_re.search(line) and len(issues['cross_dtc_references']) < 8:
                issues['cross_dtc_references'].append(line[:240])
            if comparator_re.search(line) and generic_alias_re.search(line) and len(issues['generic_alias_formulas']) < 8:
                issues['generic_alias_formulas'].append(line[:240])

            normalized_line = re.sub(r'`[^`]+`', '`CODE`', line.lower())
            normalized_line = re.sub(r'\([^)]{0,60}\)', '()', normalized_line)
            if len(normalized_line) >= 48:
                seen_count = seen_lines.get(normalized_line, 0) + 1
                seen_lines[normalized_line] = seen_count
                if seen_count == 2 and len(issues['duplicate_lines']) < 8:
                    issues['duplicate_lines'].append(line[:240])

        return issues

    def has_report_quality_issues(issues):
        return bool(
            issues['placeholder_formulas']
            or issues['speculative_phrases']
            or issues['cross_dtc_references']
            or issues['generic_alias_formulas']
            or issues['forbidden_sections']
            or issues['duplicate_lines']
            or issues['forbidden_tables']
            or issues['missing_visible_tokens']
        )

    def build_report_quality_rewrite_prompt(draft_text, issues):
        placeholder_lines = '\n'.join(f'- {line}' for line in issues['placeholder_formulas']) or '- (none)'
        speculative_lines = '\n'.join(f'- {line}' for line in issues['speculative_phrases']) or '- (none)'
        cross_ref_lines = '\n'.join(f'- {line}' for line in issues['cross_dtc_references']) or '- (none)'
        generic_alias_lines = '\n'.join(f'- {line}' for line in issues['generic_alias_formulas']) or '- (none)'
        forbidden_section_lines = '\n'.join(f'- {line}' for line in issues['forbidden_sections']) or '- (none)'
        duplicate_lines = '\n'.join(f'- {line}' for line in issues['duplicate_lines']) or '- (none)'
        forbidden_table_lines = '\n'.join(f'- {line}' for line in issues['forbidden_tables']) or '- (none)'
        missing_visible_token_lines = '\n'.join(f'- {line}' for line in issues['missing_visible_tokens']) or '- (none)'
        return f"""Rewrite the report from scratch to remove malformed diagnostic phrasing.

Do NOT continue the earlier draft.
Do NOT append a correction addendum.
Output a clean replacement report body only.

[INVALID DRAFT - FOR REFERENCE ONLY]
{draft_text}

[PROBLEMS TO REMOVE]
- Placeholder formulas / fake tokens:
{placeholder_lines}
- Speculative hedging to remove:
{speculative_lines}
- Cross-DTC shortcut references to remove:
{cross_ref_lines}
- Generic natural-language comparator aliases to remove:
{generic_alias_lines}
- Forbidden section headings to remove:
{forbidden_section_lines}
- Duplicate / repetitive lines to compress:
{duplicate_lines}
- Tables to remove:
{forbidden_table_lines}
- Missing visible source tokens to restore:
{missing_visible_token_lines}

[MANDATORY REWRITE RULES]
- Keep the same module, calibration, and DTC scope as the original request.
- Never write pseudo-equations containing placeholders such as `token N/A`, `exact ... token`, `not evidenced`, `unknown`, or `not visible`.
- If the exact source token is missing, replace that entire formula bullet with one short prose bullet stating that the compared source token is not visible in the provided scope.
- Across Function Only, Combined, and OBD sections alike, do NOT write natural-language comparator aliases such as `coolant-related entry variable`, `ambient-related entry variable`, `engine speed`, `vehicle speed`, `measured signal`, or `runtime value` inside formula bullets. Use the exact code token when visible; otherwise use one short prose bullet.
- Keep every DTC block self-contained. Do not say `same evidence state as ...` or similar shortcuts.
- Remove speculative hedges and filler such as `likely`, `plausible`, `possibly`, `Typical intent`, `Typical recovery`, `Likely shares`, `Candidate family`, `Same owning family as`, `companion polarity`, and `Needs confirmation`.
- Top-level classification uncertainty may remain when evidence is insufficient, for example `Classification: Need confirmation` or `Classification uncertain`, but keep that allowance out of per-DTC blocks.
- For unresolved ownership, use one concise prose bullet limited to evidenced facts. Do not pad the section with assumed family narratives or imported gates from other candidate families.
- Remove any section named `Real activation scenario` or `Real activation scenario from current project cal`.
- Compress repetitive content. If multiple bullets restate the same fact, keep only the most concrete version.
- Keep the report slide-friendly: short bullets, no long prose blocks, and no repeated restatement of the same active strategy or calibration fact.
- Remove all markdown or HTML tables and rewrite their content as short bullets.
- Do not create a calibration interpretation table or calibration inventory table section.
- If a calibration delta matters, convert it into one concise bullet under the relevant subsection.
- Prefer direct DTC ownership artifacts such as `FMC_*_REF`, `dtc_*_ptr`, `*_dtc_code`, ROM definition tables, and switch definitions over broader FD similarity. If a direct DTC mapping exists, use that owning family and do not reassign it to another family just because the prose intent sounds similar.
- Never restate a calibration switch or numeric value unless the exact assignment is visible in the provided current-module or related-module scope. Do not invent conflicting values.
- Do not conflate KOER / self-test-only excursion diagnostics with continuous lack-of-movement diagnostics.
- If a subsection already concludes `Not evidenced after current-module, FD-block, and related-module search`, do not append fallback bullets that speculate about a typical monitor path.
- In per-DTC OBD output, never use bullet labels such as `Likely monitor family`, `Candidate family`, `Need confirmation`, or `Same family as ...`.
- If exact DTC-specific ownership/comparator/mapping is unresolved, use one short factual bullet such as `Exact DTC-specific comparator/mapping is not evidenced in current-module, FD-block, and related-module search.` and stop that subsection.
- Preserve the intended report structure and all concretely evidenced logic.
- When a visible source token exists in the provided code, use that token in the enable-condition comparator instead of saying it is not visible.
- If code shows one runtime/source variable bounded by min/max calibrations, prefer a compact range form such as `cal_min (...) <= src_var <= cal_max (...) -> enable`.
- In per-DTC OBD output, keep the wording factual and grounded in visible code/cal evidence, but do not force a single fixed subsection template.

[SOURCE OF TRUTH]
{user_prompt}"""

    def sanitize_generated_report(report_text):
        text = report_text or ''
        if not text.strip():
            return text

        hard_gap_re = re.compile(r'(?i)not evidenced after current-module, fd-block, and related-module search|exact .* not evidenced in current-module, fd-block, and related-module search')
        soft_gap_re = re.compile(r'(?i)not explicitly evidenced|code-link confirmation needed|exact .* not visible|exact .* not evidenced')
        speculative_re = re.compile(r'(?i)\blikely\b|\bplausible\b|\bpossibly\b|needs? confirmation|candidate family|same owning family as|companion polarity')
        cross_dtc_re = re.compile(r'(?i)same evidence state as|same evidence as the previous dtc|same .* as p\d{4}|same owning family as|companion polarity')
        generic_alias_formula_re = re.compile(r'(?i)(?:<=|>=|==|<|>).*(coolant-related(?:\s+entry)?\s+variable|ambient-related(?:\s+entry)?\s+variable|temperature-related\s+variable|speed-related\s+variable|airflow-related\s+variable|load-related\s+variable|pressure-related\s+variable|entry variable|measured signal|sensor value|monitored value|runtime value|engine speed|vehicle speed|coolant temperature|ambient temperature)')
        dtc_heading_re = re.compile(r'^###\s+DTC\s+')
        subsection_heading_re = re.compile(r'^####\s+\d+\.\s+')
        forbidden_heading_re = re.compile(r'(?im)^\s*#{1,6}\s*Real activation scenario(?: from current project cal)?\b')
        markdown_table_row_re = re.compile(r'^\|.+\|\s*$')
        generic_gap_bullet = '- Exact DTC-specific comparator/mapping is not evidenced in current-module, FD-block, and related-module search.'
        obd_postlude_heading_re = re.compile(
            r'(?im)^\s*(?:#{1,6}\s*)?(?:'
            r'Priority\s+Action\s+List|'
            r'High-risk\s+Delta\s+Summary|'
            r'Fallback\s+Summary|'
            r'Base\s+Function\s+Summary|'
            r'Calibration\s+Snapshot|'
            r'Part-1:\s*Base\s*Function\s*Analysis|'
            r'Part-2:\s*OBD\s*Analysis|'
            r'Part-3:\s*Coupling\s*Summary|'
            r'A\.\s*Identity\s*&\s*Scope|'
            r'B\.\s*Entry\s*&\s*Enable\s*Conditions|'
            r'C\.\s*Core\s*Logic\s*&\s*Calculation|'
            r'D\.\s*Output\s*&\s*Action|'
            r'E\.\s*Plain-language\s*Takeaway|'
            r'F\.\s*Calibration\s*Interpretation|'
            r'G\.\s*Calibration\s*Presentation|'
            r'H\.\s*Final\s*Additional\s*Output'
            r')\b'
        )

        def enforce_obd_block_output(raw_text):
            dtc_matches = list(re.finditer(r'(?im)^###\s+DTC\s+[A-Z0-9_-]+\b', raw_text))
            if not dtc_matches:
                return raw_text

            classification_match = re.search(r'(?im)^\s*classification\s*:\s*.+$', raw_text)
            classification_line = classification_match.group(0).strip() if classification_match else 'Classification: OBD Only (Diagnostic)'

            first_dtc_start = dtc_matches[0].start()
            disabled_match = re.search(r'(?im)^\s*(?:#{1,6}\s*)?Disabled\s+DTCs\s*\(\s*Not\s*In\s*Use\s*\)\s*$', raw_text)
            disabled_block = ''
            if disabled_match and disabled_match.start() < first_dtc_start:
                disabled_start = disabled_match.start()
                disabled_end = first_dtc_start
                disabled_block = raw_text[disabled_start:disabled_end].strip()

            dtc_blocks = []
            for idx, match in enumerate(dtc_matches):
                start = match.start()
                next_start = dtc_matches[idx + 1].start() if idx + 1 < len(dtc_matches) else len(raw_text)
                block_text = raw_text[start:next_start]
                postlude_match = obd_postlude_heading_re.search(block_text)
                if postlude_match and postlude_match.start() > 0:
                    block_text = block_text[:postlude_match.start()]
                block_text = block_text.strip()
                if block_text:
                    dtc_blocks.append(block_text)

            if not dtc_blocks:
                return raw_text

            rebuilt_parts = [classification_line]
            if disabled_block:
                rebuilt_parts.extend(['', disabled_block])
            rebuilt_parts.extend([''] + dtc_blocks)
            return '\n\n'.join(part for part in rebuilt_parts if part.strip()).strip() + '\n'

        def strip_forbidden_sections(raw_text):
            lines = raw_text.splitlines()
            cleaned_lines = []
            skipping = False
            current_level = 0

            for line in lines:
                heading_match = re.match(r'^(#{1,6})\s+', line.strip())
                if forbidden_heading_re.match(line):
                    skipping = True
                    current_level = len(heading_match.group(1)) if heading_match else 1
                    continue
                if skipping and heading_match and len(heading_match.group(1)) <= current_level:
                    skipping = False
                if skipping:
                    continue
                cleaned_lines.append(line)

            return '\n'.join(cleaned_lines)

        def sanitize_block(block_lines):
            if not block_lines:
                return []

            heading = block_lines[0]
            body = block_lines[1:]

            hard_gap_line = next((line for line in body if hard_gap_re.search(line.strip())), '')
            if hard_gap_line:
                sanitized = [heading]
                if body and body[0].strip() == '':
                    sanitized.append('')
                sanitized.append(hard_gap_line)
                return sanitized

            sanitized = [heading]
            inserted_gap = False
            body_has_gap = any(hard_gap_re.search(line.strip()) or soft_gap_re.search(line.strip()) for line in body)

            for line in body:
                stripped = line.strip()
                if not stripped:
                    sanitized.append(line)
                    continue
                if markdown_table_row_re.match(stripped):
                    continue
                if cross_dtc_re.search(stripped):
                    continue
                if generic_alias_formula_re.search(stripped):
                    continue
                if speculative_re.search(stripped):
                    if stripped.startswith(('-', '*')):
                        if body_has_gap and not inserted_gap:
                            sanitized.append(generic_gap_bullet)
                            inserted_gap = True
                        continue
                sanitized.append(line)

            return sanitized

        text = strip_forbidden_sections(text)
        lines = text.splitlines()
        sanitized_lines = []
        block = []

        def flush_block():
            nonlocal block, sanitized_lines
            if not block:
                return
            sanitized_lines.extend(sanitize_block(block))
            block = []

        for line in lines:
            if dtc_heading_re.match(line) or subsection_heading_re.match(line):
                flush_block()
                block = [line]
            elif block:
                block.append(line)
            else:
                sanitized_lines.append(line)

        flush_block()
        cleaned = '\n'.join(sanitized_lines)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        if (obd_dtc_block_mode or large_obd_batch_mode or report_is_obd_only(cleaned)) and not report_is_hybrid(cleaned):
            cleaned = enforce_obd_block_output(cleaned)
        return cleaned

    related_module_context = ''
    related_modules = {}
    if prompt_dtc_scope:
        related_module_context, related_modules = build_related_module_context(
            module_file,
            keyword,
            prompt_dtc_scope,
            source_c_file,
            analysis_uds_mode,
        )
        related_module_context_block = f"""
[RELATED DTC MODULE CONTEXT]
{related_module_context or '(No additional DTC-related module context found.)'}
"""

    # Read calibration data if provided
    cal_summary = '(No calibration data provided)'
    cal_data = '(No calibration data provided)'
    cal_meta = {
        'total_rows': 0,
        'included_rows': 0,
        'omitted_rows': 0,
        'row_offset': 0,
        'next_offset': 0,
        'has_more': False,
        'dtc_rows': 0,
        'other_rows': 0,
    }
    if cal_file:
        cal_summary, cal_data, cal_meta = build_calibration_prompt_payload(
            cal_file,
            max_rows=CAL_REPORT_ROWS_PER_PART,
            row_offset=0,
            with_meta=True,
            selected_dtc_codes=prompt_dtc_scope,
            dtc_owner_lookup=dtc_owner_lookup,
            current_module=current_module_upper,
            prioritize_dtc_rows=obd_dtc_block_mode,
        )

    split_required = bool(cal_file and cal_meta.get('has_more'))
    total_rows = int(cal_meta.get('total_rows') or 0)
    total_parts = max((total_rows + CAL_REPORT_ROWS_PER_PART - 1) // CAL_REPORT_ROWS_PER_PART, 1) if split_required else 1
    split_note = (
        f'- Calibration rows are auto-split into {total_parts} parts to preserve coverage. '
        f'Part 1 includes rows {cal_meta.get("row_offset", 0)} to {max(cal_meta.get("next_offset", 0) - 1, 0)}.'
        if split_required else
        '- Calibration rows fit in a single report part.'
    )

    user_prompt = build_analysis_user_prompt(
        keyword=keyword,
        requested_mode=atype,
        classification_mode_label=classification_mode_label,
        cal_file=cal_file,
        cal_summary=cal_summary,
        cal_data=cal_data,
        module_code=module_code,
        current_module_context_block=current_module_context_block,
        control_module_context_block=control_module_context_block,
        module_dtc_architecture_block=module_dtc_architecture_block,
        fd_blocks_text=fd_blocks_text,
        related_module_context_block=related_module_context_block,
        disabled_dtcs_block=disabled_dtcs_block,
        hybrid_disabled_dtcs_block=hybrid_disabled_dtcs_block,
        dtc_mode_contract_block=dtc_mode_contract_block,
        obd_expression_contract_block=obd_expression_contract_block,
        deterministic_monitoring_enable_block=deterministic_monitoring_enable_block,
        hybrid_output_contract_block=hybrid_output_contract_block,
        control_path_focus_block=control_path_focus_block,
        split_note=split_note,
        compact=False,
    )

    compact_user_prompt = build_analysis_user_prompt(
        keyword=keyword,
        requested_mode=atype,
        classification_mode_label=classification_mode_label,
        cal_file=cal_file,
        cal_summary=cal_summary,
        cal_data=cal_data,
        module_code=module_code,
        current_module_context_block=current_module_context_block,
        control_module_context_block=control_module_context_block,
        module_dtc_architecture_block=module_dtc_architecture_block,
        fd_blocks_text=fd_blocks_text,
        related_module_context_block=related_module_context_block,
        disabled_dtcs_block=disabled_dtcs_block,
        hybrid_disabled_dtcs_block=hybrid_disabled_dtcs_block,
        dtc_mode_contract_block=dtc_mode_contract_block,
        obd_expression_contract_block=obd_expression_contract_block,
        deterministic_monitoring_enable_block=deterministic_monitoring_enable_block,
        hybrid_output_contract_block=hybrid_output_contract_block,
        control_path_focus_block=control_path_focus_block,
        split_note=split_note,
        compact=True,
    )

    def build_emergency_fallback_report(reason_text=''):
        module_name = (keyword or 'MODULE').strip().upper() or 'MODULE'
        disabled_html = ''
        if disabled_dtcs:
            disabled_items = ''.join(
                f'<li><div class="expr"><code>{code} == disabled (SW = 0)</code></div></li>'
                for code in sorted(disabled_dtcs)[:20]
            )
            disabled_html = (
                '<div class="unknown">'
                '<h3 class="subsection-title">Disabled DTCs</h3>'
                f'<ul>{disabled_items}</ul>'
                '</div>'
            )

        active_items = ''.join(
            f'<li><div class="expr"><code>{code}</code></div><div class="meaning">exact comparator path requires a successful model pass</div></li>'
            for code in active_dtcs[:8]
        )

        return prepare_report_html(f"""
<section class="report-section warn">
  <h2 class="section-title">Executive Summary</h2>
  <ul>
    <li><div class="expr"><code>classification = Need confirmation</code></div></li>
    <li><div class="expr"><code>target_module = {module_name}</code></div></li>
    <li><div class="meaning">reason: {reason_text or 'FordLLM did not return a usable report body.'}</div></li>
    <li><div class="expr"><code>active_dtc_count = {len(active_dtcs)}</code></div></li>
    <li><div class="expr"><code>disabled_dtc_count = {len(disabled_dtcs)}</code></div></li>
  </ul>
</section>
<section class="report-section">
  <h2 class="section-title">Quick Read</h2>
  <ul>
    <li><div class="expr"><code>report_generation = fallback_fragment</code></div></li>
    <li><div class="meaning">Use current-module, FD, related-module, and calibration evidence on the next retry.</div></li>
  </ul>
</section>
<section class="report-section unknown">
  <h2 class="section-title">Scope &amp; Evidence</h2>
  <ul>
    <li><div class="expr"><code>current_module = partially processed</code></div></li>
    <li><div class="expr"><code>calibration_rows_in_prompt = {int(cal_meta.get('included_rows') or 0)} / {int(cal_meta.get('total_rows') or 0)}</code></div></li>
  </ul>
</section>
<section class="report-section unknown">
  <h2 class="section-title">Main Analysis</h2>
    {disabled_html}
    <div class="unknown">
        <h3 class="subsection-title">OBD Analysis</h3>
        <ul>{active_items or '<li><div class="meaning">No enabled DTCs were available in the current scope.</div></li>'}</ul>
    </div>
</section>
<section class="report-section unknown">
    <h2 class="section-title">Additional CAL Data Items Not Explicitly Referenced Above</h2>
    <p class="muted">No additional CAL items remained after removing values already referenced above.</p>
</section>
""")

    # Stream response from Ford LLM
    def generate():
        streamed_report_text = ''
        captured_raw_source_text = ''
        upstream_error_message = ''
        generation_warning_message = ''

        def write_generation_debug(stage, report_text='', error_text='', extra=None):
            payload = {
                'pid': os.getpid(),
                'keyword': keyword,
                'stage': stage,
                'chars': len(report_text or ''),
                'raw_source_path': generation_raw_source_path,
                'preview': (report_text or '')[:1200],
                'error': error_text or '',
                'statuses_preview': [
                    f'Classification mode: {classification_mode_label}',
                    f'DTC count: {len(ordered_analysis_dtcs)}',
                    f'OBD block mode: {obd_dtc_block_mode}',
                    f'Large OBD batch mode: {large_obd_batch_mode}',
                ],
            }
            if extra:
                payload.update(extra)
            try:
                with open(generation_debug_path, 'w', encoding='utf-8') as generation_debug_file:
                    json.dump(payload, generation_debug_file, ensure_ascii=False, indent=2)
            except Exception:
                pass

        def write_raw_source_capture(raw_text, stage='raw-source-captured'):
            nonlocal captured_raw_source_text
            captured_raw_source_text = raw_text if isinstance(raw_text, str) else ''
            try:
                with open(generation_raw_source_path, 'w', encoding='utf-8', newline='') as raw_source_file:
                    raw_source_file.write(captured_raw_source_text)
            except Exception:
                pass
            write_generation_debug(
                stage,
                report_text=captured_raw_source_text,
                extra={
                    'raw_source_chars': len(captured_raw_source_text),
                },
            )

        def stream_chunks(prompt_text, active_system_prompt=None):
            nonlocal streamed_report_text
            prompt_system = active_system_prompt or system_prompt
            for chunk in call_ford_llm_streaming(prompt_system, prompt_text, emit_done=False):
                streamed_report_text += extract_chunk_content_text(chunk)
                yield chunk

        def collect_report_text(prompt_text, active_system_prompt=None):
            nonlocal upstream_error_message
            buffered_text = ''
            prompt_system = active_system_prompt or system_prompt
            for chunk in call_ford_llm_streaming(prompt_system, prompt_text, emit_done=False):
                line = (chunk or '').strip()
                if line.startswith('data: '):
                    payload_text = line[6:].strip()
                    if payload_text and payload_text != '[DONE]':
                        try:
                            payload_obj = json.loads(payload_text)
                        except Exception:
                            payload_obj = None
                        if isinstance(payload_obj, dict):
                            message_error = payload_obj.get('error')
                            if isinstance(message_error, str) and message_error.strip():
                                upstream_error_message = message_error.strip()
                buffered_text += extract_chunk_content_text(chunk)
                if upstream_error_message:
                    break
            return buffered_text

        def collect_report_text_resilient(prompt_text, active_system_prompt=None, max_attempts=3):
            buffered_text = ''
            for _attempt in range(max(max_attempts, 1)):
                buffered_text = collect_report_text(prompt_text, active_system_prompt)
                if buffered_text.strip():
                    return buffered_text
                time.sleep(0.5)
            return buffered_text

        def emit_report_text(report_text, chunk_size=6000):
            nonlocal streamed_report_text
            streamed_report_text += report_text
            for start in range(0, len(report_text), chunk_size):
                yield sse_event({
                    'choices': [
                        {
                            'delta': {'content': report_text[start:start + chunk_size]},
                            'index': 0,
                        }
                    ]
                })

        def emit_raw_source_text(raw_text, chunk_size=6000):
            source_text = raw_text if isinstance(raw_text, str) else ''
            for start in range(0, len(source_text), chunk_size):
                yield sse_event({
                    'raw_source_chunk': source_text[start:start + chunk_size],
                })
            yield sse_event({
                'raw_source_done': True,
                'raw_source_path': generation_raw_source_path,
                'raw_source_chars': len(source_text),
            })

        def build_internal_batched_obd_prompt(scope_codes, include_header):
            scope_list = ', '.join(scope_codes)
            header_rule = (
                '- Include the opening OBD Analysis structure and the optional Disabled DTCs section before the first DTC block in this pass.\n'
                if include_header else
                '- Do NOT repeat previously completed top-level sections or the disabled-DTC section in this pass.\n'
            )
            return f"""{user_prompt}

[INTERNAL BATCHED DTC SCOPE — MANDATORY]
- This pass is only for these enabled DTCs: {scope_list}
- Return HTML fragment only.
- Output DTC blocks ONLY for those codes in this pass.
- Do NOT mention or summarize any other enabled DTC in this pass.
{header_rule}- Keep the same OBD-only block format and the same evidence rules.
- If a code in this pass has partial evidence, keep all required subsections and state the evidenced gap inside the relevant subsection.
"""

        def generate_large_obd_batches():
            combined_parts = []
            batched_codes = [active_dtcs[idx:idx + obd_batch_size] for idx in range(0, len(active_dtcs), obd_batch_size)]

            for batch_index, batch_codes in enumerate(batched_codes, start=1):
                batch_scope = ', '.join(batch_codes)
                yield sse_event({'status': f'Generating OBD batch {batch_index}/{len(batched_codes)}: {batch_scope}'})
                batch_prompt = build_internal_batched_obd_prompt(batch_codes, include_header=(batch_index == 1))
                batch_text = collect_report_text_resilient(batch_prompt)
                if not batch_text.strip():
                    yield sse_event({'status': f'⚠️ OBD batch {batch_index}/{len(batched_codes)} returned empty output. Keeping earlier batches.'})
                    continue
                combined_parts.append(batch_text.strip())

            return '\n\n'.join(part for part in combined_parts if part)

        def finalize_buffered_report(final_report_text):
            nonlocal generation_warning_message
            if not (final_report_text or '').strip():
                fallback_text = collect_report_text_resilient(compact_user_prompt)
                if (fallback_text or '').strip():
                    write_raw_source_capture(fallback_text, stage='raw-source-captured-compact-fallback')
                    write_generation_debug('compact-fallback-success', report_text=fallback_text)
                    final_report_text = fallback_text
                else:
                    fallback_reason = upstream_error_message or 'Ford LLM returned an empty report body after buffering.'
                    generation_warning_message = fallback_reason
                    final_report_text = build_emergency_fallback_report(fallback_reason)
                    write_generation_debug('emergency-fallback-after-empty-buffer', report_text=final_report_text, error_text=fallback_reason)

            revised_report_text = final_report_text
            if (revised_report_text or '').strip():
                revised_report_text = sanitize_generated_report(revised_report_text)
                revised_report_text = prepare_report_html(revised_report_text)
                structured_report_text = enforce_final_report_structure(revised_report_text)
                if (structured_report_text or '').strip():
                    revised_report_text = structured_report_text

            try:
                with open(generation_report_path, 'w', encoding='utf-8') as generation_report_file:
                    generation_report_file.write(revised_report_text)
            except Exception:
                pass

            write_generation_debug(
                'final-report-ready',
                report_text=revised_report_text,
                extra={
                    'raw_source_chars': len(captured_raw_source_text),
                    'revised_chars': len(revised_report_text or ''),
                    'upstream_error': upstream_error_message,
                },
            )

            if generation_warning_message:
                yield sse_event({'status': f'⚠️ Showing fallback summary because raw Ford LLM output was unavailable: {generation_warning_message}'})

            for chunk in emit_raw_source_text(captured_raw_source_text):
                yield chunk

            for chunk in emit_report_text(revised_report_text):
                yield chunk

        yield sse_event({'status': f'Classification mode: {classification_mode_label}'})
        if ordered_analysis_dtcs:
            preview_codes = ', '.join(ordered_analysis_dtcs[:12])
            suffix = ' ...' if len(ordered_analysis_dtcs) > 12 else ''
            yield sse_event({'status': f'DTC scope for per-DTC sections: {len(ordered_analysis_dtcs)} ({preview_codes}{suffix})'})
        if related_modules:
            yield sse_event({'status': f'Cross-module DTC context included: {", ".join(related_modules)}'})
        should_buffer_first_pass = True

        if large_obd_batch_mode and not split_required:
            yield 'data: {"status": "Connecting to Ford LLM..."}\n\n'
            final_report_text = yield from generate_large_obd_batches()
            write_raw_source_capture(final_report_text, stage='raw-source-captured-large-obd')
            for chunk in finalize_buffered_report(final_report_text):
                yield chunk
            yield sse_event({'done': True})
            return

        if not split_required:
            yield 'data: {"status": "Connecting to Ford LLM..."}\n\n'
            if should_buffer_first_pass:
                final_report_text = collect_report_text_resilient(user_prompt)
                write_raw_source_capture(final_report_text, stage='raw-source-captured-single-pass')
                for chunk in finalize_buffered_report(final_report_text):
                    yield chunk
            else:
                for chunk in stream_chunks(user_prompt):
                    yield chunk
                write_raw_source_capture(streamed_report_text, stage='raw-source-captured-streamed')
            yield sse_event({'done': True})
            return

        if should_buffer_first_pass:
            yield 'data: {"status": "Connecting to Ford LLM..."}\n\n'
            yield sse_event({'status': f'Generating report part 1/{total_parts}...'})
            final_report_text = collect_report_text_resilient(user_prompt)
            write_raw_source_capture(final_report_text, stage='raw-source-captured-part-1')

            for part_idx in range(2, total_parts + 1):
                offset = (part_idx - 1) * CAL_REPORT_ROWS_PER_PART
                part_summary, _part_data, part_meta = build_calibration_prompt_payload(
                    cal_file,
                    max_rows=CAL_REPORT_ROWS_PER_PART,
                    row_offset=offset,
                    with_meta=True,
                    selected_dtc_codes=prompt_dtc_scope,
                    dtc_owner_lookup=dtc_owner_lookup,
                    current_module=current_module_upper,
                    prioritize_dtc_rows=obd_dtc_block_mode,
                )
                if int(part_meta.get('included_rows') or 0) <= 0:
                    continue

                report_tail = final_report_text[-5000:] if final_report_text else ''
                continuation_prompt = f"""Continue the same HTML fragment report for the same module and project.

Do not restart, reclassify, or reshape the report structure from the previous output.
Do not repeat earlier top-level sections or earlier calibration sections.
Continue from the next unfinished section, DTC block, subsection, or bullet chain.
Return HTML fragment only.
Do not use markdown.
Do not use markdown code fences.
Do not return a full <html> document.

[REPORT TAIL TO CONTINUE FROM]
{report_tail or '(No prior body captured.)'}

[CALIBRATION SOURCE FILE]
{cal_file or '(none)'}

[ADDITIONAL CALIBRATION CSV SUMMARY - PART {part_idx}/{total_parts}]
{part_summary}

Formatting requirements:
- Keep expression-first HTML fragment structure.
- Use short bullets and compact logic chains.
- Do not use markdown tables or HTML tables.
- Keep parameter names and values exactly as provided.
"""

                yield sse_event({'status': f'Generating report part {part_idx}/{total_parts}...'})
                continuation_text = collect_report_text_resilient(continuation_prompt)
                if continuation_text.strip():
                    final_report_text += continuation_text
                    write_raw_source_capture(final_report_text, stage=f'raw-source-captured-part-{part_idx}')
                else:
                    yield sse_event({'status': f'⚠️ Continuation part {part_idx}/{total_parts} returned empty output. Keeping prior content.'})

            for chunk in finalize_buffered_report(final_report_text):
                yield chunk
            yield sse_event({'done': True})
            return

        # Part 1: full analysis with module code.
        yield sse_event({'status': f'Generating report part 1/{total_parts}...'})
        for chunk in stream_chunks(user_prompt):
            yield chunk
            if '"error":' in chunk:
                return

        # Continuation parts: append remaining calibration chunks only.
        for part_idx in range(2, total_parts + 1):
            offset = (part_idx - 1) * CAL_REPORT_ROWS_PER_PART
            part_summary, _part_data, part_meta = build_calibration_prompt_payload(
                cal_file,
                max_rows=CAL_REPORT_ROWS_PER_PART,
                row_offset=offset,
                with_meta=True,
                selected_dtc_codes=prompt_dtc_scope,
                dtc_owner_lookup=dtc_owner_lookup,
                current_module=current_module_upper,
                prioritize_dtc_rows=obd_dtc_block_mode,
            )
            if int(part_meta.get('included_rows') or 0) <= 0:
                continue

            report_tail = streamed_report_text[-5000:] if streamed_report_text else ''
            continuation_prompt = f"""Continue the same HTML fragment report for the same module and project.

Do not restart, reclassify, or reshape the report structure from the previous output.
Do not repeat earlier top-level sections or earlier calibration sections.
Continue from the next unfinished section, DTC block, subsection, or bullet chain.
Return HTML fragment only.
Do not use markdown.
Do not use markdown code fences.
Do not return a full <html> document.

[REPORT TAIL TO CONTINUE FROM]
{report_tail or '(No prior body captured.)'}

[CALIBRATION SOURCE FILE]
{cal_file or '(none)'}

[ADDITIONAL CALIBRATION CSV SUMMARY - PART {part_idx}/{total_parts}]
{part_summary}

Formatting requirements:
- Keep expression-first HTML fragment structure.
- Use short bullets and compact logic chains.
- Do not use markdown tables or HTML tables.
- Keep parameter names and values exactly as provided.
"""

            yield sse_event({'status': f'Generating report part {part_idx}/{total_parts}...'})
            for chunk in stream_chunks(continuation_prompt):
                yield chunk
                if '"error":' in chunk:
                    return

        yield sse_event({'done': True})

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/extract-dtcs', methods=['POST'])
def api_extract_dtcs():
    filepath = request.json.get('filepath', '')
    cal_file = request.json.get('cal_file', '')
    uds_mode = normalize_uds_mode(request.json.get('uds_mode', None))
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    footer_pat  = re.compile(r'FOOTER\s+\(.*?_([A-Z][A-Z_\d]+)\)', re.IGNORECASE)

    # {DTC: {'modules': [], 'lines': []}}
    dtc_map  = {}
    dtc_sw_module_map = {}
    cur_mod  = 'UNKNOWN'
    cal_dtc_items, cal_dtc_map = get_cal_dtc_switch_items(cal_file, uds_mode)
    cal_value_lookup = build_cal_value_lookup(cal_file)
    a2l_file = find_related_a2l_file(filepath, cal_file)
    a2l_switch_map = load_a2l_dtc_switch_map(a2l_file)
    a2l_description_map = load_a2l_dtc_description_table(a2l_file)
    pcm_dt_map = load_pcm_dt_reference_table()

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                fm = footer_pat.search(line)
                if fm:
                    cur_mod = fm.group(1).upper()

                # Track which module actually references each DTC switch symbol (e.g., P2271SW).
                for sw_ref in re.findall(r'(?i)\b([PBCU][0-9][0-9A-F]{3}(?:[_-][0-9A-F]{2})?SW)\b', line):
                    sw_dtc = get_dtc_code_from_switch_parameter(sw_ref)
                    if not sw_dtc or not dtc_matches_uds_mode(sw_dtc, uds_mode):
                        continue
                    if cur_mod == 'UNKNOWN':
                        continue
                    dtc_sw_module_map.setdefault(sw_dtc, set()).add(cur_mod)

                for code in extract_dtc_codes_from_line(line):
                    if not dtc_matches_uds_mode(code, uds_mode):
                        continue
                    if code not in dtc_map:
                        dtc_map[code] = {'modules': [], 'lines': [], 'module_hits': {}}
                    if cur_mod != 'UNKNOWN' and cur_mod not in dtc_map[code]['modules']:
                        dtc_map[code]['modules'].append(cur_mod)
                    if cur_mod != 'UNKNOWN':
                        dtc_map[code]['module_hits'][cur_mod] = dtc_map[code]['module_hits'].get(cur_mod, 0) + 1
                    dtc_map[code]['lines'].append(lineno)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not dtc_map:
        return build_no_cache_json_response({
            'dtcs': [],
            'count': 0,
            'saved_to': None,
            'uds_mode': uds_mode,
            'uds_mode_label': uds_mode_label(uds_mode),
            **current_dtc_extractor_metadata(),
        })

    ccode_version = extract_ccode_version_string(cal_file) or extract_ccode_version_string(filepath)
    if ccode_version:
        out_file = os.path.join(os.path.dirname(filepath), f'DTC_list_{ccode_version}.txt')
    else:
        out_file = os.path.join(os.path.dirname(filepath), 'DTC_list.txt')

    lines_out = ['DTC | Modules | SW | Hits | Description']
    for code in sorted(dtc_map):
        info    = dtc_map[code]
        modules = [m for m in info['modules'] if m != 'UNKNOWN']
        mods    = ', '.join(modules) or '-'
        sw_items = cal_dtc_map.get(code, [])
        if sw_items:
            sw_text = '; '.join(f'{sw["name"]}={sw["value"]}' for sw in sw_items)
        else:
            sw_text = '-'
        description = get_dtc_code_description(code, a2l_description_map, pcm_dt_map=pcm_dt_map) or '-'
        lines_out.append(f'{code} | {mods} | {sw_text} | {len(info["lines"])} | {description}')

    content = '\n'.join(lines_out)
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(content)

    a2l_confirmed_dtcs = 0
    a2l_described_sw_items = 0
    a2l_matched_sw_items = 0
    dtc_list = [
        None
        for _ in ()
    ]

    dtc_list = []
    for c in sorted(dtc_map):
        raw_sw_items = cal_dtc_map.get(c, [])
        sw_items, matched_count, described_count = enrich_dtc_sw_items_with_a2l(raw_sw_items, a2l_switch_map)
        sw_status, sw_status_label = classify_dtc_sw_status(sw_items)
        if matched_count:
            a2l_confirmed_dtcs += 1
            a2l_matched_sw_items += matched_count
        a2l_described_sw_items += described_count

        dtc_list.append({
            'code': c,
            'modules': [m for m in dtc_map[c]['modules'] if m != 'UNKNOWN'],
            'sw_modules': sorted(dtc_sw_module_map.get(c, set())),
            'primary_modules': sorted([
                module for module, count in dtc_map[c].get('module_hits', {}).items()
                if count == max(dtc_map[c].get('module_hits', {}).values(), default=0)
            ]),
            'hits': len(dtc_map[c]['lines']),
            'sw_items': sw_items,
            'sw_status': sw_status,
            'sw_status_label': sw_status_label,
            'a2l_confirmed': bool(matched_count),
            'description': get_dtc_code_description(c, a2l_description_map, pcm_dt_map=pcm_dt_map),
        })

    all_cal_sw_items, _unused_dtc_map = get_cal_dtc_switch_items(cal_file, uds_mode)
    enriched_cal_sw_items, _matched_count, _described_count = enrich_dtc_sw_items_with_a2l(all_cal_sw_items, a2l_switch_map)
    sw_value_meanings = build_dtc_sw_value_meanings(enriched_cal_sw_items, cal_value_lookup)

    return build_no_cache_json_response({
        'dtcs': dtc_list,
        'count': len(dtc_list),
        'saved_to': out_file,
        'sw_value_meanings': sw_value_meanings,
        'dtc_trips_to_mil': parse_int_value(cal_value_lookup.get(compact_identifier('DTC_TRIPS_TO_MIL'))),
        'a2l_file': a2l_file or None,
        'a2l_summary': {
            'confirmed_dtcs': a2l_confirmed_dtcs,
            'matched_sw_items': a2l_matched_sw_items,
            'described_sw_items': a2l_described_sw_items,
        },
        'uds_mode': uds_mode,
        'uds_mode_label': uds_mode_label(uds_mode),
        **current_dtc_extractor_metadata(),
    })


@app.route('/api/search-cal-data', methods=['POST'])
def api_search_cal_data():
    import time as _time
    data         = request.json
    module_file  = data.get('module_file', '')
    cal_file     = data.get('cal_file', '')
    module_name  = data.get('module_name', 'MODULE')
    uds_mode     = normalize_uds_mode(data.get('uds_mode', None))

    if not os.path.exists(module_file):
        return jsonify({'error': 'Module file not found'}), 404
    if not os.path.exists(cal_file):
        return jsonify({'error': 'Calibration data file not found'}), 404

    t0  = _time.time()
    log = []
    log.append(f'\u25b6 Module      : {module_name}')
    log.append(f'\u25b6 Cal file    : {os.path.basename(cal_file)}')
    source_info = inspect_cal_source_file(cal_file, module_name)
    log.append(f'\u25b6 Cal source  : {cal_file}')
    if source_info['warning']:
        log.append(f'\u25b6 Warning     : {source_info["warning"]}')

    def build_cal_search_response(found_rows, cal_rows, dtc_rows, dtc_codes, total_cal_params, used_prepared_result, saved_to, csv_save_action, elapsed):
        return jsonify({
            'found':            found_rows,
            'count':            len(found_rows),
            'cal_count':        len(cal_rows),
            'dtc_sw_count':     len(dtc_rows),
            'dtc_codes':        dtc_codes,
            'dtc_count':        len(dtc_codes),
            'source_cal_file':  source_info['path'],
            'source_cal_name':  source_info['name'],
            'source_warning':   source_info['warning'],
            'source_generated': source_info['looks_generated'],
            'dtc_source_file':  cal_file,
            'dtc_reference_file': PCM_DT_REFERENCE_FILE if os.path.exists(PCM_DT_REFERENCE_FILE) else '',
            'dtc_reference_label': PCM_DT_REFERENCE_LABEL,
            'used_prepared_result': used_prepared_result,
            'total_cal_params': total_cal_params,
            'display_mode':     'all-matches',
            'saved_to':         saved_to,
            'csv_save_action':  csv_save_action,
            'elapsed':          elapsed,
            'uds_mode':         uds_mode,
            'uds_mode_label':   uds_mode_label(uds_mode),
            'log':              log,
        })

    prepared_result = parse_generated_cal_result(cal_file, uds_mode)
    if prepared_result is not None:
        with open(module_file, 'r', encoding='utf-8', errors='replace') as f:
            module_content = f.read()
        metadata_map = extract_parameter_metadata(module_content, [item.get('name', '') for item in prepared_result['all_found']])

        for item in prepared_result['all_found']:
            base = re.sub(r'\[.*?\]', '', item.get('name', '')).strip()
            meta = metadata_map.get(normalize_cal_key(base), {})
            unit, description, default = resolve_parameter_metadata(
                base,
                meta,
                item.get('unit', ''),
                item.get('description', ''),
                item.get('default', ''),
            )
            item['unit'] = unit
            item['description'] = description
            item['default'] = default

        for item in prepared_result['found']:
            base = re.sub(r'\[.*?\]', '', item.get('name', '')).strip()
            meta = metadata_map.get(normalize_cal_key(base), {})
            unit, description, default = resolve_parameter_metadata(
                base,
                meta,
                item.get('unit', ''),
                item.get('description', ''),
                item.get('default', ''),
            )
            item['unit'] = unit
            item['description'] = description
            item['default'] = default

        for item in prepared_result['dtc_sw_items']:
            base = re.sub(r'\[.*?\]', '', item.get('name', '')).strip()
            meta = metadata_map.get(normalize_cal_key(base), {})
            unit, description, default = resolve_parameter_metadata(
                base,
                meta,
                item.get('unit', ''),
                item.get('description', ''),
                item.get('default', ''),
            )
            item['unit'] = unit
            item['description'] = description
            item['default'] = default

        dtc_comment_map = extract_dtc_catalog(module_content)['comment_map']
        a2l_description_map = load_a2l_dtc_description_table(find_related_a2l_file(module_file, cal_file))
        pcm_dt_map = load_pcm_dt_reference_table()
        for item in prepared_result['dtc_sw_items']:
            item['dtc_description'] = get_dtc_code_description(item.get('dtc', ''), a2l_description_map, dtc_comment_map, pcm_dt_map)

        ccode_version = extract_ccode_version_string(cal_file) or extract_ccode_version_string(module_file)
        saved_to, csv_save_action = save_cal_search_result_csv(module_file, module_name, prepared_result['all_found'], ccode_version)
        elapsed = round(_time.time() - t0, 3)
        log.append('\u25b6 Mode        : Using selected generated CSV directly')
        log.append(f'\u25b6 DTC mode    : {uds_mode_label(uds_mode)}')
        log.append(f'\u25b6 CSV DTCs    : {len(prepared_result["dtc_codes"])} unique code(s) from DTC_SW rows')
        log.append(f'\u25b6 DTC SW found: {len(prepared_result["dtc_sw_items"])} switch parameters')
        log.append(f'\u25b6 Showing     : {len(prepared_result["all_found"])} total rows in web output')
        log.append(f'\u25b6 CSV status  : {csv_save_action or "updated"}')
        log.append(f'\u25b6 CSV file    : {saved_to}')
        log.append(f'\u25b6 Elapsed     : {elapsed}s')

        return build_cal_search_response(
            prepared_result['all_found'],
            prepared_result['found'],
            prepared_result['dtc_sw_items'],
            prepared_result['dtc_codes'],
            prepared_result['total_cal_params'],
            True,
            saved_to,
            csv_save_action,
            elapsed,
        )

    cal_entries_raw = parse_cal_data(cal_file)
    # Remove duplicates and keep the first entry per parameter name.
    seen_names = set()
    cal_entries = []
    for name, value in cal_entries_raw:
        key = re.sub(r'\[.*?\]', '', name).strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            cal_entries.append((name, value))
    log.append(f'\u25b6 Cal entries : {len(cal_entries_raw)} parsed -> {len(cal_entries)} unique parameters')

    with open(module_file, 'r', encoding='utf-8', errors='replace') as f:
        module_content = f.read()
    log.append(f'\u25b6 Module size : {len(module_content):,} chars  /  {module_content.count(chr(10)):,} lines')
    log.append(f'\u25b6 Searching...')
    metadata_map = extract_parameter_metadata(module_content, [name for name, _value in cal_entries])

    # Extract all module words once and use set lookup for O(N) matching.
    module_words = {w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', module_content)}
    dtc_catalog = extract_dtc_catalog(module_content)
    module_dtc_codes = dtc_catalog['diagnostic'] or dtc_catalog['all']
    dtc_comment_map = dtc_catalog['comment_map']
    a2l_description_map = load_a2l_dtc_description_table(find_related_a2l_file(module_file, cal_file))
    pcm_dt_map = load_pcm_dt_reference_table()

    found_keys = set()
    dtc_sw_items = []

    for name, value in cal_entries:
        base = re.sub(r'\[.*?\]', '', name).strip()
        key = base.lower()
        if not key or key in found_keys:
            continue

        if is_dtc_family_switch_parameter(base) and (
            key in module_words or dtc_switch_matches(key, module_dtc_codes, uds_mode)
        ):
            dtc_code = get_dtc_code_from_switch_parameter(base)
            if not dtc_matches_uds_mode(dtc_code, uds_mode):
                continue
            rule = build_dtc_rule(dtc_code)
            dtc_sw_items.append({
                'name': name,
                'value': value,
                'category': 'DTC_SW',
                'dtc': dtc_code,
                'rule': rule,
                'dtc_description': get_dtc_code_description(dtc_code, a2l_description_map, dtc_comment_map, pcm_dt_map),
                'default': '',
            })
            found_keys.add(key)

    found = []
    for name, value in cal_entries:
        base = re.sub(r'\[.*?\]', '', name).strip()
        key  = base.lower()
        if base and key in module_words and key not in found_keys:
            meta = metadata_map.get(normalize_cal_key(base), {})
            unit, description, default = resolve_parameter_metadata(base, meta)
            found.append({
                'name': name,
                'value': value,
                'category': 'Cal',
                'default': default,
                'unit': unit,
                'description': description,
            })
            found_keys.add(key)

    for item in dtc_sw_items:
        base = re.sub(r'\[.*?\]', '', item.get('name', '')).strip()
        meta = metadata_map.get(normalize_cal_key(base), {})
        _unit, _description, default = resolve_parameter_metadata(base, meta, existing_default=item.get('default', ''))
        item['default'] = default

    log.append(f'\u25b6 DTC SW found: {len(dtc_sw_items)} switch parameters')
    log.append(f'\u25b6 DTC mode    : {uds_mode_label(uds_mode)}')
    log.append(f'\u25b6 Found       : {len(found)} / {len(cal_entries)} parameters match')

    # Put switch items first so they are easy to spot in the browser.
    all_found = dtc_sw_items + found
    csv_dtc_codes = [item['dtc'] for item in dtc_sw_items]
    ccode_version = extract_ccode_version_string(cal_file) or extract_ccode_version_string(module_file)
    saved_to, csv_save_action = save_cal_search_result_csv(module_file, module_name, all_found, ccode_version)

    elapsed = round(_time.time() - t0, 3)
    log.append(f'\u25b6 CSV DTCs    : {len(csv_dtc_codes)} module-specific code(s) found from DTC_SW')
    if csv_dtc_codes:
        log.append(f'\u25b6 DTC list    : {", ".join(csv_dtc_codes)}')
    log.append(f'\u25b6 CSV status  : {csv_save_action or "updated"}')
    log.append(f'\u25b6 CSV file    : {saved_to}')
    log.append(f'\u25b6 Elapsed     : {elapsed}s')

    return build_cal_search_response(
        all_found,
        found,
        dtc_sw_items,
        csv_dtc_codes,
        len(cal_entries),
        False,
        saved_to,
        csv_save_action,
        elapsed,
    )


@app.route('/api/save-analysis', methods=['POST'])
def api_save_analysis():
    data        = request.json
    content     = data.get('content', '')
    module_file = data.get('module_file', '')
    atype       = data.get('type', 'general')
    keyword     = data.get('keyword', 'MODULE')
    artifact_label = re.sub(r'[^A-Za-z0-9_-]+', '_', str(data.get('artifact_label', '') or '').strip().upper()).strip('_')
    save_format = (data.get('format') or 'txt').strip().lower()

    if not content:
        return jsonify({'error': 'No content to save'}), 400

    out_dir = (os.path.dirname(module_file)
               if module_file and os.path.isdir(os.path.dirname(module_file))
               else BASE_DIR)
    if save_format == 'md':
        ext = '.md'
        suffix = f'_{artifact_label}{ext}' if artifact_label else '_DESCRIPTION.md'
    elif save_format == 'html':
        ext = '.html'
        suffix = f'_{artifact_label}{ext}' if artifact_label else '_DESCRIPTION.html'
    else:
        ext = '.txt'
        suffix = f'_{artifact_label}{ext}' if artifact_label else ('_OBD_DESCRIPTION.txt' if atype == 'obd' else '_DESCRIPTION.txt')
    out_file = os.path.join(out_dir, keyword + suffix)

    backup_file = ''
    if os.path.exists(out_file):
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        base_name, ext = os.path.splitext(out_file)
        backup_file = f'{base_name}_{timestamp}{ext}'
        shutil.copy2(out_file, backup_file)

    if save_format == 'html' and not looks_like_full_html_document(content):
        content = render_report(content, module_name=keyword, title=f'{keyword} Report')

    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(content)

    return jsonify({
        'saved_to': out_file,
        'format': save_format,
        'backup_created': bool(backup_file),
        'backup_file': backup_file,
    })


@app.route('/api/open-file', methods=['POST'])
def api_open_file():
    data = request.json or {}
    file_path = (data.get('file_path') or '').strip()

    if not file_path:
        return jsonify({'error': 'file_path is required'}), 400

    abs_path = os.path.abspath(file_path)
    base_abs = os.path.abspath(BASE_DIR)

    if not abs_path.startswith(base_abs + os.sep):
        return jsonify({'error': 'Opening files outside workspace is not allowed'}), 400

    if not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        os.startfile(abs_path)
    except AttributeError:
        return jsonify({'error': 'Automatic file open is only supported on Windows'}), 400
    except OSError as e:
        return jsonify({'error': f'Failed to open file: {str(e)}'}), 500

    return jsonify({'ok': True, 'opened': abs_path})


@app.route('/preview-report', methods=['GET'])
def preview_report():
    file_path = (request.args.get('path') or '').strip()

    if not file_path:
        return Response('path is required', status=400, mimetype='text/plain')

    abs_path = os.path.abspath(file_path)
    base_abs = os.path.abspath(BASE_DIR)
    abs_path_cmp = os.path.normcase(abs_path)
    base_abs_cmp = os.path.normcase(base_abs)

    if not (abs_path_cmp == base_abs_cmp or abs_path_cmp.startswith(base_abs_cmp + os.sep)):
        return Response('Preview outside workspace is not allowed', status=400, mimetype='text/plain')

    if not os.path.isfile(abs_path):
        return Response('Preview file not found', status=404, mimetype='text/plain')

    try:
        with open(abs_path, 'r', encoding='utf-8') as preview_file:
            html = preview_file.read()
    except OSError as exc:
        return Response(f'Failed to load preview: {str(exc)}', status=500, mimetype='text/plain')

    file_name = os.path.basename(abs_path)
    is_full_document = looks_like_full_html_document(html)
    is_saved_report = (
        file_name.lower().endswith('_description.html')
        or file_name.lower().endswith('_obd_description.html')
        or 'report-section' in html
        or 'report-container' in html
    )
    if is_saved_report and not is_full_document:
        module_name = re.sub(r'(?i)_(?:obd_)?description$', '', os.path.splitext(file_name)[0]) or 'MODULE'
        html = render_report(html, module_name=module_name, title=f'{module_name} Report')

    response = Response(html, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    return response


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(os.path.join(BASE_DIR, 'templates'), exist_ok=True)
    print("\n  C-BOOK ECU Code Analyzer")
    print(f"  C-BOOK dir : {CBOOK_DIR}")
    print(f"  Python     : {sys.executable}")
    print(f"  Browser    : http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)

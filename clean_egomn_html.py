"""EGOMN_DESCRIPTION.html cleaner v2"""
import re
from pathlib import Path

fp = Path(r"C-BOOK/BX726/HMYJ2_BX726E_6MX65_125PS/EGOMN/EGOMN_DESCRIPTION.html")
content = fp.read_text(encoding="utf-8")
original_len = len(content)

def step(name, c, prev):
    saved = prev - len(c)
    print(f"  {name:<47} -{saved:>5}")
    return c, len(c)

L = original_len

# 1. Related Module: Not found + SW (with or without "(enabled)")
content, L = step("Related Module boilerplate", re.sub(
    r'<p><strong>Related Module\(s\) and Ownership:</strong></p>\n'
    r'<ul>\n<li>Not found in available cross-module context; only local evidence below\.</li>\n'
    r'<li>CSV has (<code>[^<]+</code>)(?: \(enabled\))?\.</li>\n</ul>\n',
    r'<p>\1</p>\n', content), L)

# 2. Fail Case: single "Not evidenced" bullet
content, L = step("Fail Not-evidenced collapse", re.sub(
    r'<p><strong>Fail Case:</strong></p>\n<ul>\n'
    r'<li>Not evidenced in provided scope[^<]*(?:<[a-z][^>]*>[^<]*</[a-z]+>)?[^<]*</li>\n'
    r'(?:<li>[^<]*(?:do not assume)[^<]*</li>\n)?</ul>\n',
    '<p><strong>Fail:</strong> Not evidenced in provided code scope.</p>\n', content, flags=re.DOTALL), L)

# 3. Pass/Clear Case: "Not evidenced" bullet
content, L = step("Pass/Clear Not-evidenced collapse", re.sub(
    r'<p><strong>Pass/Clear Case:</strong></p>\n<ul>\n'
    r'<li>Not evidenced in provided scope\.</li>\n</ul>\n',
    '<p><strong>Pass/Clear:</strong> Not evidenced in provided code scope.</p>\n', content), L)

# 4a. Cal Linkage: only SW = N (single item)
content, L = step("Cal Linkage SW-only", re.sub(
    r'<p><strong>Calibration Linkage \(if applicable to this DTC\):</strong></p>\n'
    r'<ul>\n<li><code>[A-Z0-9]+SW\s*=\s*[0-9]+</code></li>\n</ul>\n', '', content), L)

# 4b. Cal Linkage: SW + pointer to P0131
content, L = step("Cal Linkage SW+P0131 pointer", re.sub(
    r'<p><strong>Calibration Linkage \(if applicable to this DTC\):</strong></p>\n'
    r'<ul>\n<li><code>[A-Z0-9]+SW\s*=\s*[0-9]+</code></li>\n'
    r'<li>Relevant common monitor cals listed in P0131 section \(same set\)\.</li>\n</ul>\n', '', content), L)

# 4c. Cal Linkage: SW + modulation cals pointer
content, L = step("Cal Linkage SW+mod pointer", re.sub(
    r'<p><strong>Calibration Linkage \(if applicable to this DTC\):</strong></p>\n'
    r'<ul>\n<li><code>[A-Z0-9]+SW\s*=\s*[0-9]+</code></li>\n'
    r'<li>Common entry and modulation-related cals as listed earlier\.</li>\n</ul>\n', '', content), L)

# 4d. Cal Linkage: SW + downstream same-as pointer
content, L = step("Cal Linkage SW+downstream ref", re.sub(
    r'<p><strong>Calibration Linkage \(if applicable to this DTC\):</strong></p>\n'
    r'<ul>\n<li><code>[A-Z0-9]+SW\s*=\s*[0-9]+</code></li>\n'
    r'<li>(?:Same downstream-related cals as P\d+\.|Downstream-related cals:[^<]+|Likely (?:tied|corresponds)[^<]+)</li>\n</ul>\n',
    '', content, flags=re.DOTALL), L)

# 4e. Cal Linkage: SW + any single short line
content, L = step("Cal Linkage SW+1 short line", re.sub(
    r'<p><strong>Calibration Linkage \(if applicable to this DTC\):</strong></p>\n'
    r'<ul>\n<li><code>[A-Z0-9]+SW\s*=\s*[0-9]+</code></li>\n'
    r'<li>[^<]{1,140}</li>\n</ul>\n', '', content), L)

# 5a. Enable: Not evidenced single-bullet
content, L = step("Enable Not-evidenced collapse", re.sub(
    r'<p><strong>Enable Conditions:</strong></p>\n<ul>\n'
    r'<li>Not evidenced in provided scope[^<]*</li>\n(?:<li>[^<]*</li>\n)?</ul>\n',
    '<p><strong>Enable:</strong> Not evidenced in provided code scope.</p>\n', content), L)

# 5b. Enable: "Same as PxxYY" single bullet
content, L = step("Enable Same-as pointer", re.sub(
    r'<p><strong>Enable Conditions:</strong></p>\n<ul>\n'
    r'<li>Same as P\d+[^<]*\.</li>\n</ul>\n', '', content), L)

# 5c. Enable: repeating only common EGOMN gates (3 common bullets)
content, L = step("Enable common-gates repeat", re.sub(
    r'<p><strong>Enable Conditions:</strong></p>\n<ul>\n'
    r'<li>Same EGOMN suite prerequisites[^<]+</li>\n'
    r'<li>Entry window gating via:[^<]+</li>\n'
    r'(?:<li>[^<]+</li>\n)?</ul>\n',
    '<p><strong>Enable:</strong> Same common entry conditions (ECT/VS/N/Load — see Section B).</p>\n', content), L)

# 6. N/A rows in comparative tables
content, L = step("N/A comparative table rows", re.sub(
    r'<tr>\n<td>(?:P[0-9A-F]+|All others)</td>\n'
    r'<td>N/A</td>\n<td>N/A</td>\n<td>N/A</td>\n<td>N/A</td>\n'
    r'<td>Not evidenced in scope</td>\n</tr>\n', '', content), L)

# 7. Empty rows in G-tables
content, L = step("Empty G-table rows", re.sub(
    r'<tr>\n(?:<td></td>\n){5,}</tr>\n', '', content), L)

# 8. Collapse fully empty DTC sections (SW + 3x Not evidenced) → 1 note line
content, L = step("Fully empty DTC blocks", re.sub(
    r'(<h3>DTC P[0-9A-F]+</h3>\n)(<p><code>[^<]+</code></p>\n)'
    r'<p><strong>Enable:</strong> (?:Not evidenced in provided code scope\.|Same common entry conditions[^<]+)</p>\n'
    r'<p><strong>Fail:</strong> Not evidenced in provided code scope\.</p>\n'
    r'<p><strong>Pass/Clear:</strong> Not evidenced in provided code scope\.</p>\n',
    r'\1\2<p><em>No logic evidence in provided code scope.</em></p>\n', content), L)

# 9. Section title fixes
content = content.replace('<h1>Comparative Tables (since 2+ DTCs in scope)</h1>', '<h1>Summary Tables</h1>')
content = content.replace('<h1>## Disabled DTCs (Not In Use)</h1>', '<h2>Disabled DTCs (Not In Use)</h2>')
# 10. Cleanup
content = re.sub(r'(<hr>\n){2,}', '<hr>\n', content)
content = re.sub(r'<p>\s*</p>\n', '', content)

final_len = len(content)
print(f"\n  Total: -{original_len-final_len} chars  ({100*(original_len-final_len)/original_len:.1f}%)")
print(f"  {original_len} -> {final_len}")
fp.write_text(content, encoding="utf-8")
print("  Written.")

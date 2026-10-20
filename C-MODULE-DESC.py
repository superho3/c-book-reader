import re
import os


def extract_module_description(filepath):
    """
    Reads an extracted module .txt file (from C-BOOK-READ.py),
    parses each source file section (delimited by FOOTER lines),
    and generates a structured summary of the module.
    """
    if not os.path.exists(filepath):
        return f"Error: The file '{filepath}' was not found."

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return f"An error occurred while reading the file: {e}"

    # Extract module name from first FOOTER line
    module_name = ""
    first_footer = ""
    for line in lines:
        m = re.search(r'FOOTER\s+\((V[\d.]+_([A-Z_\d]+))\)', line)
        if m:
            first_footer = m.group(1)
            module_name = m.group(2)
            break

    if not module_name:
        return "Error: No FOOTER line found. This may not be a valid module file."

    # Parse sections by FOOTER lines
    footer_pattern = re.compile(r'FOOTER\s+\((V[\d.]+_[A-Z_\d]+)\)\s+(.*)')
    sections = []
    current_section = None

    for line in lines:
        fm = footer_pattern.search(line)
        if fm:
            if current_section:
                sections.append(current_section)
            src_path = fm.group(2).strip()
            # Extract just the filename from ClearCase path (before @@)
            clean_path = src_path.split('@@')[0] if '@@' in src_path else src_path
            src_file = clean_path.split('/')[-1] if '/' in clean_path else clean_path
            current_section = {
                'version': fm.group(1),
                'source_path': src_path,
                'source_file': src_file,
                'content': []
            }
        elif current_section is not None:
            current_section['content'].append(line)

    if current_section:
        sections.append(current_section)

    # Analyze each section
    summary_sections = []
    for sec in sections:
        content_text = "".join(sec['content'])

        # Extract Abstract
        abstract = extract_comment_field(content_text, 'Abstract')

        # Extract Global Function Names
        global_funcs = extract_comment_field(content_text, 'Global Function Name')

        # Extract Functional Description
        func_desc = extract_functional_description(content_text)

        # Extract #define constants (for header files)
        defines = extract_defines(sec['content'])

        # Extract function signatures
        func_sigs = extract_function_signatures(sec['content'])

        # Determine file type
        file_type = classify_file(sec['source_file'], sec['content'])

        summary_sections.append({
            'file': sec['source_file'],
            'path': sec['source_path'],
            'type': file_type,
            'abstract': abstract,
            'global_functions': global_funcs,
            'func_desc': func_desc,
            'defines': defines,
            'func_signatures': func_sigs,
            'line_count': len(sec['content'])
        })

    # Build the description document
    desc = []
    desc.append("=" * 78)
    desc.append(f"  MODULE DESCRIPTION: {module_name}")
    desc.append(f"  Version: {first_footer}")
    desc.append(f"  Total source files: {len(sections)}")
    desc.append(f"  Total lines: {len(lines)}")
    desc.append("=" * 78)
    desc.append("")

    # Module overview
    desc.append("[ MODULE OVERVIEW ]")
    desc.append("-" * 40)
    # Try to derive overall purpose from the context header abstract
    for sec in summary_sections:
        if sec['file'].endswith('context.h') and sec['abstract']:
            desc.append(f"  {sec['abstract']}")
            desc.append("")
            break
    else:
        # Use first file with an abstract
        for sec in summary_sections:
            if sec['abstract']:
                desc.append(f"  {sec['abstract']}")
                desc.append("")
                break

    # File list summary
    desc.append("[ SOURCE FILE LIST ]")
    desc.append("-" * 40)
    for i, sec in enumerate(summary_sections, 1):
        desc.append(f"  {i:2d}. {sec['file']:<45s} ({sec['type']}, {sec['line_count']} lines)")
    desc.append("")

    # Detailed per-file descriptions
    desc.append("[ DETAILED FILE DESCRIPTIONS ]")
    desc.append("=" * 78)

    for sec in summary_sections:
        desc.append("")
        desc.append(f"  FILE: {sec['file']}")
        desc.append(f"  TYPE: {sec['type']}")
        desc.append(f"  PATH: {sec['path']}")
        desc.append(f"  LINES: {sec['line_count']}")
        desc.append("  " + "-" * 40)

        if sec['abstract']:
            desc.append(f"  ABSTRACT: {sec['abstract']}")

        if sec['global_functions']:
            desc.append(f"  GLOBAL FUNCTIONS: {sec['global_functions']}")

        if sec['func_desc']:
            desc.append(f"  FUNCTIONAL DESCRIPTION:")
            for line in sec['func_desc'].split('\n'):
                if line.strip():
                    desc.append(f"    {line.strip()}")

        if sec['defines']:
            desc.append(f"  KEY DEFINES ({len(sec['defines'])}):")
            for d in sec['defines'][:30]:  # Limit display
                desc.append(f"    {d}")
            if len(sec['defines']) > 30:
                desc.append(f"    ... and {len(sec['defines']) - 30} more")

        if sec['func_signatures']:
            desc.append(f"  FUNCTION SIGNATURES:")
            for sig in sec['func_signatures']:
                desc.append(f"    {sig}")

        desc.append("")

    return "\n".join(desc)


def extract_comment_field(text, field_name):
    """Extract a named field from C comment headers (Abstract, File Name, etc.)"""
    # Find the field line, then collect text until the next field or separator
    lines = text.split('\n')
    found = False
    result_parts = []

    field_re = re.compile(
        r'^\s*(?:/\*\*?|\*\*|\*)\s*' + re.escape(field_name) + r'[^:]*:\s*(.*)',
        re.IGNORECASE
    )
    # Separator or next field pattern
    stop_re = re.compile(r'^\s*(?:/\*\*?|\*\*|\*)\s*(?:#+|[A-Z][a-z].*:|Global |File |Abstract|Copyright)', re.IGNORECASE)

    for line in lines:
        if not found:
            m = field_re.match(line)
            if m:
                found = True
                first = re.sub(r'\*+/?\s*$', '', m.group(1)).strip()
                if first:
                    result_parts.append(first)
        else:
            # Check for separator or next field
            if stop_re.match(line) or '###' in line:
                break
            cleaned = re.sub(r'^\s*(?:/\*\*?|\*\*|\*)\s*', '', line)
            cleaned = re.sub(r'\s*\*+/?\s*$', '', cleaned).strip()
            if cleaned:
                result_parts.append(cleaned)

    return ' '.join(result_parts) if result_parts else ""


def extract_functional_description(text):
    """Extract the FUNCTIONAL DESCRIPTION block."""
    m = re.search(
        r'FUNCTIONAL DESCRIPTION.*?\n(.*?)(?:#+\*/)',
        text, re.DOTALL
    )
    if m:
        lines = []
        for line in m.group(1).split('\n'):
            cleaned = re.sub(r'^/?\*+\s?', '', line).rstrip()
            cleaned = cleaned.strip('/ ')
            cleaned = re.sub(r'\s*\*+\s*$', '', cleaned)  # Remove trailing asterisks
            if cleaned:
                lines.append(cleaned)
        return '\n'.join(lines) if lines else ""
    return ""


def extract_defines(lines):
    """Extract #define statements."""
    defines = []
    for line in lines:
        m = re.match(r'\s*#define\s+(\S+)\s+(.*)', line)
        if m:
            name = m.group(1)
            value = m.group(2).strip()
            # Get inline comment if present
            comment = ""
            cm = re.search(r'/\*\s*(.*?)\s*\*/', value)
            if cm:
                comment = cm.group(1)
                value = value[:value.index('/*')].strip()
            if comment:
                defines.append(f"{name} = {value}  // {comment}")
            else:
                defines.append(f"{name} = {value}")
    return defines


def extract_function_signatures(lines):
    """Extract C function signatures (global and static)."""
    sigs = []
    func_pattern = re.compile(
        r'^(?:static\s+)?(?:void|U8|U16|U32|S8|S16|S32|F32|F64|FLAG|auto)\s+\w+\s*\(.*\)'
    )
    for line in lines:
        stripped = line.strip()
        if func_pattern.match(stripped) and '{' not in stripped:
            sigs.append(stripped.rstrip(';'))
    return sigs


def classify_file(filename, content_lines):
    """Classify source file type."""
    fn = filename.lower()
    if fn.endswith('.h'):
        if 'context' in fn:
            return "Context Header (constants/defines)"
        elif 'data' in fn:
            return "Data Header (shared data declarations)"
        elif 'export' in fn:
            return "Export Header (public interface)"
        elif 'import' in fn:
            return "Import Header (external dependencies)"
        elif 'function' in fn:
            return "Function Header (prototypes)"
        return "Header"
    elif fn.endswith('.c'):
        if 'data_karam' in fn:
            return "Data Source (KAM-RAM variables)"
        elif 'data_ram' in fn:
            return "Data Source (RAM variables)"
        elif 'data_rom' in fn:
            return "Data Source (ROM constants)"
        elif 'data_statrom' in fn:
            return "Data Source (Static ROM)"
        elif 'data_monrom' in fn:
            return "Data Source (Monitor ROM)"
        elif 'data_slowram' in fn:
            return "Data Source (Slow RAM)"
        elif 'data_fastram' in fn:
            return "Data Source (Fast RAM)"
        elif 'data_fg' in fn:
            return "Data Source (Foreground data)"
        elif 'data_mg' in fn:
            return "Data Source (Mid-ground data)"
        elif 'controller' in fn:
            return "Controller (main control logic)"
        elif 'monitor' in fn:
            return "Monitor (diagnostic monitoring)"
        elif 'data' in fn:
            return "Data Source"
        return "Source"
    return "Unknown"


if __name__ == "__main__":
    filepath = input("Enter the module .txt file path to describe: ").strip()

    result = extract_module_description(filepath)

    if result.startswith("Error"):
        print(result)
    else:
        print(result)

        # Save to file
        output_dir = os.path.dirname(filepath)
        base_name = os.path.splitext(os.path.basename(filepath))[0]
        output_file = os.path.join(output_dir, f"{base_name}_DESCRIPTION.txt")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(result)
        print(f"\nDescription saved to: {output_file}")

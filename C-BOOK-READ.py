import re
import os

def analyze_c_code_features(filepath):
    """
    Analyzes a C code file to extract uppercase feature identifiers
    from the "Features INcluded in" block.

    Extracts the uppercase name after the version number (e.g., V2023.10.00_A_HLD -> A_HLD).
    Skips lines with UNKNOWN (no version number).
    """
    if not os.path.exists(filepath):
        return f"Error: The file '{filepath}' was not found."

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return f"An error occurred while reading the file: {e}"

    features_start_tag = "Features INcluded in"
    start_line_index = -1

    for i, line in enumerate(lines):
        if features_start_tag in line:
            start_line_index = i
            break

    if start_line_index == -1:
        return f"Error: The text '{features_start_tag}' was not found in the file."

    # Scan from start tag until the block of /** ... **/ lines ends
    feature_lines = []
    for line in lines[start_line_index + 1:]:
        stripped = line.strip()
        if stripped.startswith('/**') and stripped.endswith('**/'):
            feature_lines.append(stripped)
        elif stripped.startswith('/***'):
            # Separator line like /***...***/, skip but continue
            continue
        else:
            break

    relevant_content = "\n".join(feature_lines)

    # Match version patterns like V2023.10.00_A_HLD, V24.5000.03_ACC, V24.065.000_EDF
    # Captures the uppercase feature name after the last underscore following version digits
    pattern = r'V[\d.]+_([A-Z][A-Z_\d]*)'
    extracted_features = re.findall(pattern, relevant_content)

    return extracted_features


def extract_module_code(filepath, keyword):
    """
    Extracts all code sections belonging to a specific module (keyword)
    from the C code file. Sections are identified by FOOTER lines containing
    the keyword. Each section runs from a matching FOOTER line to the next FOOTER line.
    """
    if not os.path.exists(filepath):
        print(f"Error: The file '{filepath}' was not found.")
        return

    footer_pattern = re.compile(r'^\s*FOOTER\s')
    keyword_pattern = re.compile(r'FOOTER\s+\(.*_' + re.escape(keyword) + r'\)')

    sections = []
    current_section = []
    capturing = False

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if footer_pattern.search(line):
                    if capturing:
                        sections.append("".join(current_section))
                        current_section = []
                        capturing = False

                    if keyword_pattern.search(line):
                        capturing = True
                        current_section = [line]
                    continue

                if capturing:
                    current_section.append(line)

        if capturing and current_section:
            sections.append("".join(current_section))

    except Exception as e:
        print(f"An error occurred while reading the file: {e}")
        return

    if not sections:
        print(f"No module sections found for keyword '{keyword}'.")
        return

    output_dir = os.path.dirname(filepath)
    output_file = os.path.join(output_dir, f"{keyword}.txt")
    with open(output_file, 'w', encoding='utf-8') as f:
        for section in sections:
            f.write(section)

    print(f"\nExtracted {len(sections)} sections for '{keyword}'.")
    print(f"Saved to: {output_file}")


if __name__ == "__main__":
    file_name = input("Please enter the C code file name to analyze: ")
    result = analyze_c_code_features(file_name)

    if isinstance(result, str):
        print(result)
    elif result:
        print(f"\nExtracted {len(result)} feature identifiers:")
        for part in result:
            print(part)

        base_name = os.path.splitext(file_name)[0]
        output_file = base_name + "_features.txt"
        with open(output_file, 'w', encoding='utf-8') as f:
            for part in result:
                f.write(part + '\n')
        print(f"\nFeature list saved to: {output_file}")

        keyword = input("\nEnter a feature keyword to extract its module code (or press Enter to skip): ").strip().upper()
        if keyword:
            extract_module_code(file_name, keyword)
    else:
        print(f"No matching patterns found in '{file_name}'.")


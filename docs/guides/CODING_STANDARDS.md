# Coding Standards & Internationalization Policy

## 1. File Encoding

- **Mandatory**: All source code files must use **UTF-8** encoding
- Optional encoding declaration at file header:
  ```python
  # -*- coding: utf-8 -*-
  ```

## 2. Language Usage Policy

### 2.1 Source Code Comments & Docstrings
- **Flexible**: Chinese is preferred, English is also acceptable
- Recommendations:
  - Core API interfaces: English recommended (for international collaboration)
  - Internal implementation details: Chinese preferred
  - Be consistent within the same file

### 2.2 Program Output (stdout/stderr)

| Output Type | Language | Status |
|:---|:---|:---|
| **Human-facing report prose (stdout)** | **Chinese** | Enforced for new code |
| **Diagnostics, logging and everything on stderr** | **English only** | Enforced |
| **Exception messages** | **English only** | Enforced |
| **Report labels and codes** (finding `code`s, option names, `--help`, usage text) | **English only** | Enforced |
| **JSON/YAML associative text array** | **English for keys, Chinese is accepted for values and comments** | Enforced |
| **Markdown text sequence** | **Both English and Chinese are accepted** | Enforced |

One report, one language: a report and everything printed inside it — including
self-checks such as `python -m primer.config --show` — use the same language, and
for a human-facing report that language is Chinese.

### 2.3 Visualization Output
- **Mandatory**: Matplotlib, Plotly and other visualization libraries must use **English** for:
  - Titles
  - Axis labels (xlabel/ylabel)
  - Legends
  - Colorbar labels
  - Annotations

## 3. Code Review Checklist

When reviewing pull requests, verify:

- [ ] New human-facing report prose on stdout is in Chinese, and a self-check follows the report it belongs to?
- [ ] New diagnostics, logging, stderr output and exception messages are in English?
- [ ] New matplotlib labels are in English?
- [ ] JSON/YAML keys are in English?
- [ ] File encoding is UTF-8?

## 4. Exceptions

- Proper nouns (place names, person names) may remain in original language
- Test data, example data content is not restricted
- Documentation files (markdown) may use Chinese or English as appropriate
- Report prose is Chinese for **every** `primer` tool — `primer-slides`'s `check`/`build`/`outline` reports, `primer-book inspect`, `primer-claims verify`, `primer-literature`, `primer-references`, `primer-digest` — matching their Chinese document families (`candidates.md`, `outline.yaml`, inspection reports, ledgers). Finding `code`s, option names, `--help` and usage text stay English.

## 5. Rationale

This policy balances:
- **Code readability** for the development team (Chinese-preferred comments)
- **International collaboration** (English APIs, codes and option names)
- **Operational reliability** (English logs, stderr and exception messages for debugging)
- **Reproducibility** (English metadata in data files)
- **One voice per report** (a report and its self-checks share a single language: Chinese)

---
*Version: 1.1*
*Effective Date: 2026-05-03*
*Revised: 2026-09-28 — report prose is Chinese for every tool, not only `primer-slides`; a self-check follows the report it belongs to. (The previous exception line was an uncommitted local edit.)*

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
| **stdout / stderr** | **English only** | Enforced for new code |
| **Logging** | **English only** | Enforced |
| **JSON/YAML associative text array** | **English for keys, Chinese is accepted for values and comments** | Enforced |
| **Markdown text sequence** | **Both English and Chinese are accepted** | Enforced |
| **Exception messages** | **English only** | Enforced |

### 2.3 Visualization Output
- **Mandatory**: Matplotlib, Plotly and other visualization libraries must use **English** for:
  - Titles
  - Axis labels (xlabel/ylabel)
  - Legends
  - Colorbar labels
  - Annotations

## 3. Code Review Checklist

When reviewing pull requests, verify:

- [ ] New stdout print statements are in English?
- [ ] New logging statements are in English?
- [ ] New matplotlib labels are in English?
- [ ] JSON/YAML keys are in English?
- [ ] File encoding is UTF-8?

## 4. Exceptions

- Proper nouns (place names, person names) may remain in original language
- Test data, example data content is not restricted
- Documentation files (markdown) may use Chinese or English as appropriate

## 5. Rationale

This policy balances:
- **Code readability** for the development team (Chinese-preferred comments)
- **International collaboration** (English APIs and runtime output)
- **Operational reliability** (English logs/output for debugging)
- **Reproducibility** (English metadata in data files)

---
*Version: 1.0*
*Effective Date: 2026-05-03*

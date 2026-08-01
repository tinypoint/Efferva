# Semantic Alpha Research Workspace

This workspace bundles the AI Berkshire Codex skills from
https://github.com/xbtlin/ai-berkshire at revision
`0310788cdabb0d724ac9f67e3dbd3e9e4a13d06a`.

## Research rules

- Run `date` before research and state the data cutoff date in the report.
- Separate facts, market narratives, assumptions, and conclusions.
- Verify decision-critical financial data with at least two independent sources.
- Use `python3 tools/financial_rigor.py` for exact valuation and scenario math.
- Use `python3 tools/report_audit.py` before treating a report as publishable.
- Label incomplete evidence, source gaps, and low-confidence conclusions.
- Save generated research under `reports/`.
- This workspace is for research and learning, not investment advice.

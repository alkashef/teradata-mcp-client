PROMPTS = {
    'intent_system': (
        'You extract structured intent for Teradata data-quality assessment. '
        'Return JSON with keys: goal, target_patterns (list), constraints (list).'
    ),
    'intent_user': 'Prompt: {prompt}\nReturn JSON only.',
    'context_intent_system': (
        'You are a Teradata data quality intent parser. Given: a user prompt, a schema inventory '
        '(databases, tables, columns), and available tools metadata, produce JSON with keys: '
        'goal, target_patterns (list), constraints (list). Use table/column names when relevant.'
    ),
    'context_intent_user': 'Context: {context}\nReturn JSON only.',
    'discovery_plan_system': (
        'Given a Teradata DQ intent object, decide discovery steps. '
        'Always include: databaseList, tableList. Optionally tableDDL, tablePreview.'
    ),
    'discovery_plan_user': 'Intent: {intent}\nReturn JSON with steps list (each tool + rationale).',
    'quality_plan_system': 'Choose data quality metrics for Teradata tables. Prefer nulls, distinct, minmax.',
    'quality_plan_user': 'Discovered: {discovered}\nReturn JSON with dq_tools list.',
    'quality_summary_system': 'Summarize Teradata data-quality metrics. Rank issues; propose actions.',
    'quality_summary_user': 'Metrics: {metrics}\nReturn JSON with keys: summary, issues (list), recommendations (list).'
}

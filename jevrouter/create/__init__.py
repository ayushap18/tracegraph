"""Created files: a compact DocSpec from the model (or from an answer or a table, at zero tokens) rendered by code into
PDF, DOCX, PPTX, XLSX or Markdown, then checked against docs/RULES-files.md. See docs/PLAN-files.md."""
from .preview import preview
from .render import render
from .rules import RULES, RuleResult, SpecError, verify
from .spec import DOCSPEC_SCHEMA, FORMATS, detect_format, file_name, from_markdown, from_table, normalize

MIME = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'md': 'text/markdown; charset=utf-8',
}

__all__ = ['DOCSPEC_SCHEMA', 'FORMATS', 'MIME', 'RULES', 'RuleResult', 'SpecError', 'detect_format', 'file_name',
           'from_markdown', 'from_table', 'normalize', 'preview', 'render', 'verify']

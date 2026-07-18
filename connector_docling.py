"""
Connector Docling — isole tout le reste du programme des détails internes
de la bibliothèque Docling (imports, configuration, format de sortie).

Si Docling change d'API, ou si un jour on le remplace par un autre moteur
d'extraction, seul ce fichier doit changer.

Interface stable exposée :
- extract_sheets(xlsx_path) -> liste de SheetExtract
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TableExtract:
    headers: list
    rows: list  # liste de listes (une par ligne)
    n_rows: int
    n_cols: int


@dataclass
class SheetExtract:
    name: str          # nom réel de l'onglet
    index: int          # position dans le classeur
    tables: list = field(default_factory=list)  # liste de TableExtract


def _build_configured_converter():
    from docling.document_converter import DocumentConverter, ExcelFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.backend.msexcel_backend import MsExcelDocumentBackend, MsExcelBackendOptions

    backend_options = MsExcelBackendOptions(
        treat_singleton_as_text=True,  # évite la fragmentation en micro-tables 1x1
        gap_tolerance=0,               # sépare les blocs de données distincts
        parse_charts=False,            # inutile pour l'usage PreCogn actuel
    )
    format_option = ExcelFormatOption(backend=MsExcelDocumentBackend, backend_options=backend_options)
    return DocumentConverter(format_options={InputFormat.XLSX: format_option})


def extract_sheets(xlsx_path):
    """Convertit un classeur XLSX en liste de SheetExtract (1 onglet = 1 élément,
    conforme à la PPDC : Docling groupe déjà nativement par onglet réel)."""
    from docling_core.types.doc import GroupLabel

    converter = _build_configured_converter()
    result = converter.convert(xlsx_path)
    doc = result.document

    sheet_groups = [g for g in doc.groups if g.label == GroupLabel.SHEET]

    sheets = []
    for index, group in enumerate(sheet_groups):
        tables = []
        for child_ref in group.children:
            item = child_ref.resolve(doc)
            if type(item).__name__ != 'TableItem':
                continue
            try:
                df = item.export_to_dataframe(doc=doc)
            except TypeError:
                df = item.export_to_dataframe()
            if df.empty:
                continue
            tables.append(TableExtract(
                headers=[str(c) for c in df.columns],
                rows=df.astype(str).values.tolist(),
                n_rows=len(df),
                n_cols=len(df.columns),
            ))
        sheets.append(SheetExtract(name=group.name, index=index, tables=tables))

    return sheets

"""Reviewed headless tools from the pinned Craft engines, not arbitrary RPC.

Generic command/batch, desktop control, scripting and implicit save-in-place are
not exposed. Files are checked again by the consumer's workspace path policy.
"""

READ_TOOLS = {
    "wordcraft": {"get_text", "inspect_document"},
    "gridcraft": {"inspect_workbook", "read_range", "get_cell", "evaluate_formula", "list_functions"},
    "deckcraft": {"inspect_document", "inspect_slide"},
    "pdfcraft": {"doc_list", "doc_info", "doc_inspect", "text_extract", "text_find", "text_lines",
                 "text_paragraphs", "object_list", "content_list", "bookmark_list", "form_fields", "comment_list"},
}
ACTION_TOOLS = {
    "wordcraft": {"new_document", "open_document", "save_document", "type_text", "select_text"},
    "gridcraft": {"new_workbook", "open_workbook", "save_workbook", "write_range", "set_cell", "select",
                  "format_range", "insert_chart", "create_table", "sort_range", "filter", "undo", "redo"},
    "deckcraft": {"new_presentation", "open_presentation", "save_presentation", "export", "add_slide",
                  "go_to_slide", "add_shape", "add_text_box", "set_text", "insert_picture", "insert_table", "insert_chart"},
    "pdfcraft": {"doc_open", "doc_close", "doc_create", "doc_save", "doc_set_info", "page_rotate", "page_delete",
                 "page_move", "page_insert_blank", "page_insert_file", "page_duplicate", "page_set_box", "page_add_text",
                 "content_update", "content_delete", "form_fill", "form_reset", "comment_add", "comment_reply",
                 "comment_edit", "comment_delete", "edit_undo", "edit_redo"},
}
# These tools have filesystem arguments. All other allowed tools must receive no
# path-like fields (including unknown properties ignored by some upstream schemas).
INPUT_FIELDS = {
    ("wordcraft", "open_document"): ("path",),
    ("gridcraft", "open_workbook"): ("path",),
    ("deckcraft", "open_presentation"): ("path",),
    ("deckcraft", "insert_picture"): ("path",),
    ("pdfcraft", "doc_open"): ("path",),
    ("pdfcraft", "page_insert_file"): ("path",),
    ("pdfcraft", "doc_create"): ("path", "paths"),
    ("pdfcraft", "content_update"): ("image",),
    ("pdfcraft", "comment_add"): ("path",),
}
OUTPUT_TOOLS = {("wordcraft", "save_document"), ("gridcraft", "save_workbook"),
                ("deckcraft", "save_presentation"), ("deckcraft", "export"), ("pdfcraft", "doc_save")}
OUTPUT_FORMATS = {
    "wordcraft": {".docx", ".pdf", ".txt", ".md"},
    "gridcraft": {".xlsx", ".csv", ".tsv"},
    "deckcraft": {".pptx", ".deckcraft", ".pdf"},
    "pdfcraft": {".pdf"},
}
OPEN_TOOLS = {"wordcraft": "open_document", "gridcraft": "open_workbook",
              "deckcraft": "open_presentation", "pdfcraft": "doc_open"}
INSPECT_TOOLS = {"wordcraft": "inspect_document", "gridcraft": "inspect_workbook",
                 "deckcraft": "inspect_document", "pdfcraft": "doc_info"}

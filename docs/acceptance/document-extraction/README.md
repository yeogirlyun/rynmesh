# Acceptance: sandboxed document extraction (#39)

Run on a real machine with the node installed. Record the elapsed time and the
returned `status` for each step. **Record no filename, no absolute path, and no
line of document content** — a status code and a duration are the whole result.

Prepare four files in a scratch directory: a 2-page Markdown note, a plain text
file of about 5 MB, a normal PDF, and a PDF truncated halfway through with a
hex editor.

1. Extract the Markdown note. Expect `parsed`; record the duration.
2. Extract the 5 MB text file with the default caps. Expect `truncated`;
   confirm the returned character count equals the configured cap exactly.
3. Extract the normal PDF. Expect `parsed` when the `documents` extra is
   installed, `unsupported` when it is not. Record which.
4. Extract the truncated PDF. Expect `unsupported` or `failed:unreadable`, and
   confirm the node still answers `/health` afterwards.
5. Re-run step 4 twenty times in a loop. Confirm the node's memory does not
   grow and no child process is left behind (`pgrep -f document_extract_child`
   returns nothing).
6. Point the helper at a path that does not exist. Expect `failed:unreadable`.
7. Point it at a directory rather than a file. Expect `failed:unreadable`.
8. Grep the node log for the scratch directory's name. Expect no match.

## Non-goals for this version

- No DOCX, ODT, RTF, EPUB, or spreadsheet extraction — those return
  `unsupported` by design.
- No OCR: a scanned PDF with no text layer yields empty text, not an error.
- No encoding detection. A text file that is not valid UTF-8 is
  `failed:not_text`; the node does not guess at code pages.
- No caching. Every call re-extracts; `ReaderCache` is the model to copy if a
  consumer later needs one.
- On Windows the address-space and CPU limits are absent (`resource` is POSIX
  only). The wall-clock deadline and the output cap still apply, so a bomb is
  bounded in time and output but not in memory. Named here rather than hidden.

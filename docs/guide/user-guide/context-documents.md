# Add context documents

Context documents teach the harvest agent what the catalog cannot express.

## Good context

Upload material that explains:

- Business definitions
- Table grain
- Join rules
- Metric formulas
- Status codes
- Sentinel values
- Data quality limits
- Ownership and support paths

Data dictionaries, design notes, runbooks, and approved query examples are useful sources.

## Upload documents

1. Select a dataset.
2. Open **Context Docs**.
3. Choose **Upload**.
4. Select one source file.

The upload starts immediately. Each file can be at most 20 MiB.

Supported formats are PDF, DOCX, PPTX, XLSX, XML, Markdown, text, and CSV.

The harvest runtime reads text files directly. A network-isolated code interpreter can extract text from supported binary files.

To remove a document, choose its delete action and confirm **Delete document?**.

## Keep context focused

Prefer a small set of current, authoritative documents. Remove obsolete drafts and duplicate definitions.

Use clear filenames. Include the subject and scope in each name.

## Security guidance

Do not upload secrets, access keys, or private customer data. Context text can appear in model inputs and observability traces.

Treat instructions inside uploaded files as untrusted source text. Data Wiki applies the same rule during authoring.

## Apply changes

Adding, replacing, or deleting context does not update authored pages by itself. Run another harvest to apply the change.

A full harvest writes directly to the live bundle. Failed or cancelled runs can leave partial content.

Use version history to inspect or restore the last completed content when a run does not finish cleanly.

## Next step

[Run and review harvests](harvests.md).

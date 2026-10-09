# Direct templates and metadata

Run from the repository root:

```bash
gh slate render review \
  --template examples/templates/review.md.j2 \
  --data examples/templates/review.json \
  --meta examples/templates/target.json
```

The fixture is illustrative. Its target ID is not a real GitHub ID. Omitting
`--meta` leaves the host, repository, and target null; this template explicitly
handles that local case.

Preview with metadata resolved from your own Issue or PR:

```bash
gh slate apply review --target <ISSUE_OR_PR_URL> \
  --template examples/templates/review.md.j2 \
  --data examples/templates/review.json --dry-run
```

Apply does not accept caller-supplied metadata. Remove `--dry-run` to publish.
Later updates need only `--data`; the template and metadata snapshot live in
the comment. Repeating the same update leaves its revision unchanged.

Templates use `data` and `meta`. Ordinary strings are escaped as text;
use `md_body` for free prose with dynamic bare HTTP(S) URLs, and
`md_link(URL)`, `md_code`, `md_codeblock(LANGUAGE)`, `md_details(SUMMARY)`,
`md_table`, and `md_list` for Markdown structures. Helper output can be placed
in table cells without double escaping. For stable object keys, Jinja's
`dictsort` filter supports `{% for key, value in data.findings | dictsort %}`.
For numbered keys, use `dictsort_natural` instead: `F1`, `F2`, `F10`.
It compares ASCII digit runs numerically and text case-insensitively, breaking
ties by the original key. Keys and values are preserved; `dictsort` retains
its usual lexical ordering.

The summary example uses `{{ data.summary | md_body }}`. Each validated URL gets
an explicit link with its original display text, query, and fragment. Chinese
wrappers and sentence punctuation remain outside links. Other text is escaped;
matching backtick runs and invalid URL-like tokens stay literal in code elements.
Use `md_code` or `md_codeblock` for code fields. Default interpolation and
`md_text` keep literal semantics. Updating a stored comment's template requires
explicit `--template` or `--profile`; sending only new data keeps the old template.

The same files are available as an explicit single-template profile:

```bash
gh slate render review --config examples/templates/boards.toml \
  --profile review --data examples/templates/review.json \
  --meta examples/templates/target.json
```

You can move this directory anywhere. Paths inside `boards.toml` are relative
to that file, and are loaded only when `--profile` is explicitly selected.

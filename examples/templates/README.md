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

New templates use `data` and `meta`. Ordinary strings are escaped as text;
use `md_link(URL)`, `md_code`, `md_codeblock(LANGUAGE)`, `md_details(SUMMARY)`,
`md_table`, and `md_list` for Markdown structures. Helper output can be placed
in table cells without double escaping. For stable object keys, Jinja's
`dictsort` filter supports `{% for key, value in data.findings | dictsort %}`.

To migrate an old Jinja comment, change its source from `slate.name` to
`meta.slate.name`, `slate.repository` to `meta.repository.full_name`, and
`slate.number` / `slate.url` to `meta.target.number` / `meta.target.url`.
Replace hand-built dynamic links and code spans with the corresponding
helpers. Pass the new `--template` explicitly; existing data is retained if
`--data` is omitted, and validation happens before the single update.

The same files are available as an explicit single-template profile:

```bash
gh slate render review --config examples/templates/boards.toml \
  --profile review --data examples/templates/review.json \
  --meta examples/templates/target.json
```

You can move this directory anywhere. Paths inside `boards.toml` are relative
to that file, and are loaded only when `--profile` is explicitly selected.

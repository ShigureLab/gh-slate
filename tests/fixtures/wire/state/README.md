# State wire fixtures

These fixtures exercise the current comment format, canonical JSON, typed data,
metadata, schema snapshots, template definitions, hashes, and payload sizes.

When the model changes, update each fixture and its hashes in `manifest.json`
together. The stored comment must decode to its canonical state and reproduce
its visible Markdown from the embedded template and metadata.

`full-types` covers numeric boundaries, Unicode, empty containers, null values,
and absent properties. Its `source.state.json` includes noncanonical whitespace
and must canonicalize to `state.canonical.json`.

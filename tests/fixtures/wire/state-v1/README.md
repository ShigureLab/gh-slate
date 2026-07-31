# state-v1 wire fixtures

These fixtures are compatibility contracts, not snapshots to regenerate during
routine test updates. A released fixture must remain decodable, or a later
format must provide an explicit migration.

`state.canonical.json` is stored with one repository-friendly terminal newline;
tests remove exactly that newline before comparing the canonical wire bytes and
state hash. The complete stored `comment.md` is a decode compatibility vector:
tests deliberately do not require a freshly encoded zlib stream to reproduce
the same payload bytes across zlib implementations.

`source.state.json`, when present, records intentionally non-canonical input
forms such as `-0.000`, exponent notation, and unsorted object keys. Its
canonicalization must match `state.canonical.json`.

The `minimal` vector also freezes a complete `builtin-table@1` descriptor. Its
stored state must rerender byte-for-byte to `visible.md`; this fixture was
finalized together with the first renderer implementation, before release.

Every fixture directory and every file in it must be registered in
`manifest.json`; unregistered compatibility vectors are rejected by tests.
`.gitattributes` fixes wire fixture checkout to LF so byte hashes are portable
on Windows.

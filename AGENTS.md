# AGENTS.md

## Greenfield design

- There are no legacy usage scenarios or stored data that require compatibility.
- Use one current model throughout foundational types, data and state formats,
  rendering, commands, and documentation. Do not introduce numbered generations
  of those models or formats.
- Do not maintain legacy aliases, version dispatch, migration paths, or
  compatibility branches. Apply changes from the foundations upward and update
  affected producers, consumers, and tests together.

## Code style

1. **Simple and correct.** Use as little code and as few components as possible
   while keeping the result clear and readable.
2. **Handle realistic failures.** Validate external input at boundaries. Internally,
   trust established contracts instead of adding defensive branches, fallbacks,
   or recovery logic for impossible states.
3. **Small, cohesive functions.** Make responsibilities, inputs, outputs, and
   dependencies explicit. Split functions by responsibility, not mechanically to
   reduce line counts.
4. **Prefer pure functions and immutable data.** Concentrate side effects at
   explicit boundaries, pass dependencies explicitly, and avoid hidden mutation
   and shared mutable state.
5. **Refactor when duplication or structure calls for it.** Reduce special cases,
   wrappers, and patches. Build abstractions around real domain concepts or
   existing commonality; do not prebuild generic frameworks.
6. **One authoritative source for each fact.** Derive values from canonical state
   instead of maintaining another copy that must be kept in sync.

## Git commit conventions

- Follow yutto's commit title format: `<gitmoji> <type>: <subject>`.
- Use the Unicode emoji itself, such as `✨`, rather than an emoji shortcode.
- Choose the emoji and type to match the change:
   - `✨ feat`: new functionality.
   - `🐛 fix`: bug fixes.
   - `♻️ refactor`: code restructuring.
   - `📝 docs`: documentation.
   - `✅ test`: tests.
   - `👷 ci`: CI configuration and workflows.
   - `📦 build`: build and packaging changes.
   - `⬆️ deps`: dependency upgrades.
   - `🔧 chore`: other maintenance.
- Keep the subject concise and focused on the actual repository change.
- Preserve existing co-author trailers when amending or rebasing commits.
- For commits created with Codex, include:

   ```text
   Co-authored-by: Codex <noreply@openai.com>
   ```

Examples:

```text
✨ feat: add reusable slate profiles
🐛 fix: reject stale revisions before publishing
📝 docs: clarify profile configuration
```

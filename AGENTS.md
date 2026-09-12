# AGENTS.md

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

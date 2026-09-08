# Domain Docs

How engineering skills should consume this repository's domain documentation when exploring the codebase.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- `docs/adr/`: ADRs that touch the area being changed.

If any of these paths do not exist, proceed silently. The domain-modeling skill creates them when terms or decisions are actually resolved.

## File structure

This is a single-context repository:

```
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary's vocabulary

When naming a domain concept in an issue, proposal, test, or other output, use the term defined in `CONTEXT.md`. If a needed concept is absent, reconsider whether the project already has a suitable term and note genuine gaps for domain modeling.

## Flag ADR conflicts

If output contradicts an existing ADR, surface that conflict explicitly rather than silently overriding it.

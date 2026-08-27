# conti-agent

`conti-agent` is a small Python coding-agent runtime. It connects an
OpenAI-compatible or Anthropic-compatible model to local workspace tools while
keeping permissions, audit history, sessions, and extension points explicit.

The repository is designed for learning as well as use:

- a deterministic core that runs without network access;
- one event stream for model output and tool activity;
- plain files for configuration, sessions, memory, and audit logs;
- no required third-party runtime dependency;
- a terminal REPL rather than a graphical terminal interface.

See [`docs/SPEC.md`](docs/SPEC.md) for requirements and
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the staged build plan.

## Status

Design baseline. Implementation proceeds by the phase plan in
`docs/IMPLEMENTATION.md`.

## Development

```bash
python -m unittest discover -s tests
```

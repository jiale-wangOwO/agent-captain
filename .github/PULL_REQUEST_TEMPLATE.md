## What and why

<!-- Describe the user problem and the smallest useful change. Link an issue if there is one. -->

## Validation

<!-- List checks actually run and their results. For skipped checks, explain why.
     Use sanitized or synthetic evidence only. Documentation-only changes may
     mark unrelated suites as not run; do not connect real machines for a smoke test. -->

## Compatibility and limits

<!-- Note migrations, retained technical identifiers, manual steps, or unverified behavior.
     Highlight changes to authentication, terminal input, retries, or remote process handling. -->

## Checklist

- [ ] The change is focused; relevant tests and documentation are included.
- [ ] I reviewed the staged diff and ran `python scripts/check_publication.py --tracked`.
- [ ] No private configuration, credentials, real hosts/paths, Session IDs, business transcripts, or raw runtime files are included.
- [ ] Screenshots and binary assets, if any, were reviewed separately for private data and usage rights.
- [ ] Tests use isolated fixtures; live diagnostics remain off and no user's real agent was used without explicit consent.
- [ ] Existing sessions, human input, ambiguous deliveries, and authentication boundaries remain protected (or the relevant change is explained above).

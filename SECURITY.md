# Security policy

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/mohamadsmt/google-docs-mcp/security/advisories/new) for a suspected security issue. Do not open a public issue with exploit details or private data. If private reporting is unavailable, open a minimal issue requesting a private contact channel without including sensitive details.

Include the affected commit/version, expected and observed behavior, and a small synthetic reproduction. Never send OAuth tokens, client secrets, real document IDs/URLs, document contents, recovery exports, environment dumps, or unredacted logs. No response-time or long-term-support guarantee is offered; fixes target the current main branch.

## Security boundary

- This is a local stdio server, not a hosted service or a sandbox for untrusted MCP clients.
- Google OAuth grants are separate from the eleven exposed tools and may be much broader. Use your own account/client and the narrowest workable scopes. Token, recovery and export paths are fixed under the OS user's `~/.hermes`; Hermes profiles do not isolate them.
- Credentials are not included. Keep authorized-user tokens and client configuration outside the checkout, in private regular files. The runtime reads `~/.hermes/google_token.json` and may refresh it.
- `docs_read` deliberately returns document data to the MCP client. Client/model providers, session logs, backups, and data-retention policies are outside this server's control.
- `max_chars` caps only the `content` page, not the entire response. Document metadata, tab inventory, and the selected tab's full heading/table/link/image inventories are outside that cap; headings, linked text/destinations and image descriptions beyond the requested page can still be disclosed. Pagination is not an excerpt-only authorization boundary. Temporary Google image `contentUri` bearer-like download links are not returned.
- Revision guards and readback reduce write risks; they do not grant user consent. Markdown replacement is destructive. Table writes have multiple guarded phases and can leave partial results.
- Text insertion uses one guarded batch without deleting existing content. Persian formatting changes paragraph settings on paragraphs touched by the insertion, including existing text sharing those paragraphs. A verification/transport failure may follow a committed write: reread before retrying, never blindly insert a second time.
- Recovery TXT/DOCX files contain unencrypted private content. Directory/file permissions and startup retention checks do not replace disk encryption or a backup policy.
- PDF/DOCX delivery exports contain unencrypted private content and are retained separately from automatic recovery cleanup. They use private, exclusive local publication without arbitrary destination paths. Export reads Google but writes local files; it is not a strictly read-only MCP action. Source-stability checks are not an atomic revision-specific export guarantee. Malicious non-cooperating code already running as the same OS account is outside the isolation guarantee.
- Image insertion accepts public HTTPS URIs only; the server does not upload/host private files or grant sharing. Google fetches the URI and stores its source in the document. Never supply private signed/bearer URLs as public assets. Local lexical URL checks do not control Google's DNS resolution or redirects. Image insertion is not automatically replayed after a lost response; read back first.
- Error sanitization, `.gitignore`, tests and secret scans are defenses, not proof that all data is harmless. Review every publication candidate, including history and distribution archives.

## If a credential is exposed

Revoke or rotate it through its issuer first. Removing a file or rewriting history does not invalidate the credential or erase copies, forks, caches, or downloads. Do not repost the exposed value while reporting the incident.

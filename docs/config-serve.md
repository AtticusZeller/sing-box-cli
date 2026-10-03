# Configuration server

The server maps one domain to one private GitHub repository. Multiple domains can
share a server. Set `GH_TOKEN` or `GITHUB_TOKEN` to a fine-grained token with
**Contents: read** access to all registered repositories.

`config serve add` checks GitHub access and saves the binding; registering the same
binding again is safe. `config serve list` validates root `.json` files with the
sing-box core bundled with this package, prints valid URLs, and reports invalid
configurations on stderr. Those URLs require a separate subscription token;
the GitHub PAT stays on the server and is never used as a client credential.

## Subscription tokens

Use the short top-level command to manage subscription tokens:

```bash
sbc token -c client-linux.json             # create a token
sbc token -l                              # list token names and scopes
sbc token -r '<subscription-token>'        # revoke this exact token
sbc token -r client-linux.json             # revoke all tokens for this file
```

`-c FILE` prints the secret and a ready-to-use URL once. Store the secret when it is
shown; the server keeps only its SHA-256 hash in `tokens.json`. `-l` prints names,
domains, filenames and creation times without revealing secrets. Names are generated
automatically; optionally use `-n NAME` for a meaningful label. When multiple domains
are registered, add `-d DOMAIN` on creation. If a filename has tokens on multiple
domains, revoking by file also requires `-d DOMAIN` to choose one domain.

`-r TOKEN` matches the actual secret against the stored hash. `-r FILE.json` revokes
every token for that file without affecting other files. Multiple tokens may
authorize the same file, so rotation can create a replacement before revoking the
old token. Revocation never prints the secret.

The longer `config serve token -l/-c/-r` forms remain supported. The previous
`config serve token create/list/revoke` subcommands remain hidden compatibility
aliases; the legacy `revoke NAME` form still accepts a token name.

Clients can send the token as a Bearer header using `-t` / `--token`, or use the
printed URL with `?token=...` in applications that cannot set headers:

```bash
sbc config update https://sub.example.com/client-linux.json -t '<subscription-token>' --dry-run
sbc config update 'https://sub.example.com/client-linux.json?token=<subscription-token>' --dry-run
```

The CLI extracts URL tokens before printing or saving the URL, sends them as Bearer
credentials, and saves the clean URL and token separately on a normal update. URL
tokens take precedence over a previously saved token. Conflicting URL and explicit
`--token` credentials are rejected. `--dry-run` saves neither form.

The server accepts either form directly. Missing, incorrect, revoked or out-of-scope
credentials return **401** before any GitHub read. If both forms are supplied, they
must match. No token settings means all downloads are denied; unreadable or corrupt
settings also deny access. Token creation and revocation take effect on the next
request without restarting. A token cannot read another domain or filename.

Use HTTPS for public subscriptions. The service does not log request URLs or tokens;
keep token query strings out of reverse-proxy access logs as well.

## Background process

`config serve start` waits until the socket is listening, then prints the PID,
address and log path. Closing the terminal leaves the server running. Repeated
starts use the existing process, and repeated stops are safe. Run `stop` as the
same user with the same state directory. Restart after changing the GitHub token
or upgrading the package. Existing deployments must upgrade and restart to enable
subscription authentication; anonymous URLs then require a newly generated token.
Boot-time startup is managed separately by deployment.

Settings, process identity and `serve.log` are stored in the platform's
`sing-box-cli/serve` application directory (`~/.config/sing-box-cli/serve` on Linux).
Set `SBC_SERVE_DIR` to use another directory for all commands. Subscription token
hashes are stored there; GitHub PATs, subscription secrets and client configuration
are not. New registrations take effect while running.

## GitHub reads and validation

Requests read the repository's default branch through the GitHub Contents API.
No Git installation, clone/pull, background synchronization or persistent config
cache is needed. File updates and deletions take effect on subsequent requests.

Each response is validated and preserves the original content. Missing files,
invalid configurations and GitHub errors return HTTP errors. Configurations are
limited to 5 MiB and must be compatible with the bundled core. Referenced sibling
files are not downloaded. Existing client `--token` options and saved settings remain
supported; use an independent subscription token for this server's URLs.

Android's `route.override_android_vpn: true` is disabled only in the temporary
core-check copy, so a Linux or Windows server can distribute Android configurations.
Responses retain the original bytes, including the Android setting. Invalid
option types, unknown fields and other core errors are still rejected; this does
not verify the target device's runtime environment.

## Traefik

Merge [the example router and service](traefik-config-serve.yml) into the existing
`http.routers` and `http.services` sections. Traefik must preserve the `Host` header.
The example uses `websecure` and the host gateway from `digitalocean-public`'s
`~/devspace/traefik.yml`. Bind to that gateway for this deployment:

```bash
sbc config serve start --host 172.18.0.1 --port 8080
```

Replace `sub.example.com` with your domain and configure DNS and the TLS
certificate through your deployment. `--domain` only registers the binding.

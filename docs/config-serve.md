# Configuration server

The server maps one domain to one private GitHub repository. Multiple domains can
share a server. Set `GH_TOKEN` or `GITHUB_TOKEN` to a fine-grained token with
**Contents: read** access to all registered repositories.

`config serve add` checks GitHub access and saves the binding; registering the same
binding again is safe. `config serve list` validates root `.json` files with the
sing-box core bundled with this package, prints valid URLs, and reports invalid
configurations on stderr.

## Background process

`config serve start` waits until the socket is listening, then prints the PID,
address and log path. Closing the terminal leaves the server running. Repeated
starts use the existing process, and repeated stops are safe. Run `stop` as the
same user with the same state directory. Restart after changing the GitHub token
or upgrading the package. Boot-time startup is managed separately by deployment.

Settings, process identity and `serve.log` are stored in the platform's
`sing-box-cli/serve` application directory (`~/.config/sing-box-cli/serve` on Linux).
Set `SBC_SERVE_DIR` to use another directory for all commands. Tokens and client
configuration are not stored there. New registrations take effect while running.

## GitHub reads and validation

Requests read the repository's default branch through the GitHub Contents API.
No Git installation, clone/pull, background synchronization or persistent config
cache is needed. File updates and deletions take effect on subsequent requests.

Each response is validated and preserves the original content. Missing files,
invalid configurations and GitHub errors return HTTP errors. Configurations are
limited to 5 MiB and must be compatible with the bundled core. Referenced sibling
files are not downloaded. Existing client `--token` subscriptions and saved
settings remain supported; server URLs need no client token.

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

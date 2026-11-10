# kubeconfig-manager

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/cwaits6/kubeconfig-manager?style=flat-square&logo=github)](https://github.com/cwaits6/kubeconfig-manager/releases/latest)
[![Release](https://github.com/cwaits6/kubeconfig-manager/actions/workflows/release.yml/badge.svg)](https://github.com/cwaits6/kubeconfig-manager/actions/workflows/release.yml)
[![PR](https://github.com/cwaits6/kubeconfig-manager/actions/workflows/pr.yml/badge.svg)](https://github.com/cwaits6/kubeconfig-manager/actions/workflows/pr.yml)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](./LICENSE)

A lightweight Python CLI to merge Kubernetes kubeconfig files into your main `~/.kube/config` from two sources:

1. **Manual kubeconfig files** — Download and merge kubeconfigs from any cluster (EKS, on-prem, etc.)
2. **Rancher API** — Automatically fetch and sync kubeconfigs for all clusters across multiple Rancher instances

Handles conflicts interactively, auto-deletes merged files, and lets you switch the active context — all from your terminal.

---

## Features

* **File merging** — Safely merge clusters, contexts, and users from downloaded kubeconfig files
* **Rancher sync** — Automatically fetch kubeconfigs from all clusters across multiple Rancher instances
* **Conflict handling** — Prompts before overwriting entries in file mode; Rancher sync uses instance-qualified names and refreshes credentials
* **Persistent Rancher names** — Choose context names by Rancher cluster ID, including each instance's `local` cluster
* **Auto-cleanup** — Deletes source files after successful merge
* **Context switching** — Interactively select a new current context
* **Secure credential storage** — Rancher API tokens stored locally with 600 permissions

---

## Requirements

* **Python ≥ 3.13**
* **[uv](https://docs.astral.sh/uv/)**
* For Rancher sync: Network access to your Rancher instances (API tokens stored securely locally)

---

## Setup

Clone this repository and sync dependencies:

```bash
git clone https://github.com/cwaits6/kubeconfig-manager.git
cd kubeconfig-manager
uv sync
```

This creates a local `.venv/` environment and installs all dependencies exactly as pinned in `uv.lock`.

---

## Usage

### 1. Merge a downloaded kubeconfig file

```bash
uv run python newKube.py ~/Downloads/another-kubeconfig.yaml
```

The tool will:
- Merge the kubeconfig into `~/.kube/config`
- Prompt about any conflicting entries
- Offer to change your current context interactively
- **Auto-delete the source file** after successful merge

### 2. Sync kubeconfigs from Rancher instances

```bash
uv run python newKube.py --rancher
```

**First run:** Creates a config template at `~/.config/kubeconfig-manager/rancher.yaml`. Edit it to add your Rancher instances:

```yaml
instances:
  - name: prod-rancher
    url: https://rancher.prod.example.com
    token_env: PROD_RANCHER_TOKEN
  - name: staging-rancher
    url: https://rancher.staging.example.com
    token_env: STAGING_RANCHER_TOKEN
    verify_ssl: false  # optional, for self-signed certs
```

Then run again to fetch and merge kubeconfigs from all clusters across all instances:

```bash
uv run python newKube.py --rancher
```

The tool will:
- Fetch kubeconfigs for **all clusters** from each Rancher instance
- **Silently overwrite** existing entries with fresh credentials (ideal for credential rotation)
- Merge everything into `~/.kube/config` in one operation
- Print a summary of what was synced

Use `token_env` to name an exported environment variable containing the complete
API key. For example, add the following yourself to your private shell file,
replacing the placeholders there, then load it in your own shell:

```zsh
export PROD_RANCHER_TOKEN='ACCESS_KEY:SECRET_KEY'
export STAGING_RANCHER_TOKEN='ACCESS_KEY:SECRET_KEY'
```

The CLI reads only the named variables at runtime. It does not open or source
shell files, print token values, or copy environment tokens into its YAML files.
Use exactly one of `token_env` or a literal `token` per instance; both formats
are supported. A missing variable stops sync before any network requests.
Scheduled runs must receive the exported variables too; they may not load zsh.

#### Persistent context names

By default, contexts use `<instance-name>-<cluster-id>` (for example,
`prod-rancher-local`). On the first interactive sync, the CLI offers a name for
each new cluster, identifying its Rancher instance and cluster ID. Accept the
default or type something like `prod-management`. Choices are stored in
`~/.config/kubeconfig-manager/state.yaml`, keyed by Rancher URL and cluster ID.
Future syncs reuse them, even after token rotation or instance display-name
changes. Keep the Rancher URL stable to retain this association.

Use `--rancher --rename-contexts` to prompt again. Use `--non-interactive` for
saved/default names without prompts. Non-terminal runs also use saved/default
names. Interactive validation rejects names already assigned to another cluster.

For declarative configuration, `context_names` on each instance overrides saved
choices and bypasses prompts for those cluster IDs:

```yaml
context_names:
  local: prod-management
  c-xxxxx: prod-workloads
```

Keys are stable Rancher cluster IDs, not display names.

Cluster and user entries are also qualified, and context references are updated
together, so two Rancher instances cannot overwrite each other's `local`
credentials. Additional contexts in a generated kubeconfig use
`<chosen-name>::<original-context-name>`. Choose unique names across all mappings;
duplicate generated entry names abort the sync before writing the kubeconfig.

Existing entries from earlier versions are retained. After the first successful
sync, select one of the new contexts explicitly with
`kubectl config use-context prod-management`. The old shared `local` context
cannot reliably identify which Rancher it belonged to and is not automatically
removed or selected. Changing a mapping later creates entries under the new name;
previous entries remain until you remove them yourself.

#### HTTP 401 after token rotation

Sync reads each token from the configured `token_env` variable or literal
`token` in `~/.config/kubeconfig-manager/rancher.yaml`. Rotating a token in Rancher
requires updating that source too. Use the **complete API key** in
`access-key:secret-key` format from the corresponding Rancher instance, without
a `Bearer ` prefix or surrounding whitespace. Make sure the token is active,
unexpired, and scoped for the clusters you want to sync.

HTTP 401 means the server rejected the configured credentials. HTTP 403 means
access was denied; check token scope and permissions. Failed requests produce
a nonzero exit status. If every fetch fails, the main kubeconfig is left untouched;
if some succeed, those are merged and the sync reports its failures.

#### Managed tokens and reviewed cleanup

```sh
nkc --rancher --managed-tokens --token-ttl-hours 24
nkc --rancher --cleanup-tokens
nkc --rancher --cleanup-tokens --delete
```

Managed mode creates a fresh cluster-scoped token on each sync, with an explicit
lifetime (24 hours by default, subject to Rancher limits). Your configured API
key needs permission to create and inspect its tokens. The CLI builds a
kubeconfig using Rancher's HTTPS proxy and verifies the new credential against
the cluster's `/version` endpoint before publishing it. This mode does not call
`generateKubeconfig`, and supplies only the Rancher proxy context, not direct
cluster endpoints. The generated kubeconfig uses the system CA trust; an
instance with `verify_ssl: false` also produces a context that skips TLS
verification. Sync again before the managed token expires.

Each created token has a unique description containing `kubeconfig-manager`,
the installation ID, and cluster ID. Its ID, server UUID, user, cluster, and
description are recorded in `state.yaml` with permissions 600. Token secrets
are stored only in the kubeconfig. Back up the state file: losing it also loses
the CLI's proof of ownership. Concurrent CLI sync/cleanup runs are blocked by
a local file lock.

Cleanup previews only tokens with matching ownership records. It excludes the
authentication token, tokens marked active, and tokens still referenced anywhere
in the main kubeconfig. Deletion requires an interactive confirmation and checks
ownership and kubeconfig references again before revocation. It never adopts
or deletes existing untracked Rancher tokens based solely on their descriptions.
Those need separate manual review, because Rancher may reuse generated tokens.

Old entries retained after renaming keep their credentials protected from cleanup.
Remove those obsolete local entries when you no longer need them, then sync to
refresh the ownership state. Other kubeconfig files and machines are not tracked;
revoking a token also invalidates any copies using it. If a token creation request
succeeds remotely but the response or local state write is lost, review that
token manually; the CLI will not infer ownership to delete an unrecorded token.

---

## Shell Alias

If you want an alias to add to your `.zshrc` or `.bashrc` file, here is a template. Be sure to specify your specific `KCM_DIR` where you cloned the repo and source it afterwards:

```zshrc
# ------------------------------------------------ #
#               KubeConfig Manager                 #
# ------------------------------------------------ #

export UV_NATIVE_TLS=true
export KCM_DIR="$HOME/repos/kubeconfig-manager"
alias nkc='uv run --project "$KCM_DIR" python "$KCM_DIR/newKube.py"'
```

With this alias, you can run either:

```shell
# Merge a file
nkc ~/Downloads/my-kubeconfig.yaml

# Sync from Rancher
nkc --rancher
```

---

## Use Cases

### With Rancher (`--rancher`)

If you manage clusters through Rancher — including EKS, RKE2, K3s, or any other downstream cluster type — the `--rancher` flag is the easiest way to keep your kubeconfig up to date:

- Sync kubeconfigs for **all downstream clusters** (EKS, RKE2, K3s, etc.) in one command
- Keep credentials fresh across multiple Rancher instances without manual downloads
- Automate credential rotation by running `nkc --rancher` on a schedule (cron job, etc.)

> **EKS clusters in Rancher:** If your EKS cluster is registered as a downstream cluster in Rancher, use `nkc --rancher` to manage its kubeconfig — no need to run `aws eks update-kubeconfig` separately. Rancher handles credential management for all downstream clusters, regardless of where they're hosted.

### Without Rancher (file merge)

If you're not using Rancher, the file merge mode works with kubeconfigs from any source:

- Kubeconfigs exported from cloud providers (AWS EKS, GKE, AKS, etc.)
- On-premises or bare-metal cluster configs
- Configs from other management platforms
- Any valid kubeconfig YAML file

---

## Troubleshooting

If you receive this error when trying to run:

```shell
  × Failed to fetch: `https://pypi.org/simple/questionary/`
  ├─▶ Request failed after 3 retries
  ├─▶ error sending request for url (https://pypi.org/simple/questionary/)
  ├─▶ client error (Connect)
  ╰─▶ invalid peer certificate: UnknownIssuer
  help: Consider enabling use of system TLS certificates with the `--native-tls` command-line flag
```

You either have to run the following `uv` command:

```shell
uv --native-tls sync
```

Or add the following to your `zshrc` or `bashrc` file and then source it:

```zshrc
export UV_NATIVE_TLS=true
```

---

## Rancher Credential Security

When you run `nkc --rancher` for the first time, the tool creates a config file at:

```
~/.config/kubeconfig-manager/rancher.yaml
```

This file is created with **600 permissions** (readable/writable by you only). Your Rancher API tokens stored here are never logged or printed by the tool. Note that kubeconfigs generated by Rancher may include user credentials (e.g., tokens or client certificates) that get merged into `~/.kube/config`. Consider using scoped RBAC tokens in Rancher if you want to limit what credentials land in your kubeconfig.

---

## Cloud Provider Notes

### AWS EKS

If your EKS clusters are registered as **downstream clusters in Rancher**, use `nkc --rancher` to manage their kubeconfigs. Rancher generates kubeconfigs that route through its authentication proxy, handling credential management for you. This is the recommended approach — it eliminates the need to manage AWS CLI authentication separately for each cluster.

If you're **not using Rancher**, you can still use this tool with EKS. Generate kubeconfigs with `aws eks update-kubeconfig`, then merge them using the file mode. EKS kubeconfigs use exec plugins that fetch short-lived tokens on-demand, so they don't require manual credential renewal.

### GKE, AKS, and other providers

Export kubeconfigs from your provider and merge them using the file mode. If the cluster is managed as a Rancher downstream cluster, use `--rancher` instead.

---

## License

This project is licensed under the [MIT License](./LICENSE).

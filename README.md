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
* **Conflict handling** — Detects and prompts before overwriting existing entries (file mode) or silently overwrites with fresh credentials (Rancher mode)
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
    token: token-abcde:secretkey
  - name: staging-rancher
    url: https://rancher.staging.example.com
    token: token-fghij:secretkey
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

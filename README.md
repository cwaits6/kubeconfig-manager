# kubeconfig-manager

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
alias nkc=’uv run --project "$KCM_DIR" python "$KCM_DIR/newKube.py"’
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

**Manual file merging:**
- Adding a new EKS cluster (generated via `aws eks update-kubeconfig`)
- Integrating kubeconfigs from on-premises clusters
- Combining kubeconfigs from different cloud providers
- Safe merging with interactive conflict resolution

**Rancher sync:**
- Keep kubeconfigs fresh across multiple Rancher instances without manual downloads
- Automate credential rotation by running `nkc --rancher` on a schedule (cron job, etc.)
- Consolidate access to all Rancher-managed clusters in one place

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

This file is created with **600 permissions** (readable/writable by you only). Store your Rancher API tokens here — they are never logged, printed, or included in your kubeconfigs.

---

## AWS EKS + Other Clouds

This tool works great for managing multiple cluster types:

- **AWS EKS:** Use `aws eks update-kubeconfig` to generate kubeconfig files, then merge them with this tool
- **Rancher:** Use `--rancher` to automatically sync all Rancher-managed clusters
- **On-premises/other clouds:** Export kubeconfigs and merge them via the file mode

The EKS kubeconfigs use exec plugins that fetch short-lived tokens on-demand, so they don't need manual renewal — this tool simply consolidates multiple kubeconfigs into one place.

---

## License

This project is licensed under the [MIT License](./LICENSE).

# kubeconfig-manager

A lightweight Python CLI to merge additional Kubernetes kubeconfig files into your main `~/.kube/config`, handle conflicts interactively, and optionally switch the active context — all from your terminal.

---

## Features

* Safely merges clusters, contexts, and users across kubeconfigs
* Detects and prompts before overwriting existing entries
* Lets you interactively select a new current context
* Fully self-contained — no dependencies outside of Python

---

## Requirements

* **Python ≥ 3.13**
* **[uv](https://docs.astral.sh/uv/)**

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

### Merge a kubeconfig

```bash
uv run python newKube.py ~/Downloads/another-kubeconfig.yaml
```

You’ll be prompted about duplicate entries and offered the option to change your current context interactively.

If you want an alias to add to your `.zshrc` or `.bashrc` file, here is a template, be sure to specify your specific `KCM_DIR` where you cloned the repo & source it afterwards:

```zshrc
# ------------------------------------------------ #
#               KubeConfig Manager                 #
# ------------------------------------------------ #

export UV_NATIVE_TLS=true
export KCM_DIR="$HOME/repos/kubeconfig-manager"
alias nkc='uv run --project "$KCM_DIR" python "$KCM_DIR/newKube.py"'
```

With this alias, you can `cd` to the directory where you have your kubeconfig file downloaded and run the following:

```shell
nkc <your-kube-config.yaml>
```

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

## License

This project is licensed under the [MIT License](./LICENSE).

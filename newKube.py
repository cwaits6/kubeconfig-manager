#!/usr/bin/env python3
import argparse
from copy import deepcopy
import os
import sys
import yaml
import questionary
import requests
from urllib.parse import urlsplit

import rancher_managed as managed

EMPTY_KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [],
    "contexts": [],
    "users": [],
    "current-context": "",
}

def load_yaml(path):
    """Read YAML without disclosing file contents when parsing fails."""
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Error: File not found: {path}")
    except yaml.YAMLError:
        sys.exit(f"Error: Could not parse YAML in {path}. Details suppressed to protect credentials.")

def load_or_create_kubeconfig(path):
    """Load a kubeconfig, creating a stub if the file is missing or invalid."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_yaml(path, EMPTY_KUBECONFIG)
        os.chmod(path, 0o600)
        print(f"Created empty kubeconfig at: {path}")
        return dict(EMPTY_KUBECONFIG)
    config = load_yaml(path)
    if not isinstance(config, dict):
        print(f"Warning: {path} is not a valid kubeconfig mapping. Reinitializing.")
        write_yaml(path, EMPTY_KUBECONFIG)
        return dict(EMPTY_KUBECONFIG)
    return config

def write_yaml(path, data):
    """Write a YAML document or stop with a filesystem error."""
    try:
        with open(path, 'w') as f:
            yaml.safe_dump(data, f, default_flow_style=False)
    except Exception as e:
        sys.exit(f"Error: Could not write YAML to {path}: {e}")

def merge_section(target, source, section_name, force=False):
    """
    Merge the entries of section_name from source into target.
    If the target does not have the section, it will be created.
    On name collisions:
      - if the entries are identical: skip
      - if they differ and force=False: prompt to overwrite
      - if they differ and force=True: overwrite silently
    """
    source_entries = source.get(section_name, [])
    if not isinstance(source_entries, list):
        sys.exit(f"Error: The {section_name} section in the input file is not a list.")

    # Ensure the section exists in target
    if section_name not in target or target[section_name] is None:
        target[section_name] = []

    for entry in source_entries:
        if not isinstance(entry, dict) or 'name' not in entry:
            continue
        entry_name = entry['name']

        # Find existing entry with same name
        existing_index = next(
            (i for i, e in enumerate(target[section_name])
             if isinstance(e, dict) and e.get('name') == entry_name),
            None
        )

        if existing_index is not None:
            existing_entry = target[section_name][existing_index]
            if existing_entry == entry:
                print(f"Skipping identical {section_name[:-1]} '{entry_name}'.")
            elif force:
                target[section_name][existing_index] = entry
                print(f"Overwritten {section_name[:-1]} '{entry_name}' (force).")
            else:
                # Prompt to overwrite
                overwrite = questionary.confirm(
                    f"A {section_name[:-1]} named '{entry_name}' already exists but differs. Overwrite?"
                ).ask()
                if overwrite:
                    target[section_name][existing_index] = entry
                    print(f"Overwritten {section_name[:-1]} '{entry_name}'.")
                else:
                    print(f"Kept existing {section_name[:-1]} '{entry_name}'.")
        else:
            # No collision → just add it
            target[section_name].append(entry)
            print(f"Added {section_name[:-1]} '{entry_name}'.")

def prompt_change_context(config):
    """
    Prompts the user to optionally change the current context.
    Uses questionary for interactive arrow-key navigation.
    """
    if 'contexts' not in config or not config['contexts']:
        print("No contexts available in the merged config to select from.")
        return config.get("current-context")

    change = questionary.confirm("Would you like to change the current context?").ask()
    if not change:
        return config.get("current-context")

    # Build a list of context names from the config
    context_names = [entry.get("name") for entry in config.get("contexts", []) if entry.get("name")]
    if not context_names:
        print("No valid contexts found.")
        return config.get("current-context")

    selected = questionary.select(
        "Select a context:",
        choices=context_names
    ).ask()

    if selected:
        config["current-context"] = selected
        print(f"Current context set to: {selected}")
    else:
        print("No context selected; current context remains unchanged.")

    return config.get("current-context")

def load_rancher_config():
    """
    Load and validate Rancher instance configuration from ~/.config/kubeconfig-manager/rancher.yaml.
    On first run, creates the config file with a template and exits.
    Returns a list of instance dicts.
    """
    config_dir = os.path.join(os.path.expanduser("~"), ".config", "kubeconfig-manager")
    config_path = os.path.join(config_dir, "rancher.yaml")

    # If config doesn't exist, create template and exit
    if not os.path.exists(config_path):
        os.makedirs(config_dir, exist_ok=True)

        template = """# Rancher instances for kubeconfig-manager
# Each entry requires: name, url, and either token_env or token
# Optional: verify_ssl: false  (for self-signed certs, default true)
# Optional: context_names: {local: my-management-cluster}
instances:
  # - name: my-rancher
  #   url: https://rancher.example.com
  #   token_env: MY_RANCHER_TOKEN  # exported by your shell
"""
        with open(config_path, 'w') as f:
            f.write(template)
        os.chmod(config_path, 0o600)
        print(f"Created Rancher config template at: {config_path}")
        print("Please edit this file with your Rancher instance details and run again.")
        sys.exit(0)

    # Check file permissions
    file_mode = os.stat(config_path).st_mode & 0o777
    if file_mode & 0o077:
        sys.exit(f"Error: {config_path} has permissions {oct(file_mode)}. "
                 "It must not be group/world readable (expected 0o600). "
                 "Fix with: chmod 600 " + config_path)

    # Load and validate the config
    config = load_yaml(config_path)
    if not isinstance(config, dict) or 'instances' not in config:
        sys.exit(f"Error: {config_path} must contain an 'instances' key.")

    instances = config['instances']
    if not isinstance(instances, list) or len(instances) == 0:
        sys.exit(f"Error: 'instances' in {config_path} must be a non-empty list.")

    # Validate each instance
    names = set()
    for i, instance in enumerate(instances):
        if not isinstance(instance, dict):
            sys.exit(f"Error: Instance {i} is not a dictionary.")
        for required_field in ['name', 'url']:
            if required_field not in instance:
                sys.exit(f"Error: Instance {i} is missing required field '{required_field}'.")
            if not isinstance(instance[required_field], str) or not instance[required_field].strip():
                sys.exit(f"Error: Instance {i} field '{required_field}' must be a non-empty string.")
        if instance['name'] in names:
            sys.exit("Error: Rancher instance names must be unique.")
        names.add(instance['name'])
        parsed = urlsplit(instance['url'])
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            sys.exit(f"Error: Instance '{instance['name']}' requires an HTTPS URL without embedded credentials, query, or fragment.")
        try:
            instance['token'] = managed.resolve_token(instance)
        except managed.ManagedError as error:
            sys.exit(f"Error: Instance '{instance['name']}': {error}")
        context_names = instance.get('context_names', {})
        if not isinstance(context_names, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in context_names.items()
        ):
            sys.exit(f"Error: Instance '{instance['name']}' context_names must map cluster IDs to non-empty names.")
        normalized_names = {key.strip(): value.strip() for key, value in context_names.items()}
        if len(normalized_names) != len(context_names):
            sys.exit(f"Error: Instance '{instance['name']}' has duplicate cluster IDs after stripping whitespace.")
        instance['context_names'] = normalized_names

    return instances

def name_rancher_kubeconfig(config, instance, cluster_id):
    """Use a stable cluster ID mapping and rename every reference together."""
    config = deepcopy(config)
    base = instance.get('context_names', {}).get(cluster_id, f"{instance['name']}-{cluster_id}")
    primary = config.get('current-context')
    contexts = config.get('contexts', [])
    if not primary and contexts:
        primary = contexts[0]['name']
    mappings = {}
    for section in ('clusters', 'users', 'contexts'):
        mappings[section] = {
            entry['name']: (base if section == 'contexts' and entry['name'] == primary
                            else f"{base}::{entry['name']}")
            for entry in config.get(section, [])
        }
        for entry in config.get(section, []):
            entry['name'] = mappings[section][entry['name']]
    for entry in contexts:
        context = entry['context']
        for field, section in (('cluster', 'clusters'), ('user', 'users')):
            if field in context:
                context[field] = mappings[section][context[field]]
    if primary:
        config['current-context'] = mappings['contexts'][primary]
    return config

def report_rancher_error(error, name, operation):
    """Never include response bodies, request URLs, or credentials in errors."""
    status = error.response.status_code if error.response is not None else None
    if status == 401:
        detail = ("HTTP 401: Rancher rejected the configured API token. Update token in "
                  "~/.config/kubeconfig-manager/rancher.yaml or its token_env variable with the complete access-key:secret-key "
                  "pair issued by this Rancher instance; check expiration/revocation and token scope.")
    elif status == 403:
        detail = "HTTP 403: Access denied; check the token's cluster scope and Rancher permissions."
    else:
        detail = f"HTTP {status}" if status else type(error).__name__
    print(f"Warning: {operation} failed for '{name}': {detail}")

def list_rancher_clusters(instance):
    """Fetch cluster metadata without generating kubeconfigs or credentials."""
    url = instance['url'].rstrip('/')
    token = instance['token']
    name = instance['name']
    verify_ssl = instance.get('verify_ssl', True)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Fetch cluster list
    try:
        clusters_url = f"{url}/v3/clusters"
        response = requests.get(clusters_url, headers=headers, verify=verify_ssl, timeout=10)
        response.raise_for_status()
        clusters_data = response.json()
    except requests.exceptions.RequestException as e:
        report_rancher_error(e, name, 'Fetching cluster list')
        return [], 1

    # Extract cluster IDs
    clusters = clusters_data.get('data', [])
    if not clusters:
        print(f"No clusters found in '{name}'.")
        return [], 0
    return clusters, 0


def fetch_rancher_kubeconfigs(instance, clusters=None):
    """Fetch named kubeconfigs after all cluster naming prompts succeed."""
    failures = 0
    if clusters is None:
        clusters, failures = list_rancher_clusters(instance)
    url = instance['url'].rstrip('/')
    name = instance['name']
    verify_ssl = instance.get('verify_ssl', True)
    headers = {'Authorization': f"Bearer {instance['token']}", 'Content-Type': 'application/json'}
    kubeconfigs = []
    if not instance.get('_names_planned'):
        plan_cluster_names(instance, clusters)

    # Credential creation begins only after the naming pass completes.
    for cluster in clusters:
        cluster_id = cluster.get('id')
        if not cluster_id:
            continue

        try:
            if instance.get('_managed_tokens'):
                cluster_config = managed.create_kubeconfig(
                    instance, cluster_id, instance['_state'], instance['_ttl_hours'])
                kubeconfigs.append(name_rancher_kubeconfig(cluster_config, instance, cluster_id))
                continue
            kubeconfig_url = f"{url}/v3/clusters/{cluster_id}?action=generateKubeconfig"
            response = requests.post(kubeconfig_url, headers=headers, verify=verify_ssl, timeout=10)
            response.raise_for_status()
            kubeconfig_data = response.json()
            kubeconfig_yaml = kubeconfig_data.get('config')

            if kubeconfig_yaml:
                cluster_config = yaml.safe_load(kubeconfig_yaml)
                if not isinstance(cluster_config, dict):
                    raise ValueError('Invalid kubeconfig')
                kubeconfigs.append(name_rancher_kubeconfig(cluster_config, instance, cluster_id))
            else:
                print(f"Warning: No 'config' field in response for cluster '{cluster_id}' from '{name}'.")
                failures += 1
        except requests.exceptions.RequestException as e:
            report_rancher_error(e, name, f"Fetching kubeconfig for cluster '{cluster_id}'")
            failures += 1
            continue
        except managed.ManagedError as error:
            print(f"Warning: Cluster '{cluster_id}' from '{name}': {error}")
            failures += 1
        except (yaml.YAMLError, ValueError, KeyError, TypeError):
            print(f"Warning: Invalid kubeconfig for cluster '{cluster_id}' from '{name}'; contents suppressed.")
            failures += 1

    return kubeconfigs, failures


def plan_cluster_names(instance, clusters):
    """Complete cancellable naming work before creating remote credentials."""
    if '_state' in instance:
        for cluster in clusters:
            if cluster.get('id'):
                managed.choose_name(instance, cluster['id'], instance['_state'],
                                    instance.get('_interactive', False), instance.get('_rename', False))

def rancher_sync(managed_tokens=False, rename=False, non_interactive=False, ttl_hours=24,
                 cleanup_tokens=False, delete=False):
    """
    Fetch kubeconfigs from all configured Rancher instances and merge them into ~/.kube/config.
    """
    instances = load_rancher_config()
    main_config_path = os.path.join(os.path.expanduser("~"), ".kube", "config")
    state = managed.load_state()
    interactive = sys.stdin.isatty() and not non_interactive
    if rename and not interactive:
        raise managed.ManagedError('--rename-contexts requires an interactive terminal.')
    if cleanup_tokens:
        if delete and not interactive:
            raise managed.ManagedError('Token deletion requires an interactive terminal for review and confirmation.')
        if not os.path.exists(main_config_path):
            raise managed.ManagedError('Main kubeconfig is missing; restore it before token cleanup.')
        config = load_yaml(main_config_path)
        if not isinstance(config, dict):
            raise managed.ManagedError('Cannot verify token references in an invalid kubeconfig.')
        return managed.cleanup(instances, state, config, delete=delete,
                               config_loader=lambda: load_yaml(main_config_path))

    pending = []
    failures = 0
    owners = {}
    reserved_names = {}
    for url, names in state['names'].items():
        for cluster_id, context_name in names.items():
            reserved_names[context_name] = (url, cluster_id)
    for instance in instances:
        for cluster_id, context_name in instance.get('context_names', {}).items():
            identity = (instance['url'].rstrip('/'), cluster_id)
            if context_name in reserved_names and reserved_names[context_name] != identity:
                raise managed.ManagedError('Configured context names must be unique across Rancher clusters.')
            reserved_names[context_name] = identity

    # Plan names across every instance before any token-creating request.
    plans = []
    for instance in instances:
        instance.update({'_state': state, '_interactive': interactive, '_rename': rename,
                         '_managed_tokens': managed_tokens, '_ttl_hours': ttl_hours,
                         '_reserved_names': reserved_names})
        print(f"\nSyncing from Rancher instance: {instance['name']}")
        clusters, failed = list_rancher_clusters(instance)
        failures += failed
        plan_cluster_names(instance, clusters)
        instance['_names_planned'] = True
        plans.append((instance, clusters))

    for instance, clusters in plans:
        kubeconfigs, failed = fetch_rancher_kubeconfigs(instance, clusters)
        failures += failed

        for cluster_config in kubeconfigs:
            for section in ('clusters', 'contexts', 'users'):
                for entry in cluster_config.get(section, []):
                    key = (section, entry['name'])
                    if key in owners:
                        sys.exit("Error: Rancher context naming collision. Choose unique context_names; kubeconfig was not changed.")
                    owners[key] = instance['name']
            pending.append(cluster_config)

    if not pending:
        print("No kubeconfigs fetched; main kubeconfig was not changed.")
        return 1 if failures else 0

    print(f"Loading main kubeconfig from: {main_config_path}")
    main_config = load_or_create_kubeconfig(main_config_path)
    for cluster_config in pending:
        for section in ('clusters', 'contexts', 'users'):
            merge_section(main_config, cluster_config, section, force=True)

    # Write the merged config once
    # Save chosen names before publishing credentials, then mark token ownership
    # active only after the kubeconfig has been durably replaced.
    managed.save_state(state)
    managed.atomic_yaml(main_config_path, main_config)
    managed.commit_tokens(state, main_config)
    print(f"\nRancher sync {'incomplete' if failures else 'complete'}. Merged {len(pending)} kubeconfig(s) into {main_config_path}; {failures} failure(s).")
    return 1 if failures else 0

def main():
    """Validate CLI options and run file merging, Rancher sync, or cleanup."""
    parser = argparse.ArgumentParser(description="Merge an additional kubeconfig into your main kubeconfig at $HOME/.kube/config, or sync from configured Rancher instances.")
    parser.add_argument("input_kubeconfig", nargs="?", help="Path to the kubeconfig file to merge.")
    parser.add_argument("--rancher", action="store_true", help="Fetch and merge kubeconfigs from all configured Rancher instances.")
    parser.add_argument('--managed-tokens', action='store_true', help='Create dedicated, tracked cluster-scoped tokens instead of Rancher-generated kubeconfig tokens.')
    parser.add_argument('--token-ttl-hours', type=int, default=24, help='Managed token lifetime in hours (default: 24; Rancher may enforce a maximum).')
    parser.add_argument('--rename-contexts', action='store_true', help='Prompt again for saved Rancher context names.')
    parser.add_argument('--non-interactive', action='store_true', help='Use saved or default names without prompting.')
    parser.add_argument('--cleanup-tokens', action='store_true', help='Preview verified obsolete managed tokens without syncing.')
    parser.add_argument('--delete', action='store_true', help='With --cleanup-tokens, review and confirm token revocation.')
    args = parser.parse_args()
    if args.token_ttl_hours < 1:
        parser.error('--token-ttl-hours must be at least 1.')
    if (args.managed_tokens or args.rename_contexts or args.non_interactive or args.cleanup_tokens or args.delete) and not args.rancher:
        parser.error('Rancher options require --rancher.')
    if args.delete and not args.cleanup_tokens:
        parser.error('--delete requires --cleanup-tokens.')
    if args.cleanup_tokens and (args.managed_tokens or args.rename_contexts):
        parser.error('--cleanup-tokens cannot be combined with sync options.')

    # Validate mutual exclusion
    if args.rancher and args.input_kubeconfig:
        parser.error("--rancher and a file path are mutually exclusive.")
    if not args.rancher and not args.input_kubeconfig:
        parser.error("Provide either a kubeconfig file path or --rancher.")

    if args.rancher:
        # Rancher sync flow
        try:
            with managed.lock():
                result = rancher_sync(managed_tokens=args.managed_tokens, rename=args.rename_contexts,
                                     non_interactive=args.non_interactive, ttl_hours=args.token_ttl_hours,
                                     cleanup_tokens=args.cleanup_tokens, delete=args.delete)
            sys.exit(result)
        except (managed.ManagedError, OSError) as error:
            message = str(error) if isinstance(error, managed.ManagedError) else type(error).__name__
            sys.exit(f'Error: {message}')
    else:
        # File merge flow
        input_path = os.path.abspath(args.input_kubeconfig)
        main_config_path = os.path.join(os.path.expanduser("~"), ".kube", "config")

        # Guard against merging the live kubeconfig into itself
        try:
            if os.path.samefile(input_path, main_config_path):
                sys.exit("Error: Input file is the same as the main kubeconfig. Cannot merge a file into itself.")
        except OSError:
            if os.path.abspath(input_path) == os.path.abspath(main_config_path):
                sys.exit("Error: Input file is the same as the main kubeconfig. Cannot merge a file into itself.")

        # Load the input kubeconfig
        print(f"Loading input kubeconfig from: {input_path}")
        input_config = load_yaml(input_path)
        if not isinstance(input_config, dict):
            sys.exit("Error: The input kubeconfig is not a valid YAML mapping.")

        # Load the main kubeconfig (create stub if missing)
        print(f"Loading main kubeconfig from: {main_config_path}")
        main_config = load_or_create_kubeconfig(main_config_path)

        # Merge the sections: clusters, contexts, users
        for section in ["clusters", "contexts", "users"]:
            merge_section(main_config, input_config, section)

        # Write out the updated main kubeconfig
        write_yaml(main_config_path, main_config)
        print(f"Merged kubeconfig written to: {main_config_path}")

        # Delete the source file
        try:
            os.remove(input_path)
            print(f"Deleted source file: {input_path}")
        except OSError as e:
            print(f"Warning: Could not delete source file {input_path}: {e}")

        # Prompt for changing the current context
        new_context = prompt_change_context(main_config)
        # Write updated current-context back to file if changed
        write_yaml(main_config_path, main_config)
        print(f"Final kubeconfig current-context: {new_context}")

if __name__ == "__main__":
    main()

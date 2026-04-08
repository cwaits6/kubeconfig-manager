#!/usr/bin/env python3
import argparse
import os
import sys
import yaml
import questionary
import requests

EMPTY_KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [],
    "contexts": [],
    "users": [],
    "current-context": "",
}

def load_yaml(path):
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Error: File not found: {path}")
    except yaml.YAMLError as e:
        sys.exit(f"Error: Could not parse YAML in {path}: {e}")

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
# Each entry requires: name, url, token
# Optional: verify_ssl: false  (for self-signed certs, default true)
instances:
  # - name: my-rancher
  #   url: https://rancher.example.com
  #   token: token-abcde:secretkey
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
    for i, instance in enumerate(instances):
        if not isinstance(instance, dict):
            sys.exit(f"Error: Instance {i} is not a dictionary.")
        for required_field in ['name', 'url', 'token']:
            if required_field not in instance:
                sys.exit(f"Error: Instance {i} is missing required field '{required_field}'.")
        if not instance['url'].startswith("https://"):
            sys.exit(f"Error: Instance '{instance['name']}' URL must use HTTPS (got: {instance['url']}).")

    return instances

def fetch_rancher_kubeconfigs(instance):
    """
    Fetch kubeconfigs for all clusters from a Rancher instance.
    Returns a list of kubeconfig YAML strings.
    """
    url = instance['url']
    token = instance['token']
    name = instance['name']
    verify_ssl = instance.get('verify_ssl', True)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    kubeconfigs = []

    # Fetch cluster list
    try:
        clusters_url = f"{url}/v3/clusters"
        response = requests.get(clusters_url, headers=headers, verify=verify_ssl, timeout=10)
        response.raise_for_status()
        clusters_data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Warning: Could not fetch cluster list from '{name}' ({url}): {e}")
        return []

    # Extract cluster IDs
    clusters = clusters_data.get('data', [])
    if not clusters:
        print(f"No clusters found in '{name}'.")
        return []

    # Fetch kubeconfig for each cluster
    for cluster in clusters:
        cluster_id = cluster.get('id')
        if not cluster_id:
            continue

        try:
            kubeconfig_url = f"{url}/v3/clusters/{cluster_id}?action=generateKubeconfig"
            response = requests.post(kubeconfig_url, headers=headers, verify=verify_ssl, timeout=10)
            response.raise_for_status()
            kubeconfig_data = response.json()
            kubeconfig_yaml = kubeconfig_data.get('config')

            if kubeconfig_yaml:
                kubeconfigs.append(kubeconfig_yaml)
            else:
                print(f"Warning: No 'config' field in response for cluster '{cluster_id}' from '{name}'.")
        except requests.exceptions.RequestException as e:
            print(f"Warning: Could not fetch kubeconfig for cluster '{cluster_id}' from '{name}': {e}")
            continue

    return kubeconfigs

def rancher_sync():
    """
    Fetch kubeconfigs from all configured Rancher instances and merge them into ~/.kube/config.
    """
    instances = load_rancher_config()
    main_config_path = os.path.join(os.path.expanduser("~"), ".kube", "config")

    # Load the main kubeconfig (create stub if missing)
    print(f"Loading main kubeconfig from: {main_config_path}")
    main_config = load_or_create_kubeconfig(main_config_path)

    total_merged = 0

    # Fetch and merge from each Rancher instance
    for instance in instances:
        print(f"\nSyncing from Rancher instance: {instance['name']} ({instance['url']})")
        kubeconfigs = fetch_rancher_kubeconfigs(instance)

        for kubeconfig_yaml in kubeconfigs:
            try:
                cluster_config = yaml.safe_load(kubeconfig_yaml)
                if not isinstance(cluster_config, dict):
                    print(f"Warning: Skipping invalid kubeconfig from '{instance['name']}'.")
                    continue

                for section in ["clusters", "contexts", "users"]:
                    merge_section(main_config, cluster_config, section, force=True)

                total_merged += 1
            except yaml.YAMLError as e:
                print(f"Warning: Could not parse kubeconfig from '{instance['name']}': {e}")
                continue

    # Write the merged config once
    write_yaml(main_config_path, main_config)
    print(f"\nRancher sync complete. Merged {total_merged} kubeconfig(s) into {main_config_path}.")

def main():
    parser = argparse.ArgumentParser(description="Merge an additional kubeconfig into your main kubeconfig at $HOME/.kube/config, or sync from configured Rancher instances.")
    parser.add_argument("input_kubeconfig", nargs="?", help="Path to the kubeconfig file to merge.")
    parser.add_argument("--rancher", action="store_true", help="Fetch and merge kubeconfigs from all configured Rancher instances.")
    args = parser.parse_args()

    # Validate mutual exclusion
    if args.rancher and args.input_kubeconfig:
        parser.error("--rancher and a file path are mutually exclusive.")
    if not args.rancher and not args.input_kubeconfig:
        parser.error("Provide either a kubeconfig file path or --rancher.")

    if args.rancher:
        # Rancher sync flow
        rancher_sync()
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

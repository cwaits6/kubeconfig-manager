#!/usr/bin/env python3
import argparse
import os
import sys
import yaml
import questionary

def load_yaml(path):
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Error: File not found: {path}")
    except yaml.YAMLError as e:
        sys.exit(f"Error: Could not parse YAML in {path}: {e}")

def write_yaml(path, data):
    try:
        with open(path, 'w') as f:
            yaml.safe_dump(data, f, default_flow_style=False)
    except Exception as e:
        sys.exit(f"Error: Could not write YAML to {path}: {e}")

def merge_section(target, source, section_name):
    """
    Merge the entries of section_name from source into target.
    If the target does not have the section, it will be created.
    If an entry with the same name already exists in target, it will not be duplicated.
    """
    source_entries = source.get(section_name, [])
    if not isinstance(source_entries, list):
        sys.exit(f"Error: The {section_name} section in the input file is not a list.")
    
    # Create the section in target if missing
    if section_name not in target or target[section_name] is None:
        target[section_name] = []
    
    # Gather existing names in the target section to avoid duplicates.
    existing_names = {entry.get('name') for entry in target[section_name] if isinstance(entry, dict)}
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get('name')
        if entry_name in existing_names:
            print(f"Skipping duplicate {section_name[:-1]} entry with name '{entry_name}'.")
            continue
        target[section_name].append(entry)
        print(f"Added {section_name[:-1]} entry with name '{entry_name}'.")

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

def main():
    parser = argparse.ArgumentParser(description="Merge an additional kubeconfig into your main kubeconfig at $HOME/.kube/config.")
    parser.add_argument("input_kubeconfig", help="Path to the kubeconfig file to merge.")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input_kubeconfig)
    main_config_path = os.path.join(os.path.expanduser("~"), ".kube", "config")

    # Load the input kubeconfig
    print(f"Loading input kubeconfig from: {input_path}")
    input_config = load_yaml(input_path)

    # Load the main kubeconfig
    print(f"Loading main kubeconfig from: {main_config_path}")
    main_config = load_yaml(main_config_path)
    if not isinstance(main_config, dict):
        sys.exit("Error: The main kubeconfig is not a valid YAML mapping.")

    # Merge the sections: clusters, contexts, users
    for section in ["clusters", "contexts", "users"]:
        merge_section(main_config, input_config, section)

    # Write out the updated main kubeconfig
    write_yaml(main_config_path, main_config)
    print(f"Merged kubeconfig written to: {main_config_path}")

    # Prompt for changing the current context
    new_context = prompt_change_context(main_config)
    # Write updated current-context back to file if changed
    write_yaml(main_config_path, main_config)
    print(f"Final kubeconfig current-context: {new_context}")

if __name__ == "__main__":
    main()


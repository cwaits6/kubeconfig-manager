"""Persistent names and explicitly owned Rancher credentials.

State contains identifiers and ownership metadata, never token secrets.
"""
import os
from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
from uuid import uuid4

import questionary
import requests
import yaml


class ManagedError(Exception):
    """An error message safe to display without request or response contents."""

    def __init__(self, message, status=None):
        """Retain a safe HTTP status for recovery without exposing responses."""
        super().__init__(message)
        self.status = status


def config_directory():
    """Return the directory containing local configuration and ownership state."""
    return Path(os.path.expanduser('~')) / '.config/kubeconfig-manager'


@contextmanager
def lock():
    """Do not let two sync/cleanup processes race ownership state writes."""
    path = config_directory() / 'state.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagedError('Another sync or cleanup is running; retry after it completes.') from None
        yield


def atomic_yaml(path, data):
    """Atomically write private YAML to the destination, preserving symlinks."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            yaml.safe_dump(data, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state():
    """Load validated private ownership state or initialize a new installation."""
    path = config_directory() / 'state.yaml'
    if not path.exists():
        return {'installation_id': str(uuid4()), 'names': {}, 'tokens': []}
    if path.stat().st_mode & 0o077:
        raise ManagedError('state.yaml must have permissions 600.')
    try:
        state = yaml.safe_load(path.read_text())
        if (not isinstance(state, dict) or not isinstance(state.get('installation_id'), str)
                or not isinstance(state.get('names'), dict) or not isinstance(state.get('tokens'), list)):
            raise ValueError()
        for url, names in state['names'].items():
            if not isinstance(url, str) or not isinstance(names, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                for key, value in names.items()
            ):
                raise ValueError()
        for record in state['tokens']:
            if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key]
                for key in ('id', 'uuid', 'userId', 'clusterId', 'description', 'url')
            ) or record.get('status') not in ('pending', 'active', 'superseded'):
                raise ValueError()
        return state
    except (yaml.YAMLError, ValueError):
        raise ManagedError('Invalid state.yaml; contents suppressed. Restore its backup before cleanup.') from None


def save_state(state):
    """Persist ownership metadata and chosen names without token secrets."""
    atomic_yaml(config_directory() / 'state.yaml', state)


def resolve_token(instance):
    """Only read an explicitly configured variable when the CLI actually runs."""
    if ('token' in instance) == ('token_env' in instance):
        raise ManagedError('Configure exactly one of token or token_env for each instance.')
    if 'token_env' in instance:
        variable = instance['token_env']
        if not isinstance(variable, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable):
            raise ManagedError('token_env must be an environment variable name.')
        token = os.environ.get(variable)
        if not token:
            raise ManagedError(f'Export the environment variable {variable} before running sync.')
    else:
        token = instance['token']
    if not isinstance(token, str) or not token or token != token.strip() or token.lower().startswith('bearer '):
        raise ManagedError('API token must be non-empty, without whitespace or a Bearer prefix.')
    return token


def choose_name(instance, cluster_id, state, interactive=False, rename=False):
    """Choose and reserve a persistent name for one Rancher URL and cluster ID."""
    url = instance['url'].rstrip('/')
    saved = state['names'].setdefault(url, {})
    configured = instance.get('context_names', {}).get(cluster_id)
    default = configured or saved.get(cluster_id) or f"{instance['name']}-{cluster_id}"
    identity = (url, cluster_id)
    reserved = instance.get('_reserved_names', {})

    def validate(value):
        """Reject blank names and names reserved by another cluster."""
        if not value.strip():
            return 'Enter a context name.'
        if value.strip() in reserved and reserved[value.strip()] != identity:
            return 'That name belongs to another Rancher cluster. Choose a unique name.'
        return True

    if interactive and not configured and (rename or cluster_id not in saved):
        selected = questionary.text(
            f"Context name for '{cluster_id}' from '{instance['name']}' (saved for future syncs):",
            default=default,
            validate=validate,
        ).ask()
        if selected is None:
            raise ManagedError('Naming canceled; no kubeconfig was written.')
        default = selected.strip()
    if validate(default) is not True:
        raise ManagedError('Context naming collision. Choose unique names or run --rename-contexts.')
    saved[cluster_id] = default
    reserved[default] = identity
    instance.setdefault('context_names', {})[cluster_id] = default
    return default


def api(instance, method, path, **kwargs):
    """Call Rancher without redirects and expose only sanitized failure details."""
    token = kwargs.pop('token', instance['token'])
    try:
        response = requests.request(
            method, instance['url'].rstrip('/') + path,
            headers={'Authorization': f'Bearer {token}'},
            verify=instance.get('verify_ssl', True), timeout=15,
            allow_redirects=False, **kwargs,
        )
        response.raise_for_status()
        if response.is_redirect:
            raise ManagedError('Rancher redirected the request; use its canonical HTTPS URL.')
        return response.json() if method != 'DELETE' else None
    except requests.RequestException as error:
        status = error.response.status_code if error.response is not None else None
        raise ManagedError(f"Rancher {method} failed: HTTP {status}" if status else
                           f'Rancher {method} failed: {type(error).__name__}', status=status) from None
    except ValueError:
        raise ManagedError('Rancher returned invalid JSON; contents suppressed.') from None


def owned(record, remote):
    """Require every recorded ownership field to match the remote token."""
    fields = ('id', 'uuid', 'userId', 'clusterId', 'description')
    return isinstance(remote, dict) and all(
        isinstance(record.get(field), str) and bool(record[field])
        and remote.get(field) == record[field] for field in fields
    )


def create_kubeconfig(instance, cluster_id, state, ttl_hours):
    """Create an explicitly owned token; avoid generateKubeconfig's shared token."""
    description = f"kubeconfig-manager:{state['installation_id']}:{cluster_id}:{uuid4()}"
    # Persist the installation identity before creating any remote credential.
    save_state(state)
    remote = api(instance, 'POST', '/v3/token', json={
        'type': 'token', 'description': description, 'clusterId': cluster_id,
        'ttl': int(ttl_hours * 60 * 60 * 1000),
    })
    if not isinstance(remote, dict):
        raise ManagedError('Token creation returned invalid metadata; review Rancher before retrying.')
    record = {key: remote.get(key) for key in ('id', 'uuid', 'userId', 'clusterId', 'description')}
    secret = remote.get('token')
    if (not owned(record, remote) or record['description'] != description
            or record['clusterId'] != cluster_id or not isinstance(secret, str)
            or secret.split(':', 1)[0] != record['id'] or ':' not in secret):
        raise ManagedError('Created token could not be verified; review its kubeconfig-manager description in Rancher. No automatic deletion attempted.')
    record.update({'url': instance['url'].rstrip('/'), 'status': 'pending'})
    state['tokens'].append(record)
    save_state(state)
    path = '/k8s/clusters/' + quote(cluster_id, safe='')
    api(instance, 'GET', path + '/version', token=secret)
    cluster = {'server': instance['url'].rstrip('/') + path}
    if not instance.get('verify_ssl', True):
        cluster['insecure-skip-tls-verify'] = True
    return {
        'apiVersion': 'v1', 'kind': 'Config',
        'clusters': [{'name': cluster_id, 'cluster': cluster}],
        'users': [{'name': cluster_id, 'user': {'token': secret}}],
        'contexts': [{'name': cluster_id, 'context': {'cluster': cluster_id, 'user': cluster_id}}],
        'current-context': cluster_id,
    }


def referenced_token_ids(config):
    """Collect IDs of static tokens referenced anywhere in the kubeconfig."""
    return {
        token.split(':', 1)[0]
        for entry in config.get('users', [])
        if isinstance((token := entry.get('user', {}).get('token')), str) and ':' in token
    }


def commit_tokens(state, config):
    """Mark tokens active only after their kubeconfig has been published."""
    active = referenced_token_ids(config)
    for record in state['tokens']:
        record['status'] = 'active' if record['id'] in active else 'superseded'
    save_state(state)


def cleanup(instances, state, config, delete=False, config_loader=None):
    """Preview first, reverify exact ownership immediately before explicit deletion."""
    by_url = {instance['url'].rstrip('/'): instance for instance in instances}
    active = referenced_token_ids(config)
    candidates = []
    failures = 0
    for record in list(state['tokens']):
        instance = by_url.get(record['url'])
        if not instance or record['id'] in active or record.get('status') == 'active':
            continue
        if instance['token'].split(':', 1)[0] == record['id']:
            continue  # Never revoke the credential authenticating this operation.
        expected_prefix = f"kubeconfig-manager:{state['installation_id']}:{record['clusterId']}:"
        if not record['description'].startswith(expected_prefix):
            continue
        try:
            remote = api(instance, 'GET', '/v3/token/' + quote(record['id'], safe=''))
            if not owned(record, remote) or remote.get('current'):
                print(f"Skipping unverified/current token {record['id']}.")
                continue
            candidates.append((instance, record))
            print(f"Obsolete: {instance['name']} / {record['clusterId']} / {record['id']} / {record['description']}")
        except ManagedError as error:
            if error.status == 404:
                state['tokens'].remove(record)
                save_state(state)
                print(f"Removed already-missing token {record['id']} from local state.")
                continue
            print(str(error))
            failures += 1
    if not candidates:
        print('No verified obsolete managed tokens found. Existing untracked Rancher tokens are excluded.')
        return 1 if failures else 0
    if not delete:
        print('Preview only. Run --rancher --cleanup-tokens --delete to review and confirm revocation.')
        return 1 if failures else 0
    if not questionary.confirm(
        f'Revoke these {len(candidates)} tokens? Copies of old kubeconfigs using them will stop working.',
        default=False,
    ).ask():
        print('No tokens revoked.')
        return 0
    for instance, record in candidates:
        try:
            current_config = config_loader() if config_loader else config
            if not isinstance(current_config, dict) or record['id'] in referenced_token_ids(current_config):
                raise ManagedError('Token is now referenced by the main kubeconfig; refusing revocation.')
            remote = api(instance, 'GET', '/v3/token/' + quote(record['id'], safe=''))
            if not owned(record, remote) or remote.get('current'):
                raise ManagedError('Ownership changed; refusing revocation.')
            api(instance, 'DELETE', '/v3/token/' + quote(record['id'], safe=''))
            state['tokens'].remove(record)
            save_state(state)
            print(f"Revoked {record['id']}.")
        except ManagedError as error:
            if error.status == 404:
                state['tokens'].remove(record)
                save_state(state)
                print(f"Removed already-missing token {record['id']} from local state.")
                continue
            print(str(error))
            failures += 1
    return 1 if failures else 0

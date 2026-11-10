import contextlib
from copy import deepcopy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import yaml

import newKube as app


def kubeconfig(token='old'):
    return {
        'clusters': [{'name': 'local', 'cluster': {'server': 'https://example.test'}}],
        'users': [{'name': 'local', 'user': {'token': token}}],
        'contexts': [{'name': 'local', 'context': {'cluster': 'local', 'user': 'local'}}],
        'current-context': 'local',
    }


class RancherTests(unittest.TestCase):
    def setUp(self):
        # Never inspect the developer's real ownership state or credentials.
        state = patch.object(app.managed, 'load_state', return_value={'installation_id': 'test', 'names': {}, 'tokens': []})
        state.start()
        self.addCleanup(state.stop)
        save = patch.object(app.managed, 'save_state')
        save.start()
        self.addCleanup(save.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_two_local_clusters_do_not_share_any_entries(self):
        merged = deepcopy(app.EMPTY_KUBECONFIG)
        for name in ('prod', 'stage'):
            config = app.name_rancher_kubeconfig(kubeconfig(name), {'name': name}, 'local')
            for section in ('clusters', 'users', 'contexts'):
                app.merge_section(merged, config, section, force=True)
        for section in ('clusters', 'users', 'contexts'):
            self.assertEqual(len(merged[section]), 2)
        for context in merged['contexts']:
            prefix = context['name'].split('-')[0]
            self.assertEqual(context['context']['user'], f'{prefix}-local::local')

    def test_custom_name_survives_credential_and_display_name_changes(self):
        instance = {'name': 'prod', 'context_names': {'local': 'my-control-plane'}}
        first = app.name_rancher_kubeconfig(kubeconfig(), instance, 'local')
        second_source = kubeconfig('rotated')
        second_source['contexts'][0]['name'] = 'renamed-in-rancher'
        second_source['current-context'] = 'renamed-in-rancher'
        second = app.name_rancher_kubeconfig(second_source, instance, 'local')
        for section in ('clusters', 'users', 'contexts'):
            app.merge_section(first, second, section, force=True)
            self.assertEqual(len(first[section]), 1)
        self.assertEqual(first['contexts'][0]['name'], 'my-control-plane')
        self.assertEqual(first['users'][0]['user']['token'], 'rotated')

    def test_multiple_contexts_and_references(self):
        source = kubeconfig()
        source['contexts'].append({'name': 'direct', 'context': {'cluster': 'local', 'user': 'local'}})
        original = deepcopy(source)
        result = app.name_rancher_kubeconfig(source, {'name': 'prod'}, 'local')
        self.assertEqual(result['current-context'], 'prod-local')
        self.assertEqual(result['contexts'][1]['name'], 'prod-local::direct')
        self.assertEqual(result['contexts'][1]['context']['cluster'], 'prod-local::local')
        self.assertEqual(source, original)

    @patch.object(app.requests, 'get')
    def test_401_is_actionable_and_never_prints_exception_secrets(self, get):
        response = requests.Response()
        response.status_code = 401
        get.return_value.raise_for_status.side_effect = requests.HTTPError('PRIVATE-CREDENTIAL', response=response)
        configs, failures = app.fetch_rancher_kubeconfigs({'name': 'prod', 'url': 'https://example.test/', 'token': 'PRIVATE-CREDENTIAL'})
        self.assertEqual((configs, failures), ([], 1))
        self.assertIn('HTTP 401', self.output.getvalue())
        self.assertIn('rancher.yaml', self.output.getvalue())
        self.assertNotIn('PRIVATE-CREDENTIAL', self.output.getvalue())
        self.assertEqual(get.call_args.args[0], 'https://example.test/v3/clusters')

    @patch.object(app.requests, 'post')
    @patch.object(app.requests, 'get')
    def test_fetch_maps_using_cluster_id(self, get, post):
        get.return_value.json.return_value = {'data': [{'id': 'local', 'name': 'display-name'}]}
        post.return_value.json.return_value = {'config': yaml.safe_dump(kubeconfig())}
        configs, failures = app.fetch_rancher_kubeconfigs({'name': 'prod', 'url': 'https://example.test', 'token': 'dummy', 'context_names': {'local': 'chosen'}})
        self.assertEqual(failures, 0)
        self.assertEqual(configs[0]['contexts'][0]['name'], 'chosen')

    @patch.object(app, 'write_yaml')
    @patch.object(app, 'load_or_create_kubeconfig')
    @patch.object(app, 'fetch_rancher_kubeconfigs', return_value=([], 1))
    @patch.object(app, 'load_rancher_config', return_value=[{'name': 'prod'}])
    def test_failed_sync_does_not_touch_kubeconfig(self, load, fetch, main, write):
        self.assertEqual(app.rancher_sync(), 1)
        main.assert_not_called()
        write.assert_not_called()

    @patch.object(app, 'write_yaml')
    @patch.object(app, 'load_or_create_kubeconfig')
    @patch.object(app, 'fetch_rancher_kubeconfigs')
    @patch.object(app, 'load_rancher_config', return_value=[{'name': 'prod'}, {'name': 'stage'}])
    def test_duplicate_custom_names_abort_before_writing(self, load, fetch, main, write):
        config = app.name_rancher_kubeconfig(kubeconfig(), {'name': 'prod', 'context_names': {'local': 'same'}}, 'local')
        fetch.return_value = ([config], 0)
        with self.assertRaises(SystemExit):
            app.rancher_sync()
        main.assert_not_called()
        write.assert_not_called()

    @patch.object(app, 'fetch_rancher_kubeconfigs')
    @patch.object(app, 'load_rancher_config', return_value=[{'name': 'prod'}, {'name': 'stage'}])
    def test_partial_sync_preserves_active_context_and_other_entries(self, load, fetch):
        fetched = app.name_rancher_kubeconfig(kubeconfig('rotated'), {'name': 'prod'}, 'local')
        fetch.side_effect = [([fetched], 0), ([], 1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.kube/config'
            path.parent.mkdir()
            path.write_text(yaml.safe_dump(kubeconfig()))
            with patch.object(app.os.path, 'expanduser', return_value=directory):
                self.assertEqual(app.rancher_sync(), 1)
            result = yaml.safe_load(path.read_text())
        self.assertEqual(result['current-context'], 'local')
        self.assertEqual({entry['name'] for entry in result['contexts']}, {'local', 'prod-local'})
        self.assertEqual(result['users'][1]['user']['token'], 'rotated')

    def test_invalid_alias_config_rejected_without_reading_user_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.config/kubeconfig-manager/rancher.yaml'
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump({'instances': [{'name': 'prod', 'url': 'https://example.test', 'token': 'dummy', 'context_names': {'local': ''}}]}))
            path.chmod(0o600)
            with patch.object(app.os.path, 'expanduser', return_value=directory):
                with self.assertRaises(SystemExit) as error:
                    app.load_rancher_config()
            self.assertIn('context_names', str(error.exception))


if __name__ == '__main__':
    unittest.main()

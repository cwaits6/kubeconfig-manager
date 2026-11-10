from copy import deepcopy
import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import requests
import yaml

import rancher_managed as managed
import newKube as app


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.instance = {'name': 'prod', 'url': 'https://example.test', 'token': 'api:synthetic'}
        self.state = {'installation_id': 'installation', 'names': {}, 'tokens': []}
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def record(self, **changes):
        record = {'id': 'token-old', 'uuid': 'server-uuid', 'userId': 'user-1',
                  'clusterId': 'local', 'description': 'kubeconfig-manager:installation:local:nonce',
                  'url': 'https://example.test', 'status': 'superseded'}
        record.update(changes)
        return record

    def test_env_reference_reads_only_the_named_mock_variable(self):
        with patch.object(managed.os, 'environ', {'SYNTHETIC_TOKEN': 'access:synthetic'}):
            self.assertEqual(managed.resolve_token({'token_env': 'SYNTHETIC_TOKEN'}), 'access:synthetic')

    def test_missing_env_and_ambiguous_sources_fail_without_values(self):
        with patch.object(managed.os, 'environ', {}):
            for instance in ({'token_env': 'MISSING'}, {'token_env': 'MISSING', 'token': 'PRIVATE'}):
                with self.assertRaises(managed.ManagedError) as error:
                    managed.resolve_token(instance)
                self.assertNotIn('PRIVATE', str(error.exception))

    @patch.object(managed.questionary, 'text')
    def test_name_prompt_once_and_persist_across_instance_display_name_change(self, text):
        text.return_value.ask.return_value = 'my-control-plane'
        self.assertEqual(managed.choose_name(self.instance, 'local', self.state, True), 'my-control-plane')
        self.instance = {**self.instance, 'name': 'new-instance-display-name', 'context_names': {}}
        self.assertEqual(managed.choose_name(self.instance, 'local', self.state, True), 'my-control-plane')
        text.assert_called_once()

    @patch.object(managed.questionary, 'text')
    def test_rename_option_prompts_again(self, text):
        self.state['names'] = {'https://example.test': {'local': 'before'}}
        text.return_value.ask.return_value = 'after'
        managed.choose_name(self.instance, 'local', self.state, True, True)
        self.assertEqual(self.state['names']['https://example.test']['local'], 'after')

    def test_noninteractive_names_are_stable(self):
        self.assertEqual(managed.choose_name(self.instance, 'local', self.state), 'prod-local')
        self.assertEqual(self.state['names']['https://example.test']['local'], 'prod-local')

    @patch.object(managed.questionary, 'text')
    def test_conflicting_name_is_rejected_by_prompt_validator(self, text):
        self.instance['_reserved_names'] = {'taken': ('https://other.test', 'local')}
        text.return_value.ask.return_value = 'unique'
        managed.choose_name(self.instance, 'local', self.state, True)
        validate = text.call_args.kwargs['validate']
        self.assertIsNot(validate('taken'), True)
        self.assertIs(validate('unique'), True)

    @patch.object(managed, 'save_state')
    @patch.object(managed, 'api')
    def test_create_records_ownership_without_secret_and_verifies_new_credential(self, api, save):
        def response(instance, method, path, **kwargs):
            if method == 'POST':
                return {**self.record(), 'description': kwargs['json']['description'],
                        'token': 'token-old:PRIVATE'}
            self.assertEqual(path, '/k8s/clusters/local/version')
            self.assertEqual(kwargs['token'], 'token-old:PRIVATE')
            return {'gitVersion': 'v1.30.0'}
        api.side_effect = response
        config = managed.create_kubeconfig(self.instance, 'local', self.state, 24)
        self.assertEqual(config['users'][0]['user']['token'], 'token-old:PRIVATE')
        self.assertNotIn('PRIVATE', yaml.safe_dump(self.state))
        self.assertEqual(self.state['tokens'][0]['status'], 'pending')
        self.assertEqual(api.call_args_list[0].kwargs['json']['ttl'], 86400000)
        self.assertGreaterEqual(save.call_count, 2)

    @patch.object(managed, 'save_state')
    def test_commit_marks_only_referenced_tokens_active(self, save):
        self.state['tokens'] = [self.record(), self.record(id='token-new', status='pending')]
        managed.commit_tokens(self.state, {'users': [{'user': {'token': 'token-new:synthetic'}}]})
        self.assertEqual([token['status'] for token in self.state['tokens']], ['superseded', 'active'])

    @patch.object(managed, 'api')
    def test_cleanup_preview_never_deletes(self, api):
        record = self.record()
        self.state['tokens'] = [record]
        api.return_value = record
        self.assertEqual(managed.cleanup([self.instance], self.state, {}), 0)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET'])
        self.assertIn('Preview only', self.output.getvalue())

    @patch.object(managed, 'api')
    def test_cleanup_ignores_active_and_authentication_tokens(self, api):
        self.state['tokens'] = [self.record(), self.record(id='api'), self.record(id='active', status='active')]
        managed.cleanup([self.instance], self.state, {'users': [{'user': {'token': 'token-old:synthetic'}}]})
        api.assert_not_called()

    @patch.object(managed, 'api')
    def test_description_match_alone_never_authorizes_deletion(self, api):
        self.state['tokens'] = [self.record()]
        api.return_value = self.record(uuid='different-server-uuid')
        managed.cleanup([self.instance], self.state, {}, delete=True)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET'])

    @patch.object(managed.questionary, 'confirm')
    @patch.object(managed, 'api')
    def test_canceled_cleanup_never_deletes(self, api, confirm):
        self.state['tokens'] = [self.record()]
        api.return_value = self.record()
        confirm.return_value.ask.return_value = False
        managed.cleanup([self.instance], self.state, {}, delete=True)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET'])

    @patch.object(managed, 'save_state')
    @patch.object(managed.questionary, 'confirm')
    @patch.object(managed, 'api')
    def test_confirmed_cleanup_rechecks_and_deletes_exact_record(self, api, confirm, save):
        self.state['tokens'] = [self.record()]
        api.return_value = self.record()
        confirm.return_value.ask.return_value = True
        self.assertEqual(managed.cleanup([self.instance], self.state, {}, delete=True), 0)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET', 'GET', 'DELETE'])
        self.assertEqual(api.call_args.args[2], '/v3/token/token-old')
        self.assertEqual(self.state['tokens'], [])

    @patch.object(managed.questionary, 'confirm')
    @patch.object(managed, 'api')
    def test_cleanup_rechecks_main_config_before_deletion(self, api, confirm):
        self.state['tokens'] = [self.record()]
        api.return_value = self.record()
        confirm.return_value.ask.return_value = True
        current = lambda: {'users': [{'user': {'token': 'token-old:synthetic'}}]}
        self.assertEqual(managed.cleanup([self.instance], self.state, {}, delete=True, config_loader=current), 1)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET'])

    @patch.object(managed.questionary, 'confirm')
    @patch.object(managed, 'api')
    def test_metadata_changed_after_preview_refuses_deletion(self, api, confirm):
        self.state['tokens'] = [self.record()]
        api.side_effect = [self.record(), self.record(userId='someone-else')]
        confirm.return_value.ask.return_value = True
        self.assertEqual(managed.cleanup([self.instance], self.state, {}, delete=True), 1)
        self.assertEqual([call.args[1] for call in api.call_args_list], ['GET', 'GET'])

    @patch.object(managed.requests, 'request')
    def test_network_error_suppresses_secret_exception_text(self, request):
        request.side_effect = requests.ConnectionError('PRIVATE')
        with self.assertRaises(managed.ManagedError) as error:
            managed.api(self.instance, 'GET', '/v3/token/test')
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_state_roundtrip_is_private_and_names_are_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(managed, 'config_directory', return_value=Path(directory)):
                managed.choose_name(self.instance, 'local', self.state)
                managed.save_state(self.state)
                self.assertEqual(managed.load_state(), self.state)
                self.assertEqual((Path(directory) / 'state.yaml').stat().st_mode & 0o777, 0o600)

    def test_two_syncs_then_cleanup_with_synthetic_server_and_files(self):
        """Exercise env config, naming, rotation, persisted ownership and cleanup."""
        remote_tokens = {}
        deleted = []

        def server(instance, method, path, **kwargs):
            if method == 'POST':
                number = len(remote_tokens) + 1
                token_id = f'token-{number}'
                result = {'id': token_id, 'uuid': f'uuid-{number}', 'userId': 'user-test',
                          'clusterId': kwargs['json']['clusterId'],
                          'description': kwargs['json']['description'], 'token': f'{token_id}:synthetic'}
                remote_tokens[token_id] = result
                return result
            if path.endswith('/version'):
                return {'gitVersion': 'v1.30.0'}
            token_id = path.rsplit('/', 1)[-1]
            if method == 'DELETE':
                deleted.append(token_id)
                return None
            return remote_tokens[token_id]

        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / '.config/kubeconfig-manager'
            managed.atomic_yaml(config_dir / 'rancher.yaml', {'instances': [{
                'name': 'prod', 'url': 'https://example.test', 'token_env': 'SYNTHETIC_TOKEN'}]})
            with (patch.object(managed.os.path, 'expanduser', return_value=directory),
                  patch.object(managed.os, 'environ', {'SYNTHETIC_TOKEN': 'api:synthetic'}),
                  patch.object(app.sys.stdin, 'isatty', return_value=False),
                  patch.object(app.requests, 'get') as clusters,
                  patch.object(managed, 'api', side_effect=server)):
                clusters.return_value.json.return_value = {'data': [{'id': 'local'}]}
                self.assertEqual(app.rancher_sync(managed_tokens=True), 0)
                self.assertEqual(app.rancher_sync(managed_tokens=True), 0)
                state = managed.load_state()
                self.assertEqual([item['status'] for item in state['tokens']], ['superseded', 'active'])
                self.assertNotIn('synthetic', (config_dir / 'state.yaml').read_text())
                main_config = yaml.safe_load((Path(directory) / '.kube/config').read_text())
                self.assertEqual(len(main_config['users']), 1)
                self.assertEqual(main_config['contexts'][0]['name'], 'prod-local')
                self.assertEqual(app.rancher_sync(cleanup_tokens=True), 0)
                self.assertEqual(deleted, [])
                with (patch.object(app.sys.stdin, 'isatty', return_value=True),
                      patch.object(managed.questionary, 'confirm') as confirm):
                    confirm.return_value.ask.return_value = True
                    self.assertEqual(app.rancher_sync(cleanup_tokens=True, delete=True), 0)
                self.assertEqual(deleted, ['token-1'])
                self.assertEqual([record['id'] for record in managed.load_state()['tokens']], ['token-2'])


if __name__ == '__main__':
    unittest.main()

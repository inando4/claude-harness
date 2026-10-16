import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
import test_harness as existing
import test_setup

h=existing.h


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.HarnessTests();self.fixture.setUp();self.root=self.fixture.root;self.s=self.fixture.s
        self.probe=h.preflight_module()

    def tearDown(self):self.fixture.tearDown()

    def test_database_probe_failure_cleans_only_its_clone(self):
        backup=self.root/'snapshot.dump';backup.write_bytes(b'PGDMPsynthetic')
        def create(s):s['containerId']='temporary-container'
        def adapter(method,*args):
            if method=='service_names':return ['web']
            if method=='migrate':raise h.Blocked('refused')
        repos=Mock(sources=Mock(return_value={'main':{'path':str(self.root)}}),supplemental=Mock(return_value=[]))
        with patch.object(self.probe,'docker_identity',return_value={'context':'test'}), \
             patch.object(h,'repositories_module',return_value=repos),patch.object(h,'run',return_value=''), \
             patch.object(h,'available_port',return_value=8000),patch.object(h,'create_clone',side_effect=create), \
             patch.object(h,'adapter_call',side_effect=adapter),patch.object(h,'has_database',return_value=True), \
             patch.object(h,'database_config',return_value={'schema':'public'}),patch.object(h,'remove_clone') as remove:
            with self.assertRaises(h.Blocked):self.probe.database(h.CoreContext(),{'path':str(backup),'sha256':h.sha(backup)})
        state=remove.call_args.args[0]
        self.assertNotEqual(state['container'],self.s['container'])
        self.assertEqual(state['stage'],'cancelled')
        self.assertFalse((h.jobdir(state['id'])/'repositories').exists())

    def test_migration_failure_does_not_spend_product_cycle(self):
        def adapter(method,*args):
            if method=='checks':return {'tests':{'tests':1}}
            if method=='migrate':raise h.Blocked('network')
        with patch.object(h,'db_info'),patch.object(h,'job_fingerprint',return_value='same'), \
             patch.object(h,'has_database',return_value=True),patch.object(h,'migrations_version',return_value='m'), \
             patch.object(h,'adapter_call',side_effect=adapter):
            with self.assertRaises(h.Blocked):h.verify(self.s)
        self.assertEqual(self.s['cycles'],0);self.assertEqual(self.s['stage'],'blocked')
        self.assertTrue(self.s['migrationFailed']);self.assertEqual(self.s['blockedPhase'],'migration')

    def repair_fixture(self):
        cfg=json.loads((existing.MODULE.parent/'templates/node-postgres.json').read_text())
        cfg.update(needsConfiguration=False,migrate={'command':['docker','run','--rm','--network','host','flyway/flyway:12.4.0','migrate'],
                   'env':{'FLYWAY_URL':'jdbc:postgresql://127.0.0.1:${DB_PORT}/${DB_NAME}'}})
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        self.s.update(stage='blocked',migrationFailed=True,migrationAttempt='m',cycles=1,
                      migrationBefore=hashlib.sha256(b'history').hexdigest(),snapshotSha256='snapshot',
                      configVersion=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest())
        h.save(self.s,'fixture')
        (h.jobdir(self.s['id'])/'migrate.log').write_text('SQL State  : 08001\nConnection refused\n127.0.0.1:5555/test_db')
        h.atomic(h.STORE/'setup.json',{'snapshot':{'sha256':'snapshot'},'status':'ready'})
        return cfg

    def repair(self,history='history',probe_error=False):
        with patch.object(h,'run',return_value='[]'),patch.object(h,'db_info'), \
             patch.object(h,'migrations_version',return_value='m'),patch.object(h,'migration_history',return_value=history), \
             patch.object(h,'validate_config'),patch.object(h,'setup_module',return_value=test_setup.setup), \
             patch.object(test_setup.setup,'signature',return_value='manifest'), \
             patch.object(self.probe,'database',side_effect=h.Blocked('failed probe') if probe_error else None,
                          return_value={'status':'passed'}) as probe:
            result=self.probe.repair_flyway_network(h.CoreContext(),self.s)
        return result

    def test_network_repair_is_audited_and_refunds_only_infra_cycle(self):
        self.repair_fixture();self.repair()
        self.assertEqual(h.config()['migrate']['command'][4],'container:${DB_CONTAINER}')
        self.assertEqual(self.s['cycles'],0);self.assertFalse(self.s['migrationFailed'])
        self.assertEqual(self.s['stage'],'implementing')
        self.assertEqual(self.s['containerId'],'id-1')
        self.assertTrue(list(h.jobdir(self.s['id']).glob('network-repair-*/state-before.json')))

    def test_repair_rejects_changed_history(self):
        cfg=self.repair_fixture()
        with self.assertRaises(h.Blocked):self.repair(history='changed')
        self.assertEqual(h.config(),cfg);self.assertTrue(self.s['migrationFailed'])

    def test_failed_probe_restores_original_configuration_and_block(self):
        cfg=self.repair_fixture()
        with self.assertRaises(h.Blocked):self.repair(probe_error=True)
        self.assertEqual(h.config(),cfg);self.assertTrue(self.s['migrationFailed'])
        self.assertEqual(self.s['cycles'],1)

    def test_repair_rejects_sql_failure_instead_of_connection_refusal(self):
        self.repair_fixture();(h.jobdir(self.s['id'])/'migrate.log').write_text('SQL State  : 42000 syntax error')
        with self.assertRaises(h.Blocked):self.repair()
        self.assertTrue(self.s['migrationFailed'])


if __name__=='__main__':unittest.main()

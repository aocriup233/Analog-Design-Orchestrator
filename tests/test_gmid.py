import csv
import itertools
import math
from pathlib import Path
import tempfile
import unittest
try:
    from analog_agent.gmid import LookupTable, REQUIRED, SizingObjective
except ModuleNotFoundError as exc:
    if exc.name not in ('numpy', 'scipy'):
        raise
    raise unittest.SkipTest('Install analog-agent[gmid] for lookup tests') from exc


def synthetic(path, curve=None):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=REQUIRED)
        writer.writeheader()
        for length, vgs in itertools.product((1e-6, 2e-6), (0.2, 0.4, 0.6)):
            current = vgs*1e-4
            ratio = curve[vgs] if curve else 30-25*vgs
            writer.writerow(dict(model='synthetic', corner='tt', temp_c=27,
                l=length, vds=1, vsb=0, vgs=vgs, id=current,
                gm=current*ratio, gds=current/10, cgg=1e-12, w_ref=1e-6))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'synthetic.csv'
        synthetic(self.path)
        self.table = LookupTable(self.path)
        self.bias = dict(model='synthetic', corner='tt', temp_c=27, l=1.5e-6, vds=1, vsb=0)

    def test_forward_inverse_and_width(self):
        row = self.table.query(vgs=0.3, **self.bias)
        inverse = self.table.at_gm_id(row['gm_id'], **self.bias)
        self.assertAlmostEqual(inverse['vgs'], 0.3)
        self.assertAlmostEqual(inverse['gm_id'], row['gm_id'])
        size = self.table.size(gm_id=row['gm_id'], current=60e-6, **self.bias)
        self.assertAlmostEqual(size['width'], 2e-6)
        self.assertAlmostEqual(row['ft'], row['gm']/(2*math.pi*row['cgg']))

    def test_out_of_domain(self):
        for change in ({'vds': 2}, {'temp_c': 125}, {'l': 3e-6}, {'vgs': float('nan')}):
            with self.assertRaises(ValueError):
                self.table.query(**{**self.bias, 'vgs': 0.3, **change})
        with self.assertRaises(ValueError):
            self.table.at_gm_id(100, **self.bias)

    def test_ambiguous_inverse(self):
        synthetic(self.path, {0.2: 10, 0.4: 20, 0.6: 10})
        table = LookupTable(self.path)
        with self.assertRaises(ValueError):
            table.at_gm_id(15, **self.bias)
        self.assertLess(table.at_gm_id(15, branch=0, **self.bias)['vgs'],
                        table.at_gm_id(15, branch=1, **self.bias)['vgs'])

    def test_duplicate_and_missing(self):
        lines = self.path.read_text().splitlines()
        for bad in (lines + [lines[1]], lines[:-1]):
            self.path.write_text('\n'.join(bad))
            with self.assertRaises(ValueError):
                LookupTable(self.path)

    def test_optimizer(self):
        objective = SizingObjective(self.table, self.bias, lambda r: r['width'])
        self.assertAlmostEqual(objective([20, 1.5e-6, 40e-6]), 1e-6)
        self.assertEqual(objective([200, 1.5e-6, 40e-6]), 1e12)
        from scipy.optimize import minimize
        result = minimize(objective, [20, 1.5e-6, 40e-6], method='Nelder-Mead',
                          options={'maxiter': 10})
        self.assertTrue(math.isfinite(result.fun))

    def test_import_signed_pmos(self):
        from analog_agent.gmid_import import convert
        import json
        for name, values in {'id_w': [-20, -40], 'gm_id': [20, 10],
                             'gm_gds': [50, 50], 'ft': [-1e8, -2e8]}.items():
            with (self.path.parent/(name+'.csv')).open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['vgs', name])
                writer.writerows(zip([-0.2, -0.4], values))
        constants = dict(model='synthetic_p', corner='tt', temp_c=27,
                         l=1e-6, vds=1, vsb=0, w_ref=1e-6)
        sources = {name: dict(path=name+'.csv', x_column=0, y_column=1,
                             magnitude=True, x_magnitude=True)
                   for name in ('id_w', 'gm_id', 'gm_gds', 'ft')}
        manifest = self.path.parent/'mapping.json'
        manifest.write_text(json.dumps({'series': [dict(constants=constants, sources=sources)]}))
        destination = self.path.parent/'normalized.csv'
        self.assertEqual(convert(manifest, destination), 2)
        row = LookupTable(destination).query(model='synthetic_p', corner='tt', temp_c=27,
                                            l=1e-6, vds=1, vsb=0, vgs=0.2)
        self.assertAlmostEqual(row['gm_id'], 20)
        self.assertAlmostEqual(row['id_w'], 20)





class AdoIntegrationTests(unittest.TestCase):
    def test_campaign_proposal_private_table(self):
        from analog_agent.campaign import create_campaign, propose
        from analog_agent.core import write_json, read_json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'template.scs').write_text('// synthetic software test only')
            (root/'private').mkdir()
            synthetic(root/'private'/'lut.csv')
            cfg = {'template': 'template.scs', 'parameters': {
                'W': {'type': 'number', 'initial': '1u', 'min': '100n', 'max': '10u'},
                'L': {'type': 'number', 'initial': '1u', 'min': '1u', 'max': '2u'}},
                'metrics': {'gain': {'kind': 'scalar', 'key': 'gain'}}, 'rules': {'gain': {'min': 1}},
                'campaign': {'proposer': 'analog_agent.proposers.gmid:propose',
                             'max_parallel': 1, 'max_points_per_cycle': 2, 'max_total_points': 4}}
            config_path = root/'project.json'
            write_json(config_path, cfg)
            override = root/'private'/'global'/'config'/'overrides.json'
            sizing = dict(model='synthetic', corner='tt', temp_c=27, l=1.5e-6,
                          vds=1, vsb=0, gm_id=20, current=40e-6)
            write_json(override, {'campaign': {'gmid': {'table': 'private/lut.csv',
                'requests': [{'sizing': sizing}], 'outputs': {'width': 'W', 'l': 'L'}}}})
            campaign = create_campaign(config_path)
            # Actual campaign entrypoint, plugin loading, private overrides and validation.
            response = propose(campaign)
            result = read_json(Path(response['proposal']))
            self.assertTrue(response['review_required'])
            self.assertAlmostEqual(float(result['points'][0]['parameters']['W']), 1e-6)
            self.assertFalse(result['meta']['verified_electrical_results'])
            self.assertNotIn('lut.csv', Path(response['proposal']).read_text())
            cfg['campaign'].update(proposer='analog_agent.proposers.mobo:propose',
                metric_directions={'gain': 'max', 'power': 'min'},
                bo={'gmid_seeds': True, 'batch_size': 1, 'ref_point': [0, 1]})
            cfg['metrics']['power'] = {'kind': 'scalar', 'key': 'power'}
            write_json(config_path, cfg)
            mobo_campaign = create_campaign(config_path)
            mobo_response = propose(mobo_campaign)
            mobo_result = read_json(Path(mobo_response['proposal']))
            self.assertEqual(mobo_result['meta']['mode'], 'cold_start')
            self.assertAlmostEqual(float(mobo_result['points'][0]['parameters']['W']), 1e-6)



class AdditionalValidationTests(unittest.TestCase):
    def test_proposer_rejects_nonprivate_tables(self):
        from unittest.mock import patch
        from analog_agent.proposers.gmid import propose
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'_project_root': tmp, 'campaign': {'gmid': {'table': 'public.csv'}}}
            with patch('analog_agent.proposers.gmid.load_config', return_value=cfg):
                with self.assertRaisesRegex(ValueError, 'private'):
                    propose({'project_config': 'unused.json'})

    def test_plateau_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'table.csv'
            synthetic(path, {0.2: 10, 0.4: 10, 0.6: 10})
            bias = dict(model='synthetic', corner='tt', temp_c=27, l=1e-6, vds=1, vsb=0)
            table = LookupTable(path)
            with self.assertRaisesRegex(ValueError, 'plateau'):
                table.at_gm_id(10, **bias)
            with self.assertRaises(ValueError):
                table.size(gm_id=10, current=-1, **bias)
            path.write_text(path.read_text().replace('27,', 'nan,', 1))
            with self.assertRaises(ValueError):
                LookupTable(path)




class MissingContextTests(unittest.TestCase):
    def test_unknown_conditions_are_omitted_and_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'table.csv'
            synthetic(path)
            with path.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                for name in ('temp_c', 'vds', 'vsb'):
                    row[name] = ''
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=REQUIRED)
                writer.writeheader()
                writer.writerows(rows)
            table = LookupTable(path)
            bias = dict(model='synthetic', corner='tt', l=1.5e-6)
            row = table.query(**bias, vgs=0.3)
            self.assertNotIn('vds', row)
            self.assertNotIn('temp_c', row)
            self.assertEqual(set(row['missing_context']), {'temp_c', 'vds', 'vsb'})
            inverse = table.at_gm_id(row['gm_id'], **bias)
            self.assertAlmostEqual(inverse['vgs'], 0.3)
            with self.assertRaisesRegex(ValueError, 'not characterized'):
                table.query(**bias, vgs=0.3, vds=1)

if __name__ == '__main__':
    unittest.main()

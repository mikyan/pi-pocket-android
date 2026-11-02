import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'scripts/prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

class PackagingBoundaries(unittest.TestCase):
    def test_reject_archive_traversal(self):
        for name in ('../home/auth.json', '/data/data/other', 'x/../../escape', 'x\\escape'):
            with self.assertRaises(ValueError):
                prepare.safe_name(name)

    def test_npm_does_not_pull_conflicting_node(self):
        def p(name, depends=''):
            return {'Package': name, 'Depends': depends, 'Version': '1', 'Architecture': 'aarch64', 'Filename': name, 'SHA256': '0' * 64}
        index = {'npm': p('npm', 'nodejs | nodejs-lts'), 'nodejs': p('nodejs'), 'nodejs-lts': p('nodejs-lts')}
        result = prepare.resolve(index, ['npm', 'nodejs-lts'])
        self.assertEqual({x['Package'] for x in result}, {'npm', 'nodejs-lts'})

    def test_unknown_dependency_fails_closed(self):
        with self.assertRaises(RuntimeError):
            prepare.resolve({'nodejs-lts': {'Depends': 'missing'}}, ['nodejs-lts'])

if __name__ == '__main__':
    unittest.main()

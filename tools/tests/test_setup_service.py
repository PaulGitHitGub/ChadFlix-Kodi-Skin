"""ChadFlix Setup's start-up service: it adds a key file that is new in this version for people who already have the ChadFlix
key files, replaces only files an older Setup shipped, and never touches anything else. Temp folders only.
Run: python3 -B -m unittest discover -s tools/tests -v"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'src', 'script.chadflix.setup')
NEW = 'NetflixWatchlistMore.xml'


class Service(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='setup-service')
        self.addon, self.data, self.keymaps = (os.path.join(self.dir, d) for d in ('addon', 'data', 'keymaps'))
        shutil.copytree(os.path.join(SRC, 'resources'), os.path.join(self.addon, 'resources'))
        self.cfg = json.load(open(os.path.join(self.addon, 'resources', 'setup.template.json')))
        json.dump(self.cfg, open(os.path.join(self.addon, 'resources', 'setup.json'), 'w'))
        os.makedirs(self.keymaps)
        self.builtins, self.logs = [], []
        paths = {'path': self.addon, 'profile': self.data, 'id': 'script.chadflix.setup'}
        sys.modules['xbmc'] = types.SimpleNamespace(log=lambda m, l=1: self.logs.append(m), LOGINFO=1, executebuiltin=self.builtins.append)
        sys.modules['xbmcaddon'] = types.SimpleNamespace(Addon=lambda: types.SimpleNamespace(getAddonInfo=lambda k: paths[k]))
        sys.modules['xbmcvfs'] = types.SimpleNamespace(translatePath=lambda p: self.keymaps + '/' if p.startswith('special://masterprofile/keymaps') else p)
        ns = {'__name__': 'service_under_test'}
        exec(compile(open(os.path.join(SRC, 'service.py'), encoding='utf-8').read(), 'service.py', 'exec'), ns)
        self.main = ns['main']

    def shipped(self, name):
        return os.path.join(self.addon, 'resources', 'keymaps', name)

    def install(self, names):
        for n in names:
            shutil.copyfile(self.shipped(n), os.path.join(self.keymaps, n))

    def have(self):
        return sorted(os.listdir(self.keymaps))

    def test_the_new_key_file_is_shipped_and_listed(self):
        self.assertIn(NEW, self.cfg['keymaps'])
        self.assertEqual(self.cfg['keymaps_new'], [NEW])
        self.assertEqual(len(self.cfg['keymaps']), 5)
        for n in self.cfg['keymaps']:
            self.assertTrue(os.path.isfile(self.shipped(n)), n)

    def test_existing_user_gets_the_new_key_file(self):
        old = [n for n in self.cfg['keymaps'] if n != NEW]
        self.install(old)                                           # as after Setup 1.0.2
        self.main()
        self.assertEqual(self.have(), sorted(self.cfg['keymaps']))
        self.assertEqual(open(os.path.join(self.keymaps, NEW), 'rb').read(), open(self.shipped(NEW), 'rb').read())
        self.assertEqual(self.builtins, ['Action(reloadkeymaps)'])
        self.builtins.clear()
        self.main()                                                 # second start: nothing to do
        self.assertEqual(self.builtins, [])

    def test_never_set_up_or_undone_gets_nothing(self):
        self.main()
        self.assertEqual((self.have(), self.builtins), ([], []))
        open(os.path.join(self.keymaps, 'gen.xml'), 'w').write('<keymap/>')   # the person's own key file is not a ChadFlix one
        self.main()
        self.assertEqual((self.have(), self.builtins), (['gen.xml'], []))

    def test_only_one_old_chadflix_file_left_still_counts(self):
        self.install(['NetflixExitConfirm.xml'])
        self.main()
        self.assertEqual(self.have(), ['NetflixExitConfirm.xml', NEW])   # only the new one is added; removed ones stay removed

    def test_a_changed_file_is_left_alone_and_an_old_shipped_one_is_replaced(self):
        self.install(self.cfg['keymaps'])
        mine = os.path.join(self.keymaps, 'VirtualKeyboardBack.xml')
        open(mine, 'w').write('<keymap><!-- mine --></keymap>')
        self.main()
        self.assertIn('mine', open(mine).read())
        self.assertEqual(self.builtins, [])
        # a file an older Setup shipped (its hash is in keymaps_previous) is backed up and replaced
        name = 'NetflixRootBack.xml'
        old_text = b'<keymap><!-- old shipped --></keymap>'
        open(os.path.join(self.keymaps, name), 'wb').write(old_text)
        cfg = dict(self.cfg, keymaps_previous={name: [hashlib.sha256(old_text).hexdigest()]})
        json.dump(cfg, open(os.path.join(self.addon, 'resources', 'setup.json'), 'w'))
        self.main()
        self.assertEqual(open(os.path.join(self.keymaps, name), 'rb').read(), open(self.shipped(name), 'rb').read())
        backups = [os.path.join(r, f) for r, _d, fs in os.walk(self.data) for f in fs]
        self.assertEqual([open(b, 'rb').read() for b in backups], [old_text])

    def test_a_user_changed_copy_of_the_new_file_is_not_overwritten(self):
        self.install([n for n in self.cfg['keymaps'] if n != NEW])
        open(os.path.join(self.keymaps, NEW), 'w').write('<keymap><!-- mine --></keymap>')
        self.main()
        self.assertIn('mine', open(os.path.join(self.keymaps, NEW)).read())


if __name__ == '__main__':
    unittest.main()

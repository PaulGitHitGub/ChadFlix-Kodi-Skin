"""ChadFlix Setup answers only Kodi's own "Keep skin?" question (security audit F-08). A simulated Kodi: no real dialog.
Run: python3 -B -m unittest discover -s tools/tests -v"""
import ast
import os
import types
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'src', 'script.chadflix.setup', 'default.py')
SKIN = 'skin.arctic.fuse.3'
STRINGS = {13123: 'Keep skin?', 13111: 'Would you like to keep this change?'}
GERMAN = {13123: 'Skin behalten?', 13111: 'Soll diese Änderung beibehalten werden?'}


class Kodi:
    """dialogs: {tick: (heading, text) or None}; a dialog stays until answered or replaced. One tick = one 100 ms sleep."""

    def __init__(self, dialogs, strings=STRINGS, skin_after_set=SKIN, unreadable=False, start_skin='skin.estuary', user_answer=True,
                 heading_label=True, topmost=True):
        # unreadable: the text box cannot be read. heading_label: Control.GetLabel(1) gives the heading (as in Kodi 21).
        self.tick, self.dialogs, self.strings, self.unreadable = 0, dialogs, strings, unreadable
        self.heading_label, self.topmost = heading_label, topmost
        self.skin, self.skin_after_set = start_skin, skin_after_set
        self.dialog, self.clicks, self.logs, self.asked, self.user_answer = None, [], [], [], user_answer
        k = self

        def sleep(ms):
            for _ in range(max(1, ms // 100)):
                k.tick += 1
                if k.tick in k.dialogs:
                    k.dialog = k.dialogs[k.tick]
        def cond(text):
            if 'IsTopMost(yesnodialog)' in text:
                return k.dialog is not None and k.topmost
            return k.dialog is not None if 'yesnodialog' in text else False

        def infolabel(label):
            # Kodi evaluates Control.GetLabel(1) on the topmost window: the dialog's heading while the dialog is on top
            if label == 'Control.GetLabel(1)' and k.dialog is not None and k.heading_label:
                return k.dialog[0] if k.topmost else 'Some Other Window Label'
            return ''
        def builtin(text):
            if text.startswith('SendClick'):
                k.clicks.append((k.tick, k.dialog))
                k.dialog = None
        self.xbmc = types.SimpleNamespace(sleep=sleep, getCondVisibility=cond, executebuiltin=builtin, getSkinDir=lambda: k.skin, getInfoLabel=infolabel,
                                          getLocalizedString=lambda i: k.strings.get(i, ''), LOGINFO=1)

        class Control:
            def __init__(self, value):
                self.value = value

            def getLabel(self):
                return ''                    # Kodi 21: a label wrapper made by getControl() never holds the on-screen text

            def getText(self):               # a text box is read live
                if k.unreadable:
                    raise RuntimeError('no control')
                return self.value

        class Window:
            def __init__(self, window_id):
                assert window_id == 10100

            def getControl(self, control_id):
                if k.dialog is None:
                    raise RuntimeError('no dialog')
                return Control(k.dialog[0] if control_id == 1 else k.dialog[1])
        self.xbmcgui = types.SimpleNamespace(Window=Window)

        def rpc(method, params):
            if method == 'Settings.SetSettingValue':
                k.skin = k.skin_after_set
        dialog = types.SimpleNamespace(yesno=lambda *a, **kw: (k.asked.append(a), k.user_answer)[1], ok=lambda *a, **kw: None)
        self.ns = {'xbmc': self.xbmc, 'xbmcgui': self.xbmcgui, 'rpc': rpc, 'log': self.logs.append, 'SKIN': SKIN, 'DIALOG': dialog, 'TITLE': 'ChadFlix Setup'}
        tree = ast.parse(open(SRC, encoding='utf-8').read())
        keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in ('_dialog_texts', 'is_keep_skin_question', '_switch_once', 'switch_skin'))
                or (isinstance(n, ast.Assign) and any(getattr(t, 'id', '') in ('YESNO_DIALOG', 'DECLINED') for t in n.targets))
                or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Tuple))]
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, 'exec'), self.ns)


KEEP = (STRINGS[13123], STRINGS[13111])
OTHER = ('Update available', 'Install the update now?')


class SkinSwitch(unittest.TestCase):
    def test_the_keep_skin_question_is_answered(self):
        k = Kodi({5: KEEP})
        self.assertTrue(k.ns['_switch_once']())
        self.assertEqual([c[1] for c in k.clicks], [KEEP])
        self.assertLess(k.clicks[0][0], 10)                       # answered at once (Kodi closes it by itself after 10 s)

    def test_an_unrelated_dialog_is_never_clicked(self):
        k = Kodi({5: OTHER})                                      # stays open for the whole wait
        k.ns['_switch_once']()
        self.assertEqual(k.clicks, [])
        self.assertTrue(any('left alone' in l for l in k.logs))
        self.assertLessEqual(k.tick, 600 + 40 + 40 * 5 + 5)       # and the wait still ends (bounded)

    def test_unrelated_dialog_first_then_the_keep_skin_question(self):
        k = Kodi({3: OTHER, 20: None, 25: KEEP})
        self.assertTrue(k.ns['_switch_once']())
        self.assertEqual([c[1] for c in k.clicks], [KEEP])        # only the intended one

    def test_same_heading_other_text_is_not_clicked(self):
        k = Kodi({5: ('Keep skin?', 'Delete all your settings?')})
        k.ns['_switch_once']()
        self.assertEqual(k.clicks, [])

    def test_unreadable_text_is_not_clicked(self):
        k = Kodi({5: KEEP}, unreadable=True)
        k.ns['_switch_once']()
        self.assertEqual(k.clicks, [])                            # the text cannot be read: never click

    def test_kodi_21_label_wrapper_returns_nothing_and_it_still_answers(self):
        k = Kodi({5: KEEP}, heading_label=False)                  # neither the wrapper nor the info label gives the heading
        self.assertTrue(k.ns['_switch_once']())
        self.assertEqual([c[1] for c in k.clicks], [KEEP])        # the text identifies the question

    def test_right_text_under_another_heading_is_not_clicked(self):
        k = Kodi({5: ('Delete everything?', STRINGS[13111])})
        k.ns['_switch_once']()
        self.assertEqual(k.clicks, [])

    def test_heading_is_only_read_while_the_dialog_is_on_top(self):
        k = Kodi({5: KEEP}, topmost=False)                        # something else is on top: its label is not taken for the heading
        self.assertTrue(k.ns['_switch_once']())
        self.assertEqual([c[1] for c in k.clicks], [KEEP])

    def test_other_language(self):
        k = Kodi({5: (GERMAN[13123], GERMAN[13111])}, strings=GERMAN)
        self.assertTrue(k.ns['_switch_once']())
        self.assertEqual(len(k.clicks), 1)
        k = Kodi({5: KEEP}, strings=GERMAN)                       # an English dialog on a German Kodi is not Kodi's own question
        k.ns['_switch_once']()
        self.assertEqual(k.clicks, [])

    def test_no_dialog_at_all_times_out(self):
        k = Kodi({})
        self.assertTrue(k.ns['_switch_once']())                   # the skin is active and nothing asked: kept
        self.assertEqual(k.clicks, [])

    def test_skin_did_not_change(self):
        k = Kodi({5: KEEP}, skin_after_set='skin.estuary')        # Kodi refused the switch: nothing is clicked
        self.assertFalse(k.ns['_switch_once']())
        self.assertEqual(k.clicks, [])

    def test_already_active_and_declined(self):
        k = Kodi({}, start_skin=SKIN)
        self.assertIs(k.ns['switch_skin'](), True)
        self.assertEqual(k.asked, [])                             # nothing to switch, nothing asked
        k = Kodi({5: KEEP}, user_answer=False)
        self.assertEqual(k.ns['switch_skin'](), k.ns['DECLINED']) # "Not now": no switch, no click
        self.assertEqual((k.clicks, k.skin), ([], 'skin.estuary'))

    def test_old_code_clicked_the_unrelated_dialog(self):
        import subprocess
        old = subprocess.run(['git', 'show', '9c8aa59:src/script.chadflix.setup/default.py'], capture_output=True, text=True,
                             cwd=os.path.dirname(SRC)).stdout
        if 'def _switch_once' not in old:
            self.skipTest('release 2 source not available')
        k = Kodi({5: OTHER})
        tree = ast.parse(old)
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_switch_once'], type_ignores=[]),
                     'old', 'exec'), k.ns)
        k.ns['_switch_once']()
        self.assertEqual([c[1] for c in k.clicks], [OTHER])       # the behaviour the audit found


if __name__ == '__main__':
    unittest.main()

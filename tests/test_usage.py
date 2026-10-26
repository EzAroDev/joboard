"""Fresh quota events must be visible without rescanning unchanged session logs."""
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from usage import UsageTracker


class UsageTests(unittest.TestCase):
    def setUp(self):
        build = Path(__file__).resolve().parents[1] / 'build'
        build.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='usage-test-', dir=build)
        assert Path(self.temp.name).resolve().is_relative_to(build.resolve())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.tracker = UsageTracker(self.home / 'state.json', self.home)
        self.log = self.home / '.codex/sessions/2026/10/24/rollout-test.jsonl'
        self.log.parent.mkdir(parents=True)

    @staticmethod
    def event(percent, reset=None):
        return json.dumps({'timestamp': '2026-10-24T12:00:00Z', 'payload': {'rate_limits': {
            'primary': {'used_percent': percent, 'window_minutes': 300, 'resets_at': reset or time.time() + 10000},
            'plan_type': 'plus'}}}) + '\n'

    def test_appended_event_updates_quota_and_unchanged_file_is_cached(self):
        self.log.write_text(self.event(21), encoding='utf-8')
        first = self.tracker._codex_windows()
        self.assertEqual(first[0][0]['utilization'], .21)
        with patch.object(Path, 'open', side_effect=AssertionError('Unchanged log read twice')):
            self.assertEqual(self.tracker._codex_windows(), first)
        with self.log.open('a', encoding='utf-8') as f:
            f.write(self.event(42))
        self.assertEqual(self.tracker._codex_windows()[0][0]['utilization'], .42)

    def test_partial_event_waits_for_the_complete_line(self):
        self.log.write_text(self.event(10), encoding='utf-8')
        self.tracker._codex_windows()
        line = self.event(77)
        with self.log.open('a', encoding='utf-8') as f:
            f.write(line[:25])
        self.assertEqual(self.tracker._codex_windows()[0][0]['utilization'], .10)
        with self.log.open('a', encoding='utf-8') as f:
            f.write(line[25:])
        self.assertEqual(self.tracker._codex_windows()[0][0]['utilization'], .77)

    def test_replaced_log_drops_the_previous_limit(self):
        self.log.write_text(self.event(99) + self.event(88), encoding='utf-8')
        self.tracker._codex_windows()
        self.log.write_text(self.event(5), encoding='utf-8')
        self.assertEqual(self.tracker._codex_windows()[0][0]['utilization'], .05)

    def test_reset_and_claude_limit_event_are_reported_immediately(self):
        self.log.write_text(self.event(100, time.time() - 10), encoding='utf-8')
        self.tracker.record_claude_limits({'unifiedWindows': {'five_hour': {
            'utilization': .93, 'status': 'allowed_warning', 'resetsAt': time.time() + 1000}}})
        summary = self.tracker.summary({'claude': True, 'chatgpt': True})
        self.assertEqual(summary['claude']['windows'][0]['utilization'], .93)
        self.assertEqual(summary['chatgpt']['windows'][0]['status'], 'reset')
        self.assertFalse(summary['chatgpt']['blocked'])

    def test_latest_measurement_wins_across_concurrent_sessions(self):
        other = self.log.with_name('rollout-other.jsonl')
        other.write_text(self.event(70).replace('12:00:00', '12:05:00'), encoding='utf-8')
        self.log.write_text(self.event(20), encoding='utf-8')  # more recently modified, older measurement
        self.assertEqual(self.tracker._codex_windows()[0][0]['utilization'], .70)


class LiveQuotaTests(unittest.TestCase):
    """Mesure automatique : lue sur le compte, sans message ni quota consommé."""
    CLAUDE = {'five_hour': {'utilization': 80.0, 'resets_at': '2030-01-01T00:00:00+00:00'},
              'seven_day': {'utilization': 28.0, 'resets_at': '2030-01-05T00:00:00+00:00'}, 'seven_day_opus': None}
    CODEX = {'plan_type': 'plus', 'rate_limit': {
        'primary_window': {'used_percent': 13, 'limit_window_seconds': 18000, 'reset_at': 1893456000},
        'secondary_window': {'used_percent': 100, 'limit_window_seconds': 604800, 'reset_at': 1893800000}}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / '.claude').mkdir()
        (self.home / '.codex').mkdir()
        (self.home / '.claude/.credentials.json').write_text(json.dumps(
            {'claudeAiOauth': {'accessToken': 'a', 'expiresAt': (time.time() + 3600) * 1000}}), encoding='utf-8')
        (self.home / '.codex/auth.json').write_text(json.dumps({'tokens': {'access_token': 'b', 'account_id': 'c'}}), encoding='utf-8')
        self.tracker = UsageTracker(self.home / 'state.json', self.home)

    def fake(self, url, headers):
        return self.CLAUDE if 'anthropic' in url else self.CODEX

    def test_live_windows_for_both_accounts(self):
        with patch('usage._get_json', side_effect=self.fake):
            self.tracker.refresh_live(force=True)
        s = self.tracker.summary({'claude': True, 'chatgpt': True})
        claude, codex = s['claude'], s['chatgpt']
        self.assertTrue(claude['live'] and codex['live'])
        self.assertEqual([(w['label'], w['utilization'], w['status']) for w in claude['windows']],
                         [('Fenêtre 5 h', .8, 'allowed_warning'), ('Semaine', .28, 'allowed')])
        self.assertEqual(codex['plan'], 'plus')
        self.assertEqual([w['label'] for w in codex['windows']], ['Fenêtre 5 h', 'Semaine'])
        self.assertTrue(codex['blocked'])
        self.assertEqual(codex['availableAt'], 1893800000)

    def test_failure_keeps_last_measure_and_explains(self):
        with patch('usage._get_json', side_effect=self.fake):
            self.tracker.refresh_live(force=True)
        error = urllib.error.HTTPError('u', 401, 'unauthorized', {}, None)
        with patch('usage._get_json', side_effect=error):
            self.tracker.refresh_live(force=True)
        claude = self.tracker.summary({'claude': True})['claude']
        self.assertEqual(claude['windows'][0]['utilization'], .8)
        self.assertFalse(claude['live'])
        self.assertIn('renouveler', claude['liveError'])

    def test_rate_limited_service_is_left_alone(self):
        with patch('usage._get_json', side_effect=urllib.error.HTTPError('u', 429, 'too many', {}, None)) as get:
            self.tracker.refresh_live(force=True)
            self.tracker.refresh_live()
        self.assertEqual(get.call_count, 2)  # une tentative par compte, puis pause

    def test_expired_login_is_not_sent(self):
        (self.home / '.claude/.credentials.json').write_text(json.dumps(
            {'claudeAiOauth': {'accessToken': 'a', 'expiresAt': 1000}}), encoding='utf-8')
        with patch('usage._get_json', side_effect=self.fake) as get:
            self.tracker.refresh_live(force=True)
        self.assertNotIn('anthropic', ' '.join(str(c.args[0]) for c in get.call_args_list))
        self.assertIn('renouveler', self.tracker.summary({})['claude']['liveError'])


if __name__ == '__main__':
    unittest.main()

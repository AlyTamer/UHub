import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect_rooms as collector
from prepare_site import prepare_site


def page(cells=None):
    cells = cells or {}
    return f'<table id="{collector.TABLE}">' + ''.join(
        f'<tr><th>{day}</th>' + ''.join(
            f'<td>{cells.get(f"{day}/{slot}", "&nbsp;")}</td>' for slot in range(1, 9)
        ) + '</tr>' for day in collector.DAYS
    ) + '</table>'


def entry(location):
    return f'<div class="slot"><dl><dt>Group</dt><dd>Fake course</dd><dt>Location:</dt><dd>{location}</dd></dl></div>'


FORM = '''<form id="form1"><input type="hidden" name="__VIEWSTATE" value="state">
<input type="hidden" name="__EVENTVALIDATION" value="valid">
<input type="hidden" name="__VIEWSTATE1" value="extra"></form>
<script>var courses = [{'id': '101', 'value': 'Example'}, {'id': '102', 'value': 'Other'}], tas = [];</script>'''


class CollectorTests(unittest.TestCase):
    def test_other_rooms_are_preserved_with_occupancy(self):
        self.assertEqual(collector.rooms_in('G.206 / N/A / Workshop / A1.101-PD / H21'),
                         {'G.206', 'WORKSHOP', 'A1.101', 'H21'})
        busy = collector.parse_schedule(page({'Tuesday/4': entry('G.206 / Workshop')}))
        self.assertEqual(busy['Tuesday/4'], {'G.206', 'WORKSHOP'})

    def test_saturday_is_preserved_and_friday_is_ignored(self):
        friday = '<tr><th>Friday</th>' + ''.join(
            f'<td>{entry("D9.999")}</td>' for _ in range(8)
        ) + '</tr>'
        source = page({'Saturday/1': entry('B2.101')}).replace('</table>', friday + '</table>')
        busy = collector.parse_schedule(source)
        self.assertEqual(len(busy), 48)
        self.assertEqual(busy['Saturday/1'], {'B2.101'})
        self.assertFalse(any(key.startswith('Friday/') for key in busy))
        self.assertNotIn('D9.999', set().union(*busy.values()))

    def test_shared_flutter_schema_fixture(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'room_snapshot.json').read_text())
        busy = collector.parse_schedule(page({'Tuesday/4': entry('G.206 / D2.301-PD / H20')}))
        self.assertEqual({key: sorted(value) for key, value in busy.items()}, fixture['busy'])
        self.assertEqual(sorted(set().union(*busy.values())), fixture['rooms'])

    def test_usernames(self):
        for value in ['alice.test', 'alice.test@student.guc.edu.eg', 'guc.edu.eg\\alice.test']:
            self.assertEqual(collector.username(value), 'alice.test')

    def test_rooms_and_ignored_locations(self):
        self.assertEqual(collector.rooms_in('G.206 / B2.101 / D2.301-PD / H20'), {'G.206', 'B2.101', 'D2.301', 'H20'})
        for value in ['TBA', 'N/A', '']:
            self.assertEqual(collector.rooms_in(value), set())
        for building in 'ABCD':
            for number in range(1, 10):
                room = f'{building}{number}.101'
                self.assertEqual(collector.rooms_in(room + '-PD'), {room})

    def test_tokens_and_catalog(self):
        self.assertEqual(collector.course_ids(FORM), ['101', '102'])
        fields = dict(collector.form_fields(FORM, '101'))
        self.assertEqual(fields['__VIEWSTATE1'], 'extra')
        self.assertEqual(fields['course[]'], '101')
        self.assertNotIn('ta[]', fields)
        for source in ['<html>Login</html>', FORM.replace("'102'", "'invalid'")]:
            with self.assertRaises(collector.CollectionError):
                collector.course_ids(source)

    def test_schedule_and_complete_schema(self):
        busy = collector.parse_schedule(page({'Tuesday/4': entry('G.206 / D2.301-PD'), 'Saturday/8': entry('H20')}))
        self.assertEqual(len(busy), 48)
        self.assertEqual(busy['Tuesday/4'], {'G.206', 'D2.301'})
        self.assertEqual(busy['Saturday/8'], {'H20'})
        self.assertEqual(busy['Saturday/1'], set())
        with self.assertRaises(collector.CollectionError):
            collector.parse_schedule(page().replace('<th>Saturday</th>', '<th>Other</th>'))
        with self.assertRaises(collector.CollectionError):
            collector.parse_schedule(page({'Tuesday/4': 'Unknown markup'}))

    def test_complete_collection_and_atomic_output(self):
        responses = [FORM, page({'Tuesday/4': entry('G.206')}), FORM,
                     page({'Tuesday/4': entry('D2.301-PD / H20')})]
        with patch.object(collector, 'request_page', side_effect=responses):
            snapshot = collector.collect(object(), delay=0)
        self.assertEqual(snapshot['courseCount'], 2)
        self.assertEqual(snapshot['rooms'], ['D2.301', 'G.206', 'H20'])
        self.assertEqual(snapshot['busy']['Tuesday/4'], ['D2.301', 'G.206', 'H20'])
        self.assertEqual(set(snapshot), {'version', 'updatedAt', 'courseCount', 'rooms', 'busy'})
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'rooms.json'
            collector.write_snapshot(snapshot, output)
            original = output.read_bytes()
            with patch.object(collector, 'request_page', side_effect=[FORM, page(), collector.CollectionError('Offline')]):
                with self.assertRaises(collector.CollectionError):
                    collector.write_snapshot(collector.collect(object(), delay=0), output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(json.loads(original)['rooms'], snapshot['rooms'])

    def test_all_unknown_rooms_cannot_replace_snapshot(self):
        with patch.object(collector, 'request_page', side_effect=[FORM, page(), FORM, page()]):
            with self.assertRaises(collector.CollectionError):
                collector.collect(object(), delay=0)

    def test_auth_errors_do_not_retry_or_log_response(self):
        from unittest.mock import Mock
        session = Mock()
        session.request.return_value.status_code = 401
        session.request.return_value.text = 'SECRET'
        with self.assertRaises(collector.CollectionError) as failure:
            collector.request_page(session, 'GET')
        self.assertNotIn('SECRET', str(failure.exception))
        self.assertEqual(session.request.call_count, 1)

    def test_site_keeps_existing_assets_and_excludes_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ['index.html', 'version.json', 'custommessage.json', 'theme.json', '1.jpg', 'rooms.json', 'CNAME', 'scripts/collect_rooms.py', '.github/workflows/update-rooms.yml']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            with patch.object(subprocess, 'check_output', return_value='\0'.join(['index.html', 'version.json', 'custommessage.json', 'theme.json', '1.jpg', 'rooms.json', 'CNAME', 'scripts/collect_rooms.py', '.github/workflows/update-rooms.yml', '']).encode()):
                prepare_site(root, root / '_site')
            for name in ['index.html', 'version.json', 'custommessage.json', 'theme.json', '1.jpg', 'rooms.json', 'CNAME']:
                self.assertEqual((root / '_site' / name).read_text(), name)
            self.assertFalse((root / '_site' / 'scripts').exists())
            self.assertFalse((root / '_site' / '.github').exists())


if __name__ == '__main__':
    unittest.main()

"""Generate a complete public room-occupancy snapshot; never publish raw HTML."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from bs4 import BeautifulSoup
import requests
from requests_ntlm import HttpNtlmAuth

URL = 'https://apps.guc.edu.eg/student_ext/Scheduling/SearchAcademicScheduled_001.aspx'
TABLE = 'ContentPlaceHolderright_ContentPlaceHoldercontent_schedule'
BUTTON = 'ctl00$ctl00$ContentPlaceHolderright$ContentPlaceHoldercontent$B_ShowSchedule'
DAYS = ['Saturday', 'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday']


class CollectionError(Exception):
    """Safe diagnostic: no credentials, response bodies, or personal details."""


def username(raw: str) -> str:
    return raw.strip().rsplit('\\', 1)[-1].split('@', 1)[0].strip().lower()


def rooms_in(location: str) -> set[str]:
    text = re.sub(r'\s+', ' ', location.upper()).strip()
    placeholders = {'', '-', '--', 'N/A', 'NA', 'TBA', 'TBD', 'NONE', 'NULL', 'NOT ASSIGNED'}
    if text in placeholders:
        return set()
    pattern = re.compile(r'\b(?:[A-Z]\s*\d*\s*\.\s*\d{3}|H\s*\d+)(?!\d)')
    rooms = set()
    for part in re.split(r'[,;/&+|]+', re.sub(r'\bN/A\b', '', text)):
        part = part.strip()
        if part in placeholders:
            continue
        matches = list(pattern.finditer(part))
        if matches:
            rooms.update(re.sub(r'\s+', '', match.group()) for match in matches)
        else:
            rooms.add(part)
    return rooms

def course_ids(source: str) -> list[str]:
    match = re.search(r'\bcourses\s*=\s*(\[[\s\S]*?\])\s*[,;]\s*(?:(?:var|let|const)\s+)?tas\s*=', source)
    if not match:
        raise CollectionError('Course catalog was not found in the response.')
    catalog = match.group(1)
    ids = re.findall(r'''['"]id['"]\s*:\s*['"](\d+)['"]''', catalog)
    # Refuse to silently publish a partially parsed catalog.
    if not ids or len(ids) != len(re.findall(r'''['"]id['"]\s*:''', catalog)):
        raise CollectionError('Course catalog contains missing or unsupported IDs.')
    return sorted(set(ids), key=int)


def form_fields(source: str, course: str) -> list[tuple[str, str]]:
    doc = BeautifulSoup(source, 'html.parser')
    form = doc.find('form', id='form1')
    if form is None:
        raise CollectionError('Schedule form is missing. Check portal access.')
    fields = {field['name']: field.get('value', '')
              for field in form.select('input[type="hidden"][name]')}
    if not fields.get('__VIEWSTATE') or not fields.get('__EVENTVALIDATION'):
        raise CollectionError('Schedule form tokens are missing.')
    fields.update({'__EVENTTARGET': '', '__EVENTARGUMENT': '', BUTTON: 'Show Schedule'})
    return [*fields.items(), ('course[]', course)]


def parse_schedule(source: str) -> dict[str, set[str]]:
    table = BeautifulSoup(source, 'html.parser').find('table', id=TABLE)
    if table is None:
        raise CollectionError('Schedule table is missing.')
    occupied = {}
    days_seen = set()
    for row in table.find_all('tr'):
        if row.find_parent('table') is not table:
            continue
        cells = row.find_all(['th', 'td'], recursive=False)
        if not cells:
            continue
        day = cells[0].get_text(' ', strip=True)
        if day not in DAYS:
            continue
        if day in days_seen or len(cells) != 9 or any(
            cell.get('colspan', '1') != '1' or cell.get('rowspan', '1') != '1'
            for cell in cells[1:]
        ):
            raise CollectionError(f'{day}: expected eight separate slot cells.')
        days_seen.add(day)
        for slot, cell in enumerate(cells[1:], 1):
            entries = cell.select('div.slot')
            if not entries and cell.get_text(strip=True).replace('\xa0', '').strip():
                raise CollectionError(f'{day} slot {slot}: unrecognized class markup.')
            rooms = set()
            for entry in entries:
                labels, values = entry.find_all('dt'), entry.find_all('dd')
                if len(labels) != len(values):
                    raise CollectionError(f'{day} slot {slot}: incomplete class fields.')
                locations = [value.get_text(' ', strip=True)
                             for label, value in zip(labels, values)
                             if label.get_text(strip=True).lower().replace(':', '').strip() == 'location']
                if not locations:
                    raise CollectionError(f'{day} slot {slot}: missing location field.')
                for location in locations:
                    # Preserve other rooms; discard suffixes and unassigned placeholders.
                    rooms.update(rooms_in(location))
            occupied[f'{day}/{slot}'] = rooms
    if days_seen != set(DAYS):
        raise CollectionError('Schedule is missing one or more days.')
    return occupied


def request_page(session, method: str, **kwargs) -> str:
    for attempt in range(3):
        try:
            response = session.request(method, URL, timeout=(20, 60), allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise CollectionError('TLS validation failed; check the bundled CA certificate.') from None
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise CollectionError('Portal connection failed after three attempts.') from None
            time.sleep(2 ** (attempt + 1))
            continue
        if response.status_code in (401, 403):
            response.close()
            raise CollectionError('Portal rejected authentication. Check GUC_USERNAME and GUC_PASSWORD.')
        if response.status_code == 429 or response.status_code >= 500:
            response.close()
            if attempt == 2:
                raise CollectionError('Portal unavailable after three attempts.')
            time.sleep(2 ** (attempt + 1))
            continue
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise CollectionError(f'Unexpected portal HTTP status {status}.')
        source = response.text
        response.close()
        return source
    raise CollectionError('Portal request failed.')


def collect(session, delay: float = 0.25) -> dict:
    catalog = request_page(session, 'GET')
    ids = course_ids(catalog)
    busy = {f'{day}/{slot}': set() for day in DAYS for slot in range(1, 9)}
    print(f'Found {len(ids)} courses. Collecting complete weekly timetables.', flush=True)
    for index, course in enumerate(ids):
        # Fresh GET tokens per course; reuse the same NTLM-authenticated connection.
        form = catalog if index == 0 else request_page(session, 'GET')
        source = request_page(session, 'POST', data=form_fields(form, course))
        try:
            course_busy = parse_schedule(source)
        except CollectionError as error:
            raise CollectionError(f'Course {course}: {error}') from None
        for key, rooms in course_busy.items():
            busy[key].update(rooms)
        if (index + 1) % 10 == 0 or index + 1 == len(ids):
            print(f'Checked {index + 1}/{len(ids)} courses.', flush=True)
        if delay:
            time.sleep(delay)
    rooms = set().union(*busy.values())
    if not rooms:
        raise CollectionError('No rooms found; refusing to replace the published snapshot.')
    return {
        'version': 1,
        'updatedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'courseCount': len(ids),
        'rooms': sorted(rooms),
        'busy': {key: sorted(value) for key, value in busy.items()},
    }


def write_snapshot(snapshot: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    temporary.write_text(json.dumps(snapshot, separators=(',', ':')) + '\n', encoding='utf-8')
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('rooms.json'))
    parser.add_argument('--delay', type=float, default=0.25)
    args = parser.parse_args()
    login = username(os.environ.get('GUC_USERNAME', ''))
    password = os.environ.get('GUC_PASSWORD', '')
    if not login or not password:
        print('Missing GUC_USERNAME or GUC_PASSWORD environment secret.', file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory() as temporary, requests.Session() as session:
            # Append the same public intermediate certificate used by the app.
            ca = Path(temporary) / 'ca-bundle.pem'
            extra = Path(__file__).parent / 'certs' / 'SectigoR36.pem'
            ca.write_bytes(Path(requests.certs.where()).read_bytes() + b'\n' + extra.read_bytes())
            session.verify = str(ca)
            session.auth = HttpNtlmAuth('guc.edu.eg\\' + login, password)
            session.headers.update({'User-Agent': 'UHub-Room-Collector/1.0', 'Accept': 'text/html'})
            snapshot = collect(session, max(0, args.delay))
            write_snapshot(snapshot, args.output)
            print(f'Saved {len(snapshot["rooms"])} rooms from {snapshot["courseCount"]} courses.', flush=True)
    except CollectionError as error:
        print(f'Collection failed: {error}', file=sys.stderr)
        return 1
    except Exception as error:
        # Never log credential-bearing objects or portal response bodies.
        print(f'Collection failed ({type(error).__name__}); no snapshot was published.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

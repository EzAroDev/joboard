"""Cache company marks for the current tracker. Reads Sheets through the running
local app; only company names go to autocomplete, domains to the favicon service.
No credentials, rows or conversation data are saved in the manifest.
"""
import hashlib
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Optional, private: tracker-specific aliases and domains (ignored by Git).
ALIASES_PATH = Path(__file__).with_name('company-aliases.json')
LOCAL_ALIASES = json.loads(ALIASES_PATH.read_text(encoding='utf-8')) if ALIASES_PATH.is_file() else {}


def normalize(name):
    value = ''.join(c for c in unicodedata.normalize('NFD', name.lower()) if not unicodedata.combining(c))
    value = re.sub(r'\b(sa|sas|inc|ltd|llc|plc|corp|corporation)\b', '', value)
    return re.sub('[^a-z0-9]', '', value)


def queries(name):
    clean = re.split(r'\s*[(/×]|\s+[—–]', name)[0].strip()
    regional = re.sub(r'\s+(?:(?:CIB|Investment Managers)\s+)?(?:Americas|North America|US|USA|France|Belgique|Belgium|Switzerland|Suisse)$', '', clean, flags=re.I)
    parent_aliases = LOCAL_ALIASES.get('parents', {})
    short = re.sub(r'\s+(SAS|SA|SE|Inc\.?|Conseil|Business Support)$', '', regional, flags=re.I)
    alternatives = [part.strip() for part in name.split('/') if part.strip()]
    return list(dict.fromkeys([name, clean, regional, short] + alternatives + ([parent_aliases[name]] if name in parent_aliases else [])))


def choose_domain(matches, query):
    key = normalize(query)
    exact = [c for c in matches if normalize(c.get('name', '')) == key and re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', c.get('domain', ''))]
    if len(exact) == 1:
        return exact[0]['domain']
    branded = [c for c in matches if re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', c.get('domain', '')) and
               re.sub(r'^(groupe|group)', '', normalize(c['domain'].split('.')[0])) == key and
               (normalize(c.get('name', '')).startswith(key) or key in normalize(c.get('name', '')))]
    preferred = [c for c in branded if c['domain'].endswith('.com')]
    return preferred[0]['domain'] if len(preferred) == 1 else branded[0]['domain'] if len(branded) == 1 else None


def read_url(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'SuiviCandidatures/1.0'})
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.read()


class MetaParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'sc-token':
            self.token = attrs.get('content')


def company_names():
    base = 'http://127.0.0.1:8765'
    parser = MetaParser()
    parser.feed(read_url(base + '/').decode())
    request = urllib.request.Request(base + '/api/call', data=json.dumps({'fn': 'getAllData', 'args': []}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-SC-Token': parser.token})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)['result']
    hints = {}
    ignored = ('linkedin.com', 'hellowork.com', 'indeed.com', 'welcometothejungle.com', 'glassdoor.com',
               'apec.fr', 'google.com', 'googleusercontent.com', 'drive.google.com', 'microsoft.com',
               'youtube.com', 'myworkdayjobs.com', 'workday.com', 'greenhouse.io', 'lever.co', 'smartrecruiters.com',
               'mon-vie-via.businessfrance.fr', 'businessfrance.fr', 'jobs.ch', 'jobup.ch', 'teamtailor.com',
               'intercom.com', 'wikipedia.org', 'wix.com', 'notion.so', 'on.com')
    for tab in data['tabs'].values():
        for row in tab['rows']:
            name = str(row.get(tab['cols'].get('Entreprise'), '')).strip()
            if not name:
                continue
            hints.setdefault(name, set())
            key = normalize(name)
            for value in list(row.values()) + list((row.get('_links') or {}).values()):
                if not isinstance(value, str):
                    continue
                for url in re.findall(r'https?://[^\s<>\"\)]+', value):
                    host = (urllib.parse.urlparse(url).hostname or '').lower().removeprefix('www.')
                    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host) or any(host == d or host.endswith('.' + d) for d in ignored):
                        continue
                    if any(len(part) >= 4 and normalize(part) in key for part in host.split('.')[:-1]):
                        hints[name].add(host)
    return sorted(hints), hints


def main():
    names, hints = company_names()
    # Reuse the reviewed domain map maintained in web/logos.js.
    js = (ROOT / 'web/logos.js').read_text(encoding='utf-8')
    domain_block = js.split('const domains = {', 1)[1].split('};', 1)[0]
    known = dict(re.findall(r"(\w+): '([^']+)'", domain_block))
    known.update(LOCAL_ALIASES.get('domains', {}))
    directory = ROOT / 'web/assets/companies'
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / 'web/assets/company-logos.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}

    def cache(name):
        key = normalize(name)
        if key in manifest and (ROOT / 'web' / manifest[key]['src'].lstrip('/')).is_file():
            return key, manifest[key]
        try:
            candidates = queries(name)
            host = next((known[normalize(q)] for q in candidates if normalize(q) in known), None)
            if not host and len(hints[name]) == 1:
                host = next(iter(hints[name]))
            if not host:
                for query in candidates:
                    matches = json.loads(read_url('https://autocomplete.clearbit.com/v1/companies/suggest?query=' + urllib.parse.quote(query)))
                    host = choose_domain(matches, query)
                    if host:
                        break
            if not host:
                return key, None
            try:
                mark = read_url('https://www.google.com/s2/favicons?domain=' + urllib.parse.quote(host) + '&sz=64')
            except Exception:
                mark = read_url('https://www.google.com/s2/favicons?domain=' + urllib.parse.quote('www.' + host) + '&sz=64')
            extension = ('png' if mark.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if mark.startswith(b'\xff\xd8\xff')
                         else 'ico' if mark.startswith(b'\x00\x00\x01\x00') else 'gif' if mark.startswith((b'GIF87a', b'GIF89a'))
                         else 'webp' if mark.startswith(b'RIFF') and mark[8:12] == b'WEBP' else None)
            if not extension:
                return key, None
            filename = hashlib.sha256(key.encode()).hexdigest()[:20] + '.' + extension
            (directory / filename).write_bytes(mark)
            return key, {'domain': host, 'src': '/assets/companies/' + filename}
        except Exception:
            return key, None

    print(f'{len(names)} entreprises à identifier', flush=True)
    missing = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(cache, name): name for name in names}
        for i, job in enumerate(as_completed(jobs), 1):
            key, result = job.result()
            if result:
                manifest[key] = result
            else:
                missing.append(jobs[job])
            if i % 20 == 0:
                print(f'{i}/{len(names)} traitées', flush=True)
    keys = {normalize(name) for name in names}
    manifest = {k: v for k, v in manifest.items() if k in keys}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (ROOT / 'build/logo-unresolved.json').write_text(json.dumps(missing, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(manifest)} logos mis en cache ; {len(missing)} noms sans correspondance certaine.', flush=True)


if __name__ == '__main__':
    main()

"""Browser checks for external AI prompts and usage, with synthetic data only."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_fixtures import ROOT, fixture, usage_fixture


def run():
    data = fixture()
    usage = usage_fixture()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        context.add_init_script("""
            localStorage.setItem('sc-view', 'ai');
            window.copiedText = '';
            Object.defineProperty(navigator, 'clipboard', {value: {
                writeText: async text => { window.copiedText = text; }
            }});
        """)
        page = context.new_page()
        errors, ai_requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(req):
            path = req.request.url.split("localhost:9876")[-1].split("?")[0]
            if path.startswith('/api/ai'):
                ai_requests.append(path)
                req.abort()
            elif path == '/api/call':
                body = req.request.post_data_json
                fn = body['fn']
                result = data if fn == 'getAllData' else []
                if fn == 'calendarStatus':
                    result = {"configured": False, "connected": False, "account": "Compte de test",
                              "timezone": "Europe/Paris", "ics": {"set": False}, "pending": None}
                elif fn == 'calendarEvents':
                    result = {"events": [{"id": "local-test", "title": "Entretien de test",
                              "start": "2026-10-27T10:00", "end": "2026-10-27T11:00",
                              "allDay": False, "source": "local", "kind": "entretien",
                              "description": "Préparer les projets data", "location": "Paris"}],
                              "error": None}
                req.fulfill(json={"result": result})
            elif path == '/api/usage':
                req.fulfill(json={"result": usage})
            elif path == '/api/ping':
                req.fulfill(json={"session": "{{SC_SESSION}}"})
            else:
                file = ROOT / 'web' / ('index.html' if path == '/' else path.lstrip('/'))
                if file.is_file():
                    req.fulfill(path=str(file))
                else:
                    req.abort()

        page.route('http://localhost:9876/**', route)
        page.route('https://autocomplete.clearbit.com/**', lambda req: req.fulfill(json=[]))
        page.goto('http://localhost:9876/')
        page.wait_for_selector('.kpi')
        def copy_preview():
            field = page.locator('#ai-prompt-text')
            field.wait_for()
            assert page.evaluate('window.copiedText') == ''
            prompt = field.input_value()
            for heading in ('# Mission', '# Contexte fourni', '# Réponse attendue', '# Consignes'):
                assert heading in prompt, prompt
            assert 'N’invente aucune' in prompt
            page.locator('[data-form="ai-prompt"] button[type="submit"]').click()
            page.wait_for_function('window.copiedText.length > 50')
            assert page.evaluate('window.copiedText') == prompt
            page.locator('[data-form="ai-prompt"] [data-act="close-modal"]').click()
            page.wait_for_selector('#ai-prompt-text', state='detached')
            return prompt

        assert page.locator('.nav-item[data-view="ai"]').count() == 0
        assert page.locator('script[src*="assistant-ui"], link[href*="assistant-ui"]').count() == 0
        for view in ('offres', 'spont', 'contacts', 'vie'):
            page.locator(f'.nav-item[data-view="{view}"]').click()
            page.locator('[data-act="open"]').first.click()
            page.locator('[data-act="ask-ai"]').click()
            copied = copy_preview()
            assert 'Entreprise' in copied and ('Apple' in copied or 'démonstration' in copied or 'Microsoft' in copied), copied
            assert page.locator('#panel').get_attribute('aria-hidden') == 'false'
            page.keyboard.press('Escape')
            page.wait_for_timeout(350)
            page.evaluate("window.copiedText = ''")

        page.locator('.nav-item[data-view="calendrier"]').click()
        page.locator('[data-act="cal-event"][data-id="local-test"]').first.click()
        page.locator('[data-act="ask-ai-event"]').first.click()
        copied = copy_preview()
        assert 'Entretien de test' in copied and 'Préparer les projets data' in copied
        page.evaluate("window.copiedText = ''")
        page.locator('#usage-pill').click()
        page.wait_for_selector('#usage-pop .usage-card')
        assert page.locator('#usage-pop .usage-card').count() == 2
        page.keyboard.press('Escape')
        page.keyboard.press('Control+k')
        page.locator('#palette-q').fill('Préparer mon suivi')
        page.keyboard.press('Enter')
        assert 'Contexte :' in copy_preview()
        page.keyboard.press('6')
        assert 'active' in page.locator('.nav-item[data-view="calendrier"]').get_attribute('class')
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        page.locator('#usage-pill').evaluate('(el) => el.click()')
        page.wait_for_selector('#usage-pop .usage-card')
        page.wait_for_timeout(500)
        assert page.locator('#usage-pop .usage-card').count() == 2
        output = ROOT / 'build' / 'ui'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'copy-ai-usage-mobile.png'))
        page.keyboard.press('Escape')
        page.locator('[data-act="cal-event"][data-id="local-test"]').first.click()
        page.locator('[data-act="ask-ai-event"]').first.click()
        page.wait_for_timeout(400)
        field = page.locator('#ai-prompt-text')
        # Editing the preview changes exactly what is copied; the footer stays visible.
        edited = field.input_value() + '\n\nMa précision personnelle : privilégie les exemples concrets.'
        field.fill(edited)
        button = page.locator('[data-form="ai-prompt"] button[type="submit"]')
        bounds = button.bounding_box()
        assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 844, bounds
        assert page.locator('.ai-prompt-box').evaluate('(el) => el.scrollWidth <= el.clientWidth')
        page.screenshot(path=str(output / 'ai-prompt-mobile.png'))
        button.click()
        assert page.evaluate('window.copiedText') == edited
        # Clipboard-denied fallback is still available in the same dialog.
        page.evaluate("""() => {
            navigator.clipboard.writeText = async () => { throw new Error('denied'); };
            document.execCommand = command => { window.copyFallback = command; return true; };
        }""")
        button.click()
        assert page.evaluate('window.copyFallback') == 'copy'
        assert not ai_requests, ai_requests
        assert not errors, errors
        browser.close()
        print('OK: structured prompt dialogs, explicit copy, editing, clipboard fallback, mobile footer, usage and navigation; no chat requests.')


if __name__ == '__main__':
    run()

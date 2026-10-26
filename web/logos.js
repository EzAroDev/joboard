/* Company marks: known domains first, exact-name lookup otherwise.
 * Only company names are sent to the public autocomplete service.
 * Missing/ambiguous brands retain their initials; logos never block a view.
 */
(() => {
  'use strict';
  const normalize = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
    .replace(/\b(sa|sas|inc|ltd|llc|plc|corp|corporation)\b/g, '').replace(/[^a-z0-9]/g, '');
  const domains = {
    accenture: 'accenture.com', airbus: 'airbus.com', airliquide: 'airliquide.com', amazon: 'amazon.com',
    apple: 'apple.com', axa: 'axa.com', bnp: 'bnpparibas.com', bnpparibas: 'bnpparibas.com',
    capgemini: 'capgemini.com', creditagricole: 'credit-agricole.com', danone: 'danone.com',
    dassaultsystemes: '3ds.com', deloitte: 'deloitte.com', ey: 'ey.com', google: 'google.com',
    hermes: 'hermes.com', kering: 'kering.com', loreal: 'loreal.com', lvmh: 'lvmh.com',
    microsoft: 'microsoft.com', natixis: 'natixis.com', orange: 'orange.com', publicis: 'publicisgroupe.com',
    pwc: 'pwc.com', safran: 'safran-group.com', saintgobain: 'saint-gobain.com',
    salesforce: 'salesforce.com', sanofi: 'sanofi.com', schneiderelectric: 'se.com',
    societegenerale: 'societegenerale.com', thales: 'thalesgroup.com', totalenergies: 'totalenergies.com',
    ubisoft: 'ubisoft.com', veolia: 'veolia.com', vinci: 'vinci.com'
  };
  // Enabled only after approval for company-name/domain lookups.
  const EXTERNAL_LOGOS_ENABLED = true;
  let localMarks = {};
  const manifestReady = fetch('/assets/company-logos.json').then(r => r.ok ? r.json() : {}).then(data => { localMarks = data; }).catch(() => {});
  const pending = new Map();
  let cache = {};
  try { cache = JSON.parse(localStorage.getItem('sc-company-domains') || '{}'); } catch (_) { /* optional cache */ }
  let active = 0;
  const queue = [];
  function pump() {
    while (active < 3 && queue.length) {
      active++;
      const job = queue.shift();
      job().finally(() => { active--; pump(); });
    }
  }
  async function domain(name) {
    await manifestReady;
    const key = normalize(name);
    if (localMarks[key]) return localMarks[key].domain;
    if (!EXTERNAL_LOGOS_ENABLED) return '';
    if (domains[key]) return Promise.resolve(domains[key]);
    const cached = cache[key];
    if (cached && Date.now() - cached.at < (cached.domain ? 30 : 1) * 86400000) return Promise.resolve(cached.domain);
    if (pending.has(key)) return pending.get(key);
    const promise = new Promise(resolve => {
      queue.push(async () => {
        let result = '';
        try {
          const res = await fetch(`https://autocomplete.clearbit.com/v1/companies/suggest?query=${encodeURIComponent(name)}`, { signal: AbortSignal.timeout(6000), referrerPolicy: 'no-referrer' });
          if (!res.ok) throw new Error('Company lookup unavailable');
          const matches = (await res.json()).filter(c => normalize(c.name) === key && /^[a-z0-9.-]+\.[a-z]{2,}$/i.test(c.domain));
          if (matches.length === 1) result = matches[0].domain;
          else {
            const branded = matches.filter(c => normalize(c.domain.split('.')[0]) === key);
            const preferred = branded.filter(c => c.domain.endsWith('.com'));
            if (preferred.length === 1) result = preferred[0].domain;
            else if (branded.length === 1) result = branded[0].domain;
          }
          cache[key] = { domain: result, at: Date.now() };
          try { localStorage.setItem('sc-company-domains', JSON.stringify(cache)); } catch (_) { /* optional cache */ }
        } catch (_) { /* retry on a later session, keep initials */ }
        resolve(result);
      });
      pump();
    });
    pending.set(key, promise);
    return promise;
  }
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    const el = entry.target;
    observer.unobserve(el);
    domain(el.dataset.companyLogo).then(host => {
      if (!host || !el.isConnected) return;
      const img = new Image();
      img.alt = '';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.onload = () => { el.append(img); el.classList.add('has-logo'); };
      img.onerror = () => { el.title = `Logo indisponible : ${el.dataset.companyLogo}`; };
      const local = localMarks[normalize(el.dataset.companyLogo)];
      img.src = local ? local.src : `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=64`;
    });
  }), { rootMargin: '100px' });
  function hydrate() {
    document.querySelectorAll('[data-company-logo]:not([data-logo-observed])').forEach(el => {
      el.dataset.logoObserved = 'true';
      observer.observe(el);
    });
  }
  new MutationObserver(hydrate).observe(document.body, { childList: true, subtree: true });
  hydrate();
})();

/**
 * MedHunt Content Script
 *
 * Injects the enrichment button and scrapes whatever candidate fields it can
 * find on the page. It deliberately reports WHAT it found so you can see
 * whether a failure is a scraping problem or a PDL problem.
 *
 * NOTE ON SELECTORS: job boards change their DOM constantly and each board's
 * markup differs. The selectors below are starting points, not verified
 * against live pages. Use the "show extracted data" panel to see what is
 * actually being picked up, then tighten the selectors for the specific pages
 * you work on. Bad scraping is the most common cause of "no match".
 */

console.log('MedHunt: content script loaded on', location.hostname);

const SITE = (() => {
  const h = location.hostname;
  if (h.includes('indeed')) return 'indeed';
  if (h.includes('linkedin')) return 'linkedin';
  if (h.includes('ziprecruiter')) return 'ziprecruiter';
  if (h.includes('facebook')) return 'facebook';
  if (h.includes('vivian')) return 'vivian';
  return 'unknown';
})();

function text(selectors) {
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      const t = el && el.textContent.trim();
      if (t) return t;
    } catch (e) { /* invalid selector, skip */ }
  }
  return '';
}

function splitName(full) {
  const parts = full.replace(/\s+/g, ' ').trim().split(' ');
  if (parts.length < 2) return { firstName: parts[0] || '', lastName: '' };
  return { firstName: parts.slice(0, -1).join(' '), lastName: parts[parts.length - 1] };
}

function splitLocation(loc) {
  // "Los Angeles, CA" / "Los Angeles, California, United States"
  const parts = loc.split(',').map(s => s.trim()).filter(Boolean);
  return { city: parts[0] || '', state: parts[1] || '' };
}

function extract() {
  const c = { source: SITE };

  // --- Name -----------------------------------------------------------
  const fullName = text([
    '[data-testid="candidate-name"]',
    '[data-testid="resume-name"]',
    'h1[class*="name"]',
    'h1'
  ]);
  Object.assign(c, splitName(fullName));

  // --- Location -------------------------------------------------------
  const loc = text([
    '[data-testid="candidate-location"]',
    '[data-testid="resume-location"]',
    '[class*="location"]',
    '[class*="Location"]'
  ]);
  Object.assign(c, splitLocation(loc));

  // --- Company / title ------------------------------------------------
  c.company = text([
    '[data-testid="current-employer"]',
    '[data-testid*="company"]',
    '[class*="company"]'
  ]);
  c.title = text([
    '[data-testid="current-title"]',
    '[class*="job-title"]',
    '[class*="headline"]'
  ]);

  // --- Profile URL (highest-value PDL input) --------------------------
  if (SITE === 'linkedin' && /\/in\//.test(location.pathname)) {
    c.profileUrl = 'linkedin.com/in/' + location.pathname.split('/in/')[1].split('/')[0];
  } else {
    const li = document.querySelector('a[href*="linkedin.com/in/"]');
    if (li) c.profileUrl = li.href.replace(/^https?:\/\/(www\.)?/, '').split('?')[0];
  }

  // --- Contact already on page ---------------------------------------
  const mail = document.querySelector('a[href^="mailto:"]');
  if (mail) c.email = mail.href.replace('mailto:', '').split('?')[0];
  const tel = document.querySelector('a[href^="tel:"]');
  if (tel) c.phone = tel.href.replace('tel:', '');

  return c;
}

// ============================================================================
// UI
// ============================================================================

function panel(html, borderColor) {
  document.getElementById('medhunt-panel')?.remove();
  const el = document.createElement('div');
  el.id = 'medhunt-panel';
  el.style.cssText = `
    position:fixed; bottom:70px; right:20px; width:300px; padding:14px;
    background:#fff; border:2px solid ${borderColor}; border-radius:8px;
    box-shadow:0 4px 16px rgba(0,0,0,.18); z-index:2147483647;
    font:13px/1.45 -apple-system,Segoe UI,Arial,sans-serif; color:#222;
  `;
  el.innerHTML = html;
  el.addEventListener('click', e => {
    const t = e.target;
    if (t.dataset && t.dataset.copy) {
      navigator.clipboard.writeText(t.dataset.copy);
      const old = t.textContent;
      t.textContent = 'copied';
      setTimeout(() => { t.textContent = old; }, 1200);
    }
  });
  document.body.appendChild(el);
  return el;
}

function row(label, value) {
  if (!value) return `<div style="margin:6px 0;color:#999">${label}: —</div>`;
  return `<div style="margin:6px 0"><strong>${label}</strong><br>
    <span data-copy="${value}" style="display:inline-block;margin-top:3px;padding:6px 8px;
    background:#f2f4f7;border-radius:4px;cursor:pointer;word-break:break-all">${value}</span></div>`;
}

function enrich() {
  const candidate = extract();
  console.log('MedHunt: extracted', candidate);

  const found = Object.entries(candidate)
    .filter(([k, v]) => v && k !== 'source')
    .map(([k]) => k);

  if (!candidate.firstName || !candidate.lastName) {
    panel(
      `<div style="font-weight:600;color:#c0392b;margin-bottom:8px">Couldn't read a name</div>
       <div style="color:#555">Found on page: ${found.join(', ') || 'nothing'}</div>
       <div style="color:#555;margin-top:8px">The selectors in content.js don't match this page.
       Open the console to see what was scraped.</div>`,
      '#c0392b'
    );
    return;
  }

  panel(
    `<div style="font-weight:600;color:#0066cc">Searching PDL…</div>
     <div style="color:#666;margin-top:6px;font-size:12px">Using: ${found.join(', ')}</div>`,
    '#0066cc'
  );

  chrome.runtime.sendMessage({ action: 'enrichCandidate', candidate }, res => {
    if (chrome.runtime.lastError) {
      panel(`<div style="color:#c0392b;font-weight:600">Extension error</div>
             <div style="margin-top:6px">${chrome.runtime.lastError.message}</div>`, '#c0392b');
      return;
    }

    if (!res) {
      panel(`<div style="color:#c0392b;font-weight:600">No response from background</div>
             <div style="margin-top:6px;font-size:12px">Check the service worker console:
             chrome://extensions → MedHunt → "service worker"</div>`, '#c0392b');
      return;
    }

    if (res.success) {
      panel(
        `<div style="font-weight:600;color:#1e8e3e;margin-bottom:6px">Match found</div>
         <div style="font-size:12px;color:#666;margin-bottom:8px">${res.name || ''} · ${res.location || ''}</div>
         ${row('Email', res.email)}
         ${row('Phone', res.phone)}
         <div style="margin-top:10px;font-size:11px;color:#888">
           likelihood ${res.likelihood}/10 · matched via ${res.strategy} · click a value to copy
         </div>`,
        '#1e8e3e'
      );
      chrome.runtime.sendMessage({ action: 'recordStat', success: !!(res.email || res.phone) });
    } else {
      panel(
        `<div style="font-weight:600;color:#c0392b;margin-bottom:6px">No match</div>
         <div style="color:#555">${res.error}</div>
         <div style="margin-top:8px;font-size:12px;color:#666">Sent: ${found.join(', ')}</div>`,
        '#c0392b'
      );
      chrome.runtime.sendMessage({ action: 'recordStat', success: false });
    }
  });
}

function addButton() {
  if (document.getElementById('medhunt-btn')) return;
  const btn = document.createElement('button');
  btn.id = 'medhunt-btn';
  btn.textContent = 'Get Email & Phone';
  btn.style.cssText = `
    position:fixed; bottom:20px; right:20px; padding:11px 18px;
    background:#0066cc; color:#fff; border:0; border-radius:6px;
    font:600 14px -apple-system,Segoe UI,Arial,sans-serif; cursor:pointer;
    z-index:2147483647; box-shadow:0 2px 10px rgba(0,0,0,.25);
  `;
  btn.addEventListener('click', enrich);
  document.body.appendChild(btn);
  console.log('MedHunt: button injected');
}

addButton();

// Job boards are SPAs — re-inject if the app wipes the DOM on navigation.
new MutationObserver(() => addButton())
  .observe(document.body, { childList: true, subtree: false });

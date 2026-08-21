/**
 * MedHunt Popup
 */

const els = {
  testBtn: document.getElementById('test-btn'),
  resetBtn: document.getElementById('reset-btn'),
  enriched: document.getElementById('enriched-count'),
  failed: document.getElementById('failed-count'),
  msg: document.getElementById('message-area')
};

const API_KEY = 'f47086602b33a0382b1f7d1605306d6f6c29793c2a5e524e5a6c15ceb58e3f1c';

document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  els.testBtn.addEventListener('click', testAPI);
  els.resetBtn.addEventListener('click', resetStats);
});

function loadStats() {
  chrome.storage.local.get(['stats'], items => {
    const s = items.stats || { enriched: 0, failed: 0 };
    els.enriched.textContent = s.enriched;
    els.failed.textContent = s.failed;
  });
}

async function testAPI() {
  els.testBtn.disabled = true;
  els.testBtn.textContent = 'Testing…';

  // A real enrich call with a valid parameter combination
  // (first_name + last_name + company satisfies PDL's minimum).
  const qs = new URLSearchParams({
    first_name: 'sean',
    last_name: 'thorne',
    company: 'people data labs',
    min_likelihood: '4'
  });

  try {
    const res = await fetch(
      `https://api.peopledatalabs.com/v5/person/enrich?${qs}`,
      { headers: { 'X-Api-Key': API_KEY, 'Accept': 'application/json' } }
    );

    if (res.status === 200) {
      const body = await res.json();
      show(`API key works. Test match returned (likelihood ${body.likelihood}/10).`, 'success');
    } else if (res.status === 404) {
      show('API key works — the test person just had no match. Ready to use.', 'success');
    } else if (res.status === 401) {
      show('401: API key rejected. Check the key in background.js.', 'error');
    } else if (res.status === 402) {
      show('402: PDL credits exhausted. Top up your PDL account.', 'error');
    } else if (res.status === 429) {
      show('429: rate limited. Wait a minute and retry.', 'error');
    } else {
      show(`${res.status}: ${await res.text()}`, 'error');
    }
  } catch (e) {
    show('Network error: ' + e.message, 'error');
  }

  els.testBtn.disabled = false;
  els.testBtn.textContent = 'Test API';
}

function resetStats() {
  chrome.storage.local.set({ stats: { enriched: 0, failed: 0 } }, () => {
    loadStats();
    show('Stats reset.', 'success');
  });
}

function show(text, type) {
  els.msg.innerHTML = '';
  const box = document.createElement('div');
  box.className = `message msg-${type}`;
  box.textContent = text;
  els.msg.appendChild(box);
}

// Stat recording is handled in the background worker; this listener is a
// fallback for when the popup happens to be open.
chrome.runtime.onMessage.addListener(req => {
  if (req.action === 'recordStat') {
    chrome.storage.local.get(['stats'], items => {
      const s = items.stats || { enriched: 0, failed: 0 };
      req.success ? s.enriched++ : s.failed++;
      chrome.storage.local.set({ stats: s }, loadStats);
    });
  }
});

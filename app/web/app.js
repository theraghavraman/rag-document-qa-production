(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  let access = null;
  let refreshToken = null;
  let currentUser = null;
  let freshSecret = '';
  function message(text) {
    const el = $('#notice'); el.textContent = text; el.classList.remove('hidden');
    setTimeout(() => el.classList.add('hidden'), 5000);
  }
  async function api(path, options = {}) {
    const headers = {...options.headers};
    if (access) headers.Authorization = `Bearer ${access}`;
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let res = await fetch(path, {...options, headers});
    if (res.status === 401 && access && refreshToken && path !== '/v1/auth/refresh') {
      try {
        const renewed = await fetch('/v1/auth/refresh', {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({refresh_token:refreshToken})});
        if (renewed.ok) {
          const fresh = await renewed.json(); access = fresh.access_token;
          refreshToken = fresh.refresh_token;
          res = await fetch(path, {...options, headers:{...headers, Authorization:`Bearer ${access}`}});
        }
      } catch(_) { /* Caller will show the authentication failure. */ }
    }
    const payload = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) {
      if (res.status === 401 && access) logout();
      throw new Error((payload && typeof payload.detail === 'string') ? payload.detail : `Request failed (${res.status})`);
    }
    return payload;
  }
  function logout() {
    access = null; refreshToken = null; currentUser = null; freshSecret = '';
    $('#shell').classList.add('hidden'); $('#login-view').classList.remove('hidden');
    $('#login-form').reset();
  }
  async function signInTokens(tokens) {
    access = tokens.access_token; refreshToken = tokens.refresh_token;
    currentUser = await api('/v1/auth/me');
    $('#identity').textContent = `${currentUser.email} · ${currentUser.role}`;
    $('#workspace-name').textContent = currentUser.workspace_name;
    $('#workspace-plan').textContent = currentUser.plan;
    $('#login-view').classList.add('hidden'); $('#activation-view').classList.add('hidden');
    $('#signup-view').classList.add('hidden'); $('#shell').classList.remove('hidden');
    showTab('documents');
  }
  function showTab(tab) {
    document.querySelectorAll('.page').forEach(x => x.classList.add('hidden'));
    $(`#tab-${tab}`).classList.remove('hidden');
    document.querySelectorAll('[data-tab]').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
    $('#section-title').textContent = {'documents':'Documents','ask':'Ask a question','usage':'Usage & billing','team':'Team & API keys'}[tab];
    if (tab === 'documents') refreshDocuments();
    if (tab === 'usage') refreshUsage();
    if (tab === 'team' && ['owner', 'admin'].includes(currentUser.role)) refreshKeys();
  }
  function td(text, className) { const node = document.createElement('td'); node.textContent = text; if (className) node.className = className; return node; }
  function time(value) { return new Date(value).toLocaleString(); }
  async function refreshDocuments() {
    try {
      const result = await api('/v1/documents?limit=100');
      const body = $('#documents-body'); body.replaceChildren();
      if (!result.documents.length) { const tr = document.createElement('tr'); const cell = td('No documents yet. Upload one to get started.'); cell.colSpan = 4; tr.append(cell); body.append(tr); }
      result.documents.forEach(doc => {
        const tr = document.createElement('tr');
        tr.append(td(doc.filename, 'file-name'));
        const status = document.createElement('td'); const pill = document.createElement('span');
        pill.className = `pill ${doc.status}`; pill.textContent = doc.status; status.append(pill); tr.append(status);
        tr.append(td(time(doc.updated_at)));
        const actions = document.createElement('td');
        const revisions = document.createElement('button'); revisions.className = 'quiet'; revisions.textContent = 'Versions';
        revisions.addEventListener('click', async () => {
          try { const versions = await api(`/v1/documents/${doc.id}/versions`); message(versions.map(v => `v${v.version_number}: ${v.status}`).join(' · ') || 'No versions'); } catch(e) { message(e.message); }
        }); actions.append(revisions);
        if (['admin', 'owner', 'member'].includes(currentUser.role)) {
          const uploadVersion = document.createElement('button'); uploadVersion.className = 'quiet';
          uploadVersion.textContent = 'Update';
          const picker = document.createElement('input'); picker.type = 'file';
          picker.accept = '.pdf,.docx,.txt,.md'; picker.classList.add('hidden');
          uploadVersion.addEventListener('click', () => picker.click());
          picker.addEventListener('change', async () => {
            if (!picker.files.length) return;
            const data = new FormData(); data.append('file', picker.files[0]);
            uploadVersion.disabled = true;
            try { await api(`/v1/documents/${doc.id}/versions`, {method:'POST', body:data});
              message('New revision queued; active version remains available'); refreshDocuments(); }
            catch(e) { message(e.message); } finally { uploadVersion.disabled = false; }
          });
          actions.append(uploadVersion, picker);
          const remove = document.createElement('button'); remove.className = 'quiet'; remove.textContent = 'Delete';
          remove.addEventListener('click', async () => {
            if (!confirm(`Delete ${doc.filename}? This cannot be undone.`)) return;
            try { await api(`/v1/documents/${doc.id}`, {method:'DELETE'}); await refreshDocuments(); message('Document deleted'); } catch(e) {message(e.message);}
          }); actions.append(remove);
        }
        tr.append(actions); body.append(tr);
      });
    } catch(e) {message(e.message);}
  }
  async function refreshUsage() {
    try {
      const data = await api('/v1/workspace/usage'); $('#workspace-plan').textContent = data.plan;
      const grid = $('#usage-grid'); grid.replaceChildren();
      [['Questions', 'queries'], ['Uploads', 'uploads'], ['Documents', 'documents'], ['Storage', 'storage_bytes']].forEach(([label, key]) => {
        const card = document.createElement('div'); card.className = 'stat';
        const title = document.createElement('small'); title.textContent = label;
        const amount = document.createElement('b'); const limit = document.createElement('small');
        amount.textContent = key === 'storage_bytes' ? `${(data.usage[key] / 1048576).toFixed(1)} MB` : data.usage[key].toLocaleString();
        limit.textContent = key === 'storage_bytes' ? `of ${(data.limits.bytes / 1048576).toFixed(0)} MB` : `of ${data.limits[key].toLocaleString()}`;
        card.append(title, amount, limit); grid.append(card);
      });
    } catch(e) {message(e.message);}
  }
  async function refreshKeys() {
    try {
      const keys = await api('/v1/auth/keys'); const list = $('#keys-list'); list.replaceChildren();
      keys.forEach(key => {
        const line = document.createElement('div'); line.style.padding = '10px 0'; line.style.borderBottom = '1px solid #edf0f3';
        const span = document.createElement('span'); span.textContent = `${key.name} · ${key.prefix} · ${key.role} ${key.revoked_at ? '(revoked)' : ''}`; line.append(span);
        if (!key.revoked_at) {
          const btn = document.createElement('button'); btn.textContent = 'Revoke'; btn.className = 'quiet';
          btn.addEventListener('click', async () => {if (confirm('Revoke this API key?')) {await api(`/v1/auth/keys/${key.id}`, {method:'DELETE'}); refreshKeys();}});
          line.append(btn);
        } list.append(line);
      });
    } catch(e) {message(e.message);}
  }
  $('#login-form').addEventListener('submit', async e => {
    e.preventDefault(); $('#login-error').textContent = '';
    const form = new FormData(e.currentTarget);
    try {
      const tokens = await api('/v1/auth/login', {method:'POST', body:JSON.stringify(Object.fromEntries(form))});
      await signInTokens(tokens);
    } catch(err) {$('#login-error').textContent = err.message;}
  });
  $('#logout').addEventListener('click', async () => {
    try {if (refreshToken) await api('/v1/auth/logout', {method:'POST', body:JSON.stringify({refresh_token: refreshToken})});}
    catch(_) {} logout();
  });
  $('#signup-link').addEventListener('click', e => {e.preventDefault(); $('#login-view').classList.add('hidden'); $('#signup-view').classList.remove('hidden');});
  $('#signup-form').addEventListener('submit', async e => {
    e.preventDefault(); const body = JSON.stringify(Object.fromEntries(new FormData(e.currentTarget)));
    try {const answer = await api('/v1/auth/register/start', {method:'POST', body});
      $('#signup-result').style.color = '#23724d'; $('#signup-result').textContent = answer.message;
    } catch(err) {$('#signup-result').textContent = err.message;}
  });
  const params = new URLSearchParams(window.location.search);
  const verification = params.get('verify'); const invitation = params.get('invite');
  if (verification || invitation) {
    $('#login-view').classList.add('hidden'); $('#activation-view').classList.remove('hidden');
    if (verification) {$('#activation-title').textContent = 'Verify your email';
      $('#activation-copy').textContent = 'Confirm your workspace registration to sign in.';
      $('#activation-password-label').classList.add('hidden'); $('#activation-password').required = false;}
    // Do not retain onboarding token in browser history after the page has loaded.
    history.replaceState(null, '', '/app');
    $('#activation-form').addEventListener('submit', async e => {
      e.preventDefault();
      try {const tokens = await api(verification ? '/v1/auth/register/verify' : '/v1/auth/invitations/accept', {
        method:'POST', body:JSON.stringify(verification ? {token: verification} :
          {token:invitation, password:$('#activation-password').value})});
        await signInTokens(tokens);
      } catch(err) {$('#activation-error').textContent = err.message;}
    });
  }
  document.querySelectorAll('[data-tab]').forEach(btn => btn.addEventListener('click', () => showTab(btn.dataset.tab)));
  $('#refresh-documents').addEventListener('click', refreshDocuments);
  $('#refresh-keys').addEventListener('click', refreshKeys);
  $('#upload-form').addEventListener('submit', async e => {
    e.preventDefault(); const btn = e.currentTarget.querySelector('button'); btn.disabled = true;
    try { const body = new FormData(); body.append('file', $('#upload-file').files[0]);
      await api('/v1/documents', {method:'POST', body}); $('#upload-form').reset(); message('Upload accepted; indexing in progress'); refreshDocuments(); }
    catch(err) {message(err.message);} finally {btn.disabled = false;}
  });
  $('#query-form').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.currentTarget.querySelector('button'); button.disabled = true;
    try {const data = await api('/v1/qa/query', {method:'POST', body:JSON.stringify({question:$('#question').value, top_k:5})});
      $('#answer-panel').classList.remove('hidden'); $('#answer-text').textContent = data.answer;
      $('#answer-grounded').textContent = data.grounded ? 'Citation references validated (not a factual guarantee)' : 'Insufficient verified citations';
      const root = $('#answer-sources'); root.replaceChildren();
      data.sources.forEach(source => {const article=document.createElement('div'); article.className='source';
        const name=document.createElement('strong'); name.textContent=`[${source.source_id}] ${source.filename} · Page ${source.page ?? 'N/A'}`;
        const excerpt=document.createElement('p'); excerpt.textContent=source.snippet; article.append(name,excerpt); root.append(article);});
    } catch(err) {message(err.message);} finally {button.disabled = false;}
  });
  $('#key-form').addEventListener('submit', async e => {
    e.preventDefault(); try {const result=await api('/v1/auth/keys', {method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.currentTarget)))});
      freshSecret=result.api_key; $('#new-secret').textContent=freshSecret; $('#secret-panel').classList.remove('hidden'); refreshKeys();}
    catch(err) {message(err.message);}
  });
  $('#copy-secret').addEventListener('click', async () => {try {await navigator.clipboard.writeText(freshSecret); message('Copied');} catch(e) {message('Clipboard unavailable');}});
  $('#invite-form').addEventListener('submit', async e => {
    e.preventDefault(); try {const result=await api('/v1/auth/invitations', {method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.currentTarget)))});
      $('#invite-token').textContent=result.delivery === 'email' ? 'Invitation sent by email.' : result.invitation_token; $('#invite-panel').classList.remove('hidden');}
    catch(err) {message(err.message);}
  });
  async function billing(path) {try {const result=await api(path,{method:'POST'}); if(result.url && new URL(result.url).protocol === 'https:') window.location.href=result.url;
    else message('Billing redirect unavailable');} catch(e) {message(e.message);}}
  $('#upgrade-starter').addEventListener('click', () => billing('/v1/billing/checkout/starter'));
  $('#upgrade-business').addEventListener('click', () => billing('/v1/billing/checkout/business'));
  $('#billing-portal').addEventListener('click', () => billing('/v1/billing/portal'));
  setInterval(() => {if (access && !$('#tab-documents').classList.contains('hidden')) refreshDocuments();}, 12000);
})();

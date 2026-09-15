'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const isReset = location.pathname === '/reset-password';
  let token = isReset ? new URLSearchParams(location.hash.slice(1)).get('token') || '' : '';
  let busy = false;
  // Keep the link secret out of history, storage, subsequent URLs, and logs.
  if (isReset && (location.hash || location.search)) history.replaceState(null, '', location.pathname);

  function error(message) {
    $('recovery-error').textContent = message;
    $('recovery-error').hidden = !message;
  }
  async function post(action, data) {
    // Refresh CSRF after a restart, sign-out, or an expired session without
    // reloading the page and losing the in-memory reset token.
    const session = await fetch('/api/session', {cache:'no-store'});
    if (!session.ok) throw Error('Cannot reach Sentleaf. Please try again.');
    let csrf;
    try {csrf = (await session.json()).csrf;}
    catch {throw Error('Sentleaf is temporarily unavailable. Please try again.');}
    const response = await fetch('/api/password-reset/' + action, {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify(data)
    });
    let result;
    try {result = await response.json();}
    catch {throw Error('Sentleaf is temporarily unavailable. Please try again.');}
    if (!response.ok) throw Error(result.error || 'This request could not be completed. Please try again.');
    return result;
  }
  function result(title, message, kind) {
    $('recovery-title').textContent = title;
    $('recovery-intro').hidden = true;
    $('request-form').hidden = true;
    $('reset-form').hidden = true;
    $('recovery-loading').hidden = true;
    $('recovery-result').hidden = false;
    $('recovery-result').classList.toggle('invalid-link', kind === 'invalid');
    document.querySelector('.recovery-mark').textContent = kind === 'invalid' ? '!' : '✓';
    $('recovery-message').textContent = message;
    $('try-another').hidden = kind !== 'sent';
    $('request-another').hidden = kind !== 'invalid';
    $('reset-signin').hidden = kind !== 'complete';
  }
  function invalidLink() {
    token = '';
    $('reset-form').reset();
    error('');
    result('Let’s get a fresh link.', 'This reset link is missing, expired, or already used. Request a new one to continue.', 'invalid');
  }
  function requestForm() {
    $('recovery-title').textContent = 'Let’s get you back in.';
    $('recovery-intro').hidden = false;
    $('recovery-intro').textContent = 'Use the email address you sign into Sentleaf with.';
    $('recovery-result').hidden = true;
    $('recovery-loading').hidden = true;
    $('request-form').hidden = false;
    error('');
  }
  $('try-another').onclick = () => {requestForm();$('request-email').focus();};
  $('request-form').onsubmit = async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;error('');$('request-submit').disabled = true;$('request-submit').textContent = 'Requesting link…';
    try {
      const response = await post('request', {email:$('request-email').value});
      result('Check your inbox.', response.message, 'sent');
    } catch (e) {error(e.message);}
    finally {busy = false;$('request-submit').disabled = false;$('request-submit').textContent = 'Send reset link';}
  };
  $('reset-form').onsubmit = async event => {
    event.preventDefault();
    if (busy) return;
    error('');
    const password = $('new-password').value, confirmation = $('confirm-new-password').value;
    if (password !== confirmation) {error('Passwords do not match.');return;}
    if (password.length < 12 || password.length > 256) {error('Use between 12 and 256 characters for your password.');return;}
    busy = true;$('reset-submit').disabled = true;$('reset-submit').textContent = 'Saving password…';
    try {
      await post('complete', {token,password,confirmation});
      token = '';$('reset-form').reset();
      result('You’re ready to sign in.', 'Your password has been changed and previous sign-in sessions have ended. Sign in with your new password.', 'complete');
    } catch (e) {
      if (/reset link is invalid or has expired/.test(e.message)) invalidLink();
      else error(e.message);
    } finally {busy = false;$('reset-submit').disabled = false;$('reset-submit').textContent = 'Save new password';}
  };
  $('show-new-password').onclick = () => {
    const show = $('new-password').type === 'password';
    $('new-password').type = show ? 'text' : 'password';
    $('show-new-password').textContent = show ? 'Hide' : 'Show';
    $('show-new-password').setAttribute('aria-label', show ? 'Hide new password' : 'Show new password');
    $('show-new-password').setAttribute('aria-pressed', String(show));
  };
  window.addEventListener('pagehide', () => {token = '';$('reset-form').reset();});
  window.addEventListener('pageshow', event => {if (event.persisted && isReset) invalidLink();});
  async function start() {
    if (!isReset) {requestForm();return;}
    $('recovery-title').textContent = 'Choose a new password.';
    $('recovery-intro').textContent = 'A fresh password for your Sentleaf account.';
    if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {invalidLink();return;}
    try {
      const response = await post('check', {token});
      if (!response.valid) {invalidLink();return;}
      $('recovery-loading').hidden = true;
      $('reset-form').hidden = false;
    } catch (e) {
      $('recovery-loading').hidden = true;
      // Keep the token in memory: a temporary network error must not discard it.
      error(e.message);
      $('reset-form').hidden = false;
    }
  }
  start();
})();

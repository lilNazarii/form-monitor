'use strict';
const form = document.getElementById('inquiry-form');
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('send-request');
  const status = document.getElementById('form-message');
  button.disabled = true; status.textContent = 'Saving your request…'; status.className = '';
  try {
    const session = await fetch('/api/session');
    if (!session.ok) throw Error('The website is temporarily unavailable. Please try again.');
    const {csrf} = await session.json();
    const response = await fetch('/api/inquiries', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({kind:document.getElementById('kind').value,
        email:document.getElementById('email').value, website:document.getElementById('website').value,
        message:document.getElementById('message').value, company_fax:document.getElementById('company-fax').value,
        consent:document.getElementById('consent').checked})
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Could not save the request. Please try again.');
    status.textContent = result.message; status.className = 'contact-success'; form.reset();
  } catch (error) { status.textContent = error.message; status.className = 'contact-error'; }
  finally { button.disabled = false; }
});

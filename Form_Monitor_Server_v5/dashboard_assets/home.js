'use strict';
if (/^#(?:monitors|history|notifications|connections|billing(?:\/return)?|connect|monitor\/[a-f0-9]{32})$/.test(location.hash)) {
  location.replace('/workspace' + location.hash);
}
const example=document.getElementById('example-check');
document.querySelectorAll('[data-example]').forEach(button=>button.addEventListener('click',()=>{
 const failed=button.dataset.example==='failure';
 example.classList.toggle('is-failed',failed);
 document.querySelectorAll('[data-example]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));
 document.getElementById('example-title').textContent=failed?'Needs attention.':'Delivered.';
 document.getElementById('example-step-title').textContent=failed?'No matching email found':'Email found in Gmail Inbox';
 document.getElementById('example-step-description').textContent=failed?'The inbox check could not confirm delivery.':'The matching test message arrived.';
 document.getElementById('example-step-icon').textContent=failed?'!':'✓';
 document.getElementById('example-result').lastElementChild.textContent=failed?'Delivery needs a closer look':'Submission and delivery confirmed';
 document.getElementById('example-seal').innerHTML=failed?'<svg viewBox="0 0 32 32"><path d="M16 9v9m0 5h.01"/></svg>':'<svg viewBox="0 0 32 32"><path d="m8 16 5 5 11-12"/></svg>';
}));

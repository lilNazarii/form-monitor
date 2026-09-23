'use strict';
// One catalog feeds both the public offer and authenticated workspace.
const SentleafPlans = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = plan => new Intl.NumberFormat('en-IE', {style:'currency', currency:plan.currency, maximumFractionDigits:0}).format(plan.amount / 100);
  function cards(catalog, current, owner = false) {
    return `<div class="plans-grid">${catalog.plans.map(plan => {
      const selected = current === plan.id;
      const canBuy = catalog.checkout_ready && !owner;
      return `<article class="plan-card ${plan.id === 'solo' ? 'plan-primary' : ''}">
        <div class="plan-heading"><h3>${escape(plan.name)}</h3><span class="pill">${selected ? 'YOUR PLAN' : plan.monitors === 1 ? 'ONE IMPORTANT FORM' : 'A SMALL PORTFOLIO'}</span></div>
        <p class="plan-price">${escape(money(plan))}<span>/ month</span></p>
        <p class="plan-description">${plan.monitors === 1 ? 'Keep an eye on the form your business relies on.' : 'Keep your client forms together in one workspace.'}</p>
        <ul class="plan-features"><li><strong>${plan.monitors} ${plan.monitors === 1 ? 'monitored form' : 'monitored forms'}</strong>${plan.monitors > 1 ? ', across your sites' : ', on one site'}</li><li>Scheduled checks every ${plan.interval / 60} hours</li><li>Real submission + Gmail Inbox confirmation</li><li>Problem and recovery email alerts</li><li>${plan.monitors * 6} check cycles per day, including manual checks</li></ul>
        ${current === undefined ? `<a class="button ${plan.id === 'solo' ? '' : 'secondary'}" href="${canBuy ? "/workspace#billing" : "/contact"}">${canBuy ? 'Choose ' + escape(plan.name) : 'Request early access'} <span aria-hidden="true">↗</span></a>` : `<button class="button ${plan.id === 'solo' ? '' : 'secondary'}" data-action="billing-checkout" data-id="${escape(plan.id)}" ${!canBuy || selected ? 'disabled' : ''}>${owner ? 'Included for the owner' : selected ? 'Current plan' : canBuy ? 'Choose ' + escape(plan.name) : 'Payments opening soon'}</button>`}
      </article>`;
    }).join('')}</div>`;
  }
  return {cards, money, escape};
})();

async function loadPublicPlans() {
  const target = document.getElementById('public-plans');
  if (!target) return;
  try {
    const response = await fetch('/api/plans');
    if (!response.ok) throw Error('unavailable');
    const catalog = await response.json();
    target.innerHTML = SentleafPlans.cards(catalog);
    document.getElementById('pricing-status').textContent = catalog.checkout_ready
      ? (catalog.test_mode ? 'Payment testing is in progress. No live payments are available.' : 'Monthly subscriptions. No setup fee. Cancel from your workspace.')
      : 'Planned launch prices. Payments are not open yet; access is by invitation.';
  } catch {
    target.textContent = 'Plans are temporarily unavailable. Please refresh the page.';
  }
}
loadPublicPlans();

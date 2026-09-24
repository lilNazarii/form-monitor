'use strict';
const SentleafPlans = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = plan => new Intl.NumberFormat('en-IE', {style:'currency', currency:plan.currency, maximumFractionDigits:0}).format(plan.amount / 100);
  function cards(catalog, current, owner = false) {
    return `<div class="plans-grid">${catalog.plans.map(plan => {
      const selected = current === plan.id, studio = plan.id === 'studio';
      const canBuy = catalog.checkout_ready && !owner;
      return `<article class="plan-card ${studio ? 'plan-primary' : ''}">
        <div class="plan-heading"><h3>${escape(plan.name)}</h3><span class="pill">${selected ? 'YOUR PLAN' : studio ? 'FOR CLIENT WORK' : 'FOR ONE WEBSITE'}</span></div>
        <p class="plan-price">${escape(money(plan))}<span>/ month</span></p>
        <p class="plan-description">${studio ? 'A clearer view across your client websites.' : 'Everything you need to check one important form.'}</p>
        <ul class="plan-features"><li><strong>${plan.monitors} monitored ${studio ? 'forms across sites' : 'form'}</strong></li><li>Real submission + Gmail delivery check</li><li>Failure and recovery email alerts</li><li>Checks every ${plan.interval / 60} hours or less often</li><li>${plan.monitors * 6} check cycles per day</li><li class="premium-feature">${plan.history_days} days of check history</li>${studio ? '<li class="premium-feature">Client and site groups</li><li class="premium-feature">Weekly reports in your workspace</li><li class="premium-feature">CSV history export</li>' : ''}</ul>
        ${current === undefined ? '<a class="button '+(studio?'':'secondary')+'" href="/trial">Start 7-day free trial</a>' : '<button class="button '+(studio?'':'secondary')+'" data-action="billing-checkout" data-id="'+escape(plan.id)+'" '+(!canBuy||selected?'disabled':'')+'>'+(owner?'Included for the owner':selected?'Current plan':canBuy?'Choose '+escape(plan.name):'Payments opening soon')+'</button>'}
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
    document.getElementById('pricing-status').textContent = catalog.checkout_ready && !catalog.test_mode
      ? 'Monthly subscriptions. Cancel from your workspace.'
      : 'Launch prices shown. Payments are not open yet; trial access is reviewed.';
  } catch { target.textContent = 'Plans are temporarily unavailable. Please refresh the page.'; }
}
loadPublicPlans();

// Shared license grace-period banner. Included on every dashboard page.
// Shows a persistent warning while the license is expired-but-in-grace so an
// admin renews before the app locks down. No-op in every other state.
(function () {
  async function showGraceBannerIfNeeded() {
    let notice;
    try {
      const r = await fetch('/api/license/notice');
      if (!r.ok) return;
      notice = await r.json();
    } catch (e) {
      return;
    }
    if (!notice || notice.state !== 'grace') return;

    const days = notice.grace_days_left;
    const daysText = (days === 1) ? '1 day' : `${days} days`;

    const bar = document.createElement('div');
    bar.id = 'license-grace-banner';
    bar.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2000',
      'background:#b45309', 'color:#fff', 'padding:10px 16px',
      'font-size:14px', 'font-weight:600', 'text-align:center',
      'box-shadow:0 2px 8px rgba(0,0,0,0.3)'
    ].join(';');
    bar.innerHTML =
      '⚠ License expired — running in a grace period with ' + daysText +
      ' left. <a href="/license" style="color:#fff;text-decoration:underline;">Renew now</a> to avoid a lockdown.';
    document.body.appendChild(bar);
    // Nudge the app content down so the fixed bar doesn't cover it.
    document.body.style.paddingTop = bar.offsetHeight + 'px';
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', showGraceBannerIfNeeded);
  } else {
    showGraceBannerIfNeeded();
  }
})();

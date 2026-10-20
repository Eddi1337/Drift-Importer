// One touch-friendly phone menu; desktop keeps its compact More disclosure.
(() => {
  const header = document.querySelector('.topbar');
  const toggle = document.getElementById('navToggle');
  const nav = document.getElementById('primaryNav');
  if (!header || !toggle || !nav) return;
  const phone = window.matchMedia('(max-width: 900px)');
  const more = nav.querySelector('.nav-more');
  const label = document.getElementById('navToggleText');
  function setOpen(open, restoreFocus = false) {
    header.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    label.textContent = open ? 'Close' : 'Menu';
    if (!open && more) more.open = false;
    if (restoreFocus) toggle.focus();
  }
  toggle.hidden = false;
  header.classList.add('nav-ready');
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  nav.addEventListener('click', event => {
    if (event.target.closest('a')) setOpen(false);
  });
  document.addEventListener('pointerdown', event => {
    if (!header.contains(event.target)) setOpen(false);
    else if (more && !more.contains(event.target)) more.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (phone.matches && header.classList.contains('nav-open')) {
      setOpen(false, true);
      event.preventDefault();
    } else if (more?.open) {
      more.open = false;
      more.querySelector('summary').focus();
      event.preventDefault();
    }
  });
  nav.addEventListener('focusout', event => {
    if (event.relatedTarget && !header.contains(event.relatedTarget)) setOpen(false);
  });
  phone.addEventListener('change', () => setOpen(false));
})();

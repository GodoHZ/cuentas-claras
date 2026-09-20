/* Finanzas — lo mínimo de JavaScript: la hoja de apuntar, el aviso, el gráfico
   y el service worker. Todo lo demás lo hacen el servidor y HTMX. */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);

  // ------------------------------------------------ hoja de apuntar (botón +)

  function quickForm() { return $('#txform-qa'); }

  function applyType(form, type) {
    const radio = form.querySelector('input[name="tipo"][value="' + type + '"]');
    if (radio) { radio.checked = true; onTypeChange(form); }
  }

  function onTypeChange(form) {
    const type = (form.querySelector('input[name="tipo"]:checked') || {}).value || 'gasto';
    // deseleccionar lo que ya no se ve, para no mandar una categoría de otro tipo
    form.querySelectorAll('input[name="categoria_id"]:checked').forEach((input) => {
      const group = input.closest('[data-show]');
      if (!group || !group.dataset.show.includes(type === 'ingreso' ? 'ingreso' : 'gasto')) input.checked = false;
      if (type === 'aporte_sobre' || type === 'retiro_sobre') input.checked = false;
    });
    if (type === 'aporte_sobre' || type === 'retiro_sobre') {
      const none = form.querySelector('input[name="sobre_id"][value=""]');
      if (none && none.checked) none.checked = false;
    }
    if (!form.dataset.cuentaTocada) {
      const wanted = (type === 'aporte_sobre' || type === 'retiro_sobre')
        ? form.dataset.cuentaSobres : form.dataset.cuentaGeneral;
      const account = form.querySelector('input[name="cuenta_id"][value="' + wanted + '"]');
      if (account) account.checked = true;
    }
  }

  function openQuickAdd(options) {
    const dialog = $('#qa');
    const form = quickForm();
    if (!dialog || !form) return false;
    if (options && options.tipo) applyType(form, options.tipo);
    if (options && options.sobre) {
      const env = form.querySelector('input[name="sobre_id"][value="' + options.sobre + '"]');
      if (env) env.checked = true;
    }
    if (options && options.categoria) {
      const cat = form.querySelector('input[name="categoria_id"][value="' + options.categoria + '"]');
      if (cat) cat.checked = true;
    }
    if (!dialog.open) dialog.showModal();
    const amount = form.querySelector('input[name="importe"]');
    if (amount) { amount.focus(); amount.select(); }   // en el mismo gesto: iOS abre el teclado
    return true;
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-qa]');
    if (opener) {
      let options = {};
      try { options = JSON.parse(opener.dataset.qa || '{}'); } catch (e) { /* vacío */ }
      if (openQuickAdd(options)) event.preventDefault();
      return;
    }
    if (event.target.closest('[data-close]')) {
      const dialog = $('#qa');
      if (dialog && dialog.open) dialog.close();
      return;
    }
    const dateButton = event.target.closest('[data-fecha]');
    if (dateButton) {
      const form = dateButton.closest('form');
      const input = form && form.querySelector('input[name="fecha"]');
      if (input) {
        const day = new Date();
        day.setDate(day.getDate() + parseInt(dateButton.dataset.fecha, 10));
        input.value = day.getFullYear() + '-' + String(day.getMonth() + 1).padStart(2, '0') + '-' +
                      String(day.getDate()).padStart(2, '0');
      }
    }
  });

  document.addEventListener('change', (event) => {
    const form = event.target.form;
    if (!form || !form.classList.contains('txform')) return;
    if (event.target.name === 'tipo') onTypeChange(form);
    if (event.target.name === 'cuenta_id') form.dataset.cuentaTocada = '1';
  });

  // ------------------------------------------------ aviso flotante

  let toastTimer;
  function toast(text) {
    const box = $('#toast');
    if (!box) return;
    box.textContent = text;
    box.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.remove('on'), 3200);
  }

  document.body.addEventListener('txGuardado', (event) => {
    const dialog = $('#qa');
    if (dialog && dialog.open) dialog.close();
    toast(event.detail.value || 'Apuntado');
    if (window.htmx) {
      htmx.ajax('GET', location.pathname + location.search,
                { target: '#main', select: '#main', swap: 'outerHTML' });
    }
  });

  // ------------------------------------------------ gráfico (solo donde hace falta)

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function drawCharts() {
    const canvas = document.querySelector('canvas[data-chart]');
    if (!canvas || canvas.dataset.drawn) return;
    const source = document.getElementById(canvas.id + '-data');
    if (!source) return;
    if (!window.Chart) {
      if (!document.getElementById('chartjs')) {
        const tag = document.createElement('script');
        tag.id = 'chartjs';
        tag.src = '/static/js/chart.umd.min.js';
        tag.onload = drawCharts;
        document.head.appendChild(tag);
      }
      return;
    }
    const data = JSON.parse(source.textContent);
    const ink = cssVar('--ink-2') || '#52514e';
    const grid = cssVar('--line') || '#e1e0d9';
    const surface = cssVar('--surface') || '#fff';
    canvas.dataset.drawn = '1';
    new Chart(canvas, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: data.series.map((s) => ({
          label: s.name,
          data: s.data,
          backgroundColor: cssVar('--s' + s.slot),
          borderColor: surface,
          borderWidth: { top: 0, bottom: 0, left: 1, right: 1 },
          borderRadius: { topLeft: 4, topRight: 4 },
          borderSkipped: 'bottom',
        })),
      },
      options: {
        responsive: true,
        categoryPercentage: 0.9,
        barPercentage: 0.95,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => item.dataset.label + ': ' +
                item.parsed.y.toLocaleString('es-ES', { style: 'currency', currency: 'EUR' }),
            },
          },
        },
        scales: {
          x: { grid: { display: false }, border: { color: grid }, ticks: { color: ink, font: { size: 11 } } },
          y: {
            beginAtZero: true, border: { display: false },
            grid: { color: grid, drawTicks: false },
            ticks: { color: ink, font: { size: 11 }, maxTicksLimit: 5,
                     callback: (value) => value.toLocaleString('es-ES') + ' €' },
          },
        },
      },
    });
  }

  document.addEventListener('DOMContentLoaded', drawCharts);
  document.body.addEventListener('htmx:afterSettle', drawCharts);

  // ------------------------------------------------ PWA

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
  }
})();

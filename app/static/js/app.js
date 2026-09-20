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
    const uid = form.querySelector('input[name="uid"]');
    if (uid) uid.value = nuevoUid();
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

  // ------------------------------------------------ gestos, como en una app de iPhone

  // El objetivo de un evento no siempre es un elemento (puede ser el documento).
  function elementoDe(evento) {
    const t = evento.target;
    return t && typeof t.closest === 'function' ? t : null;
  }

  // Deslizar un movimiento hacia la izquierda deja a la vista el botón de borrar.
  (function deslizarParaBorrar() {
    let fila = null, x0 = 0, y0 = 0, dx = 0, decidido = '';

    function cerrarTodo(salvo) {
      document.querySelectorAll('.tx-wrap.abierto').forEach((w) => {
        if (w !== salvo) w.classList.remove('abierto');
      });
    }

    document.addEventListener('touchstart', (e) => {
      const t = elementoDe(e);
      const w = t && t.closest('.tx-wrap');
      cerrarTodo(w);
      if (!w) return;
      fila = w; x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; dx = 0; decidido = '';
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
      if (!fila) return;
      dx = e.touches[0].clientX - x0;
      const dy = e.touches[0].clientY - y0;
      if (!decidido) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        decidido = Math.abs(dx) > Math.abs(dy) ? 'lado' : 'scroll';   // el scroll manda
        if (decidido === 'lado') fila.classList.add('arrastrando');
      }
      if (decidido !== 'lado') return;
      const abierto = fila.classList.contains('abierto');
      const desde = abierto ? -92 : 0;
      const pos = Math.max(-92, Math.min(0, desde + dx));
      fila.querySelector('.tx').style.transform = 'translateX(' + pos + 'px)';
    }, { passive: true });

    document.addEventListener('touchend', () => {
      if (!fila) return;
      const tx = fila.querySelector('.tx');
      if (decidido === 'lado') {
        const pos = parseFloat((tx.style.transform.match(/-?[\d.]+/) || [0])[0]);
        fila.classList.toggle('abierto', pos < -46);
      }
      tx.style.transform = '';
      fila.classList.remove('arrastrando');
      fila = null; decidido = '';
    });

    // si la fila está abierta, el primer toque solo la cierra
    document.addEventListener('click', (e) => {
      const t = elementoDe(e);
      const w = t && t.closest('.tx-wrap.abierto');
      if (w && !t.closest('.tx-borrar')) { e.preventDefault(); w.classList.remove('abierto'); }
    }, true);
  })();

  // Tirar hacia abajo desde arriba del todo para actualizar los números.
  (function tirarParaActualizar() {
    const UMBRAL = 70;
    let y0 = null, tirando = false;

    function aviso() { return $('#tirar'); }

    document.addEventListener('touchstart', (e) => {
      const main = $('#main');
      const t = elementoDe(e);
      const enDialogo = t && t.closest('dialog');
      y0 = (main && main.scrollTop <= 0 && !enDialogo) ? e.touches[0].clientY : null;
      tirando = false;
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
      if (y0 === null) return;
      const dy = e.touches[0].clientY - y0;
      if (dy > 12 && !tirando) { tirando = true; }
      if (!tirando) return;
      const caja = aviso();
      if (!caja) return;
      const avance = Math.min(dy, UMBRAL + 30);
      caja.classList.add('visible');
      caja.style.transform = 'translate(-50%, ' + Math.min(avance - 56, 8) + 'px)';
      caja.querySelector('span').textContent = dy > UMBRAL ? 'Suelta para actualizar' : 'Tira para actualizar';
    }, { passive: true });

    document.addEventListener('touchend', (e) => {
      const caja = aviso();
      if (y0 === null || !tirando || !caja) { y0 = null; return; }
      const dy = (e.changedTouches[0] || {}).clientY - y0;
      if (dy > UMBRAL) {
        caja.classList.add('girando');
        caja.querySelector('span').textContent = 'Actualizando…';
        const fin = () => { caja.classList.remove('visible', 'girando'); caja.style.transform = ''; };
        if (window.htmx) {
          htmx.ajax('GET', location.pathname + location.search,
                    { target: '#main', select: '#main', swap: 'outerHTML' }).then(fin, fin);
        } else { location.reload(); }
      } else {
        caja.classList.remove('visible');
        caja.style.transform = '';
      }
      y0 = null; tirando = false;
    });
  })();

  // ------------------------------------------------ apuntar sin conexión
  //
  // Si al guardar no hay red, el movimiento se queda en el móvil y se envía solo
  // cuando vuelve. Cada uno lleva un identificador propio, así que si el envío
  // se repite el servidor lo reconoce y no lo duplica.

  const COLA = 'finanzas-pendientes';

  function leerCola() {
    try { return JSON.parse(localStorage.getItem(COLA) || '[]'); } catch (e) { return []; }
  }

  function guardarCola(cola) {
    try { localStorage.setItem(COLA, JSON.stringify(cola)); } catch (e) { /* sin sitio */ }
    pintarPendientes();
  }

  function pintarPendientes() {
    const boton = $('#pendientes');
    if (!boton) return;
    const cola = leerCola();
    boton.hidden = cola.length === 0;
    const texto = $('#pendientes-texto');
    if (texto) {
      texto.textContent = cola.length === 1
        ? '1 movimiento sin enviar · toca para reintentar'
        : cola.length + ' movimientos sin enviar · toca para reintentar';
    }
  }

  function encolar(form) {
    const datos = {};
    new FormData(form).forEach((valor, clave) => { datos[clave] = valor; });
    if (!datos.uid) datos.uid = nuevoUid();
    const cola = leerCola();
    cola.push({ datos: datos, cuando: Date.now() });
    guardarCola(cola);
  }

  function nuevoUid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'uid-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
  }

  async function enviarPendientes(avisar) {
    const cola = leerCola();
    if (!cola.length) return;
    const quedan = [];
    let enviados = 0;
    for (const item of cola) {
      try {
        const respuesta = await fetch('/movimientos/nuevo', {
          method: 'POST',
          headers: { 'HX-Request': 'true' },
          body: new URLSearchParams(item.datos),
        });
        const guardado = (respuesta.headers.get('HX-Trigger') || '').indexOf('txGuardado') >= 0;
        if (respuesta.ok && guardado) { enviados++; continue; }
        if (respuesta.ok) { item.problema = true; quedan.push(item); continue; }  // el servidor lo rechaza
        quedan.push(item);
      } catch (e) {
        quedan.push(item);                          // sigue sin haber red: se queda para luego
      }
    }
    guardarCola(quedan);
    if (enviados && avisar !== false) {
      toast(enviados === 1 ? 'Enviado el movimiento que tenías pendiente'
                           : 'Enviados ' + enviados + ' movimientos pendientes');
      if (window.htmx) {
        htmx.ajax('GET', location.pathname + location.search,
                  { target: '#main', select: '#main', swap: 'outerHTML' });
      }
    }
    if (quedan.some((i) => i.problema)) {
      toast('Algo de lo pendiente no se pudo guardar: ábrelo y revísalo');
    }
  }

  document.body.addEventListener('htmx:sendError', (event) => {
    const form = event.target.closest && event.target.closest('.txform');
    if (!form) return;                              // solo el formulario de apuntar
    encolar(form);
    const dialog = $('#qa');
    if (dialog && dialog.open) dialog.close();
    toast('Sin conexión: lo he guardado en el móvil y lo enviaré solo');
  });

  document.addEventListener('click', (event) => {
    if (event.target.closest('#pendientes')) enviarPendientes(true);
  });

  window.addEventListener('online', () => enviarPendientes(true));
  document.addEventListener('DOMContentLoaded', () => { pintarPendientes(); enviarPendientes(false); });
  document.body.addEventListener('htmx:afterSettle', pintarPendientes);

  // ------------------------------------------------ PWA

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
  }
})();

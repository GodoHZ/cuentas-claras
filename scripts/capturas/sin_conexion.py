"""Qué pasa cuando no hay red: la cola de pendientes y las pantallas guardadas.

WebKit (el motor de Safari) revienta al navegar sin red con service worker —es
una limitación de Playwright, no de la app—, así que la cola se prueba en WebKit
cortando solo la petición de guardar, y el service worker se prueba en Chromium.
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
notas = []

with sync_playwright() as p:
    # ---------------------------------------- 1) la cola, en WebKit
    nav = p.webkit.launch()
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                          has_touch=True, locale="es-ES")
    pag = ctx.new_page()
    pag.on("pageerror", lambda e: notas.append("error JS: " + str(e)))
    pag.goto(BASE + "/movimientos", wait_until="networkidle")

    # cortar la red de verdad: con un service worker por medio, interceptar la
    # petición desde Playwright no llega a la app. Sin navegar mientras tanto.
    ctx.set_offline(True)
    pag.click(".fab")
    pag.fill('#txform-qa input[name="importe"]', "9,99")
    pag.click('#txform-qa label.chip:has-text("Ocio")')
    pag.click('button[form="txform-qa"]')
    pag.wait_for_timeout(1200)
    notas.append("avisa al usuario: " + pag.locator("#toast").inner_text())
    notas.append("la hoja se ha cerrado: " + str(not pag.is_visible("dialog#qa[open]")))
    notas.append("sale el aviso de pendientes: " +
                 (pag.locator("#pendientes-texto").inner_text() if pag.locator("#pendientes").is_visible() else "NO"))
    notas.append("guardado en el móvil: " + str(pag.evaluate(
        "JSON.parse(localStorage.getItem('finanzas-pendientes') || '[]').length")))
    pag.screenshot(path="/out/pendiente.png")

    ctx.set_offline(False)                                            # vuelve la red
    pag.evaluate("window.dispatchEvent(new Event('online'))")
    pag.wait_for_timeout(2500)
    notas.append("tras volver la red quedan pendientes: " + str(pag.evaluate(
        "JSON.parse(localStorage.getItem('finanzas-pendientes') || '[]').length")))
    pag.goto(BASE + "/movimientos", wait_until="networkidle")
    notas.append("el movimiento aparece una sola vez: " +
                 str(pag.locator('.tx:has-text("9,99")').count() == 1))

    # reenviar dos veces lo mismo no debe duplicar
    pag.evaluate("""() => {
        const item = {datos: {origen: 'dialogo', uid: 'prueba-doble', tipo: 'gasto', importe: '3,33',
                              categoria_id: document.querySelector('#txform-qa input[name=categoria_id]').value,
                              sobre_id: '', cuenta_id: document.querySelector('#txform-qa input[name=cuenta_id]').value,
                              fecha: '2026-09-19', concepto: 'Reenvío'}, cuando: Date.now()};
        localStorage.setItem('finanzas-pendientes', JSON.stringify([item, item]));
        window.dispatchEvent(new Event('online')); }""")
    pag.wait_for_timeout(2500)
    pag.goto(BASE + "/movimientos", wait_until="networkidle")
    notas.append("enviado dos veces, aparece una sola vez: " +
                 str(pag.locator('.tx:has-text("3,33")').count() == 1))
    nav.close()

    # ---------------------------------------- 2) el service worker, en Chromium
    nav = p.chromium.launch()
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, locale="es-ES")
    pag = ctx.new_page()
    pag.goto(BASE + "/", wait_until="networkidle")
    pag.wait_for_timeout(1500)
    notas.append("service worker activo: " + str(pag.evaluate("!!navigator.serviceWorker.controller")))
    pag.goto(BASE + "/movimientos", wait_until="networkidle")
    pag.wait_for_timeout(500)

    ctx.set_offline(True)
    pag.goto(BASE + "/movimientos", wait_until="domcontentloaded")
    pag.wait_for_timeout(600)
    notas.append("sin red, la pantalla se abre igual: " + str(pag.locator(".tx").count() > 0))
    aviso = pag.locator(".sin-conexion")
    notas.append("y avisa: " + (aviso.inner_text() if aviso.count() else "NO AVISA"))
    pag.screenshot(path="/out/sin-conexion.png", full_page=False)

    pag.goto(BASE + "/deudas", wait_until="domcontentloaded")         # una que no había visitado
    pag.wait_for_timeout(600)
    notas.append("una pantalla nunca vista cae en la página de sin conexión: " +
                 str("Sin conexión" in pag.title() or "conexión" in pag.content()))
    nav.close()

open("/out/sin_conexion.txt", "w").write("\n".join(notas))
